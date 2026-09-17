from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Point the app at a throwaway database before anything imports app.config, so a
# test run can never touch the developer's real compass.db.
_TMP_DB = Path(tempfile.gettempdir()) / "compass_test.db"
os.environ["COMPASS_DB_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.setdefault("COMPASS_SECRET_KEY", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
# Never spend 30s importing torch in a unit-test run.
os.environ["COMPASS_PRELOAD_EMBEDDINGS"] = "false"


@pytest.fixture
def session():
    """A clean database per test."""
    from app.db import SessionLocal, engine
    from app.models import Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def user(session):
    """The primary test account (owner, uncapped)."""
    from app.auth import create_owner

    return create_owner(
        session, email="owner@example.com", password="a-long-enough-passphrase",
        display_name="Owner",
    )


@pytest.fixture
def other_user(session, user):
    """A second, unrelated account - the one that must never see `user`'s data."""
    from app.auth import create_invite, accept_invite

    invited = create_invite(session, email="other@example.com", invited_by=user)
    accept_invite(session, invited, "another-long-passphrase")
    return invited


@pytest.fixture
def client(session):
    """A TestClient with a signed-in owner, and no redirect following."""
    from fastapi.testclient import TestClient

    from app.auth import create_owner
    from app.main import app

    owner = create_owner(
        session, email="owner@example.com", password="a-long-enough-passphrase",
        display_name="Owner",
    )
    test_client = TestClient(app, follow_redirects=False)
    test_client.post(
        "/login",
        data={"email": owner.email, "password": "a-long-enough-passphrase", "next": "/"},
    )
    test_client.owner = owner  # type: ignore[attr-defined]
    return test_client
