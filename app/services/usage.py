"""Per-user LLM spend tracking and caps (PRD NFR: cost control).

Phase 1 had one in-process rate limiter, which is fine when the only user is
also the person paying. Phase 2 invites other people onto someone else's API
key, so "how many calls per hour" stops being the useful question and "how many
dollars, whose, and what happens at the ceiling" starts.

The owning user is carried in a `ContextVar` set per request rather than passed
through every call site. `LLMClient` is a module singleton several layers below
the router, and threading a `user` argument through `application_service` ->
`tailor` -> `llm.structured` would touch a dozen signatures to move one value
that is genuinely request-scoped.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import date

from sqlalchemy.orm import Session

from app.auth import touch_period
from app.models import User

logger = logging.getLogger(__name__)

# Set by the request dependency; None outside a request (scripts, tests).
_current_user_id: ContextVar[int | None] = ContextVar("compass_user_id", default=None)

# USD per million tokens, input/output. Falls back to Opus pricing for an
# unrecognised model so an unknown id over-estimates rather than under-charges.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICE = (5.0, 25.0)

# Whole-provider overrides, applied before the per-model table. Keyed on the
# `provider:` prefix the LLM client attaches to the model string.
PROVIDER_PRICES: dict[str, tuple[float, float]] = {
    # Runs on the user's own machine: no billing to meter.
    "ollama": (0.0, 0.0),
    # Gemini's free tier is rate-limited rather than metered. Priced at zero so
    # a free-tier user is never blocked by a budget cap that does not apply to
    # them; if you move to a paid tier, add the real rates here.
    "gemini": (0.0, 0.0),
}


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would exceed a user's ceiling."""


def set_current_user(user_id: int | None):
    """Bind the owning user for the current request. Returns the token so the
    caller can reset it - important under a thread pool, where a leaked value
    would attribute the next request's spend to the wrong account."""
    return _current_user_id.set(user_id)


def reset_current_user(token) -> None:
    _current_user_id.reset(token)


def get_current_user_id() -> int | None:
    return _current_user_id.get()


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """`model` may be bare (`claude-opus-5`) or provider-qualified
    (`gemini:gemini-2.5-flash`), which is what the LLM client records."""
    provider, _, bare = (model or "").partition(":")
    if bare and provider in PROVIDER_PRICES:
        price_in, price_out = PROVIDER_PRICES[provider]
    else:
        price_in, price_out = MODEL_PRICES.get(bare or model, DEFAULT_PRICE)
    return input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


def check_budget(session: Session, user_id: int | None) -> None:
    """Raise `BudgetExceeded` when the user is already over their ceiling.

    Checked *before* the call, which means a single request can overshoot by one
    generation. Enforcing a hard pre-flight limit would need a reliable token
    estimate for a response that does not exist yet; overshooting by at most one
    call is the better trade.
    """
    if user_id is None:
        return  # scripts and tests run unattributed
    user = session.get(User, user_id)
    if user is None or user.monthly_budget_usd <= 0:
        return  # unknown, or deliberately uncapped (the owner)

    touch_period(user)
    session.commit()

    if user.period_spend_usd >= user.monthly_budget_usd:
        raise BudgetExceeded(
            f"You have used ${user.period_spend_usd:.2f} of your "
            f"${user.monthly_budget_usd:.2f} monthly AI budget. It resets on the "
            "1st. Compass's résumé checker, JD scoring and tracker keep working "
            "in the meantime - they cost nothing."
        )


def record_spend(
    session: Session,
    user_id: int | None,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Attribute a completed call. Returns the cost in USD."""
    cost = estimate_cost(model, input_tokens, output_tokens)
    if user_id is None or cost <= 0:
        return cost
    user = session.get(User, user_id)
    if user is None:
        return cost

    touch_period(user)
    user.period_spend_usd += cost
    user.lifetime_spend_usd += cost
    session.commit()

    if user.monthly_budget_usd > 0:
        used = user.period_spend_usd / user.monthly_budget_usd
        if used >= 0.8:
            logger.warning(
                "%s has used %.0f%% of their monthly AI budget ($%.2f of $%.2f)",
                user.email, used * 100, user.period_spend_usd, user.monthly_budget_usd,
            )
    return cost


def budget_status(user: User) -> dict:
    """For the Settings page and the dashboard banner."""
    touch_period(user)
    capped = user.monthly_budget_usd > 0
    percent = (
        min(100.0, user.period_spend_usd / user.monthly_budget_usd * 100.0)
        if capped
        else 0.0
    )
    return {
        "capped": capped,
        "budget_usd": user.monthly_budget_usd,
        "spent_usd": round(user.period_spend_usd, 4),
        "percent_used": round(percent, 1),
        "lifetime_usd": round(user.lifetime_spend_usd, 4),
        "exhausted": capped and user.period_spend_usd >= user.monthly_budget_usd,
    }
