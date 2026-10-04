"""CLI side of inbound upload links (spec §18): `sharing upload-link create/list/revoke/put`, and the
CLI adopting pending uploads on `list`/`get`/`info`. Follows tests/client/test_cli_links.py (how it runs
the CLI against the live server, in-process for the owner side, a real subprocess for concurrency)."""
import json
import os
import re
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from fileshare.db import connect
from tests.conftest import REPO
from tests.helpers.uploadlinks import drop, make_link

SHARING = REPO / "skill" / "sharing" / "sharing.py"
URL_RE = re.compile(r"^http://127\.0\.0\.1:\d+/u/[A-Za-z0-9_-]{43}#[A-Za-z0-9_-]{87}$")


@pytest.fixture
def stranger(tmp_path, monkeypatch):
    """A directory with no repo, no config and an empty HOME: someone who only has the URL."""
    home = tmp_path / "empty-home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    d = tmp_path / "stranger"
    d.mkdir()
    return d


def _owner_post_json(sim):
    def f(path, body):
        r = sim.http.post(path, json=body)
        assert r.status_code == 201, r.text
        return r.json()
    return f


def _public_get_json(base_url):
    def f(path):
        r = httpx.get(base_url + path, timeout=30)
        assert r.status_code == 200, r.text
        return r.json()
    return f


def _public_post_multipart(base_url):
    def f(path, meta, blob):
        r = httpx.post(base_url + path, data={"meta": json.dumps(meta)},
                       files={"blob": ("blob.shr", blob, "application/octet-stream")}, timeout=30)
        assert r.status_code == 201, r.text
        return r.json()
    return f


def _create(cli, dev_repo, *extra):
    r = cli(dev_repo.root, "upload-link", "create", "--json", *extra)
    assert r.code == 0, r.err
    return r.json()


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHARING_")}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


# ---------------------------------------------------------------- create / list / revoke


def test_create_prints_only_the_url_and_list_shows_waiting_and_label(cli, dev_repo):
    j = _create(cli, dev_repo, "--label", "from Alice")
    assert set(j) == {"id", "url", "expires_at"}
    assert j["id"].startswith("upl_")
    assert URL_RE.fullmatch(j["url"]), j["url"]

    r = cli(dev_repo.root, "upload-link", "list", "--json")
    assert r.code == 0, r.err
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == j["id"] and row["state"] == "waiting" and row["label"] == "from Alice"
    assert "wrapped_lpriv" not in row

    h = cli(dev_repo.root, "upload-link", "list")
    assert h.code == 0 and "waiting" in h.out and "from Alice" in h.out


# ---------------------------------------------------------------- put


