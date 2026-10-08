"""A small fake host for browser tests of the Remote pages: it plays the part of the computer's orch host over the REAL
mailbox routes (an approved device that owns the space, long-polling for requests) and answers with the Python
reference implementation of the protocol (tests/support/bridge_protocol_ref.py): host_check for what to do with a
request, envelope() for the sealed, host-signed answers. TEST SUPPORT ONLY; nothing in fileshare/ imports it."""
import base64
import hashlib
import json
import os
import struct
import threading
import time

import httpx

from tests.support import bridge_protocol_ref as ref

BEAT = {"sessions": 1, "in_progress": 1, "needs_you": 0, "factory": "none"}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _cbor(raw: bytes, at: int = 0):
    """A minimal CBOR reader for the attestation object (definite lengths; ints, bytes, text, arrays, maps)."""
    first = raw[at]
    major, info = first >> 5, first & 31
    at += 1
    if info < 24:
        n = info
    else:
        width = 1 << (info - 24)
        n = int.from_bytes(raw[at:at + width], "big")
        at += width
    if major == 0:
        return n, at
    if major == 1:
        return -1 - n, at
    if major in (2, 3):
        v = raw[at:at + n]
        return (v if major == 2 else v.decode()), at + n
    out = [] if major == 4 else {}
    for _ in range(n):
        if major == 4:
            v, at = _cbor(raw, at)
            out.append(v)
        else:
            k, at = _cbor(raw, at)
            v, at = _cbor(raw, at)
            out[k] = v
    return out, at


