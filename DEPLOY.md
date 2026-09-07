# Getting Sift live at a real URL

This is a normal Flask app — one backend file, one static frontend file. Any host that runs Python works. Fastest path below.

## 1. Get an Anthropic API key

https://console.anthropic.com/ → **API Keys** → create one. Copy it — you'll paste it into whichever host you pick below. (This is separate from a claude.ai subscription — it's pay-as-you-go, and this app is cheap to run: each answer costs a fraction of a cent.)

## 2. Deploy — Render.com (free tier, ~5 minutes, no credit card)

1. Push this folder to a new GitHub repo (or use Render's "Deploy from a public repo" with the files as-is).
2. On https://render.com → **New +** → **Web Service** → connect the repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn server:app`
4. Under **Environment**, add a variable: `ANTHROPIC_API_KEY` = your key from step 1.
5. Deploy. Render gives you a URL like `https://sift-xyz.onrender.com` — that's it, live.

**Before you share the link publicly:** the app has no login, so anyone with the URL can ask it questions on your API key's dime. Two safeguards are already built in and on by default — a per-visitor cap (8 questions/minute) and a hard daily ceiling (300 questions/day total) — both overridable via environment variables if you need to raise or lower them:

- `SIFT_RATE_LIMIT_PER_MINUTE` (default `8`)
- `SIFT_DAILY_ASK_CAP` (default `300`)

These are a best-effort guard, not a security system — they live in the app's memory, so keep the deployment at a single worker/instance (the default), and if this ever gets popular enough to need real scale, swap them for a proper rate limiter backed by Redis or similar.

(Free tier sleeps after inactivity and takes ~30s to wake on the next visit. Paid tier removes that if it matters for a promo link you're sharing.)

**About the database:** Sift stores every answer and its confidence grades in a local SQLite file (`sift.db`, created automatically) so the Track Record tab has something to calibrate against. On most hosts' free tiers the filesystem is ephemeral — a redeploy can wipe it. That's fine for testing; if you want the Track Record history to survive redeploys long-term, mount a persistent disk (Render's paid tiers support this, as do Railway/Fly volumes) and point `SIFT_DB_PATH` at a file on it.

## Alternatives

- **Railway** (railway.app) — same idea, drag-and-drop or GitHub-connect, set `ANTHROPIC_API_KEY`, done.
- **Fly.io** — `fly launch` in this folder, then `fly secrets set ANTHROPIC_API_KEY=...`.
- **Vercel** — works too but needs a `vercel.json` adapting Flask to serverless functions; Render/Railway are more direct for this app.

## Running it locally first (recommended before deploying)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-your-key-here
python server.py
```

Then open http://localhost:5000 and try it.

## It's already installable as an app on phones

Sift is a PWA (Progressive Web App) — once it's deployed, opening the URL on a phone and choosing **"Add to Home Screen"** (Safari: Share → Add to Home Screen; Android Chrome: menu → Install app) puts a real Sift icon on the home screen. It opens full-screen, no browser bar, works like a native app — because that's genuinely a good app icon and manifest doing their job, not a trick.

What this *isn't*: a listing in the App Store or Google Play. That's a separate, much bigger project — an Apple Developer account ($99/year), a Google Play account ($25 one-time), app review (days to weeks), and typically either wrapping this same web app in a native shell (Capacitor/Cocoon) or rebuilding natively (Swift/Kotlin/React Native). Worth doing later if this takes off and you want store presence — not something to block launch on now.

## Seeing where your visitors come from

Sift now logs a lightweight, no-cookie visit count on every homepage load — just a timestamp, the referring domain (if any), and a `src` value if the link had one, e.g. `https://your-url.replit.app/?src=producthunt`. No IP addresses, no fingerprinting, no third-party trackers.

To view it, set one more environment variable: `SIFT_ANALYTICS_KEY` (make up any hard-to-guess string). Then visit `https://your-url/admin/<that key>` — that page shows total visits and questions asked, a 14-day daily breakdown, and a ranked list of sources. If `SIFT_ANALYTICS_KEY` is never set, that route is disabled entirely (returns a plain 404), so it's safe by default.

Practical use: build a distinct link for every place you share Sift — `?src=linkedin`, `?src=producthunt`, `?src=friend-name` — and the stats page tells you which one actually brought people.

## What to change if you want to make it yours

- `server.py` → `SYSTEM_PROMPT`: this is the whole trick — it's what forces the model to grade its own confidence and generate follow-ups. Tune the tone here if you want it stricter, friendlier, or scoped to a topic (e.g. only about your book/app).
- `static/index.html` → the `<h1>`, `.lede`, and `footer` text, plus the CSS custom properties at the top (`--lamp`, `--clear`, `--warm`, `--fog`) if you want different brand colors.
- `MODEL` in `server.py` — swap to a different Claude model if you want a cheaper/faster or more capable one.
- The **Track Record** tab (calibration tracking) is genuinely new — no other AI app ships this. It only gets interesting with real usage: the more people rate past claims as "held up" or "turned out wrong," the more the percentages mean something. Worth mentioning to early users explicitly, since an empty Track Record tab undersells the idea.
