"""The terminal routes of the computer's dashboard, on top of fake_bridge_host.FakeHost (which has the streams), with the
host's typing-lease rules (orch-core: routes_terminals.py, bridge_loop.LEASE_ROUTES, host_check.py; docs/bridge-protocol.md
section 9.4). TEST SUPPORT ONLY.

  - a POST to a lease route (terminal keys, size, end) runs only if its header names a stream THIS device opened (the
    host's stream table); with none it is refused assertion_failed (no subject for a fresh assertion), with one this device
    did not open ref.host_check already refused it forbidden_scope. With such a stream it is refused lease_required until
    an assert request for the lease succeeds; the lease then lasts `lease_ms` on the host's clock (FakeHost.leases). The
    assert request re-checks that stream is still open (forbidden_scope, no lease, if not);
  - a start (POST /terminals/new, /t/<ref>/agent/start, /quick/<id>/agent/start) is a FRESH assertion route, not a lease
    route: it needs no stream, is refused assertion_required with a subject, and runs once after the assertion;
  - the refusal comes before the route runs, so it never takes a post number; the keys route follows routes_terminals.py
    `_device_keys_refusal`: n an integer >= 1, page 4-64 of A-Za-z0-9_-, at most 64 items and 2048 characters of text,
    n at or below the highest taken for (device, page) answers 409 and types nothing;
  - a revoke ends the device's streams with LAST | REFUSAL `revoked` (FakeHost) and drops its lease.
`typed` is every text item that reached the terminal, in order; `posts` is every lease-route or start POST that ran."""
import json
import re

from tests.support import bridge_protocol_ref as ref
from tests.support.fake_bridge_host import FakeHost

LEASE_RULE = {"kind": "lease", "shown": "Type for 15 minutes", "digest": "", "scope": "type", "purpose": "lease"}
START_RULE = {"kind": "action", "shown": "Start a session", "digest": "cd" * 32, "scope": "type"}
LEASE_ROUTES = [re.compile(p) for p in (r"/terminals/[^/]+/(?:keys|size|end)",)]
START_ROUTES = [re.compile(p) for p in (r"/terminals/new", r"/t/[^/]+/agent/start", r"/quick/[^/]+/agent/start")]
PAGE = re.compile(r"[A-Za-z0-9_-]{4,64}")


def _is(routes, meta):
    return meta.get("method") == "POST" and any(r.fullmatch(meta.get("path", "").split("?")[0]) for r in routes)


class TerminalHost(FakeHost):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.typed: list[str] = []
        self.posts: list[dict] = []               # {"path", "body", "status"} of every such POST that ran
        self.taken: dict[tuple, int] = {}         # (device, page) -> the highest post number taken
        self.refused: list[tuple] = []            # (path, code) of every such POST refused before it ran
        self.headers_seen: list[tuple] = []       # (path, stream header hex or "") of every such POST that reached the host

    # ---- the rules
    def rule_for(self, req, meta, data):
        path = meta.get("path", "").split("?")[0]
        if _is(START_ROUTES, meta):
            self.headers_seen.append((path, req.stream.hex() if req.stream != ref.ZERO_ID else ""))
            return dict(START_RULE)
        if _is(LEASE_ROUTES, meta):
            self.headers_seen.append((path, req.stream.hex() if req.stream != ref.ZERO_ID else ""))
            # host_check.py _authorize: lease_class needs a header stream this device opened
            if req.stream == ref.ZERO_ID or self.state["streams"].get(req.stream.hex()) != req.device.hex():
                return {"refuse": "assertion_failed", "path": path}
            return dict(LEASE_RULE)
        return super().rule_for(req, meta, data)

    def _ask_assertion(self, req, meta, data, rule):
        path = meta.get("path", "").split("?")[0]
        if rule.get("refuse"):
            self.refused.append((path, rule["refuse"]))
            return self._send(req, {"refusal": rule["refuse"]}, flags=ref.F_LAST | ref.F_REFUSAL)
        self.refused.append((path, "lease_required" if rule.get("purpose") == "lease" else "assertion_required"))
        return super()._ask_assertion(req, meta, data, rule)

    def _assert(self, req, meta):
        """host_check.py _assert: R1 runs only if its stream is still open and the device's (else forbidden_scope, and no
        lease is opened); the parked request is gone either way."""
        parked = self.parked.get(meta.get("for"))
        if parked is not None and parked[0].stream != ref.ZERO_ID and self.state["streams"].get(parked[0].stream.hex()) != req.device.hex():
            self.parked.pop(meta["for"], None)
            self.audit.append({"ok": False, "why": "forbidden_scope", "rid": meta.get("for"), "purpose": "lease"})
            return self._send(req, {"refusal": "forbidden_scope"}, flags=ref.F_LAST | ref.F_REFUSAL)
        return super()._assert(req, meta)

    # ---- the streams (FakeHost keeps them in `open`)
    def push_screen(self, name: str | None, text: str):
        """One `screen` event to every open stream of terminal `name` (None: the list's stream)."""
        self.push(f"/terminals/{name}/stream" if name else "/terminals/stream",
                  f"event: screen\ndata: {json.dumps(text)}\n\n".encode())

    def end_streams(self, refusal: str | None = None, device: str | None = None):
        for rid in list(self.open):
            if device is None or self.open[rid]["device"] == device:
                self.state["streams"].pop(rid, None)
        self.end(refusal=refusal, device=device)

    def revoke(self, device: str):
        self.leases.pop(device, None)
        super().revoke(device)

    # ---- the routes
    def _serve(self, req, meta, data=b""):
        path = meta.get("path", "").split("?")[0]
        if _is(LEASE_ROUTES, meta) or _is(START_ROUTES, meta):
            self.seen.append(meta)
            status, text = self._run_post(req.device.hex(), path, data)
            return self._send(req, {"status": status, "headers": {"content-type": "text/plain"}}, text.encode())
        super()._serve(req, meta, data)

    def _run_post(self, device: str, path: str, data: bytes) -> tuple[int, str]:
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            body = None
        rec = {"path": path, "body": body, "status": 204}
        self.posts.append(rec)
        if path.endswith("/keys"):
            n, page, seq = (body or {}).get("n"), (body or {}).get("page"), (body or {}).get("seq")
            if type(n) is not int or n < 1 or not isinstance(page, str) or not PAGE.fullmatch(page):
                rec["status"] = 400
                return 400, "key posts from a device carry a post number n and a page name"
            if not isinstance(seq, list) or len(seq) > 64 or sum(len(i.get("text", "")) for i in seq if isinstance(i, dict)) > 2048:
                rec["status"] = 413
                return 413, "too many keys in one post"
            if n <= self.taken.get((device, page), 0):
                rec["status"] = 409
                return 409, "post number already used"
            self.taken[(device, page)] = n
            self.typed += [i["text"] for i in seq if isinstance(i, dict) and isinstance(i.get("text"), str)]
        return 204, ""
