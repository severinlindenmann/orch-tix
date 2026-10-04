import re
from datetime import timedelta

from fileshare import clock
from fileshare.admin import create_setup_code, main
from fileshare.db import connect, get_meta, migrate
from fileshare.security import sha256_hex


def test_create_setup_code_stores_hash_and_expiry(tmp_path, frozen_clock):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    code = create_setup_code(conn)
    assert re.fullmatch(r"setup_[A-Za-z0-9_-]{32}", code)
    assert get_meta(conn, "setup_code_hash") == sha256_hex(code)
    assert get_meta(conn, "setup_code_expires_at") == clock.now_iso(clock.now() + timedelta(minutes=15))


def test_main_prints_code_and_migrates(tmp_path, capsys):
    assert main(["setup-code", "--data-dir", str(tmp_path / "d")]) == 0
    out = capsys.readouterr()
    code = out.out.strip()
    assert re.fullmatch(r"setup_[A-Za-z0-9_-]{32}", code)
    assert "15 minutes" in out.err
    conn = connect(tmp_path / "d" / "fileshare.db")
    assert get_meta(conn, "setup_code_hash") == sha256_hex(code)


def test_new_code_replaces_old(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    first = create_setup_code(conn)
    second = create_setup_code(conn)
    assert get_meta(conn, "setup_code_hash") == sha256_hex(second) != sha256_hex(first)
