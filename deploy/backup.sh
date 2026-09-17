#!/usr/bin/env bash
# Back up Compass's database and uploads.
#
#   ./deploy/backup.sh              -> ./backups/
#   ./deploy/backup.sh /mnt/backups -> somewhere else
#
# Uses SQLite's own `.backup` rather than `cp`. With WAL enabled - which it is -
# a plain copy of compass.db taken while a write is in flight produces a file
# that is missing whatever is still in the -wal, and you find out when you try to
# restore it. `.backup` takes a consistent snapshot of a live database.
#
# Add to crontab on the server:
#   0 3 * * * cd /opt/compass && ./deploy/backup.sh >> /var/log/compass-backup.log 2>&1

set -euo pipefail

DEST="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$DEST"

if ! docker compose ps --status running --services 2>/dev/null | grep -qx app; then
    echo "compass app container is not running - nothing to back up" >&2
    exit 1
fi

echo "[$STAMP] backing up to $DEST"

# --- database -------------------------------------------------------------
# sqlite3 is not in the runtime image, so use Python's bundled module. The
# backup API is the same thing sqlite3's .backup command calls.
docker compose exec -T app python -c "
import sqlite3, sys
src = sqlite3.connect('file:/data/compass.db?mode=ro', uri=True)
dst = sqlite3.connect('/tmp/backup.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
" >/dev/null

docker compose cp app:/tmp/backup.db "$DEST/compass-$STAMP.db"
docker compose exec -T app rm -f /tmp/backup.db
gzip -f "$DEST/compass-$STAMP.db"
echo "  database: $DEST/compass-$STAMP.db.gz"

# --- uploads and exports --------------------------------------------------
docker compose exec -T app tar -cf - -C /app/data . \
    | gzip > "$DEST/compass-files-$STAMP.tar.gz"
echo "  files:    $DEST/compass-files-$STAMP.tar.gz"

# --- verify ---------------------------------------------------------------
# A backup nobody has restored is a hypothesis. This at least proves the file is
# a readable SQLite database with the expected tables in it.
TABLES=$(gzip -dc "$DEST/compass-$STAMP.db.gz" > /tmp/verify.db \
    && python3 -c "
import sqlite3
c = sqlite3.connect('/tmp/verify.db')
names = {r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}
required = {'user', 'career_profile', 'application', 'job_posting'}
missing = required - names
print('MISSING: ' + ', '.join(sorted(missing)) if missing else len(names))
" ; rm -f /tmp/verify.db)

case "$TABLES" in
    MISSING*) echo "  VERIFY FAILED - $TABLES" >&2; exit 1 ;;
    *)        echo "  verified: $TABLES tables readable" ;;
esac

# --- prune ----------------------------------------------------------------
find "$DEST" -name 'compass-*.gz' -type f -mtime "+$KEEP_DAYS" -print -delete \
    | sed 's/^/  pruned: /' || true

echo "[$STAMP] done"
