"""SQLAlchemy models - a direct expansion of the PRD Section 7 data model.

**Phase 2: multi-tenant.** Every aggregate root carries `user_id`; children reach
their owner through their parent's foreign key rather than duplicating it. The
roots are `CareerProfile`, `IntakeMessage`, `ResumeVariant`, `JobPosting`,
`Application`, `Contact`, `GoogleToken` and `SyncState` - if you add a new
top-level entity, it needs a `user_id` and a scoped query, and
`tests/test_tenancy.py` will fail until it has one.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC.

    Every timestamp column here is a plain `DateTime`, and SQLite hands those
    back without a tzinfo. Returning an aware value would mean a freshly created
    object and the same row re-read from the database compare differently -
    which raises `can't subtract offset-naive and offset-aware datetimes` the
    first time staleness is computed. Storing naive UTC keeps both sides
    consistent; anything crossing an API boundary re-attaches UTC explicitly.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enums (stored as plain strings so SQLite stays inspectable by hand)
# --------------------------------------------------------------------------


class Stage(str, enum.Enum):
    SAVED = "saved"
    APPLIED = "applied"
    SCREEN = "screen"
    INTERVIEW = "interview"
    OFFER = "offer"
    CLOSED = "closed"

    @classmethod
    def ordered(cls) -> list["Stage"]:
        return [cls.SAVED, cls.APPLIED, cls.SCREEN, cls.INTERVIEW, cls.OFFER, cls.CLOSED]

    @property
    def label(self) -> str:
        return {"saved": "Saved", "applied": "Applied", "screen": "Screen",
                "interview": "Interview", "offer": "Offer", "closed": "Closed"}[self.value]


class SourceType(str, enum.Enum):
    """Only two ways a posting can enter Compass. See PRD Section 5.2."""

    API = "api"              # public, documented job API
    USER_PASTED = "user_pasted"  # a single posting the user is personally looking at


# --------------------------------------------------------------------------
# Phase 2 - accounts
# --------------------------------------------------------------------------


class User(Base):
    """An invited user. There is no self-service signup by design: Phase 2 is
    "shareable with a few people", and an open registration form on a personal
    machine holding real résumés is a different product with different duties."""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")

    # "" until the invite is accepted and a password is chosen.
    password_hash: Mapped[str] = mapped_column(String(500), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- invite flow ---
    invite_token: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    # --- spend controls (PRD NFR: cost control) ---
    # 0.0 means unlimited, which is only ever appropriate for the owner. Every
    # invited user gets a real ceiling so one person cannot drain the budget.
    monthly_budget_usd: Mapped[float] = mapped_column(Float, default=5.0)
    period_started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_spend_usd: Mapped[float] = mapped_column(Float, default=0.0)
    lifetime_spend_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def label(self) -> str:
        return self.display_name or self.email


# --------------------------------------------------------------------------
# Epic A - Career Profile
# --------------------------------------------------------------------------


class CareerProfile(Base):
    __tablename__ = "career_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    full_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    links: Mapped[list] = mapped_column(JSON, default=list)  # [{label, url}]

    headline: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    # Free-form notes captured during intake that don't fit a field yet -
    # deliberately preserved rather than dropped, because Epic D reads them.
    intake_notes: Mapped[str] = mapped_column(Text, default="")
    # Questions the profile still cannot answer. Drives the intake follow-up
    # queue, so the next session picks up where the last one stopped.
    open_questions: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    skills: Mapped[list["Skill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    certifications: Mapped[list["Certification"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    experiences: Mapped[list["Experience"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Experience.sort_order",
    )
    education: Mapped[list["Education"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    projects: Mapped[list["PortfolioProject"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", lazy="selectin"
    )
    aspiration: Mapped["Aspiration | None"] = relationship(
        back_populates="profile", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )


class Skill(Base):
    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    # hard | soft | tool | platform | domain
    category: Mapped[str] = mapped_column(String(40), default="hard")
    # beginner | working | strong | expert
    proficiency: Mapped[str] = mapped_column(String(40), default="working")
    years: Mapped[float] = mapped_column(Float, default=0.0)

    profile: Mapped[CareerProfile] = relationship(back_populates="skills")


class Certification(Base):
    __tablename__ = "certification"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str] = mapped_column(String(200), default="")
    # String, not Date: certifications are commonly dated to the month ("2024-06")
    # or the year alone, and coercing that to a real date meant either inventing
    # a day or - as it did before - dropping the value entirely.
    issued_on: Mapped[str] = mapped_column(String(32), default="")
    credential_id: Mapped[str] = mapped_column(String(200), default="")

    profile: Mapped[CareerProfile] = relationship(back_populates="certifications")


class Experience(Base):
    __tablename__ = "experience"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(200), default="")
    start_date: Mapped[str] = mapped_column(String(32), default="")  # "2023-04" - month precision
    end_date: Mapped[str] = mapped_column(String(32), default="")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    responsibilities: Mapped[list] = mapped_column(JSON, default=list)  # list[str]
    achievements: Mapped[list] = mapped_column(JSON, default=list)      # list[str], quantified

    # Epic A asks explicitly what changed vs. the previous role. That answer is
    # the single most useful input to a seniority-signalling rewrite, so it gets
    # its own column instead of being buried in responsibilities.
    scope_change_note: Mapped[str] = mapped_column(Text, default="")

    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    profile: Mapped[CareerProfile] = relationship(back_populates="experiences")


class Education(Base):
    __tablename__ = "education"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    degree: Mapped[str] = mapped_column(String(200))
    institution: Mapped[str] = mapped_column(String(200), default="")
    field: Mapped[str] = mapped_column(String(200), default="")
    end_year: Mapped[str] = mapped_column(String(16), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    profile: Mapped[CareerProfile] = relationship(back_populates="education")


class PortfolioProject(Base):
    """First-class per PRD Epic A / Epic H - not an afterthought field."""

    __tablename__ = "portfolio_project"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    tech: Mapped[list] = mapped_column(JSON, default=list)  # list[str]
    link: Mapped[str] = mapped_column(String(500), default="")
    # idea | in_progress | shipped | archived
    status: Mapped[str] = mapped_column(String(40), default="in_progress")
    highlight: Mapped[str] = mapped_column(Text, default="")  # the one-line pitch
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    profile: Mapped[CareerProfile] = relationship(back_populates="projects")


class Aspiration(Base):
    __tablename__ = "aspiration"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("career_profile.id", ondelete="CASCADE"))
    target_titles: Mapped[list] = mapped_column(JSON, default=list)
    target_industries: Mapped[list] = mapped_column(JSON, default=list)
    target_companies: Mapped[list] = mapped_column(JSON, default=list)
    locations: Mapped[list] = mapped_column(JSON, default=list)
    remote_preference: Mapped[str] = mapped_column(String(40), default="hybrid")
    comp_min: Mapped[float] = mapped_column(Float, default=0.0)
    comp_max: Mapped[float] = mapped_column(Float, default=0.0)
    comp_currency: Mapped[str] = mapped_column(String(8), default="INR")
    non_negotiables: Mapped[list] = mapped_column(JSON, default=list)

    profile: Mapped[CareerProfile] = relationship(back_populates="aspiration")


class IntakeMessage(Base):
    """Transcript of the Epic A conversational intake. Kept so the profile can
    be recompiled later without re-interviewing."""

    __tablename__ = "intake_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------
# Epic B - resumes
# --------------------------------------------------------------------------


class ResumeVariant(Base):
    __tablename__ = "resume_variant"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("career_profile.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(200))
    # e.g. "power-bi-forward", "data-engineering-forward", "base"
    flavor: Mapped[str] = mapped_column(String(80), default="base")
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)

    source_filename: Mapped[str] = mapped_column(String(300), default="")
    stored_path: Mapped[str] = mapped_column(String(500), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    content_md: Mapped[str] = mapped_column(Text, default="")  # editable markdown body

    ats_score: Mapped[float] = mapped_column(Float, default=0.0)
    ats_report: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    applications: Mapped[list["Application"]] = relationship(back_populates="resume_variant")


# --------------------------------------------------------------------------
# Epic C/D - postings and applications
# --------------------------------------------------------------------------


class JobPosting(Base):
    __tablename__ = "job_posting"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(20), default=SourceType.USER_PASTED.value)
    # adzuna | greenhouse | lever | ashby | remoteok | arbeitnow | user
    source_name: Mapped[str] = mapped_column(String(40), default="user")
    external_id: Mapped[str] = mapped_column(String(200), default="")

    company: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    jd_text: Mapped[str] = mapped_column(Text, default="")
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    applications: Mapped[list["Application"]] = relationship(
        back_populates="job_posting", cascade="all, delete-orphan"
    )


class Application(Base):
    __tablename__ = "application"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    job_posting_id: Mapped[int] = mapped_column(ForeignKey("job_posting.id", ondelete="CASCADE"))
    resume_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_variant.id", ondelete="SET NULL"), nullable=True
    )

    stage: Mapped[str] = mapped_column(String(20), default=Stage.SAVED.value)

    # --- Epic B output, snapshotted at generation time ---
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    match_report: Mapped[dict] = mapped_column(JSON, default=dict)
    gap_report: Mapped[dict] = mapped_column(JSON, default=dict)

    # --- Epic D output ---
    tailored_resume_md: Mapped[str] = mapped_column(Text, default="")
    cover_letter_text: Mapped[str] = mapped_column(Text, default="")
    talking_points: Mapped[list] = mapped_column(JSON, default=list)
    tailoring_meta: Mapped[dict] = mapped_column(JSON, default=dict)

    # --- Epic D quality gate ---
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    quality_report: Mapped[dict] = mapped_column(JSON, default=dict)
    # pass | review | templated | not_run
    quality_verdict: Mapped[str] = mapped_column(String(20), default="not_run")
    gate_override_reason: Mapped[str] = mapped_column(Text, default="")

    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job_posting: Mapped[JobPosting] = relationship(back_populates="applications", lazy="joined")
    resume_variant: Mapped[ResumeVariant | None] = relationship(back_populates="applications")
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationEvent.occurred_at.desc()",
        lazy="selectin",
    )
    prep_briefs: Mapped[list["InterviewPrepBrief"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationEvent(Base):
    """Timeline entry. `source` records provenance so a Gmail-inferred stage
    change is always distinguishable from something you typed yourself."""

    __tablename__ = "application_event"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_event_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id", ondelete="CASCADE"))

    # stage_change | email | interview_scheduled | note | followup_due
    kind: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    # manual | gmail | calendar | system
    source: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(300), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="events")


# --------------------------------------------------------------------------
# Epic F - outreach (schema in Phase 1, UI lands in Phase 2)
# --------------------------------------------------------------------------


class Contact(Base):
    """User-supplied only. Nothing in this codebase writes to this table from
    an automated discovery path - see docs/CONSTRAINTS.md."""

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(200), default="")
    relationship_note: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    # How you came to know this person - required by the intake form so the
    # "manual, by design" rule is visible in the data, not just the docs.
    how_known: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    messages: Mapped[list["OutreachMessage"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )


class OutreachMessage(Base):
    __tablename__ = "outreach_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id", ondelete="CASCADE"))
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("application.id", ondelete="SET NULL"), nullable=True
    )
    variant: Mapped[str] = mapped_column(String(40), default="short_dm")
    body: Mapped[str] = mapped_column(Text, default="")
    sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # not_sent | awaiting | replied | no_reply
    reply_status: Mapped[str] = mapped_column(String(20), default="not_sent")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contact: Mapped[Contact] = relationship(back_populates="messages")


# --------------------------------------------------------------------------
# Epic G - interview prep
# --------------------------------------------------------------------------


class InterviewPrepBrief(Base):
    __tablename__ = "interview_prep_brief"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id", ondelete="CASCADE"))

    company_snapshot: Mapped[str] = mapped_column(Text, default="")
    likely_questions: Mapped[list] = mapped_column(JSON, default=list)
    star_stories: Mapped[list] = mapped_column(JSON, default=list)
    technical_focus: Mapped[list] = mapped_column(JSON, default=list)
    salary_briefing: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)  # [{title, url}]

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="prep_briefs")
    sessions: Mapped[list["PracticeSession"]] = relationship(
        back_populates="brief", cascade="all, delete-orphan"
    )


class PracticeSession(Base):
    """Rehearsal only. See PRD Epic G - explicitly not a live interview assist."""

    __tablename__ = "practice_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("interview_prep_brief.id", ondelete="CASCADE"))
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # [{role, content}]
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    brief: Mapped[InterviewPrepBrief] = relationship(back_populates="sessions")


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------


class GoogleToken(Base):
    """One row per user, holding that user's own OAuth credentials.

    Per-user is not optional here: this is the single most sensitive record in
    the database, and a shared row would mean one invitee's Gmail being read on
    another's behalf.
    """

    __tablename__ = "google_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True, index=True
    )
    token_json: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    account_email: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LLMCache(Base):
    """Content-addressed cache so re-opening a scored application is free.
    Complements Anthropic-side prompt caching; this one avoids the call entirely."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    # The owning user is also mixed into the hash, so entries are never shared
    # across accounts. This column exists for auditing and cleanup, not lookup:
    # cached output is derived from someone's résumé, and saving a few cents by
    # serving it to another account is not a trade worth making.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    prompt_name: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(80))
    response_json: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncState(Base):
    """Bookmarks for incremental integrations (e.g. Gmail message ids already
    seen). Keyed per user - a shared bookmark would make one user's sync skip
    another's mail."""

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_sync_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# Job discovery (Phase 2) - docs/CONSTRAINTS.md §1
# --------------------------------------------------------------------------
#
# Postings come from applicant-tracking systems' own public job-board endpoints.
# Those exist so that employers can syndicate their listings; consuming one is
# what it is for, which is a different act from crawling a job board that
# forbids it. The restricted-platform list in docs/CONSTRAINTS.md is unchanged
# and `tests/test_constraints.py` pins the allowed hosts.
#
# Both tables are per-user rather than a shared corpus. Two accounts watching the
# same company do store the posting twice, which is wasteful and bought
# deliberately: a shared corpus would need its own answer to "user A dismissed
# this, does user B still see it", and the tenancy invariant in this module's
# docstring says a new top-level entity carries a `user_id`.


