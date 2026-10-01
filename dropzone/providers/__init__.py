"""Provider selection, driven entirely by environment variables.

`LLM_PROVIDER` picks the adapter and `LLM_MODEL` the model. No model name is
hardcoded anywhere in the codebase: they go stale, and the course requires that
switching provider needs no code change.
"""

from __future__ import annotations

import os

from .base import LLMProvider, Message, ProviderError, Reply, ToolCall

__all__ = [
    "LLMProvider",
    "Message",
    "ProviderError",
    "Reply",
    "ToolCall",
    "get_provider",
    "provider_status",
]

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

SUPPORTED = tuple(KEY_ENV)


def get_provider() -> LLMProvider:
    """Build the configured provider, or explain exactly what is missing."""
    name = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not name:
        raise ProviderError(
            "LLM_PROVIDER is not set. Copy .env.example to .env and set "
            f"LLM_PROVIDER to one of: {', '.join(SUPPORTED)}."
        )
    if name not in SUPPORTED:
        raise ProviderError(f"unknown LLM_PROVIDER {name!r}; expected one of {', '.join(SUPPORTED)}")

    model = (os.getenv("LLM_MODEL") or "").strip()
    if not model:
        raise ProviderError(f"LLM_MODEL is not set; pick a {name} model id in .env")

    key_var = KEY_ENV[name]
    api_key = (os.getenv(key_var) or "").strip()
    if not api_key:
        raise ProviderError(f"{key_var} is not set in the environment")

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model, api_key=api_key)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(model=model, api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL"))

    from .gemini_provider import GeminiProvider

    return GeminiProvider(model=model, api_key=api_key)


def provider_status() -> dict[str, str | bool]:
    """A short report for `/health`, with no key material in it."""
    name = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    return {
        "provider": name or "unset",
        "model": (os.getenv("LLM_MODEL") or "unset").strip(),
        "api_key_present": bool((os.getenv(KEY_ENV.get(name, "")) or "").strip()),
    }
