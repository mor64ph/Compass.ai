"""Career Profile persistence and projection (PRD Epic A).

The profile is the hub of the data model: Epics B, D, E and G all read from it.
Three projections matter:

* `profile_to_dict` - what gets injected into prompts.
* `profile_evidence_text` - flat text for keyword matching, so the scorer can
  distinguish "you don't have this" from "you have this but it isn't on the
  résumé".
* `profile_to_markdown` - a plain single-column base résumé, which doubles as the
  starting point for tailored variants and is ATS-clean by construction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Aspiration,
    CareerProfile,
    Certification,
    Education,
    Experience,
    PortfolioProject,
    Skill,
)
from app.schemas import LLMCareerProfile
from app.services.textutil import format_range


def get_active_profile(session: Session, user_id: int) -> CareerProfile | None:
    """This user's profile. `user_id` is required rather than implicit: a missing
    tenant filter here would silently return someone else's career history."""
    return session.scalars(
        select(CareerProfile)
        .where(
            CareerProfile.user_id == user_id,
            CareerProfile.is_active.is_(True),
        )
        .order_by(CareerProfile.version.desc())
    ).first()


def get_or_create_profile(session: Session, user_id: int) -> CareerProfile:
    profile = get_active_profile(session, user_id)
    if profile is None:
        profile = CareerProfile(user_id=user_id)
        session.add(profile)
        session.commit()
        session.refresh(profile)
    return profile


def upsert_from_payload(
    session: Session, user_id: int, payload: LLMCareerProfile, *, merge: bool = True
) -> CareerProfile:
    """Write a compiled profile into the DB.

    Children are replaced wholesale rather than diffed. The LLM compile step is
    given the existing profile and told to carry unchanged data forward, so it
    already returns the full intended state - diffing here would just be a
    second, weaker merge with a different opinion.
    """
    profile = get_or_create_profile(session, user_id)

    def keep(new: str, old: str) -> str:
        return new if (new or not merge) else old

    profile.full_name = keep(payload.full_name, profile.full_name)
    profile.email = keep(payload.email, profile.email)
    profile.phone = keep(payload.phone, profile.phone)
    profile.location = keep(payload.location, profile.location)
    profile.headline = keep(payload.headline, profile.headline)
    profile.summary = keep(payload.summary, profile.summary)
    if payload.links or not merge:
        profile.links = [link.model_dump() for link in payload.links]

    if payload.skills or not merge:
        profile.skills = [
            Skill(
                name=s.name, category=s.category, proficiency=s.proficiency, years=s.years
            )
            for s in payload.skills
        ]
    if payload.certifications or not merge:
        profile.certifications = [
            Certification(
                name=c.name,
                issuer=c.issuer,
                credential_id=c.credential_id,
                issued_on=c.issued_on,
            )
            for c in payload.certifications
        ]
    if payload.experiences or not merge:
        profile.experiences = [
            Experience(
                title=e.title,
                company=e.company,
                location=e.location,
                start_date=e.start_date,
                end_date=e.end_date,
                is_current=e.is_current,
                responsibilities=list(e.responsibilities),
                achievements=list(e.achievements),
                scope_change_note=e.scope_change_note,
                sort_order=i,
            )
            for i, e in enumerate(payload.experiences)
        ]
    if payload.education or not merge:
        profile.education = [
            Education(
                degree=e.degree,
                institution=e.institution,
                field=e.field,
                end_year=e.end_year,
                notes=e.notes,
            )
            for e in payload.education
        ]
    if payload.projects or not merge:
        profile.projects = [
            PortfolioProject(
                name=p.name,
                description=p.description,
                tech=list(p.tech),
                link=p.link,
                status=p.status,
                highlight=p.highlight,
                sort_order=i,
            )
            for i, p in enumerate(payload.projects)
        ]

    asp = payload.aspiration
    if profile.aspiration is None:
        profile.aspiration = Aspiration()
    target = profile.aspiration
    if asp.target_titles or not merge:
        target.target_titles = list(asp.target_titles)
    if asp.target_industries or not merge:
        target.target_industries = list(asp.target_industries)
    if asp.target_companies or not merge:
        target.target_companies = list(asp.target_companies)
    if asp.locations or not merge:
        target.locations = list(asp.locations)
    if asp.non_negotiables or not merge:
        target.non_negotiables = list(asp.non_negotiables)
    if asp.remote_preference:
        target.remote_preference = asp.remote_preference
    if asp.comp_min:
        target.comp_min = asp.comp_min
    if asp.comp_max:
        target.comp_max = asp.comp_max
    if asp.comp_currency:
        target.comp_currency = asp.comp_currency

    profile.version += 1
    session.commit()
    session.refresh(profile)
    return profile


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------


