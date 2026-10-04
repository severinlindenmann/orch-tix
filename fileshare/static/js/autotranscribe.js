// fileshare/static/js/autotranscribe.js: auto-transcription in the browser and PWA (spec §20).
//
// After an audio upload succeeds (a direct upload, or an outbox item once it is sent), and when a
// Deepgram key is set and auto_transcribe is on, the audio is sent to Deepgram, the transcript is
// added to the file's decrypted meta (every other key kept), the meta is re-sealed with the file's
// DEK under a fresh nonce, and PATCH /api/files/{ref}/meta stores it. The file view's Transcribe
// button runs the same pipeline on demand.
//
// - One transcription at a time; the rest wait in a FIFO. Nothing here blocks the upload UI.
// - Plaintext audio lives only in memory for the one job; decrypted bytes are zeroed afterwards.
//   Nothing is written to IndexedDB or the outbox, and the key is never logged or stored.
// - The settings are read once per page load, and again after a Settings save (BroadcastChannel).
import { api, ApiError } from "./api.js";
import { decryptBlob, openFileMetaFull, openSettings, sealFileMeta } from "./crypto.js";
import { TranscribeError, transcribeAudio } from "./deepgram.js";
import { LANGUAGE_LABELS, SETTINGS_CHANNEL, transcriptionSettings } from "./transcribe-settings.js";
import { loadKeys } from "./keystore.js";
import { el, toast } from "./ui.js";
import { loadSessionName } from "./browsersession.js";
import { fetchPlaintext, transcriptSection } from "./preview.js";

export { transcriptionSettings };
// The server's cap on enc_meta, in base64url characters (routes/files.py MAX_ENC_META_B64).
export const META_CAP_B64 = 524288;

export const STATUS = Object.freeze({
  running: "Transcribing…",
  added: "Transcript added",
  empty: "No speech detected",
  tooLong: "Transcript too long to store — shown once in the file view",
  failed: (reason) => `Couldn't transcribe: ${reason}`,
  unavailable: "Couldn't load settings — try again",
});

export const isAudio = (mime) => typeof mime === "string" && /^audio\//i.test(mime);

export const withTranscript = (raw, transcript) => ({ ...raw, transcript });

const isoNow = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

function wipe(bytes) {
  if (bytes instanceof Uint8Array) bytes.fill(0);
}

// The pipeline, with every outside dependency injected (the tests pass fakes).
// A job is {id, manual, audio({dek, uuid}) -> Promise<{body, owned}>}: `owned` bytes are zeroed after.
// Resolves {outcome: "added"|"empty"|"too_long"|"skipped"|"exists"|"failed", transcript?, reason?}.
export function createTranscriber({
  getSettings, getKeys, getFile, patchMeta, transcribe = transcribeAudio, sessionName = async () => "browser",
  now = isoNow, status = () => {}, onAdded = () => {}, onTooLong = () => {}, log = () => {},
}) {
  let tail = Promise.resolve();
  let pending = 0; // queued plus running

  async function run(job) {
    const settings = await getSettings();
    if (settings.unavailable) {
      // A failed read is not "no key": automatic runs skip, a manual one can simply be retried.
      if (job.manual) status(job.id, STATUS.unavailable, "error");
      return { outcome: "skipped", reason: "settings_unavailable" };
    }
    if (!settings.key) {
      if (job.manual) status(job.id, STATUS.failed("set a Deepgram key in Settings"), "error");
      return { outcome: "skipped", reason: "no_key" };
    }
    if (!job.manual && !settings.auto) return { outcome: "skipped", reason: "auto_off" };
    const keys = await getKeys();
    if (!keys) return { outcome: "skipped", reason: "signed_out" };
    let opened = await openFileMetaFull(keys.mk, job.record?.enc_meta ? job.record : await getFile(job.id));
    if (!isAudio(opened.raw.mime)) return { outcome: "skipped", reason: "not_audio" };
    if (opened.meta.transcript) {
      onAdded(job, opened.meta.transcript);
      return { outcome: "exists", transcript: opened.meta.transcript };
    }
    status(job.id, STATUS.running);
    let audio = await job.audio({ dek: opened.dek, uuid: opened.uuid });
    let result;
    try {
      result = await transcribe(audio.body, { key: settings.key, mime: opened.raw.mime, language: settings.language });
    } finally {
      if (audio.owned) wipe(audio.body);
      audio = null; // release the plaintext as soon as Deepgram has it
    }
    if (!result.text) {
      status(job.id, STATUS.empty);
      return { outcome: "empty" };
    }
    const transcript = {
      text: result.text, language: result.language, model: result.model, created_at: now(),
      by: (await sessionName().catch(() => "")) || "browser",
    };
    // Re-read right before sealing, so a rename or tag change made meanwhile isn't overwritten.
    opened = await openFileMetaFull(keys.mk, await getFile(job.id));
    if (opened.meta.transcript) {
      onAdded(job, opened.meta.transcript);
      return { outcome: "exists", transcript: opened.meta.transcript };
    }
    const enc = await sealFileMeta(opened.dek, opened.uuid, withTranscript(opened.raw, transcript));
    if (enc.length > META_CAP_B64) {
      status(job.id, STATUS.tooLong);
      onTooLong(job, transcript);
      return { outcome: "too_long", transcript };
    }
    await patchMeta(job.id, enc);
    status(job.id, STATUS.added, "ok");
    onAdded(job, transcript);
    return { outcome: "added", transcript };
  }

  async function guarded(job) {
    try {
      return await run(job);
    } catch (e) {
      let reason = "something went wrong";
      if (e instanceof TranscribeError) reason = e.reason;
      else if (e instanceof ApiError && e.status === 401) reason = "session expired";
      else if (e instanceof ApiError && (e.status === 404 || e.status === 410)) reason = "the file is gone";
      else if (e instanceof ApiError && e.status === 0) reason = "couldn't reach the server";
      else if (e instanceof ApiError) reason = `couldn't save it (${e.code})`;
      else if (e?.name === "IntegrityError") reason = "couldn't decrypt the file";
      else log(e);
      status(job.id, STATUS.failed(reason), "error");
      return { outcome: "failed", reason };
    } finally {
      pending -= 1;
    }
  }

  // Queues a job behind the running one; resolves with its result (never rejects).
  function enqueue(job) {
    pending += 1;
    const p = tail.then(() => guarded(job));
    tail = p.then(() => {}, () => {});
    return p;
  }

  return { enqueue, busy: () => pending > 0, idle: () => tail };
}

