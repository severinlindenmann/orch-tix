"""Tickets HTTP API (spec T6). Request shapes are checked here; fileshare.tickets does the rest."""
import re
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from fileshare import tickets
from fileshare.db import connect
from fileshare.deps import Principal, api_error, get_db, require_any, require_device, require_session
from fileshare.ids import parse_ref, parse_ticket_ref
from fileshare.routes.files import MIN_ENC_META_LEN, UUID_RE, WRAPPED_DEK_LEN, check_envelope, read_bounded_json
from fileshare.tags import BadTag, normalize_tags

router = APIRouter()

MAX_ENC_B64 = 262144            # enc_content and enc_body (spec T4)
MIN_ENC_LEN = MIN_ENC_META_LEN  # version byte, nonce, tag
MAX_WRAPPED_B64 = 4096
BODY_SLACK = 16384              # JSON framing, refs, labels around the envelopes
MAX_BLOCKERS = 20
MAX_EVENT_FILES = 10
MAX_QUESTIONS = 20
MAX_LIMIT = 500
PROJECT_RE = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
DUE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
PATCH_KEYS = {"rev", "event_uuid", "enc_body", *tickets.EDITABLE}


def _bad(why: str, code: str = "bad_request"):
    return api_error(400, code, why)


def _uuid(v, field: str) -> str:
    if not isinstance(v, str) or not UUID_RE.fullmatch(v):
        raise _bad(f"{field} must be 32 lowercase hex characters")
    return v


def _enc(v, field: str) -> str:
    check_envelope(v, field, MAX_ENC_B64, lambda b: len(b) >= MIN_ENC_LEN, code="bad_request")
    return v


def _choice(v, field: str, allowed) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise _bad(f"{field} must be one of " + ", ".join(allowed))
    return v


def _project(v) -> str:
    if not isinstance(v, str) or not PROJECT_RE.fullmatch(v):
        raise _bad("project must match ^[A-Za-z0-9._-]{1,64}$")
    return v


def _labels(v) -> list[str]:
    try:
        return normalize_tags(v)
    except BadTag as e:
        raise _bad(str(e).replace("per file", "per ticket"))


def _due(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str) and DUE_RE.fullmatch(v):
        try:
            date.fromisoformat(v)
            return v
        except ValueError:
            pass
    raise _bad("due must be a YYYY-MM-DD date or null")


def _ref(v) -> int:
    n = parse_ticket_ref(v) if isinstance(v, str) else None
    if n is None:
        raise _bad(f"not a ticket reference: {v!r}", "bad_ref")
    return n


def _parent(v) -> int | None:
    return None if v is None else _ref(v)


def _blockers(v) -> list[int]:
    if not isinstance(v, list):
        raise _bad("blocked_by must be a list of ticket references")
    ns = list(dict.fromkeys(_ref(x) for x in v))
    if len(ns) > MAX_BLOCKERS:
        raise _bad(f"at most {MAX_BLOCKERS} blockers")
    return ns


def _files(v) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_EVENT_FILES:
        raise _bad(f"files must be a list of at most {MAX_EVENT_FILES} FILE ids")
    for f in v:
        if not isinstance(f, str) or parse_ref(f) is None:
            raise _bad(f"not a file reference: {f!r}", "bad_ref")
    return list(dict.fromkeys(v))


def _writable(request: Request) -> None:
    """The legacy freeze (FS_LEGACY_TICKETS=readonly). A route dependency, so it answers before auth."""
    if request.app.state.settings.legacy_tickets == "readonly":
        raise api_error(410, "legacy_readonly", "Tickets now live in orch-core workspaces; TIX shows them as mirrors. "
                                                "This legacy ticket is read-only.")


WRITABLE = [Depends(_writable)]


def _live(conn: sqlite3.Connection, ref: str):
    """The ticket for a legacy write: 404/410 as load_ticket, and 410 legacy_readonly once it was migrated
    (archived), whatever the freeze flag says."""
    row = tickets.load_ticket(conn, ref)
    if row["archived_at"] is not None:
        raise api_error(410, "legacy_readonly", f"{tickets.format_ticket_id(row['n'])} was migrated to an "
                                                "orch-core workspace and is read-only")
    return row


