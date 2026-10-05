"""Reference implementation of the bridge protocol, v1 (docs/bridge-protocol.md). TEST SUPPORT ONLY.

Nothing in fileshare/ imports this: the TIX server only relays sealed bytes. It exists so that
tests/bridge_vectors.json is reproducible and so that orch-core (the host, R3) and the TIX app (the
browser, R10) can check their own code against the same vectors. Section numbers (§n) refer to the
specification; where this file and the specification disagree, the specification wins and this file
is a bug.

Run `uv run python -m tests.support.bridge_protocol_ref` to rewrite tests/bridge_vectors.json.
"""
import base64
import hashlib
import hmac
import json
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# --- constants (§3, §5) ---------------------------------------------------------------------------

MAGIC = b"SHRB"
VERSION = 1
TO_HOST, TO_DEVICE = 1, 2
F_LAST, F_STREAM, F_REFUSAL = 0x01, 0x02, 0x04
HEADER_LEN = 104
TAG_LEN = 16
SIG_LEN = 64
OVERHEAD = HEADER_LEN + TAG_LEN + SIG_LEN            # 184
MAX_REQUEST = 1 << 20                                # whole envelope, decoded (the mailbox's limit)
MAX_CHUNK = 256 * 1024
MAX_META = 64 * 1024
WINDOW_MS = 300_000                                  # each way
SEQ_WINDOW = 64
RID_RETENTION_MS = 900_000
RID_AFTER_TS_MS = WINDOW_MS + 60_000                 # a rid is also kept until ts_ms + 360 s (§5.3)
BUDGET = 10                                          # unverified refusals per minute, host-wide (§6.1)
BUDGET_WINDOW_MS = 60_000
ZERO_NONCE = bytes(12)
ZERO_ID = bytes(16)
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

L_WS = b"sharing/bridge/ws/v1|"
L_MSG = b"sharing/bridge/msg/v1"
L_SIG = b"sharing/bridge/sig/v1|"
L_DEVICE = b"sharing/bridge/device/v1|"
L_FP = b"sharing/bridge/fp/v1|"
L_HOST = b"sharing/bridge/host/v1|"
L_PAIR = b"sharing/bridge/pair/v1|"
L_PHONE = b"sharing/bridge/phone-link/v1|"
L_ASSERT = b"sharing/bridge/assert/v1|"
L_REG = b"sharing/bridge/webauthn-reg/v1|"

SCOPES = {"look": 1, "decide": 2, "operate": 3, "type": 4}
PURPOSES = {"fresh": 1, "lease": 2}
AD_UP, AD_UV, AD_BE = 0x01, 0x04, 0x08

_HDR = struct.Struct(">4sBBBB16s16s16s16sQQ16s")
assert _HDR.size == HEADER_LEN


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# --- keys (§2) ------------------------------------------------------------------------------------

def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA-256. An empty salt means HashLen zero bytes (RFC 5869 §2.2), as in WebCrypto."""
    return HKDF(hashes.SHA256(), length, salt or None, info).derive(ikm)


def workspace_key(mk: bytes, workspace_hex: str) -> bytes:
    """K_ws: the per-workspace channel key, from the account master key. Nothing new is distributed."""
    assert len(mk) == 32 and len(workspace_hex) == 32
    return hkdf(mk, b"", L_WS + workspace_hex.encode("ascii"))


def message_key(k_ws: bytes, salt: bytes) -> bytes:
    """K_msg: used for exactly one envelope (§2.3), so the GCM nonce can be the constant ZERO_NONCE."""
    assert len(salt) == 16
    return hkdf(k_ws, salt, L_MSG)


def private_key(d: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())


def public_bytes(priv: ec.EllipticCurvePrivateKey) -> bytes:
    n = priv.public_key().public_numbers()
    return b"\x04" + n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")


def load_public(pub: bytes) -> ec.EllipticCurvePublicKey:
    if len(pub) != 65 or pub[0] != 4:
        raise ValueError("not an uncompressed P-256 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub)   # checks the point is on the curve


def device_id(workspace: bytes, pub: bytes) -> bytes:
    """Per workspace: one browser key does not give one id across workspaces (§2.4)."""
    return hashlib.sha256(L_DEVICE + workspace + pub).digest()[:16]


def device_fingerprint(pub: bytes) -> str:
    """TIX's fingerprint format (security.py, crypto.js: 100 bits, 4-4-4-4-4 base32) under the bridge label."""
    raw = base64.b32encode(hashlib.sha256(L_FP + pub).digest()).decode()[:20]
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def host_pin(host_pub: bytes) -> bytes:
    return hashlib.sha256(L_HOST + host_pub).digest()


def pair_mac(secret: bytes, workspace: bytes, pairing_id: bytes, pub: bytes) -> bytes:
    return hmac.new(secret, L_PAIR + workspace + pairing_id + pub, hashlib.sha256).digest()


def phone_link_proof(phone_key: bytes, dev_id: bytes) -> bytes:
    return hmac.new(phone_key, L_PHONE + dev_id, hashlib.sha256).digest()


# --- signatures (§3.4): ECDSA P-256 / SHA-256, raw r||s (WebCrypto's format), never DER ------------

