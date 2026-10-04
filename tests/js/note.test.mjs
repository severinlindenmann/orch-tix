// tests/js/note.test.mjs — the Note composer's pure helpers (spec §16).
import { test } from "node:test";
import assert from "node:assert/strict";
import { noteName, noteFile } from "../../fileshare/static/js/note.js";

test("the default name is note-YYYY-MM-DD-HHMM.md in local time, zero-padded", () => {
  assert.equal(noteName(new Date(2026, 0, 2, 3, 4, 59)), "note-2026-01-02-0304.md");
  assert.equal(noteName(new Date(2026, 11, 31, 23, 59)), "note-2026-12-31-2359.md");
});

test("a note is a text/markdown file; an empty name falls back to the default", async () => {
  const f = noteFile("# Hi\n", "  ", "note-2026-01-02-0304.md");
  assert.equal(f.name, "note-2026-01-02-0304.md");
  assert.equal(f.type, "text/markdown");
  assert.equal(await f.text(), "# Hi\n");
  assert.equal(noteFile("x", " todo.md ", "d.md").name, "todo.md");
});

test("an empty or blank text makes no note", () => {
  assert.equal(noteFile("", "a.md", "d.md"), null);
  assert.equal(noteFile(" \n\t", "a.md", "d.md"), null);
});
