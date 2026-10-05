# Bridge protocol, version 1

This document says what is inside the sealed bodies that the bridge mailbox (`fileshare/bridge.py`,
`fileshare/routes/bridge.py`) carries between an `orch` host and the owner's browsers. It covers which
keys seal and sign, where each key lives, how a device is recognised, the exact bytes, and what each
side does with an envelope that is not right.

Status: **draft, revised after the first cryptography review** (ticket #58, part of #22 and orch-core#85).
The host side (orch-core R3, #89) and the TIX app side (R10, #25) implement this document. The contract
is [`tests/bridge_vectors.json`](../tests/bridge_vectors.json). The reference implementation that
produces it, [`tests/support/bridge_protocol_ref.py`](../tests/support/bridge_protocol_ref.py), is test
support only and is not shipped. Where the reference and this document disagree, this document wins and
the reference has a bug.

Conventions: integers are unsigned big-endian. `||` is concatenation. Labels are ASCII byte strings
written in double quotes. `H(x)` is SHA-256. "Hex" is lower case. "b64u" is base64url without padding
(TIX's canonical spelling, `crypto.js` `b64u`). "MUST" and "MUST NOT" are requirements; anything else is
explanation.

Primitives come only from WebCrypto and the Python `cryptography` package: AES-256-GCM with a 128-bit
tag, HKDF-SHA-256 (RFC 5869), HMAC-SHA-256, ECDSA on P-256 with SHA-256, and SHA-256. There is no
compression anywhere, because compression before encryption leaks content through length.

## 1. Threat model for the bridge

The parties:

- the **host**: `orch` serving one workspace with `--remote`, on the owner's computer, which is also the
  TIX device that owns that workspace's space;
- **devices**: the owner's browsers and phones that were paired with that host;
- the **TIX server**, which relays the mailbox;
- every other **holder of the master key**: every approved CLI device in any repo and every signed-in
  browser (§2.1).

| Who | Can | Cannot |
|---|---|---|
| The TIX server | See the cleartext header of every envelope (§3.2), sizes, timing, which session posted it. Drop, delay, reorder or replay envelopes. Refuse service. | Read any body. Forge or alter an envelope (tag and signature). Make the host run anything twice (§5). Make a device accept anything as a response. |
| Another master-key holder (not a paired device) | Decrypt any bridge envelope it obtains. It obtains them only by also being, or controlling, the TIX server: the mailbox hands requests only to the space's owner device and responses only to the session that posted the request. | Make the host act: every request needs a registered device's signature. Make a device accept a response: every response needs the host's signature. |
| A paired device, honest or compromised | Act at its own scope. Read whatever the host returns to it, and, as a master-key holder, any envelope it obtains. | Act above its scope (the host decides every request again). Act as another device, or on another device's streams. Act after revocation. |
| A TIX web app compromised **after** pairing (the server serving modified JavaScript) | Use the non-extractable device key of each browser that loads it, at that device's scope, while it is loaded (an **accepted risk**, owner decision). Recover the raw master key, as today's threat model already says, and so read every bridge envelope it can obtain, for every workspace. Show the person one action and request an assertion over another: against this attacker an assertion proves only that the person performed a user verification on that device at about that time (§9.6). | Export the existing device key to use elsewhere. Act at a scope the device does not hold. Outlast revocation on the host. |
| A TIX web app compromised **at pairing time** | Everything above, permanently. While the pairing ceremony runs it holds MK and the pairing secret. It can generate an *extractable* key, or any key, and show its fingerprint as the device's. Because attestation is none (§9.1), it can register a software WebAuthn credential. The result is a paired device whose key lives outside the browser and whose "fresh" assertions are forged without any user verification, until the owner revokes it. This exceeds "acts at the device's scope while loaded" (D8). Mitigations: the host's audit log of every assertion-backed action with its subject, shown in the Remote tab (§9.5), and the rate limit on fresh actions (D9). | Act at a scope the owner did not approve on the Mac. Outlast revocation on the host. |
| A local process on the host computer running as the owner (for example an agent with shell access) | Read MK (the workspace's `.claude/skills/sharing/config.json`), and the host key, registry and request store (the orch config directory), because the operating-system user is shared. The orch command guard refuses agents' commands on those paths, but it is the only barrier (§2.7). This is orch's documented limit for local agents; the protocol does not change it. | Nothing this protocol can promise. |

Scopes (Look, Decide, Operate, Type) are enforced **by the host, by device identity** (the signature),
never by key secrecy. Every device of the workspace can decrypt every envelope, so no scope may depend
on someone not being able to read something.

What this protocol adds to TIX's protections:

- requests are authenticated by device, and responses by host;
- every request is fresh and runs at most once;
- a platform-authenticator gesture is bound to the exact request it allows.

It adds no confidentiality boundary between master-key holders (D1).

## 2. Keys

### 2.1 What exists today (verified in this repository)

- **One master key (MK), 32 bytes, per account.** There is no per-space key yet ("per-space keys are
  planned as A4.1", [threat-model.md](threat-model.md)). Spaces (`fileshare/mirrors.py`) have a 32-hex id
  and a label sealed under MK (`sharing/space/v1|`). The bridge mailbox uses that same space id as the
  workspace id (`/api/bridge/{space}`), and only the device that owns the space may host it
  (`routes/bridge.py` `_host`, `mirrors.owner_space`). "Space key" in the ticket therefore means MK.
- **A browser** (desktop or phone, a signed-in session) holds the KEK and MK only as **non-extractable
  AES-GCM CryptoKeys** in IndexedDB (`login.js`, `setup.js`, `keystore.js`). A non-extractable AES-GCM key
  cannot be an HKDF input. The browser can still obtain the raw MK when it needs it, without the
  passphrase: `GET /api/keyblob`, then AES-GCM-open `wrapped_mk` with the stored KEK under AAD
  `"sharing/mk/v1"`. That is exactly what device approval does today (`approve.js` `approveDevice`). The
  passphrase is never stored on any device, phones included.
- **An approved CLI device** (the `sharing` skill) holds MK in the clear in
  `<repo>/.claude/skills/sharing/config.json`, mode 0600 or an owner-only ACL (`sharing.py`
  `write_config`, `secure_file`, `check_permissions`), **inside the workspace**. It received MK sealed to
  its ECDH P-256 key at approval (`sealToDevice`, AAD `sharing/approve/v1|`), after the owner compared its
  fingerprint.
- **The host** is the CLI device that owns the space, so the host computer already holds MK.
- **The existing phone pairing** (orch remote humans, `pairing.js`) is a 32-byte HMAC key per (space,
  phone). It is delivered in the fragment of a `/pair#…` link shown on the desktop and checked with a
  6-digit code. The phone stores it as a non-extractable, sign-only HMAC CryptoKey; the desktop stores
  it in orch's `remote-humans.json`. It signs decisions. Because it is symmetric, the desktop can
  produce the same MAC.

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
- The **host** needs K_ws, not MK. The more restrictive arrangement is required here: the workspace's
  `sharing` CLI derives K_ws and hands only K_ws to `orch` (a new CLI command, follow-up F1). That is
  the same way the orch-tix addon already uses the CLI instead of reading MK. `orch` MUST NOT read
  `config.json` itself. This limits what the `orch` process holds; it gives **no** protection against a
  local agent, because MK sits in the workspace beside it. The new command is a guarded path like the
  others in §2.7.
- `key_version` in the header (§3.2) is TIX's MK version (`/api/keyblob` `key_version`, today 1). When MK
  rotation, per-space keys (A4.1) or a version 2 of this protocol arrive, the IKM or the labels change. A
  v1 host drops envelopes of any other key version.

### 2.3 Per-envelope keys K_msg and the nonce

```
K_msg = HKDF-SHA-256(IKM = K_ws, salt = header.salt (16 random bytes), info = "sharing/bridge/msg/v1", L = 32)
nonce = 12 zero bytes
```

Every envelope draws a fresh 16-byte `salt` from a CSPRNG at the moment it is sealed, so every K_msg seals
exactly one plaintext and the constant nonce is never reused with a key. This is the construction of
Tink's AES-GCM-HKDF streaming AEAD (a random salt into HKDF per message). It is used here because K_ws is
shared by many senders (every device and the host) that have no shared counter.

- **Limit:** a nonce is reused only if two envelopes under one K_ws draw the same salt. For `q`
  envelopes the probability is at most `q² / 2^129`. That is below 2^-33 for 2^48 envelopes, which at a
  sustained 100 envelopes per second (the mailbox's per-host chunk ceiling) is about 89,000 years.
  Version 1 has no rekeying inside a key version and needs none. A sender MUST draw the salt freshly for
  every seal operation. Re-sending identical bytes is fine; re-sealing different bytes under a remembered
  salt is not.
- **No sender state:** nothing has to survive a reload, a second tab or a restored backup to keep nonces
  unique.
- **AES-256 only:** the single-use-key argument, and the bound above, assume a 256-bit K_msg. It MUST NOT
  be adapted to AES-128.
- AES-GCM is not key-committing. That is harmless here, but not because the sender cannot influence the
  key: the sender chooses the salt and so K_msg. The conclusion holds because the salt is part of the
  header, which is the AAD and is signed, and because every receiver uses the one K_ws of the
  (workspace, key version). Opening the same ciphertext under a different key would need a different
  signed header.

Vectors: `hkdf[2]` (K_msg), `seal[0]` (`message_key`, `nonce`, `sealed`).

### 2.4 Device signing keys (new)

Each device generates one **ECDSA P-256** key pair with a **non-extractable** private half
(`generateKey(..., false, ["sign", "verify"])`) and stores it in IndexedDB like the other CryptoKeys.
There is one key per browser profile, registered separately with each host (each workspace) it is
paired with.

```
device_id          = H("sharing/bridge/device/v1|" || workspace (16 bytes) || pub)[0:16]    pub = 65-byte uncompressed point
device_fingerprint = base32(H("sharing/bridge/fp/v1|" || pub))[0:20], grouped 4-4-4-4-4
```

The device id includes the workspace, so one browser key does not give one id across workspaces, and
the cleartext header does not link a browser's bridge traffic across workspaces. This is the better of
the two options: it costs nothing, it keeps one key per profile, and the fingerprint the owner compares
stays the same in every workspace. A key per workspace would add keys and give the owner a new
fingerprint for each workspace, for no further gain: the server can still link the traffic through the
TIX session that posts it (D1 notes this). Vectors: `ids` (`device_id`, `device_id_other_workspace`).

The fingerprint is TIX's existing format (`security.py` / `crypto.js` `fingerprint`) under a bridge
label of its own. At 100 bits, a key cannot be ground to match a fingerprint the person compares.

The host keeps its **registry in the orch config directory, never in the workspace**, one per
workspace. Each entry holds the device id, public key, scope, label, paired-at time, revoked flag, the
sequence state (§5.2), the phone link (§8.2) and the WebAuthn credential (§9.2). The exact path is R3's;
it is owner-only like `remote-humans.json`, and §2.7 applies.

### 2.5 The host signing key (new)

Each workspace's host has one ECDSA P-256 key pair, created on the first `--remote` start and kept
owner-only in the orch config directory beside the registry. Its public key reaches a device only
through the pairing link, as a pin (§8.1):

```
host_pin = H("sharing/bridge/host/v1|" || host_pub)           (32 bytes, shown as b64u)
```

The device stores the host public key with its pairing and verifies every response with it. Version 1
has **no in-band rotation**; replacing the host key means the registry is reset and every device pairs
again (D6). The key is per workspace, not per computer, so one workspace's key cannot speak for another.

### 2.6 Where every key and record lives

| Item | Where | Who can use it |
|---|---|---|
| MK | browser: non-extractable AES-GCM CryptoKey, raw only transiently via KEK + `/api/keyblob`; CLI: `config.json` in the repo | every master-key holder |
| K_ws | browser: non-extractable HKDF CryptoKey per workspace; host: memory of the `orch --remote` process | every master-key holder (it is derivable from MK) |
| K_msg | memory, one envelope | sender and readers of that envelope |
| device signing key | browser: non-extractable ECDSA CryptoKey; host: the public half in the registry | that browser |
| host signing key | orch config directory, owner-only (guarded, §2.7); devices: the public half, pinned | the host |
| registry | orch config directory (guarded) | the host |
| request store: rid records, and stored replay bodies of up to 64 KiB, which may hold dashboard data or terminal output | orch config directory (guarded) | the host |
| audit log of assertion-backed actions | orch config directory (guarded) | the host; shown in the Remote tab |
| pairing secret S | the pairing link fragment (one use, 10 minutes) and the host's memory | whoever holds the link |
| WebAuthn credential | the platform authenticator (possibly synced, §9.6); host: public key in the registry | the person, by user verification |

### 2.7 Guarded paths

The host key, the registry, the request store, the audit log and the F1 command that hands out K_ws are
**guarded paths**: the orch command guard refuses an agent's command that touches them. The
operating-system user is shared with every agent on that computer, so the guard is the only barrier, not
file permissions. Therefore:

- registry changes are made only from the Remote tab (pairing, approval, scope change, revocation);
- every registry change is written to the audit log;
- every `--remote` start shows the registry's devices, scopes and the last changes.

## 3. Envelope

### 3.1 Layout

```
envelope = header (104 bytes) || ciphertext || tag (16 bytes) || signature (64 bytes)
```

The mailbox carries the envelope as b64u text in its `body` field and limits the **decoded** size: a
request envelope to **1 MiB** (1,048,576 bytes), a response chunk envelope to **256 KiB** (262,144 bytes)
(`bridge.MAX_REQUEST_BYTES`, `MAX_CHUNK_BYTES`; checked by `test_size_limits_match_the_mailbox`). The
fixed overhead is 184 bytes, so a request plaintext is at most 1,048,392 bytes and a chunk plaintext at
most 261,960 bytes.

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
| 80 | 8 | ts_ms | sender's clock (with its offset, §5.1), milliseconds since the Unix epoch | host's clock |
| 88 | 16 | salt | §2.3 | §2.3 |

Unknown flag bits MUST be zero. The whole header is cleartext and fully authenticated: it is the AAD and
it is signed. The mailbox already shows the TIX server the rid, idx, last, stream, sizes, timing and
posting session. Beyond that the header shows it:

- the device id, stable per device and workspace (not across workspaces, §2.4);
- the sequence numbers;
- the sender's clock;
- which requests belong to which stream.

### 3.3 Sealing

```
AAD               = header (all 104 bytes)
ciphertext || tag = AES-256-GCM-Encrypt(K_msg, nonce = 12 zero bytes, plaintext, AAD)
```

Vector: `seal[0]` gives K_ws, the header, K_msg, the plaintext and `ciphertext || tag`.

### 3.4 Signature

```
signed    = "sharing/bridge/sig/v1|" || header || ciphertext || tag
signature = ECDSA-P256-SHA256(signer, signed), as raw r || s, 32 bytes each (IEEE P1363)
```

Requests are signed by the device's key, responses by the host's key.

- **Format:** raw `r || s` is WebCrypto's native format. Python converts with `encode_dss_signature` /
  `decode_dss_signature`. DER is never used on the wire; WebAuthn assertion signatures (§9) are a
  separate thing and are DER.
- **Range:** a verifier MUST reject a signature that is not 64 bytes, or whose `r` or `s` is outside
  `1 … n-1`, before verifying (vectors `sig_scalars`, `sign`). OpenSSL checks this too; other libraries
  may not.
- **Malleability:** ECDSA signatures are malleable (`(r, n-s)` also verifies, vector `sign[3]`), so a
  signature MUST NOT be used as an identifier. The idempotency digest (§5.3) leaves it out.
- **Randomness:** signing may be randomised (WebCrypto) or deterministic. The reference uses RFC 6979
  only so that the vector file is reproducible.

The signature covers the ciphertext, not the plaintext (encrypt-then-sign). Any party can check it
without the key, it binds the exact bytes, and the device can drop a forged response before decrypting.

### 3.5 Plaintext

```
plaintext = meta_len (4 bytes) || meta (canonical JSON, UTF-8, at most 65,536 bytes) || data (raw bytes)
```

A `meta_len` above 65,536 or beyond the plaintext is malformed (vector `meta_longer_than_64_kib`).
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

A stream is opened by an `http` request with the `STREAM` flag, and its response chunks carry `STREAM`.
Input to a stream (terminal keys) is an `http` request whose header `stream` names it.

Response `meta`:

- chunk 0 has `status` and `headers`;
- later chunks have `{}`, or `{"keepalive": true}` with empty `data`;
- a refusal is a single chunk with `LAST | REFUSAL` and `meta = {"refusal": <code>, …}` (§6.2).

## 4. Chunks and streams

A page response is the chunks `seq = 0, 1, …`, with `LAST` on the final one, each a full envelope signed
and sealed on its own. A stream's frames are numbered the same way and end with `LAST`. The device MUST
check that the mailbox's cleartext `id`, `idx`, `last` and `stream` equal the signed header's `rid`,
`seq`, `LAST` and `STREAM`. It MUST accept chunk `n` only after chunk `n-1`. A response that has no
`LAST` by the request's timeout fails as a whole, and nothing of it is rendered.

**A stream belongs to the device that opened it.** The host MUST refuse with `forbidden_scope` any input
to, or `cancel` of, a stream that the signing device did not open (vectors `input_to_own_stream`,
`input_to_other_devices_stream`, `cancel_of_other_devices_stream`). A typing lease (§9.4) covers only
streams its own device opened.

The host sends a keepalive at least every 20 s on an idle stream (the mailbox's TTL is 60 s). A device
MUST treat a stream with no chunk for more than 60 s as closed. The host closes a stream with a final
chunk (`LAST`, and `REFUSAL` with code `revoked`, `scope_changed` or `stopped` when it ends it for that
reason) on revocation, scope change and the kill switch, and drops the device's lease. The device's
`cancel` request ends a stream from its side. Terminal frames are coalesced by R6. Keystrokes are one
request each, so the server sees typing rhythm and length (F4).

## 5. Time, order and single execution

### 5.1 The window and the clock offset

The host accepts a request only if `|host_now_ms − ts_ms| ≤ 300,000`, that is 300 s each way. Exactly
300,000 ms is inside (vectors `timestamp_at_window_edge`, `future_timestamp_at_window_edge`,
`old_timestamp`, `future_timestamp`).

The device keeps one clock offset per host, starting at 0, and uses `device_now + offset` both to write
`ts_ms` and to check responses. It judges whether a response is fresh mainly by its own pending rid and
the chunk order (§7); the window on responses only adds a bound. A host-signed `stale_timestamp` refusal
carries the host's clock (`host_ms`):

- the device MUST NOT drop that refusal because of its own clock;
- it sets `offset = host_ms − device_now`;
- it sends the request again as a **new** request (new rid, new seq).

Without this, a device with a skewed clock would never recover (vectors
`stale_timestamp_refusal_to_a_skewed_clock`, `response_old_timestamp_corrected_by_offset`).

### 5.2 Per-device sequence numbers

Each device keeps one counter per host. It starts at 1 and is incremented in a readwrite IndexedDB
transaction for each request, so two tabs never share a value. The host keeps per device:

- `high`, the highest accepted seq;
- a 64-bit `bitmap`, where bit `i` set means `high − i` was accepted.

A seq is accepted if it is above `high` (the window slides), or if it is within the 64 values below
`high` and not yet seen. Anything else, including 0, is refused with `stale_sequence` and `high`. The
window rather than strict increase is there because requests posted concurrently may arrive out of
order. Vectors: `sequence_zero`, `repeated_sequence`, `sequence_below_window`,
`sequence_reordered_inside_window`, `window_slides_then_old_top_repeats`, `window_slides_far_then_old_seq`.

**Resync rule:** on a `stale_sequence` refusal (verified host signature, §7), the device sets its counter
to `max(counter, high + 1)` and sends the request again as a **new** request (new rid, new seq, new
envelope); the refused one is recorded as refused and never runs. A device that lost its counter has lost
its key too (same storage), so it pairs again as a new device. Seq values stay below 2^53, the
JavaScript safe integer limit; a device at that limit pairs again.

### 5.3 Idempotency

The rid is the idempotency key. Before anything runs, **and before any refusal after the signature
verified is sent**, the host records and **persists** this, together with the sequence state:

```
rid → { device id, digest = H(header || ciphertext || tag), received_at, outcome, until }
until = max(received_at + 900 s, ts_ms + 300 s + 60 s)
```

`outcome` is one of:

- *none* (accepted, not finished);
- the response head and up to 64 KiB of body once done;
- the refusal code and its fields.

**Every refusal issued after the signature verified (§6.1 steps 5–9) MUST be persisted as the rid's
outcome before it is sent.** Otherwise a future-dated envelope refused at `t0` would run when the same
bytes arrive a few seconds later. The record lives until `until`: at least 900 s, more than the 600 s
span of timestamps the window lets in, and at least until the envelope's own timestamp has left the
window plus a 60 s margin.

For a known rid whose record has not expired:

| Case | Answer |
|---|---|
| same device, same digest, finished | a **replay**: the stored outcome, re-sealed with a fresh salt. It never runs again (vector `replay_of_finished_request`). |
| same device, same digest, refused | the stored refusal, with the stored fields (vectors `future_timestamp_then_in_window`, `repeated_sequence_then_replayed`, `sequence_refused_then_same_bytes_after_window_moved`) |
| same device, same digest, no outcome (still running, or the host crashed between recording and finishing) | `already_done` with `status: "unknown"`. The app says "outcome unknown", never "failed" (vector `replayed_request`). |
| anything else (another device, other bytes) | `rid_conflict` (vectors `same_rid_other_content`, `replay_from_other_device_record`) |

When no response arrived, a device retrying MUST resend the identical envelope bytes. The mailbox refuses
a second post of an id while its route exists (`duplicate_id`), so the retry first cancels the old
request (`DELETE /api/bridge/{space}/requests/{id}`) or waits until the route's 60 s TTL has passed.
Once the record has expired, the timestamp is long outside the window, so an old envelope is refused and
never runs (`test_replay_never_runs_twice`). The signature is not in the digest because it is malleable
(§3.4).

## 6. What the host does with a request

### 6.1 Order (normative)

The rule: **silence for anything a party without K_ws could have produced, and a sealed, host-signed
refusal for everything after the tag verified**, because only a master-key holder gets past the tag. The
server and other outsiders therefore learn nothing new about the host's state from any refusal; the
mailbox already shows that a response exists and its size.

1. **Shape.** Drop if any of these holds:
   - the size is outside 184 … 1,048,576 bytes;
   - magic ≠ `"SHRB"`, version ≠ 1 or direction ≠ 1;
   - a flag other than `STREAM` is set;
   - the key_version or workspace is not this host's;
   - the mailbox `id` the envelope arrived under differs from the header's `rid` (vector
     `mailbox_id_differs_from_rid`).
2. **Tag.** Derive K_msg and open. Drop on failure (vectors `tampered_tag`, `tampered_header_field`,
   `other_workspace_key`).
3. **Device.** Look up the header's device id in the registry:
   - Unknown: `op = "pair"` goes to §8.1. From a device id with a pending pairing, `pair_status`,
     `credential_begin` and `credential_finish` are verified with the pending key and go to §8.1 and
     §9.2. Anything else is refused `not_paired`.
   - Revoked: refuse `revoked`.
   - Otherwise verify the signature with the registered key; refuse `bad_signature` on failure (vector
     `wrong_device_signature`).

   **Refusal budget:** the refusals of this step are issued before any registered signature verified.
   They count against **one host-wide budget** of 10 per minute, never against the claimed device id,
   which anyone with K_ws can write. Over the budget the host drops them. A request whose signature
   verified is never dropped because of the budget (vectors
   `refusal_budget_spent_drops_unverified`, `refusal_budget_spent_signed_request_runs`).
4. **Replay.** A known rid gets §5.3's answer.
5. **Framing.** If the plaintext framing or the meta JSON is invalid: record, then refuse `malformed`.
6. **Time.** Outside the window (§5.1): record, then refuse `stale_timestamp` with `host_ms`.
7. **Sequence.** Outside the sequence window (§5.2): record, then refuse `stale_sequence` with `high`.
8. **Stream.** If the header names a stream the device did not open (§4): record, then refuse
   `forbidden_scope`.
9. **Record and run.** Record the rid (no outcome yet) and persist it with the sequence state. Then
   check the route's scope tag against the device's scope (R2), then the assertion requirement (§9),
   then run the request. Refusals here are `forbidden_scope`, `assertion_required`, `lease_required` and
   `assertion_failed`, and each replaces the record's outcome before it is sent.

The host does one AES-GCM open (up to 1 MiB) before the signature check. The mailbox limits each client
to 20 requests per second, which bounds that work.

### 6.2 Refusal codes

`malformed`, `not_paired`, `revoked`, `bad_signature`, `rid_conflict`, `already_done`,
`stale_timestamp`, `stale_sequence`, `pairing_closed`, `forbidden_scope`, `assertion_required`,
`lease_required`, `assertion_failed`, `scope_changed`, `stopped`. The text of a refusal is fixed per code,
and it never echoes request content.

## 7. What the device does with a response chunk

The device drops a chunk, and never renders it, unless all of the following hold, checked in this
order:

1. the size is within 184 … 262,144 bytes;
2. magic, version (equal to the request's) and direction 2 are right, and the flags are valid (`REFUSAL`
   requires `LAST`);
3. workspace, key_version and device are its own;
4. **the rid is one of its pending requests** (vector `response_for_unknown_request`);
5. the mailbox fields match the header (§4);
6. **the host signature verifies with the pinned host key**;
7. the tag verifies;
8. `seq` is the next expected index;
9. the timestamp is within the window of `device_now + offset`, with the one exception of §5.1.

Vectors: `device_cases`. A chunk that a device key signed is not a response
(`response_signed_by_a_device`). This is what stops another master-key holder, or the server, from
answering in the host's name. A refusal chunk is shown as the host's fixed message for its code.

**Host key pin failure:** when the host signature does not verify with the pinned key, the device drops
the chunk. If that happens to every response, the app says "the host key changed: pair again". It never
trusts a new host key on first use, and never replaces the pin without a new pairing link (D6).

## 8. Pairing and revocation

### 8.1 Pairing a browser or phone

1. **On the Mac** (the Remote tab, orch-core R5), the owner chooses "pair a device" and a scope. The
   host creates an offer: a `pairing_id` (16 random bytes), a secret `S` (32 random bytes), the scope,
   and an expiry of 10 minutes; the offer can be used once. The Mac shows a link, as a QR code and for
   copying:

   ```
   https://<tix-origin>/remote/pair#v1.<workspace hex>.<pairing_id hex>.<b64u S>.<b64u host_pin>
   ```

   The fragment never reaches the server, as with TIX's public links and the existing `/pair` link.
2. **On the device**, the TIX app (signed in, so it holds MK) parses the fragment strictly, derives
   K_ws, generates its signing key (§2.4) and sends a request with `op = "pair"`:

   ```
   meta = {"op": "pair", "pairing_id": hex, "pub": hex (65 bytes), "label": text (≤ 80),
           "mac": hex HMAC-SHA-256(S, "sharing/bridge/pair/v1|" || workspace || pairing_id || pub),
           "phone_id": optional, "phone_proof": optional (§8.2)}
   ```

   The request is signed with the new key, as proof of possession, and its header device id is
   `device_id(workspace, pub)`.
3. **The host**, after §6.1 steps 1 and 2, checks in this order:
   - the offer exists, is open and has not expired;
   - `device_id(workspace, pub)` equals the header's (vector `pair_request_device_id_not_of_pub`);
   - the signature verifies with `pub`;
   - the MAC verifies (a wrong MAC gets the same `pairing_closed` refusal as no offer);
   - the timestamp is in the window (vector `pair_request_stale_timestamp`).

   These refusals count against the host-wide budget. The host then marks the offer used and shows on
   the Mac the label (as text, cleaned like `shown`, §9.3), the scope, the phone link (§8.2) and the
   **device fingerprint**. It answers `pending` in a response signed by the host key.
4. **The device** checks that `H("sharing/bridge/host/v1|" || host_pub)` equals `host_pin` from the
   link, then verifies the response's signature with that `host_pub`, and pins it. It registers its
   platform credential now (§9.2) and shows its own device fingerprint: "compare this on your Mac".
5. **The owner compares the fingerprints and decides on the Mac.** The Mac's default action is **Reject**.
   Approve becomes available only once the owner has confirmed the fingerprint shown on the device (for
   example by entering its last group). The Mac also shows whether a credential was registered, and
   whether it is synced (§9.6). The owner may lower the scope. The host then writes the registry entry
   and logs it (§2.7). The device asks with `op = "pair_status"` (signed with its key, read-only, so a
   replay changes nothing) until the answer is `approved` or `rejected`.

A device that gets `pairing_closed` for a link it just opened MUST say: "this link was used by someone
else: reject it on your Mac". Someone else holding the link is exactly the case the fingerprint
comparison exists for.

What each part protects:

- the link's `host_pin` authenticates the host to the device, with no human comparison;
- `S` keeps strangers and other master-key holders from opening pairings;
- the fingerprint comparison catches a link that reached someone else, at 100 bits of second-preimage
  resistance, because the attacker cannot choose the honest device's key.

Against a TIX web app compromised during the ceremony, none of this helps (§1, D8). Vectors: `pairing`;
host cases `pair_request_then_status`, `pair_request_bad_mac`, `pair_request_no_offer`,
`pair_request_stale_timestamp`, `pair_request_device_id_not_of_pub`, `pair_request_with_phone_link`,
`pair_request_phone_link_without_proof`, `pair_status_without_pairing`.

Another computer pairs the same way, from the copied link. The link is a secret for 10 minutes: it is
never logged and shown once.

### 8.2 The existing phone pairing

The orch HMAC pairing (§2.1) stays what it is. It signs decisions on the existing decision path, which
keeps refusing `move`. It is not a bridge credential and does not replace the steps above: the desktop
holds the same symmetric key, so it cannot provide a non-extractable signature (D2).

A phone that has one may ask the host to link the two pairings. A bare `phone_id` would be
self-asserted, so the phone **proves** it holds that pairing:

```
phone_proof = HMAC-SHA-256(phone pairing key, "sharing/bridge/phone-link/v1|" || device_id)
```

It signs this with its existing sign-only HMAC CryptoKey (`pairing.js`). The host checks the proof
against its own pairing record for that `phone_id`. Only with a valid proof does it record the link;
without one it records no link, and the Mac says "not linked to an existing phone pairing". Revoking
either pairing of a linked pair revokes both (D2). Vectors: `pair_request_with_phone_link`,
`pair_request_phone_link_without_proof`, `pairing.phone_proof`.

### 8.3 Revocation

Revocation is a host-side registry change, made only from the Remote tab and logged. It takes effect on
the next check: every later request of that device is refused `revoked`. At that moment the host also:

- ends the device's open streams (final chunk, `REFUSAL` `revoked`);
- drops its typing lease and any action waiting for its assertion;
- refuses its queued requests that have not started.

A request already running finishes, but its response is replaced by the `revoked` refusal. The rid
records stay until they expire, so no replay runs. Nothing on the TIX server has to change. The owner
can also sign the browser out on TIX's Devices page, which stops it from posting to the mailbox.

Revoking a device in one workspace's Remote tab revokes it in every workspace registry **on that
computer**. It does not reach other computers: their hosts keep their own registries, and the owner
revokes there too (D7). Changing a device's scope ends its streams with `scope_changed` and drops its
lease. The kill switch refuses everything with `stopped`.

## 9. Platform-authenticator binding

The TIX app, not the sandboxed dashboard frame, performs every WebAuthn ceremony. The frame has no
`publickey-credentials-get` permission (R0 spike) and must not get one. The text the person approves is
host-supplied and rendered by the TIX app as text only.

### 9.1 Parameters

- RP id: the TIX host name (`location.hostname` of the TIX app).
- Origin: the TIX origin.
- Algorithm: ES256 (COSE −7) only.
- User verification: required.
- Attestation: none. Synced passkeys provide no attestation that could be checked (§9.6); the
  consequence is in §1.

### 9.2 Registration, one credential per paired device

Registration happens **only inside the pairing ceremony, before the owner approves it on the Mac**, so
that the person who compares the fingerprint also sees "with Face ID / Touch ID / Windows Hello". A
credential cannot be added or replaced later without pairing again. With attestation none, the host
cannot tell a platform authenticator from software, so a later registration from a compromised web app
would be invisible.

1. `op = "credential_begin"`, signed with the pending device key. The host returns `nonce` (32 random
   bytes) and `expires_ms` (now + 120 s), single use.
2. The device computes
   `challenge = H("sharing/bridge/webauthn-reg/v1|" || workspace || device_id || expires_ms (8) || nonce)`
   (vector `assertion.registration_challenge`) and calls `navigator.credentials.create` (§10.5).
3. `op = "credential_finish"` with `credential_id`, `attestation_object` and `client_data_json` (b64u).
   The host checks:
   - the challenge is open and was issued to this device;
   - `type = "webauthn.create"`, `origin` is the TIX origin, and `crossOrigin` is not true;
   - `rpIdHash = H(rp id)`;
   - the flags UP, UV and AT are set;
   - the COSE key is EC2, P-256, alg −7.

   It stores the credential id, public key, sign count, the **BE** flag (backup eligible) and BS flag,
   the rp id and the origin with the pending registration.

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

The fields:

- `rid` is the request the assertion allows;
- `scope` is the scope the route requires;
- `nonce` is 32 random bytes from the host;
- `expires_ms` is the host's clock + 120 s.

The host builds `subject` from its own data for that route (R2, R13). Vectors:
`assertion.challenge_inputs`, `assertion.challenge`, `assertion.lease_challenge`.

**The `shown` text** (and a pairing label):

- It MUST consist of Unicode scalar values only; a string with a lone surrogate is an error, and the host
  does not issue the challenge.
- The host removes every C0 control except line feed, DEL, every C1 control, and the bidi formatting
  characters U+061C, U+200E, U+200F, U+202A–U+202E and U+2066–U+2069.
- There is **no normalisation**: the hash covers exactly what is shown.
- The TIX app renders it as plain text in an isolated bidi context (`dir="auto"` inside an element
  with `unicode-bidi: isolate`), never as HTML.

Vectors: `shown`.

### 9.4 Fresh and lease

1. The device sends the action as a normal request R1. The host completes §6.1 up to step 9 and finds
   that the route needs an assertion. It parks R1 (its exact digest) and answers R1 with the refusal
   `assertion_required` (or `lease_required`). The refusal carries `purpose`, `scope`, `expires_ms`,
   `nonce` and `subject`.
2. The TIX app shows `subject.shown`, recomputes the challenge from those parts itself, and calls
   `navigator.credentials.get` with that challenge and only its own credential id (§10.5).
3. The device sends R2 with `op = "assert"` and `{"for": R1 rid hex, "credential_id",
   "authenticator_data", "client_data_json", "signature"}` (b64u). R2 goes through §6.1 like any
   request.
4. The host verifies the assertion (§9.5). On success:
   - **fresh**: R1 runs exactly once, after its scope is checked again, and R2's response carries R1's
     result. The assertion allows nothing else.
   - **lease**: the host opens a **15-minute typing lease** for that device and runs R1. The lease uses
     the host clock and is not kept across a host restart. While it is open, that device's requests in
     the lease's class run without a new assertion. The lease ends at 15 minutes, on revocation, scope
     change, the kill switch, host restart, or when the owner ends it.

What the lease covers, in the restrictive default: only input to a terminal stream that **this device
opened** and that is already open. Starting a terminal or an agent, and every Factory action (allow a
permission, start or edit an epic, sign the verdict, edit a ticket under a running epic), need a fresh
assertion (D3).

### 9.5 Verification

The host checks the following, in order, and fails closed with `assertion_failed`:

1. the challenge, taken from `clientDataJSON`, is one it issued and is still open. It is **removed
   whatever happens next** (single use);
2. it has not expired;
3. it was issued to the device that signed R2, and the credential belongs to that device;
4. the assertion's `credential_id` equals the registered one;
5. `type = "webauthn.get"`, `origin` is the TIX origin, and `crossOrigin` is not true;
6. `rpIdHash = H(rp id)`;
7. the flags UP and UV are both set;
8. the BE flag equals the one recorded at registration;
9. the DER ECDSA signature over `authenticatorData || H(clientDataJSON)` verifies with the stored key;
10. the **sign count** increases, if either the stored or the new value is non-zero (both zero is
    allowed, because synced passkeys report 0).

If the count did not increase:

- for a credential without BE, the host refuses **that assertion** and shows the event in the Remote
  tab, but does not suspend the credential (D5);
- for a credential with BE set, the counter is advisory only: the assertion passes, and the event is
  shown.

Then the stored count is updated.

Every assertion-backed action is written to the host's **audit log**: device, time, purpose, scope and
the `subject`. The Remote tab shows the log. Fresh actions are rate-limited per device (D9). Together
these are the mitigation the owner has against a device whose assertions are forged (§1).

Vectors: `assertion.cases`:

- verified;
- replayed;
- count not increased (refused without BE; advisory with BE);
- zero count;
- user verification flag clear;
- other origin, cross-origin, `webauthn.create`, other rp id;
- other credential id, BE changed, another authenticator's signature;
- expired;
- sent by another device, challenge issued to another device;
- other subject.

### 9.6 Synced passkeys and revoking one device

A platform credential may be synced to the owner's other devices (iCloud Keychain, Google Password
Manager); the BE/BS flags say so. An assertion is accepted only inside an envelope signed by the device
key it was registered under, and device keys are non-extractable and never synced. So:

- revoking a device's signing key on the host revokes it, even though its passkey lives on elsewhere;
- another device that holds the same synced passkey still needs its own pairing. Because `user.id` is
  the device id, it registers **its own new credential** at that pairing rather than reusing the first;
- removing a passkey itself is an operating-system action that the host cannot see;
- the sign count gives no clone detection for synced passkeys; the binding to the device comes from the
  device key.

What an assertion proves: that the person performed user verification on that device for a challenge
that commits to the request id, the scope and what the TIX app was given to show. Against a compromised
TIX web app it proves only the user verification, and against one compromised at pairing, nothing (§1).

## 10. The browser's WebCrypto calls

`tests/js/bridge-vectors.test.mjs` runs every call below against the vectors, in Node's WebCrypto.

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

Optional hardening: with a KEK that has the `unwrapKey` usage, `unwrapKey("raw", wrapped_mk, kek,
{name: "AES-GCM", iv, additionalData}, "HKDF", false, ["deriveBits"])` would import MK straight into a
non-extractable HKDF key, so raw MK never reaches JavaScript. Today's KEK has only `encrypt` and
`decrypt`, so this needs a change at sign-in (F6).

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
const deviceId = (await sha256(concat(te.encode("sharing/bridge/device/v1|"), workspace, pub))).subarray(0, 16);
const signed = concat(te.encode("sharing/bridge/sig/v1|"), header, body);
const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, kp.privateKey, signed)); // r||s
const host = await crypto.subtle.importKey("raw", hostPub, { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
const ok = await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, host, sig, signed);
```

### 10.4 Header fields

The Node test uses this function verbatim and compares its output with `seal[0].header` byte for byte.

```js
function encodeHeader({ direction, flags, keyVersion, workspace, deviceId, rid, stream, seq, tsMs, salt }) {
  const h = new Uint8Array(104), dv = new DataView(h.buffer);
  h.set(te.encode("SHRB"), 0); h[4] = 1; h[5] = direction; h[6] = flags; h[7] = keyVersion;
  h.set(workspace, 8); h.set(deviceId, 24); h.set(rid, 40); h.set(stream, 56);
  dv.setBigUint64(72, BigInt(seq), false); dv.setBigUint64(80, BigInt(tsMs), false); h.set(salt, 88);
  return h;
}
// tsMs = Date.now() + offsetMs (§5.1)
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

The host side uses `cryptography`:

- `HKDF(hashes.SHA256(), 32, salt or None, info)`;
- `AESGCM(key)`;
- `ec.ECDSA(hashes.SHA256())`, with `encode_dss_signature` / `decode_dss_signature` for raw `r || s`;
- `EllipticCurvePublicKey.from_encoded_point`, which rejects points off the curve.

See the reference.

## 11. Failure handling and downgrade

| Condition | Host | Device |
|---|---|---|
| size, magic, unknown version, wrong direction, unknown flag, other workspace or key version | drop | drop |
| mailbox id differs from the header's rid | drop | drop |
| bad tag | drop | drop |
| unknown device, revoked device, bad signature | refusal (`not_paired`, `revoked`, `bad_signature`), within the host-wide budget, otherwise drop | drop (bad host signature) |
| invalid framing after a verified signature | refusal `malformed`, recorded | drop |
| old or future timestamp | refusal `stale_timestamp` + `host_ms`, recorded | drop, except a `stale_timestamp` refusal (§5.1) |
| repeated, zero or too old seq | refusal `stale_sequence` + `high`, recorded | — (a chunk out of order: drop) |
| another device's stream | refusal `forbidden_scope`, recorded | — |
| known rid, same bytes | stored outcome or stored refusal, or `already_done` / `unknown`; never runs again | — |
| known rid, other bytes or other device | refusal `rid_conflict` | — |
| response for a rid that is not pending | — | drop |

**Downgrade resistance:**

- Version 1 has exactly one suite and no negotiation.
- The version, direction, flags and key version are inside the AAD and the signature, so nobody can
  change them without failing both.
- A host drops every version it does not speak and never falls back.
- A device pins the version with its pairing and never sends a lower one; a response must carry the
  request's version.
- Every label ends in a version (`…/v1`), so a future version derives unrelated keys and signatures that
  cannot be confused with these. That is also how a version 2 can add a per-device secret (D1) without
  breaking v1.

## 12. Test vectors

`tests/bridge_vectors.json`. All keys are FAKE (SHA-256 of a public label). Each case gives the exact
input bytes, the state, the clock and the expected result. A host case with `steps` runs its envelopes
in order against **one** state, starting from the given initial state, so what a step leaves behind is
checked too. The `why` fields are informative and not part of the contract.

| Section | Covers |
|---|---|
| `hkdf` | K_ws for two workspaces, K_msg |
| `seal` | header encoding, K_msg, zero nonce, AAD, `ciphertext || tag` |
| `sign`, `sig_scalars` | valid, wrong key, changed message, the malleable twin, `r = 0`, `r = n`, `s = n`, `r = n + 1`, `r = n − 1`, short |
| `ids` | device ids (and that another workspace gives another id), fingerprints, the host pin |
| `host_cases` | full request; replay without an outcome, of a finished request, from another device's record; same rid with other content; old, future and edge timestamps; a future-dated envelope refused and then in the window; tampered tag and header; mailbox id mismatch; wrong-device signature; unknown device; refusal budget (unverified dropped, signed still runs); unknown version, direction, flag; another workspace's key; revoked; meta over 64 KiB; seq 0, repeated, below the window, reordered, the sliding window, stored refusal versus a moved window; own and another device's stream; oversize; pairing (first, twice, status, other key, bad MAC, no offer, stale, device id not of the key, phone link with and without proof) |
| `device_cases` | full response chunk; signed by a device key; for another device; for an unknown request; tampered tag; old timestamp, and corrected by the offset; out of order; mailbox mismatch; refusal chunk; refusal without `LAST`; a `stale_timestamp` refusal to a skewed clock |
| `pairing` | the link fragment, host pin, pairing MAC, device id, fingerprint, phone-link proof |
| `shown` | controls and bidi removed, no normalisation, lone surrogate |
| `assertion` | challenge inputs and bytes, lease challenge, registration challenge, 17 verification cases with a fake authenticator |

Every MUST in §3–§9 that the reference implements has a negative vector.
`tests/test_bridge_protocol_mutations.py` applies 32 mutations to the reference (the first review's 16,
plus 16 for the rules added since) and fails unless the vectors catch every one.

Not covered by vectors yet: parsing a real WebAuthn attestation object (CBOR) at registration, and the
`credential_begin` / `credential_finish` requests of a pending device (F2).

## 13. Reused and new

**Reused from TIX unchanged:**

- MK and its storage on every device;
- re-opening MK in the browser with the KEK (`approve.js`);
- the space id as workspace id;
- AES-256-GCM with a 128-bit tag;
- HKDF-SHA-256, as already used by `deriveRaw`, `approvalKey` and upload links;
- canonical JSON;
- the 100-bit fingerprint format;
- the fragment-secret link pattern of public links and `/pair`;
- the phone's sign-only HMAC pairing key (for the link proof);
- non-extractable CryptoKeys in IndexedDB;
- the mailbox and its limits.

**New:**

- the HKDF and MAC labels `sharing/bridge/*/v1`;
- K_ws and the per-envelope K_msg;
- the envelope;
- device ECDSA signing keys and the host's registry;
- the host signing key and its pin;
- the pairing link and ceremony;
- the sequence window and the request store;
- the clock offset;
- the WebAuthn registration and challenge formats;
- the audit log;
- a `sharing` CLI command that hands K_ws to `orch` (F1).

## 14. Decisions needed

Until the owner decides, the specification takes the more restrictive option for each, with one stated
exception (D4).

- **D1 — Confidentiality between master-key holders. PENDING THE OWNER.** As the ticket decided, K_ws is
  derivable from MK. So every master-key holder can read bridge traffic, terminal output included, if it
  also obtains the ciphertext, which today takes the server's position. That includes approved CLI
  devices in every repo, among them a remote agent's VM for another client, and a compromised TIX web
  app. The question is whether to mix a per-device ECDH secret, agreed with the host key at pairing,
  into K_msg now or in a version 2, so that only that device and the host can read its traffic. Cost: a
  second non-extractable key per device and a pairing field; scopes stay host-enforced. Meanwhile: as
  written. The versioned labels and `key_version` let a v2 come without breaking v1.
- **D2 — The existing phone pairing.** Meanwhile: it does not replace the link and fingerprint steps
  (§8.2). A link between the two pairings is recorded only with the HMAC proof, and revoking either
  revokes both.
- **D3 — What the 15-minute lease covers.** Meanwhile: only input to an already open terminal stream that
  the same device opened. Starting a terminal or an agent, and every Factory action, needs a fresh
  assertion.
- **D4 — Synced (backup-eligible) passkeys.** Refusing BE credentials is the restrictive option, but on an
  iPhone every platform passkey is synced, so it would make Type and Factory unusable on a phone.
  Meanwhile: **accept** them, record BE/BS, show them in the Remote tab, and rely on the device key for
  the binding to the device (§9.6). This is the one less restrictive default; please confirm.
- **D5 — Sign count that did not increase.** Meanwhile: refuse that assertion and show it in the Remote
  tab, without suspending the credential. For BE credentials the counter is advisory. Both-zero counts
  are accepted.
- **D6 — Host key rotation.** Meanwhile: no in-band rotation; a new host key means every device pairs
  again. On a pin failure the device drops every response, says "the host key changed: pair again", and
  never trusts a new key on first use.
- **D7 — How far a revocation reaches.** Meanwhile: revoking a device in one workspace's Remote tab
  revokes it in every workspace registry on that computer. It does not reach other computers; their
  hosts are revoked separately.
- **D8 — The accepted limits. PENDING THE OWNER'S RECONFIRMATION.** The limits are:
  - a TIX web app compromised after pairing can recover MK and read every workspace's bridge traffic it
    obtains, which goes beyond "acts at the device's scope", and it can obtain an assertion for
    something other than what it shows;
  - a TIX web app compromised **at pairing time** can create a permanent device with an extractable or
    foreign key and a software credential, whose "fresh" assertions need no user verification, until it
    is revoked (§1). The mitigations are the audit log in the Remote tab and the rate limit on fresh
    actions;
  - a local process running as the owner on the host computer can read MK, the host key, the registry
    and the request store, with the orch command guard as the only barrier (§2.7).
- **D9 — Lifetimes and limits.** Meanwhile:
  - pairing offer: 10 minutes, single use;
  - assertion and registration challenges: 120 s;
  - rid records: `max(received_at + 900 s, ts_ms + 360 s)`, refusals included;
  - stored replay body: 64 KiB;
  - refusal budget: 10 per minute, host-wide;
  - fresh actions: at most 6 per 10 minutes per device;
  - stream silence that counts as closed: 60 s.

## 15. Follow-ups (not in this document's scope)

- **F1:** a `sharing` CLI command, a guarded path, that hands over K_ws for a workspace it owns, so that
  `orch` never reads MK (skill change, before R3).
- **F2:** vectors for parsing a WebAuthn attestation object (CBOR, COSE key) at registration, and for
  `credential_begin` / `credential_finish`, with R3.
- **F3:** the HTTP mapping inside `meta` (method, path, header allow-list, the remote client address) is
  R2/R3's; this document fixes only the envelope around it.
- **F4:** keystroke requests show typing rhythm and length to the server (§4); R6 should batch or pad
  them.
- **F5:** update `docs/threat-model.md` with the bridge's cleartext header fields once R3 and R10 ship.
- **F6:** a KEK with the `unwrapKey` usage, so that raw MK never reaches JavaScript (§10.1).