def sign(priv: ec.EllipticCurvePrivateKey, msg: bytes) -> bytes:
    # Deterministic (RFC 6979) only so the vector file is reproducible; real signers may randomise.
    der = priv.sign(msg, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def scalars_in_range(sig: bytes) -> bool:
    """A MUST of its own (§3.4): OpenSSL happens to check it too, other libraries may not."""
    if len(sig) != SIG_LEN:
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    return 0 < r < P256_N and 0 < s < P256_N


def verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    if not scalars_in_range(sig):
        return False
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    try:
        load_public(pub).verify(encode_dss_signature(r, s), msg, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- envelope (§3) --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Header:
    direction: int
    flags: int
    workspace: bytes
    device: bytes
    rid: bytes
    stream: bytes
    seq: int
    ts_ms: int
    salt: bytes
    key_version: int = 1
    version: int = VERSION
    magic: bytes = MAGIC

    def encode(self) -> bytes:
        return _HDR.pack(self.magic, self.version, self.direction, self.flags, self.key_version, self.workspace,
                         self.device, self.rid, self.stream, self.seq, self.ts_ms, self.salt)

    @classmethod
    def decode(cls, b: bytes) -> "Header":
        m, v, d, f, kv, ws, dev, rid, st, seq, ts, salt = _HDR.unpack(b[:HEADER_LEN])
        return cls(d, f, ws, dev, rid, st, seq, ts, salt, kv, v, m)

    def as_json(self) -> dict:
        return {"magic": self.magic.decode("latin-1"), "version": self.version, "direction": self.direction,
                "flags": self.flags, "key_version": self.key_version, "workspace": self.workspace.hex(),
                "device": self.device.hex(), "rid": self.rid.hex(), "stream": self.stream.hex(),
                "seq": self.seq, "ts_ms": self.ts_ms, "salt": self.salt.hex()}


def frame(meta: dict, data: bytes = b"") -> bytes:
    m = canonical_json(meta)
    assert len(m) <= MAX_META
    return struct.pack(">I", len(m)) + m + data


def unframe(pt: bytes) -> tuple[dict, bytes]:
    if len(pt) < 4:
        raise ValueError("short plaintext")
    n = struct.unpack(">I", pt[:4])[0]
    if n > MAX_META or 4 + n > len(pt):
        raise ValueError("bad meta length")
    meta = json.loads(pt[4:4 + n].decode("utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("meta is not an object")
    return meta, pt[4 + n:]


def seal_body(k_ws: bytes, header: bytes, plaintext: bytes) -> bytes:
    """ciphertext || tag. AAD = the 104 header bytes."""
    return AESGCM(message_key(k_ws, Header.decode(header).salt)).encrypt(ZERO_NONCE, plaintext, header)


def open_body(k_ws: bytes, header: bytes, body: bytes) -> bytes:
    return AESGCM(message_key(k_ws, Header.decode(header).salt)).decrypt(ZERO_NONCE, body, header)


def signed_bytes(header: bytes, body: bytes) -> bytes:
    return L_SIG + header + body


def envelope(k_ws: bytes, signer: ec.EllipticCurvePrivateKey, h: Header, plaintext: bytes) -> bytes:
    hb = h.encode()
    body = seal_body(k_ws, hb, plaintext)
    return hb + body + sign(signer, signed_bytes(hb, body))


def split(env: bytes) -> tuple[bytes, bytes, bytes]:
    return env[:HEADER_LEN], env[HEADER_LEN:-SIG_LEN], env[-SIG_LEN:]


def digest(env: bytes) -> bytes:
    """The idempotency digest: header and ciphertext, not the (malleable) signature."""
    hb, body, _ = split(env)
    return hashlib.sha256(hb + body).digest()


# --- the host's checks (§6), in their normative order --------------------------------------------

def _drop(why):
    return {"result": "drop", "why": why}


def _refuse(code, **extra):
    return {"result": "refuse", "code": code, **extra}


def _unverified(state: dict, now_ms: int, code: str, **extra) -> dict:
    """A refusal before any registered signature verified: counted against ONE host-wide budget (never
    the claimed device id, which anyone can write), dropped once the budget is spent."""
    recent = [t for t in state.get("unverified", []) if now_ms - t < BUDGET_WINDOW_MS]
    if len(recent) >= BUDGET:
        state["unverified"] = recent
        return _drop("budget")
    state["unverified"] = recent + [now_ms]
    return _refuse(code, **extra)


def _record(state, h: Header, env: bytes, now_ms: int, outcome) -> None:
    """Persisted (on a real host) before the refusal is sent or the request runs."""
    state["rids"][h.rid.hex()] = {"device": h.device.hex(), "digest": digest(env).hex(), "outcome": outcome,
                                  "until": max(now_ms + RID_RETENTION_MS, h.ts_ms + RID_AFTER_TS_MS)}


def _recorded_refusal(state, h, env, now_ms, code, **extra) -> dict:
    _record(state, h, env, now_ms, {"refusal": code, **extra})
    return _refuse(code, **extra)


def seq_accept(dev: dict, seq: int) -> bool:
    """64-wide anti-replay window. bit i of dev["bitmap"] set <=> seq (high - i) was accepted."""
    high, bm = dev["high"], dev["bitmap"]
    if seq > high:
        shift = seq - high
        dev["bitmap"] = ((bm << shift) | 1) & ((1 << SEQ_WINDOW) - 1) if shift < SEQ_WINDOW else 1
        dev["high"] = seq
        return True
    i = high - seq
    if seq < 1 or i >= SEQ_WINDOW or bm >> i & 1:
        return False
    dev["bitmap"] = bm | 1 << i
    return True


def host_check(env: bytes, state: dict, now_ms: int, mailbox_id: str | None = None) -> dict:
    """What the host does with one request envelope. `state` (mutated) is the workspace's
    {"workspace", "k_ws", "key_version", "devices": {id hex: {pub, scope, revoked, high, bitmap}},
     "rids": {rid hex: {device, digest, outcome, until}}, "offers": {pairing id hex: {secret, scope, expires_ms}},
     "pending_pairs", "phones": {phone id: key hex}, "streams": {rid hex: device hex}, "unverified": [ms]}.
    `mailbox_id` is the cleartext id the TIX mailbox delivered the envelope under.

    Silence ("drop") for everything a non-holder of K_ws could have produced; a sealed, host-signed
    refusal for everything after the tag verified (the sender holds K_ws)."""
    # 1. shape: nothing here needs a key, so nothing here may answer
    if not OVERHEAD <= len(env) <= MAX_REQUEST:
        return _drop("size")
    hb, body, sig = split(env)
    h = Header.decode(hb)
    if h.magic != MAGIC or h.version != VERSION:
        return _drop("version")
    if h.direction != TO_HOST or h.flags & ~F_STREAM:
        return _drop("direction_or_flags")
    if h.key_version != state["key_version"] or h.workspace.hex() != state["workspace"]:
        return _drop("workspace")
    if mailbox_id is not None and mailbox_id != h.rid.hex():
        return _drop("mailbox_mismatch")
    # 2. the tag: from here on the sender holds K_ws
    try:
        pt = open_body(state["k_ws"], hb, body)
    except InvalidTag:
        return _drop("tag")
    # 3. who signed it
    did = h.device.hex()
    dev = state["devices"].get(did)
    if dev is None:
        try:
            meta, _ = unframe(pt)
        except ValueError:
            return _unverified(state, now_ms, "malformed")
        if meta.get("op") == "pair":
            return _pair(state, h, hb, body, sig, meta, now_ms)
        waiting = state.get("pending_pairs", {}).get(did)
        if meta.get("op") == "pair_status" and waiting is not None:
            if not verify(bytes.fromhex(waiting["pub"]), sig, signed_bytes(hb, body)):
                return _unverified(state, now_ms, "bad_signature")
            if abs(now_ms - h.ts_ms) > WINDOW_MS:
                return _unverified(state, now_ms, "stale_timestamp", host_ms=now_ms)
            return {"result": "pair_status", "state": waiting["state"]}     # read-only: a replay changes nothing
        return _unverified(state, now_ms, "not_paired")
    if dev.get("revoked"):
        return _unverified(state, now_ms, "revoked")
    if not verify(bytes.fromhex(dev["pub"]), sig, signed_bytes(hb, body)):
        return _unverified(state, now_ms, "bad_signature")
    # 4. a known request id: never runs twice, a stored refusal stays a refusal
    rid = h.rid.hex()
    known = state["rids"].get(rid)
    if known is not None and now_ms < known["until"]:
        if known["device"] == did and known["digest"] == digest(env).hex():
            out = known["outcome"]
            if out is None:                             # accepted, no outcome stored (running, or a crash)
                return _refuse("already_done", status="unknown")
            if "refusal" in out:
                return _refuse(out["refusal"], **{k: v for k, v in out.items() if k != "refusal"})
            return {"result": "replay", "outcome": out}
        return _refuse("rid_conflict")
    # 5. from here every refusal is recorded before it is sent
    try:
        meta, data = unframe(pt)
    except ValueError:
        return _recorded_refusal(state, h, env, now_ms, "malformed")
    if abs(now_ms - h.ts_ms) > WINDOW_MS:
        return _recorded_refusal(state, h, env, now_ms, "stale_timestamp", host_ms=now_ms)
    if not seq_accept(dev, h.seq):
        return _recorded_refusal(state, h, env, now_ms, "stale_sequence", high=dev["high"])
    streams = state.setdefault("streams", {})
    if h.stream != ZERO_ID and streams.get(h.stream.hex()) != did:
        return _recorded_refusal(state, h, env, now_ms, "forbidden_scope")    # not this device's stream
    # 6. recorded (and, on a real host, persisted) before anything runs
    _record(state, h, env, now_ms, None)
    if h.flags & F_STREAM:
        streams[rid] = did
    return {"result": "accept", "scope": dev["scope"], "meta": meta, "data": data.hex()}


def _pair(state, h, hb, body, sig, meta, now_ms):
    try:
        pid, pub, mac = meta["pairing_id"], bytes.fromhex(meta["pub"]), bytes.fromhex(meta["mac"])
        offer = state["offers"].get(pid)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _unverified(state, now_ms, "malformed")
    if offer is None or now_ms >= offer["expires_ms"] or offer.get("used"):
        return _unverified(state, now_ms, "pairing_closed")
    if device_id(h.workspace, pub) != h.device or not verify(pub, sig, signed_bytes(hb, body)):
        return _unverified(state, now_ms, "bad_signature")
    want = pair_mac(bytes.fromhex(offer["secret"]), h.workspace, bytes.fromhex(pid), pub)
    if not hmac.compare_digest(want, mac):
        return _unverified(state, now_ms, "pairing_closed")      # the same answer as no offer: the link is the secret
    if abs(now_ms - h.ts_ms) > WINDOW_MS:
        return _unverified(state, now_ms, "stale_timestamp", host_ms=now_ms)
    offer["used"] = True
    link = None                                    # a phone link is recorded only with its proof (§8.2)
    phone_key = state.get("phones", {}).get(str(meta.get("phone_id", "")))
    if phone_key is not None:
        try:
            proof = bytes.fromhex(meta.get("phone_proof", ""))
        except (TypeError, ValueError):
            proof = b""
        if hmac.compare_digest(phone_link_proof(bytes.fromhex(phone_key), h.device), proof):
            link = meta["phone_id"]
    state.setdefault("pending_pairs", {})[h.device.hex()] = {"pub": pub.hex(), "state": "pending",
                                                             "scope": offer["scope"], "phone_link": link,
                                                             "label": clean_shown(str(meta.get("label", "")))[:80]}
    return {"result": "pair_pending", "fingerprint": device_fingerprint(pub), "device": h.device.hex(),
            "scope": offer["scope"], "phone_link": link}


# --- the device's checks on a response chunk (§7) ------------------------------------------------

def device_check(env: bytes, ctx: dict, mailbox: dict, now_ms: int) -> dict:
    """ctx: {"workspace", "k_ws", "key_version", "device", "host_pub", "pending": {rid hex: {next, stream}},
    "offset_ms"}. mailbox: the cleartext {id, idx, last, stream} the TIX server delivered the chunk with.
    Anything wrong is dropped and never rendered; a missing chunk fails the request on its timeout."""
    if not OVERHEAD <= len(env) <= MAX_CHUNK:
        return _drop("size")
    hb, body, sig = split(env)
    h = Header.decode(hb)
    if h.magic != MAGIC or h.version != VERSION or h.direction != TO_DEVICE or h.flags & ~(F_LAST | F_STREAM | F_REFUSAL):
        return _drop("version_direction_or_flags")
    if h.flags & F_REFUSAL and not h.flags & F_LAST:
        return _drop("flags")
    if h.key_version != ctx["key_version"] or h.workspace.hex() != ctx["workspace"] or h.device.hex() != ctx["device"]:
        return _drop("not_for_this_device")
    pend = ctx["pending"].get(h.rid.hex())
    if pend is None:
        return _drop("unknown_request")
    if (mailbox["id"], mailbox["idx"], mailbox["last"], mailbox["stream"]) != \
            (h.rid.hex(), h.seq, bool(h.flags & F_LAST), bool(h.flags & F_STREAM)) or pend["stream"] != mailbox["stream"]:
        return _drop("mailbox_mismatch")
    if not verify(ctx["host_pub"], sig, signed_bytes(hb, body)):
        return _drop("host_signature")
    try:
        meta, data = unframe(open_body(ctx["k_ws"], hb, body))
    except (InvalidTag, ValueError):
        return _drop("tag")
    if h.seq != pend["next"]:
        return _drop("out_of_order")
    res = {"result": "accept", "last": bool(h.flags & F_LAST), "refusal": bool(h.flags & F_REFUSAL),
           "meta": meta, "data": data.hex()}
    if h.flags & F_REFUSAL and meta.get("refusal") == "stale_timestamp" and type(meta.get("host_ms")) is int:
        # the host says this device's clock is off: never dropped for that clock (§5.1)
        res["offset_ms"] = meta["host_ms"] - now_ms
    elif abs(now_ms + ctx.get("offset_ms", 0) - h.ts_ms) > WINDOW_MS:
        return _drop("stale_timestamp")
    pend["next"] += 1
    return res


# --- the platform-authenticator binding (§9) ------------------------------------------------------

_BIDI = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def clean_shown(s: str) -> str:
    """The host's `shown` text (§9.3): Unicode scalar values only (a lone surrogate is an error), C0 except
    LF, DEL, C1 and bidi formatting characters removed, no normalisation."""
    if any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        raise ValueError("not Unicode scalar values")
    return "".join(c for c in s if not ((ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F or ord(c) in _BIDI))


def subject_hash(subject: dict) -> bytes:
    return hashlib.sha256(canonical_json(subject)).digest()


def assertion_challenge(workspace: bytes, device: bytes, rid: bytes, purpose: str, scope: str,
                        expires_ms: int, nonce: bytes, subject: dict) -> bytes:
    assert len(nonce) == 32
    return hashlib.sha256(L_ASSERT + workspace + device + rid + bytes([PURPOSES[purpose], SCOPES[scope]])
                          + struct.pack(">Q", expires_ms) + nonce + subject_hash(subject)).digest()


def registration_challenge(workspace: bytes, device: bytes, expires_ms: int, nonce: bytes) -> bytes:
    return hashlib.sha256(L_REG + workspace + device + struct.pack(">Q", expires_ms) + nonce).digest()


def verify_assertion(cred: dict, pending: dict, sender_device: str, a: dict, now_ms: int) -> dict:
    """cred: {device, credential_id, pub (65 bytes hex), sign_count, be, rp_id, origin}; pending: {challenge
    hex: {device, expires_ms}} (single use, mutated); a: {credential_id, authenticator_data,
    client_data_json, signature (DER)} hex."""
    try:
        ad, cdj, sig = (bytes.fromhex(a[k]) for k in ("authenticator_data", "client_data_json", "signature"))
        client = json.loads(cdj)
        ch = unb64u(client["challenge"]).hex()
    except (KeyError, TypeError, ValueError):
        return _refuse("assertion_failed", why="malformed")
    issued = pending.pop(ch, None)                               # single use, whatever happens next
    if issued is None:
        return _refuse("assertion_failed", why="unknown_or_used_challenge")
    if now_ms >= issued["expires_ms"]:
        return _refuse("assertion_failed", why="expired")
    if issued["device"] != sender_device or cred["device"] != sender_device:
        return _refuse("assertion_failed", why="other_device")
    if a.get("credential_id") != cred["credential_id"]:
        return _refuse("assertion_failed", why="other_credential")
    if client.get("type") != "webauthn.get" or client.get("origin") != cred["origin"] or client.get("crossOrigin"):
        return _refuse("assertion_failed", why="client_data")
    if len(ad) < 37 or ad[:32] != hashlib.sha256(cred["rp_id"].encode()).digest():
        return _refuse("assertion_failed", why="rp_id")
    if ad[32] & (AD_UP | AD_UV) != AD_UP | AD_UV:
        return _refuse("assertion_failed", why="user_not_verified")
    if bool(ad[32] & AD_BE) != cred["be"]:
        return _refuse("assertion_failed", why="backup_eligibility_changed")
    try:
        load_public(bytes.fromhex(cred["pub"])).verify(sig, ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return _refuse("assertion_failed", why="signature")
    count = struct.unpack(">I", ad[33:37])[0]
    if (count or cred["sign_count"]) and count <= cred["sign_count"]:
        if not cred["be"]:
            return _refuse("assertion_failed", why="sign_count")          # this assertion only; shown, not suspended
        return {"result": "verified", "counter_warning": True}          # synced: the counter is advisory
    cred["sign_count"] = count
    return {"result": "verified"}


# --- the vector file ------------------------------------------------------------------------------

VECTORS = Path(__file__).resolve().parents[1] / "bridge_vectors.json"


def fake(label: str) -> bytes:
    """Deterministic FAKE material: SHA-256 of a label. Never a real key."""
    return hashlib.sha256(b"FAKE bridge test vector: " + label.encode()).digest()


def _keys():
    k = {}
    for name in ("device_a", "device_b", "host", "intruder", "authenticator"):
        priv = private_key(fake(name))
        k[name] = {"d": fake(name).hex(), "pub": public_bytes(priv).hex()}
    return k


def build() -> dict:
    keys = _keys()
    pk = {n: private_key(bytes.fromhex(v["d"])) for n, v in keys.items()}
    pub = {n: bytes.fromhex(v["pub"]) for n, v in keys.items()}
    mk = fake("mk")
    ws_hex = fake("workspace")[:16].hex()
    ws = bytes.fromhex(ws_hex)
    k_ws = workspace_key(mk, ws_hex)
    dev_a, dev_b = device_id(ws, pub["device_a"]), device_id(ws, pub["device_b"])
    now = 1_790_000_000_000
    out = {
        "comment": "Bridge protocol v1 test vectors (docs/bridge-protocol.md). Every key here is FAKE: "
                   "SHA-256 of a public label. Regenerate with: uv run python -m tests.support.bridge_protocol_ref",
        "version": VERSION,
        "constants": {"magic": MAGIC.decode(), "header_len": HEADER_LEN, "tag_len": TAG_LEN, "sig_len": SIG_LEN,
                      "max_request": MAX_REQUEST, "max_chunk": MAX_CHUNK, "max_meta": MAX_META,
                      "window_ms": WINDOW_MS, "seq_window": SEQ_WINDOW, "rid_retention_ms": RID_RETENTION_MS,
                      "rid_after_ts_ms": RID_AFTER_TS_MS, "budget": BUDGET, "budget_window_ms": BUDGET_WINDOW_MS,
                      "labels": {n: v.decode() for n, v in (("ws", L_WS), ("msg", L_MSG), ("sig", L_SIG),
                                 ("device", L_DEVICE), ("fp", L_FP), ("host", L_HOST), ("pair", L_PAIR),
                                 ("phone_link", L_PHONE), ("assert", L_ASSERT), ("webauthn_reg", L_REG))}},
        "keys": {"mk": mk.hex(), "workspace": ws_hex, **keys},
    }

    # HKDF (§2.2)
    out["hkdf"] = [
        {"name": "workspace_key", "ikm": mk.hex(), "salt": "", "info": (L_WS + ws_hex.encode()).hex(),
         "okm": k_ws.hex()},
        {"name": "workspace_key_other_workspace", "ikm": mk.hex(), "salt": "",
         "info": (L_WS + b"0" * 32).hex(), "okm": workspace_key(mk, "0" * 32).hex()},
        {"name": "message_key", "ikm": k_ws.hex(), "salt": fake("salt-1")[:16].hex(), "info": L_MSG.hex(),
         "okm": message_key(k_ws, fake("salt-1")[:16]).hex()},
    ]
    out["ids"] = {n: {"pub": keys[n]["pub"], "device_id": device_id(ws, pub[n]).hex(),
                      "device_id_other_workspace": device_id(bytes(16), pub[n]).hex(),
                      "fingerprint": device_fingerprint(pub[n])} for n in ("device_a", "device_b")}
    out["ids"]["host_pin"] = host_pin(pub["host"]).hex()

    def hdr(direction=TO_HOST, flags=0, device=dev_a, rid=None, stream=ZERO_ID, seq=1, ts=now, salt=None, **kw):
        return Header(direction, flags, ws, device, rid or fake(f"rid-{seq}")[:16], stream, seq, ts,
                      salt or fake(f"salt-{direction}-{seq}-{ts}")[:16], **kw)

    # seal / open (§3.3)
    h = hdr()
    pt = frame({"op": "http", "method": "GET", "path": "/api/status"})
    out["seal"] = [{"name": "request_body", "k_ws": k_ws.hex(), "header": h.encode().hex(), "header_fields": h.as_json(),
                    "message_key": message_key(k_ws, h.salt).hex(), "nonce": ZERO_NONCE.hex(),
                    "plaintext": pt.hex(), "sealed": seal_body(k_ws, h.encode(), pt).hex()}]
    # sign / verify (§3.4)
    msg = signed_bytes(h.encode(), bytes.fromhex(out["seal"][0]["sealed"]))
    sig = sign(pk["device_a"], msg)
    s_int = int.from_bytes(sig[32:], "big")
    nb = P256_N.to_bytes(32, "big")
    out["sign"] = [
        {"name": "device_a_signs", "pub": keys["device_a"]["pub"], "msg": msg.hex(), "sig": sig.hex(), "valid": True},
        {"name": "same_sig_other_key", "pub": keys["device_b"]["pub"], "msg": msg.hex(), "sig": sig.hex(), "valid": False},
        {"name": "one_bit_of_msg", "pub": keys["device_a"]["pub"], "msg": (msg[:-1] + bytes([msg[-1] ^ 1])).hex(),
         "sig": sig.hex(), "valid": False},
        {"name": "high_s_twin", "pub": keys["device_a"]["pub"], "msg": msg.hex(),
         "sig": (sig[:32] + (P256_N - s_int).to_bytes(32, "big")).hex(), "valid": True},
        {"name": "zero_r", "pub": keys["device_a"]["pub"], "msg": msg.hex(), "sig": (bytes(32) + sig[32:]).hex(),
         "valid": False},
        {"name": "r_equals_n", "pub": keys["device_a"]["pub"], "msg": msg.hex(), "sig": (nb + sig[32:]).hex(),
         "valid": False},
        {"name": "s_equals_n", "pub": keys["device_a"]["pub"], "msg": msg.hex(), "sig": (sig[:32] + nb).hex(),
         "valid": False},
        {"name": "short_signature", "pub": keys["device_a"]["pub"], "msg": msg.hex(), "sig": sig[:63].hex(),
         "valid": False},
    ]

    out["sig_scalars"] = [{"name": n, "sig": v.hex(), "in_range": ok} for n, v, ok in (
        ("valid", sig, True), ("r_zero", bytes(32) + sig[32:], False), ("r_equals_n", nb + sig[32:], False),
        ("r_n_plus_one", (P256_N + 1).to_bytes(32, "big") + sig[32:], False), ("s_equals_n", sig[:32] + nb, False),
        ("r_n_minus_one", (P256_N - 1).to_bytes(32, "big") + sig[32:], True), ("short", sig[:63], False))]

    def state():
        return {"workspace": ws_hex, "k_ws": k_ws, "key_version": 1,
                "devices": {dev_a.hex(): {"pub": keys["device_a"]["pub"], "scope": "operate", "revoked": False,
                                          "high": 0, "bitmap": 0},
                            dev_b.hex(): {"pub": keys["device_b"]["pub"], "scope": "look", "revoked": False,
                                          "high": 0, "bitmap": 0}},
                "rids": {}, "offers": {}, "pending_pairs": {}, "phones": {}, "streams": {}, "unverified": []}

    def jstate(s):
        return {k: v for k, v in s.items() if k != "k_ws"}

    def req(signer="device_a", meta=None, data=b"", raw_pt=None, **kw):
        pt = raw_pt if raw_pt is not None else frame(meta or {"op": "http", "method": "GET", "path": "/"}, data)
        return envelope(k_ws, pk[signer], hdr(**kw), pt)

    full = req(meta={"op": "http", "method": "POST", "path": "/api/tickets/T-1/move",
                     "headers": {"content-type": "application/json"}}, data=b'{"to":"testing"}', seq=7)
    host_cases = []
    keep = ("result", "code", "scope", "meta", "data", "outcome", "high", "fingerprint", "state", "status",
            "phone_link", "host_ms")

    def case(name, env, st, now_ms, mutate=None, mailbox_id=None):
        s = st if st is not None else state()
        if mutate:
            mutate(s)
        before = json.loads(json.dumps(jstate(s)))
        mb = mailbox_id or (Header.decode(env).rid.hex() if len(env) >= HEADER_LEN else "")
        res = host_check(env, s, now_ms, mb)
        # an all-zero envelope is written as its length, so the file stays small
        wire = {"envelope_zeros": len(env)} if not any(env) else {"envelope": env.hex()}
        host_cases.append({"name": name, **wire, "mailbox_id": mb, "state": before, "now_ms": now_ms,
                           "expect": {k: v for k, v in res.items() if k in keep}})
        return s

    def chain(name, steps, mutate=None):
        """Several envelopes against ONE state, from its initial value: what a step leaves behind is part
        of what the vector checks (a mutant that forgets to record a refusal fails the next step)."""
        s = state()
        if mutate:
            mutate(s)
        before = json.loads(json.dumps(jstate(s)))
        out_steps = []
        for env, now_ms in steps:
            mb = Header.decode(env).rid.hex()
            res = host_check(env, s, now_ms, mb)
            out_steps.append({"envelope": env.hex(), "mailbox_id": mb, "now_ms": now_ms,
                              "expect": {k: v for k, v in res.items() if k in keep}})
        host_cases.append({"name": name, "state": before, "steps": out_steps})

    def da(**kw):
        return lambda s: s["devices"][dev_a.hex()].update(**kw)

    case("full_request_accepted", full, None, now + 1500)
    other = req(meta={"op": "http", "method": "POST", "path": "/api/tickets/T-1/move"}, data=b'{"to":"done"}', seq=8,
                rid=fake("rid-7")[:16])
    chain("replayed_request", [(full, now + 1500), (full, now + 60_000), (full, now + RID_RETENTION_MS + 1499),
                               (full, now + RID_RETENTION_MS + 1500)])
    chain("same_rid_other_content", [(full, now + 1500), (other, now + 61_000)])
    done = case("replay_of_finished_request", full, None, now + 1500)
    done["rids"][fake("rid-7")[:16].hex()]["outcome"] = {"status": 200}
    case("replay_of_finished_request", full, done, now + 30_000)
    host_cases.pop(-2)
    case("replay_from_other_device_record", full, None, now + 1500,
         lambda s: s["rids"].update({fake("rid-7")[:16].hex(): {"device": dev_b.hex(), "digest": digest(full).hex(),
                                                                "outcome": {"status": 200}, "until": now + 900_000}}))
    case("old_timestamp", req(seq=2, ts=now - WINDOW_MS - 1), None, now)
    case("future_timestamp", req(seq=2, ts=now + WINDOW_MS + 1), None, now)
    fut = req(seq=2, ts=now + WINDOW_MS + 1)
    chain("future_timestamp_then_in_window", [(fut, now), (fut, now + 2000), (fut, now + RID_RETENTION_MS - 1)])
    case("timestamp_at_window_edge", req(seq=2, ts=now - WINDOW_MS), None, now)
    case("future_timestamp_at_window_edge", req(seq=2, ts=now + WINDOW_MS), None, now)
    bad_tag = bytearray(full)
    bad_tag[-SIG_LEN - 1] ^= 0x01
    case("tampered_tag", bytes(bad_tag), None, now)
    bad_hdr = bytearray(full)
    bad_hdr[72 + 7] ^= 0x01                        # seq, inside the AAD
    case("tampered_header_field", bytes(bad_hdr), None, now)
    case("mailbox_id_differs_from_rid", full, None, now + 1500, mailbox_id=fake("other-rid")[:16].hex())
    case("wrong_device_signature", req(signer="device_b", seq=3), None, now)
    case("intruder_signature_unknown_device", req(signer="intruder", device=device_id(ws, pub["intruder"]), seq=3),
         None, now)
    case("refusal_budget_spent_drops_unverified", req(signer="device_b", seq=3), None, now,
         lambda s: s.update(unverified=[now - i for i in range(BUDGET)]))
    case("refusal_budget_spent_signed_request_runs", req(seq=3), None, now,
         lambda s: s.update(unverified=[now - i for i in range(BUDGET)]))
    case("unknown_version", full[:4] + bytes([2]) + full[5:], None, now)
    case("response_sent_to_host", req(direction=TO_DEVICE, seq=4), None, now)
    case("unknown_flag", req(flags=0x08, seq=4), None, now)
    case("other_workspace_key", envelope(workspace_key(mk, "0" * 32), pk["device_a"], hdr(seq=5), frame({"op": "x"})),
         None, now)
    case("revoked_device", req(seq=5), None, now, da(revoked=True))
    big = canonical_json({"op": "http", "pad": "x" * MAX_META})
    case("meta_longer_than_64_kib", req(seq=5, raw_pt=struct.pack(">I", len(big)) + big), None, now)
    case("sequence_zero", req(seq=0, rid=fake("rid-0")[:16]), None, now)
    case("repeated_sequence", req(seq=9, rid=fake("rid-9b")[:16]), None, now, da(high=9, bitmap=1))
    late = req(seq=9, rid=fake("rid-9c")[:16])
    chain("repeated_sequence_then_replayed", [(late, now), (late, now + 1000)], da(high=9, bitmap=1))
    # the replay gets the STORED refusal (high = 110), not a fresh one (high = 111)
    chain("sequence_refused_then_same_bytes_after_window_moved",
          [(late, now), (req(seq=111, rid=fake("rid-111")[:16]), now + 1), (late, now + 2)], da(high=110, bitmap=1))
    case("sequence_below_window", req(seq=30), None, now, da(high=94, bitmap=1))
    case("sequence_reordered_inside_window", req(seq=90), None, now, da(high=94, bitmap=1))
    chain("window_slides_then_old_top_repeats", [(req(seq=6), now), (req(seq=5, rid=fake("rid-5b")[:16]), now + 10)],
          da(high=5, bitmap=1))
    chain("window_slides_far_then_old_seq", [(req(seq=70), now), (req(seq=7, rid=fake("rid-7b")[:16]), now + 10),
                                             (req(seq=69, rid=fake("rid-69")[:16]), now + 20),
                                             (req(seq=69, rid=fake("rid-69b")[:16]), now + 30)], da(high=6, bitmap=1))
    case("oversize", b"\x00" * (MAX_REQUEST + 1), None, now)

    # streams belong to the device that opened them (§4)
    term = fake("terminal-stream")[:16]

    def owned(owner):
        return lambda s: s["streams"].update({term.hex(): owner.hex()})

    keys_meta = {"op": "http", "method": "POST", "path": "/terminal/input"}
    case("input_to_own_stream", req(meta=keys_meta, data=b"ls\n", stream=term, seq=11), None, now, owned(dev_a))
    case("input_to_other_devices_stream", req(meta=keys_meta, data=b"ls\n", stream=term, seq=11), None, now,
         owned(dev_b))
    case("cancel_of_other_devices_stream", req(meta={"op": "cancel"}, stream=term, seq=12), None, now, owned(dev_b))

    # pairing (§8)
    pid, secret = fake("pairing-id")[:16], fake("pairing-secret")
    new_pub = pub["intruder"]               # a fresh, not yet registered device key
    new_id = device_id(ws, new_pub)
    good_mac = pair_mac(secret, ws, pid, new_pub)
    phone_id, phone_key = "ph_0123456789ab", fake("phone-pairing-key")

    def offer(s):
        s["offers"][pid.hex()] = {"secret": secret.hex(), "scope": "decide", "expires_ms": now + 600_000}
        s["phones"][phone_id] = phone_key.hex()

    pair_meta = {"op": "pair", "pairing_id": pid.hex(), "pub": new_pub.hex(), "mac": good_mac.hex(), "label": "Phone"}

    def pair(meta=pair_meta, device=new_id, signer="intruder", **kw):
        return req(signer=signer, device=device, meta=meta, **kw)

    case("pair_request_bad_mac", pair({**pair_meta, "mac": fake("wrong")[:32].hex()}), None, now, offer)
    case("pair_request_no_offer", pair(), None, now)
    case("pair_request_stale_timestamp", pair(ts=now - WINDOW_MS - 1), None, now, offer)
    case("pair_request_device_id_not_of_pub", pair(device=fake("unregistered-id")[:16]), None, now, offer)
    case("pair_request_with_phone_link", pair({**pair_meta, "phone_id": phone_id,
                                              "phone_proof": phone_link_proof(phone_key, new_id).hex()}), None, now, offer)
    case("pair_request_phone_link_without_proof", pair({**pair_meta, "phone_id": phone_id,
                                                       "phone_proof": fake("no-proof").hex()}), None, now, offer)
    status = {"op": "pair_status", "pairing_id": pid.hex()}
    chain("pair_request_then_status", [(pair(), now), (pair(seq=2), now + 1000), (pair(status, seq=3), now + 3000),
                                       (pair(status, signer="device_b", seq=4), now + 3000)], offer)
    case("pair_status_without_pairing", pair(status, seq=3), None, now)
    out["host_cases"] = host_cases
    out["pairing"] = {"workspace": ws_hex, "pairing_id": pid.hex(), "secret": secret.hex(),
                      "host_pub": keys["host"]["pub"], "host_pin": host_pin(pub["host"]).hex(),
                      "link_fragment": f"v1.{ws_hex}.{pid.hex()}.{b64u(secret)}.{b64u(host_pin(pub['host']))}",
                      "device_pub": new_pub.hex(), "device_id": new_id.hex(), "mac": good_mac.hex(),
                      "device_fingerprint": device_fingerprint(new_pub), "phone_id": phone_id,
                      "phone_key": phone_key.hex(), "phone_proof": phone_link_proof(phone_key, new_id).hex()}

    # responses (§7)
    rid = fake("rid-7")[:16]

    def resp(signer="host", seq=0, flags=F_LAST, meta=None, data=b"", device=dev_a, ts=now + 2000, key=k_ws, r=rid):
        h = hdr(direction=TO_DEVICE, flags=flags, device=device, rid=r, seq=seq, ts=ts)
        return envelope(key, pk[signer], h, frame(meta if meta is not None else {"status": 200,
                                                  "headers": {"content-type": "application/json"}}, data))

    def ctx(offset=0):
        return {"workspace": ws_hex, "k_ws": k_ws, "key_version": 1, "device": dev_a.hex(), "host_pub": pub["host"],
                "pending": {rid.hex(): {"next": 0, "stream": False}}, "offset_ms": offset}

    dev_cases = []

    def dcase(name, env, now_ms=now + 2500, mailbox=None, offset=0):
        c = ctx(offset)
        hh = Header.decode(env)
        mb = mailbox or {"id": hh.rid.hex(), "idx": hh.seq, "last": bool(hh.flags & F_LAST),
                         "stream": bool(hh.flags & F_STREAM)}
        pend_before = json.loads(json.dumps(c["pending"]))
        res = device_check(env, c, mb, now_ms)
        dev_cases.append({"name": name, "envelope": env.hex(), "mailbox": mb, "pending": pend_before,
                          "offset_ms": offset, "now_ms": now_ms,
                          "expect": {k: v for k, v in res.items() if k in ("result", "last", "refusal", "meta", "data",
                                                                           "offset_ms")}})

    ok = resp(data=b'{"moved":true}')
    dcase("full_response_chunk", ok)
    dcase("response_signed_by_a_device", resp(signer="device_a"))
    dcase("response_for_another_device", resp(device=dev_b))
    dcase("response_for_unknown_request", resp(r=fake("not-pending")[:16]))
    t = bytearray(ok)
    t[-SIG_LEN - 1] ^= 0x01
    dcase("response_tampered_tag", bytes(t))
    dcase("response_old_timestamp", ok, now_ms=now + 2000 + WINDOW_MS + 1)
    dcase("response_old_timestamp_corrected_by_offset", ok, now_ms=now + 2000 + WINDOW_MS + 1, offset=-400_000)
    dcase("response_out_of_order", resp(seq=1, flags=0))
    dcase("response_mailbox_says_not_last", ok, mailbox={"id": rid.hex(), "idx": 0, "last": False, "stream": False})
    dcase("refusal_chunk", resp(flags=F_LAST | F_REFUSAL, meta={"refusal": "stale_sequence", "high": 9}))
    dcase("refusal_without_last", resp(flags=F_REFUSAL, meta={"refusal": "x"}))
    skew = now + 2000 + 400_000                       # the device's clock runs 400 s fast
    dcase("stale_timestamp_refusal_to_a_skewed_clock",
          resp(flags=F_LAST | F_REFUSAL, meta={"refusal": "stale_timestamp", "host_ms": now + 2000}), now_ms=skew)
    out["device_cases"] = dev_cases

    # the `shown` text (§9.3)
    out["shown"] = [
        {"name": "controls_and_bidi_removed",
         "input": [ord(c) for c in "Run ‮rm -rf‬\u0007 ok\u0085\n⁧x⁩é"],
         "expect": "Run rm -rf ok\nxé"},
        {"name": "no_normalisation", "input": [0x65, 0x301], "expect": "é"},
        {"name": "lone_surrogate", "input": [0x41, 0xD800], "expect": None},
    ]

    # assertion binding (§9)
    subject = {"kind": "charter", "shown": "Start epic E-12: Migrate jobs", "digest": hashlib.sha256(b"FAKE charter").hexdigest()}
    nonce, expires = fake("assert-nonce"), now + 120_000
    ch = assertion_challenge(ws, dev_a, rid, "fresh", "type", expires, nonce, subject)
    rp_id, origin = "tix.example", "https://tix.example"
    cred_id = fake("credential-id")[:16].hex()

    def assertion(count=1, flags=AD_UP | AD_UV, challenge=ch, org=origin, signer="authenticator", typ="webauthn.get",
                  cross=False, rp=rp_id, cid=cred_id):
        ad = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
        cdj = canonical_json({"type": typ, "challenge": b64u(challenge), "origin": org, "crossOrigin": cross})
        der = pk[signer].sign(ad + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
        return {"credential_id": cid, "authenticator_data": ad.hex(), "client_data_json": cdj.hex(), "signature": der.hex()}

    def cred(count=0, be=False):
        return {"device": dev_a.hex(), "credential_id": cred_id, "pub": keys["authenticator"]["pub"],
                "sign_count": count, "be": be, "rp_id": rp_id, "origin": origin}

    acases = []

    def acase(name, a, c=None, sender=dev_a.hex(), now_ms=now + 5000, pending=None):
        c = c or cred()
        p = pending if pending is not None else {ch.hex(): {"device": dev_a.hex(), "expires_ms": expires}}
        acases.append({"name": name, "credential": dict(c), "pending": json.loads(json.dumps(p)), "sender": sender,
                       "now_ms": now_ms, "assertion": a, "expect": verify_assertion(c, p, sender, a, now_ms)})

    good = assertion()
    acase("fresh_assertion_verified", good)
    acase("replayed_assertion", good, pending={})
    acase("sign_count_not_increased", assertion(count=5), c=cred(5))
    acase("synced_counter_not_increased_is_advisory", assertion(count=5, flags=AD_UP | AD_UV | AD_BE), c=cred(5, be=True))
    acase("synced_passkey_zero_count", assertion(count=0))
    acase("user_not_verified", assertion(flags=AD_UP))
    acase("other_origin", assertion(org="https://other.example"))
    acase("cross_origin", assertion(cross=True))
    acase("create_instead_of_get", assertion(typ="webauthn.create"))
    acase("other_rp_id", assertion(rp="other.example"))
    acase("other_credential_id", assertion(cid=fake("other-credential")[:16].hex()))
    acase("backup_eligibility_changed", assertion(flags=AD_UP | AD_UV | AD_BE))
    acase("signed_by_another_authenticator", assertion(signer="intruder"))
    acase("expired_challenge", good, now_ms=expires)
    acase("sent_by_other_device", good, sender=dev_b.hex())
    acase("challenge_issued_to_other_device", good,
          pending={ch.hex(): {"device": dev_b.hex(), "expires_ms": expires}})
    acase("other_subject", assertion(challenge=assertion_challenge(ws, dev_a, rid, "fresh", "type", expires, nonce,
                                                                   {**subject, "shown": "Start epic E-13"})))
    out["assertion"] = {
        "challenge_inputs": {"workspace": ws_hex, "device": dev_a.hex(), "rid": rid.hex(), "purpose": "fresh",
                             "scope": "type", "expires_ms": expires, "nonce": nonce.hex(), "subject": subject,
                             "subject_json": canonical_json(subject).decode(),
                             "subject_hash": subject_hash(subject).hex()},
        "challenge": ch.hex(),
        "lease_challenge": assertion_challenge(ws, dev_a, rid, "lease", "type", expires, nonce,
                                               {"kind": "lease", "shown": "Type for 15 minutes", "digest": ""}).hex(),
        "registration_challenge": {"workspace": ws_hex, "device": dev_a.hex(), "expires_ms": expires,
                                   "nonce": nonce.hex(),
                                   "challenge": registration_challenge(ws, dev_a, expires, nonce).hex()},
        "cases": acases,
    }
    return out


def render(vectors: dict) -> str:
    return json.dumps(vectors, indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    VECTORS.write_text(render(build()), encoding="utf-8")
    print(f"wrote {VECTORS}")
