"""Login, logout, first-run setup, and invite acceptance.

These are the only routes that do **not** require a logged-in user, so they are
also the only place where an unauthenticated request can do anything at all.
Each one is deliberately narrow:

* `/setup` works only while no account exists, and refuses afterwards.
* `/invite/{token}` needs an unguessable token that expires and is single-use.
* `/login` reports the same message for a wrong password and an unknown address.
* `/signup` exists only when COMPASS_OPEN_SIGNUP is set, is throttled per source
  address, and reports the same message whether the address is taken or the
  password was rejected — so it cannot be used to enumerate who has an account.

`/account/delete` is here rather than in the settings router because it is the
counterpart to registration: an instance anyone can join has to be one they can
leave.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import auth
from app.config import get_settings
from app.db import get_session
from app.security import client_address, login_throttle, signup_throttle
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


def _demo_hint() -> dict | None:
    """Credentials to publish on the login page, or None.

    Only when demo mode is on. Reading it from `app.services.demo` rather than
    repeating the strings means the page cannot drift out of step with the
    account that was actually seeded.
    """
    if not get_settings().compass_demo_mode:
        return None
    from app.services import demo

    return {
        "email": demo.DEMO_EMAIL,
        "password": demo.DEMO_PASSWORD,
        "budget": demo.DEMO_BUDGET_USD,
    }


@router.get("/login")
def login_form(request: Request, session: Session = Depends(get_session)):
    if auth.needs_bootstrap(session):
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request, session) is not None:
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "login.html",
        {
            "next": auth.safe_next(request.query_params.get("next")),
            "demo": _demo_hint(),
            "open_signup": get_settings().compass_open_signup,
        },
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
            request, "login.html",
            {"email": email, "next": auth.safe_next(next), "demo": _demo_hint(),
             "open_signup": get_settings().compass_open_signup},
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
            request, "login.html",
            {"email": email, "next": auth.safe_next(next), "demo": _demo_hint(),
             "open_signup": get_settings().compass_open_signup},
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
# Self-service registration  (only when COMPASS_OPEN_SIGNUP is on)
# --------------------------------------------------------------------------


def _signup_context(session: Session, **extra) -> dict:
    settings = get_settings()
    cap = settings.compass_max_accounts
    taken = auth.user_count(session)
    return {
        "budget": settings.compass_signup_budget_usd,
        "places_left": max(0, cap - taken) if cap else None,
        "full": bool(cap and taken >= cap),
        **extra,
    }


@router.get("/signup")
def signup_form(request: Request, session: Session = Depends(get_session)):
    if not get_settings().compass_open_signup:
        # Not a 404: the page genuinely exists on other instances, and saying
        # so is more useful than pretending the route is unknown.
        flash(request, "This instance is invite-only.", "info")
        return RedirectResponse("/login", status_code=303)
    if auth.needs_bootstrap(session):
        return RedirectResponse("/setup", status_code=303)
    if auth.current_user(request, session) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "signup.html", _signup_context(session))


@router.post("/signup")
def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    display_name: str = Form(""),
    accept: str = Form(""),
    session: Session = Depends(get_session),
):
    if not get_settings().compass_open_signup:
        flash(request, "This instance is invite-only.", "error")
        return RedirectResponse("/login", status_code=303)

    client = client_address(request)

    # Throttled on the source address, using the same limiter as login. Without
    # it one script fills the account table and burns the ceiling in seconds.
    # Keyed on a fixed string rather than the submitted email, because an
    # attacker chooses the email and would otherwise get a fresh allowance per
    # attempt.
    wait = signup_throttle.retry_after(email="signup", client=client)
    if wait:
        logger.warning("Throttled signup from %s", client)
        flash(
            request,
            f"Too many attempts from here. Try again in about "
            f"{max(1, wait // 60)} minute(s).",
            "error",
        )
        return RedirectResponse("/signup", status_code=303)

    problem = auth.password_problem(password, confirm)
    if problem:
        flash(request, problem, "error")
        return render(
            request, "signup.html",
            _signup_context(session, email=email, display_name=display_name),
        )

    if accept != "true":
        flash(
            request,
            "Please confirm you have read what this instance stores.",
            "warn",
        )
        return render(
            request, "signup.html",
            _signup_context(session, email=email, display_name=display_name),
        )

    # Counted before the attempt, not only on failure. A *successful*
    # registration is the thing worth rate-limiting — counting only rejections
    # would let a script create accounts at full speed until the ceiling.
    signup_throttle.record_failure(email="signup", client=client)

    try:
        user = auth.register(
            session, email=email, password=password, display_name=display_name
        )
    except ValueError as exc:
        flash(request, str(exc), "error")
        return render(
            request, "signup.html",
            _signup_context(session, email=email, display_name=display_name),
        )

    auth.log_in(request, user)
    flash(
        request,
        "Account created. Start with the career profile — everything else reads "
        "from it.",
        "success",
    )
    return RedirectResponse("/profile", status_code=303)


# --------------------------------------------------------------------------
# Leaving
# --------------------------------------------------------------------------


@router.post("/account/delete")
def delete_account(
    request: Request,
    confirm_email: str = Form(""),
    session: Session = Depends(get_session),
):
    """Erase the signed-in account and everything belonging to it.

    Exists because an instance strangers can join has to be one they can leave —
    India's DPDP Act gives a data principal the right to erasure. The owner is
    refused: deleting the only admin would strand the instance with no way to
    administer it, and `/setup` does not reopen.
    """
    user = auth.current_user(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if user.is_admin:
        flash(
            request,
            "The administrator account cannot be deleted from here — it would "
            "leave the instance with no way to manage it.",
            "error",
        )
        return RedirectResponse("/settings", status_code=303)

    # Typing the address is the confirmation. A one-click irreversible delete on
    # a page someone is skimming is a trap.
    if confirm_email.strip().lower() != user.email:
        flash(
            request,
            "Type your email address exactly to confirm deletion.",
            "warn",
        )
        return RedirectResponse("/settings", status_code=303)

    auth.delete_account(session, user)
    auth.log_out(request)
    flash(request, "Your account and all its data have been deleted.", "info")
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
