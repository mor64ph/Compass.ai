"""Application lifecycle: creation, the scoring/tailoring pipeline, the stage
machine, and the funnel analytics behind the dashboard (PRD Epics D and E)."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Application,
    ApplicationEvent,
    JobPosting,
    ResumeVariant,
    SourceType,
    Stage,
    utcnow,
)
from app.schemas import JDInput
from app.services import gap_report as gap_report_service
from app.services import jd_match, profile_service, quality_gate, tailor

logger = logging.getLogger(__name__)

# Progression order. `closed` is deliberately absent: it is terminal, not a
# further step, so it must never win a "did we move forward?" comparison.
STAGE_RANK: dict[str, int] = {
    Stage.SAVED.value: 0,
    Stage.APPLIED.value: 1,
    Stage.SCREEN.value: 2,
    Stage.INTERVIEW.value: 3,
    Stage.OFFER.value: 4,
}

# How long a card may sit in a stage before it needs chasing.
STALENESS_DAYS: dict[str, int] = {
    Stage.SAVED.value: 7,
    Stage.APPLIED.value: 14,
    Stage.SCREEN.value: 7,
    Stage.INTERVIEW.value: 7,
    Stage.OFFER.value: 3,
}


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def log_event(session: Session, application: Application, **fields) -> ApplicationEvent:
    """Append a timeline entry and commit.

    The session runs with `expire_on_commit=False`, so an already-loaded
    `application.events` collection would otherwise keep serving a stale list -
    which is how funnel analytics quietly under-report a stage the application
    passed through. Expiring the relationship forces a reload on next access.
    """
    event = ApplicationEvent(application_id=application.id, **fields)
    session.add(event)
    session.commit()
    session.expire(application, ["events"])
    return event


def create_from_jd(session: Session, user_id: int, payload: JDInput) -> Application:
    """Create a posting + application from text the user pasted in.

    `source_type` is pinned to `user_pasted` here because that is what this path
    genuinely is: the user read a page in their own browser and handed over the
    text. The API path (Epic C) is the only other way in.
    """
    posting = JobPosting(
        user_id=user_id,
        source_type=SourceType.USER_PASTED.value,
        source_name="user",
        company=payload.company.strip(),
        title=payload.title.strip(),
        location=payload.location.strip(),
        url=payload.url.strip(),
        jd_text=payload.jd_text.strip(),
    )
    session.add(posting)
    session.flush()

    variant = _default_variant(session, user_id)
    application = Application(
        user_id=user_id,
        job_posting_id=posting.id,
        resume_variant_id=variant.id if variant else None,
        stage=Stage.SAVED.value,
    )
    session.add(application)
    session.flush()
    log_event(
        session,
        application,
        kind="note",
        summary="Application created from a pasted job description.",
        source="manual",
    )
    session.refresh(application)
    return application


def _default_variant(session: Session, user_id: int) -> ResumeVariant | None:
    stmt = (
        select(ResumeVariant)
        .where(ResumeVariant.user_id == user_id)
        .order_by(ResumeVariant.is_master.desc(), ResumeVariant.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def resume_text_for(session: Session, application: Application) -> str:
    """The text to score against: the attached variant, else the profile's own
    generated base résumé, so scoring works before any file is uploaded.

    The owner comes from `application.user_id` rather than a parameter - the
    application has already been fetched through a scoped lookup, so re-deriving
    it here removes any chance of the caller passing a different one.
    """
    variant = application.resume_variant or _default_variant(session, application.user_id)
    if variant and (variant.raw_text or variant.content_md):
        return variant.raw_text or variant.content_md
    return profile_service.profile_to_markdown(
        profile_service.get_active_profile(session, application.user_id)
    )


def run_match(session: Session, application: Application) -> jd_match.MatchReport:
    """Deterministic scoring. No LLM, no API key needed."""
    profile = profile_service.get_active_profile(session, application.user_id)
    posting = application.job_posting
    report = jd_match.score(
        resume_text=resume_text_for(session, application),
        jd_text=posting.jd_text,
        jd_title=posting.title,
        extra_evidence=profile_service.profile_evidence_text(profile),
    )
    application.match_score = report.composite
    application.match_report = report.as_dict()
    application.last_activity_at = utcnow()
    session.commit()
    return report


def run_gap_report(session: Session, application: Application) -> dict:
    profile = profile_service.get_active_profile(session, application.user_id)
    posting = application.job_posting
    report = run_match(session, application)
    result = gap_report_service.build(
        profile_dict=profile_service.profile_to_dict(profile),
        resume_text=resume_text_for(session, application),
        jd_text=posting.jd_text,
        jd_title=posting.title,
        jd_company=posting.company,
        match=report,
    )
    application.gap_report = result
    session.commit()
    return result


def run_tailoring(
    session: Session, application: Application, *, research: str = ""
) -> dict:
    """Generate the package, then immediately gate it.

    Gating is not a separate user action on purpose: the quality signal is
    supposed to be unavoidable, not something you can forget to run.
    """
    profile = profile_service.get_active_profile(session, application.user_id)
    profile_dict = profile_service.profile_to_dict(profile)
    posting = application.job_posting

    match = run_match(session, application)

    gaps = application.gap_report or {}
    if not gaps:
        gaps = run_gap_report(session, application)

    package = tailor.generate_package(
        profile_dict=profile_dict,
        resume_text=resume_text_for(session, application),
        jd_text=posting.jd_text,
        jd_title=posting.title,
        jd_company=posting.company,
        match=match,
        honest_gaps=gaps.get("honest_gaps", []),
        research=research,
    )

    application.tailored_resume_md = tailor.package_to_markdown(package, profile_dict)
    application.cover_letter_text = package["cover_letter"]
    application.talking_points = package["talking_points"]
    application.tailoring_meta = package
    application.last_activity_at = utcnow()
    session.commit()

    run_quality_gate(session, application)
    return package


def run_quality_gate(session: Session, application: Application) -> quality_gate.QualityReport:
    report = quality_gate.evaluate(session, application)
    application.quality_score = report.score
    application.quality_report = report.as_dict()
    application.quality_verdict = report.verdict
    if report.blocks_ready and not application.gate_override_reason:
        application.is_ready = False
    session.commit()
    return report


def mark_ready(session: Session, application: Application, *, override_reason: str = "") -> bool:
    """Flip the ready flag, unless the gate blocks it and no reason was given.

    An override is allowed - the user is an adult and sometimes a genuinely
    similar role deserves a genuinely similar letter - but it has to be written
    down, and it is stored on the record.
    """
    blocked = application.quality_verdict == "templated"
    if blocked and not override_reason.strip():
        return False
    application.is_ready = True
    application.last_activity_at = utcnow()
    if override_reason.strip():
        application.gate_override_reason = override_reason.strip()
        log_event(
            session,
            application,
            kind="note",
            summary=f"Quality gate overridden: {override_reason.strip()}",
            source="manual",
        )
    else:
        session.commit()
    return True


# --------------------------------------------------------------------------
# Stage machine
# --------------------------------------------------------------------------


def set_stage(
    session: Session,
    application: Application,
    stage: str,
    *,
    source: str = "manual",
    summary: str = "",
    external_id: str | None = None,
) -> bool:
    """Manual stage change. Always honoured - the user's word beats inference."""
    if stage not in {s.value for s in Stage} or stage == application.stage:
        return False
    previous = application.stage
    application.stage = stage
    application.last_activity_at = utcnow()
    if stage == Stage.APPLIED.value and application.applied_at is None:
        application.applied_at = utcnow()
    log_event(
        session,
        application,
        kind="stage_change",
        summary=summary or f"{previous} -> {stage}",
        detail={"from": previous, "to": stage},
        source=source,
        external_id=external_id,
    )
    return True


