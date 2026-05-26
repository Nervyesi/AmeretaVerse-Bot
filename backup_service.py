"""
backup_service.py — Secure SQLite DB backup helpers.

Provides consistent point-in-time snapshots of the live production DB for:
  (a) owner-only on-demand download (api.py: /api/admin/backup/download), and
  (b) automatic weekly upload to R2 (cogs/backup.py).

Design notes:
  * Always uses sqlite3's online backup API (connection.backup()) — never copies
    the raw file. This yields a transactionally consistent image even while the
    bot is actively writing, avoiding corruption.
  * Reuses the app's existing DB path resolution (database.DB_PATH) and the
    existing R2 client (r2_client.get_r2_client / R2_BUCKET_NAME). No new deps.
"""
import os
import re
import uuid
import sqlite3
import tempfile
from datetime import datetime, timezone

from database import DB_PATH

# R2 layout / retention for automatic backups.
R2_BACKUP_PREFIX    = 'backups/'
R2_BACKUP_RETENTION = 8  # keep the newest N weekly backups; prune older ones

# Current (safe) key format carries a uuid suffix; the legacy format did not and
# was guessable on the public CDN. Used to identify old keys for one-off cleanup.
_NEW_KEY_RE = re.compile(r'^backups/ameretaverse-\d{8}-\d{6}-[0-9a-f]{32}\.db$')
_OLD_KEY_RE = re.compile(r'^backups/ameretaverse-\d{8}-\d{6}\.db$')


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def backup_filename() -> str:
    """Dated filename for an on-demand download, e.g.
    ameretaverse-backup-20260526-031200.db"""
    return f'ameretaverse-backup-{_timestamp()}.db'


def make_consistent_copy() -> str:
    """Create a consistent snapshot of the live DB using sqlite3's online backup
    API. Returns the path to a temp .db file the CALLER is responsible for
    deleting once it is no longer needed.

    Using connection.backup() (rather than copying DB_PATH on disk) guarantees a
    transactionally consistent image even while the bot holds the DB open and is
    writing to it.
    """
    fd, tmp_path = tempfile.mkstemp(prefix='avbackup-', suffix='.db')
    os.close(fd)
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return tmp_path


def upload_backup_to_r2() -> dict:
    """Make a consistent copy and upload it to R2 under backups/, then prune old
    backups beyond the newest R2_BACKUP_RETENTION.

    Returns {'key', 'size', 'pruned'}. Raises on upload failure — callers must
    wrap in try/except so a failure never crashes the bot.
    """
    from r2_client import get_r2_client, R2_BUCKET_NAME

    tmp_path = make_consistent_copy()
    try:
        # The avbot-assets bucket is served publicly via the CDN (R2_PUBLIC_URL),
        # so objects are reachable by key. A bare timestamp key would be brute-
        # forceable (only ~seconds of entropy over a week). Append a uuid4 so the
        # key carries 128 bits of entropy and cannot be guessed/enumerated. The
        # fixed-width timestamp stays first, so lexical sort == chronological for
        # retention pruning.
        key  = f'{R2_BACKUP_PREFIX}ameretaverse-{_timestamp()}-{uuid.uuid4().hex}.db'
        size = os.path.getsize(tmp_path)
        client = get_r2_client()
        with open(tmp_path, 'rb') as f:
            client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
                Body=f,
                ContentType='application/x-sqlite3',
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    pruned         = _prune_old_backups(keep=R2_BACKUP_RETENTION)
    legacy_deleted = cleanup_legacy_backups()
    return {'key': key, 'size': size, 'pruned': pruned, 'legacy_deleted': legacy_deleted}


def cleanup_legacy_backups() -> list:
    """One-off cleanup: delete old guessable-key backups (the legacy
    backups/ameretaverse-YYYYMMDD-HHMMSS.db format WITHOUT the uuid suffix).

    Safe by construction: only deletes keys matching the exact legacy format
    under the backups/ prefix, and never a key that also matches the current
    uuid format. Anything else in the bucket is untouched. Returns the deleted
    keys. Never raises — runs opportunistically on each backup."""
    try:
        from r2_client import get_r2_client, R2_BUCKET_NAME
        client = get_r2_client()
        resp = client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=R2_BACKUP_PREFIX)
        deleted = []
        for o in resp.get('Contents', []):
            key = o['Key']
            if _OLD_KEY_RE.match(key) and not _NEW_KEY_RE.match(key):
                client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                deleted.append(key)
        if deleted:
            print(f'[backup] cleanup_legacy_backups: deleted {len(deleted)} '
                  f'old-format backup(s): {deleted}')
        return deleted
    except Exception as e:
        print(f'[backup] cleanup_legacy_backups failed (non-fatal): {type(e).__name__}: {e}')
        return []


def _prune_old_backups(keep: int = R2_BACKUP_RETENTION) -> list:
    """Delete R2 backups beyond the newest `keep`. Returns the list of deleted
    keys. Never raises — pruning is best-effort and must not fail a backup."""
    try:
        from r2_client import get_r2_client, R2_BUCKET_NAME
        client = get_r2_client()
        resp = client.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=R2_BACKUP_PREFIX)
        objs = [o for o in resp.get('Contents', []) if o['Key'].endswith('.db')]
        # Keys embed a sortable UTC timestamp, so lexical sort == chronological.
        objs.sort(key=lambda o: o['Key'], reverse=True)
        deleted = []
        for o in objs[keep:]:
            client.delete_object(Bucket=R2_BUCKET_NAME, Key=o['Key'])
            deleted.append(o['Key'])
        return deleted
    except Exception as e:
        print(f'[backup] prune failed (non-fatal): {type(e).__name__}: {e}')
        return []
