from datetime import datetime, timedelta, timezone

from fileshare import clock
from fileshare.db import backfill_device_fingerprints, connect, get_meta, migrate, set_meta
from fileshare.security import b64u_encode, fingerprint


def _tables(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert migrate(conn) == 10
    assert _tables(conn) >= {"meta", "devices", "onboarding_tokens", "files", "sessions", "settings", "links"}
    assert get_meta(conn, "schema_version") == "10"
    assert migrate(conn) == 10


def test_pragmas(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_connect_creates_parent_dir(tmp_path):
    connect(tmp_path / "a" / "b" / "x.db")
    assert (tmp_path / "a" / "b" / "x.db").exists()


def test_meta_roundtrip_and_overwrite(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    assert get_meta(conn, "k") is None
    set_meta(conn, "k", "v1")
    set_meta(conn, "k", "v2")
    assert get_meta(conn, "k") == "v2"


def _cols(conn, table):
    return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def test_devices_columns_v3(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    cols = _cols(conn, "devices")
    assert set(cols) == {"id", "name", "project", "hostname", "token_hash", "pubkey", "fingerprint",
                         "platform", "created_at", "approved_at", "device_bundle", "last_seen_at", "revoked_at"}
    assert cols["pubkey"]["notnull"] == 1 and cols["fingerprint"]["notnull"] == 1
    assert cols["approved_at"]["notnull"] == 0 and cols["device_bundle"]["notnull"] == 0


def test_onboarding_tokens_have_no_bundle(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    assert set(_cols(conn, "onboarding_tokens")) == {
        "id", "lookup_hash", "created_at", "expires_at", "used_at", "device_id"}


def test_new_device_is_pending_with_defaults(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO devices(id,name,project,token_hash,pubkey,fingerprint,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("dev_000000000001", "mbp", "proj", "h" * 64, "BA", "ABCD-EFGH", clock.now_iso()))
    row = conn.execute("SELECT * FROM devices").fetchone()
    assert row["hostname"] == "" and row["platform"] == ""
    assert row["approved_at"] is None and row["device_bundle"] is None and row["revoked_at"] is None


def test_backfill_rewrites_stale_device_fingerprints(tmp_path):
    # A device onboarded before the fingerprint was lengthened (§4.6) still has an 8-char value;
    # backfill must rewrite it to the 20-char one the client computes, without re-onboarding.
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    pub = bytes([0x04]) + bytes((3 * i + 5) % 256 for i in range(64))
    want = fingerprint(pub)
    assert len(want) == 24
    conn.execute(
        "INSERT INTO devices(id,name,project,token_hash,pubkey,fingerprint,created_at,approved_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("dev_000000000001", "old", "proj", "h" * 64, b64u_encode(pub), "ABCD-EFGH",
         clock.now_iso(), clock.now_iso()))
    # A device already on the new format must be left untouched (and not counted).
    pub2 = bytes([0x04]) + bytes((7 * i + 1) % 256 for i in range(64))
    conn.execute(
        "INSERT INTO devices(id,name,project,token_hash,pubkey,fingerprint,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("dev_000000000002", "new", "proj", "h2" * 32, b64u_encode(pub2), fingerprint(pub2),
         clock.now_iso()))

    assert backfill_device_fingerprints(conn) == 1
    rows = {r["id"]: r["fingerprint"] for r in conn.execute("SELECT id, fingerprint FROM devices")}
    assert rows["dev_000000000001"] == want
    assert rows["dev_000000000002"] == fingerprint(pub2)
    # Idempotent: a second run rewrites nothing.
    assert backfill_device_fingerprints(conn) == 0


def _insert_file(conn, uuid):
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (uuid, 10, 1, "w", "m", None, "web", clock.now_iso()))
    return conn.execute("SELECT n FROM files WHERE uuid=?", (uuid,)).fetchone()["n"]


def test_file_numbers_are_never_reused(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    n1 = _insert_file(conn, "a" * 32)
    conn.execute("DELETE FROM files WHERE n=?", (n1,))
    n2 = _insert_file(conn, "b" * 32)
    assert (n1, n2) == (1, 2)


def test_files_device_fk_enforced(tmp_path):
    import sqlite3
    import pytest
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("c" * 32, 1, 1, "w", "m", "dev_missing", "p", clock.now_iso()))


def test_clock_iso_format_and_parse(frozen_clock):
    frozen_clock(datetime(2026, 9, 24, 12, 0, 5, 999, tzinfo=timezone.utc))
    assert clock.now_iso() == "2026-09-24T12:00:05Z"
    assert clock.parse_iso("2026-09-24T12:00:05Z") == datetime(2026, 9, 24, 12, 0, 5, tzinfo=timezone.utc)
    later = clock.now() + timedelta(minutes=15)
    assert clock.now_iso(later) == "2026-09-24T12:15:05Z"


def test_create_app_migrates(tmp_path):
    from fileshare.app import create_app
    from fileshare.settings import Settings
    create_app(Settings(data_dir=tmp_path / "data", public_url="http://testserver", cookie_secure=False))
    conn = connect(tmp_path / "data" / "fileshare.db")
    assert get_meta(conn, "schema_version") == "10"


def test_v2_columns(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    assert {"name"} <= set(_cols(conn, "sessions"))
    assert _cols(conn, "sessions")["name"]["notnull"] == 1
    assert {"session_name", "acked_at", "acked_by", "expires_at"} <= set(_cols(conn, "files"))
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(files)")}
    assert "files_expires" in indexes


def test_migrating_a_001_database_keeps_rows(tmp_path, monkeypatch):
    """The live VPS database was created by 001 only; 002 must upgrade it in place."""
    import shutil
    from fileshare import db as db_mod
    only_001 = tmp_path / "m"
    only_001.mkdir()
    shutil.copy(db_mod.MIGRATIONS_DIR / "001_init.sql", only_001)
    real_dir = db_mod.MIGRATIONS_DIR
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", only_001)
    path = tmp_path / "live.db"
    conn = connect(path)
    assert migrate(conn) == 1
    ts = "2026-09-24T12:00:00Z"
    conn.execute(
        "INSERT INTO devices(id,name,project,token_hash,pubkey,fingerprint,created_at,approved_at)"
        " VALUES ('dev_000000000001','mbp','proj','h','BA','ABCD-EFGH',?,?)", (ts, ts))
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", "dev_000000000001", "proj", ts))
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at,deleted_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", ("b" * 32, 10, 1, "", "", None, "web", ts, ts))
    conn.execute("INSERT INTO sessions(id_hash,created_at,last_used_at) VALUES ('s1',?,?)", (ts, ts))
    conn.close()

    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real_dir)
    conn = connect(path)
    assert migrate(conn) == 10
    f1, f2 = conn.execute("SELECT * FROM files ORDER BY n").fetchall()
    assert (f1["n"], f1["uuid"], f1["wrapped_dek"], f1["enc_meta"], f1["device_id"]) == (
        1, "a" * 32, "w", "m", "dev_000000000001")
    assert (f1["session_name"], f1["acked_at"], f1["acked_by"], f1["expires_at"]) == (None, None, None, None)
    assert (f2["n"], f2["deleted_at"]) == (2, ts)
    s = conn.execute("SELECT * FROM sessions").fetchone()
    assert (s["id_hash"], s["name"]) == ("s1", "")
    assert conn.execute("SELECT name FROM devices").fetchone()["name"] == "mbp"
    # the next file number still continues after the old ones
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", ("c" * 32, 10, 1, "w", "m", None, "web", ts))
    assert conn.execute("SELECT MAX(n) FROM files").fetchone()[0] == 3


def test_settings_table(tmp_path):
    import sqlite3
    import pytest
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    cols = _cols(conn, "settings")
    assert set(cols) == {"id", "enc_settings", "rev", "updated_at"}
    assert all(cols[c]["notnull"] == 1 for c in ("enc_settings", "rev", "updated_at"))
    ts = "2026-09-24T12:00:00Z"
    conn.execute("INSERT INTO settings(id,enc_settings,rev,updated_at) VALUES (1,'e',1,?)", (ts,))
    with pytest.raises(sqlite3.IntegrityError):      # a single row: id must be 1
        conn.execute("INSERT INTO settings(id,enc_settings,rev,updated_at) VALUES (2,'e',1,?)", (ts,))


def test_migrating_a_002_database_keeps_rows(tmp_path, monkeypatch):
    """The live VPS database is at 002; 003 must add settings in place and leave the rest alone."""
    import shutil
    from fileshare import db as db_mod
    upto_002 = tmp_path / "m"
    upto_002.mkdir()
    for name in ("001_init.sql", "002_v2.sql"):
        shutil.copy(db_mod.MIGRATIONS_DIR / name, upto_002)
    real_dir = db_mod.MIGRATIONS_DIR
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", upto_002)
    path = tmp_path / "live.db"
    conn = connect(path)
    assert migrate(conn) == 2
    ts = "2026-09-24T12:00:00Z"
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at,acked_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", None, "web", ts, ts))
    conn.execute("INSERT INTO sessions(id_hash,created_at,last_used_at,name) VALUES ('s1',?,?,'phone')", (ts, ts))
    conn.close()

    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real_dir)
    conn = connect(path)
    assert migrate(conn) == 10
    assert get_meta(conn, "schema_version") == "10"
    f = conn.execute("SELECT * FROM files").fetchone()
    assert (f["uuid"], f["acked_at"], f["enc_meta"]) == ("a" * 32, ts, "m")
    assert conn.execute("SELECT name FROM sessions").fetchone()["name"] == "phone"
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert migrate(conn) == 10


LINK_COLS = {"id", "file_n", "token_hash", "wrapped_dek_link", "created_at", "created_by_device",
             "created_by_session", "expires_at", "max_downloads", "downloads", "revoked_at"}


def test_links_table(tmp_path):
    import sqlite3
    import pytest
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    cols = _cols(conn, "links")
    assert set(cols) == LINK_COLS
    assert cols["id"]["pk"] == 1
    for c in ("file_n", "token_hash", "wrapped_dek_link", "created_at", "expires_at", "downloads"):
        assert cols[c]["notnull"] == 1, c
    for c in ("created_by_device", "created_by_session", "max_downloads", "revoked_at"):
        assert cols[c]["notnull"] == 0, c
    assert cols["downloads"]["dflt_value"] == "0"
    fks = conn.execute("PRAGMA foreign_key_list(links)").fetchall()
    assert [(f["table"], f["from"], f["to"]) for f in fks] == [("files", "file_n", "n")]
    ts = "2026-09-24T12:00:00Z"
    with pytest.raises(sqlite3.IntegrityError):          # file_n must name a real file
        conn.execute("INSERT INTO links(id,file_n,token_hash,wrapped_dek_link,created_at,expires_at)"
                     " VALUES ('lnk_1',99,'h','w',?,?)", (ts, ts))
    conn.execute("INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,project,created_at)"
                 " VALUES (?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", "web", ts))
    conn.execute("INSERT INTO links(id,file_n,token_hash,wrapped_dek_link,created_at,expires_at)"
                 " VALUES ('lnk_1',1,'h','w',?,?)", (ts, ts))
    assert conn.execute("SELECT downloads FROM links").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):          # token_hash is unique
        conn.execute("INSERT INTO links(id,file_n,token_hash,wrapped_dek_link,created_at,expires_at)"
                     " VALUES ('lnk_2',1,'h','w',?,?)", (ts, ts))


