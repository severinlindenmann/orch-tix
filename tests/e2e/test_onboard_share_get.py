import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.helpers.browser_sim import BrowserSim

pytestmark = pytest.mark.e2e

if sys.platform == "win32":
    pytest.skip("the bash installer runs on POSIX; Windows is covered by Task 23", allow_module_level=True)
for tool in ("uv", "curl", "git", "bash"):
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not on PATH", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_REL = Path(".claude/skills/sharing")
INSTALL = 'curl -fsS "$SHARING_SERVER/onboarding.txt" | bash -s -- "$CODE" "$@"'
V3_KEYS = {"v", "device_id", "device_name", "project", "server_url", "device_token", "mk", "key_version"}
TIMEOUT = 600   # the first run resolves `cryptography` into uv's cache before the handshake
APPROVED_TIMEOUT = 60   # after approval the installer only fetches the bundle and writes config


# --- helpers -----------------------------------------------------------------------------

def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def device_env(live_server) -> dict:
    env = os.environ.copy()
    env["SHARING_SERVER"] = live_server.url
    env.pop("SHARING_CONFIG_HOME", None)   # gone in v3; make sure nothing relies on it
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the installer and everything it started (curl, bash, uv, the CLI): its own session."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


class Install:
    """The installer in the background. Output goes to a file, so a chatty uv can't fill a pipe.

    It runs in its own session, so killing it kills its whole process group, and every instance is
    registered for the `_reap_installers` fixture, which aborts any still running at teardown.
    """

    running: list["Install"] = []

    def __init__(self, repo: Path, env: dict, code: str, *extra: str, log_dir: Path):
        self.log = log_dir / f"install-{time.monotonic_ns()}.log"
        self._fh = self.log.open("wb")
        self.proc = subprocess.Popen(["bash", "-c", INSTALL, "install", *extra], cwd=repo,
                                     env={**env, "CODE": code}, start_new_session=True,
                                     stdout=self._fh, stderr=subprocess.STDOUT)
        Install.running.append(self)

    def _output(self) -> str:
        self._fh.close()
        return self.log.read_text(errors="replace")

    def wait(self, timeout: float = TIMEOUT) -> tuple[int, str]:
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(self.proc)
            pytest.fail(f"installer still running after {timeout}s:\n" + self._output())
        return rc, self._output()

    def abort(self) -> str:
        _kill_group(self.proc)
        return self._output()


@pytest.fixture(autouse=True)
def _reap_installers():
    """No test path, failed asserts included, leaves a live installer behind."""
    Install.running.clear()
    yield
    for inst in Install.running:
        if inst.proc.poll() is None:
            inst.abort()
        else:
            inst._fh.close()
    Install.running.clear()


def install_sync(repo: Path, env: dict, code: str, *extra: str) -> subprocess.CompletedProcess:
    """For installs that must stop before the handshake (preflight refusals)."""
    args = ["bash", "-c", INSTALL, "install", *extra]
    proc = subprocess.Popen(args, cwd=repo, env={**env, "CODE": code}, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        pytest.fail(f"installer still running after {TIMEOUT}s:\n{out}{err}")
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def onboard(repo: Path, env: dict, sim: BrowserSim, log_dir: Path, *extra: str, code=None):
    """Run the installer and approve the device it creates, the way Severin would in the browser."""
    code, token_id = code or sim.onboarding_code()
    inst = Install(repo, env, code, *extra, log_dir=log_dir)
    device = wait_for_pending(sim, token_id, inst)   # fails fast if the installer dies first
    try:
        sim.approve(device["id"])
    except Exception as e:
        pytest.fail(f"approving {device['id']} failed: {e!r}\n{inst.abort()}")
    rc, out = inst.wait(timeout=APPROVED_TIMEOUT)   # uv's cache is warm by now
    return rc, out, device


def wait_for_pending(sim: BrowserSim, token_id: str, inst: Install) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        r = sim.request("GET", f"/api/onboarding-tokens/{token_id}")
        assert r.status_code == 200, f"GET onboarding token {token_id} -> {r.status_code} {r.text}"
        dev = r.json()["device"]
        if dev is not None:
            assert dev["status"] == "pending", dev
            return dev
        if inst.proc.poll() is not None:
            pytest.fail("installer exited before the handshake:\n" + inst.wait()[1])
        time.sleep(0.5)
    pytest.fail("device never became pending:\n" + inst.abort())


def cli(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(repo / SKILL_REL / "sharing"), *args], cwd=repo,
                          env=env, capture_output=True, text=True, timeout=300)


