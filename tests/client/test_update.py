import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tests.conftest import REPO, run_live_server


@pytest.fixture
def skill_v2(tmp_path):
    """A copy of skill/sharing/ advertising VERSION 9.9.9, as a future release would."""
    dst = tmp_path / "skill-v2"
    shutil.copytree(REPO / "skill" / "sharing", dst)
    p = dst / "sharing.py"
    text = p.read_text()
    assert 'VERSION = "2.1.1"' in text
    p.write_text(text.replace('VERSION = "2.1.1"', 'VERSION = "9.9.9"', 1))
    (dst / "SKILL.md").write_text("# sharing 9.9.9\n")
    (dst / "tickets-SKILL.md").write_text("---\nname: sharing-tickets\n---\n# tickets 9.9.9\n")
    return dst


@pytest.fixture
def live_server(tmp_path, skill_v2):
    with run_live_server(tmp_path, FS_SKILL_DIR=str(skill_v2)) as srv:
        yield srv


@pytest.fixture
def installed(dev_repo):
    """The device's skill folder as the installer leaves it: today's files next to config.json."""
    skill = dev_repo.config_path.parent
    for name in ("sharing.py", "sharing", "tickets-SKILL.md"):
        shutil.copy2(REPO / "skill" / "sharing" / name, skill / name)
    return dev_repo


def _snapshot(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir()) if p.is_file()}


def test_whoami_reports_update_available(cli, installed):
    j = cli(installed.root, "whoami", "--json").json()
    assert j["skill_version"] == "2.1.1"
    assert j["latest_skill_version"] == "9.9.9" and j["update_available"] is True
    assert "update available: 9.9.9" in cli(installed.root, "whoami").out


def test_check_only_changes_nothing(cli, installed):
    skill = installed.config_path.parent
    before = _snapshot(skill)
    r = cli(installed.root, "update", "--check")
    assert r.code == 0 and "update available: 2.1.1 → 9.9.9" in r.out
    assert _snapshot(skill) == before


def test_update_replaces_files_verifies_hashes_and_keeps_config(cli, installed, skill_v2, sharing, monkeypatch):
    skill = installed.config_path.parent
    config_before = installed.config_path.read_bytes()
    order = []
    real_replace = os.replace

    def spy(src, dst):
        if Path(dst).parent == skill:   # not the sharing-tickets copy the reload makes afterwards
            order.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(sharing.os, "replace", spy)
    r = cli(installed.root, "update")
    monkeypatch.undo()
    assert r.code == 0, r.err
    assert "updated 2.1.1 → 9.9.9" in r.out and "new Claude Code session" in r.out
    for name in ("SKILL.md", "tickets-SKILL.md", "sharing", "sharing.py"):
        assert (skill / name).read_bytes() == (skill_v2 / name).read_bytes()
    assert order[-1] == "sharing.py"
    assert order == [n for n in ("SKILL.md", "tickets-SKILL.md", "sharing", "sharing.cmd", "sharing.py")
                     if n in order]
    # the next CLI start installs the new tickets skill where Claude Code finds it
    tickets = installed.root / ".claude" / "skills" / "sharing-tickets" / "SKILL.md"
    assert tickets.read_bytes() == (skill_v2 / "tickets-SKILL.md").read_bytes()
    if os.name != "nt":
        assert (skill / "sharing").stat().st_mode & 0o111 == 0o111   # the wrapper stays executable
        assert (skill / "sharing.py").stat().st_mode & 0o111 == 0
    assert installed.config_path.read_bytes() == config_before
    assert (skill / ".gitignore").read_bytes() == b"*\n"
    assert not [p for p in skill.iterdir() if p.name.startswith(".update-")]
    assert cli(installed.root, "whoami").code == 0


def test_hash_mismatch_replaces_nothing(cli, installed, sharing, monkeypatch):
    skill = installed.config_path.parent
    before = _snapshot(skill)
    real = sharing.Api.get_json

    def tampered(self, path):
        m = real(self, path)
        if path == "/skill/manifest.json":
            m["files"]["sharing.py"] = hashlib.sha256(b"not the file").hexdigest()
        return m

    monkeypatch.setattr(sharing.Api, "get_json", tampered)
    r = cli(installed.root, "update")
    assert r.code == 5 and "sharing.py" in r.err and "nothing was replaced" in r.err
    assert _snapshot(skill) == before
    assert not [p for p in skill.iterdir() if p.name.startswith(".update-")]


def test_malformed_manifest_is_an_integrity_error(cli, installed, sharing, monkeypatch):
    monkeypatch.setattr(sharing.Api, "get_json",
                        lambda self, path: {"version": "9.9.9", "files": {"SKILL.md": "00"}}
                        if path == "/skill/manifest.json" else pytest.fail(path))
    assert cli(installed.root, "update").code == 5


def test_update_from_an_old_version_restores_the_missing_tickets_skill(cli, installed, skill_v2, sharing,
                                                                        monkeypatch):
    """A 1.5.x `sharing update` never downloads tickets-SKILL.md, so the 1.7.0 it installs starts without
    one. Running `sharing update` again at the same version fetches just that file, verified."""
    skill = installed.config_path.parent
    (skill / "tickets-SKILL.md").unlink()
    monkeypatch.setattr(sharing, "VERSION", "9.9.9")   # already current, like the new code after the update
    before = _snapshot(skill)
    assert cli(installed.root, "update", "--check", "--json").json()["changed"] is False
    assert _snapshot(skill) == before
    r = cli(installed.root, "update", "--json")
    assert r.code == 0, r.err
    j = r.json()
    assert j["changed"] is True and j["old"] == j["new"] == "9.9.9"
    after = _snapshot(skill)
    assert after.pop("tickets-SKILL.md") == (skill_v2 / "tickets-SKILL.md").read_bytes()
    assert after == before   # nothing else was touched
    tickets = installed.root / ".claude" / "skills" / "sharing-tickets" / "SKILL.md"
    assert tickets.read_bytes() == (skill_v2 / "tickets-SKILL.md").read_bytes()
    assert "already up to date" in cli(installed.root, "update").out


def test_restoring_the_tickets_skill_checks_its_hash(cli, installed, sharing, monkeypatch):
    skill = installed.config_path.parent
    (skill / "tickets-SKILL.md").unlink()
    monkeypatch.setattr(sharing, "VERSION", "9.9.9")
    real = sharing.Api.get_json

    def tampered(self, path):
        m = real(self, path)
        if path == "/skill/manifest.json":
            m["files"]["tickets-SKILL.md"] = hashlib.sha256(b"not the file").hexdigest()
        return m

    monkeypatch.setattr(sharing.Api, "get_json", tampered)
    r = cli(installed.root, "update")
    assert r.code == 5 and "tickets-SKILL.md" in r.err
    assert not (skill / "tickets-SKILL.md").exists()
