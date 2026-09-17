"""Authentication: invite-only by default, open registration by choice.

Three ways an account can come into existence, and they are separate functions
because they are allowed to produce different things:

* `create_owner` — the first account, once, and only while none exists. Admin,
  uncapped. `/setup` closes permanently afterwards.
* `create_invite` / `accept_invite` — the default path. An admin issues an
  unguessable single-use token that expires in 14 days.
* `register` — self-service, and **only when `COMPASS_OPEN_SIGNUP` is on**.
  Never an admin, never uncapped, subject to an account ceiling.

Open registration is off in a fresh clone on purpose. Switching it on means
strangers store a résumé, an employment history and contact details on your
instance, which makes you the data fiduciary for it — see docs/CONSTRAINTS.md §6
for what that obliges and what Compass does and does not do about it.
`delete_account` exists because of that: an instance anyone can join has to be
one they can leave.

No password reset emails and no OAuth login. Both need infrastructure this does
not have, and a reset flow with nowhere to send mail is a security hole rather
than a feature.

Passwords use `hashlib.scrypt` from the standard library rather than bcrypt or
argon2. It is a memory-hard KDF, it is in Python itself so there is no dependency
to keep patched, and at these parameters a single verification costs ~100ms —
which is the point.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import User, utcnow

logger = logging.getLogger(__name__)

# scrypt cost parameters. n must be a power of two; 2**15 with r=8 needs about
# 32 MB per hash, which is deliberately hostile to offline cracking.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
# `hashlib.scrypt` enforces maxmem; the default is too small for these params.
SCRYPT_MAXMEM = 132 * 1024 * 1024

SESSION_USER_KEY = "_user_id"
INVITE_TTL_DAYS = 14
MIN_PASSWORD_LENGTH = 12


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return `scrypt$n$r$p$salt_hex$hash_hex`.

    The parameters are stored alongside the hash so they can be raised later
    without invalidating existing passwords.
    """
    if not password:
        raise ValueError("Password must not be empty")
    salt = os.urandom(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Returns False rather than raising on a
    malformed record, so a corrupted row cannot become an auth bypass."""
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError) as exc:
        logger.warning("Malformed password hash rejected: %s", exc)
        return False
    return hmac.compare_digest(derived, bytes.fromhex(hash_hex))


def password_problem(password: str, confirm: str = None) -> str:
    """Return a human-readable problem, or "" if the password is acceptable.

    A length floor only. Composition rules ("one capital, one symbol") push
    people towards `Password1!` and are worse than length.
    """
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters - a short phrase is fine."
    if password.lower() in {"password1234", "compass12345", "123456789012"}:
        return "Pick something less guessable."
    return ""


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------


def new_invite_token() -> str:
    return secrets.token_urlsafe(32)


def create_invite(
    session: Session,
    *,
    email: str,
    invited_by: User | None = None,
    is_admin: bool = False,
    monthly_budget_usd: float = 5.0,
    display_name: str = "",
) -> User:
    """Create (or re-invite) an account and return it with a fresh token."""
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("That does not look like an email address.")

    user = session.scalars(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email)
        session.add(user)
    elif user.has_password:
        raise ValueError(f"{email} already has an account.")

    user.display_name = display_name.strip() or user.display_name
    user.is_admin = is_admin
    user.is_active = True
    user.monthly_budget_usd = monthly_budget_usd
    user.invite_token = new_invite_token()
    user.invite_expires_at = utcnow() + timedelta(days=INVITE_TTL_DAYS)
    user.invited_by_id = invited_by.id if invited_by else None
    session.commit()
    session.refresh(user)
    return user


def user_for_invite(session: Session, token: str) -> User | None:
    if not token:
        return None
    user = session.scalars(select(User).where(User.invite_token == token)).first()
    if user is None:
        return None
    if user.invite_expires_at and user.invite_expires_at < utcnow():
        return None
    return user


def accept_invite(session: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    user.invite_token = None
    user.invite_expires_at = None
    session.commit()


# --------------------------------------------------------------------------
# Login / session
# --------------------------------------------------------------------------


def authenticate(session: Session, *, email: str, password: str) -> User | None:
    """Verify credentials. Runs the KDF even when the email is unknown, so
    response timing does not reveal which addresses have accounts."""
    user = session.scalars(
        select(User).where(User.email == email.strip().lower())
    ).first()

    stored = user.password_hash if (user and user.has_password) else _DUMMY_HASH
    ok = verify_password(password, stored)

    if user is None or not user.has_password or not user.is_active or not ok:
        return None

    user.last_login_at = utcnow()
    session.commit()
    return user


# A real hash of a random value, so the unknown-email path costs the same as the
# known-email path. Computed once at import.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def log_in(request: Request, user: User) -> None:
    request.session.clear()  # new session id contents on privilege change
    request.session[SESSION_USER_KEY] = user.id


def log_out(request: Request) -> None:
    request.session.clear()


def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    """The logged-in user, or None. Use `require_user` to demand one."""
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(
    request: Request, session: Session = Depends(get_session)
) -> User:
    """Dependency for every page that touches user data.

    Redirects to the login page rather than returning 401, because every caller
    is a browser. The originally requested path is preserved so login can send
    the user back to it.
    """
    user = current_user(request, session)
    if user is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=303,
            detail="Login required",
            headers={"Location": f"/login?next={_quote(target)}"},
        )
    return user


def require_admin(request: Request, session: Session = Depends(get_session)) -> User:
    user = require_user(request, session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrators only")
    return user


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects.

    `//evil.example` and `https://evil.example` are both open-redirect vectors
    and both start life as an innocent-looking `?next=` parameter.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def user_count(session: Session) -> int:
    return session.scalar(select(func.count(User.id))) or 0


def needs_bootstrap(session: Session) -> bool:
    """True when no usable account exists yet, so the app should offer to create
    the first (owner) account instead of an unanswerable login form."""
    return session.scalar(
        select(func.count(User.id)).where(User.password_hash != "")
    ) == 0


def create_owner(
    session: Session, *, email: str, password: str, display_name: str = ""
) -> User:
    """Create the first account. Refuses once any account exists, so this can
    never be used to escalate later."""
    if not needs_bootstrap(session):
        raise ValueError("An account already exists; ask an admin for an invite.")
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)

    user = User(
        email=email.strip().lower(),
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        is_admin=True,
        is_active=True,
        monthly_budget_usd=0.0,  # owner is uncapped
        period_started_on=date.today(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Created owner account %s", user.email)
    return user


def register(
    session: Session, *, email: str, password: str, display_name: str = ""
) -> User:
    """Create a self-registered account.

    Separate from `create_owner` and `accept_invite` on purpose, because the
    three differ in what they are allowed to produce. This one never makes an
    admin and never leaves the budget uncapped: a self-registered account spends
    the operator's API key, so an uncapped one is an open invitation to spend it.

    Raises `ValueError` with a message meant for the user.
    """
    settings = get_settings()
    if not settings.compass_open_signup:
        raise ValueError(
            "This instance is invite-only. Ask whoever runs it for a link."
        )

    email = (email or "").strip().lower()
    # Not a full RFC check — that is a losing game and the address is never
    # mailed. Enough to reject the shapes that are obviously not addresses,
    # including `@example.com`, which passes a naive "contains @ and a dot".
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain or domain.startswith("."):
        raise ValueError("That does not look like an email address.")
    if domain.endswith(".") or ".." in email or " " in email:
        raise ValueError("That does not look like an email address.")
    if len(email) > 320:
        raise ValueError("That email address is too long.")

    problem = password_problem(password)
    if problem:
        raise ValueError(problem)

    cap = settings.compass_max_accounts
    if cap and user_count(session) >= cap:
        raise ValueError(
            "This instance has reached its account limit. Nothing is wrong — "
            "it is a personal deployment with a ceiling on how many people it "
            "will hold."
        )

    existing = session.scalars(select(User).where(User.email == email)).first()
    if existing is not None:
        # Deliberately the same wording whether the address is taken or the
        # password was rejected, so /signup cannot be used to enumerate who has
        # an account here. `/login` is careful about this; it would be pointless
        # if registration gave it away.
        raise ValueError(
            "Could not create that account. If you already have one, sign in "
            "instead."
        )

    user = User(
        email=email,
        display_name=(display_name or "").strip()[:200],
        password_hash=hash_password(password),
        is_admin=False,
        is_active=True,
        monthly_budget_usd=max(0.01, settings.compass_signup_budget_usd),
        period_started_on=date.today(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Registered account %s", user.email)
    return user


def delete_account(session: Session, user: User) -> None:
    """Erase an account and everything belonging to it.

    Every aggregate root cascades from `user.id`, so deleting the row removes
    the profile, résumés, applications, postings, discovered jobs, contacts,
    tokens, sync state and cached LLM responses with it.

    This exists because an instance that lets strangers register has to let them
    leave. India's DPDP Act gives a data principal the right to erasure, and a
    tool that stores someone's employment history with no way to remove it is not
    one you should be running. Uploaded files are unlinked separately — those
    live on disk, not in a table.
    """
    from app.models import ResumeVariant

    stored = [
        v.stored_path
        for v in session.scalars(
            select(ResumeVariant).where(ResumeVariant.user_id == user.id)
        )
        if v.stored_path
    ]

    email = user.email
    session.delete(user)
    session.commit()

    for path in stored:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            # The row is already gone; a stranded upload is a housekeeping
            # problem, not a reason to fail the deletion the user asked for.
            logger.warning("Could not remove %s during account deletion: %s", path, exc)

    logger.info("Deleted account %s and its uploads", email)


def touch_period(user: User) -> None:
    """Roll the spend window over when the calendar month changes."""
    today = date.today()
    if user.period_started_on is None or (
        user.period_started_on.year,
        user.period_started_on.month,
    ) != (today.year, today.month):
        user.period_started_on = today
        user.period_spend_usd = 0.0


def now() -> datetime:
    return utcnow()
