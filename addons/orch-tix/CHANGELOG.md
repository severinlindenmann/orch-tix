# Changelog

## Unreleased

- Phone v4: at `full`, pinned images the gated text shows or that prove a criterion are shared (once, at most 4 per push) so the phone can show them after verifying their sha256; `title` keeps artifact items without labels.
- The Shared files page has a "Sent to TIX" tab: every push (ticket, redaction level, the names of the sealed fields, size, ok / retry / refused), context files, and the phone decisions received with their outcome. The last 200 entries, in `sentlog.json` (0600).
- The devices table has three columns and empty tables say what next (`orch addon check --strict`; the "TIX" product name passes once orch-core lists it as a proper noun).
- Phone verdicts carry the verdict hash (schema 1.3): the Apply intent passes it as `expected_hash`, a verdict without it or with another is stale, and `title` keeps the document's `verdict` {hash, round}.
- The phone's history comes from orch events (who, what; free text only at `full`) instead of the Log.
- `title` also sends each gate's `covers` (orch-core hash v2), so the phone can say what an approval binds.

## 0.1.0

- Mirrors tickets to TIX with redaction, takes phone decisions back (direct apply for paired phones, Apply / Ignore
  otherwise) and adds the TIX section, the Synced to TIX panel and the Phone answers tile.
- Shared files page: download, upload, public and upload links, attach to ticket, messages with Mark as done.