def test_migrating_a_003_database_keeps_rows(tmp_path, monkeypatch):
    """The live VPS database is at 003; 004 must add links in place and leave the rest alone."""
    import shutil
    from fileshare import db as db_mod
    upto_003 = tmp_path / "m"
    upto_003.mkdir()
    for name in ("001_init.sql", "002_v2.sql", "003_settings.sql"):
        shutil.copy(db_mod.MIGRATIONS_DIR / name, upto_003)
    real_dir = db_mod.MIGRATIONS_DIR
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", upto_003)
    path = tmp_path / "live.db"
    conn = connect(path)
    assert migrate(conn) == 3
    ts = "2026-09-24T12:00:00Z"
    conn.execute(
        "INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,device_id,project,created_at,expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", None, "web", ts, ts))
    conn.execute("INSERT INTO settings(id,enc_settings,rev,updated_at) VALUES (1,'e',4,?)", (ts,))
    conn.execute("INSERT INTO sessions(id_hash,created_at,last_used_at,name) VALUES ('s1',?,?,'phone')", (ts, ts))
    conn.close()

    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real_dir)
    conn = connect(path)
    assert migrate(conn) == 10
    assert get_meta(conn, "schema_version") == "10"
    f = conn.execute("SELECT * FROM files").fetchone()
    assert (f["n"], f["uuid"], f["enc_meta"], f["expires_at"]) == (1, "a" * 32, "m", ts)
    assert conn.execute("SELECT rev FROM settings").fetchone()["rev"] == 4
    assert conn.execute("SELECT name FROM sessions").fetchone()["name"] == "phone"
    assert set(_cols(conn, "links")) == LINK_COLS
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
    assert migrate(conn) == 10


