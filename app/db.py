"""Engine + session plumbing, for SQLite on a local disk or Postgres over a network.

SQLite is the default and is right for a single machine: one file, no server,
trivially backed up. It stops being right the moment the host has no persistent
disk — a free-tier service restarts and the file is gone, taking every account
with it. `COMPASS_DB_URL` pointed at a managed Postgres is the answer there.

Nothing above this module changes. Every column type in `app/models.py` is
portable, there is no raw SQL outside the SQLite pragmas below, and the whole
schema compiles for the Postgres dialect — which a test asserts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

# The first migration, which creates the whole schema. Named here so a database
# built by the old create_all path can be stamped at it instead of replaying a
# migration that would fail on tables that already exist.
BASELINE_REVISION = "bdd7c2231e7c"

_settings = get_settings()
_url = _settings.compass_db_url
_is_sqlite = _url.startswith("sqlite")

if _is_sqlite:
    # SQLite only: the connection is used from more than one thread because
    # FastAPI runs sync endpoints in a thread pool.
    engine = create_engine(
        _url, connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()
else:
    # Managed Postgres, which is a network service that goes away.
    #
    #   pool_pre_ping   Neon and friends scale compute to zero after a few
    #                   minutes idle, and a pooled connection held across that
    #                   is dead. Without this the first request after a quiet
    #                   spell fails with a closed-connection error rather than
    #                   transparently reconnecting.
    #   pool_recycle    Managed providers also drop long-lived connections on
    #                   their own schedule; recycling under that window means we
    #                   never hand out one they have already closed.
    #   small pool      One uvicorn worker on a free instance. A large pool just
    #                   holds connections the provider counts against a quota.
    engine = create_engine(
        _url,
        future=True,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=3,
        max_overflow=2,
    )


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Bring the database up to the current schema, whatever state it is in.

    Three cases, because a deployment that has been running since before
    migrations existed is a real case and not a hypothetical one:

    * **Empty database** — run every migration from scratch.
    * **Tables present, no `alembic_version`** — the schema was built by the old
      `create_all` path. Stamp it at the baseline revision rather than replaying
      migrations that would fail on tables that already exist, then upgrade.
    * **Already stamped** — upgrade to head; a no-op when there is nothing new.

    Doing this here rather than as a separate deploy step is deliberate. A free
    host gives one process and no shell, so "remember to run `alembic upgrade`"
    is a step that will be forgotten exactly once, on the deploy that needed it.
    """
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    from app import models  # noqa: F401  (registers mappers)

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", _url)

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    with engine.connect() as connection:
        stamped = MigrationContext.configure(connection).get_current_revision()

    if stamped is None and existing - {"alembic_version"}:
        # A pre-Alembic database, built by the `create_all` this replaced. Which
        # revision it corresponds to cannot be assumed: a schema created by an
        # older build of the app matches the baseline, while one created by the
        # current build already has every later column. Stamping the baseline in
        # the second case makes the next upgrade try to add a column that is
        # already there, so ask the database which it is.
        from alembic.autogenerate import compare_metadata

        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            drift = [
                change
                for change in compare_metadata(context, models.Base.metadata)
                if "alembic_version" not in str(change)
            ]

        target = BASELINE_REVISION if drift else "head"
        logger.info(
            "Existing schema with no migration history - stamping %s (%d "
            "difference(s) from the models)", target, len(drift),
        )
        # `stamp` writes the version row and runs no DDL, which is the point:
        # the tables are already there.
        command.stamp(config, target)

    command.upgrade(config, "head")
    logger.info("Schema is at head")


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for use outside a request (CLI, sync jobs, tests)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
