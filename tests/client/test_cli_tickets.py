"""CLI side of legacy tickets: since 2.0.0 `sharing tickets` only lists, shows and migrates them (Task 12);
tickets live in orch-core. Legacy tickets are seeded through the browser session (BrowserSim), as the PWA did.
"""
import os
import stat
import subprocess
import time

import pytest

from tests.helpers.tickets import sim_ticket

QUESTIONS = {"text": "Two choices:\n", "allow_text": True, "questions": [
    {"id": "db", "text": "Which database?", "type": "single", "options": ["sqlite", "postgres"],
     "recommended": "sqlite", "required": True}]}


def tix(cli, repo, *args):
    return cli(repo.root, "tickets", *args, "--json")


def ok(r):
    assert r.code == 0, (r.code, r.out, r.err)
    return r.json()


def web_event(sim, ref, kind, body, **flags):
    """What the web UI does for a comment or a move: seal under the DEK, POST."""
    s = sim.s
    t = sim.request("GET", f"/api/tickets/{ref}").json()
    _, dek = s.open_ticket(sim.mk, t)
    eu = os.urandom(16)
    r = sim.request("POST", f"/api/tickets/{ref}/events",
                    json={"uuid": eu.hex(), "kind": kind, "enc_body": s.seal_ticket_event(dek, bytes.fromhex(t["uuid"]),
                                                                                          eu, body), **flags})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- refs, exit code 7

@pytest.mark.parametrize("raw", ["TIX-42", "tix-42", "TIX42", "tix42", "42", " 42 "])
def test_ticket_refs(sharing, raw):
    assert sharing.norm_ticket_ref(raw) == "TIX-42"


@pytest.mark.parametrize("raw", ["", "0", "TIX-", "FILE42", "TIX-4x", "-1"])
def test_bad_ticket_refs(sharing, raw):
    with pytest.raises(sharing.UsageError):
        sharing.norm_ticket_ref(raw)


@pytest.mark.parametrize("code", ["bad_move", "claimed", "claim_lost", "conflict", "duplicate_uuid"])
def test_409_codes_exit_7(sharing, code):
    assert sharing.EXIT_CONFLICT == 7
    assert sharing.ApiError(409, code, "x").exit_code == 7


# ---------------------------------------------------------------- read-only since 2.0.0

def test_version_is_2(sharing):
    assert sharing.VERSION == "2.1.1"


@pytest.mark.parametrize("cmd", ["new", "claim", "release", "update", "ask", "test", "edit", "move"])
def test_legacy_write_commands_are_gone(cli, dev_repo, cmd):
    r = tix(cli, dev_repo, cmd, "TIX-1")
    assert r.code == 1 and "invalid choice" in r.json()["detail"]


def test_wait_says_tickets_live_in_orch_core(cli, dev_repo, sim):
    ref = sim_ticket(sim, project="proj-a", title="x", status="open")
    r = tix(cli, dev_repo, "wait", ref)
    assert r.code == 6 and r.json()["error"] == "legacy_readonly"
    assert "legacy tickets are read-only; tickets now live in orch-core" in r.json()["detail"]
    assert "orch wait" in r.json()["detail"]


# ---------------------------------------------------------------- list / show

def test_list_default_hides_done_and_all_shows_it(cli, dev_repo, sim):
    a = sim_ticket(sim, project="proj-a", title="alpha", status="done", labels=["x"])
    b = sim_ticket(sim, project="proj-a", title="beta", status="open")
    rows = ok(tix(cli, dev_repo, "list"))
    assert [r["id"] for r in rows] == [b]
    assert set(rows[0]) == {"id", "title", "status", "project", "type", "priority", "labels", "claim_state",
                            "open_questions", "updated_at"}
    assert rows[0]["title"] == "beta" and rows[0]["claim_state"] is None
    assert {r["id"] for r in ok(tix(cli, dev_repo, "list", "--all"))} == {a, b}
    assert [r["id"] for r in ok(tix(cli, dev_repo, "list", "--status", "done"))] == [a]
    assert [r["id"] for r in ok(tix(cli, dev_repo, "list", "--all", "--label", "x"))] == [a]
    assert ok(tix(cli, dev_repo, "list", "--project", "nope")) == []


def test_list_shows_questions(cli, dev_repo, sim):
    ref = sim_ticket(sim, project="proj-a", title="asked", status="waiting", device=dev_repo, questions=QUESTIONS)
    row = ok(tix(cli, dev_repo, "list"))[0]
    assert row["id"] == ref and row["status"] == "waiting" and row["open_questions"] == 1


def test_show_decrypts_content_and_events(cli, dev_repo, sim):
    ref = sim_ticket(sim, project="proj-a", title="Add dark mode", status="open", body="Make it *dark*.\n",
                     fm={"estimate": 2})
    web_event(sim, ref, "update", {"text": "looks good"})
    j = ok(tix(cli, dev_repo, "show", ref))
    assert j["id"] == ref and j["title"] == "Add dark mode" and j["body"] == "Make it *dark*.\n"
    assert j["fm"] == {"estimate": 2} and j["status"] == "open" and j["files"] == []
    assert "wrapped_dek" not in j and "enc_content" not in j and j["archived_at"] is None
    assert [(e["kind"], e["actor"]["kind"]) for e in j["events"]] == [("created", "web"), ("update", "web")]
    assert j["events"][1]["body"] == {"text": "looks good"}