def test_file_tags_table(tmp_path):
    import sqlite3
    import pytest
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    cols = _cols(conn, "file_tags")
    assert set(cols) == {"file_n", "tag"}
    assert cols["file_n"]["notnull"] == 1 and cols["tag"]["notnull"] == 1
    assert (cols["file_n"]["pk"], cols["tag"]["pk"]) == (1, 2)
    names = [r["name"] for r in conn.execute("PRAGMA index_list(file_tags)")]
    by_cols = [[c["name"] for c in conn.execute(f"PRAGMA index_info({n})")] for n in names]
    assert ["tag", "file_n"] in by_cols
    ts = "2026-09-24T12:00:00Z"
    conn.execute("INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,project,created_at)"
                 " VALUES (?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", "web", ts))
    conn.execute("INSERT INTO file_tags(file_n, tag) VALUES (1, 'notes')")
    with pytest.raises(sqlite3.IntegrityError):          # (file_n, tag) is the primary key
        conn.execute("INSERT INTO file_tags(file_n, tag) VALUES (1, 'notes')")
    with pytest.raises(sqlite3.IntegrityError):          # file_n references files(n)
        conn.execute("INSERT INTO file_tags(file_n, tag) VALUES (99, 'notes')")


def test_migrating_a_004_database_keeps_rows(tmp_path, monkeypatch):
    """The live VPS database is at 004; 005 must add file_tags in place and leave the rest alone."""
    import shutil
    from fileshare import db as db_mod
    upto_004 = tmp_path / "m"
    upto_004.mkdir()
    for name in ("001_init.sql", "002_v2.sql", "003_settings.sql", "004_links.sql"):
        shutil.copy(db_mod.MIGRATIONS_DIR / name, upto_004)
    real_dir = db_mod.MIGRATIONS_DIR
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", upto_004)
    path = tmp_path / "live.db"
    conn = connect(path)
    assert migrate(conn) == 4
    ts = "2026-09-24T12:00:00Z"
    conn.execute("INSERT INTO files(uuid,size,key_version,wrapped_dek,enc_meta,project,created_at)"
                 " VALUES (?,?,?,?,?,?,?)", ("a" * 32, 10, 1, "w", "m", "web", ts))
    conn.execute("INSERT INTO links(id,file_n,token_hash,wrapped_dek_link,created_at,expires_at)"
                 " VALUES ('lnk_1',1,'h','w',?,?)", (ts, ts))
    conn.close()

    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real_dir)
    conn = connect(path)
    assert migrate(conn) == 10
    assert get_meta(conn, "schema_version") == "10"
    assert conn.execute("SELECT uuid FROM files").fetchone()["uuid"] == "a" * 32
    assert conn.execute("SELECT id FROM links").fetchone()["id"] == "lnk_1"
    assert conn.execute("SELECT COUNT(*) FROM file_tags").fetchone()[0] == 0
    assert migrate(conn) == 10


