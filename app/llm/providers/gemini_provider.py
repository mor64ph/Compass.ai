"""Google Gemini provider, over the documented REST API.

Uses plain HTTP rather than a Google SDK on purpose: the request shape here is
four fields, `requests` is already an installed dependency, and it means one
fewer SDK whose surface can shift underneath us.

Endpoint and field names verified against
https://ai.google.dev/api/generate-content and
https://ai.google.dev/gemini-api/docs/structured-output.

Why Gemini is the default provider: its free tier is genuinely usable and needs
no card, which matters when the alternative is a work-issued key with an expiry
date.
"""

from __future__ import annotations

import json
import logging
import re
import time

from app.llm.providers.base import (
    Message,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderResult,
    ProviderUnavailable,
    describe_error,
    to_gemini_schema,
)

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_SECONDS = 180

# The free tier allows 20 requests/minute. Compass fires several calls per user
# action (score -> gaps -> tailor), so brushing the quota is normal rather than
# exceptional, and Google's 429 body carries the exact delay to wait.
MAX_RETRIES = 2
# Observed delays on an exhausted free-tier minute run to ~58s, so a 45s ceiling
# turned a recoverable wait into a user-visible failure.
MAX_RETRY_WAIT_SECONDS = 70.0
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

_RETRY_IN_RE = re.compile(r"retry in ([0-9.]+)s", re.I)

# Effort has no direct equivalent; map it onto temperature. Lower temperature
# for the tasks Compass runs at high effort, because those are the ones where
# invention is the failure mode.
EFFORT_TEMPERATURE = {
    "low": 0.7,
    "medium": 0.4,
    "high": 0.2,
    "xhigh": 0.1,
    "max": 0.1,
}


