// Every vector of tests/bridge_vectors.json through the PRODUCTION device module (fileshare/static/js/bridge-crypto.js).
// Shared by tests/js/bridge-conformance.test.mjs (Node) and tests/browser/test_bridge_module.py (Chromium, under the
// app's own CSP), so it imports nothing: the caller passes the modules in. Throws on the first mismatch; returns how
// many vectors (and chain steps) each section ran.
export async function conformance(B, C, VEC) {
  const { hexToBytes: hex, bytesToHex: toHex, b64u, unb64u, canonicalJson } = C;
  const subtle = globalThis.crypto.subtle, te = new TextEncoder();
  const fail = (msg) => { throw new Error(msg); };
  const same = (a, b, msg) => {
    const x = typeof a === "object" ? canonicalJson(a) : a, y = typeof b === "object" ? canonicalJson(b) : b;
    if (x !== y) fail(`${msg}: ${x} !== ${y}`);
  };
  const throws = async (f, msg) => {
    try { await f(); } catch { return; }
    fail(`${msg}: did not throw`);
  };
  const n = {};
  const count = (k) => { n[k] = (n[k] || 0) + 1; };
  const ws = hex(VEC.keys.workspace);
  const kWs = await B.workspaceKey(hex(VEC.keys.mk), VEC.keys.workspace);
  if (kWs.extractable !== false || canonicalJson(kWs.usages) !== '["deriveKey"]') fail("K_ws must be non-extractable, deriveKey only");
  const probe = te.encode("bridge conformance probe");
  const gcm0 = { name: "AES-GCM", iv: new Uint8Array(12), tagLength: 128 };
  const encUnder = async (key) => toHex(new Uint8Array(await subtle.encrypt(gcm0, key, probe)));

  // hkdf: the production K_ws / K_msg cannot be exported, so each is compared by what it encrypts.
  for (const c of VEC.hkdf) {
    const info = new TextDecoder().decode(hex(c.info));
    let got, ref;
    if (c.name === "message_key") {
      got = await B.messageKey(kWs, hex(c.salt), "encrypt");
      ref = await subtle.importKey("raw", hex(c.okm), "AES-GCM", false, ["encrypt"]);
    } else {
      const mk = hex(c.ikm), w = info.slice("sharing/bridge/ws/v1|".length);
      got = await B.messageKey(await B.workspaceKey(mk, w), new Uint8Array(16), "encrypt");
      if (mk.some((x) => x !== 0)) fail(`${c.name}: the raw MK was not zeroed`);
      ref = await B.messageKey(await subtle.importKey("raw", hex(c.okm), "HKDF", false, ["deriveKey"]), new Uint8Array(16), "encrypt");
    }
    same(await encUnder(got), await encUnder(ref), c.name);
    count("hkdf");
  }

  // seal: header bytes, K_msg, zero nonce, AAD, ciphertext || tag, framing
  for (const c of VEC.seal) {
    const f = c.header_fields;
    const h = B.encodeHeader({ direction: f.direction, flags: f.flags, keyVersion: f.key_version, workspace: hex(f.workspace),
      deviceId: hex(f.device), rid: hex(f.rid), stream: hex(f.stream), seq: f.seq, tsMs: f.ts_ms, salt: hex(f.salt) });
    same(toHex(h), c.header, `${c.name} header`);
    const d = B.decodeHeader(h);
    same([d.magic, d.version, d.direction, d.flags, d.keyVersion, toHex(d.workspace), toHex(d.device), toHex(d.rid), toHex(d.stream),
      String(d.seq), String(d.tsMs), toHex(d.salt)], [true, f.version, f.direction, f.flags, f.key_version, f.workspace, f.device,
      f.rid, f.stream, String(f.seq), String(f.ts_ms), f.salt], `${c.name} decoded`);
    same(await encUnder(await B.messageKey(kWs, d.salt, "encrypt")),
      await encUnder(await subtle.importKey("raw", hex(c.message_key), "AES-GCM", false, ["encrypt"])), `${c.name} K_msg`);
    same(c.nonce, "00".repeat(12), "nonce");
    same(toHex(await B.sealBody(kWs, h, hex(c.plaintext))), c.sealed, `${c.name} sealed`);
    const pt = await B.openBody(kWs, h, hex(c.sealed));
    same(toHex(pt), c.plaintext, `${c.name} opened`);
    const { meta, data } = B.unframe(pt);
    same(toHex(B.frame(meta, data)), c.plaintext, `${c.name} framing`);
    count("seal");
  }

  // sign + sig_scalars: raw r || s, the 1 … n-1 range checked before verifying
  for (const c of VEC.sign) {
    same(await B.verifySigned(await B.importPublicKey(hex(c.pub)), hex(c.sig), hex(c.msg)), c.valid, c.name);
    count("sign");
  }
  same(toHex(B.signedBytes(hex(VEC.seal[0].header), hex(VEC.seal[0].sealed))), VEC.sign[0].msg, "signed bytes");
  const pubA = await B.importPublicKey(hex(VEC.sign[0].pub));
  for (const c of VEC.sig_scalars) {
    same(B.scalarsInRange(hex(c.sig)), c.in_range, c.name);
    if (!c.in_range) same(await B.verifySigned(pubA, hex(c.sig), hex(VEC.sign[0].msg)), false, `${c.name} verifies`);
    count("sig_scalars");
  }

  // ids: device ids per workspace, fingerprints, the host pin
  for (const name of ["device_a", "device_b"]) {
    const c = VEC.ids[name], pub = hex(c.pub);
    same(toHex(await B.deviceId(ws, pub)), c.device_id, `${name} id`);
    same(toHex(await B.deviceId(new Uint8Array(16), pub)), c.device_id_other_workspace, `${name} id, other workspace`);
    same(await B.deviceFingerprint(pub), c.fingerprint, `${name} fingerprint`);
    count("ids");
  }
  same(toHex(await B.hostPin(hex(VEC.keys.host.pub))), VEC.ids.host_pin, "host pin");
  count("ids");

  // pairing: link, pin, MAC (through the pair meta builder), device id, fingerprint, phone-link proof
  const p = VEC.pairing;
  const link = B.parsePairFragment(p.link_fragment);
  if (!link) fail("link fragment did not parse");
  same([link.workspace, toHex(link.pairingId), toHex(link.secret), toHex(link.hostPin)], [p.workspace, p.pairing_id, p.secret, p.host_pin], "link");
  same(B.parsePairFragment("#" + p.link_fragment) !== null, true, "link with #");
  same(toHex(await B.hostPin(hex(p.host_pub))), p.host_pin, "pairing host pin");
  await B.hostKeyFromPin(hex(p.host_pub), hex(p.host_pin));
  const devPub = hex(p.device_pub);
  same(toHex(await B.deviceId(hex(p.workspace), devPub)), p.device_id, "pairing device id");
  same(await B.deviceFingerprint(devPub), p.device_fingerprint, "pairing fingerprint");
  const phoneKey = await subtle.importKey("raw", hex(p.phone_key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const pairMeta = async (meta) => B.pairRequestMeta({ link: B.parsePairFragment(p.link_fragment), pub: hex(meta.pub), label: meta.label,
    phone: meta.phone_id ? { phoneId: meta.phone_id, key: phoneKey } : undefined });
  const built = await pairMeta({ pub: p.device_pub, label: "Phone", phone_id: p.phone_id });
  same([built.mac, built.phone_proof], [p.mac, p.phone_proof], "pairing mac and phone proof");
  const used = B.parsePairFragment(p.link_fragment);
  await B.pairRequestMeta({ link: used, pub: devPub, label: "Phone" });
  if (used.secret.some((x) => x !== 0)) fail("the pairing secret was not zeroed");
  count("pairing");

  // host_cases: the device is the SENDER of these. Every envelope is decoded, re-encoded byte for byte (header and
  // ciphertext || tag) and checked by the production primitives, and what they say must agree with the host's verdict.
  const pubOf = new Map();
  for (const k of ["device_a", "device_b", "host", "intruder", "authenticator"]) {
    pubOf.set(toHex(await B.deviceId(ws, hex(VEC.keys[k].pub))), hex(VEC.keys[k].pub));
  }
  const tagDrops = new Set();
  async function request(name, step) {
    if (step.envelope_zeros !== undefined) {
      if (!(step.envelope_zeros > B.MAX_REQUEST) || step.expect.result !== "drop") fail(`${name}: oversize`);
      return;
    }
    const env = hex(step.envelope), e = step.expect;
    const { header, body, sig } = B.splitEnvelope(env);
    const h = B.decodeHeader(header);
    const fields = { direction: h.direction, flags: h.flags, keyVersion: h.keyVersion, workspace: h.workspace, deviceId: h.device,
      rid: h.rid, stream: h.stream, seq: Number(h.seq), tsMs: Number(h.tsMs), salt: h.salt };
    if (h.tsMs > BigInt(Number.MAX_SAFE_INTEGER)) await throws(() => B.encodeHeader(fields), `${name}: a ts_ms the device never writes`);
    else same(toHex(B.encodeHeader(fields).subarray(5)), toHex(header.subarray(5)), `${name} header`);   // the encoder writes v1 only
    same(h.magic, true, `${name} magic`);
    if (h.version !== 1 || h.direction !== B.TO_HOST || h.flags & ~B.F_STREAM) {       // a header the device never writes
      same(e.result, "drop", `${name} shape`);
      if (h.flags & ~B.F_STREAM) await throws(() => B.sealRequest({ kWs, signKey: null, workspace: ws, deviceId: h.device, keyVersion: 1,
        seq: 1, tsMs: 0, meta: {}, flags: h.flags }), `${name} sealRequest`);
      return;
    }
    let pt;
    try { pt = await B.openBody(kWs, header, body); } catch { pt = null; }
    if (pt === null) {                                  // only the tag cases fail to open, and the host drops them
      same(e.result, "drop", `${name} tag`);
      tagDrops.add(name);
      return;
    }
    same(toHex(await B.sealBody(kWs, header, pt)), toHex(body), `${name} re-seal`);
    let framed;
    try { framed = B.unframe(pt); } catch { framed = null; }
    if (framed === null) { same(e.code, "malformed", `${name} framing`); return; }
    if (e.code === "malformed") {       // the host's strict meta (§3.5): only meta the device would never write
      let differs = toHex(B.frame(framed.meta, framed.data)) !== toHex(pt);
      if (!differs && framed.meta.op === "pair") {
        const { phone_proof: _p, ...mine } = await pairMeta(framed.meta), { phone_proof: _q, ...theirs } = framed.meta;
        differs = canonicalJson(mine) !== canonicalJson(theirs);
      }
      same(differs, true, `${name}: malformed, but the device could have sent it`);
      return;
    }
    same(toHex(B.frame(framed.meta, framed.data)), toHex(pt), `${name} canonical meta`);
    if (e.meta !== undefined) same([framed.meta, toHex(framed.data)], [e.meta, e.data], `${name} meta and data`);
    const m = framed.meta, claimed = pubOf.get(toHex(h.device));
    const signer = m.op === "pair" ? hex(m.pub) : claimed;
    if (signer) {
      const ok = await B.verifySigned(await B.importPublicKey(signer), sig, B.signedBytes(header, body));
      if (!ok && !(e.code === "bad_signature" || e.result === "drop")) fail(`${name}: signature fails but the host did not refuse`);
      if (ok && e.code === "bad_signature" && !(m.op === "pair" && toHex(await B.deviceId(ws, signer)) !== toHex(h.device))) {
        fail(`${name}: signature verifies but the host refused it`);
      }
    } else if (!(["not_paired", "pairing_closed"].includes(e.code) || e.result === "drop")) {
      fail(`${name}: no key for the claimed device, but the host did not refuse`);
    }
    if (m.op === "pair" && [...m.label].length > 80) {   // the host truncates such a label; the device never sends one
      await throws(() => pairMeta(m), `${name}: a label over 80 code points`);
      return;
    }
    if (m.op === "pair") {                              // the production meta builder makes the same meta, MAC and proof
      const { phone_proof: mineProof, ...mine } = await pairMeta(m), { phone_proof: theirProof, ...theirs } = m;
      if (mine.mac === m.mac) same(mine, theirs, `${name} pair meta`);
      else if (e.result === "pair_pending") fail(`${name}: the host accepted a MAC the device would not make`);
      if (theirProof !== undefined) same(mineProof === theirProof, e.phone_link !== null, `${name} phone proof`);
    }
  }
  for (const c of VEC.host_cases) {
    if (c.steps) {
      for (const [i, s] of c.steps.entries()) { await request(`${c.name}[${i}]`, s); count("host_steps"); }
    } else {
      await request(c.name, c);
    }
    count("host_cases");
  }
  same([...tagDrops].sort(), ["other_workspace_key", "tampered_header_field", "tampered_tag"], "the envelopes that fail the tag");

  // device_cases: the production response check (§7), against the vector's pending state, offset and clock
  const hostKey = await B.importPublicKey(hex(VEC.keys.host.pub));
  for (const c of VEC.device_cases) {
    const pending = new Map(Object.entries(c.pending).map(([k, v]) => [k, { ...v, offsetAdopted: v.offset_adopted }]));
    const ctx = { workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: VEC.ids.device_a.device_id, hostKey, pending, offsetMs: c.offset_ms };
    const r = await B.openResponse(ctx, hex(c.envelope), c.mailbox, c.now_ms);
    const got = { result: r.result };
    if (r.result === "accept") Object.assign(got, { last: r.last, refusal: r.refusal, meta: r.meta, data: toHex(r.data) });
    got.offset_ms = r.offsetMs ?? null;
    got.clock_wrong = r.clockWrong ?? null;
    got.pin_failure = r.pinFailure ?? null;
    same(got, c.expect, c.name);
    const before = c.pending[toHex(B.decodeHeader(hex(c.envelope).subarray(0, 104)).rid)];
    if (r.result === "accept") same(pending.get(r.rid).next, before.next + 1, `${c.name} next`);
    else for (const [k, v] of Object.entries(c.pending)) same(pending.get(k).next, v.next, `${c.name} unchanged`);
    count("device_cases");
  }

  const ai = VEC.assertion.challenge_inputs;
  const parts = { workspace: hex(ai.workspace), deviceId: hex(ai.device), rid: hex(ai.rid), purpose: ai.purpose, scope: ai.scope,
    expiresMs: ai.expires_ms, nonce: hex(ai.nonce), subject: ai.subject };
  // pin_runs (§7): the run of pin failures the device counts; a verified chunk resets it, other drops neither count nor reset
  const byName = new Map(VEC.device_cases.map((c) => [c.name, c]));
  for (const c of VEC.pin_runs) {
    let run = 0, alarm = false;
    const got = [];
    for (const name of c.steps) {
      const d = byName.get(name);
      const pending = new Map(Object.entries(d.pending).map(([k, v]) => [k, { ...v }]));
      const r = await B.openResponse({ workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: VEC.ids.device_a.device_id,
        hostKey, pending, offsetMs: d.offset_ms }, hex(d.envelope), d.mailbox, d.now_ms);
      if (r.pinFailure) { run += 1; alarm ||= run >= 3; } else if (r.result === "accept") run = 0;
      got.push(alarm);
    }
    same(got, c.alarm, c.name);
    count("pin_runs");
  }

  // pending_answers (§8.1 step 4): the one response opened before a host key is pinned. The composition of production
  // primitives a caller uses: tag first, host_pub (bytes) against the link's pin, the signature with it, then §7.
  for (const c of VEC.pending_answers) {
    const env = hex(c.envelope), { header, body, sig } = B.splitEnvelope(env);
    let got;
    try {
      const { meta } = B.unframe(await B.openBody(kWs, header, body));
      if (meta.state !== "pending" || typeof meta.host_pub !== "string") throw new Error("not a pending answer");
      const key = await B.hostKeyFromPin(hex(meta.host_pub), hex(c.host_pin));
      if (!await B.verifySigned(key, sig, B.signedBytes(header, body))) throw new Error("host signature");
      const pending = new Map(Object.entries(c.pending).map(([k, v]) => [k, { ...v }]));
      const r = await B.openResponse({ workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: VEC.ids.device_a.device_id,
        hostKey: key, pending, offsetMs: 0 }, env, c.mailbox, c.now_ms);
      got = r.result === "accept" ? { result: "accept", host_pub: meta.host_pub, fingerprint: meta.fingerprint } : { result: "drop" };
    } catch {
      got = { result: "drop" };
    }
    same(got, c.expect, c.name);
    count("pending_answers");
  }

  // labels (§8.1 step 2): the device refuses more than 80 code points (the host's truncation is checked in Python)
  for (const c of VEC.labels) {
    const s = String.fromCodePoint(...c.codepoints);
    let ok = true;
    try { await B.pairRequestMeta({ link: B.parsePairFragment(p.link_fragment), pub: devPub, label: s }); } catch { ok = false; }
    same(ok, c.device_accepts, c.name);
    count("labels");
  }

  // links (§8.1 step 1)
  for (const c of VEC.links) {
    const l = B.parsePairFragment(c.fragment);
    same(l && { workspace: l.workspace, pairing_id: toHex(l.pairingId), secret: toHex(l.secret), host_pin: toHex(l.hostPin) },
      c.parsed, c.name);
    count("links");
  }

  // challenge_parts (§9.2, §9.4): the module takes the nonce as bytes and expires_ms as an integer; parsing the host's
  // answer (64 lower-case hex, a JSON integer) is the caller's. What the module itself refuses is checked here.
  for (const c of VEC.challenge_parts) {
    const exp = c.meta.expires_ms;
    if (!Number.isSafeInteger(exp) || exp < 0) {
      await throws(() => B.assertionChallenge({ ...parts, expiresMs: exp }), `${c.name}: expires_ms`);
    }
    count("challenge_parts");
  }

  // shown: the device never re-cleans; it takes exactly what it received, and refuses what is not scalar values
  for (const c of VEC.shown) {
    const lone = c.input.some((x) => x >= 0xD800 && x <= 0xDFFF);
    const s = String.fromCodePoint(...c.input.filter((x) => x < 0xD800 || x > 0xDFFF)) + (lone ? "\uD800" : "");
    if (c.expect === null) await throws(() => B.acceptShown(s), c.name);
    else {
      same(B.acceptShown(c.expect), c.expect, `${c.name} cleaned text kept`);
      same(B.acceptShown(s), s, `${c.name} not re-cleaned`);
    }
    count("shown");
  }

  // assertion: the challenges the device recomputes, and for each verification case the challenge its client data names
  const a = VEC.assertion, i = a.challenge_inputs;
  same(canonicalJson(B.subjectOf(i.subject)), i.subject_json, "subject json");
  const ch = toHex(await B.assertionChallenge(parts));
  same(ch, a.challenge, "assertion challenge");
  same(toHex(await B.assertionChallenge({ ...parts, purpose: "lease", subject: { kind: "lease", shown: "Type for 15 minutes", digest: "" } })),
    a.lease_challenge, "lease challenge");
  const rc = a.registration_challenge;
  same(toHex(await B.registrationChallenge({ workspace: hex(rc.workspace), deviceId: hex(rc.device), expiresMs: rc.expires_ms,
    nonce: hex(rc.nonce) })), rc.challenge, "registration challenge");
  const other = toHex(await B.assertionChallenge({ ...parts, subject: { ...i.subject, shown: "Start epic E-13" } }));
  for (const c of a.cases) {
    const client = JSON.parse(new TextDecoder().decode(hex(c.assertion.client_data_json)));
    same(toHex(unb64u(client.challenge)), c.name === "other_subject" ? other : ch, `${c.name} challenge`);
    const cred = hex(c.assertion.credential_id), win = fakeWindow(cred, c.assertion, hex);
    const out = await B.assert(hex(i.rid), B.assertionOptions({ challenge: hex(ch), credentialId: cred, rpId: "tix.example" }), win);
    same(out, { for: i.rid, credential_id: b64u(cred), authenticator_data: b64u(hex(c.assertion.authenticator_data)),
      client_data_json: b64u(hex(c.assertion.client_data_json)), signature: b64u(hex(c.assertion.signature)) }, `${c.name} payload`);
    count("assertion_cases");
  }
  count("assertion_challenges"); count("assertion_challenges"); count("assertion_challenges");
  return n;
}

// §7 and §8.1 rules checked a second time with chunks built here (the amended vectors, #82, now cover them too). Each
// chunk here is sealed with K_ws and signed with the FAKE host key, so only the rule under test can drop it.
export async function ownChecks(B, C, VEC) {
  const { hexToBytes: hex, b64u, bytesToHex: toHex } = C;
  const subtle = globalThis.crypto.subtle;
  const fail = (msg) => { throw new Error(msg); };
  const ws = hex(VEC.keys.workspace), dev = hex(VEC.ids.device_a.device_id), now = 1_790_000_000_000;
  const kWs = await B.workspaceKey(hex(VEC.keys.mk), VEC.keys.workspace);
  const hp = hex(VEC.keys.host.pub);
  const hostPriv = await subtle.importKey("jwk", { kty: "EC", crv: "P-256", d: b64u(hex(VEC.keys.host.d)), x: b64u(hp.subarray(1, 33)),
    y: b64u(hp.subarray(33)), ext: false }, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const hostKey = await B.importPublicKey(hp), rid = hex("cd".repeat(16));
  async function chunk({ version = 1, direction = B.TO_DEVICE, flags = B.F_LAST, keyVersion = 1, workspace = ws, ts = now, meta = { status: 200 }, pad = 0 }) {
    const header = B.encodeHeader({ direction, flags, keyVersion, workspace, deviceId: dev, rid, stream: B.ZERO_ID, seq: 0, tsMs: ts,
      salt: crypto.getRandomValues(new Uint8Array(16)) });
    header[4] = version;
    const body = await B.sealBody(kWs, header, B.frame(meta, new Uint8Array(pad)));
    return B.cat(header, body, new Uint8Array(await subtle.sign({ name: "ECDSA", hash: "SHA-256" }, hostPriv, B.signedBytes(header, body))));
  }
  const mb = { id: toHex(rid), idx: 0, last: true, stream: false };
  const cases = [
    ["accepted as built", {}, mb, "accept"],
    ["direction 1", { direction: B.TO_HOST }, mb, "drop"],
    ["version 2", { version: 2 }, mb, "drop"],
    ["unknown flag", { flags: B.F_LAST | 0x08 }, mb, "drop"],
    ["larger than 256 KiB", { pad: B.MAX_CHUNK }, mb, "drop"],
    ["exactly 256 KiB", { pad: B.MAX_CHUNK - 184 - 4 - 14 }, mb, "accept"],
    ["another key version", { keyVersion: 2 }, mb, "drop"],
    ["another workspace", { workspace: new Uint8Array(16) }, mb, "drop"],
    ["exactly 300 s old", { ts: now - B.WINDOW_MS }, mb, "accept"],
    ["300 s and 1 ms old", { ts: now - B.WINDOW_MS - 1 }, mb, "drop"],
    ["exactly 300 s ahead", { ts: now + B.WINDOW_MS }, mb, "accept"],
    ["300 s and 1 ms ahead", { ts: now + B.WINDOW_MS + 1 }, mb, "drop"],
    ["mailbox says stream", {}, { ...mb, stream: true }, "drop"],
    ["mailbox idx differs", {}, { ...mb, idx: 1 }, "drop"],
    ["mailbox idx not an integer", {}, { ...mb, idx: "0" }, "drop"],
    ["mailbox id differs", {}, { ...mb, id: "ab".repeat(16) }, "drop"],
    ["a stale_timestamp without an integer host_ms is not exempt",
      { flags: B.F_LAST | B.F_REFUSAL, ts: now - 10 ** 7, meta: { refusal: "stale_timestamp", host_ms: String(now) } }, mb, "drop"],
  ];
  for (const [name, opts, mailbox, want] of cases) {
    const ctx = { workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: toHex(dev), hostKey, offsetMs: 0,
      pending: new Map([[toHex(rid), { next: 0, stream: false }]]) };
    const env = await chunk(opts);
    if (name === "exactly 256 KiB" && env.length !== B.MAX_CHUNK) fail(`${name}: built ${env.length} bytes`);
    const r = await B.openResponse(ctx, env, mailbox, now);
    if (r.result !== want) fail(`${name}: ${r.result} (${r.why})`);
  }
  const streamCtx = { workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: toHex(dev), hostKey, offsetMs: 0,
    pending: new Map([[toHex(rid), { next: 0, stream: true }]]) };
  if ((await B.openResponse(streamCtx, await chunk({}), mb, now)).result !== "drop") fail("a stream's rid answered without STREAM");
  const f = VEC.pairing.link_fragment;
  for (const bad of [f.replace("v1.", "v2."), f.replace("v1.", "v11."), f.replace("v1.", ""),
    f.replace(VEC.pairing.workspace, VEC.pairing.workspace.toUpperCase()), f.replace(VEC.pairing.pairing_id, VEC.pairing.pairing_id.toUpperCase())]) {
    if (B.parsePairFragment(bad) !== null) fail(`link ${bad.slice(0, 40)} parsed`);
  }

  // The pin alarm (§7): a chunk only a K_ws holder could make (tag verifies, host signature does not) is a pin failure;
  // one built from the cleartext fields with random body and signature, which the TIX server can make, is not.
  const ctx = () => ({ workspace: VEC.keys.workspace, kWs, keyVersion: 1, device: toHex(dev), hostKey, offsetMs: 0,
    pending: new Map([[toHex(rid), { next: 0, stream: false }]]) });
  const good = await chunk({}), keyed = good.slice();
  keyed.set(crypto.getRandomValues(new Uint8Array(64)), keyed.length - 64);
  const r1 = await B.openResponse(ctx(), keyed, mb, now);
  if (r1.why !== "host_signature" || r1.pinFailure !== true) fail("a K_ws-sealed chunk with a bad signature is not a pin failure");
  const forged = B.cat(good.subarray(0, 104), crypto.getRandomValues(new Uint8Array(good.length - 104)));
  const r2 = await B.openResponse(ctx(), forged, mb, now);
  if (r2.why !== "host_signature" || r2.pinFailure !== false) fail("a chunk forged without K_ws counts as a pin failure");

  // A label is limited in code points, not UTF-16 units: 80 with an astral character (81 units) passes, 81 does not.
  const link = () => B.parsePairFragment(f), pub = hex(VEC.pairing.device_pub), at80 = "\u{1F44D}" + "x".repeat(79);
  if ((await B.pairRequestMeta({ link: link(), pub, label: at80 })).label !== at80) fail("label of 80 code points refused");
  let refused = false;
  try { await B.pairRequestMeta({ link: link(), pub, label: at80 + "x" }); } catch { refused = true; }
  if (!refused) fail("label of 81 code points accepted");
  return cases.length + 5;
}

// A top-level window whose authenticator answers with the vector's bytes.
export function fakeWindow(rawId, a, hex) {
  const w = { navigator: { credentials: { get: async () => ({ rawId: rawId.buffer.slice(0),
    response: { authenticatorData: hex(a.authenticator_data).buffer, clientDataJSON: hex(a.client_data_json).buffer, signature: hex(a.signature).buffer } }) } } };
  w.top = w.self = w;
  return w;
}