// ------------------------------------------------------------------ browser wiring

let settingsPromise = null;

// The transcription settings for this page load: {key, language, auto}. A failure isn't cached.
export function loadTranscriptionSettings() {
  if (!settingsPromise) {
    settingsPromise = (async () => {
      const keys = await loadKeys();
      if (!keys) return transcriptionSettings(null);
      const res = await api("GET", "/api/settings");
      return transcriptionSettings(res.enc_settings ? await openSettings(keys.mk, res.enc_settings) : {});
    })();
    settingsPromise.catch(() => { settingsPromise = null; });
  }
  return settingsPromise.catch(() => ({ ...transcriptionSettings(null), unavailable: true }));
}

export function forgetTranscriptionSettings() {
  settingsPromise = null;
}

let sessionNamePromise = null;
function sessionNameOnce() {
  if (!sessionNamePromise) {
    sessionNamePromise = loadSessionName().then((n) => {
      if (n == null) sessionNamePromise = null;
      return n || "browser";
    });
  }
  return sessionNamePromise;
}

// uuid -> a transcript too long to store, waiting to be shown once in the file view.
export const unstored = new Map();

let transcriber = null;
function pageTranscriber() {
  if (!transcriber) {
    transcriber = createTranscriber({
      getSettings: loadTranscriptionSettings,
      getKeys: loadKeys,
      getFile: (id) => api("GET", `/api/files/${encodeURIComponent(id)}`),
      patchMeta: (id, enc) => api("PATCH", `/api/files/${encodeURIComponent(id)}/meta`, { json: { enc_meta: enc } }),
      sessionName: sessionNameOnce,
      status: (id, text, kind = "info") => toast(`${id} — ${text}`, kind),
      onAdded: (job, transcript) => window.dispatchEvent(new CustomEvent("fs:transcript-added",
        { detail: { n: job.n, id: job.id, uuid: job.uuid, transcript } })),
      onTooLong: (job, transcript) => {
        unstored.set(job.uuid, transcript);
        window.dispatchEvent(new CustomEvent("fs:transcript-unstored", { detail: { n: job.n, id: job.id, uuid: job.uuid } }));
      },
      log: (e) => console.error("transcribing", e?.name || "error"),
    });
  }
  return transcriber;
}

// `record` (a FileOut with wrapped_dek and enc_meta) saves the first GET; the meta is re-read anyway
// right before it is sealed.
const jobOf = (out) => ({ id: out.id, n: out.n, uuid: out.uuid, record: out });

// A direct upload succeeded: `file` is the File the user picked or recorded (still in memory).
export function transcribeAfterUpload(out, file) {
  if (!out?.id || !isAudio(file?.type)) return;
  pageTranscriber().enqueue({ ...jobOf(out), manual: false, audio: async () => ({ body: file, owned: false }) });
}

// A queued item was sent: decrypt its ciphertext locally (the MK unwraps the DEK in the pipeline).
export function transcribeAfterOutbox(out, item) {
  if (!out?.id || !item?.blob) return;
  const blob = item.blob;
  pageTranscriber().enqueue({
    ...jobOf(out), manual: false,
    audio: async ({ dek, uuid }) => ({ body: await decryptBlob(dek, uuid, new Uint8Array(blob)), owned: true }),
  });
}

