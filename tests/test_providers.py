"""Tests for the LLM provider layer.

All HTTP is mocked - these must run offline, with no keys, and never touch a
real API. What they check is the part that is easy to get wrong and invisible
until it fails in production: the request shape, the schema dialect translation,
and whether an error becomes a useful message or a stack trace.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.llm.client import _extract_json
from app.llm.providers import build_provider
from app.llm.providers.base import (
    Message,
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderUnavailable,
    to_gemini_schema,
    to_ollama_schema,
)
from app.llm.providers.gemini_provider import GeminiProvider
from app.llm.providers.ollama_provider import OllamaProvider

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "nested": {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    },
    "required": ["name", "tags", "nested"],
    "additionalProperties": False,
}


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# --------------------------------------------------------------------------
# Schema dialect translation
# --------------------------------------------------------------------------


def test_gemini_schema_strips_unsupported_keywords():
    """Google's responseSchema is an OpenAPI subset and rejects
    `additionalProperties`, which the hardened Anthropic schema always sets."""
    out = to_gemini_schema(SCHEMA)
    assert "additionalProperties" not in json.dumps(out)
    # Required survives - it is the part that actually shapes the output.
    assert out["required"] == ["name", "tags", "nested"]
    assert out["properties"]["nested"]["required"] == ["count"]


def test_gemini_schema_pins_property_order():
    out = to_gemini_schema(SCHEMA)
    assert out["propertyOrdering"] == ["name", "tags", "nested"]


def test_gemini_schema_does_not_mutate_the_input():
    before = json.dumps(SCHEMA, sort_keys=True)
    to_gemini_schema(SCHEMA)
    assert json.dumps(SCHEMA, sort_keys=True) == before


def test_ollama_schema_keeps_standard_json_schema():
    out = to_ollama_schema({**SCHEMA, "$schema": "http://json-schema.org/draft"})
    assert "$schema" not in out
    # Unlike Gemini, llama.cpp's grammar converter handles these.
    assert out["additionalProperties"] is False


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def gemini_ok_payload(text: str = '{"name":"x","tags":[],"nested":{"count":1}}'):
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 45,
            "totalTokenCount": 165,
        },
        "modelVersion": "gemini-2.5-flash",
    }


def test_gemini_is_unavailable_without_a_key():
    provider = GeminiProvider(api_key="", model="gemini-2.5-flash")
    assert provider.available() is False
    with pytest.raises(ProviderUnavailable, match="aistudio.google.com"):
        provider.complete(system="s", messages=[Message("user", "u")], max_tokens=100)


def test_gemini_request_shape(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data)
        return FakeResponse(200, gemini_ok_payload())

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
    result = provider.complete(
        system="You are a parser.",
        messages=[Message("user", "parse this")],
        max_tokens=4096,
        json_schema=SCHEMA,
        effort="high",
    )

    assert captured["url"].endswith("/models/gemini-2.5-flash:generateContent")
    # The key goes in a header, not the query string, so it stays out of logs.
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert "key=" not in captured["url"]

    body = captured["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "You are a parser."
    assert body["contents"][0]["role"] == "user"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 4096
    # high effort -> low temperature, because invention is the failure mode.
    assert body["generationConfig"]["temperature"] <= 0.2

    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.provider == "gemini"


def test_gemini_assistant_role_is_renamed_to_model(monkeypatch):
    """Gemini calls the assistant turn "model"; sending "assistant" is rejected."""
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["body"] = json.loads(data)
        return FakeResponse(200, gemini_ok_payload("hello"))

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    GeminiProvider(api_key="k", model="m").complete(
        system="",
        messages=[Message("user", "hi"), Message("assistant", "there")],
        max_tokens=100,
    )
    assert [c["role"] for c in captured["body"]["contents"]] == ["user", "model"]


def test_gemini_omits_schema_for_freeform_calls(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["body"] = json.loads(data)
        return FakeResponse(200, gemini_ok_payload("prose"))

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    GeminiProvider(api_key="k", model="m").complete(
        system="chat", messages=[Message("user", "hi")], max_tokens=100
    )
    assert "responseSchema" not in captured["body"]["generationConfig"]
    assert "responseMimeType" not in captured["body"]["generationConfig"]


def test_gemini_web_search_adds_the_grounding_tool(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["body"] = json.loads(data)
        return FakeResponse(
            200,
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "research"}]},
                        "finishReason": "STOP",
                        "groundingMetadata": {
                            "groundingChunks": [
                                {"web": {"uri": "https://example.com/a", "title": "A"}},
                                {"web": {"uri": "https://example.com/a", "title": "dup"}},
                                {"web": {"uri": "https://example.com/b", "title": "B"}},
                            ]
                        },
                    }
                ],
                "usageMetadata": {},
            },
        )

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    result = GeminiProvider(api_key="k", model="m").complete(
        system="", messages=[Message("user", "research")], max_tokens=100, web_search=True
    )
    assert captured["body"]["tools"] == [{"google_search": {}}]
    # Deduplicated, and only URLs the provider actually returned.
    assert [c["url"] for c in result.citations] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


@pytest.mark.parametrize(
    "status,exc,needle",
    [
        (429, ProviderRateLimited, "rate limit"),
        (401, ProviderUnavailable, "rejected the API key"),
        (403, ProviderUnavailable, "rejected the API key"),
        (404, ProviderError, "COMPASS_GEMINI_MODEL"),
        # 5xx is transient - "currently experiencing high demand" was observed
        # in practice on the newest models - so it must read as retry-able
        # rather than as a configuration error.
        (500, ProviderRateLimited, "temporarily unavailable"),
        (503, ProviderRateLimited, "temporarily unavailable"),
    ],
)
def test_gemini_errors_become_actionable_messages(monkeypatch, status, exc, needle):
    import requests

    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(status, text="boom")
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)  # don't actually wait
    with pytest.raises(exc, match=needle):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_explains_that_grounding_is_not_free_tier(monkeypatch):
    """Verified against the live API: plain generation succeeds on
    gemini-3.5-flash, 3.5-flash-lite and 3.1-flash-lite while the same call with
    `google_search` returns 429. Telling the user to "wait a moment" would be
    advice that never comes true."""
    import requests

    from app.llm.providers import gemini_provider

    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(429, {"error": {"message": "quota exceeded"}}),
    )
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _s: None)

    with pytest.raises(ProviderUnavailable, match="not available on the Gemini free"):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "research")], max_tokens=10,
            web_search=True,
        )


def test_gemini_daily_quota_says_to_switch_model(monkeypatch):
    """Per-day and per-minute exhaustion need different advice: one is a short
    wait, the other means that model is done until the quota resets."""
    import requests

    from app.llm.providers import gemini_provider

    payload = {
        "error": {
            "message": "You exceeded your current quota",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            "quotaValue": "20",
                        }
                    ],
                }
            ],
        }
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(429, payload))
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _s: None)

    with pytest.raises(ProviderRateLimited, match="20 requests per day"):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_surfaces_googles_own_error_message(monkeypatch):
    """Regression: a hand-written 404 message replaced Google's far more useful
    one - *"gemini-2.5-flash is no longer available to new users, use
    gemini-3.6-flash"* - and turned a 30-second fix into a debugging session."""
    import requests

    payload = {
        "error": {
            "code": 404,
            "message": "This model models/gemini-2.5-flash is no longer available "
                       "to new users. Please update your code to use "
                       "models/gemini-3.6-flash",
            "status": "NOT_FOUND",
        }
    }
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(404, payload))
    with pytest.raises(ProviderError, match="no longer available to new users"):
        GeminiProvider(api_key="k", model="gemini-2.5-flash").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_waits_the_delay_google_asks_for(monkeypatch):
    """The free tier is 20 req/min and the 429 body states the exact wait.
    Failing instead of sleeping ~20s wastes the user's time."""
    import requests

    from app.llm.providers import gemini_provider

    slept: list[float] = []
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(
                429,
                {
                    "error": {
                        "message": "Quota exceeded. Please retry in 21.0s",
                        "details": [{"retryDelay": "21s"}],
                    }
                },
            )
        return FakeResponse(200, gemini_ok_payload("done"))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda s: slept.append(s))

    result = GeminiProvider(api_key="k", model="m").complete(
        system="", messages=[Message("user", "u")], max_tokens=10
    )
    assert result.text == "done"
    assert calls["n"] == 2, "should have retried once"
    assert slept and 21.0 <= slept[0] <= 22.0, slept


