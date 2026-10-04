"""The addon's own small files under ctx.state_dir: links.json, space.json, decisions.json, inbox.json, history.json, sentlog.json.
Every write holds state.lock and goes through a temp file + os.replace; files are 0600."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

PRUNE_AFTER = timedelta(days=30)
LOG_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, state_dir):
        self.dir = Path(state_dir)

    # -- files -------------------------------------------------------------------------------------------
    def _lock(self) -> FileLock:
        self.dir.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.dir / "state.lock"), timeout=10)

    def _read(self, name: str, *, keep_broken: bool = False) -> dict:
        """The file as a dict; a missing or corrupt one reads as {}. Never writes (reads also run during page
        renders); only the write path, under the lock, keeps a corrupt file as `<name>.broken`."""
        path = self.dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        if keep_broken:
            broken = path.with_name(name + ".broken")
            fd = os.open(broken, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        return {}

    def _write(self, name: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.dir / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _update(self, name: str, fn):
        with self._lock():
            data = self._read(name, keep_broken=True)
            result = fn(data)
            self._write(name, data)
            return result

    # -- links -------------------------------------------------------------------------------------------
    def links(self) -> dict:
        return self._read("links.json")

    def active_links(self) -> dict:
        return {k: v for k, v in self.links().items() if not v.get("retired")}

    def link(self, key: str, *, by: str, auto: bool) -> dict:
        """Start (or restart) mirroring `key`. A new link is gen 1, rev 0. A retired link moves to the next
        generation and is pushed with --relink once (the CLI reports the generation it really used); only the
        human's link action restarts a retired link (sync.drain never does)."""
        def fn(links):
            old = links.get(key) or {}
            entry = dict(old)
            if not old:
                entry.update(gen=1, rev=0)
            elif old.get("retired"):
                entry.update(gen=int(old.get("gen") or 1) + 1, relink=True, n=None, done_at=None,
                             context_artifacts=[])
            entry.update(linked_at=_now(), by=by, auto=auto, unlinked_by_hand=False, retired=False)
            entry.setdefault("rev", 0)
            links[key] = entry
            return dict(entry)
        return self._update("links.json", fn)

    def unlink(self, key: str, by_hand: bool = True) -> None:
        def fn(links):
            if key in links:
                links[key].update(retired=True, unlinked_by_hand=by_hand, unlinked_at=_now(), relink=False)
        self._update("links.json", fn)

    def reserve_rev(self, key: str, rev: int) -> None:
        """Take `rev` before the CLI call (final review I4): a push the server stored but whose answer was lost
        (a timeout) must never be pushed again under the same rev, or the retry's newer doc is a "duplicate"."""
        def fn(links):
            entry = links.setdefault(key, {"gen": 1, "rev": 0})
            entry["rev"] = max(int(entry.get("rev") or 0), int(rev))
        self._update("links.json", fn)

    def commit_rev(self, key: str, rev: int, n: str | None, *, gen: int | None = None, done: bool = False) -> None:
        """After a successful push: the rev the server now has, the TIX id and the generation the CLI used."""
        def fn(links):
            entry = links.setdefault(key, {"gen": 1, "rev": 0})
            entry["rev"] = max(int(entry.get("rev") or 0), int(rev))
            if n:
                entry["n"] = n
            if gen:
                entry["gen"] = int(gen)
            entry["relink"] = False
            entry["pushed_at"] = _now()
            entry["done_at"] = (entry.get("done_at") or _now()) if done else None
        self._update("links.json", fn)

    def retire(self, key: str, gen: int | None = None) -> None:
        """The server says this link is over (unlinked elsewhere): keep the entry. A retired link is never
        auto-linked again, whatever retired it; only the explicit link action re-links (with --relink)."""
        def fn(links):
            if key in links:
                # a human's Stop syncing that raced this push stays a human's: never auto-link it again
                links[key].update(retired=True, relink=False, unlinked_at=_now(),
                                  unlinked_by_hand=bool(links[key].get("unlinked_by_hand")))
                if gen:
                    links[key]["gen"] = int(gen)
        self._update("links.json", fn)

    def set_redaction(self, key: str, value: str | None) -> None:
        def fn(links):
            if key in links:
                links[key]["redaction"] = value
        self._update("links.json", fn)

    def add_context_artifact(self, key: str, name: str, file_id: str) -> None:
        def fn(links):
            entry = links.setdefault(key, {"gen": 1, "rev": 0})
            arts = [a for a in entry.get("context_artifacts") or [] if a.get("name") != name]
            entry["context_artifacts"] = [*arts, {"name": name, "file": file_id}]
        self._update("links.json", fn)

    # -- the "Sent to TIX" log (feedback round E): what left this machine and what came back ------------
    def log(self, entry: dict) -> None:
        """Append one entry (a push, a shared file, a phone decision). Names and counts only, never sealed text."""
        rec = {**entry, "at": entry.get("at") or _now()}
        self._update("sentlog.json", lambda d: d.__setitem__("entries", [*(d.get("entries") or []), rec][-LOG_MAX:]))

    def sent_log(self) -> list:
        return list(reversed(self._read("sentlog.json").get("entries") or []))

    # -- history (orch events per linked ticket, history.py) ----------------------------------------------
    def history(self, key: str) -> list:
        return list(self._read("history.json").get(key) or [])

    def add_history(self, key: str, entries) -> None:
        from .history import merge
        new = [e for e in entries or [] if isinstance(e, dict)]
        if not new:
            return
        self._update("history.json", lambda d: d.__setitem__(key, merge(d.get(key) or [], new)))

    # -- space ---------------------------------------------------------------------------------------------
    def space(self) -> dict | None:
        return self._read("space.json") or None

    def save_space(self, d: dict) -> None:
        with self._lock():
            self._write("space.json", dict(d))

    # -- decisions -----------------------------------------------------------------------------------------
    def decisions(self) -> dict:
        return self._read("decisions.json")

    def put_decision(self, did: str, rec: dict) -> None:
        def fn(data):
            data.setdefault(did, dict(rec))
            self._prune(data)
        self._update("decisions.json", fn)

    def update_decision(self, did: str, **fields) -> None:
        def fn(data):
            if did in data:
                data[did].update(fields)
        self._update("decisions.json", fn)

    @staticmethod
    def _prune(data: dict) -> None:
        cutoff = datetime.now(timezone.utc) - PRUNE_AFTER
        for did, rec in list(data.items()):
            try:
                at = datetime.fromisoformat(str(rec.get("received_at")))
            except ValueError:
                continue
            if rec.get("acked") and at.tzinfo is not None and at < cutoff:
                del data[did]

    # -- inbox cursor and logged messages ------------------------------------------------------------------
    def cursor(self) -> int:
        try:
            return int(self._read("inbox.json").get("cursor") or 0)
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, value: int) -> None:
        self._update("inbox.json", lambda d: d.update(cursor=int(value)))

    def logged_messages(self) -> list:
        return list(self._read("inbox.json").get("logged") or [])

    def mark_logged(self, ids) -> None:
        def fn(d):
            d["logged"] = list(dict.fromkeys([*(d.get("logged") or []), *ids]))[-500:]
        self._update("inbox.json", fn)

    def attached_files(self) -> list:
        return list(self._read("inbox.json").get("attached") or [])

    def mark_attached(self, entry: str) -> None:
        def fn(d):
            d["attached"] = list(dict.fromkeys([*(d.get("attached") or []), entry]))[-1000:]
        self._update("inbox.json", fn)
