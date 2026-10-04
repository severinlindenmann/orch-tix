"""Task 13 fix round 1: the §8.2 guards fail closed, and the HTTP client never fails open."""
import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.client.conftest import git_init
from tests.client.test_cli import ME, SDDL_INHERITED, SDDL_OWNER_ONLY, _rewrite_config


# ------------------------------------------------------------------ 1. Windows ACL: NULL DACL / missing user

@pytest.mark.parametrize("sddl", [
    "D:NO_ACCESS_CONTROL",
    "D:PNO_ACCESS_CONTROL",
    f"O:{ME}G:{ME}D:NO_ACCESS_CONTROLS:AI(AU;ID;FA;;;WD)",
])
def test_check_sddl_refuses_null_dacl(sharing, tmp_path, sddl):
    assert sharing._sddl_trustees(sddl) == []          # why it used to pass: no trustees, nothing "foreign"
    with pytest.raises(sharing.Refused, match="NULL DACL"):
        sharing._check_sddl(sddl, ME, tmp_path / "config.json")


@pytest.mark.parametrize("sddl", [
    "",                                                # empty icacls output
    "O:BAG:SY",                                        # no DACL at all
    "D:P",                                             # empty DACL
    "D:PAI(garbage)",                                  # malformed ACE
    "D:PAI(A;;FA;;;S-1-5-21-9-9-9-1002)",              # somebody else only
])
def test_check_sddl_refuses_when_current_user_is_absent(sharing, tmp_path, sddl):
    with pytest.raises(sharing.Refused, match="icacls"):
        sharing._check_sddl(sddl, ME, tmp_path / "config.json")


def test_check_sddl_owner_only_passes_and_extra_trustees_refused(sharing, tmp_path):
    sharing._check_sddl(SDDL_OWNER_ONLY, ME, tmp_path / "c")
    sharing._check_sddl(SDDL_OWNER_ONLY, ME.lower(), tmp_path / "c")
    with pytest.raises(sharing.Refused, match="readable by SY, BA, BU"):
        sharing._check_sddl(SDDL_INHERITED, ME, tmp_path / "c")


def test_check_permissions_windows_branch_refuses_null_dacl(sharing, tmp_path, monkeypatch):
    # The real Windows branch, fed captured output, on every OS.
    monkeypatch.setattr(sharing, "IS_WINDOWS", True)
    monkeypatch.setattr(sharing, "_windows_sid", lambda: ME)
    monkeypatch.setattr(sharing, "_windows_sddl", lambda p: "D:NO_ACCESS_CONTROL")
    with pytest.raises(sharing.Refused, match="NULL DACL"):
        sharing.check_permissions(tmp_path / "config.json")
    monkeypatch.setattr(sharing, "_windows_sddl", lambda p: SDDL_OWNER_ONLY)
    sharing.check_permissions(tmp_path / "config.json")


# ------------------------------------------------------------------ 2. git missing or erroring fails closed

def _no_git(monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def _fake_git(sharing, monkeypatch, rc: int, stderr: bytes = b""):
    real = subprocess.run

    def run(cmd, *a, **k):
        if cmd and cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, rc, b"", stderr)
        return real(cmd, *a, **k)
    monkeypatch.setattr(sharing.subprocess, "run", run)


def test_git_tracked_git_missing_is_refused(sharing, tmp_path, monkeypatch):
    repo = git_init(tmp_path / "r")
    _no_git(monkeypatch, tmp_path)
    with pytest.raises(sharing.Refused, match="Install git"):
        sharing.git_tracked(repo, "x.txt")
    with pytest.raises(sharing.Refused, match="Install git"):     # .git further up the tree
        sharing.git_tracked(repo / "sub", "x.txt")
    assert sharing.git_tracked(tmp_path / "plain", "x.txt") is False   # no .git anywhere: nothing tracked


def test_git_tracked_exit_128_is_refused(sharing, tmp_path, monkeypatch):
    repo = git_init(tmp_path / "r")
    _fake_git(sharing, monkeypatch, 128, b"fatal: detected dubious ownership in repository")
    with pytest.raises(sharing.Refused, match="dubious ownership.*Resolve the git error"):
        sharing.git_tracked(repo, "x.txt")
    # "not a git repository" with no .git anywhere up the tree: not tracked.
    _fake_git(sharing, monkeypatch, 128, b"fatal: not a git repository (or any of the parent directories): .git")
    assert sharing.git_tracked(tmp_path / "plain", "x.txt") is False


