"""Sentence-similarity backend.

Primary backend is a local `sentence-transformers` model - chosen so résumé and
JD text never leaves the machine, which matters more here than the last few
points of accuracy.

`sentence-transformers` pulls in torch, so it can be slow to install and the
model downloads on first use. Rather than making the whole app unusable until
that finishes, there is a pure-Python lexical fallback. Every score carries its
backend name so a fallback score is never mistaken for a semantic one.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter
from typing import Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9+#.]+")


class Backend(Protocol):
    name: str
    semantic: bool

    def similarity_matrix(self, left: list[str], right: list[str]) -> list[list[float]]: ...


class SentenceTransformerBackend:
    semantic = True

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        # Load from the local cache first. Left to itself, SentenceTransformer
        # revalidates every file against the Hugging Face API on construction -
        # measured at ~50s here versus ~0.6s cache-only. A cache miss raises
        # OSError immediately, so falling back costs nothing on first run.
        try:
            self._model = SentenceTransformer(model_name, local_files_only=True)
            logger.debug("Loaded %s from the local cache", model_name)
        except Exception:
            logger.info(
                "%s is not in the local cache - downloading it once (~90 MB)", model_name
            )
            self._model = SentenceTransformer(model_name)

    def similarity_matrix(self, left: list[str], right: list[str]) -> list[list[float]]:
        if not left or not right:
            return [[0.0] * len(right) for _ in left]
        import numpy as np

        a = self._model.encode(left, normalize_embeddings=True, show_progress_bar=False)
        b = self._model.encode(right, normalize_embeddings=True, show_progress_bar=False)
        return (np.asarray(a) @ np.asarray(b).T).tolist()


class LexicalBackend:
    """Cosine over token counts plus character 4-grams.

    The character n-grams are what stop this from being a pure bag-of-words
    match: they give partial credit for morphological variants ("model" /
    "modelling") and typos, which is most of what the semantic model buys on
    short résumé bullets.
    """

    name = "lexical-fallback"
    semantic = False

    def similarity_matrix(self, left: list[str], right: list[str]) -> list[list[float]]:
        lv = [self._vector(t) for t in left]
        rv = [self._vector(t) for t in right]
        return [[_sparse_cosine(a, b) for b in rv] for a in lv]

    @staticmethod
    def _vector(text: str) -> Counter:
        lowered = text.lower()
        tokens = _TOKEN_RE.findall(lowered)
        vec: Counter = Counter(tokens)
        squashed = re.sub(r"[^a-z0-9]+", "", lowered)
        for i in range(len(squashed) - 3):
            vec["#" + squashed[i : i + 4]] += 1
        return vec


def _sparse_cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    dot = sum(count * larger.get(key, 0) for key, count in smaller.items())
    if not dot:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


_backend: Backend | None = None
_lock = threading.Lock()
# Why the model was rejected, when it was. "" means it loaded, or was never tried.
_fallback_reason: str = ""


def get_backend() -> Backend:
    """Load the sentence-transformer once, falling back on any failure.

    Never called at import time. Most of the cost is importing torch and
    transformers (~28s on this machine, and minutes the very first time while
    Windows Defender scans the freshly installed DLLs), so `preload_in_background`
    moves it off the request path at startup.
    """
    global _backend, _fallback_reason
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        model_name = get_settings().compass_embedding_model
        started = time.monotonic()
        try:
            _backend = SentenceTransformerBackend(model_name)
            _fallback_reason = ""
            logger.info(
                "Embedding backend ready in %.0fs: %s",
                time.monotonic() - started,
                model_name,
            )
        except Exception as exc:
            _fallback_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "sentence-transformers unavailable (%s) - falling back to lexical "
                "similarity. Retry from Settings, or install it with: "
                "pip install sentence-transformers",
                exc,
            )
            _backend = LexicalBackend()
        return _backend


def reset_backend() -> None:
    """Discard the cached backend so the next call re-attempts the model.

    Without this, a *transient* load failure is permanent for the life of the
    process: `_backend` gets pinned to the lexical fallback and every subsequent
    score is silently approximate until you restart the server. Observed for
    real when a `--reload` restart interrupted an in-flight torch import and left
    `transformers` half-initialised ("Could not import module 'PreTrainedModel'").
    """
    global _backend, _fallback_reason
    with _lock:
        _backend = None
        _fallback_reason = ""


def backend_status() -> dict:
    """Describe the backend **without loading it.**

    Loading the sentence-transformer takes ~45s the first time. The Settings
    page only wants to report which backend is in play, and forcing a load to
    answer that made the first page a new user opens hang for a minute.
    `find_spec` checks importability without importing torch.
    """
    import importlib.util

    settings = get_settings()
    installed = importlib.util.find_spec("sentence_transformers") is not None
    if _backend is not None:
        return {
            "loaded": True,
            "name": _backend.name,
            "is_model": _backend.semantic,
            "configured": settings.compass_embedding_model,
            "installed": installed,
            "fallback_reason": _fallback_reason,
            # The package is present but the load failed - offer a retry rather
            # than leaving the user silently on approximate scores.
            "retryable": installed and not _backend.semantic,
        }
    return {
        "loaded": False,
        "name": settings.compass_embedding_model if installed else "lexical-fallback",
        "is_model": installed,
        "configured": settings.compass_embedding_model,
        "installed": installed,
        "fallback_reason": "",
        "retryable": False,
    }


def preload_in_background() -> None:
    """Warm the model on a daemon thread at startup.

    Without this the first JD scoring request pays the whole ~45s model load
    while the user waits on a spinner. Startup is not blocked - the server is
    serving immediately and the model arrives shortly after.
    """

    def _warm() -> None:
        try:
            get_backend()  # logs its own timing
        except Exception:  # pragma: no cover - already logged in get_backend
            logger.debug("Background embedding preload failed", exc_info=True)

    threading.Thread(target=_warm, name="compass-embedding-preload", daemon=True).start()


def similarity(left: str, right: str) -> float:
    return get_backend().similarity_matrix([left], [right])[0][0]


def pairwise_mean(texts: list[str]) -> float:
    """Mean similarity across all distinct pairs. Used by the Epic D quality gate
    to detect a set of cover letters converging on one template."""
    if len(texts) < 2:
        return 0.0
    matrix = get_backend().similarity_matrix(texts, texts)
    total = 0.0
    count = 0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            total += matrix[i][j]
            count += 1
    return total / count if count else 0.0


def best_match_scores(needles: list[str], haystack: list[str]) -> list[float]:
    """For each needle, its similarity to the closest item in the haystack."""
    if not needles:
        return []
    if not haystack:
        return [0.0] * len(needles)
    matrix = get_backend().similarity_matrix(needles, haystack)
    return [max(row) if row else 0.0 for row in matrix]
