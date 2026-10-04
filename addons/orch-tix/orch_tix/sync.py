"""Up-sync (spec §4.1): on_event only enqueues; drain builds one snapshot per ticket and pushes it."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from . import history
from .cli import SharingError
from .mapping import needs_of, payload

MAX_TICKETS_PER_DRAIN = 10
SKIP = {"addon.action", "addon.decision"}   # our own actions push directly
NAME = "orch-tix"


def on_event(event, outbox) -> None:
    if not event.ticket:
        return
    ev = history.entry(event)                  # the phone's history comes from events, not from the Log
    if event.kind in SKIP or event.via == f"addon:{NAME}":
        # Our own writes (a phone decision applied through the addon) push directly; they still belong in the
        # history, so they are recorded without a sync of their own.
        if ev and event.kind not in SKIP:
            outbox.put({"op": "history", "ticket": event.ticket, "seq": event.seq, "kind": event.kind, "ev": ev})
        return
    item = {"op": "sync", "ticket": event.ticket, "seq": event.seq, "kind": event.kind}
    if ev:
        item["ev"] = ev
    if event.kind == "artifact.added":
        item.update(artifact=str((event.data or {}).get("name") or ""), context=(event.data or {}).get("context") is True)
    outbox.put(item)


def should_link(link_mode: str, doc: dict, link: dict | None) -> bool:
    """Push this ticket? An active link always; a retired one (by hand, done cleanup or gone) never — only the
    explicit link action re-links it. A ticket never linked follows the link mode."""
    if link:
        return not link.get("retired")
    if link_mode == "auto-on-question":
        return needs_of(doc) is not None
    if link_mode == "all-active":
        return doc.get("status") not in ("backlog", "done")
    return False


def out_dir(ctx) -> Path:
    """Payload files live inside the workspace (orchestrator/.state/addons/orch-tix/out/): the sharing CLI reads
    --file only from inside its repo."""
    d = ctx.addon.state_dir / "out"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def level_of(link: dict | None, settings) -> str:
    """The redaction level of one ticket: its own override, else the workspace setting."""
    return (link or {}).get("redaction") or settings.get("redaction") or "title"


def sent_fields(doc: dict) -> list[str]:
    """The names of what a push sealed: the doc's keys, and the section names at full (never their text)."""
    out = sorted(k for k in doc if k != "sections")
    names = [n for n, v in (doc.get("sections") or {}).items() if isinstance(v, str) and v.strip()]
    if names:
        out.append("sections (" + ", ".join(names) + ")")
    return out


