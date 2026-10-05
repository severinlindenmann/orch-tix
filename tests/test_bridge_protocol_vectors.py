"""docs/bridge-protocol.md: the vectors in tests/bridge_vectors.json are the contract between the host
(orch-core R3) and the browser (TIX R10). These tests prove the reference implementation reproduces
every vector and rejects every negative case, and check the primitives against independent code."""
import hashlib
import hmac
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tests.support import bridge_protocol_ref as ref

VEC = json.loads(ref.VECTORS.read_text(encoding="utf-8"))
K_WS = bytes.fromhex(VEC["hkdf"][0]["okm"])
PUB = {n: bytes.fromhex(VEC["keys"][n]["pub"]) for n in ("device_a", "device_b", "host", "intruder", "authenticator")}


def by_name(cases):
    return pytest.mark.parametrize("c", cases, ids=lambda c: c["name"])


def test_vector_file_is_up_to_date():
    assert ref.render(ref.build()) == ref.VECTORS.read_text(encoding="utf-8"), \
        "run: uv run python -m tests.support.bridge_protocol_ref"


def test_keys_are_labelled_fake():
    assert "FAKE" in VEC["comment"]
    for name in ("device_a", "device_b", "host", "intruder", "authenticator"):
        assert VEC["keys"][name]["d"] == ref.fake(name).hex()
    assert VEC["keys"]["mk"] == ref.fake("mk").hex()


def rfc5869(ikm: bytes, salt: bytes, info: bytes, n: int = 32) -> bytes:
    """Independent of the `cryptography` HKDF the reference uses."""
    prk = hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 1
    while len(okm) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm, i = okm + t, i + 1
    return okm[:n]


@by_name(VEC["hkdf"])
def test_hkdf(c):
    ikm, salt, info = (bytes.fromhex(c[k]) for k in ("ikm", "salt", "info"))
    assert rfc5869(ikm, salt, info).hex() == c["okm"]
    assert ref.hkdf(ikm, salt, info).hex() == c["okm"]


def test_workspace_key_is_the_documented_derivation():
    assert ref.workspace_key(bytes.fromhex(VEC["keys"]["mk"]), VEC["keys"]["workspace"]) == K_WS
    assert VEC["hkdf"][0]["info"] == (b"sharing/bridge/ws/v1|" + VEC["keys"]["workspace"].encode()).hex()
    assert VEC["hkdf"][1]["okm"] != VEC["hkdf"][0]["okm"]


@by_name(VEC["seal"])
def test_seal_open(c):
    hb, pt = bytes.fromhex(c["header"]), bytes.fromhex(c["plaintext"])
    assert ref.Header.decode(hb).as_json() == c["header_fields"]
    assert ref.Header.decode(hb).encode() == hb and len(hb) == ref.HEADER_LEN
    # independent: HKDF by hand, AES-256-GCM with a zero nonce and the header as AAD
    k_msg = rfc5869(K_WS, bytes.fromhex(c["header_fields"]["salt"]), b"sharing/bridge/msg/v1")
    assert k_msg.hex() == c["message_key"]
    assert AESGCM(k_msg).encrypt(bytes(12), pt, hb).hex() == c["sealed"]
    assert ref.seal_body(K_WS, hb, pt).hex() == c["sealed"]
    assert ref.open_body(K_WS, hb, bytes.fromhex(c["sealed"])) == pt


@by_name(VEC["sign"])
def test_sign_verify(c):
    pub, msg, sig = (bytes.fromhex(c[k]) for k in ("pub", "msg", "sig"))
    assert ref.verify(pub, sig, msg) is c["valid"]


@by_name(VEC["sig_scalars"])
def test_signature_scalars_in_range(c):
    assert ref.scalars_in_range(bytes.fromhex(c["sig"])) is c["in_range"]


def test_signatures_are_deterministic_and_raw():
    c = VEC["sign"][0]
    priv = ref.private_key(bytes.fromhex(VEC["keys"]["device_a"]["d"]))
    assert ref.sign(priv, bytes.fromhex(c["msg"])).hex() == c["sig"] and len(bytes.fromhex(c["sig"])) == 64


