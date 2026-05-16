# Newsletter Digest — Project Plan

## Overview

A Python pipeline that pulls AI newsletter emails from a Gmail label, follows links to source articles, deduplicates stories semantically, summarizes content using a frontier LLM, clusters stories into tracked topics, and outputs a significance-ranked Markdown digest with "what's new since last time" annotations.

**Scope (phase 1):** Core data pipeline and LLM processing only. No frontend, no email blast, no scheduling. Designed from the start to support those additions later.

**Future:** Automate and host on Railway (SQLite → Postgres is a connection-string swap; schema is Postgres-compatible throughout).

---

## Project Layout

```
newsletter-digest/
├── src/
│   ├── config.py                    # pydantic-settings, all config from .env
│   ├── pipeline.py                  # CLI entry point, orchestrates all phases
│   ├── gmail_client.py              # Gmail API: incremental fetch by label
│   ├── article_fetcher.py           # httpx fetch + trafilatura extraction
│   ├── db/
│   │   ├── models.py                # SQLAlchemy ORM models
│   │   └── migrations/              # Alembic migrations
│   └── llm/
│       ├── client.py                # LiteLLM abstraction layer
│       ├── deduplicator.py          # Semantic near-duplicate clustering
│       ├── summarizer.py            # Per-story summarization + significance scoring
│       ├── topic_clusterer.py       # Topic detection + continuity tracking
│       └── digest_writer.py         # Final Markdown digest assembly
├── data/                            # SQLite database
├── output/                          # Generated digest Markdown files
├── .env                             # Local secrets (gitignored)
├── .env.example                     # Template committed to repo
└── requirements.txt
```

---

## Data Model

All tables via SQLAlchemy. SQLite now; Postgres-compatible for Railway later.

### `newsletter_sources`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| sender_email | str unique | |
| display_name | str | |
| trust_weight | float | Default 1.0. Higher = more credible source. Passed to LLM as context. |
| added_at | datetime | Auto-registered on first email seen |

Adding a new newsletter = one DB insert, no code changes.

### `newsletters`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| gmail_id | str unique | Gmail message ID |
| source_id | FK → newsletter_sources | |
| sender | str | Raw From header |
| subject | str | |
| date | datetime | |
| body_raw | text | Raw HTML body |
| fetched_at | datetime | |

### `articles`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| url | str unique | Original extracted URL |
| canonical_url | str | Normalized/resolved URL |
| title | str | |
| body_text | text | Cleaned text from trafilatura |
| is_paywalled | bool | |
| published_at | datetime | Parsed from article metadata |
| fetched_at | datetime | |
| http_status | int | |
| processing_status | enum | `pending` / `in_progress` / `done` / `failed` |
| canonical_story_id | FK → canonical_stories (nullable) | Assigned after deduplication |

`processing_status` makes the pipeline **resumable** — a crash mid-run resumes from pending/in_progress records.

### `canonical_stories`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| representative_article_id | FK → articles | The article used for summarization |
| article_ids | JSON array | All article IDs in this cluster |
| embedding | JSON float array | Centroid embedding of the cluster |

Near-duplicate articles (same story covered in multiple newsletters) are merged here **before** any LLM calls. The LLM summarizes once per canonical story, not once per newsletter mention. This is the primary cost control lever.

### `newsletter_articles`
Many-to-many join: `newsletter_id` ↔ `article_id`.

### `article_summaries`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| article_id | FK → articles | |
| summary_text | text | |
| significance_score | float | 0–10 |
| model_used | str | e.g., `claude-opus-4-5` |
| prompt_version | str | e.g., `v1.2`. Keeps old scores interpretable after rubric changes. |
| created_at | datetime | |

### `topics`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| name | str | LLM-generated |
| description | str | |
| embedding | JSON float array | For continuity matching across digests |
| first_seen | datetime | |
| last_seen | datetime | |
| status | enum | `active` / `resolved` / `merged_into` |
| merged_into_id | FK → topics (nullable) | |

### `topic_articles`
Join: `topic_id` ↔ `article_id` ↔ `digest_id`.

### `digests`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| generated_at | datetime | |
| date_range_start | datetime | |
| date_range_end | datetime | |
| output_path | str | Path to Markdown file |

### `digest_topics`
| Field | Type | Notes |
|---|---|---|
| id | int PK | |
| digest_id | FK → digests | |
| topic_id | FK → topics | |
| summary_text | text | Topic-level summary for this digest |
| significance_score | float | Aggregate score for this topic |
| what_is_new_text | text | "Since we last covered this 4 days ago…" (null if first appearance) |

