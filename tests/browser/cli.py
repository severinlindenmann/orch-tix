import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skill" / "sharing" / "sharing.py"
FP_RE = r"\b([A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4})\b"


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _argv(repo: Path, base_url: str, args: tuple[str, ...]) -> list[str]:
    """`_handshake` gets the flags the installer would pass (Task 14): the server, the repo, and a
    skill dir INSIDE the repo. Without `--skill-dir` it would default to skill/sharing/ of this
    source tree and write config.json there."""
    argv = list(args)
    if argv and argv[0] == "_handshake":
        argv += ["--server", base_url, "--repo", str(repo),
                 "--skill-dir", str(repo / ".claude" / "skills" / "sharing")]
    return [sys.executable, str(SKILL), *argv]


def _env() -> dict:
    return {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "PYTHONUNBUFFERED": "1"}


def run_cli(repo: Path, base_url: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run the real CLI (the dev venv has `cryptography`) inside `repo` and wait for it."""
    return subprocess.run(_argv(repo, base_url, args), cwd=repo, env=_env(),
                          capture_output=True, text=True, timeout=timeout)


class CliProc:
    """A CLI running in the background (e.g. `_handshake` waiting for approval), stdout+stderr merged."""

    def __init__(self, popen: subprocess.Popen):
        self.p = popen
        self.lines: list[str] = []
        self._done = False
        self._cv = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.p.stdout:
            with self._cv:
                self.lines.append(line)
                self._cv.notify_all()
        with self._cv:
            self._done = True
            self._cv.notify_all()

    @property
    def output(self) -> str:
        with self._cv:
            return "".join(self.lines)

    def wait_for(self, pattern: str, timeout: float = 30) -> re.Match:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for line in self.lines:
                    m = rx.search(line)
                    if m:
                        return m
                left = deadline - time.monotonic()
                if self._done or left <= 0:
                    raise AssertionError(f"{pattern!r} not in CLI output:\n{''.join(self.lines)}")
                self._cv.wait(left)

    def wait_for_fingerprint(self, timeout: float = 30) -> str:
        return self.wait_for(FP_RE, timeout).group(1)

    def wait(self, timeout: float = 60) -> int:
        return self.p.wait(timeout)

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait(10)


def start_cli(repo: Path, base_url: str, *args: str) -> CliProc:
    p = subprocess.Popen(_argv(repo, base_url, args), cwd=repo, env=_env(), text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return CliProc(p)


def onboard_approved(sim, base_url: str, repo: Path, name: str, project: str) -> None:
    """Onboard `repo` as an approved device without the UI: CLI handshake + BrowserSim approval."""
    code, token_id = sim.onboarding_code()
    proc = start_cli(repo, base_url, "_handshake", "--code", code, "--device", name, "--project", project)
    try:
        proc.wait_for_fingerprint()
        sim.approve_when_pending(token_id)
        assert proc.wait(60) == 0, proc.output
    finally:
        proc.kill()