def test_put_a_single_file_then_list_adopts_and_get_returns_it(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    f = stranger / "file.txt"
    f.write_bytes(b"hello from a stranger")

    r = cli(stranger, "upload-link", "put", j["url"], str(f), "--note", "hi")
    assert r.code == 0, r.err
    assert "sent" in r.out and "file.txt" in r.out

    lst = cli(dev_repo.root, "list", "--json")
    assert lst.code == 0, lst.err
    rows = lst.json()
    assert len(rows) == 1
    fid = rows[0]["id"]

    g = cli(dev_repo.root, "get", fid, "--json", "--no-ack")
    assert g.code == 0, g.err
    path = Path(g.json()["path"])
    assert path.read_bytes() == b"hello from a stranger"

    info = cli(dev_repo.root, "info", fid, "--json")
    assert info.code == 0 and info.json()["note"] == "hi"


def test_put_a_directory_zips_with_nested_utf8_names(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    d = stranger / "dir"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "ä.txt").write_bytes("hällo".encode("utf-8"))
    (d / "b.bin").write_bytes(b"\x00\x01binary")

    r = cli(stranger, "upload-link", "put", j["url"], str(d))
    assert r.code == 0, r.err

    assert cli(dev_repo.root, "list", "--json").code == 0
    fid = cli(dev_repo.root, "list", "--json").json()[0]["id"]
    g = cli(dev_repo.root, "get", fid, "--json", "--no-ack")
    assert g.code == 0, g.err
    zpath = Path(g.json()["path"])
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        assert names == {"dir/sub/ä.txt", "dir/b.bin"}
        assert zf.read("dir/sub/ä.txt") == "hällo".encode("utf-8")
        assert zf.read("dir/b.bin") == b"\x00\x01binary"


def test_put_to_the_same_url_twice_is_exit_2(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    f = stranger / "a.txt"
    f.write_bytes(b"once")
    assert cli(stranger, "upload-link", "put", j["url"], str(f)).code == 0
    r = cli(stranger, "upload-link", "put", j["url"], str(f), "--json")
    assert r.code == 2
    assert "expired or was revoked" in r.json()["detail"]


def test_put_refuses_a_url_with_a_stripped_fragment_before_touching_anything(cli, dev_repo, stranger, sharing, monkeypatch):
    j = _create(cli, dev_repo)
    _assert_refused_untouched(cli, stranger, sharing, monkeypatch, j["url"].split("#")[0])


def test_put_refuses_a_short_key_before_touching_anything(cli, dev_repo, stranger, sharing, monkeypatch):
    j = _create(cli, dev_repo)
    base = j["url"].split("#")[0]
    _assert_refused_untouched(cli, stranger, sharing, monkeypatch, base + "#" + sharing.b64u(os.urandom(64)))


def test_put_refuses_plain_http_off_loopback_before_touching_anything(cli, dev_repo, stranger, sharing, monkeypatch):
    j = _create(cli, dev_repo)
    token = j["url"].split("/u/", 1)[1]
    bad = "http://example.com/u/" + token
    _assert_refused_untouched(cli, stranger, sharing, monkeypatch, bad)


def _assert_refused_untouched(cli, stranger, sharing, monkeypatch, bad_url):
    f = stranger / "x.txt"
    f.write_bytes(b"x")

    def boom(*a, **k):
        raise AssertionError("the network was touched for a bad upload-link URL")
    monkeypatch.setattr(sharing.Api, "__init__", boom)
    r = cli(stranger, "upload-link", "put", bad_url, str(f), "--json")
    assert r.code in (1, 6), r.err
    assert r.json()["error"] in ("usage", "refused")


def test_put_a_secret_looking_file_is_refused_unless_allowed(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    d = stranger / "dir"
    d.mkdir()
    (d / ".env").write_text("SECRET=1\n")
    (d / "notes.txt").write_text("fine\n")

    r = cli(stranger, "upload-link", "put", j["url"], str(d), "--json")
    assert r.code == 6
    assert r.json()["error"] == "refused"
    assert cli(dev_repo.root, "upload-link", "list", "--json").json()[0]["state"] == "waiting"

    r2 = cli(stranger, "upload-link", "put", j["url"], str(d), "--allow-secret", "--json")
    assert r2.code == 0, r2.err
    row = cli(dev_repo.root, "upload-link", "list", "--json").json()[0]
    assert row["state"] in ("pending", "received")


def test_put_too_large_reports_the_links_actual_limit_not_a_hardcoded_200_mib(cli, dev_repo, stranger, sharing, monkeypatch):
    """Final review: `put`'s too-large message must report the link's own `max_bytes` (from the
    server's `GET /api/public/u/<token>`), not a hard-coded 200 MiB. Patches just that one response
    field so the test needs no giant file and no second server."""
    j = _create(cli, dev_repo)
    f = stranger / "big.bin"
    f.write_bytes(b"x" * 4096)

    real_get_json = sharing.Api.get_json

    def tiny_limit(self, path):
        out = real_get_json(self, path)
        if path.startswith("/api/public/u/"):
            out["max_bytes"] = 1024
        return out

    monkeypatch.setattr(sharing.Api, "get_json", tiny_limit)
    r = cli(stranger, "upload-link", "put", j["url"], str(f), "--json")
    assert r.code == 6
    detail = r.json()["detail"]
    assert "1024 bytes" in detail
    assert "200 MiB" not in detail


def test_put_refuses_two_files_that_would_both_be_named_the_same_in_the_zip(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    (stranger / "a").mkdir()
    (stranger / "b").mkdir()
    (stranger / "a" / "x.txt").write_bytes(b"one")
    (stranger / "b" / "x.txt").write_bytes(b"two")

    r = cli(stranger, "upload-link", "put", j["url"], str(stranger / "a" / "x.txt"),
            str(stranger / "b" / "x.txt"), "--json")
    assert r.code == 1
    assert "x.txt" in r.json()["detail"]
    assert cli(dev_repo.root, "upload-link", "list", "--json").json()[0]["state"] == "waiting"


def test_put_an_empty_directory_is_nothing_to_send(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    d = stranger / "empty"
    d.mkdir()
    r = cli(stranger, "upload-link", "put", j["url"], str(d), "--json")
    assert r.code == 1
    assert "nothing to send" in r.json()["detail"]


def test_revoke_then_list_shows_revoked_and_put_is_exit_2(cli, dev_repo, stranger):
    j = _create(cli, dev_repo)
    rv = cli(dev_repo.root, "upload-link", "revoke", j["id"], "--json")
    assert rv.code == 0, rv.err
    assert rv.json() == {"id": j["id"], "revoked": True}

    row = cli(dev_repo.root, "upload-link", "list", "--json").json()[0]
    assert row["state"] == "revoked"

    f = stranger / "a.txt"
    f.write_bytes(b"x")
    r = cli(stranger, "upload-link", "put", j["url"], str(f), "--json")
    assert r.code == 2
    assert "expired or was revoked" in r.json()["detail"]


def test_revoke_unknown_id_is_exit_2_and_bad_id_is_exit_1(cli, dev_repo):
    assert cli(dev_repo.root, "upload-link", "revoke", "nope").code == 1
    assert cli(dev_repo.root, "upload-link", "revoke", "upl_ffffffffffff").code == 2


def test_revoke_a_link_with_a_pending_upload_adopts_it_first_instead_of_discarding_it(cli, dev_repo, stranger):
    """Final review: revoking a link that already carries an unclaimed upload must not silently throw
    the upload away. `revoke` adopts pending uploads first (like list/get/info), and once adopted the
    link is already "received" -- there is nothing left to revoke."""
    j = _create(cli, dev_repo)
    f = stranger / "a.txt"
    f.write_bytes(b"don't lose me")
    r = cli(stranger, "upload-link", "put", j["url"], str(f))
    assert r.code == 0, r.err

    rv = cli(dev_repo.root, "upload-link", "revoke", j["id"], "--json")
    assert rv.code == 0, rv.err
    out = rv.json()
    assert out["revoked"] is False
    assert out["file"] and out["file"].startswith("FILE")

    files = cli(dev_repo.root, "list", "--json").json()
    assert len(files) == 1
    g = cli(dev_repo.root, "get", files[0]["id"], "--json", "--no-ack")
    assert Path(g.json()["path"]).read_bytes() == b"don't lose me"

    row = cli(dev_repo.root, "upload-link", "list", "--json").json()[0]
    assert row["state"] == "received"


# ---------------------------------------------------------------- adopt (list/get/info) races and failures


def test_adopt_race_two_concurrent_list_runs_adopt_exactly_once(dev_repo, sim, live_server):
    made = make_link(_owner_post_json(sim), sim.mk, sim.key_version)
    drop(_public_post_multipart(live_server.url), _public_get_json(live_server.url),
        made["token"], made["pub"], b"race payload", "race.bin", "application/octet-stream")

    def run_list():
        return subprocess.run([sys.executable, str(SHARING), "list", "--json"], cwd=dev_repo.root,
                              capture_output=True, text=True, timeout=30, env=_env())

    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(run_list)
        f2 = ex.submit(run_list)
        r1, r2 = f1.result(30), f2.result(30)

    for r in (r1, r2):
        assert r.returncode == 0, r.stderr
        assert "warning" not in r.stderr.lower(), r.stderr

    files = sim.http.get("/api/files?limit=10").json()["files"]
    assert len(files) == 1


def test_adopt_pending_with_a_tampered_sealed_dek_warns_once_and_list_still_succeeds(cli, dev_repo, sim,
                                                                                     live_server, sharing):
    made = make_link(_owner_post_json(sim), sim.mk, sim.key_version)
    drop(_public_post_multipart(live_server.url), _public_get_json(live_server.url),
        made["token"], made["pub"], b"tampered payload", "t.bin", "application/octet-stream")

    conn = connect(live_server.data_dir / "fileshare.db")
    bad = sharing.b64u(os.urandom(sharing.SEALED_DEK_LEN))
    conn.execute("UPDATE upload_links SET sealed_dek = ? WHERE uuid = ?", (bad, made["uuid"].hex()))
    conn.commit()
    conn.close()

    r = cli(dev_repo.root, "list", "--json")
    assert r.code == 0, r.err
    assert "warning" in r.err.lower()
    assert r.json() == []   # never adopted: it stays pending, not a FILE
