import base64
import hashlib
import logging
import re
from datetime import timedelta

import pytest

from fileshare import clock
from fileshare.security import (
    P256_PUB_LEN, RateLimiter, b64u_decode, b64u_encode, fingerprint, hash_secret,
    new_device_token, new_id, new_session_token, sha256_hex, valid_p256_point, verify_secret,
)

P256_P = 2**256 - 2**224 + 2**192 + 2**96 - 1


def test_b64u_roundtrip_unpadded():
    for raw in (b"", b"\x00", b"\xff\xfe", bytes(range(32))):
        enc = b64u_encode(raw)
        assert "=" not in enc and "+" not in enc and "/" not in enc
        assert b64u_decode(enc) == raw


@pytest.mark.parametrize("bad", ["a", "ab=", "a+b/", "ab cd", "ab\n", "AB==", "_x", "AAB"])
def test_b64u_decode_is_strict(bad):
    # "AAB" and "_x" decode, but not canonically; "a" has an impossible length.
    with pytest.raises(ValueError):
        b64u_decode(bad)


def test_sha256_hex():
    assert sha256_hex("abc") == sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_hash_and_verify_secret():
    key = bytes(range(32))
    stored = hash_secret(key)
    assert re.fullmatch(r"scrypt\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}", stored)
    assert "$" not in stored
    assert verify_secret(key, stored) is True
    assert verify_secret(bytes(32), stored) is False
    assert hash_secret(key) != stored  # random salt


@pytest.mark.parametrize("stored", [None, "", "scrypt", "bcrypt.a.b", "scrypt.!!.??", "scrypt.a.b.c"])
def test_verify_secret_fails_closed_never_raises(stored):
    assert verify_secret(b"x" * 32, stored) is False


def test_verify_secret_rejects_empty_secret():
    assert verify_secret(b"", hash_secret(b"x")) is False


def test_verify_secret_rejects_non_string_stored():
    assert verify_secret(b"x", 123) is False


def test_verify_secret_rejects_wrong_length_salt():
    assert verify_secret(b"x" * 32, "scrypt..AAAA") is False


def test_verify_secret_rejects_wrong_length_digest():
    salt = b64u_encode(bytes(range(16)))
    stored = f"scrypt.{salt}.{b64u_encode(b'short')}"
    assert verify_secret(b"x" * 32, stored) is False


def test_token_shapes():
    assert re.fullmatch(r"shd_[A-Za-z0-9_-]{43}", new_device_token())
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", new_session_token())
    assert re.fullmatch(r"dev_[0-9a-f]{12}", new_id("dev"))
    assert new_device_token() != new_device_token()


