// fileshare/static/js/recorder.js — "Record audio": record from the microphone, then hand the
// recording to upload.js's uploadFiles, which encrypts it in this browser like any other upload.
// Not an entry module: upload.js imports it and passes uploadFiles and maxUpload in, so node can
// test the pure helpers without loading the crypto and API modules.
import { el, toast } from "./ui.js";
import { stamp } from "./paste.js";
import { keepAwake } from "./wakelock.js";

export const AUDIO_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
export const MAX_RECORDING_MS = 60 * 60 * 1000;
export const SIZE_HEADROOM = 0.95; // stop before the next chunk could push the upload past maxUpload()
const TIMESLICE_MS = 1000;
const TICK_MS = 500;
const LEVEL_MS = 100;
const LEVEL_BARS = 12;

const MSG_DENIED = "Microphone access was denied — allow it in the browser settings";
const MSG_UNSUPPORTED = "Recording isn't supported in this browser";
const MSG_NO_MIC = "No microphone was found";
const MSG_FAILED = "Couldn't start the microphone";
const MSG_UPLOAD_FAILED = "Upload failed — your recording is kept. Retry when you're back online.";
const MSG_SESSION = "Your session expired — the recording is kept here. Log in in a new tab, then press Retry.";

export function pickAudioType(isTypeSupported) {
  if (typeof isTypeSupported !== "function") return "";
  for (const type of AUDIO_TYPES) {
    try {
      if (isTypeSupported(type)) return type;
    } catch {
      return "";
    }
  }
  return "";
}

export function extForAudioType(type) {
  const t = String(type || "").toLowerCase();
  if (t.includes("mp4")) return ".m4a";
  if (t.includes("ogg")) return ".ogg";
  return ".webm";
}

export function recordingName(date, ext) {
  return `recording-${stamp(date)}${ext}`;
}

