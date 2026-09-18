"""Résumé ingestion and variant management (PRD Epic B)."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import get_llm
from app.models import CareerProfile, ResumeVariant
from app.schemas import LLMCareerProfile
from app.services import ats_check, profile_service, text_extract
from app.services.textutil import clip

logger = logging.getLogger(__name__)

MAX_RESUME_CHARS = 20000


def variants(session: Session, user_id: int) -> list[ResumeVariant]:
    return list(
        session.scalars(
            select(ResumeVariant)
            .where(ResumeVariant.user_id == user_id)
            .order_by(ResumeVariant.is_master.desc(), ResumeVariant.created_at.desc())
        )
    )


def ingest(
    session: Session,
    user_id: int,
    *,
    content: bytes,
    filename: str,
    label: str = "",
    flavor: str = "base",
    make_master: bool = False,
) -> ResumeVariant:
    """Store the upload, extract text, and run the ATS check.

    No LLM call happens here - the parseability report is available immediately
    and works with no API key at all.
    """
    settings = get_settings()
    suffix = Path(filename).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    stored_path = settings.upload_dir / stored_name
    stored_path.write_bytes(content)

    doc = text_extract.extract(stored_path, original_filename=filename)
    report = ats_check.check(doc)

    profile = profile_service.get_active_profile(session, user_id)
    variant = ResumeVariant(
        user_id=user_id,
        profile_id=profile.id if profile else None,
        label=label.strip() or Path(filename).stem,
        flavor=flavor or "base",
        source_filename=filename,
        stored_path=str(stored_path),
        # Kept so a layout re-check still works after the local disk is wiped.
        stored_bytes=content,
        raw_text=doc.text,
        content_md=doc.text,
        ats_score=report.score,
        ats_report=report.as_dict(),
    )

    if make_master or not variants(session, user_id):
        for existing in variants(session, user_id):
            existing.is_master = False
        variant.is_master = True

    session.add(variant)
    session.commit()
    session.refresh(variant)
    return variant


def recheck(session: Session, variant: ResumeVariant) -> ats_check.ATSReport:
    """Re-run the ATS check against the original file wherever it can be found.

    See `_reread` - the layout rules need the real bytes, and on a host with no
    persistent disk the local copy will not be there.
    """
    doc = _reread(variant)
    report = ats_check.check(doc)
    variant.ats_score = report.score
    variant.ats_report = report.as_dict()
    session.commit()
    return report


def parse_into_profile(session: Session, variant: ResumeVariant) -> CareerProfile:
    """LLM-assisted structuring of the résumé into the Career Profile."""
    text = variant.raw_text or variant.content_md
    if not text.strip():
        raise ValueError(
            "There is no readable text in this résumé, so there is nothing to parse. "
            "Check the ATS report - this is usually a scanned PDF."
        )

    llm = get_llm()
    result = llm.structured(
        "resume_parse",
        user=f"# Résumé text\n{clip(text, MAX_RESUME_CHARS)}",
        output_model=LLMCareerProfile,
        category="parse",
    )
    payload: LLMCareerProfile = result.value

    # The variant was fetched through a scoped lookup, so its owner is
    # authoritative here.
    profile = profile_service.upsert_from_payload(session, variant.user_id, payload)
    profile.open_questions = list(payload.unresolved_questions)
    if variant.profile_id is None:
        variant.profile_id = profile.id
    session.commit()
    session.refresh(profile)
    return profile


def create_from_profile(
    session: Session, user_id: int, *, label: str, flavor: str = "base"
) -> ResumeVariant:
    """Generate a fresh single-column variant straight from the profile.

    Useful as a clean baseline: by construction it has no tables, no columns and
    conventional headings, so it should score near-perfectly on the ATS check.
    """
    profile = profile_service.get_active_profile(session, user_id)
    markdown = profile_service.profile_to_markdown(profile)
    doc = text_extract.ExtractedDoc(text=markdown, kind="txt", filename=f"{label}.md")
    report = ats_check.check(doc)

    variant = ResumeVariant(
        user_id=user_id,
        profile_id=profile.id if profile else None,
        label=label,
        flavor=flavor,
        source_filename="",
        raw_text=markdown,
        content_md=markdown,
        ats_score=report.score,
        ats_report=report.as_dict(),
    )
    session.add(variant)
    session.commit()
    session.refresh(variant)
    return variant


def _reread(variant: ResumeVariant) -> "text_extract.ExtractedDoc":
    """The original document, from the best source still available.

    Three tiers, because the ATS checker's layout rules - two-column detection,
    text boxes, tables, contact-in-header - can only run on the real file, and
    the local disk is not durable on a free host:

    1. **The local file**, if it is still there. Cheapest.
    2. **`stored_bytes` from the database**, written to a temporary file because
       pdfplumber and python-docx both want a path. This is the tier that makes
       a re-check survive a restart.
    3. **The extracted text**, where only the content rules apply. Reached when
       the variant was generated from the profile rather than uploaded, so there
       never was a file.
    """
    path = Path(variant.stored_path) if variant.stored_path else None
    if path and path.is_file():
        return text_extract.extract(path, original_filename=variant.source_filename)

    if variant.stored_bytes:
        suffix = Path(variant.source_filename or "").suffix.lower() or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(variant.stored_bytes)
            temp = Path(handle.name)
        try:
            doc = text_extract.extract(
                temp, original_filename=variant.source_filename
            )
        finally:
            temp.unlink(missing_ok=True)
        # Re-populate the disk cache so the next read is the cheap path again.
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(variant.stored_bytes)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                logger.info("Could not restore the local copy of %s: %s", path, exc)
        return doc

    return text_extract.ExtractedDoc(
        text=variant.content_md or variant.raw_text,
        kind="txt",
        filename=variant.source_filename or variant.label,
    )


def set_master(session: Session, variant: ResumeVariant) -> None:
    for existing in variants(session, variant.user_id):
        existing.is_master = existing.id == variant.id
    session.commit()


def delete(session: Session, variant: ResumeVariant) -> None:
    path = Path(variant.stored_path) if variant.stored_path else None
    session.delete(variant)
    session.commit()
    if path and path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not delete stored résumé %s: %s", path, exc)


def bullets_from_profile(profile: CareerProfile | None, limit: int = 12) -> list[str]:
    """Candidate bullets for the rewrite assistant, weakest-looking first."""
    if profile is None:
        return []
    weak_markers = re.compile(
        r"responsible for|worked on|involved in|helped with|assisted|participated", re.I
    )
    scored: list[tuple[int, str]] = []
    for experience in profile.experiences:
        for bullet in list(experience.responsibilities or []) + list(
            experience.achievements or []
        ):
            if not bullet.strip():
                continue
            score = 0
            if weak_markers.search(bullet):
                score -= 2
            if not re.search(r"\d", bullet):
                score -= 1
            scored.append((score, bullet))
    scored.sort(key=lambda pair: pair[0])
    return [bullet for _, bullet in scored[:limit]]
