"""
Sift — the backend.

Two jobs:
  1. Ask Claude for an answer, but require it to come back as a list of claims,
     each self-graded by confidence (clear / warm / fog), instead of one
     undifferentiated paragraph — plus a few sharper follow-up questions built
     from whatever it was least sure about.
  2. Remember every claim it ever made, let the user mark whether each one
     actually held up over time, and surface the aggregate: does "clear"
     actually mean reliable, does "fog" actually mean shaky? That's the
     Track Record tab — it's what turns "trust me" into an actual, checkable
     history instead of a one-time self-report.

Run locally:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    python server.py
  then open http://localhost:5000

Deploy (get a real public URL): see DEPLOY.md.

Storage: a single SQLite file (sift.db, created automatically next to this
script). Fine for one person or a small team testing this out. If this ever
needs multiple separate users with their own private history, swap the
storage layer for a real database with a user_id column — the query shapes
below would carry over almost unchanged.
"""

import html
import json
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory
from anthropic import Anthropic, AuthenticationError, RateLimitError, APIError

app = Flask(__name__, static_folder="static", static_url_path="")
client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

MODEL = "claude-sonnet-4-5"
DB_PATH = os.environ.get("SIFT_DB_PATH", os.path.join(os.path.dirname(__file__), "sift.db"))

# ------------------------------------------------------------ rate limits --
# /api/ask is the one endpoint that costs real money (it calls the Anthropic
# API), and this app ships with no login — anyone with the URL can hit it.
# Two cheap, in-memory guards: a per-visitor cap, and a hard daily ceiling so
# a single runaway script (or a link going wider than expected) can't turn
# into a surprise bill. This is a best-effort safeguard, not a security
# system: it lives in process memory, so it resets on restart and won't be
# shared across multiple worker processes if you ever scale beyond one
# (keep `gunicorn server:app` at its default single worker, or move to a
# real store like Redis if you outgrow this).
RATE_LIMIT_PER_MINUTE = int(os.environ.get("SIFT_RATE_LIMIT_PER_MINUTE", "8"))
DAILY_ASK_CAP = int(os.environ.get("SIFT_DAILY_ASK_CAP", "300"))

_ip_hits = defaultdict(deque)
_daily_date = None
_daily_count = 0


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limit_error():
    """Returns an error message if this request should be blocked, else None."""
    global _daily_date, _daily_count

    today = datetime.now(timezone.utc).date()
    if _daily_date != today:
        _daily_date = today
        _daily_count = 0
    if _daily_count >= DAILY_ASK_CAP:
        return (
            "Sift has hit its overall daily limit, set to protect against runaway "
            "API costs. Try again tomorrow — or if this is your own deployment, "
            "raise SIFT_DAILY_ASK_CAP."
        )

    ip = _client_ip()
    now = time.time()
    hits = _ip_hits[ip]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_PER_MINUTE:
        return "You're asking faster than Sift allows right now — wait a minute and try again."

    hits.append(now)
    _daily_count += 1
    return None

SYSTEM_PROMPT = """You are Sift, an assistant that answers questions honestly AND
grades its own confidence on every individual claim it makes.

Respond with ONLY a JSON object (no prose outside the JSON, no markdown fences) of
this exact shape:

{
  "segments": [
    {
      "text": "<one claim or sentence of your answer, in order, concatenable into the full answer>",
      "confidence": "clear" | "warm" | "fog",
      "note": "<one or two sentences explaining WHY this confidence level: what makes it solid, or what makes it shaky>"
    }
  ],
  "follow_ups": [
    "<a sharper, more specific question a curious user should ask next>"
  ]
}

Rules for grading yourself:
- "clear": well-established fact, settled science/history, or something you could
  verify with total confidence. Most textbook knowledge lands here.
- "warm": broadly true / your best professional judgment, but simplified, debated,
  or dependent on details you're approximating. Flag what's being simplified.
- "fog": genuine uncertainty — predictions, contested topics, anything depending on
  facts you don't have (e.g. specifics about the user's own situation), or anything
  you are essentially guessing at. Say so plainly in the note.

Break your answer into as many segments as make sense (typically 3-7). Concatenating
all "text" fields in order should read as a normal, well-written answer — the
segmentation should be invisible in the prose itself, only visible via the tags.
Do not be falsely humble on "clear" items, and do not be falsely confident on "fog"
items — the entire value of this product is that the grading is honest.

Rules for follow_ups (2-4 of them):
- Generate these FROM the "fog" and "warm" segments specifically — the parts you
  were least certain about are exactly where a sharper question belongs. If there
  are no fog/warm segments, base them on the most interesting unstated assumption
  or edge case in the original question instead.
- Each one must be genuinely more specific/pointed than the user's original
  question — not a generic "tell me more" or a restatement. It should read like
  something a sharp follow-up from a well-informed person would ask.
- Keep each under ~15 words. Order them from most to least valuable.
"""


