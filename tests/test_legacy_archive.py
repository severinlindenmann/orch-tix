"""Task 12: legacy tickets are archived (read-only for 90 days) and then tombstoned. The archive route works
while the legacy freeze is on; mirrors are never touched."""
import dataclasses
from datetime import timedelta

from fileshare import admin, clock
from fileshare.db import connect
from fileshare.expiry import expire_legacy_tickets
from tests.helpers.tickets import (create, device_client, fake_dek, fake_env, get, new_uuid,  # noqa: F401
                                   other_device, other_device_client)

SPACE = "a" * 32


def _freeze(app):
    app.state.settings = dataclasses.replace(app.state.settings, legacy_tickets="readonly")


def _archive(c, ref, **kw):
    return c.post(f"/api/tickets/{ref}/archive", json={"uuid": new_uuid(), "enc_body": fake_env(), **kw})


def _mirror(dc):
    assert dc.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()}).status_code == 201
    r = dc.put(f"/api/mirrors/{new_uuid()}", json={
        "space": SPACE, "mirror_rev": 1, "schema_version": "1.0.0", "status": "waiting", "priority": "high",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": fake_dek(), "enc_content": fake_env(200),
        "event_uuid": new_uuid()})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_ticket_out_carries_archived_at(session_client):
    assert create(session_client, project="proj")["archived_at"] is None


def test_the_projects_device_archives_with_an_update_while_frozen(app, session_client, device_client):
    t = create(session_client, project="proj")
    _freeze(app)
    r = _archive(device_client, t["id"])
    assert r.status_code == 200, r.text
    got = get(session_client, t["id"])
    assert got["archived_at"] and got["status"] == t["status"]
    last = got["events"][-1]
    assert last["kind"] == "update" and last["actor"]["kind"] == "device" and last["enc_body"]
    assert device_client.post(f"/api/tickets/{t['id']}/events",
                              json={"uuid": new_uuid(), "kind": "update", "enc_body": fake_env()}).status_code == 410


def test_archiving_again_changes_nothing(session_client, device_client):
    t = create(session_client, project="proj")
    first = _archive(device_client, t["id"]).json()
    n_events = len(get(session_client, t["id"])["events"])
    again = _archive(device_client, t["id"])
    assert again.status_code == 200 and again.json()["archived_at"] == first["archived_at"]
    assert len(get(session_client, t["id"])["events"]) == n_events


def test_another_projects_device_cannot_archive(session_client, other_device_client):
    t = create(session_client, project="proj")
    r = _archive(other_device_client, t["id"])
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert get(session_client, t["id"])["archived_at"] is None


def test_archive_checks_its_body(session_client):
    t = create(session_client, project="proj")
    assert _archive(session_client, t["id"], uuid="nope").status_code == 400
    assert session_client.post(f"/api/tickets/{t['id']}/archive", json={"uuid": new_uuid()}).status_code == 400


def test_admin_archive_legacy_stamps_every_legacy_ticket(settings, session_client, device_client, capsys):
    a = create(session_client, project="proj")
    b = create(session_client, project="other")
    tix = _mirror(device_client)
    assert admin.main(["archive-legacy", "--data-dir", str(settings.data_dir)]) == 0
    assert "2 legacy ticket(s) archived" in capsys.readouterr().out
    assert get(session_client, a["id"])["archived_at"] and get(session_client, b["id"])["archived_at"]
    conn = connect(settings.db_path)
    try:
        assert conn.execute("SELECT archived_at FROM tickets WHERE mode = 'mirror'").fetchone()[0] is None
    finally:
        conn.close()
    assert session_client.get(f"/api/mirrors/{tix}").status_code == 200
    assert admin.main(["archive-legacy", "--data-dir", str(settings.data_dir)]) == 0
    assert "0 legacy ticket(s) archived" in capsys.readouterr().out


def test_expire_tombstones_legacy_tickets_archived_over_90_days(settings, session_client, device_client):
    t = create(session_client, project="proj")
    keep = create(session_client, project="proj")             # never archived: kept
    mirror = _mirror(device_client)
    _archive(device_client, t["id"])
    conn = connect(settings.db_path)
    try:
        assert expire_legacy_tickets(conn, clock.now() + timedelta(days=89)) == 0
        assert expire_legacy_tickets(conn, clock.now() + timedelta(days=91)) == 1
        row = conn.execute("SELECT * FROM tickets WHERE n = ?", (t["n"],)).fetchone()
        assert row["deleted_at"] and row["wrapped_dek"] is None and row["enc_content"] is None
        assert conn.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_n = ? AND enc_body IS NOT NULL",
                            (t["n"],)).fetchone()[0] == 0
        m = conn.execute("SELECT * FROM tickets WHERE mode = 'mirror'").fetchone()
        assert m["deleted_at"] is None and m["enc_content"]
        assert expire_legacy_tickets(conn, clock.now() + timedelta(days=91)) == 0
    finally:
        conn.close()
    assert session_client.get(f"/api/tickets/{t['id']}").status_code == 410
    assert get(session_client, keep["id"])["deleted_at"] is None
    assert session_client.get(f"/api/mirrors/{mirror}").status_code == 200


def test_an_archived_ticket_is_read_only_even_without_the_freeze(session_client, device_client):
    t = create(session_client, project="proj")
    _archive(device_client, t["id"])
    for method, path, body in [("POST", f"/api/tickets/{t['id']}/events",
                                {"uuid": new_uuid(), "kind": "update", "enc_body": fake_env()}),
                               ("PATCH", f"/api/tickets/{t['id']}", {"rev": 1, "event_uuid": new_uuid()}),
                               ("POST", f"/api/tickets/{t['id']}/claim", {"event_uuid": new_uuid()}),
                               ("DELETE", f"/api/tickets/{t['id']}", None)]:
        c = session_client if method == "DELETE" else device_client
        r = c.request(method, path, json=body)
        assert r.status_code == 410 and r.json()["error"] == "legacy_readonly", (method, path, r.text)
    assert get(session_client, t["id"])["status"] == t["status"]


def test_the_tombstone_sweep_wakes_long_polls(settings, session_client, device_client):
    from fileshare.tickets import Bus
    t = create(session_client, project="proj")
    _archive(device_client, t["id"])
    bus = Bus(0)
    conn = connect(settings.db_path)
    try:
        assert expire_legacy_tickets(conn, clock.now() + timedelta(days=91), bus=bus) == 1
        last = conn.execute("SELECT MAX(seq) FROM ticket_events WHERE ticket_n = ?", (t["n"],)).fetchone()[0]
    finally:
        conn.close()
    assert bus.last_seq == last


def test_admin_archive_legacy_also_clears_claims(settings, session_client, device_client):
    """Final review M2: archive-legacy is the last switch-over step; a claim left on a legacy ticket goes."""
    t = create(session_client, project="proj", status="open")
    assert device_client.post(f"/api/tickets/{t['id']}/claim", json={"event_uuid": new_uuid()}).status_code == 200
    assert get(session_client, t["id"])["claim"] is not None
    assert admin.main(["archive-legacy", "--data-dir", str(settings.data_dir)]) == 0
    got = get(session_client, t["id"])
    assert got["claim"] is None and got["archived_at"]
