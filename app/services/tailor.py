"""Tailored application generator (PRD Epic D).

Produces the package; it does not submit anything. The output lands in the UI
for the user to read, edit, export and send themselves - see
docs/CONSTRAINTS.md for why that boundary is load-bearing rather than
incidental.
"""

from __future__ import annotations

import logging

from app.llm import get_llm
from app.schemas import LLMBulletRewrites, LLMTailoredPackage
from app.services.jd_match import MatchReport
from app.services.textutil import clip, format_range

logger = logging.getLogger(__name__)

MAX_JD_CHARS = 14000
MAX_RESEARCH_CHARS = 6000


def generate_package(
    *,
    profile_dict: dict,
    resume_text: str,
    jd_text: str,
    jd_title: str,
    jd_company: str,
    match: MatchReport,
    honest_gaps: list[str],
    research: str = "",
) -> dict:
    llm = get_llm()

    match_summary = {
        "composite": match.composite,
        "keyword_score": match.keyword_score,
        "semantic_score": match.semantic_score,
        "terms_to_work_in": match.top_missing(18),
        "terms_already_covered": [
            h.term for h in sorted(match.found, key=lambda h: -h.weight)[:15]
        ],
        "on_profile_but_missing_from_resume": [
            h.term for h in match.found if "profile only" in h.matched_as
        ],
        "seniority": match.seniority,
    }

    user = (
        f"# Role\n{jd_title} at {jd_company}\n\n"
        f"# Current résumé text\n{clip(resume_text, 14000)}\n\n"
        f"# Job description\n{clip(jd_text, MAX_JD_CHARS)}"
    )

    result = llm.structured(
        "tailor_package",
        user=user,
        output_model=LLMTailoredPackage,
        prompt_vars={
            "profile_json": profile_dict,
            "honest_gaps": honest_gaps or ["(none identified)"],
            "match_json": match_summary,
            "research": clip(research, MAX_RESEARCH_CHARS) if research else "(no research run)",
        },
        category="tailor",
    )
    package: LLMTailoredPackage = result.value

    return {
        "headline": package.headline,
        "summary": package.summary,
        "bullets": [b.model_dump() for b in package.bullets],
        "skills_to_surface": list(package.skills_to_surface),
        "cover_letter": package.cover_letter,
        "talking_points": [t.model_dump() for t in package.talking_points],
        "keywords_incorporated": list(package.keywords_incorporated),
        "claims_avoided": list(package.claims_avoided),
        "company_specific_details_used": list(package.company_specific_details_used),
        "cached": result.cached,
    }


def package_to_markdown(package: dict, profile_dict: dict) -> str:
    """Render the tailored package as a single-column markdown résumé.

    Bullets are grouped back under their source company and the remaining
    profile sections are appended verbatim, so the export is a complete
    document rather than a fragment.
    """
    out: list[str] = []
    name = profile_dict.get("full_name") or ""
    if name:
        out.append(f"# {name}")
    if package.get("headline"):
        out.append(package["headline"])

    contact = [
        profile_dict.get("location", ""),
        profile_dict.get("phone", ""),
        profile_dict.get("email", ""),
    ]
    contact += [link.get("url", "") for link in profile_dict.get("links", [])]
    line = " | ".join(c for c in contact if c)
    if line:
        out.append(line)

    if package.get("summary"):
        out += ["", "## Summary", package["summary"]]

    if package.get("skills_to_surface"):
        out += ["", "## Skills", ", ".join(package["skills_to_surface"])]

    experiences = profile_dict.get("experiences", [])
    bullets_by_company: dict[str, list[str]] = {}
    for bullet in package.get("bullets", []):
        bullets_by_company.setdefault(bullet.get("company", ""), []).append(
            bullet.get("tailored", "")
        )

    if experiences:
        out += ["", "## Experience"]
        for exp in experiences:
            header = f"### {exp.get('title', '')}, {exp.get('company', '')}"
            if exp.get("location"):
                header += f" ({exp['location']})"
            out.append(header)
            dates = format_range(
                exp.get("start_date", ""),
                exp.get("end_date", ""),
                bool(exp.get("is_current")),
            )
            if dates:
                out.append(dates)
            company_bullets = bullets_by_company.get(exp.get("company", ""), [])
            if not company_bullets:
                # No tailored bullets for this role - fall back to the profile's
                # own, so an older job never renders as an empty heading.
                company_bullets = list(exp.get("achievements", [])) + list(
                    exp.get("responsibilities", [])
                )
            for bullet in company_bullets:
                if bullet:
                    out.append(f"- {bullet}")
            out.append("")

    projects = profile_dict.get("projects", [])
    if projects:
        out.append("## Projects")
        for project in projects:
            out.append(f"### {project.get('name', '')}")
            detail = project.get("highlight") or project.get("description", "")
            if detail:
                out.append(detail)
            if project.get("tech"):
                out.append("Tech: " + ", ".join(project["tech"]))
            if project.get("link"):
                out.append(project["link"])
            out.append("")

    certifications = profile_dict.get("certifications", [])
    if certifications:
        out.append("## Certifications")
        for cert in certifications:
            suffix = f" - {cert['issuer']}" if cert.get("issuer") else ""
            out.append(f"- {cert.get('name', '')}{suffix}")
        out.append("")

    education = profile_dict.get("education", [])
    if education:
        out.append("## Education")
        for edu in education:
            bits = [edu.get("degree", "")]
            if edu.get("field"):
                bits.append(edu["field"])
            text = ", ".join(b for b in bits if b)
            if edu.get("institution"):
                text += f" - {edu['institution']}"
            if edu.get("end_year"):
                text += f" ({edu['end_year']})"
            out.append(f"- {text}")

    return "\n".join(out).strip()


def rewrite_bullets(
    *, profile_dict: dict, bullets: list[str], flavor: str = "general"
) -> list[dict]:
    """Standalone bullet-rewrite assistant (Epic B), independent of any JD."""
    if not bullets:
        return []
    llm = get_llm()
    user = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(bullets))
    result = llm.structured(
        "bullet_rewrite",
        user=user,
        output_model=LLMBulletRewrites,
        prompt_vars={"profile_json": profile_dict, "flavor": flavor},
        category="parse",
    )
    return [r.model_dump() for r in result.value.rewrites]
