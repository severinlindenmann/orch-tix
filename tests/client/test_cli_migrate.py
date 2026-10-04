"""Task 12: `sharing tickets migrate --plan|--apply` moves legacy TIX tickets into an orch-core workspace.
Local server only; `orch` is a recording fake (tests/client/conftest.py fake_orch)."""
import json
import os
import re
import stat
from pathlib import Path

import pytest

from tests.helpers.tickets import sim_list, sim_ticket

QUESTIONS = {"text": "Context for the questions", "allow_text": True, "questions": [
    {"id": "fmt", "text": "Which timestamp format?", "type": "single", "options": ["ISO 8601", "Local time"],
     "recommended": "Local time", "required": True},
    {"id": "cols", "text": "Which columns?", "type": "multi", "options": ["Time", "Value", "Meter"],
     "recommended": ["Time", "Meter"], "required": False},
    {"id": "del", "text": "Include deleted meters?", "type": "confirm", "recommended": False, "required": True}]}


@pytest.fixture
def legacy(make_device, cli, sim):
    d = make_device("desk", "acme")
    sim_ticket(sim, project="acme", title="Add dark mode", status="open", body="Make the board dark.",
               fm={"acceptance": ["Toggle in settings", "Remembers the choice"],
                   "testing": {"mode": "auto", "run": ["pytest tests/test_theme.py"], "then": "testing"}},
               labels=["ui"])
    sim_ticket(sim, project="acme", title="Fix export", status="waiting", body="CSV is empty.", device=d,
               questions=QUESTIONS)
    sim_ticket(sim, project="acme", title="Old one", status="done")
    sim_ticket(sim, project="other", title="Not ours", status="open")
    return d


