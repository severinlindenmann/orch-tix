import { test } from "node:test";
import assert from "node:assert/strict";
import * as at from "../../fileshare/static/js/autotranscribe.js";
import * as ts from "../../fileshare/static/js/transcribe-settings.js";
import * as c from "../../fileshare/static/js/crypto.js";
import { TranscribeError } from "../../fileshare/static/js/deepgram.js";

const MK = Uint8Array.from({ length: 32 }, (_, i) => i + 3);
const KEY = "dg-test-key-0123456789abcdef";

// A fake server holding one file per id; the meta can carry keys the UI doesn't know.
async function fakeServer(files) {
  const db = new Map();
  let n = 0;
  for (const { id, mime, extra = {} } of files) {
    const fc = await c.newFileCrypto(MK, { name: `${id}.bin`, mime, note: "" });
    const meta = { name: `${id}.bin`, mime, note: "", ...extra };
    const enc = await c.sealFileMeta(fc.dek, fc.uuid, meta);
    db.set(id, { id, n: ++n, uuid: fc.uuidHex, wrapped_dek: fc.wrappedDek, enc_meta: enc, dek: fc.dek });
  }
  const patches = [];
  return {
    db,
    patches,
    getFile: async (id) => {
      const f = db.get(id);
      if (!f) throw new Error("unknown");
      return { ...f };
    },
    patchMeta: async (id, enc) => {
      patches.push({ id, enc });
      db.get(id).enc_meta = enc;
    },
  };
}

function setup(server, { settings = { deepgram_api_key: KEY }, transcribe, ...rest } = {}) {
  const statuses = [];
  const added = [];
  const tooLong = [];
  const calls = [];
  const t = at.createTranscriber({
    getSettings: async () => at.transcriptionSettings(settings),
    getKeys: async () => ({ mk: MK }),
    getFile: server.getFile,
    patchMeta: server.patchMeta,
    transcribe: transcribe ?? (async (body, opts) => {
      calls.push({ body, opts });
      return { text: "Hallo zusammen", language: opts.language, model: "nova-3" };
    }),
    sessionName: async () => "Chrome on Mac",
    now: () => "2026-09-25T12:00:00Z",
    status: (id, text, kind) => statuses.push({ id, text, kind }),
    onAdded: (job, tr) => added.push({ id: job.id, tr }),
    onTooLong: (job, tr) => tooLong.push({ id: job.id, tr }),
    ...rest,
  });
  return { t, statuses, added, tooLong, calls };
}

const job = (id, over = {}) => ({ id, manual: false, audio: async () => ({ body: new Uint8Array([1, 2, 3]), owned: true }), ...over });

async function metaOf(server, id) {
  const f = server.db.get(id);
  return c.openMetaObject(f.dek, c.hexToBytes(f.uuid), f.enc_meta);
}

test("transcriptionSettings: defaults de and on; only the three languages", () => {
  assert.equal(at.transcriptionSettings, ts.transcriptionSettings, "autotranscribe re-exports the shared helper");
  assert.deepEqual(ts.transcriptionSettings(null), { key: null, language: "de", auto: true });
  assert.deepEqual(ts.transcriptionSettings({}), { key: null, language: "de", auto: true });
  assert.deepEqual(ts.transcriptionSettings({ deepgram_api_key: KEY, transcribe_language: "de-CH", auto_transcribe: false }),
    { key: KEY, language: "de-CH", auto: false });
  assert.equal(ts.transcriptionSettings({ transcribe_language: "fr" }).language, "de");
  assert.equal(ts.transcriptionSettings({ transcribe_language: "en" }).language, "en");
  assert.equal(ts.transcriptionSettings({ deepgram_api_key: "" }).key, null);
  assert.equal(ts.transcriptionSettings({ deepgram_api_key: 5 }).key, null);
});

test("auto_transcribe fails closed: missing or true is on, every other value is off", () => {
  assert.equal(ts.transcriptionSettings({}).auto, true);
  assert.equal(ts.transcriptionSettings({ auto_transcribe: true }).auto, true);
  for (const v of [false, "no", "yes", "true", 1, 0, null, [], {}]) {
    assert.equal(ts.transcriptionSettings({ auto_transcribe: v }).auto, false, JSON.stringify(v));
  }
});

