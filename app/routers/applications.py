"""Epic D - per-application scoring, tailoring, the quality gate, and export.

Note what is absent: there is no submit endpoint. Export hands the user a file;
the last click is theirs, in the employer's own form. See docs/CONSTRAINTS.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.models import Application, ResumeVariant
from app.schemas import JDInput
from app.scoping import require_owned
from app.services import application_service, export, prep, profile_service, resume_service
from app.web import flash, guard, partial, render

router = APIRouter(prefix="/applications")


@router.get("")
def index(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    applications = list(
        session.scalars(
            select(Application)
            .where(Application.user_id == user.id)
            .order_by(Application.last_activity_at.desc())
        )
    )
    return render(
        request,
        "applications.html",
        {
            "user": user,
            "applications": applications,
            "variants": resume_service.variants(session, user.id),
        },
    )


@router.post("")
def create(
    request: Request,
    user: CurrentUser,
    company: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    jd_text: str = Form(...),
    session: Session = Depends(get_session),
):
    if len(jd_text.strip()) < 80:
        flash(
            request,
            "That job description looks too short to score. Paste the full text of the posting.",
            "warn",
        )
        return RedirectResponse("/applications", status_code=303)

    application = application_service.create_from_jd(
        session,
        user.id,
        JDInput(company=company, title=title, location=location, url=url, jd_text=jd_text),
    )
    # Deterministic scoring runs immediately - it is free and needs no API key.
    application_service.run_match(session, application)
    flash(request, "Job added and scored.", "success")
    return RedirectResponse(f"/applications/{application.id}", status_code=303)


@router.get("/{application_id}")
def detail(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    return render(
        request,
        "application_detail.html",
        {
            "user": user,
            "application": application,
            "profile": profile_service.get_active_profile(session, user.id),
            "variants": resume_service.variants(session, user.id),
            "brief": prep.latest_brief(session, application),
        },
    )


# --------------------------------------------------------------------------
# Pipeline steps
# --------------------------------------------------------------------------


@router.post("/{application_id}/score")
def score(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    report = application_service.run_match(session, application)
    return partial(
        request,
        "partials/match_report.html",
        {"application": application, "match": report.as_dict()},
    )


@router.post("/{application_id}/gaps")
@guard
def gaps(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    report = application_service.run_gap_report(session, application)
    return partial(
        request, "partials/gap_report.html", {"application": application, "gap": report}
    )


@router.post("/{application_id}/tailor")
@guard
def tailor_route(
    application_id: int,
    request: Request,
    user: CurrentUser,
    with_research: bool = Form(False),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    posting = application.job_posting

    research = ""
    if with_research and posting and posting.company:
        try:
            research, citations = prep.research_company(
                company=posting.company, title=posting.title, location=posting.location
            )
            meta = dict(application.tailoring_meta or {})
            meta["research_citations"] = citations
            application.tailoring_meta = meta
            session.commit()
        except Exception as exc:  # research is optional - never block tailoring
            flash(request, f"Company research failed, tailoring without it: {exc}", "warn")

    application_service.run_tailoring(session, application, research=research)
    session.refresh(application)
    return partial(request, "partials/tailored_package.html", {"application": application})


@router.post("/{application_id}/gate")
def gate(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    application_service.run_quality_gate(session, application)
    session.refresh(application)
    return partial(request, "partials/quality_report.html", {"application": application})


@router.post("/{application_id}/ready")
def ready(
    application_id: int,
    request: Request,
    user: CurrentUser,
    override_reason: str = Form(""),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    ok = application_service.mark_ready(session, application, override_reason=override_reason)
    if not ok:
        flash(
            request,
            "The quality gate flagged this application as templated. Regenerate it, "
            "or record a reason for overriding the gate.",
            "warn",
        )
    else:
        flash(request, "Marked ready to submit. The submit click is yours.", "success")
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------


@router.post("/{application_id}/edit")
def edit(
    application_id: int,
    request: Request,
    user: CurrentUser,
    tailored_resume_md: str = Form(""),
    cover_letter_text: str = Form(""),
    notes: str = Form(""),
    resume_variant_id: str = Form(""),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    application.tailored_resume_md = tailored_resume_md
    application.cover_letter_text = cover_letter_text
    application.notes = notes
    if resume_variant_id.strip().isdigit():
        # Verify the variant is also this user's before attaching it.
        variant = require_owned(
            session, ResumeVariant, int(resume_variant_id), user.id
        )
        application.resume_variant_id = variant.id
    session.commit()
    # A human edit changes the artefact the gate scored, so re-score it.
    application_service.run_quality_gate(session, application)
    flash(request, "Saved and re-gated.", "success")
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/stage")
def stage(
    application_id: int,
    request: Request,
    user: CurrentUser,
    stage: str = Form(...),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    application_service.set_stage(session, application, stage)
    flash(request, f"Moved to {stage}.", "success")
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/note")
def note(
    application_id: int,
    request: Request,
    user: CurrentUser,
    summary: str = Form(...),
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    if summary.strip():
        application_service.log_event(
            session, application, kind="note", summary=summary.strip(), source="manual"
        )
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


@router.post("/{application_id}/delete")
def delete(
    application_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    session.delete(application)
    session.commit()
    flash(request, "Application deleted.", "info")
    return RedirectResponse("/applications", status_code=303)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


@router.get("/{application_id}/export/{kind}.{fmt}")
def export_document(
    application_id: int,
    kind: str,
    fmt: str,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    application = require_owned(session, Application, application_id, user.id)
    posting = application.job_posting
    profile_dict = profile_service.profile_to_dict(
        profile_service.get_active_profile(session, user.id)
    )

    if kind == "resume":
        markdown = application.tailored_resume_md
        if not markdown:
            flash(request, "Generate the tailored package first.", "warn")
            return RedirectResponse(f"/applications/{application_id}", status_code=303)
        name = export.safe_filename(
            profile_dict.get("full_name", "resume"),
            posting.company if posting else "",
            posting.title if posting else "",
        )
    elif kind == "cover-letter":
        if not application.cover_letter_text:
            flash(request, "Generate the tailored package first.", "warn")
            return RedirectResponse(f"/applications/{application_id}", status_code=303)
        markdown = export.cover_letter_markdown(
            body=application.cover_letter_text,
            profile=profile_dict,
            company=posting.company if posting else "",
            title=posting.title if posting else "",
        )
        name = export.safe_filename(
            profile_dict.get("full_name", "cover"),
            "cover_letter",
            posting.company if posting else "",
        )
    else:
        raise HTTPException(status_code=404, detail="Unknown document")

    if fmt == "docx":
        path = export.to_docx(markdown, name)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "pdf":
        path = export.to_pdf(markdown, name)
        media = "application/pdf"
    else:
        raise HTTPException(status_code=404, detail="Unknown format")

    return FileResponse(str(path), media_type=media, filename=path.name)