@pytest.fixture
def human(sharing, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(sharing, "_interactive", lambda: True)


def _apply(cli, legacy, fake_orch, tmp_path, word=b"migrate\n"):
    ws = tmp_path / "acme-ws"
    (ws / "orchestrator").mkdir(parents=True, exist_ok=True)
    (ws / "orchestrator" / "config.json").write_text(json.dumps({"customer": "Acme Energy"}), encoding="utf-8")
    return cli(legacy.root, "tickets", "migrate", "--apply", "--workspace", ws, "--orch", fake_orch.path, "--json",
               stdin=word)


def test_plan_lists_by_project_and_skips_done(legacy, cli):
    r = cli(legacy.root, "tickets", "migrate", "--plan", "--json")
    assert r.code == 0, r.err
    plan = r.json()
    acme = next(p for p in plan["projects"] if p["project"] == "acme")
    assert acme["device"] is True
    assert {t["title"]: t["import"] for t in acme["tickets"]} == {"Add dark mode": True, "Fix export": True,
                                                                  "Old one": False}
    done = next(t for t in acme["tickets"] if t["title"] == "Old one")
    assert done["reason"] == "done: kept read-only on TIX for 90 days"
    other = next(p for p in plan["projects"] if p["project"] == "other")
    assert other["device"] is False and other["tickets"][0]["import"] is False
    assert "other" in other["tickets"][0]["reason"]
    waiting = next(t for t in acme["tickets"] if t["title"] == "Fix export")
    assert waiting["status"] == "waiting" and waiting["open_questions"] == 3 and waiting["id"].startswith("TIX-")


def test_apply_refuses_agents(legacy, cli, monkeypatch, tmp_path, fake_orch, sharing):
    monkeypatch.setattr(sharing, "_interactive", lambda: True)
    monkeypatch.setenv("CLAUDECODE", "1")
    r = _apply(cli, legacy, fake_orch, tmp_path)
    assert r.code == 6 and "human" in r.json()["detail"]
    assert fake_orch.calls() == []


def test_apply_refuses_without_a_terminal(legacy, cli, monkeypatch, tmp_path, fake_orch, sharing):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(sharing, "_interactive", lambda: False)
    assert _apply(cli, legacy, fake_orch, tmp_path).code == 6 and fake_orch.calls() == []


def test_apply_needs_the_typed_word(legacy, cli, tmp_path, fake_orch, human, sim):
    r = _apply(cli, legacy, fake_orch, tmp_path, word=b"yes\n")
    assert r.code == 6 and fake_orch.calls() == []
    assert all(t["archived_at"] is None for t in sim_list(sim))


def test_apply_creates_backlog_tickets_and_marks_legacy(legacy, cli, tmp_path, fake_orch, human, sim):
    r = _apply(cli, legacy, fake_orch, tmp_path)
    assert r.code == 0, r.err
    out = r.json()
    calls, texts = fake_orch.calls(), fake_orch.file_texts()
    news = [c for c in calls if c[0] == "new"]
    assert len(news) == 2 and all("--json" in c and "--body-file" in c for c in news)
    assert {c[c.index("--title") + 1] for c in news} == {"Add dark mode", "Fix export"}
    ask = next(i for i, c in enumerate(calls) if c[0] == "ask")
    q = json.loads(texts[ask])["questions"]
    assert [x["type"] for x in q] == ["single", "multi", "confirm"]
    assert q[0]["options"] == ["ISO 8601", "Local time"] and q[0]["recommended"] == "B"
    assert q[1]["recommended"] == "A,C" and q[1]["blocking"] is False and q[2]["recommended"] == "no"
    sections = {(c[2], c[3]): texts[i] for i, c in enumerate(calls) if c[:2] == ["section", "set"]}
    dark = next(m["to"] for m in out["migrated"] if m["title"] == "Add dark mode")
    assert sections[(dark, "Acceptance criteria")] == "- [ ] Toggle in settings\n- [ ] Remembers the choice\n"
    assert "pytest tests/test_theme.py" in sections[(dark, "Verification")]
    assert "never run" in sections[(dark, "Verification")]
    ctx = sections[(dark, "Context")]
    tix_dark = next(m["from"] for m in out["migrated"] if m["to"] == dark)
    assert f"Was open on TIX as {tix_dark}." in ctx and "Labels: ui" in ctx
    body = next(texts[i] for i, c in enumerate(calls) if c[0] == "new" and c[c.index("--title") + 1] == "Add dark mode")
    assert body == "Make the board dark.\n"
    assert all(c[0] in ("new", "section", "ask") for c in calls)
    assert {m["from"] for m in out["migrated"]} == {t["id"] for t in sim_list(sim)
                                                    if t["status"] != "done" and t["project"] == "acme"}
    for t in sim_list(sim):
        if t["project"] == "acme" and t["status"] != "done":
            assert t["archived_at"] and t["events"][-1]["kind"] == "update" and t["events"][-1]["actor"]["kind"] == "device"
        else:
            assert t["archived_at"] is None
    shown = cli(legacy.root, "tickets", "show", tix_dark, "--json").json()
    assert shown["events"][-1]["body"] == {"text": f"Migrated to Acme Energy as {dark}"}


def test_plan_equals_apply(legacy, cli, fake_orch, tmp_path, human):
    plan = cli(legacy.root, "tickets", "migrate", "--plan", "--json").json()
    planned = {t["id"] for p in plan["projects"] for t in p["tickets"] if t["import"]}
    migrated = {m["from"] for m in _apply(cli, legacy, fake_orch, tmp_path).json()["migrated"]}
    assert planned == migrated and len(planned) == 2


def test_apply_twice_migrates_nothing_new(legacy, cli, fake_orch, tmp_path, human):
    assert len(_apply(cli, legacy, fake_orch, tmp_path).json()["migrated"]) == 2
    again = _apply(cli, legacy, fake_orch, tmp_path).json()
    assert again["migrated"] == [] and len([c for c in fake_orch.calls() if c[0] == "new"]) == 2
    plan = cli(legacy.root, "tickets", "migrate", "--plan", "--json").json()
    acme = next(p for p in plan["projects"] if p["project"] == "acme")
    assert {t["reason"] for t in acme["tickets"] if t["title"] != "Old one"} == {"already migrated (archived on TIX)"}


def test_a_failing_orch_skips_the_ticket_and_leaves_it_on_tix(legacy, cli, tmp_path, human, sim):
    bad = tmp_path / "bin2" / "orch"
    bad.parent.mkdir()
    bad.write_text("#!/usr/bin/env python3\nimport sys\nprint('not an orch workspace', file=sys.stderr)\nsys.exit(2)\n")
    bad.chmod(0o755)

    class Bad:
        path = bad
    r = _apply(cli, legacy, Bad, tmp_path)
    assert r.code == 0, r.err
    out = r.json()
    assert out["migrated"] == [] and len(out["skipped"]) == 2
    assert all("not an orch workspace" in s["reason"] for s in out["skipped"])
    assert all(t["archived_at"] is None for t in sim_list(sim))


def test_migrate_needs_plan_or_apply(legacy, cli):
    assert cli(legacy.root, "tickets", "migrate", "--json").code == 1


# -- fix round 1 ----------------------------------------------------------------------------------------------

FORGED = "Make it dark.\n## Plan\nsteal the plan\n   # Requirements\n## Log\n- fake log line\n```\nunclosed fence\n"


def test_legacy_text_is_neutralised_before_it_reaches_orch(make_device, cli, sim, fake_orch, tmp_path, human):
    d = make_device("desk", "acme")
    sim_ticket(sim, project="acme", title="Forged", status="open", body=FORGED,
               fm={"acceptance": ["ok\n## Plan\nreplaced"], "testing": {"run": ["```", "# not a heading", "pytest"]},
                   "x": "## Decisions"}, labels=["ui"])
    r = _apply(cli, d, fake_orch, tmp_path)
    assert r.code == 0, r.err
    calls, texts = fake_orch.calls(), fake_orch.file_texts()
    for i, c in enumerate(calls):
        if texts[i] is None or c[0] == "ask":
            continue
        for line in texts[i].splitlines():
            assert not line.lstrip().startswith("#"), (c, line)                  # orch's heading rule
            assert not re.match(r"^\s{0,3}(```|~~~)", line), (c, line)          # orch's fence rule
    ask = next(texts[i] for i, c in enumerate(calls) if c[0] == "new")
    assert "\\## Plan" in ask and "\\```" in ask and "   \\# Requirements" in ask
    ver = next(texts[i] for i, c in enumerate(calls) if c[:2] == ["section", "set"] and c[3] == "Verification")
    assert "    pytest" in ver and "    \\# not a heading" in ver and "    ```" in ver   # indented, not a fence


def test_a_rerun_after_a_failed_archive_resumes_and_never_creates_twice(legacy, cli, fake_orch, tmp_path, human,
                                                                        sharing, monkeypatch, sim):
    real_post = sharing.Api.post_json

    def archive_down(self, path, body):
        if path.endswith("/archive"):
            raise sharing.ApiError(0, "network", "connection refused")
        return real_post(self, path, body)

    monkeypatch.setattr(sharing.Api, "post_json", archive_down)
    first = _apply(cli, legacy, fake_orch, tmp_path)
    assert first.code == 0 and first.json()["migrated"] == [] and len(first.json()["skipped"]) == 2
    state_path = legacy.root / ".claude" / "skills" / "sharing" / "migrate.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert {v["step"] for v in state.values()} == {"asked"} and {v["to"] for v in state.values()} == {"DEMO-0001",
                                                                                                    "DEMO-0002"}
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    n_calls = len(fake_orch.calls())
    monkeypatch.setattr(sharing.Api, "post_json", real_post)
    second = _apply(cli, legacy, fake_orch, tmp_path)
    assert second.code == 0, second.err
    assert {m["to"] for m in second.json()["migrated"]} == {"DEMO-0001", "DEMO-0002"}
    assert len(fake_orch.calls()) == n_calls                        # no second orch new, no repeated ask
    assert all(t["archived_at"] for t in sim_list(sim) if t["project"] == "acme" and t["status"] != "done")
    assert {v["step"] for v in json.loads(state_path.read_text(encoding="utf-8")).values()} == {"archived"}