def test_git_tracked_exit_1_is_not_tracked(sharing, tmp_path, monkeypatch):
    repo = git_init(tmp_path / "r")
    _fake_git(sharing, monkeypatch, 1, b"error: pathspec 'x.txt' did not match any file(s) known to git")
    assert sharing.git_tracked(repo, "x.txt") is False
    _fake_git(sharing, monkeypatch, 0)
    assert sharing.git_tracked(repo, "x.txt") is True


def test_cli_refuses_when_git_missing(cli, dev_repo, monkeypatch, tmp_path):
    _no_git(monkeypatch, tmp_path)
    r = cli(dev_repo.root, "whoami")
    assert r.code == 6
    assert "Install git" in r.err


def test_cli_refuses_on_git_error(cli, dev_repo, sharing, monkeypatch):
    _fake_git(sharing, monkeypatch, 128, b"fatal: detected dubious ownership in repository")
    r = cli(dev_repo.root, "--json", "whoami")
    assert r.code == 6
    assert r.json()["error"] == "refused" and "dubious ownership" in r.json()["detail"]


def test_ensure_git_exclude_without_git_writes_git_info_exclude(sharing, tmp_path, monkeypatch):
    repo = git_init(tmp_path / "r")
    path = repo / ".git" / "info" / "exclude"
    path.write_bytes(b"# mine\r\n*.tmp")                        # CRLF, no final newline
    _no_git(monkeypatch, tmp_path)
    assert sharing.ensure_git_exclude(repo) == path
    assert sharing.ensure_git_exclude(repo) == path             # idempotent
    assert path.read_bytes() == b"# mine\r\n*.tmp\r\n/.claude/skills/sharing/\r\n/.claude/skills/sharing-tickets/\r\n/share/\r\n"
    sharing.remove_git_exclude(repo)
    assert path.read_bytes() == b"# mine\r\n*.tmp\r\n"
    shutil.rmtree(repo / ".git" / "info")                      # info/ missing: created
    sharing.ensure_git_exclude(repo)
    assert path.read_bytes() == b"/.claude/skills/sharing/\n/.claude/skills/sharing-tickets/\n/share/\n"
    assert sharing.ensure_git_exclude(tmp_path / "plain") is None


def test_ensure_git_exclude_on_git_error_falls_back_to_git_dir(sharing, tmp_path, monkeypatch):
    repo = git_init(tmp_path / "r")
    _fake_git(sharing, monkeypatch, 128, b"fatal: detected dubious ownership in repository")
    path = sharing.ensure_git_exclude(repo)
    assert path == repo / ".git" / "info" / "exclude" and "/share/" in path.read_text()


# ------------------------------------------------------------------ 3. share/.gitignore repaired every run

def test_guards_repair_share_gitignore(cli, dev_repo):
    share = dev_repo.root / "share"
    gi = share / ".gitignore"
    gi.unlink()
    assert cli(dev_repo.root, "whoami").code == 0
    assert gi.read_bytes() == b"*\n"
    gi.write_text("x.txt\n")
    assert cli(dev_repo.root, "whoami").code == 0
    assert gi.read_bytes() == b"*\n"
    shutil.rmtree(share)
    assert cli(dev_repo.root, "whoami").code == 0
    assert gi.read_bytes() == b"*\n"


# ------------------------------------------------------------------ a tiny local HTTP server

@contextmanager
def tiny_server(handle):
    """handle(req_handler) answers every GET; `hits` records (path, Authorization header)."""
    hits = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            handle(self)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


def _send(h, status: int, body: bytes, ctype: str = "application/json", length: int | None = None, **headers):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body) if length is None else length))
    for k, v in headers.items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)
    h.wfile.flush()


# ------------------------------------------------------------------ 4. read-time network errors → exit 4

def test_non_json_200_is_bad_response(sharing):
    with tiny_server(lambda h: _send(h, 200, b"<html>captive portal</html>", "text/html")) as (url, _):
        with pytest.raises(sharing.ApiError) as ei:
            sharing.Api(url, None).get_json("/api/whoami")
    assert ei.value.status == 0 and ei.value.code == "bad_response" and ei.value.exit_code == 4


def test_json_array_200_is_bad_response(sharing):
    with tiny_server(lambda h: _send(h, 200, b"[1, 2]")) as (url, _):
        with pytest.raises(sharing.ApiError, match="not the expected JSON"):
            sharing.Api(url, None).get_json("/x")


def test_truncated_body_is_network_error(sharing, tmp_path):
    def short(h):
        _send(h, 200, b'{"a": 1', length=1000)   # promises 1000 bytes, sends 7, closes
        h.close_connection = True
    with tiny_server(short) as (url, _):
        with pytest.raises(sharing.ApiError) as ei:
            sharing.Api(url, None).get_json("/x")
        assert ei.value.status == 0 and ei.value.code == "network"
        with open(tmp_path / "out", "wb") as dst, pytest.raises(sharing.ApiError) as ei:
            sharing.Api(url, None).download("/x", dst)
        assert ei.value.code == "network"


