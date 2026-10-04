"""Widgets for the TIX section (Workspace & addons), the ticket's "Synced to TIX" panel and the Today tile.
Read-only: the addon's own state files and cached snapshots, never a command."""
from __future__ import annotations

from orch.addons.widgets import KV, Action, Callout, Card, Copy, Table, Text, Tile

from .cli import configured_path
from .mapping import LEVEL_LABEL

BACKGROUND_HINT = ("Turn on \"Keep syncing while Mission Control runs\" to get phone answers without an open "
                   "Mission Control tab.")
DEVICES_NOTE = "Approve and revoke devices on tix.severin.io → Settings."
FOREIGN_TITLE = "Other devices can read these tickets"
SHARED_KEY_NOTE = "Every TIX device can read synced tickets"
FOREIGN_TEXT = "Every TIX device holds the key. Use Title only or Key only for confidential clients."


def _text(value, fallback: str = "") -> str:
    return str(value) if isinstance(value, (str, int, float)) and str(value) else fallback


def _items(view, provider: str) -> list[dict]:
    out = []
    for snap in view.snapshots(provider):
        out.extend(i for i in snap.items if isinstance(i, dict))
    return out


def uses_full(view, links: dict) -> bool:
    return view.settings.get("redaction") == "full" or any(
        (v or {}).get("redaction") == "full" for v in links.values() if not (v or {}).get("retired"))


def foreign_devices(view, space: dict | None) -> bool | None:
    """Whether the devices snapshot shows an active device of another project; None when the list is unknown (a
    device cannot list devices, only the browser can)."""
    devices = [d for d in _items(view, "devices") if d.get("role") == "ok"]
    if not devices:
        return None
    mine = (space or {}).get("project")
    return any(d.get("project") != mine for d in devices)


def workspace_section(addon, view) -> list:
    space = addon.state.space() or {}
    links = addon.state.links()
    active = [k for k, v in links.items() if not v.get("retired")]
    body = [KV((("Server", _text(space.get("server"), "not set up")),
                ("Space", _text(space.get("label"), "none yet")),
                ("This device", _text(space.get("device"), "unknown")),
                ("Desktop last seen", _text(space.get("last_seen_at"), "never")),
                ("Linked tickets", len(active)),
                ("Background sync", BACKGROUND_HINT)))]
    if not configured_path(view.settings.get("sharing_path")):
        body.append(Callout("warn", "Set the sharing path",
                            "Save the absolute path of the sharing CLI in the TIX settings below."))
        body.append(Copy("Path", str(view.root / ".claude/skills/sharing/sharing")))
    for err in addon.errors[-3:]:
        body.append(Callout("err", "Sync problem", err))
    if uses_full(view, links):
        foreign = foreign_devices(view, space)
        if foreign:
            body.append(Callout("warn", FOREIGN_TITLE, FOREIGN_TEXT))
        elif foreign is None:
            body.append(Text(SHARED_KEY_NOTE))
    rows = tuple((" · ".join(x for x in (_text(d.get("label"), "?"), _text(d.get("project"))) if x), _text(d.get("text")),
                  _text(d.get("last_seen_at"))) for d in _items(view, "devices"))
    body.append(Table(("Device", "State", "Last seen"), rows, empty="The device list is in your browser."))
    body.append(Text(DEVICES_NOTE))
    body.append(Action("push_now", "Sync now"))
    return [Card("TIX", tuple(body))]


def ticket_panel(addon, view) -> list:
    ticket = view.ticket
    if ticket is None:
        return []
    key = ticket.id
    link = addon.state.links().get(key)
    if not link or link.get("retired"):
        return [Card("Synced to TIX", (Text("Not on the phone."), Action("link", "Sync to TIX", key)))]
    override = link.get("redaction")
    level = override or view.settings.get("redaction") or "title"
    body = [KV((("TIX", _text(link.get("n"), "waiting for the first push")),
                ("Shows", LEVEL_LABEL.get(level, level)),
                ("Last push", f"rev {int(link.get('rev') or 0)}"))),
            Action("push_now", "Sync now", key)]
    if override == "full":
        body.append(Action("redaction", "Use the workspace setting", key, confirm="Show this ticket as the workspace "
                                                                                 "setting says?"))
    else:
        body.append(Action("redaction", "Show in full on the phone", key))
    body.append(Action("unlink", "Stop syncing", key))
    sent = {a.get("name") for a in link.get("context_artifacts") or []}
    try:
        names = [str(a) for a in view.document(key).get("artifacts") or []]
    except Exception:
        names = []
    if level == "key-only":
        names = []                             # no files leave the machine at Key only
    for name in names[:20]:
        target = f"{key}|{name}"
        if len(target) <= 500:
            label = f"Send {name} again" if name in sent else f"Send {name} to phone"
            body.append(Action("send_artifact", label[:200], target))
    return [Card("Synced to TIX", tuple(body))]


def summary_tile(addon, view) -> list:
    n = sum(1 for rec in addon.state.decisions().values() if rec.get("outcome") is None)
    return [Tile("Phone answers", n, "info", href="/")] if n > 0 else []


def page(addon, view) -> list:
    from .files_page import page as files_page
    return files_page(addon, view)