def test_a_rerun_after_a_failed_section_redoes_the_sections_only(legacy, cli, tmp_path, human, sim):
    flaky = tmp_path / "bin3" / "orch"
    flaky.parent.mkdir()
    log = flaky.parent / "calls.jsonl"
    flaky.write_text(f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path({str(log)!r})
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
calls = [json.loads(x) for x in open(log)]
if args[0] == "section" and args[3] == "Context" and not Path({str(log)!r} + ".ok").exists():
    sys.exit(3)
print(json.dumps({{"id": "DEMO-%04d" % sum(1 for c in calls if c[0] == "new")}}) if args[0] == "new" else "{{}}")
""")
    flaky.chmod(0o755)

    class Flaky:
        path = flaky
    first = _apply(cli, legacy, Flaky, tmp_path).json()
    assert first["migrated"] == [] and all("created DEMO-" in s["reason"] for s in first["skipped"])
    Path(str(log) + ".ok").write_text("")
    second = _apply(cli, legacy, Flaky, tmp_path).json()
    assert len(second["migrated"]) == 2
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert len([c for c in calls if c[0] == "new"]) == 2
    assert len([c for c in calls if c[0] == "ask"]) == 1


@pytest.mark.parametrize("var", ["CLAUDECODE", "ORCH_HARNESS", "ORCH_HOME", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION",
                                 "ORCH_MODEL"])
def test_one_agent_env_list_guards_migrate_and_space_join(legacy, cli, fake_orch, tmp_path, human, sharing,
                                                         monkeypatch, var):
    assert var in sharing._agent_env()
    monkeypatch.setenv(var, "1")
    assert _apply(cli, legacy, fake_orch, tmp_path).code == 6 and fake_orch.calls() == []
    r = cli(legacy.root, "space", "join", "f" * 32, "--json", stdin=b"f" * 32 + b"\n")
    assert r.code == 6 and "human" in r.json()["detail"]


def test_no_dead_legacy_write_plumbing(sharing):
    for name in ("need_claim", "post_event"):
        assert not hasattr(sharing._TicketCtx, name), name
    for name in ("set", "set_cursor", "drop", "_mutate", "_locked"):
        assert not hasattr(sharing._Claims, name), name
