import json
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.client.conftest import git_init


def hs(cli, sharing, repo, code, server, *extra, name="dev-a", project="proj-a"):
    return cli(repo, "_handshake", "--code", "-", "--server", server, "--repo", repo,
               "--skill-dir", repo / ".claude" / "skills" / "sharing",
               "--device", name, "--project", project, "--hostname", "host-" + name, *extra,
               stdin=code.encode())


def hs_approved(cli, sharing, sim, repo, server, *extra, **kw):
    """Run the blocking handshake while the 'browser' approves from another thread."""
    code, token_id = sim.onboarding_code()
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(sim.approve_when_pending, token_id)
        r = hs(cli, sharing, repo, code, server, *extra, **kw)
        fut.result(60)
    return r, code


def whoami_status(url, token):
    return httpx.get(url + "/api/whoami", headers={"Authorization": f"Bearer {token}"}).status_code


def cfg_of(repo):
    return json.loads((repo / ".claude" / "skills" / "sharing" / "config.json").read_text())


def test_handshake_waits_for_approval_then_writes_active_config(cli, sharing, live_server, sim, tmp_path):
    repo = git_init(tmp_path / "r1")
    r, code = hs_approved(cli, sharing, sim, repo, live_server.url)
    assert r.code == 0, r.err
    assert code not in r.out + r.err

    cfg = cfg_of(repo)
    assert set(cfg) == {"v", "device_id", "device_name", "project", "server_url", "device_token", "mk",
                        "key_version"}
    assert sharing.unb64u(cfg["mk"]) == sim.mk and cfg["device_name"] == "dev-a"
    fp = next(d for d in sim.devices() if d["id"] == cfg["device_id"])["fingerprint"]
    assert fp in r.err and "Approve this device" in r.err

    skill = repo / ".claude" / "skills" / "sharing"
    if os.name != "nt":
        assert stat.S_IMODE((skill / "config.json").stat().st_mode) == 0o600
    assert (skill / ".gitignore").read_bytes() == b"*\n"
    assert (repo / "share" / ".gitignore").read_bytes() == b"*\n"
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.claude/skills/sharing/") == 1 and exclude.count("/share/") == 1
    assert cli(repo, "whoami").code == 0


def test_no_wait_leaves_pending_then_wait_completes(cli, sharing, live_server, sim, tmp_path):
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    r = hs(cli, sharing, repo, code, live_server.url, "--no-wait")
    assert r.code == 0, r.err
    cfg = cfg_of(repo)
    assert "pending_privkey" in cfg and "mk" not in cfg
    assert cli(repo, "whoami").code == 3
    assert whoami_status(live_server.url, cfg["device_token"]) == 403

    sim.approve(cfg["device_id"])
    w = cli(repo, "wait")
    assert w.code == 0, w.err
    cfg = cfg_of(repo)
    assert "pending_privkey" not in cfg and sharing.unb64u(cfg["mk"]) == sim.mk
    assert cli(repo, "whoami").code == 0
    assert cli(repo, "wait").code == 0  # already approved: no-op


def test_rejected_device_removes_config(cli, sharing, live_server, sim, tmp_path):
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    assert hs(cli, sharing, repo, code, live_server.url, "--no-wait").code == 0
    sim.revoke(cfg_of(repo)["device_id"])
    w = cli(repo, "wait")
    assert w.code == 3 and "rejected" in w.err
    assert not (repo / ".claude" / "skills" / "sharing" / "config.json").exists()


def test_wait_timeout_keeps_pending_config(cli, sharing, live_server, sim, tmp_path, monkeypatch):
    monkeypatch.setattr(sharing, "APPROVAL_TIMEOUT_S", 0.3)
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    r = hs(cli, sharing, repo, code, live_server.url)
    assert r.code == 3 and "sharing wait" in r.err
    assert "pending_privkey" in cfg_of(repo)


