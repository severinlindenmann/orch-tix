"""A small fake host for browser tests of the Remote pages: it plays the part of the computer's orch host over the REAL
mailbox routes (an approved device that owns the space, long-polling for requests) and answers with the Python
reference implementation of the protocol (tests/support/bridge_protocol_ref.py): host_check for what to do with a
request, envelope() for the sealed, host-signed answers. TEST SUPPORT ONLY; nothing in fileshare/ imports it."""
import base64
import os
import threading
import time

import httpx

from tests.support import bridge_protocol_ref as ref

BEAT = {"sessions": 1, "in_progress": 1, "needs_you": 0, "factory": "none"}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeHost:
    def __init__(self, base_url: str, space: str, headers: dict, mk: bytes, pages: dict | None = None, beat: bool = True):
        self.base, self.space, self.headers = base_url, space, headers
        self.ws = bytes.fromhex(space)
        self.k_ws = ref.workspace_key(mk, space)
        self.priv = ref.private_key(os.urandom(31) + b"\x01")
        self.host_pub = ref.public_bytes(self.priv)
        self.state = {"workspace": space, "k_ws": self.k_ws, "key_version": 1, "devices": {}, "rids": {}, "offers": {},
                      "pending_pairs": {}, "phones": {}, "streams": {}, "unverified": [], "host_pub": self.host_pub.hex()}
        self.pages = pages or {}
        self.seen: list[dict] = []           # the meta of every request that ran
        self.lie_fingerprint = False         # True: the pending answer names a different device key's fingerprint
        self.silent = False                  # True: read requests and answer nothing
        self.sse: dict[str, list[bytes]] = {}   # path -> the frames a stream for it opens with
        self.open: dict[str, dict] = {}      # rid hex -> {"req": Header, "path", "idx", "device"}: the streams that are open
        self.cancelled: list[str] = []       # rids a `cancel` op closed
        self.stream_opens: list[tuple[str, float]] = []   # (path, time) of every stream request that ran
        self._lock = threading.Lock()
        self.holder = "fakehost" + os.urandom(3).hex()
        self._stop = threading.Event()
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=30)
        self._beat = beat
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ---- lifecycle
    def start(self):
        if self._beat:
            self.heartbeat()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)
        self._http.close()

    def heartbeat(self):
        self._http.post(f"/api/presence/{self.space}/heartbeat", json=BEAT).raise_for_status()

    def goodbye(self):
        self._http.post(f"/api/presence/{self.space}/goodbye", json={}).raise_for_status()

    # ---- the owner's side (the Mac)
    def offer(self, scope: str = "operate") -> str:
        """A pairing link fragment (what follows the # of /remote/pair)."""
        pid, secret = os.urandom(16), os.urandom(32)
        self.state["offers"][pid.hex()] = {"secret": secret.hex(), "scope": scope, "expires_ms": self._now() + 600_000}
        return ".".join(["v1", self.space, pid.hex(), _b64u(secret), _b64u(ref.host_pin(self.host_pub))])

    def waiting(self) -> dict:
        return self.state["pending_pairs"]

    def approve(self, device: str):
        p = self.state["pending_pairs"][device]
        p["state"] = "approved"
        self.state["devices"][device] = {"pub": p["pub"], "scope": p["scope"], "revoked": False, "high": 0, "bitmap": 0}

    def reject(self, device: str):
        self.state["pending_pairs"][device]["state"] = "rejected"

    def revoke(self, device: str):
        self.state["devices"][device]["revoked"] = True
        self.end(refusal="revoked", device=device)        # §4: revocation ends the device's open streams with the stored refusal

    # ---- streams (the host side of §4)
    def push(self, path: str, *frames: bytes, coalesce: bool = False):
        """Send frames (SSE text as bytes) to every open stream on `path`: one chunk each, or all in one chunk."""
        for rid, s in list(self.open.items()):
            if s["path"] == path:
                for data in ([b"".join(frames)] if coalesce else frames):
                    self._frame(s, {}, data)

    def keepalive(self, path: str):
        for s in list(self.open.values()):
            if s["path"] == path:
                self._frame(s, {"keepalive": True}, b"")

    def end(self, path: str | None = None, refusal: str | None = None, device: str | None = None):
        """Close the open streams (all, one path or one device's): LAST, and LAST | REFUSAL with a code."""
        for rid, s in list(self.open.items()):
            if (path is None or s["path"] == path) and (device is None or s["device"] == device):
                self.open.pop(rid, None)
                flags = ref.F_STREAM | ref.F_LAST | (ref.F_REFUSAL if refusal else 0)
                self._frame(s, {"refusal": refusal} if refusal else {}, b"", flags)

    def _frame(self, s: dict, meta: dict, data: bytes, flags: int = ref.F_STREAM):
        with self._lock:
            idx = s["idx"]
            s["idx"] += 1
            self._send(s["req"], meta, data, flags, idx=idx)

    # ---- the loop
    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _run(self):
        last_beat = time.time()
        while not self._stop.is_set():
            try:
                if self._beat and time.time() - last_beat > 8:
                    self.heartbeat()
                    last_beat = time.time()
                r = self._http.get(f"/api/bridge/{self.space}/requests", params={"holder": self.holder, "wait": 1, "take_over": 1})
                for item in r.json().get("requests", []):
                    self._handle(item["id"], _unb64u(item["body"]))
            except Exception:
                time.sleep(0.2)

    def _send(self, req: ref.Header, meta: dict, data: bytes = b"", flags: int = ref.F_LAST, idx: int = 0):
        if req.flags & ref.F_STREAM:
            flags |= ref.F_STREAM                # a refusal of a stream request carries STREAM too (§3.5)
        h = ref.Header(ref.TO_DEVICE, flags, self.ws, req.device, req.rid, req.rid if flags & ref.F_STREAM else ref.ZERO_ID, idx,
                       self._now(), os.urandom(16))
        env = ref.envelope(self.k_ws, self.priv, h, ref.frame(meta, data))
        self._http.post(f"/api/bridge/{self.space}/responses/{req.rid.hex()}", params={"holder": self.holder},
                        json={"idx": idx, "last": bool(flags & ref.F_LAST), "body": _b64u(env)}).raise_for_status()

    def _handle(self, rid: str, env: bytes):
        if self.silent:
            return
        req = ref.Header.decode(env[:ref.HEADER_LEN])
        res = ref.host_check(env, self.state, self._now(), mailbox_id=rid)
        kind = res["result"]
        if kind == "refuse":
            self._send(req, {"refusal": res["code"], **{k: v for k, v in res.items() if k not in ("result", "code")}},
                       flags=ref.F_LAST | ref.F_REFUSAL)
        elif kind == "pair_pending":
            answer = dict(res["answer"])
            if self.lie_fingerprint:
                answer["fingerprint"] = ref.device_fingerprint(os.urandom(65))
            self._send(req, answer)
        elif kind == "pair_status":
            self._send(req, res["answer"])
        elif kind == "accept":
            meta = res["meta"]
            if meta.get("op") == "pair_status":          # the device is registered now: the host answers from its registry
                self._send(req, {"state": "approved", "scope": res["scope"]})
                return
            self.seen.append(meta)
            path = meta.get("path", "").split("?")[0]
            if meta.get("op") == "cancel":
                self.cancelled.append(req.stream.hex())
                self.open.pop(req.stream.hex(), None)
                self._send(req, {"status": 200, "headers": {}})
                return
            if req.flags & ref.F_STREAM:
                self.stream_opens.append((path, time.time()))
                s = self.open[rid] = {"req": req, "path": path, "idx": 0, "device": req.device.hex()}
                self._frame(s, {"status": 200, "headers": {"content-type": "text/event-stream"}}, b"")
                for data in self.sse.get(path, []):
                    self._frame(s, {}, data)
                return
            page = self.pages.get(path)
            if page is None:
                self._send(req, {"status": 404, "headers": {"content-type": "text/plain"}}, b"not found")
            else:
                status, headers, body = page["status"], page["headers"], page["body"]
                self._send(req, {"status": status, "headers": headers, **({"page": True} if page.get("page") else {})},
                           body.encode() if isinstance(body, str) else body)
