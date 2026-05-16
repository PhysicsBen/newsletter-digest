"""
Final Markdown digest assembly.

Sorts topics by significance_score descending, formats each topic's articles,
flags paywalled and stale content, writes to output/.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Article, ArticleSummary, Digest, DigestTopic, TopicArticle

log = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")

_GENERIC_TOPIC_NAMES = {"ai/ml update", "ai update", "ml update"}
_MAX_TITLE_LEN = 90


def _first_sentence(text: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """Return the first sentence of text, truncated to max_len chars."""
    # Split on '. ' or '.\n' to find sentence boundary
    for sep in (". ", ".\n", "! ", "? "):
        idx = text.find(sep)
        if 0 < idx < max_len:
            return text[: idx + 1].strip()
    # No sentence boundary found — truncate at word boundary
    if len(text) <= max_len:
        return text.strip()
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated.strip() + "…"


def _derive_section_title(
    session: "Session",
    topic_articles: list,
    topic_name: str,
) -> str:
    """
    Return a descriptive title for a digest section.

    Priority:
    1. article.title from the first article that has one
    2. First sentence of the first article's summary_text
    3. topic_name (fallback)
    """
    # Try article titles first
    for ta in topic_articles:
        article = session.get(Article, ta.article_id)
        if article and article.title and article.title.strip():
            return article.title.strip()

    # Fall back to first sentence of first summary
    for ta in topic_articles:
        article_summary = session.scalars(
            select(ArticleSummary).where(ArticleSummary.article_id == ta.article_id)
        ).first()
        if article_summary and article_summary.summary_text:
            title = _first_sentence(article_summary.summary_text)
            if title:
                return title

    # Last resort: topic_name
    return topic_name


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
    digest_topics = list(session.scalars(
        select(DigestTopic)
        .where(DigestTopic.digest_id == digest.id)
        .order_by(DigestTopic.significance_score.desc())
    ).all())

    # Apply significance filter up front so counts are accurate
    digest_topics = [dt for dt in digest_topics if (dt.significance_score or 0.0) >= 5.0]

    now = digest.date_range_end or datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = timedelta(days=settings.staleness_days)

    start = digest.date_range_start
    start_str = start.strftime("%Y-%m-%d") if start else "all-time"
    end_str = now.strftime("%Y-%m-%d")

    total_stories = sum(
        session.query(TopicArticle)
        .filter(TopicArticle.digest_id == digest.id, TopicArticle.topic_id == dt.topic_id)
        .count()
        for dt in digest_topics
    )

    lines: list[str] = [
        f"# AI Newsletter Digest — {start_str} to {end_str}",
        "",
        f"*Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · {len(digest_topics)} topics · {total_stories} stories*",
        "",
        "---",
        "",
    ]

    for dt in digest_topics:
        score = dt.significance_score or 0.0
        topic = dt.topic

        if score >= 7.0:
            badge = "HIGH"
        elif score >= 4.0:
            badge = "MEDIUM"
        else:
            badge = "LOW"

        topic_articles = list(session.scalars(
            select(TopicArticle)
            .where(
                TopicArticle.topic_id == dt.topic_id,
                TopicArticle.digest_id == digest.id,
            )
        ).all())

        # Derive a descriptive title for the section from the first article
        section_title = _derive_section_title(session, topic_articles, topic.name)

        lines.append(f"## {section_title}  ·  {badge} {score:.1f}")
        lines.append("")

        if dt.what_is_new_text:
            lines.append(f"> **What's new:** {dt.what_is_new_text}")
            lines.append("")

        for ta in topic_articles:
            article = session.get(Article, ta.article_id)
            if article is None:
                continue

            article_summary = session.scalars(
                select(ArticleSummary).where(ArticleSummary.article_id == ta.article_id)
            ).first()

            flags: list[str] = []
            if article.is_paywalled:
                flags.append("[paywalled]")
            if article.published_at and (now - article.published_at) > stale_cutoff:
                flags.append(f"[stale — published {article.published_at.strftime('%Y-%m-%d')}]")

            flag_str = (" " + " ".join(flags)) if flags else ""
            summary_text = (article_summary.summary_text or "") if article_summary else ""

            if summary_text:
                lines.append(f"- {summary_text}")
                lines.append(f"  [Source]({article.url}){flag_str}")
            else:
                lines.append(f"- [Source]({article.url}){flag_str}")

        lines.append("")

    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = f"digest_{start_str}_{end_str}.md"
    output_path = OUTPUT_DIR / filename
    output_path.write_text("\n".join(lines), encoding="utf-8")

    digest.output_path = str(output_path)
    session.commit()

    log.info("Digest written to %s (%d topics, %d stories)", output_path, len(digest_topics), total_stories)
    return str(output_path)