def config_path(repo: Path) -> Path:
    return repo / SKILL_REL / "config.json"


def repo_config(repo: Path) -> dict:
    return json.loads(config_path(repo).read_text())


def token_used(sim: BrowserSim, token_id: str) -> bool:
    return sim.request("GET", f"/api/onboarding-tokens/{token_id}").json()["used_at"] is not None


def statuses(sim: BrowserSim) -> dict:
    return {d["id"]: d["status"] for d in sim.devices()}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def assert_never_committable(repo: Path, secret: str) -> None:
    """The skill folder and share/ are invisible to git, even to `git add -A`."""
    status = git(repo, "status", "--porcelain", "--ignored=matching", "--untracked-files=all").splitlines()
    skill = SKILL_REL.as_posix()
    assert f"!! {skill}/" in status or f"!! {skill}/config.json" in status, status
    visible = [line for line in status if not line.startswith("!! ")
               and (skill in line or line[3:].startswith("share/"))]
    assert visible == [], visible
    git(repo, "add", "-A")
    try:
        staged = git(repo, "diff", "--cached", "--name-only").splitlines()
        assert [p for p in staged if p.startswith(skill) or p.startswith("share/")] == [], staged
        for p in staged:
            assert secret.encode() not in (repo / p).read_bytes(), p
    finally:
        git(repo, "reset", "-q")


# --- tests -----------------------------------------------------------------------------------

