"""
Article fetching and content extraction.

For each pending article:
- Normalize/canonicalize URL
- Fetch with httpx (async under the hood, sync interface for pipeline)
- Extract clean text and published_at metadata with trafilatura
- Detect paywalls (HTTP 403, soft-paywall signals)
- Update processing_status to done or failed
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import trafilatura
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Article, ProcessingStatus
from src.db.session import get_session

log = logging.getLogger(__name__)

_SOFT_PAYWALL_SIGNALS = [
    "subscribe to read",
    "subscription required",
    "members only",
    "sign in to read",
    "create a free account to read",
]

FETCH_TIMEOUT = 20.0  # seconds
FETCH_WORKERS = 20    # concurrent HTTP workers


# Query parameters that are purely for tracking and carry no content identity.
_TRACKING_PARAMS = frozenset({
    # UTM
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    # Ad-click IDs
    "fbclid", "gclid", "dclid", "gbraid", "wbraid",
    # Substack email personalisation
    "token", "r",
    # Mailchimp
    "mc_cid", "mc_eid",
    # Dub.co short-link tracking
    "dub_id",
})


def canonicalize_url(url: str) -> str:
    """Strip tracking parameters and normalize the URL."""
    parsed = urlparse(url)
    clean_qs = urlencode(
        [(k, v) for k, v in parse_qsl(parsed.query) if k not in _TRACKING_PARAMS]
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_qs, ""))


def _is_soft_paywalled(text: str) -> bool:
    lower = text.lower()
    return any(signal in lower for signal in _SOFT_PAYWALL_SIGNALS)


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_pending_articles(session: Session) -> tuple[int, int]:
    """
    Fetch and extract content for all articles with processing_status=pending.
    Uses a thread pool so multiple HTTP requests run concurrently.
    Each worker runs with its own DB session (SQLAlchemy sessions are not thread-safe).

    Returns (done_count, failed_count).
    """
    pending_ids: list[int] = (
        session.execute(
            select(Article.id).where(Article.processing_status == ProcessingStatus.pending)
        )
        .scalars()
        .all()
    )

    log.info("Found %d pending articles to fetch", len(pending_ids))
    if not pending_ids:
        return 0, 0

    done_count = 0
    failed_count = 0

    # httpx.Client is thread-safe; share one instance across all workers.
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_one_threaded, article_id, client): article_id
                for article_id in pending_ids
            }
            for future in as_completed(futures):
                article_id = futures[future]
                try:
                    status = future.result()
                    if status == "done":
                        done_count += 1
                    elif status == "failed":
                        failed_count += 1
                    # "pending" (rate-limited) counts as neither
                except Exception as exc:
                    log.warning("Unexpected error processing article %d: %s", article_id, exc)
                    failed_count += 1

    log.info("Article fetch complete: %d done, %d failed", done_count, failed_count)
    return done_count, failed_count


def _fetch_one_threaded(article_id: int, client: httpx.Client) -> str:
    """Fetch a single article using a thread-local DB session. Returns 'done', 'failed', or 'pending'."""
    with get_session() as thread_session:
        article = thread_session.execute(
            select(Article).where(Article.id == article_id)
        ).scalar_one_or_none()
        if article is None:
            return "failed"

        article.processing_status = ProcessingStatus.in_progress
        thread_session.flush()

        canonical = canonicalize_url(article.url)
        try:
            _fetch_one(thread_session, client, article, canonical)
            thread_session.commit()
            return "done" if article.processing_status == ProcessingStatus.done else "pending"
        except Exception as exc:
            log.warning("Failed to fetch %s: %s", article.url, exc)
            article.processing_status = ProcessingStatus.failed
            thread_session.commit()
            return "failed"


def _fetch_one(session: Session, client: httpx.Client, article: Article, canonical_url: str) -> None:
    """Fetch a single article, extract content, update the article record."""
    # If another article with the same canonical URL is already done, reuse its content
    existing = (
        session.execute(
            select(Article).where(
                Article.url == canonical_url,
                Article.processing_status == ProcessingStatus.done,
                Article.id != article.id,
            )
        )
        .scalars()
        .first()
    )
    if existing:
        article.body_text = existing.body_text
        article.published_at = existing.published_at
        article.is_paywalled = existing.is_paywalled
        article.processing_status = ProcessingStatus.done
        return

    try:
        response = client.get(canonical_url)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise RuntimeError(f"HTTP error: {exc}") from exc

    article.http_status = response.status_code
    article.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)

    # Store the final URL after any redirects (resolves tracking/redirect links)
    final_url = str(response.url)
    article.canonical_url = canonicalize_url(final_url)

    if response.status_code == 403:
        article.is_paywalled = True
        article.processing_status = ProcessingStatus.done
        return

    if response.status_code == 429:
        # Transient rate-limit — leave as pending so the next pipeline run retries
        article.processing_status = ProcessingStatus.pending
        log.debug("Rate limited on %s — will retry next run", canonical_url)
        return

    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")

    html = response.text
    extracted = trafilatura.extract(
        html,
        output_format="json",
        include_comments=False,
        include_tables=False,
        with_metadata=True,
    )

    if not extracted:
        # trafilatura couldn't extract anything meaningful
        article.processing_status = ProcessingStatus.done
        return

    data = json.loads(extracted)
    body = data.get("text") or ""

    if _is_soft_paywalled(body):
        article.is_paywalled = True

    article.body_text = body
    article.title = (data.get("title") or "").strip() or None

    # Prefer the publisher's own canonical URL extracted from <link rel="canonical">
    trafilatura_url = (data.get("url") or "").strip()
    if trafilatura_url:
        article.canonical_url = canonicalize_url(trafilatura_url)

    pub_date = data.get("date")
    if pub_date:
        try:
            article.published_at = datetime.fromisoformat(pub_date)
        except (ValueError, TypeError):
            pass

    article.processing_status = ProcessingStatus.done
