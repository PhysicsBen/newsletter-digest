"""Tests for Phase 4 — LLM summarization."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import (
    Article,
    ArticleSummary,
    Base,
    CanonicalStory,
    Newsletter,
    NewsletterArticle,
    NewsletterSource,
    ProcessingStatus,
)
from src.llm.summarizer import (
    _chunk,
    _parse_llm_response,
    _truncate,
    summarize_canonical_stories,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _make_story(session, body_text: str = "Some article body text.") -> tuple[CanonicalStory, Article]:
    article = Article(
        url=f"https://example.com/{id(body_text)}",
        body_text=body_text,
        processing_status=ProcessingStatus.done,
    )
    session.add(article)
    session.flush()
    story = CanonicalStory(representative_article_id=article.id, article_ids=[article.id])
    session.add(story)
    session.flush()
    article.canonical_story_id = story.id
    session.flush()
    return story, article


# ── unit tests ────────────────────────────────────────────────────────────────

def test_truncate_short_text():
    text = "hello world"
    assert _truncate(text, max_tokens=100) == text


def test_truncate_long_text():
    text = "x" * 10000
    result = _truncate(text, max_tokens=100)
    assert len(result) == 400  # 100 tokens * 4 chars/token


def test_chunk_splits_evenly():
    text = "a" * 800
    chunks = _chunk(text, chunk_tokens=100)  # 400 chars each
    assert len(chunks) == 2
    assert all(len(c) == 400 for c in chunks)


def test_chunk_single_chunk():
    text = "short"
    chunks = _chunk(text, chunk_tokens=100)
    assert chunks == ["short"]


def test_parse_llm_response_plain_json():
    raw = '{"summary": "Great article.", "significance_score": 7.5}'
    summary, score = _parse_llm_response(raw)
    assert summary == "Great article."
    assert score == 7.5


def test_parse_llm_response_with_code_fence():
    raw = '```json\n{"summary": "Cool stuff.", "significance_score": 4.0}\n```'
    summary, score = _parse_llm_response(raw)
    assert summary == "Cool stuff."
    assert score == 4.0


def test_parse_llm_response_invalid_raises():
    with pytest.raises((json.JSONDecodeError, KeyError)):
        _parse_llm_response("not json at all")


# ── integration tests (LLM mocked) ───────────────────────────────────────────

def test_no_stories_returns_zero(session):
    result = summarize_canonical_stories(session)
    assert result == 0


def test_summarizes_single_story(session):
    story, _ = _make_story(session, "Interesting AI research body text here.")
    llm_response = '{"summary": "AI research summary.", "significance_score": 6.0}'

    with patch("src.llm.summarizer.call_llm", return_value=llm_response):
        count = summarize_canonical_stories(session)

    assert count == 1
    saved = session.query(ArticleSummary).filter_by(canonical_story_id=story.id).one()
    assert saved.summary_text == "AI research summary."
    assert saved.significance_score == 6.0
    assert saved.prompt_version == "v1.0"


def test_skips_already_summarized_story(session):
    story, article = _make_story(session)
    summary = ArticleSummary(
        canonical_story_id=story.id,
        article_id=article.id,
        summary_text="Already done.",
        significance_score=5.0,
        model_used="test",
        prompt_version="v1.0",
    )
    session.add(summary)
    session.commit()

    with patch("src.llm.summarizer.call_llm") as mock_llm:
        count = summarize_canonical_stories(session)

    mock_llm.assert_not_called()
    assert count == 0


def test_skips_story_with_no_body_text(session):
    article = Article(url="https://example.com/empty", body_text=None, processing_status=ProcessingStatus.done)
    session.add(article)
    session.flush()
    story = CanonicalStory(representative_article_id=article.id, article_ids=[article.id])
    session.add(story)
    session.commit()

    with patch("src.llm.summarizer.call_llm") as mock_llm:
        count = summarize_canonical_stories(session)

    mock_llm.assert_not_called()
    assert count == 0


def test_llm_failure_skips_and_continues(session):
    story1, _ = _make_story(session, "Body text 1")
    story2, _ = _make_story(session, "Body text 2")

    call_count = 0

    def mock_llm(messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("API error")
        return '{"summary": "Second story.", "significance_score": 5.0}'

    with patch("src.llm.summarizer.call_llm", side_effect=mock_llm):
        count = summarize_canonical_stories(session)

    assert count == 1
    summaries = session.query(ArticleSummary).all()
    assert len(summaries) == 1
    assert summaries[0].summary_text == "Second story."


def test_uses_trust_weight_from_newsletter_source(session):
    story, article = _make_story(session, "Trusted source article.")

    source = NewsletterSource(sender_email="trusted@example.com", display_name="Trusted", trust_weight=2.0)
    session.add(source)
    session.flush()
    newsletter = Newsletter(gmail_id="msg1", source_id=source.id, sender="trusted@example.com", subject="Test")
    session.add(newsletter)
    session.flush()
    na = NewsletterArticle(newsletter_id=newsletter.id, article_id=article.id)
    session.add(na)
    session.commit()

    captured = {}

    def mock_llm(messages, **kwargs):
        captured["messages"] = messages
        return '{"summary": "Trusted summary.", "significance_score": 8.0}'

    with patch("src.llm.summarizer.call_llm", side_effect=mock_llm):
        summarize_canonical_stories(session)

    user_content = captured["messages"][1]["content"]
    assert "2.00" in user_content
