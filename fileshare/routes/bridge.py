"""The bridge mailbox HTTP API (Remote, R8). Bodies are sealed by the sender and opaque here: the server
checks their size and that they are base64url text, and passes them on unread.

Client side (a browser session or an approved device):
  POST   /api/bridge/{space}/requests               {id, body, tab?, stream?}  -> 201 {id, expires_in, host_online}
  GET    /api/bridge/{space}/responses?wait=&tab=   -> {chunks: [{id, idx, last, stream, body}]}  (one multiplexed poll)
  DELETE /api/bridge/{space}/requests/{id}          -> 204 (give up: frees the quota and drops what is buffered)
Host side (the approved device that owns the space; a holder id for the host process is required):
  GET    /api/bridge/{space}/requests?holder=&wait=&take_over=  -> {requests: [{id, body}], lease_s}
  POST   /api/bridge/{space}/responses/{id}?holder= {idx, last, body}          -> 204
  DELETE /api/bridge/{space}/host?holder=           -> 204 (release the lease)
"""
import asyncio
import re
import time

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from fileshare import bridge as br
from fileshare import mirrors
from fileshare.deps import Principal, api_error, require_any
from fileshare.routes.files import UUID_RE, read_bounded_json
from fileshare.routes.tickets import _authenticate, _int_param, _short

router = APIRouter()

_B64U = re.compile(r"[A-Za-z0-9_-]+")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,64}")      # tab and holder ids: chosen by the client, never interpreted


def _space(v: str) -> str:
    if not UUID_RE.fullmatch(v):
        raise api_error(400, "bad_request", "space must be 32 lowercase hex characters")
    return v


def _rid(v) -> str:
    if not isinstance(v, str) or not UUID_RE.fullmatch(v):
        raise api_error(400, "bad_request", "id must be 32 lowercase hex characters")
    return v


def _opaque(v, name: str, required: bool = False) -> str:
    if not v and not required:
        return ""
    if not isinstance(v, str) or not _OPAQUE.fullmatch(v):
        raise api_error(400, "bad_request", f"{name} must be 1-64 characters of A-Z a-z 0-9 _ -")
    return v


def _sealed(v, max_bytes: int) -> str:
    """Shape only: base64url text no longer than max_bytes decoded. Nothing is decoded."""
    if not isinstance(v, str) or not _B64U.fullmatch(v):
        raise api_error(400, "bad_request", "body must be base64url text")
    if len(v) > br.b64_len(max_bytes):
        raise api_error(413, "too_large", f"body is limited to {max_bytes} bytes")
    return v


def _wait(raw: str) -> int:
    return _int_param(raw, 0, br.MAX_WAIT_S, "wait")


def _client_key(p: Principal) -> str:
    return "d:" + p.device["id"] if p.kind == "device" else "s:" + p.session_hash


def _bridge(request: Request) -> br.Bridge:
    return request.app.state.bridge


async def _client(request: Request, space: str) -> tuple[Principal, str]:
    """A browser session or an approved device may use any existing space as a client.

    This is safe only because an instance has a single owner: every session and device is that owner's, and
    the host re-checks each sealed request. If an instance ever gets a second account, this needs an
    ownership or membership check on the space."""
    def check(conn):
        p = require_any(request, conn)
        mirrors.space_row(conn, space)                 # 404 no_space
        return p
    p = await run_in_threadpool(_short, request, check)
    return p, _client_key(p)


async def _host(request: Request, space: str) -> Principal:
    """Only the approved device that owns the space is its host; a browser session never is."""
    def check(conn):
        p = require_any(request, conn)
        if p.kind != "device":
            raise api_error(403, "forbidden", "only the workspace's device hosts a mailbox")
        mirrors.owner_space(conn, space, p.device)     # 404 no_space, 403 not_owner
        return p
    return await run_in_threadpool(_short, request, check)


async def _wait_for(request: Request, wake: br.Wake, wait: int, step):
    """Run step() until it returns something non-empty, the deadline passes or the client leaves. The wake
    waiter is armed before each step, so a post that lands during it resolves the next wait at once."""
    deadline = time.monotonic() + wait
    while True:
        fut = wake.arm()
        try:
            out = await step()
            if out:
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await request.is_disconnected():
                return None
            try:
                await asyncio.wait_for(fut, min(remaining, br.WAKE_SLICE_S))
            except asyncio.TimeoutError:
                pass
        finally:
            wake.drop(fut)


# --- client ------------------------------------------------------------------------------------

@router.post("/api/bridge/{space}/requests", status_code=201)
async def post_request(space: str, request: Request):
    space = _space(space)
    _, client = await _client(request, space)
    bridge, now = _bridge(request), br.now_ts()
    bridge.prune(now)
    bridge.admit(space, client)                      # rate and quota before the body (up to ~1.4 MB) is read
    body = await read_bounded_json(request, br.b64_len(br.MAX_REQUEST_BYTES) + 1024)
    rid, sealed = _rid(body.get("id")), _sealed(body.get("body"), br.MAX_REQUEST_BYTES)
    tab, stream = _opaque(body.get("tab"), "tab"), body.get("stream", False)
    if type(stream) is not bool:
        raise api_error(400, "bad_request", "stream must be true or false")
    bridge.reserve(space, rid, client, tab, stream, now)
    try:
        await run_in_threadpool(_short, request, lambda conn: br.insert_request(conn, space, rid, sealed, now))
    except BaseException:
        bridge.routes.pop((space, rid), None)
        raise
    bridge.box(space).req_wake.fire()
    return {"id": rid, "expires_in": br.TTL_S, "host_online": bridge.host_online(space, now)}


