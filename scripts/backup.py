"""Back up and restore Compass's data, whatever database it is using.

    .venv\\Scripts\\python.exe scripts\\backup.py                 -> ./backups/
    .venv\\Scripts\\python.exe scripts\\backup.py --out D:\\safe
    .venv\\Scripts\\python.exe scripts\\backup.py --restore backups\\x.json.gz

Why this exists when `deploy/backup.sh` already did
---------------------------------------------------
That script shells into a Docker container and calls SQLite's own `.backup`.
Both assumptions are wrong for the deployment that actually happened: there is
no container, and the database is a managed Postgres on another continent.

Why not `pg_dump`
-----------------
It is the right tool and it is not installed on the machine this has to run
from — Postgres client binaries are a separate download on Windows, and a backup
procedure nobody can execute is not a backup procedure. This reads through
SQLAlchemy instead, so it works against SQLite and Postgres with nothing beyond
what `requirements.txt` already installs.

The trade, stated plainly: this is a *logical* export of table rows, not a
byte-faithful dump. It does not capture indexes, sequences, or anything outside
the application's own tables — Alembic rebuilds all of that from
`migrations/`. Restore is therefore: create the schema with `init_db`, then load
these rows. For this application, at this scale, that is sufficient. For a
database with real operational history it would not be.

On Neon's free plan this matters more than it looks: point-in-time restore is a
**six-hour** window. Beyond that, the only copy is the one you took.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FORMAT_VERSION = 2


def _encode(value):
    """JSON cannot hold bytes, dates, or datetimes; tag them so restore can tell
    a real string from an encoded one."""
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    return value


def _decode(value):
    if isinstance(value, dict):
        if "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
    return value


def dump(out_dir: Path) -> Path:
    from sqlalchemy import select

    from app.db import engine, SessionLocal
    from app.models import Base

    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"compass-{stamp}.json.gz"

    payload = {
        "format_version": FORMAT_VERSION,
        "taken_at": now.isoformat(),
        "dialect": engine.dialect.name,
        "tables": {},
    }

    session = SessionLocal()
    try:
        # Insertion order, so a restore satisfies foreign keys without having to
        # defer constraints.
        for table in Base.metadata.sorted_tables:
            rows = []
            for row in session.execute(select(table)).mappings():
                rows.append({k: _encode(v) for k, v in row.items()})
            payload["tables"][table.name] = rows
    finally:
        session.close()

    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    counts = {name: len(rows) for name, rows in payload["tables"].items() if rows}
    size_kb = target.stat().st_size / 1024
    print(f"wrote {target}  ({size_kb:.0f} KB, {engine.dialect.name})")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {count:6}  {name}")
    if not counts:
        print("    (no rows - is COMPASS_DB_URL pointing where you think?)")
    return target


def verify(path: Path) -> bool:
    """Re-open the file and check it parses and holds the tables that matter.

    A backup nobody has read back is a hypothesis. This is the cheapest check
    that distinguishes a real backup from a gzipped error message.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"VERIFY FAILED: {path} is not readable JSON ({exc})")
        return False

    if payload.get("format_version") != FORMAT_VERSION:
        print(f"VERIFY FAILED: unexpected format_version "
              f"{payload.get('format_version')!r}")
        return False

    tables = payload.get("tables", {})
    required = {"user", "career_profile", "application", "job_posting"}
    missing = required - set(tables)
    if missing:
        print(f"VERIFY FAILED: missing tables {sorted(missing)}")
        return False

    print(f"verified: {len(tables)} tables, {sum(len(r) for r in tables.values())} rows")
    return True


def restore(path: Path, *, force: bool) -> None:
    from sqlalchemy import delete, insert, select

    from app.db import SessionLocal, engine, init_db
    from app.models import Base, User

    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    init_db()  # migrations bring the schema to head first

    session = SessionLocal()
    try:
        existing = session.scalar(select(User).limit(1))
        if existing is not None and not force:
            print(
                "REFUSING: the target database already has accounts in it.\n"
                "Restoring would delete them. Re-run with --force if that is "
                "what you want."
            )
            return

        # Reverse order for the delete so children go before parents.
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(delete(table))

        total = 0
        for table in Base.metadata.sorted_tables:
            rows = payload.get("tables", {}).get(table.name) or []
            if not rows:
                continue
            decoded = [{k: _decode(v) for k, v in row.items()} for row in rows]
            session.execute(insert(table), decoded)
            total += len(decoded)
        session.commit()
        print(f"restored {total} rows from {path}")

        if engine.dialect.name == "postgresql":
            # Rows carry their original ids, so every SERIAL sequence is now
            # behind the data and the next insert would collide. Postgres does
            # not fix this itself.
            _resync_sequences(session)
            print("re-synced Postgres id sequences")
    finally:
        session.close()


def _resync_sequences(session) -> None:
    from sqlalchemy import text

    from app.models import Base

    for table in Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not pk[0].autoincrement:
            continue
        column = pk[0].name
        session.execute(text(
            f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', '{column}'), "
            f"COALESCE((SELECT MAX({column}) FROM \"{table.name}\"), 1), true)"
        ))
    session.commit()


def prune(out_dir: Path, keep: int) -> None:
    backups = sorted(out_dir.glob("compass-*.json.gz"))
    for stale in backups[:-keep] if keep else []:
        stale.unlink()
        print(f"pruned {stale.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backups", help="where to write (default ./backups)")
    parser.add_argument("--keep", type=int, default=14, help="how many to retain; 0 keeps all")
    parser.add_argument("--restore", metavar="FILE", help="load a backup instead of taking one")
    parser.add_argument("--force", action="store_true",
                        help="with --restore, overwrite a database that has accounts")
    args = parser.parse_args()

    from app.config import get_settings

    url = get_settings().compass_db_url
    # Never print the password.
    shown = url.split("@")[-1] if "@" in url else url
    print(f"database: ...{shown}\n")

    if args.restore:
        restore(Path(args.restore), force=args.force)
        return 0

    out_dir = Path(args.out)
    written = dump(out_dir)
    if not verify(written):
        return 1
    prune(out_dir, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
