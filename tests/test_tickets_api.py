"""Tickets (spec T4–T6): migration, create/list/get/patch/delete, validation, limits, idempotency."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from fileshare import ids
from fileshare.db import connect
from tests.helpers.files import upload
from tests.helpers.onboard import onboard_device
from tests.helpers.tickets import (OMIT, claim, create, device_client, device_client_for, fake_env,  # noqa: F401
                                   get, new_uuid, other_device, other_device_client, post_event)

T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

TICKET_OUT_KEYS = {"id", "n", "uuid", "key_version", "wrapped_dek", "enc_content", "status", "project",
                   "type", "priority", "labels", "due", "parent", "blocked_by", "created_by", "opened_by",
                   "rev", "open_questions", "files", "claim", "created_at", "updated_at", "deleted_at", "last_seq",
                   "archived_at"}
EVENT_OUT_KEYS = {"seq", "ticket", "uuid", "kind", "actor", "status_from", "status_to", "created_at",
                  "files", "enc_body"}


def _db(settings) -> sqlite3.Connection:
    return connect(settings.db_path)


# --- migration ----------------------------------------------------------------------------

def test_migration_creates_the_ticket_tables(app, settings):
    conn = _db(settings)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "9"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"tickets", "ticket_labels", "ticket_blocks", "ticket_files", "ticket_events",
            "push_subs"} <= names


# --- ids ------------------------------------------------------------------------------------

@pytest.mark.parametrize("ref", ["TIX-42", "tix-42", "TIX42", "tix42", "42", "Tix-42"])
def test_ticket_refs_parse(ref):
    assert ids.parse_ticket_ref(ref) == 42


@pytest.mark.parametrize("ref", ["", "0", "TIX-", "TIX-0", "FILE42", "TIX--42", "tix 42", "-42", "4a",
                                 "attention", "changes", "TIX-42x", None])
def test_bad_ticket_refs(ref):
    assert ids.parse_ticket_ref(ref) is None


def test_format_ticket_id():
    assert ids.format_ticket_id(42) == "TIX-42"


# --- create -----------------------------------------------------------------------------------

def test_create_as_session(session_client):
    t = create(session_client, status="backlog", project="orchestrator", labels=["Web", "tags"],
               type="bug", priority="high", due="2026-10-01")
    assert set(t) == TICKET_OUT_KEYS
    assert t["id"] == f"TIX-{t['n']}" and t["status"] == "backlog"
    assert t["project"] == "orchestrator" and t["labels"] == ["tags", "web"]
    assert t["type"] == "bug" and t["priority"] == "high" and t["due"] == "2026-10-01"
    assert t["created_by"] == {"kind": "web", "id": None, "name": "browser"}
    assert t["rev"] == 1 and t["open_questions"] == 0 and t["claim"] is None
    assert t["opened_by"] is None and t["files"] == [] and t["blocked_by"] == [] and t["parent"] is None
    assert t["last_seq"] >= 1 and t["deleted_at"] is None


def test_create_open_sets_opened_by(session_client):
    assert create(session_client, status="open")["opened_by"] == "browser"


def test_create_as_device_takes_the_project_from_the_device(device_client, device):
    t = create(device_client, project="somewhere-else")
    assert t["project"] == device.project
    assert t["created_by"] == {"kind": "device", "id": device.id, "name": device.name}
    assert t["opened_by"] == device.name


def test_session_create_needs_a_project(session_client):
    r = create(session_client, raw=True, project=OMIT)
    assert r.status_code == 400


def test_ids_increase_and_are_never_reused(session_client):
    a = create(session_client)
    b = create(session_client)
    assert b["n"] == a["n"] + 1
    assert session_client.delete(f"/api/tickets/{b['id']}").status_code == 204
    c = create(session_client)
    assert c["n"] == b["n"] + 1


def test_create_rejects_moves_outside_the_matrix(session_client):
    r = create(session_client, status="in-progress", raw=True)
    assert r.status_code == 409 and r.json()["error"] == "bad_move"
    r = create(session_client, status="nope", raw=True)
    assert r.status_code == 400


@pytest.mark.parametrize("field,value,code", [
    ("labels", ["bad label!"], "bad_request"),
    ("labels", [f"l{i}" for i in range(11)], "bad_request"),
    ("labels", "notalist", "bad_request"),
    ("project", "no spaces", "bad_request"),
    ("project", "x" * 65, "bad_request"),
    ("due", "2026-13-01", "bad_request"),
    ("due", "tomorrow", "bad_request"),
    ("type", "epic", "bad_request"),
    ("priority", "meh", "bad_request"),
    ("blocked_by", ["TIX-999"], "bad_ref"),
    ("blocked_by", ["nonsense"], "bad_ref"),
    ("blocked_by", "TIX-1", "bad_request"),
    ("parent", "TIX-999", "bad_ref"),
    ("enc_content", "OVERSIZED", "bad_request"),
    ("enc_content", "not base64!", "bad_request"),
    ("wrapped_dek", "SHORT_DEK", "bad_request"),
    ("uuid", "XYZ", "bad_request"),
    ("event_uuid", OMIT, "bad_request"),
    ("key_version", 0, "bad_request"),
])
def test_create_validation(session_client, field, value, code):
    # generated here, not in the table: random values would make unstable test ids
    value = {"OVERSIZED": fake_env(196608), "SHORT_DEK": fake_env(10)}.get(value, value) \
        if isinstance(value, str) else value
    r = create(session_client, raw=True, **{field: value})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == code


def test_create_with_parent_and_blockers(session_client):
    a = create(session_client)
    b = create(session_client)
    t = create(session_client, parent=f"tix{a['n']}", blocked_by=[str(b["n"]), a["id"], a["id"]])
    assert t["parent"] == a["id"]
    assert t["blocked_by"] == [a["id"], b["id"]]


def test_too_many_blockers(session_client):
    refs = [create(session_client)["id"] for _ in range(21)]
    r = create(session_client, raw=True, blocked_by=refs)
    assert r.status_code == 400


def test_61st_device_create_within_an_hour_is_429(device_client, session_client, frozen_clock):
    frozen_clock(T0)
    for _ in range(60):
        create(device_client)
    r = create(device_client, raw=True)
    assert r.status_code == 429
    create(session_client)                      # the browser is not limited
    frozen_clock(T0 + timedelta(hours=1, seconds=1))
    create(device_client)


def test_duplicate_ticket_uuid_is_409(session_client):
    t = create(session_client)
    r = create(session_client, raw=True, uuid=t["uuid"])
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_duplicate_created_event_uuid_is_409(session_client):
    ev = new_uuid()
    create(session_client, event_uuid=ev)
    r = create(session_client, raw=True, event_uuid=ev)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_duplicate_event_uuid_is_409(session_client):
    t = create(session_client)
    u = new_uuid()
    post_event(session_client, t["id"], "update", uuid=u)
    r = post_event(session_client, t["id"], "update", uuid=u, raw=True)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"
    assert len(get(session_client, t["id"])["events"]) == 2


def test_pending_device_is_403(app, owner, sharing, session_client):
    t = create(session_client)
    pending = onboard_device(owner, sharing, name="pend", project="proj", approve=False)
    c = device_client_for(app, pending)
    for method, path in [("GET", "/api/tickets"), ("POST", "/api/tickets"), ("GET", f"/api/tickets/{t['id']}"),
                         ("PATCH", f"/api/tickets/{t['id']}"), ("POST", f"/api/tickets/{t['id']}/events"),
                         ("POST", f"/api/tickets/{t['id']}/claim"), ("DELETE", f"/api/tickets/{t['id']}/claim")]:
        r = c.request(method, path, json={})
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["error"] == "pending"


def test_session_writes_check_origin(app, session_client):
    t = create(session_client)
    r = session_client.patch(f"/api/tickets/{t['id']}", json={"rev": 1, "event_uuid": new_uuid()},
                             headers={"Origin": "http://evil"})
    assert r.status_code == 403


def test_device_cannot_delete(device_client, session_client):
    t = create(session_client)
    r = device_client.delete(f"/api/tickets/{t['id']}")
    assert r.status_code in (401, 403)
    assert get(session_client, t["id"])["status"] == "open"


# --- list ----------------------------------------------------------------------------------------

def _ids(r) -> list[str]:
    assert r.status_code == 200, r.text
    return [t["id"] for t in r.json()["tickets"]]


def test_list_filters(session_client, frozen_clock):
    frozen_clock(T0)
    a = create(session_client, status="backlog", project="alpha", labels=["web", "ui"])
    frozen_clock(T0 + timedelta(seconds=1))
    b = create(session_client, status="open", project="beta", labels=["web"])
    frozen_clock(T0 + timedelta(seconds=2))
    c = create(session_client, status="open", project="alpha")
    assert _ids(session_client.get("/api/tickets")) == [c["id"], b["id"], a["id"]]
    assert _ids(session_client.get("/api/tickets?status=open")) == [c["id"], b["id"]]
    assert _ids(session_client.get("/api/tickets?status=open&status=backlog")) == [c["id"], b["id"], a["id"]]
    assert _ids(session_client.get("/api/tickets?project=alpha")) == [c["id"], a["id"]]
    assert _ids(session_client.get("/api/tickets?label=web")) == [b["id"], a["id"]]
    assert _ids(session_client.get("/api/tickets?label=web&label=UI")) == [a["id"]]
    assert _ids(session_client.get("/api/tickets?limit=1")) == [c["id"]]
    # an event bumps updated_at, so a moves to the top
    frozen_clock(T0 + timedelta(seconds=3))
    post_event(session_client, a["id"], "update")
    assert _ids(session_client.get("/api/tickets"))[0] == a["id"]


@pytest.mark.parametrize("q", ["status=nope", "limit=0", "limit=501", "limit=x", "project=bad%20p",
                               "label=bad!"])
def test_list_bad_params(session_client, q):
    assert session_client.get(f"/api/tickets?{q}").status_code == 400


def test_list_as_device(device_client, session_client):
    t = create(session_client)
    assert _ids(device_client.get("/api/tickets")) == [t["id"]]


# --- get ------------------------------------------------------------------------------------------

def test_get_includes_ascending_events(session_client):
    t = create(session_client)
    post_event(session_client, t["id"], "update")
    got = get(session_client, t["id"])
    assert set(got) == TICKET_OUT_KEYS | {"events"}
    evs = got["events"]
    assert [e["kind"] for e in evs] == ["created", "update"]
    assert set(evs[0]) == EVENT_OUT_KEYS
    assert evs[0]["seq"] < evs[1]["seq"] == got["last_seq"]
    assert evs[0]["status_from"] is None and evs[0]["status_to"] == "open"
    assert evs[0]["actor"] == {"kind": "web", "id": None, "name": "browser"}
    assert evs[0]["enc_body"] is None and evs[1]["enc_body"]
    assert evs[0]["ticket"] == t["id"]


@pytest.mark.parametrize("ref", ["nope", "TIX-999", "0"])
def test_get_unknown_is_404(session_client, ref):
    r = session_client.get(f"/api/tickets/{ref}")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


@pytest.mark.parametrize("form", ["TIX-{n}", "tix-{n}", "TIX{n}", "tix{n}", "{n}"])
def test_get_accepts_every_ref_form(session_client, form):
    t = create(session_client)
    assert get(session_client, form.format(n=t["n"]))["id"] == t["id"]


# --- patch ------------------------------------------------------------------------------------------

def _patch(client, ref, raw=False, headers=None, **body):
    body.setdefault("event_uuid", new_uuid())
    r = client.patch(f"/api/tickets/{ref}", json=body, headers=headers or {})
    if raw:
        return r
    assert r.status_code == 200, r.text
    return r.json()


def test_patch_bumps_rev_and_writes_an_edit_event(session_client):
    t = create(session_client, labels=["a"])
    new_content = fake_env(80)
    p = _patch(session_client, t["id"], rev=1, enc_content=new_content, labels=["b", "C"], priority="urgent",
               type="chore", due="2026-12-31", enc_body=fake_env())
    assert p["rev"] == 2 and p["enc_content"] == new_content and p["labels"] == ["b", "c"]
    assert p["priority"] == "urgent" and p["type"] == "chore" and p["due"] == "2026-12-31"
    evs = get(session_client, t["id"])["events"]
    assert evs[-1]["kind"] == "edit" and evs[-1]["status_to"] is None and evs[-1]["enc_body"]
    # absent fields stay; null clears the nullable ones
    p = _patch(session_client, t["id"], rev=2, due=None)
    assert p["due"] is None and p["labels"] == ["b", "c"] and p["rev"] == 3


def test_patch_with_a_stale_rev_is_409(session_client):
    t = create(session_client)
    _patch(session_client, t["id"], rev=1, priority="low")
    r = _patch(session_client, t["id"], raw=True, rev=1, priority="high")
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert get(session_client, t["id"])["priority"] == "low"


def test_patch_parent_and_blockers(session_client):
    a = create(session_client)
    t = create(session_client)
    p = _patch(session_client, t["id"], rev=1, parent=a["id"], blocked_by=[a["id"]])
    assert p["parent"] == a["id"] and p["blocked_by"] == [a["id"]]
    p = _patch(session_client, t["id"], rev=2, parent=None, blocked_by=[])
    assert p["parent"] is None and p["blocked_by"] == []
    r = _patch(session_client, t["id"], raw=True, rev=3, blocked_by=[t["id"]])
    assert r.status_code == 400 and r.json()["error"] == "bad_ref"
    r = _patch(session_client, t["id"], raw=True, rev=3, parent=t["id"])
    assert r.status_code == 400 and r.json()["error"] == "bad_ref"


@pytest.mark.parametrize("body", [{"rev": "1"}, {}, {"rev": 1, "labels": ["bad!"]},
                                  {"rev": 1, "enc_content": "x"}, {"rev": 1, "project": "other"}])
def test_patch_validation(session_client, body):
    t = create(session_client)
    r = _patch(session_client, t["id"], raw=True, **body)
    assert r.status_code == 400, r.text


def test_duplicate_edit_event_uuid(session_client):
    t = create(session_client)
    ev = new_uuid()
    _patch(session_client, t["id"], rev=1, priority="low", event_uuid=ev)
    r = _patch(session_client, t["id"], raw=True, rev=2, priority="high", event_uuid=ev)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_device_patch(device_client, session_client):
    t = create(session_client)
    assert _patch(device_client, t["id"], rev=1, priority="low")["rev"] == 2
    evs = get(session_client, t["id"])["events"]
    assert evs[-1]["actor"]["kind"] == "device"


# --- delete --------------------------------------------------------------------------------------------

def test_delete_makes_a_tombstone(session_client, device_client, settings):
    a = create(session_client, labels=["x"])
    t = create(session_client, labels=["x"], blocked_by=[a["id"]])
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "update", claim=tok)
    assert session_client.delete(f"/api/tickets/{t['id']}").status_code == 204
    r = session_client.get(f"/api/tickets/{t['id']}")
    assert r.status_code == 410 and r.json()["error"] == "deleted" and r.json()["ticket"] == t["id"]
    assert t["id"] not in _ids(session_client.get("/api/tickets"))
    assert session_client.delete(f"/api/tickets/{t['id']}").status_code == 410
    conn = _db(settings)
    try:
        row = conn.execute("SELECT * FROM tickets WHERE n=?", (t["n"],)).fetchone()
        assert row["wrapped_dek"] is None and row["enc_content"] is None and row["deleted_at"]
        assert row["claim_device_id"] is None and row["claim_token_hash"] is None
        assert conn.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_n=? AND enc_body IS NOT NULL",
                            (t["n"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ticket_labels WHERE ticket_n=?", (t["n"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ticket_blocks WHERE ticket_n=?", (t["n"],)).fetchone()[0] == 0
    finally:
        conn.close()
    # a deleted ticket can't be referenced, posted to or claimed
    assert create(session_client, raw=True, blocked_by=[t["id"]]).status_code == 400
    assert post_event(session_client, t["id"], "update", raw=True).status_code == 410
    assert claim(device_client, t["id"], raw=True).status_code == 410


# --- events and files ------------------------------------------------------------------------------------

def test_event_out_shape_and_files(session_client):
    t = create(session_client)
    f = upload(session_client).json()
    ev = post_event(session_client, t["id"], "update", files=[f["id"]])
    assert set(ev) == EVENT_OUT_KEYS
    assert ev["files"] == [f["id"]] and ev["ticket"] == t["id"]
    assert get(session_client, t["id"])["files"] == [f["id"]]


@pytest.mark.parametrize("files,code", [(["FILE999"], "bad_ref"), (["zzz"], "bad_ref"), ("FILE1", "bad_request"),
                                        ([f"FILE{i}" for i in range(11)], "bad_request")])
def test_event_bad_files(session_client, files, code):
    t = create(session_client)
    r = post_event(session_client, t["id"], "update", files=files, raw=True)
    assert r.status_code == 400 and r.json()["error"] == code


@pytest.mark.parametrize("kind", ["created", "claim", "edit", "release", "nope"])
def test_event_kinds_not_postable(session_client, kind):
    t = create(session_client)
    r = post_event(session_client, t["id"], kind, raw=True)
    assert r.status_code == 400


def test_event_body_is_checked(session_client):
    t = create(session_client)
    assert post_event(session_client, t["id"], "update", enc_body="x", raw=True).status_code == 400
    assert post_event(session_client, t["id"], "update", enc_body=OMIT, raw=True).status_code == 400
    assert post_event(session_client, t["id"], "update", enc_body=fake_env(196608), raw=True).status_code == 400
    assert post_event(session_client, t["id"], "update", uuid="nothex", raw=True).status_code == 400


def test_create_links_files_on_the_created_event(session_client):
    """Speak a ticket (spec T13): the voice note's FILE rides on the `created` event."""
    f = upload(session_client).json()
    t = create(session_client, files=[f["id"], f["id"]])
    assert t["files"] == [f["id"]]
    (created,) = [e for e in get(session_client, t["id"])["events"] if e["kind"] == "created"]
    assert created["files"] == [f["id"]]


