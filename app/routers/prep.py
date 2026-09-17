"""Epic G (lightweight) - per-application prep brief and rehearsal mode."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.models import Application, PracticeSession
from app.scoping import require_owned, require_prep_brief
from app.services import prep as prep_service
from app.web import flash, guard, partial, render

router = APIRouter(prefix="/prep")


@router.get("/{application_id}")
def view(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    brief = prep_service.latest_brief(session, application)
    practice = (
        sorted(brief.sessions, key=lambda s: s.created_at, reverse=True) if brief else []
    )
    return render(
        request,
        "prep.html",
        {
            "user": user,
            "application": application,
            "brief": brief,
            "practice": practice[0] if practice else None,
        },
    )


@router.post("/{application_id}/build")
@guard
async def build(
    application_id: int,
    request: Request,
    user: CurrentUser,
    with_research: bool = Form(True),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    prep_service.build_brief(session, application, with_research=with_research)
    flash(request, "Prep brief generated.", "success")
    return RedirectResponse(f"/prep/{application_id}", status_code=303)


@router.post("/session/{brief_id}/turn")
@guard
async def practice_turn(
    brief_id: int,
    request: Request,
    user: CurrentUser,
    message: str = Form(...),
    session: Session = Depends(get_session),
):
    brief = require_prep_brief(session, brief_id, user.id)

    practice = (
        sorted(brief.sessions, key=lambda s: s.created_at, reverse=True)[0]
        if brief.sessions
        else None
    )
    if practice is None:
        practice = PracticeSession(brief_id=brief.id, transcript=[])
        session.add(practice)
        session.commit()
        session.refresh(practice)

    transcript = list(practice.transcript or [])
    reply = prep_service.mock_turn(
        session, brief, transcript=transcript, user_message=message.strip()
    )
    transcript.append({"role": "user", "content": message.strip()})
    transcript.append({"role": "assistant", "content": reply})
    practice.transcript = transcript
    session.commit()

    return partial(request, "partials/practice_log.html", {"practice": practice})


@router.post("/session/{brief_id}/reset")
def practice_reset(
    brief_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    brief = require_prep_brief(session, brief_id, user.id)
    for practice in list(brief.sessions):
        session.delete(practice)
    session.commit()
    flash(request, "Rehearsal transcript cleared.", "info")
    return RedirectResponse(f"/prep/{brief.application_id}", status_code=303)
