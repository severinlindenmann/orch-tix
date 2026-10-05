"""R0 SPIKE, throwaway. Mailbox prototype bolted onto the real TIX app (same DB layer, single worker).

Run: uvicorn --factory spikes.remote_bridge.server:create --port N   (FS_DATA_DIR must be a temp dir)
No auth on purpose: the spike measures transport and commit cost, not access control.
Bodies are opaque bytes (the client seals them). Wake modes, switchable at runtime:
  step  : the existing TIX pattern, a global counter polled every 0.5 s
  event : a per-mailbox asyncio.Event set on insert
"""
import asyncio
import os
import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from fileshare import db
from fileshare.app import create_app
from fileshare.settings import Settings

STEP_S = 0.5
router = APIRouter(prefix="/spike")

DDL = """
CREATE TABLE IF NOT EXISTS bridge_msgs (
  n INTEGER PRIMARY KEY AUTOINCREMENT, mb TEXT NOT NULL, dir TEXT NOT NULL, rid TEXT NOT NULL,
  idx INTEGER NOT NULL, last INTEGER NOT NULL, body BLOB NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS bridge_msgs_q ON bridge_msgs(mb, dir, rid, n);
CREATE TABLE IF NOT EXISTS bridge_presence (mb TEXT PRIMARY KEY, at REAL NOT NULL, info BLOB);
"""

S = {"mode": "event", "counter": 0, "events": defaultdict(asyncio.Event), "commit_ms": [], "lock_errors": 0,
     "path": None}


def _conn():
    return db.connect(S["path"])


def _insert(mb, d, rid, idx, last, body):
    t0 = time.perf_counter()
    c = _conn()
    try:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO bridge_msgs(mb,dir,rid,idx,last,body,at) VALUES (?,?,?,?,?,?,?)",
                      (mb, d, rid, idx, last, body, time.time()))
            c.execute("COMMIT")
        except Exception as e:  # sqlite3.OperationalError: database is locked
            S["lock_errors"] += 1
            raise
    finally:
        c.close()
    S["commit_ms"].append((time.perf_counter() - t0) * 1000)


def _take(mb, d, rid=None, limit=64):
    """Deliver-and-delete (envelopes are deleted after delivery)."""
    c = _conn()
    try:
        q, a = "SELECT n,rid,idx,last,body FROM bridge_msgs WHERE mb=? AND dir=?", [mb, d]
        if rid is not None:
            q += " AND rid=?"
            a.append(rid)
        rows = c.execute(q + " ORDER BY n LIMIT ?", (*a, limit)).fetchall()
        if rows:
            t0 = time.perf_counter()
            c.execute("BEGIN IMMEDIATE")
            c.execute(f"DELETE FROM bridge_msgs WHERE n IN ({','.join('?' * len(rows))})", [r['n'] for r in rows])
            c.execute("COMMIT")
            S["commit_ms"].append((time.perf_counter() - t0) * 1000)
        return [(r["rid"], r["idx"], r["last"], bytes(r["body"])) for r in rows]
    finally:
        c.close()


def _wake(mb):
    S["counter"] += 1
    if S["mode"] == "event":
        for k in (f"{mb}|req", f"{mb}|resp"):
            if k in S["events"]:
                S["events"][k].set()


def _pack(items):
    # simple framing: for each item: 4B rid-len, rid, 4B idx, 1B last, 4B body-len, body
    out = bytearray()
    for rid, idx, last, body in items:
        r = rid.encode()
        out += len(r).to_bytes(4, "big") + r + idx.to_bytes(4, "big") + bytes([last]) + len(body).to_bytes(4, "big") + body
    return bytes(out)


async def _poll(request, mb, d, rid, wait):
    key = f"{mb}|{'req' if d == 'req' else 'resp'}"
    S["events"][key]  # create
    # event mode: clear BEFORE the first fetch so an insert between fetch and wait is not lost (set after clear)
    if S["mode"] == "event":
        S["events"][key].clear()
    seen = S["counter"]
    items = await run_in_threadpool(_take, mb, d, rid)
    if items:
        return Response(_pack(items), media_type="application/octet-stream")
    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or await request.is_disconnected():
            return Response(status_code=204)
        if S["mode"] == "event":
            try:
                await asyncio.wait_for(S["events"][key].wait(), min(remaining, 5))
            except asyncio.TimeoutError:
                continue
            S["events"][key].clear()
        else:
            await asyncio.sleep(min(STEP_S, remaining))
            if S["counter"] <= seen:
                continue
            seen = S["counter"]
        items = await run_in_threadpool(_take, mb, d, rid)
        if items:
            return Response(_pack(items), media_type="application/octet-stream")


@router.post("/mode/{mode}")
async def set_mode(mode: str):
    assert mode in ("step", "event")
    S["mode"] = mode
    return {"mode": mode}


@router.post("/stats/reset")
async def reset():
    S["commit_ms"].clear()
    S["lock_errors"] = 0
    return {}


@router.get("/stats")
async def stats():
    xs = sorted(S["commit_ms"])
    pct = lambda p: xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None
    return {"n": len(xs), "p50": pct(.5), "p95": pct(.95), "p99": pct(.99), "max": xs[-1] if xs else None,
            "lock_errors": S["lock_errors"]}


@router.post("/mb/{mb}/req/{rid}")
async def post_req(mb: str, rid: str, request: Request):
    await run_in_threadpool(_insert, mb, "req", rid, 0, 1, await request.body())
    _wake(mb)
    return {"ok": True}


@router.get("/mb/{mb}/req")
async def get_req(mb: str, request: Request, wait: float = 25):
    return await _poll(request, mb, "req", None, wait)


@router.post("/mb/{mb}/resp/{rid}")
async def post_resp(mb: str, rid: str, request: Request, idx: int = 0, last: int = 0):
    await run_in_threadpool(_insert, mb, "resp", rid, idx, last, await request.body())
    _wake(mb)
    return {"ok": True}


@router.get("/mb/{mb}/resp/{rid}")
async def get_resp(mb: str, rid: str, request: Request, wait: float = 25):
    return await _poll(request, mb, "resp", rid, wait)


def _presence(mb, info):
    t0 = time.perf_counter()
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO bridge_presence(mb,at,info) VALUES (?,?,?) ON CONFLICT(mb) DO UPDATE SET at=excluded.at, info=excluded.info",
                  (mb, time.time(), info))
        c.execute("COMMIT")
    except Exception:
        S["lock_errors"] += 1
        raise
    finally:
        c.close()
    S["commit_ms"].append((time.perf_counter() - t0) * 1000)


@router.post("/mb/{mb}/presence")
async def presence(mb: str, request: Request):
    await run_in_threadpool(_presence, mb, await request.body())
    return {"ok": True}


def create():
    data = Path(os.environ["FS_DATA_DIR"])
    s = Settings(data_dir=data, public_url="http://127.0.0.1", cookie_secure=False)
    app = create_app(s)
    S["path"] = s.db_path
    c = db.connect(S["path"])
    c.executescript(DDL)
    c.close()
    # before the page catch-alls
    n = len(app.router.routes)
    app.include_router(router)
    new = app.router.routes[n:]
    del app.router.routes[n:]
    app.router.routes[0:0] = new
    return app
