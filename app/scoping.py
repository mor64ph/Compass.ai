"""Tenant-scoped record lookup.

`session.get(Application, 5)` returns application 5 regardless of who owns it.
In a multi-user app that is an IDOR hole: change the number in the URL, read
someone else's résumé. Every route that resolves an id from the path must go
through `require_owned` instead.

Children (an `ApplicationEvent`, a `PracticeSession`) have no `user_id` of their
own and are reached through their parent, so `require_owned_via` walks the
relationship and checks the root.
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    Base,
    CareerProfile,
    Contact,
    GoogleToken,
    InterviewPrepBrief,
    JobPosting,
    PracticeSession,
    ResumeVariant,
)

T = TypeVar("T", bound=Base)

# Models that carry `user_id` directly.
OWNED_MODELS: tuple[type, ...] = (
    CareerProfile,
    ResumeVariant,
    JobPosting,
    Application,
    Contact,
    GoogleToken,
)


def get_owned(session: Session, model: type[T], obj_id: int | None, user_id: int) -> T | None:
    """Fetch by id, but only if this user owns it. Returns None otherwise -
    deliberately indistinguishable from "does not exist", so the URL space does
    not leak which ids are real."""
    if obj_id is None:
        return None
    if not hasattr(model, "user_id"):
        raise TypeError(
            f"{model.__name__} has no user_id; use require_owned_via for child records"
        )
    return session.scalars(
        select(model).where(model.id == obj_id, model.user_id == user_id)
    ).first()


def require_owned(session: Session, model: type[T], obj_id: int | None, user_id: int) -> T:
    obj = get_owned(session, model, obj_id, user_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


def require_prep_brief(
    session: Session, brief_id: int | None, user_id: int
) -> InterviewPrepBrief:
    """A brief belongs to an application, which belongs to a user."""
    brief = session.get(InterviewPrepBrief, brief_id) if brief_id else None
    if brief is None:
        raise HTTPException(status_code=404, detail="Prep brief not found")
    require_owned(session, Application, brief.application_id, user_id)
    return brief


def require_practice_session(
    session: Session, practice_id: int | None, user_id: int
) -> PracticeSession:
    practice = session.get(PracticeSession, practice_id) if practice_id else None
    if practice is None:
        raise HTTPException(status_code=404, detail="Practice session not found")
    require_prep_brief(session, practice.brief_id, user_id)
    return practice