export function formatElapsed(ms) {
  const s = Math.floor(Math.max(0, ms) / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

// The level meter (opt-in, `level: true`): the RMS of 8-bit time-domain samples (128 is silence),
// in [0, 1], and how many of `n` bars it lights (square-rooted, so speech shows well).
export function levelOf(samples) {
  if (!samples || !samples.length) return 0;
  let sum = 0;
  for (const v of samples) {
    const x = (v - 128) / 128;
    sum += x * x;
  }
  return Math.min(1, Math.sqrt(sum / samples.length));
}

export function litBars(level, n) {
  return Math.max(0, Math.min(n, Math.round(Math.sqrt(Math.max(0, level)) * n * 1.4)));
}

function errorText(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") return MSG_DENIED;
  if (name === "NotFoundError" || name === "OverconstrainedError") return MSG_NO_MIC;
  if (name === "NotSupportedError") return MSG_UNSUPPORTED;
  return MSG_FAILED;
}

let active = null;

// upload(files, note, {ms}) -> Promise<FileOut[]> (`ms`: how long the recording is); maxBytes() ->
// the server's ciphertext limit. Speak a ticket (spec T13) words the sheet its own way: `title`,
// `stopLabel`, `className`, and `level` for a level meter under the timer.
export function openRecorder({ upload, maxBytes, title = "Record audio", stopLabel = "Stop & upload", className = "", level = false }) {
  if (active) return active.done;
  const bars = level ? Array.from({ length: LEVEL_BARS }, () => el("span", { class: "recorder-bar" })) : [];
  const meter = level ? el("div", { class: "recorder-level", "aria-hidden": "true" }, bars) : null;
  const timer = el("div", { class: "recorder-timer mono", "aria-hidden": "true" }, "00:00");
  const status = el("p", { class: "recorder-status", role: "status" }, "Starting the microphone…");
  const error = el("p", { class: "recorder-error", role: "alert", hidden: "" });
  // Logging in in another tab re-saves the keys to the same IndexedDB and shares the new session
  // cookie, so Retry here works afterwards; navigating this tab away would lose the recording.
  const login = el("a", { class: "recorder-login", href: "/login", target: "_blank", rel: "noopener", hidden: "" },
    "Open login in a new tab");
  const cancel = el("button", { class: "btn", type: "button" }, "Cancel");
  const stop = el("button", { class: "btn btn-accent", type: "button", disabled: "" }, stopLabel);
  const retry = el("button", { class: "btn btn-accent", type: "button", hidden: "" }, "Retry");
  const dlg = el(
    "dialog",
    { class: className ? `recorder ${className}` : "recorder", "aria-labelledby": "recorder-title" },
    el("h2", { id: "recorder-title" }, title),
    timer,
    meter,
    status,
    error,
    login,
    el("div", { class: "sheet-actions" }, cancel, stop, retry),
    el("p", { class: "hint" }, "Encrypted in this browser before it leaves the device."),
  );

  // phase: starting -> recording -> uploading -> closed; uploading <-> upload-failed (Retry) until
  // success or Discard; starting/recording -> closed on cancel; any -> failed on a mic error.
  // s.file holds the finished recording only between Stop and a successful upload or Discard.
  const s = { phase: "starting", stream: null, rec: null, chunks: [], bytes: 0, tick: null, t0: 0, ms: 0, type: "", file: null,
    audio: null, levelTick: null, awake: null };
  let resolveDone;
  const done = new Promise((r) => { resolveDone = r; });
  active = { done };

  // While a recording exists only in this tab, leaving the page asks first.
  const onBeforeUnload = (e) => {
    e.preventDefault();
    e.returnValue = "";
  };
  const stopLevel = () => {
    clearInterval(s.levelTick);
    s.levelTick = null;
    if (s.audio) s.audio.close().catch(() => {});
    s.audio = null;
    for (const b of bars) b.classList.remove("on");
  };
  // Best effort: without Web Audio the meter just stays dark.
  const startLevel = (stream) => {
    if (!meter) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      s.audio = new Ctx();
      // iOS starts it suspended (the mic prompt broke the tap's activation): resume, or the meter
      // stays dark. A refusal is harmless, the recording itself doesn't use this context.
      s.audio.resume().catch(() => {});
      const analyser = s.audio.createAnalyser();
      analyser.fftSize = 512;
      s.audio.createMediaStreamSource(stream).connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      s.levelTick = setInterval(() => {
        analyser.getByteTimeDomainData(buf);
        const lit = litBars(levelOf(buf), bars.length);
        bars.forEach((b, i) => b.classList.toggle("on", i < lit));
      }, LEVEL_MS);
    } catch (err) {
      console.error("recorder level meter", err);
      stopLevel();
    }
  };
  // The screen stays on while recording and uploading: a phone that locks suspends the page, and
  // with it the microphone or the upload. It may sleep again once the dialog waits on the user.
  const holdAwake = () => {
    if (!s.awake) s.awake = keepAwake();
  };
  const letSleep = () => {
    if (s.awake) s.awake.release();
    s.awake = null;
  };
  const stopTracks = () => {
    stopLevel();
    if (s.stream) s.stream.getTracks().forEach((t) => t.stop());
    s.stream = null;
  };
  // Stops the timer, the recorder and every track (the mic indicator goes off) and drops the audio.
  const release = () => {
    clearInterval(s.tick);
    dlg.classList.remove("is-recording");
    if (s.rec) {
      s.rec.ondataavailable = null;
      s.rec.onerror = null;
      if (s.rec.state !== "inactive") {
        try { s.rec.stop(); } catch { /* already stopped */ }
      }
    }
    s.rec = null;
    s.chunks = [];
    stopTracks();
  };
  const close = () => {
    if (s.phase === "closed") return;
    s.phase = "closed";
    release();
    letSleep();
    s.file = null;
    window.removeEventListener("beforeunload", onBeforeUnload);
    window.removeEventListener("pagehide", close);
    dlg.close();
    dlg.remove();
    active = null;
    resolveDone();
  };
  const fail = (text) => {
    release();
    letSleep();
    s.phase = "failed";
    status.hidden = true;
    error.textContent = text;
    error.hidden = false;
    stop.disabled = true;
    cancel.disabled = false;
    cancel.textContent = "Close";
  };

  async function finish(reason) {
    if (s.phase !== "recording") return;
    s.phase = "uploading";
    s.ms = Date.now() - s.t0;
    clearInterval(s.tick);
    stopLevel();
    dlg.classList.remove("is-recording");
    stop.disabled = true;
    cancel.disabled = true;
    if (reason) toast(reason);
    status.textContent = "Encrypting and uploading…";
    const rec = s.rec;
    // The final dataavailable fires before stop, so every chunk is in once this resolves.
    await new Promise((resolve) => {
      if (rec.state === "inactive") {
        resolve();
        return;
      }
      rec.addEventListener("stop", resolve, { once: true });
      rec.stop();
    });
    rec.ondataavailable = null;
    rec.onerror = null;
    stopTracks();
    if (s.phase !== "uploading") return; // closed (pagehide) or failed while the recorder stopped
    const recorded = rec.mimeType || s.type;
    const type = (recorded || "audio/webm").split(";")[0].trim();
    s.file = new File(s.chunks, recordingName(new Date(), extForAudioType(recorded)), { type });
    s.chunks = [];
    s.rec = null;
    window.addEventListener("beforeunload", onBeforeUnload); // removed by close(): success or Discard
    await attemptUpload();
  }

  // uploadFiles never throws: it returns [] after its own error toast, or silently on a network
  // error or 401 (the banner explains). Empty means failed, and the recording stays in memory.
  // A 401, and keys cleared by the session banner, both surface as fs:unauthenticated.
  async function attemptUpload() {
    s.phase = "uploading";
    holdAwake();
    stop.disabled = true;
    retry.disabled = true;
    cancel.disabled = true;
    error.hidden = true;
    login.hidden = true;
    status.hidden = false;
    status.textContent = "Encrypting and uploading…";
    let out = [];
    let unauthenticated = false;
    const onUnauth = () => { unauthenticated = true; };
    window.addEventListener("fs:unauthenticated", onUnauth);
    try {
      out = await upload([s.file], "", { ms: s.ms }); // toasts "Uploaded FILE<n> · name" and dispatches fs:files-changed
    } catch (err) {
      console.error("uploading the recording", err);
    } finally {
      window.removeEventListener("fs:unauthenticated", onUnauth);
    }
    if (s.phase !== "uploading") return; // the page went away meanwhile
    if (out && out.length) {
      close();
      return;
    }
    s.phase = "upload-failed";
    letSleep();
    status.hidden = true;
    error.textContent = unauthenticated ? MSG_SESSION : MSG_UPLOAD_FAILED;
    error.hidden = false;
    login.hidden = !unauthenticated;
    stop.hidden = true;
    retry.hidden = false;
    retry.disabled = false;
    cancel.textContent = "Discard";
    cancel.disabled = false;
    retry.focus();
  }

  cancel.addEventListener("click", close);
  stop.addEventListener("click", () => finish());
  retry.addEventListener("click", () => {
    if (s.phase === "upload-failed") attemptUpload();
  });
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    // A kept recording is dropped only by an explicit Discard, never by a stray Escape.
    if (s.phase !== "uploading" && s.phase !== "upload-failed") close();
  });
  // However the dialog or the page goes away, the mic and the timer must not outlive it.
  dlg.addEventListener("close", close);
  window.addEventListener("pagehide", close);
  document.body.append(dlg);
  dlg.showModal();

  (async () => {
    const md = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
    if (!md || typeof md.getUserMedia !== "function" || typeof MediaRecorder === "undefined") {
      fail(MSG_UNSUPPORTED);
      return;
    }
    let stream;
    try {
      stream = await md.getUserMedia({ audio: true });
    } catch (err) {
      if (s.phase === "starting") fail(errorText(err));
      return;
    }
    if (s.phase !== "starting") {
      stream.getTracks().forEach((t) => t.stop()); // cancelled while the permission prompt was up
      return;
    }
    s.stream = stream;
    s.type = pickAudioType((t) => MediaRecorder.isTypeSupported(t));
    let rec;
    try {
      rec = new MediaRecorder(stream, s.type ? { mimeType: s.type } : undefined);
    } catch {
      fail(MSG_UNSUPPORTED);
      return;
    }
    const limit = maxBytes() * SIZE_HEADROOM;
    rec.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      s.chunks.push(e.data);
      s.bytes += e.data.size;
      // Predicts the next chunk as the same size as this one (hence e.data.size counted twice).
      if (s.phase === "recording" && s.bytes + e.data.size > limit) {
        finish("The recording reached the upload limit — uploading it now");
      }
    };
    rec.onerror = () => fail("Recording failed");
    s.rec = rec;
    try {
      rec.start(TIMESLICE_MS);
    } catch {
      fail(MSG_UNSUPPORTED);
      return;
    }
    s.phase = "recording";
    holdAwake();
    s.t0 = Date.now();
    startLevel(stream);
    status.textContent = "Recording…";
    dlg.classList.add("is-recording");
    stop.disabled = false;
    stop.focus();
    s.tick = setInterval(() => {
      const elapsed = Date.now() - s.t0;
      timer.textContent = formatElapsed(elapsed);
      if (elapsed >= MAX_RECORDING_MS) finish("The recording reached 60 minutes — uploading it now");
    }, TICK_MS);
  })().catch((err) => {
    console.error("recorder", err);
    if (s.phase === "starting" || s.phase === "recording") fail(MSG_FAILED);
  });

  return done;
}
