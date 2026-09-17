"""Pull postings from a user's ATS boards into `DiscoveredJob` rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DiscoveredJob, JobSource, utcnow
from app.services.discovery import ats

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    sources_polled: int = 0
    added: int = 0
    updated: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "sources_polled": self.sources_polled,
            "added": self.added,
            "updated": self.updated,
            "closed": self.closed,
            "errors": self.errors,
        }


def add_source(
    session: Session, user_id: int, *, ats_name: str, token: str, company_name: str = ""
) -> JobSource:
    """Register a board after proving it actually resolves.

    The probe is the point: a typo'd slug saved silently would sit in the list
    looking healthy and simply never return anything.
    """
    ats_name = (ats_name or "").strip().lower()
    token = (token or "").strip().strip("/")
    if ats_name not in ats.FETCHERS:
        raise ats.ATSError(
            f"Unknown ATS {ats_name!r}. Supported: {', '.join(sorted(ats.FETCHERS))}."
        )
    if not token:
        raise ats.ATSError("A board token is required.")

    existing = session.scalars(
        select(JobSource).where(
            JobSource.user_id == user_id,
            JobSource.ats == ats_name,
            JobSource.board_token == token,
        )
    ).first()
    if existing is not None:
        raise ats.ATSError(f"{ats_name}/{token} is already on your list.")

    postings = ats.fetch(ats_name, token)  # raises ATSError on a bad token

    source = JobSource(
        user_id=user_id,
        ats=ats_name,
        board_token=token,
        company_name=(
            company_name.strip()
            or next((p.company for p in postings if p.company), "")
            or token
        ),
    )
    session.add(source)
    session.commit()
    session.refresh(source)

    _absorb(session, source, postings, RefreshResult())
    session.commit()
    return source


def refresh(
    session: Session, user_id: int, *, source_id: int | None = None
) -> RefreshResult:
    """Re-poll one board or all of a user's active boards.

    One board failing does not abandon the rest: a company that renamed its slug
    should cost you that company, not the whole refresh.
    """
    query = select(JobSource).where(
        JobSource.user_id == user_id, JobSource.is_active == True  # noqa: E712
    )
    if source_id is not None:
        query = query.where(JobSource.id == source_id)

    result = RefreshResult()
    for source in session.scalars(query):
        result.sources_polled += 1
        try:
            postings = ats.fetch(source.ats, source.board_token)
        except ats.ATSError as exc:
            source.last_error = str(exc)
            source.last_fetched_at = utcnow()
            result.errors.append(f"{source.label}: {exc}")
            session.commit()
            continue

        source.last_error = ""
        source.last_fetched_at = utcnow()
        source.last_job_count = len(postings)
        _absorb(session, source, postings, result)
        session.commit()

    return result


def _absorb(
    session: Session,
    source: JobSource,
    postings: list[ats.Posting],
    result: RefreshResult,
) -> None:
    """Upsert this poll's postings and close the ones that vanished."""
    existing = {
        job.external_id: job
        for job in session.scalars(
            select(DiscoveredJob).where(DiscoveredJob.source_id == source.id)
        )
    }
    seen: set[str] = set()

    for posting in postings:
        if not posting.external_id or not posting.title:
            continue
        seen.add(posting.external_id)
        job = existing.get(posting.external_id)

        if job is None:
            session.add(_build(source, posting))
            result.added += 1
            continue

        # Re-score only when the text the score depends on actually moved.
        # Employers re-publish postings constantly with no substantive edit, and
        # invalidating the score each time would mean re-embedding the whole
        # board on every refresh.
        content_changed = (
            job.jd_text != posting.jd_text
            or job.title != posting.title
            or job.location != posting.location
        )
        job.title = posting.title
        job.location = posting.location
        job.department = posting.department
        job.employment_type = posting.employment_type
        job.is_remote = posting.is_remote
        job.url = posting.url or job.url
        job.jd_text = posting.jd_text
        job.last_seen_at = utcnow()
        job.is_open = True
        if content_changed:
            job.scored_at = None
            job.is_deep_scored = False
            result.updated += 1

    for external_id, job in existing.items():
        if external_id not in seen and job.is_open:
            # Marked closed rather than deleted: a role you already adopted
            # should not disappear from your own history because the employer
            # filled it.
            job.is_open = False
            result.closed += 1


def _build(source: JobSource, posting: ats.Posting) -> DiscoveredJob:
    return DiscoveredJob(
        user_id=source.user_id,
        source_id=source.id,
        external_id=posting.external_id,
        company=posting.company or source.company_name,
        title=posting.title,
        location=posting.location,
        department=posting.department,
        employment_type=posting.employment_type,
        is_remote=posting.is_remote,
        url=posting.url,
        jd_text=posting.jd_text,
        posted_at=posting.posted_at,
    )


def delete_source(session: Session, source: JobSource) -> None:
    session.delete(source)  # cascades to its DiscoveredJob rows
    session.commit()
