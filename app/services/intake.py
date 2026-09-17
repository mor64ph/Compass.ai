"""Career intake agent (PRD Epic A).

A hybrid of conversation and form. The conversation does the work a form cannot:
pushing for the numbers behind a vague bullet, and asking what changed about a
person's scope of ownership.

Compilation is a single pass over the whole transcript rather than an incremental
patch after each turn. Incremental extraction sounds tidier but it means every
turn is another chance to silently overwrite something, and it makes the profile
depend on the order questions were asked in.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.llm import get_llm
from app.models import CareerProfile, IntakeMessage
from app.schemas import LLMCareerProfile
from app.services import profile_service
from app.services.textutil import clip

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 40000
OPENING_MESSAGE = (
    "Let's build your career profile. I'll ask about your work, push for the "
    "numbers behind it, and cover where you're trying to get to.\n\n"
    "Start wherever you like — what are you responsible for in your current "
    "role, and what's changed about that in the last year or so?"
)


def transcript(session: Session, user_id: int) -> list[IntakeMessage]:
    return list(
        session.scalars(
            select(IntakeMessage)
            .where(IntakeMessage.user_id == user_id)
            .order_by(IntakeMessage.created_at.asc())
        )
    )


def reset(session: Session, user_id: int) -> None:
    session.execute(delete(IntakeMessage).where(IntakeMessage.user_id == user_id))
    session.commit()


def chat_turn(session: Session, user_id: int, user_message: str) -> str:
    profile = profile_service.get_active_profile(session, user_id)
    history = transcript(session, user_id)

    session.add(IntakeMessage(user_id=user_id, role="user", content=user_message))
    session.commit()

    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": user_message})

    llm = get_llm()
    result = llm.chat(
        "intake_chat",
        messages=messages,
        prompt_vars={
            "profile_json": profile_service.profile_to_dict(profile),
            "open_questions": (profile.open_questions if profile else []) or ["(none yet)"],
        },
        category="intake",
        max_tokens=1500,
    )
    reply = result.value

    session.add(IntakeMessage(user_id=user_id, role="assistant", content=reply))
    session.commit()
    return reply


def compile_profile(session: Session, user_id: int) -> CareerProfile:
    """Turn the transcript into the structured profile every other epic reads."""
    profile = profile_service.get_active_profile(session, user_id)
    history = transcript(session, user_id)
    if not history:
        raise ValueError("There is no intake conversation to compile yet.")

    body = "\n\n".join(
        f"{'You' if m.role == 'assistant' else 'Candidate'}: {m.content}" for m in history
    )

    llm = get_llm()
    result = llm.structured(
        "intake_compile",
        user=f"# Intake transcript\n{clip(body, MAX_TRANSCRIPT_CHARS)}",
        output_model=LLMCareerProfile,
        prompt_vars={"profile_json": profile_service.profile_to_dict(profile)},
        category="parse",
    )
    payload: LLMCareerProfile = result.value

    updated = profile_service.upsert_from_payload(session, user_id, payload)
    updated.open_questions = list(payload.unresolved_questions)
    session.commit()
    session.refresh(updated)
    return updated
