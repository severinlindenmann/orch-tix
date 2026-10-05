# Bridge protocol, version 1

What is inside the sealed bodies that the bridge mailbox (`fileshare/bridge.py`, `fileshare/routes/bridge.py`)
carries between an `orch` host and the owner's browsers: which keys seal and sign, where each key lives,
how a device is recognised, the exact bytes, and what each side does with an envelope that is not right.

Status: **draft for review** (ticket #58, part of #22 and orch-core#85). The host side (orch-core R3, #89)
and the TIX app side (R10, #25) implement this document. The contract is
[`tests/bridge_vectors.json`](../tests/bridge_vectors.json); the reference implementation that produces
it, [`tests/support/bridge_protocol_ref.py`](../tests/support/bridge_protocol_ref.py), is test support
only and is not shipped. Where the reference and this document disagree, this document wins and the
reference has a bug.

Conventions: integers are unsigned big-endian. `||` is concatenation. Labels are ASCII byte strings
written in double quotes. `H(x)` is SHA-256. "Hex" is lower case. "b64u" is base64url without padding
(TIX's canonical spelling, `crypto.js` `b64u`). "MUST" and "MUST NOT" are requirements; anything else is
explanation.

Primitives, all from WebCrypto and the Python `cryptography` package, nothing else: AES-256-GCM with a
128-bit tag, HKDF-SHA-256 (RFC 5869), HMAC-SHA-256, ECDSA on P-256 with SHA-256, SHA-256. No compression
anywhere, ever (compression before encryption leaks content through length).

## 1. Threat model for the bridge

Parties: the **host** (`orch` serving one workspace with `--remote`, on the owner's computer, which is
also the TIX device that owns that workspace's space), **devices** (the owner's browsers and phones that
were paired with that host), the **TIX server** (relays the mailbox), and every other **holder of the
master key** (every approved CLI device in any repo, every signed-in browser; see §2.1).

| Who | Can | Cannot |
|---|---|---|
| The TIX server | See the cleartext header of every envelope (§3.2), sizes, timing, which session posted it; drop, delay, reorder or replay envelopes; refuse service. | Read any body; forge or alter an envelope (tag and signature); make the host run anything twice (§5); make a device accept anything as a response. |
| Another master-key holder (not a paired device) | Decrypt any bridge envelope it obtains. It obtains them only by also being, or controlling, the TIX server: the mailbox hands requests only to the space's owner device and responses only to the session that posted the request. | Make the host act: every request needs a registered device's signature. Make a device accept a response: every response needs the host's signature. |
| A paired device, honest or compromised | Act at its own scope; read whatever the host returns to it, and as a master-key holder, read any envelope it obtains. | Act above its scope (the host decides every request again); act as another device; act after revocation. |
| A compromised TIX web app (the server serving modified JavaScript) | Use the non-extractable device key of each browser that loads it, at that device's scope (an **accepted risk**, owner decision); obtain the raw master key, as today's threat model already says, and therefore read any bridge envelope it can obtain, for every workspace. It can also show the person one action and ask for an assertion over a different challenge: against this attacker, an assertion proves only that the person performed a user verification on that device at about that time (§9.6). | Export a device key to use elsewhere; act at a scope the device does not hold; outlast revocation on the host. |
| A local process on the host computer running as the owner | Read the master key (the workspace's `.claude/skills/sharing/config.json`) and the host key and registry (the orch config directory). That is orch's documented limit for local agents with shell access; this protocol does not change it. | Nothing this protocol can promise. |

Scopes (Look, Decide, Operate, Type) are enforced **by the host, by device identity** (the signature),
never by key secrecy: every device of the workspace can decrypt every envelope, so no scope may depend
on someone not being able to read something.

What this protocol adds to TIX's protections: authenticity of requests by device and of responses by
host, freshness and single execution of requests, and the binding of a platform-authenticator gesture to
the exact request it allows. It adds no confidentiality boundary between master-key holders (see D1).

## 2. Keys

### 2.1 What exists today (verified in this repository)

- **One master key (MK), 32 bytes, per account.** There is no per-space key yet ("per-space keys are
  planned as A4.1", [threat-model.md](threat-model.md)). Spaces (`fileshare/mirrors.py`) have a 32-hex id
  and a label sealed under MK (`sharing/space/v1|`). The bridge mailbox uses that same space id as the
  workspace id (`/api/bridge/{space}`), and only the device that owns the space may host it
  (`routes/bridge.py` `_host`, `mirrors.owner_space`). "Space key" in the ticket therefore means MK.
- **A browser** (desktop or phone, a signed-in session) holds the KEK and MK only as **non-extractable
  AES-GCM CryptoKeys** in IndexedDB (`login.js`, `setup.js`, `keystore.js`). A non-extractable AES-GCM key
  cannot be an HKDF input. A browser can still obtain the raw MK when it needs it, without the passphrase:
  `GET /api/keyblob`, then AES-GCM-open `wrapped_mk` with the stored KEK under AAD `"sharing/mk/v1"`. That is
  exactly what device approval does today (`approve.js` `approveDevice`). The passphrase is never stored
  on any device, phones included.
- **An approved CLI device** (the `sharing` skill) holds MK in the clear in
  `<repo>/.claude/skills/sharing/config.json`, mode 0600 or an owner-only ACL (`sharing.py`
  `write_config`, `secure_file`, `check_permissions`). It received MK sealed to its ECDH P-256 key at
  approval (`sealToDevice`, AAD `sharing/approve/v1|`), after the owner compared its fingerprint.
- **The host** is the CLI device that owns the space, so the host computer already holds MK.
- **The existing phone pairing** (orch remote humans, `pairing.js`): a 32-byte HMAC key per (space,
  phone), delivered in the fragment of a `/pair#…` link shown on the desktop, checked with a 6-digit code,
  stored on the phone as a non-extractable HMAC CryptoKey and on the desktop in orch's
  `remote-humans.json`. It signs decisions; it is symmetric, so the desktop can produce the same MAC.

Nothing in this specification changes any of that.

### 2.2 The workspace channel key K_ws (reused hierarchy, no new distribution)

```
K_ws = HKDF-SHA-256(IKM = MK, salt = "" (empty), info = "sharing/bridge/ws/v1|" || workspace_hex, L = 32)
```

`workspace_hex` is the 32 lower-case hex characters of the space id, as ASCII (the same convention as
`aadSpace`). An empty salt means 32 zero bytes (RFC 5869 §2.2), which is what both WebCrypto and the
`cryptography` package do with an empty or absent salt. Vector: `hkdf[0]`; a second workspace gives an
unrelated key: `hkdf[1]`.

- The **browser** derives K_ws once per workspace from the raw MK (re-opened as in §2.1), zeroes the raw
  bytes, and keeps K_ws only as a **non-extractable HKDF CryptoKey** (§10.1). It can recompute it at any
  time the same way.
- The **host** needs K_ws, not MK. The more restrictive arrangement, required here: the `sharing` CLI of
  the workspace derives K_ws and hands only K_ws to `orch` (a new CLI command, follow-up F1), the same
  way the orch-tix addon already uses the CLI instead of reading MK. `orch` MUST NOT read
  `config.json` itself.
- `key_version` in the header (§3.2) is TIX's MK version (`/api/keyblob` `key_version`, today 1). When MK
  rotation or per-space keys (A4.1) arrive, the IKM changes and a new label version is defined; a v1 host
  drops envelopes of any other key version.

### 2.3 Per-envelope keys K_msg and the nonce

```
K_msg = HKDF-SHA-256(IKM = K_ws, salt = header.salt (16 random bytes), info = "sharing/bridge/msg/v1", L = 32)
nonce = 12 zero bytes
```

Every envelope draws a fresh 16-byte `salt` from a CSPRNG at the moment it is sealed, so every K_msg seals
exactly one plaintext and the constant nonce is never reused with a key. This is the construction of
Tink's AES-GCM-HKDF streaming AEAD (a random salt into HKDF per message), chosen here because K_ws is
shared by many senders (every device and the host) with no shared counter.

- **Limit:** a nonce is reused only if two envelopes under one K_ws draw the same salt. For `q`
  envelopes the probability is at most `q² / 2^129`: below 2^-33 for 2^48 envelopes, which at a
  sustained 100 envelopes per second (the mailbox's per-host chunk ceiling) is about 89,000 years. Version 1 has no rekeying
  inside a key version, and needs none. A sender MUST draw the salt freshly for every seal operation;
  re-sending identical bytes is fine, re-sealing different bytes under a remembered salt is not.
- **No sender state:** nothing has to survive a reload, a second tab or a restored backup to keep nonces
  unique.
- AES-GCM is not key-committing. That is harmless here: there is one K_ws per (workspace, key version),
  the key is not chosen by the sender, and the signature covers the ciphertext.

Vectors: `hkdf[2]` (K_msg), `seal[0]` (`message_key`, `nonce`, `sealed`).

### 2.4 Device signing keys (new)

Each device generates one **ECDSA P-256** key pair, private half **non-extractable**
(`generateKey(..., false, ["sign", "verify"])`), stored in IndexedDB like the other CryptoKeys. One key
per browser profile, registered separately with each host (each workspace) it is paired with.

```
device_id          = H("sharing/bridge/device/v1|" || pub)[0:16]           pub = 65-byte uncompressed point
device_fingerprint = base32(H("sharing/bridge/fp/v1|" || pub))[0:20], grouped 4-4-4-4-4
```

The fingerprint is TIX's existing format (`security.py` / `crypto.js` `fingerprint`, 100 bits, so a key
cannot be ground to match a fingerprint the person compares) under a bridge label of its own. Vectors:
`ids`.

The host keeps its **registry in the orch config directory, never in the workspace**, one per
workspace: device id, public key, scope, label, paired-at, revoked flag, the sequence state (§5.2), and
the WebAuthn credential (§9.2). Its exact path is R3's; it is owner-only like `remote-humans.json`.

### 2.5 The host signing key (new)

Each workspace's host has one ECDSA P-256 key pair, created on the first `--remote` start and kept
owner-only in the orch config directory beside the registry. Its public key reaches a device only
through the pairing link, as a pin (§8.1):

```
host_pin = H("sharing/bridge/host/v1|" || host_pub)           (32 bytes, shown as b64u)
```

The device stores the host public key with its pairing and verifies every response with it. Version 1
has **no in-band rotation**: replacing the host key means the registry is reset and every device pairs
again (D6). A per-workspace key, not per computer, keeps one workspace's key from speaking for another.

### 2.6 Where every key lives

| Key | Where | Who can use it |
|---|---|---|
| MK | browser: non-extractable AES-GCM CryptoKey, raw only transiently via KEK + `/api/keyblob`; CLI: `config.json` in the repo | every master-key holder |
| K_ws | browser: non-extractable HKDF CryptoKey per workspace; host: memory of the `orch --remote` process | every master-key holder (it is derivable from MK) |
| K_msg | memory, one envelope | sender and readers of that envelope |
| device signing key | browser: non-extractable ECDSA CryptoKey; host: the public half in the registry | that browser |
| host signing key | orch config directory, owner-only; devices: the public half, pinned | the host |
| pairing secret S | the pairing link fragment (one use, 10 minutes) and the host's memory | whoever holds the link |
| WebAuthn credential | the platform authenticator (possibly synced, §9.5); host: public key in the registry | the person, by user verification |

## 3. Envelope

### 3.1 Layout

```
envelope = header (104 bytes) || ciphertext || tag (16 bytes) || signature (64 bytes)
```

The mailbox carries the envelope as b64u text in its `body` field and limits the **decoded** size:
a request envelope to **1 MiB** (1,048,576 bytes), a response chunk envelope to **256 KiB** (262,144
bytes) (`bridge.MAX_REQUEST_BYTES`, `MAX_CHUNK_BYTES`; checked by `test_size_limits_match_the_mailbox`).
The fixed overhead is 184 bytes, so a request plaintext is at most 1,048,392 bytes and a chunk plaintext
at most 261,960 bytes.

### 3.2 Header

| Offset | Size | Field | Request (to host) | Response chunk (to device) |
|---|---|---|---|---|
| 0 | 4 | magic | `"SHRB"` | `"SHRB"` |
| 4 | 1 | version | 1 | the request's version |
| 5 | 1 | direction | 1 | 2 |
| 6 | 1 | flags | only `STREAM` (0x02) may be set | `LAST` 0x01, `STREAM` 0x02, `REFUSAL` 0x04 (REFUSAL requires LAST) |
| 7 | 1 | key_version | MK version (1) | same |
| 8 | 16 | workspace | the space id, raw | same |
| 24 | 16 | device | the sender's device id | the device the response is for |
| 40 | 16 | rid | request id = the mailbox `id`, raw; 16 CSPRNG bytes | the request's rid |
| 56 | 16 | stream | the rid of the stream this request acts on, or zeros | the stream's rid for frames, else zeros |
| 72 | 8 | seq | the device's sequence number (§5.2), from 1 | the chunk index = the mailbox `idx`, from 0 |
| 80 | 8 | ts_ms | sender's clock, milliseconds since the Unix epoch | host's clock |
| 88 | 16 | salt | §2.3 | §2.3 |

Unknown flag bits MUST be zero. The whole header is cleartext and fully authenticated (it is the AAD and
it is signed). What it shows the TIX server beyond what the mailbox already shows (rid, idx, last,
stream, sizes, timing, posting session): the device id (stable per device and workspace), the sequence
numbers, the sender's clock, and which requests belong to which stream.

### 3.3 Sealing

```
AAD        = header (all 104 bytes)
ciphertext || tag = AES-256-GCM-Encrypt(K_msg, nonce = 12 zero bytes, plaintext, AAD)
```

Vector: `seal[0]` gives K_ws, the header, K_msg, the plaintext and `ciphertext || tag`.

### 3.4 Signature

```
signed    = "sharing/bridge/sig/v1|" || header || ciphertext || tag
signature = ECDSA-P256-SHA256(signer, signed), as raw r || s, 32 bytes each (IEEE P1363)
```

Requests are signed by the device's key, responses by the host's key. Raw `r || s` is WebCrypto's native
format; Python converts with `encode_dss_signature` / `decode_dss_signature`; DER is never used on the
wire (WebAuthn assertion signatures, §9, are a separate thing and are DER). A verifier MUST reject
`r` or `s` outside `1 … n-1`. ECDSA signatures are malleable (`(r, n-s)` also verifies, vector
`sign[3]`), so a signature MUST NOT be used as an identifier; the idempotency digest (§5.3) leaves it out.
Signing may be randomised (WebCrypto) or deterministic (the reference uses RFC 6979 only so the vector
file is reproducible). Vectors: `sign`.

The signature covers the ciphertext, not the plaintext (encrypt-then-sign): any party can check it
without the key, it binds the exact bytes, and the device can drop a forged response before decrypting.

### 3.5 Plaintext

```
plaintext = meta_len (4 bytes) || meta (canonical JSON, UTF-8, at most 65,536 bytes) || data (raw bytes)
```

Canonical JSON is TIX's (`crypto.js` `canonicalJson`, Python `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`). `data` is the HTTP body or the stream bytes, unencoded.

Request `meta` always has `op`:

| op | meaning | other fields |
|---|---|---|
| `http` | one dashboard request | `method`, `path` (with query), `headers` (the allow-list R2/R3 define; never `Cookie`, `Origin` or `Host`) |
| `cancel` | close a stream from the device side | header `stream` = the stream's rid |
| `pair`, `pair_status` | §8 | §8 |
| `credential_begin`, `credential_finish` | §9.2 | §9.2 |
| `assert` | §9.4 | §9.4 |

A stream is opened by an `http` request with the `STREAM` flag; its response chunks carry `STREAM`;
input to a stream (terminal keys) is an `http` request whose header `stream` names it.

Response `meta`: chunk 0 has `status` and `headers`; later chunks have `{}`, or `{"keepalive": true}`
with empty `data` (the host sends one at least every 20 s on an idle stream, inside the mailbox's 60 s
TTL). A refusal is a single chunk with `LAST | REFUSAL` and `meta = {"refusal": <code>, …}` (§6.2).

## 4. Chunks and streams

A page response is chunks `seq = 0, 1, …` with `LAST` on the final one, each a full envelope signed and
sealed on its own. A stream's frames are numbered the same way and end with `LAST`. The device MUST check
that the mailbox's cleartext `id`, `idx`, `last` and `stream` equal the signed header's `rid`, `seq`,
`LAST` and `STREAM`, and MUST accept chunk `n` only after chunk `n-1`. A truncated response (no `LAST`
by the request's timeout) fails as a whole; nothing of it is rendered.

The host closes a stream with a final chunk (`LAST`, and `REFUSAL` with code `revoked`, `scope_changed`
or `stopped` when it ends it for that reason) on revocation, scope change and the kill switch, and drops
the device's lease (§9.4). The device's `cancel` request ends a stream from its side. Terminal frames are
coalesced by R6; keystrokes are one request each, so the server sees typing rhythm and length (F4).

## 5. Time, order and single execution

### 5.1 The window

The host accepts a request only if `|host_now_ms − ts_ms| ≤ 300,000` (300 s each way). The device
accepts a response chunk only if `|device_now_ms − ts_ms| ≤ 300,000`. Exactly 300,000 ms is inside
(vectors `timestamp_at_window_edge`, `old_timestamp`, `future_timestamp`, `response_old_timestamp`).

### 5.2 Per-device sequence numbers

Each device keeps one counter per host, starting at 1, incremented with a readwrite IndexedDB
transaction per request so tabs never share a value. The host keeps, per device, `high` (the highest
accepted seq) and a 64-bit `bitmap` (bit `i` set means `high − i` was accepted). A seq is accepted if it
is above `high` (the window slides) or within the 64 below `high` and not yet seen; otherwise it is
refused with `stale_sequence` and `high`. Requests posted concurrently may arrive out of order, hence the
window rather than strict increase (vectors `repeated_sequence`, `sequence_below_window`,
`sequence_reordered_inside_window`).

**Resync rule:** on a `stale_sequence` refusal (verified host signature, §7), the device sets its counter
to `max(counter, high + 1)` and sends the request again as a **new** request (new rid, new seq, new
envelope); the refused one was recorded as refused and did not run. A device that lost its counter has
lost its key too (same storage), so it pairs again as a new device. Seq values stay below 2^53 (the
JavaScript safe integer limit); a device at that limit pairs again.

### 5.3 Idempotency

The rid is the idempotency key. Before anything runs, the host records, and **persists** across restarts,
together with the new sequence state:

```
rid → { device id, digest = H(header || ciphertext || tag), received_at, outcome }
```

`outcome` is the state (`accepted`, then `done` with the response head and up to 64 KiB of body, or the
refusal code). The record is kept for **900 s**, more than the 600 s span of timestamps the window lets
in, so any envelope the window accepts is still recognised. For a known rid:

- same device and same digest: a **replay**; the host never runs it again and answers with the stored
  outcome (re-sealed with a fresh salt), or `already_done` with the stored status when the body was not
  kept (vector `replayed_request`);
- anything else: `rid_conflict` (vector `same_rid_other_content`).

A device retrying after a lost response MUST resend the identical envelope bytes. After 900 s the
timestamp is out of the window, so an old envelope is refused and never runs
(`test_replay_never_runs_twice`). The signature is not in the digest because it is malleable (§3.4).

## 6. What the host does with a request

### 6.1 Order (normative)

The rule: **silence for anything a party without K_ws could have produced; a sealed, host-signed
refusal for everything after the tag verified**, because only a master-key holder gets past the tag.
The server and other outsiders therefore learn nothing new about the host's state from any refusal (the
mailbox already shows that a response exists and its size).

1. **Shape** — drop if: the size is outside 184 … 1,048,576; magic ≠ `"SHRB"`; version ≠ 1; direction
   ≠ 1; a flag other than `STREAM`; key_version or workspace not this host's.
2. **Tag** — derive K_msg, open. Drop on failure (vectors `tampered_tag`, `tampered_header_field`,
   `other_workspace_key`).
3. **Framing** — refuse `malformed` if the plaintext framing or meta JSON is invalid.
4. **Device** — the header's device id in the registry:
   - unknown: `op = "pair"` goes to §8.1; from a device id with a pending pairing, `pair_status`,
     `credential_begin` and `credential_finish` are verified with the pending key and go to §8.1 and
     §9.2; anything else is refused `not_paired`;
   - revoked: refuse `revoked`;
   - otherwise verify the signature with the registered key; refuse `bad_signature` on failure
     (vector `wrong_device_signature`).
5. **Replay** — §5.3.
6. **Time** — refuse `stale_timestamp` (with the host's clock, `host_ms`) outside the window.
7. **Sequence** — refuse `stale_sequence` (with `high`) per §5.2.
8. **Record** — persist the rid record and the sequence state.
9. **Authorise and run** — the route's scope tag against the device's scope (R2), then the assertion
   requirement (§9), then the request. Refusals here: `forbidden_scope`, `assertion_required`,
   `lease_required`, `assertion_failed`.

The host does one AES-GCM open (up to 1 MiB) before the signature check; the mailbox limits each client
to 20 requests per second, which bounds that work. The host MAY drop instead of refusing when one
device or one unknown device id has drawn more than 10 refusals in a minute.

### 6.2 Refusal codes

`malformed`, `not_paired`, `revoked`, `bad_signature`, `rid_conflict`, `already_done`,
`stale_timestamp`, `stale_sequence`, `pairing_closed`, `forbidden_scope`, `assertion_required`,
`lease_required`, `assertion_failed`, `scope_changed`, `stopped`. A refusal is recorded as the rid's
outcome like any other outcome. Its text is fixed per code; it never echoes request content.

## 7. What the device does with a response chunk

Drop (and never render) unless, in this order: the size is within 184 … 262,144; magic, version (equal
to the request's), direction 2 and the flags are valid (`REFUSAL` requires `LAST`); workspace,
key_version and device are its own; the rid is one of its pending requests; the mailbox fields match
the header (§4); **the host signature verifies with the pinned host key**; the tag verifies; `seq` is the
next expected index; the timestamp is in the window (vectors `device_cases`). A chunk that a device key
signed is not a response (`response_signed_by_a_device`): this is what stops another master-key holder,
or the server, from answering in the host's name. A refusal chunk is shown as the host's fixed message
for its code.

## 8. Pairing and revocation

### 8.1 Pairing a browser or phone

1. **On the Mac** (the Remote tab, orch-core R5) the owner chooses "pair a device" and a scope. The
   host creates an offer: `pairing_id` (16 random bytes), secret `S` (32 random bytes), the scope, an
   expiry of 10 minutes, single use. It shows a link, as a QR code and for copying:

   ```
   https://<tix-origin>/remote/pair#v1.<workspace hex>.<pairing_id hex>.<b64u S>.<b64u host_pin>
   ```

   The fragment never reaches the server, as with TIX's public links and the existing `/pair` link.
2. **On the device**, the TIX app (signed in, so it holds MK) parses the fragment strictly, derives
   K_ws, generates its signing key (§2.4) and sends a request with `op = "pair"`:

   ```
   meta = {"op": "pair", "pairing_id": hex, "pub": hex (65 bytes), "label": text (≤ 80),
           "mac": hex HMAC-SHA-256(S, "sharing/bridge/pair/v1|" || workspace || pairing_id || pub)}
   ```

   signed with the new key (proof of possession), header device id = `device_id(pub)`.
3. **The host** checks, in this order after §6.1 steps 1–3: the offer exists, is open and unexpired;
   `device_id(pub)` equals the header's; the signature verifies with `pub`; the MAC verifies (a wrong
   MAC gets the same `pairing_closed` refusal as no offer); the timestamp is in the window. It marks the
   offer used and shows on the Mac the label (as text only), the scope, and the **device fingerprint**.
   It answers `pending` in a response signed by the host key.
4. **The device** verifies that `H("sharing/bridge/host/v1|" || host_pub) = host_pin` from the link,
   then the response's signature with that `host_pub`, and pins it. It registers its platform
   credential now (§9.2) and shows its own device fingerprint: "compare this on your Mac".
5. **The owner compares** the two fingerprints and approves on the Mac (possibly lowering the scope);
   the Mac shows whether a credential was registered, and whether it is synced (§9.6).
   The host writes the registry entry. The device asks with `op = "pair_status"` (signed with its key,
   read-only, so a replay changes nothing) until the answer is `approved` or `rejected`.

What each part protects: the link's `host_pin` authenticates the host to the device with no human
comparison; `S` keeps strangers and other master-key holders from opening pairings; the fingerprint
comparison catches a link that reached someone else, at 100 bits of second-preimage resistance (the
attacker cannot choose the honest device's key). Vectors: `pairing`, host cases `pair_request_first`,
`pair_request_twice`, `pair_request_bad_mac`, `pair_request_no_offer`, `pair_status_pending`,
`pair_status_signed_by_other_key`.

Another computer pairs the same way, from the copied link. The link is a secret for 10 minutes: never
logged, shown once.

### 8.2 The existing phone pairing

The orch HMAC pairing (§2.1) stays what it is: it signs decisions on the existing decision path, which
keeps refusing `move`. It is not a bridge credential and does not stand in for the steps above, because
the desktop holds the same symmetric key and it cannot give a non-extractable signature (D2). A phone
that has one sends its `phoneId` in the `pair` meta (`"phone_id"`, optional); the host records the link
between the two, and revoking either revokes both (the restrictive default of D2).

### 8.3 Revocation

Revocation is a host-side registry change and takes effect on the next check: every later request of
that device is refused `revoked`. At that moment the host also ends the device's open streams (final
chunk, `REFUSAL` `revoked`), drops its typing lease and any action waiting for its assertion, and
refuses its queued requests that have not started. A request already running finishes, but its
response is replaced by the `revoked` refusal. The rid records stay until they expire, so no replay
runs. Nothing on the TIX server has to change; the owner can additionally sign the browser out on
TIX's Devices page, which also stops it from posting to the mailbox. Revoking in one workspace's Remote
tab revokes the device in every workspace registry on that computer (D7). Changing a device's scope
ends its streams with `scope_changed` and drops its lease. The kill switch refuses everything with
`stopped`.

## 9. Platform-authenticator binding

The TIX app, not the sandboxed dashboard frame, performs every WebAuthn ceremony: the frame has no
`publickey-credentials-get` permission (R0 spike) and must not get one. The text the person approves is
host-supplied and rendered by the TIX app as text only.

### 9.1 Parameters

RP id: the TIX host name (`location.hostname` of the TIX app). Origin: the TIX origin. Algorithm: ES256
(COSE −7) only. User verification: required. Attestation: none (attestation cannot be checked for
synced passkeys anyway, see §9.6).

### 9.2 Registration, one credential per paired device

Registration happens **only inside the pairing ceremony, before the owner approves it on the Mac**, so
that the person who compares the fingerprint also sees "with Face ID / Touch ID / Windows Hello". A
credential cannot be added or replaced later without pairing again. (With attestation none, the host
cannot tell a platform authenticator from software, so a later registration from a compromised web app
would be invisible.)

1. `op = "credential_begin"` (signed with the pending device key): the host returns `nonce` (32 random
   bytes) and `expires_ms` (now + 120 s), single use.
2. The device computes
   `challenge = H("sharing/bridge/webauthn-reg/v1|" || workspace || device_id || expires_ms (8) || nonce)`
   (vector `assertion.registration_challenge`) and calls `navigator.credentials.create` (§10.5).
3. `op = "credential_finish"` with `credential_id`, `attestation_object`, `client_data_json` (b64u).
   The host checks: the challenge is open and was issued to this device; `type = "webauthn.create"`,
   `origin` is the TIX origin, `crossOrigin` is not true; `rpIdHash = H(rp id)`; flags UP, UV and AT are
   set; the COSE key is EC2, P-256, alg −7. It stores the credential id, public key, sign count,
   the BE and BS flags, rp id and origin with the pending registration.

### 9.3 The challenge

```
subject      = {"kind": "action" | "command" | "charter" | "verdict" | "permission" | "lease",
                "shown": <the exact text the TIX app shows>,
                "digest": <hex H(the full artefact: command bytes, charter, verdict text), or "">}
subject_hash = H(canonical JSON of subject)
challenge    = H("sharing/bridge/assert/v1|" || workspace (16) || device_id (16) || rid (16)
                 || purpose (1: fresh = 1, lease = 2) || scope (1: look 1, decide 2, operate 3, type 4)
                 || expires_ms (8) || nonce (32) || subject_hash (32))
```

`rid` is the request the assertion allows, `scope` the scope the route requires, `nonce` 32 random
bytes from the host, `expires_ms` the host's clock + 120 s. The host builds `subject` from its own data
for that route (R2, R13). Vectors: `assertion.challenge_inputs`, `assertion.challenge`,
`assertion.lease_challenge`.

### 9.4 Fresh and lease

1. The device sends the action as a normal request R1. The host completes §6.1 up to step 9 and finds
   that the route needs an assertion. It parks R1 (its exact digest) and answers R1 with the refusal
   `assertion_required` (or `lease_required`) carrying `purpose`, `scope`, `expires_ms`, `nonce` and
   `subject`.
2. The TIX app shows `subject.shown`, recomputes the challenge from those parts itself, and calls
   `navigator.credentials.get` with that challenge and only its own credential id (§10.5).
3. The device sends R2 with `op = "assert"` and `{"for": R1 rid hex, "credential_id",
   "authenticator_data", "client_data_json", "signature"}` (b64u). R2 passes §6.1 like any request.
4. The host verifies the assertion (§9.5). On success:
   - **fresh**: R1 runs exactly once (re-checking its scope first), and R2's response carries R1's
     result. Nothing else is allowed by it.
   - **lease**: the host opens a **15-minute typing lease** for that device (host clock, not persisted
     across a host restart) and runs R1. While the lease is open, that device's requests in the lease's
     class run without a new assertion. The lease ends at 15 minutes, on revocation, scope change, the
     kill switch, host restart or when the owner ends it.

Which requests may run under the lease: in the restrictive default, only input to a terminal stream that
is already open. Starting a terminal or an agent, and every Factory action (allow a permission, start or
edit an epic, sign the verdict, edit a ticket under a running epic), need a fresh assertion (D3).

### 9.5 Verification

The host checks, failing closed with `assertion_failed`: the challenge (from `clientDataJSON`) is one
it issued and still open, and it is **removed whatever happens next** (single use); it has not expired;
it was issued to the device that signed R2, and the credential belongs to that device; `type =
"webauthn.get"`, `origin` is the TIX origin, `crossOrigin` is not true; `rpIdHash = H(rp id)`; flags UP
and UV are both set; the **sign count** increases if either the stored or the new value is non-zero
(both zero is allowed: synced passkeys report 0); the DER ECDSA signature over `authenticatorData ||
H(clientDataJSON)` verifies with the stored key. Then the stored sign count is updated. A count that did
not increase is refused, and the credential is suspended until the device pairs again (D5). Vectors:
`assertion.cases` (verified, replayed, count not increased, zero count, UV clear, other origin, expired,
sent by another device, other subject).

### 9.6 Synced passkeys and revoking one device

A platform credential may be synced to the owner's other devices (iCloud Keychain, Google Password
Manager; the BE/BS flags say so). An assertion is accepted only inside an envelope signed by the device
key it was registered under, and device keys are non-extractable and never synced. So:

- revoking a device's signing key on the host revokes it, even though its passkey lives on elsewhere;
- another device using the same synced passkey still needs its own pairing, and registers its own use
  of the credential;
- removing the passkey itself is an operating-system action the host cannot see;
- the sign count gives no clone detection for synced passkeys; the device key gives the device binding.

What an assertion proves: that the person performed user verification on that device for a challenge
that commits to the request id, the scope and what the TIX app was given to show. Against a
compromised TIX web app it proves only the user verification (§1): the app could show one text and
request a challenge for another.

## 10. The browser's WebCrypto calls

Every call below is exercised against the vectors by `tests/js/bridge-vectors.test.mjs` (Node's
WebCrypto).

### 10.1 K_ws

```js
// mkRaw: re-opened as in approve.js (KEK from keystore.js, GET /api/keyblob, AAD "sharing/mk/v1")
const mk = await crypto.subtle.importKey("raw", mkRaw, "HKDF", false, ["deriveBits"]);
mkRaw.fill(0);
const raw = new Uint8Array(await crypto.subtle.deriveBits(
  { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0),
    info: te.encode("sharing/bridge/ws/v1|" + workspaceHex) }, mk, 256));
const kWs = await crypto.subtle.importKey("raw", raw, "HKDF", false, ["deriveKey"]);
raw.fill(0);                                  // store kWs (non-extractable) in IndexedDB per workspace
```

### 10.2 Seal and open

```js
const salt = crypto.getRandomValues(new Uint8Array(16));        // header bytes 88..104
const kMsg = await crypto.subtle.deriveKey(
  { name: "HKDF", hash: "SHA-256", salt, info: te.encode("sharing/bridge/msg/v1") }, kWs,
  { name: "AES-GCM", length: 256 }, false, ["encrypt"]);         // "decrypt" to open
const params = { name: "AES-GCM", iv: new Uint8Array(12), additionalData: header, tagLength: 128 };
const body = new Uint8Array(await crypto.subtle.encrypt(params, kMsg, plaintext));   // ciphertext || tag
const plain = new Uint8Array(await crypto.subtle.decrypt(params, kMsgForOpen, body)); // throws on a bad tag
```

### 10.3 Device key, sign, verify

```js
const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
const pub = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));       // 65 bytes
const deviceId = (await sha256(concat(te.encode("sharing/bridge/device/v1|"), pub))).subarray(0, 16);
const signed = concat(te.encode("sharing/bridge/sig/v1|"), header, body);
const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, kp.privateKey, signed)); // r||s
const host = await crypto.subtle.importKey("raw", hostPub, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
const ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, host, sig, signed);
```

### 10.4 Header fields

```js
const h = new Uint8Array(104), dv = new DataView(h.buffer);
h.set(te.encode("SHRB"), 0); h[4] = 1; h[5] = 1; h[6] = flags; h[7] = keyVersion;
h.set(workspace, 8); h.set(deviceId, 24); h.set(rid, 40); h.set(stream, 56);
dv.setBigUint64(72, BigInt(seq), false); dv.setBigUint64(80, BigInt(Date.now()), false); h.set(salt, 88);
```

### 10.5 WebAuthn

```js
// registration (§9.2)
await navigator.credentials.create({ publicKey: {
  challenge, rp: { id: location.hostname, name: "TIX" },
  user: { id: deviceId, name: label, displayName: label },
  pubKeyCredParams: [{ type: "public-key", alg: -7 }],
  authenticatorSelection: { authenticatorAttachment: "platform", userVerification: "required", residentKey: "discouraged" },
  attestation: "none", timeout: 60000 } });
// assertion (§9.4): challenge recomputed locally from the refusal's parts and what is shown
await navigator.credentials.get({ publicKey: {
  challenge, rpId: location.hostname, allowCredentials: [{ type: "public-key", id: credentialId }],
  userVerification: "required", timeout: 60000 } });
```

The host side uses `cryptography`: `HKDF(hashes.SHA256(), 32, salt or None, info)`, `AESGCM(key)`,
`ec.ECDSA(hashes.SHA256())` with `encode_dss_signature` / `decode_dss_signature` for raw `r || s`, and
`EllipticCurvePublicKey.from_encoded_point` (which rejects points off the curve). See the reference.

## 11. Failure handling and downgrade

| Condition | Host | Device |
|---|---|---|
| size, magic, unknown version, wrong direction, unknown flag, other workspace or key version | drop | drop |
| bad tag | drop | drop |
| invalid framing after a good tag | refusal `malformed` | drop |
| unknown device | refusal `not_paired` (pairing ops excepted) | — |
| bad signature | refusal `bad_signature` | drop (bad host signature) |
| old or future timestamp | refusal `stale_timestamp` | drop |
| repeated or too old seq | refusal `stale_sequence` + `high` | — (chunk out of order: drop) |
| known rid, same bytes | stored outcome, never re-run | — |
| known rid, other bytes | refusal `rid_conflict` | — |

**Downgrade resistance:** version 1 has exactly one suite and no negotiation. The version, direction,
flags and key version are inside the AAD and the signature, so nobody can change them without failing
both. A host drops every version it does not speak and never falls back. A device pins the version with
its pairing and never sends a lower one; a response must carry the request's version. All labels end in
a version (`…/v1`), so a future version derives unrelated keys and signatures that cannot be confused
with these.

## 12. Test vectors

`tests/bridge_vectors.json` (all keys are FAKE: SHA-256 of a public label). Each case gives the exact
input bytes, the state, the clock, and the expected result; `why` fields are informative and not part of
the contract.

| Section | Covers |
|---|---|
| `hkdf` | K_ws for two workspaces, K_msg |
| `seal` | header encoding, K_msg, zero nonce, AAD, `ciphertext || tag` |
| `sign` | a valid signature, the wrong key, a changed message, the malleable twin, `r = 0` |
| `ids` | device ids, fingerprints, the host pin |
| `host_cases` | a full request (`full_request_accepted`), a replayed request, the same rid with other content, old/future/edge timestamps, a tampered tag, a tampered header field, a wrong-device signature, an unknown device, an unknown version, a response sent to the host, an unknown flag, another workspace's key, a revoked device, repeated, too old and reordered sequence numbers, an oversize envelope, the pairing cases |
| `device_cases` | a full response chunk, a response signed by a device key, for another device, a tampered tag, an old timestamp, out of order, a mailbox mismatch, a refusal chunk, a refusal without `LAST` |
| `pairing` | the link fragment, host pin, pairing MAC, device fingerprint |
| `assertion` | the challenge inputs and bytes, a lease challenge, the registration challenge, and verification cases with a fake authenticator |

Not covered by vectors yet: parsing a real WebAuthn attestation object (CBOR) at registration and the
`credential_begin` / `credential_finish` requests of a pending device (F2).

## 13. Reused and new

**Reused from TIX unchanged:** MK and its storage on every device; re-opening MK in the browser with the
KEK (`approve.js`); the space id as workspace id; AES-256-GCM with a 128-bit tag; HKDF-SHA-256 as already
used by `deriveRaw`, `approvalKey` and upload links; canonical JSON; the 100-bit fingerprint format; the
fragment-secret link pattern of public links and `/pair`; non-extractable CryptoKeys in IndexedDB; the
mailbox and its limits.

**New:** the HKDF labels `sharing/bridge/*/v1`; K_ws and per-envelope K_msg; the envelope; device ECDSA
signing keys and the host's registry; the host signing key and its pin; the pairing link and ceremony;
the sequence window and idempotency store; the WebAuthn registration and challenge formats; a `sharing`
CLI command that hands K_ws to `orch` (F1).

## 14. Decisions needed

The specification takes the more restrictive option for each until the owner decides, with one stated
exception (D4).

- **D1 — Confidentiality between master-key holders.** As the ticket decided, K_ws is derivable from MK,
  so every master-key holder (approved CLI devices in every repo, including a remote agent's VM of another
  client, and a compromised TIX web app) can read bridge traffic (terminal output included) if it also
  obtains the ciphertext, which today takes the server's position. Option: a per-device ECDH key agreed
  with the host key at pairing, so only that device and the host can read its traffic. Cost: a second
  non-extractable key per device and a pairing field; scopes stay host-enforced. Meanwhile: as decided,
  with this stated in §1.
- **D2 — The existing phone pairing.** May an existing orch HMAC pairing replace the link + fingerprint
  steps for a phone? Meanwhile: no (§8.2), and revoking either revokes both when they are linked.
- **D3 — What the 15-minute lease covers.** Meanwhile: only input to an already open terminal stream;
  starting a terminal or an agent and every Factory action need a fresh assertion.
- **D4 — Synced (backup-eligible) passkeys.** Refusing BE credentials is the restrictive option, but on
  iPhone every platform passkey is synced, so it would make Type and Factory unusable on a phone.
  Meanwhile: **accept** them, record BE/BS, show them in the Remote tab, and rely on the device key for
  the device binding (§9.6). This is the one less restrictive default; please confirm.
- **D5 — Sign count regression.** Meanwhile: refuse and suspend the credential until the device pairs
  again (rather than only refusing that assertion).
- **D6 — Host key rotation.** Meanwhile: none in-band; a new host key means every device pairs again.
- **D7 — Revocation reach.** Meanwhile: revoking a device in one workspace's Remote tab revokes it in every
  workspace registry on that computer.
- **D8 — Accept the two limits in §1:** a compromised TIX web app can read every workspace's bridge
  traffic it obtains (it can recover MK; this goes beyond "acts at the device's scope"), and it can obtain
  an assertion for something other than what it shows; a local process running as the owner on the host
  computer can read MK, the host key and the registry.
- **D9 — Lifetimes.** Meanwhile: pairing offer 10 minutes, single use; assertion and registration
  challenges 120 s; rid records 900 s; stored replay body 64 KiB; refusal budget 10 per minute.

## 15. Follow-ups (not in this document's scope)

- **F1** — `sharing` CLI command that prints or hands over K_ws for a workspace it owns, so `orch` never
  reads MK (skill change, before R3).
- **F2** — vectors for parsing a WebAuthn attestation object (CBOR, COSE key) at registration, with R3.
- **F3** — the HTTP mapping inside `meta` (method, path, header allow-list, the remote client address)
  is R2/R3's; this document fixes only the envelope around it.
- **F4** — keystroke requests show typing rhythm and length to the server (§4); R6 should batch or pad
  them.
- **F5** — update `docs/threat-model.md` with the bridge's cleartext header fields once R3 and R10
  ship it.
