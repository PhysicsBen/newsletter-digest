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
from src.db.models import Digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_backtest(n_weeks: int) -> None:
    """Generate one digest per week for the last n_weeks weeks.

    Skips phases 1-4 (data must already be ingested and summarized).
    Processes weeks oldest-first so topic continuity accumulates naturally.
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
            date_range_end=datetime.utcnow(),
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


def main() -> None:
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
    args = parser.parse_args()
    if args.backtest:
        run_backtest(args.backtest)
    else:
        run(since=args.since)


if __name__ == "__main__":
    main()
