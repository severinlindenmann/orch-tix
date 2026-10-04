"""End to end on Windows: the real PowerShell installer run the way the web UI tells the user to,
approval by the owner's browser, then sharing.cmd share on one repo and get on another (Task 23)."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows installer end to end")]

SHELLS = [s for s in ("powershell.exe", "pwsh") if shutil.which(s)]
GIT_BASH = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _argv(shell: str, url: str, code: str, *extra: str) -> list[str]:
    cmd = f"& ([scriptblock]::Create((irm {url}/onboarding.ps1))) '{code}' -Yes {' '.join(extra)}".strip()
    return [shutil.which(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]


def _env(url: str) -> dict:
    return {**os.environ, "SHARING_SERVER": url}


def _approve_while_running(sim, proc, token_id: str, timeout_s: int = 240):
    """Like sim.approve_when_pending, but gives up as soon as the installer dies, instead of
    waiting out the timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        dev = sim.request("GET", f"/api/onboarding-tokens/{token_id}").json().get("device")
        if dev and dev["status"] == "pending":
            sim.approve(dev["id"])
            return dev
        time.sleep(1)
    proc.kill()
    raise TimeoutError("the installer never reached the pending state")


def _onboard(shell, live_server, sim, repo: Path, tmp_path: Path, *extra: str) -> dict:
    code, token_id = sim.onboarding_code()
    log = tmp_path / f"{repo.name}-{shell}.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(_argv(shell, live_server.url, code, *extra), cwd=repo,
                                env=_env(live_server.url), stdout=fh, stderr=subprocess.STDOUT)
        dev = _approve_while_running(sim, proc, token_id)
        proc.wait(timeout=300)
    out = log.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode == 0, out
    assert dev is not None, out
    assert dev["fingerprint"] in out, "the installer must show the same fingerprint the owner approved"
    assert "sharing: done." in out, out
    return dev


def _skill(repo: Path) -> Path:
    return repo / ".claude" / "skills" / "sharing"


def _cmd(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(_skill(repo) / "sharing.cmd"), *args], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)


@pytest.mark.parametrize("shell", SHELLS)
def test_install_approve_share_get(shell, live_server, sim, sharing, tmp_path):
    repo_a = _git_init(tmp_path / "repo-a")
    repo_b = _git_init(tmp_path / "repo-b")
    _onboard(shell, live_server, sim, repo_a, tmp_path)
    _onboard(shell, live_server, sim, repo_b, tmp_path)

    for repo in (repo_a, repo_b):
        cfg_path = _skill(repo) / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert cfg["mk"] and "pending_privkey" not in cfg
        sharing.check_permissions(cfg_path)                                   # owner-only ACL
        assert (_skill(repo) / ".gitignore").read_bytes() == b"*\n"          # no BOM, LF
        assert (repo / "share" / ".gitignore").read_bytes() == b"*\n"
        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert "/.claude/skills/sharing/" in exclude and "/share/" in exclude
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout
        assert ".claude" not in status and "share/" not in status

    payload = "Grüezi from Windows\r\nline two\r\n".encode("utf-8")
    (repo_a / "note.md").write_bytes(payload)
    up = _cmd(repo_a, "share", "note.md", "-m", "from windows", "--json")
    assert up.returncode == 0, up.stdout + up.stderr
    file_id = json.loads(up.stdout)["id"]

    down = _cmd(repo_b, "get", file_id, "--json")
    assert down.returncode == 0, down.stdout + down.stderr
    got = Path(json.loads(down.stdout)["path"])
    assert got.read_bytes() == payload                                      # byte-exact, CRLF kept
    assert got.resolve().parent == (repo_b / "share").resolve()

    # `& exit /b` in sharing.cmd must keep the CLI's failure exit code, not turn it into 0.
    bad = _cmd(repo_b, "get", "no-such-file", "--json")
    assert bad.returncode != 0, bad.stdout + bad.stderr

    # Claude Code on Windows runs its shell commands in Git Bash, so the POSIX wrapper must work too.
    if GIT_BASH.exists():
        r = subprocess.run([str(GIT_BASH), "-c", ".claude/skills/sharing/sharing whoami"], cwd=repo_b,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_failed_preflight_leaves_the_code_unspent(shell, live_server, sim, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    code, token_id = sim.onboarding_code()
    r = subprocess.run(_argv(shell, live_server.url, code), cwd=plain, env=_env(live_server.url),
                       capture_output=True, text=True, timeout=180)
    assert r.returncode != 0
    assert "inside a git repository" in r.stdout + r.stderr
    tok = sim.request("GET", f"/api/onboarding-tokens/{token_id}").json()
    assert tok["used_at"] is None and tok["device"] is None
    assert not (plain / ".claude").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_existing_identity_is_refused_without_force(shell, live_server, sim, tmp_path):
    repo = _git_init(tmp_path / "repo")
    _onboard(shell, live_server, sim, repo, tmp_path)
    before = (_skill(repo) / "config.json").read_bytes()

    code, token_id = sim.onboarding_code()
    r = subprocess.run(_argv(shell, live_server.url, code), cwd=repo, env=_env(live_server.url),
                       capture_output=True, text=True, timeout=180)
    assert r.returncode != 0
    assert "already onboarded" in r.stdout + r.stderr
    assert (_skill(repo) / "config.json").read_bytes() == before
    assert sim.request("GET", f"/api/onboarding-tokens/{token_id}").json()["used_at"] is None
