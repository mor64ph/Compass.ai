"""Epic B - résumé upload, ATS report, variants, bullet rewriting."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.deps import CurrentUser
from app.models import ResumeVariant
from app.scoping import require_owned
from app.services import export, profile_service, resume_service, tailor
from app.web import flash, guard, partial, render

router = APIRouter(prefix="/resumes")

# Only what text_extract can actually read. Checked server-side: the `accept`
# attribute on the file input is a convenience for the file picker, not a
# control - a hand-rolled POST ignores it entirely.
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
CHUNK_BYTES = 64 * 1024


async def _read_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read up to `limit` bytes, or None if the upload is larger.

    Streamed in chunks rather than `await file.read()`: reading first and
    checking the length afterwards means a 2 GB POST is fully resident in memory
    before it can be rejected, which is a trivial way to knock the process over.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("")
def index(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    profile = profile_service.get_active_profile(session, user.id)
    return render(
        request,
        "resumes.html",
        {
            "user": user,
            "variants": resume_service.variants(session, user.id),
            "profile": profile,
            "rewrite_candidates": resume_service.bullets_from_profile(profile),
        },
    )


@router.post("/upload")
async def upload(
    request: Request,
    user: CurrentUser,
    file: UploadFile = File(...),
    label: str = Form(""),
    flavor: str = Form("base"),
    make_master: bool = Form(False),
    session: Session = Depends(get_session),
):
    limit_mb = get_settings().compass_max_upload_mb
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        flash(
            request,
            f"Compass reads {', '.join(sorted(ALLOWED_SUFFIXES))} - not {suffix or 'that'}. "
            "Export your résumé as a PDF or DOCX first.",
            "error",
        )
        return RedirectResponse("/resumes", status_code=303)

    content = await _read_capped(file, limit_mb * 1024 * 1024)
    if content is None:
        flash(
            request,
            f"Files over {limit_mb} MB are rejected - a résumé should be far smaller.",
            "error",
        )
        return RedirectResponse("/resumes", status_code=303)
    if not content:
        flash(request, "That file was empty.", "error")
        return RedirectResponse("/resumes", status_code=303)

    variant = resume_service.ingest(
        session,
        user.id,
        content=content,
        filename=file.filename or "resume",
        label=label,
        flavor=flavor,
        make_master=make_master,
    )
    flash(
        request,
        f"Uploaded. ATS parseability score: {variant.ats_score:.0f}/100 "
        f"({variant.ats_report.get('grade', '')}).",
        "success" if variant.ats_score >= 75 else "warn",
    )
    return RedirectResponse(f"/resumes/{variant.id}", status_code=303)


@router.post("/generate")
def generate(
    request: Request,
    user: CurrentUser,
    label: str = Form("Generated from profile"),
    flavor: str = Form("base"),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_active_profile(session, user.id)
    if profile is None or not profile.experiences:
        flash(request, "Build the career profile first - there is nothing to generate from.", "warn")
        return RedirectResponse("/profile", status_code=303)
    variant = resume_service.create_from_profile(
        session, user.id, label=label, flavor=flavor
    )
    flash(
        request,
        f"Generated a clean single-column variant (ATS {variant.ats_score:.0f}/100).",
        "success",
    )
    return RedirectResponse(f"/resumes/{variant.id}", status_code=303)


@router.get("/{variant_id}")
def detail(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    return render(request, "resume_detail.html", {"user": user, "variant": variant})


@router.post("/{variant_id}/recheck")
def recheck(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    report = resume_service.recheck(session, variant)
    return partial(request, "partials/ats_report.html", {"report": report.as_dict()})


@router.post("/{variant_id}/parse")
@guard
def parse(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    try:
        resume_service.parse_into_profile(session, variant)
    except ValueError as exc:
        flash(request, str(exc), "warn")
    else:
        flash(
            request,
            "Résumé parsed into the career profile. Review it, then run the intake "
            "conversation to fill the gaps.",
            "success",
        )
    return RedirectResponse("/profile", status_code=303)


@router.post("/{variant_id}/content")
def save_content(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    content_md: str = Form(""),
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    variant.content_md = content_md
    session.commit()
    resume_service.recheck(session, variant)
    flash(request, "Content saved and re-checked.", "success")
    return RedirectResponse(f"/resumes/{variant_id}", status_code=303)


@router.post("/{variant_id}/master")
def make_master(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    resume_service.set_master(session, variant)
    flash(request, f"'{variant.label}' is now the master résumé.", "success")
    return RedirectResponse("/resumes", status_code=303)


@router.post("/{variant_id}/delete")
def delete(
    variant_id: int,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    label = variant.label
    resume_service.delete(session, variant)
    flash(request, f"Deleted '{label}'.", "info")
    return RedirectResponse("/resumes", status_code=303)


@router.get("/{variant_id}/export.{fmt}")
def export_variant(
    variant_id: int,
    fmt: str,
    request: Request,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    variant = require_owned(session, ResumeVariant, variant_id, user.id)
    markdown = variant.content_md or variant.raw_text
    filename = export.safe_filename("compass", variant.label)
    if fmt == "docx":
        path = export.to_docx(markdown, filename)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "pdf":
        path = export.to_pdf(markdown, filename)
        media = "application/pdf"
    else:
        flash(request, "Export format must be docx or pdf.", "error")
        return RedirectResponse(f"/resumes/{variant_id}", status_code=303)
    return FileResponse(str(path), media_type=media, filename=path.name)


@router.post("/rewrite")
@guard
def rewrite(
    request: Request,
    user: CurrentUser,
    bullets: str = Form(""),
    flavor: str = Form("general"),
    session: Session = Depends(get_session),
):
    lines = [line.strip() for line in bullets.splitlines() if line.strip()]
    if not lines:
        return partial(request, "partials/rewrites.html", {"rewrites": []})
    profile = profile_service.get_active_profile(session, user.id)
    rewrites = tailor.rewrite_bullets(
        profile_dict=profile_service.profile_to_dict(profile),
        bullets=lines,
        flavor=flavor,
    )
    return partial(request, "partials/rewrites.html", {"rewrites": rewrites})
