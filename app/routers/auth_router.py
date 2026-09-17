"""Login, logout, first-run setup, and invite acceptance.

These are the only routes that do **not** require a logged-in user, so they are
also the only place where an unauthenticated request can do anything at all.
Each one is deliberately narrow:

* `/setup` works only while no account exists, and refuses afterwards.
* `/invite/{token}` needs an unguessable token that expires and is single-use.
* `/login` reports the same message for a wrong password and an unknown address.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import auth
from app.db import get_session
from app.security import client_address, login_throttle
from app.web import flash, render

logger = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------
# First run
# --------------------------------------------------------------------------


@router.get("/setup")
def setup_form(request: Request, session: Session = Depends(get_session)):
    if not auth.needs_bootstrap(session):
        flash(request, "Compass is already set up. Sign in instead.", "info")
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", {})


@router.post("/setup")
def setup_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    confirm: str = Form(...),
    session: Session = Depends(get_session),
):
    if not auth.needs_bootstrap(session):
        flash(request, "Compass is already set up.", "error")
        return RedirectResponse("/login", status_code=303)

    problem = auth.password_problem(password, confirm)
    if problem:
        flash(request, problem, "error")
        return render(
            request, "setup.html", {"email": email, "display_name": display_name}
        )

    try:
        user = auth.create_owner(
            session, email=email, password=password, display_name=display_name
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return render(request, "setup.html", {"email": email})

    auth.log_in(request, user)
    flash(request, "Account created. This is the owner account - it is uncapped.", "success")
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# Login / logout
# --------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, session: Session = Depends(get_session)):
    if auth.needs_bootstrap(session):
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request, session) is not None:
        return RedirectResponse("/", status_code=303)
    return render(
        request, "login.html", {"next": auth.safe_next(request.query_params.get("next"))}
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: Session = Depends(get_session),
):
    client = client_address(request)

    # Checked before the KDF runs: a throttled attempt should not also cost
    # 100ms of CPU, or the lockout becomes its own denial-of-service.
    wait = login_throttle.retry_after(email=email, client=client)
    if wait:
        logger.warning("Throttled login for %r from %s", email, client)
        flash(
            request,
            f"Too many failed attempts. Try again in about {max(1, wait // 60)} "
            "minute(s).",
            "error",
        )
        return render(
            request, "login.html", {"email": email, "next": auth.safe_next(next)}
        )

    user = auth.authenticate(session, email=email, password=password)
    if user is None:
        login_throttle.record_failure(email=email, client=client)
        # One message for every failure mode. Distinguishing "no such account"
        # from "wrong password" tells an attacker which addresses are worth
        # attacking.
        logger.info("Failed login for %r", email)
        flash(request, "Email or password is incorrect.", "error")
        return render(
            request, "login.html", {"email": email, "next": auth.safe_next(next)}
        )

    login_throttle.record_success(email=email, client=client)
    auth.log_in(request, user)
    return RedirectResponse(auth.safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth.log_out(request)
    flash(request, "Signed out.", "info")
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------


@router.get("/invite/{token}")
def invite_form(token: str, request: Request, session: Session = Depends(get_session)):
    user = auth.user_for_invite(session, token)
    if user is None:
        return render(request, "invite.html", {"invalid": True})
    return render(request, "invite.html", {"invited": user, "token": token})


@router.post("/invite/{token}")
def invite_submit(
    token: str,
    request: Request,
    display_name: str = Form(""),
    password: str = Form(...),
    confirm: str = Form(...),
    session: Session = Depends(get_session),
):
    user = auth.user_for_invite(session, token)
    if user is None:
        return render(request, "invite.html", {"invalid": True})

    problem = auth.password_problem(password, confirm)
    if problem:
        flash(request, problem, "error")
        return render(request, "invite.html", {"invited": user, "token": token})

    if display_name.strip():
        user.display_name = display_name.strip()
    auth.accept_invite(session, user, password)
    auth.log_in(request, user)
    flash(request, f"Welcome to Compass, {user.label}.", "success")
    return RedirectResponse("/", status_code=303)
