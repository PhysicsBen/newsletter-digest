"""
Topic clustering and continuity tracking.

Clusters article summaries into topics for the current digest.
Matches against existing topics by embedding similarity to track continuity.
Generates what_is_new_text for recurring topics.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import (
    ArticleSummary, Digest, DigestTopic, Topic, TopicArticle, TopicStatus,
)
from src.llm.client import call_llm

log = logging.getLogger(__name__)

_TOPIC_NAME_SYSTEM = """\
You are an expert AI/ML analyst. Given article summaries sharing a common theme, identify that theme concisely.
Respond in JSON: {"name": "Brief topic name (3-6 words)", "description": "One sentence describing this topic."}
"""

_WHAT_IS_NEW_SYSTEM = """\
You are tracking an ongoing AI/ML topic across newsletter digests.
Given prior coverage and new articles, describe what has changed or progressed.
Respond in JSON: {"what_is_new_text": "1-2 sentence description of what is new or has changed."}
"""


def _parse_json_response(raw: str) -> dict:
    """Strip think-tags and markdown fences, return first JSON object."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", cleaned).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in LLM response: {cleaned[:100]!r}")
    obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    return obj


def _name_cluster(summary_texts: list[str]) -> tuple[str, str]:
    """Call LLM to name and describe a topic cluster. Returns (name, description)."""
    bullets = "\n".join(f"- {s}" for s in summary_texts[:5])
    raw = call_llm([
        {"role": "system", "content": _TOPIC_NAME_SYSTEM},
        {"role": "user", "content": (
            f"{bullets}\n\n"
            "Based on the information above, provide the topic name and description."
        )},
    ])
    data = _parse_json_response(raw)
    return str(data.get("name", "AI/ML Update")), str(data.get("description", ""))


def _generate_what_is_new(prior_summary: str, current_texts: list[str]) -> str:
    """Call LLM to generate what_is_new_text for a recurring topic."""
    current_bullets = "\n".join(f"- {s}" for s in current_texts[:5])
    raw = call_llm([
        {"role": "system", "content": _WHAT_IS_NEW_SYSTEM},
        {"role": "user", "content": (
            f"Prior coverage:\n{prior_summary}\n\n"
            f"New articles:\n{current_bullets}\n\n"
            "Based on the information above, describe what is new."
        )},
    ])
    data = _parse_json_response(raw)
    return str(data.get("what_is_new_text", ""))


