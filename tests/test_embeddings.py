"""Tests for the embedding backend's loading and degradation behaviour.

Deliberately a separate module: the other suites use an autouse fixture that
stubs `get_backend` for speed, which would prevent these tests from exercising
the real loader at all.
"""

from __future__ import annotations

import pytest

from app.services import embeddings


@pytest.fixture(autouse=True)
def clean_backend_state(monkeypatch):
    """Reset the module singletons around each test."""
    monkeypatch.setattr(embeddings, "_backend", None)
    monkeypatch.setattr(embeddings, "_fallback_reason", "")
    yield
    monkeypatch.setattr(embeddings, "_backend", None)
    monkeypatch.setattr(embeddings, "_fallback_reason", "")


def fake_installed(monkeypatch, installed: bool) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (object() if installed else None)
        if name == "sentence_transformers"
        else None,
    )


# --------------------------------------------------------------------------
# backend_status must never trigger a load
# --------------------------------------------------------------------------


def test_backend_status_does_not_load_the_model(monkeypatch):
    """Settings only *reports* the backend. Forcing a load to answer that made
    the first page a new user opens hang for ~45 seconds."""

    def explode(*_args, **_kwargs):
        raise AssertionError("backend_status must not construct a backend")

    monkeypatch.setattr(embeddings, "SentenceTransformerBackend", explode)
    fake_installed(monkeypatch, True)

    status = embeddings.backend_status()
    assert status["loaded"] is False
    assert status["is_model"] is True       # installed, just not loaded yet
    assert status["retryable"] is False     # nothing has failed
    assert embeddings._backend is None


def test_backend_status_reports_fallback_when_package_absent(monkeypatch):
    fake_installed(monkeypatch, False)
    status = embeddings.backend_status()
    assert status["installed"] is False
    assert status["is_model"] is False
    assert status["name"] == "lexical-fallback"


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_load_failure_falls_back_and_is_retryable(monkeypatch):
    """A transient load failure must not silently pin the process to the
    approximate backend with no way back.

    Observed for real: a `--reload` restart interrupted an in-flight torch
    import, leaving transformers half-initialised, and every later score in that
    worker was quietly lexical.
    """

    def boom(_name):
        raise ImportError("Could not import module 'PreTrainedModel'")

    monkeypatch.setattr(embeddings, "SentenceTransformerBackend", boom)
    fake_installed(monkeypatch, True)

    backend = embeddings.get_backend()
    assert not backend.semantic          # fell back, as designed
    assert backend.name == "lexical-fallback"

    status = embeddings.backend_status()
    assert status["loaded"] is True
    assert status["is_model"] is False
    assert status["retryable"] is True
    assert "PreTrainedModel" in status["fallback_reason"]


def test_missing_package_is_not_offered_as_retryable(monkeypatch):
    """No point offering a retry when the dependency simply is not installed."""

    def boom(_name):
        raise ModuleNotFoundError("No module named 'sentence_transformers'")

    monkeypatch.setattr(embeddings, "SentenceTransformerBackend", boom)
    fake_installed(monkeypatch, False)

    embeddings.get_backend()
    status = embeddings.backend_status()
    assert status["installed"] is False
    assert status["retryable"] is False


def test_reset_backend_allows_a_second_attempt(monkeypatch):
    """Recovery must not require restarting the server."""
    attempts: list[int] = []

    def flaky(_name):
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("transient")
        return embeddings.LexicalBackend()  # stand-in for a successful load

    monkeypatch.setattr(embeddings, "SentenceTransformerBackend", flaky)
    fake_installed(monkeypatch, True)

    embeddings.get_backend()
    assert len(attempts) == 1

    # Cached: no second attempt without an explicit reset.
    embeddings.get_backend()
    assert len(attempts) == 1

    embeddings.reset_backend()
    assert embeddings._backend is None
    assert embeddings._fallback_reason == ""

    embeddings.get_backend()
    assert len(attempts) == 2


def test_backend_is_loaded_once_and_cached(monkeypatch):
    calls: list[str] = []

    def counting(name):
        calls.append(name)
        return embeddings.LexicalBackend()

    monkeypatch.setattr(embeddings, "SentenceTransformerBackend", counting)
    fake_installed(monkeypatch, True)

    for _ in range(5):
        embeddings.get_backend()
    assert len(calls) == 1


# --------------------------------------------------------------------------
# The lexical fallback still has to be a usable similarity function
# --------------------------------------------------------------------------


def test_lexical_fallback_ranks_sensibly():
    backend = embeddings.LexicalBackend()
    matrix = backend.similarity_matrix(
        ["Optimised DAX measures and cut refresh time"],
        [
            "Tuned DAX measures to reduce refresh duration",
            "Coordinated catering for the regional sales offsite",
        ],
    )
    related, unrelated = matrix[0]
    assert related > unrelated
    assert 0.0 <= unrelated <= related <= 1.0


def test_similarity_of_identical_text_is_one():
    backend = embeddings.LexicalBackend()
    assert backend.similarity_matrix(["same text"], ["same text"])[0][0] == pytest.approx(1.0)


def test_empty_inputs_do_not_crash():
    backend = embeddings.LexicalBackend()
    assert backend.similarity_matrix([], ["a"]) == []
    assert backend.similarity_matrix(["a"], []) == [[]]
    assert backend.similarity_matrix([""], [""])[0][0] == 0.0
