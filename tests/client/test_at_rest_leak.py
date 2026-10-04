"""Success criterion §1.4: a copy of the disk, the DB, backups or the logs learns nothing about
names, notes or contents. Everything the server persists (DB, -wal, -shm, blobs, tmp, spool) and
everything it logged is searched for the plaintext of a CLI share and a browser upload."""
from pathlib import Path

import pytest

from tests.conftest import run_live_server

CLI_NAME, CLI_NOTE, CLI_BODY = "secret-name-XYZ.md", "note-XYZ-7731", "body-XYZ-4412"
WEB_NAME, WEB_NOTE, WEB_BODY = "phone-name-QRS.txt", "note-QRS-5519", "body-QRS-8830"
SECRETS = (CLI_NAME, CLI_NOTE, CLI_BODY, WEB_NAME, WEB_NOTE, WEB_BODY)


@pytest.fixture
def live_server(tmp_path):
    # Starlette spools multipart parts to TMPDIR; keep that spool inside data_dir so it is scanned.
    spool = tmp_path / "server-data" / "spool"
    spool.mkdir(parents=True)
    with run_live_server(tmp_path, TMPDIR=str(spool)) as srv:
        yield srv


def _needles(text: str) -> list[bytes]:
    return [text.encode("utf-8"), text.encode("utf-16-le"), text.encode("utf-16-be")]


def _persisted(live_server) -> list[Path]:
    files = [p for p in live_server.data_dir.rglob("*") if p.is_file()]
    return files + [live_server.log_path]


def _leaks(paths: list[Path]) -> list[tuple[str, str]]:
    found = []
    for p in paths:
        try:
            data = p.read_bytes()
        except FileNotFoundError:  # a -wal/-shm checkpointed away mid-scan; the DB holds its pages
            continue
        for secret in SECRETS:
            if any(n in data for n in _needles(secret)):
                found.append((str(p), secret))
    return found


def test_nothing_plaintext_reaches_disk_or_logs(live_server, sim, dev_repo, cli):
    (dev_repo.root / CLI_NAME).write_text(f"# heading\n\n{CLI_BODY}\n")
    r = cli(dev_repo.root, "share", CLI_NAME, "-m", CLI_NOTE, "--json")
    assert r.code == 0, r.err
    sim.upload(WEB_NAME, f"{WEB_BODY}\n".encode(), note=WEB_NOTE)

    # Both files round-trip, so the plaintext really went through the server.
    listed = cli(dev_repo.root, "list", "--json").json()
    assert {(f["name"], f["note"]) for f in listed} == {(CLI_NAME, CLI_NOTE), (WEB_NAME, WEB_NOTE)}

    paths = _persisted(live_server)
    names = {p.name for p in paths}
    assert "fileshare.db" in names and any(p.suffix == ".shr" for p in paths), names
    assert live_server.log_path.stat().st_size > 0, "the server log must have captured requests"
    assert _leaks(paths) == []


def test_leak_search_would_catch_a_plaintext_copy(live_server, tmp_path):
    """Guards the guard: the search really finds each encoding it claims to look for."""
    canary = live_server.data_dir / "tmp" / "canary"
    canary.parent.mkdir(exist_ok=True)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        canary.write_bytes(b"\x00junk" + CLI_NAME.encode(encoding) + b"junk")
        assert _leaks([canary]) == [(str(canary), CLI_NAME)]
