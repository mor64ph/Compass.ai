"""Open registration, and the things that make it defensible.

Compass is invite-only by default. `COMPASS_OPEN_SIGNUP` lets strangers create
accounts, which is a deliberate change of posture: the instance then holds other
people's employment history. These tests pin the limits that come with that —
never an admin, always capped, throttled, non-enumerable, and erasable.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import func, select

from app import auth
from app.config import get_settings
from app.models import (Application, CareerProfile, DiscoveredJob, JobPosting,
                        JobSource, LLMCache, ResumeVariant, User)
from app.security import signup_throttle


@pytest.fixture
def anon(session, user):
    """A signed-OUT client against an instance that already has its owner.

    The shared `client` fixture is signed in, which makes /signup redirect to /
    and sends /account/delete down the administrator branch — so it cannot
    exercise registration at all.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def _open_signup(monkeypatch):
    """Registration off by default, so every test here turns it on explicitly."""
    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_OPEN_SIGNUP", "true")
    monkeypatch.setenv("COMPASS_MAX_ACCOUNTS", "50")
    monkeypatch.setenv("COMPASS_SIGNUP_BUDGET_USD", "1.0")
    signup_throttle.reset()
    yield
    signup_throttle.reset()
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# It is off unless asked for
# --------------------------------------------------------------------------


def test_open_signup_is_off_by_default():
    """Checked on the field's declared default rather than by constructing
    Settings: `_env_file=None` skips the .env file but not the environment, and
    this module's autouse fixture sets the variable."""
    from app.config import Settings

    assert Settings.model_fields["compass_open_signup"].default is False


