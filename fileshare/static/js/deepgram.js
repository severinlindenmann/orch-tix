// fileshare/static/js/deepgram.js: the browser's Deepgram client (spec §20). Pure: no DOM, no
// storage. The plaintext audio goes to Deepgram over HTTPS; the key goes only to that origin, in
// the Authorization header. Neither the key nor Deepgram's response body ever reaches a message,
// an error property or the console. Errors carry one short reason for the status line.
import { TRANSCRIPT_MAX_CHARS } from "./crypto.js";

export const LISTEN_URL = "https://api.deepgram.com/v1/listen";
export const MODEL = "nova-3";
export const LANGUAGES = Object.freeze(["de", "de-CH", "en"]);
export const DEFAULT_LANGUAGE = "de";
export const TIMEOUT_MS = 120_000;
export const TRANSCRIPT_CAP = TRANSCRIPT_MAX_CHARS;

// The same rule settings.js applies when the key is saved: 16–256 printable ASCII, no spaces.
const KEY_RE = /^[\x21-\x7e]{16,256}$/;

export const REASONS = Object.freeze({
  key: "Deepgram rejected the API key",
  quota: "Deepgram quota or rate limit",
  unavailable: "Deepgram unavailable",
  audio: "Deepgram couldn't read this audio",
  shape: "Deepgram sent an unexpected response",
  noKey: "No valid Deepgram key in Settings",
  language: "Unsupported transcription language",
});

export class TranscribeError extends Error {
  constructor(reason) {
    super(reason);
    this.name = "TranscribeError";
    this.reason = reason;
  }
}

export function listenUrl(language) {
  if (typeof language !== "string" || !LANGUAGES.includes(language)) throw new TranscribeError(REASONS.language);
  return `${LISTEN_URL}?model=${MODEL}&smart_format=true&language=${language}`;
}

// C0 controls except \n and \t (CR and CRLF become \n first), DEL, C1, and the bidi controls
// (ALM, LRM, RLM, the embeddings/overrides and the isolates).
const CONTROL_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f؜‎‏‪-‮⁦-⁩]/g;

export function cleanTranscript(text) {
  let t = String(text).replace(/\r\n?/g, "\n").replace(CONTROL_RE, "").trim();
  if (t.length > TRANSCRIPT_CAP) {
    t = t.slice(0, TRANSCRIPT_CAP);
    if (/[\ud800-\udbff]$/.test(t)) t = t.slice(0, -1); // never leave half a surrogate pair
  }
  return t;
}

function reasonFor(status) {
  if (status === 401 || status === 403) return REASONS.key;
  if (status === 402 || status === 429) return REASONS.quota;
  if (status === 400) return REASONS.audio;
  if (status >= 500) return REASONS.unavailable;
  return `Deepgram refused the request (HTTP ${status})`;
}

// results.channels[0].alternatives[0].transcript, or a TranscribeError for any other shape.
export function transcriptOf(json) {
  const alt = json?.results?.channels?.[0]?.alternatives?.[0];
  if (!Array.isArray(json?.results?.channels) || !Array.isArray(json.results.channels[0]?.alternatives)
      || typeof alt?.transcript !== "string") {
    throw new TranscribeError(REASONS.shape);
  }
  return alt.transcript;
}

// Sends `body` (a Blob, File or bytes of plaintext audio) and resolves {text, language, model}.
// `text` is cleaned and capped, and may be "" (no speech). Rejects with a TranscribeError only.
export async function transcribeAudio(body, { key, mime, language, fetch = globalThis.fetch, timeoutMs = TIMEOUT_MS } = {}) {
  const url = listenUrl(language);
  if (typeof key !== "string" || !KEY_RE.test(key)) throw new TranscribeError(REASONS.noKey);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs); // covers reading the answer's body too
  try {
    let res;
    try {
      res = await fetch(url, {
        method: "POST",
        headers: { Authorization: `Token ${key}`, "Content-Type": mime || "application/octet-stream" },
        body,
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
        signal: ctrl.signal,
      });
    } catch {
      // Offline, a CORS or redirect refusal, or the timeout: the cause is dropped on purpose.
      throw new TranscribeError(REASONS.unavailable);
    }
    if (!res || res.status < 200 || res.status > 299) throw new TranscribeError(reasonFor(res?.status ?? 0));
    let json;
    try {
      json = await res.json();
    } catch {
      throw new TranscribeError(ctrl.signal.aborted ? REASONS.unavailable : REASONS.shape);
    }
    return { text: cleanTranscript(transcriptOf(json)), language, model: MODEL };
  } finally {
    clearTimeout(timer);
  }
}