test("settings that couldn't be loaded: automatic runs skip silently, a manual run says so", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, { getSettings: async () => ({ ...ts.transcriptionSettings(null), unavailable: true }) });
  assert.deepEqual(await s.t.enqueue(job("FILE1")), { outcome: "skipped", reason: "settings_unavailable" });
  assert.deepEqual(s.statuses, []);
  assert.deepEqual(await s.t.enqueue(job("FILE1", { manual: true })), { outcome: "skipped", reason: "settings_unavailable" });
  assert.equal(s.statuses[0].text, "Couldn't load settings — try again");
  assert.equal(s.calls.length, 0);
});

test("isAudio", () => {
  for (const m of ["audio/webm", "audio/mp4", "audio/webm;codecs=opus", "AUDIO/OGG"]) assert.ok(at.isAudio(m), m);
  for (const m of ["video/webm", "text/plain", "", null, undefined, "application/audio"]) assert.ok(!at.isAudio(m), String(m));
});

test("an added transcript keeps every other meta key and is sealed with the file's DEK", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm", extra: { x_future: { a: [1] }, tags_hint: "keep" } }]);
  const s = setup(server, { settings: { deepgram_api_key: KEY, transcribe_language: "en" } });
  const r = await s.t.enqueue(job("FILE1"));
  assert.equal(r.outcome, "added");
  assert.equal(s.calls.length, 1);
  assert.deepEqual(s.calls[0].opts, { key: KEY, mime: "audio/webm", language: "en" });
  assert.equal(server.patches.length, 1);
  const meta = await metaOf(server, "FILE1");
  assert.deepEqual(meta, {
    name: "FILE1.bin", mime: "audio/webm", note: "", x_future: { a: [1] }, tags_hint: "keep",
    transcript: { text: "Hallo zusammen", language: "en", model: "nova-3", created_at: "2026-09-25T12:00:00Z", by: "Chrome on Mac" },
  });
  assert.deepEqual(s.statuses.map((x) => x.text), ["Transcribing…", "Transcript added"]);
  assert.equal(s.added.length, 1);
});

test("owned plaintext is zeroed after use; borrowed bytes are left alone", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }, { id: "FILE2", mime: "audio/webm" }]);
  const s = setup(server);
  const owned = new Uint8Array([7, 7, 7]);
  const borrowed = new Uint8Array([9, 9, 9]);
  await s.t.enqueue(job("FILE1", { audio: async () => ({ body: owned, owned: true }) }));
  await s.t.enqueue(job("FILE2", { audio: async () => ({ body: borrowed, owned: false }) }));
  assert.deepEqual([...owned], [0, 0, 0]);
  assert.deepEqual([...borrowed], [9, 9, 9]);
});

test("owned plaintext is zeroed even when Deepgram fails", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, { transcribe: async () => { throw new TranscribeError("Deepgram rejected the API key"); } });
  const owned = new Uint8Array([7, 7]);
  const r = await s.t.enqueue(job("FILE1", { audio: async () => ({ body: owned, owned: true }) }));
  assert.deepEqual([...owned], [0, 0]);
  assert.deepEqual(r, { outcome: "failed", reason: "Deepgram rejected the API key" });
  assert.equal(s.statuses.at(-1).text, "Couldn't transcribe: Deepgram rejected the API key");
  assert.equal(s.statuses.at(-1).kind, "error");
  assert.equal(server.patches.length, 0);
});

test("auto off: nothing is sent; a manual run still goes", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, { settings: { deepgram_api_key: KEY, auto_transcribe: false } });
  let asked = 0;
  const r = await s.t.enqueue(job("FILE1", { audio: async () => { asked += 1; return { body: new Uint8Array(1), owned: true }; } }));
  assert.deepEqual(r, { outcome: "skipped", reason: "auto_off" });
  assert.equal(asked, 0, "the audio isn't even decrypted");
  assert.equal(s.calls.length, 0);
  assert.deepEqual(s.statuses, []);
  assert.equal((await s.t.enqueue(job("FILE1", { manual: true }))).outcome, "added");
});

test("no key: auto is silent, manual says so; nothing is sent", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, { settings: {} });
  assert.equal((await s.t.enqueue(job("FILE1"))).reason, "no_key");
  assert.deepEqual(s.statuses, []);
  assert.equal((await s.t.enqueue(job("FILE1", { manual: true }))).reason, "no_key");
  assert.match(s.statuses[0].text, /^Couldn't transcribe: set a Deepgram key in Settings$/);
  assert.equal(s.calls.length, 0);
});

