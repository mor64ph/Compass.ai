"""Provider abstraction for the LLM layer.

Compass was built against Anthropic. That turned out to be a single point of
failure: a key with an expiry date, issued by an employer, funding a personal
project. This layer removes the lock-in — the rest of the app asks for "a
completion, optionally shaped by this JSON schema" and does not care who serves
it.

What stays above this layer (in `app/llm/client.py`): prompt versioning, the
response cache, rate limiting, spend accounting, and Pydantic validation of the
result. What each provider owns: the wire format, the auth, and translating a
strict JSON schema into whatever dialect it accepts.

Adding a provider means implementing `LLMProvider` and registering it in
`app/llm/providers/__init__.py`. Nothing else in the app should need to change.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A provider could not serve the request. The message is shown to the user,
    so it must say what to do about it."""


class ProviderUnavailable(ProviderError):
    """Not configured, not installed, or not reachable - as opposed to a request
    that was rejected on its merits."""


class ProviderRateLimited(ProviderError):
    """Back off and retry. Distinguished from a hard failure so the UI can say
    'try again in a minute' rather than 'this is broken'."""


class ProviderRefused(ProviderError):
    """The model declined on safety grounds."""


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class ProviderResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    # Source URLs, when the provider performed a grounded web search.
    citations: list[dict] = field(default_factory=list)
    truncated: bool = False


class LLMProvider(Protocol):
    """The whole contract. Deliberately small: three capabilities, because that
    is all Compass actually uses."""

    name: str
    supports_web_search: bool

    def available(self) -> bool:
        """True when a call would have credentials/endpoint to use. Must not
        make a network request - it is called on page loads."""
        ...

    def describe(self) -> dict:
        """Human-readable status for the Settings page."""
        ...

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
        """One completion. When `json_schema` is given, the returned text must be
        parseable JSON conforming to it."""
        ...


# --------------------------------------------------------------------------
# Schema dialect helpers
# --------------------------------------------------------------------------

# JSON Schema keywords that Google's `responseSchema` rejects outright. Its
# dialect is a subset of OpenAPI 3.0, not full JSON Schema.
_GEMINI_UNSUPPORTED = {
    "additionalProperties",
    "$schema",
    "$defs",
    "definitions",
    "patternProperties",
    "allOf",
    "oneOf",
    "not",
    "const",
    "examples",
    "default",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def to_gemini_schema(schema: dict) -> dict:
    """Strip keywords Google's schema dialect does not accept.

    The hardened schema from `app/llm/schema.py` sets
    `additionalProperties: false` on every object, which Anthropic requires and
    Gemini rejects. Rather than weaken the canonical schema, translate here.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in _GEMINI_UNSUPPORTED:
                continue
            out[key] = walk(value)
        # Gemini honours `propertyOrdering` and otherwise emits fields in an
        # arbitrary order; fixing the order makes cached comparisons stable.
        if out.get("type") == "object" and "properties" in out:
            out["propertyOrdering"] = list(out["properties"].keys())
        return out

    return walk(copy.deepcopy(schema))


def to_ollama_schema(schema: dict) -> dict:
    """Ollama passes the schema to llama.cpp's GBNF converter, which handles
    standard JSON Schema but chokes on `$schema` metadata."""
    cleaned = copy.deepcopy(schema)
    cleaned.pop("$schema", None)
    return cleaned


def describe_error(provider: str, status: int, body: str, limit: int = 300) -> str:
    snippet = (body or "").strip().replace("\n", " ")
    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"
    return f"{provider} returned HTTP {status}: {snippet}"
