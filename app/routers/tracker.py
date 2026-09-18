"""Epic E - the Kanban tracker and the Gmail/Calendar sync triggers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.models import Application
from app.scoping import require_owned
from app.services import application_service
from app.services.google import calendar_sync, gmail_sync, oauth
from app.web import flash, guard, partial, render

router = APIRouter(prefix="/tracker")


@router.get("")
def board(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    google = oauth.status(session, user.id)
    return render(
        request,
        "tracker.html",
        {
            "user": user,
            "board": application_service.board(session, user.id),
            "stale": dict(application_service.stale_applications(session, user.id)),
            "funnel": application_service.funnel(session, user.id),
            "google": google,
            "upcoming": (
                calendar_sync.upcoming(session, user.id) if google["connected"] else []
            ),
        },
    )


@router.post("/{application_id}/stage")
def move(
    application_id: int,
    request: Request,
    user: CurrentUser,
    stage: str = Form(...),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    application_service.set_stage(session, application, stage)
    return partial(
        request,
        "partials/board.html",
        {
            "board": application_service.board(session, user.id),
            "stale": dict(application_service.stale_applications(session, user.id)),
        },
    )


@router.post("/sync/gmail")
@guard
def sync_gmail(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    summary = gmail_sync.sync(session, user.id)
    return partial(request, "partials/sync_result.html", {"summary": summary.as_dict()})


@router.post("/sync/calendar")
def sync_calendar(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    summary = calendar_sync.place_pending_interviews(session, user.id)
    return partial(
        request, "partials/sync_result.html", {"summary": summary.as_dict(), "kind": "calendar"}
    )


@router.post("/{application_id}/schedule")
def schedule(
    application_id: int,
    request: Request,
    user: CurrentUser,
    start: str = Form(...),
    duration_minutes: int = Form(60),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    parsed = calendar_sync.parse_datetime(start)
    if parsed is None:
        flash(request, f"Could not read '{start}' as a date and time.", "error")
        return RedirectResponse(f"/applications/{application_id}", status_code=303)

    try:
        event = calendar_sync.create_interview_event(
            session, application, start=parsed, duration_minutes=duration_minutes, note=note
        )
    except RuntimeError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/applications/{application_id}", status_code=303)

    if event is None:
        flash(request, "Connect your Google account first (Settings).", "warn")
    else:
        flash(request, f"Interview placed for {parsed:%d %b %Y, %H:%M}.", "success")
    return RedirectResponse(f"/applications/{application_id}", status_code=303)
