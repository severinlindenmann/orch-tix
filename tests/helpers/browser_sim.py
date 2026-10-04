"""A scripted stand-in for the web UI: the same crypto and API calls the browser makes.

Uses sharing.py's crypto, which the vector tests prove byte-identical to static/js/crypto.js.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import time

import httpx

from tests.helpers.sharing_mod import load


class BrowserSim:
    def __init__(self, client_or_base_url, passphrase: str = "correct horse battery", iterations: int = 1000):
        self.s = load()
        if isinstance(client_or_base_url, str):
            base = client_or_base_url.rstrip("/")
            self.http = httpx.Client(base_url=base, headers={"Origin": base}, timeout=30)
        else:
            self.http = client_or_base_url
        self.passphrase = passphrase
        self.iterations = iterations
        self.mk: bytes | None = None
        self.key_version = 1

    @staticmethod
    def _check(r: httpx.Response, *ok: int) -> httpx.Response:
        if r.status_code not in ok:
            raise AssertionError(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}")
        return r

    def setup(self, setup_code: str) -> None:
        s = self.s
        self.mk = os.urandom(32)
        salt = os.urandom(16)
        auth_key, kek = s.derive(self.passphrase, salt, self.iterations)
        body = {
            "setup_code": setup_code,
            "kdf_salt": s.b64u(salt),
            "kdf_iterations": self.iterations,
            "auth_key": s.b64u(auth_key),
            "wrapped_mk": s.b64u(s.seal(kek, self.mk, s.AAD_MK)),
        }
        self._check(self.http.post("/api/setup", json=body), 204)

    def login(self) -> None:
        s = self.s
        kdf = self._check(self.http.get("/api/kdf"), 200).json()
        auth_key, kek = s.derive(self.passphrase, s.unb64u(kdf["kdf_salt"]), int(kdf["kdf_iterations"]))
        self._check(self.http.post("/api/login", json={"auth_key": s.b64u(auth_key)}), 204)
        blob = self._check(self.http.get("/api/keyblob"), 200).json()
        self.mk = s.open_(kek, s.unb64u(blob["wrapped_mk"]), s.AAD_MK)
        self.key_version = int(blob["key_version"])

    def onboarding_code(self) -> tuple[str, str]:
        """(code, token_id). v3: the code carries only the 16-byte lookup; no secret."""
        s = self.s
        lookup = os.urandom(16)
        r = self._check(self.http.post("/api/onboarding-tokens", json={"lookup": s.b64u(lookup)}), 201)
        return f"shr1.{s.b64u(lookup)}", r.json()["id"]

    def approve(self, device_id: str) -> None:
        """What approve.js does: check the fingerprint against the public key, seal MK to it, POST approve."""
        s = self.s
        assert self.mk is not None, "call setup() or login() first"
        d = next((x for x in self.devices() if x["id"] == device_id), None)
        assert d is not None, f"unknown device {device_id}"
        pub = s.unb64u(d["pubkey"])
        assert s.fingerprint(pub) == d["fingerprint"], "server fingerprint does not match the device's public key"
        bundle = s.seal_to_device(self.mk, pub, device_id)
        self._check(self.http.post(f"/api/devices/{device_id}/approve",
                                   json={"device_bundle": s.b64u(bundle)}), 204)

    def approve_when_pending(self, token_id: str, timeout_s: float = 60) -> dict:
        """Poll the onboarding token until a pending device is attached, approve it, return its DeviceOut."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            t = self._check(self.http.get(f"/api/onboarding-tokens/{token_id}"), 200).json()
            dev = t.get("device")
            if dev and dev.get("status") == "pending":
                self.approve(dev["id"])
                return dev
            time.sleep(0.05)
        raise AssertionError(f"no pending device attached to {token_id} within {timeout_s}s")

    def upload(self, name: str, data: bytes, note: str = "", ttl: str | None = None,
               mime: str | None = None, tags: list[str] | None = None) -> dict:
        s = self.s
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        fc = s.new_file_crypto(self.mk, name, mime, note)
        ct = io.BytesIO()
        s.encrypt_stream(fc["dek"], fc["uuid"], self.key_version, io.BytesIO(data), ct)
        meta = {"uuid": fc["uuid_hex"], "key_version": self.key_version,
                "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"]}
        if ttl is not None:
            meta["ttl"] = ttl
        if tags is not None:
            meta["tags"] = tags
        r = self.http.post("/api/files", data={"meta": json.dumps(meta)},
                           files={"blob": ("blob.shr", ct.getvalue(), "application/octet-stream")})
        return self._check(r, 201).json()

    def get_file(self, ref: str) -> dict:
        return self._check(self.http.get(f"/api/files/{ref}"), 200, 410).json()

    def set_transcript(self, ref: str, transcript) -> None:
        """What `sharing transcribe` does after Deepgram: re-seal the whole metadata with a transcript
        under the file's existing DEK and AAD, then PATCH it. `transcript` is stored as given, so a
        test can plant a malformed or hostile one."""
        s = self.s
        meta, dek = s.open_file_meta(self.mk, self.get_file(ref))
        enc = s.seal_file_meta(dek, bytes.fromhex(self.get_file(ref)["uuid"]), {**meta, "transcript": transcript})
        self._check(self.http.patch(f"/api/files/{ref}/meta", json={"enc_meta": enc}), 204)

    def get_settings(self) -> tuple[dict | None, int, str | None]:
        """(decrypted settings or None when unset, rev, updated_at), as settings.js loads them."""
        body = self._check(self.http.get("/api/settings"), 200).json()
        obj = None if body["enc_settings"] is None else self.s.open_settings(self.mk, body["enc_settings"])
        return obj, body["rev"], body["updated_at"]

    def put_settings(self, obj: dict, rev: int | None = None) -> int:
        """Seal `obj` under MK and PUT it (at the current rev unless given); returns the new rev."""
        if rev is None:
            rev = self.get_settings()[1]
        r = self.http.put("/api/settings", json={"enc_settings": self.s.seal_settings(self.mk, obj), "rev": rev})
        return self._check(r, 200).json()["rev"]

    def create_link(self, ref: str, ttl: str = "7d", max_downloads: int | None = None) -> tuple[str, dict]:
        """What the Share link dialog does (§17): unwrap the DEK, wrap it under a fresh link key,
        POST it. Returns (full URL with the key in the fragment, the create response)."""
        s = self.s
        f = self.get_file(ref)
        _, dek = s.open_file_meta(self.mk, f)
        lk = s.new_link_key()
        body = {"wrapped_dek_link": s.wrap_dek_for_link(dek, bytes.fromhex(f["uuid"]), lk), "ttl": ttl}
        if max_downloads is not None:
            body["max_downloads"] = max_downloads
        made = self._check(self.http.post(f"/api/files/{ref}/links", json=body), 201).json()
        return f"{str(self.http.base_url).rstrip('/')}/p/{made['token']}#{s.b64u(lk)}", made

    def delete(self, ref: str) -> None:
        self._check(self.http.delete(f"/api/files/{ref}"), 204)

    def revoke(self, device_id: str) -> None:
        """Revoke an active device, or Reject a pending one (same endpoint)."""
        self._check(self.http.delete(f"/api/devices/{device_id}"), 204)

    def devices(self) -> list:
        return self._check(self.http.get("/api/devices"), 200).json()["devices"]

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        """A raw session-authenticated request (Origin already set); no status check."""
        return self.http.request(method, path, **kw)
