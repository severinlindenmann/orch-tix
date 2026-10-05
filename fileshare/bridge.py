"""The bridge mailbox (Orch Remote, R8): per-workspace queues that carry sealed bytes between a host and
its clients. Everything stored or held here is ciphertext plus ids, sizes and timing; no function in this
module or its routes decodes, parses or logs a body.

State is in this process (one uvicorn worker, like tickets.Bus): the host lease, the routes (which client
waits for which request id), stream frames and the wake-ups. Only requests awaiting their host and
page-response chunks go to the database, and both are lost on restart on purpose (startup purges them),
because the routes that tell a response where to go are not persisted either."""
import asyncio
import logging
import sqlite3
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from fileshare import clock
from fileshare.deps import api_error
from fileshare.security import WindowLimiter

log = logging.getLogger("fileshare.bridge")

TTL_S = 60                      # requests, page chunks and frames; a stream is kept alive by its frames
LEASE_S = 40                    # a host that has not polled for this long loses the workspace
MAX_WAIT_S = 25
WAKE_SLICE_S = 2.0              # how often a waiting poll re-checks its lease and the connection
MAX_REQUEST_BYTES = 1 << 20     # sealed request, decoded size
MAX_CHUNK_BYTES = 256 * 1024    # sealed response chunk, decoded size
CLIENT_INFLIGHT = 16            # open requests per device or browser session
CLIENT_STREAMS = 4              # of those, how many may be streams
BOX_QUEUED_REQUESTS = 64        # requests waiting for the host in one mailbox
BOX_PAGE_BYTES = 8 << 20        # undelivered page-response text in the database, per mailbox
BOX_FRAME_BYTES = 4 << 20       # undelivered stream frames in memory, per mailbox
CLIENT_POLLS = 6                # concurrent bridge long polls per device or session (not the ticket slots)
HOST_POLLS = 2
HOST_REQUESTS_PER_POLL = 16
CLIENT_CHUNKS_PER_POLL = 32
POSTS_PER_S = 20                # requests per second per device or session
POLLS_PER_S = 10                # polls per second per device or session
CHUNKS_PER_S = 100              # response chunks per second per host device


def b64_len(n_bytes: int) -> int:
    return (n_bytes * 4 + 2) // 3      # unpadded base64url characters for n_bytes


@dataclass
class Route:
    client: str
    tab: str
    stream: bool
    expires: float
    queued: bool = True         # still waiting for the host to fetch it
    closed: bool = False        # the host has posted the last chunk


@dataclass
class Frame:
    rid: str
    idx: int
    last: bool
    body: str
    expires: float


class Wake:
    """A wake-up many pollers can wait on. arm() registers a waiter BEFORE the poller fetches, so a post that
    lands between the fetch and the wait resolves it and nothing is lost; one waiter can never consume
    another's wake-up (an asyncio.Event cleared by one waiter would)."""

    def __init__(self):
        self._futs: set[asyncio.Future] = set()

    def arm(self) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._futs.add(fut)
        return fut

    def drop(self, fut: asyncio.Future) -> None:
        self._futs.discard(fut)

    def fire(self) -> None:
        for fut in self._futs:
            if not fut.done():
                fut.set_result(None)
        self._futs.clear()

    def waiting(self) -> bool:
        return bool(self._futs)


@dataclass
class Box:
    req_wake: Wake = field(default_factory=Wake)
    resp_wake: Wake = field(default_factory=Wake)
    frames: deque = field(default_factory=deque)
    frame_bytes: int = 0
    lease: tuple[str, str, float] | None = None    # (device id, holder, last poll)


