"""Job discovery - ATS boards in, ranked shortlist out, one click into the pipeline.

The last part is the point. A matcher that hands you a ranked list and stops has
answered the easy half; `adopt` turns a posting into a real `Application` with the
JD already in place, so the gap report, tailoring, quality gate and export all
work on it immediately.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.models import Application, DiscoveredJob, JobSource
from app.scoping import require_owned
from app.services import application_service
from app.services.discovery import ats, ingest, rank
from app.web import flash, partial, render

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discover")

SORTS = {
    "closeable": DiscoveredJob.closeable_score.desc(),
    "current": DiscoveredJob.match_score.desc(),
    "newest": DiscoveredJob.posted_at.desc(),
}
PAGE_SIZE = 40


@router.get("")
def index(
    request: Request,
    user: CurrentUser,
    sort: str = "closeable",
    q: str = "",
    remote: str = "",
    source_id: int = 0,
    show: str = "open",
    session: Session = Depends(get_session),
):
    query = select(DiscoveredJob).where(DiscoveredJob.user_id == user.id)

    if show == "open":
        query = query.where(
            DiscoveredJob.is_open == True,  # noqa: E712
            DiscoveredJob.dismissed == False,  # noqa: E712
        )
    elif show == "dismissed":
        query = query.where(DiscoveredJob.dismissed == True)  # noqa: E712
    elif show == "adopted":
        query = query.where(DiscoveredJob.adopted_application_id.is_not(None))

    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            DiscoveredJob.title.ilike(like) | DiscoveredJob.company.ilike(like)
        )
    if remote == "yes":
        query = query.where(DiscoveredJob.is_remote == True)  # noqa: E712
    if source_id:
        query = query.where(DiscoveredJob.source_id == source_id)

    total = session.scalar(
        select(func.count()).select_from(query.subquery())
    ) or 0
    jobs = list(
        session.scalars(
            query.order_by(SORTS.get(sort, SORTS["closeable"])).limit(PAGE_SIZE)
        )
    )

    sources = list(
        session.scalars(
            select(JobSource)
            .where(JobSource.user_id == user.id)
            .order_by(JobSource.company_name.asc())
        )
    )
    unscored = session.scalar(
        select(func.count(DiscoveredJob.id)).where(
            DiscoveredJob.user_id == user.id,
            DiscoveredJob.is_open == True,  # noqa: E712
            DiscoveredJob.is_deep_scored == False,  # noqa: E712
            DiscoveredJob.dismissed == False,  # noqa: E712
        )
    ) or 0

    return render(
        request,
        "discover.html",
        {
            "user": user,
            "jobs": jobs,
            "total": total,
            "shown": len(jobs),
            "sources": sources,
            "unscored": unscored,
            "providers": ats.PROVIDERS,
            "sort": sort,
            "q": q,
            "remote": remote,
            "source_id": source_id,
            "show": show,
            "deep_limit": rank.DEEP_LIMIT,
        },
    )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@router.post("/sources")
def add_source(
    request: Request,
    user: CurrentUser,
    ats_name: str = Form(...),
    board_token: str = Form(...),
    company_name: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        source = ingest.add_source(
            session,
            user.id,
            ats_name=ats_name,
            token=board_token,
            company_name=company_name,
        )
    except ats.ATSError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/discover", status_code=303)

    count = session.scalar(
        select(func.count(DiscoveredJob.id)).where(
            DiscoveredJob.source_id == source.id
        )
    ) or 0
    flash(
        request,
        f"Added {source.label} — {count} open role(s) pulled. "
        "Rank them to see which fit.",
        "success",
    )
    return RedirectResponse("/discover", status_code=303)


@router.post("/sources/{source_id}/delete")
def delete_source(
    source_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    source = require_owned(session, JobSource, source_id, user.id)
    label = source.label
    ingest.delete_source(session, source)
    flash(request, f"Removed {label} and its postings.", "info")
    return RedirectResponse("/discover", status_code=303)


@router.post("/refresh")
def refresh(
    request: Request,
    user: CurrentUser,
    source_id: int = Form(0),
    session: Session = Depends(get_session),
):
    result = ingest.refresh(
        session, user.id, source_id=source_id or None
    )
    if not result.sources_polled:
        flash(request, "No active boards to refresh. Add one first.", "warn")
    else:
        flash(
            request,
            f"Polled {result.sources_polled} board(s): {result.added} new, "
            f"{result.updated} changed, {result.closed} closed.",
            "success" if result.ok else "warn",
        )
    for error in result.errors[:4]:
        flash(request, error, "error")
    return RedirectResponse("/discover", status_code=303)


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


@router.post("/rank")
def rank_jobs(
    request: Request,
    user: CurrentUser,
    rescore_all: bool = Form(False),
    session: Session = Depends(get_session),
):
    result = rank.rank(session, user.id, rescore_all=rescore_all)
    if not result.considered:
        flash(request, "Nothing to rank yet — add a board and refresh.", "warn")
    elif not result.deep_scored:
        flash(
            request,
            f"All {result.considered} open role(s) are already scored. "
            "Use 'Re-score everything' after editing your profile.",
            "info",
        )
    else:
        flash(
            request,
            f"Scored {result.deep_scored} of {result.considered} role(s) "
            f"via {result.backend or 'lexical fallback'}. "
            "Sorted by fit-after-tailoring.",
            "success",
        )
    return RedirectResponse("/discover", status_code=303)


@router.post("/{job_id}/dismiss")
def dismiss(
    job_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    job = require_owned(session, DiscoveredJob, job_id, user.id)
    job.dismissed = not job.dismissed
    session.commit()
    return partial(request, "partials/discovered_row.html", {"job": job})


# --------------------------------------------------------------------------
# Into the pipeline
# --------------------------------------------------------------------------


@router.post("/{job_id}/adopt")
def adopt(
    job_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    """Turn a posting into a real application, scored and ready to tailor.

    Reuses `application_service.create` rather than writing rows directly, so an
    adopted job goes through exactly the same path - and the same deterministic
    scoring - as a hand-pasted JD.
    """
    job = require_owned(session, DiscoveredJob, job_id, user.id)

    if job.adopted_application_id:
        existing = session.get(Application, job.adopted_application_id)
        if existing is not None:
            flash(request, "Already in your pipeline.", "info")
            return RedirectResponse(
                f"/applications/{existing.id}", status_code=303
            )

    if not job.jd_text.strip():
        flash(
            request,
            "That posting arrived without a description, so there is nothing to "
            "tailor against. Open it and paste the text in manually.",
            "warn",
        )
        return RedirectResponse("/discover", status_code=303)

    application = application_service.create_from_api(
        session,
        user.id,
        company=job.company or job.source.label,
        title=job.title,
        location=job.location,
        url=job.url,
        jd_text=job.jd_text,
        source_name=job.source.ats,
        external_id=job.external_id,
    )
    # Score it immediately, exactly as the pasted path does, so the detail page
    # opens with a match report rather than an empty one.
    application_service.run_match(session, application)
    job.adopted_application_id = application.id
    session.commit()

    flash(
        request,
        f"{job.title} added and scored. Next: generate the gap report.",
        "success",
    )
    return RedirectResponse(f"/applications/{application.id}", status_code=303)
