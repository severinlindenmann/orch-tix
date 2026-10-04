"""Shared files and messages as actions: download, attach to a ticket, upload, links and message acks.
Every call is the sharing CLI through ctx.run; decrypted files land only under the addon's state folder."""
from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from orch.addons.api import FileResult, Reveal
from orch.errors import ValidationError

from .cli import SharingError

FILE_REF = re.compile(r"FILE[1-9][0-9]{0,11}")
MSG_ID = re.compile(r"msg_[0-9a-f]{32}")
DL_KEEP_S = 600


def file_ref(value: str) -> str:
    ref = str(value or "").strip().upper()
    if not FILE_REF.fullmatch(ref):
        raise ValidationError("a shared file is FILE and a number, e.g. FILE7")
    return ref


def sweep_downloads(state_dir) -> int:
    """Remove download folders older than 10 minutes (plaintext a crash or an unserved FileResult left behind).
    Runs on every download and on every files-provider tick. Returns how many went."""
    base = Path(state_dir) / "dl"
    if not base.is_dir() or base.is_symlink():
        return 0
    now, gone = time.time(), 0
    for old in base.iterdir():
        try:
            if old.is_symlink():
                old.unlink()
                gone += 1
            elif old.is_dir() and now - old.stat().st_mtime > DL_KEEP_S:
                shutil.rmtree(old, ignore_errors=True)
                gone += 1
        except OSError:
            pass
    return gone


def _dl_dir(pctx) -> Path:
    """A fresh folder under state_dir/dl for one download; stale folders are swept first."""
    base = pctx.addon.state_dir / "dl"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    sweep_downloads(pctx.addon.state_dir)
    d = base / secrets.token_hex(8)
    d.mkdir(mode=0o700)
    return d


def offered_file(pctx, value: str) -> str:
    """A FILE id the Shared files page shows (the cached files snapshot); anything else is refused, because the
    page may be stale and a made-up target must never reach the CLI."""
    ref = file_ref(value)
    for snap in pctx.snapshots("files"):
        if any(isinstance(i, dict) and i.get("id") == ref for i in snap.items):
            return ref
    raise ValidationError(f"{ref} is not on the Shared files page any more; refresh it")


def fetch(addon, pctx, ref: str) -> tuple[Path, str, str]:
    """Download and decrypt `ref` into a fresh folder under state_dir/dl without acknowledging it.
    Returns (path, name, mime)."""
    out = _dl_dir(pctx)
    try:
        r = addon.sharing(pctx).run_json("get", ref, "-o", str(out), "--no-ack", "--force", timeout=300)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)       # never leave a partial plaintext behind
        raise
    path = Path(str(r.get("path") or ""))
    try:
        inside = path.is_file() and not path.is_symlink() and path.resolve().parent == out.resolve()
    except OSError:
        inside = False
    if not inside:
        shutil.rmtree(out, ignore_errors=True)
        raise SharingError("bad_output", f"the sharing CLI did not write {ref} where it was asked to")
    return path, path.name, str(r.get("mime") or "application/octet-stream")


def download(addon, pctx, target: str) -> FileResult:
    path, name, mime = fetch(addon, pctx, offered_file(pctx, target))
    return FileResult(path, name, mime)


def attach(addon, pctx, ref: str, key: str) -> str:
    """Attach a shared file to a ticket as remote-<name>, as the addon's agent."""
    path, name, _ = fetch(addon, pctx, ref)
    try:
        pctx.addon.ops().add_artifact(key, path, f"remote-{name}")
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    return f"remote-{name}"


def save_to_ticket(addon, pctx, target: str) -> str:
    ref, sep, ticket = str(target or "").partition("|")
    if not sep or not ticket:
        raise ValidationError("attach needs FILE7|<ticket>")
    ref = offered_file(pctx, ref)
    key = str(pctx.document(ticket.strip())["id"])
    link = addon.state.links().get(key)
    if not link or link.get("retired"):                 # the page offers synced tickets only
        raise ValidationError(f"{key} is not synced to TIX; attach from a synced ticket")
    return f"Attached {ref} to {key} as {attach(addon, pctx, ref, key)}"


def upload(addon, pctx, upload_) -> str:
    if upload_ is None:
        raise ValidationError("choose a file to upload")
    r = addon.sharing(pctx).run_json("share", str(upload_.path), "--name", str(upload_.name), "--ttl", "7d",
                                     timeout=600)
    return f"Shared as {r.get('id')}"


def public_link(addon, pctx, target: str) -> Reveal:
    r = addon.sharing(pctx).run_json("link", file_ref(target), "--ttl", "7d", timeout=30)
    return Reveal("Public link (shown once)", _url(r))


def upload_link(addon, pctx) -> Reveal:
    r = addon.sharing(pctx).run_json("upload-link", "create", timeout=30)
    return Reveal("Upload link (shown once)", _url(r))


def _url(r: dict) -> str:
    url = r.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise SharingError("bad_output", "the sharing CLI did not return a link")
    return url


def ack_message(addon, pctx, target: str) -> str:
    mid = str(target or "").strip()
    if not MSG_ID.fullmatch(mid):
        raise ValidationError("a message id is msg_ and 32 hex characters")
    addon.sharing(pctx).run_json("msg", "ack", mid, timeout=15)
    return "Done"