def test_gemini_gives_up_when_the_delay_is_absurd(monkeypatch):
    """Nobody waits ten minutes on a page load."""
    import requests

    from app.llm.providers import gemini_provider

    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(
            429, {"error": {"message": "nope", "details": [{"retryDelay": "600s"}]}}
        ),
    )
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _s: pytest.fail("slept"))
    with pytest.raises(ProviderRateLimited):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_retries_a_tls_interception_error(monkeypatch):
    """A TLS-inspecting corporate proxy intercepts intermittently: the same URL
    succeeded minutes after failing."""
    import requests

    from app.llm.providers import gemini_provider

    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.SSLError("self-signed certificate in chain")
        return FakeResponse(200, gemini_ok_payload("recovered"))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _s: None)

    result = GeminiProvider(api_key="k", model="m").complete(
        system="", messages=[Message("user", "u")], max_tokens=10
    )
    assert result.text == "recovered"


def test_gemini_explains_a_persistent_tls_failure(monkeypatch):
    import requests

    from app.llm.providers import gemini_provider

    def always_fail(*a, **k):
        raise requests.exceptions.SSLError("self-signed certificate in chain")

    monkeypatch.setattr(requests, "post", always_fail)
    monkeypatch.setattr(gemini_provider.time, "sleep", lambda _s: None)
    with pytest.raises(ProviderUnavailable, match="corporate proxy"):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_safety_block_is_a_refusal(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **k: FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}),
    )
    with pytest.raises(ProviderRefused, match="declined"):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_gemini_timeout_is_explained(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.Timeout()

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ProviderError, match="did not respond"):
        GeminiProvider(api_key="k", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


def ollama_ok_payload(content: str = '{"name":"x","tags":[],"nested":{"count":1}}'):
    return {
        "model": "llama3.2:3b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": 900,
        "eval_count": 210,
    }


def test_ollama_probe_failure_means_unavailable(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "get", boom)
    provider = OllamaProvider(host="http://localhost:11434", model="llama3.2:3b")
    assert provider.available() is False
    with pytest.raises(ProviderUnavailable, match="not reachable"):
        provider.complete(system="", messages=[Message("user", "u")], max_tokens=10)


def test_ollama_probe_is_cached_then_can_be_forgotten(monkeypatch):
    import requests

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResponse(200, {"models": []})

    monkeypatch.setattr(requests, "get", fake_get)
    provider = OllamaProvider(host="http://h", model="m")
    for _ in range(5):
        assert provider.available() is True
    assert len(calls) == 1, "available() must not probe on every page load"

    provider.forget_probe()
    provider.available()
    assert len(calls) == 2


def test_ollama_request_shape(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json.loads(data)
        return FakeResponse(200, ollama_ok_payload())

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"models": []}))
    monkeypatch.setattr(requests, "post", fake_post)

    provider = OllamaProvider(host="http://localhost:11434", model="llama3.2:3b", num_ctx=8192)
    result = provider.complete(
        system="You are a parser.",
        messages=[Message("user", "parse this")],
        max_tokens=2000,
        json_schema=SCHEMA,
        effort="high",
    )

    assert captured["url"] == "http://localhost:11434/api/chat"
    body = captured["body"]
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "You are a parser."}
    assert body["messages"][1] == {"role": "user", "content": "parse this"}
    # A schema, not the string "json": constrained decoding is what makes a
    # small model's structured output usable at all.
    assert isinstance(body["format"], dict)
    assert body["options"]["num_predict"] == 2000
    # The context window must be raised or long prompts silently truncate.
    assert body["options"]["num_ctx"] == 8192

    assert result.input_tokens == 900
    assert result.output_tokens == 210
    assert result.provider == "ollama"


