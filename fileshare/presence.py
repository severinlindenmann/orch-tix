"""Host presence (Remote, R9): what a workspace's host says about itself in the clear. The server keeps the
last heartbeat time, the goodbye time, three counts, a Factory state code and three integers; names (workspace,
machine, device label) never come here. State is derived on read from the clock, so nothing ticks in the
background and a server restart loses nothing."""
import sqlite3

ONLINE_S = 30                   # a heartbeat newer than this: online
LOST_S = 300                    # older than this with no goodbye: lost
FACTORY = ("none", "running", "paused", "waiting", "ready", "stopped", "done")
STATES = ("online", "not_answering", "lost", "stopped", "never_started")
MAX_COUNT = 9999
MAX_BUDGET_PCT = 999
BEATS_PER_MIN = 30              # per device; the host sends 6 a minute
FIELDS = ("sessions", "in_progress", "needs_you", "factory", "children_done", "children_total", "budget_pct")
OPTIONAL = ("children_done", "children_total", "budget_pct")     # absent means unknown, never zero


def derive_state(row, now: float) -> str:
    """row: anything with hb_at and bye_at, or None for a space that never reported."""
    if row is None:
        return "never_started"
    if row["bye_at"] is not None and row["bye_at"] >= row["hb_at"]:
        return "stopped"
    age = now - row["hb_at"]
    return "online" if age < ONLINE_S else "not_answering" if age <= LOST_S else "lost"


def check_beat(body: dict) -> dict:
    """The strict heartbeat shape: exactly these keys (the optional ones may be missing or null), integers in
    range, a Factory code from the list. Raises ValueError with a message for the caller to wrap."""
    extra = set(body) - set(FIELDS)
    if extra:
        raise ValueError("unknown field: only " + ", ".join(FIELDS) + " are accepted")
    out = {}
    for k in FIELDS:
        v = body.get(k)
        if k in OPTIONAL and v is None:
            out[k] = None
        elif k == "factory":
            if v not in FACTORY:
                raise ValueError("factory must be one of " + ", ".join(FACTORY))
            out[k] = v
        else:
            hi = MAX_BUDGET_PCT if k == "budget_pct" else MAX_COUNT
            if type(v) is not int or not 0 <= v <= hi:
                raise ValueError(f"{k} must be an integer in [0, {hi}]")
            out[k] = v
    if (out["children_done"] is None) != (out["children_total"] is None):
        raise ValueError("children_done and children_total go together")
    if out["children_total"] is not None and out["children_done"] > out["children_total"]:
        raise ValueError("children_done cannot exceed children_total")
    return out


def beat(conn: sqlite3.Connection, space: str, device_id: str, vals: dict, now: float) -> None:
    conn.execute(
        "INSERT INTO presence(space_id, device_id, hb_at, bye_at, sessions, in_progress, needs_you, factory,"
        " children_done, children_total, budget_pct) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(space_id) DO UPDATE SET device_id = excluded.device_id, hb_at = excluded.hb_at, bye_at = NULL,"
        " sessions = excluded.sessions, in_progress = excluded.in_progress, needs_you = excluded.needs_you,"
        " factory = excluded.factory, children_done = excluded.children_done,"
        " children_total = excluded.children_total, budget_pct = excluded.budget_pct",
        (space, device_id, now, *(vals[k] for k in FIELDS)))


def goodbye(conn: sqlite3.Connection, space: str, now: float) -> None:
    conn.execute("UPDATE presence SET bye_at = ? WHERE space_id = ?", (now, space))


def read(conn: sqlite3.Connection, now: float) -> list[dict]:
    """Every live space with its clear presence data and derived state (a never-started space has nulls)."""
    from fileshare import mirrors
    out = []
    for s in mirrors.list_spaces(conn):
        p = conn.execute("SELECT * FROM presence WHERE space_id = ?", (s["id"],)).fetchone()
        out.append({**s, "state": derive_state(p, now),
                    "last_seen": None if p is None else p["hb_at"],
                    "goodbye_at": None if p is None else p["bye_at"],
                    **{k: None if p is None else p[k] for k in FIELDS}})
    return out
