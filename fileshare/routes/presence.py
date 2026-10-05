"""Host presence HTTP API (Remote, R9). Clear status only; see fileshare/presence.py.

Host side (the approved device that owns the space, same binding as the bridge host routes):
  POST /api/presence/{space}/heartbeat  {sessions, in_progress, needs_you, factory[, children_done, children_total,
                                         budget_pct]}  -> 204   (the host sends one every 10 s)
  POST /api/presence/{space}/goodbye    {} -> 204               (a clean stop)
Client side (a browser session or an approved device):
  GET  /api/presence  -> {now, spaces: [{...the /api/spaces row, state, last_seen, goodbye_at, counts...}]}
"""
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from fileshare import presence as pr
from fileshare.bridge import now_ts
from fileshare.deps import api_error, require_any
from fileshare.routes.bridge import _host, _space
from fileshare.routes.files import read_bounded_json
from fileshare.routes.tickets import _short

router = APIRouter()
MAX_BODY = 512


async def _body(request: Request, space: str):
    space = _space(space)
    principal = await _host(request, space)
    if not request.app.state.presence_limiter.allow(principal.device["id"]):
        raise api_error(429, "rate_limited", f"at most {pr.BEATS_PER_MIN} presence calls per minute per device")
    return space, principal, await read_bounded_json(request, MAX_BODY)


@router.post("/api/presence/{space}/heartbeat", status_code=204)
async def heartbeat(space: str, request: Request):
    space, principal, body = await _body(request, space)
    try:
        vals = pr.check_beat(body)
    except ValueError as e:
        raise api_error(400, "bad_request", str(e))
    await run_in_threadpool(_short, request, lambda c: pr.beat(c, space, principal.device["id"], vals, now_ts()))
    return Response(status_code=204)


@router.post("/api/presence/{space}/goodbye", status_code=204)
async def say_goodbye(space: str, request: Request):
    space, _, body = await _body(request, space)
    if body:
        raise api_error(400, "bad_request", "goodbye takes an empty object")
    await run_in_threadpool(_short, request, lambda c: pr.goodbye(c, space, now_ts()))
    return Response(status_code=204)


@router.get("/api/presence")
async def list_presence(request: Request):
    def work(conn):
        require_any(request, conn)
        now = now_ts()
        return {"now": now, "spaces": pr.read(conn, now)}
    return await run_in_threadpool(_short, request, work)