def test_ollama_cannot_search_the_web(monkeypatch):
    """A local model has no web access; pretending otherwise would produce
    invented citations."""
    provider = OllamaProvider(host="http://h", model="m")
    assert provider.supports_web_search is False
    with pytest.raises(ProviderError, match="cannot search the web"):
        provider.complete(
            system="", messages=[Message("user", "u")], max_tokens=10, web_search=True
        )


def test_ollama_missing_model_says_how_to_fix_it(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"models": []}))
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(404, text="not found"))
    with pytest.raises(ProviderError, match="ollama pull"):
        OllamaProvider(host="http://h", model="llama3.2:3b").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


def test_ollama_empty_response_blames_the_context_window(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"models": []}))
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse(200, ollama_ok_payload(""))
    )
    with pytest.raises(ProviderError, match="context window"):
        OllamaProvider(host="http://h", model="m").complete(
            system="", messages=[Message("user", "u")], max_tokens=10
        )


# --------------------------------------------------------------------------
# Registry / auto-selection
# --------------------------------------------------------------------------


def test_auto_prefers_gemini_when_a_key_is_present():
    settings = Settings(
        compass_llm_provider="auto", gemini_api_key="k", anthropic_api_key="a"
    )
    assert build_provider(settings).name == "gemini"


def test_auto_falls_through_to_anthropic(monkeypatch):
    import requests

    # No Gemini key, and Ollama not running.
    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "get", boom)
    settings = Settings(
        compass_llm_provider="auto", gemini_api_key="", anthropic_api_key="sk-test"
    )
    assert build_provider(settings).name == "anthropic"


