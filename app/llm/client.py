"""Provider-agnostic LLM client.

Everything Compass asks a model to do goes through here. This layer owns the
concerns that are the same whoever serves the request:

* **Cost control (PRD NFR 9)** - a content-addressed SQLite cache means
  re-opening a scored application costs nothing, a per-process rate limiter caps
  how many tailoring/prep generations a session can burn, and per-user spend
  caps are enforced before the call.
* **Versioned prompts (Section 8)** - prompt text lives in
  `app/llm/prompts/<name>.v<N>.md`, never inline in Python, so prompts can be
  diffed and rolled forward independently of code.
* **Structured output** - callers get a validated Pydantic instance, not a
  string they have to trust.

The wire format, the auth and the schema dialect belong to the provider
(`app/llm/providers/`). Nothing above this file knows or cares which one is
serving.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.llm.providers import (
    Message,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderUnavailable,
    build_provider,
)
from app.llm.schema import strict_json_schema

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"

# Current version of each prompt. Bump when the file changes materially; the
# version is part of the cache key, so a bump invalidates cached responses.
PROMPT_VERSIONS: dict[str, str] = {
    "resume_parse": "1",
    "intake_chat": "1",
    "intake_compile": "1",
    "gap_report": "1",
    "bullet_rewrite": "1",
    "tailor_package": "1",
    # v2: resolve relative dates ("Thursday 28 August") from the email's own
    # Date header, so Calendar auto-placement actually has a time to use.
    "email_classify": "2",
    "company_research": "1",
    "prep_brief": "1",
    "mock_interview": "1",
}

T = TypeVar("T", bound=BaseModel)

# Non-streaming default. Large enough that structured output is never truncated
# mid-object, small enough to stay inside typical HTTP timeouts.
DEFAULT_MAX_TOKENS = 16000


class LLMUnavailable(RuntimeError):
    """No provider is configured, or the configured one cannot be reached.

    Callers surface this as a friendly banner rather than a 500 - the
    deterministic half of Compass (ATS checks, keyword coverage, the tracker,
    the quality gate) works fine without any provider at all.
    """


class RateLimited(RuntimeError):
    pass


@dataclass
class LLMResult:
    """`value` is a validated `output_model` instance for `structured()`, and a
    plain string for `chat()` / `research()`."""

    value: Any
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    request_id: str = ""
    citations: list[dict] = field(default_factory=list)
    provider: str = ""


class _RateLimiter:
    """Fixed-window counter, per category. Deliberately in-process and simple;
    per-user dollar caps in `app/services/usage.py` are the real control."""

    def __init__(self, limits: dict[str, tuple[int, float]]) -> None:
        self._limits = limits
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, category: str) -> None:
        limit = self._limits.get(category)
        if not limit:
            return
        max_calls, window = limit
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(category, []) if now - t < window]
            if len(hits) >= max_calls:
                wait = int(window - (now - hits[0])) + 1
                raise RateLimited(
                    f"Rate limit for {category!r}: {max_calls} per "
                    f"{int(window)}s. Try again in ~{wait}s."
                )
            hits.append(now)
            self._hits[category] = hits


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._provider: Any = None
        self._limiter = _RateLimiter(
            {
                # The expensive, user-triggered generations.
                "tailor": (12, 3600),
                "prep": (12, 3600),
                "research": (20, 3600),
                # Cheap and interactive - looser.
                "intake": (120, 3600),
                "parse": (30, 3600),
                "classify": (60, 3600),
            }
        )

    # ---------------------------------------------------------------- provider

    @property
    def provider(self) -> Any:
        if self._provider is None:
            self._provider = build_provider(self.settings)
        return self._provider

    def reset_provider(self) -> None:
        """Re-resolve on the next call - for after the user edits .env or starts
        Ollama, so they need not restart the server."""
        self._provider = None

    def available(self) -> bool:
        try:
            return bool(self.provider.available())
        except Exception:
            return False

    def describe_provider(self) -> dict:
        try:
            return self.provider.describe()
        except Exception as exc:  # pragma: no cover - defensive
            return {"name": "none", "label": "None", "available": False, "needs": str(exc)}

    @property
    def model(self) -> str:
        try:
            return self.provider.model
        except Exception:
            return ""

    # ---------------------------------------------------------------- prompts

    @staticmethod
    def load_prompt(name: str, **vars_: Any) -> str:
        version = PROMPT_VERSIONS.get(name)
        if version is None:
            raise KeyError(f"Unknown prompt {name!r}. Register it in PROMPT_VERSIONS.")
        path = PROMPT_DIR / f"{name}.v{version}.md"
        if not path.is_file():
            raise FileNotFoundError(f"Prompt file missing: {path}")
        return render(path.read_text(encoding="utf-8"), **vars_)

    # ---------------------------------------------------------------- calls

    def structured(
        self,
        prompt_name: str,
        *,
        user: str,
        output_model: type[T],
        prompt_vars: dict[str, Any] | None = None,
        category: str = "parse",
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        cache: bool = True,
        cache_prefix: bool = True,  # noqa: ARG002 - provider-side concern now
    ) -> LLMResult:
        """One structured call. Returns a validated `output_model` instance."""
        system = self.load_prompt(prompt_name, **(prompt_vars or {}))
        schema = strict_json_schema(output_model)
        version = PROMPT_VERSIONS[prompt_name]
        effort = effort or self.settings.compass_effort

        key = _cache_key(
            provider=self.provider.name,
            model=self.model,
            prompt_name=prompt_name,
            version=version,
            system=system,
            user=user,
            schema=schema,
            effort=effort,
        )

        if cache and self.settings.compass_llm_cache:
            hit = _cache_get(key)
            if hit is not None:
                try:
                    return LLMResult(
                        value=output_model.model_validate_json(hit),
                        cached=True,
                        provider=self.provider.name,
                    )
                except ValidationError:
                    # Schema changed under a stale row - drop it and re-call.
                    _cache_delete(key)

        self._limiter.check(category)

        result = self._complete(
            system=system,
            messages=[Message(role="user", content=user)],
            max_tokens=max_tokens,
            json_schema=schema,
            effort=effort,
        )

        try:
            value = output_model.model_validate_json(_extract_json(result.text))
        except (ValidationError, ValueError) as exc:
            logger.warning("Structured output failed validation for %s: %s", prompt_name, exc)
            raise LLMUnavailable(
                f"{self.provider.name} returned a response that did not match the "
                f"expected shape for {prompt_name!r}. "
                + (
                    "Smaller local models struggle with long structured output - "
                    "try a larger model or a cloud provider."
                    if self.provider.name == "ollama"
                    else "Try again, or reduce the input size."
                )
            ) from exc

        if cache and self.settings.compass_llm_cache:
            _cache_put(
                key,
                prompt_name=prompt_name,
                version=version,
                model=self.model,
                payload=value.model_dump_json(),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        return LLMResult(
            value=value,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider=result.provider,
        )

    def chat(
        self,
        prompt_name: str,
        *,
        messages: list[dict],
        prompt_vars: dict[str, Any] | None = None,
        category: str = "intake",
        effort: str | None = None,
        max_tokens: int = 4000,
    ) -> LLMResult:
        """Free-form multi-turn call (Epic A intake, Epic G mock interview).
        Not cached - the whole point is a fresh turn each time."""
        system = self.load_prompt(prompt_name, **(prompt_vars or {}))
        self._limiter.check(category)
        result = self._complete(
            system=system,
            messages=[
                Message(role=m["role"], content=m["content"])
                for m in messages
                if m.get("content")
            ],
            max_tokens=max_tokens,
            json_schema=None,
            effort=effort or "medium",
        )
        return LLMResult(
            value=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider=result.provider,
        )

    def research(
        self,
        prompt_name: str,
        *,
        user: str,
        prompt_vars: dict[str, Any] | None = None,
        max_uses: int = 5,  # noqa: ARG002 - provider decides its own budget
        max_tokens: int = 8000,
        cache: bool = True,
    ) -> LLMResult:
        """A cited web-search pass.

        The only place Compass touches the live web, and it goes through the
        provider's own grounded-search tool - a search engine, not a crawler
        pointed at a job board. See docs/CONSTRAINTS.md.

        Returns markdown plus the source URLs the search actually returned, so a
        later `structured()` call can shape it without being free to invent
        citations.
        """
        if not self.provider.supports_web_search:
            raise LLMUnavailable(
                f"{self.provider.describe().get('label', self.provider.name)} cannot "
                "search the web, so company research is unavailable. Generate the "
                "brief without research, or switch provider in .env."
            )

        system = self.load_prompt(prompt_name, **(prompt_vars or {}))
        version = PROMPT_VERSIONS[prompt_name]
        key = _cache_key(
            provider=self.provider.name,
            model=self.model,
            prompt_name=f"{prompt_name}:research",
            version=version,
            system=system,
            user=user,
        )
        if cache and self.settings.compass_llm_cache:
            hit = _cache_get(key)
            if hit is not None:
                payload = json.loads(hit)
                return LLMResult(
                    value=payload["text"],
                    cached=True,
                    citations=payload.get("citations", []),
                    provider=self.provider.name,
                )

        self._limiter.check("research")
        result = self._complete(
            system=system,
            messages=[Message(role="user", content=user)],
            max_tokens=max_tokens,
            json_schema=None,
            effort="medium",
            web_search=True,
        )

        if cache and self.settings.compass_llm_cache:
            _cache_put(
                key,
                prompt_name=f"{prompt_name}:research",
                version=version,
                model=self.model,
                payload=json.dumps({"text": result.text, "citations": result.citations}),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        return LLMResult(
            value=result.text,
            citations=result.citations,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider=result.provider,
        )

    # ---------------------------------------------------------------- internals

    def _complete(self, **kwargs: Any) -> Any:
        """Budget check, provider call, spend accounting, error translation."""
        from app.db import session_scope
        from app.services import usage

        # Whose budget is this? None outside a request (scripts, tests), which
        # is unattributed and uncapped by design.
        user_id = usage.get_current_user_id()
        with session_scope() as session:
            usage.check_budget(session, user_id)

        try:
            result = self.provider.complete(**kwargs)
        except ProviderRateLimited as exc:
            raise RateLimited(str(exc)) from exc
        except (ProviderUnavailable, ProviderRefused, ProviderError) as exc:
            raise LLMUnavailable(str(exc)) from exc

        # Attribute the spend before anything else can raise. A refused or
        # truncated response still consumed billable tokens, so accounting must
        # not sit behind a code path that bails out.
        with session_scope() as session:
            usage.record_spend(
                session,
                user_id,
                model=f"{result.provider}:{result.model}" if result.provider else result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        return result


# -------------------------------------------------------------------- helpers


def render(template: str, **vars_: Any) -> str:
    """`{{name}}` substitution.

    Deliberately not `str.format` or Jinja: prompt files contain JSON examples
    full of literal braces, and `.format` would choke on every one of them.
    """
    out = template
    for name, value in vars_.items():
        if not isinstance(value, str):
            value = json.dumps(value, indent=2, sort_keys=True, default=str)
        out = out.replace("{{" + name + "}}", value)
    return out


def _extract_json(text: str) -> str:
    """Recover a *valid* JSON document from a response that may be wrapped.

    Providers with constrained decoding return bare JSON. Local models often add
    a ```json fence or a sentence of preamble despite being told not to, and
    rejecting that outright would fail a response that is otherwise fine.

    Each candidate is parsed before being returned, so the caller either gets
    something `json.loads` accepts or a clear error. Returning unparsed text and
    letting Pydantic complain produced misleading messages about missing fields
    when the real problem was a truncated response.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty response")

    for candidate in _json_candidates(stripped):
        try:
            json.loads(candidate)
        except ValueError:
            continue
        return candidate

    raise ValueError(
        "no valid JSON found in the response (it may have been truncated)"
    )


def _json_candidates(text: str):
    """Progressively looser guesses at where the JSON is."""
    yield text

    # ```json ... ``` or bare ``` ... ```
    fence = text.find("```")
    if fence != -1:
        line_end = text.find("\n", fence)
        if line_end != -1:
            close = text.find("```", line_end)
            yield text[line_end : close if close != -1 else None].strip()

    # Widest span between the first opener and the last matching closer.
    for opener, closer in (("{", "}"), ("[", "]")):
        first = text.find(opener)
        last = text.rfind(closer)
        if first != -1 and last > first:
            yield text[first : last + 1]


