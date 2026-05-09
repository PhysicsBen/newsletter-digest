"""
Semantic deduplication: embed articles with local sentence-transformers,
cluster near-duplicates by cosine similarity into canonical_stories.

Deduplication MUST happen before any LLM call — it is the primary cost control mechanism.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Article, CanonicalStory, ProcessingStatus

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def embed_articles(texts: list[str]) -> list[list[float]]:
    """
    Embed article texts using the local sentence-transformers model.
    Never calls an external embeddings API.
    """
    # TODO: load model from config.embedding_model, encode texts, return as list of floats
    raise NotImplementedError


def cluster_into_canonical_stories(session: Session) -> int:
    """
    Embed all fetched articles that have not yet been assigned a canonical_story_id,
    cluster near-duplicates by cosine similarity, create CanonicalStory records,
    and write canonical_story_id back onto each Article.

    Only processes articles with processing_status=done and canonical_story_id=None.

    Returns the number of canonical stories created or updated.
    """
    # TODO: implement
    # 1. Query articles where processing_status=done and canonical_story_id is None
    # 2. Embed their body_text via embed_articles()
    # 3. Cluster by cosine similarity (scikit-learn AgglomerativeClustering or similar)
    # 4. For each cluster: create or update a CanonicalStory, set representative_article_id
    # 5. Write canonical_story_id back onto each Article
    raise NotImplementedError