def test_explicit_choice_is_honoured_even_when_unconfigured():
    """An explicit choice that silently fell back would leave the user wondering
    why output quality changed."""
    settings = Settings(compass_llm_provider="ollama", gemini_api_key="k")
    assert build_provider(settings).name == "ollama"


def test_unknown_provider_is_rejected_clearly():
    with pytest.raises(ValueError, match="Unknown provider"):
        build_provider(Settings(compass_llm_provider="gpt5"))


def test_no_provider_configured_still_returns_something_that_explains_itself(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "get", boom)
    provider = build_provider(
        Settings(compass_llm_provider="auto", gemini_api_key="", anthropic_api_key="")
    )
    assert provider.available() is False
    with pytest.raises(ProviderUnavailable):
        provider.complete(system="", messages=[Message("user", "u")], max_tokens=10)


# --------------------------------------------------------------------------
# JSON recovery - the local-model reality
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1}',
        '  {"a":1}  ',
        '```json\n{"a":1}\n```',
        '```\n{"a":1}\n```',
        'Here is the JSON you asked for:\n{"a":1}',
        'Sure!\n```json\n{"a":1}\n```\nHope that helps.',
    ],
)
def test_json_is_recovered_from_wrapped_responses(raw):
    """Providers with constrained decoding return bare JSON. Local models often
    add a fence or a sentence of preamble despite being told not to, and
    rejecting that would fail a response that is otherwise fine."""
    assert json.loads(_extract_json(raw)) == {"a": 1}


def test_json_array_is_recovered():
    assert json.loads(_extract_json('```json\n[1,2]\n```')) == [1, 2]


@pytest.mark.parametrize("raw", ["", "   ", "no json here at all", "{ unterminated"])
def test_unrecoverable_responses_raise(raw):
    with pytest.raises(ValueError):
        _extract_json(raw)
