import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class DeviceRepo:
    root: Path
    device_id: str
    token: str
    name: str
    project: str
    config_path: Path


@dataclass
class CliResult:
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(autouse=True)
def _not_an_agent(sharing, monkeypatch):
    """The suite may itself run inside an agent harness: human-only commands must see a clean environment
    unless a test sets a marker on purpose."""
    for var in sharing._agent_env():
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _never_real_deepgram(monkeypatch):
    """§20: this CLI no longer talks to Deepgram at all, but a stray DEEPGRAM_API_KEY in a
    developer's shell must never leak into a test's redaction assertions."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)


@pytest.fixture
def cli(sharing, monkeypatch):
    def run(cwd: Path, *args: str, stdin: bytes = b"") -> CliResult:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
        with redirect_stdout(out), redirect_stderr(err):
            code = sharing.main([str(a) for a in args])
        return CliResult(code, out.getvalue(), err.getvalue())
    return run


def git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture(autouse=True)
def fast_approval(sharing, monkeypatch):
    monkeypatch.setattr(sharing, "APPROVAL_POLL_S", 0.05)


@pytest.fixture
def make_device(live_server, sim, tmp_path, sharing, cli):
    def _make(name: str = "dev-a", project: str = "proj-a", approve: bool = True) -> DeviceRepo:
        root = git_init(tmp_path / "repos" / name)
        code, token_id = sim.onboarding_code()
        args = ["_handshake", "--code", "-", "--server", live_server.url, "--repo", root,
                "--skill-dir", root / ".claude" / "skills" / "sharing", "--device", name,
                "--project", project, "--hostname", "host-" + name]
        if approve:
            with ThreadPoolExecutor(1) as ex:
                fut = ex.submit(sim.approve_when_pending, token_id)
                r = cli(root, *args, stdin=code.encode())
                fut.result(60)
        else:
            r = cli(root, *args, "--no-wait", stdin=code.encode())
        assert r.code == 0, r.err
        path = root / sharing.REPO_CONFIG
        data = json.loads(path.read_text())
        return DeviceRepo(root, data["device_id"], data["device_token"], name, project, path)
    return _make


@pytest.fixture
def dev_repo(make_device):
    return make_device()


@dataclass
class FakeOrch:
    path: Path
    log: Path

    def _lines(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def calls(self) -> list[list[str]]:
        return [c["argv"] for c in self._lines()]

    def file_texts(self) -> list[str | None]:
        """For each call, the text of the --file / --body-file it was given (read when it ran)."""
        return [c["file"] for c in self._lines()]


FAKE_ORCH = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path(__file__).with_name("orch-calls.jsonl")
args = sys.argv[1:]
text = None
for flag in ("--file", "--body-file"):
    if flag in args:
        text = Path(args[args.index(flag) + 1]).read_text(encoding="utf-8")
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": args, "file": text}) + "\n")
if args and args[0] == "new":
    n = sum(1 for line in open(log, encoding="utf-8") if json.loads(line)["argv"][:1] == ["new"])
    print(json.dumps({"id": f"DEMO-{n:04d}", "status": "backlog"}))
else:
    print("{}")
'''


@pytest.fixture
def fake_orch(tmp_path):
    """An `orch` that records its argv (one JSON line per call) and answers `new` with DEMO-0001, DEMO-0002, …"""
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "orch"
    p.write_text(FAKE_ORCH, encoding="utf-8")
    p.chmod(0o755)
    return FakeOrch(p, d / "orch-calls.jsonl")
