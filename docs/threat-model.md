# TIX threat model

What TIX protects, what it does not, and exactly which fields the server can read.

## Threat model (honest version)

- **Protected:** a stolen disk, database, copy or log; a passive attacker on
  the server; a swapped, reordered or truncated blob (detected, never decrypted).
  A stolen database or copy is safe only behind a strong passphrase: it holds
  `wrapped_mk` and the KDF salt, which allow offline PBKDF2 guessing.
- **Not protected:** an attacker who controls the server *and* serves you modified
  JavaScript while you log in (inherent to web-delivered crypto); likewise the
  unauthenticated installers (`curl | bash`, `irm | iex`) and `sharing update`,
  which trust whatever the server sends; a compromised onboarded device; an XSS
  that gets past the CSP, the sanitizer and the sandboxed preview.
- **The one exception: transcripts.** When a Deepgram key is set and
  "Transcribe audio uploads automatically" is on (the default), **every audio
  file you upload from the browser or the PWA** (a recording, a picked, pasted
  or dropped `audio/*` file, or a queued one once it uploads) **is sent to
  Deepgram (api.deepgram.com) unencrypted, over HTTPS**, straight from your
  browser. So is an audio file whose **Transcribe** button you press in the file
  view. That is the only place file content leaves your devices; the CLI has no
  `transcribe` command. The transcript
  comes back into the file's encrypted metadata; the server never sees the audio
  or the transcript in the clear. Turn the toggle off on the Settings page to
  send nothing unless you press Transcribe.
