# Railway Deployment Runbook

## Prerequisites

- Railway account at [railway.app](https://railway.app)
- Railway CLI installed: `npm install -g @railway/cli`
- Local repo with a working pipeline (all phases tested with `python -m src.pipeline`)
- A valid `token.json` already generated locally (covers both `gmail.readonly` and `gmail.send` scopes)

---

## Step 1 — Verify credentials.json and token.json are not tracked by Git

```bash
git status
```

Neither file should appear. Both are covered by `.gitignore`. If either shows up:

```bash
git rm --cached credentials.json token.json
git commit -m "chore: remove credentials from tracking"
```

---

## Step 2 — Re-generate token.json with both Gmail scopes (if needed)

The Railway container uses the token for both reading newsletters (`gmail.readonly`) and sending digests (`gmail.send`). If your current `token.json` was issued with only `gmail.readonly`, regenerate it locally:

```bash
# Delete the old token to force re-auth
del token.json          # Windows
# or: rm token.json    # Mac/Linux

python -m src.pipeline --backtest 0
# A browser window will open; log in and grant both scopes.
# token.json will be rewritten with both scopes.
```

Verify the scopes in the new token:

```python
import json
t = json.load(open("token.json"))
print(t.get("scopes"))
# should include both gmail.readonly and gmail.send
```

---

## Step 3 — Create a new Railway project

```bash
railway login
railway init          # choose "Empty Project", name it e.g. "newsletter-digest"
railway link          # if you created it in the dashboard instead
```

---

## Step 4 — Add a Postgres database

In the Railway dashboard:

1. Open your project → **+ New** → **Database** → **PostgreSQL**
2. Railway automatically injects `DATABASE_URL` (as `postgres://...`) into the service environment.
   The app normalises `postgres://` → `postgresql://` automatically.

No code changes needed. Confirm the variable name is `DATABASE_URL` in the Railway variables panel.

---

## Step 5 — Set environment variables

In the Railway dashboard under your service → **Variables**, add each of the following:

| Variable | Value | Notes |
|---|---|---|
| `GEMINI_API_KEY` | your key | From [Google AI Studio](https://aistudio.google.com/app/apikey) |
| `GMAIL_TOKEN_JSON` | *(see below)* | Raw JSON content of `token.json` |
| `DIGEST_RECIPIENT_EMAIL` | your@email.com | Where digests are emailed |
| `GMAIL_LABEL` | `AI Newsletters` | Must match the label name in your Gmail account |
| `LLM_MODEL` | `gemini/gemini-3.1-flash-lite` | Or override to a different model |

**Setting `GMAIL_TOKEN_JSON`:** copy the entire content of `token.json` as a single-line string:

```bash
# Windows PowerShell
Get-Content token.json -Raw | Set-Clipboard

# Mac/Linux
cat token.json | tr -d '\n' | pbcopy    # Mac
cat token.json | xclip -selection c    # Linux
```

Paste the clipboard contents as the value of `GMAIL_TOKEN_JSON` in the Railway variables panel. It should be valid JSON starting with `{`.

Leave `DATABASE_URL` alone — Railway injects it from the Postgres plugin.

---

## Step 6 — Deploy

```bash
railway up
```

Railway builds the Dockerfile, then runs:

```
alembic upgrade head && python -m src.pipeline
```

Watch the deploy logs in the Railway dashboard or with:

```bash
railway logs
```

Expected log output on a clean first run:

```
INFO  alembic — Running upgrade  -> 422de0ae9ef3, initial_schema
INFO  alembic — Running upgrade 422de0ae9ef3 -> bfa4f274efa0, add_blurb...
INFO  Phase 1 — Gmail ingestion
INFO  Fetched N new emails
INFO  Phase 2 — Article fetching
...
INFO  Digest written to output/digest_YYYY-MM-DD_YYYY-MM-DD.md
```

---

## Step 7 — Verify the digest email

Check the inbox at `DIGEST_RECIPIENT_EMAIL`. If the email did not arrive:

1. Check Railway logs for errors in Phase 6 / email sending.
2. Confirm `DIGEST_RECIPIENT_EMAIL` is set correctly.
3. Confirm `GMAIL_TOKEN_JSON` includes the `gmail.send` scope (see Step 2).

---

## Step 8 — Schedule weekly runs (cron)

Railway services run once per deploy by default. To run on a schedule:

1. In the Railway dashboard, open your service → **Settings** → **Cron Schedule**
2. Set the cron expression, e.g. every Monday at 07:00 UTC:

   ```
   0 7 * * 1
   ```

3. Save. Railway will restart the container on that schedule.

---

## Re-deploying after code changes

```bash
git push           # if repo is linked to Railway via GitHub
# — or —
railway up         # manual redeploy from local CLI
```

---

## Refreshing the Gmail token

OAuth2 refresh tokens are long-lived but can expire if unused for 6+ months or if Google revokes them. If the logs show:

```
RuntimeError: Gmail credentials are invalid or expired and no browser is available.
```

Regenerate locally (see Step 2) and update `GMAIL_TOKEN_JSON` in the Railway variables panel. No code change or redeploy needed — the next scheduled run picks up the new token automatically.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `UnsupportedParamsError: thinking` | Wrong LiteLLM version | `pip install --upgrade litellm` locally, update requirements.txt |
| `sqlalchemy.exc.OperationalError: could not connect` | `DATABASE_URL` not set or wrong format | Check Railway variables; confirm Postgres plugin is attached |
| `type "processingstatus" already exists` | Postgres migration retried after partial failure | Run `alembic downgrade base` then `alembic upgrade head` against the Railway DB (see below) |
| No emails fetched | `GMAIL_LABEL` doesn't match Gmail label name exactly | Check Gmail; label names are case-sensitive |
| Empty digest | No articles passed significance threshold (default 5.0) | Lower `DIGEST_SIGNIFICANCE_MIN_SCORE` temporarily to debug |

### Running Alembic against the Railway database directly

```bash
# Get the DATABASE_URL from Railway
railway variables

# Set it in your local shell (PowerShell)
$env:DATABASE_URL = "postgresql://..."

# Then run alembic locally
alembic downgrade base
alembic upgrade head
```
</content>
</invoke>