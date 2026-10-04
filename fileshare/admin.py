"""On-box admin: `uv run python -m fileshare.admin setup-code|archive-legacy [--data-dir DIR]`."""
import argparse
import secrets
import sys
from datetime import timedelta
from pathlib import Path

from fileshare import clock
from fileshare.db import connect, migrate, set_meta
from fileshare.security import sha256_hex
from fileshare.settings import Settings

SETUP_CODE_TTL = timedelta(minutes=15)


def create_setup_code(conn) -> str:
    code = "setup_" + secrets.token_urlsafe(24)
    # One transaction, so a concurrent /api/setup never sees a new hash with an old expiry.
    conn.execute("BEGIN IMMEDIATE")
    try:
        set_meta(conn, "setup_code_hash", sha256_hex(code))
        set_meta(conn, "setup_code_expires_at", clock.now_iso(clock.now() + SETUP_CODE_TTL))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fileshare.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup-code", help="print a one-time code for /setup (first setup or restore)")
    p.add_argument("--data-dir", type=Path, default=None)
    p = sub.add_parser("archive-legacy", help="mark every legacy ticket archived (read-only, tombstoned in 90 days)")
    p.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    data_dir = args.data_dir or settings.data_dir
    conn = connect(data_dir / "fileshare.db")
    try:
        migrate(conn)
        if args.cmd == "archive-legacy":
            from fileshare.tickets import archive_all_legacy
            n = archive_all_legacy(conn, clock.now_iso())
            print(f"{n} legacy ticket(s) archived; they are tombstoned 90 days from now")
            return 0
        code = create_setup_code(conn)
    finally:
        conn.close()
    print(code)
    print(f"valid for 15 minutes, once; open {settings.public_url}/setup", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
