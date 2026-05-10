"""
Tests for gmail_client.py.

All Gmail API calls are mocked — no real credentials required.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import (
    Article, Base, Newsletter, NewsletterArticle, NewsletterSource,
    PipelineWatermark, ProcessingStatus,
)
from src.gmail_client import (
    _decode_str,
    _extract_body,
    _extract_blurb,
    _extract_urls,
    _extract_urls_with_blurbs,
    _upsert_source,
    fetch_new_emails,
    get_watermark,
    set_watermark,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)


@pytest.fixture
def session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()


def _make_raw_email(
    from_addr: str = "Test Newsletter <test@example.com>",
    subject: str = "Weekly AI Digest",
    html_body: str = "<html><body><a href='https://example.com/article-1'>Link</a></body></html>",
    date: str = "Sat, 09 May 2026 12:00:00 +0000",
) -> str:
    """Build a base64url-encoded raw RFC 2822 email for Gmail API mocking."""
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = "<test@example.com>"
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode()


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------

def test_extract_urls_basic():
    html = """
    <html><body>
      <a href="https://example.com/article-1">Article 1</a>
      <a href="https://other.com/post">Post</a>
      <a href="mailto:unsubscribe@example.com">Unsub</a>
      <a href="ftp://old.server/file">FTP</a>
    </body></html>
    """
    urls = _extract_urls(html)
    assert "https://example.com/article-1" in urls
    assert "https://other.com/post" in urls
    # mailto and ftp must be excluded
    assert not any("mailto" in u for u in urls)
    assert not any("ftp" in u for u in urls)


def test_extract_urls_deduplication():
    html = """
    <a href="https://example.com/article">First</a>
    <a href="https://example.com/article">Duplicate</a>
    <a href="https://example.com/article#section">Fragment</a>
    """
    urls = _extract_urls(html)
    assert urls.count("https://example.com/article") == 1


def test_extract_urls_skips_tracking():
    html = """
    <a href="https://click.mailchimp.com/track/click/xxx">Track</a>
    <a href="https://example.com/unsubscribe?id=123">Unsub</a>
    <a href="https://example.com/real-article">Real</a>
    <a href="https://open.example.com/pixel.png">Pixel</a>
    """
    urls = _extract_urls(html)
    assert urls == ["https://example.com/real-article"]


def test_extract_urls_skips_beehiiv_tracking():
    html = """
    <a href="https://link.mail.beehiiv.com/ss/c/u001.xxx">Author name</a>
    <a href="https://email.beehiivstatus.com/abc123/hclick">Pixel</a>
    <a href="https://example.com/real-article">Real Article</a>
    """
    urls = _extract_urls(html)
    assert urls == ["https://example.com/real-article"]


def test_extract_urls_with_blurbs_returns_blurb():
    html = """
    <div>
      <p><a href="https://example.com/story">Breaking: LLM outperforms humans</a> — 
      This is a significant result showing that modern LLMs can now surpass humans 
      on a range of complex reasoning tasks.</p>
    </div>
    """
    results = _extract_urls_with_blurbs(html)
    assert len(results) == 1
    url, blurb = results[0]
    assert url == "https://example.com/story"
    assert blurb is not None
    assert "significant result" in blurb


def test_extract_urls_with_blurbs_fallback_to_link_text():
    """If no parent context, long link text is used as blurb."""
    html = '<a href="https://example.com/story">New approach to training transformer models achieves SOTA</a>'
    results = _extract_urls_with_blurbs(html)
    assert len(results) == 1
    _, blurb = results[0]
    assert blurb == "New approach to training transformer models achieves SOTA"


def test_extract_urls_with_blurbs_none_for_short_link_text():
    """Short link text with no parent context returns None blurb."""
    html = '<a href="https://example.com/page">Read</a>'
    results = _extract_urls_with_blurbs(html)
    _, blurb = results[0]
    assert blurb is None


def test_blurb_capped_at_500_chars():
    long_context = "X" * 600
    html = f'<p>{long_context} <a href="https://example.com/article">Title</a></p>'
    results = _extract_urls_with_blurbs(html)
    _, blurb = results[0]
    assert blurb is not None
    assert len(blurb) <= 500


def test_extract_body_html_preferred():
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("Plain text fallback", "plain", "utf-8"))
    msg.attach(MIMEText("<p>HTML body</p>", "html", "utf-8"))
    from email import message_from_bytes
    parsed = message_from_bytes(msg.as_bytes())
    body = _extract_body(parsed)
    assert "<p>HTML body</p>" in body


def test_extract_body_plain_fallback():
    msg = MIMEText("Just plain text", "plain", "utf-8")
    from email import message_from_bytes
    parsed = message_from_bytes(msg.as_bytes())
    body = _extract_body(parsed)
    assert "Just plain text" in body


def test_upsert_source_new(session):
    src = _upsert_source(session, "Test Newsletter <newsletter@example.com>")
    assert src.sender_email == "newsletter@example.com"
    assert src.display_name == "Test Newsletter"
    assert src.trust_weight == 1.0


def test_upsert_source_idempotent(session):
    _upsert_source(session, "Test <newsletter@example.com>")
    _upsert_source(session, "Test <newsletter@example.com>")
    session.commit()
    count = session.query(NewsletterSource).filter_by(sender_email="newsletter@example.com").count()
    assert count == 1


def test_upsert_source_case_insensitive(session):
    _upsert_source(session, "Test <Newsletter@Example.COM>")
    session.commit()
    src = session.query(NewsletterSource).filter_by(sender_email="newsletter@example.com").first()
    assert src is not None


# ---------------------------------------------------------------------------
# Integration tests — fetch_new_emails with mocked Gmail API
# ---------------------------------------------------------------------------

def _build_mock_service(message_ids: list[str], raw_emails: dict[str, str], labels: list[dict] | None = None):
    """
    Build a mock Gmail service that returns the given message IDs and raw emails.
    labels: list of {"id": ..., "name": ...} dicts; defaults to Newsletters/AI present.
    """
    if labels is None:
        labels = [{"id": "Label_123", "name": "Newsletters/AI"}]

    service = MagicMock()
    service.users().labels().list(userId="me").execute.return_value = {"labels": labels}

    # messages().list() returns all IDs in one page
    service.users().messages().list(
        userId="me", q=MagicMock(), maxResults=500
    ).execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids]
    }

    def get_raw(userId, id, format):
        mock = MagicMock()
        mock.execute.return_value = {"raw": raw_emails[id]}
        return mock

    service.users().messages().get.side_effect = get_raw
    return service


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_stores_newsletter(mock_svc, session):
    raw = _make_raw_email(
        from_addr="AI Weekly <ai@weekly.com>",
        subject="AI Digest #1",
        html_body='<a href="https://example.com/story-1">Story 1</a>',
    )
    mock_svc.return_value = _build_mock_service(["msg001"], {"msg001": raw})

    count = fetch_new_emails(session)

    assert count == 1
    nl = session.query(Newsletter).filter_by(gmail_id="msg001").one()
    assert nl.subject == "AI Digest #1"
    assert nl.source.sender_email == "ai@weekly.com"


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_creates_pending_articles(mock_svc, session):
    raw = _make_raw_email(
        html_body='<a href="https://example.com/a1">A1</a><a href="https://example.com/a2">A2</a>',
    )
    mock_svc.return_value = _build_mock_service(["msg001"], {"msg001": raw})

    fetch_new_emails(session)

    articles = session.query(Article).all()
    urls = {a.url for a in articles}
    assert "https://example.com/a1" in urls
    assert "https://example.com/a2" in urls
    for a in articles:
        assert a.processing_status == ProcessingStatus.pending


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_skips_duplicate_gmail_id(mock_svc, session):
    raw = _make_raw_email()
    mock_svc.return_value = _build_mock_service(["msg001"], {"msg001": raw})

    first = fetch_new_emails(session)
    second = fetch_new_emails(session)

    assert first == 1
    assert second == 0
    assert session.query(Newsletter).count() == 1


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_deduplicates_article_urls(mock_svc, session):
    """Same URL in two different newsletters creates only one Article row."""
    raw1 = _make_raw_email(
        from_addr="NL1 <nl1@example.com>",
        subject="NL1",
        html_body='<a href="https://shared.com/story">Shared</a>',
    )
    raw2 = _make_raw_email(
        from_addr="NL2 <nl2@example.com>",
        subject="NL2",
        html_body='<a href="https://shared.com/story">Shared again</a>',
    )
    mock_svc.return_value = _build_mock_service(
        ["msg001", "msg002"], {"msg001": raw1, "msg002": raw2}
    )

    fetch_new_emails(session)

    assert session.query(Article).filter_by(url="https://shared.com/story").count() == 1
    assert session.query(NewsletterArticle).count() == 2  # both newsletters linked


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_updates_watermark(mock_svc, session):
    raw = _make_raw_email()
    mock_svc.return_value = _build_mock_service(["msg001"], {"msg001": raw})

    assert get_watermark(session) is None
    fetch_new_emails(session)
    assert get_watermark(session) is not None


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_stores_blurb(mock_svc, session):
    """blurb is extracted and stored in NewsletterArticle."""
    raw = _make_raw_email(
        html_body='<p><a href="https://example.com/story">A major AI breakthrough was announced today</a> — researchers achieved a new SOTA result on reasoning benchmarks.</p>',
    )
    mock_svc.return_value = _build_mock_service(["msg001"], {"msg001": raw})
    fetch_new_emails(session)
    join = session.query(NewsletterArticle).one()
    assert join.blurb is not None
    assert "breakthrough" in join.blurb or "SOTA" in join.blurb


@patch("src.gmail_client._get_gmail_service")
def test_fetch_new_emails_label_not_found_raises(mock_svc, session):
    service = MagicMock()
    service.users().labels().list(userId="me").execute.return_value = {
        "labels": [{"id": "Label_1", "name": "Other Label"}]
    }
    mock_svc.return_value = service

    with pytest.raises(ValueError, match="Newsletters/AI"):
        fetch_new_emails(session)
