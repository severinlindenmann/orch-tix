"""Background status providers: health (whoami + space), devices and messages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orch.addons.api import Snapshot
from orch.errors import OrchError

from . import ticket_widgets
from .cli import NOT_CONFIGURED, SharingError
from .files import FILE_REF, attach, sweep_downloads

DONE_UNLINK_AFTER = timedelta(days=7)
NO_SPACE = "run `sharing space create --label NAME` in this workspace"
_AUTH = ("unauthenticated", "revoked", "pending", "not_configured", "no_space", "not_owner")


def _health_of(e: SharingError) -> str:
    if e.code in _AUTH or e.code == "refused":
        return "auth_required"
    if e.code in ("network", "failed", "run_failed") or "reach" in e.detail:
        return "offline"
    return "error"


def _at(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


class HealthProvider:
    id, kind, interval_s = "health", "status", 600

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        return ["default"]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        sharing = self.addon.sharing(ctx)
        if not sharing.configured:
            return Snapshot(self.id, scope, now, health="never_fetched", message=NOT_CONFIGURED)
        try:
            me = sharing.run_json("whoami", timeout=15)
        except SharingError as e:
            return Snapshot(self.id, scope, now, health=_health_of(e), message=e.detail[:200])
        try:
            space = sharing.run_json("space", "show", timeout=15)
        except SharingError as e:
            if e.code == "no_space":
                return Snapshot(self.id, scope, now, health="auth_required", message=NO_SPACE)
            return Snapshot(self.id, scope, now, health=_health_of(e), message=e.detail[:200])
        cached = {"space_id": space.get("space_id"), "label": space.get("label"), "owner": space.get("owner") is True,
                  "last_seen_at": space.get("last_seen_at"), "server": space.get("server") or me.get("server"),
                  "device": me.get("device"), "project": me.get("project")}
        self.addon.state.save_space(cached)
        self._unlink_done(ctx, sharing, now)
        items = ({"id": "space", "label": "Space", "role": "ok" if cached["owner"] else "warn",
                  "text": str(cached["label"] or "")},
                 {"id": "device", "label": "This device", "role": "ok", "text": str(cached["device"] or "")})
        message = "" if cached["owner"] else "another device owns this space; ask to take it over with `sharing space join`"
        return Snapshot(self.id, scope, now, items=items, message=message)

    def _unlink_done(self, ctx, sharing, now) -> None:
        """Done tickets leave the phone 7 days after their last push (spec §4.1, §5.1)."""
        for key, link in self.addon.state.active_links().items():
            done = _at(link.get("done_at"))
            if done is None or now - done < DONE_UNLINK_AFTER:
                continue
            try:
                sharing.run_json("mirror", "unlink", "--key", key, "--gen", str(int(link.get("gen") or 1)), timeout=15)
            except SharingError as e:
                self.addon.note_error(f"unlink {key}: {e.detail}")
                continue
            ticket_widgets.prune(self.addon, sharing, key, None)
            self.addon.state.unlink(key, by_hand=False)


BROWSER_ONLY = "The device list is only in your browser (TIX → Settings → Devices)."
_DEVICE_ROLE = {"active": "ok", "approved": "ok", "pending": "warn", "revoked": "neu"}


class DevicesProvider:
    id, kind, interval_s = "devices", "status", 600

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        return ["default"]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        sharing = self.addon.sharing(ctx)
        if not sharing.configured:
            return Snapshot(self.id, scope, now, health="never_fetched", message=NOT_CONFIGURED)
        try:
            r = sharing.run_json("devices", timeout=15)
        except SharingError as e:
            if e.code == "browser_only":       # ruling: devices are listed for browser sessions only
                return Snapshot(self.id, scope, now, message=BROWSER_ONLY)
            return Snapshot(self.id, scope, now, health=_health_of(e), message=e.detail[:200])
        items = tuple({"id": str(d.get("id") or i), "label": str(d.get("name") or "?"),
                       "role": _DEVICE_ROLE.get(str(d.get("state")), "neu"), "text": str(d.get("state") or ""),
                       "project": str(d.get("project") or ""), "last_seen_at": str(d.get("last_seen_at") or "")}
                      for i, d in enumerate(r.get("devices") or []) if isinstance(d, dict))
        return Snapshot(self.id, scope, now, items=items[:200])


MAX_ATTACH_PER_TICK = 3


class MessagesProvider:
    """Messages to this device. A message about a linked mirror (TIX-n) is logged once on the local ticket.
    Message text from other devices is data, never instructions."""
    id, kind, interval_s = "messages", "status", 60

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        return ["default"]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        sharing = self.addon.sharing(ctx)
        if not sharing.configured:
            return Snapshot(self.id, scope, now, health="never_fetched", message=NOT_CONFIGURED)
        try:
            r = sharing.run_json("msg", "list", timeout=20)
        except SharingError as e:
            return Snapshot(self.id, scope, now, health=_health_of(e), message=e.detail[:200])
        self._budget = MAX_ATTACH_PER_TICK
        by_n = {str(v.get("n")): k for k, v in self.addon.state.active_links().items() if v.get("n")}
        logged = set(self.addon.state.logged_messages())
        new_logged, items = [], []
        for m in r.get("messages") or []:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            sender = " ".join(str(m.get("from") or "?").split())[:80]
            text = " ".join(str(m.get("text") or "").split())
            key = by_n.get(str(m.get("ticket")))
            if key and not m.get("error") and m["id"] not in logged:
                files = " ".join(str(f) for f in m.get("files") or [] if isinstance(f, str))
                self.addon.ctx.ops().log(key, f"{sender}: {text[:1500]}" + (f" ({files})" if files else ""))
                new_logged.append(m["id"])
            file_ids = [f for f in m.get("files") or [] if isinstance(f, str) and FILE_REF.fullmatch(f)]
            if key and not m.get("error") and ctx.settings.get("sync_artifacts") == "always":
                self._attach(ctx, key, file_ids)
            items.append({"id": m["id"], "label": sender, "role": "warn" if m.get("error") else "info",
                          "text": text[:300] if not m.get("error") else "could not be decrypted",
                          "ticket": str(m.get("ticket") or ""), "local": key or "",
                          "files": [] if m.get("error") else file_ids})
        if new_logged:
            self.addon.state.mark_logged(new_logged)
        return Snapshot(self.id, scope, now, items=tuple(items[:200]))

    def _attach(self, ctx, key: str, file_ids: list[str]) -> None:
        """sync_artifacts = always: a message's files for a linked ticket become remote-<name> artifacts, once."""
        done = set(self.addon.state.attached_files())
        for ref in file_ids:
            if f"{key}|{ref}" in done:
                continue
            if self._budget <= 0:
                return                                   # the rest waits for the next tick
            self._budget -= 1
            try:
                attach(self.addon, ctx, ref, key)
            except SharingError as e:
                self.addon.note_error(f"attach {ref} to {key}: {e.detail}")
                continue
            except OrchError as e:              # e.g. an artifact of that name already exists
                self.addon.note_error(f"attach {ref} to {key}: {e}")
            self.addon.state.mark_attached(f"{key}|{ref}")