def push(addon, ctx, key: str, doc: dict) -> str:
    """Push one snapshot of `key`. Returns the CLI status: pushed | stale | duplicate | gone."""
    st, settings = addon.state, ctx.settings
    link = st.links()[key]
    rev = int(link.get("rev") or 0) + 1
    level = level_of(link, settings)
    body = payload(doc, key=key, gen=int(link.get("gen") or 1), rev=rev, level=level,
                   sync_log=bool(settings.get("sync_log")), context_artifacts=link.get("context_artifacts") or [],
                   history=st.history(key))
    path = out_dir(ctx) / f"mirror-{secrets.token_hex(8)}.json"
    raw = json.dumps(body, ensure_ascii=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    args = ["mirror", "push", "--file", str(path)] + (["--relink"] if link.get("relink") else [])
    st.reserve_rev(key, rev)                    # the next push is rev + 1, whatever happens to this call
    entry = {"kind": "relink" if link.get("relink") else "push", "key": key, "tix": link.get("n"), "level": level,
             "rev": rev, "fields": sent_fields(body["doc"]), "size": len(raw.encode("utf-8"))}
    try:
        r = addon.sharing(ctx).run_json(*args, timeout=30)
    except SharingError as e:
        st.log({**entry, "result": "retry", "reason": f"{e.code}: {e.detail}"[:200]})
        raise
    finally:
        path.unlink(missing_ok=True)
    status = str(r.get("status") or "")
    result = {"pushed": "ok", "duplicate": "ok"}.get(status, "refused")
    st.log({**entry, "tix": r.get("id") or link.get("n"), "result": result,
            "reason": "" if result == "ok" else {"stale": "the server holds a newer copy",
                                                  "gone": "the ticket is gone on the server"}.get(status, status)})
    gen = r.get("gen") if isinstance(r.get("gen"), int) else None
    if status == "gone":
        st.retire(key, gen)
        return status
    stored = r.get("rev") if isinstance(r.get("rev"), int) else rev     # after a takeover: server_rev + 1
    st.commit_rev(key, max(stored, int(r.get("server_rev") or 0)), r.get("id"), gen=gen,
                  done=doc.get("status") == "done")
    return status


def share_artifact(addon, ctx, key: str, name: str) -> str:
    """Share one artifact of `key` as a context FILE (7 days) and remember it on the link; returns the FILE id."""
    name = Path(str(name)).name
    path = ctx.addon.ws.artifacts_dir / key / name
    if not name or not path.is_file():
        raise SharingError("not_found", f"{key} has no artifact {name!r}")
    try:
        r = addon.sharing(ctx).run_json("share", str(path), "--name", name, "--ttl", "7d", "--tag", "context", timeout=120)
    except SharingError as e:
        addon.state.log({"kind": "file", "key": key, "fields": ["artifact"], "size": path.stat().st_size,
                         "result": "refused" if e.code == "refused" else "retry", "reason": f"{e.code}: {e.detail}"[:200]})
        raise
    file_id = str(r.get("id") or "")
    addon.state.log({"kind": "file", "key": key, "tix": file_id, "fields": ["artifact"], "size": path.stat().st_size,
                     "result": "ok" if file_id else "refused"})
    if not file_id:
        raise SharingError("bad_output", "the sharing CLI did not return a FILE id")
    addon.state.add_context_artifact(key, name, file_id)
    return file_id


def _share_context(addon, ctx, key: str, data_items: list[dict]) -> None:
    """Share the ticket's new context artifacts. Nothing at key-only (workspace or ticket). A failing share is
    noted and skipped: it never holds back the push."""
    mode = ctx.settings.get("sync_artifacts") or "on-request"
    link = addon.state.links().get(key) or {}
    if mode == "never" or level_of(link, ctx.settings) == "key-only":
        return
    sent = {a.get("name") for a in link.get("context_artifacts") or []}
    for data in data_items:
        name = data.get("artifact")
        if data.get("kind") != "artifact.added" or not name or name in sent:
            continue
        if mode == "always" or data.get("context") is True:
            try:
                share_artifact(addon, ctx, key, name)
            except SharingError as e:
                addon.note_error(f"share {key}/{name}: {e.detail}")
            sent.add(name)


def drain(addon, ctx, items) -> list[str]:
    mode = ctx.settings.get("link_mode") or "auto-on-question"
    if mode == "off":
        return [i["id"] for i in items]
    by_ticket: dict[str, list[dict]] = {}
    acked = []
    for i in items:
        t = (i.get("data") or {}).get("ticket")
        if t:
            by_ticket.setdefault(t, []).append(i)
        else:
            acked.append(i["id"])
    for ref in list(by_ticket)[:MAX_TICKETS_PER_DRAIN]:
        group = by_ticket[ref]
        ids = [i["id"] for i in group]
        try:
            doc = ctx.document(ref)
        except Exception:
            acked += ids                       # a ticket that no longer parses or exists: nothing to push
            continue
        key = str(doc.get("id") or ref)
        link = addon.state.links().get(key)
        evs = [(i.get("data") or {}).get("ev") for i in group]
        if all((i.get("data") or {}).get("op") == "history" for i in group):
            # only history: kept for a linked ticket, sent with its next push
            if link and not link.get("retired"):
                addon.state.add_history(key, history.stored([e for e in evs if e], level_of(link, ctx.settings)))
            acked += ids
            continue
        if not should_link(mode, doc, link):
            acked += ids
            continue
        try:
            if not link:
                addon.state.link(key, by="auto", auto=True)
            _share_context(addon, ctx, key, [i.get("data") or {} for i in group])
            addon.state.add_history(key, history.stored([e for e in evs if e],
                                                        level_of(addon.state.links().get(key), ctx.settings)))
            push(addon, ctx, key, doc)
        except SharingError as e:
            addon.note_error(f"push {key}: {e.detail}")
            continue                           # keep the items: the outbox retries
        acked += ids
    return acked