class Bridge:
    def __init__(self):
        self.boxes: dict[str, Box] = {}
        self.routes: dict[tuple[str, str], Route] = {}
        self.polls: dict[str, int] = {}
        self.post_rate = WindowLimiter(POSTS_PER_S, 1)
        self.poll_rate = WindowLimiter(POLLS_PER_S, 1)
        self.chunk_rate = WindowLimiter(CHUNKS_PER_S, 1)

    # --- bookkeeping -------------------------------------------------------------------------------

    def box(self, space: str) -> Box:
        return self.boxes.setdefault(space, Box())

    def prune(self, now: float) -> None:
        # ponytail: linear scans; routes are bounded by clients x CLIENT_INFLIGHT. Index by space if that grows.
        for key in [k for k, r in self.routes.items() if r.expires <= now]:
            del self.routes[key]
        for space, box in list(self.boxes.items()):
            if box.frames:
                keep = deque(f for f in box.frames if f.expires > now and (space, f.rid) in self.routes)
                box.frame_bytes = sum(len(f.body) for f in keep)
                box.frames = keep
            if box.lease and now - box.lease[2] >= LEASE_S:
                box.lease = None
            if not (box.frames or box.lease or box.req_wake.waiting() or box.resp_wake.waiting()
                    or any(k[0] == space for k in self.routes)):
                del self.boxes[space]

    def refuse(self, space: str, status: int, code: str, detail: str):
        log.info("bridge %s: refused %s", space, code)
        raise api_error(status, code, detail)

    @contextmanager
    def poll_slot(self, key: str, limit: int, space: str):
        if not self.poll_rate.allow(key):
            self.refuse(space, 429, "rate_limited", f"at most {POLLS_PER_S} bridge polls per second")
        if self.polls.get(key, 0) >= limit:
            self.refuse(space, 429, "too_many_polls", f"at most {limit} concurrent bridge polls")
        self.polls[key] = self.polls.get(key, 0) + 1
        try:
            yield
        finally:
            left = self.polls.get(key, 1) - 1
            if left > 0:
                self.polls[key] = left
            else:
                self.polls.pop(key, None)

    # --- the host lease ----------------------------------------------------------------------------

    def host_lease(self, space: str, device_id: str, holder: str, *, take_over: bool, now: float) -> None:
        """Take or refresh the lease (poll). One live holder per workspace; another holder needs take_over."""
        box = self.box(space)
        lease = box.lease
        live = lease is not None and now - lease[2] < LEASE_S and lease[0] == device_id
        if live and lease[1] != holder and not take_over:
            self.refuse(space, 409, "host_taken", "another host is serving this workspace; "
                        "send take_over=1 to replace it")
        if not live or lease[1] != holder:
            log.info("bridge %s: host lease %s by device %s", space,
                     "taken over" if live else "taken", device_id)
        box.lease = (device_id, holder, now)

    def check_lease(self, space: str, device_id: str, holder: str, now: float) -> None:
        lease = self.box(space).lease
        if lease is None or lease[0] != device_id or lease[1] != holder or now - lease[2] >= LEASE_S:
            self.refuse(space, 409, "lease_lost", "this host no longer holds the workspace; poll again to take it")

    def host_online(self, space: str, now: float) -> bool:
        lease = self.boxes[space].lease if space in self.boxes else None
        return lease is not None and now - lease[2] < LEASE_S

    # --- client requests -------------------------------------------------------------------------

    def reserve(self, space: str, rid: str, client: str, tab: str, stream: bool, now: float) -> None:
        """Count the request against every quota and register its route (before the database insert, so
        two parallel posts cannot both slip under a limit)."""
        if not self.post_rate.allow(client):
            self.refuse(space, 429, "rate_limited", f"at most {POSTS_PER_S} bridge requests per second")
        if (space, rid) in self.routes:
            raise api_error(409, "duplicate_id", "this request id is already in use")
        mine = [r for r in self.routes.values() if r.client == client]
        if len(mine) >= CLIENT_INFLIGHT:
            self.refuse(space, 429, "too_many_inflight", f"at most {CLIENT_INFLIGHT} open requests")
        if stream and sum(r.stream for r in mine) >= CLIENT_STREAMS:
            self.refuse(space, 429, "too_many_streams", f"at most {CLIENT_STREAMS} open streams")
        if sum(r.queued for k, r in self.routes.items() if k[0] == space) >= BOX_QUEUED_REQUESTS:
            self.refuse(space, 429, "mailbox_full", "the host is not keeping up; try again shortly")
        self.routes[(space, rid)] = Route(client, tab, stream, now + TTL_S)

    def own_route(self, space: str, rid: str, client: str) -> Route:
        route = self.routes.get((space, rid))
        if route is None or route.client != client:       # another client's id looks like no id at all
            raise api_error(404, "unknown_request", "no such open request")
        return route

    def drop_route(self, space: str, rid: str) -> None:
        self.routes.pop((space, rid), None)
        box = self.boxes.get(space)
        if box and box.frames:
            keep = deque(f for f in box.frames if f.rid != rid)
            box.frame_bytes = sum(len(f.body) for f in keep)
            box.frames = keep

    def take_frames(self, space: str, client: str, tab: str, now: float) -> list[Frame]:
        box = self.boxes.get(space)
        if not box or not box.frames:
            return []
        out, keep = [], deque()
        for f in box.frames:
            route = self.routes.get((space, f.rid))
            if route is None or f.expires <= now:
                continue
            if route.client == client and route.tab == tab and len(out) < CLIENT_CHUNKS_PER_POLL:
                out.append(f)
                if f.last:
                    del self.routes[(space, f.rid)]
            else:
                keep.append(f)
        box.frames = keep
        box.frame_bytes = sum(len(f.body) for f in keep)
        return out

    def open_page_rids(self, space: str, client: str, tab: str) -> list[str]:
        return [k[1] for k, r in self.routes.items()
                if k[0] == space and r.client == client and r.tab == tab and not r.stream]

    # --- host responses --------------------------------------------------------------------------

    def accept_chunk(self, space: str, rid: str, device_id: str, holder: str, now: float) -> Route:
        self.check_lease(space, device_id, holder, now)
        if not self.chunk_rate.allow(device_id):
            self.refuse(space, 429, "rate_limited", f"at most {CHUNKS_PER_S} response chunks per second")
        route = self.routes.get((space, rid))
        if route is None:
            raise api_error(404, "unknown_request", "no such open request")
        if route.closed:
            raise api_error(409, "already_complete", "the last chunk of this response was already posted")
        if route.queued:
            raise api_error(409, "not_delivered", "this request was not fetched by a host yet")
        return route

    def add_frame(self, space: str, rid: str, route: Route, idx: int, last: bool, body: str, now: float) -> None:
        box = self.box(space)
        if box.frame_bytes + len(body) > BOX_FRAME_BYTES:
            self.refuse(space, 429, "mailbox_full", "undelivered stream frames are over the limit; slow down")
        box.frames.append(Frame(rid, idx, last, body, now + TTL_S))
        box.frame_bytes += len(body)
        route.expires = now + TTL_S
        route.closed = last
        box.resp_wake.fire()


