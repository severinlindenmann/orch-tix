from datetime import timedelta

from fileshare import clock
from fileshare.db import connect, migrate
from fileshare.security import sha256_hex
from fileshare import sessions
from fileshare.sessions import (
    IDLE_S, RENEW_EVERY_S, cookie_name, create_session, delete_session, renew_session,
    resolve_session, session_name,
)
from fileshare.settings import Settings


def _conn(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    return conn


def test_cookie_name_depends_on_secure(tmp_path):
    assert cookie_name(Settings(data_dir=tmp_path, public_url="https://x")) == "__Host-fs_session"
    assert cookie_name(Settings(data_dir=tmp_path, public_url="http://x", cookie_secure=False)) == "fs_session"


def test_create_stores_only_hash(tmp_path, frozen_clock):
    conn = _conn(tmp_path)
    token = create_session(conn)
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    assert len(rows) == 1 and rows[0]["id_hash"] == sha256_hex(token)
    assert token not in rows[0]["id_hash"]
    assert resolve_session(conn, token) == sha256_hex(token)


def test_resolve_unknown_or_empty(tmp_path):
    conn = _conn(tmp_path)
    assert resolve_session(conn, None) is None
    assert resolve_session(conn, "") is None
    assert resolve_session(conn, "nope") is None


def test_idle_expiry(tmp_path, frozen_clock):
    conn = _conn(tmp_path)
    t0 = clock.now()
    token = create_session(conn)
    frozen_clock(t0 + timedelta(seconds=IDLE_S - 1))
    assert resolve_session(conn, token) is not None
    frozen_clock(t0 + timedelta(seconds=2 * IDLE_S))  # idle for IDLE_S + 1 since last use
    assert resolve_session(conn, token) is None
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_idle_is_thirty_days_and_there_is_no_absolute_cap(tmp_path, frozen_clock):
    assert IDLE_S == 30 * 24 * 3600
    assert not hasattr(sessions, "ABSOLUTE_S")
    conn = _conn(tmp_path)
    t0 = clock.now()
    token = create_session(conn)
    frozen_clock(t0 + timedelta(days=29))
    assert resolve_session(conn, token) is not None      # idle 29 days: still valid
    t = t0 + timedelta(days=29)
    for _ in range(12):                                   # a year of monthly use, never capped
        t += timedelta(days=29)
        frozen_clock(t)
        assert resolve_session(conn, token) is not None
    frozen_clock(t + timedelta(days=31))                  # idle 31 days: gone
    assert resolve_session(conn, token) is None


def test_renewal_is_throttled_to_once_an_hour(tmp_path, frozen_clock):
    assert RENEW_EVERY_S == 3600
    conn = _conn(tmp_path)
    t0 = clock.now()
    token = create_session(conn)
    assert renew_session(conn, token) == (sha256_hex(token), False)   # just created
    frozen_clock(t0 + timedelta(minutes=59))
    assert renew_session(conn, token) == (sha256_hex(token), False)
    frozen_clock(t0 + timedelta(minutes=61))
    assert renew_session(conn, token) == (sha256_hex(token), True)
    last = conn.execute("SELECT last_used_at FROM sessions").fetchone()[0]
    assert last == clock.now_iso()
    frozen_clock(t0 + timedelta(minutes=90))
    assert renew_session(conn, token) == (sha256_hex(token), False)
    assert renew_session(conn, "nope") == (None, False)


def test_session_name_stored(tmp_path):
    conn = _conn(tmp_path)
    token = create_session(conn, "iPhone · Safari")
    assert session_name(conn, sha256_hex(token)) == "iPhone · Safari"
    assert session_name(conn, sha256_hex(create_session(conn))) == ""


def test_delete_session(tmp_path):
    conn = _conn(tmp_path)
    token = create_session(conn)
    delete_session(conn, token)
    delete_session(conn, None)
    assert resolve_session(conn, token) is None
