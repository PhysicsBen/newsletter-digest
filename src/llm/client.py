import os

import litellm

from src.config import settings

# Provide the API key to LiteLLM via environment variable
if settings.gemini_api_key:
    os.environ.setdefault("GEMINI_API_KEY", settings.gemini_api_key)


def _is_ollama() -> bool:
    return settings.llm_model.startswith("ollama/")


def _is_gemini() -> bool:
    return settings.llm_model.startswith("gemini/")


def call_llm(messages: list[dict], thinking: bool = False, **kwargs) -> str:
    """
    Single entry point for all LLM calls. Uses LiteLLM to abstract the provider.

    - Model is read from config (LLM_MODEL). Changing models requires no code changes.
    - Pass thinking=True to enable extended reasoning for a call (Gemini/Qwen3 only).

    Provider-specific behaviour (all handled here, callers are provider-agnostic):
    - Gemini: uses thinking_level from config; temperature must stay at 1.0 (default).
    - Ollama/Qwen3: injects /no_think or /think into the system prompt;
      uses temperature 0.7 (non-thinking) or 0.6 (thinking).
    """
    extra: dict = {}

    if _is_gemini():
        if settings.llm_thinking_level:
            extra["thinking"] = {
                "type": "enabled" if thinking else "disabled",
                "thinking_level": settings.llm_thinking_level,
            }
        # Gemini 3 requires temperature=1.0 (default); never set it.

    elif _is_ollama():
        extra["api_base"] = settings.ollama_base_url
        # Qwen3 thinking mode is controlled via a /think or /no_think token
        # injected into the last system message (or prepended to messages).
        directive = "/think" if thinking else "/no_think"
        messages = _inject_qwen3_directive(messages, directive)
        extra["temperature"] = 0.6 if thinking else 0.7

    if settings.llm_temperature is not None:
        extra["temperature"] = settings.llm_temperature  # explicit override wins

    response = litellm.completion(
        model=settings.llm_model,
        messages=messages,
        num_retries=settings.llm_retries,
        **extra,
        **kwargs,
    )
    content = response.choices[0].message.content
    if content is None:
        raise ValueError(
            f"LLM returned None content (safety filter or empty response) "
            f"for model {settings.llm_model}"
        )
    return content


def _inject_qwen3_directive(messages: list[dict], directive: str) -> list[dict]:
    """Append /think or /no_think to the system message for Qwen3 thinking control."""
    messages = [m.copy() for m in messages]  # don't mutate caller's list
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"].rstrip() + "\n" + directive
            return messages
    # No system message present — prepend one
    messages.insert(0, {"role": "system", "content": directive})
    return messages
