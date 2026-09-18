"""Migrations, and the three database states `init_db` has to cope with.

The interesting case is the middle one. This project ran `create_all` for its
whole life before Alembic existed, and there is a live deployment carrying a
schema built that way. A migration tool that only works on a fresh database
would be useless exactly where it is needed.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def scratch_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'scratch.db').as_posix()}"


def _alembic_config(url: str):
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _revision_of(url: str) -> str | None:
    from alembic.runtime.migration import MigrationContext

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# The migration scripts themselves
# --------------------------------------------------------------------------


def test_there_is_exactly_one_head():
    """Two heads means two people generated a migration from the same parent and
    the next upgrade fails with a message that does not explain itself."""
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(
        _alembic_config("sqlite://")
    ).get_heads()
    assert len(heads) == 1, f"expected one head, found {heads}"


def test_the_baseline_revision_named_in_db_py_exists():
    """`init_db` stamps a pre-Alembic database at this exact revision. A typo
    here would be a startup crash on the one deployment that needs the stamp."""
    from alembic.script import ScriptDirectory

    from app.db import BASELINE_REVISION

    scripts = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert scripts.get_revision(BASELINE_REVISION) is not None


def test_every_migration_is_reversible(scratch_url):
    """Not because downgrades get run in anger, but because a migration with no
    downgrade is usually one written without thinking about what it changes."""
    from alembic import command

    config = _alembic_config(scratch_url)
    command.upgrade(config, "head")
    at_head = _revision_of(scratch_url)
    assert at_head is not None

    command.downgrade(config, "base")
    assert _revision_of(scratch_url) is None

    command.upgrade(config, "head")
    assert _revision_of(scratch_url) == at_head


def test_migrations_produce_the_same_schema_as_the_models(scratch_url):
    """The check that actually matters: after upgrading, autogenerate must find
    nothing left to do. If it finds something, the models and the migrations
    have drifted and the next deploy writes to columns that are not there.
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.models import Base

    command.upgrade(_alembic_config(scratch_url), "head")

    engine = create_engine(scratch_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    # alembic_version is Alembic's own table and is not in our metadata.
    real = [d for d in diff if "alembic_version" not in str(d)]
    assert not real, f"models and migrations have drifted:\n  {real}"


# --------------------------------------------------------------------------
# The three states init_db has to handle
# --------------------------------------------------------------------------


def test_an_empty_database_gets_the_full_schema(scratch_url, monkeypatch):
    from alembic import command

    command.upgrade(_alembic_config(scratch_url), "head")

    engine = create_engine(scratch_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "alembic_version" in tables
    assert {"user", "career_profile", "application", "resume_variant"} <= tables


def _init_db_against(url: str):
    """Run the real `init_db` against an arbitrary database.

    `app.db` resolves its engine at import time from settings, so pointing it
    elsewhere means reloading the module with the environment changed. Worth the
    awkwardness: the alternative is testing a copy of the logic rather than the
    logic.
    """
    import importlib
    import os

    import app.db as db_module
    from app.config import get_settings

    previous = os.environ.get("COMPASS_DB_URL")
    os.environ["COMPASS_DB_URL"] = url
    get_settings.cache_clear()
    try:
        reloaded = importlib.reload(db_module)
        reloaded.init_db()
    finally:
        if previous is None:
            os.environ.pop("COMPASS_DB_URL", None)
        else:
            os.environ["COMPASS_DB_URL"] = previous
        get_settings.cache_clear()
        importlib.reload(db_module)


def test_a_pre_alembic_database_matching_the_models_is_stamped_at_head(scratch_url):
    """A schema built by the *current* create_all already has every column the
    migrations would add. Stamping the baseline here would make the next upgrade
    try to add `stored_bytes` to a table that already has it - which is exactly
    what happened before init_db learned to check.
    """
    from app.models import Base

    engine = create_engine(scratch_url)
    Base.metadata.create_all(bind=engine)
    before = set(inspect(engine).get_table_names())
    engine.dispose()
    assert "alembic_version" not in before

    _init_db_against(scratch_url)   # must not raise

    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()
    assert _revision_of(scratch_url) == head

    engine = create_engine(scratch_url)
    try:
        assert before <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_a_pre_alembic_database_behind_the_models_is_stamped_at_the_baseline(scratch_url):
    """The live deployment's case: tables created before `stored_bytes` existed.
    That one does need the baseline stamp and then a real upgrade.
    """
    from app.db import BASELINE_REVISION
    from app.models import Base

    # Build the schema, then remove the column a later migration adds, so the
    # database looks like it was created by the older build.
    engine = create_engine(scratch_url)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE resume_variant DROP COLUMN stored_bytes"))
    columns = {c["name"] for c in inspect(engine).get_columns("resume_variant")}
    engine.dispose()
    assert "stored_bytes" not in columns

    _init_db_against(scratch_url)   # stamps the baseline, then upgrades

    engine = create_engine(scratch_url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("resume_variant")}
    finally:
        engine.dispose()
    assert "stored_bytes" in columns, "the pending migration was not applied"
    assert _revision_of(scratch_url) != BASELINE_REVISION


def test_upgrading_twice_is_a_no_op(scratch_url):
    from alembic import command

    config = _alembic_config(scratch_url)
    command.upgrade(config, "head")
    first = _revision_of(scratch_url)
    command.upgrade(config, "head")
    assert _revision_of(scratch_url) == first


# --------------------------------------------------------------------------
# The column the migration added
# --------------------------------------------------------------------------


def test_uploaded_bytes_are_kept_so_a_layout_recheck_survives(session, user):
    """`stored_path` points at a disk that a free host wipes on restart. The
    extracted text survives regardless, but the ATS checker's layout rules need
    the real file - and "why does re-check report less than it did yesterday" is
    a bad thing to have to explain.
    """
    import pathlib as _pathlib

    from app.services import resume_service

    variant = resume_service.ingest(
        session, user.id,
        content=b"Jane Doe\njane@example.com\n+91 98765 43210\n\n# Experience\n"
                b"- Cut refresh from 42 min to 9 min\n",
        filename="cv.md", label="bytes probe",
    )
    first_score = variant.ats_score
    stored = _pathlib.Path(variant.stored_path)

    assert variant.stored_bytes, "the upload's bytes were not kept"
    assert stored.is_file()

    # The restart.
    stored.unlink()
    assert not stored.is_file()

    report = resume_service.recheck(session, variant)
    assert report.score == first_score, "re-check degraded once the file was gone"
    # And the cheap path is available again for next time.
    assert stored.is_file(), "the local copy was not restored from the database"


def test_a_variant_with_no_file_at_all_still_rechecks(session, user):
    """A profile-generated variant never had a file, so the text tier is the
    only one available and must not raise."""
    from app.services import profile_service, resume_service
    from app.schemas import (LLMAspiration, LLMCareerProfile, LLMExperience,
                             LLMSkill)

    profile_service.upsert_from_payload(session, user.id, LLMCareerProfile(
        full_name="No File", email="n@example.com", phone="+91 98765 43210",
        location="Bangalore", links=[], headline="Analyst", summary="s",
        skills=[LLMSkill(name="SQL", category="hard", proficiency="strong", years=3.0)],
        certifications=[],
        experiences=[LLMExperience(
            title="Analyst", company="Acme", location="Bangalore",
            start_date="2022-01", end_date="", is_current=True,
            responsibilities=["r"], achievements=["Cut a report from 8h to 20m"],
            scope_change_note="")],
        education=[], projects=[],
        aspiration=LLMAspiration(
            target_titles=["Analyst"], target_industries=[], target_companies=[],
            locations=[], remote_preference="any", comp_min=0, comp_max=0,
            comp_currency="INR", non_negotiables=[]),
        unresolved_questions=[],
    ))
    variant = resume_service.create_from_profile(session, user.id, label="generated")
    assert not variant.stored_bytes
    report = resume_service.recheck(session, variant)
    assert report.score >= 0
