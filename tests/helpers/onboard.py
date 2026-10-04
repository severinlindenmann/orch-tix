import os
from dataclasses import dataclass

from fileshare import admin
from fileshare.db import connect
from fileshare.security import b64u_encode

PASSPHRASE = "correct horse battery staple"
ITER = 1000


@dataclass
class Owner:
    client: object
    mk: bytes
    auth_key: bytes


@dataclass
class DeviceCreds:
    id: str
    token: str
    mk: bytes | None      # None while the device is pending
    name: str
    project: str
    priv: bytes           # PKCS8 DER private key
    pub: bytes            # 65-byte SEC1 uncompressed point

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def setup_owner(client, settings, sharing) -> Owner:
    conn = connect(settings.db_path)
    try:
        code = admin.create_setup_code(conn)
    finally:
        conn.close()
    salt, mk = os.urandom(16), os.urandom(32)
    auth_key, kek = sharing.derive(PASSPHRASE, salt, ITER)
    r = client.post("/api/setup", json={
        "setup_code": code,
        "kdf_salt": sharing.b64u(salt),
        "kdf_iterations": ITER,
        "auth_key": sharing.b64u(auth_key),
        "wrapped_mk": sharing.b64u(sharing.seal(kek, mk, sharing.AAD_MK)),
    })
    assert r.status_code == 204, r.text
    return Owner(client, mk, auth_key)


def new_code() -> tuple[str, bytes]:
    lookup = os.urandom(16)
    return "shr1." + b64u_encode(lookup), lookup


def create_token(owner: Owner, lookup: bytes) -> str:
    r = owner.client.post("/api/onboarding-tokens", json={"lookup": b64u_encode(lookup)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def handshake(client, sharing, lookup: bytes, name="laptop", project="proj", hostname="host1",
              platform="linux"):
    priv, pub = sharing.new_device_keypair()
    r = client.post("/api/handshake", json={
        "lookup": b64u_encode(lookup),
        "device_name": name,
        "project": project,
        "hostname": hostname,
        "platform": platform,
        "device_pub": b64u_encode(pub),
    })
    return r, priv, pub


def approve(owner: Owner, sharing, device_id: str, pub: bytes) -> None:
    bundle = sharing.seal_to_device(owner.mk, pub, device_id)
    r = owner.client.post(f"/api/devices/{device_id}/approve",
                          json={"device_bundle": sharing.b64u(bundle)})
    assert r.status_code == 204, r.text


_approve = approve   # onboard_device's `approve` flag shadows the function name inside its body


def onboard_device(owner: Owner, sharing, name: str, project: str, approve: bool = True) -> DeviceCreds:
    """Token -> handshake -> (optionally) approve -> fetch and open the bundle, like a real device."""
    _, lookup = new_code()
    create_token(owner, lookup)
    r, priv, pub = handshake(owner.client, sharing, lookup, name, project)
    assert r.status_code == 201, r.text
    body = r.json()
    creds = DeviceCreds(body["device_id"], body["device_token"], None, name, project, priv, pub)
    if approve:
        _approve(owner, sharing, creds.id, pub)
        b = owner.client.get("/api/devices/self/bundle", headers=creds.headers)
        assert b.status_code == 200, b.text
        creds.mk = sharing.open_device_bundle(priv, pub, creds.id,
                                              sharing.unb64u(b.json()["device_bundle"]))
        assert creds.mk == owner.mk
    return creds
