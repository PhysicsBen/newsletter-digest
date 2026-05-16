"""
Migrate all data from the local SQLite database to a Postgres database.

Usage:
    # Set POSTGRES_URL to your Railway Postgres connection string, then:
    python scripts/migrate_sqlite_to_postgres.py

    # Or pass URLs explicitly:
    python scripts/migrate_sqlite_to_postgres.py \
        --sqlite sqlite:///data/newsletter.db \
        --postgres "postgresql://user:pass@host:5432/railway"

The script:
  1. Runs alembic upgrade head against Postgres to create the schema.
  2. Copies every table in FK-safe insertion order.
  3. Resets all Postgres sequences to match the max id so future inserts
     don't collide with the migrated rows.
  4. Is safe to re-run: existing rows with the same primary key are skipped
     (INSERT ... ON CONFLICT DO NOTHING).

Environment variables:
    SQLITE_URL    SQLite source (default: sqlite:///data/newsletter.db)
    POSTGRES_URL  Postgres target (required if --postgres not passed)
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

# Tables in insertion order (parents before children).
# topic_articles references digests, but digests has no FK deps besides topics,
# so we split them carefully.
ORDERED_TABLES = [
    "newsletter_sources",
    "newsletters",
    "canonical_stories",
    "articles",
    "newsletter_articles",
    "article_summaries",
    "topics",
    "digests",
    "digest_topics",
    "topic_articles",
    "pipeline_watermarks",
]


BATCH_SIZE = 500


def copy_table(src_conn, dst_conn, table: str) -> int:
    """Copy all rows from src to dst for one table. Returns rows inserted."""
    rows = src_conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
    if not rows:
        log.info("  %s — empty, skipping", table)
        return 0

    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)

    # Use ON CONFLICT DO NOTHING so the script is safely re-runnable.
    stmt = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    )
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = [dict(r) for r in rows[i : i + BATCH_SIZE]]
        result = dst_conn.execute(stmt, batch)
        inserted += result.rowcount
        if len(rows) > BATCH_SIZE:
            log.info("  %s — %d / %d rows …", table, min(i + BATCH_SIZE, len(rows)), len(rows))

    log.info("  %s — %d / %d rows inserted", table, inserted, len(rows))
    return inserted


def reset_sequences(dst_conn, tables: list[str]) -> None:
    """Reset Postgres sequences so nextval() starts above the migrated max id."""
    for table in tables:
        try:
            dst_conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE(MAX(id), 0) + 1, false) FROM {table}"
                )
            )
        except Exception as exc:
            log.debug("Could not reset sequence for %s: %s", table, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite → Postgres")
    parser.add_argument(
        "--sqlite",
        default=os.environ.get("SQLITE_URL", "sqlite:///data/newsletter.db"),
        help="SQLAlchemy URL for the SQLite source database.",
    )
    parser.add_argument(
        "--postgres",
        default=os.environ.get("POSTGRES_URL", ""),
        help="SQLAlchemy URL for the Postgres target database.",
    )
    args = parser.parse_args()

    if not args.postgres:
        sys.exit(
            "ERROR: Postgres URL is required. Pass --postgres or set POSTGRES_URL env var.\n"
            "  Get it from the Railway dashboard → your Postgres service → Connect tab."
        )

    # Normalise Railway's postgres:// scheme
    pg_url = args.postgres
    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    log.info("Source:  %s", args.sqlite)
    log.info("Target:  %s", pg_url[:pg_url.index("@") + 1] + "***" if "@" in pg_url else pg_url)

    # Step 1 — run alembic migrations against Postgres
    log.info("Running alembic upgrade head on Postgres …")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": pg_url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    log.info("Schema ready.")

    # Step 2 — copy data
    src_engine = create_engine(args.sqlite)
    dst_engine = create_engine(pg_url)

    src_inspector = inspect(src_engine)
    src_tables = set(src_inspector.get_table_names())

    total = 0
    with src_engine.connect() as src_conn, dst_engine.connect() as dst_conn:
        for table in ORDERED_TABLES:
            if table not in src_tables:
                log.info("  %s — not in source, skipping", table)
                continue
            n = copy_table(src_conn, dst_conn, table)
            total += n
            dst_conn.commit()

        # Step 3 — reset sequences
        log.info("Resetting Postgres sequences …")
        reset_sequences(dst_conn, ORDERED_TABLES)
        dst_conn.commit()

    log.info("Done. %d rows migrated total.", total)


if __name__ == "__main__":
    main()
