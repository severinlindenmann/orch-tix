"""Up-sync (spec §4.1): on_event only enqueues; drain builds one snapshot per ticket and pushes it."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import history
from .cli import SharingError
from . import ticket_widgets
from .state import GONE_REASON
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


def watch_item(event) -> dict | None:
    """The outbox item the immediate watcher (watch.py) syncs for an event, or None when it is no ticket event.
    Unlike on_event this does not skip our own writes (via addon:orch-tix): a phone decision applied through the
    addon changes the ticket's needs, and the phone should hear that at once; only the addon's own log events skip."""
    if not event.ticket or event.kind in SKIP:
        return None
    item = {"op": "sync", "ticket": event.ticket, "seq": event.seq, "kind": event.kind}
    ev = history.entry(event)
    if ev:
        item["ev"] = ev
    if event.kind == "artifact.added":
        item.update(artifact=str((event.data or {}).get("name") or ""), context=(event.data or {}).get("context") is True)
    return item


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


def _widgets(addon, ctx, key: str) -> list[dict]:
    """The ticket's widgets (full only); a widget that cannot be built never holds back the push."""
    share = (ctx.settings.get("sync_widget_docs") == "always"
             and (ctx.settings.get("sync_artifacts") or "on-request") != "never")
    try:
        return ticket_widgets.entries(addon, ctx, key, share=share)
    except Exception as e:  # noqa: BLE001 — a renderer bug must not stop the ticket reaching the phone
        addon.note_error(f"widgets {key}: {e}")
        return []


def sent_fields(doc: dict) -> list[str]:
    """The names of what a push sealed: the doc's keys, and the section names at full (never their text)."""
    out = sorted(k for k in doc if k != "sections")
    names = [n for n, v in (doc.get("sections") or {}).items() if isinstance(v, str) and v.strip()]
    if names:
        out.append("sections (" + ", ".join(names) + ")")
    return out


IMAGE_KINDS = ("screenshot", "diagram")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PINNED_PER_PUSH = 4
_ARTIFACT_REF = re.compile(r"!\[[^\]]*\]\(artifact:([^)\s]+)\)")


def pinned_to_share(doc: dict, sent: set) -> list[tuple[str, str]]:
    """Phone v4: the pinned images a full ticket shows, not yet sent: image artifacts with a sha256 that the gated text
    shows (![…](artifact:<name>)) or that prove a criterion. The phone shows them only after hashing the bytes."""
    shown = set()
    for g in (doc.get("gates") or {}).values():
        covers = g.get("covers") if isinstance(g, dict) and isinstance(g.get("covers"), list) else []
        for name in covers:
            text = (doc.get("sections") or {}).get(name)
            if isinstance(text, str):
                shown.update(_ARTIFACT_REF.findall(text))
    out = []
    for it in doc.get("artifact_items") or []:
        if not isinstance(it, dict) or it.get("source") != "file" or not isinstance(it.get("sha256"), str):
            continue
        name, sha = str(it.get("name") or ""), it["sha256"]
        # `sent` holds (name, sha256): an image re-pinned with --replace (a new sha256) is shared again
        if (it.get("kind") in IMAGE_KINDS and name.lower().endswith(IMAGE_EXT) and (name, sha) not in sent
                and "/" not in name and (name in shown or isinstance(it.get("ac"), int))):
            out.append((name, sha))
    return out[:PINNED_PER_PUSH]


SHARE_RETRY = timedelta(hours=1)        # a pinned image that failed to share is tried again at most hourly
PINNED_TIMEOUT_S = 10                   # each pinned share; they run after the mirror push, at most this long in all
MAX_SHARE_BYTES = 50 * 1024 * 1024      # on-request shares
MAX_PINNED_BYTES = 8 * 1024 * 1024      # pinned auto-shares: the phone refuses a larger image


