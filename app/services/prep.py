"""Interview prep brief (PRD Epic G, lightweight for Phase 1).

Chains the three inputs no incumbent tool joins up: this exact JD, live cited
research on this company, and the résumé's actual gaps. The gap-probe questions
are the point — they are the ones that decide screens, and they are the ones a
generic question bank cannot produce.

Research goes through Anthropic's server-side `web_search` tool: a search
engine, not a crawler aimed at a job board or a discussion forum. The two-call
shape (search, then shape) exists so the structuring step can only cite URLs the
search actually returned.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.llm import get_llm
from app.models import Application, InterviewPrepBrief
from app.schemas import LLMPrepBrief
from app.services import profile_service
from app.services.textutil import clip

logger = logging.getLogger(__name__)

MAX_JD_CHARS = 12000


def research_company(*, company: str, title: str, location: str) -> tuple[str, list[dict]]:
    """Cited web research. Returns `(markdown, citations)`."""
    llm = get_llm()
    user = (
        f"Company: {company or '(not stated)'}\n"
        f"Role: {title or '(not stated)'}\n"
        f"Location: {location or '(not stated)'}\n\n"
        "Research this company and role for an interview candidate."
    )
    result = llm.research("company_research", user=user)
    return result.value, result.citations


def build_brief(
    session: Session, application: Application, *, with_research: bool = True
) -> InterviewPrepBrief:
    llm = get_llm()
    profile = profile_service.get_active_profile(session, application.user_id)
    posting = application.job_posting

    research_text = ""
    citations: list[dict] = []
    if with_research and posting and posting.company:
        try:
            research_text, citations = research_company(
                company=posting.company, title=posting.title, location=posting.location
            )
        except Exception as exc:
            # Research is a nice-to-have; a brief without it still beats nothing.
            logger.warning("Company research failed: %s", exc)
            research_text = f"(research unavailable: {exc})"

    user = (
        f"# Role\n{posting.title if posting else ''} at "
        f"{posting.company if posting else ''}\n\n"
        f"# Job description\n{clip(posting.jd_text if posting else '', MAX_JD_CHARS)}\n\n"
        f"# Tailored package already generated for this application\n"
        f"{clip(application.cover_letter_text or '(none yet)', 3000)}"
    )

    result = llm.structured(
        "prep_brief",
        user=user,
        output_model=LLMPrepBrief,
        prompt_vars={
            "profile_json": profile_service.profile_to_dict(profile),
            "gap_json": application.gap_report or {},
            "research": research_text or "(no research run)",
            "citations_json": citations,
        },
        category="prep",
    )
    payload: LLMPrepBrief = result.value

    brief = InterviewPrepBrief(
        application_id=application.id,
        company_snapshot=payload.company_snapshot,
        likely_questions=[q.model_dump() for q in payload.likely_questions],
        star_stories=[s.model_dump() for s in payload.star_stories],
        technical_focus=list(payload.technical_focus),
        salary_briefing=payload.salary_briefing,
        citations=citations,
    )
    # Questions to ask them have no column of their own; they belong with the
    # snapshot the candidate reads just before the call.
    if payload.questions_to_ask_them:
        brief.company_snapshot += "\n\n### Questions to ask them\n" + "\n".join(
            f"- {q}" for q in payload.questions_to_ask_them
        )

    session.add(brief)
    session.commit()
    session.refresh(brief)
    return brief


def latest_brief(session: Session, application: Application) -> InterviewPrepBrief | None:
    briefs = sorted(application.prep_briefs, key=lambda b: b.created_at, reverse=True)
    return briefs[0] if briefs else None


def mock_turn(
    session: Session,
    brief: InterviewPrepBrief,
    *,
    transcript: list[dict],
    user_message: str,
) -> str:
    """One rehearsal turn. Explicitly a practice tool - see the prompt file."""
    llm = get_llm()
    application = brief.application
    posting = application.job_posting
    profile = profile_service.get_active_profile(session, application.user_id)

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in transcript
        if m.get("role") in {"user", "assistant"} and m.get("content")
    ]
    messages.append({"role": "user", "content": user_message})

    result = llm.chat(
        "mock_interview",
        messages=messages,
        prompt_vars={
            "role_context": (
                f"{posting.title} at {posting.company}" if posting else "Unknown role"
            ),
            "profile_json": profile_service.profile_to_dict(profile),
            "questions_json": brief.likely_questions or [],
        },
        category="prep",
        max_tokens=2000,
    )
    return result.value