def test_ids():
    for name in ("device_a", "device_b"):
        ids = VEC["ids"][name]
        ws = bytes.fromhex(VEC["keys"]["workspace"])
        assert ref.device_id(ws, PUB[name]).hex() == ids["device_id"]
        assert ref.device_id(bytes(16), PUB[name]).hex() == ids["device_id_other_workspace"] != ids["device_id"]
        assert ref.device_fingerprint(PUB[name]) == ids["fingerprint"]
        assert len(ids["fingerprint"]) == 24 and ids["fingerprint"].count("-") == 4
    assert ref.host_pin(PUB["host"]).hex() == VEC["ids"]["host_pin"]


def host_state(c):
    s = json.loads(json.dumps(c["state"]))
    s["k_ws"] = K_WS
    return s


def envelope_of(c):
    return bytes(c["envelope_zeros"]) if "envelope_zeros" in c else bytes.fromhex(c["envelope"])


def steps_of(c):
    """A single case, or a chain of steps run in order against one state."""
    return c["steps"] if "steps" in c else [c]


SINGLE = [c for c in VEC["host_cases"] if "steps" not in c]


@by_name(VEC["host_cases"])
def test_host_case(c):
    s = host_state(c)
    for i, st in enumerate(steps_of(c)):
        res = ref.host_check(envelope_of(st), s, st["now_ms"], st["mailbox_id"])
        assert {k: v for k, v in res.items() if k in st["expect"]} == st["expect"], f"step {i}"
        assert res["result"] == st["expect"]["result"], f"step {i}"


def test_every_refusal_after_the_signature_is_recorded():
    """A refused request can never run later: the refusal is the rid's outcome (finding 1)."""
    for c in SINGLE:
        s = host_state(c)
        res = ref.host_check(envelope_of(c), s, c["now_ms"], c["mailbox_id"])
        if res.get("code") in ("malformed", "stale_timestamp", "stale_sequence", "forbidden_scope") \
                and c["expect"]["result"] == "refuse" and not c["name"].startswith("pair_"):
            again = ref.host_check(envelope_of(c), s, c["now_ms"] + 2_000, c["mailbox_id"])
            assert (again["result"], again.get("code")) == ("refuse", res["code"]), c["name"]


def test_every_named_negative_case_is_present():
    want = {"replayed_request": ("refuse", "already_done"),
            "replay_of_finished_request": ("replay", None),
            "future_timestamp_then_in_window": ("refuse", "stale_timestamp"), "old_timestamp": ("refuse", "stale_timestamp"),
            "tampered_tag": ("drop", None), "wrong_device_signature": ("refuse", "bad_signature"),
            "repeated_sequence": ("refuse", "stale_sequence"), "unknown_version": ("drop", None),
            "revoked_device": ("refuse", "revoked")}
    got = {c["name"]: {(st["expect"]["result"], st["expect"].get("code")) for st in steps_of(c)}
           for c in VEC["host_cases"]}
    for name, outcome in want.items():
        assert outcome in got[name], name


def test_replay_never_runs_twice():
    c = next(c for c in SINGLE if c["name"] == "full_request_accepted")
    s = host_state(c)
    env = envelope_of(c)
    assert ref.host_check(env, s, c["now_ms"])["result"] == "accept"
    for later in (1, 60_000, ref.RID_RETENTION_MS - 1):
        assert ref.host_check(env, s, c["now_ms"] + later)["code"] == "already_done"
    # after the retention the timestamp is long out of window: still refused, never run
    assert ref.host_check(env, s, c["now_ms"] + ref.RID_RETENTION_MS)["code"] == "stale_timestamp"


def test_drops_carry_no_refusal():
    for c in VEC["host_cases"]:
        for st in steps_of(c):
            if st["expect"]["result"] == "drop":
                assert set(st["expect"]) == {"result"}, c["name"]


