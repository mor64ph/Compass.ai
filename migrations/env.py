"""Alembic environment.

The database URL comes from `app.config`, never from alembic.ini. A URL in the
ini file would be a second source of truth — and on a deployment it would be the
wrong one, because the real value is an environment variable holding a password
that must not be in the repository.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Only as a fallback. A URL already on the config wins, because the caller set it
# on purpose — `app.db.init_db` passes the running application's, and a test
# passes a scratch database. Overriding unconditionally made both impossible:
# every migration ran against whatever COMPASS_DB_URL happened to be, so a test
# pointed at a temporary file silently migrated the wrong database and then found
# no tables in the one it was inspecting.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().compass_db_url)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate from proposing a drop for tables it does not own.

    `alembic_version` is Alembic's own bookkeeping; without this, a generated
    migration would cheerfully include a DROP TABLE for it.
    """
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        # SQLite cannot ALTER a column in place, so a change to an existing one
        # has to be done by building a new table and copying. Alembic can do
        # that automatically, but only if asked.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
