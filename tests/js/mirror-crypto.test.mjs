// Task 9: mirror-crypto.js against tests/vectors/mirror1.json (made by sharing.py). Same bytes both ways.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as c from "../../fileshare/static/js/crypto.js";
import { aadDecision, aadMirror, aadSpace, openMirror, openSpaceLabel, sealDecision } from "../../fileshare/static/js/mirror-crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
const byName = Object.fromEntries(VEC.cases.map((x) => [x.name, x]));
const hex = c.hexToBytes;

test("the AAD helpers give the vector bytes", () => {
  const s = byName["space-label"], m = byName.mirror, dt = byName["decision-ticket"], ds = byName["decision-space"];
  assert.equal(c.bytesToHex(aadSpace(s.space_id)), s.aad);
  assert.equal(c.bytesToHex(aadMirror(hex(m.ticket_uuid))), m.aad);
  assert.equal(c.bytesToHex(aadDecision(hex(dt.scope), hex(dt.decision_uuid))), dt.aad);
  assert.equal(c.bytesToHex(aadDecision(hex(ds.scope), hex(ds.decision_uuid))), ds.aad);
});

test("openMirror opens the wrapped DEK under MK and the doc under the DEK", async () => {
  const m = byName.mirror;
  const mk = await c.importAesKey(hex(VEC.mk));
  const row = { uuid: m.ticket_uuid, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
  const { doc, dek } = await openMirror(mk, row);
  assert.deepEqual(doc, m.obj);
  assert.equal(c.bytesToHex(dek), VEC.dek);
});

test("openMirror refuses a doc under the wrong ticket uuid", async () => {
  const m = byName.mirror;
  const mk = await c.importAesKey(hex(VEC.mk));
  const row = { uuid: "0".repeat(32), wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
  await assert.rejects(openMirror(mk, row), c.IntegrityError);
});

test("openSpaceLabel gives the label", async () => {
  const s = byName["space-label"];
  const mk = await c.importAesKey(hex(VEC.mk));
  assert.equal(await openSpaceLabel(mk, { id: s.space_id, enc_label: s.env }), s.label);
});

test("sealDecision reproduces the Python envelope for a ticket and a space scope", async () => {
  for (const name of ["decision-ticket", "decision-space"]) {
    const v = byName[name];
    const env = await sealDecision(hex(v.key), hex(v.scope), hex(v.decision_uuid), v.obj, { nonce: hex(v.nonce) });
    assert.equal(env, v.env, name);
  }
});

// ---- final review I3: the phone checks the cleartext routing against the sealed doc
import { mirrorUuid, boundToRow } from "../../fileshare/static/js/mirror-crypto.js";

test("mirrorUuid is sha256(space|key|gen)[:16] like core and the CLI (vectors)", async () => {
  for (const u of VEC.uuids) assert.equal(await mirrorUuid(u.space_id, u.key, u.gen), u.mirror_uuid);
});

test("a row is bound to its doc only when uuid = mirrorUuid(row.space, doc.id, doc.gen)", async () => {
  const u = VEC.uuids[0];
  const doc = { id: u.key, gen: u.gen };
  assert.equal(await boundToRow({ uuid: u.mirror_uuid, space: u.space_id }, doc), true);
  assert.equal(await boundToRow({ uuid: u.mirror_uuid, space: "b".repeat(32) }, doc), false, "moved space");
  assert.equal(await boundToRow({ uuid: u.mirror_uuid, space: u.space_id }, { ...doc, id: "DEMO-0039" }), false);
  assert.equal(await boundToRow({ uuid: u.mirror_uuid, space: u.space_id }, { ...doc, gen: 2 }), false);
  assert.equal(await boundToRow({ uuid: u.mirror_uuid, space: u.space_id }, { id: u.key }), false, "no gen: not proven");
});

// ---- final review I7: messages to the human, and a ticket request sealed under MK for its space
import { openMessage, sealTicketRequest } from "../../fileshare/static/js/mirror-crypto.js";

test("openMessage opens a message to the human under MK (msg vector)", async () => {
  const m = byName.msg;
  const mk = await c.importAesKey(hex(VEC.mk));
  assert.deepEqual(await openMessage(mk, { uuid: m.msg_uuid, enc_body: m.env }), m.obj);
  await assert.rejects(openMessage(mk, { uuid: "0".repeat(32), enc_body: m.env }));
});

test("sealTicketRequest seals under MK with the space as scope (decision-space vector)", async () => {
  const ds = byName["decision-space"];
  const mk = await c.importAesKey(hex(VEC.mk));
  assert.equal(await sealTicketRequest(mk, ds.obj.space, ds.decision_uuid, ds.obj, { nonce: hex(ds.nonce) }), ds.env);
});
