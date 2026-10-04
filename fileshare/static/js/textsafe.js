// fileshare/static/js/textsafe.js — hidden characters in ticket text, as orch-core's orch/textsafe.py defines them:
// Unicode category Cc (control), Cf (format: zero-width, bidi marks and overrides, soft hyphen, tags), Zl, Zp,
// Co (private use) and Cn (unassigned), tab and newline aside, plus the invisible fillers U+034F, U+115F, U+1160,
// U+3164 and U+FFA0. Such characters can reorder, hide or rewrite what the human reads before deciding, so the
// phone shows each as a visible <U+XXXX> badge and decides nothing on text that holds one. Pure: node tests it.
const HIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Co}\p{Cn}͏ᅟᅠㅤﾠ]/u;
const ANY_HIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Co}\p{Cn}͏ᅟᅠㅤﾠ]/u;

export function isHidden(ch) {
  return ch !== "\t" && ch !== "\n" && HIDDEN.test(ch);
}

export function hasHidden(text) {
  const s = typeof text === "string" ? text : String(text ?? "");
  if (!HIDDEN.test(s)) return false;
  for (const ch of s) if (isHidden(ch)) return true;
  return false;
}

// "<U+202E>" for one character.
export function badge(ch) {
  return `<U+${ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}>`;
}

// The text in runs: [{text}] for what shows as is, [{hidden: "<U+202E>"}] for each hidden character.
export function splitHidden(text) {
  const s = typeof text === "string" ? text : String(text ?? "");
  if (!hasHidden(s)) return s ? [{ text: s }] : [];
  const out = [];
  let run = "";
  for (const ch of s) {
    if (isHidden(ch)) {
      if (run) out.push({ text: run });
      run = "";
      out.push({ hidden: badge(ch) });
    } else {
      run += ch;
    }
  }
  if (run) out.push({ text: run });
  return out;
}

// Whether any of the strings (nested arrays and objects' string values too) holds a hidden character.
export function anyHidden(...values) {
  const walk = (v) => (typeof v === "string" ? hasHidden(v)
    : Array.isArray(v) ? v.some(walk) : v && typeof v === "object" ? Object.values(v).some(walk) : false);
  return values.some(walk);
}
