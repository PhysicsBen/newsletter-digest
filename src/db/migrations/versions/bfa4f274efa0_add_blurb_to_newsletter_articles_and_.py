"""add_blurb_to_newsletter_articles_and_canonical_url_index

Revision ID: bfa4f274efa0
Revises: 422de0ae9ef3
Create Date: 2026-05-10 08:28:58.852415

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bfa4f274efa0'
down_revision: Union[str, None] = '422de0ae9ef3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("newsletter_articles", sa.Column("blurb", sa.Text(), nullable=True))
    # Index for canonical_url deduplication lookups
    op.create_index("ix_articles_canonical_url", "articles", ["canonical_url"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_articles_canonical_url", table_name="articles")
    op.drop_column("newsletter_articles", "blurb")
