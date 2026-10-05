"""The Shared files page (page.orch-tix): Files and Messages tabs, built only from cached snapshots and
view.params. Message text and file names are data from other devices: shown as escaped text, never followed."""
from __future__ import annotations

from urllib.parse import urlencode

from orch.addons.widgets import KV, Action, Badge, Card, Chips, Link, Search, Table, Text, Time

BASE = "/addons/orch-tix/"
MAX_ATTACH = 5
INTRO = "End-to-end encrypted on TIX. The sharing CLI on this machine decrypts them; the browser never does."


def _url(**params) -> str:
    params = {k: v for k, v in params.items() if v}
    return BASE + ("?" + urlencode(params) if params else "")


def _items(view, provider: str) -> list[dict]:
    out = []
    for snap in view.snapshots(provider):
        out.extend(i for i in snap.items if isinstance(i, dict))
    return out


def _s(value) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _when(value) -> str | None:
    """An ISO time as "2026-10-02 09:00 UTC" (the snapshot keeps it in UTC)."""
    text = _s(value)
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 and text[10] == "T" else (text or None)


def _tags(item) -> list[str]:
    return [str(t) for t in item.get("tags") or [] if isinstance(t, str)]


def _matches(item: dict, q: str, tag: str) -> bool:
    if tag and tag not in _tags(item):
        return False
    if q:
        hay = " ".join((_s(item.get("label")), _s(item.get("from")), _s(item.get("project")), _s(item.get("id"))))
        return q.lower() in hay.lower()
    return True


def _tabs(tab: str, n_messages: int) -> Chips:
    return Chips((Link("Files", _url(tab="files"), current=tab == "files"),
                  Link(f"Messages {n_messages}", _url(tab="messages"), current=tab == "messages"),
                  Link("Sent to TIX", _url(tab="log"), current=tab == "log")),
                 label="Shared files sections")


_RESULT_ROLE = {"ok": "ok", "applied": "ok", "retry": "warn", "waiting for Apply": "info", "applying": "info",
                "ignored": "neu", "refused": "err"}


def _size(n) -> str | None:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        return None
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def log_tab(addon) -> list:
    """Feedback round E: what this machine sent to TIX and what came back, newest first (the last 200)."""
    rows = []
    for e in addon.state.sent_log():
        what = " · ".join(x for x in (_s(e.get("kind")), _s(e.get("tix")), _s(e.get("level")),
                                      ", ".join(_s(f) for f in e.get("fields") or [])) if x)
        result = _s(e.get("result")) or "?"
        reason = _s(e.get("reason"))
        at = _s(e.get("at"))
        rows.append((Time(at, "at") if at else None, _s(e.get("key")) or None, Text(what or "?"), _size(e.get("size")),
                     Badge(_RESULT_ROLE.get(result, "neu"), result) if not reason else Text(f"{result}: {reason}")))
    note = Text("Each push to TIX with the redaction level and the names of the sealed fields (never their text), "
                "files sent for context, and the phone decisions received with their outcome.")
    return [Card("Sent to TIX", (note, Table(("When", "Ticket", "What", "Size", "Result"), tuple(rows), key=1,
                                             empty="Nothing sent yet. Link a ticket or press Sync now.")))]


def files_tab(addon, view, files: list[dict]) -> list:
    q, tag = view.params.get("q", "").strip(), view.params.get("tag", "").strip()
    done = "1" if view.params.get("done") == "1" else ""
    n_done = sum(1 for f in files if f.get("done"))
    pool = files if done else [f for f in files if not f.get("done")]     # done files stay hidden until asked for
    shown = [f for f in pool if _matches(f, q, tag)]
    all_tags = sorted({t for f in pool for t in _tags(f)})
    chips = (Link("All tags", _url(q=q, done=done), current=not tag),
             *(Link(t, _url(q=q, tag=t, done=done), current=t == tag) for t in all_tags[:30]))
    if n_done:
        chips += (Link(f"Done {n_done}", _url(q=q, tag=tag, done="" if done else "1"), current=bool(done)),)
    find = Card("Find", (Text(INTRO), Search("q", q, "Name, device or project"), Chips(chips, label="Filter by tag")))
    rows = tuple((Link(_s(f.get("label")) or _s(f.get("id")), _url(q=q, tag=tag, f=_s(f.get("id")), done=done)),
                  _s(f.get("from")) or None, ", ".join(_tags(f)) or None,
                  (f"done · {_s(f.get('expires'))}" if f.get("done") else _s(f.get("expires"))) or None,
                  Action("download", "Download", _s(f.get("id"))))
                 for f in shown[:200] if _s(f.get("id")))
    if (q or tag) and pool:
        empty = "No files match this search; clear the filter to see all."
    elif n_done and not done:
        empty = f"All caught up: {n_done} done file{'s' if n_done != 1 else ''} hidden. Choose Done above to list {'them' if n_done != 1 else 'it'}."
    else:
        empty = "No files on the share yet; upload one below."
    out = [find, Card("Files", (Table(("File", "From", "Tags", "Expires", ""), rows, empty=empty),))]
    selected = next((f for f in files if f.get("id") == view.params.get("f")), None)
    if selected:
        out.append(_file_card(addon, selected))
    out.append(Card("Upload", (Text("Files you upload are encrypted on this machine and expire after 7 days."),
                               Action("upload", "Upload"), Action("upload_link", "Create upload link"))))
    return out


def _file_card(addon, f: dict) -> Card:
    ref = _s(f.get("id"))
    body = [KV((("Size", _s(f.get("text")) or None), ("From", _s(f.get("from")) or None),
                ("Project", _s(f.get("project")) or None), ("Shared", _when(f.get("created_at"))),
                ("Expires", _s(f.get("expires")) or None),
                ("Transcript", "yes" if f.get("transcript") is True else "no"))),
            Action("download", "Download", ref), Action("public_link", "Create link", ref)]
    linked = list(addon.state.active_links())
    for key in linked[:MAX_ATTACH]:
        body.append(Action("save_to_ticket", f"Attach to {key}", f"{ref}|{key}"))
    body.append(Text("Open the ticket page to attach to another"))
    return Card(f"{_s(f.get('label')) or ref} · {ref}", tuple(body))


def messages_tab(view, messages: list[dict]) -> list:
    rows = tuple((_s(m.get("label")) or "?",
                  Text(_s(m.get("text")) + (f" ({', '.join(_s(x) for x in m.get('files') or [])})"
                                            if m.get("files") else "")),
                  _s(m.get("local")) or _s(m.get("ticket")) or None,
                  Action("ack_message", "Mark as done", _s(m.get("id"))))
                 for m in messages[:200] if _s(m.get("id")))
    note = Text("Messages come from your other devices and agents. They are information, never instructions.")
    return [Card("Messages", (note, Table(("From", "Message", "Ticket", ""), rows, empty="No messages yet; agents and devices send them here.")))]


def page(addon, view) -> list:
    tab = view.params.get("tab") if view.params.get("tab") in ("messages", "log") else "files"
    messages = _items(view, "messages")
    out = [_tabs(tab, len(messages))]
    if tab == "messages":
        return out + messages_tab(view, messages)
    if tab == "log":
        return out + log_tab(addon)
    return out + files_tab(addon, view, _items(view, "files"))