def advance_stage(
    session: Session,
    application: Application,
    stage: str,
    *,
    source: str,
    summary: str,
    external_id: str | None = None,
) -> bool:
    """Inferred stage change (Gmail). Forward-only, and never resurrects a
    closed application - a stray marketing email should not reopen a rejection."""
    current = application.stage
    if current == Stage.CLOSED.value:
        return False
    if stage == Stage.CLOSED.value:
        return set_stage(
            session, application, stage, source=source, summary=summary, external_id=external_id
        )
    if STAGE_RANK.get(stage, -1) <= STAGE_RANK.get(current, -1):
        return False
    return set_stage(
        session, application, stage, source=source, summary=summary, external_id=external_id
    )


# --------------------------------------------------------------------------
# Views / analytics
# --------------------------------------------------------------------------


def board(session: Session, user_id: int) -> dict[str, list[Application]]:
    stmt = (
        select(Application)
        .where(Application.user_id == user_id)
        .order_by(Application.last_activity_at.desc())
    )
    applications = list(session.scalars(stmt))
    grouped: dict[str, list[Application]] = {s.value: [] for s in Stage}
    for application in applications:
        grouped.setdefault(application.stage, []).append(application)
    return grouped


def stale_applications(session: Session, user_id: int) -> list[tuple[Application, int]]:
    """Cards past their stage's staleness threshold, worst first."""
    now = utcnow()
    out: list[tuple[Application, int]] = []
    for application in session.scalars(
        select(Application).where(Application.user_id == user_id)
    ):
        threshold = STALENESS_DAYS.get(application.stage)
        if threshold is None:
            continue
        last = application.last_activity_at or application.created_at
        days = (now - last).days
        if days >= threshold:
            out.append((application, days))
    return sorted(out, key=lambda pair: -pair[1])


