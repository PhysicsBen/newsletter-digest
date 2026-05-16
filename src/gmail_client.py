"""
Gmail API client: incremental fetch of newsletter emails by label.

Uses OAuth2 with existing credentials.json/token.json.
Auto-registers new senders in newsletter_sources (trust_weight defaults to 1.0).
Tracks fetch progress via PipelineWatermark in the DB.

Label: configured via GMAIL_LABEL in .env (e.g. "Newsletters/AI").
Gmail API represents nested labels with "/" — the label ID is resolved by name at runtime.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Article, Newsletter, NewsletterArticle, NewsletterSource, PipelineWatermark, ProcessingStatus

log = logging.getLogger(__name__)

WATERMARK_KEY = "gmail_last_fetch"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Only follow http/https links; skip mailto, cid, tracking pixels, etc.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
# Skip common tracking/unsubscribe domains and non-article paths.
# beehiiv tracking links (link.mail.beehiiv.com, email.beehiivstatus.com) never resolve
# to real article content — they serve beehiiv profile/ad pages instead.
_SKIP_URL_RE = re.compile(
    r"(unsubscribe|optout|manage-preferences|click\.|track\.|open\.|beacon\.|pixel\.|n"
    r"mailchi\.mp/track|list-manage\.com|beehiiv\.com/(unsubscribe|manage)|"
    r"link\.mail\.beehiiv\.com|email\.beehiivstatus\.com|"
    r"\.(png|jpg|jpeg|gif|webp|ico|css|js)(\?|$))",
    re.IGNORECASE,
)


def _get_credentials() -> Credentials:
    """Load OAuth2 credentials from token.json, running browser OAuth flow if absent."""
    import os
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds: Optional[Credentials] = None
    if os.path.exists(settings.gmail_token_path):
        creds = Credentials.from_authorized_user_file(settings.gmail_token_path, SCOPES)
        # Force re-auth if the stored token has no scopes recorded OR is missing
        # a required scope (e.g. gmail.send was added after initial auth).
        if creds and (not creds.scopes or not set(SCOPES).issubset(creds.scopes)):
            log.info("Stored token is missing required scopes — re-authenticating")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                log.info("Token refresh failed (likely scope change) — re-authenticating")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.gmail_credentials_path, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(settings.gmail_token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def _get_gmail_service():
    """Build and return an authenticated Gmail API service object."""
    return build("gmail", "v1", credentials=_get_credentials(), cache_discovery=False)


def _resolve_label_id(service, label_name: str) -> str:
    """
    Resolve a label name (e.g. 'Newsletters/AI') to its Gmail label ID.
    Raises ValueError if not found.
    """
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    available = [l["name"] for l in result.get("labels", [])]
    raise ValueError(
        f"Gmail label {label_name!r} not found. Available labels: {available}"
    )


def _decode_str(value: str) -> str:
    """Decode an RFC 2047-encoded header value to a plain string."""
    return str(make_header(decode_header(value)))


def _extract_body(msg) -> str:
    """
    Extract the HTML body (preferred) or plain-text body from an email.Message.
    Returns an empty string if neither is found.
    """
    html_part = None
    text_part = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            if ct == "text/html" and html_part is None:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_part = payload.decode(charset, errors="replace")
            elif ct == "text/plain" and text_part is None:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_part = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_part = body
            else:
                text_part = body

    return html_part or text_part or ""


def _extract_blurb(anchor_tag) -> str | None:
    """
    Extract a blurb (the newsletter author's description) for a link by walking up
    the DOM to find the nearest block element with meaningful surrounding text.
    Falls back to the link text itself if it is substantive (>= 15 chars).
    Returns None if no meaningful context is found.
    """
    link_text = anchor_tag.get_text(strip=True)
    node = anchor_tag.parent
    for _ in range(6):
        if node is None:
            break
        tag_name = getattr(node, "name", None)
        if tag_name in ("p", "td", "li", "div", "section", "article", "blockquote"):
            full_text = node.get_text(separator=" ", strip=True)
            # Only use if there's substantively more text than just the link itself
            if len(full_text) > len(link_text) + 25:
                blurb = " ".join(full_text.split())  # normalise whitespace
                return blurb[:500]
        node = node.parent
    # Fallback: use link text if it's a meaningful description (not just "Read more")
    if len(link_text) >= 15:
        return link_text[:500]
    return None


def _extract_urls_with_blurbs(html_body: str) -> list[tuple[str, str | None]]:
    """
    Parse hrefs from HTML body. Returns deduplicated list of (url, blurb) pairs
    for http/https URLs, excluding tracking links, unsubscribe pages, and assets.
    blurb is the newsletter author's surrounding description of the link, or None.
    """
    soup = BeautifulSoup(html_body, "lxml")
    seen: set[str] = set()
    results: list[tuple[str, str | None]] = []
    for tag in soup.find_all("a", href=True):
        url: str = tag["href"].strip()
        if not _URL_RE.match(url):
            continue
        if _SKIP_URL_RE.search(url):
            continue
        # Strip fragment
        url = url.split("#")[0].rstrip("/?")
        if url and url not in seen:
            seen.add(url)
            results.append((url, _extract_blurb(tag)))
    return results


def _extract_urls(html_body: str) -> list[str]:
    """Compatibility wrapper — returns only URLs (blurbs discarded)."""
    return [url for url, _ in _extract_urls_with_blurbs(html_body)]


def _upsert_source(session: Session, sender_raw: str) -> NewsletterSource:
    """
    Parse the From header and upsert a NewsletterSource row.
    New senders get trust_weight=1.0 (default).
    """
    display_name, email_addr = parseaddr(sender_raw)
    email_addr = email_addr.lower().strip()
    if not email_addr:
        email_addr = sender_raw.lower().strip()
    display_name = _decode_str(display_name) if display_name else email_addr

    source = session.query(NewsletterSource).filter_by(sender_email=email_addr).first()
    if source is None:
        source = NewsletterSource(sender_email=email_addr, display_name=display_name)
        session.add(source)
        session.flush()
        log.info("Registered new newsletter source: %s <%s>", display_name, email_addr)
    return source


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


def fetch_new_emails(session: Session, since: Optional[datetime] = None) -> int:
    """
    Fetch all emails under the configured Gmail label (GMAIL_LABEL) since the
    last watermark (or `since` if provided). Auto-registers new senders.
    Creates Article records (processing_status=pending) for extracted URLs.
    Updates the watermark on success.

    Returns the number of new Newsletter records stored.
    """
    fetch_start = datetime.now(timezone.utc)

    watermark = since or get_watermark(session)
    after_ts = int(watermark.timestamp()) if watermark else None

    service = _get_gmail_service()
    label_id = _resolve_label_id(service, settings.gmail_label)

    # Build the Gmail search query using label name (not ID) — Gmail's q= supports
    # label:Name syntax natively and is more reliable than the labelIds parameter.
    query = f"label:{settings.gmail_label}"
    if after_ts:
        query += f" after:{after_ts}"

    log.info("Fetching Gmail messages: query=%r", query)

    # Page through all matching message IDs
    message_ids: list[str] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for m in resp.get("messages", []):
            message_ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d messages to process", len(message_ids))
    stored_count = 0

    for gmail_id in message_ids:
        # Skip already-stored messages
        if session.query(Newsletter).filter_by(gmail_id=gmail_id).first():
            continue

        try:
            # Fetch full message (raw format gives us the original RFC 2822 bytes)
            raw = service.users().messages().get(
                userId="me", id=gmail_id, format="raw"
            ).execute()

            raw_bytes = base64.urlsafe_b64decode(raw["raw"] + "==")
            msg = message_from_bytes(raw_bytes)

            sender_raw = msg.get("From", "")
            subject = _decode_str(msg.get("Subject", ""))
            date_str = msg.get("Date", "")
            try:
                email_date = parsedate_to_datetime(date_str) if date_str else None
            except Exception:
                email_date = None

            body_raw = _extract_body(msg)
            source = _upsert_source(session, sender_raw)

            newsletter = Newsletter(
                gmail_id=gmail_id,
                source_id=source.id,
                sender=sender_raw,
                subject=subject,
                date=email_date,
                body_raw=body_raw,
            )
            session.add(newsletter)
            session.flush()

            # Extract article URLs and create pending Article records
            urls_and_blurbs = _extract_urls_with_blurbs(body_raw)
            for url, blurb in urls_and_blurbs:
                article = session.query(Article).filter_by(url=url).first()
                if article is None:
                    # Check if we've already fetched this canonical URL via a different tracking link
                    article = session.query(Article).filter_by(canonical_url=url).first()
                if article is None:
                    article = Article(url=url, processing_status=ProcessingStatus.pending)
                    session.add(article)
                    session.flush()

                # Create join record if it doesn't exist yet
                existing_join = session.query(NewsletterArticle).filter_by(
                    newsletter_id=newsletter.id, article_id=article.id
                ).first()
                if existing_join is None:
                    session.add(NewsletterArticle(
                        newsletter_id=newsletter.id,
                        article_id=article.id,
                        blurb=blurb,
                    ))
                elif blurb and not existing_join.blurb:
                    # Backfill blurb if we now have one and didn't before
                    existing_join.blurb = blurb

            session.commit()
            stored_count += 1
            blurb_count = sum(1 for _, b in urls_and_blurbs if b)
            log.info("Stored [%d/%d] %s — %s (%d URLs, %d w/blurb)", stored_count, len(message_ids), source.sender_email, subject, len(urls_and_blurbs), blurb_count)

        except Exception as exc:
            log.warning("Skipping email %s due to error: %s", gmail_id, exc)
            session.rollback()

    set_watermark(session, fetch_start)
    session.commit()
    return stored_count
