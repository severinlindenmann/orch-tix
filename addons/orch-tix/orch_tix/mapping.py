"""What a ticket looks like on the phone (spec §9.1, decisions §15.6). One function per redaction level, each
naming the keys it keeps: a key orch adds later never reaches the phone by default."""
from __future__ import annotations

import copy
import re

from . import history as history_mod

LEVELS = ("full", "title", "key-only")
LEVEL_LABEL = {"full": "Everything but the log", "title": "Title and questions", "key-only": "Key only"}
_NEEDS = {"answer": "question", "approve-requirements": "approval", "approve-plan": "approval",
          "re-approve": "approval", "verdict": "verdict"}
_ALWAYS = ("schema_version", "id", "status", "needs", "created", "updated")
_TITLE_META = ("title", "type", "priority", "size", "labels")
# Below full the phone sees that a question was answered (answered, via), never the answer or its note: those
# can carry client detail and stay on the desktop (final review M7).
_QUESTION_KEYS = ("id", "text", "why", "type", "options", "recommended", "blocking", "hash", "asked", "answered",
                  "via")
_FULL_DROP_SECTIONS = ("Log",)
LOG_LINES = 20


def needs_of(doc: dict) -> str | None:
    """The phone's needs kind: the first orch need that the human answers on the phone. MC2-T `task` and
    `broken` are no phone notification (human tasks show read-only in the task progress)."""
    for need in doc.get("needs") or []:
        kind = _NEEDS.get((need or {}).get("kind"))
        if kind:
            return kind
    return None


def open_questions(doc: dict) -> int:
    return sum(1 for q in doc.get("questions") or [] if isinstance(q, dict) and q.get("answer") in (None, ""))


def _needs(doc: dict) -> list:
    """Below full, a need is its kind, the verdict round and the question ids: never its detail text."""
    out = []
    for n in doc.get("needs") or []:
        if not isinstance(n, dict) or not n.get("kind"):
            continue
        item = {"kind": str(n["kind"])}
        if isinstance(n.get("round"), int) and not isinstance(n.get("round"), bool):
            item["round"] = n["round"]
        if n.get("together") is True:            # schema 1.4: requirements and plan approved as one
            item["together"] = True
        if n["kind"] == "answer":
            item["qids"] = [q for q in (p.strip().upper() for p in str(n.get("detail") or "").split(","))
                            if re.fullmatch(r"Q[1-9][0-9]*", q)]
        out.append(item)
    return out


def _keep(doc: dict, keys) -> dict:
    return {k: copy.deepcopy(doc[k]) for k in keys if k in doc}


def _first_line(text) -> str:
    """The first non-empty line outside fenced blocks (a ```orch widget, such as a receipt's gates, is no summary)."""
    fence = None
    for line in str(text or "").splitlines():
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None:
            if line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
                fence = None
        elif line.strip():
            return line.strip()
    return ""


def _progress(doc: dict) -> dict:
    summary = (doc.get("tasks") or {}).get("summary") or {}
    return {"done": int(summary.get("done") or 0), "total": int(summary.get("total") or 0)}


def _context(context_artifacts) -> list:
    return [{"name": str(a.get("name")), "file": a.get("file")} for a in context_artifacts or [] if isinstance(a, dict)]


def _full(doc: dict, sync_log: bool, context_artifacts) -> dict:
    out = copy.deepcopy(doc)
    sections = {k: v for k, v in (doc.get("sections") or {}).items() if k not in _FULL_DROP_SECTIONS}
    if sync_log:
        lines = [line for line in str((doc.get("sections") or {}).get("Log") or "").splitlines() if line.strip()]
        if lines:
            sections["Log"] = "\n".join(lines[-LOG_LINES:])
    out["sections"] = sections
    out["context_artifacts"] = _context(context_artifacts)
    out["open_questions"] = open_questions(doc)
    return out


_MOVE_KEYS = ("who", "kind", "label", "ref", "why", "epic")
_ITEM_KEYS = ("source", "kind", "name", "sha256", "task", "ac")
# schema 1.7: a receipt's facts and its step names; the commands and the output stay on the desktop
_RUN_KEYS = ("exit", "timed_out", "commit", "dirty", "at", "seconds", "check")
_STEP_KEYS = ("name", "status", "seconds")
_BY = re.compile(r"^(human:you|agent:[A-Za-z0-9._-]{1,40})(?::[0-9A-Za-z-]{1,8})?$")


