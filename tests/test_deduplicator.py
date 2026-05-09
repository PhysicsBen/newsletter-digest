"""Tests for Phase 3 — semantic deduplication."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Article, Base, CanonicalStory, ProcessingStatus
from src.llm.deduplicator import cluster_into_canonical_stories


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _make_article(session, url: str, body: str, status: ProcessingStatus = ProcessingStatus.done) -> Article:
    a = Article(url=url, body_text=body, processing_status=status)
    session.add(a)
    session.flush()
    return a


# ── helpers ──────────────────────────────────────────────────────────────────

def _unit_vec(dim: int, idx: int, size: int = 10) -> list[float]:
    """Return a normalised embedding pointing mostly in direction `idx`."""
    v = np.zeros(size, dtype=np.float32)
    v[idx % size] = 1.0
    return v.tolist()


def _similar_vecs(n: int, size: int = 10) -> list[list[float]]:
    """Return `n` nearly-identical unit vectors (cosine similarity ≈ 1)."""
    base = np.zeros(size, dtype=np.float32)
    base[0] = 1.0
    return [base.tolist()] * n


# ── tests ─────────────────────────────────────────────────────────────────────

def test_no_articles_returns_zero(session):
    result = cluster_into_canonical_stories(session)
    assert result == 0


def test_skips_pending_articles(session):
    _make_article(session, "https://a.com/1", "text", ProcessingStatus.pending)
    with patch("src.llm.deduplicator.embed_articles") as mock_embed:
        result = cluster_into_canonical_stories(session)
    mock_embed.assert_not_called()
    assert result == 0


def test_skips_articles_already_assigned(session):
    story = CanonicalStory(representative_article_id=1, article_ids=[1], embedding=[0.0])
    session.add(story)
    session.flush()

    a = _make_article(session, "https://a.com/1", "text")
    a.canonical_story_id = story.id
    session.flush()

    with patch("src.llm.deduplicator.embed_articles") as mock_embed:
        result = cluster_into_canonical_stories(session)
    mock_embed.assert_not_called()
    assert result == 0


def test_single_article_creates_one_story(session):
    a = _make_article(session, "https://a.com/1", "some article text here")
    fake_embedding = [_unit_vec(10, 0)]

    with patch("src.llm.deduplicator.embed_articles", return_value=fake_embedding):
        result = cluster_into_canonical_stories(session)

    assert result == 1
    assert a.canonical_story_id is not None
    story = session.get(CanonicalStory, a.canonical_story_id)
    assert story.representative_article_id == a.id
    assert story.article_ids == [a.id]


def test_similar_articles_merged_into_one_story(session):
    a1 = _make_article(session, "https://a.com/1", "AI breakthrough story")
    a2 = _make_article(session, "https://b.com/1", "AI breakthrough story copy")

    # Both articles get nearly-identical embeddings → should cluster together
    similar = _similar_vecs(2)
    with patch("src.llm.deduplicator.embed_articles", return_value=similar):
        with patch("src.llm.deduplicator.settings") as mock_settings:
            mock_settings.dedup_similarity_threshold = 0.50  # low threshold → always merge
            result = cluster_into_canonical_stories(session)

    assert result == 1
    assert a1.canonical_story_id == a2.canonical_story_id
    story = session.get(CanonicalStory, a1.canonical_story_id)
    assert set(story.article_ids) == {a1.id, a2.id}


def test_dissimilar_articles_become_separate_stories(session):
    a1 = _make_article(session, "https://a.com/1", "Quantum computing paper")
    a2 = _make_article(session, "https://b.com/1", "New JavaScript framework released")

    # Orthogonal embeddings → cosine similarity == 0
    size = 10
    v1 = np.zeros(size, dtype=np.float32)
    v1[0] = 1.0
    v2 = np.zeros(size, dtype=np.float32)
    v2[1] = 1.0
    embeddings = [v1.tolist(), v2.tolist()]

    with patch("src.llm.deduplicator.embed_articles", return_value=embeddings):
        with patch("src.llm.deduplicator.settings") as mock_settings:
            mock_settings.dedup_similarity_threshold = 0.99  # very high → never merge
            result = cluster_into_canonical_stories(session)

    assert result == 2
    assert a1.canonical_story_id != a2.canonical_story_id


def test_representative_is_longest_article(session):
    short = _make_article(session, "https://a.com/short", "short")
    long_ = _make_article(session, "https://a.com/long", "much longer article body text here")

    similar = _similar_vecs(2)
    with patch("src.llm.deduplicator.embed_articles", return_value=similar):
        with patch("src.llm.deduplicator.settings") as mock_settings:
            mock_settings.dedup_similarity_threshold = 0.50
            cluster_into_canonical_stories(session)

    story = session.get(CanonicalStory, short.canonical_story_id)
    assert story.representative_article_id == long_.id


def test_canonical_story_has_centroid_embedding(session):
    a1 = _make_article(session, "https://a.com/1", "article one")
    a2 = _make_article(session, "https://b.com/2", "article two")

    size = 4
    v1 = [1.0, 0.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0, 0.0]  # identical → centroid == same
    embeddings = [v1, v2]

    with patch("src.llm.deduplicator.embed_articles", return_value=embeddings):
        with patch("src.llm.deduplicator.settings") as mock_settings:
            mock_settings.dedup_similarity_threshold = 0.50
            cluster_into_canonical_stories(session)

    story = session.get(CanonicalStory, a1.canonical_story_id)
    assert story.embedding is not None
    assert len(story.embedding) == size


def test_skips_articles_with_empty_body(session):
    _make_article(session, "https://a.com/empty", "")

    with patch("src.llm.deduplicator.embed_articles") as mock_embed:
        result = cluster_into_canonical_stories(session)

    mock_embed.assert_not_called()
    assert result == 0


def test_skips_articles_with_null_body(session):
    a = Article(url="https://a.com/null", body_text=None, processing_status=ProcessingStatus.done)
    session.add(a)
    session.flush()

    with patch("src.llm.deduplicator.embed_articles") as mock_embed:
        result = cluster_into_canonical_stories(session)

    mock_embed.assert_not_called()
    assert result == 0
