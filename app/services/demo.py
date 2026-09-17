"""Seed a signed-in-able demo account, for a public portfolio deployment.

Compass is invite-only by design, which is correct for an instance holding real
career data and useless for a link on a CV: a visitor hits the login wall and
sees nothing. With `COMPASS_DEMO_MODE=true` an empty database gets one demo
account with published credentials and enough data that every page has something
on it.

Three deliberate limits, because "published credentials on the public internet"
is exactly as dangerous as it sounds:

* **Not an admin.** No invites, no user list, no budget editing, no sight of
  anyone else. `require_admin` keeps every one of those out of reach.
* **Budget-capped.** The demo runs on the operator's API key, so it carries a
  small monthly ceiling. When it is spent, the deterministic half - ATS checks,
  JD scoring, the quality gate, the tracker - keeps working, which is most of
  what a visitor came to look at anyway.
* **Never on an instance that already has accounts.** Seeding only touches a
  database with no users at all, so turning this on cannot bolt a public login
  onto someone's real data.

The data below is fiction. It has to be: seeding a demo with the operator's own
résumé would publish it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, JobPosting, ResumeVariant, Stage, User, utcnow

logger = logging.getLogger(__name__)

DEMO_EMAIL = "demo@compass.local"
DEMO_PASSWORD = "compass-demo-account"
DEMO_BUDGET_USD = 2.0

# A résumé that deliberately omits things the profile knows, so Discover's
# fit-if-tailored number has something real to show. A demo where the headline
# metric reads +0 demonstrates nothing.
DEMO_RESUME = """# Priya Raghavan
priya.raghavan@example.com | +91 98765 43210 | Bengaluru, India
github.com/example

## Summary
Business intelligence developer with six years on Power BI semantic models and
DAX, working with finance and supply-chain teams.

## Experience

### Senior Power BI Developer - Meridian Retail
Apr 2021 - Present | Bengaluru
- Cut model refresh from 42 minutes to 9 by removing 6 calculated columns
- Rebuilt the sales semantic model used by 340 weekly report consumers
- Took over the semantic layer end to end, having previously only built reports

### BI Analyst - Thornbury Logistics
Jun 2019 - Mar 2021 | Bengaluru
- Built the first company-wide freight cost dashboard
- Automated a weekly report that had taken two days of manual work

## Skills
Power BI, DAX, SQL, Power Query, dimensional modelling, row-level security

