"""Tests for backtest/date-range functionality in pipeline and topic_clusterer."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import (
    Article,
    ArticleSummary,
    Base,
    CanonicalStory,
    Digest,
    DigestTopic,
    Newsletter,
    NewsletterArticle,
    NewsletterSource,
    ProcessingStatus,
    Topic,
    TopicArticle,
    TopicStatus,
)
from src.llm.topic_clusterer import cluster_topics


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _make_article_with_newsletter(session, url: str, newsletter_date: datetime, summary_text: str = "A summary.") -> Article:
    """Create a NewsletterSource, Newsletter, Article, CanonicalStory and ArticleSummary."""
    source = session.query(NewsletterSource).filter_by(sender_email="src@test.com").first()
    if source is None:
        source = NewsletterSource(sender_email="src@test.com", display_name="Test")
        session.add(source)
        session.flush()

    newsletter = Newsletter(
        gmail_id=f"gmail-{url}",
        source_id=source.id,
        sender="src@test.com",
        subject="Weekly AI",
        date=newsletter_date,
    )
    session.add(newsletter)
    session.flush()

    article = Article(url=url, processing_status=ProcessingStatus.done)
    session.add(article)
    session.flush()

    story = CanonicalStory(representative_article_id=article.id, article_ids=[article.id])
    session.add(story)
    session.flush()

    article.canonical_story_id = story.id
    session.flush()

    na = NewsletterArticle(newsletter_id=newsletter.id, article_id=article.id)
    session.add(na)
    session.flush()

    summary = ArticleSummary(
        canonical_story_id=story.id,
        article_id=article.id,
        summary_text=summary_text,
        significance_score=5.0,
        prompt_version="v1.0",
    )
    session.add(summary)
    session.flush()

    return article


# ── date-range filtering ───────────────────────────────────────────────────────

_FAKE_EMBEDDING = np.zeros(384, dtype=np.float32)
_FAKE_EMBEDDING[0] = 1.0


def _fake_encode(texts, **kwargs):
    return np.tile(_FAKE_EMBEDDING, (len(texts), 1))


def _fake_llm(messages):
    return '[{"name": "Test Topic", "description": "A test topic."}]'


def test_date_range_filters_summaries(session):
    """cluster_topics with date_start/date_end only includes summaries from that window."""
    now = datetime(2026, 5, 11)
    week_start = now - timedelta(weeks=1)  # 2026-05-04
    week_end = now                          # 2026-05-11

    # Article in window
    in_window = _make_article_with_newsletter(
        session, "https://example.com/in", newsletter_date=datetime(2026, 5, 7)
    )
    # Article outside window (older)
    _make_article_with_newsletter(
        session, "https://example.com/out", newsletter_date=datetime(2026, 4, 1)
    )

    digest = Digest(date_range_start=week_start, date_range_end=week_end)
    session.add(digest)
    session.flush()

    model_path = "src.llm.topic_clusterer.SentenceTransformer"
    llm_path = "src.llm.topic_clusterer.call_llm"

    mock_model = type("M", (), {"encode": staticmethod(_fake_encode)})()

    with patch(model_path, return_value=mock_model), patch(llm_path, side_effect=_fake_llm):
        count = cluster_topics(session, digest, date_start=week_start, date_end=week_end)

    assert count == 1

    # Only the in-window article should be in a TopicArticle for this digest
    topic_articles = session.query(TopicArticle).filter_by(digest_id=digest.id).all()
    article_ids = {ta.article_id for ta in topic_articles}
    assert in_window.id in article_ids


def test_out_of_window_article_excluded(session):
    """Articles outside the date window should not appear in the digest."""
    now = datetime(2026, 5, 11)
    week_start = now - timedelta(weeks=1)
    week_end = now

    out_of_window = _make_article_with_newsletter(
        session, "https://example.com/old", newsletter_date=datetime(2026, 3, 1)
    )

    digest = Digest(date_range_start=week_start, date_range_end=week_end)
    session.add(digest)
    session.flush()

    mock_model = type("M", (), {"encode": staticmethod(_fake_encode)})()

    with patch("src.llm.topic_clusterer.SentenceTransformer", return_value=mock_model), \
         patch("src.llm.topic_clusterer.call_llm", side_effect=_fake_llm):
        count = cluster_topics(session, digest, date_start=week_start, date_end=week_end)

    assert count == 0
    topic_articles = session.query(TopicArticle).filter_by(digest_id=digest.id).all()
    assert all(ta.article_id != out_of_window.id for ta in topic_articles)


def test_no_date_filter_uses_unassigned(session):
    """Without date params, cluster_topics only picks up summaries not yet in any TopicArticle."""
    now = datetime(2026, 5, 11)

    unassigned = _make_article_with_newsletter(
        session, "https://example.com/new", newsletter_date=now
    )
    already_assigned = _make_article_with_newsletter(
        session, "https://example.com/old", newsletter_date=now
    )

    # Pre-assign the second article to an existing digest
    prior_digest = Digest(date_range_start=now - timedelta(weeks=2), date_range_end=now - timedelta(weeks=1))
    session.add(prior_digest)
    session.flush()
    prior_topic = Topic(name="Prior", status=TopicStatus.active, first_seen=now, last_seen=now)
    session.add(prior_topic)
    session.flush()
    session.add(TopicArticle(topic_id=prior_topic.id, article_id=already_assigned.id, digest_id=prior_digest.id))
    session.flush()

    digest = Digest(date_range_start=now - timedelta(weeks=1), date_range_end=now)
    session.add(digest)
    session.flush()

    mock_model = type("M", (), {"encode": staticmethod(_fake_encode)})()

    with patch("src.llm.topic_clusterer.SentenceTransformer", return_value=mock_model), \
         patch("src.llm.topic_clusterer.call_llm", side_effect=_fake_llm):
        count = cluster_topics(session, digest)

    assert count == 1
    topic_articles = session.query(TopicArticle).filter_by(digest_id=digest.id).all()
    article_ids = {ta.article_id for ta in topic_articles}
    assert unassigned.id in article_ids
    assert already_assigned.id not in article_ids


# ── run_backtest week boundary generation ─────────────────────────────────────

def test_backtest_generates_correct_week_count():
    """run_backtest should produce exactly N weekly digests."""
    from unittest.mock import MagicMock, call
    from src.pipeline import run_backtest

    call_log: list[tuple[datetime, datetime]] = []

    def fake_cluster_topics(session, digest, date_start=None, date_end=None):
        call_log.append((date_start, date_end))
        return 0

    def fake_write_digest(session, digest):
        return "output/fake.md"

    with patch("src.pipeline.init_db"), \
         patch("src.pipeline.cluster_topics", side_effect=fake_cluster_topics), \
         patch("src.pipeline.write_digest", side_effect=fake_write_digest), \
         patch("src.pipeline.get_session") as mock_gs:
        mock_session = MagicMock()
        mock_session.__enter__ = lambda s: mock_session
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.add = MagicMock()
        mock_session.flush = MagicMock()
        mock_session.commit = MagicMock()
        mock_gs.return_value = mock_session

        run_backtest(8)

    assert len(call_log) == 8

    # Each window should be 7 days wide
    for start, end in call_log:
        assert (end - start).days == 7

    # Weeks should be contiguous and oldest-first
    for i in range(1, len(call_log)):
        assert call_log[i][0] == call_log[i - 1][1]
