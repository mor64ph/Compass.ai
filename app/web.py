"""Shared web-layer helpers: the Jinja environment, flash messages, and a
decorator that turns service-layer failures into a banner instead of a 500."""

from __future__ import annotations

import functools
import inspect
import logging
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.llm.client import LLMUnavailable, RateLimited
from app.models import Stage

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

templates.env.globals.update(
    {
        "STAGES": Stage.ordered(),
        "app_name": "Compass",
    }
)


def _score_class(value: float | int | None) -> str:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        return "muted"
    if score >= 75:
        return "good"
    if score >= 55:
        return "warn"
    return "bad"


def _pct(value: float | int | None) -> str:
    try:
        return f"{float(value or 0):.0f}"
    except (TypeError, ValueError):
        return "0"


def _score_word(value: float | int | None) -> str:
    """The same judgement `score_class` encodes as a colour, said in a word.

    A score that is only green or only red is unreadable to a reader with
    deuteranopia, and unreadable to anyone looking at a greyscale screenshot.
    The two are meant to be used together - `.judgement` in the stylesheet pairs
    the dot with this text.
    """
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        return "not scored"
    if score >= 75:
        return "strong"
    if score >= 55:
        return "needs work"
    return "weak"


templates.env.filters["score_class"] = _score_class
templates.env.filters["score_word"] = _score_word
templates.env.filters["pct"] = _pct


# --------------------------------------------------------------------------
# Flash messages
# --------------------------------------------------------------------------

FLASH_KEY = "_flashes"


def flash(request: Request, message: str, level: str = "info") -> None:
    """Queue a one-shot message. Levels: info | success | warn | error."""
    bucket = request.session.setdefault(FLASH_KEY, [])
    bucket.append({"message": message, "level": level})


def take_flashes(request: Request) -> list[dict]:
    return request.session.pop(FLASH_KEY, [])


def render(request: Request, template: str, context: dict[str, Any] | None = None, **kwargs):
    ctx = {"request": request, "flashes": take_flashes(request), **(context or {}), **kwargs}
    return templates.TemplateResponse(request, template, ctx)


def partial(request: Request, template: str, context: dict[str, Any] | None = None, **kwargs):
    """Render without consuming flashes - HTMX swaps replace a fragment, and
    eating the queue here would drop a message the next full page should show."""
    ctx = {"request": request, **(context or {}), **kwargs}
    return templates.TemplateResponse(request, template, ctx)


def error_fragment(message: str, level: str = "error") -> HTMLResponse:
    css = {"error": "bad", "warn": "warn"}.get(level, "info")
    return HTMLResponse(f'<div class="banner {css}">{message}</div>')


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def guard(fn):
    """Wrap a route so an unconfigured API key or a rate limit reads as an
    explanation rather than a stack trace.

    Deliberately narrow: only the two exceptions Compass raises on purpose are
    caught. Anything unexpected still surfaces as a real error.

    Works on both sync and async handlers, and that matters more than it looks.
    Every route this decorates makes a **blocking** LLM call of 20-60 seconds.
    Declared `async def`, such a route runs on the event loop and holds it for
    the whole call: with one worker, nothing else can be served meanwhile -
    including a platform health check. The host concludes the instance is dead,
    restarts it, and the user sees 502 Bad Gateway instead of their gap report.

    Declared as a plain `def`, FastAPI runs the handler in a threadpool and the
    loop stays free. So these routes are deliberately synchronous, and this
    wrapper must not force them back into a coroutine.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except RateLimited as exc:
                logger.info("Rate limited: %s", exc)
                return _explain(_request_from(args, kwargs), str(exc), "warn")
            except LLMUnavailable as exc:
                logger.warning("LLM unavailable: %s", exc)
                return _explain(_request_from(args, kwargs), str(exc), "error")

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RateLimited as exc:
            logger.info("Rate limited: %s", exc)
            return _explain(_request_from(args, kwargs), str(exc), "warn")
        except LLMUnavailable as exc:
            logger.warning("LLM unavailable: %s", exc)
            return _explain(_request_from(args, kwargs), str(exc), "error")

    return sync_wrapper


def _request_from(args, kwargs) -> Request | None:
    request = kwargs.get("request")
    if request is not None:
        return request
    return next((a for a in args if isinstance(a, Request)), None)


def _explain(request: Request | None, message: str, level: str):
    is_htmx = bool(request and request.headers.get("hx-request"))
    if is_htmx:
        return error_fragment(message, level)
    if request is not None:
        flash(request, message, level)
        referer = request.headers.get("referer") or "/"
        from fastapi.responses import RedirectResponse

        return RedirectResponse(referer, status_code=303)
    return error_fragment(message, level)