def test_ctrl_c_while_waiting_is_resumable(cli, sharing, live_server, sim, tmp_path, monkeypatch):
    def interrupt(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(sharing, "_sleep", interrupt)
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    r = hs(cli, sharing, repo, code, live_server.url)
    assert r.code == 3 and "sharing wait" in r.err
    monkeypatch.undo()
    monkeypatch.setattr(sharing, "APPROVAL_POLL_S", 0.05)
    sim.approve(cfg_of(repo)["device_id"])
    assert cli(repo, "wait").code == 0


def test_bundle_sealed_to_another_key_fails_exit_5_and_stays_pending(cli, sharing, make_device, sim):
    dev = make_device("dev-x", approve=False)
    _, other_pub = sharing.new_device_keypair()
    bad = sharing.seal_to_device(sim.mk, other_pub, dev.device_id)
    r = sim.request("POST", f"/api/devices/{dev.device_id}/approve", json={"device_bundle": sharing.b64u(bad)})
    assert r.status_code == 204
    w = cli(dev.root, "wait")
    assert w.code == 5
    assert "pending_privkey" in json.loads(dev.config_path.read_text())


def test_code_is_single_use(cli, sharing, live_server, sim, tmp_path):
    r1, code = hs_approved(cli, sharing, sim, git_init(tmp_path / "r1"), live_server.url)
    assert r1.code == 0, r1.err
    r2 = git_init(tmp_path / "r2")
    r = hs(cli, sharing, r2, code, live_server.url)
    assert r.code == 3 and "refused" in r.err
    assert not (r2 / ".claude" / "skills" / "sharing" / "config.json").exists()


def test_existing_identity_refused_without_force_and_code_not_spent(cli, sharing, live_server, sim,
                                                                     make_device, tmp_path):
    a = make_device("dev-a")
    code, token_id = sim.onboarding_code()
    r = hs(cli, sharing, a.root, code, live_server.url)
    assert r.code == 6 and "--force" in r.err
    assert cfg_of(a.root)["device_id"] == a.device_id
    other = git_init(tmp_path / "other")
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(sim.approve_when_pending, token_id)
        assert hs(cli, sharing, other, code, live_server.url, name="dev-b").code == 0
        fut.result(60)


def test_force_keeps_old_identity_until_new_one_is_approved(cli, sharing, live_server, sim, make_device):
    a = make_device("dev-a")
    code, _ = sim.onboarding_code()
    r = hs(cli, sharing, a.root, code, live_server.url, "--force", "--no-wait", name="dev-a2")
    assert r.code == 0, r.err
    new = cfg_of(a.root)
    assert new["device_id"] != a.device_id and new["device_name"] == "dev-a2"
    assert (a.root / ".claude/skills/sharing/config.json.old").exists()
    assert whoami_status(live_server.url, a.token) == 200

    sim.approve(new["device_id"])
    assert cli(a.root, "wait").code == 0
    assert whoami_status(live_server.url, a.token) == 401
    assert not (a.root / ".claude/skills/sharing/config.json.old").exists()
    assert cfg_of(a.root)["device_id"] == new["device_id"]


def test_force_rejected_restores_the_old_identity(cli, sharing, live_server, sim, make_device):
    a = make_device("dev-a")
    code, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, code, live_server.url, "--force", "--no-wait", name="dev-a2").code == 0
    sim.revoke(cfg_of(a.root)["device_id"])
    assert cli(a.root, "wait").code == 3
    assert cfg_of(a.root)["device_id"] == a.device_id
    assert cli(a.root, "whoami").code == 0


@pytest.mark.parametrize("bad", ["shr1.short", "shr1." + "A" * 22 + "." + "B" * 43, "nope"])
def test_bad_code_format_refused_before_network(cli, sharing, tmp_path, bad):
    repo = git_init(tmp_path / "r")
    r = hs(cli, sharing, repo, bad, "http://127.0.0.1:9")
    assert r.code == 6
    assert bad not in r.out + r.err


def test_plain_http_to_a_remote_host_is_refused(cli, sharing, tmp_path):
    repo = git_init(tmp_path / "r")
    assert hs(cli, sharing, repo, "shr1." + "A" * 22, "http://example.com").code == 6