def test_migrating_a_007_database_with_tickets_keeps_them_legacy(tmp_path, monkeypatch):
    """008 adds the A4 tables and mirror columns in place; existing tickets and events stay legacy and intact."""
    import shutil
    from fileshare import db as db_mod
    upto_007 = tmp_path / "m"
    upto_007.mkdir()
    for path in sorted(db_mod.MIGRATIONS_DIR.glob("00[1-7]_*.sql")):
        shutil.copy(path, upto_007)
    real_dir = db_mod.MIGRATIONS_DIR
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", upto_007)
    path = tmp_path / "live.db"
    conn = connect(path)
    assert migrate(conn) == 7
    ts = "2026-09-30T12:00:00Z"
    conn.execute("INSERT INTO tickets(uuid,key_version,wrapped_dek,enc_content,status,project,created_by_name,"
                 "open_questions,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("t" * 32, 1, "w", "c", "waiting", "proj", "laptop", 2, ts, ts))
    conn.execute("INSERT INTO ticket_events(ticket_n,uuid,kind,actor_kind,actor_name,created_at)"
                 " VALUES (1,?,'created','web','browser',?)", ("e" * 32, ts))
    conn.close()

    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", real_dir)
    conn = connect(path)
    assert migrate(conn) == 10
    t = conn.execute("SELECT * FROM tickets").fetchone()
    assert (t["n"], t["uuid"], t["status"], t["open_questions"], t["mode"]) == (1, "t" * 32, "waiting", 2, "legacy")
    assert (t["space_id"], t["needs"], t["mirror_rev"], t["schema_version"], t["archived_at"]) == (None,) * 5
    assert conn.execute("SELECT uuid FROM ticket_events").fetchone()["uuid"] == "e" * 32
    for table in ("spaces", "space_join_requests", "decisions", "messages"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert "owner_gen" in _cols(conn, "spaces")
    assert migrate(conn) == 10
    conn.close()
