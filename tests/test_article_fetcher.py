"""Tests for article_fetcher utility functions."""
import pytest

from src.article_fetcher import canonicalize_url, _is_soft_paywalled


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
