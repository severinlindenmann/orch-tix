"""Hashing, tokens and the login rate limit. Ported from an earlier auth module."""
import base64
import collections
import hashlib
import hmac
import logging
import re
import secrets
import threading

from fileshare import clock

log = logging.getLogger("fileshare.auth")

_B64U = re.compile(r"[A-Za-z0-9_-]*")

# N=2**15, r=8 needs 128*N*r = 32 MiB, which is exactly OpenSSL's default
# ceiling, so scrypt refuses unless maxmem is raised explicitly.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_MAXMEM = 96 * 1024 * 1024


def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def b64u_decode(s: str) -> bytes:
    if not isinstance(s, str) or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise ValueError("invalid base64url")
    out = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64u_encode(out) != s:
        raise ValueError("non-canonical base64url")
    return out


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                          dklen=32, maxmem=SCRYPT_MAXMEM)


def hash_secret(secret: bytes) -> str:
    # Dot-separated, never "$": the value may pass through an env file over ssh,
    # where an unquoted $ is eaten and yields a hash that can never match.
    salt = secrets.token_bytes(16)
    return f"scrypt.{b64u_encode(salt)}.{b64u_encode(_scrypt(secret, salt))}"


def verify_secret(secret: bytes, stored) -> bool:
    if not secret or not isinstance(stored, str) or not stored:
        return False
    try:
        scheme, salt_s, dk_s = stored.split(".")
        if scheme != "scrypt":
            return False
        salt, want = b64u_decode(salt_s), b64u_decode(dk_s)
    except (ValueError, TypeError):
        return False
    if len(salt) != 16 or len(want) != 32:
        return False
    return hmac.compare_digest(_scrypt(secret, salt), want)


def new_device_token() -> str:
    return "shd_" + secrets.token_urlsafe(32)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class RateLimiter:
    """In-memory, one process. It resets on restart; fail2ban covers persistence."""

    # Caps memory even if many distinct IPs fail once and are never retried:
    # without this, _hits would grow forever since only reset() deletes a key.
    MAX_TRACKED = 10_000

    def __init__(self, limit: int = 10, window_s: int = 300):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, collections.deque] = {}
        # Attempts between try_begin() and end(). Keys are dropped at 0, so the size
        # is bounded by the number of concurrent requests, not by distinct IPs.
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Lock()

    def _prune(self, q: collections.deque, now: float) -> None:
        while q and q[0] <= now - self.window_s:
            q.popleft()

    def _prune_and_drop(self, ip: str, now: float) -> collections.deque | None:
        """Prune ip's deque and drop the key entirely once it's empty. Caller holds the lock."""
        q = self._hits.get(ip)
        if q is None:
            return None
        self._prune(q, now)
        if not q:
            del self._hits[ip]
            return None
        return q

    def blocked(self, ip: str) -> bool:
        now = clock.now().timestamp()
        with self._lock:
            q = self._prune_and_drop(ip, now)
            return q is not None and len(q) >= self.limit

    def _record_failure(self, ip: str, now: float) -> int:
        """Append one failure for ip and return the count in the window. Caller holds the lock."""
        q = self._prune_and_drop(ip, now)
        if q is None:
            if len(self._hits) >= self.MAX_TRACKED:
                # Evict whichever tracked IP's most recent failure is the
                # stalest, to make room for this new one.
                oldest_ip = min(self._hits, key=lambda k: self._hits[k][-1])
                del self._hits[oldest_ip]
            q = collections.deque(maxlen=self.limit)
            self._hits[ip] = q
        q.append(now)
        return len(q)

    def fail(self, ip: str) -> int:
        now = clock.now().timestamp()
        with self._lock:
            n = self._record_failure(ip, now)
        log.warning("auth: failed attempt %d/%d from %s", n, self.limit, ip)
        return n

    def try_begin(self, ip: str) -> bool:
        """Reserve a slot for one attempt. Attempts still running count as if they had
        failed, so a parallel burst cannot slip past the limit before failures are recorded.
        Every True must be paired with exactly one end()."""
        now = clock.now().timestamp()
        with self._lock:
            q = self._prune_and_drop(ip, now)
            failures = 0 if q is None else len(q)
            running = self._in_flight.get(ip, 0)
            if failures + running >= self.limit:
                return False
            self._in_flight[ip] = running + 1
            return True

    def end(self, ip: str, failed: bool) -> None:
        """Release a try_begin() slot. If failed, record it exactly as fail() does."""
        now = clock.now().timestamp()
        with self._lock:
            running = self._in_flight.get(ip, 0) - 1
            if running > 0:
                self._in_flight[ip] = running
            else:
                self._in_flight.pop(ip, None)
            n = self._record_failure(ip, now) if failed else None
        if n is not None:
            log.warning("auth: failed attempt %d/%d from %s", n, self.limit, ip)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)


class WindowLimiter:
    """At most `limit` requests per IP in any `window_s` (sliding window). In memory, one process,
    like RateLimiter. Refused requests are not counted, so a client that backs off recovers."""

    MAX_TRACKED = 10_000

    def __init__(self, limit: int = 60, window_s: int = 60):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = clock.now().timestamp()
        with self._lock:
            q = self._hits.get(ip)
            if q is None:
                if len(self._hits) >= self.MAX_TRACKED:
                    stale = [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window_s]
                    for k in stale:
                        del self._hits[k]
                    if len(self._hits) >= self.MAX_TRACKED:
                        del self._hits[min(self._hits, key=lambda k: self._hits[k][-1])]
                q = collections.deque(maxlen=self.limit)
                self._hits[ip] = q
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def refund(self, key: str) -> None:
        """Give back the newest slot of `key`: the request it allowed did not succeed (batch-2 review)."""
        with self._lock:
            q = self._hits.get(key)
            if q:
                q.pop()


# --- P-256 public keys (device approval, spec §4.6) -------------------------
# Pure integer math: the server validates and fingerprints device keys but
# never imports a cipher (Global Constraints).
P256_PUB_LEN = 65
_P256_P = 2**256 - 2**224 + 2**192 + 2**96 - 1
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
FP_PREFIX = b"sharing/fp/v1|"


def valid_p256_point(pub) -> bool:
    # P-256 has cofactor 1 and infinity has no uncompressed encoding, so
    # "on the curve with coordinates in the field" is the whole check.
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != P256_PUB_LEN or pub[0] != 4:
        return False
    x = int.from_bytes(pub[1:33], "big")
    y = int.from_bytes(pub[33:65], "big")
    if x >= _P256_P or y >= _P256_P:
        return False
    return (y * y - (x * x * x - 3 * x + _P256_B)) % _P256_P == 0


def fingerprint(pub: bytes) -> str:
    # 100 bits: the first 20 base32 chars of the hash, grouped 4-4-4-4-4 (§4.6). A shorter value
    # would let a compromised server grind a colliding key past the human approval check.
    raw = base64.b32encode(hashlib.sha256(FP_PREFIX + bytes(pub)).digest()).decode("ascii")[:20]
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))
