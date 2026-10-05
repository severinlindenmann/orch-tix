// fileshare/static/js/bridge-session.js: the device's rules for one paired workspace (docs/bridge-protocol.md §4, §5, §7):
// pending requests, identical-bytes retry, the clock offset, the sequence resync, stream silence and the host-key pin.
// No transport: the caller posts `envelope` to the mailbox and hands every chunk it gets back to receive().
import { bytesToHex, hexToBytes } from "./crypto.js";
import { F_STREAM, HostKeyError, MESSAGES, ZERO_ID, openResponse, sealRequest } from "./bridge-crypto.js";
import { sequenceCounter } from "./bridge-store.js";

export const STREAM_SILENCE_MS = 60_000;   // §4: a stream with no chunk for more than this is closed
export const PIN_FAILURES = 3;             // §7: this many host-signature failures in a row, none verified between

export class DeviceSession {
  // workspace: hex; kWs, keyVersion: from bridge-store.js; deviceId: 16 bytes; signKey: the device's private key;
  // hostKey: the pinned host key (pinnedHostKey). counter defaults to the persisted one.
  constructor({ workspace, kWs, keyVersion, deviceId, signKey, hostKey, counter, offsetMs = 0, now = Date.now }) {
    Object.assign(this, { workspace, kWs, keyVersion, deviceIdBytes: deviceId, device: bytesToHex(deviceId), signKey, hostKey,
      offsetMs, now, counter: counter || sequenceCounter(workspace) });
    this.pending = new Map();
    this.pinFailures = 0;
    this.hostKeyChanged = false;
  }

  // A new request: new rid, next seq, ts = device clock + offset. Returns {id (rid hex = mailbox id), envelope}.
  async request({ meta, data, flags = 0, stream = ZERO_ID }) {
    if (this.hostKeyChanged) throw new HostKeyError();
    const seq = await this.counter.next();
    const { rid, envelope } = await sealRequest({ kWs: this.kWs, signKey: this.signKey, workspace: hexToBytes(this.workspace),
      deviceId: this.deviceIdBytes, keyVersion: this.keyVersion, seq, tsMs: this.now() + this.offsetMs, meta, data, flags, stream });
    const id = bytesToHex(rid);
    this.pending.set(id, { next: 0, stream: !!(flags & F_STREAM), envelope, args: { meta, data, flags, stream }, lastAt: this.now() });
    return { id, envelope };
  }

  // §5.3: a retry of a request that got no answer sends exactly the same bytes (after the caller cancelled its mailbox
  // route, or its TTL passed). Never re-sealed.
  retry(id) {
    return this.pending.get(id)?.envelope ?? null;
  }

  // One response chunk with the mailbox's cleartext {id, idx, last, stream}. A drop is never rendered. An accepted
  // chunk may carry `resend` (the arguments to send again as a NEW request) and `message` (a fixed text to show).
  async receive(env, mailbox) {
    if (this.hostKeyChanged) return { result: "drop", why: "host_key_changed", message: MESSAGES.hostKeyChanged };
    const r = await openResponse(this, env, mailbox, this.now());
    if (r.result === "drop") {
      if (r.why === "host_signature" && ++this.pinFailures >= PIN_FAILURES) {
        this.hostKeyChanged = true;      // drop everything; only a new pairing link replaces the pin
        this.pending.clear();
        r.message = MESSAGES.hostKeyChanged;
      }
      return r;
    }
    this.pinFailures = 0;
    const pend = this.pending.get(r.rid);
    pend.lastAt = this.now();
    if (r.offsetMs !== undefined) this.offsetMs = r.offsetMs;
    if (r.refusal) {
      const { refusal: code, high } = r.meta;
      if (code === "stale_sequence" && Number.isSafeInteger(high) && high >= 0) {
        await this.counter.atLeast(high + 1);
        r.resend = pend.args;
      } else if (code === "stale_timestamp" && r.offsetMs !== undefined) {
        r.resend = pend.args;
      } else if (code === "already_done") {
        r.message = MESSAGES.outcomeUnknown;
      }
      if (r.clockWrong) r.message = MESSAGES.clockWrong;
    }
    if (r.last) this.pending.delete(r.rid);
    return r;
  }

  // §4: streams silent for more than 60 s are closed. Returns their ids.
  expire() {
    const t = this.now(), gone = [];
    for (const [id, p] of this.pending) {
      if (p.stream && t - p.lastAt > STREAM_SILENCE_MS) { this.pending.delete(id); gone.push(id); }
    }
    return gone;
  }
}

