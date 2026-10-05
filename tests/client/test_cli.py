import json
import os
import shutil
import stat
import subprocess

import pytest

from tests.client.conftest import git_init
from tests.conftest import REPO

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="real icacls")


# ------------------------------------------------------------------ pure helpers

@pytest.mark.parametrize("status,exit_code", [
    (404, 2), (410, 2), (401, 3), (403, 3), (0, 4), (400, 1), (413, 1), (500, 1)])
def test_api_error_exit_codes(sharing, status, exit_code):
    assert sharing.ApiError(status, "x").exit_code == exit_code


@pytest.mark.parametrize("ref,want", [("FILE7", "FILE7"), ("file7", "FILE7"), ("File7", "FILE7"),
                                      ("7", "FILE7"), (" 12 ", "FILE12")])
def test_norm_ref_accepts(sharing, ref, want):
    assert sharing.norm_ref(ref) == want


@pytest.mark.parametrize("ref", ["", "FILE", "FILE0", "0", "07", "-1", "FILE7x", "abc", "FILE" + "9" * 13])
def test_norm_ref_rejects(sharing, ref):
    with pytest.raises(sharing.UsageError):
        sharing.norm_ref(ref)


def test_plaintext_size_is_exact(sharing):
    c = sharing.CHUNK
    for n in (0, 1, 1000, c - 1, c, c + 1, 3 * c + 5):
        ct = sharing.HEADER_LEN + n + 16 * max(1, -(-n // c) if n else 1)
        assert sharing.plaintext_size(ct) == n


def test_multipart_length_matches_body(sharing, tmp_path):
    blob = tmp_path / "b.shr"
    blob.write_bytes(os.urandom(200_000))
    headers, body, length = sharing.Api("http://127.0.0.1:1", "t")._multipart({"uuid": "ab" * 16}, blob)
    data = b"".join(body)
    assert len(data) == length == int(headers["Content-Length"])
    boundary = headers["Content-Type"].split("boundary=", 1)[1]
    assert data.startswith(f"--{boundary}\r\n".encode())
    assert data.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert b'name="meta"' in data and b'name="blob"' in data
    assert blob.read_bytes() in data


def test_platform_is_one_of_three(sharing):
    assert sharing.PLATFORM in ("darwin", "linux", "windows")


def test_version_is_semver(sharing):
    parts = sharing.VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


# ------------------------------------------------------------------ config file guards (pure, every OS)

def test_ensure_self_ignore_writes_and_repairs(sharing, tmp_path):
    d = tmp_path / "skill"
    sharing.ensure_self_ignore(d)
    assert (d / ".gitignore").read_bytes() == b"*\n"
    (d / ".gitignore").write_text("config.json\n")
    sharing.ensure_self_ignore(d)
    assert (d / ".gitignore").read_bytes() == b"*\n"


def test_ensure_self_ignore_skips_an_identical_file(sharing, tmp_path):
    d = tmp_path / "skill"
    sharing.ensure_self_ignore(d)
    before = (d / ".gitignore").stat().st_mtime_ns
    sharing.ensure_self_ignore(d)
    assert (d / ".gitignore").stat().st_mtime_ns == before
    assert sorted(p.name for p in d.iterdir()) == [".gitignore"]   # no temp file left behind


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("dangling", [False, True])
def test_ensure_self_ignore_never_follows_a_symlink(sharing, tmp_path, capsys, dangling):
    """A cloned repo can commit `.gitignore -> ~/.bashrc`; writing through it would clobber the target."""
    d = tmp_path / "skill"
    d.mkdir()
    target = tmp_path / "outside" / "bashrc"
    target.parent.mkdir()
    if not dangling:
        target.write_text("export KEEP=1\n")
    (d / ".gitignore").symlink_to(target)
    sharing.ensure_self_ignore(d)
    gi = d / ".gitignore"
    assert not gi.is_symlink() and gi.read_bytes() == b"*\n"
    if dangling:
        assert not target.exists()
    else:
        assert target.read_text() == "export KEEP=1\n"
    assert "symlink" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_share_gitignore_symlink_out_of_the_repo_is_replaced_not_followed(cli, dev_repo, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    gi = dev_repo.root / "share" / ".gitignore"
    gi.unlink()
    gi.symlink_to(target)
    assert cli(dev_repo.root, "whoami").code == 0
    assert target.read_text() == "keep me\n"
    assert not gi.is_symlink() and gi.read_bytes() == b"*\n"


def test_ensure_self_ignore_leaves_a_non_file_alone(sharing, tmp_path, capsys):
    d = tmp_path / "skill"
    (d / ".gitignore").mkdir(parents=True)
    sharing.ensure_self_ignore(d)
    assert (d / ".gitignore").is_dir()
    assert "not a regular file" in capsys.readouterr().err


def test_write_config_is_atomic_and_leaves_no_temp(sharing, tmp_path):
    p = tmp_path / "skill" / "config.json"
    sharing.write_config(p, {"v": 1, "a": "b"})
    assert json.loads(p.read_text()) == {"v": 1, "a": "b"}
    assert sorted(x.name for x in p.parent.iterdir()) == ["config.json"]
    sharing.check_permissions(p)


@POSIX_ONLY
def test_write_config_is_0600_and_check_refuses_looser(sharing, tmp_path):
    p = tmp_path / "config.json"
    sharing.write_config(p, {"v": 1})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    p.chmod(0o644)
    with pytest.raises(sharing.Refused, match="chmod 600"):
        sharing.check_permissions(p)


ME = "S-1-5-21-111-222-333-1001"
SDDL_OWNER_ONLY = f"D:PAI(A;;FA;;;{ME})"
SDDL_INHERITED = f"D:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;{ME})(A;ID;0x1200a9;;;BU)S:AI(AU;ID;FA;;;WD)"


def test_sddl_trustees_owner_only(sharing):
    assert sharing._sddl_trustees(SDDL_OWNER_ONLY) == [ME]
    assert sharing._foreign_trustees(sharing._sddl_trustees(SDDL_OWNER_ONLY), ME) == []


def test_sddl_trustees_inherited_ignores_sacl(sharing):
    got = sharing._sddl_trustees(SDDL_INHERITED)
    assert got == ["SY", "BA", ME, "BU"]
    assert sharing._foreign_trustees(got, ME) == ["SY", "BA", "BU"]


def test_parse_icacls_save_file_is_codepage_independent(sharing):
    # `icacls <p> /save <f>` writes UTF-16LE: the file name line, then the SDDL line.
    data = ("config.json\r\n" + SDDL_OWNER_ONLY + "\r\n").encode("utf-16-le")
    assert sharing._parse_icacls_save(data) == SDDL_OWNER_ONLY
    assert sharing._parse_icacls_save(b"\xff\xfe" + data) == SDDL_OWNER_ONLY   # with BOM


def test_parse_whoami_user_csv(sharing):
    assert sharing._parse_whoami_sid('"desktop-1\\sévérin","S-1-5-21-111-222-333-1001"\r\n') == ME


@WINDOWS_ONLY
def test_real_icacls_owner_only_then_looser_is_refused(sharing, tmp_path):
    p = tmp_path / "config.json"
    sharing.write_config(p, {"v": 1})
    sharing.check_permissions(p)
    subprocess.run(["icacls", str(p), "/grant", "*S-1-5-32-545:(R)"], check=True, capture_output=True)
    with pytest.raises(sharing.Refused, match="icacls"):
        sharing.check_permissions(p)


def test_git_tracked(sharing, tmp_path):
    repo = git_init(tmp_path / "r")
    (repo / "x.txt").write_text("x")
    assert not sharing.git_tracked(repo, "x.txt")
    subprocess.run(["git", "-C", str(repo), "add", "x.txt"], check=True)
    assert sharing.git_tracked(repo, "x.txt")
    assert not sharing.git_tracked(tmp_path / "not-a-repo", "x.txt")


def test_ensure_git_exclude_idempotent(sharing, tmp_path):
    repo = git_init(tmp_path / "r")
    path = sharing.ensure_git_exclude(repo)
    sharing.ensure_git_exclude(repo)
    lines = path.read_text().splitlines()
    assert lines.count("/.claude/skills/sharing/") == 1 and lines.count("/share/") == 1


def test_ensure_git_exclude_keeps_crlf(sharing, tmp_path):
    repo = git_init(tmp_path / "r")
    path = repo / ".git" / "info" / "exclude"
    path.write_bytes(b"# mine\r\n*.tmp\r\n")
    sharing.ensure_git_exclude(repo)
    raw = path.read_bytes()
    assert raw == b"# mine\r\n*.tmp\r\n/.claude/skills/sharing/\r\n/.claude/skills/sharing-tickets/\r\n/share/\r\n"
    sharing.remove_git_exclude(repo)
    assert path.read_bytes() == b"# mine\r\n*.tmp\r\n"


def test_ensure_git_exclude_in_a_worktree(sharing, tmp_path):
    main = git_init(tmp_path / "main")
    subprocess.run(["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    path = sharing.ensure_git_exclude(wt)
    assert path is not None and "/share/" in path.read_text()


def test_ensure_git_exclude_outside_git_returns_none(sharing, tmp_path):
    assert sharing.ensure_git_exclude(tmp_path) is None


# ------------------------------------------------------------------ fail-closed / usage

def test_crypto_missing_fails_closed_before_network(sharing, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(sharing, "_CRYPTO_ERR", ImportError("No module named 'cryptography'"))
    monkeypatch.setattr(sharing.Api, "_req", lambda *a, **k: pytest.fail("network used"))
    r = cli(tmp_path, "whoami")
    assert r.code == 5
    assert "cryptography" in r.err


def test_usage_errors_exit_1(cli, tmp_path):
    assert cli(tmp_path).code == 1
    assert cli(tmp_path, "frobnicate").code == 1
    assert cli(tmp_path, "list", "-n", "0").code == 1


def test_help_exits_0(cli, tmp_path):
    r = cli(tmp_path, "--help")
    assert r.code == 0 and "whoami" in r.out


def test_not_onboarded_exit_3(cli, tmp_path):
    git_init(tmp_path / "repo")
    r = cli(tmp_path / "repo", "whoami")
    assert r.code == 3
    assert "not onboarded" in r.err


def test_json_errors_go_to_stdout(cli, tmp_path):
    git_init(tmp_path / "repo")
    r = cli(tmp_path / "repo", "--json", "whoami")
    assert r.code == 3
    assert r.json()["error"] == "not_configured"


def test_json_usage_errors_go_to_stdout(cli, tmp_path):
    # R11: a parse-time usage error still honours --json.
    for args in (("--json", "list", "-n", "0"), ("list", "--json", "-n", "0"), ("--json", "frobnicate")):
        r = cli(tmp_path, *args)
        assert r.code == 1
        assert r.json()["error"] == "usage" and r.json()["detail"]


# ------------------------------------------------------------------ against a live server

def _rewrite_config(sharing, dev, **changes):
    data = json.loads(dev.config_path.read_text())
    data.update(changes)
    sharing.write_config(dev.config_path, data)


def test_whoami_human_and_json(cli, dev_repo, live_server, sim):
    r = cli(dev_repo.root, "whoami")
    assert r.code == 0, r.err
    assert "dev-a" in r.out and "proj-a" in r.out and "reachable" in r.out
    j = cli(dev_repo.root, "whoami", "--json").json()
    assert j["device_id"] == dev_repo.device_id
    assert j["server"] == live_server.url and j["reachable"] is True
    fp = next(d for d in sim.devices() if d["id"] == dev_repo.device_id)["fingerprint"]
    assert j["fingerprint"] == fp
    assert j["skill_version"] == "2.1.1"
    assert j["latest_skill_version"] == "2.1.1" and j["update_available"] is False


def test_whoami_from_subdirectory(cli, dev_repo):
    sub = dev_repo.root / "src" / "deep"
    sub.mkdir(parents=True)
    assert cli(sub, "whoami").code == 0


def test_config_holds_the_secrets_in_v3_shape(dev_repo, sim, sharing):
    data = json.loads(dev_repo.config_path.read_text())
    assert set(data) == {"v", "device_id", "device_name", "project", "server_url", "device_token", "mk",
                         "key_version"}
    assert sharing.unb64u(data["mk"]) == sim.mk and data["device_token"].startswith("shd_")


@POSIX_ONLY
def test_config_must_be_private(cli, dev_repo):
    dev_repo.config_path.chmod(0o644)
    r = cli(dev_repo.root, "whoami")
    assert r.code == 6
    assert "chmod 600" in r.err


def test_config_tracked_by_git_is_refused(cli, dev_repo):
    subprocess.run(["git", "-C", str(dev_repo.root), "add", "-f", ".claude/skills/sharing/config.json"],
                   check=True)
    r = cli(dev_repo.root, "whoami")
    assert r.code == 6
    assert "tracked by git" in r.err


def test_skill_dir_and_share_are_uncommittable_even_with_add_all(dev_repo):
    (dev_repo.root / ".git" / "info" / "exclude").write_text("")
    (dev_repo.root / "share" / "x.txt").write_text("x")
    (dev_repo.root / "README.md").write_text("r")
    subprocess.run(["git", "-C", str(dev_repo.root), "add", "-A"], check=True)
    tracked = subprocess.run(["git", "-C", str(dev_repo.root), "ls-files"], capture_output=True, text=True,
                             check=True).stdout.split()
    assert tracked == ["README.md"]


def test_guards_repair_the_self_ignore_and_exclude(cli, dev_repo):
    gi = dev_repo.config_path.parent / ".gitignore"
    gi.unlink()
    exclude = dev_repo.root / ".git" / "info" / "exclude"
    exclude.write_text("")
    assert cli(dev_repo.root, "whoami").code == 0
    assert gi.read_bytes() == b"*\n"
    assert "/.claude/skills/sharing/" in exclude.read_text()


def test_pending_device_exit_3_with_fingerprint(cli, make_device, sim):
    dev = make_device("dev-p", approve=False)
    r = cli(dev.root, "whoami")
    assert r.code == 3
    fp = next(d for d in sim.devices() if d["id"] == dev.device_id)["fingerprint"]
    assert fp in r.err and "sharing wait" in r.err
    assert cli(dev.root, "--json", "list").json()["error"] == "pending"


def test_revoked_device_exit_3(cli, dev_repo, sim):
    sim.revoke(dev_repo.device_id)
    r = cli(dev_repo.root, "whoami")
    assert r.code == 3
    assert "revoked" in r.err


def test_unreachable_server_exit_4(cli, dev_repo, sharing):
    _rewrite_config(sharing, dev_repo, server_url="http://127.0.0.1:9")
    r = cli(dev_repo.root, "whoami")
    assert r.code == 4
    assert "unreachable" in r.out


def test_list_decrypts_names_newest_first(cli, dev_repo, sim):
    sim.upload("a.md", b"# a", note="first")
    sim.upload("b.json", b"{}", note="second")
    rows = cli(dev_repo.root, "list", "--json").json()
    assert [r["name"] for r in rows] == ["b.json", "a.md"]
    assert rows[0]["note"] == "second" and rows[0]["device"] == "browser"
    assert rows[1]["size"] == 3
    assert set(rows[0]) >= {"id", "name", "mime", "size", "note", "device", "project", "created_at", "deleted_at"}
    human = cli(dev_repo.root, "list").out
    assert "a.md" in human and "note: second" in human
    assert len(cli(dev_repo.root, "list", "-n", "1", "--json").json()) == 1
    assert cli(dev_repo.root, "list", "--project", "nope", "--json").json() == []
    assert len(cli(dev_repo.root, "list", "--from", "browser", "--json").json()) == 2


def test_list_hides_tombstones_unless_all(cli, dev_repo, sim):
    fid = sim.upload("gone.txt", b"x")["id"]
    sim.upload("kept.txt", b"y")
    sim.delete(fid)
    names = [r["name"] for r in cli(dev_repo.root, "list", "--json").json()]
    assert names == ["kept.txt"]
    rows = cli(dev_repo.root, "list", "--all", "--json").json()
    assert [r["id"] for r in rows if r["deleted_at"]] == [fid]


def test_list_escapes_control_characters(cli, dev_repo, sim):
    sim.upload("evil\x1b[31m.txt", b"x")
    out = cli(dev_repo.root, "list").out
    assert "\x1b" not in out and "evil?[31m.txt" in out


def test_info_found_deleted_unknown_and_bad_ref(cli, dev_repo, sim):
    fid = sim.upload("n.md", b"hello", note="for you")["id"]
    r = cli(dev_repo.root, "info", fid.lower())
    assert r.code == 0 and "n.md" in r.out and "for you" in r.out
    sim.delete(fid)
    r = cli(dev_repo.root, "info", fid)
    assert r.code == 2 and "was deleted" in r.err
    assert cli(dev_repo.root, "info", "FILE999").code == 2
    assert cli(dev_repo.root, "info", "abc").code == 1


# ------------------------------------------------------------------ share / get / rm / uninstall

import re as _re
from pathlib import Path

import httpx


def _share_dir_files(repo):
    d = repo / "share"
    return sorted(p.name for p in d.iterdir() if p.name != ".gitignore") if d.exists() else []


def test_share_then_get_roundtrip_between_devices(cli, make_device, sharing):
    a, b = make_device("dev-a", "proj-a"), make_device("dev-b", "proj-b")
    payload = os.urandom(sharing.CHUNK + 123)
    (a.root / "notes.md").write_bytes(payload)
    r = cli(a.root, "share", "notes.md", "-m", "for b")
    assert r.code == 0, r.err
    fid = r.out.strip()
    assert _re.fullmatch(r"FILE\d+", fid)

    (b.root / "share" / ".gitignore").unlink()
    g = cli(b.root, "get", fid, "--json")
    assert g.code == 0, g.err
    out = Path(g.json()["path"])
    assert out.resolve() == (b.root / "share" / "notes.md").resolve()
    assert out.read_bytes() == payload
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert (b.root / "share" / ".gitignore").read_bytes() == b"*\n"
    assert g.json()["note"] == "for b" and g.json()["device"] == "dev-a" and g.json()["project"] == "proj-a"
    assert g.json()["size"] == len(payload)

    listed = cli(b.root, "list", "--all", "--json").json()   # the get acknowledged it
    assert listed[0]["name"] == "notes.md" and listed[0]["size"] == len(payload)


def test_share_json_output(cli, dev_repo):
    (dev_repo.root / "x.json").write_text("{}")
    j = cli(dev_repo.root, "share", "x.json", "--json").json()
    assert j["name"] == "x.json" and j["mime"] == "application/json" and j["size"] == 2


def test_empty_file_roundtrip(cli, dev_repo):
    (dev_repo.root / "empty.txt").write_bytes(b"")
    fid = cli(dev_repo.root, "share", "empty.txt").out.strip()
    assert cli(dev_repo.root, "get", fid, "-o", dev_repo.root / "dl").code == 0
    assert (dev_repo.root / "dl" / "empty.txt").read_bytes() == b""
    assert not (dev_repo.root / "dl" / ".gitignore").exists()


def test_share_from_stdin(cli, dev_repo):
    r = cli(dev_repo.root, "share", "-", "--name", "out.log", "-m", "piped", stdin=b"line 1\nline 2\n")
    assert r.code == 0, r.err
    assert cli(dev_repo.root, "get", r.out.strip()).code == 0
    assert (dev_repo.root / "share" / "out.log").read_bytes() == b"line 1\nline 2\n"


def test_share_from_stdin_requires_name(cli, dev_repo):
    assert cli(dev_repo.root, "share", "-", stdin=b"x").code == 1


def test_get_name_collisions(cli, dev_repo):
    (dev_repo.root / "a.txt").write_text("v1")
    fid = cli(dev_repo.root, "share", "a.txt").out.strip()
    assert cli(dev_repo.root, "get", fid).code == 0
    assert cli(dev_repo.root, "get", fid).code == 0
    assert _share_dir_files(dev_repo.root) == sorted(["a.txt", f"{fid}-a.txt"])
    r = cli(dev_repo.root, "get", fid)
    assert r.code == 6 and "--force" in r.err
    (dev_repo.root / "share" / "a.txt").write_text("local edit")
    assert cli(dev_repo.root, "get", fid, "--force").code == 0
    assert (dev_repo.root / "share" / "a.txt").read_text() == "v1"


def test_get_latest_skips_deleted(cli, dev_repo, sim):
    (dev_repo.root / "one.txt").write_text("1")
    (dev_repo.root / "two.txt").write_text("2")
    cli(dev_repo.root, "share", "one.txt")
    two = cli(dev_repo.root, "share", "two.txt").out.strip()
    assert Path(cli(dev_repo.root, "get", "latest", "--json").json()["path"]).name == "two.txt"
    sim.delete(two)
    assert Path(cli(dev_repo.root, "get", "LATEST", "--json", "--force").json()["path"]).name == "one.txt"


def test_get_latest_on_empty_share(cli, dev_repo):
    assert cli(dev_repo.root, "get", "latest").code == 2


def test_hostile_filename_lands_inside_share(cli, dev_repo, sim):
    fid = sim.upload("../../.ssh/authorized_keys", b"ssh-ed25519 AAAA attacker")["id"]
    j = cli(dev_repo.root, "get", fid, "--json").json()
    assert Path(j["path"]).resolve() == (dev_repo.root / "share" / "authorized_keys").resolve()
    fid2 = sim.upload("-rf", b"x")["id"]
    j2 = cli(dev_repo.root, "get", fid2, "--json").json()
    assert Path(j2["path"]).name == f"{fid2}.bin"


@pytest.mark.parametrize("hostile", ["-rf", "a\x00b", "..", ".", "dir/", "line\nbreak"])
def test_hostile_filename_falls_back_to_file_n_bin_via_real_get(cli, dev_repo, sim, hostile):
    fid = sim.upload(hostile, b"payload")["id"]
    g = cli(dev_repo.root, "get", fid, "--json")
    assert g.code == 0, g.err
    out = Path(g.json()["path"])
    assert out.resolve() == (dev_repo.root / "share" / f"{fid}.bin").resolve()
    assert out.read_bytes() == b"payload"


# ---- fix round 1: upload limit before encryption, share/ symlinked out of the repo

def test_ciphertext_size_matches_encrypt_stream(sharing):
    import io
    c = 1000
    for n in (0, 1, c - 1, c, c + 1, 3 * c):
        dst = io.BytesIO()
        got = sharing.encrypt_stream(os.urandom(32), os.urandom(16), 1, io.BytesIO(b"x" * n), dst, chunk_size=c)
        assert got == len(dst.getvalue()) == sharing.ciphertext_size(n, c)
        assert sharing.plaintext_size(got, c) == n


def _spy_encrypt(sharing, monkeypatch):
    calls = []
    real = sharing.encrypt_stream

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(sharing, "encrypt_stream", spy)
    return calls


def test_file_over_the_limit_is_refused_before_encrypting(cli, dev_repo, sharing, sim, monkeypatch, tmp_path):
    monkeypatch.setattr(sharing, "MAX_UPLOAD", sharing.ciphertext_size(1000))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(sharing.tempfile, "tempdir", str(tmpdir))
    calls = _spy_encrypt(sharing, monkeypatch)
    (dev_repo.root / "big.bin").write_bytes(b"x" * 1001)
    r = cli(dev_repo.root, "share", "big.bin")
    assert r.code == 6 and "200 MiB" in r.err
    assert calls == [] and list(tmpdir.iterdir()) == []
    (dev_repo.root / "ok.bin").write_bytes(b"x" * 1000)   # exactly at the limit is fine
    assert cli(dev_repo.root, "share", "ok.bin").code == 0
    assert calls == [1] and list(tmpdir.iterdir()) == []
    assert [f["size"] for f in sim.http.get("/api/files").json()["files"]] == [sharing.ciphertext_size(1000)]


def test_stdin_over_the_limit_is_refused_and_leaves_no_temp(cli, dev_repo, sharing, sim, monkeypatch, tmp_path):
    monkeypatch.setattr(sharing, "MAX_UPLOAD", sharing.ciphertext_size(1000))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(sharing.tempfile, "tempdir", str(tmpdir))
    r = cli(dev_repo.root, "share", "-", "--name", "x.log", stdin=b"z" * 5000)
    assert r.code == 6 and "200 MiB" in r.err
    assert list(tmpdir.iterdir()) == []
    assert sim.http.get("/api/files").json()["files"] == []
    assert cli(dev_repo.root, "share", "-", "--name", "y.log", stdin=b"z" * 1000).code == 0


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_get_refuses_a_share_symlink_leaving_the_repo_unless_o_is_given(cli, dev_repo, sim, tmp_path):
    fid = sim.upload("k.txt", b"k")["id"]
    victim = tmp_path / "victim-ssh"
    victim.mkdir()
    share = dev_repo.root / "share"
    for p in share.iterdir():
        p.unlink()
    share.rmdir()
    os.symlink(victim, share)   # what a hostile repo could commit
    r = cli(dev_repo.root, "get", fid)
    assert r.code == 6 and "outside the repo" in r.err and "-o" in r.err
    assert list(victim.iterdir()) == []   # not even a .gitignore was written there
    g = cli(dev_repo.root, "get", fid, "-o", victim, "--json")
    assert g.code == 0, g.err
    assert (victim / "k.txt").read_bytes() == b"k"


def test_tampered_blob_fails_closed_and_leaves_no_file(cli, make_device, sim, live_server, sharing):
    a, b = make_device("dev-a"), make_device("dev-b")
    (a.root / "t.txt").write_bytes(b"precious" * 100)
    fid = cli(a.root, "share", "t.txt").out.strip()
    p = live_server.blob_path(sim.get_file(fid)["uuid"])
    raw = bytearray(p.read_bytes())
    raw[sharing.HEADER_LEN + 3] ^= 0x01
    p.write_bytes(bytes(raw))
    r = cli(b.root, "get", fid)
    assert r.code == 5
    assert _share_dir_files(b.root) == []


def test_blob_truncated_at_a_chunk_boundary_fails(cli, dev_repo, sim, live_server, sharing):
    (dev_repo.root / "big.bin").write_bytes(os.urandom(sharing.CHUNK + 10))
    fid = cli(dev_repo.root, "share", "big.bin").out.strip()
    p = live_server.blob_path(sim.get_file(fid)["uuid"])
    p.write_bytes(p.read_bytes()[: sharing.HEADER_LEN + sharing.CHUNK + 16])
    assert cli(dev_repo.root, "get", fid).code == 5
    assert _share_dir_files(dev_repo.root) == []


def test_swapped_blob_is_detected(cli, dev_repo, sim, live_server):
    (dev_repo.root / "x.txt").write_text("xxxx")
    (dev_repo.root / "y.txt").write_text("yyyy")
    fx = cli(dev_repo.root, "share", "x.txt").out.strip()
    fy = cli(dev_repo.root, "share", "y.txt").out.strip()
    px = live_server.blob_path(sim.get_file(fx)["uuid"])
    py = live_server.blob_path(sim.get_file(fy)["uuid"])
    py.write_bytes(px.read_bytes())
    assert cli(dev_repo.root, "get", fy).code == 5
    assert _share_dir_files(dev_repo.root) == []


def test_guards_refuse_secrets_dirs_and_outside_paths(cli, dev_repo, sim, tmp_path):
    (dev_repo.root / ".env").write_text("TOKEN=hunter2")
    assert cli(dev_repo.root, "share", ".env").code == 6
    assert cli(dev_repo.root, "share", "-", "--name", "server.key", stdin=b"k").code == 6
    (dev_repo.root / "notes.txt").write_text("hi")
    assert cli(dev_repo.root, "share", "notes.txt", "--name", ".env").code == 6
    (dev_repo.root / "adir").mkdir()
    r = cli(dev_repo.root, "share", "adir")
    assert r.code == 6 and "tar" in r.err
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    assert cli(dev_repo.root, "share", str(outside)).code == 6
    assert sim.http.get("/api/files").json()["files"] == []

    assert cli(dev_repo.root, "share", ".env", "--allow-secret").code == 0
    assert cli(dev_repo.root, "share", str(outside), "--yes").code == 0
    assert cli(dev_repo.root, "share", "missing.txt").code == 1


def test_skill_dir_is_never_shareable_even_with_allow_secret(cli, dev_repo, sim):
    for rel in (".claude/skills/sharing/config.json", ".claude/skills/sharing/.gitignore"):
        r = cli(dev_repo.root, "share", rel, "--allow-secret", "--yes")
        assert r.code == 6 and "never" in r.err
    assert sim.http.get("/api/files").json()["files"] == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_secret_via_symlink_is_refused(cli, dev_repo, tmp_path):
    (tmp_path / "id_rsa").write_text("-----BEGIN")
    os.symlink(tmp_path / "id_rsa", dev_repo.root / "innocent.txt")
    assert cli(dev_repo.root, "share", "innocent.txt", "--yes").code == 6


def test_rm_only_by_uploader_then_get_reports_deleted(cli, make_device):
    a, b = make_device("dev-a"), make_device("dev-b")
    (a.root / "r.txt").write_text("r")
    fid = cli(a.root, "share", "r.txt").out.strip()
    other = cli(b.root, "rm", fid, "--json")
    assert other.code == 6, other.err
    assert other.json() == {"error": "forbidden",
                            "detail": f"only the device that shared {fid} (or the web UI) can delete it"}
    assert cli(a.root, "rm", fid).code == 0
    g = cli(b.root, "get", fid)
    assert g.code == 2 and "was deleted" in g.err


@pytest.mark.parametrize("status,code,want", [
    (403, "forbidden", 6), (403, "pending", 3), (401, "revoked", 3), (401, "unauthenticated", 3),
    (403, "refused", 3), (404, "not_found", 2), (410, "deleted", 2), (0, "network", 4), (500, "x", 1),
])
def test_api_error_exit_codes(sharing, status, code, want):
    assert sharing.ApiError(status, code).exit_code == want


def test_rm_keeps_a_pending_refusal_at_exit_3(cli, dev_repo, sharing, monkeypatch):
    def pending(self, path):
        raise sharing.ApiError(403, "pending", "approve this device's fingerprint in the web UI")
    monkeypatch.setattr(sharing.Api, "delete", pending)
    r = cli(dev_repo.root, "rm", "FILE1", "--json")
    assert r.code == 3 and r.json()["error"] == "pending"


def test_uninstall_revokes_and_cleans_up(cli, dev_repo, live_server):
    exclude = dev_repo.root / ".git" / "info" / "exclude"
    shutil.copy2(REPO / "skill" / "sharing" / "tickets-SKILL.md", dev_repo.config_path.parent)
    assert cli(dev_repo.root, "whoami").code == 0   # installs .claude/skills/sharing-tickets/
    assert (dev_repo.root / ".claude" / "skills" / "sharing-tickets" / "SKILL.md").is_file()
    assert "/.claude/skills/sharing-tickets/" in exclude.read_text().splitlines()
    r = cli(dev_repo.root, "uninstall")
    assert r.code == 0, r.err
    assert not (dev_repo.root / ".claude" / "skills" / "sharing").exists()
    assert not (dev_repo.root / ".claude" / "skills" / "sharing-tickets").exists()
    assert not (dev_repo.root / "share").exists()
    assert "/.claude/skills/sharing/" not in exclude.read_text()
    assert "/.claude/skills/sharing-tickets/" not in exclude.read_text()
    assert "/share/" not in exclude.read_text()
    s = httpx.get(live_server.url + "/api/whoami", headers={"Authorization": f"Bearer {dev_repo.token}"})
    assert s.status_code == 401


def test_uninstall_keeps_share_excluded_while_it_has_files(cli, dev_repo):
    (dev_repo.root / "share" / "kept.txt").write_text("k")
    assert cli(dev_repo.root, "uninstall").code == 0
    lines = (dev_repo.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/share/" in lines and "/.claude/skills/sharing/" not in lines
    assert (dev_repo.root / "share" / ".gitignore").read_bytes() == b"*\n"


def test_uninstall_offline_needs_force(cli, dev_repo, sharing):
    _rewrite_config(sharing, dev_repo, server_url="http://127.0.0.1:9")
    r = cli(dev_repo.root, "uninstall")
    assert r.code == 4 and "--force" in r.err
    assert (dev_repo.root / ".claude" / "skills" / "sharing").exists()
    assert cli(dev_repo.root, "uninstall", "--force").code == 0
    assert not (dev_repo.root / ".claude" / "skills" / "sharing").exists()


def test_update_when_already_current(cli, dev_repo):
    # as the installer leaves it (a missing tickets-SKILL.md would be restored, see test_update.py)
    shutil.copy2(REPO / "skill" / "sharing" / "tickets-SKILL.md", dev_repo.config_path.parent)
    r = cli(dev_repo.root, "update")
    assert r.code == 0 and "already up to date (2.1.1)" in r.out
    j = cli(dev_repo.root, "update", "--check", "--json").json()
    assert j == {"old": "2.1.1", "new": "2.1.1", "changed": False, "update_available": False}


# ---- acceptance items beyond the brief

def test_config_json_old_is_never_shareable_with_or_without_allow_secret(cli, dev_repo, sim, sharing):
    old = dev_repo.config_path.parent / sharing.OLD_CONFIG
    sharing.write_config(old, json.loads(dev_repo.config_path.read_text()))   # owner-only, as --force leaves it
    rel = ".claude/skills/sharing/config.json.old"
    for extra in ((), ("--allow-secret",), ("--allow-secret", "--yes")):
        r = cli(dev_repo.root, "share", rel, *extra)
        assert r.code == 6 and "never" in r.err, (extra, r.err)
    r = cli(dev_repo.root / ".claude" / "skills" / "sharing", "share", "config.json.old", "--allow-secret")
    assert r.code == 6 and "never" in r.err
    assert sim.http.get("/api/files").json()["files"] == []


def test_tampered_last_chunk_leaves_no_file_or_temp_in_share(cli, make_device, sim, live_server, sharing):
    a, b = make_device("dev-a"), make_device("dev-b")
    (a.root / "big.bin").write_bytes(os.urandom(sharing.CHUNK + 5000))
    fid = cli(a.root, "share", "big.bin").out.strip()
    p = live_server.blob_path(sim.get_file(fid)["uuid"])
    raw = bytearray(p.read_bytes())
    raw[-5] ^= 0x01   # the LAST chunk: the first chunk's plaintext is already written when this fails
    p.write_bytes(bytes(raw))
    r = cli(b.root, "get", fid)
    assert r.code == 5
    assert sorted(x.name for x in (b.root / "share").iterdir()) == [".gitignore"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_get_output_is_never_executable_even_over_an_executable_file(cli, dev_repo):
    (dev_repo.root / "run.sh").write_text("echo hi\n")
    os.chmod(dev_repo.root / "run.sh", 0o755)
    fid = cli(dev_repo.root, "share", "run.sh").out.strip()
    (dev_repo.root / "share" / "run.sh").write_text("old")
    os.chmod(dev_repo.root / "share" / "run.sh", 0o755)
    assert cli(dev_repo.root, "get", fid, "--force").code == 0
    assert (dev_repo.root / "share" / "run.sh").read_text() == "echo hi\n"
    assert stat.S_IMODE((dev_repo.root / "share" / "run.sh").stat().st_mode) == 0o644
