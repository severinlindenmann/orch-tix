# Changelog

## 0.3.0

- A TIX section on orch-core's How it works page (`guide.section`, needs the orch-core release that adds that slot; an older orch-core rejects the manifest, so this version needs it): answering a blocked agent from your phone, end-to-end encryption, pairing and signed decisions in the same ledger, and notifications that clear when you decided on the desktop. It shows only while the addon is enabled and replaces core's generic phone paragraph.

## 0.2.0

- A ticket the server answered `gone` for is linked again on the next sync cycle as a new generation (once per cycle, when the sync policy would mirror it); by-hand unlinks and done cleanups stay retired. The link records why it was retired (`retired_why`).

- The mirrored document carries orch-core's `signed` block (schema 1.6) at full and title (never key-only), cleaned to `{signed, by}` per gate and verdict; the phone names who signed each approved gate and the verdict. An older orch-core sends none and the phone keeps its "not signed here" wording.
- A held decision's reason on the phone comes from `RemoteResult.code` (orch-core API 2.4); unknown codes fall back by status, and message matching stays only for an orch-core that sends no code.
- Phone v4: at `full`, pinned images the gated text shows or that prove a criterion are shared (once, at most 4 per push) so the phone can show them after verifying their sha256; `title` keeps artifact items without labels.
- Ticket widgets (orch.widgets.v1) reach the phone at redaction full: each block's text alternative, a core
  type's document inline (up to 128 KiB each, 512 KiB per ticket), agent HTML as text only unless `sync_widget_docs = always` (default `never`; `redaction` stays `title` until a workspace sets `full`) (then a 7-day context FILE, not with
  `sync_artifacts = never`, deleted when its block changes or goes, redaction drops below full or the ticket is
  unlinked). Title and key-only send no widgets. Documents are dropped (text kept) when the sealed
  ticket would pass the server's 1 MiB. Every entry carries `raw_sha256` (the fence text) and, with a document, `sha256` of its exact bytes: the phone hashes both and shows the text only on a mismatch. Needs orch-core with `ctx.ticket_widgets`; an older one sends none.
- The Shared files page has a "Sent to TIX" tab: every push (ticket, redaction level, the names of the sealed fields, size, ok / retry / refused), context files, and the phone decisions received with their outcome. The last 200 entries, in `sentlog.json` (0600).
- The devices table has three columns and empty tables say what next (`orch addon check --strict`; the "TIX" product name passes once orch-core lists it as a proper noun).
- Phone verdicts carry the verdict hash (schema 1.3): the Apply intent passes it as `expected_hash`, a verdict without it or with another is stale, and `title` keeps the document's `verdict` {hash, round}.
- The phone's history comes from orch events (who, what; free text only at `full`) instead of the Log.
- `title` also sends each gate's `covers` (orch-core hash v2), so the phone can say what an approval binds.

## 0.1.0

- Mirrors tickets to TIX with redaction, takes phone decisions back (direct apply for paired phones, Apply / Ignore
  otherwise) and adds the TIX section, the Synced to TIX panel and the Phone answers tile.
- Shared files page: download, upload, public and upload links, attach to ticket, messages with Mark as done.
