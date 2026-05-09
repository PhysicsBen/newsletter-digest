"""
Final Markdown digest assembly.

Sorts topics by significance_score descending, formats each topic's articles,
flags paywalled and stale content, writes to output/.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Digest

log = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")


def write_digest(session: Session, digest: Digest) -> str:
    """
    Assemble the Markdown digest from digest_topics sorted by significance_score
    descending. For each topic:
      - Significance badge
      - what_is_new_text callout (if recurring)
      - Bullet-point article summaries with source links
      - [paywalled] suffix on paywalled article links
      - [stale — published {date}] suffix on articles older than config.staleness_days

    Writes to output/digest_{start}_{end}.md, updates digest.output_path, and
    persists the record.

    Returns the output file path.
    """
    # TODO: implement
    # 1. Query digest_topics for this digest, ordered by significance_score DESC
    # 2. For each topic: render header, what_is_new callout, article bullets
    # 3. Flag is_paywalled articles and stale published_at articles
    # 4. Write Markdown file; set digest.output_path; commit
    raise NotImplementedError
