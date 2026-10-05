"""Immediate sync (QA #54): the phone hears that a ticket needs you (a question asked, a gate waiting, a decision
applied) within about a second, not at the next outbox pass (core pumps addon events every `pull_seconds`, 60 s by
default).

A long-poll provider (always_on, like the inbox) watches the event log. A fetch loops for WAIT_S seconds; when the log
grows it reads the new events, turns them into the same sync items on_event makes (sync.watch_item) and runs
`addon.drain` on them at once. The periodic pass stays as the fallback: a ticket the watcher synced is remembered
(state watch.json, ticket -> newest seq), and TixAddon.drain acknowledges outbox items at or below that seq without
pushing again. Anything the watcher could not sync (offline, a CLI error) is simply left to the periodic pass.

Like the inbox it runs only while Mission Control is open or "Keep syncing while Mission Control runs" is on.
Needs orch-core's event log reader (orch.core.events); without it the provider has no scope and nothing changes."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from orch.addons.api import Snapshot

from . import sync

WAIT_S = 20.0        # one fetch (core caps a long-poll fetch at 40 s)
POLL_S = 0.5         # how often the log is looked at inside a fetch
MAX_EVENTS = 200     # one round's events; the rest come next round


MAX_SEQ_GAP = 1000   # as core: a line whose seq jumps further ahead than this is not an event (never moves a cursor)


def _log_path(ctx) -> Path | None:
    """orchestrator/.state/events.jsonl of the workspace. Addons may not import orch.core, so the file core appends to
    is read here (JSON lines: seq, at, ticket, kind, actor, via, data); anything that does not parse is skipped."""
    try:
        return Path(ctx.addon.ws.state_dir) / "events.jsonl"
    except AttributeError:
        return None


def _read(path: Path, after: int) -> tuple[list, int]:
    """(events with seq > after, in order; the newest valid seq). A forged far-ahead seq never counts."""
    try:
        raw = path.read_bytes()
    except OSError:
        return [], after
    events, last = [], 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            d = json.loads(line.decode("utf-8"))
            seq = d["seq"]
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last or seq > last + MAX_SEQ_GAP and last:
            continue
        last = seq
        if seq > after:
            events.append(SimpleNamespace(seq=seq, at=str(d.get("at") or ""), ticket=d.get("ticket"),
                                          kind=str(d.get("kind") or ""), actor=str(d.get("actor") or ""),
                                          via=str(d.get("via") or ""), data=d.get("data") if isinstance(d.get("data"), dict) else {}))
    return events, max(last, after) if events else max(last, 0)


class NeedsWatchProvider:
    id, kind, interval_s, always_on, mode = "needs-watch", "status", 5, True, "long_poll"

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        if _log_path(ctx) is None or not self.addon.state.space() or ctx.settings.get("link_mode") == "off":
            return []
        return ["space"]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        try:
            self.watch(ctx)
        except Exception as e:      # noqa: BLE001 — never lose the periodic pass over this
            self.addon.note_error(f"watch: {type(e).__name__}: {e}")
        return Snapshot(self.id, scope, now, items=())

    def watch(self, ctx) -> int:
        path = _log_path(ctx)
        if path is None:
            return 0
        st = self.addon.state
        cursor = st.watch()["cursor"]
        _, newest = _read(path, 0)
        if cursor is None or cursor > newest:           # first run (or the log was reset): start now, never replay
            st.set_watch(newest, {})
            cursor = newest
        deadline = time.monotonic() + WAIT_S
        synced, sig = 0, None
        while True:
            try:
                info = path.stat()
                now_sig = (info.st_size, info.st_mtime_ns)
            except OSError:
                now_sig = None
            if now_sig != sig:
                sig = now_sig
                events, _ = _read(path, cursor)
                if events:
                    events = events[:MAX_EVENTS]
                    synced += self._sync(ctx, events)
                    cursor = events[-1].seq
                    continue
            if time.monotonic() >= deadline:
                return synced
            time.sleep(POLL_S)

    def _sync(self, ctx, events) -> int:
        items, seqs = [], {}
        for e in events:
            item = sync.watch_item(e)
            if item is None:
                continue
            items.append({"id": f"watch-{e.seq}", "at": "", "data": item})
            seqs[f"watch-{e.seq}"] = (item["ticket"], e.seq)
        done: dict[str, int] = {}
        if items:
            for iid in self.addon.drain(ctx, items):
                if iid in seqs:
                    ref, seq = seqs[iid]
                    done[ref] = max(done.get(ref, 0), seq)
        self.addon.state.set_watch(events[-1].seq, done)
        return len(done)
