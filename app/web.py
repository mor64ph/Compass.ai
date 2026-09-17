"""Shared web-layer helpers: the Jinja environment, flash messages, and a
decorator that turns service-layer failures into a banner instead of a 500."""

from __future__ import annotations

import functools
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


templates.env.filters["score_class"] = _score_class
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
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        request: Request | None = kwargs.get("request")
        if request is None:
            request = next((a for a in args if isinstance(a, Request)), None)
        try:
            return await fn(*args, **kwargs)
        except RateLimited as exc:
            logger.info("Rate limited: %s", exc)
            return _explain(request, str(exc), "warn")
        except LLMUnavailable as exc:
            logger.warning("LLM unavailable: %s", exc)
            return _explain(request, str(exc), "error")

    return wrapper


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
