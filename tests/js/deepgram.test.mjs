import { test } from "node:test";
import assert from "node:assert/strict";
import * as dg from "../../fileshare/static/js/deepgram.js";

const KEY = "dg-test-key-0123456789abcdef";
const ok = (transcript, extra = {}) => ({
  status: 200,
  ok: true,
  json: async () => ({ metadata: {}, results: { channels: [{ alternatives: [{ transcript, confidence: 0.9 }] }] }, ...extra }),
});
const status = (s, body = { err_msg: `secret body ${KEY}` }) => ({ status: s, ok: s >= 200 && s < 300, json: async () => body });

function fakeFetch(response) {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init });
    if (typeof response === "function") return response(url, init);
    return response;
  };
  f.calls = calls;
  return f;
}

const run = (fetch, opts = {}) => dg.transcribeAudio(new Uint8Array([1, 2, 3]), {
  key: KEY, mime: "audio/webm", language: "de", fetch, ...opts,
});

test("the request: URL, method, headers, body and the fetch options", async () => {
  const f = fakeFetch(ok("Hallo Welt"));
  const body = new Uint8Array([9, 8, 7]);
  const out = await dg.transcribeAudio(body, { key: KEY, mime: "audio/mp4", language: "de-CH", fetch: f });
  assert.equal(f.calls.length, 1);
  const { url, init } = f.calls[0];
  assert.equal(url, "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&language=de-CH");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.headers, { Authorization: `Token ${KEY}`, "Content-Type": "audio/mp4" });
  assert.equal(init.body, body);
  assert.equal(init.credentials, "omit");
  assert.equal(init.redirect, "error");
  assert.equal(init.referrerPolicy, "no-referrer");
  assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual(out, { text: "Hallo Welt", language: "de-CH", model: "nova-3" });
});

test("every allowed language maps to its URL; anything else throws before fetching", async () => {
  assert.deepEqual(dg.LANGUAGES, ["de", "de-CH", "en"]);
  for (const lang of dg.LANGUAGES) {
    assert.equal(dg.listenUrl(lang), `https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&language=${lang}`);
  }
  for (const bad of ["fr", "DE", "de-ch", "", null, undefined, "de&detect_language=true", "en ", 1]) {
    assert.throws(() => dg.listenUrl(bad), dg.TranscribeError);
    const f = fakeFetch(ok("x"));
    await assert.rejects(run(f, { language: bad }), dg.TranscribeError);
    assert.equal(f.calls.length, 0, `no request for language ${String(bad)}`);
  }
});

test("a missing or malformed key throws before fetching", async () => {
  for (const key of ["", null, undefined, "short", "has space in the middle of it", "x".repeat(257), "line\nbreak-0123456789"]) {
    const f = fakeFetch(ok("x"));
    await assert.rejects(run(f, { key }), (e) => e instanceof dg.TranscribeError && !String(e.message).includes(String(key || "@@")));
    assert.equal(f.calls.length, 0);
  }
});

test("a missing mime falls back to a generic type", async () => {
  const f = fakeFetch(ok("x"));
  await run(f, { mime: "" });
  assert.equal(f.calls[0].init.headers["Content-Type"], "application/octet-stream");
});

test("HTTP errors map to short reasons", async () => {
  const cases = [
    [401, "Deepgram rejected the API key"],
    [403, "Deepgram rejected the API key"],
    [402, "Deepgram quota or rate limit"],
    [429, "Deepgram quota or rate limit"],
    [400, "Deepgram couldn't read this audio"],
    [500, "Deepgram unavailable"],
    [502, "Deepgram unavailable"],
    [503, "Deepgram unavailable"],
    [404, "Deepgram refused the request (HTTP 404)"],
    [301, "Deepgram refused the request (HTTP 301)"],
  ];
  for (const [s, reason] of cases) {
    await assert.rejects(run(fakeFetch(status(s))), (e) => {
      assert.ok(e instanceof dg.TranscribeError);
      assert.equal(e.reason, reason);
      assert.equal(e.message, reason);
      return true;
    });
  }
});