def test_register_refuses_when_the_flag_is_off(session, user, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_OPEN_SIGNUP", "false")
    with pytest.raises(ValueError, match="invite-only"):
        auth.register(session, email="x@example.com", password="a-long-passphrase")
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# The limits
# --------------------------------------------------------------------------


def test_a_registered_account_is_never_an_admin(session, user):
    """It is created by a stranger. Admin would hand over invites, the user list
    and everyone's budgets."""
    new = auth.register(session, email="a@example.com", password="a-long-passphrase")
    assert new.is_admin is False


def test_a_registered_account_is_never_uncapped(session, user):
    """0 means uncapped elsewhere in the model, and this account spends the
    operator's API key."""
    new = auth.register(session, email="b@example.com", password="a-long-passphrase")
    assert new.monthly_budget_usd > 0


def test_the_account_ceiling_is_enforced(session, user, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_MAX_ACCOUNTS", "2")  # owner + one
    auth.register(session, email="first@example.com", password="a-long-passphrase")
    with pytest.raises(ValueError, match="account limit"):
        auth.register(session, email="second@example.com", password="a-long-passphrase")
    get_settings.cache_clear()


def test_a_zero_ceiling_means_no_limit(session, user, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_MAX_ACCOUNTS", "0")
    for i in range(4):
        auth.register(session, email=f"n{i}@example.com", password="a-long-passphrase")
    get_settings.cache_clear()


def test_a_weak_password_is_refused(session, user):
    with pytest.raises(ValueError, match="at least 12"):
        auth.register(session, email="c@example.com", password="short")


def test_a_malformed_address_is_refused(session, user):
    for bad in ("nope", "no@domain", "@example.com", ""):
        with pytest.raises(ValueError):
            auth.register(session, email=bad, password="a-long-passphrase")


# --------------------------------------------------------------------------
# It must not become an account oracle
# --------------------------------------------------------------------------


def test_a_taken_address_does_not_say_so(session, user):
    """`/login` is careful never to reveal which addresses have accounts. A
    registration form that answers "already taken" gives it away for free."""
    auth.register(session, email="taken@example.com", password="a-long-passphrase")
    with pytest.raises(ValueError) as taken:
        auth.register(session, email="taken@example.com", password="a-long-passphrase")

    assert "already" not in str(taken.value).lower().replace("already have one", "")
    # The wording points at signing in rather than confirming the address exists.
    assert "sign in" in str(taken.value).lower()


def test_signup_is_throttled_per_source_address(anon):
    """Keyed on a constant, not the submitted email — the attacker picks that
    field and would otherwise get a fresh allowance per attempt."""
    payload = {
        "password": "a-long-enough-passphrase",
        "confirm": "a-long-enough-passphrase",
        "accept": "true",
    }
    for i in range(signup_throttle.limit):
        created = anon.post("/signup", data={**payload, "email": f"spam{i}@example.com"})
        assert created.headers["location"] == "/profile", f"attempt {i} was refused"

    # Asserted on the redirect rather than the flash text: by now the client is
    # signed in from those five, so following the redirect lands on the dashboard
    # and the message renders somewhere the test was not looking.
    blocked = anon.post("/signup", data={**payload, "email": "another@example.com"})
    assert blocked.headers["location"] == "/signup"

    from sqlalchemy import select

    from app.db import SessionLocal
    check = SessionLocal()
    try:
        assert check.scalar(
            select(User).where(User.email == "another@example.com")
        ) is None, "a throttled attempt still created the account"
    finally:
        check.close()


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def test_the_page_states_what_is_stored_before_the_form(anon):
    """Somebody is about to type their employment history into a stranger's
    deployment. The disclosure has to be readable before they commit, not after."""
    body = anon.get("/signup", follow_redirects=True).text
    assert "Not encrypted at rest" in body
    assert "delete your account" in body.lower()
    assert "No password reset" in body


def test_the_disclosure_must_be_acknowledged(anon):
    """Without the checkbox the post is rejected, so nobody can be said not to
    have been told."""
    r = anon.post("/signup", data={
        "email": "noack@example.com", "password": "a-long-enough-passphrase",
        "confirm": "a-long-enough-passphrase",
    }, follow_redirects=True)
    assert "confirm you have read" in r.text


def test_signup_is_refused_when_the_flag_is_off(anon, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("COMPASS_OPEN_SIGNUP", "false")
    r = anon.get("/signup")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Leaving
# --------------------------------------------------------------------------


def test_deleting_an_account_erases_every_table_it_touches(session, user):
    """The right to erasure is why open registration is defensible at all, so
    the cascade is asserted directly rather than through a route that might
    quietly fail to create a row.
    """
    person = auth.register(session, email="leaver@example.com",
                           password="a-long-passphrase")
    pid = person.id

    posting = JobPosting(user_id=pid, company="Acme", title="Analyst",
                         jd_text="Requirements\n- SQL\n")
    session.add(posting)
    session.flush()
    session.add_all([
        CareerProfile(user_id=pid, full_name="A Leaver"),
        ResumeVariant(user_id=pid, label="cv", content_md="x", raw_text="x",
                      ats_score=50.0),
        Application(user_id=pid, job_posting_id=posting.id),
        LLMCache(key="k-leaver", user_id=pid, prompt_name="p", prompt_version="1",
                 model="m", response_json="{}"),
    ])
    source = JobSource(user_id=pid, ats="greenhouse", board_token="acme")
    session.add(source)
    session.flush()
    session.add(DiscoveredJob(user_id=pid, source_id=source.id, external_id="1",
                              title="Role", jd_text="x"))
    session.commit()

    tables = (CareerProfile, ResumeVariant, Application, JobPosting, JobSource,
              DiscoveredJob, LLMCache)
    before = {t.__name__: session.scalar(
        select(func.count()).select_from(t).where(t.user_id == pid)) for t in tables}
    assert all(n >= 1 for n in before.values()), before

    auth.delete_account(session, person)

    after = {t.__name__: session.scalar(
        select(func.count()).select_from(t).where(t.user_id == pid)) for t in tables}
    assert all(n == 0 for n in after.values()), after
    assert session.get(User, pid) is None


def test_deleting_an_account_removes_its_uploaded_files(session, user, tmp_path):
    """Files live on disk, not in a table, so the cascade does not reach them."""
    person = auth.register(session, email="files@example.com",
                           password="a-long-passphrase")
    upload = tmp_path / "cv.txt"
    upload.write_text("a resume", encoding="utf-8")
    session.add(ResumeVariant(
        user_id=person.id, label="cv", content_md="x", raw_text="x",
        ats_score=50.0, stored_path=str(upload),
    ))
    session.commit()

    assert upload.exists()
    auth.delete_account(session, person)
    assert not upload.exists()


def test_deletion_leaves_other_accounts_alone(session, user):
    keeper = auth.register(session, email="keeper@example.com",
                           password="a-long-passphrase")
    leaver = auth.register(session, email="goer@example.com",
                           password="a-long-passphrase")
    session.add_all([
        CareerProfile(user_id=keeper.id, full_name="Keeper"),
        CareerProfile(user_id=leaver.id, full_name="Goer"),
    ])
    session.commit()

    auth.delete_account(session, leaver)

    assert session.get(User, keeper.id) is not None
    remaining = list(session.scalars(select(CareerProfile)))
    assert len(remaining) == 1
    assert remaining[0].full_name == "Keeper"
    # And the owner from the fixture.
    assert session.get(User, user.id) is not None


def test_the_route_requires_the_email_typed_exactly(anon):
    """A one-click irreversible delete on a page someone is skimming is a trap."""
    created = anon.post("/signup", data={
        "email": "typed@example.com", "password": "a-long-enough-passphrase",
        "confirm": "a-long-enough-passphrase", "accept": "true"})
    assert created.headers["location"] == "/profile"

    anon.post("/account/delete", data={"confirm_email": "wrong@example.com"})

    # The account survived, which is the point - asserted on state rather than
    # on a flash whose rendering location depends on the redirect chain.
    from sqlalchemy import select

    from app.db import SessionLocal
    check = SessionLocal()
    try:
        assert check.scalar(
            select(User).where(User.email == "typed@example.com")
        ) is not None
    finally:
        check.close()
    assert anon.get("/profile").status_code == 200  # still signed in


def test_the_admin_account_cannot_delete_itself(client):
    """It would strand the instance: no admin, and /setup does not reopen."""
    r = client.post("/account/delete",
                    data={"confirm_email": client.owner.email},
                    follow_redirects=True)
    assert "cannot be deleted" in r.text
    assert client.get("/settings").status_code == 200


def test_settings_offers_deletion_to_a_registered_user_only(client, session):
    """The owner sees no delete form, because the route refuses them anyway and
    a button that always errors is worse than no button."""
    assert "Delete my account and data" not in client.get("/settings").text