def test_a_device_create_links_files_too(device_client, session_client):
    f = upload(session_client).json()
    t = create(device_client, files=[f["id"]])
    assert t["files"] == [f["id"]]
    (created,) = [e for e in get(session_client, t["id"])["events"] if e["kind"] == "created"]
    assert created["files"] == [f["id"]] and created["actor"]["kind"] == "device"


def test_create_without_files_links_nothing(session_client):
    t = create(session_client)
    assert t["files"] == []
    assert get(session_client, t["id"])["events"][0]["files"] == []


@pytest.mark.parametrize("files,code", [(["FILE999"], "bad_ref"), (["zzz"], "bad_ref"), ("FILE1", "bad_request"),
                                        ([f"FILE{i}" for i in range(11)], "bad_request")])
def test_create_bad_files(session_client, files, code):
    r = create(session_client, files=files, raw=True)
    assert r.status_code == 400 and r.json()["error"] == code
    assert session_client.get("/api/tickets").json()["tickets"] == []


def test_event_files_persist(session_client):
    t = create(session_client)
    f1, f2 = upload(session_client).json(), upload(session_client).json()
    ev = post_event(session_client, t["id"], "update", files=[f2["id"], f1["id"], f2["id"]])
    assert ev["files"] == [f2["id"], f1["id"]]
    evs = {e["uuid"]: e for e in get(session_client, t["id"])["events"]}
    assert evs[ev["uuid"]]["files"] == [f2["id"], f1["id"]]
    assert all(e["files"] == [] for u, e in evs.items() if u != ev["uuid"])


def test_delete_writes_an_edit_event_and_bumps_the_bus(app, session_client, settings):
    from fileshare import tickets
    t = create(session_client)
    before = get(session_client, t["id"])["last_seq"]
    assert session_client.delete(f"/api/tickets/{t['id']}").status_code == 204
    conn = _db(settings)
    try:
        evs = conn.execute("SELECT * FROM ticket_events WHERE ticket_n=? ORDER BY seq", (t["n"],)).fetchall()
        last = evs[-1]
        assert last["seq"] > before and last["kind"] == "edit"
        assert last["actor_kind"] == "web" and last["enc_body"] is None
        assert app.state.ticket_bus.last_seq == last["seq"]
        # what /changes (Task 3) will emit for a tombstone
        out = tickets.ticket_out(conn, conn.execute("SELECT * FROM tickets WHERE n=?", (t["n"],)).fetchone())
        assert out["deleted_at"] and out["last_seq"] == last["seq"]
        assert out["wrapped_dek"] is None and out["enc_content"] is None and out["claim"] is None
        assert tickets.event_out(conn, last)["ticket"] == t["id"]
    finally:
        conn.close()