## Education
B.E. Computer Science, 2019
"""

# Held in the profile but absent from the résumé above. These are what the
# closeable-fit metric surfaces.
DEMO_EXTRA_SKILLS = ["dbt", "Snowflake", "Airflow", "Python", "Azure Data Factory"]


def seed_if_empty(session: Session) -> User | None:
    """Create the demo account and its data, or do nothing.

    Returns the user when it seeded, None when it declined. Safe to call on
    every start; it is a no-op once anything exists.
    """
    existing = session.scalar(select(User).limit(1))
    if existing is not None:
        logger.info("Demo mode on, but accounts already exist - not seeding")
        return None

    from app.auth import hash_password

    user = User(
        email=DEMO_EMAIL,
        display_name="Priya Raghavan (demo)",
        password_hash=hash_password(DEMO_PASSWORD),
        is_admin=False,          # no invites, no user list, no budget editing
        is_active=True,
        monthly_budget_usd=DEMO_BUDGET_USD,
    )
    session.add(user)
    session.flush()

    _seed_profile(session, user)
    _seed_resume(session, user)
    _seed_applications(session, user)
    session.commit()

    logger.info(
        "Seeded the demo account %s - not an admin, $%.2f/month cap",
        DEMO_EMAIL, DEMO_BUDGET_USD,
    )
    return user


def _seed_profile(session: Session, user: User) -> None:
    from app.schemas import (LLMAspiration, LLMCareerProfile, LLMExperience,
                             LLMSkill)
    from app.services import profile_service

    def skill(name: str, years: float = 4.0) -> LLMSkill:
        return LLMSkill(name=name, category="hard", proficiency="strong", years=years)

    profile_service.upsert_from_payload(session, user.id, LLMCareerProfile(
        full_name="Priya Raghavan",
        email="priya.raghavan@example.com",
        phone="+91 98765 43210",
        location="Bengaluru, India",
        links=[],
        headline="Senior Power BI Developer - semantic modelling & DAX",
        summary=(
            "Business intelligence developer with six years on Power BI semantic "
            "models, moving toward analytics engineering."
        ),
        skills=[skill(n) for n in
                ["Power BI", "DAX", "SQL", "Power Query", "Dimensional modelling"]]
              + [skill(n, 2.0) for n in DEMO_EXTRA_SKILLS],
        certifications=[],
        experiences=[
            LLMExperience(
                title="Senior Power BI Developer", company="Meridian Retail",
                location="Bengaluru", start_date="2021-04", end_date="",
                is_current=True,
                responsibilities=["Own the sales and finance semantic layer"],
                achievements=[
                    "Cut model refresh from 42 minutes to 9 by removing 6 calculated columns",
                    "Rebuilt the sales semantic model used by 340 weekly consumers",
                ],
                scope_change_note=(
                    "Took over the semantic layer end to end; previously only built "
                    "reports on top of it"
                ),
            ),
            LLMExperience(
                title="BI Analyst", company="Thornbury Logistics",
                location="Bengaluru", start_date="2019-06", end_date="2021-03",
                is_current=False,
                responsibilities=["Freight and warehouse reporting"],
                achievements=["Built the first company-wide freight cost dashboard"],
                scope_change_note="",
            ),
        ],
        education=[],
        projects=[],
        aspiration=LLMAspiration(
            target_titles=["Analytics Engineer", "Senior Data Engineer",
                           "BI Platform Engineer"],
            target_industries=[], target_companies=[],
            locations=["Bengaluru", "Remote"],
            remote_preference="hybrid",
            comp_min=0, comp_max=0, comp_currency="INR",
            non_negotiables=[],
        ),
        unresolved_questions=[
            "What was the measurable outcome of the freight dashboard?",
            "How large was the dbt project you worked on?",
        ],
    ))


def _seed_resume(session: Session, user: User) -> None:
    """Run the real ATS checker over the demo résumé rather than inventing a
    score - a hand-written number could disagree with what the checker says
    when a visitor clicks Re-check."""
    from app.services import ats_check, text_extract

    doc = text_extract.ExtractedDoc(
        text=DEMO_RESUME, kind="md", filename="priya-raghavan.md")
    report = ats_check.check(doc)

    session.add(ResumeVariant(
        user_id=user.id,
        label="Power BI forward - v3",
        flavor="power-bi-forward",
        source_filename="priya-raghavan.md",
        raw_text=DEMO_RESUME,
        content_md=DEMO_RESUME,
        is_master=True,
        ats_score=report.score,
        ats_report=report.as_dict(),
    ))


def _seed_applications(session: Session, user: User) -> None:
    """Three applications at different stages, scored by the real deterministic
    scorer for the same reason as the résumé above."""
    from app.services import application_service, jd_match

    postings = [
        (
            "Meridian Data", "Analytics Engineer", "Bengaluru (hybrid)",
            Stage.INTERVIEW.value,
            "About the role\n"
            "We are building the semantic layer for a retail analytics platform.\n\n"
            "Requirements\n"
            "- Strong SQL and dimensional modelling\n"
            "- dbt for transformations\n"
            "- Power BI or a comparable BI tool\n"
            "- Ownership of a data model end to end\n\n"
            "Nice to have\n"
            "- Snowflake\n- Airflow\n",
        ),
        (
            "Thornbury Cloud", "Senior Data Engineer", "Remote - India",
            Stage.SCREEN.value,
            "Responsibilities\n"
            "- Build and operate batch pipelines in Airflow\n"
            "- Model data in Snowflake with dbt\n"
            "- Partner with analysts on semantic models\n\n"
            "Requirements\n"
            "- Python and SQL at depth\n"
            "- Experience owning production pipelines\n"
            "- Spark or a comparable distributed engine\n",
        ),
        (
            "Atlas Systems", "BI Platform Engineer", "Bengaluru",
            Stage.APPLIED.value,
            "What you will do\n"
            "- Administer and tune a Power BI tenant\n"
            "- Implement row-level security across workspaces\n"
            "- Define the semantic model standards other teams build on\n\n"
            "Requirements\n"
            "- Deep Power BI and DAX\n- Row-level security\n- Capacity planning\n",
        ),
    ]

    resume_text = DEMO_RESUME
    evidence = ", ".join(DEMO_EXTRA_SKILLS)

    for company, title, location, stage, jd_text in postings:
        posting = JobPosting(
            user_id=user.id,
            source_type="user_pasted",
            source_name="user",
            company=company, title=title, location=location, jd_text=jd_text,
        )
        session.add(posting)
        session.flush()

        report = jd_match.score(
            resume_text=resume_text, jd_text=jd_text, jd_title=title,
            extra_evidence=evidence,
        )
        application = Application(
            user_id=user.id,
            job_posting_id=posting.id,
            stage=stage,
            match_score=report.composite,
            match_report=report.as_dict(),
            last_activity_at=utcnow(),
        )
        session.add(application)
        session.flush()
        application_service.log_event(
            session, application, kind="note",
            summary="Demo application created at startup.", source="manual",
        )
