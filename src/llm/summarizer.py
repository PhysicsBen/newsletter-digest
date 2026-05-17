"""
LLM summarization and significance scoring for canonical stories.

One LLM call per canonical_story — never per individual article mention.
Articles are truncated to token_budget before the call; longer articles are
chunked, each chunk summarized independently, then combined.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Article, ArticleSummary, CanonicalStory, NewsletterArticle
from src.llm.client import call_llm

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

# Rough chars-per-token estimate for truncation without a tokenizer dependency.
_CHARS_PER_TOKEN = 4


def _truncate(text: str, max_tokens: int) -> str:
    max_chars = max_tokens * _CHARS_PER_TOKEN
    return text[:max_chars]


def _chunk(text: str, chunk_tokens: int) -> list[str]:
    chunk_size = chunk_tokens * _CHARS_PER_TOKEN
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _extract_json_object(text: str) -> str:
    """Return the first complete {...} JSON object from text, handling nesting."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in: {text[:200]!r}")
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError(f"Unclosed JSON object in: {text[:200]!r}")


def _parse_llm_response(raw: str) -> tuple[str, float]:
    """Extract summary and significance_score from LLM JSON response."""
    # Strip <think>...</think> blocks (Qwen3 may emit these even with /no_think)
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", cleaned).strip()
    # Extract first complete JSON object — models often append trailing explanation text
    json_str = _extract_json_object(cleaned)
    data = json.loads(json_str)
    return str(data["summary"]), float(data["significance_score"])


def _summarize_text(text: str, trust_weight: float) -> tuple[str, float]:
    """
    Summarize a single text. Chunks if it exceeds token_budget, then combines
    chunk summaries into a final summary via a second LLM call.
    """
    if not text or not text.strip():
        raise ValueError("Cannot summarize empty body_text")

    budget = settings.token_budget

    if len(text) <= budget * _CHARS_PER_TOKEN:
        # Single call: article content first, instruction anchored at end (Gemini best practice)
        user_content = (
            f"Source trust weight: {trust_weight:.2f}\n\n"
            f"{text}\n\n"
            "Based on the information above, provide the JSON summary and significance score."
        )
        raw = call_llm([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ])
        return _parse_llm_response(raw)

    # Chunked path: summarize each chunk, then combine
    chunks = _chunk(text, budget)
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        user_content = (
            f"Source trust weight: {trust_weight:.2f}\n\n"
            f"[Part {i} of {len(chunks)}]\n{chunk}\n\n"
            "Based on the information above, provide the JSON summary and significance score."
        )
        raw = call_llm([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ])
        summary, _ = _parse_llm_response(raw)
        chunk_summaries.append(summary)

    # Combine chunk summaries into final summary
    combined = "\n\n".join(f"Part {i}: {s}" for i, s in enumerate(chunk_summaries, 1))
    user_content = (
        f"Source trust weight: {trust_weight:.2f}\n\n"
        f"{combined}\n\n"
        "Based on the partial summaries above, provide a single unified JSON summary "
        "and significance score for the complete article."
    )
    raw = call_llm([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ])
    return _parse_llm_response(raw)


def summarize_canonical_stories(session: Session) -> int:
    """
    For each CanonicalStory without an ArticleSummary, call the LLM to generate
    a summary and significance score. Skips stories already summarized.

    LLM calls run concurrently (settings.llm_concurrency workers); DB writes
    happen on the calling thread after each future completes.

    Returns the number of stories summarized.
    """
    already_done = select(ArticleSummary.canonical_story_id)
    stmt = select(CanonicalStory).where(CanonicalStory.id.not_in(already_done))
    stories = list(session.scalars(stmt).all())

    if not stories:
        log.info("All canonical stories already summarized")
        return 0

    log.info("Summarizing %d canonical stories with %d workers", len(stories), settings.llm_concurrency)

    # Collect all work up-front so DB reads stay on this thread.
    pending: list[tuple[int, int, str, float]] = []  # (story_id, article_id, body_text, trust_weight)
    for story in stories:
        rep_article = session.get(Article, story.representative_article_id)
        body_text = (rep_article.body_text or "").strip() if rep_article else ""
        if not body_text or len(body_text) < 50:
            log.warning(
                "Skipping canonical_story %d — body_text missing or too short (%d chars)",
                story.id, len(body_text),
            )
            continue

        trust_weight = 1.0
        na = session.scalars(
            select(NewsletterArticle).where(NewsletterArticle.article_id == rep_article.id).limit(1)
        ).first()
        if na and na.newsletter and na.newsletter.source:
            trust_weight = na.newsletter.source.trust_weight

        pending.append((story.id, rep_article.id, body_text, trust_weight))

    def _worker(args: tuple[int, int, str, float]) -> tuple[int, int, str, float]:
        """Called in a thread — no DB access. Returns (story_id, article_id, summary, score)."""
        story_id, article_id, body_text, trust_weight = args
        summary_text, significance_score = _summarize_text(body_text, trust_weight)
        return story_id, article_id, summary_text, significance_score

    count = 0
    total = len(pending)
    start_time = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=settings.llm_concurrency) as executor:
        future_to_story = {executor.submit(_worker, p): p[0] for p in pending}
        for future in concurrent.futures.as_completed(future_to_story):
            story_id = future_to_story[future]
            try:
                sid, article_id, summary_text, significance_score = future.result()
            except Exception:
                log.exception("LLM call failed for canonical_story %d — skipping", story_id)
                continue

            session.add(ArticleSummary(
                canonical_story_id=sid,
                article_id=article_id,
                summary_text=summary_text,
                significance_score=significance_score,
                model_used=settings.llm_model,
                prompt_version=settings.prompt_version,
            ))
            session.commit()
            count += 1
            if count % 10 == 0:
                elapsed = time.monotonic() - start_time
                rate = count / elapsed
                remaining = total - count
                eta_min = remaining / rate / 60
                log.info(
                    "Summarized %d / %d  |  %.1f/min  |  ETA ~%.0f min",
                    count, total, rate * 60, eta_min,
                )

    log.info("Summarization complete: %d stories summarized", count)
    return count