test("an empty transcript says 'No speech detected' and stores nothing", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, { transcribe: async () => ({ text: "", language: "de", model: "nova-3" }) });
  assert.equal((await s.t.enqueue(job("FILE1"))).outcome, "empty");
  assert.equal(server.patches.length, 0);
  assert.equal(s.statuses.at(-1).text, "No speech detected");
  assert.equal(s.added.length, 0);
});

test("a transcript too long for the meta cap is not stored and is handed to onTooLong", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const text = "x".repeat(400_000);
  const s = setup(server, { transcribe: async () => ({ text, language: "de", model: "nova-3" }) });
  const r = await s.t.enqueue(job("FILE1"));
  assert.equal(r.outcome, "too_long");
  assert.equal(server.patches.length, 0);
  assert.equal(s.tooLong.length, 1);
  assert.equal(s.tooLong[0].tr.text, text);
  assert.equal(s.statuses.at(-1).text, at.STATUS.tooLong);
});

test("non-audio files and files that already have a transcript are skipped", async () => {
  const t0 = { text: "old", language: "de", model: "nova-3", created_at: "2026-09-24T00:00:00Z", by: "x" };
  const server = await fakeServer([{ id: "FILE1", mime: "text/plain" }, { id: "FILE2", mime: "audio/webm", extra: { transcript: t0 } }]);
  const s = setup(server);
  assert.equal((await s.t.enqueue(job("FILE1"))).reason, "not_audio");
  assert.equal((await s.t.enqueue(job("FILE2"))).outcome, "exists");
  assert.equal(s.calls.length, 0);
  assert.equal(server.patches.length, 0);
});

test("the meta is re-read before sealing, so a rename made meanwhile survives", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const s = setup(server, {
    transcribe: async (body, opts) => {
      const f = server.db.get("FILE1");
      f.enc_meta = await c.sealFileMeta(f.dek, c.hexToBytes(f.uuid), { name: "renamed.webm", mime: "audio/webm", note: "n" });
      return { text: "hi", language: opts.language, model: "nova-3" };
    },
  });
  await s.t.enqueue(job("FILE1"));
  const meta = await metaOf(server, "FILE1");
  assert.equal(meta.name, "renamed.webm");
  assert.equal(meta.note, "n");
  assert.equal(meta.transcript.text, "hi");
});

test("one transcription at a time, in order", async () => {
  const server = await fakeServer([1, 2, 3, 4].map((i) => ({ id: `FILE${i}`, mime: "audio/webm" })));
  let active = 0;
  let peak = 0;
  const order = [];
  const s = setup(server, {
    transcribe: async (body, opts) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 5));
      active -= 1;
      return { text: "t", language: opts.language, model: "nova-3" };
    },
    onAdded: (j) => order.push(j.id),
  });
  const all = [1, 2, 3, 4].map((i) => s.t.enqueue(job(`FILE${i}`)));
  assert.ok(s.t.busy());
  await Promise.all(all);
  assert.equal(peak, 1);
  assert.deepEqual(order, ["FILE1", "FILE2", "FILE3", "FILE4"]);
  assert.ok(!s.t.busy());
});

test("a failing job doesn't stall the queue, and server errors map to short reasons", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }, { id: "FILE2", mime: "audio/webm" }]);
  const { ApiError } = await import("../../fileshare/static/js/api.js");
  let first = true;
  const s = setup(server, {
    patchMeta: async (id, enc) => {
      if (first) { first = false; throw new ApiError(400, "bad_meta", "x"); }
      return server.patchMeta(id, enc);
    },
  });
  const [a, b] = await Promise.all([s.t.enqueue(job("FILE1")), s.t.enqueue(job("FILE2"))]);
  assert.deepEqual(a, { outcome: "failed", reason: "couldn't save it (bad_meta)" });
  assert.equal(b.outcome, "added");
});

test("an unexpected error is reported generically and logged without details", async () => {
  const server = await fakeServer([{ id: "FILE1", mime: "audio/webm" }]);
  const logged = [];
  const s = setup(server, { transcribe: async () => { throw new Error(`boom ${KEY}`); }, log: (e) => logged.push(e) });
  const r = await s.t.enqueue(job("FILE1"));
  assert.deepEqual(r, { outcome: "failed", reason: "something went wrong" });
  assert.ok(!s.statuses.some((x) => x.text.includes(KEY)));
  assert.equal(logged.length, 1);
});
