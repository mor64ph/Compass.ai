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

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

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
    """Create tables. Phase 1 has no migration tool - the schema is created
    on startup and evolved by hand until Phase 2 introduces Alembic."""
    from app import models  # noqa: F401  (registers mappers)

    models.Base.metadata.create_all(bind=engine)


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