def _cache_key(**parts: Any) -> str:
    """Content-addressed, and scoped to the owning user.

    Mixing the user id into the hash means two accounts with byte-identical
    inputs still get separate entries. That forgoes a little cost saving, but
    cached output is derived from someone's résumé and serving it to a different
    account is not a trade worth making.
    """
    from app.services import usage

    parts["user_id"] = usage.get_current_user_id()
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    from app.db import session_scope
    from app.models import LLMCache

    with session_scope() as session:
        row = session.get(LLMCache, key)
        return row.response_json if row else None


def _cache_put(
    key: str,
    *,
    prompt_name: str,
    version: str,
    model: str,
    payload: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    from app.db import session_scope
    from app.models import LLMCache
    from app.services import usage as usage_service

    with session_scope() as session:
        session.merge(
            LLMCache(
                key=key,
                user_id=usage_service.get_current_user_id(),
                prompt_name=prompt_name,
                prompt_version=version,
                model=model,
                response_json=payload,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )


def _cache_delete(key: str) -> None:
    from app.db import session_scope
    from app.models import LLMCache

    with session_scope() as session:
        row = session.get(LLMCache, key)
        if row:
            session.delete(row)


_singleton: LLMClient | None = None


def get_llm() -> LLMClient:
    global _singleton
    if _singleton is None:
        _singleton = LLMClient()
    return _singleton
