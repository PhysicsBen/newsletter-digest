"""Tests for src/email_sender.py."""
from __future__ import annotations

import base64
from pathlib import Path
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest

from src.email_sender import (
    _build_message,
    _markdown_to_html,
    _subject_from_path,
    send_digest_file,
    send_all_digests,
)


# ── unit tests ────────────────────────────────────────────────────────────────

def test_subject_weekly_digest():
    p = Path("output/digest_2026-05-05_2026-05-12.md")
    assert _subject_from_path(p) == "AI Digest: 2026-05-05 → 2026-05-12"


def test_subject_all_time_digest():
    p = Path("output/digest_all-time_2026-05-12.md")
    assert _subject_from_path(p) == "AI Digest: all-time → 2026-05-12"


def test_markdown_to_html_heading():
    html = _markdown_to_html("# Hello World")
    assert "<h1" in html
    assert "Hello World" in html


def test_markdown_to_html_link():
    html = _markdown_to_html("[OpenAI](https://openai.com)")
    assert 'href="https://openai.com"' in html
    assert "OpenAI" in html


def test_build_message_structure():
    msg_dict = _build_message("to@example.com", "Test Subject", "**bold**")
    assert "raw" in msg_dict

    raw_bytes = base64.urlsafe_b64decode(msg_dict["raw"] + "==")
    msg = message_from_bytes(raw_bytes)

    assert msg["To"] == "to@example.com"
    assert msg["Subject"] == "Test Subject"
    assert msg.get_content_type() == "multipart/alternative"

    parts = msg.get_payload()
    content_types = {p.get_content_type() for p in parts}
    assert "text/plain" in content_types
    assert "text/html" in content_types


def test_build_message_html_contains_bold():
    msg_dict = _build_message("to@example.com", "S", "**bold text**")
    raw_bytes = base64.urlsafe_b64decode(msg_dict["raw"] + "==")
    msg = message_from_bytes(raw_bytes)
    html_part = next(p for p in msg.get_payload() if p.get_content_type() == "text/html")
    html_content = html_part.get_payload(decode=True).decode("utf-8")
    assert "<strong>" in html_content


def test_build_message_plain_is_raw_markdown():
    body = "## Heading\n\n- item"
    msg_dict = _build_message("to@example.com", "S", body)
    raw_bytes = base64.urlsafe_b64decode(msg_dict["raw"] + "==")
    msg = message_from_bytes(raw_bytes)
    plain_part = next(p for p in msg.get_payload() if p.get_content_type() == "text/plain")
    plain_content = plain_part.get_payload(decode=True).decode("utf-8")
    assert "## Heading" in plain_content


# ── integration-style tests (Gmail API mocked) ────────────────────────────────

@pytest.fixture
def tmp_digest(tmp_path: Path) -> Path:
    f = tmp_path / "digest_2026-05-05_2026-05-12.md"
    f.write_text("# AI Digest\n\n- Some story", encoding="utf-8")
    return f


def _mock_gmail_service():
    service = MagicMock()
    send_mock = MagicMock()
    send_mock.execute.return_value = {"id": "msg123"}
    service.users.return_value.messages.return_value.send.return_value = send_mock
    return service


def test_send_digest_file_calls_gmail_api(tmp_digest: Path):
    service = _mock_gmail_service()
    with patch("src.email_sender._get_credentials", return_value=MagicMock()), \
         patch("src.email_sender.build", return_value=service):
        send_digest_file(tmp_digest, "test@example.com")

    service.users().messages().send.assert_called_once()
    _, kwargs = service.users().messages().send.call_args
    assert kwargs["userId"] == "me"
    assert "raw" in kwargs["body"]


def test_send_digest_file_raises_if_no_recipient(tmp_digest: Path):
    with pytest.raises(ValueError, match="digest_recipient_email"):
        send_digest_file(tmp_digest, "")


def test_send_digest_file_raises_if_missing_file():
    with pytest.raises(FileNotFoundError):
        send_digest_file(Path("output/nonexistent.md"), "test@example.com")


def test_send_all_digests_sends_each_file(tmp_path: Path):
    for name in ["digest_2026-04-28_2026-05-05.md", "digest_2026-05-05_2026-05-12.md"]:
        (tmp_path / name).write_text("# Digest", encoding="utf-8")

    service = _mock_gmail_service()
    with patch("src.email_sender._get_credentials", return_value=MagicMock()), \
         patch("src.email_sender.build", return_value=service), \
         patch("src.email_sender._SEND_DELAY_S", 0):
        count = send_all_digests(digest_dir=tmp_path, recipient="test@example.com")

    assert count == 2
    assert service.users().messages().send.call_count == 2


def test_send_all_digests_raises_if_no_recipient(tmp_path: Path):
    with pytest.raises(ValueError, match="digest_recipient_email"):
        send_all_digests(digest_dir=tmp_path, recipient="")


def test_send_all_digests_empty_dir(tmp_path: Path):
    service = _mock_gmail_service()
    with patch("src.email_sender._get_credentials", return_value=MagicMock()), \
         patch("src.email_sender.build", return_value=service):
        count = send_all_digests(digest_dir=tmp_path, recipient="test@example.com")

    assert count == 0
    service.users().messages().send.assert_not_called()


def test_send_all_digests_sorted_oldest_first(tmp_path: Path):
    """Files should be sent in alphabetical (i.e. chronological) order."""
    names = [
        "digest_2026-05-05_2026-05-12.md",
        "digest_2026-04-28_2026-05-05.md",
        "digest_2026-04-21_2026-04-28.md",
    ]
    for name in names:
        (tmp_path / name).write_text("# D", encoding="utf-8")

    sent_subjects: list[str] = []

    def capture_send(userId, body):
        raw = base64.urlsafe_b64decode(body["raw"] + "==")
        msg = message_from_bytes(raw)
        sent_subjects.append(msg["Subject"])
        mock = MagicMock()
        mock.execute.return_value = {}
        return mock

    service = MagicMock()
    service.users.return_value.messages.return_value.send.side_effect = capture_send

    with patch("src.email_sender._get_credentials", return_value=MagicMock()), \
         patch("src.email_sender.build", return_value=service), \
         patch("src.email_sender._SEND_DELAY_S", 0):
        send_all_digests(digest_dir=tmp_path, recipient="test@example.com")

    assert sent_subjects == sorted(sent_subjects)