def _by(value) -> str | None:
    """Who added an artifact, without the agent's session id: `human:you` or `agent:<harness>`."""
    m = _BY.match(value) if isinstance(value, str) else None
    return m.group(1) if m else None


def _item(i: dict) -> dict:
    out = _keep(i, _ITEM_KEYS)
    by = _by(i.get("by"))
    if by:
        out["by"] = by
    run = i.get("run")
    if isinstance(run, dict):
        out["run"] = _keep(run, _RUN_KEYS)
        out["run"]["steps"] = [_keep(s, _STEP_KEYS) for s in run.get("steps") or [] if isinstance(s, dict)]
    return out


def _move(doc: dict, keys=_MOVE_KEYS) -> dict | None:
    """Schema 1.4 `move`: whose move it is, as the dashboard says (core's fixed labels, ids and refs)."""
    m = doc.get("move")
    return {k: m[k] for k in keys if k in m and isinstance(m[k], (str, type(None)))} if isinstance(m, dict) else None


def _title(doc: dict, context_artifacts) -> dict:
    out = _keep(doc, _ALWAYS + _TITLE_META)
    if _move(doc) is not None:
        out["move"] = _move(doc)
    out["needs"] = _needs(doc)
    out["questions"] = [_keep(q, _QUESTION_KEYS) for q in doc.get("questions") or [] if isinstance(q, dict)]
    out["open_questions"] = open_questions(doc)
    # covers: what each gate's hash binds (orch-core hash v2: Summary, the gated sections, size and type), so the
    # phone can say what an approval covers; the covered text itself stays at full.
    out["gates"] = {name: _keep(g, ("state", "hash", "covers")) for name, g in (doc.get("gates") or {}).items()
                    if isinstance(g, dict)}
    summary = _first_line((doc.get("sections") or {}).get("Verification"))
    if summary:
        out["verification_summary"] = summary
    out["tasks"] = {"progress": _progress(doc)}
    # schema 1.3: what a verdict must echo, {hash, round}; a hash and a number, no text
    if isinstance(doc.get("verdict"), dict):
        out["verdict"] = {k: doc["verdict"].get(k) for k in ("hash", "round")}
    out["claim"] = {"harness": (doc.get("claim") or {}).get("harness")}
    out["artifacts"] = [str(a) for a in doc.get("artifacts") or []]
    out["context_artifacts"] = _context(context_artifacts)
    # schema 1.5 artifacts: names, kinds, the pinned sha256 and what they prove; the labels (free text) stay at full
    out["artifact_items"] = [_item(i) for i in doc.get("artifact_items") or [] if isinstance(i, dict)]
    # schema 1.7: an open or backlog ticket nobody touched for a while; a number, no text
    rv = doc.get("revalidate")
    if isinstance(rv, dict) and isinstance(rv.get("idle_days"), int) and not isinstance(rv.get("idle_days"), bool):
        out["revalidate"] = {"idle_days": rv["idle_days"]}
    return out


def _key_only(doc: dict) -> dict:
    out = _keep(doc, _ALWAYS)
    if _move(doc) is not None:
        out["move"] = _move(doc, ("who", "kind"))
    out["needs"] = _needs(doc)
    out["open_questions"] = open_questions(doc)
    out["gates"] = {name: _keep(g, ("state",)) for name, g in (doc.get("gates") or {}).items() if isinstance(g, dict)}
    return out


def redact(doc: dict, level: str, *, sync_log: bool, context_artifacts, history=None) -> dict:
    """`history`: the ticket's entries from orch events (history.py); full sends their text, title only what
    happened, key-only none."""
    if level == "full":
        out = _full(doc, sync_log, context_artifacts)
    elif level == "key-only":
        out = _key_only(doc)
    else:
        level, out = "title", _title(doc, context_artifacts)
    out["redaction"] = level
    sent = history_mod.redact(history, level) if history else []
    if sent:
        out["history"] = sent
    return out


def payload(doc: dict, *, key: str, gen: int, rev: int, level: str, sync_log: bool, context_artifacts,
            history=None) -> dict:
    """The `sharing mirror push --file` body: cleartext routing fields plus the doc the CLI seals."""
    return {"key": key, "gen": int(gen), "rev": int(rev), "status": str(doc.get("status") or "backlog"),
            "priority": str(doc.get("priority") or "normal"), "needs": needs_of(doc),
            "open_questions": open_questions(doc), "schema_version": str(doc.get("schema_version") or "1.0.0"),
            "doc": redact(doc, level, sync_log=sync_log, context_artifacts=context_artifacts, history=history)}