// The file view's Transcribe button. `bytes()` returns the plaintext the player already holds, if any.
export function transcribeNow(file, bytes) {
  return pageTranscriber().enqueue({
    ...jobOf(file), manual: true,
    audio: async () => {
      const held = bytes?.();
      if (held) return { body: held, owned: false };
      return { body: await fetchPlaintext(file), owned: true };
    },
  });
}

if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
  const ch = new BroadcastChannel(SETTINGS_CHANNEL);
  ch.onmessage = () => forgetTranscriptionSettings();
}

// ------------------------------------------------------------------ the file view (spec §20)

const NOTICE_TOO_LONG = "This transcript is too long to store with the file. It's shown here once and isn't saved.";

// Turns `section` (the "No transcript yet" card from preview.js transcriptSection) into the card for
// `transcript`, in place: fileview.js keeps a reference to the element and may re-insert it.
function fillCard(section, file, transcript, { notice = false } = {}) {
  const card = transcriptSection({ ...file, meta: { ...file.meta, transcript } });
  if (notice) {
    card.classList.add("transcript-unstored");
    card.children[0].after(el("p", { class: "notice transcript-notice", role: "note" }, NOTICE_TOO_LONG));
  }
  section.className = card.className;
  section.replaceChildren(...card.childNodes);
}

// The open view's listeners. They hold the view's plaintext through `bytes`, so the view drops
// them when it closes (detachTranscribe) and a new view replaces them.
let attached = null;

export function detachTranscribe() {
  attached?.abort();
  attached = null;
}

// The Transcribe button for an audio file without a transcript. `bytes()` returns the plaintext the
// player already holds (or null, and the pipeline decrypts the blob itself). A transcript that was
// too long to store is shown here once instead.
export function attachTranscribe(section, { file, bytes }) {
  detachTranscribe();
  if (!section || file.meta.transcript) return;
  const uuid = file.uuid;
  const showUnstored = () => {
    const t = unstored.get(uuid);
    if (!t) return false;
    unstored.delete(uuid);
    detachTranscribe();
    fillCard(section, file, t, { notice: true });
    return true;
  };
  if (showUnstored()) return;

  const button = el("button", { type: "button", class: "btn btn-small transcribe-btn", disabled: true }, "Transcribe");
  const hint = el("span", { class: "transcribe-hint" }, "Checking settings…");
  const line = el("p", { class: "transcribe-status", role: "status", hidden: true });
  const say = (text, error = false) => {
    line.textContent = text;
    line.className = error ? "transcribe-status error" : "transcribe-status";
    line.hidden = !text;
  };
  const row = el("div", { class: "transcribe-row" }, button, hint);
  const none = section.querySelector(".transcript-none");
  if (none) none.after(row, line);
  else section.append(row, line);

  const ac = new AbortController();
  attached = ac;
  const showSettings = (s) => {
    if (ac.signal.aborted) return;
    if (s.unavailable) {
      // Couldn't read them (offline, a server error): the button stays usable, and a press retries.
      button.disabled = false;
      hint.textContent = STATUS.unavailable;
      return;
    }
    if (!s.key) {
      button.disabled = true;
      hint.replaceChildren("Set a Deepgram key in ", el("a", { href: "/settings" }, "Settings"), " to transcribe.");
      return;
    }
    button.disabled = false;
    hint.textContent = `Sends the audio to Deepgram · ${LANGUAGE_LABELS[s.language]}`;
  };
  loadTranscriptionSettings().then(showSettings);
  window.addEventListener("fs:transcript-added", (e) => {
    if (e.detail?.uuid !== uuid) return;
    detachTranscribe();
    fillCard(section, file, e.detail.transcript);
  }, { signal: ac.signal });
  window.addEventListener("fs:transcript-unstored", (e) => {
    if (e.detail?.uuid === uuid) showUnstored();
  }, { signal: ac.signal });

  button.addEventListener("click", async () => {
    button.disabled = true;
    say(STATUS.running);
    const r = await transcribeNow(file, () => (ac.signal.aborted ? null : bytes?.()));
    if (ac.signal.aborted) return;
    if (r.outcome === "empty") say(STATUS.empty);
    else if (r.outcome === "failed") say(STATUS.failed(r.reason), true);
    else if (r.reason === "settings_unavailable") say(STATUS.unavailable, true);
    else if (r.outcome === "skipped") say(STATUS.failed(r.reason === "no_key" ? "set a Deepgram key in Settings" : "not available"), true);
    button.disabled = false;
    loadTranscriptionSettings().then(showSettings); // the hint follows what the retry learned
  });
}
