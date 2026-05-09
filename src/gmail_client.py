"""
Gmail API client: incremental fetch of newsletter emails by label.

Uses OAuth2 with existing credentials.json/token.json.
Auto-registers new senders in newsletter_sources (trust_weight defaults to 1.0).
Tracks fetch progress via PipelineWatermark in the DB.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email import message_from_bytes
from typing import Optional

from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Newsletter, NewsletterArticle, NewsletterSource, PipelineWatermark

log = logging.getLogger(__name__)

WATERMARK_KEY = "gmail_last_fetch"


def _get_credentials():
    """Load or refresh OAuth2 credentials from token.json."""
    # TODO: implement using google-auth-oauthlib
    raise NotImplementedError


def _get_gmail_service():
    """Build and return an authenticated Gmail API service object."""
    # TODO: implement using googleapiclient.discovery.build
    raise NotImplementedError


def get_watermark(session: Session) -> Optional[datetime]:
    """Return the last successful fetch timestamp, or None if first run."""
    row = session.query(PipelineWatermark).filter_by(key=WATERMARK_KEY).first()
    if row is None:
        return None
    return datetime.fromisoformat(row.value)


def set_watermark(session: Session, ts: datetime) -> None:
    """Upsert the watermark timestamp."""
    row = session.query(PipelineWatermark).filter_by(key=WATERMARK_KEY).first()
    if row is None:
        row = PipelineWatermark(key=WATERMARK_KEY, value=ts.isoformat())
        session.add(row)
    else:
        row.value = ts.isoformat()


def fetch_new_emails(session: Session) -> int:
    """
    Fetch all emails under the configured Gmail label since the last watermark.
    Auto-registers new senders in newsletter_sources.
    Creates Article records (processing_status=pending) for extracted URLs.
    Updates the watermark on success.

    Returns the number of new newsletter emails stored.
    """
    # TODO: implement
    # 1. Get watermark via get_watermark()
    # 2. Query Gmail API for messages under settings.gmail_label since watermark
    # 3. For each message:
    #    a. Parse From header → upsert NewsletterSource
    #    b. Store Newsletter record (skip if gmail_id already exists)
    #    c. Extract hrefs from body_raw
    #    d. Upsert Article(url=href, processing_status=pending) for each unique URL
    #    e. Create NewsletterArticle join records
    # 4. set_watermark() with current time
    raise NotImplementedError
