"""Pydantic schemas.

Almost everything here is an `LLM*` model: the exact output contract handed to
the Messages API as a strict JSON schema (see `app/llm/schema.py`).

**Every field on an `LLM*` model is required and non-nullable.** Optional and
nullable fields make a strict schema fragile, so where a value may be unknown
the prompt instructs Claude to emit `""` or `[]` rather than omitting the key.
`tests/test_pipeline.py::test_llm_schemas_harden_cleanly` enforces the shape.

Database rows use the SQLAlchemy models in `app/models.py`; the structured
projection passed into prompts is a plain dict built by
`profile_service.profile_to_dict`. There is deliberately no third parallel
Pydantic representation of the profile to keep in sync.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# ==========================================================================
# LLM output contracts - all fields required, no Optional / no None
# ==========================================================================


class LLMLink(BaseModel):
    label: str
    url: str


class LLMSkill(BaseModel):
    name: str
    category: Literal["hard", "soft", "tool", "platform", "domain"]
    proficiency: Literal["beginner", "working", "strong", "expert"]
    years: float


class LLMCertification(BaseModel):
    name: str
    issuer: str
    issued_on: str
    credential_id: str


class LLMExperience(BaseModel):
    title: str
    company: str
    location: str
    start_date: str
    end_date: str
    is_current: bool
    responsibilities: list[str]
    achievements: list[str]
    scope_change_note: str


class LLMEducation(BaseModel):
    degree: str
    institution: str
    field: str
    end_year: str
    notes: str


class LLMProject(BaseModel):
    name: str
    description: str
    tech: list[str]
    link: str
    status: Literal["idea", "in_progress", "shipped", "archived"]
    highlight: str


class LLMAspiration(BaseModel):
    target_titles: list[str]
    target_industries: list[str]
    target_companies: list[str]
    locations: list[str]
    remote_preference: Literal["onsite", "hybrid", "remote", "any"]
    comp_min: float
    comp_max: float
    comp_currency: str
    non_negotiables: list[str]


class LLMCareerProfile(BaseModel):
    """Output of both resume parsing and intake-transcript compilation."""

    full_name: str
    email: str
    phone: str
    location: str
    links: list[LLMLink]
    headline: str
    summary: str
    skills: list[LLMSkill]
    certifications: list[LLMCertification]
    experiences: list[LLMExperience]
    education: list[LLMEducation]
    projects: list[LLMProject]
    aspiration: LLMAspiration
    # Fields the source material did not cover - drives the intake follow-up queue.
    unresolved_questions: list[str]


# --- Epic B: gap report ------------------------------------------------------


class LLMGapItem(BaseModel):
    kind: Literal[
        "missing_keyword",
        "missing_skill",
        "unquantified_bullet",
        "weak_verb",
        "section_gap",
        "seniority_signal",
        "overclaim_risk",
    ]
    severity: Literal["high", "medium", "low"]
    detail: str
    suggested_fix: str
    evidence: str


class LLMGapReport(BaseModel):
    summary: str
    items: list[LLMGapItem]
    # Requirements the resume genuinely does not support. Named separately so
    # Epic D can be told never to invent coverage for them.
    honest_gaps: list[str]
    strongest_matches: list[str]


# --- Epic B: bullet rewrite --------------------------------------------------


class LLMBulletRewrite(BaseModel):
    original: str
    rewritten: str
    rationale: str
    # "" when the rewrite is fully supported by the source bullet.
    needs_user_input: str


class LLMBulletRewrites(BaseModel):
    rewrites: list[LLMBulletRewrite]


# --- Epic D: tailored package ------------------------------------------------


class LLMTalkingPoint(BaseModel):
    point: str
    evidence: str


class LLMTailoredBullet(BaseModel):
    company: str
    original: str
    tailored: str
    rationale: str


class LLMTailoredPackage(BaseModel):
    headline: str
    summary: str
    bullets: list[LLMTailoredBullet]
    skills_to_surface: list[str]
    cover_letter: str
    talking_points: list[LLMTalkingPoint]
    keywords_incorporated: list[str]
    # Explicit anti-fabrication channel: what Claude deliberately did NOT claim.
    claims_avoided: list[str]
    company_specific_details_used: list[str]


# --- Epic E: Gmail classification -------------------------------------------


class LLMEmailClassification(BaseModel):
    category: Literal[
        "application_confirmation",
        "rejection",
        "interview_invite",
        "recruiter_outreach",
        "offer",
        "assessment_request",
        "other",
    ]
    company: str
    role_title: str
    confidence: float
    reasoning: str
    # ISO-8601 or "" - only when the email states an interview time.
    interview_datetime: str


class LLMEmailBatchClassification(BaseModel):
    results: list[LLMEmailClassification]


# --- Epic G: prep brief ------------------------------------------------------


class LLMPrepQuestion(BaseModel):
    question: str
    why_likely: str
    answer_scaffold: str
    category: Literal["behavioural", "technical", "domain", "role_specific", "gap_probe"]


class LLMStarStory(BaseModel):
    title: str
    situation: str
    task: str
    action: str
    result: str
    maps_to: list[str]


class LLMPrepBrief(BaseModel):
    company_snapshot: str
    likely_questions: list[LLMPrepQuestion]
    star_stories: list[LLMStarStory]
    technical_focus: list[str]
    salary_briefing: str
    questions_to_ask_them: list[str]


# ==========================================================================
# Request/response shapes for the HTTP layer
# ==========================================================================


class JDInput(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    jd_text: str