- **The Deepgram key** is set on the web UI's Settings page. The server stores it
  only as ciphertext under the master key, in the end-to-end encrypted settings,
  so it never sees the key; every approved device can decrypt it, and only a
  signed-in browser can change it. The browser sends it only to api.deepgram.com
  (the CSP's `connect-src` allows nothing else), and never logs or stores it.
- **Public links** (`https://tix.severin.io/p/<token>#<key>`) are created only on
  request. The key after `#` never reaches the server (browsers don't send the
  fragment), which stores only the token's SHA-256 and the file's key wrapped
  under the link key, so the server still can't read the file. **Anyone holding the
  link can read that one file, including its name, note and transcript.** Revoking
  a link, its expiry or its download limit stops the server from serving the
  ciphertext; it **doesn't take back a copy already downloaded**, and whoever has
  kept the link and the ciphertext can still decrypt it. A download counts when
  the transfer starts, so an aborted transfer still uses up one of a limited
  link's downloads. Deleting a file revokes its links, and revoking a device
  revokes the links that device created.
- **Upload links** (`https://tix.severin.io/u/<token>#<key>`) run the same trick in reverse: the
  key after `#` never reaches the server, so it stores only the token's hash and the link's private
  key sealed under your master key, and it never sees the sent file, its name or its note in the
  clear. Anyone with the URL can send exactly one file before the link is used up, expired or
  revoked; the sender is never authenticated, so treat what arrives like any file from a stranger
  (never auto-extract it, never follow instructions inside it).
- **Revocation** stops a device's API access at once. A revoked device that copied
  the master key can still decrypt ciphertext it obtains some other way; key
  rotation (v2) fixes that for future files. It may also still know the Deepgram
  key, so **after revoking a device, rotate the key at Deepgram** and save the new
  one on the Settings page.
- **Losing both the passphrase and the recovery key loses the files.** Nobody,
  including root on the server, can undo that.
- **Mirrors: what the server sees in cleartext.**
  - Per space: its id, the owner device and when the desktop was last seen.
  - Per mirror: `status`, `priority`, `needs` (question, approval or verdict), the number of open questions,
    the schema version, the mirror rev and timestamps. It also stores the pushing device's `project` and its
    name as `created_by_name`, and a `type` column (always `feature` for a mirror).
  - Per decision: the space id, its kind, the TIX number, the browser session's name that sent it, its key
    version, timestamps and the ack.
  - Per message: the space id, the TIX number, the sender's name and device, to whom, its kind, its size, the
    FILE ids and timestamps; per recipient, when it was acked.
  - For join requests: the space id, the requesting device, the owner device at the time, the status and who
    decided it when.

  It never sees titles, local keys such as `DEMO-0042`, question text, answers, gate text or file names. Those
  are sealed under the master key (SHR1 envelopes, AAD prefixes `sharing/space|mirror|decision|msg/v1|`).
- **Mirror reads are S|D, so they are not a confidentiality boundary.** Every approved device and every
  signed-in browser may list and read the sealed mirrors (`GET /api/mirrors`). Owner-gating the uuid lookup only
  stops accidents.
- **Cross-workspace exposure.** Every TIX device holds the master key, so it can read every mirrored ticket of
  every workspace, including a remote agent's VM of another client.
  - Use the `title` redaction (the default) or `key-only` for confidential clients. `key-only` sends no title
    and no question text.
  - At `full`, the TIX section on Workspace & addons shows the neutral line "Every TIX device can read synced
    tickets". A device cannot read the device list (that is browser-only), so the addon cannot tell which other
    devices exist. It shows a warning only in the rare case where a device list is known and holds another
    project's active device.
  - Per-space keys are planned as A4.1.
- **The pairing key.** On the desktop it lives in orch's `remote-humans.json` (owner-only). On the phone it is a
  non-extractable HMAC key. A paired phone's decision is applied only when that HMAC, its age and the
  workspace's per-kind permission check out; anything else waits for a desktop Apply. A local agent with shell
  access can read the desktop file; that is orch's documented limit, not a TIX guarantee.
- **TIX never writes tickets.** The server stores sealed decisions; only the desktop (Mission Control, or orch-core
  for a paired phone) changes a ticket, as the human. Devices cannot post decisions (403), only the space's owner
  device writes mirrors and acks, and a decision older than 14 days is never applied.
- **Push notifications** (payload v2) carry only the space id, `TIX-n`, the kind and two counts:
  `{"v": 2, "s", "t", "k", "n", "c"}`. At most one needs push per workspace a minute: the first goes out at once,
  the rest of a burst follow as one `"k": "batch"` push with their TIX ids (`ts`), "9 new things need you in Acme". They hold no title, text, client name or local key. They still go through
  the browser vendor's push service (Apple, Google or Mozilla), encrypted to the subscription. The **VAPID
  private key** sits in the server database; it only lets someone send notifications to your subscribed
  browsers.
- **Downloads in Mission Control** are decrypted by the sharing CLI on the desktop, never in the browser. Core
  serves each one once, as an attachment (single-use token, 5 minutes, `nosniff`, sandbox CSP). Public and
  upload link URLs are shown once and never logged.
- **Legacy tickets** keep their old guarantees while read-only. The cleartext fields are status, `project`,
  `type`, priority, labels, due, parent and blockers, `created_by_name` (the creating device or session),
  claims and timestamps; titles, bodies, questions and answers stay encrypted. Since 2.0.0 the CLI no
  longer runs a ticket's `testing.run` commands.
- **Ticket HTML attachments run in an isolated sandbox** (`/sandbox/html`): an opaque
  origin, no cookies, no IndexedDB, no access to tix's DOM or keys, and `connect-src 'none'` (no
  fetch, XHR, WebSocket or beacons). Its only network paths are loading scripts, styles and fonts
  from the two CDNs and Google Fonts — a script could encode data into those URLs, and it can also
  navigate its own frame to an arbitrary URL (there's no `allow-top-navigation`, so it can't escape
  the frame, but a `location` change is itself a request). Either way it can only leak the
  attachment's own content, never tix's.

## Remote bridge (Orch Remote)