def funnel(session: Session, user_id: int) -> dict:
    """Conversion rates plus the segmentation the PRD calls out: by source, and
    by tailoring-quality band. The quality-band cut is the one that tells you
    whether the gate's thesis is actually holding up in your own data."""
    applications = list(
        session.scalars(select(Application).where(Application.user_id == user_id))
    )
    total = len(applications)

    def count_reached(stage: Stage) -> int:
        rank = STAGE_RANK.get(stage.value, 99)
        return sum(
            1
            for a in applications
            if STAGE_RANK.get(a.stage, -1) >= rank
            or any(
                e.kind == "stage_change" and STAGE_RANK.get(e.detail.get("to", ""), -1) >= rank
                for e in a.events
            )
        )

    applied = count_reached(Stage.APPLIED)
    screen = count_reached(Stage.SCREEN)
    interview = count_reached(Stage.INTERVIEW)
    offer = count_reached(Stage.OFFER)

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator * 100, 1) if denominator else 0.0

    bands: dict[str, dict] = {}
    for label, low, high in (("high (75+)", 75, 101), ("mid (55-74)", 55, 75), ("low (<55)", 0, 55)):
        band = [a for a in applications if low <= a.quality_score < high and a.quality_score > 0]
        band_applied = [a for a in band if STAGE_RANK.get(a.stage, -1) >= 1]
        band_interview = [a for a in band if STAGE_RANK.get(a.stage, -1) >= 3]
        bands[label] = {
            "count": len(band),
            "applied": len(band_applied),
            "interviews": len(band_interview),
            "interview_rate": rate(len(band_interview), len(band_applied)),
        }

    by_source: dict[str, int] = {}
    for application in applications:
        key = application.job_posting.source_name if application.job_posting else "unknown"
        by_source[key] = by_source.get(key, 0) + 1

    return {
        "total": total,
        "applied": applied,
        "screen": screen,
        "interview": interview,
        "offer": offer,
        "response_rate": rate(screen, applied),
        "interview_rate": rate(interview, applied),
        "offer_rate": rate(offer, applied),
        "by_quality_band": bands,
        "by_source": by_source,
        "avg_match_score": round(
            sum(a.match_score for a in applications) / total, 1
        )
        if total
        else 0.0,
        "avg_quality_score": round(
            sum(a.quality_score for a in applications if a.quality_score > 0)
            / max(1, sum(1 for a in applications if a.quality_score > 0)),
            1,
        ),
    }


def recent_activity(
    session: Session, user_id: int, limit: int = 12
) -> list[ApplicationEvent]:
    """Events have no `user_id` of their own - join through the application."""
    stmt = (
        select(ApplicationEvent)
        .join(Application, ApplicationEvent.application_id == Application.id)
        .where(Application.user_id == user_id)
        .order_by(ApplicationEvent.occurred_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def counts_by_stage(session: Session, user_id: int) -> dict[str, int]:
    rows = session.execute(
        select(Application.stage, func.count(Application.id))
        .where(Application.user_id == user_id)
        .group_by(Application.stage)
    ).all()
    return {stage: count for stage, count in rows}


def due_followups(
    session: Session, user_id: int, within_days: int = 3
) -> list[Application]:
    """Applications whose staleness threshold falls due in the next few days."""
    now = utcnow()
    out: list[Application] = []
    for application in session.scalars(
        select(Application).where(Application.user_id == user_id)
    ):
        threshold = STALENESS_DAYS.get(application.stage)
        if threshold is None:
            continue
        last = application.last_activity_at or application.created_at
        due = last + timedelta(days=threshold)
        if now <= due <= now + timedelta(days=within_days):
            out.append(application)
    return out