def test_timeout_during_read_is_network_error(sharing, tmp_path, monkeypatch):
    def slow(h):
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(b"x" * 10)
        h.wfile.flush()
        time.sleep(1.5)
    monkeypatch.setattr(sharing.Api, "TIMEOUT", 0.3)
    with tiny_server(slow) as (url, _):
        with open(tmp_path / "out", "wb") as dst, pytest.raises(sharing.ApiError) as ei:
            sharing.Api(url, None).download("/blob", dst)
    assert ei.value.status == 0 and ei.value.code == "network"


def test_cli_bad_response_exit_4_json(cli, dev_repo, sharing):
    with tiny_server(lambda h: _send(h, 200, b"<html>hi</html>", "text/html")) as (url, _):
        _rewrite_config(sharing, dev_repo, server_url=url)
        r = cli(dev_repo.root, "--json", "list")
    assert r.code == 4
    assert r.json()["error"] == "bad_response"


# ------------------------------------------------------------------ 5. catch-all: JSON on stdout, no token

def test_unexpected_error_is_reported_without_the_token(cli, tmp_path, sharing, monkeypatch):
    def boom(args):
        raise RuntimeError("kaboom Authorization: Bearer shd_SeCrEtToKeN-123")
    monkeypatch.setattr(sharing, "cmd_whoami", boom)
    r = cli(tmp_path, "--json", "whoami")
    assert r.code == 1
    j = r.json()
    assert j["error"] == "internal" and "kaboom" in j["detail"]
    assert "SeCrEtToKeN" not in r.out + r.err
    r = cli(tmp_path, "whoami")
    assert r.code == 1 and "kaboom" in r.err and "SeCrEtToKeN" not in r.out + r.err


# ------------------------------------------------------------------ 6. https only, redirects never followed

@pytest.mark.parametrize("url,want", [
    ("https://share.example.com", "https://share.example.com"),
    ("https://share.example.com:8443/", "https://share.example.com:8443"),
    ("http://127.0.0.1:8808", "http://127.0.0.1:8808"),
    ("http://localhost/", "http://localhost"),
    ("http://[::1]:8808", "http://[::1]:8808"),
])
def test_check_server_url_accepts(sharing, url, want):
    assert sharing.check_server_url(url) == want


@pytest.mark.parametrize("url", [
    "http://share.example.com", "http://192.168.1.10:8808", "http://127.0.0.2", "http://localhost.evil.com",
    "ftp://share.example.com", "share.example.com", "", "https://", "http://127.0.0.1:notaport",
])
def test_check_server_url_refuses(sharing, url):
    with pytest.raises(sharing.Refused):
        sharing.check_server_url(url)


def test_api_refuses_plain_http_remote(sharing):
    with pytest.raises(sharing.Refused, match="https"):
        sharing.Api("http://share.example.com", "shd_x")


def test_cli_http_remote_exit_6_without_network(cli, dev_repo, sharing, monkeypatch):
    _rewrite_config(sharing, dev_repo, server_url="http://share.example.com")
    monkeypatch.setattr(sharing.Api, "_req", lambda *a, **k: pytest.fail("network used"))
    r = cli(dev_repo.root, "whoami")
    assert r.code == 6
    assert "https://" in r.err


def _redirector(h):
    if h.path.startswith("/target"):
        _send(h, 200, json.dumps({"device": {}}).encode())
    else:
        _send(h, 302, b"", Location=f"http://127.0.0.1:{h.server.server_address[1]}/target")


def test_redirect_is_not_followed_and_token_not_forwarded(sharing):
    with tiny_server(_redirector) as (url, hits):
        with pytest.raises(sharing.ApiError) as ei:
            sharing.Api(url, "shd_secret").get_json("/api/whoami")
    assert ei.value.status == 302 and ei.value.code == "redirect"
    assert [p for p, _ in hits] == ["/api/whoami"]          # /target was never requested


def test_cli_redirect_is_an_error(cli, dev_repo, sharing):
    with tiny_server(_redirector) as (url, hits):
        _rewrite_config(sharing, dev_repo, server_url=url)
        r = cli(dev_repo.root, "--json", "list")
    assert r.code == 1
    assert r.json()["error"] == "redirect"
    assert all(not p.startswith("/target") for p, _ in hits)
    assert dev_repo.token not in r.out + r.err
