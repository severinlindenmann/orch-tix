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
  - Per space: its id, the owner device, when the desktop was last seen and **`notify_messages`**: whether agent
    messages that name no ticket may notify your phone (a yes/no, off by default).
  - Per mirror: `status`, `priority`, `needs` (question, approval or verdict), the number of open questions,
    the schema version, the mirror rev and timestamps, and **`notify`**: whether this ticket may notify your phone
    (a yes/no, off by default; see Push notifications). It also stores the pushing device's `project` and its
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
- **Push notifications are per ticket and off by default.** The server sends a push for a mirror only while its
  cleartext `notify` is on, and for an agent message only when its ticket's `notify` is on or, with no ticket, its
  space's `notify_messages` is on. A workspace join request ("<device> wants to sync") always notifies, because it
  is a security prompt. The switch is the owner's: Mission Control (new ticket, the approve card, the ticket page)
  sets it through the addon, and the app's "Notify me about this ticket" sets it through a browser-session-only
  endpoint (a device gets 403). Honest limit: the desktop's device credential, which the `sharing` CLI uses for
  agents too, can also write `notify` in a mirror push, because the same credential pushes mirrors. orch-core keeps
  agents from setting it through orch (human-only everywhere), and the sharing skill tells agents not to, but a
  process that holds the device token could. The worst outcome is a notification on your phone, never access to
  content: the flag is not a security boundary, only a courtesy switch, and the server still never sees any text.
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
