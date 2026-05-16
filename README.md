# newsletter-digest

A Python pipeline that fetches AI newsletter emails from a Gmail label, follows links to source articles, deduplicates stories semantically, summarises each story once with an LLM, clusters results into tracked topics, and outputs a significance-ranked Markdown digest — optionally emailed to your inbox.

## How it works

```
Gmail label
    │
    ▼
Phase 1 — Fetch new emails (Gmail API, incremental watermark)
    │
    ▼
Phase 2 — Fetch & extract article text (httpx + trafilatura)
    │
    ▼
Phase 3 — Semantic deduplication (sentence-transformers embeddings)
          → groups the same story from multiple newsletters into one canonical_story
    │
    ▼
Phase 4 — LLM summarisation & significance scoring (LiteLLM → Gemini)
          → one LLM call per canonical story, never per newsletter mention
    │
    ▼
Phase 5 — Topic clustering & continuity (scikit-learn cosine similarity)
          → detects recurring topics, generates "what's new since last time"
    │
    ▼
Phase 6 — Digest assembly → output/digest_YYYY-MM-DD_YYYY-MM-DD.md
                          → optional email via Gmail API
```

## Requirements

- Python 3.13+
- A Google Cloud project with the Gmail API enabled and an OAuth2 `credentials.json`
- A Gemini API key (or any LiteLLM-compatible provider)

## Setup

```bash
git clone https://github.com/your-org/newsletter-digest
cd newsletter-digest
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Mac/Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | API key from [Google AI Studio](https://aistudio.google.com/app/apikey) |
| `GMAIL_LABEL` | Gmail label to read newsletters from (e.g. `AI Newsletters`) |
| `DIGEST_RECIPIENT_EMAIL` | Address to email finished digests to |
| `DATABASE_URL` | Defaults to `sqlite:///data/newsletter.db`; set to a Postgres URL for production |

Initialise the database:

```bash
alembic upgrade head
```

Authenticate Gmail (opens a browser on first run):

```bash
python -m src.pipeline --since 2026-01-01
```

`token.json` is written after successful auth and reused on subsequent runs.

## Usage

```bash
# Run the full pipeline (fetch new emails → summarise → write digest)
python -m src.pipeline

# Process emails since a specific date
python -m src.pipeline --since 2026-05-01

# Generate N weekly digests from existing data (skips ingestion/summarisation)
python -m src.pipeline --backtest 4

# Generate and email weekly digests
python -m src.pipeline --backtest 4 --send

# Email all digest files in output/ to DIGEST_RECIPIENT_EMAIL
python -m src.pipeline --send-digests
```

## Running tests

```bash
python -m pytest tests/ -q
```

## Project layout

```
src/
  config.py              # pydantic-settings — all config from .env
  pipeline.py            # CLI entry point; orchestrates all phases
  gmail_client.py        # Gmail API: incremental fetch by label
  article_fetcher.py     # httpx fetch + trafilatura extraction
  email_sender.py        # Send digest emails via Gmail API
  db/
    models.py            # SQLAlchemy ORM models
    session.py           # Engine / session factory
    migrations/          # Alembic migrations
  llm/
    client.py            # LiteLLM wrapper — all LLM calls go here
    deduplicator.py      # Embed articles, cluster near-duplicates
    summarizer.py        # Per-story summarisation + significance scoring
    topic_clusterer.py   # Topic detection and continuity tracking
    digest_writer.py     # Assemble final Markdown digest
data/                    # SQLite DB (gitignored)
output/                  # Generated digest files (gitignored)
docs/
  plan.md                # Architecture and data model reference
  railway_deployment.md  # Step-by-step Railway deploy runbook
```

## Deploying to Railway

See [docs/railway_deployment.md](docs/railway_deployment.md) for the full step-by-step runbook.

The short version:

1. Add the Railway Postgres plugin (sets `DATABASE_URL` automatically).
2. Set `GEMINI_API_KEY`, `GMAIL_TOKEN_JSON` (raw JSON from your local `token.json`), and `DIGEST_RECIPIENT_EMAIL` as Railway environment variables.
3. `railway up` — the container runs `alembic upgrade head && python -m src.pipeline` on start.
4. Set a weekly cron in Railway settings (e.g. `0 7 * * 1`).

## Significance scoring

Stories are scored 1–10 by the LLM:

| Score | Meaning |
|---|---|
| 1–3 | New unproven libraries, minor tool releases, incremental updates |
| 4–6 | Noteworthy research, meaningful launches, established tools |
| 7–10 | Fundamental LLM performance gains, emerging standards, breakthrough research |

Topics scoring below `DIGEST_SIGNIFICANCE_MIN_SCORE` (default 5.0) are excluded from the digest.

## Key design decisions

- **Deduplication before LLM** — the same story covered by five newsletters costs one LLM call, not five.
- **Local embeddings only** — `sentence-transformers` (`bge-small-en-v1.5`); no embedding API calls.
- **Resumable pipeline** — articles have a `processing_status` (`pending → done/failed`); a crashed run restarts without reprocessing completed work.
- **Model-agnostic** — all LLM calls go through LiteLLM; switching models is a one-line `.env` change.
