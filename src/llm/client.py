import os

import litellm

from src.config import settings

# Provide the API key to LiteLLM via environment variable
if settings.gemini_api_key:
    os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)


def call_llm(messages: list[dict], **kwargs) -> str:
    """
    Single entry point for all LLM calls. Uses LiteLLM to abstract the provider.

    - Model is read from config (LLM_MODEL). Changing models requires no code changes.
    - Temperature is left at the LiteLLM default (1.0) — required for Gemini 3.
    - Thinking level is set via config (LLM_THINKING_LEVEL, default "low").
    """
    extra: dict = {}
    if settings.llm_thinking_level:
        extra["thinking"] = {
            "type": "enabled",
            "thinking_level": settings.llm_thinking_level,
        }

    response = litellm.completion(
        model=settings.llm_model,
        messages=messages,
        **extra,
        **kwargs,
    )
    return response.choices[0].message.content
