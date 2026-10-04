"""Ticket widgets on the phone (orch-core docs/widgets.md, format orch.widgets.v1, "TIX"). Only at redaction
full, which already carries the sections the blocks stand in. Per block the phone gets its text alternative and,
when it can be drawn, the standalone document from `render_document`: a core type inline in the sealed doc, agent
HTML (which inlines whole libraries) as a 7-day context FILE only with `sync_widget_docs = always` (default `never`:
its text alone). Every entry carries `raw_sha256` (the fence text) and, with a document, `sha256` (the document's
exact bytes): the phone refuses a document, inline or FILE, whose bytes do not match. A FILE whose block changed, went away or no longer goes to the phone is deleted. Nothing is
rendered on the server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time

from .cli import SharingError

INLINE_MAX = 128 * 1024          # one core document inline (about 90 KB: 60 KB are the inlined fonts)
INLINE_TOTAL = 512 * 1024        # all inline documents of one ticket (fit() still guards the 1 MiB)
MAX_SEALED_B64 = 1 << 20         # fileshare/routes/mirrors.py MAX_MIRROR_B64: the whole sealed doc, base64
SEAL_OVERHEAD = 29 + 64          # envelope version + nonce + tag, and the mirror_rev/gen the CLI adds
RESHARE_AFTER_S = 6 * 86400      # a shared document lives 7 days; share it again a day before it goes


def entries(addon, ctx, key: str, *, share: bool) -> list[dict]:
    """The `widgets` list of a full doc. An orch-core without ctx.ticket_widgets (before widgets) gives none."""
    try:
        blocks = ctx.ticket_widgets
    except AttributeError:
        return []
    out, inline_total = [], 0
    for b in blocks(key):
        if b["section"] == "Log":                # never sent at full (mapping._FULL_DROP_SECTIONS)
            continue
        e = {k: b[k] for k in ("section", "index", "key", "name", "title", "text")}
        e["layer"] = b["layer"] or "invalid"
        e["raw_sha256"] = b["raw_sha256"]       # the phone hashes each fence's text and wants this (it covers the template pin)
        if b["source"]:
            e["source"] = b["source"]
        doc = b["document"]
        if doc is not None:
            size = len(doc.encode("utf-8"))
            if b["layer"] == "type" and size <= INLINE_MAX and inline_total + size <= INLINE_TOTAL:
                e["doc"] = doc
                e["sha256"] = exact_sha(doc)
                inline_total += size
            elif share and _FRAME_META in doc:        # agent HTML only: never the "turn agent HTML on" note page
                fid, pin = _shared(addon, ctx, key, b, doc)
                if fid:
                    e["file"], e["sha256"] = fid, pin
        out.append(e)
    return out


_FRAME_META = '<meta name="orch-frame" content="'   # what orch-core's frame assembler puts in an agent-HTML document
_NONCE = re.compile(r'<meta name="orch-frame" content="([A-Za-z0-9_-]{8,64})">')


def doc_sha(doc: str) -> str:
    """The document's digest with its frame nonce taken out (a fresh one per render): a changed template, data or
    image gives another digest, a re-render of the same one does not."""
    m = _NONCE.search(doc)
    return hashlib.sha256((doc.replace(m.group(1), "") if m else doc).encode("utf-8")).hexdigest()


def exact_sha(doc: str) -> str:
    """The pin the phone checks: sha256 of the document's exact UTF-8 bytes, nonce included, as inlined or shared."""
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()


def _shared(addon, ctx, key: str, block: dict, doc: str) -> tuple[str | None, str | None]:
    """(FILE id, pin) of this block's document, shared once per document and again before the 7 days run out; the
    FILE it replaces is deleted. The pin is the exact bytes of the FILE (the first render's nonce stays in it)."""
    from .sync import out_dir
    digest, wkey = doc_sha(doc), block["key"]
    known = ((addon.state.links().get(key) or {}).get("widget_files") or {}).get(wkey) or {}
    if known.get("sha") == digest and known.get("file") and known.get("pin") and time.time() - float(known.get("at") or 0) < RESHARE_AFTER_S:
        return known["file"], known["pin"]
    path = out_dir(ctx) / f"widget-{secrets.token_hex(8)}.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    try:
        r = addon.sharing(ctx).run_json("share", str(path), "--name", f"{key}-widget-{wkey}.html",
                                        "--ttl", "7d", "--tag", "context", timeout=120)
    except SharingError as e:
        addon.note_error(f"share {key} widget {wkey}: {e.detail}")
        addon.state.log({"kind": "file", "key": key, "fields": ["widget"], "size": len(doc.encode("utf-8")),
                         "result": "refused" if e.code == "refused" else "retry", "reason": f"{e.code}: {e.detail}"[:200]})
        return None, None
    finally:
        path.unlink(missing_ok=True)
    fid = str(r.get("id") or "")
    addon.state.log({"kind": "file", "key": key, "tix": fid, "fields": ["widget"], "size": len(doc.encode("utf-8")),
                     "result": "ok" if fid else "refused"})
    if fid:
        if known.get("file") and known["file"] != fid:
            _delete(addon, addon.sharing(ctx), key, wkey, known["file"])
        addon.state.set_widget_file(key, wkey, {"sha": digest, "pin": exact_sha(doc), "file": fid, "at": time.time()})
    return (fid, exact_sha(doc)) if fid else (None, None)


def _delete(addon, sharing, key: str, wkey: str, fid: str) -> None:
    try:
        sharing.run_json("rm", fid, timeout=30)
    except SharingError as e:
        if e.code not in ("not_found", "deleted"):
            addon.note_error(f"delete {key} widget {wkey} ({fid}): {e.detail}")
            return
    addon.state.drop_widget_file(key, wkey)


def prune(addon, sharing, key: str, widgets: list[dict] | None) -> None:
    """Delete every shared widget FILE of `key` the doc being pushed no longer names (a removed block, a FILE
    replaced, redaction below full, sharing turned off, the ticket unlinked: `widgets` None or [])."""
    keep = {str(w["key"]): w["file"] for w in widgets or [] if w.get("file")}
    known = (addon.state.links().get(key) or {}).get("widget_files") or {}
    for wkey, rec in list(known.items()):
        if rec.get("file") and keep.get(wkey) != rec["file"]:
            _delete(addon, sharing, key, wkey, rec["file"])


def _sealed_b64(doc: dict) -> int:
    n = len(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return (n + SEAL_OVERHEAD + 2) // 3 * 4


def fit(addon, key: str, body: dict) -> dict:
    """Keep the sealed doc under the server's limit: drop the inline documents (each widget keeps its text)."""
    doc = body["doc"]
    if doc.get("widgets") and _sealed_b64(doc) > MAX_SEALED_B64:
        doc["widgets"] = [{k: v for k, v in w.items() if k != "doc"} for w in doc["widgets"]]
        addon.note_error(f"push {key}: widget documents dropped, the ticket is too large for the phone with them")
    return body
