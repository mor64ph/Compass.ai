"""Epic A - Career intake: the form half, the conversation half, and compile."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import CurrentUser
from app.models import (
    Aspiration,
    CareerProfile,
    Education,
    Experience,
    PortfolioProject,
    Skill,
)
from app.services import intake, profile_service
from app.web import flash, guard, partial, render

router = APIRouter(prefix="/profile")


@router.get("")
def view(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    profile = profile_service.get_active_profile(session, user.id)
    return render(
        request,
        "profile.html",
        {
            "user": user,
            "profile": profile,
            "completeness": profile_service.profile_completeness(profile),
            "messages": intake.transcript(session, user.id),
            "opening": intake.OPENING_MESSAGE,
        },
    )


# --------------------------------------------------------------------------
# Conversational intake
# --------------------------------------------------------------------------


@router.post("/intake/message")
@guard
async def intake_message(
    request: Request,
    user: CurrentUser,
    message: str = Form(...),
    session: Session = Depends(get_session),
):
    text = message.strip()
    if text:
        intake.chat_turn(session, user.id, text)
    return partial(
        request,
        "partials/intake_log.html",
        {"messages": intake.transcript(session, user.id)},
    )


@router.post("/intake/compile")
@guard
async def intake_compile(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    try:
        intake.compile_profile(session, user.id)
    except ValueError as exc:
        flash(request, str(exc), "warn")
    else:
        flash(request, "Profile compiled from the intake conversation.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/intake/reset")
def intake_reset(
    request: Request, user: CurrentUser, session: Session = Depends(get_session)
):
    intake.reset(session, user.id)
    flash(request, "Intake conversation cleared. The compiled profile is untouched.", "info")
    return RedirectResponse("/profile", status_code=303)


# --------------------------------------------------------------------------
# Form half
# --------------------------------------------------------------------------


@router.post("/basics")
def save_basics(
    request: Request,
    user: CurrentUser,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    headline: str = Form(""),
    summary: str = Form(""),
    links: str = Form(""),
    intake_notes: str = Form(""),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_or_create_profile(session, user.id)
    profile.full_name = full_name.strip()
    profile.email = email.strip()
    profile.phone = phone.strip()
    profile.location = location.strip()
    profile.headline = headline.strip()
    profile.summary = summary.strip()
    profile.intake_notes = intake_notes.strip()
    profile.links = [
        {"label": _link_label(url), "url": url}
        for url in (line.strip() for line in links.splitlines())
        if url
    ]
    session.commit()
    flash(request, "Basics saved.", "success")
    return RedirectResponse("/profile", status_code=303)


def _link_label(url: str) -> str:
    lowered = url.lower()
    for needle, label in (
        ("github", "GitHub"),
        ("gitlab", "GitLab"),
        ("linkedin", "LinkedIn"),
        ("medium", "Blog"),
        ("substack", "Blog"),
    ):
        if needle in lowered:
            return label
    return "Portfolio"


@router.post("/aspirations")
def save_aspirations(
    request: Request,
    user: CurrentUser,
    target_titles: str = Form(""),
    target_industries: str = Form(""),
    target_companies: str = Form(""),
    locations: str = Form(""),
    remote_preference: str = Form("hybrid"),
    comp_min: float = Form(0.0),
    comp_max: float = Form(0.0),
    comp_currency: str = Form("INR"),
    non_negotiables: str = Form(""),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_or_create_profile(session, user.id)
    if profile.aspiration is None:
        profile.aspiration = Aspiration()
    target = profile.aspiration
    target.target_titles = _lines(target_titles)
    target.target_industries = _lines(target_industries)
    target.target_companies = _lines(target_companies)
    target.locations = _lines(locations)
    target.remote_preference = remote_preference
    target.comp_min = comp_min
    target.comp_max = comp_max
    target.comp_currency = comp_currency.strip() or "INR"
    target.non_negotiables = _lines(non_negotiables)
    session.commit()
    flash(request, "Aspirations saved.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/experience")
def add_experience(
    request: Request,
    user: CurrentUser,
    title: str = Form(...),
    company: str = Form(...),
    location: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    is_current: bool = Form(False),
    responsibilities: str = Form(""),
    achievements: str = Form(""),
    scope_change_note: str = Form(""),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_or_create_profile(session, user.id)
    profile.experiences.append(
        Experience(
            title=title.strip(),
            company=company.strip(),
            location=location.strip(),
            start_date=start_date.strip(),
            end_date=end_date.strip(),
            is_current=is_current,
            responsibilities=_lines(responsibilities),
            achievements=_lines(achievements),
            scope_change_note=scope_change_note.strip(),
            sort_order=len(profile.experiences),
        )
    )
    session.commit()
    flash(request, f"Added {title} at {company}.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/project")
def add_project(
    request: Request,
    user: CurrentUser,
    name: str = Form(...),
    description: str = Form(""),
    highlight: str = Form(""),
    tech: str = Form(""),
    link: str = Form(""),
    status: str = Form("shipped"),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_or_create_profile(session, user.id)
    profile.projects.append(
        PortfolioProject(
            name=name.strip(),
            description=description.strip(),
            highlight=highlight.strip(),
            tech=[t.strip() for t in tech.split(",") if t.strip()],
            link=link.strip(),
            status=status,
            sort_order=len(profile.projects),
        )
    )
    session.commit()
    flash(request, f"Added project {name}.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/skills")
def replace_skills(
    request: Request,
    user: CurrentUser,
    skills: str = Form(""),
    session: Session = Depends(get_session),
):
    """One skill per line, optional `name | category | proficiency`."""
    profile = profile_service.get_or_create_profile(session, user.id)
    parsed: list[Skill] = []
    for line in _lines(skills):
        parts = [p.strip() for p in line.split("|")]
        parsed.append(
            Skill(
                name=parts[0],
                category=parts[1] if len(parts) > 1 and parts[1] else "hard",
                proficiency=parts[2] if len(parts) > 2 and parts[2] else "working",
            )
        )
    profile.skills = parsed
    session.commit()
    flash(request, f"{len(parsed)} skills saved.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/education")
def add_education(
    request: Request,
    user: CurrentUser,
    degree: str = Form(...),
    institution: str = Form(""),
    field: str = Form(""),
    end_year: str = Form(""),
    session: Session = Depends(get_session),
):
    profile = profile_service.get_or_create_profile(session, user.id)
    profile.education.append(
        Education(
            degree=degree.strip(),
            institution=institution.strip(),
            field=field.strip(),
            end_year=end_year.strip(),
        )
    )
    session.commit()
    flash(request, "Education added.", "success")
    return RedirectResponse("/profile", status_code=303)


@router.post("/delete/{kind}/{item_id}")
def delete_item(
    request: Request,
    kind: str,
    item_id: int,
    user: CurrentUser,
    session: Session = Depends(get_session),
):
    model = {
        "experience": Experience,
        "project": PortfolioProject,
        "education": Education,
        "skill": Skill,
    }.get(kind)
    if model is None:
        flash(request, f"Unknown item type {kind!r}.", "error")
        return RedirectResponse("/profile", status_code=303)

    # These children carry no user_id, so join to the profile and check the
    # owner - otherwise anyone could delete rows out of someone else's profile
    # by guessing an id.
    item = session.scalars(
        select(model)
        .join(CareerProfile, model.profile_id == CareerProfile.id)
        .where(model.id == item_id, CareerProfile.user_id == user.id)
    ).first()
    if item is not None:
        session.delete(item)
        session.commit()
        flash(request, f"{kind.title()} removed.", "info")
    return RedirectResponse("/profile", status_code=303)


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]