---

## Pipeline

Run via: `python -m src.pipeline [--since DATE]`

### Phase 1 — Gmail ingestion
1. Load watermark (last successful run timestamp) from DB
2. Fetch all emails under configured Gmail label since watermark via Gmail API (OAuth2, existing `credentials.json`)
3. Auto-register new senders in `newsletter_sources` (trust_weight defaults to 1.0)
4. Parse HTML body → plain text; extract all hrefs
5. Write to `newsletters` + `newsletter_articles`; set `processing_status=pending` for new article URLs

### Phase 2 — Article fetching
6. For each pending article: normalize/canonicalize URL, skip if already `done` in DB
7. Fetch with `httpx`; extract clean text and `published_at` using **trafilatura**
8. Detect paywalls (HTTP 403, soft-paywall signals); set `is_paywalled=True`
9. Update `processing_status` to `done` or `failed`

### Phase 3 — Semantic deduplication (before any LLM calls)
10. Embed all new article texts using local **sentence-transformers** (e.g., `bge-small-en`) — zero API cost, fast on GPU
11. Cluster near-duplicates by cosine similarity into `canonical_stories`
12. Assign `canonical_story_id` on each article

### Phase 4 — LLM summarization & significance scoring
13. For each `canonical_story` without an `article_summary`: call LLM via **LiteLLM**
14. System prompt includes significance rubric, source `trust_weight` as context, current `prompt_version`
15. Token budget: ~6000 tokens per story. Longer articles chunked → each chunk summarized → combined. Transparent to caller.
16. Store summary + score + `model_used` + `prompt_version` in `article_summaries`

**Significance rubric:**
| Score | Meaning |
|---|---|
| 1–3 | New unproven libraries, minor tool releases, incremental product updates |
| 4–6 | Noteworthy research, meaningful launches, tools with track record |
| 7–10 | Fundamental LLM performance gains, emerging standards, paradigm shifts, breakthrough research |

### Phase 5 — Topic clustering & continuity
17. Embed article summaries; cluster by cosine similarity; LLM names and describes each cluster
18. Compare new topic embeddings to existing `topics` in DB — similarity > threshold = same ongoing topic (update `last_seen`); below threshold = new topic (`status=active`)
19. For recurring topics: pass prior `digest_topics.summary_text` + current articles to LLM → generate `what_is_new_text`

### Phase 6 — Digest assembly
20. Sort topics by `significance_score` descending
21. For each topic: significance badge, `what_is_new_text` (if recurring), bullet-point article summaries, source links
22. Flag paywalled articles; flag articles where `published_at` > 30 days ago as stale
23. Write Markdown to `output/digest_{start}_{end}.md`
24. Persist `digests` + `digest_topics`; update watermark

---

## LLM & Embedding Strategy

- **LiteLLM** as abstraction layer — swap model in `.env`, zero code changes
- **Starting model:** Top frontier model (Claude or Gemini latest) for quality
- **Embeddings:** Local `sentence-transformers` (`bge-small-en`) — no API cost, 3060ti handles trivially
- **Cost control:** Deduplication before LLM; one summary per canonical story, not per mention

---

## Paywall Handling

Best-effort: extract whatever is publicly visible, mark `is_paywalled=True`, include link in digest with `[paywalled]` note.

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `google-api-python-client`, `google-auth-oauthlib` | Gmail API |
| `trafilatura` | Article content extraction + published_at metadata |
| `sqlalchemy`, `alembic` | ORM + migrations |
| `litellm` | LLM provider abstraction |
| `sentence-transformers` | Local embeddings |
| `scikit-learn` | Cosine similarity, clustering |
| `pydantic-settings` | Config from `.env` |
| `httpx` | HTTP fetching |
| `python-dotenv` | `.env` loading |

---

## Verification Checklist

1. **Incremental fetch:** Run pipeline twice — second run fetches 0 new emails
2. **Resumability:** Kill pipeline mid-run; re-run completes without reprocessing `done` articles
3. **Deduplication:** 3 newsletters covering same story → 1 canonical_story, 1 LLM call, 1 digest entry
4. **Significance scoring:** Known minor library release scores 1–3; known major model launch scores 7–10
5. **What's new:** Two consecutive digests on overlapping topic → second shows `what_is_new_text`
6. **Paywall flag:** Paywalled URL appears in digest with `[paywalled]` note
7. **Stale flag:** Article published 60+ days ago appears with stale indicator

---

## Decisions & Constraints

