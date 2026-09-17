"""Demo mode: the public-deployment escape hatch, and its three limits.

Compass is invite-only, which is right for real career data and useless for a
link on a CV. Demo mode publishes one account's credentials so a visitor can get
in. That is a deliberate hole, so the limits around it are the part that needs
tests: it must not be an admin, it must be spend-capped, and it must never
appear on an instance that already holds someone's data.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Application, CareerProfile, ResumeVariant, User
from app.services import demo


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def test_seeds_an_account_on_an_empty_database(session):
    user = demo.seed_if_empty(session)
    assert user is not None
    assert user.email == demo.DEMO_EMAIL
    assert user.has_password


def test_refuses_to_seed_when_any_account_exists(session, user):
    """The load-bearing guard. Turning demo mode on against a real instance must
    not bolt a publicly-known login onto somebody's career history."""
    assert demo.seed_if_empty(session) is None
    assert session.scalar(select(User).where(User.email == demo.DEMO_EMAIL)) is None


def test_seeding_twice_is_a_no_op(session):
    """Runs on every startup, so it has to be idempotent."""
    first = demo.seed_if_empty(session)
    assert first is not None
    assert demo.seed_if_empty(session) is None
    assert session.scalar(
        select(User).where(User.email == demo.DEMO_EMAIL)
    ) is not None


# --------------------------------------------------------------------------
# The limits
# --------------------------------------------------------------------------


def test_demo_account_is_not_an_admin(session):
    """An admin could create invites, read the user list and edit budgets. The
    credentials are published, so this is the difference between a demo and an
    open door."""
    user = demo.seed_if_empty(session)
    assert user.is_admin is False


def test_demo_account_is_spend_capped(session):
    """It runs on the operator's API key."""
    user = demo.seed_if_empty(session)
    assert 0 < user.monthly_budget_usd <= 5.0


def test_demo_data_is_fiction(session):
    """Seeding a demo with the operator's own résumé would publish it. The
    fixture names a person who does not exist and an example.com address."""
    demo.seed_if_empty(session)
    variant = session.scalar(select(ResumeVariant))
    assert "example.com" in variant.raw_text
    assert "@accenture" not in variant.raw_text.lower()
    assert "hrisit" not in variant.raw_text.lower()


# --------------------------------------------------------------------------
# What a visitor actually sees
# --------------------------------------------------------------------------


def test_every_page_has_something_on_it(session):
    """A demo whose pages are all empty states demonstrates nothing."""
    demo.seed_if_empty(session)

    profile = session.scalar(select(CareerProfile))
    assert profile is not None
    assert len(profile.experiences) >= 2
    assert len(profile.skills) >= 5

    assert session.scalar(select(ResumeVariant)) is not None
    applications = list(session.scalars(select(Application)))
    assert len(applications) >= 3
    # Spread across stages, so the tracker board is not one column.
    assert len({a.stage for a in applications}) >= 3


def test_scores_come_from_the_real_scorer(session):
    """Hand-written scores would disagree with the app the moment a visitor
    clicked Re-score, which is worse than having none."""
    demo.seed_if_empty(session)
    for application in session.scalars(select(Application)):
        assert application.match_score > 0
        report = application.match_report or {}
        assert report.get("backend"), "no scoring backend recorded"
        assert "matched_terms" in report


def test_the_resume_omits_skills_the_profile_holds(session):
    """Discover's headline metric is fit-after-tailoring, which is the gap
    between what the résumé says and what the profile knows. A fixture where
    they match shows a column of +0 and demonstrates the opposite of the
    feature."""
    demo.seed_if_empty(session)
    variant = session.scalar(select(ResumeVariant))
    profile = session.scalar(select(CareerProfile))

    resume_text = variant.raw_text.lower()
    profile_skills = {s.name.lower() for s in profile.skills}
    missing_from_resume = {s for s in profile_skills if s not in resume_text}

    assert missing_from_resume, (
        "every profile skill is already on the résumé, so the uplift is zero"
    )


def test_ats_score_is_measured_not_asserted(session):
    demo.seed_if_empty(session)
    variant = session.scalar(select(ResumeVariant))
    assert 0 < variant.ats_score <= 100
    assert variant.ats_report.get("grade")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_login_page_publishes_the_credentials_only_in_demo_mode(monkeypatch):
    """Off by default. A normal instance must not advertise a login."""
    from app.config import get_settings
    from app.routers import auth_router

    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_DEMO_MODE", "false")
    assert auth_router._demo_hint() is None

    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_DEMO_MODE", "true")
    hint = auth_router._demo_hint()
    assert hint is not None
    assert hint["email"] == demo.DEMO_EMAIL
    assert hint["password"] == demo.DEMO_PASSWORD

    get_settings.cache_clear()


def test_demo_mode_is_off_by_default():
    from app.config import Settings

    assert Settings(_env_file=None).compass_demo_mode is False
