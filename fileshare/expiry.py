"""File expiry (spec §14 E): tombstone every live file whose `expires_at` has passed.

A tombstone is exactly what DELETE /api/files/{ref} leaves: `deleted_at` set, the key material
blanked, the blob unlinked, the row (and so the FILE<n> id) kept and never reused.
"""
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from fileshare import bridge, clock
from fileshare.blobs import BlobStore
from fileshare.db import connect

log = logging.getLogger("fileshare.expiry")

# RETURNING landed in SQLite 3.35 (2021); below that we fall back to SELECT-then-DELETE inside the
# same BEGIN IMMEDIATE transaction, which is just as atomic, only an extra round trip.
_SQLITE_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35)

TTLS: dict[str, timedelta | None] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "never": None,
}
DEFAULT_TTL = "7d"
SWEEP_EVERY_S: float = 3600


def expires_at(ttl: str, start: datetime) -> str | None:
    delta = TTLS[ttl]
    return None if delta is None else clock.now_iso(start + delta)


def tombstone(conn: sqlite3.Connection, blobs: BlobStore, n: int, uuid_hex: str, ts: str) -> bool:
    """Tombstone one file, revoke its public links (§17) and drop its tags (§19). False if it was already deleted
    (a concurrent delete or sweep won)."""
    cur = conn.execute(
        "UPDATE files SET deleted_at = ?, wrapped_dek = '', enc_meta = ''"
        " WHERE n = ? AND deleted_at IS NULL",
        (ts, n))
    conn.execute("UPDATE links SET revoked_at = ? WHERE file_n = ? AND revoked_at IS NULL", (ts, n))
    conn.execute("DELETE FROM file_tags WHERE file_n = ?", (n,))   # §19: tags count live files only
    blobs.delete(uuid_hex)
    return cur.rowcount == 1


def expire_files(conn: sqlite3.Connection, blobs: BlobStore, now: datetime | None = None) -> int:
    """Tombstone every live file with expires_at <= now. Returns how many were expired."""
    now = clock.now() if now is None else now
    ts = clock.now_iso(now)
    due = conn.execute(
        "SELECT n, uuid FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?"
        " AND deleted_at IS NULL ORDER BY n",
        (ts,)).fetchall()
    count = sum(tombstone(conn, blobs, row["n"], row["uuid"], ts) for row in due)
    if count:
        log.info("expiry: tombstoned %d expired file(s)", count)
    return count


LEGACY_ARCHIVE_DAYS = 90       # Task 12: an archived legacy ticket stays readable this long, then is tombstoned


def expire_legacy_tickets(conn: sqlite3.Connection, now: datetime | None = None, bus=None) -> int:
    """Tombstone legacy tickets archived more than 90 days ago (same blanking as a delete). Mirrors are never
    touched. Returns how many."""
    from fileshare.tickets import tombstone_archived
    now = clock.now() if now is None else now
    count = tombstone_archived(conn, clock.now_iso(now - timedelta(days=LEGACY_ARCHIVE_DAYS)), clock.now_iso(now),
                               bus=bus)
    if count:
        log.info("expiry: tombstoned %d archived legacy ticket(s)", count)
    return count


UPLOAD_LINK_UNUSED_TTL = timedelta(days=7)     # an unused link, dead 7+ days: nobody is coming back for it
UPLOAD_LINK_PENDING_TTL = timedelta(days=30)   # an uploaded-but-never-adopted blob: give the owner a month
UPLOAD_LINK_RECEIVED_TTL = timedelta(days=30)  # the link row itself, once its file lives on independently
UPLOAD_LINK_DISCARDED_TTL = timedelta(days=7)  # a revoked pending upload (its blob is already gone)


def expire_upload_links(conn: sqlite3.Connection, blobs: BlobStore, now: datetime | None = None) -> int:
    """Purge dead upload_links rows (upload-links spec, Task 3). Returns how many rows were deleted.

    Four independent rules, each on rows past its own grace period:
    - unused (never claimed): gone once dead (expired or revoked) more than 7 days.
    - pending (claimed, not yet adopted by the owner): gone, blob and all, after 30 days.
    - received (adopted into a normal file): the link row is gone after 30 days; the file lives on.
    - discarded (a pending upload the owner revoked): gone after 7 days (its blob is already gone).
    """
    now = clock.now() if now is None else now
    unused_cutoff = clock.now_iso(now - UPLOAD_LINK_UNUSED_TTL)
    pending_cutoff = clock.now_iso(now - UPLOAD_LINK_PENDING_TTL)
    received_cutoff = clock.now_iso(now - UPLOAD_LINK_RECEIVED_TTL)
    discarded_cutoff = clock.now_iso(now - UPLOAD_LINK_DISCARDED_TTL)

    count = conn.execute(
        "DELETE FROM upload_links WHERE used_at IS NULL"
        " AND (expires_at < ? OR revoked_at < ?)",
        (unused_cutoff, unused_cutoff)).rowcount

    # The pending rule deletes rows *and* their blobs. An owner can still adopt a pending upload at
    # any moment (there is no cutoff on adoption itself), so the row deletion happens inside its own
    # BEGIN IMMEDIATE, and only the uuids it actually deleted (never one a concurrent adopt just
    # claimed) get their blobs removed, and only after COMMIT.
    pending_sql = "DELETE FROM upload_links WHERE file_uuid IS NOT NULL AND file_n IS NULL AND used_at < ?"
    deleted_blobs = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _SQLITE_HAS_RETURNING:
            rows = conn.execute(pending_sql + " RETURNING file_uuid", (pending_cutoff,)).fetchall()
            deleted_blobs = [r["file_uuid"] for r in rows]
        else:
            rows = conn.execute(
                "SELECT file_uuid FROM upload_links"
                " WHERE file_uuid IS NOT NULL AND file_n IS NULL AND used_at < ?",
                (pending_cutoff,)).fetchall()
            deleted_blobs = [r["file_uuid"] for r in rows]
            conn.execute(pending_sql, (pending_cutoff,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    for uuid_hex in deleted_blobs:
        blobs.delete(uuid_hex)
    count += len(deleted_blobs)

    count += conn.execute(
        "DELETE FROM upload_links WHERE file_n IS NOT NULL AND adopted_at < ?",
        (received_cutoff,)).rowcount

    count += conn.execute(
        "DELETE FROM upload_links WHERE used_at IS NOT NULL AND file_uuid IS NULL"
        " AND revoked_at IS NOT NULL AND revoked_at < ?",
        (discarded_cutoff,)).rowcount

    if count:
        log.info("expiry: purged %d dead upload link(s)", count)
    return count


def _sweep_once(db_path, blobs: BlobStore, bus=None) -> None:
    conn = connect(db_path)
    try:
        expire_files(conn, blobs)
        expire_upload_links(conn, blobs)
        expire_legacy_tickets(conn, bus=bus)
        bridge.purge(conn, bridge.now_ts())
    finally:
        conn.close()


async def sweep_forever(db_path, blobs: BlobStore, bus=None) -> None:
    """The hourly sweep. One failed run is logged and the loop carries on. `bus` is the app's ticket_bus."""
    while True:
        await asyncio.sleep(SWEEP_EVERY_S)
        try:
            await asyncio.to_thread(_sweep_once, db_path, blobs, bus)
        except Exception:
            log.exception("expiry: sweep failed")