def cluster_topics(session: Session, digest: Digest) -> int:
    """
    Embed article summaries for the current digest, cluster by cosine similarity,
    have the LLM name and describe each cluster, then match against existing Topic
    records by embedding similarity (threshold from config.topic_similarity_threshold).

    For recurring topics: generates what_is_new_text by comparing the prior
    digest_topics.summary_text to the current articles.

    Returns the number of DigestTopic records created.
    """
    # Load summaries not yet assigned to any TopicArticle (across all digests)
    already_assigned = select(TopicArticle.article_id)
    summaries = list(session.scalars(
        select(ArticleSummary)
        .where(ArticleSummary.summary_text.isnot(None))
        .where(ArticleSummary.article_id.not_in(already_assigned))
    ).all())

    if not summaries:
        log.info("No new summaries to cluster into topics")
        return 0

    log.info("Embedding %d summaries for topic clustering", len(summaries))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(settings.embedding_model, device=device)
    texts = [s.summary_text for s in summaries]
    embeddings = model.encode(
        texts, batch_size=64, show_progress_bar=True, normalize_embeddings=True
    )
    embeddings = np.array(embeddings, dtype=np.float32)

    if len(summaries) == 1:
        labels = np.array([0])
    else:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            metric="cosine",
            linkage="average",
            distance_threshold=1.0 - settings.topic_similarity_threshold,
        )
        labels = clustering.fit_predict(embeddings)

    cluster_indices: dict[int, list[int]] = defaultdict(list)
    for i, label in enumerate(labels.tolist()):
        cluster_indices[label].append(i)

    log.info("Formed %d topic clusters from %d summaries", len(cluster_indices), len(summaries))

    # Load existing active topics for continuity matching
    existing_topics = list(session.scalars(
        select(Topic).where(
            Topic.status == TopicStatus.active,
            Topic.embedding.isnot(None),
        )
    ).all())
    existing_embeddings: np.ndarray | None = None
    if existing_topics:
        existing_embeddings = np.array(
            [t.embedding for t in existing_topics], dtype=np.float32
        )

    # Precompute per-cluster centroids and scores
    cluster_items: list[tuple] = []
    for label, indices in cluster_indices.items():
        cluster_summaries = [summaries[i] for i in indices]
        cluster_embs = embeddings[indices]
        centroid = cluster_embs.mean(axis=0)
        centroid = centroid / (np.linalg.norm(centroid) + 1e-8)
        agg_score = float(np.mean([s.significance_score or 0.0 for s in cluster_summaries]))
        cluster_items.append((label, indices, cluster_summaries, centroid, agg_score))

    # Parallel LLM calls to name each cluster
    log.info("Naming %d clusters via LLM (%d workers)", len(cluster_items), settings.llm_concurrency)
    naming_results: dict[int, tuple[str, str]] = {}

    def _name_item(item: tuple) -> tuple[int, str, str]:
        lbl, _, cluster_summaries, _, _ = item
        name, desc = _name_cluster([s.summary_text for s in cluster_summaries])
        return lbl, name, desc

    done_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=settings.llm_concurrency) as executor:
        futs = {executor.submit(_name_item, item): item[0] for item in cluster_items}
        for fut in concurrent.futures.as_completed(futs):
            lbl = futs[fut]
            try:
                _, name, desc = fut.result()
                naming_results[lbl] = (name, desc)
            except Exception:
                log.warning("Failed to name cluster %d — using fallback", lbl)
                naming_results[lbl] = ("AI/ML Update", "")
            done_count += 1
            if done_count % 50 == 0:
                log.info("Named %d / %d clusters", done_count, len(cluster_items))

    # Persist Topic and DigestTopic records on the main thread
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    count = 0

    for label, indices, cluster_summaries, centroid, agg_score in cluster_items:
        name, description = naming_results.get(label, ("AI/ML Update", ""))

        # Continuity matching against existing active topics
        matched_topic: Topic | None = None
        if existing_embeddings is not None:
            sims = cosine_similarity(centroid.reshape(1, -1), existing_embeddings)[0]
            best_idx = int(np.argmax(sims))
            if float(sims[best_idx]) >= settings.topic_similarity_threshold:
                matched_topic = existing_topics[best_idx]

        what_is_new_text: str | None = None

        if matched_topic is not None:
            matched_topic.last_seen = now
            prior_dt = session.scalars(
                select(DigestTopic)
                .where(DigestTopic.topic_id == matched_topic.id)
                .where(DigestTopic.digest_id != digest.id)
                .order_by(DigestTopic.id.desc())
                .limit(1)
            ).first()
            if prior_dt and prior_dt.summary_text:
                try:
                    what_is_new_text = _generate_what_is_new(
                        prior_dt.summary_text,
                        [s.summary_text for s in cluster_summaries],
                    )
                except Exception:
                    log.warning(
                        "Failed to generate what_is_new for topic %d", matched_topic.id
                    )
            topic = matched_topic
        else:
            topic = Topic(
                name=name,
                description=description,
                embedding=centroid.tolist(),
                status=TopicStatus.active,
                first_seen=now,
                last_seen=now,
            )
            session.add(topic)
            session.flush()

        digest_topic = DigestTopic(
            digest_id=digest.id,
            topic_id=topic.id,
            summary_text=description,
            significance_score=agg_score,
            what_is_new_text=what_is_new_text,
        )
        session.add(digest_topic)
        session.flush()

        for i in indices:
            session.add(TopicArticle(
                topic_id=topic.id,
                article_id=summaries[i].article_id,
                digest_id=digest.id,
            ))

        count += 1
        if count % 100 == 0:
            session.commit()
            log.info("Persisted %d / %d DigestTopics", count, len(cluster_items))

    session.commit()
    log.info("Topic clustering complete: %d topics", count)
    return count

