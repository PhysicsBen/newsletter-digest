"""
Send digest Markdown files to an email inbox via Gmail API.

Converts Markdown → HTML and sends a multipart/alternative email (plain + HTML).
Uses the same OAuth2 credentials as gmail_client.py (token.json / credentials.json).
Requires the gmail.send scope — token.json will be regenerated automatically if
it was issued with gmail.readonly only.
"""
from __future__ import annotations

import base64
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown as md
from googleapiclient.discovery import build

from src.config import settings
from src.gmail_client import _get_credentials

log = logging.getLogger(__name__)

# Seconds to wait between sends to stay well within Gmail's sending rate limit.
_SEND_DELAY_S = 2


def _markdown_to_html(text: str) -> str:
    """Convert Markdown text to an HTML string."""
    return md.markdown(
        text,
        extensions=["tables", "fenced_code", "toc"],
        output_format="html",
    )


def _build_message(recipient: str, subject: str, markdown_body: str) -> dict:
    """Build a Gmail API message dict from a Markdown body."""
    html_body = _markdown_to_html(markdown_body)

    msg = MIMEMultipart("alternative")
    msg["To"] = recipient
    msg["From"] = "me"
    msg["Subject"] = subject

    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw}


def _subject_from_path(path: Path) -> str:
    """Derive an email subject from the digest filename.

    E.g. digest_2026-05-05_2026-05-12.md → 'AI Digest: 2026-05-05 → 2026-05-12'
         digest_all-time_2026-05-12.md   → 'AI Digest: all-time → 2026-05-12'
    """
    stem = path.stem  # e.g. 'digest_2026-05-05_2026-05-12'
    parts = stem.split("_", 1)
    if len(parts) == 2:
        date_part = parts[1].replace("_", " \u2192 ")
        return f"AI Digest: {date_part}"
    return f"AI Digest: {stem}"


def send_digest_file(path: Path, recipient: str) -> None:
    """Send a single digest Markdown file as an email.

    Args:
        path: Path to the .md digest file.
        recipient: Destination email address.

    Raises:
        ValueError: If recipient is empty.
        FileNotFoundError: If the digest file does not exist.
    """
    if not recipient:
        raise ValueError(
            "digest_recipient_email is not set. Add DIGEST_RECIPIENT_EMAIL=you@example.com to .env"
        )
    if not path.exists():
        raise FileNotFoundError(f"Digest file not found: {path}")

    subject = _subject_from_path(path)
    body = path.read_text(encoding="utf-8")

    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message = _build_message(recipient, subject, body)
    service.users().messages().send(userId="me", body=message).execute()
    log.info("Sent %s → %s", path.name, recipient)


def send_all_digests(digest_dir: Path | None = None, recipient: str | None = None) -> int:
    """Send all .md digest files in digest_dir, sorted oldest-first by filename.

    Returns the number of emails sent.
    """
    digest_dir = digest_dir or Path("output")
    recipient = recipient or settings.digest_recipient_email

    if not recipient:
        raise ValueError(
            "digest_recipient_email is not set. Add DIGEST_RECIPIENT_EMAIL=you@example.com to .env"
        )

    files = sorted(
        f for f in digest_dir.glob("*.md") if f.name != ".gitkeep"
    )
    if not files:
        log.info("No digest files found in %s", digest_dir)
        return 0

    creds = _get_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    sent = 0
    for path in files:
        subject = _subject_from_path(path)
        body = path.read_text(encoding="utf-8")
        message = _build_message(recipient, subject, body)
        service.users().messages().send(userId="me", body=message).execute()
        log.info("Sent %s → %s  (%d / %d)", path.name, recipient, sent + 1, len(files))
        sent += 1
        if sent < len(files):
            time.sleep(_SEND_DELAY_S)

    return sent