def share_pinned(addon, ctx, key: str, doc: dict) -> int:
    """Share the pinned images a full ticket shows that are not on the phone yet (or were re-pinned). Runs after the
    mirror push, never before it; stops after PINNED_TIMEOUT_S in all. A failure is retried at most hourly and noted
    once per failure (the same sha256 and reason are not noted again). Returns how many were shared."""
    st = addon.state
    link = st.links().get(key) or {}
    sent = {(a.get("name"), a.get("sha256")) for a in link.get("context_artifacts") or []}
    failed = link.get("share_failures") or {}
    now = datetime.now(timezone.utc)
    started, shared = time.monotonic(), 0
    for name, sha in pinned_to_share(doc, sent):
        f = failed.get(name) or {}
        try:
            last = datetime.fromisoformat(str(f.get("at"))) if f.get("sha256") == sha else None
        except ValueError:
            last = None
        if last is not None and now - last < SHARE_RETRY:
            continue
        left = PINNED_TIMEOUT_S - (time.monotonic() - started)
        if left <= 1:
            break
        try:
            share_artifact(addon, ctx, key, name, expect_sha=sha, timeout=left, quiet=True, max_bytes=MAX_PINNED_BYTES)
            st.share_failure(key, name, None)
            shared += 1
        except SharingError as e:
            if st.share_failure(key, name, {"sha256": sha, "code": e.code, "at": now.isoformat()}):
                addon.note_error(f"share {key}/{name}: {e.detail}")
                st.log({"kind": "file", "key": key, "fields": ["artifact"], "result": "refused" if e.code == "refused" else "retry",
                        "reason": f"{e.code}: {e.detail}"[:200]})
    return shared


PHONE_KINDS = ("answer", "approve", "request_changes", "verdict")
PHONE_WINDOW = timedelta(minutes=30)


def phone_decision(addon, key: str, body: dict) -> str | None:
    """The id of the phone decision this push reports, when the ticket stopped needing you because Mission Control
    applied a phone answer for it (QA #55): an unannounced decision of the phone for `key`, being applied or applied,
    received in the last 30 minutes. Only a push with `needs` null qualifies. The server uses the answer to word the
    "handled" push and to skip the phone that decided."""
    if body.get("needs") is not None:
        return None
    now = datetime.now(timezone.utc)
    for did, rec in addon.state.decisions().items():
        if rec.get("ticket") != key or rec.get("kind") not in PHONE_KINDS or rec.get("via_sent"):
            continue
        if rec.get("outcome") not in ("applying", "applied"):
            continue
        try:
            at = datetime.fromisoformat(str(rec.get("received_at")))
        except ValueError:
            continue
        if at.tzinfo is not None and now - at <= PHONE_WINDOW:
            return did
    return None


def push(addon, ctx, key: str, doc: dict, *, pinned: bool = True) -> str:
    """Push one snapshot of `key`. Returns the CLI status: pushed | stale | duplicate | gone."""
    st, settings = addon.state, ctx.settings
    link = st.links()[key]
    rev = int(link.get("rev") or 0) + 1
    level = level_of(link, settings)
    widgets = _widgets(addon, ctx, key) if level == "full" else None
    body = payload(doc, key=key, gen=int(link.get("gen") or 1), rev=rev, level=level,
                   sync_log=bool(settings.get("sync_log")), context_artifacts=link.get("context_artifacts") or [],
                   widgets=widgets, history=st.history(key))
    body = ticket_widgets.fit(addon, key, body)
    decided = phone_decision(addon, key, body)
    if decided:
        body["decided_via"] = "phone"
    ticket_widgets.prune(addon, addon.sharing(ctx), key, body["doc"].get("widgets"))
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
    if decided and status in ("pushed", "duplicate"):
        addon.state.update_decision(decided, via_sent=True)          # said once; the next push of the ticket does not
    result = {"pushed": "ok", "duplicate": "ok"}.get(status, "refused")
    st.log({**entry, "tix": r.get("id") or link.get("n"), "result": result,
            "reason": "" if result == "ok" else {"stale": "the server holds a newer copy",
                                                  "gone": GONE_REASON}.get(status, status)})
    gen = r.get("gen") if isinstance(r.get("gen"), int) else None
    if status == "gone":
        st.retire(key, gen)
        return status
    stored = r.get("rev") if isinstance(r.get("rev"), int) else rev     # after a takeover: server_rev + 1
    st.commit_rev(key, max(stored, int(r.get("server_rev") or 0)), r.get("id"), gen=gen,
                  done=doc.get("status") == "done")
    # Phone v4: the pinned images go after the mirror (which never waits for them); when some went out, one more push
    # carries their FILEs
    if pinned and level == "full" and (settings.get("sync_artifacts") or "on-request") != "never":
        if share_pinned(addon, ctx, key, doc):
            return push(addon, ctx, key, doc, pinned=False)
    return status


