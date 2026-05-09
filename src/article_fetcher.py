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

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import httpx
import trafilatura
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Article, ProcessingStatus

log = logging.getLogger(__name__)

_SOFT_PAYWALL_SIGNALS = [
    "subscribe to read",
    "subscription required",
    "members only",
    "sign in to read",
    "create a free account to read",
]

FETCH_TIMEOUT = 20.0  # seconds


def canonicalize_url(url: str) -> str:
    """Strip tracking parameters and normalize the URL."""
    parsed = urlparse(url)
    # Drop fragment; keep scheme/netloc/path/params/query as-is for now
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def _is_soft_paywalled(text: str) -> bool:
    lower = text.lower()
    return any(signal in lower for signal in _SOFT_PAYWALL_SIGNALS)


def fetch_pending_articles(session: Session) -> tuple[int, int]:
    """
    Fetch and extract content for all articles with processing_status=pending.
    Skips articles already at done/failed.

    Returns (done_count, failed_count).
    """
    # TODO: implement
    # 1. Query articles WHERE processing_status=pending
    # 2. For each article:
    #    a. Set processing_status=in_progress; flush
    #    b. canonical_url = canonicalize_url(article.url)
    #    c. Check if another article with same canonical_url is already done → skip/link
    #    d. Fetch with httpx; handle timeouts and HTTP errors
    #    e. If 403 → mark is_paywalled=True, status=done
    #    f. Extract text + published_at with trafilatura
    #    g. Check for soft-paywall signals in extracted text
    #    h. Set processing_status=done or failed
    # 3. session.commit() after each article for resumability
    raise NotImplementedError
