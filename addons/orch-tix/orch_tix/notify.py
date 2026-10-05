"""Per-ticket phone notifications, desktop side (default OFF).

The owner's choice lives in orch-core (`ticket_options`, "Notify my phone about this ticket": Mission Control's new-ticket
form, approve card and ticket page, or `orch addon ticket-option set`; only a human sets it). This addon copies it into
the mirror as the cleartext field `notify`, and the server sends a push for a ticket only while it is on.

The phone can change it too (the "Notify me about this ticket" switch on the ticket). The server keeps a counter of
the phone's changes (`notify_rev`); every push reports how many this desktop has merged (`notify_seen`), and the server
ignores a desktop value built before the phone's change. This desktop merges a phone change into orch-core
(`relay_ticket_option`, recorded as this addon, never as the human) the next time it looks (each inbox cycle) or when a
push answers with a value it did not send.

Messages that name no ticket follow the workspace setting `notify_unticketed` (default off), pushed to the server as the
space's `notify_messages`. Join requests always notify; nothing here touches them."""
from __future__ import annotations

from .cli import SharingError

OPTION = "notify"


def wanted(ctx, key: str):
    """The human's choice for this ticket as a bool, or None when this orch-core has no ticket options (the server then
    keeps its value, which starts off)."""
    fn = getattr(ctx, "ticket_option", None)
    if not callable(fn):
        return None
    try:
        return bool(fn(key, OPTION))
    except Exception:  # noqa: BLE001: an unknown option or ticket never holds back a push
        return None


def _adopt(addon, ctx, key: str, server_notify: bool, server_rev: int) -> None:
    """The phone's choice becomes the desktop's (when it differs), and its counter is remembered."""
    link = addon.state.links().get(key) or {}
    if server_rev <= int(link.get("notify_seen") or 0):
        return
    if wanted(ctx, key) != server_notify:
        try:
            ctx.addon.ops().relay_ticket_option(key, OPTION, server_notify)
        except Exception as e:  # noqa: BLE001: orch-core without relay: the phone's choice still stands on the server
            addon.note_error(f"notify {key}: {type(e).__name__}")
            return
    addon.state.set_notify_seen(key, server_rev)


def after_push(addon, ctx, key: str, result: dict) -> None:
    if "notify" not in result or not isinstance(result.get("notify_rev"), int):
        return
    sent = wanted(ctx, key)
    if sent is None:
        return
    if result["notify"] != sent:                       # the server kept the phone's newer choice
        _adopt(addon, ctx, key, bool(result["notify"]), int(result["notify_rev"]))
    else:
        addon.state.set_notify_seen(key, int(result["notify_rev"]))


def merge_phone_changes(addon, ctx) -> int:
    """Look at the server's per-ticket switches and adopt what the phone changed. Returns how many tickets changed.
    Errors are noted, never raised: this runs inside the inbox cycle."""
    links = {l.get("n"): k for k, l in addon.state.active_links().items() if l.get("n")}
    if not links:
        return 0
    try:
        r = addon.sharing(ctx).run_json("mirror", "notify-state", timeout=15)
    except SharingError as e:
        if e.code not in ("not_owner", "not_configured", "no_space", "unauthenticated", "run_failed"):
            addon.note_error(f"notify state: {e.detail}")
        return 0
    changed = 0
    for row in r.get("mirrors") or []:
        key = links.get(row.get("id")) if isinstance(row, dict) else None
        if key is None or not isinstance(row.get("notify_rev"), int):
            continue
        before = int((addon.state.links().get(key) or {}).get("notify_seen") or 0)
        if row["notify_rev"] > before:
            _adopt(addon, ctx, key, row.get("notify") is True, row["notify_rev"])
            changed += 1
    return changed


def push_message_setting(addon, ctx) -> None:
    """The workspace setting "Phone notifications for messages without a ticket" to the server, when it changed since the
    last time it went (or the server's copy differs). Only the space's owner device may; others are refused quietly."""
    want = bool(ctx.settings.get("notify_unticketed"))
    sent = addon.state.message_notify()
    if sent is want or (sent is None and not want):
        return              # the server starts off: nothing to send until the owner turns it on
    try:
        addon.sharing(ctx).run_json("space", "notify", "--messages", "on" if want else "off", timeout=15)
    except SharingError as e:
        if e.code not in ("not_owner", "not_configured", "no_space", "unauthenticated", "run_failed"):
            addon.note_error(f"notify messages: {e.detail}")
        return
    addon.state.set_message_notify(want)
