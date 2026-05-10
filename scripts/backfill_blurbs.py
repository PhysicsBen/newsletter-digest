"""
Backfill blurbs for existing newsletter_article rows.

Reads body_raw from each Newsletter, re-extracts (url, blurb) pairs,
and populates NewsletterArticle.blurb for any row where blurb is NULL.

Safe to re-run: skips rows that already have a blurb.
"""
from __future__ import annotations

import logging
import sys

from src.db.session import init_db, get_session
from src.db.models import Article, Newsletter, NewsletterArticle
from src.gmail_client import _extract_urls_with_blurbs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def backfill_blurbs() -> None:
    init_db()
    with get_session() as session:
        newsletters = session.query(Newsletter).filter(Newsletter.body_raw.isnot(None)).all()
        log.info("Processing %d newsletters", len(newsletters))

        total_updated = 0
        for i, nl in enumerate(newsletters, 1):
            if not nl.body_raw:
                continue

            urls_and_blurbs = _extract_urls_with_blurbs(nl.body_raw)
            url_to_blurb: dict[str, str | None] = {url: blurb for url, blurb in urls_and_blurbs}

            # Load all join rows for this newsletter that have no blurb yet
            joins = (
                session.query(NewsletterArticle)
                .filter_by(newsletter_id=nl.id)
                .filter(NewsletterArticle.blurb.is_(None))
                .all()
            )
            for join in joins:
                article = session.get(Article, join.article_id)
                if article is None:
                    continue
                # Match by original URL or canonical URL
                blurb = url_to_blurb.get(article.url) or url_to_blurb.get(article.canonical_url or "")
                if blurb:
                    join.blurb = blurb
                    total_updated += 1

            if i % 100 == 0:
                session.commit()
                log.info("[%d/%d] newsletters processed, %d blurbs written so far", i, len(newsletters), total_updated)

        session.commit()
        log.info("Done. Total blurbs backfilled: %d", total_updated)


if __name__ == "__main__":
    backfill_blurbs()