- **SQLite → Postgres:** Schema is Postgres-compatible. Railway deploy = connection string swap in `.env`
- **Out of scope (phase 1):** Frontend, email blast, scheduling, Railway deploy
- **Topics:** Auto-discovered by LLM. Lifecycle management (merge/resolve) is backlog; schema is ready
- **Trigger:** Manual CLI for now (`python -m src.pipeline`); ready for cron/Railway cron later
- **New newsletters:** Add a row to `newsletter_sources`; no code changes required

---

## Railway Migration Plan

**Target:** Run the pipeline as a scheduled Railway Cron Service against Railway Postgres.

**Model note:** Migrated default from `gemini/gemini-3-flash-preview` (discontinued May 25 2026) to `gemini/gemini-3.1-flash-lite` (GA). Already updated in `src/config.py`.

### Phase 1 — Database: SQLite → Railway Postgres
- Provision Railway Postgres plugin; Railway auto-injects `DATABASE_URL`
- Run `alembic upgrade head` against Postgres; validate schema (watch for JSON column and enum dialect differences)
- Smoke-test pipeline end-to-end against Postgres before proceeding

### Phase 2 — Gmail OAuth2 Credentials (highest risk)
The current flow assumes a local filesystem and browser for the initial OAuth dance.

**credentials.json** (Google OAuth client config):
- Store JSON contents as Railway secret env var `GMAIL_CREDENTIALS_JSON`
- Modify `_get_credentials()` in `gmail_client.py` to write a temp file from this env var if the credentials path doesn't exist on disk

**token.json** (user OAuth token):
- Perform one-time browser OAuth flow locally to generate a valid `token.json`
- Serialize contents to Railway secret env var `GMAIL_TOKEN_JSON`
- Modify `_get_credentials()` to bootstrap `token.json` from this env var on cold start
- Handle token refresh write-back: intercept the refresh callback and update a Railway Volume file (or re-persist to the env var via Railway API) — without this, the token will go stale after container restarts

Risk: the initial OAuth flow requires a browser (can't run on Railway). Must be done locally. Token refresh persistence requires custom plumbing.

### Phase 3 — Docker Image
Use a Dockerfile (not Nixpacks) to:
1. Install Python dependencies
2. Pre-download and cache the `BAAI/bge-small-en-v1.5` embedding model (~130MB) at **build time**:
   ```
   RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
   ```
   This avoids a ~130MB HuggingFace download on every cold start (Railway containers have ephemeral filesystems)
3. Copy source code; set entrypoint to `python -m src.pipeline`

### Phase 4 — Scheduled Execution
- Deploy as a **Railway Cron Service** (not a web service)
- Add `railway.json` with cron schedule (weekly) and the pipeline command
- Railway Cron runs the container on schedule, exits cleanly — maps naturally to the existing one-shot CLI

### Phase 5 — Output Files
The `output/` directory is ephemeral in Railway containers. Since digests are already emailed, the simplest path is to accept this. Options:
1. **Accept ephemeral** — digests delivered by email, file archive not needed *(recommended)*
2. **Railway Volume** — mount persistent volume at `output/` if file archive is wanted
3. **Store in DB** — add `body_markdown` column to `digests` table (future enhancement)

### Phase 6 — Environment Variables
| Variable | Notes |
|---|---|
| `DATABASE_URL` | Auto-injected by Railway Postgres plugin |
| `GEMINI_API_KEY` | Secret |
| `GMAIL_CREDENTIALS_JSON` | Secret — full JSON string from `credentials.json` |
| `GMAIL_TOKEN_JSON` | Secret — full JSON string from `token.json` (post-initial auth) |
| `DIGEST_RECIPIENT_EMAIL` | Delivery address |
| `LLM_MODEL` | `gemini/gemini-3.1-flash-lite` |
| `LLM_THINKING_LEVEL`, `LLM_CONCURRENCY`, etc. | Non-sensitive config |

### Migration Sequence
1. Stand up Railway Postgres; run `alembic upgrade head`; validate schema
2. Perform one-time local OAuth flow; capture fresh `token.json`
3. Load all secrets and config into Railway environment variables
4. Write and test Dockerfile locally (verify embedding model bakes in cleanly)
5. Modify `_get_credentials()` to bootstrap credentials and token from env vars; handle refresh write-back
6. Harden `config.py`: remove SQLite fallback default, require `DATABASE_URL` to be set explicitly
7. Deploy as Railway Cron service; trigger manually to validate end-to-end
8. Decide on output file persistence (ephemeral vs. volume)
