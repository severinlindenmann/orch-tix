# Operating TIX

Detail that the README leaves out: the orch-core ticket bridge, upload links from the CLI, and design-token upkeep. Deploying to a VPS is in [infra/RUNBOOK.md](../infra/RUNBOOK.md).

## Tickets on orch-core

Tickets live in **orch-core workspaces** (one per client repo, `orchestrator/tickets/…`). Those workspaces are
the source of truth. TIX is the phone side:
- The `orch-tix` addon of Mission Control (`addons/orch-tix/`) mirrors the tickets that need you (a question,
  an approval, a verdict) into the workspace's TIX space, sealed. They appear as `TIX-42`.
- The PWA shows them under **Needs you**.
- **The phone writes decisions, never tickets.** An answer, an approval, a request for changes or a verdict is
  sealed and sent to the desktop.
- **An approval covers exactly what the phone showed.** The ticket document names what each gate's hash binds
  (`gates.<g>.covers`: the gated sections, a non-empty Summary, and for requirements also size and type). The
  phone shows all of it, computes the hash itself and offers Approve only when it matches. Without `covers` (an
  orch-core from before hash v2), below `full`, or on a mismatch it approves nothing; Request changes still works.
- **A verdict binds the criteria and evidence.** The document's `verdict.hash` (schema 1.3) covers the ticket's id,
  status, Acceptance criteria and Verification. The phone sends it with Done and Send back. It offers Done only at
  `full`, after hashing what it shows; at `title` only Send back. An epic's verdict and a document without the hash
  stay on the desktop.
- **History comes from orch events**, not the Log: the addon sends who did what (with the event's text only at
  `full`). The event log is writable by agents, so the phone labels it "not verified" and never shows a logged
  human event as "you"; only the decisions this phone sent are listed as its own.
- **Hidden characters** (controls, bidi and zero-width marks, private use, unassigned, invisible fillers: the set
  orch-core refuses) show as visible `<U+XXXX>` badges. The phone neither approves a gate nor sends an answer on
  text that holds one; it asks to have them removed.
- **The desktop applies them.** A phone paired with that workspace is you: orch-core verifies its signed decision
  (answers, approvals, requests for changes, verdicts and ticket requests) and applies it at once, with no second
  step. The phone says "applying on your desktop" while it is in transit and shows the outcome when the desktop
  acks it; a refused one says why (the text changed since, or the ticket moved on). An unpaired phone's decisions
  wait in Mission Control for an Apply / Ignore, and the phone says so. When the plan is drafted together with the
  requirements, the phone offers "Approve requirements and plan" as one decision bound to both hashes.
- Agents keep working with the orch skills and `orch wait`. Messages between environments go through
  `sharing msg`.
- Standalone TIX projects without an orch-core workspace are out of scope.

Agents keep working with the orch skills; `sharing whoami` shows the skill version and `sharing update`
installs the newest. Connecting a workspace step by step is in `infra/RUNBOOK.md` §8.

The ticket document the addon mirrors is in `docs/ticket-format-example.md` (`orch schema example`).

## Upload links

An upload link (`https://tix.severin.io/u/<token>#<key>`) lets someone without an account send
**one** file (or a folder, zipped) into your share: `sharing upload-link create --label '…'`
prints the URL once. The sender needs no onboarding — `sharing upload-link put '<url>' PATH...`
encrypts the file to that link's key and sends it, exactly like `open-link` on the receiving side.
It becomes a normal FILE only once an owned device (or the web UI) adopts it, which `sharing list`,
`get` and `info` do automatically; `sharing upload-link list` shows each link's state (`waiting`,
`pending`, `received`, `revoked`, `expired`) and `sharing upload-link revoke` revokes one.

## Design tokens

The colours, type and spacing come from orch-core's design tokens: `fileshare/static/css/tokens.css` is
orch-core's generated file, copied as is and checked against `css/SHA256SUMS`. Never edit it here; after a
token change in orch-core run `uv run python scripts/sync_tokens.py --from ../orch-core` (`--check` only
compares) and commit both files. app.css keeps only TIX's own sizes and older names mapped onto the tokens.