# --- database (sync; call through run_in_threadpool with a short connection) ----------------------

@contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def purge(conn: sqlite3.Connection, now: float) -> None:
    """Delete expired rows. A plain read first, so an idle mailbox costs no commit."""
    if conn.execute("SELECT 1 FROM bridge_msgs WHERE expires_at <= ? LIMIT 1", (int(now),)).fetchone():
        with _tx(conn):
            conn.execute("DELETE FROM bridge_msgs WHERE expires_at <= ?", (int(now),))


def insert_request(conn, space: str, rid: str, body: str, now: float) -> None:
    purge(conn, now)
    with _tx(conn):
        conn.execute("INSERT INTO bridge_msgs(space_id, kind, rid, idx, last, body, expires_at)"
                     " VALUES (?, 'req', ?, 0, 1, ?, ?)", (space, rid, body, int(now) + TTL_S))


def insert_page(conn, space: str, rid: str, idx: int, last: bool, body: str, now: float) -> None:
    purge(conn, now)
    with _tx(conn):
        used = conn.execute("SELECT COALESCE(SUM(length(body)), 0) FROM bridge_msgs WHERE space_id = ? AND kind = 'page'",
                            (space,)).fetchone()[0]
        if used + len(body) > BOX_PAGE_BYTES:
            raise api_error(429, "mailbox_full", "undelivered page responses are over the limit; slow down")
        conn.execute("INSERT INTO bridge_msgs(space_id, kind, rid, idx, last, body, expires_at)"
                     " VALUES (?, 'page', ?, ?, ?, ?, ?)", (space, rid, idx, int(last), body, int(now) + TTL_S))


def take_requests(conn, space: str, now: float) -> list[sqlite3.Row]:
    """The oldest waiting requests, deleted as they are handed over (FIFO)."""
    if not conn.execute("SELECT 1 FROM bridge_msgs WHERE space_id = ? AND kind = 'req' AND expires_at > ? LIMIT 1",
                        (space, int(now))).fetchone():
        return []
    with _tx(conn):
        rows = conn.execute("SELECT n, rid, body FROM bridge_msgs WHERE space_id = ? AND kind = 'req' AND expires_at > ?"
                            " ORDER BY n LIMIT ?", (space, int(now), HOST_REQUESTS_PER_POLL)).fetchall()
        _delete(conn, rows)
    return rows


def take_pages(conn, space: str, rids: list[str], now: float) -> list[sqlite3.Row]:
    if not rids:
        return []
    marks = ",".join("?" * len(rids))
    q = (f"SELECT n, rid, idx, last, body FROM bridge_msgs WHERE space_id = ? AND kind = 'page' AND expires_at > ?"
         f" AND rid IN ({marks}) ORDER BY n LIMIT ?")
    args = (space, int(now), *rids, CLIENT_CHUNKS_PER_POLL)
    if not conn.execute(q, args).fetchone():
        return []
    with _tx(conn):
        rows = conn.execute(q, args).fetchall()
        _delete(conn, rows)
    return rows


def _delete(conn, rows) -> None:
    if rows:
        conn.execute(f"DELETE FROM bridge_msgs WHERE n IN ({','.join('?' * len(rows))})", [r["n"] for r in rows])


def delete_rid(conn, space: str, rid: str) -> None:
    if conn.execute("SELECT 1 FROM bridge_msgs WHERE space_id = ? AND rid = ? LIMIT 1", (space, rid)).fetchone():
        with _tx(conn):
            conn.execute("DELETE FROM bridge_msgs WHERE space_id = ? AND rid = ?", (space, rid))


def now_ts() -> float:
    return clock.now().timestamp()