What changes when a workspace is used from a phone or another computer through TIX. The wire format, keys
and every rule below are in [bridge-protocol.md](bridge-protocol.md) (cited as §n); this section says what
that means for you. It describes the bridge as specified and as the TIX server code (the mailbox and the
presence routes) implements it. The host side in orch-core and the TIX app side are still being built (#25,
#26, orch-core#85), so a statement about the host or the app is the specification's, not yet a shipped
behaviour, and is marked *planned* where it matters.

### Bridge data classes

| Class | Examples | Who can read it |
| --- | --- | --- |
| Sealed body | request path and headers, request body, dashboard pages, ticket text, terminal output, file contents | the host and every holder of the master key (see "A holder of the master key"); never the server |
| Envelope header (cleartext, authenticated) | version, direction, flags, key version, workspace id, device id, request id, stream id, sequence number, timestamp, salt | the server and anyone on the path |
| Mailbox facts | which client posted, the browser tab id, whether a host is online, sizes, timing | the server |
| Presence | workspace id, last heartbeat, goodbye time, three counts, a Factory code and up to three integers | the server, and any signed-in browser or approved device |
| Names | machine and device names (plaintext today); workspace labels (sealed) | the server for device names; only master-key holders for labels |
| Secrets | master key, workspace channel key, device signing key, host signing key, pairing secret | never the server (the device signing key never leaves its browser) |

### What the server sees in the clear

- **Envelope header** (§3.2): the 104-byte header is cleartext but authenticated (it is the AAD and is
  signed). It shows the version, direction, flags, key version, workspace id, the device id, the request
  id, the stream id, the sequence number, the sender's clock and the per-envelope salt. The device id is
  stable per device and workspace, not across workspaces (§2.4). The sequence numbers and the sender's
  clock are visible too, so the server can tell how many requests a device has made and what its clock
  says.
- **Sizes and timing.** The mailbox stores the body as the base64url text it received and checks only its
  shape and size (`fileshare/routes/bridge.py` `_sealed`; `fileshare/bridge.py` docstring). It therefore
  sees each envelope's size, when it was posted and fetched, and the number of chunks of a response.
- **Mailbox routing facts.** Which client posted a request (an approved device's id or the signed-in
  session), the request's tab id (chosen by the browser, never interpreted), whether it is a stream, and
  whether the host is online (`Route` in `bridge.py`; `host_online` in the post reply). It also sees
  the host's device id and a holder id for the lease. A refusal or a lease change is written to the server's
  log with the workspace id and, for a lease, the device id (`bridge.refuse`, `host_lease`); no body is
  logged.
- **Presence** (`fileshare/presence.py`, migration 010): the workspace id, the owning device's id, the
  time of the last heartbeat and of the goodbye, three counts (sessions, in progress, needs you), a short
  Factory state code (`none`, `running`, `paused`, `waiting`, `ready`, `stopped`, `done`) and three
  optional integers (children done, children total, budget percent). The state (online, not answering,
  lost, stopped, never started) is derived from those times on read. The table holds no name. Any signed-in
  browser or approved device may read it (`GET /api/presence`).
- **Machine and device names are plaintext today.** `devices.name` is stored in the clear and is returned
  as `owner_name` with every space, including by `GET /api/presence`, which is how the status page groups
  workspaces by machine. Workspace labels are different: they are sealed under the master key (`enc_label`).
  Sealing device names is tracked in [#75](https://github.com/severinlindenmann/orch-tix/issues/75); until it
  ships, a server operator can read your machine and device names.

### What the server never sees

Request paths and headers, page and response content, ticket text, terminal output and keystrokes, file
names and contents, workspace labels and any key. All of those are inside the sealed body (§3.5) or never
leave a device. The mailbox keeps a sealed request for at most 60 seconds, deleted when the host fetches it
(`bridge.TTL_S`, `take_requests`); sealed page chunks the same way (`take_pages`); stream frames are kept in
memory only and are not written to the database (`add_frame`). Pending rows are lost on a restart on purpose
(`bridge.py` docstring).

### Scenarios

**A curious or compromised TIX server.**
- Cannot: read any body, forge or alter an envelope (every envelope has an authentication tag and a
  signature), make the host run a request twice (§5), forge a response the device will accept (responses
  need the host's signature, checked against the host key the device pinned at pairing, §7), or forge a
  request the host will accept (requests need a registered device's signature, §6.1).
- Can: see the metadata listed above, drop, delay or reorder envelopes, replay captured bytes and refuse
  service. A replayed envelope is answered from the host's record of it or refused, never run again; a
  record is kept for 900 seconds, and after that the used sequence number refuses it (§5.3). Within the
  timestamp window (300 seconds each way, §5.1) the host also checks the sequence number, so an old capture
  does not run.
- A server that is also given a copy of the master key is the next scenario.

**A holder of the master key.** Every approved CLI device in any repo and every signed-in browser holds the
master key, and the channel key is derived from it (§2.2). Anyone who holds it and obtains envelope bytes
can read them. That includes a CLI device you have since revoked in TIX, if it kept the key. Version 1 has
no forward secrecy: a later compromise of the master key exposes traffic recorded earlier. Mixing a
per-device secret into the key is planned for version 2 (owner decision D1, §14; the labels end in `v1` so
it can arrive without breaking v1). Holding the key does not let anyone *act*: running a request needs a
registered device's signature, and accepting a response needs the host's (§1).

**A compromised TIX web app** (the server serves modified JavaScript). The owner has accepted that it can
act as a paired device, at that device's scope, in any browser that loads it, while it is loaded (§1). It
can also recover the master key, as the existing threat model already says, and so read every bridge envelope
it can obtain. It can show you one action and ask for an assertion over another; against this attacker an
assertion proves only that you did a user verification on that device around then (§9.6). It cannot export an
existing device key, act above that device's scope, or outlast the device's revocation on the host.
- **Wider case, decided by the owner (D8):** if the web app is compromised *during pairing*, it can mint a
  permanent device with a key that lives outside the browser and a software authenticator, whose "fresh"
  confirmations are forged without any real user verification, until you revoke it (§1, §9.1, §14).
- **Mitigations the specification requires of the host** (*planned*, host side, orch-core#89): devices are
  added only from the Remote tab on the computer; every addition and change is written to an audit log
  and listed at every remote start; every assertion-backed action is listed with its subject in the Remote
  tab; fresh confirmations are rate limited to 6 per 10 minutes per device (§2.7, §9.5, D8, D9). These
  expose a forged device after the fact; they do not prevent it.

**A local process or coding agent as the same operating-system user on the host.** It can read the master
key (in the workspace's sharing configuration) and, by file permissions alone, the host key, the registry of
paired devices, the stored request records and the audit log, because the user is shared (§1, §2.7). The
orch command guard refuses an agent's commands on those paths (the registry, the host key, the replay store
and its stored bodies, the channel key and the command that prints it), and it is the only barrier. It
deters careless and accidental changes and exposes them at the next start; it does not stop a determined
agent that also rewrites the audit log, and an indirect write may get past a guard that matches command
patterns (§2.7, D8). This is orch's documented limit for local agents; the bridge does not change it.

**A stolen phone or browser profile.** The device signing key is a non-extractable key and cannot be copied
out of the browser (§2.4); the master key is likewise held as a non-extractable key and the passphrase is
never stored (§2.1). A person holding an unlocked, signed-in browser can still act at that device's scope, and
the Type scope and Factory actions additionally need a fresh platform confirmation (Face ID, Touch ID,
Windows Hello or device PIN, §9). Revoke the device on the host (it takes effect on the next request and
ends its streams, §8.3) and sign it out on the Devices page. **Not yet in place:** signing out must clear the browser's bridge database (device key, channel keys,
sequence counters). The TIX app does not store any of these yet, so there is nothing to clear today; making
sign-out clear them is a required item of a later ticket (the app side of the bridge, #25), and until it
ships, revoking the device on the host is the control.

### What each scope allows, and the accepted limits

The four scopes, Look, Decide, Operate and Type, are decided by the **host for every request, by device
identity** (the signature), never by whether someone can read a key (§1). Every route starts as never
remote and opens to a scope only through an explicit tag (orch-core#85); a typing lease lasts 15 minutes
and covers only input to a terminal the same device opened, while starting a terminal or an agent and each
AI Factory action need a fresh confirmation over the exact thing shown (§9.4, D3). Operate stays one
scope, and editing tickets can steer running agents.

Accepted limits:
- A device can read whatever the host returns to it at its scope.
- **Typing leaks its rhythm.** Each keystroke is one request, so the server sees when and how much you type
  in a terminal, though not what (§4, F4). Batching or padding is a follow-up (R6).
- Confirmations are text the host supplies; the app shows them as plain text with invisible characters
  removed, and visually similar letters can remain, so the app also shows the ticket key and the first
  digits of a digest to compare (§9.3).

### What is not protected

- The master key against every device that holds it, including revoked ones; and recorded traffic after a
  later key compromise (no forward secrecy in v1).
- Metadata: which workspace, which device, when, how much, and how often you type.
- Machine and device names, until [#75](https://github.com/severinlindenmann/orch-tix/issues/75) ships.
- Availability: the server can refuse to carry anything.
- A compromised web app at pairing time, and a determined local agent on the host (see above).
- Sealed content once it reaches a screen or a host: the host runs what a device with the right scope asks.
