"""fileshare.pushworker: the only place that imports `cryptography` / `pywebpush` (spec T7).

The server process never imports this module in-process; `fileshare.push.SubprocessPusher` runs it
as a subprocess (`python -m fileshare.pushworker <cmd>`), so `test_server_never_imports_cryptography`
stays green.

Commands (see `fileshare/push.py` for the exact request/response shapes):
  genkeys   Prints {"private": b64u DER, "public": b64u uncompressed point} on stdout.
  send      Reads {"private", "sub", "subs": [{"id", "endpoint", "keys"}], "payload", "ttl"} on
            stdin, POSTs each with `webpush()`, and prints {"gone": [ids], "failed": [ids]}.
"""
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid
from pywebpush import WebPushException, webpush


def _b64u_encode(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def genkeys() -> dict:
    v = Vapid()
    v.generate_keys()
    private_der = v.private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_point = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {"private": _b64u_encode(private_der), "public": _b64u_encode(public_point)}


def _endpoint_allowed(endpoint: str) -> bool:
    if endpoint.startswith("https://"):
        return True
    return endpoint.startswith("http://") and os.environ.get("FS_PUSH_ALLOW_HTTP") == "1"


def send(req: dict) -> dict:
    gone: list[str] = []
    failed: list[str] = []
    for sub in req.get("subs", []):
        endpoint = sub["endpoint"]
        if not _endpoint_allowed(endpoint):
            failed.append(sub["id"])
            continue
        try:
            webpush(
                subscription_info={"endpoint": endpoint, "keys": sub["keys"]},
                data=req["payload"],
                vapid_private_key=req["private"],
                vapid_claims={"sub": req["sub"]},
                ttl=req.get("ttl", 86400),
                timeout=10,
            )
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                gone.append(sub["id"])
            else:
                failed.append(sub["id"])
        except Exception:
            failed.append(sub["id"])
    return {"gone": gone, "failed": failed}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("genkeys", "send"):
        print("usage: python -m fileshare.pushworker genkeys|send", file=sys.stderr)
        return 2
    if argv[0] == "genkeys":
        print(json.dumps(genkeys()))
        return 0
    req = json.load(sys.stdin)
    print(json.dumps(send(req)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
