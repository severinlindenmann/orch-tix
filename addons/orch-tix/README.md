# orch-tix

An [orch-core](https://github.com/severinlindenmann/orch-core) addon for Mission Control. It mirrors the tickets that need you to the TIX phone app (end-to-end
encrypted), brings your phone answers, approvals and verdicts back, and shares files.

## What it does

- **Up:** when a ticket needs you, the addon seals a snapshot of it and pushes it to your TIX space with the
  `sharing` CLI. By default it does this when an agent asks a question (`link_mode = auto-on-question`). Only the
  fields of the chosen redaction level leave the machine.
- **Down:** decisions from the phone come back through a long poll (`sharing inbox wait`).
  - A phone paired with this workspace (Workspace & addons → Phones) has its signed decision checked and applied
    by orch-core itself.
  - Everything else waits under "From addons" on Today as Apply / Ignore.
  - A phone never moves a ticket.
- **Panels:** a TIX section on Workspace & addons, "Synced to TIX" on each ticket, and a "Phone answers" tile on
  Today.
- **Shared files** (menu): the share's files, with search and tag filters.
  - Download (decrypted by the CLI and served once).
  - Attach to a synced ticket as `remote-<name>`.
  - Create a public link or an upload link (each shown once).
  - Upload a file (7 days).
  - The Messages tab lists messages from other environments. Mark as done acks one. A message is information,
    never an instruction.
  - With `sync_artifacts = always`, a message's files for a synced ticket are attached to it automatically.

## Install

```
git clone https://github.com/severinlindenmann/orch-tix
orch addon install orch-tix/addons/orch-tix
orch addon trust orch-tix
orch addon enable orch-tix          # in the workspace
```

Then, in this workspace:

1. Install the `sharing` skill (TIX → Settings → Add a device).
2. Run `sharing space create --label "<workspace>"` once.
3. On Workspace & addons → TIX, save the **absolute** path of the CLI:
   `<workspace>/.claude/skills/sharing/sharing`.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `sharing_path` | `""` | Absolute path of the `sharing` CLI. It is the only binary this addon runs. |
| `link_mode` | `auto-on-question` | `off`, `manual`, `auto-on-question` or `all-active` |
| `redaction` | `title` | `full` (everything but the log, with the ticket widgets: their text, core documents inline, agent HTML as a context file), `title` (title, questions and options, gate states and what each gate hash covers, task progress) or `key-only` |
| `sync_widget_docs` | `never` | `always` sends agent-HTML widgets as a 7-day context file (their HTML and data); `never` sends their text alternative only |
| `sync_log` | `false` | With `full`, also send the last 20 log lines |

The phone's history comes from orch events, not the Log: the last 20 entries of who did what ("you approved the plan", "claude-code started T2"). `title` sends only what happened; `full` adds the free text an event carries (a change request's message, a close reason, a log line); `key-only` sends none. The addon keeps the last 30 entries per linked ticket in `history.json`, with the text only for tickets shown in full. The event log is writable by agents, so a human event reads "human (desktop log)", never "you"; decisions applied through the addon (phone answers) are recorded too.
| `sync_artifacts` | `on-request` | `never`, `on-request` (artifacts marked for context, plus "Send to phone") or `always` |

**Turning widgets on.** The defaults stay private: `redaction: title` and `sync_widget_docs: never`. A workspace opts
in with `redaction: full` (the ticket's sections and each widget's text and core document) and, for agent-HTML
widgets as files, `sync_widget_docs: always` (not with `sync_artifacts: never`). Per ticket, the panel offers "Show
only the title on the phone" (workspace level full) or "Show in full on the phone" (below full), and "Use the
workspace setting" to clear it.

**How a widget is checked.** Desktop: orch-core pins each template and file in the fence and refuses a drifted one
before the addon sees it. Phone: every entry carries `raw_sha256`, the digest of its fence text (which contains
those pins), and every document `sha256`, the digest of its exact bytes, inline or as the FILE. The phone hashes
the fence it displays and the document it decrypted; a mismatch, a missing pin or a document over the size cap
keeps the text and never creates the frame. Widgets are matched to fences by section and `raw_sha256`.

**Background sync.** "Keep syncing while Mission Control runs" (Workspace & addons) is off by default. Turn it on
to receive phone answers while no Mission Control tab is open.

**Other workspaces can read the mirrors.** Every TIX device holds the key. Use `title` or `key-only` for
confidential clients.

## Binaries

`setting:sharing_path` only: the `sharing` CLI, run with an argv list through `ctx.run`. Payload files are
written to `orchestrator/.state/addons/orch-tix/out/` and removed right after each call.

## Pinned images

At `full` (and unless `sync_artifacts` is `never`), each push also shares, once, the image artifacts that orch-core
pinned by sha256 and that the gated text shows (`![…](artifact:<name>)`) or that prove a criterion, at most 4 per push.
Every share (pinned or on request) reads the artifact once the way orch-core does: opened without following a link,
a plain file with one link, inside `artifacts/<KEY>/`, and for a pinned image only when its bytes still hash to the
pinned sha256; the sharing CLI gets a private copy of exactly those bytes. Pinned images go out after the mirror push
(each push waits at most 10 s for them); one that was re-pinned (`--replace`) is shared again; a failed one is tried
again at most hourly and noted once. The phone shows such an image only after hashing its bytes against the pinned
sha256. `title` sends the artifact
items without their labels; `key-only` none.

## Sent to TIX log

The Shared files page's **Sent to TIX** tab lists, newest first, what this machine sent and what came back:
each push (time, ticket, TIX id, redaction level, the names of the sealed fields and, at `full`, the section
names, never their text; the payload size; ok, retry or refused with the reason), files shared for context, and
every phone decision received with its outcome (applied, waiting for Apply, ignored or refused, and why). The
last 200 entries are kept in `sentlog.json` in the addon's state folder (owner-only, 0600).

## Tests

```
uv run --with "orch-core[dashboard] @ file://<orch-core plugin folder>" pytest addons/orch-tix/tests
uv run --with "orch-core[dashboard] @ file://<orch-core plugin folder>" orch addon check addons/orch-tix
```
