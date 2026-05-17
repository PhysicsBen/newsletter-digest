"""
CLI entry point for the newsletter digest pipeline.

Usage:
    python -m src.pipeline [--since YYYY-MM-DD]
    python -m src.pipeline --backtest N   # generate N weekly digests going back N weeks

Phases:
    1. Gmail ingestion
    2. Article fetching
    3. Semantic deduplication (before any LLM call)
    4. LLM summarization & significance scoring
    5. Topic clustering & continuity
    6. Digest assembly
"""
from __future__ import annotations
import subprocess
import sys
print("[pipeline] Python interpreter started, loading imports...", flush=True)

import argparse
import logging
from datetime import datetime, timedelta, timezone

from src.db.session import get_session, init_db
from src.gmail_client import fetch_new_emails
from src.article_fetcher import fetch_pending_articles
from src.llm.deduplicator import cluster_into_canonical_stories
from src.llm.summarizer import summarize_canonical_stories
from src.llm.topic_clusterer import cluster_topics
from src.llm.digest_writer import write_digest
from src.email_sender import send_all_digests, send_digest_file
from src.db.models import Article, Digest, ProcessingStatus
from src.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_backtest(n_weeks: int, send: bool = False) -> None:
    """Generate one digest per week for the last n_weeks weeks.

    Skips phases 1-4 (data must already be ingested and summarized).
    Processes weeks oldest-first so topic continuity accumulates naturally.
    If send=True, emails each digest to settings.digest_recipient_email after writing it.
    """
    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Build week boundaries oldest-first
    weeks: list[tuple[datetime, datetime]] = []
    for i in range(n_weeks, 0, -1):
        end = now - timedelta(weeks=i - 1)
        start = end - timedelta(weeks=1)
        weeks.append((start, end))

    log.info("Running backtest: %d weekly digests from %s to %s",
             n_weeks, weeks[0][0].strftime("%Y-%m-%d"), weeks[-1][1].strftime("%Y-%m-%d"))

    for week_start, week_end in weeks:
        log.info("--- Week %s → %s ---",
                 week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d"))
        with get_session() as session:
            digest = Digest(
                date_range_start=week_start,
                date_range_end=week_end,
            )
            session.add(digest)
            session.flush()

            log.info("Phase 5 — Topic clustering")
            topic_count = cluster_topics(
                session, digest,
                date_start=week_start,
                date_end=week_end,
            )
            log.info("Assigned %d topics", topic_count)

            log.info("Phase 6 — Digest assembly")
            output_path = write_digest(session, digest)
            log.info("Digest written to %s", output_path)

            session.commit()

        if send:
            from pathlib import Path
            send_digest_file(Path(output_path), settings.digest_recipient_email)


def run(since: datetime | None = None) -> None:
    init_db()

    with get_session() as session:
        log.info("Phase 1 — Gmail ingestion")
        email_count = fetch_new_emails(session, since=since)
        log.info("Fetched %d new emails", email_count)

        log.info("Phase 2 — Article fetching")
        done, failed = fetch_pending_articles(session)
        log.info("Articles: %d done, %d failed", done, failed)

        log.info("Phase 3 — Semantic deduplication")
        story_count = cluster_into_canonical_stories(session)
        log.info("Clustered into %d canonical stories", story_count)

        log.info("Phase 4 — LLM summarization")
        summarized = summarize_canonical_stories(session)
        log.info("Summarized %d stories", summarized)

        digest = Digest(
            date_range_start=since,
            date_range_end=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(digest)
        session.flush()

        log.info("Phase 5 — Topic clustering")
        topic_count = cluster_topics(session, digest)
        log.info("Assigned %d topics", topic_count)

        log.info("Phase 6 — Digest assembly")
        output_path = write_digest(session, digest)
        log.info("Digest written to %s", output_path)

        session.commit()

    if settings.digest_recipient_email and output_path:
        from pathlib import Path
        try:
            send_digest_file(Path(output_path), settings.digest_recipient_email)
            log.info("Digest emailed to %s", settings.digest_recipient_email)
        except Exception:
            log.exception("Failed to send digest email")
            raise


def _run_migrations() -> None:
    """Run alembic upgrade head via subprocess so output appears in the Python log stream."""
    print("[pipeline] Running database migrations...", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=False,  # inherit stdout/stderr so Railway captures them
    )
    if result.returncode != 0:
        print(f"[pipeline] alembic exited with code {result.returncode} — aborting", flush=True)
        sys.exit(result.returncode)
    print("[pipeline] Migrations complete.", flush=True)


def main() -> None:
    _run_migrations()
    parser = argparse.ArgumentParser(description="Newsletter digest pipeline")
    parser.add_argument(
        "--since",
        type=lambda s: datetime.fromisoformat(s),
        default=None,
        metavar="DATE",
        help="Process emails since this ISO date (e.g. 2026-05-01). Defaults to last watermark.",
    )
    parser.add_argument(
        "--backtest",
        type=int,
        default=0,
        metavar="N",
        help="Generate N weekly digests going back N weeks (phases 5-6 only, data must exist).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Email each digest to DIGEST_RECIPIENT_EMAIL after generating it (use with --backtest).",
    )
    parser.add_argument(
        "--send-digests",
        action="store_true",
        help="Email all existing digest files in output/ to DIGEST_RECIPIENT_EMAIL.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset all failed articles back to pending so they are re-fetched on this run.",
    )
    args = parser.parse_args()

    if args.retry_failed:
        init_db()
        with get_session() as session:
            from sqlalchemy import update
            result = session.execute(
                update(Article)
                .where(Article.processing_status == ProcessingStatus.failed)
                .values(processing_status=ProcessingStatus.pending)
            )
            session.commit()
            log.info("Reset %d failed articles back to pending", result.rowcount)

    if args.send_digests:
        sent = send_all_digests(recipient=settings.digest_recipient_email)
        log.info("Sent %d digest(s)", sent)
    elif args.backtest:
        run_backtest(args.backtest, send=args.send)
    else:
        run(since=args.since)


if __name__ == "__main__":
    import sys
    import traceback
    try:
        main()
    except Exception:
        # Force traceback to stdout so Railway's log viewer captures it even if stderr is split.
        print("[FATAL] Pipeline crashed with unhandled exception:", flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        sys.exit(1)