@by_name(VEC["device_cases"])
def test_device_case(c):
    ctx = {"workspace": VEC["keys"]["workspace"], "k_ws": K_WS, "key_version": 1,
           "device": VEC["ids"]["device_a"]["device_id"], "host_pub": PUB["host"],
           "pending": json.loads(json.dumps(c["pending"])), "offset_ms": c["offset_ms"]}
    res = ref.device_check(bytes.fromhex(c["envelope"]), ctx, c["mailbox"], c["now_ms"])
    assert {k: v for k, v in res.items() if k in c["expect"]} == c["expect"]


def test_pairing_link():
    p = VEC["pairing"]
    v, ws, pid, secret, pin = p["link_fragment"].split(".")
    assert (v, ws, pid) == ("v1", p["workspace"], p["pairing_id"])
    assert ref.unb64u(secret).hex() == p["secret"] and ref.unb64u(pin).hex() == p["host_pin"]
    assert ref.host_pin(bytes.fromhex(p["host_pub"])).hex() == p["host_pin"]
    mac = ref.pair_mac(bytes.fromhex(p["secret"]), bytes.fromhex(ws), bytes.fromhex(pid), bytes.fromhex(p["device_pub"]))
    assert mac.hex() == p["mac"]
    assert ref.device_fingerprint(bytes.fromhex(p["device_pub"])) == p["device_fingerprint"]
    assert ref.device_id(bytes.fromhex(ws), bytes.fromhex(p["device_pub"])).hex() == p["device_id"]
    proof = ref.phone_link_proof(bytes.fromhex(p["phone_key"]), bytes.fromhex(p["device_id"]))
    assert proof.hex() == p["phone_proof"]


@by_name(VEC["shown"])
def test_shown_text(c):
    s = "".join(map(chr, c["input"]))
    if c["expect"] is None:
        with pytest.raises(ValueError):
            ref.clean_shown(s)
    else:
        assert ref.clean_shown(s) == c["expect"]


def test_assertion_challenge():
    a = VEC["assertion"]
    i = a["challenge_inputs"]
    assert ref.canonical_json(i["subject"]).decode() == i["subject_json"]
    assert hashlib.sha256(i["subject_json"].encode()).hexdigest() == i["subject_hash"]
    ch = ref.assertion_challenge(bytes.fromhex(i["workspace"]), bytes.fromhex(i["device"]), bytes.fromhex(i["rid"]),
                                 i["purpose"], i["scope"], i["expires_ms"], bytes.fromhex(i["nonce"]), i["subject"])
    assert ch.hex() == a["challenge"]
    # by hand, as the specification lays the bytes out
    raw = (b"sharing/bridge/assert/v1|" + bytes.fromhex(i["workspace"] + i["device"] + i["rid"]) + bytes([1, 4])
           + i["expires_ms"].to_bytes(8, "big") + bytes.fromhex(i["nonce"] + i["subject_hash"]))
    assert hashlib.sha256(raw).hexdigest() == a["challenge"]
    r = a["registration_challenge"]
    assert ref.registration_challenge(bytes.fromhex(r["workspace"]), bytes.fromhex(r["device"]), r["expires_ms"],
                                      bytes.fromhex(r["nonce"])).hex() == r["challenge"]


@by_name(VEC["assertion"]["cases"])
def test_assertion_case(c):
    cred, pending = dict(c["credential"]), json.loads(json.dumps(c["pending"]))
    assert ref.verify_assertion(cred, pending, c["sender"], c["assertion"], c["now_ms"]) == c["expect"]
    presented = ref.unb64u(json.loads(bytes.fromhex(c["assertion"]["client_data_json"]))["challenge"]).hex()
    assert presented not in pending, "a challenge is single use, whatever the outcome"


def test_size_limits_match_the_mailbox():
    from fileshare import bridge as br
    assert ref.MAX_REQUEST == br.MAX_REQUEST_BYTES and ref.MAX_CHUNK == br.MAX_CHUNK_BYTES
    assert ref.OVERHEAD == 184
