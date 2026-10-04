// fileshare/static/js/ticketreply-model.js — the pure logic of the reply, testing and comment boxes
// (spec T10) and the event bodies they seal (spec T5). No DOM, no crypto: node tests it.
//
//   answer:  {"question_event": uuid, "answers": {id: str|[str]|bool}, "text": str?, "voice": {file, transcript}?}
//   verdict: {"verdict": "done"|"follow-up", "text": str?, "voice": {…}?}
//   update:  {"text": md, "voice": {file, transcript}?}   (a voice-only comment has text "")

export const VERDICTS = ["done", "follow-up"];

// The newest event of `kind` (events come in ascending seq), or null.
export function latestOfKind(events, kind) {
  if (!Array.isArray(events)) return null;
  for (let i = events.length - 1; i >= 0; i -= 1) if (events[i]?.kind === kind) return events[i];
  return null;
}

export const isRequired = (q) => q?.required !== false;
const options = (q) => (Array.isArray(q?.options) ? q.options.map(String) : []);

// `v` as this question's answer, cleaned (options kept in their own order), or undefined when it
// isn't an answer at all.
function clean(q, v) {
  switch (q?.type) {
    case "single":
      return typeof v === "string" && options(q).includes(v) ? v : undefined;
    case "multi": {
      if (!Array.isArray(v)) return undefined;
      const picked = options(q).filter((o) => v.includes(o));
      return picked.length ? picked : undefined;
    }
    case "confirm":
      return typeof v === "boolean" ? v : undefined;
    case "text":
      return typeof v === "string" && v.trim() ? v.trim() : undefined;
    default:
      return undefined;
  }
}

export const isAnswered = (q, v) => clean(q, v) !== undefined;

// Send is enabled once every required question has an answer.
export function canSend(questions, answers = {}) {
  return (Array.isArray(questions) ? questions : []).every((q) => !isRequired(q) || isAnswered(q, answers[q.id]));
}

// Whether `value` (an option, or true/false for confirm) carries the "recommended" mark.
export function isRecommended(q, value) {
  const rec = q?.recommended;
  if (rec === undefined || rec === null) return false;
  if (q.type === "confirm") return typeof rec === "boolean" && rec === value;
  return Array.isArray(rec) ? rec.includes(value) : rec === value;
}

const trimmed = (s) => (typeof s === "string" && s.trim() ? s.trim() : null);
const voiceOf = (v) => (v && typeof v === "object" ? { file: v.file ?? null, transcript: typeof v.transcript === "string" ? v.transcript : null } : null);

export function buildAnswer({ questionEvent, questions = [], answers = {}, text = "", voice = null }) {
  const out = {};
  for (const q of questions) {
    const v = clean(q, answers[q.id]);
    if (v !== undefined) out[q.id] = v;
  }
  const body = { question_event: questionEvent, answers: out };
  const t = trimmed(text);
  if (t) body.text = t;
  const vo = voiceOf(voice);
  if (vo) body.voice = vo;
  return body;
}

// Done needs nothing; a follow-up needs text or a voice note.
export function canGiveVerdict(verdict, { text = "", voice = null } = {}) {
  if (verdict === "done") return true;
  if (verdict !== "follow-up") return false;
  return Boolean(trimmed(text) || voiceOf(voice));
}

export function buildVerdict(verdict, { text = "", voice = null } = {}) {
  if (!canGiveVerdict(verdict, { text, voice })) throw new Error(`a ${verdict} verdict needs text or a voice note`);
  const body = { verdict };
  const t = trimmed(text);
  if (t) body.text = t;
  const vo = voiceOf(voice);
  if (vo) body.voice = vo;
  return body;
}

// A comment (spec T14): {text} or, with a voice note, {text, voice}; a voice-only comment has
// text "". Null when there's neither.
export function buildComment(text, voice = null) {
  const t = trimmed(text);
  const vo = voiceOf(voice);
  if (vo) return { text: t ?? "", voice: vo };
  return t ? { text: t } : null;
}

// The exact POST /api/tickets/{ref}/events body. The server computes the status from the kind, so
// no status_to; a verdict carries its cleartext flag, which must match the sealed body.
export function eventRequest({ uuid, kind, encBody, files = [], verdict }) {
  const req = { uuid, kind, enc_body: encBody };
  if (Array.isArray(files) && files.length) req.files = [...files];
  if (kind === "verdict") req.verdict = verdict;
  return req;
}

// A voice note recorded offline is queued as its own FILE item; its event was sealed with
// voice.file null. Once the file is up, the body gets the FILE id; if the file is gone (discarded),
// the voice is dropped rather than pointing at nothing.
export function withVoiceFile(body, fileId) {
  const { voice, ...rest } = body;
  if (!voice) return { ...rest };
  return fileId ? { ...rest, voice: { ...voice, file: fileId } } : { ...rest };
}

export const STILL_SEND = "You can still send: the agent will only see that a voice note exists.";

// The note under a voice note after transcribeNow() resolved {outcome, reason, transcript}, in the
// file view's words (`status` is autotranscribe.js's STATUS). `settings: true` means the text ends
// with a link to Settings: "Set a Deepgram key in Settings to transcribe."
export function transcriptNote(r, status) {
  const text = r?.transcript?.text;
  if ((r?.outcome === "added" || r?.outcome === "exists") && typeof text === "string" && text) {
    return { ok: true, text: "Transcript added below. Edit it before sending if you like.", settings: false };
  }
  if (r?.outcome === "skipped" && r.reason === "no_key") return { ok: false, text: STILL_SEND, settings: true };
  let why;
  if (r?.outcome === "empty") why = status.empty;
  else if (r?.outcome === "skipped" && r.reason === "settings_unavailable") why = status.unavailable;
  else if (r?.outcome === "failed") why = status.failed(r.reason || "something went wrong");
  else if (r?.outcome === "too_long") why = status.tooLong;
  else why = status.failed("not available");
  return { ok: false, text: `${why}. ${STILL_SEND}`.replace(/([.…])\. /, "$1 "), settings: false };
}

// How a queued ticket event reads in the outbox list.
export function queuedLabel(req, ref) {
  const what = { answer: "Answer", verdict: "Verdict", update: "Comment", status: "Move" }[req?.kind] ?? "Event";
  return `${what} on ${ref}`;
}
