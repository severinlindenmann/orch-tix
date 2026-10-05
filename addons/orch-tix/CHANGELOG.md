# Changelog

## 0.3.0

- **Behaviour change: phone notifications are off by default, per ticket.** Tickets still sync to TIX and show under Needs you; only the push notifications are suppressed. After updating the addon and the TIX server, every existing ticket starts off (and so does "messages without a ticket"), so no push arrives until the owner turns it on.
- A per-ticket **"Notify my phone about this ticket"** switch (`ticket_options`, needs an orch-core with ticket options): on the new-ticket form, the approve card for the requirements or plan gate, and the ticket page. Human-only (Mission Control, `orch addon ticket-option set`, or the phone); agents can read it. It is synced into the mirror as the cleartext field `notify`; the server pushes only while it is on.
- The phone's "Notify me about this ticket" switch is merged back into orch-core within one inbox cycle (recorded as `addon:orch-tix`). A push built before the desktop saw the phone's change cannot undo it (`notify_seen`).
- New setting `notify_unticketed` (default off): phone notifications for agent messages that name no ticket. Messages with `--ticket` follow that ticket's switch. Workspace join requests always notify.
- Needs the sharing CLI and TIX server of this release (`mirror notify-state`, `space notify`).
- The TIX section on orch-core's How it works page (`guide.section`) is now declared in the manifest (it was held back until an orch-core with the slot shipped; orch-core's guide page has).
- **Needs orch-core with `ticket_options` and `guide.section`:** orch-core validates a manifest strictly, so an older orch-core rejects this version. Update orch-core first.

## 0.2.0

- A new always-on provider `needs-watch` syncs a ticket to the phone within about a second when its events change (question asked, gate waiting, decision applied) instead of at the next outbox pass (core pumps every `pull_seconds`, 60 s). The periodic pass stays as the fallback and skips what the watcher already synced. Runs while Mission Control is open or "Keep syncing while Mission Control runs" is on.
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
