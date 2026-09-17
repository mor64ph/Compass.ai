"""Auth and tenant-isolation tests.

This is the suite that earns Phase 2 the right to be shared with anyone. It
tests through the HTTP layer rather than the service layer, because the failure
mode being guarded against is *a route that forgot to scope its query* - and a
service-level test would happily pass while the route leaked.

Two accounts exist throughout: `owner` and `intruder`. Anything `owner` creates,
`intruder` must be unable to read, edit or delete - and must not be able to tell
whether it exists at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.models import Application, CareerProfile, JobPosting, ResumeVariant, User
from app.services import embeddings

JD_TEXT = """\
Senior Power BI Developer

Requirements
- 5+ years with Power BI and strong DAX
- Dimensional modelling and star schema design
- Advanced SQL and query optimisation

Responsibilities
- Own the semantic model
- Mentor junior analysts
"""

OWNER_PASSWORD = "owner-long-enough-passphrase"
INTRUDER_PASSWORD = "intruder-long-enough-pass"


@pytest.fixture(autouse=True)
def lexical_backend(monkeypatch):
    backend = embeddings.LexicalBackend()
    monkeypatch.setattr(embeddings, "get_backend", lambda: backend)
    return backend


@pytest.fixture
def accounts(session):
    owner = auth.create_owner(
        session, email="owner@example.com", password=OWNER_PASSWORD, display_name="Owner"
    )
    invited = auth.create_invite(session, email="intruder@example.com", invited_by=owner)
    auth.accept_invite(session, invited, INTRUDER_PASSWORD)
    return owner, invited


def sign_in(email: str, password: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/login", data={"email": email, "password": password, "next": "/"}
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def owner_client(accounts):
    return sign_in("owner@example.com", OWNER_PASSWORD)


@pytest.fixture
def intruder_client(accounts):
    return sign_in("intruder@example.com", INTRUDER_PASSWORD)


# --------------------------------------------------------------------------
# The wall
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/profile", "/resumes", "/applications", "/tracker", "/settings"],
)
def test_every_page_requires_a_login(session, path):
    client = TestClient(app, follow_redirects=False)
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_first_run_offers_setup_then_closes_it(session):
    client = TestClient(app, follow_redirects=False)
    assert client.get("/login").headers.get("location") == "/setup"
    assert client.get("/setup").status_code == 200

    created = client.post(
        "/setup",
        data={
            "email": "first@example.com",
            "display_name": "First",
            "password": "a-long-enough-passphrase",
            "confirm": "a-long-enough-passphrase",
        },
    )
    assert created.status_code == 303
    assert client.get("/").status_code == 200

    # Setup must never work twice - otherwise it is a privilege-escalation route.
    assert client.get("/setup").headers.get("location") == "/login"
    second = client.post(
        "/setup",
        data={
            "email": "sneaky@example.com",
            "password": "another-long-passphrase",
            "confirm": "another-long-passphrase",
        },
    )
    assert second.status_code == 303
    assert session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(User.id))
    ) == 1


def test_login_does_not_reveal_whether_an_account_exists(accounts):
    client = TestClient(app, follow_redirects=False)
    wrong_password = client.post(
        "/login", data={"email": "owner@example.com", "password": "nope", "next": "/"}
    )
    unknown_email = client.post(
        "/login", data={"email": "nobody@example.com", "password": "nope", "next": "/"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 200
    assert "incorrect" in wrong_password.text.lower()
    assert "incorrect" in unknown_email.text.lower()


@pytest.mark.parametrize(
    "target", ["https://evil.example/steal", "//evil.example", "http://evil.example"]
)
def test_login_refuses_an_external_redirect(accounts, target):
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/login",
        data={"email": "owner@example.com", "password": OWNER_PASSWORD, "next": target},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_deactivated_user_cannot_use_an_existing_session(session, accounts):
    owner, intruder = accounts
    client = sign_in("intruder@example.com", INTRUDER_PASSWORD)
    assert client.get("/").status_code == 200

    intruder.is_active = False
    session.commit()

    # The cookie is still valid, but the account is not.
    assert client.get("/").status_code == 303


# --------------------------------------------------------------------------
# Cross-account reads
# --------------------------------------------------------------------------


def _create_application(client: TestClient) -> int:
    response = client.post(
        "/applications",
        data={
            "company": "Acme Manufacturing",
            "title": "Senior Power BI Developer",
            "location": "Bangalore",
            "url": "",
            "jd_text": JD_TEXT,
        },
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def _create_variant(client: TestClient) -> int:
    response = client.post(
        "/resumes/upload",
        files={"file": ("cv.txt", b"Jane Doe\njane@example.com\n+91 98765 43210\n" * 30, "text/plain")},
        data={"label": "Private CV", "flavor": "base"},
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_applications_list_shows_only_your_own(owner_client, intruder_client):
    _create_application(owner_client)
    assert "Acme Manufacturing" in owner_client.get("/applications").text
    assert "Acme Manufacturing" not in intruder_client.get("/applications").text


def test_another_users_application_is_a_404_not_a_403(owner_client, intruder_client):
    application_id = _create_application(owner_client)
    response = intruder_client.get(f"/applications/{application_id}")
    # 404 rather than 403: telling an intruder that a record exists but is not
    # theirs still leaks which ids are real.
    assert response.status_code == 404


def test_another_users_resume_cannot_be_read_or_exported(owner_client, intruder_client):
    variant_id = _create_variant(owner_client)
    assert owner_client.get(f"/resumes/{variant_id}").status_code == 200
    assert intruder_client.get(f"/resumes/{variant_id}").status_code == 404
    assert intruder_client.get(f"/resumes/{variant_id}/export.pdf").status_code == 404


def test_resume_list_is_isolated(owner_client, intruder_client):
    _create_variant(owner_client)
    assert "Private CV" in owner_client.get("/resumes").text
    assert "Private CV" not in intruder_client.get("/resumes").text


def test_profile_is_isolated(owner_client, intruder_client):
    owner_client.post(
        "/profile/basics",
        data={
            "full_name": "Hrisit Biswas",
            "email": "hrisit@example.com",
            "phone": "+91 98765 43210",
            "location": "Bangalore",
            "headline": "Senior Power BI Developer",
            "summary": "Owns the semantic layer.",
            "links": "",
            "intake_notes": "",
        },
    )
    assert "Hrisit Biswas" in owner_client.get("/profile").text
    assert "Hrisit Biswas" not in intruder_client.get("/profile").text


def test_intake_transcript_is_isolated(session, accounts, owner_client, intruder_client):
    from app.models import IntakeMessage
    from app.services import intake

    owner, intruder = accounts
    session.add(IntakeMessage(user_id=owner.id, role="user", content="my secret career story"))
    session.commit()

    assert len(intake.transcript(session, owner.id)) == 1
    assert intake.transcript(session, intruder.id) == []
    assert "my secret career story" not in intruder_client.get("/profile").text


# --------------------------------------------------------------------------
# Cross-account writes
# --------------------------------------------------------------------------


def test_another_users_application_cannot_be_mutated(owner_client, intruder_client):
    application_id = _create_application(owner_client)
    for path, payload in (
        (f"/applications/{application_id}/stage", {"stage": "applied"}),
        (f"/applications/{application_id}/note", {"summary": "injected"}),
        (f"/applications/{application_id}/edit", {"cover_letter_text": "injected"}),
        (f"/applications/{application_id}/ready", {"override_reason": "x"}),
        (f"/applications/{application_id}/delete", {}),
        (f"/tracker/{application_id}/stage", {"stage": "closed"}),
    ):
        response = intruder_client.post(path, data=payload)
        assert response.status_code == 404, f"{path} returned {response.status_code}"

    # And the record is untouched.
    assert owner_client.get(f"/applications/{application_id}").status_code == 200


def test_another_users_resume_cannot_be_mutated(owner_client, intruder_client):
    variant_id = _create_variant(owner_client)
    for path, payload in (
        (f"/resumes/{variant_id}/content", {"content_md": "wiped"}),
        (f"/resumes/{variant_id}/master", {}),
        (f"/resumes/{variant_id}/recheck", {}),
        (f"/resumes/{variant_id}/delete", {}),
    ):
        assert intruder_client.post(path, data=payload).status_code == 404, path
    assert owner_client.get(f"/resumes/{variant_id}").status_code == 200


def test_cannot_attach_another_users_resume_to_your_application(
    owner_client, intruder_client
):
    """The variant id arrives in a form field rather than the path, which is
    exactly the sort of parameter that gets trusted by accident."""
    victim_variant = _create_variant(owner_client)
    intruder_application = _create_application(intruder_client)

    response = intruder_client.post(
        f"/applications/{intruder_application}/edit",
        data={
            "tailored_resume_md": "",
            "cover_letter_text": "",
            "notes": "",
            "resume_variant_id": str(victim_variant),
        },
    )
    assert response.status_code == 404


def test_profile_children_of_another_user_cannot_be_deleted(
    session, accounts, owner_client, intruder_client
):
    owner, intruder = accounts
    owner_client.post(
        "/profile/experience",
        data={
            "title": "Senior Power BI Analyst",
            "company": "Accenture",
            "location": "",
            "start_date": "2023-04",
            "end_date": "",
            "achievements": "Cut refresh from 42 to 9 minutes",
            "responsibilities": "",
            "scope_change_note": "",
        },
    )
    profile = session.scalars(
        __import__("sqlalchemy").select(CareerProfile).where(CareerProfile.user_id == owner.id)
    ).first()
    assert profile is not None and len(profile.experiences) == 1
    experience_id = profile.experiences[0].id

    # These children have no user_id of their own - the route must join to the
    # profile to establish ownership.
    intruder_client.post(f"/profile/delete/experience/{experience_id}")
    session.expire_all()
    still_there = session.scalars(
        __import__("sqlalchemy").select(CareerProfile).where(CareerProfile.user_id == owner.id)
    ).first()
    assert len(still_there.experiences) == 1


def test_prep_brief_of_another_user_is_inaccessible(
    session, accounts, owner_client, intruder_client
):
    from app.models import InterviewPrepBrief

    application_id = _create_application(owner_client)
    brief = InterviewPrepBrief(application_id=application_id, company_snapshot="secret")
    session.add(brief)
    session.commit()
    session.refresh(brief)

    assert intruder_client.post(
        f"/prep/session/{brief.id}/turn", data={"message": "start"}
    ).status_code == 404
    assert intruder_client.post(f"/prep/session/{brief.id}/reset").status_code == 404
    assert intruder_client.get(f"/prep/{application_id}").status_code == 404


# --------------------------------------------------------------------------
# Admin surface
# --------------------------------------------------------------------------


def test_non_admin_cannot_invite_or_change_budgets(accounts, intruder_client):
    owner, intruder = accounts
    assert intruder.is_admin is False
    assert intruder_client.post(
        "/settings/invite", data={"email": "x@example.com", "monthly_budget_usd": "5"}
    ).status_code == 403
    assert intruder_client.post(
        f"/settings/users/{owner.id}/budget", data={"monthly_budget_usd": "999"}
    ).status_code == 403
    assert intruder_client.post(
        f"/settings/users/{owner.id}/deactivate"
    ).status_code == 403


def test_admin_can_invite(session, owner_client):
    response = owner_client.post(
        "/settings/invite",
        data={"email": "friend@example.com", "display_name": "Friend", "monthly_budget_usd": "3"},
    )
    assert response.status_code == 303
    invited = session.scalars(
        __import__("sqlalchemy").select(User).where(User.email == "friend@example.com")
    ).first()
    assert invited is not None
    assert invited.invite_token
    assert invited.monthly_budget_usd == 3.0
    assert not invited.has_password  # not usable until they accept


def test_admin_cannot_deactivate_themselves(session, accounts, owner_client):
    owner, _ = accounts
    owner_client.post(f"/settings/users/{owner.id}/deactivate")
    session.expire_all()
    assert session.get(User, owner.id).is_active is True


def test_settings_hides_the_people_section_from_non_admins(owner_client, intruder_client):
    assert "Invite someone" in owner_client.get("/settings").text
    assert "Invite someone" not in intruder_client.get("/settings").text


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------


def test_invite_token_is_single_use(session, accounts, owner_client):
    invited = auth.create_invite(session, email="friend@example.com")
    token = invited.invite_token

    client = TestClient(app, follow_redirects=False)
    assert client.get(f"/invite/{token}").status_code == 200
    accepted = client.post(
        f"/invite/{token}",
        data={
            "display_name": "Friend",
            "password": "friend-long-enough-pass",
            "confirm": "friend-long-enough-pass",
        },
    )
    assert accepted.status_code == 303

    # The token is spent.
    reused = TestClient(app, follow_redirects=False).get(f"/invite/{token}")
    assert "isn't valid" in reused.text


def test_expired_invite_is_refused(session):
    from datetime import timedelta

    from app.models import utcnow

    invited = auth.create_invite(session, email="late@example.com")
    invited.invite_expires_at = utcnow() - timedelta(days=1)
    session.commit()

    assert auth.user_for_invite(session, invited.invite_token) is None
    client = TestClient(app, follow_redirects=False)
    assert "isn't valid" in client.get(f"/invite/{invited.invite_token}").text


def test_unknown_invite_token_is_refused(session, accounts):
    client = TestClient(app, follow_redirects=False)
    assert "isn't valid" in client.get("/invite/not-a-real-token").text


def test_invite_rejects_a_weak_password(session, accounts):
    invited = auth.create_invite(session, email="weak@example.com")
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        f"/invite/{invited.invite_token}",
        data={"display_name": "", "password": "short", "confirm": "short"},
    )
    assert response.status_code == 200
    session.expire_all()
    assert not session.get(User, invited.id).has_password


# --------------------------------------------------------------------------
# New accounts start empty
# --------------------------------------------------------------------------


def test_a_new_account_sees_an_empty_instance(session, accounts, owner_client, intruder_client):
    _create_application(owner_client)
    _create_variant(owner_client)

    dashboard = intruder_client.get("/")
    assert dashboard.status_code == 200
    assert "Acme Manufacturing" not in dashboard.text

    import sqlalchemy

    owner, intruder = accounts
    for model in (Application, JobPosting, ResumeVariant):
        mine = session.scalars(
            sqlalchemy.select(model).where(model.user_id == intruder.id)
        ).all()
        assert mine == [], f"{model.__name__} leaked into the new account"