def checked_artifact(ctx, key: str, name: str, expect_sha: str | None = None, max_bytes: int = MAX_SHARE_BYTES) -> tuple[bytes, str]:
    """The bytes of artifact `name` of `key`, read once, as orch-core reads them: the ticket's folder opened once
    without following a link (O_DIRECTORY | O_NOFOLLOW) and the file opened relative to that handle (O_NOFOLLOW), a
    plain file with one link (no hard link), at most `max_bytes`, and, when the item is pinned, hashing to its
    sha256. Returns (bytes, sha256 hex); SharingError otherwise (an OS error while reading is a retryable one)."""
    name = Path(str(name)).name
    if not name or name in (".", ".."):
        raise SharingError("not_found", f"{key} has no artifact {name!r}")
    try:
        dfd = os.open(ctx.addon.ws.artifacts_dir / key, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise SharingError("refused", f"the artifact folder of {key} is missing or a link") from None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
        except OSError:
            raise SharingError("not_found", f"{key} has no artifact {name!r} (or it is a link)") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SharingError("refused", f"{key}/{name} is not a plain file (a link)")
            if info.st_size > max_bytes:
                raise SharingError("refused", f"{key}/{name} is larger than {max_bytes} bytes")
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SharingError("refused", f"{key}/{name} is larger than {max_bytes} bytes")
                chunks.append(chunk)
        except OSError as e:
            raise SharingError("retry", f"cannot read {key}/{name}: {e.strerror or type(e).__name__}") from None
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    data = b"".join(chunks)
    digest = hashlib.sha256(data).hexdigest()
    if expect_sha is not None and digest != expect_sha:
        raise SharingError("refused", f"{key}/{name} no longer matches its pinned sha256")
    return data, digest


def share_artifact(addon, ctx, key: str, name: str, *, expect_sha: str | None = None, timeout: float = 120,
                   quiet: bool = False, max_bytes: int = MAX_SHARE_BYTES) -> str:
    """Share one artifact of `key` as a context FILE (7 days) and remember it on the link (with its sha256); returns
    the FILE id. The CLI gets a private copy of exactly the checked bytes (checked_artifact), never the path. The
    private copy is removed whatever happens, an OS error while writing it included."""
    name = Path(str(name)).name
    data, digest = checked_artifact(ctx, key, name, expect_sha, max_bytes)
    copy = out_dir(ctx) / f"share-{secrets.token_hex(8)}"
    try:
        try:
            fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError as e:
            raise SharingError("retry", f"cannot copy {key}/{name}: {e.strerror or type(e).__name__}") from None
        r = addon.sharing(ctx).run_json("share", str(copy), "--name", name, "--ttl", "7d", "--tag", "context",
                                        timeout=timeout)
    except SharingError as e:
        if not quiet:
            addon.state.log({"kind": "file", "key": key, "fields": ["artifact"], "size": len(data),
                             "result": "refused" if e.code == "refused" else "retry", "reason": f"{e.code}: {e.detail}"[:200]})
        raise
    finally:
        copy.unlink(missing_ok=True)
    file_id = str(r.get("id") or "")
    addon.state.log({"kind": "file", "key": key, "tix": file_id, "fields": ["artifact"], "size": len(data),
                     "result": "ok" if file_id else "refused"})
    if not file_id:
        raise SharingError("bad_output", "the sharing CLI did not return a FILE id")
    addon.state.add_context_artifact(key, name, file_id, sha256=digest)
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
    relinked: set[str] = set()                 # a lost ticket is linked again at most once per cycle
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
        if key not in relinked and should_link(mode, doc, None) and addon.state.relink_if_lost(key):
            relinked.add(key)
            addon.state.log({"kind": "relink", "key": key, "result": "ok", "reason": "re-linked after the server lost it"})
            link = addon.state.links().get(key)
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