def test_onboard_approve_share_get_revoke(live_server, sim, sharing, tmp_path):
    repo_a, repo_b = make_repo(tmp_path, "repo-a"), make_repo(tmp_path, "repo-b")
    env = device_env(live_server)

    # Onboard A with explicit names, and B with an auto-detected project. Each waits for approval.
    rc, out_a, dev_a = onboard(repo_a, env, sim, tmp_path, "--device", "laptop-a", "--project", "repo-a", "--yes")
    assert rc == 0, out_a
    assert dev_a["fingerprint"] in out_a, "the installer must print what the browser compares"
    rc, out_b, dev_b = onboard(repo_b, env, sim, tmp_path, "--device", "laptop-b", "--yes")
    assert rc == 0, out_b
    assert statuses(sim)[dev_a["id"]] == statuses(sim)[dev_b["id"]] == "active"

    for repo, name, project, dev in ((repo_a, "laptop-a", "repo-a", dev_a),
                                     (repo_b, "laptop-b", "repo-b", dev_b)):
        cfg = repo_config(repo)
        assert set(cfg) == V3_KEYS, "pending_privkey must be gone once MK arrived"
        assert cfg["v"] == 1
        assert (cfg["device_id"], cfg["device_name"], cfg["project"], cfg["server_url"]) == \
            (dev["id"], name, project, live_server.url)
        assert cfg["device_token"].startswith("shd_")
        assert cfg["mk"] == sharing.b64u(sim.mk) and cfg["key_version"] == 1
        assert stat.S_IMODE(config_path(repo).stat().st_mode) == 0o600
        assert (repo / SKILL_REL / ".gitignore").read_text() == "*\n"
        assert (repo / "share" / ".gitignore").read_text() == "*\n"
        exclude_path = repo / ".git" / "info" / "exclude"
        exclude = exclude_path.read_text()
        assert "/.claude/skills/sharing/" in exclude.splitlines() and "/share/" in exclude.splitlines()

        assert_never_committable(repo, cfg["device_token"])
        exclude_path.write_text("")               # the folders' own .gitignore alone must suffice
        assert_never_committable(repo, cfg["device_token"])
        exclude_path.write_text(exclude)

        who = cli(repo, env, "whoami", "--json")
        assert who.returncode == 0, who.stderr
        assert json.loads(who.stdout)["device_id"] == cfg["device_id"]

    # A shares a multi-chunk file. B gets it byte-for-byte, twice (the second get reuses the identical copy, QA TF-19; only different content gets the FILE1- prefix).
    payload = b"# notes\n" + os.urandom(2 * 1024 * 1024 + 123)
    (repo_a / "notes.md").write_bytes(payload)
    r = cli(repo_a, env, "share", "notes.md", "-m", "e2e test", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["id"] == "FILE1"

    r = cli(repo_b, env, "get", "file1", "--json")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert Path(got["path"]) == repo_b / "share" / "notes.md"
    assert Path(got["path"]).read_bytes() == payload
    assert got["note"] == "e2e test"
    r = cli(repo_b, env, "get", "FILE1", "--json")
    assert Path(json.loads(r.stdout)["path"]) == repo_b / "share" / "notes.md"          # identical: reused, no pile-up
    assert sorted(p.name for p in (repo_b / "share").iterdir() if not p.name.startswith(".")) == ["notes.md"]
    (repo_b / "share" / "notes.md").write_bytes(b"edited locally")
    r = cli(repo_b, env, "get", "FILE1", "--json")
    assert Path(json.loads(r.stdout)["path"]) == repo_b / "share" / "FILE1-notes.md"    # different content: both kept
    assert (repo_b / "share" / "notes.md").read_bytes() == b"edited locally"
    assert (repo_b / "share" / ".gitignore").read_text() == "*\n"
    assert_never_committable(repo_b, repo_config(repo_b)["device_token"])

    # A browser upload (e.g. from the phone) reaches a device.
    up = sim.upload("phone.txt", b"from the phone\n", note="photo notes")
    assert up["id"] == "FILE2"
    r = cli(repo_b, env, "get", "2", "--json")
    assert r.returncode == 0, r.stderr
    assert (repo_b / "share" / "phone.txt").read_bytes() == b"from the phone\n"

    listed = json.loads(cli(repo_a, env, "list", "--all", "--json").stdout)   # B's gets acknowledged both
    assert [(f["id"], f["name"]) for f in listed] == [("FILE2", "phone.txt"), ("FILE1", "notes.md")]

    # Revoke B. Its next call exits 3.
    sim.revoke(dev_b["id"])
    r = cli(repo_b, env, "list", "--json")
    assert r.returncode == 3, r.stdout + r.stderr
    assert json.loads(r.stdout)["error"]


def test_rejected_device_exits_3_and_leaves_no_config(live_server, sim, tmp_path):
    repo = make_repo(tmp_path, "repo-r")
    env = device_env(live_server)
    code, token_id = sim.onboarding_code()
    inst = Install(repo, env, code, "--device", "stranger", "--yes", log_dir=tmp_path)
    dev = wait_for_pending(sim, token_id, inst)
    assert dev["status"] == "pending"
    sim.revoke(dev["id"])                         # Reject == revoke (§6)
    rc, out = inst.wait()
    assert rc == 3, out
    assert not config_path(repo).exists()
    assert statuses(sim)[dev["id"]] == "revoked"


def test_reinstall_without_force_refuses_and_keeps_config(live_server, sim, tmp_path):
    repo = make_repo(tmp_path, "repo-a")
    env = device_env(live_server)
    rc, out, first = onboard(repo, env, sim, tmp_path, "--device", "laptop-a", "--yes")
    assert rc == 0, out
    before = config_path(repo).read_bytes()

    code2, token2 = sim.onboarding_code()
    r = install_sync(repo, env, code2, "--device", "laptop-a2", "--yes")
    assert r.returncode != 0
    assert "--force" in r.stdout + r.stderr
    assert config_path(repo).read_bytes() == before
    assert not token_used(sim, token2), "preflight must refuse before spending the code"

    # --force: the new identity is approved first, then the old device is revoked.
    rc, out, second = onboard(repo, env, sim, tmp_path, "--device", "laptop-a2", "--force", "--yes",
                              code=(code2, token2))
    assert rc == 0, out
    cfg = repo_config(repo)
    assert (cfg["device_id"], cfg["device_name"]) == (second["id"], "laptop-a2")
    assert statuses(sim)[first["id"]] == "revoked"
    assert statuses(sim)[second["id"]] == "active"
    assert not (repo / SKILL_REL / "config.json.old").exists(), "the replaced identity is dropped after approval"
    assert_never_committable(repo, cfg["device_token"])


def test_failed_preflight_does_not_spend_the_code(live_server, sim, tmp_path):
    repo = make_repo(tmp_path, "repo-c")
    env = device_env(live_server)
    broken = tmp_path / "broken-bin"
    broken.mkdir()
    fake_uv = broken / "uv"
    fake_uv.write_text("#!/bin/sh\necho 'uv: broken for this test' >&2\nexit 1\n")
    fake_uv.chmod(0o755)

    code, token_id = sim.onboarding_code()
    r = install_sync(repo, {**env, "PATH": f"{broken}:{env['PATH']}"}, code, "--device", "laptop-c", "--yes")
    assert r.returncode != 0
    assert not (repo / SKILL_REL).exists()
    assert not token_used(sim, token_id)

    rc, out, _ = onboard(repo, env, sim, tmp_path, "--device", "laptop-c", "--yes", code=(code, token_id))
    assert rc == 0, out
    assert token_used(sim, token_id)


# --- sharing update ------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(live_server, skill_dir: Path, log: Path) -> tuple[subprocess.Popen, str]:
    """A second server on the same data dir, serving a newer skill (like a deploy would)."""
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, **live_server.env, "FS_SKILL_DIR": str(skill_dir), "FS_PUBLIC_URL": url}
    with log.open("wb") as fh:          # the child holds its own copy of the fd
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "fileshare.app:create_app",
                                 "--host", "127.0.0.1", "--port", str(port)],
                                cwd=REPO_ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    proc.kill()
    proc.wait()
    pytest.fail("second server did not start:\n" + log.read_text(errors="replace"))


