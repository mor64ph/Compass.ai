"""Settings, the Google OAuth handshake, and user administration."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import create_invite
from app.config import get_settings
from app.db import get_session
from app.deps import AdminUser, CurrentUser
from app.llm import get_llm
from app.llm.client import PROMPT_VERSIONS
from app.llm.providers import all_providers
from app.models import LLMCache, User
from app.services import embeddings, usage
from app.services.google import oauth
from app.web import flash, render

logger = logging.getLogger(__name__)
router = APIRouter()

OAUTH_STATE_KEY = "_google_oauth_state"


@router.get("/settings")
def view(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    settings = get_settings()
    llm = get_llm()

    # Cache stats are scoped to this user - one invitee has no business seeing
    # how much everyone else has generated.
    cache_rows, cache_in, cache_out = session.execute(
        select(
            func.count(LLMCache.key),
            func.coalesce(func.sum(LLMCache.input_tokens), 0),
            func.coalesce(func.sum(LLMCache.output_tokens), 0),
        ).where(LLMCache.user_id == user.id)
    ).one()

    others = []
    if user.is_admin:
        others = list(
            session.scalars(select(User).order_by(User.created_at.asc()))
        )

    return render(
        request,
        "settings.html",
        {
            "user": user,
            "budget": usage.budget_status(user),
            "google": oauth.status(session, user.id),
            "llm_ready": llm.available(),
            "model": llm.model,
            "effort": settings.compass_effort,
            "llm_cache_enabled": settings.compass_llm_cache,
            "cache_rows": cache_rows,
            "cache_input_tokens": cache_in,
            "cache_output_tokens": cache_out,
            "prompt_versions": PROMPT_VERSIONS,
            "embedding": embeddings.backend_status(),
            # Every provider, so Settings can show what is configured and what
            # it would take to enable the others.
            "providers": [p.describe() for p in all_providers(settings)],
            "active_provider": llm.describe_provider(),
            "provider_choice": settings.compass_llm_provider,
            "db_url": settings.compass_db_url,
            "quality_window": settings.compass_quality_window,
            "templated_threshold": settings.compass_templated_threshold,
            "all_users": others,
        },
    )


# --------------------------------------------------------------------------
# Google OAuth
# --------------------------------------------------------------------------


@router.get("/google/connect")
def connect(request: Request, user: CurrentUser):
    try:
        url, state = oauth.authorization_url()
    except oauth.GoogleNotConfigured as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)
    request.session[OAUTH_STATE_KEY] = state
    return RedirectResponse(url, status_code=303)


@router.get("/google/callback")
def callback(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    params = request.query_params
    if params.get("error"):
        flash(request, f"Google returned an error: {params['error']}", "error")
        return RedirectResponse("/settings", status_code=303)

    expected = request.session.pop(OAUTH_STATE_KEY, None)
    returned = params.get("state")
    if not expected or expected != returned:
        # Mismatched state means the redirect did not originate from this
        # session - refuse it rather than exchanging the code.
        flash(
            request,
            "OAuth state did not match this session. Start the connection again.",
            "error",
        )
        return RedirectResponse("/settings", status_code=303)

    code = params.get("code")
    if not code:
        flash(request, "Google did not return an authorization code.", "error")
        return RedirectResponse("/settings", status_code=303)

    try:
        oauth.exchange_code(session, user.id, code=code, state=returned)
    except Exception as exc:
        logger.exception("Google token exchange failed")
        flash(request, f"Could not complete the Google connection: {exc}", "error")
        return RedirectResponse("/settings", status_code=303)

    flash(request, "Google account connected. Gmail and Calendar sync are live.", "success")
    return RedirectResponse("/settings", status_code=303)


@router.post("/google/disconnect")
def disconnect(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    oauth.disconnect(session, user.id)
    flash(
        request,
        "Disconnected locally. Revoke Compass's access at "
        "myaccount.google.com/permissions to complete it on Google's side.",
        "info",
    )
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


@router.post("/settings/cache/clear")
def clear_cache(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    session.execute(delete(LLMCache).where(LLMCache.user_id == user.id))
    session.commit()
    flash(request, "Your LLM response cache was cleared.", "info")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/provider/reload")
def reload_provider(request: Request, user: CurrentUser):
    """Re-resolve the provider after editing .env or starting Ollama, without
    restarting the server."""
    llm = get_llm()
    llm.reset_provider()
    provider = llm.describe_provider()
    if provider.get("available"):
        flash(
            request,
            f"Now using {provider.get('label')} ({provider.get('model')}).",
            "success",
        )
    else:
        flash(
            request,
            f"{provider.get('label')} is still not configured: {provider.get('needs')}",
            "warn",
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/embeddings/reload")
def reload_embeddings(request: Request, user: CurrentUser):
    """Re-attempt the embedding model after a failed load.

    A transient failure would otherwise pin this process to the lexical fallback
    until the server restarts, leaving every subsequent score silently
    approximate.
    """
    embeddings.reset_backend()
    embeddings.preload_in_background()
    flash(
        request,
        "Reloading the embedding model in the background - this takes about "
        "30 seconds. Refresh this page to see the result.",
        "info",
    )
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------
# User administration (admins only)
# --------------------------------------------------------------------------


@router.post("/settings/invite")
def invite(
    request: Request,
    admin: AdminUser,
    email: str = Form(...),
    display_name: str = Form(""),
    monthly_budget_usd: float = Form(5.0),
    session: Session = Depends(get_session),
):
    try:
        invited = create_invite(
            session,
            email=email,
            invited_by=admin,
            display_name=display_name,
            monthly_budget_usd=max(0.0, monthly_budget_usd),
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)

    link = str(request.base_url).rstrip("/") + f"/invite/{invited.invite_token}"
    flash(
        request,
        f"Invite created for {invited.email}. Send them this link - it expires in "
        f"14 days and works once: {link}",
        "success",
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/users/{user_id}/budget")
def set_budget(
    user_id: int,
    request: Request,
    admin: AdminUser,
    monthly_budget_usd: float = Form(...),
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if target is None:
        flash(request, "No such user.", "error")
        return RedirectResponse("/settings", status_code=303)
    target.monthly_budget_usd = max(0.0, monthly_budget_usd)
    session.commit()
    flash(
        request,
        f"{target.label}'s monthly AI budget is now "
        + ("uncapped." if target.monthly_budget_usd == 0 else f"${target.monthly_budget_usd:.2f}."),
        "success",
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/users/{user_id}/deactivate")
def deactivate(
    user_id: int,
    request: Request,
    admin: AdminUser,
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if target is None:
        flash(request, "No such user.", "error")
    elif target.id == admin.id:
        # Locking the only admin out of their own instance is not a feature.
        flash(request, "You cannot deactivate your own account.", "error")
    else:
        target.is_active = False
        session.commit()
        flash(
            request,
            f"{target.label} can no longer sign in. Their data is retained - "
            "delete the account to remove it.",
            "info",
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/users/{user_id}/reactivate")
def reactivate(
    user_id: int,
    request: Request,
    admin: AdminUser,
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if target is None:
        flash(request, "No such user.", "error")
    elif target.id == admin.id:
        flash(request, "You cannot change your own status.", "error")
    else:
        target.is_active = True
        session.commit()
        flash(request, f"{target.label} can sign in again.", "success")
    return RedirectResponse("/settings", status_code=303)