def parse_attestation(att: bytes):
    """-> (authData, credential id, 65-byte public key) of a WebAuthn attestation object."""
    obj, _ = _cbor(att)
    ad = obj["authData"]
    n = int.from_bytes(ad[53:55], "big")
    cid = ad[55:55 + n]
    key, _ = _cbor(ad[55 + n:])
    return ad, cid, b"\x04" + key[-2] + key[-3]


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
        self.bodies: list[tuple] = []        # (method, path, body bytes) of every request that ran
        self.lie_fingerprint = False         # True: the pending answer names a different device key's fingerprint
        self.silent = False                  # True: read requests and answer nothing
        self.sse: dict[str, list[bytes]] = {}   # path -> the frames a stream for it opens with
        self.open: dict[str, dict] = {}      # rid hex -> {"req": Header, "path", "idx", "device"}: the streams that are open
        self.cancelled: list[str] = []       # rids a `cancel` op closed
        self.stream_opens: list[tuple[str, float]] = []   # (path, time) of every stream request that ran
        self._lock = threading.Lock()
        # R11: the platform credential and the assertions (docs/bridge-protocol.md section 9). `requires` maps a path to
        # {"kind", "shown", "digest", "scope", "purpose": "fresh" | "lease"}; a request for it is parked and refused
        # assertion_required / lease_required until an `assert` request verifies.
        self.requires: dict[str, dict] = {}
        self.rp_id, self.origin = "localhost", ""
        self.creds: dict[str, dict] = {}         # device hex -> the registered credential (ref.verify_assertion's shape)
        self.registering: dict[str, dict] = {}   # device hex -> the open registration challenge
        self.challenges: dict[str, dict] = {}    # challenge hex -> {device, expires_ms, rid, purpose}
        self.parked: dict[str, tuple] = {}       # rid hex -> (header, meta, data)
        self.leases: dict[str, int] = {}
        self.lie_subject: str | None = None      # the refusal shows this text, the challenge was built from the real one
        self.refuse_registration = False         # True: credential_finish is refused
        self.audit: list[dict] = []              # one row per assert request: the verdict and the subject
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

    # ---- R11: credential_begin / credential_finish of a pending device, assertions
    def _credential(self, env: bytes) -> bool:
        hb, body, sig = ref.split(env)
        h = ref.Header.decode(hb)
        did = h.device.hex()
        pend = self.state["pending_pairs"].get(did)
        if pend is None or did in self.state["devices"]:
            return False
        try:
            meta, _ = ref.unframe(ref.open_body(self.k_ws, hb, body), strict=True)
        except Exception:
            return False
        op = meta.get("op")
        if op not in ("credential_begin", "credential_finish"):
            return False
        if not ref.verify(bytes.fromhex(pend["pub"]), sig, ref.signed_bytes(hb, body)):
            return True
        if op == "credential_begin":
            nonce, expires = os.urandom(32), self._now() + 120_000
            self.registering[did] = {"challenge": ref.registration_challenge(h.workspace, h.device, expires, nonce), "expires_ms": expires}
            self._send(h, {"nonce": nonce.hex(), "expires_ms": expires})
            return True
        reg = self.registering.pop(did, None)          # single use, whatever happens next
        try:
            cid, att, cdj = (ref.unb64u(meta[k]) for k in ("credential_id", "attestation_object", "client_data_json"))
            client = json.loads(cdj)
            ad, got_cid, pub = parse_attestation(att)
            ok = (reg is not None and self._now() < reg["expires_ms"] and not self.refuse_registration
                  and client["type"] == "webauthn.create" and ref.unb64u(client["challenge"]) == reg["challenge"]
                  and client["origin"] == self.origin and not client.get("crossOrigin")
                  and ad[:32] == hashlib.sha256(self.rp_id.encode()).digest() and ad[32] & 0x45 == 0x45 and got_cid == cid)
        except Exception:
            ok = False
        if not ok:
            self._send(h, {"refusal": "assertion_failed"}, flags=ref.F_LAST | ref.F_REFUSAL)
            return True
        be = bool(ad[32] & 0x08)
        self.creds[did] = {"device": did, "credential_id": cid.hex(), "pub": pub.hex(), "sign_count": struct.unpack(">I", ad[33:37])[0],
                           "be": be, "rp_id": self.rp_id, "origin": self.origin}
        self._send(h, {"registered": True, "synced": be})
        return True

    def _ask_assertion(self, req: ref.Header, meta: dict, data: bytes, rule: dict):
        did, rid = req.device.hex(), req.rid.hex()
        purpose = rule.get("purpose", "fresh")
        subject = {"kind": rule["kind"], "shown": rule["shown"], "digest": rule.get("digest", "")}
        nonce, expires = os.urandom(32), self._now() + 120_000
        ch = ref.assertion_challenge(self.ws, req.device, req.rid, purpose, rule["scope"], expires, nonce, subject)
        self.challenges[ch.hex()] = {"device": did, "expires_ms": expires, "rid": rid, "purpose": purpose}
        self.parked[rid] = (req, meta, data)
        sent = {**subject, "shown": self.lie_subject} if self.lie_subject else subject
        self._send(req, {"refusal": "assertion_required" if purpose == "fresh" else "lease_required", "purpose": purpose,
                         "scope": rule["scope"], "expires_ms": expires, "nonce": nonce.hex(), "subject": sent},
                   flags=ref.F_LAST | ref.F_REFUSAL)

    def _assert(self, req: ref.Header, meta: dict):
        did = req.device.hex()
        try:
            a = {k: ref.unb64u(meta[k]).hex() for k in ("credential_id", "authenticator_data", "client_data_json", "signature")}
            issued = dict(self.challenges.get(ref.unb64u(json.loads(bytes.fromhex(a["client_data_json"]))["challenge"]).hex()) or {})
        except Exception:
            a, issued = None, {}
        cred = self.creds.get(did)
        res = ref.verify_assertion(cred, self.challenges, did, a, self._now()) if cred and a else {"result": "refuse", "why": "no_credential"}
        ok = res["result"] == "verified" and issued.get("rid") == meta.get("for") and meta.get("for") in self.parked
        self.audit.append({"ok": ok, "why": res.get("why"), "rid": meta.get("for"), "purpose": issued.get("purpose")})
        if not ok:
            self.parked.pop(meta.get("for"), None)
            self._send(req, {"refusal": "assertion_failed"}, flags=ref.F_LAST | ref.F_REFUSAL)
            return
        _, m1, m3 = self.parked.pop(meta["for"])
        if issued["purpose"] == "lease":
            self.leases[did] = self._now() + 15 * 60_000
        self._serve(req, m1, m3)

    def _serve(self, req: ref.Header, meta: dict, data: bytes = b""):
        """Run a request that may run (also a parked one after its assertion): `req` is the header answered."""
        rid = req.rid.hex()
        self.seen.append(meta)
        self.bodies.append((meta.get("method"), meta.get("path"), data))
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

    def _handle(self, rid: str, env: bytes):
        if self.silent:
            return
        req = ref.Header.decode(env[:ref.HEADER_LEN])
        if self._credential(env):
            return
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
            if meta.get("op") == "assert":
                return self._assert(req, meta)
            rule = self.requires.get(meta.get("path", "").split("?")[0])
            data = bytes.fromhex(res["data"])
            if callable(rule):                  # a rule that reads the request: (meta, body bytes) -> rule | None
                rule = rule(meta, data)
            if rule and rule.get("refuse"):      # the host builds no subject (a stale hash, an unknown request): a plain refusal
                return self._send(req, {"refusal": rule["refuse"]}, flags=ref.F_LAST | ref.F_REFUSAL)
            if rule and not (rule.get("purpose") == "lease"and self.leases.get(req.device.hex(), 0) > self._now()):
                return self._ask_assertion(req, meta, data, rule)
            self._serve(req, meta, data)