def bumped_skill_copy(dst: Path) -> str:
    shutil.copytree(REPO_ROOT / "skill" / "sharing", dst)
    py = dst / "sharing.py"
    text = py.read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "(\d+)\.(\d+)\.(\d+)"', text, re.M)
    assert m, "sharing.py must define VERSION = \"X.Y.Z\""
    new = f"{m[1]}.{m[2]}.{int(m[3]) + 1}"
    py.write_text(text[:m.start()] + f'VERSION = "{new}"' + text[m.end():], encoding="utf-8")
    skill_md = dst / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\n<!-- e2e update marker -->\n",
                        encoding="utf-8")
    return new


def test_update_replaces_skill_files_but_never_config(live_server, sim, tmp_path):
    repo = make_repo(tmp_path, "repo-u")
    env = device_env(live_server)
    rc, out, _ = onboard(repo, env, sim, tmp_path, "--device", "laptop-u", "--yes")
    assert rc == 0, out

    skill_dir = tmp_path / "skill-next"
    new_version = bumped_skill_copy(skill_dir)
    proc, url2 = start_server(live_server, skill_dir, tmp_path / "server2.log")
    try:
        cfg = repo_config(repo)
        cfg["server_url"] = url2
        config_path(repo).write_text(json.dumps(cfg))     # rewritten in place: mode stays 0600
        assert stat.S_IMODE(config_path(repo).stat().st_mode) == 0o600
        before = config_path(repo).read_bytes()
        installed = repo / SKILL_REL

        chk = cli(repo, env, "update", "--check")
        assert chk.returncode == 0, chk.stdout + chk.stderr
        assert new_version in chk.stdout
        assert (installed / "SKILL.md").read_bytes() != (skill_dir / "SKILL.md").read_bytes(), \
            "--check must not replace anything"

        r = cli(repo, env, "update")
        assert r.returncode == 0, r.stdout + r.stderr
        assert new_version in r.stdout
        for name in ("SKILL.md", "sharing.py", "sharing"):
            assert (installed / name).read_bytes() == (skill_dir / name).read_bytes(), name
        assert os.access(installed / "sharing", os.X_OK)
        assert config_path(repo).read_bytes() == before
        assert stat.S_IMODE(config_path(repo).stat().st_mode) == 0o600
        assert (installed / ".gitignore").read_text() == "*\n"

        again = cli(repo, env, "update", "--check")
        assert again.returncode == 0, again.stderr
        assert "up to date" in again.stdout

        who = cli(repo, env, "whoami", "--json")              # the updated CLI keeps the same identity
        assert who.returncode == 0, who.stderr
        assert json.loads(who.stdout)["device_id"] == cfg["device_id"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
