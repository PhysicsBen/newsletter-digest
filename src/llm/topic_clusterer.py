"""
Topic clustering and continuity tracking.

Clusters article summaries into topics for the current digest.
Matches against existing topics by embedding similarity to track continuity.
Generates what_is_new_text for recurring topics.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Digest, DigestTopic, Topic

log = logging.getLogger(__name__)


def cluster_topics(session: Session, digest: Digest) -> int:
    """
    Embed article summaries for the current digest, cluster by cosine similarity,
    have the LLM name and describe each cluster, then match against existing Topic
    records by embedding similarity (threshold from config.topic_similarity_threshold).

    For recurring topics: generates what_is_new_text by comparing the prior
    digest_topics.summary_text to the current articles.

    Returns the number of DigestTopic records created.
    """
    # TODO: implement
    # 1. Load all ArticleSummary records not yet assigned to a topic in this digest
    # 2. Embed summaries; cluster by cosine similarity
    # 3. For each cluster: call LLM to name + describe the topic
    # 4. Compare cluster embedding to existing Topic.embedding records
    #    - similarity > topic_similarity_threshold → existing topic (update last_seen)
    #    - below threshold → new Topic (status=active)
    # 5. For recurring topics: fetch prior digest_topics.summary_text,
    #    call LLM to generate what_is_new_text
    # 6. Create DigestTopic records; compute aggregate significance_score
    raise NotImplementedError