@router.get("/api/bridge/{space}/responses")
async def poll_responses(space: str, request: Request, wait: str = "0", tab: str | None = None):
    space, wait_s, tab = _space(space), _wait(wait), _opaque(tab, "tab")
    _, client = await _client(request, space)
    bridge = _bridge(request)
    with bridge.poll_slot(client, br.CLIENT_POLLS, space):
        async def step():
            now = br.now_ts()
            bridge.prune(now)
            frames = bridge.take_frames(space, client, tab, now)
            rids = bridge.open_page_rids(space, client, tab)
            rows = await run_in_threadpool(_short, request, lambda conn: br.take_pages(conn, space, rids, now))
            if not frames and not rows:
                return None
            await _authenticate(request, require_any)           # revoked while waiting: nothing is handed over
            chunks = [{"id": r["rid"], "idx": r["idx"], "last": bool(r["last"]), "stream": False, "body": r["body"]}
                      for r in rows]
            for r in rows:
                if r["last"]:
                    bridge.routes.pop((space, r["rid"]), None)
            chunks += [{"id": f.rid, "idx": f.idx, "last": f.last, "stream": True, "body": f.body} for f in frames]
            return chunks

        chunks = await _wait_for(request, bridge.box(space).resp_wake, wait_s, step)
    return {"chunks": chunks or []}


@router.delete("/api/bridge/{space}/requests/{rid}", status_code=204)
async def cancel_request(space: str, rid: str, request: Request):
    space, rid = _space(space), _rid(rid)
    _, client = await _client(request, space)
    bridge = _bridge(request)
    bridge.own_route(space, rid, client)
    bridge.drop_route(space, rid)
    await run_in_threadpool(_short, request, lambda conn: br.delete_rid(conn, space, rid))
    return Response(status_code=204)


# --- host --------------------------------------------------------------------------------------

@router.get("/api/bridge/{space}/requests")
async def poll_requests(space: str, request: Request, holder: str = "", wait: str = "0", take_over: str = "0"):
    space, wait_s = _space(space), _wait(wait)
    holder = _opaque(holder, "holder", required=True)
    if take_over not in ("0", "1"):
        raise api_error(400, "bad_request", "take_over must be 0 or 1")
    principal = await _host(request, space)
    device_id, bridge = principal.device["id"], _bridge(request)
    with bridge.poll_slot("h:" + device_id, br.HOST_POLLS, space):
        # Accepted gap: _host() below checks owner and approval, then take_requests() dequeues. If ownership moves or
        # the device is revoked in between, this one poll can still take up to HOST_REQUESTS_PER_POLL (16) sealed
        # requests and hand them to the displaced device; the new owner never sees them. They are lost, not
        # disclosed: the bodies stay sealed and the client's request simply expires.
        first = [take_over == "1"]

        async def step():
            await _host(request, space)         # owner and approval first, every slice: a poll that is no longer
            now = br.now_ts()                   # the host fails here, before it touches the lease or the queue
            bridge.prune(now)
            bridge.host_lease(space, device_id, holder, take_over=first[0], now=now)
            first[0] = False                                    # the take-over happens once, not on every slice
            rows = await run_in_threadpool(_short, request, lambda conn: br.take_requests(conn, space, now))
            if not rows:
                return None
            for r in rows:
                route = bridge.routes.get((space, r["rid"]))
                if route:
                    route.queued = False
            return [{"id": r["rid"], "body": r["body"]} for r in rows]

        requests = await _wait_for(request, bridge.box(space).req_wake, wait_s, step)
    return {"requests": requests or [], "lease_s": br.LEASE_S}


@router.post("/api/bridge/{space}/responses/{rid}", status_code=204)
async def post_response(space: str, rid: str, request: Request, holder: str = ""):
    space, rid = _space(space), _rid(rid)
    holder = _opaque(holder, "holder", required=True)
    principal = await _host(request, space)
    body = await read_bounded_json(request, br.b64_len(br.MAX_CHUNK_BYTES) + 1024)
    sealed = _sealed(body.get("body"), br.MAX_CHUNK_BYTES)
    idx, last = body.get("idx"), body.get("last", False)
    if type(idx) is not int or not 0 <= idx < 2**31 or type(last) is not bool:
        raise api_error(400, "bad_request", "idx must be a non-negative integer and last true or false")
    bridge, now = _bridge(request), br.now_ts()
    bridge.prune(now)
    route = bridge.accept_chunk(space, rid, principal.device["id"], holder, now)
    if route.stream:
        bridge.add_frame(space, rid, route, idx, last, sealed, now)        # memory only: no database commit
    else:
        route.closed = last
        try:
            await run_in_threadpool(_short, request, lambda conn: br.insert_page(conn, space, rid, idx, last, sealed, now))
        except BaseException:
            route.closed = False
            raise
        bridge.box(space).resp_wake.fire()
    return Response(status_code=204)


@router.delete("/api/bridge/{space}/host", status_code=204)
async def release_host(space: str, request: Request, holder: str = ""):
    space = _space(space)
    holder = _opaque(holder, "holder", required=True)
    principal = await _host(request, space)
    box = _bridge(request).box(space)
    if box.lease and box.lease[0] == principal.device["id"] and box.lease[1] == holder:
        box.lease = None
    return Response(status_code=204)