def test_check_server_url(sharing):
    assert sharing.check_server_url("https://tix.severin.io/") == "https://tix.severin.io"
    assert sharing.check_server_url("http://127.0.0.1:8808") == "http://127.0.0.1:8808"
    assert sharing.check_server_url("http://localhost") == "http://localhost"
    for bad in ("http://tix.severin.io", "ftp://x", "https://", "file:///etc/passwd", "http://127.0.0.1.evil.com"):
        with pytest.raises(sharing.Refused):
            sharing.check_server_url(bad)


# ---- rulings beyond the brief: CRLF stdin, --force edge cases, guards on config.json.old

def test_code_from_stdin_with_crlf_is_accepted(cli, sharing, live_server, sim, tmp_path):
    """PowerShell pipes add CRLF; the code must still parse."""
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    r = cli(repo, "_handshake", "--code", "-", "--server", live_server.url, "--repo", repo,
            "--skill-dir", repo / ".claude" / "skills" / "sharing", "--device", "dev-a",
            "--project", "proj-a", "--no-wait", stdin=(code + "\r\n").encode())
    assert r.code == 0, r.err
    assert "pending_privkey" in cfg_of(repo)


def test_fingerprint_is_printed_as_five_groups_of_four(cli, sharing, live_server, sim, tmp_path):
    repo = git_init(tmp_path / "r")
    code, _ = sim.onboarding_code()
    r = hs(cli, sharing, repo, code, live_server.url, "--no-wait")
    assert r.code == 0, r.err
    fp = next(d for d in sim.devices() if d["id"] == cfg_of(repo)["device_id"])["fingerprint"]
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", fp)
    assert f"fingerprint  {fp}" in r.err


def test_old_config_is_owner_only_and_ignored_by_git(cli, sharing, live_server, sim, make_device):
    a = make_device("dev-a")
    code, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, code, live_server.url, "--force", "--no-wait", name="dev-a2").code == 0
    old = a.root / ".claude" / "skills" / "sharing" / sharing.OLD_CONFIG
    assert old.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(old.stat().st_mode) == 0o600
    untracked = subprocess.run(["git", "-C", str(a.root), "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True, check=True).stdout
    assert ".claude" not in untracked


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_old_config_with_loose_permissions_is_refused(cli, sharing, live_server, sim, make_device):
    a = make_device("dev-a")
    code, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, code, live_server.url, "--force", "--no-wait", name="dev-a2").code == 0
    os.chmod(a.root / ".claude" / "skills" / "sharing" / sharing.OLD_CONFIG, 0o644)
    assert cli(a.root, "wait").code == 6


def test_second_force_keeps_the_original_identity_aside(cli, sharing, live_server, sim, make_device):
    """--force twice before approval: config.json.old must still be the ORIGINAL active identity."""
    a = make_device("dev-a")
    c1, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, c1, live_server.url, "--force", "--no-wait", name="dev-a2").code == 0
    first_pending = cfg_of(a.root)
    c2, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, c2, live_server.url, "--force", "--no-wait", name="dev-a3").code == 0
    old = json.loads((a.root / ".claude/skills/sharing" / sharing.OLD_CONFIG).read_text())
    assert old["device_id"] == a.device_id and "mk" in old
    assert whoami_status(live_server.url, first_pending["device_token"]) == 401   # abandoned one withdrawn
    sim.revoke(cfg_of(a.root)["device_id"])
    assert cli(a.root, "wait").code == 3
    assert cfg_of(a.root)["device_id"] == a.device_id and cli(a.root, "whoami").code == 0


def test_handshake_refuses_a_tracked_skill_folder_before_spending_the_code(cli, sharing, live_server, sim,
                                                                           tmp_path):
    repo = git_init(tmp_path / "r")
    skill = repo / ".claude" / "skills" / "sharing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".claude/skills/sharing/SKILL.md"], check=True)
    code, token_id = sim.onboarding_code()
    r = hs(cli, sharing, repo, code, live_server.url, "--no-wait")
    assert r.code == 6 and "tracked by git" in r.err
    assert sim.request("GET", f"/api/onboarding-tokens/{token_id}").json()["used_at"] is None


# ---- T14 review fixes: _retire_old keeps config.json.old on transient failures; wait retries 5xx/429

