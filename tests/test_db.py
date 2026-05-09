"""Tests for DB models and session — tables create correctly, basic CRUD works."""
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.db.models import (
    Base, Article, CanonicalStory, NewsletterSource, Newsletter,
    NewsletterArticle, ArticleSummary, Topic, Digest, DigestTopic,
    TopicArticle, PipelineWatermark, ProcessingStatus, TopicStatus,
)


@pytest.fixture
def engine():
    """In-memory SQLite engine per test."""
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


def test_all_tables_created(engine):
    inspector = inspect(engine)
    expected = {
        "newsletter_sources", "newsletters", "articles", "canonical_stories",
        "newsletter_articles", "article_summaries", "topics", "topic_articles",
        "digests", "digest_topics", "pipeline_watermarks",
    }
    assert expected.issubset(set(inspector.get_table_names()))


def test_newsletter_source_crud(session):
    src = NewsletterSource(sender_email="test@example.com", display_name="Test")
    session.add(src)
    session.commit()

    fetched = session.query(NewsletterSource).filter_by(sender_email="test@example.com").one()
    assert fetched.display_name == "Test"
    assert fetched.trust_weight == 1.0


def test_article_default_status(session):
    article = Article(url="https://example.com/article-1")
    session.add(article)
    session.commit()

    fetched = session.query(Article).filter_by(url="https://example.com/article-1").one()
    assert fetched.processing_status == ProcessingStatus.pending
    assert fetched.is_paywalled is False
    assert fetched.canonical_story_id is None


def test_article_url_unique(session):
    from sqlalchemy.exc import IntegrityError

    session.add(Article(url="https://example.com/dup"))
    session.commit()
    session.add(Article(url="https://example.com/dup"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_canonical_story_article_relationship(session):
    article = Article(url="https://example.com/story-1")
    session.add(article)
    session.flush()

    story = CanonicalStory(
        representative_article_id=article.id,
        article_ids=[article.id],
    )
    session.add(story)
    session.flush()

    article.canonical_story_id = story.id
    session.commit()

    fetched = session.query(CanonicalStory).first()
    assert fetched.representative_article_id == article.id
    assert len(fetched.articles) == 1


def test_pipeline_watermark_upsert(session):
    from src.gmail_client import get_watermark, set_watermark
    from datetime import datetime, timezone

    assert get_watermark(session) is None

    ts = datetime(2026, 5, 1, 12, 0, 0)
    set_watermark(session, ts)
    session.commit()

    result = get_watermark(session)
    assert result == ts


def test_digest_topic_relationship(session):
    digest = Digest()
    topic = Topic(name="LLM Research", status=TopicStatus.active)
    session.add_all([digest, topic])
    session.flush()

    dt = DigestTopic(digest_id=digest.id, topic_id=topic.id, significance_score=7.5)
    session.add(dt)
    session.commit()

    fetched = session.query(DigestTopic).first()
    assert fetched.significance_score == 7.5
    assert fetched.topic.name == "LLM Research"