def profile_to_dict(profile: CareerProfile | None) -> dict:
    if profile is None:
        return {}
    return {
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "links": profile.links or [],
        "headline": profile.headline,
        "summary": profile.summary,
        "skills": [
            {
                "name": s.name,
                "category": s.category,
                "proficiency": s.proficiency,
                "years": s.years,
            }
            for s in profile.skills
        ],
        "certifications": [
            {
                "name": c.name,
                "issuer": c.issuer,
                "issued_on": c.issued_on,
                "credential_id": c.credential_id,
            }
            for c in profile.certifications
        ],
        "experiences": [
            {
                "title": e.title,
                "company": e.company,
                "location": e.location,
                "start_date": e.start_date,
                "end_date": e.end_date,
                "is_current": e.is_current,
                "responsibilities": e.responsibilities or [],
                "achievements": e.achievements or [],
                "scope_change_note": e.scope_change_note,
            }
            for e in profile.experiences
        ],
        "education": [
            {
                "degree": e.degree,
                "institution": e.institution,
                "field": e.field,
                "end_year": e.end_year,
            }
            for e in profile.education
        ],
        "projects": [
            {
                "name": p.name,
                "description": p.description,
                "tech": p.tech or [],
                "link": p.link,
                "status": p.status,
                "highlight": p.highlight,
            }
            for p in profile.projects
        ],
        "aspiration": (
            {
                "target_titles": profile.aspiration.target_titles or [],
                "target_industries": profile.aspiration.target_industries or [],
                "target_companies": profile.aspiration.target_companies or [],
                "locations": profile.aspiration.locations or [],
                "remote_preference": profile.aspiration.remote_preference,
                "comp_min": profile.aspiration.comp_min,
                "comp_max": profile.aspiration.comp_max,
                "comp_currency": profile.aspiration.comp_currency,
                "non_negotiables": profile.aspiration.non_negotiables or [],
            }
            if profile.aspiration
            else {}
        ),
        "intake_notes": profile.intake_notes,
    }


def profile_evidence_text(profile: CareerProfile | None) -> str:
    """Everything in the profile as flat text, for keyword coverage."""
    if profile is None:
        return ""
    parts: list[str] = [profile.headline, profile.summary, profile.intake_notes]
    parts += [s.name for s in profile.skills]
    parts += [c.name for c in profile.certifications]
    for e in profile.experiences:
        parts += [e.title, e.company, e.scope_change_note]
        parts += list(e.responsibilities or [])
        parts += list(e.achievements or [])
    for p in profile.projects:
        parts += [p.name, p.description, p.highlight]
        parts += list(p.tech or [])
    return "\n".join(p for p in parts if p)


def profile_completeness(profile: CareerProfile | None) -> dict:
    """Drives the intake progress indicator. Weighted by what downstream epics
    actually need, not by field count."""
    if profile is None:
        return {"percent": 0, "missing": ["everything - run the intake"], "quantified_bullets": 0}

    checks: list[tuple[str, bool, int]] = [
        ("name and contact details", bool(profile.full_name and profile.email), 10),
        ("headline", bool(profile.headline), 5),
        ("summary", bool(profile.summary), 5),
        ("work history", len(profile.experiences) > 0, 20),
        ("quantified achievements", _quantified_count(profile) >= 3, 20),
        ("skills", len(profile.skills) >= 5, 10),
        ("portfolio projects", len(profile.projects) > 0, 10),
        ("education", len(profile.education) > 0, 5),
        (
            "target roles",
            bool(profile.aspiration and profile.aspiration.target_titles),
            10,
        ),
        (
            "compensation range",
            bool(profile.aspiration and profile.aspiration.comp_max),
            5,
        ),
    ]
    earned = sum(weight for _, ok, weight in checks if ok)
    total = sum(weight for _, _, weight in checks)
    return {
        "percent": round(earned / total * 100) if total else 0,
        "missing": [label for label, ok, _ in checks if not ok],
        "quantified_bullets": _quantified_count(profile),
    }


def _quantified_count(profile: CareerProfile) -> int:
    import re

    pattern = re.compile(r"\d")
    return sum(
        1
        for e in profile.experiences
        for bullet in (e.achievements or [])
        if pattern.search(bullet)
    )


def profile_to_markdown(profile: CareerProfile | None) -> str:
    """Single-column, standard-heading markdown résumé.

    This shape is ATS-clean on purpose: no tables, no columns, conventional
    section names, plain-text contact line.
    """
    if profile is None:
        return ""
    out: list[str] = []
    if profile.full_name:
        out.append(f"# {profile.full_name}")
    if profile.headline:
        out.append(profile.headline)

    contact = [profile.location, profile.phone, profile.email]
    contact += [link.get("url", "") for link in (profile.links or [])]
    contact_line = " | ".join(c for c in contact if c)
    if contact_line:
        out.append(contact_line)

    if profile.summary:
        out += ["", "## Summary", profile.summary]

    if profile.skills:
        out += ["", "## Skills"]
        by_category: dict[str, list[str]] = {}
        for skill in profile.skills:
            by_category.setdefault(skill.category, []).append(skill.name)
        for category, names in by_category.items():
            out.append(f"**{category.title()}:** " + ", ".join(names))

    if profile.experiences:
        out += ["", "## Experience"]
        for e in profile.experiences:
            dates = format_range(e.start_date, e.end_date, e.is_current)
            header = f"### {e.title}, {e.company}"
            if e.location:
                header += f" ({e.location})"
            out.append(header)
            if dates:
                out.append(dates)
            for bullet in list(e.achievements or []) + list(e.responsibilities or []):
                out.append(f"- {bullet}")
            out.append("")

    if profile.projects:
        out += ["## Projects"]
        for p in profile.projects:
            out.append(f"### {p.name}")
            line = p.highlight or p.description
            if line:
                out.append(line)
            if p.tech:
                out.append("Tech: " + ", ".join(p.tech))
            if p.link:
                out.append(p.link)
            out.append("")

    if profile.certifications:
        out += ["## Certifications"]
        for c in profile.certifications:
            out.append(f"- {c.name}" + (f" - {c.issuer}" if c.issuer else ""))
        out.append("")

    if profile.education:
        out += ["## Education"]
        for e in profile.education:
            line = f"- {e.degree}"
            if e.field:
                line += f", {e.field}"
            if e.institution:
                line += f" - {e.institution}"
            if e.end_year:
                line += f" ({e.end_year})"
            out.append(line)

    return "\n".join(out).strip()
