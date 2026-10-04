import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from fileshare import clock
from fileshare.app import create_app
from fileshare.settings import Settings
from tests.helpers.onboard import onboard_device, setup_owner

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", public_url="http://testserver", cookie_secure=False)


@pytest.fixture
def app(settings, request):
    """The app under test. Its pusher is a NullPusher, so no test starts SubprocessPusher threads by
    accident; a test of the real pusher is marked `real_pusher`. Tests that record pushes replace
    app.state.pusher themselves."""
    app = create_app(settings)
    if request.node.get_closest_marker("real_pusher") is None:
        from fileshare.tickets import NullPusher
        app.state.pusher = NullPusher()
    return app


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
        yield c


@pytest.fixture
def frozen_clock(monkeypatch):
    state = {"now": datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(clock, "now", lambda: state["now"])

    def set_now(dt):
        state["now"] = dt

    return set_now


@pytest.fixture(scope="session")
def sharing():
    # One module object for the fixture and tests/helpers (BrowserSim), whichever loads first.
    from tests.helpers.sharing_mod import load
    return load()


@pytest.fixture
def owner(client, settings, sharing):
    return setup_owner(client, settings, sharing)


@pytest.fixture
def session_client(owner):
    return owner.client


@pytest.fixture
def device(owner, sharing):
    # An APPROVED device. Needs POST /api/devices/{id}/approve (Task 11); no Task 10 test uses it.
    return onboard_device(owner, sharing, name="laptop", project="proj")


@dataclass
class LiveServer:
    url: str
    data_dir: Path
    env: dict
    log_path: Path

    def setup_code(self) -> str:
        out = subprocess.run([sys.executable, "-m", "fileshare.admin", "setup-code"], env=self.env,
                             cwd=REPO, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line.strip()][-1].strip()

    def blob_path(self, uuid_hex: str) -> Path:
        return self.data_dir / "blobs" / uuid_hex[:2] / uuid_hex[2:4] / f"{uuid_hex}.shr"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def run_live_server(tmp_path: Path, **env_overrides: str) -> Iterator[LiveServer]:
    """A real uvicorn on a free loopback port. The CLI speaks urllib, so TestClient can't serve it.

    env_overrides (e.g. FS_SKILL_DIR=...) let a test module run a differently configured server by
    overriding the `live_server` fixture with its own `with run_live_server(tmp_path, ...)`.
    """
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    data = tmp_path / "server-data"
    data.mkdir(exist_ok=True)
    env = {**os.environ, "FS_DATA_DIR": str(data), "FS_PUBLIC_URL": url, "FS_COOKIE_SECURE": "0",
           "FS_SKILL_DIR": str(REPO / "skill" / "sharing"), "NO_PROXY": "127.0.0.1,localhost",
           **env_overrides}
    log_path = tmp_path / "server.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--factory", "fileshare.app:create_app",
             "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
            env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("server exited early:\n" + log_path.read_text())
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                        if r.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("server never became healthy:\n" + log_path.read_text())
                time.sleep(0.1)
            yield LiveServer(url=url, data_dir=data, env=env, log_path=log_path)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def live_server(tmp_path):
    with run_live_server(tmp_path) as srv:
        yield srv


@pytest.fixture
def sim(live_server):
    """The owner's browser, scripted: setup done (iterations=1000), session cookie held, `.mk` known.

    The one BrowserSim fixture for every test directory (client, e2e, browser). A test module that
    needs seeded data overrides it as `def sim(sim): ...; return sim`.
    """
    from tests.helpers.browser_sim import BrowserSim
    s = BrowserSim(live_server.url)
    s.setup(live_server.setup_code())
    return s