def _int_param(raw: str, lo: int, hi: int, name: str) -> int:
    # ASCII digits only ("²".isdigit() is true), and short enough that int() can't blow up
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 19) or not lo <= int(raw) <= hi:
        raise _bad(f"{name} must be an integer in [{lo}, {hi}]")
    return int(raw)


# --- collection -------------------------------------------------------------------------------------

@router.get("/api/tickets")
def list_tickets(status: list[str] = Query(default=[]), project: str | None = None,
                 label: list[str] = Query(default=[]), limit: str = str(MAX_LIMIT),
                 _: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    statuses = [_choice(s, "status", tickets.STATUSES) for s in dict.fromkeys(status)]
    proj = None if project is None else _project(project)
    labels = _labels(label)
    lim = _int_param(limit, 1, MAX_LIMIT, "limit")
    return {"tickets": tickets.list_tickets(conn, statuses=statuses, project=proj, labels=labels, limit=lim)}


@router.post("/api/tickets", status_code=201, dependencies=WRITABLE)
async def create_ticket(request: Request, principal: Principal = Depends(require_any),
                        conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_ENC_B64 + BODY_SLACK)
    kv = body.get("key_version")
    if type(kv) is not int or not 1 <= kv <= 255:
        raise _bad("key_version must be an integer in [1, 255]")
    check_envelope(body.get("wrapped_dek"), "wrapped_dek", MAX_WRAPPED_B64,
                   lambda b: len(b) == WRAPPED_DEK_LEN, code="bad_request")
    fields = dict(
        uuid=_uuid(body.get("uuid"), "uuid"),
        event_uuid=_uuid(body.get("event_uuid"), "event_uuid"),
        key_version=kv,
        wrapped_dek=body["wrapped_dek"],
        enc_content=_enc(body.get("enc_content"), "enc_content"),
        status=_choice(body.get("status"), "status", tickets.STATUSES),
        type=_choice(body.get("type", "feature"), "type", tickets.TYPES),
        priority=_choice(body.get("priority", "normal"), "priority", tickets.PRIORITIES),
        labels=_labels(body.get("labels", [])),
        due=_due(body.get("due")),
        parent=_parent(body.get("parent")),
        blocked_by=_blockers(body.get("blocked_by", [])),
        files=_files(body.get("files")),          # linked on the `created` event (spec T13, a spoken ticket)
    )
    actor = tickets.principal_actor(conn, principal)
    if principal.kind == "device":
        fields["project"] = principal.device["project"]     # a device files tickets for its own project
        if not request.app.state.ticket_create_limiter.allow(principal.device["id"]):
            raise api_error(429, "rate_limited", "at most 60 new tickets per hour per device")
    else:
        fields["project"] = _project(body.get("project"))
    return tickets.create_ticket(conn, request.app, actor=actor, **fields)


# --- long-polls (spec T6) ------------------------------------------------------------------------------
# /attention and /changes sit above every /{ref} route, so they are never read as a ticket ref.
# The long-polls are `async def` without Depends(get_db): every DB touch opens and closes its own short
# connection in the threadpool, and none is held while the loop sleeps.

MAX_SEQ = 2**63 - 1


def _short(request: Request, fn):
    """fn(conn) on a fresh connection, closed right after. Call it through run_in_threadpool."""
    conn = connect(request.app.state.settings.db_path)
    try:
        return fn(conn)
    finally:
        conn.close()


def _poll_params(after: str, wait: str) -> tuple[int, int]:
    return _int_param(after, 0, MAX_SEQ, "after"), _int_param(wait, 0, tickets.MAX_WAIT_S, "wait")


async def _authenticate(request: Request, dependency) -> Principal:
    return await run_in_threadpool(_short, request, lambda conn: dependency(request, conn))


@router.get("/api/tickets/attention")
def attention(_: Principal = Depends(require_session), conn: sqlite3.Connection = Depends(get_db)):
    return {"count": tickets.attention_count(conn)}


@router.get("/api/tickets/changes")
async def board_changes(request: Request, after: str = "0", wait: str = "0"):
    after_n, wait_s = _poll_params(after, wait)
    await _authenticate(request, require_session)
    cursor = {"seq": after_n}

    def fetch() -> list[dict]:
        changed, cursor["seq"] = _short(request, lambda conn: tickets.changes_after(conn, after_n))
        return changed

    changed = await tickets.wait_for_events(request, request.app.state.ticket_bus, after_n, wait_s, fetch)
    return {"tickets": changed, "cursor": cursor["seq"] if changed else after_n}


@router.get("/api/tickets/{ref}/events")
async def ticket_events(ref: str, request: Request, after: str = "0", wait: str = "0"):
    after_n, wait_s = _poll_params(after, wait)
    principal = await _authenticate(request, require_any)
    with tickets.poll_slot(principal.device["id"] if principal.kind == "device" else None):   # 429

        def start(conn) -> int:
            row = tickets.load_ticket(conn, ref)                          # 404 / 410
            if tickets.actor_for(request, principal, conn, row).holder:   # 409 claim_lost; seen_at
                tickets.mark_polling(conn, row)                           # poll_at: "listening"
            return row["n"]

        n = await run_in_threadpool(_short, request, start)
        events = await tickets.wait_for_events(
            request, request.app.state.ticket_bus, after_n, wait_s,
            lambda: _short(request, lambda conn: tickets.events_after(conn, n, after_n)))
        ticket = await run_in_threadpool(_short, request, lambda conn: tickets.ticket_out_n(conn, n))
    return {"events": events, "ticket": ticket, "cursor": events[-1]["seq"] if events else after_n}


# --- one ticket --------------------------------------------------------------------------------------

@router.get("/api/tickets/{ref}")
def get_ticket(ref: str, request: Request, principal: Principal = Depends(require_any),
               conn: sqlite3.Connection = Depends(get_db)):
    row = tickets.load_ticket(conn, ref)
    tickets.actor_for(request, principal, conn, row)      # validates X-Claim-Token, refreshes seen_at
    row = tickets.load_ticket(conn, ref)
    return {**tickets.ticket_out(conn, row), "events": tickets.events_of(conn, row["n"])}


@router.patch("/api/tickets/{ref}", dependencies=WRITABLE)
async def patch_ticket(ref: str, request: Request, principal: Principal = Depends(require_any),
                       conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, 2 * MAX_ENC_B64 + BODY_SLACK)
    row = _live(conn, ref)
    unknown = sorted(set(body) - PATCH_KEYS)
    if unknown:
        raise _bad("these fields can't be changed here: " + ", ".join(unknown))
    rev = body.get("rev")
    if type(rev) is not int or rev < 1:
        raise _bad("rev must be a positive integer")
    event_uuid = _uuid(body.get("event_uuid"), "event_uuid")
    enc_body = None if body.get("enc_body") is None else _enc(body["enc_body"], "enc_body")
    parse = {"enc_content": lambda v: _enc(v, "enc_content"),
             "type": lambda v: _choice(v, "type", tickets.TYPES),
             "priority": lambda v: _choice(v, "priority", tickets.PRIORITIES),
             "labels": _labels, "due": _due, "parent": _parent, "blocked_by": _blockers}
    changes = {k: parse[k](body[k]) for k in tickets.EDITABLE if k in body}
    tickets.check_new_event_uuid(conn, event_uuid)
    actor = tickets.actor_for(request, principal, conn, row)
    return tickets.edit_ticket(conn, request.app, row, actor=actor, rev=rev, changes=changes,
                               event_uuid=event_uuid, enc_body=enc_body)


@router.delete("/api/tickets/{ref}", status_code=204, dependencies=WRITABLE)
def delete_ticket(ref: str, request: Request, principal: Principal = Depends(require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    tickets.delete_ticket(conn, request.app, _live(conn, ref),
                          actor=tickets.principal_actor(conn, principal))
    return Response(status_code=204)


@router.post("/api/tickets/{ref}/events", status_code=201, dependencies=WRITABLE)
async def post_event(ref: str, request: Request, principal: Principal = Depends(require_any),
                     conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_ENC_B64 + BODY_SLACK)
    row = _live(conn, ref)
    uuid = _uuid(body.get("uuid"), "uuid")
    kind = body.get("kind")
    if kind not in tickets.POSTABLE_KINDS:
        raise _bad("kind must be one of " + ", ".join(tickets.POSTABLE_KINDS))
    raw_body = body.get("enc_body")
    if raw_body is None and kind != "status":
        raise _bad(f"{kind} events need enc_body")
    enc_body = None if raw_body is None else _enc(raw_body, "enc_body")
    status_to = body.get("status_to")
    if status_to is not None:
        _choice(status_to, "status_to", tickets.STATUSES)
    files = _files(body.get("files"))
    flags: dict = {}
    question_count = None
    if kind == "question":
        question_count = body.get("question_count")
        if type(question_count) is not int or not 1 <= question_count <= MAX_QUESTIONS:
            raise _bad(f"question events carry question_count in [1, {MAX_QUESTIONS}]", "bad_question")
    elif kind == "test":
        for flag in ("passed", "manual"):
            if type(body.get(flag)) is not bool:
                raise _bad(f"test events carry the cleartext {flag} flag (true or false)")
            flags[flag] = body[flag]
    elif kind == "verdict":
        flags["verdict"] = _choice(body.get("verdict"), "verdict", tickets.VERDICTS)
    tickets.check_new_event_uuid(conn, uuid)
    actor = tickets.actor_for(request, principal, conn, row)
    new_status = tickets.check_move(actor, row, kind, status_to, flags)
    return tickets.insert_event(conn, request.app, row, uuid=uuid, kind=kind, actor=actor, status_to=new_status,
                                enc_body=enc_body, files=files, question_count=question_count,
                                manual=kind == "test" and flags["manual"])


@router.post("/api/tickets/{ref}/archive")
async def archive_ticket(ref: str, request: Request, principal: Principal = Depends(require_any),
                         conn: sqlite3.Connection = Depends(get_db)):
    """Task 12: the ticket moved to an orch-core workspace. Not WRITABLE-gated: it is how the freeze is reached.
    The browser, or a device of the ticket's own project (the one that ran `sharing tickets migrate`)."""
    body = await read_bounded_json(request, MAX_ENC_B64 + BODY_SLACK)
    row = tickets.load_ticket(conn, ref)
    uuid = _uuid(body.get("uuid"), "uuid")
    enc_body = _enc(body.get("enc_body"), "enc_body")
    actor = tickets.principal_actor(conn, principal)
    if principal.kind == "device" and principal.device["project"] != row["project"]:
        raise api_error(403, "forbidden", "only a device of the ticket's own project (or the browser) archives it")
    if row["archived_at"] is None:
        tickets.check_new_event_uuid(conn, uuid)
    return tickets.archive_ticket(conn, request.app, row, actor=actor, uuid=uuid, enc_body=enc_body)


@router.post("/api/tickets/{ref}/claim", dependencies=WRITABLE)
async def claim_ticket(ref: str, request: Request, principal: Principal = Depends(require_device),
                       conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, 4096)
    row = _live(conn, ref)
    event_uuid = _uuid(body.get("event_uuid"), "event_uuid")
    takeover = body.get("takeover", False)
    if type(takeover) is not bool:
        raise _bad("takeover must be true or false")
    tickets.check_new_event_uuid(conn, event_uuid)
    token, ticket, event = tickets.claim_ticket(conn, request.app, row, actor=tickets.principal_actor(conn, principal),
                                                takeover=takeover, event_uuid=event_uuid)
    return {"claim_token": token, "ticket": ticket, "event": event}


@router.delete("/api/tickets/{ref}/claim", status_code=204, dependencies=WRITABLE)
def release_claim(ref: str, request: Request, principal: Principal = Depends(require_any),
                  conn: sqlite3.Connection = Depends(get_db)):
    row = _live(conn, ref)
    actor = tickets.actor_for(request, principal, conn, row)
    if actor.kind == "device" and not actor.holder:
        raise api_error(403, "forbidden", "only the claim holder (X-Claim-Token) or the browser releases a claim")
    tickets.release_claim(conn, request.app, row, actor=actor)
    return Response(status_code=204)