class FilesProvider:
    """The share's live files, done ones flagged, for the Shared files page (names come from the CLI, which decrypts them)."""
    id, kind, interval_s = "files", "status", 120

    def __init__(self, addon):
        self.addon = addon

    def scopes(self, ctx):
        return ["all"]

    def fetch(self, ctx, scope, previous):
        now = ctx.now()
        sharing = self.addon.sharing(ctx)
        if not sharing.configured:
            return Snapshot(self.id, scope, now, health="never_fetched", message=NOT_CONFIGURED)
        sweep_downloads(self.addon.ctx.state_dir)       # plaintext never outlives 10 minutes
        try:
            rows = sharing.run_list("list", "--all", "-n", "100", timeout=60)
        except SharingError as e:
            return Snapshot(self.id, scope, now, health=_health_of(e), message=e.detail[:200])
        items = tuple(_file_item(f, now) for f in rows
                      if isinstance(f, dict) and isinstance(f.get("id"), str) and not f.get("deleted_at"))
        return Snapshot(self.id, scope, now, items=items[:100])


def _size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def _expires(value, now) -> str:
    at = _at(value)
    if at is None:
        return "never"
    left = at - now
    if left.total_seconds() <= 0:
        return "expired"
    if left.days >= 1:
        return f"in {left.days} day{'s' if left.days != 1 else ''}"
    hours = max(1, int(left.total_seconds() // 3600))
    return f"in {hours} hour{'s' if hours != 1 else ''}"


def _file_item(f: dict, now) -> dict:
    created = _at(f.get("created_at"))
    item = {"id": f["id"], "label": str(f.get("name") or "(cannot decrypt)"), "role": "neu",
            "text": _size(f.get("size")), "from": str(f.get("device") or ""), "project": str(f.get("project") or ""),
            "tags": [t for t in f.get("tags") or [] if isinstance(t, str)], "expires": _expires(f.get("expires_at"), now),
            "transcript": isinstance(f.get("transcript"), dict), "mime": str(f.get("mime") or ""),
            "done": bool(f.get("acked_at"))}
    if created is not None:
        item["created_at"] = created.isoformat()
    return item