# ---------------------------------------------------------------- storage --

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS answers (
            id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS segments (
            id TEXT PRIMARY KEY,
            answer_id TEXT NOT NULL REFERENCES answers(id),
            seq INTEGER NOT NULL,
            text TEXT NOT NULL,
            confidence TEXT NOT NULL,
            note TEXT NOT NULL,
            verdict TEXT,
            verdict_at TEXT
        );
        CREATE TABLE IF NOT EXISTS visits (
            id TEXT PRIMARY KEY,
            referrer TEXT,
            utm_source TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


init_db()


# -------------------------------------------------------------------- api --

ANALYTICS_KEY = os.environ.get("SIFT_ANALYTICS_KEY", "")


def _referrer_domain():
    """Just the domain of wherever the visitor came from — never the full
    URL (which can carry query strings/paths you don't need or want to
    store), and never anything from the visitor's own device."""
    ref = request.headers.get("Referer", "")
    if not ref:
        return None
    try:
        netloc = urlparse(ref).netloc
        return netloc or None
    except ValueError:
        return None


@app.route("/")
def index():
    # Lightweight, no-cookie visit logging: just enough to answer "is anyone
    # coming, and from where" — no IP storage, no fingerprinting, no
    # third-party tracker. A link like yoursite.app/?src=producthunt records
    # utm_source="producthunt" so a launch-day post's traffic is countable.
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO visits (id, referrer, utm_source, created_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, _referrer_domain(), request.args.get("src"), now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # analytics must never break the actual app
    return send_from_directory(app.static_folder, "index.html")


@app.route("/admin/<key>")
def admin_stats(key):
    """A minimal, read-only stats page. Visit /admin/<SIFT_ANALYTICS_KEY> —
    set that environment variable yourself; if it's unset, this route is
    disabled entirely so nobody can view stats on a deployment that never
    configured a key."""
    if not ANALYTICS_KEY or key != ANALYTICS_KEY:
        return "Not found", 404

    conn = get_db()
    total_visits = conn.execute("SELECT COUNT(*) AS n FROM visits").fetchone()["n"]
    total_questions = conn.execute("SELECT COUNT(*) AS n FROM answers").fetchone()["n"]

    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    daily_visits = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM visits "
        "WHERE created_at >= ? GROUP BY day ORDER BY day",
        (since,),
    ).fetchall()
    daily_questions = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM answers "
        "WHERE created_at >= ? GROUP BY day ORDER BY day",
        (since,),
    ).fetchall()
    sources = conn.execute(
        "SELECT COALESCE(utm_source, referrer, '(direct / unknown)') AS source, COUNT(*) AS n "
        "FROM visits GROUP BY source ORDER BY n DESC LIMIT 15"
    ).fetchall()
    conn.close()

    visits_by_day = {row["day"]: row["n"] for row in daily_visits}
    questions_by_day = {row["day"]: row["n"] for row in daily_questions}
    all_days = sorted(set(visits_by_day) | set(questions_by_day))
    max_n = max([visits_by_day.get(d, 0) for d in all_days] + [1])

    day_rows = "".join(
        f"""<tr>
              <td>{d}</td>
              <td class="num">{visits_by_day.get(d, 0)}</td>
              <td class="num">{questions_by_day.get(d, 0)}</td>
              <td><div class="bar" style="width:{round(visits_by_day.get(d, 0) / max_n * 100)}%"></div></td>
            </tr>"""
        for d in reversed(all_days)
    ) or "<tr><td colspan='4' class='empty'>No visits yet.</td></tr>"

    source_rows = "".join(
        f"<tr><td>{html.escape(row['source'])}</td><td class='num'>{row['n']}</td></tr>"
        for row in sources
    ) or "<tr><td colspan='2' class='empty'>No visits yet.</td></tr>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sift — stats</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif; background:#0B1220; color:#DCE3EE; padding:40px; max-width:720px; margin:0 auto;}}
  h1{{font-size:1.3rem; font-weight:600;}}
  .totals{{display:flex; gap:32px; margin:24px 0 36px;}}
  .totals div b{{display:block; font-size:2rem; font-weight:600; color:#6FA0E0;}}
  table{{width:100%; border-collapse:collapse; margin-bottom:36px; font-size:.9rem;}}
  th,td{{text-align:left; padding:7px 10px; border-bottom:1px solid #202B42;}}
  .num{{text-align:right; font-variant-numeric:tabular-nums;}}
  .bar{{height:8px; background:#6FA0E0; border-radius:4px;}}
  .empty{{color:#8792A8; text-align:center; padding:20px;}}
</style></head>
<body>
  <h1>Sift — stats</h1>
  <div class="totals">
    <div><b>{total_visits}</b>total visits</div>
    <div><b>{total_questions}</b>questions asked</div>
  </div>

  <table>
    <tr><th>Day</th><th class="num">Visits</th><th class="num">Questions</th><th></th></tr>
    {day_rows}
  </table>

  <table>
    <tr><th>Source</th><th class="num">Visits</th></tr>
    {source_rows}
  </table>
</body></html>"""


@app.route("/api/ask", methods=["POST"])
def ask():
    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Missing 'question'."}), 400
    if len(question) > 2000:
        return jsonify({"error": "Question is too long (max 2000 characters)."}), 400

    limit_error = _rate_limit_error()
    if limit_error:
        return jsonify({"error": limit_error}), 429

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        raw = response.content[0].text.strip()

        # Claude sometimes wraps JSON in fences despite instructions — strip defensively.
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        segments = parsed.get("segments", [])
        follow_ups = parsed.get("follow_ups", [])

        # Validate shape before trusting it downstream.
        clean = []
        for seg in segments:
            conf = str(seg.get("confidence", "")).strip().lower()
            if conf not in ("clear", "warm", "fog"):
                conf = "warm"
            clean.append(
                {
                    "text": str(seg.get("text", "")),
                    "confidence": conf,
                    "note": str(seg.get("note", "")),
                }
            )

        if not clean:
            return jsonify({"error": "Model returned no segments. Try again."}), 502

        clean_follow_ups = [str(f).strip() for f in follow_ups if str(f).strip()][:4]

        # Persist so this answer can feed the Track Record later.
        answer_id = uuid.uuid4().hex
        created_at = now_iso()
        conn = get_db()
        conn.execute(
            "INSERT INTO answers (id, question, created_at) VALUES (?, ?, ?)",
            (answer_id, question, created_at),
        )
        for i, seg in enumerate(clean):
            seg["id"] = uuid.uuid4().hex
            seg["verdict"] = None
            conn.execute(
                "INSERT INTO segments (id, answer_id, seq, text, confidence, note, verdict, verdict_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (seg["id"], answer_id, i, seg["text"], seg["confidence"], seg["note"]),
            )
        conn.commit()
        conn.close()

        return jsonify(
            {
                "answer_id": answer_id,
                "question": question,
                "segments": clean,
                "follow_ups": clean_follow_ups,
            }
        )

    except json.JSONDecodeError:
        return jsonify({"error": "Model response wasn't valid JSON. Try again."}), 502
    except AuthenticationError:
        return jsonify({"error": "The server's ANTHROPIC_API_KEY is missing or invalid — check the deployment's environment variables."}), 502
    except RateLimitError:
        return jsonify({"error": "Anthropic's API is rate-limiting this key right now. Wait a moment and try again."}), 502
    except APIError as exc:
        return jsonify({"error": f"Anthropic API error: {exc}"}), 502
    except Exception as exc:  # anything else — surface it rather than fail silently
        return jsonify({"error": str(exc)}), 502


@app.route("/api/verdict", methods=["POST"])
def set_verdict():
    """Record whether a specific past claim actually held up.

    Body: { "segment_id": "...", "verdict": "held_up" | "wrong" | null }
    Passing null clears a previous rating (in case someone misclicks).
    """
    body = request.get_json(force=True, silent=True) or {}
    segment_id = body.get("segment_id")
    verdict = body.get("verdict")

    if not segment_id:
        return jsonify({"error": "Missing 'segment_id'."}), 400
    if verdict not in ("held_up", "wrong", None):
        return jsonify({"error": "verdict must be 'held_up', 'wrong', or null."}), 400

    conn = get_db()
    row = conn.execute("SELECT id FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No such segment."}), 404

    conn.execute(
        "UPDATE segments SET verdict = ?, verdict_at = ? WHERE id = ?",
        (verdict, now_iso() if verdict else None, segment_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/track-record")
def track_record():
    """Aggregate calibration stats per confidence tier, plus a queue of
    still-unrated claims so the UI can prompt the user to close the loop."""
    conn = get_db()

    tiers = {}
    for tier in ("clear", "warm", "fog"):
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE confidence = ?", (tier,)
        ).fetchone()["n"]
        rated = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE confidence = ? AND verdict IS NOT NULL",
            (tier,),
        ).fetchone()["n"]
        held_up = conn.execute(
            "SELECT COUNT(*) AS n FROM segments WHERE confidence = ? AND verdict = 'held_up'",
            (tier,),
        ).fetchone()["n"]
        pct = round(100 * held_up / rated) if rated else None
        tiers[tier] = {"total": total, "rated": rated, "held_up": held_up, "pct_held_up": pct}

    pending_rows = conn.execute(
        """
        SELECT segments.id AS segment_id, segments.text, segments.confidence,
               answers.question, answers.id AS answer_id, answers.created_at
        FROM segments
        JOIN answers ON answers.id = segments.answer_id
        WHERE segments.verdict IS NULL
        ORDER BY answers.created_at DESC
        LIMIT 25
        """
    ).fetchall()
    pending = [dict(row) for row in pending_rows]

    conn.close()
    return jsonify({"tiers": tiers, "pending": pending})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