def _force_then_approve(cli, sharing, live_server, sim, make_device):
    a = make_device("dev-a")
    code, _ = sim.onboarding_code()
    assert hs(cli, sharing, a.root, code, live_server.url, "--force", "--no-wait", name="dev-a2").code == 0
    sim.approve(cfg_of(a.root)["device_id"])
    return a, a.root / ".claude" / "skills" / "sharing" / sharing.OLD_CONFIG


def _failing_revoke(sharing, monkeypatch, token, status):
    real = sharing.Api.delete

    def fake(self, path):
        if path == "/api/devices/self" and self.token == token:
            raise sharing.ApiError(status, "boom", "server trouble")
        return real(self, path)

    monkeypatch.setattr(sharing.Api, "delete", fake)
    return real


@pytest.mark.parametrize("status", [0, 500, 503])
def test_retire_old_keeps_old_config_when_revoke_fails_transiently(cli, sharing, live_server, sim, make_device,
                                                                    monkeypatch, status):
    a, old = _force_then_approve(cli, sharing, live_server, sim, make_device)
    real = _failing_revoke(sharing, monkeypatch, a.token, status)
    w = cli(a.root, "wait")
    assert w.code == 0, w.err
    assert old.exists() and "warning" in w.err
    assert whoami_status(live_server.url, a.token) == 200
    monkeypatch.setattr(sharing.Api, "delete", real)
    assert cli(a.root, "wait").code == 0   # "already approved" retries the retirement
    assert not old.exists()
    assert whoami_status(live_server.url, a.token) == 401


def test_retire_old_drops_old_config_silently_when_already_revoked(cli, sharing, live_server, sim, make_device):
    a, old = _force_then_approve(cli, sharing, live_server, sim, make_device)
    sim.revoke(a.device_id)   # revoked in the web UI meanwhile: the revoke answers 401
    w = cli(a.root, "wait")
    assert w.code == 0, w.err
    assert not old.exists() and "warning" not in w.err


def test_retire_old_drops_old_config_on_404(cli, sharing, live_server, sim, make_device, monkeypatch):
    a, old = _force_then_approve(cli, sharing, live_server, sim, make_device)
    _failing_revoke(sharing, monkeypatch, a.token, 404)
    w = cli(a.root, "wait")
    assert w.code == 0, w.err
    assert not old.exists() and "warning" not in w.err


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_wait_keeps_polling_through_5xx_and_429(cli, sharing, make_device, sim, monkeypatch, status):
    dev = make_device("dev-x", approve=False)
    sim.approve(dev.device_id)
    real = sharing.Api.get_json
    calls = {"n": 0}

    def flaky(self, path):
        if path == "/api/devices/self/bundle" and calls["n"] < 3:
            calls["n"] += 1
            raise sharing.ApiError(status, "busy", "try later")
        return real(self, path)

    monkeypatch.setattr(sharing.Api, "get_json", flaky)
    w = cli(dev.root, "wait")
    assert w.code == 0, w.err
    assert calls["n"] == 3
    assert "mk" in json.loads(dev.config_path.read_text())


def test_wait_gives_up_at_the_deadline_while_the_server_keeps_failing(cli, sharing, make_device, monkeypatch):
    dev = make_device("dev-x", approve=False)
    monkeypatch.setattr(sharing, "APPROVAL_TIMEOUT_S", 0.3)

    def down(self, path):
        raise sharing.ApiError(503, "unavailable", "")

    monkeypatch.setattr(sharing.Api, "get_json", down)
    w = cli(dev.root, "wait")
    assert w.code == 3 and "sharing wait" in w.err
    assert "pending_privkey" in json.loads(dev.config_path.read_text())


def test_wait_still_fails_fast_on_other_4xx(cli, sharing, make_device, monkeypatch):
    dev = make_device("dev-x", approve=False)
    monkeypatch.setattr(sharing, "APPROVAL_TIMEOUT_S", 30)

    def bad(self, path):
        raise sharing.ApiError(400, "bad", "nope")

    monkeypatch.setattr(sharing.Api, "get_json", bad)
    assert cli(dev.root, "wait").code == 1
