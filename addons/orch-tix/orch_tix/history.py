"""The phone's ticket history from orch events (not from the Log text): one short entry per event, built in
`on_event` (it rides along in the outbox item) and kept per linked ticket in history.json. Each entry is
{seq, at, who, what} plus `text` where the event carries free text (a change request's message, a close reason,
a log line, an artifact name, a task note). `what` never holds free text, so the title level sends `what` only;
full adds `text`; key-only sends no history.

The event log is writable by the agents in the workspace, so `who` is what it claims, never proof: a human event
reads "human (desktop log)", never "you" (the phone says "you" only for the decisions it sent itself)."""
from __future__ import annotations

import re

KEEP = 30          # entries kept per ticket
SEND = 20          # entries sent to the phone
TEXT_MAX = 160
_ID = re.compile(r"^(?:T|Q)[1-9][0-9]{0,4}$")
# orch-core's section names (orch.core.constants SECTIONS and LEGACY_SECTIONS): any other name an agent chose
# stays off the phone ("edited a section").
SECTIONS = frozenset({"Ask", "Summary", "Context", "Requirements", "Acceptance criteria", "Out of scope", "Plan",
                      "Tasks", "Current state", "Verification", "Log", "Findings", "Proposal", "Decisions"})
HUMAN = "human (desktop log)"
_WORD = re.compile(r"^[a-z][a-z-]{0,20}$")


def _who(actor: str) -> str:
    actor = str(actor or "")
    if actor.startswith("human:"):
        return HUMAN
    if actor.startswith("agent:"):
        name = actor[len("agent:"):].split(":", 1)[0]
        return name if re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name) else "an agent"
    return "orch"


def _word(value, default: str) -> str:
    v = str(value or "")
    return v if _WORD.match(v) else default


def _ids(values) -> str:
    ids = [str(v) for v in values or [] if _ID.match(str(v))]
    return ", ".join(ids[:6])


def _text(value) -> str:
    t = " ".join(str(value or "").split())
    return t[:TEXT_MAX]


def describe(kind: str, data: dict) -> tuple[str, str]:
    """(what, text) for an event kind and its data; what holds only fixed words, ids and states."""
    d = data if isinstance(data, dict) else {}
    if kind == "ticket.created":
        return "created the ticket", ""
    if kind == "ticket.moved":
        frm, to = _word(d.get("from"), "?"), _word(d.get("to"), "?")
        if d.get("command") in ("close", "reopen"):
            verb = "closed" if d["command"] == "close" else "reopened"
            return f"{verb} the ticket ({frm} → {to})", _text(d.get("reason"))
        return f"moved {frm} → {to}", ""
    if kind in ("gate.approved", "gate.invalidated"):
        verb = "approved" if kind == "gate.approved" else "invalidated"
        return f"{verb} the {_word(d.get('gate'), 'gate')}", ""
    if kind == "gate.changes_requested":
        return f"asked for changes on the {_word(d.get('gate'), 'gate')}", _text(d.get("message"))
    if kind == "verdict.given":
        return f"gave the verdict {_word(d.get('verdict'), '?')}", ""
    if kind == "question.asked":
        ids = _ids(d.get("qids"))
        return (f"asked {ids}" if ids else "asked a question"), ""
    if kind == "question.answered":
        qid = str(d.get("qid") or "")
        return (f"answered {qid}" if _ID.match(qid) else "answered a question"), ""
    if kind == "claim.taken":
        return "claimed the ticket", ""
    if kind == "claim.released":
        return "released the claim", ""
    if kind == "artifact.added":
        return "added an artifact", _text(d.get("name"))
    if kind == "ticket.edited":
        section = str(d.get("section") or "")
        if section in SECTIONS:
            return f"edited {section}", ""
        if section:
            return "edited a section", ""
        if any(d.get(k) for k in ("branch", "worktree", "pr", "external")):
            return "linked code", ""
        return "edited the ticket", ""
    if kind == "log.added":
        return "logged a note", _text(d.get("text"))
    if kind == "state.updated":
        return "updated the state", ""
    if kind == "task.added":
        return f"added {_ids(d.get('tasks')) or 'tasks'}", ""
    if kind == "task.edited":
        return f"edited {_ids([d.get('task')]) or 'a task'}", ""
    if kind == "task.moved":
        tid = _ids([d.get("task")]) or "a task"
        verb = {"doing": "started", "done": "finished", "skipped": "skipped", "blocked": "blocked", "todo": "reopened"}
        now = str(d.get("now") or "")
        return f"{verb.get(now, 'moved')} {tid}", _text(d.get("note") or d.get("why"))
    return _word(kind.replace(".", "-"), "event").replace("-", " "), ""


def entry(event) -> dict | None:
    """The history entry of one orch event, or None for one without a ticket."""
    if not getattr(event, "ticket", None):
        return None
    what, text = describe(str(event.kind), getattr(event, "data", None) or {})
    out = {"seq": int(event.seq), "at": str(event.at or ""), "who": _who(getattr(event, "actor", "")), "what": what}
    if text:
        out["text"] = text
    return out


def stored(entries, level: str) -> list:
    """What history.json keeps: the event text only while the ticket is shown in full."""
    if level == "full":
        return [dict(e) for e in entries if isinstance(e, dict)]
    return [{k: v for k, v in e.items() if k != "text"} for e in entries if isinstance(e, dict)]


def merge(old: list, new: list) -> list:
    """Entries by seq, oldest first, the newest KEEP."""
    by_seq = {e["seq"]: e for e in [*(old or []), *(new or [])] if isinstance(e, dict) and isinstance(e.get("seq"), int)}
    return [by_seq[s] for s in sorted(by_seq)][-KEEP:]


def redact(entries, level: str) -> list:
    """What the phone gets: full = what and text, title = what only, key-only = nothing."""
    if level == "key-only":
        return []
    keys = ("seq", "at", "who", "what", "text") if level == "full" else ("seq", "at", "who", "what")
    return [{k: e[k] for k in keys if k in e} for e in list(entries or [])[-SEND:]]
