"""Tests for article_fetcher utility functions."""
import json
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.article_fetcher import canonicalize_url, _is_soft_paywalled, fetch_pending_articles
from src.db.models import Article, Base, ProcessingStatus


def test_canonicalize_url_strips_fragment():
    url = "https://example.com/article?ref=newsletter#section"
    result = canonicalize_url(url)
    assert "#section" not in result
    assert "example.com/article" in result


def test_canonicalize_url_preserves_query():
    url = "https://example.com/article?id=123"
    assert canonicalize_url(url) == "https://example.com/article?id=123"


@pytest.mark.parametrize("text", [
    "Subscribe to read the full article",
    "This content is for members only.",
    "Sign in to read more",
    "Create a free account to read this story",
])
def test_soft_paywall_detection(text):
    assert _is_soft_paywalled(text) is True


def test_no_soft_paywall(text="This is a normal article with full content available."):
    assert _is_soft_paywalled(text) is False


# ---------------------------------------------------------------------------
# fetch_pending_articles integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    """Fresh in-memory SQLite session per test.

    Uses StaticPool so all connections (including worker threads) share one
    underlying SQLite connection and see each other's committed data.
    get_session is patched so _fetch_one_threaded uses this engine too.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()

    @contextmanager
    def _mock_get_session():
        ts = SessionLocal()
        try:
            yield ts
        except Exception:
            ts.rollback()
            raise
        finally:
            ts.close()

    with patch("src.article_fetcher.get_session", _mock_get_session):
        yield s
    s.close()
    Base.metadata.drop_all(engine)


def _make_article(session, url="https://example.com/story-1", status=ProcessingStatus.pending):
    a = Article(url=url, processing_status=status)
    session.add(a)
    session.flush()
    return a


def _mock_response(status_code=200, text="<html><body><p>Article body text here.</p></body></html>"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


@patch("src.article_fetcher.httpx.Client")
@patch("src.article_fetcher.trafilatura.extract")
def test_fetch_marks_done(mock_extract, mock_client_cls, session):
    _make_article(session)
    session.commit()

    mock_extract.return_value = json.dumps({"text": "Full article text.", "date": "2025-01-15"})
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = _mock_response()
    mock_client_cls.return_value = client

    done, failed = fetch_pending_articles(session)

    assert done == 1
    assert failed == 0
    session.expire_all()
    a = session.query(Article).filter_by(url="https://example.com/story-1").one()
    assert a.processing_status == ProcessingStatus.done
    assert a.body_text == "Full article text."
    assert a.published_at is not None


@patch("src.article_fetcher.httpx.Client")
@patch("src.article_fetcher.trafilatura.extract")
def test_fetch_403_marks_paywalled(mock_extract, mock_client_cls, session):
    _make_article(session)
    session.commit()

    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = _mock_response(status_code=403)
    mock_client_cls.return_value = client

    done, failed = fetch_pending_articles(session)

    assert done == 1
    session.expire_all()
    a = session.query(Article).filter_by(url="https://example.com/story-1").one()
    assert a.is_paywalled is True
    assert a.processing_status == ProcessingStatus.done
    mock_extract.assert_not_called()


@patch("src.article_fetcher.httpx.Client")
@patch("src.article_fetcher.trafilatura.extract")
def test_fetch_soft_paywall_detected(mock_extract, mock_client_cls, session):
    _make_article(session)
    session.commit()

    mock_extract.return_value = json.dumps({"text": "Subscribe to read the full article.", "date": None})
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = _mock_response()
    mock_client_cls.return_value = client

    fetch_pending_articles(session)

    session.expire_all()
    a = session.query(Article).filter_by(url="https://example.com/story-1").one()
    assert a.is_paywalled is True


@patch("src.article_fetcher.httpx.Client")
def test_fetch_network_error_marks_failed(mock_client_cls, session):
    import httpx
    _make_article(session)
    session.commit()

    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = httpx.TimeoutException("timed out")
    mock_client_cls.return_value = client

    done, failed = fetch_pending_articles(session)

    assert failed == 1
    session.expire_all()
    a = session.query(Article).filter_by(url="https://example.com/story-1").one()
    assert a.processing_status == ProcessingStatus.failed


@patch("src.article_fetcher.httpx.Client")
@patch("src.article_fetcher.trafilatura.extract")
def test_fetch_skips_done_articles(mock_extract, mock_client_cls, session):
    _make_article(session, status=ProcessingStatus.done)
    session.commit()

    done, failed = fetch_pending_articles(session)

    assert done == 0
    assert failed == 0
    mock_extract.assert_not_called()
