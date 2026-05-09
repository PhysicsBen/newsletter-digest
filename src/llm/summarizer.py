"""
LLM summarization and significance scoring for canonical stories.

One LLM call per canonical_story — never per individual article mention.
Articles are truncated to token_budget before the call; longer articles are
chunked, each chunk summarized independently, then combined.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import ArticleSummary, CanonicalStory

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert AI/ML analyst summarizing articles for a technical newsletter digest.

For each article, provide:
1. A concise 2-3 sentence summary suitable for a technical audience.
2. A significance score from 1-10 using this rubric:
   1-3: New unproven libraries, minor tool releases, incremental product updates
   4-6: Noteworthy research, meaningful launches, tools with established track record
   7-10: Fundamental LLM performance gains, emerging standards, paradigm shifts, breakthrough research

Respond in JSON: {"summary": "...", "significance_score": <float>}
"""


def summarize_canonical_stories(session: Session) -> int:
    """
    For each CanonicalStory without an ArticleSummary, call the LLM to generate
    a summary and significance score. Skips stories already summarized (done).

    Returns the number of stories summarized.
    """
    # TODO: implement
    # 1. Query canonical_stories LEFT JOIN article_summaries WHERE summary IS NULL
    # 2. For each story: get representative article body_text
    # 3. Truncate to config.token_budget; chunk if needed
    # 4. Build messages with SYSTEM_PROMPT + article content first, then instruction
    # 5. Call llm.client.call_llm(); parse JSON response
    # 6. Insert ArticleSummary with prompt_version from config
    raise NotImplementedError
