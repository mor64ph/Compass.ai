"""Local model provider, via Ollama's HTTP API.

This is the backend that needs no key, no account and no network. It is the
answer to "what happens when my API access ends" — Compass keeps working, for
free, forever, with résumé text never leaving the machine. That last part sits
well with the rest of the design: embeddings already run locally for the same
reason.

**Be honest about the trade.** On a 4-core laptop with no usable GPU, a 3B model
generates a couple of thousand tokens in minutes, not seconds. Compass's prompts
are long (career profile + résumé + JD) and its outputs are structured JSON, so
this is a viable fallback rather than a pleasant daily driver. It is also weaker
at following the anti-fabrication instructions the prompts lean on, which is why
`docs/CONSTRAINTS.md` §5 matters more here, not less.

Request/response shape verified against
https://github.com/ollama/ollama/blob/main/docs/api.md.
"""

from __future__ import annotations

import json
import logging

from app.llm.providers.base import (
    Message,
    ProviderError,
    ProviderResult,
    ProviderUnavailable,
    describe_error,
    to_ollama_schema,
)

logger = logging.getLogger(__name__)

# Generous: a long structured generation on CPU genuinely can take minutes, and
# timing out at the default 60s would make the backend look broken when it is
# merely slow.
TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 2

# Context window. Compass routinely sends 5-8k tokens of profile + résumé + JD,
# and Ollama's default of 2048 would silently truncate the prompt - dropping the
# JD and producing confident nonsense. Raising it costs RAM, which is the real
# constraint on a laptop.
DEFAULT_NUM_CTX = 8192

EFFORT_TEMPERATURE = {
    "low": 0.7,
    "medium": 0.4,
    "high": 0.2,
    "xhigh": 0.1,
    "max": 0.1,
}


class OllamaProvider:
    name = "ollama"
    # No grounded search: a local model has no web access, and pretending
    # otherwise would produce invented citations.
    supports_web_search = False

    def __init__(self, *, host: str, model: str, num_ctx: int = DEFAULT_NUM_CTX) -> None:
        self.host = (host or "http://localhost:11434").rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self._reachable: bool | None = None

    # ------------------------------------------------------------------ status

    def available(self) -> bool:
        """Cheap liveness probe, cached for the process.

        `available()` is called on page loads, so it must not block. A 2-second
        connect timeout to localhost is effectively instant when Ollama is
        running and fails fast when it is not.
        """
        if self._reachable is not None:
            return self._reachable
        import requests

        try:
            response = requests.get(f"{self.host}/api/tags", timeout=PROBE_TIMEOUT_SECONDS)
            self._reachable = response.status_code == 200
        except Exception:
            self._reachable = False
        return self._reachable

    def forget_probe(self) -> None:
        """Re-check on the next `available()` - for after the user starts Ollama."""
        self._reachable = None

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": "Local model (Ollama)",
            "model": self.model,
            "available": self.available(),
            "needs": f"Ollama running at {self.host} with `ollama pull {self.model}`",
            "cost_note": "Free and fully local. Slow on CPU: minutes per generation.",
        }

    def installed_models(self) -> list[str]:
        import requests

        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            if response.status_code != 200:
                return []
            return sorted(
                entry.get("name", "") for entry in (response.json().get("models") or [])
            )
        except Exception:
            return []

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
        if web_search:
            raise ProviderError(
                "The local model cannot search the web. Run company research on a "
                "cloud provider, or generate the brief without research."
            )
        if not self.available():
            raise ProviderUnavailable(
                f"Ollama is not reachable at {self.host}. Start it (it runs as a "
                "background service after install), then retry from Settings."
            )

        payload: dict = {
            "model": self.model,
            "messages": (
                ([{"role": "system", "content": system}] if system else [])
                + [{"role": m.role, "content": m.content} for m in messages]
            ),
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
                "temperature": EFFORT_TEMPERATURE.get((effort or "high").lower(), 0.2),
            },
        }
        if json_schema is not None:
            # A JSON schema here (rather than the string "json") constrains
            # decoding, which is what makes a small model's structured output
            # usable at all.
            payload["format"] = to_ollama_schema(json_schema)

        import requests

        try:
            response = requests.post(
                f"{self.host}/api/chat",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise ProviderError(
                f"The local model did not finish within {TIMEOUT_SECONDS // 60} "
                "minutes. Try a smaller model, or a shorter input."
            ) from exc
        except requests.RequestException as exc:
            self._reachable = None
            raise ProviderUnavailable(f"Could not reach Ollama: {exc}") from exc

        if response.status_code == 404:
            raise ProviderError(
                f"Ollama does not have the model {self.model!r}. Fetch it with:\n"
                f"    ollama pull {self.model}"
            )
        if response.status_code >= 400:
            raise ProviderError(describe_error("Ollama", response.status_code, response.text))

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("Ollama returned a response that was not JSON.") from exc

        text = ((body.get("message") or {}).get("content")) or ""
        if not text.strip():
            raise ProviderError(
                "The local model returned an empty response. This usually means the "
                "prompt exceeded its context window - try a shorter résumé or JD."
            )

        return ProviderResult(
            text=text,
            input_tokens=int(body.get("prompt_eval_count") or 0),
            output_tokens=int(body.get("eval_count") or 0),
            model=body.get("model", self.model),
            provider="ollama",
        )