test("a network error, a redirect refused by fetch and a timeout are 'Deepgram unavailable'", async () => {
  const net = fakeFetch(() => { throw new TypeError(`Failed to fetch ${KEY}`); });
  await assert.rejects(run(net), { reason: "Deepgram unavailable" });

  const hang = (url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
  const t0 = Date.now();
  await assert.rejects(run(fakeFetch(hang), { timeoutMs: 30 }), { reason: "Deepgram unavailable" });
  assert.ok(Date.now() - t0 < 2000);
  assert.equal(dg.TIMEOUT_MS, 120_000);
});

test("the response shape is validated", async () => {
  const bad = [
    null, [], "x", {}, { results: null }, { results: {} }, { results: { channels: [] } },
    { results: { channels: {} } }, { results: { channels: [null] } }, { results: { channels: [{}] } },
    { results: { channels: [{ alternatives: [] }] } }, { results: { channels: [{ alternatives: [{}] }] } },
    { results: { channels: [{ alternatives: [{ transcript: 5 }] }] } },
    { results: { channels: [{ alternatives: [{ transcript: null }] }] } },
  ];
  for (const body of bad) {
    await assert.rejects(run(fakeFetch(status(200, body))), { reason: "Deepgram sent an unexpected response" });
  }
  const notJson = fakeFetch({ status: 200, ok: true, json: async () => { throw new SyntaxError(`bad ${KEY}`); } });
  await assert.rejects(run(notJson), { reason: "Deepgram sent an unexpected response" });
});

test("an empty transcript comes back as empty text", async () => {
  assert.equal((await run(fakeFetch(ok("")))).text, "");
  assert.equal((await run(fakeFetch(ok("  \n‏ ")))).text, "");
});

test("control and bidi characters are stripped; newlines and tabs stay", () => {
  const dirty = "a\u0000b\u0007c\u001bd\u007fe\u0085f\u009fg‮h⁦i⁩j‎k‏l؜m\n\tn\r\no\rp";
  assert.equal(dg.cleanTranscript(dirty), "abcdefghijklm\n\tn\no\np");
  assert.equal(dg.cleanTranscript("  Grüße ✓ 😀  "), "Grüße ✓ 😀");
});

test("the transcript is capped at the §14 G limit without splitting a surrogate pair", () => {
  const long = "a".repeat(dg.TRANSCRIPT_CAP + 10);
  assert.equal(dg.cleanTranscript(long).length, dg.TRANSCRIPT_CAP);
  assert.equal(dg.TRANSCRIPT_CAP, 1_000_000);
  const pair = "a".repeat(dg.TRANSCRIPT_CAP - 1) + "😀";
  const capped = dg.cleanTranscript(pair);
  assert.ok(capped.length <= dg.TRANSCRIPT_CAP);
  assert.ok(!/[\ud800-\udbff]$/.test(capped));
});

test("the key never appears in any thrown message or error property", async () => {
  const responders = [
    status(401), status(403), status(429), status(400), status(500), status(404),
    status(200, { key: KEY }),
    () => { throw new TypeError(`Failed ${KEY}`); },
    { status: 200, ok: true, json: async () => { throw new Error(KEY); } },
  ];
  for (const r of responders) {
    try {
      await run(fakeFetch(r));
      assert.fail("should throw");
    } catch (e) {
      const all = [e.message, e.reason, e.stack, String(e), JSON.stringify(e), String(e.cause ?? "")].join("\n");
      assert.ok(!all.includes(KEY), `key leaked: ${all}`);
      assert.ok(!all.includes("secret body"), `response body leaked: ${all}`);
    }
  }
});

test("nothing is logged to the console", async () => {
  const seen = [];
  const orig = { log: console.log, error: console.error, warn: console.warn, info: console.info, debug: console.debug };
  for (const k of Object.keys(orig)) console[k] = (...a) => seen.push(a.join(" "));
  try {
    await run(fakeFetch(ok("x")));
    await run(fakeFetch(status(401))).catch(() => {});
    await run(fakeFetch(() => { throw new TypeError("x"); })).catch(() => {});
  } finally {
    Object.assign(console, orig);
  }
  assert.deepEqual(seen, []);
});
