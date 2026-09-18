"""Anthropic provider.

Compass's prompts were written and tuned against Claude, so this remains the
quality benchmark even though it is no longer the default. Keep it working: the
only way to judge whether a cheaper provider is "good enough" is to have
something to compare against.
"""

from __future__ import annotations

import logging
from typing import Any

from app.llm.providers.base import (
    Message,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderResult,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

# Shared with the Gemini provider: one user action must fit inside a reverse
# proxy's patience, or the failure arrives as an unexplained 502.
REQUEST_BUDGET_SECONDS = 90.0

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


class AnthropicProvider:
    name = "anthropic"
    supports_web_search = True

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = (api_key or "").strip()
        self.model = model
        self._client: Any = None

    # ------------------------------------------------------------------ status

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ProviderUnavailable(
                    "The `anthropic` package is not installed. "
                    "Run: pip install -r requirements.txt"
                ) from exc
            # The SDK defaults to a 600s timeout and 2 retries, so one call can
            # occupy ~30 minutes. Behind a managed host's proxy the request is
            # severed long before that and the browser gets 502 - which looks
            # identical to a crash. Bound it to the same budget Gemini uses, and
            # handle retries here, where the error messages are written.
            kwargs: dict[str, Any] = {
                "timeout": REQUEST_BUDGET_SECONDS,
                "max_retries": 1,
            }
            if self._api_key:
                kwargs["api_key"] = self._api_key
            try:
                self._client = anthropic.Anthropic(**kwargs)
            except Exception as exc:
                raise ProviderUnavailable(
                    f"Could not initialise the Anthropic client: {exc}"
                ) from exc
        return self._client

    def available(self) -> bool:
        """`Anthropic()` constructs happily with nothing configured and only
        fails at request time, so constructing it proves nothing. The SDK
        resolves an API key, an auth token, or an `ant auth login` profile into
        one of these three attributes."""
        try:
            client = self.client
        except ProviderUnavailable:
            return False
        return any(
            getattr(client, attr, None) for attr in ("api_key", "auth_token", "credentials")
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": "Anthropic Claude",
            "model": self.model,
            "available": self.available(),
            "needs": "ANTHROPIC_API_KEY in .env, or `ant auth login`",
            "cost_note": "Paid. Roughly $1 for a full verify run; cents per application.",
        }

    # ------------------------------------------------------------------ calls

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int,
        json_schema: dict | None = None,
        effort: str | None = None,
        web_search: bool = False,
    ) -> ProviderResult:
        if not self.available():
            raise ProviderUnavailable(
                "No Anthropic credentials are configured. Set ANTHROPIC_API_KEY "
                "in .env, or run `ant auth login`."
            )

        output_config: dict = {"effort": effort or "high"}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system}] if system else [],
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            # Auto-caches the last cacheable block. The system prompt plus the
            # career profile is usually well past the ~1024-token floor.
            "cache_control": {"type": "ephemeral"},
        }
        if web_search:
            request["tools"] = [WEB_SEARCH_TOOL]

        response = self._create(request)

        text = "\n\n".join(b.text for b in response.content if b.type == "text")
        return ProviderResult(
            text=text,
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            model=getattr(response, "model", self.model),
            provider="anthropic",
            citations=_search_citations(response),
            truncated=response.stop_reason == "max_tokens",
        )

    def _create(self, request: dict[str, Any]) -> Any:
        import anthropic

        try:
            response = self.client.messages.create(**request)
        except TypeError as exc:
            # The SDK raises a bare TypeError when it cannot resolve an auth
            # method. Without this it escapes as a 500.
            if "authentication method" in str(exc):
                raise ProviderUnavailable(
                    "Anthropic could not resolve an authentication method. Set "
                    "ANTHROPIC_API_KEY in .env, or run `ant auth login`."
                ) from exc
            raise
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailable(
                "Anthropic rejected the credentials. Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after", "60")
            raise ProviderRateLimited(
                f"Anthropic rate limit hit. Retry in {retry_after}s."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(
                "Could not reach the Anthropic API - check your connection."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc

        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.category})"
            raise ProviderRefused(f"Claude declined this request{detail}.")
        if response.stop_reason == "max_tokens":
            logger.warning(
                "Response hit max_tokens (request_id=%s) - output may be truncated",
                getattr(response, "_request_id", "?"),
            )
        return response


def _search_citations(response: Any) -> list[dict]:
    """Real source URLs from `web_search_tool_result` blocks.

    On success `.content` is a *list* of results; on error it is a single error
    *object*. Branch before indexing - a failed search returns HTTP 200, not an
    exception.
    """
    citations: list[dict] = []
    seen: set[str] = set()
    for block in response.content:
        if block.type != "web_search_tool_result":
            continue
        content = block.content
        if not isinstance(content, list):
            logger.warning(
                "web_search returned an error: %s", getattr(content, "error_code", content)
            )
            continue
        for result in content:
            url = getattr(result, "url", "") or ""
            if url and url not in seen:
                seen.add(url)
                citations.append({"title": getattr(result, "title", "") or url, "url": url})
    return citations
