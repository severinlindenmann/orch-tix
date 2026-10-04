"""End-to-end: a real uvicorn, a real installer and a real CLI subprocess send a file through an
upload link and the owner adopts it -- across a genuine server restart on the same data dir
(Review Focus 1: the startup sweep must not destroy a pending, not-yet-adopted upload).

Modelled on test_onboard_share_get.py: the owner is onboarded with the bash installer and a
BrowserSim playing the browser; the sender runs `upload-link put` from a bare directory that has
never seen `sharing`, exactly like a stranger who was only given the URL.
"""
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.conftest import run_live_server
from tests.e2e.test_onboard_share_get import (Install, SKILL_REL, config_path, device_env, make_repo,
                                              onboard, repo_config)
from tests.helpers.browser_sim import BrowserSim

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
PUT_SCRIPT = REPO_ROOT / "skill" / "sharing" / "sharing.py"
URL_RE = re.compile(r"^http://127\.0\.0\.1:\d+/u/[A-Za-z0-9_-]{43}#[A-Za-z0-9_-]{87}$")


@pytest.fixture(autouse=True)
def _reap_installers():
    """Mirrors test_onboard_share_get's fixture: this module runs its own installers directly."""
    Install.running.clear()
    yield
    for inst in Install.running:
        if inst.proc.poll() is None:
            inst.abort()
        else:
            inst._fh.close()
    Install.running.clear()


def _cli(repo, env, *args, timeout=300):
    return subprocess.run([str(repo / SKILL_REL / "sharing"), *args],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)


def _put(cwd, url, *paths, note=None, extra_env=None):
    """Run `upload-link put` as a stranger: the plain script, no config, no onboarding, from a
    directory that was never touched by the installer."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHARING_")}
    env["NO_PROXY"] = "127.0.0.1,localhost"
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, str(PUT_SCRIPT), "upload-link", "put", url, *paths, "--json"]
    if note:
        args += ["--note", note]
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def test_upload_link_survives_restart_and_is_adopted(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")

    # --- server #1: onboard the owner, create the link, and receive the upload -----------------
    with run_live_server(tmp_path) as srv1:
        sim = BrowserSim(srv1.url)
        sim.setup(srv1.setup_code())
        env = device_env(srv1)
        rc, out, dev_a = onboard(repo_a, env, sim, tmp_path, "--device", "owner-a", "--yes")
        assert rc == 0, out

        made = _cli(repo_a, env, "upload-link", "create", "--label", "from a stranger", "--json")
        assert made.returncode == 0, made.stderr
        link = json.loads(made.stdout)
        assert URL_RE.fullmatch(link["url"]), link["url"]

        # The sender is a stranger: a bare directory the installer, and `sharing`, never touched.
        stranger = tmp_path / "stranger"
        drop = stranger / "drop"
        (drop / "sub").mkdir(parents=True)
        (drop / "sub" / "ä.txt").write_bytes("hällo from a stranger".encode("utf-8"))
        (drop / "notes.txt").write_bytes(b"plain notes\n")
        assert not (stranger / ".claude").exists(), "the sender must have no sharing config at all"

        sent = _put(stranger, link["url"], str(drop), note="e2e drop", extra_env={"HOME": str(stranger)})
        assert sent.returncode == 0, sent.stderr
        assert json.loads(sent.stdout)["sent"] is True

        # Not yet adopted: the upload is only "pending" on the link, no FILE exists yet. Checked
        # straight through the owner's browser session (sim), never the CLI: any `sharing`
        # command (list/get/info/upload-link list) would itself adopt it right here, before the
        # restart even happens.
        rows = sim._check(sim.http.get("/api/upload-links"), 200).json()["links"]
        row = next(r for r in rows if r["id"] == link["id"])
        assert row["state"] == "pending" and row["file"] is None

    # --- a real restart: the process is gone, the data dir (sqlite + blobs) is what's left --------
    with run_live_server(tmp_path) as srv2:
        assert srv2.data_dir == srv1.data_dir, "the restart must reuse the same data dir"
        cfg = repo_config(repo_a)
        cfg["server_url"] = srv2.url                 # the new process picked a fresh port
        config_path(repo_a).write_text(json.dumps(cfg))
        env = device_env(srv2)

        listed = _cli(repo_a, env, "list", "--json")
        assert listed.returncode == 0, listed.stderr
        files = json.loads(listed.stdout)
        assert len(files) == 1, "the pending upload must have survived the restart and been adopted"
        fid = files[0]["id"]

        after = _cli(repo_a, env, "upload-link", "list", "--json")
        assert json.loads(after.stdout)[0]["state"] == "received"

        got = _cli(repo_a, env, "get", fid, "--json", "--no-ack")
        assert got.returncode == 0, got.stderr
        path = Path(json.loads(got.stdout)["path"])
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            assert names == {"drop/sub/ä.txt", "drop/notes.txt"}
            assert zf.read("drop/sub/ä.txt") == "hällo from a stranger".encode("utf-8")
            assert zf.read("drop/notes.txt") == b"plain notes\n"
