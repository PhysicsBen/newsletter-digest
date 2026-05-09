"""
Semantic deduplication: embed articles with local sentence-transformers,
cluster near-duplicates by cosine similarity into canonical_stories.

Deduplication MUST happen before any LLM call — it is the primary cost control mechanism.
"""
from __future__ import annotations

import logging

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Article, CanonicalStory, ProcessingStatus

log = logging.getLogger(__name__)

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Lazy-load and cache the sentence-transformers model for the process lifetime."""
    global _model
    if _model is None:
        log.info("Loading embedding model: %s", settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
    return _model


def embed_articles(texts: list[str]) -> list[list[float]]:
    """
    Embed article texts using the local sentence-transformers model.
    Never calls an external embeddings API.

    Returns L2-normalised embeddings so dot-product == cosine similarity.
    """
    model = _get_model()
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return embeddings.tolist()


def cluster_into_canonical_stories(session: Session) -> int:
    """
    Embed all fetched articles that have not yet been assigned a canonical_story_id,
    cluster near-duplicates by cosine similarity, create CanonicalStory records,
    and write canonical_story_id back onto each Article.

    Only processes articles with processing_status=done and canonical_story_id=None.

    Returns the number of canonical stories created.
    """
    stmt = select(Article).where(
        Article.processing_status == ProcessingStatus.done,
        Article.canonical_story_id.is_(None),
        Article.body_text.isnot(None),
        Article.body_text != "",
    )
    articles = list(session.scalars(stmt).all())

    if not articles:
        log.info("No unprocessed articles to deduplicate")
        return 0

    log.info("Embedding %d articles for deduplication", len(articles))
    texts = [a.body_text for a in articles]
    embeddings = embed_articles(texts)
    embeddings_np = np.array(embeddings, dtype=np.float32)

    if len(articles) == 1:
        labels = [0]
    else:
        # cosine distance = 1 - cosine similarity; embeddings are already normalised
        distance_threshold = 1.0 - settings.dedup_similarity_threshold
        clustering = AgglomerativeClustering(
            n_clusters=None,
            metric="cosine",
            linkage="average",
            distance_threshold=distance_threshold,
        )
        labels = clustering.fit_predict(embeddings_np).tolist()

    # Group article indices by cluster label
    clusters: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        clusters.setdefault(label, []).append(idx)

    stories_created = 0
    for indices in clusters.values():
        cluster_articles = [articles[i] for i in indices]
        cluster_embeddings = embeddings_np[indices]

        centroid = cluster_embeddings.mean(axis=0).tolist()
        representative = max(cluster_articles, key=lambda a: len(a.body_text or ""))

        story = CanonicalStory(
            representative_article_id=representative.id,
            article_ids=[a.id for a in cluster_articles],
            embedding=centroid,
        )
        session.add(story)
        session.flush()  # populate story.id before back-linking

        for article in cluster_articles:
            article.canonical_story_id = story.id

        stories_created += 1

    log.info(
        "Deduplicated %d articles into %d canonical stories",
        len(articles),
        stories_created,
    )
    return stories_created
