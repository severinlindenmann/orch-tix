"""The web's tag rules (fileshare/static/js/tags.js) match the server's (fileshare/tags.py, spec §19):
the Python tests' GOOD_TAGS / BAD_TAGS sets run through the JS module under node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fileshare import tags
from tests.test_tags_api import BAD_TAGS, GOOD_TAGS

TAGS_JS = Path(__file__).resolve().parent.parent / "fileshare" / "static" / "js" / "tags.js"

SCRIPT = """
import { normalizeTag, normalizeTags } from %s;
const input = JSON.parse(process.argv[1]);
const run = (fn) => (raw) => { try { return { ok: fn(raw) }; } catch (e) { return { bad: e.name }; } };
process.stdout.write(JSON.stringify({
  good: input.good.map(run(normalizeTag)), bad: input.bad.map(run(normalizeTag)), lists: input.lists.map(run(normalizeTags)),
}));
"""

LISTS = [
    ["b", "A", "a", " b ", "Weekly Report", "weekly_report"],
    [f"t{i}" for i in range(10)] + ["T0", "t1 "],
    [f"t{i}" for i in range(11)],
    [],
    "notes",
    ["ok", "no/slash"],
]


@pytest.fixture(scope="module")
def js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    payload = json.dumps({"good": [raw for raw, _ in GOOD_TAGS], "bad": BAD_TAGS, "lists": LISTS})
    out = subprocess.run([node, "--input-type=module", "-e", SCRIPT % json.dumps(TAGS_JS.as_uri()), payload],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_good_tags_normalise_the_same(js):
    assert [r.get("ok") for r in js["good"]] == [want for _, want in GOOD_TAGS]


def test_bad_tags_are_rejected_the_same(js):
    assert all(r == {"bad": "BadTag"} for r in js["bad"]), list(zip(BAD_TAGS, js["bad"]))


def test_lists_normalise_the_same(js):
    want = []
    for raw in LISTS:
        try:
            want.append({"ok": tags.normalize_tags(raw)})
        except tags.BadTag:
            want.append({"bad": "BadTag"})
    assert js["lists"] == want