class JobSource(Base):
    """One employer's ATS job board, polled on demand."""

    __tablename__ = "job_source"
    __table_args__ = (
        UniqueConstraint("user_id", "ats", "board_token", name="uq_source_user_board"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    # greenhouse | ashby | smartrecruiters | lever | workable
    ats: Mapped[str] = mapped_column(String(40))
    # The employer's board slug, e.g. "stripe" in boards.greenhouse.io/stripe.
    board_token: Mapped[str] = mapped_column(String(200))
    company_name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_job_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    jobs: Mapped[list["DiscoveredJob"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return self.company_name or self.board_token


class DiscoveredJob(Base):
    """A posting pulled from a `JobSource`, with this user's fit scores.

    Scores live here rather than in a join table because the row is already
    per-user, so there is nothing to join to.
    """

    __tablename__ = "discovered_job"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source_id", "external_id", name="uq_discovered_external"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("job_source.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(200))

    company: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    department: Mapped[str] = mapped_column(String(200), default="")
    employment_type: Mapped[str] = mapped_column(String(60), default="")
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(String(1000), default="")
    jd_text: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # `is_open` goes false when a refresh no longer lists the posting, rather
    # than deleting the row: a closed role you already adopted should not
    # vanish from the tracker's history.
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # --- fit, computed by app/services/discovery/rank.py ---
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    keyword_score: Mapped[float] = mapped_column(Float, default=0.0)
    semantic_score: Mapped[float] = mapped_column(Float, default=0.0)
    # What the fit becomes once the résumé says what the profile already knows.
    # The metric the ranked list sorts on, and the one nobody else computes.
    closeable_score: Mapped[float] = mapped_column(Float, default=0.0)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Whether the semantic pass ran, or only the cheap lexical prefilter.
    is_deep_scored: Mapped[bool] = mapped_column(Boolean, default=False)
    match_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set once the posting has been pulled into the application pipeline, so the
    # discover list can show it as taken rather than offering it again.
    adopted_application_id: Mapped[int | None] = mapped_column(
        ForeignKey("application.id", ondelete="SET NULL"), nullable=True
    )

    source: Mapped["JobSource"] = relationship(back_populates="jobs")