def test_show_missing_exits_2(cli, dev_repo):
    assert tix(cli, dev_repo, "show", "TIX-77").code == 2


# ---------------------------------------------------------------- input files (mirror push --file uses them)

def test_input_files_outside_the_repo_are_refused(sharing, dev_repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(sharing.Refused, match="outside this repo"):
        sharing._read_input_file(str(outside), dev_repo.root.resolve())


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_input_file_symlinked_out_of_the_repo_is_refused(sharing, dev_repo, tmp_path, monkeypatch):
    target = tmp_path / "secret-notes.md"
    target.write_text("not yours", encoding="utf-8")
    (dev_repo.root / "link.md").symlink_to(target)
    monkeypatch.chdir(dev_repo.root)
    with pytest.raises(sharing.Refused, match="outside this repo"):
        sharing._read_input_file("link.md", dev_repo.root.resolve())
    (dev_repo.root / "real.md").write_text("inside", encoding="utf-8")
    (dev_repo.root / "inlink.md").symlink_to(dev_repo.root / "real.md")
    assert sharing._read_input_file("inlink.md", dev_repo.root.resolve()) == "inside"


# ---------------------------------------------------------------- the sharing-tickets skill (T8 "Installing")

def _tickets_src(root):
    return root / ".claude" / "skills" / "sharing" / "tickets-SKILL.md"


def _tickets_dst(root):
    return root / ".claude" / "skills" / "sharing-tickets" / "SKILL.md"


def test_ensure_tickets_skill(sharing, tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    src, dst = _tickets_src(root), _tickets_dst(root)
    src.parent.mkdir(parents=True)

    sharing.ensure_tickets_skill(root)   # no source yet (an install older than 1.6.0): nothing happens
    assert not dst.parent.exists()

    src.write_text("---\nname: sharing-tickets\n---\nv1\n")
    sharing.ensure_tickets_skill(root)   # missing: copied
    assert dst.read_bytes() == src.read_bytes()
    assert (dst.parent / ".gitignore").read_bytes() == b"*\n"
    if os.name != "nt":
        assert stat.S_IMODE(dst.stat().st_mode) == 0o644

    before = dst.stat().st_mtime_ns
    time.sleep(0.01)
    sharing.ensure_tickets_skill(root)   # identical: idempotent, not rewritten
    assert dst.stat().st_mtime_ns == before
    assert sorted(p.name for p in dst.parent.iterdir()) == [".gitignore", "SKILL.md"]

    dst.write_text("edited locally\n")
    (dst.parent / ".gitignore").write_text("")
    sharing.ensure_tickets_skill(root)   # different: replaced, and the folder ignores itself again
    assert dst.read_bytes() == src.read_bytes()
    assert (dst.parent / ".gitignore").read_bytes() == b"*\n"

    src.write_text("---\nname: sharing-tickets\n---\nv2\n")
    sharing.ensure_tickets_skill(root)   # a `sharing update` brought a new version: follows it
    assert dst.read_text() == src.read_text()
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                       capture_output=True, text=True, check=True)
    assert "sharing-tickets" not in r.stdout


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_ensure_tickets_skill_never_writes_through_a_symlink_out_of_the_repo(sharing, tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    src = _tickets_src(root)
    src.parent.mkdir(parents=True)
    src.write_text("skill\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".claude" / "skills" / "sharing-tickets").symlink_to(outside, target_is_directory=True)
    sharing.ensure_tickets_skill(root)
    assert list(outside.iterdir()) == []


def test_every_command_installs_the_tickets_skill_and_excludes_it(cli, dev_repo, sharing):
    src = _tickets_src(dev_repo.root)
    src.write_text("---\nname: sharing-tickets\n---\nbody\n")
    r = cli(dev_repo.root, "whoami", "--json")
    assert r.code == 0, r.err
    assert _tickets_dst(dev_repo.root).read_bytes() == src.read_bytes()
    assert "/.claude/skills/sharing-tickets/" in sharing.EXCLUDE_LINES
    lines = (dev_repo.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count("/.claude/skills/sharing-tickets/") == 1
    assert ok(tix(cli, dev_repo, "list")) == []
    lines = (dev_repo.root / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count("/.claude/skills/sharing-tickets/") == 1   # idempotent


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("dangling", [False, True])
def test_ensure_tickets_skill_replaces_a_symlinked_gitignore(sharing, tmp_path, dangling):
    """A cloned repo can commit .claude/skills/sharing-tickets/.gitignore -> ~/.bashrc."""
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    src = _tickets_src(root)
    src.parent.mkdir(parents=True)
    src.write_text("skill\n")
    target = tmp_path / "home" / ".bashrc"
    target.parent.mkdir()
    if not dangling:
        target.write_text("alias ll='ls -l'\n")
    gi = _tickets_dst(root).parent / ".gitignore"
    gi.parent.mkdir(parents=True)
    gi.symlink_to(target)
    sharing.ensure_tickets_skill(root)
    assert not gi.is_symlink() and gi.read_bytes() == b"*\n"
    assert _tickets_dst(root).read_text() == "skill\n"
    if dangling:
        assert not target.exists()
    else:
        assert target.read_text() == "alias ll='ls -l'\n"
