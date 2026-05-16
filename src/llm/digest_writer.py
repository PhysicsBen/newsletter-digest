"""
Final Markdown digest assembly.

Sorts topics by significance_score descending, formats each topic's articles,
flags paywalled and stale content, writes to output/.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Article, ArticleSummary, Digest, DigestTopic, TopicArticle
from src.llm.client import call_llm

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
    topic_articles: list,
    articles_by_id: dict,
    summaries_by_article_id: dict,
    topic_name: str,
) -> str:
    """
    Return a descriptive title for a digest section.

    Priority:
    1. article.title from the first article that has one
    2. First sentence of the first article's summary_text
    3. topic_name (fallback)
    """
    for ta in topic_articles:
        article = articles_by_id.get(ta.article_id)
        if article and article.title and article.title.strip():
            return article.title.strip()

    for ta in topic_articles:
        art_sum = summaries_by_article_id.get(ta.article_id)
        if art_sum and art_sum.summary_text:
            title = _first_sentence(art_sum.summary_text)
            if title:
                return title

    return topic_name


def _generate_section_title(summaries: list[str], fallback: str) -> str:
    """Call LLM to produce a crisp 8–12 word headline for a topic section."""
    if not summaries:
        return fallback
    content = "\n\n".join(s[:500] for s in summaries[:3])
    messages = [
        {
            "role": "user",
            "content": (
                f"{content}\n\n"
                "Based on the information above, write a single concise headline "
                "(8–12 words, no trailing punctuation) that captures the core development. "
                "Return only the headline."
            ),
        }
    ]
    try:
        return call_llm(messages).strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        log.warning("Title generation failed (%s), using fallback", exc)
        return fallback


def _generate_overview(top_items: list[tuple[str, float]]) -> str:
    """Generate a 3–5 sentence overview of the week's key AI developments."""
    if not top_items:
        return ""
    bullets = "\n".join(f"- {title} (significance {score:.1f})" for title, score in top_items)
    messages = [
        {
            "role": "user",
            "content": (
                f"{bullets}\n\n"
                "Based on the information above, write a 3–5 sentence overview of this "
                "week's most important AI developments for a technical reader. Be specific; "
                "name the key breakthroughs, companies, and themes. "
                "Return only the overview paragraph."
            ),
        }
    ]
    try:
        return call_llm(messages).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Overview generation failed (%s), skipping", exc)
        return ""


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
    digest_topics = [
        dt for dt in digest_topics
        if (dt.significance_score or 0.0) >= settings.digest_significance_min_score
    ]

    now = digest.date_range_end or datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = timedelta(days=settings.staleness_days)

    start = digest.date_range_start
    start_str = start.strftime("%Y-%m-%d") if start else "all-time"
    end_str = now.strftime("%Y-%m-%d")

    # Bulk-load all TopicArticles for this digest in one query
    all_topic_articles = list(session.scalars(
        select(TopicArticle).where(TopicArticle.digest_id == digest.id)
    ).all())

    topic_articles_by_topic: dict[int, list] = defaultdict(list)
    for ta in all_topic_articles:
        topic_articles_by_topic[ta.topic_id].append(ta)

    all_article_ids = [ta.article_id for ta in all_topic_articles]

    # Bulk-load articles and summaries
    articles_by_id: dict[int, Article] = {}
    summaries_by_article_id: dict[int, ArticleSummary] = {}
    if all_article_ids:
        articles_by_id = {
            a.id: a for a in session.scalars(
                select(Article).where(Article.id.in_(all_article_ids))
            ).all()
        }
        summaries_by_article_id = {
            s.article_id: s for s in session.scalars(
                select(ArticleSummary).where(ArticleSummary.article_id.in_(all_article_ids))
            ).all()
        }

    total_stories = len(all_topic_articles)

    lines: list[str] = [
        f"# AI Newsletter Digest — {start_str} to {end_str}",
        "",
        f"*Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · {len(digest_topics)} topics · {total_stories} stories*",
        "",
    ]

    # ── First pass: generate LLM titles in parallel ───────────────────────────
    # Build (dt, topic_articles, summaries, fallback) for each topic first
    work_items = []
    for dt in digest_topics:
        topic = dt.topic
        topic_articles = topic_articles_by_topic.get(dt.topic_id, [])
        summaries: list[str] = [
            summaries_by_article_id[ta.article_id].summary_text
            for ta in topic_articles
            if ta.article_id in summaries_by_article_id
            and summaries_by_article_id[ta.article_id].summary_text
        ]
        fallback = _derive_section_title(topic_articles, articles_by_id, summaries_by_article_id, topic.name)
        work_items.append((dt, topic_articles, summaries, fallback))

    # Run title generation in parallel (I/O-bound LLM calls)
    titles: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=settings.llm_concurrency) as pool:
        future_to_idx = {
            pool.submit(_generate_section_title, summaries, fallback): i
            for i, (_, _, summaries, fallback) in enumerate(work_items)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            titles[idx] = future.result()

    section_data: list[tuple] = [
        (dt, topic_articles, titles[i])
        for i, (dt, topic_articles, _, _) in enumerate(work_items)
    ]

    # ── Overview paragraph ────────────────────────────────────────────────────
    top_items = [
        (title, dt.significance_score or 0.0)
        for dt, _, title in section_data
        if (dt.significance_score or 0.0) >= 7.0
    ][:12]
    overview = _generate_overview(top_items) if top_items else ""

    if overview:
        lines.append(overview)
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── Second pass: render sections ─────────────────────────────────────────
    for dt, topic_articles, section_title in section_data:
        score = dt.significance_score or 0.0

        if score >= 7.0:
            badge = "HIGH"
        elif score >= 4.0:
            badge = "MEDIUM"
        else:
            badge = "LOW"

        lines.append(f"## {section_title}  ·  {badge} {score:.1f}")
        lines.append("")

        if dt.what_is_new_text:
            lines.append(f"> **What's new:** {dt.what_is_new_text}")
            lines.append("")

        for ta in topic_articles:
            article = articles_by_id.get(ta.article_id)
            if article is None:
                continue

            article_summary = summaries_by_article_id.get(ta.article_id)

            flags: list[str] = []
            if article.is_paywalled:
                flags.append("[paywalled]")
            if article.published_at and (now - article.published_at) > stale_cutoff:
                flags.append(f"[stale — published {article.published_at.strftime('%Y-%m-%d')}]")

            flag_str = (" " + " ".join(flags)) if flags else ""
            summary_text = (article_summary.summary_text or "") if article_summary else ""
            link_url = article.canonical_url or article.url

            if summary_text:
                lines.append(f"- {summary_text}")
                lines.append(f"  [Source]({link_url}){flag_str}")
            else:
                lines.append(f"- [Source]({link_url}){flag_str}")

        lines.append("")

    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = f"digest_{start_str}_{end_str}.md"
    output_path = OUTPUT_DIR / filename
    output_path.write_text("\n".join(lines), encoding="utf-8")

    digest.output_path = str(output_path)
    session.commit()

    log.info("Digest written to %s (%d topics, %d stories)", output_path, len(digest_topics), total_stories)
    return str(output_path)

