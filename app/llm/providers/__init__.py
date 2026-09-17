"""Provider registry.

`COMPASS_LLM_PROVIDER` in .env selects one of `gemini`, `ollama` or `anthropic`.
The default is `auto`, which picks the first configured provider in a deliberate
order: whatever the user has actually set up, preferring the one that costs
nothing and needs no account.
"""

from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderResult,
    ProviderUnavailable,
)
from app.llm.providers.gemini_provider import GeminiProvider
from app.llm.providers.ollama_provider import OllamaProvider

logger = logging.getLogger(__name__)

__all__ = [
    "LLMProvider",
    "Message",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRefused",
    "ProviderResult",
    "ProviderUnavailable",
    "build_provider",
    "all_providers",
    "PROVIDER_NAMES",
]

PROVIDER_NAMES = ("gemini", "ollama", "anthropic")

# Order for `auto`. Gemini first: free tier, no card, good enough for structured
# output. Ollama second: free and offline but slow. Anthropic last, not because
# it is worst - it is the best - but because it is the one that costs money and,
# in this deployment, the one whose key expires.
AUTO_ORDER = ("gemini", "ollama", "anthropic")


def _construct(name: str, settings: Settings) -> LLMProvider:
    if name == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key, model=settings.compass_gemini_model
        )
    if name == "ollama":
        return OllamaProvider(
            host=settings.compass_ollama_host,
            model=settings.compass_ollama_model,
            num_ctx=settings.compass_ollama_num_ctx,
        )
    if name == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key, model=settings.compass_model
        )
    raise ValueError(
        f"Unknown provider {name!r}. Choose one of: {', '.join(PROVIDER_NAMES)}, or auto."
    )


def all_providers(settings: Settings | None = None) -> list[LLMProvider]:
    """Every provider, configured or not - for the Settings page."""
    settings = settings or get_settings()
    return [_construct(name, settings) for name in PROVIDER_NAMES]


def build_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    choice = (settings.compass_llm_provider or "auto").strip().lower()

    if choice != "auto":
        provider = _construct(choice, settings)
        if not provider.available():
            # Deliberately not silently falling back: an explicit choice that
            # cannot be honoured should say so, or the user spends an afternoon
            # wondering why output quality changed.
            logger.warning(
                "Provider %r is selected but not configured (%s)",
                choice,
                provider.describe().get("needs", ""),
            )
        return provider

    for name in AUTO_ORDER:
        provider = _construct(name, settings)
        if provider.available():
            logger.info("LLM provider: %s (%s)", provider.name, provider.model)
            return provider

    # Nothing configured. Return the first choice anyway so its `complete()`
    # raises a message explaining how to configure it, rather than the app
    # failing with an opaque None.
    logger.warning("No LLM provider is configured - AI features are unavailable")
    return _construct(AUTO_ORDER[0], settings)