def test_rate_limiter_blocks_after_limit_and_logs(frozen_clock, caplog):
    rl = RateLimiter(limit=10, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        for k in range(1, 11):
            assert rl.blocked("203.0.113.9") is False
            assert rl.fail("203.0.113.9") == k
    assert rl.blocked("203.0.113.9") is True
    assert rl.blocked("198.51.100.1") is False
    msgs = [r.getMessage() for r in caplog.records if r.name == "fileshare.auth"]
    assert msgs[0] == "auth: failed attempt 1/10 from 203.0.113.9"
    assert msgs[-1] == "auth: failed attempt 10/10 from 203.0.113.9"


def test_rate_limiter_window_expires(frozen_clock):
    rl = RateLimiter(limit=2, window_s=300)
    t0 = clock.now()
    rl.fail("ip")
    rl.fail("ip")
    assert rl.blocked("ip")
    frozen_clock(t0 + timedelta(seconds=301))
    assert not rl.blocked("ip")
    assert rl.fail("ip") == 1


def test_rate_limiter_reset(frozen_clock):
    rl = RateLimiter(limit=1)
    rl.fail("ip")
    assert rl.blocked("ip")
    rl.reset("ip")
    assert not rl.blocked("ip")


def test_rate_limiter_drops_key_after_window_expires(frozen_clock):
    rl = RateLimiter(limit=5, window_s=300)
    t0 = clock.now()
    rl.fail("ip")
    assert "ip" in rl._hits
    frozen_clock(t0 + timedelta(seconds=301))
    assert not rl.blocked("ip")
    assert "ip" not in rl._hits


def test_rate_limiter_deque_never_exceeds_limit(frozen_clock):
    rl = RateLimiter(limit=3, window_s=300)
    for _ in range(10):
        rl.fail("ip")
    assert rl.blocked("ip")
    assert len(rl._hits["ip"]) == 3


def test_rate_limiter_evicts_oldest_when_max_tracked_reached(frozen_clock, monkeypatch):
    monkeypatch.setattr(RateLimiter, "MAX_TRACKED", 3)
    rl = RateLimiter(limit=10, window_s=300)
    t0 = clock.now()
    for i, ip in enumerate(["ip1", "ip2", "ip3", "ip4"]):
        frozen_clock(t0 + timedelta(seconds=i))
        rl.fail(ip)
    assert "ip1" not in rl._hits
    assert set(rl._hits) == {"ip2", "ip3", "ip4"}


def test_try_begin_counts_in_flight_attempts_toward_limit(frozen_clock):
    rl = RateLimiter(limit=10, window_s=300)
    for _ in range(10):
        assert rl.try_begin("203.0.113.9") is True
    assert rl.try_begin("203.0.113.9") is False
    assert rl.try_begin("198.51.100.1") is True


def test_try_begin_counts_recorded_failures_plus_in_flight(frozen_clock):
    rl = RateLimiter(limit=3, window_s=300)
    rl.fail("ip")
    rl.fail("ip")
    assert rl.try_begin("ip") is True
    assert rl.try_begin("ip") is False


def test_end_without_failure_frees_slot_and_records_nothing(frozen_clock, caplog):
    rl = RateLimiter(limit=1, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        assert rl.try_begin("ip") is True
        assert rl.try_begin("ip") is False
        rl.end("ip", failed=False)
    assert "ip" not in rl._hits and "ip" not in rl._in_flight
    assert not [r for r in caplog.records if r.name == "fileshare.auth"]
    assert rl.try_begin("ip") is True


def test_end_with_failure_records_and_logs_like_fail(frozen_clock, caplog):
    rl = RateLimiter(limit=2, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        for _ in range(2):
            assert rl.try_begin("203.0.113.9")
            rl.end("203.0.113.9", failed=True)
    assert rl.blocked("203.0.113.9")
    assert rl.try_begin("203.0.113.9") is False
    assert "203.0.113.9" not in rl._in_flight
    msgs = [r.getMessage() for r in caplog.records if r.name == "fileshare.auth"]
    assert msgs == ["auth: failed attempt 1/2 from 203.0.113.9",
                    "auth: failed attempt 2/2 from 203.0.113.9"]


def test_valid_point_accepts_real_keys(sharing):
    for _ in range(5):
        _, pub = sharing.new_device_keypair()
        assert len(pub) == P256_PUB_LEN == 65
        assert valid_p256_point(pub)


def test_valid_point_rejects_malformed(sharing):
    _, pub = sharing.new_device_keypair()
    off_curve = pub[:-1] + bytes([pub[-1] ^ 1])
    cases = [
        b"",
        pub[:64],
        pub + b"\x00",
        b"\x02" + pub[1:],                                   # compressed prefix
        off_curve,
        b"\x04" + P256_P.to_bytes(32, "big") + pub[33:],     # x >= p
        b"\x04" + pub[1:33] + P256_P.to_bytes(32, "big"),    # y >= p
        b"\x04" + bytes(64),
    ]
    for case in cases:
        assert not valid_p256_point(case), case.hex()


def test_valid_point_rejects_non_bytes():
    assert not valid_p256_point("04" * 65)
    assert not valid_p256_point(None)


def _node_fingerprint(pub: bytes) -> str:
    # Cross-check the browser implementation (crypto.js) so all three agree on a fixed key (§4.6).
    import json
    import subprocess
    from pathlib import Path

    crypto_js = Path(__file__).resolve().parent.parent / "fileshare" / "static" / "js" / "crypto.js"
    script = (
        f"import {{ fingerprint }} from {json.dumps(str(crypto_js))};\n"
        "const pub = new Uint8Array(process.argv[1].match(/../g).map((b) => parseInt(b, 16)));\n"
        "process.stdout.write(await fingerprint(pub));\n"
    )
    r = subprocess.run(["node", "--input-type=module", "-e", script, pub.hex()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_fingerprint_matches_client_and_format(sharing):
    # A fixed device_pub (0x04 || 64 deterministic bytes) so the parity check is over a
    # known vector, not chance; fingerprint only hashes the bytes, so the point need not be valid.
    pub = bytes([0x04]) + bytes((7 * i + 11) % 256 for i in range(64))
    fp = fingerprint(pub)
    # 100 bits: 20 base32 chars grouped 4-4-4-4-4 -> 24 chars total (§4.6, security fix 2026-09-25).
    assert fp == sharing.fingerprint(pub)           # Python server == Python CLI
    assert fp == _node_fingerprint(pub)             # == browser (crypto.js)
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", fp)
    assert len(fp) == 24
    raw = base64.b32encode(hashlib.sha256(b"sharing/fp/v1|" + pub).digest()).decode()[:20]
    assert fp == "-".join(raw[i:i + 4] for i in range(0, 20, 4))


# --- WindowLimiter (public links, §17) ---

def test_window_limiter_slides_and_does_not_count_refusals(frozen_clock):
    from datetime import datetime, timezone
    from fileshare.security import WindowLimiter
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    lim = WindowLimiter(limit=3, window_s=60)
    assert [lim.allow("a") for _ in range(5)] == [True, True, True, False, False]
    assert lim.allow("b")
    frozen_clock(t0 + timedelta(seconds=30))
    assert not lim.allow("a")
    frozen_clock(t0 + timedelta(seconds=60))        # the first three have left the window
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]


def test_window_limiter_memory_is_capped(frozen_clock, monkeypatch):
    from datetime import datetime, timezone
    from fileshare.security import WindowLimiter
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    monkeypatch.setattr(WindowLimiter, "MAX_TRACKED", 5)
    lim = WindowLimiter(limit=1, window_s=60)
    for i in range(20):
        frozen_clock(t0 + timedelta(seconds=i))
        assert lim.allow(f"10.0.0.{i}")
    assert len(lim._hits) <= 5


def test_window_limiter_refund_gives_the_newest_slot_back():
    from fileshare.security import WindowLimiter
    lim = WindowLimiter(2, 3600)
    assert lim.allow("d") and lim.allow("d") and not lim.allow("d")
    lim.refund("d")
    assert lim.allow("d") and not lim.allow("d")
    lim.refund("unknown")                       # nothing to give back: a no-op
    assert lim.allow("unknown")