class GeminiProvider:
    name = "gemini"
    supports_web_search = True

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = (api_key or "").strip()
        self.model = model

    # ------------------------------------------------------------------ status

    def available(self) -> bool:
        return bool(self._api_key)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": "Google Gemini",
            "model": self.model,
            "available": self.available(),
            "needs": "GEMINI_API_KEY in .env - free tier at aistudio.google.com/apikey",
            "cost_note": "Free tier available; no card required.",
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
                "No Gemini API key configured. Get a free one at "
                "https://aistudio.google.com/apikey and set GEMINI_API_KEY in .env."
            )

        generation_config: dict = {"maxOutputTokens": max_tokens}
        temperature = EFFORT_TEMPERATURE.get((effort or "high").lower())
        if temperature is not None:
            generation_config["temperature"] = temperature

        if json_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = to_gemini_schema(json_schema)

        body: dict = {
            "contents": [
                {
                    "role": "user" if m.role == "user" else "model",
                    "parts": [{"text": m.content}],
                }
                for m in messages
            ],
            "generationConfig": generation_config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if web_search:
            # Grounding with Google Search. A search tool, not a crawler - the
            # same distinction docs/CONSTRAINTS.md draws for Anthropic's.
            body["tools"] = [{"google_search": {}}]

        try:
            payload = self._post(f"/models/{self.model}:generateContent", body)
        except ProviderRateLimited as exc:
            if web_search:
                # Verified across gemini-3.5-flash, 3.5-flash-lite and
                # 3.1-flash-lite: plain generation succeeds on all of them while
                # the same call with `google_search` returns 429. Grounding is
                # not a free-tier feature, so "wait a moment and try again" is
                # advice that will never come true.
                raise ProviderUnavailable(
                    "Google Search grounding is not available on the Gemini free "
                    "tier, so cited company research cannot run. Generate the brief "
                    "without research, enable billing on your Google Cloud project, "
                    "or switch COMPASS_LLM_PROVIDER to anthropic for this step. "
                    "Everything else in Compass works on the free tier."
                ) from exc
            raise

        return self._parse(payload)

    # ------------------------------------------------------------------ internals

    def _headers(self) -> dict:
        # The key goes in a header rather than `?key=`, which is also documented:
        # query strings end up in proxy and server logs, headers usually do not.
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        """POST, honouring the retry delay Google supplies on a quota error.

        The free tier allows 20 requests/minute and the 429 body says exactly
        how long to wait ("Please retry in 21.0s"). Surfacing that to the user as
        a failure when sleeping 20 seconds would have worked is a waste of their
        time - so wait, within reason, and only give up if the delay is longer
        than anyone would sit through.
        """
        import requests

        last_response = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.post(
                    API_ROOT + path,
                    headers=self._headers(),
                    data=json.dumps(body),
                    timeout=TIMEOUT_SECONDS,
                )
            except requests.Timeout as exc:
                raise ProviderError(
                    f"Gemini did not respond within {TIMEOUT_SECONDS}s."
                ) from exc
            except requests.exceptions.SSLError as exc:
                # A TLS-inspecting corporate proxy intercepting this connection.
                # Observed intermittently: the same URL succeeds minutes later,
                # so retry before giving up.
                if attempt < MAX_RETRIES:
                    logger.warning("TLS error talking to Gemini, retrying: %s", exc)
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise ProviderUnavailable(
                    "TLS verification failed talking to Gemini. This usually means a "
                    "corporate proxy is inspecting HTTPS. Compass already trusts the "
                    "Windows certificate store; if this persists, ask IT for the proxy "
                    f"root CA. ({exc})"
                ) from exc
            except requests.RequestException as exc:
                raise ProviderUnavailable(f"Could not reach Gemini: {exc}") from exc

            last_response = response
            if response.status_code not in RETRYABLE_STATUSES or attempt == MAX_RETRIES:
                break

            delay = _retry_delay(response)
            if delay is None or delay > MAX_RETRY_WAIT_SECONDS:
                break
            logger.info(
                "Gemini asked us to wait %.1fs (attempt %d/%d) - sleeping",
                delay, attempt + 1, MAX_RETRIES,
            )
            time.sleep(delay + 0.5)  # a little past the deadline, not exactly on it

        return self._check(last_response)

    def _get(self, path: str) -> dict:
        import requests

        try:
            response = requests.get(
                API_ROOT + path, headers=self._headers(), timeout=30
            )
        except requests.RequestException as exc:
            raise ProviderUnavailable(f"Could not reach Gemini: {exc}") from exc
        return self._check(response)

    @staticmethod
    def _server_message(response) -> str:
        """Google's own error text, which is usually more useful than anything
        we could infer from the status code.

        Learned the hard way: a hand-written "does not recognise that model"
        replaced the actual message — *"gemini-2.5-flash is no longer available
        to new users, use gemini-3.6-flash"* — and turned a 30-second fix into a
        debugging session.
        """
        try:
            return (response.json().get("error") or {}).get("message", "").strip()
        except Exception:
            return ""

    @staticmethod
    def _quota_id(response) -> str:
        """`GenerateRequestsPerDayPerProjectPerModel-FreeTier` and friends."""
        try:
            error = (response.json() or {}).get("error") or {}
            for detail in error.get("details") or []:
                for violation in detail.get("violations") or []:
                    if violation.get("quotaId"):
                        return violation["quotaId"]
        except Exception:
            pass
        return ""

    @classmethod
    def _check(cls, response) -> dict:
        message = cls._server_message(response) if response.status_code >= 400 else ""

        if response.status_code == 429:
            # Per-day and per-minute exhaustion need different advice: one is a
            # short wait, the other means you are done with that model until
            # tomorrow. Google distinguishes them in the quotaId, and telling a
            # user to "wait a moment" when the quota resets at midnight is
            # actively unhelpful.
            if "PerDay" in cls._quota_id(response):
                raise ProviderRateLimited(
                    "Gemini's free tier allows only 20 requests per day *per model*, "
                    "and this model is spent until the quota resets. Switch to "
                    "another model — each has its own allowance — by setting "
                    "COMPASS_GEMINI_MODEL in .env (Settings lists the options), or "
                    "use the local Ollama provider, which has no quota at all."
                )
            raise ProviderRateLimited(
                "Gemini rate limit reached. The free tier limits requests per minute "
                "- wait a moment and try again."
                + (f" ({message})" if message else "")
            )
        if response.status_code in (500, 502, 503, 504):
            # Observed in practice on the newest models: "currently experiencing
            # high demand". Transient, so it must read as retry-able rather than
            # as a configuration error.
            raise ProviderRateLimited(
                f"Gemini is temporarily unavailable - {message or 'try again shortly'}. "
                "If it persists, set COMPASS_GEMINI_MODEL to another model "
                "(Settings lists the ones your key can use)."
            )
        if response.status_code in (401, 403):
            raise ProviderUnavailable(
                "Gemini rejected the API key. Check GEMINI_API_KEY in .env, and that "
                "the Generative Language API is enabled for it."
                + (f" ({message})" if message else "")
            )
        if response.status_code == 404:
            raise ProviderError(
                (message or "Gemini does not recognise that model.")
                + " Set COMPASS_GEMINI_MODEL in .env - Settings lists the models "
                "your key can actually use."
            )
        if response.status_code >= 400:
            raise ProviderError(
                message or describe_error("Gemini", response.status_code, response.text)
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("Gemini returned a response that was not JSON.") from exc

    @staticmethod
    def _parse(payload: dict) -> ProviderResult:
        candidates = payload.get("candidates") or []
        if not candidates:
            # A prompt blocked before generation returns no candidates at all.
            feedback = (payload.get("promptFeedback") or {}).get("blockReason")
            if feedback:
                raise ProviderRefused(f"Gemini declined the request ({feedback}).")
            raise ProviderError("Gemini returned no candidates.")

        candidate = candidates[0]
        reason = candidate.get("finishReason", "")
        if reason == "SAFETY":
            raise ProviderRefused("Gemini declined this request on safety grounds.")

        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(part.get("text", "") for part in parts)

        usage = payload.get("usageMetadata") or {}
        citations = _grounding_citations(candidate)

        if reason == "MAX_TOKENS":
            logger.warning("Gemini response hit maxOutputTokens - output may be truncated")

        return ProviderResult(
            text=text,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            model=payload.get("modelVersion", ""),
            provider="gemini",
            citations=citations,
            truncated=reason == "MAX_TOKENS",
        )


def _retry_delay(response) -> float | None:
    """Seconds Google wants us to wait, from the structured detail if present and
    the human-readable message otherwise."""
    try:
        error = (response.json() or {}).get("error") or {}
    except Exception:
        return None

    for detail in error.get("details") or []:
        raw = detail.get("retryDelay")
        if isinstance(raw, str) and raw.endswith("s"):
            try:
                return float(raw[:-1])
            except ValueError:
                pass

    match = _RETRY_IN_RE.search(error.get("message", "") or "")
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    # A retryable status with no stated delay: a short fixed pause is better than
    # hammering or giving up.
    return 5.0 if response.status_code in RETRYABLE_STATUSES else None


def _grounding_citations(candidate: dict) -> list[dict]:
    """Pull real source URLs out of a grounded response.

    Same rule as the Anthropic path: only URLs the provider actually returned
    are ever shown, so a citation can never be invented.
    """
    metadata = candidate.get("groundingMetadata") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for chunk in metadata.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        url = web.get("uri") or ""
        if url and url not in seen:
            seen.add(url)
            out.append({"title": web.get("title") or url, "url": url})
    return out
