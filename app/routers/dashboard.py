"""Dashboard: funnel, nudges, and what needs attention next."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.llm import get_llm
from app.services import application_service, profile_service, resume_service, usage
from app.services.google import calendar_sync, oauth
from app.web import render

router = APIRouter()


@router.get("/")
def home(
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    profile = profile_service.get_active_profile(session, user.id)
    variants = resume_service.variants(session, user.id)
    google = oauth.status(session, user.id)

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "budget": usage.budget_status(user),
            "profile": profile,
            "completeness": profile_service.profile_completeness(profile),
            "variants": variants,
            "master": next((v for v in variants if v.is_master), None),
            "funnel": application_service.funnel(session, user.id),
            "counts": application_service.counts_by_stage(session, user.id),
            "stale": application_service.stale_applications(session, user.id)[:6],
            "followups": application_service.due_followups(session, user.id),
            "activity": application_service.recent_activity(session, user.id),
            "google": google,
            # Read from the calendar only when connected - otherwise this page
            # would make a doomed API call on every load.
            "upcoming": (
                calendar_sync.upcoming(session, user.id) if google["connected"] else []
            ),
            "llm_ready": get_llm().available(),
        },
    )
