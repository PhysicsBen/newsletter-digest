"""add_performance_indexes

Revision ID: c3f1a8d92e47
Revises: bfa4f274efa0
Create Date: 2026-05-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3f1a8d92e47'
down_revision: Union[str, None] = 'bfa4f274efa0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phase 2: fetch pending articles
    op.create_index("ix_articles_processing_status", "articles", ["processing_status"])
    # Phase 3: find articles without a canonical story yet
    op.create_index("ix_articles_canonical_story_id", "articles", ["canonical_story_id"])
    # digest_writer: per-article summary lookups
    op.create_index("ix_article_summaries_article_id", "article_summaries", ["article_id"])
    # digest_writer: topic article lookups by digest
    op.create_index("ix_topic_articles_digest_id", "topic_articles", ["digest_id"])
    # summarizer: trust_weight lookups via newsletter_articles
    op.create_index("ix_newsletter_articles_article_id", "newsletter_articles", ["article_id"])


def downgrade() -> None:
    op.drop_index("ix_newsletter_articles_article_id", table_name="newsletter_articles")
    op.drop_index("ix_topic_articles_digest_id", table_name="topic_articles")
    op.drop_index("ix_article_summaries_article_id", table_name="article_summaries")
    op.drop_index("ix_articles_canonical_story_id", table_name="articles")
    op.drop_index("ix_articles_processing_status", table_name="articles")
