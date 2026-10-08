"""The terminal routes of the computer's dashboard, on top of fake_bridge_host.FakeHost, with the host's typing-lease rules
(orch-core: routes_terminals.py, bridge_loop.LEASE_ROUTES, host_check.py; docs/bridge-protocol.md section 9.4). TEST SUPPORT ONLY.

  - terminal output streams (GET /terminals/<name>/stream, /terminals/stream) are kept open, one chunk per push();
  - a POST to a lease route (terminal keys, size, end, new, Start agent) runs only if its header names a stream THIS
    device opened and that is still open; otherwise it is refused assertion_failed (the real host: no subject for a
    fresh assertion). With such a stream it is refused lease_required until an assert request for the lease succeeds;
    the lease then lasts `lease_ms` from the host's clock (FakeHost.leases);
  - the refusal comes before the route runs, so it never takes a post number; the keys route follows routes_terminals.py
    `_device_keys_refusal`: n an integer >= 1, page 4-64 of A-Za-z0-9_-, at most 64 items and 2048 characters of text,
    n at or below the highest taken for (device, page) answers 409 and types nothing;
  - a revoke ends the device's streams with LAST | REFUSAL `revoked` and drops its lease.
`typed` is every text item that reached the terminal, in order; `posts` is every lease-route POST that ran."""
import json
import os
import re
import threading

from tests.support import bridge_protocol_ref as ref
from tests.support.fake_bridge_host import FakeHost, _b64u

LEASE_RULE = {"kind": "lease", "shown": "Type for 15 minutes", "digest": "", "scope": "type", "purpose": "lease"}
LEASE_ROUTES = [re.compile(p) for p in (r"/terminals/new", r"/terminals/[^/]+/(?:keys|size|end)", r"/t/[^/]+/agent/start", r"/quick/[^/]+/agent/start")]
PAGE = re.compile(r"[A-Za-z0-9_-]{4,64}")


class TerminalHost(FakeHost):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.streams: dict[str, dict] = {}        # rid hex -> {"req", "path", "idx", "device"}
        self.typed: list[str] = []
        self.posts: list[dict] = []               # {"path", "body", "status"} of every lease-route POST that ran
        self.taken: dict[tuple, int] = {}         # (device, page) -> the highest post number taken
        self.refused: list[tuple] = []            # (path, code) of every lease-route POST refused before it ran
        self._flock = threading.Lock()

    # ---- the lease rules
    def rule_for(self, req, meta, data):
        path = meta.get("path", "").split("?")[0]
        if meta.get("method") == "POST" and any(r.fullmatch(path) for r in LEASE_ROUTES):
            s = self.streams.get(req.stream.hex()) if req.stream != ref.ZERO_ID else None
            if s is None or s["device"] != req.device.hex():
                return {"refuse": "assertion_failed", "path": path}
            return dict(LEASE_RULE)
        return super().rule_for(req, meta, data)

    def _ask_assertion(self, req, meta, data, rule):
        if rule.get("refuse"):
            self.refused.append((rule["path"], rule["refuse"]))
            return self._send(req, {"refusal": rule["refuse"]}, flags=ref.F_LAST | ref.F_REFUSAL)
        self.refused.append((meta.get("path", "").split("?")[0], "lease_required"))
        return super()._ask_assertion(req, meta, data, rule)

    # ---- sending: more than one chunk, and the stream header of a stream's chunks
    def _send(self, req, meta, data=b"", flags=ref.F_LAST, idx=0):
        if req.flags & ref.F_STREAM:
            flags |= ref.F_STREAM                  # a refusal of a stream request carries STREAM too (section 3.5)
        h = ref.Header(ref.TO_DEVICE, flags, self.ws, req.device, req.rid, req.rid if flags & ref.F_STREAM else ref.ZERO_ID, idx, self._now(), os.urandom(16))
        env = ref.envelope(self.k_ws, self.priv, h, ref.frame(meta, data))
        self._http.post(f"/api/bridge/{self.space}/responses/{req.rid.hex()}", params={"holder": self.holder},
                        json={"idx": idx, "last": bool(flags & ref.F_LAST), "body": _b64u(env)}).raise_for_status()

    def _frame(self, s, meta, data=b"", flags=ref.F_STREAM):
        with self._flock:
            idx = s["idx"]
            s["idx"] += 1
            self._send(s["req"], meta, data, flags, idx=idx)

    # ---- the routes
    def push(self, name: str | None, text: str):
        """One `screen` event to every open stream of terminal `name` (None: the list's stream)."""
        path = f"/terminals/{name}/stream" if name else "/terminals/stream"
        for s in list(self.streams.values()):
            if s["path"] == path:
                self._frame(s, {}, f"event: screen\ndata: {json.dumps(text)}\n\n".encode())

    def end_streams(self, refusal: str | None = None, device: str | None = None):
        for rid, s in list(self.streams.items()):
            if device is None or s["device"] == device:
                self.streams.pop(rid, None)
                self.state["streams"].pop(rid, None)
                self._frame(s, {"refusal": refusal} if refusal else {}, b"", ref.F_STREAM | ref.F_LAST | (ref.F_REFUSAL if refusal else 0))

    def revoke(self, device: str):
        super().revoke(device)
        self.leases.pop(device, None)
        self.end_streams("revoked", device)

    def _serve(self, answer, meta, data=b""):
        path = meta.get("path", "").split("?")[0]
        if meta.get("op") == "cancel":
            self.seen.append(meta)
            self.streams.pop(answer.stream.hex(), None)
            return self._send(answer, {"status": 200, "headers": {}})
        if answer.flags & ref.F_STREAM and path.startswith("/terminals/"):
            self.seen.append(meta)
            s = self.streams[answer.rid.hex()] = {"req": answer, "path": path, "idx": 0, "device": answer.device.hex()}
            return self._frame(s, {"status": 200, "headers": {"content-type": "text/event-stream"}}, b"")
        if meta.get("method") == "POST" and any(r.fullmatch(path) for r in LEASE_ROUTES):
            self.seen.append(meta)
            status, text = self._run_post(answer.device.hex(), path, data)
            return self._send(answer, {"status": status, "headers": {"content-type": "text/plain"}}, text.encode())
        super()._serve(answer, meta, data)

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
