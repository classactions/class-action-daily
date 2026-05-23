# Deployment Runbook — Polling Mode (v1)

This is the v1 deployment: **Neon Postgres + Fly.io scheduled worker, no
webhooks.** Should take about 30 minutes start to finish.

The webhook server (`server/webhook_server.py`, `fly.toml` http_service
block) is staying in the repo as ready-to-enable code for when you
outgrow polling. Ignore those files for v1.

---

## Step 1 — Create the Neon database (5 min)

1. Go to **https://console.neon.tech** and sign up / log in.
2. **Create a project**:
   - Name: `class-action-daily`
   - Postgres version: 16 (default)
   - Region: **AWS US East (N. Virginia) — `aws-us-east-1`** (close to
     CourtListener's servers, low latency)
3. After creation, you land on the dashboard. Click **Connection Details**.
4. Copy the connection string. It looks like:
   ```
   postgresql://username:AbCdEf123XYZ@ep-cool-name-123456.us-east-1.aws.neon.tech/neondb?sslmode=require
   ```
5. **Save this somewhere safe** (password manager). You'll paste it as
   `DATABASE_URL` in step 3.

That's it for Neon. We're not creating tables yet — the migration script
does that.

---

## Step 2 — Run the schema migration (3 min)

You can do this from your laptop before anything is deployed.

```bash
cd class_action_tracker

# Set up Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt

# Migrate
export DATABASE_URL='postgresql://username:...@ep-...neon.tech/neondb?sslmode=require'
python -m server.worker migrate
```

Expected output:
```
Applying migration 0001_initial.sql...
Migrations complete.
```

Verify the tables landed:
```bash
python -c "
from server import db
with db.get_conn() as conn, conn.cursor() as cur:
    cur.execute(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\")
    for r in cur.fetchall(): print(r['tablename'])
"
```
Should print: `filings`, `law360_items`, `law360_matches`,
`schema_migrations`, `webhook_events`.

(The `webhook_events` table is harmless empty scaffolding for v1.)

---

## Step 3 — Test the worker locally end-to-end (5 min)

Before deploying, prove the pipeline works on your machine. This uses your
laptop's IP for the CourtListener API call, so no Fly involvement yet.

```bash
export DATABASE_URL='postgresql://...'   # same as before
export COURTLISTENER_TOKEN='your_cl_token_here'

python -m server.worker poll --days 1
```

Expected output (numbers will vary by day):
```
poll: inserted=23 updated=0 skipped=4
```

A non-zero `inserted` means real class action filings landed in Neon.
If you see `inserted=0`, double-check the token and try `--days 3` to
widen the search window.

Now (optionally) pull Law360 RSS. **Skip this step** if you haven't yet
filled in `law360_feeds.json` with your per-section RSS URLs — the
worker will skip Law360 gracefully.

```bash
python -m server.worker law360
```

Generate the static snapshot:
```bash
python -m server.worker export --out enriched.json --export-days 7
```

You should see something like `export: wrote 23 filings to enriched.json`.

Spot-check the UI:
```bash
python -m http.server 8000
# open http://localhost:8000 in a browser
```

You should see real filings, no banner, and the Internal/Public toggle
working. **If this all works, the hard part is over.**

---

## Step 4 — Deploy the worker to Fly.io as a scheduled machine (10 min)

The plan: build the same Docker image we already have, push it to
Fly's registry, and run it as a *scheduled* machine that wakes every
30 minutes, runs `python -m server.worker all`, and exits.

### 4a. Initialize the Fly app (without deploying)

```bash
cd class_action_tracker
fly launch --no-deploy --copy-config --name class-action-daily-XXXXX
```
Pick a unique suffix for `XXXXX` (Fly app names are globally unique).
Accept defaults. Decline the offer to provision Postgres — you have Neon.
Decline the offer to create an Upstash Redis.

This will modify your `fly.toml` slightly. **Critical:** open `fly.toml`
and **remove or comment out the `[http_service]` block and the `[[http_service.checks]]` block**.
Polling-mode workers don't accept HTTP, so leaving these in will make
Fly think the deploy failed because nothing is listening on port 8080.

Your final `fly.toml` should look like:

```toml
app = "class-action-daily-XXXXX"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  PYTHONPATH = "/app"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = 256
```

### 4b. Set secrets

```bash
fly secrets set \
  DATABASE_URL='postgresql://...your full Neon URL...' \
  COURTLISTENER_TOKEN='your_cl_token'
```

(If you have Law360 RSS URLs in `law360_feeds.json`, that file gets
baked into the Docker image. That's fine for now since the repo is
private; if you ever make the repo public, switch to a Fly secret.)

### 4c. Build and push the image

```bash
fly deploy --build-only --push
```

This builds the Docker image and pushes to Fly's registry without
trying to run it as a service. When done, you'll see something like:
```
==> Image: registry.fly.io/class-action-daily-XXXXX:deployment-01J...
```
Save that image tag.

### 4d. Create the scheduled machine

```bash
fly machine run \
  --schedule daily \
  --region iad \
  --restart no \
  registry.fly.io/class-action-daily-XXXXX:latest \
  python -m server.worker all
```

Wait — `--schedule daily` only runs once a day. Fly's built-in scheduler
supports `hourly`, `daily`, `weekly`, `monthly` — not every-30-min.

**You have two options here:**

#### Option A: Run hourly via Fly's scheduler (simpler)

Change `--schedule daily` to `--schedule hourly`. Class action filings
that lag 24-72h in RECAP don't benefit much from sub-hour refresh anyway.

```bash
fly machine run \
  --schedule hourly \
  --region iad \
  --restart no \
  registry.fly.io/class-action-daily-XXXXX:latest \
  python -m server.worker all
```

#### Option B: Use GitHub Actions for true 30-min cadence (more flexible)

If you want 30-min refreshes, GitHub Actions cron is the path. Skip the
Fly scheduled machine entirely, and add this to your repo at
`.github/workflows/refresh.yml`:

```yaml
name: Refresh Filings
on:
  schedule:
    - cron: '*/30 * * * *'    # every 30 min
  workflow_dispatch:

jobs:
  refresh:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r server/requirements.txt
      - run: python -m server.worker all
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          COURTLISTENER_TOKEN: ${{ secrets.COURTLISTENER_TOKEN }}
      - name: Commit enriched.json
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add enriched.json
          git diff --cached --quiet || git commit -m "refresh $(date -u +%FT%TZ)"
          git push
```

Set the same two secrets in your GitHub repo settings (Settings →
Secrets and variables → Actions). GitHub Actions is free for public repos
and gets 2,000 free minutes/month for private repos — at ~10 sec/run × 48
runs/day × 30 days = 4 hours/month, you stay free.

With Option B, you can skip the rest of step 4 (no Fly deploy needed).

### 4e. Verify the scheduled run (if using Fly)

```bash
fly machine list
# should show one machine with schedule="hourly"

fly logs
# wait until the next hour boundary, then watch
```

Look for output like:
```
poll: inserted=2 updated=21 skipped=3
law360: items_pulled=87 matches_made=4
export: wrote 178 filings to enriched.json
```

The `updated` count growing while `inserted` stays small means polling
is working — same dockets being re-seen, occasionally with new fields.

---

## Step 5 — Serve the front-end (5 min)

The front-end is just `index.html` + `enriched.json`. Three ways to host:

### 5a. GitHub Pages (recommended for internal use)

If you went with GitHub Actions in step 4, this is free and trivial.
The Action commits `enriched.json` back to the repo on every refresh,
and GitHub Pages serves whatever's on the branch.

1. Push your repo (private is fine — Pages still works on private repos
   with a GitHub Team or Enterprise plan; otherwise use a public repo
   *but* ensure `law360_feeds.json` and `.env*` are gitignored — they
   already are).
2. Repo Settings → Pages → Source: Deploy from a branch → `main` / root.
3. After ~2 min, your site is at `https://<username>.github.io/<repo>/`.

### 5b. Cloudflare Pages

Same idea, but the fetch happens on Cloudflare's side. Connect your
repo at https://dash.cloudflare.com/?to=/:account/pages, point at the
root directory, no build command needed.

### 5c. Behind firm SSO

If the front-end is internal-only (which is your default), use
**Cloudflare Access** (free for up to 50 users) to gate the site behind
your firm's Google Workspace / Okta / Azure AD identity. Setup is
~20 minutes; their docs walk you through it.

---

## Step 6 — Smoke test

1. Visit your hosted URL. You should see real filings with a recent
   `generated_at` timestamp in the masthead.
2. Toggle Internal / Public; verify Law360 chips appear / disappear if
   you have any L360 coverage.
3. Click a row. The drawer opens with full docket info. If
   `complaint_url` is set, the "View / Download Complaint (PDF)" button
   takes you straight to a real PACER document on CourtListener.

## Step 7 — Monitor for a week

Useful queries against Neon (run from `psql "$DATABASE_URL"` or the
Neon SQL editor in the dashboard):

```sql
-- How many filings per day, last 14 days, and how many flagged
-- as low-confidence (= future LLM candidates)?
SELECT date_filed,
       COUNT(*) AS filings,
       COUNT(*) FILTER (WHERE needs_llm_review) AS uncertain,
       ROUND(100.0 * COUNT(*) FILTER (WHERE needs_llm_review) / COUNT(*), 1) AS pct_uncertain
FROM filings
WHERE date_filed > CURRENT_DATE - INTERVAL '14 days'
GROUP BY date_filed
ORDER BY date_filed DESC;

-- Category breakdown
SELECT category,
       COUNT(*) AS n,
       ROUND(AVG(category_confidence)::numeric, 2) AS avg_conf
FROM filings
WHERE date_filed > CURRENT_DATE - INTERVAL '7 days'
GROUP BY category
ORDER BY n DESC;

-- Has the worker run recently? (Updated_at moves on every refresh)
SELECT MAX(updated_at) AS last_refresh,
       NOW() - MAX(updated_at) AS time_since_last
FROM filings;
```

The `pct_uncertain` column is the key number when you eventually flip
on the LLM categorizer: it tells you what fraction of filings are
keyword-classifier failures that the LLM will need to handle.

---

## What to do if something breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `psycopg.OperationalError: connection refused` | DATABASE_URL wrong or Neon project asleep | Wake Neon by hitting the SQL editor; verify URL has `?sslmode=require` |
| `HTTP 403 from CourtListener` | Token wrong or missing | `echo $COURTLISTENER_TOKEN` to verify; regenerate at courtlistener.com/profile/api/ |
| Worker runs but `inserted=0 updated=0` | No new class actions today (rare) or filter too strict | Try `--days 5`; check https://www.courtlistener.com/recap/?q=%22class+action%22&type=r manually |
| Fly logs show "no machines running" | Scheduled machine not created | Re-run `fly machine run --schedule hourly ...` |
| `enriched.json` is stale on the site | Worker not running, or front-end caching | Check `MAX(updated_at)` in DB; hard-refresh the page (Ctrl-Shift-R) |
| `export: wrote 0 filings` after a successful poll | export_days mismatch with poll days | Use `--export-days 7` to be safe |

