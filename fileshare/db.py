import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def _statements(sql: str) -> list[str]:
    # Line comments are dropped first (006 carries a ";" in one). Our migrations contain no
    # semicolons, and no "--", inside string literals.
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in sql.split(";") if s.strip()]


def backfill_device_fingerprints(conn: sqlite3.Connection) -> int:
    """Rewrite any device whose stored fingerprint no longer matches the one recomputed from its
    pubkey, and return how many were updated. Runs at startup so devices onboarded before the
    fingerprint was lengthened (§4.6, security fix 2026-09-25) show the same 20-char value the
    client computes — they keep working without re-onboarding. A SQL migration cannot do this
    (SQLite has no sha256), and the stored column stays the server-reported value the client
    cross-checks (R12), so the mismatch guard keeps its meaning."""
    # security.py hashes with hashlib only, never a cipher, so importing it here is allowed.
    from fileshare.security import b64u_decode, fingerprint
    stale = []
    for row in conn.execute("SELECT id, pubkey, fingerprint FROM devices").fetchall():
        try:
            want = fingerprint(b64u_decode(row["pubkey"]))
        except ValueError:
            continue  # an unparseable pubkey can never be approved; leave its column untouched
        if want != row["fingerprint"]:
            stale.append((want, row["id"]))
    if stale:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("UPDATE devices SET fingerprint = ? WHERE id = ?", stale)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(stale)


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    current = int(get_meta(conn, "schema_version") or 0)
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:3])
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _statements(path.read_text(encoding="utf-8")):
                conn.execute(stmt)
            set_meta(conn, "schema_version", str(version))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current
