// fileshare/static/js/transcribe-settings.js: the transcription keys in the §15 settings object
// (spec §20), shared by settings.js and autotranscribe.js. Pure: no DOM, no network.
import { DEFAULT_LANGUAGE, LANGUAGES } from "./deepgram.js";

export { DEFAULT_LANGUAGE, LANGUAGES };
export const SETTINGS_CHANNEL = "fileshare-settings";
export const LANGUAGE_KEY = "transcribe_language";
export const AUTO_KEY = "auto_transcribe";
export const DEEPGRAM_KEY = "deepgram_api_key";
export const LANGUAGE_LABELS = Object.freeze({ de: "German", "de-CH": "Swiss German", en: "English" });

// {key, language, auto} from the decrypted settings object. A missing language or toggle gets the
// default (de, on). auto_transcribe fails closed: only a missing value or a real `true` is on;
// anything else (false, "yes", 1, null) is off, so a garbled setting never sends audio out.
export function transcriptionSettings(obj) {
  const o = obj && typeof obj === "object" && !Array.isArray(obj) ? obj : {};
  const key = typeof o[DEEPGRAM_KEY] === "string" && o[DEEPGRAM_KEY] !== "" ? o[DEEPGRAM_KEY] : null;
  const language = LANGUAGES.includes(o[LANGUAGE_KEY]) ? o[LANGUAGE_KEY] : DEFAULT_LANGUAGE;
  const auto = !Object.hasOwn(o, AUTO_KEY) || o[AUTO_KEY] === true;
  return { key, language, auto };
}
