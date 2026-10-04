"""Parity tests for the restricted YAML subset (spec T8): tests/vectors/frontmatter.json is shared
with the retired tests/js/frontmatter.test.mjs (the PWA no longer parses frontmatter: legacy tickets open as JSON)."""
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VEC = json.loads((REPO / "tests" / "vectors" / "frontmatter.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("c", VEC["ok"], ids=lambda c: c["name"])
def test_frontmatter_ok(sharing, c):
    fm, body = sharing.parse_frontmatter(c["text"])
    assert fm == c["fm"]
    assert body == c["body"]


@pytest.mark.parametrize("c", VEC["bad"], ids=lambda c: c["name"])
def test_frontmatter_bad(sharing, c):
    with pytest.raises(sharing.UsageError) as exc_info:
        sharing.parse_frontmatter(c["text"])
    msg = str(exc_info.value)
    assert f"line {c['line']}" in msg


@pytest.mark.parametrize("obj", VEC["roundtrip"], ids=lambda o: o.get("title", "questions"))
def test_frontmatter_roundtrip(sharing, obj):
    emitted = sharing.emit_yaml_subset(obj)
    assert sharing.parse_yaml_subset(emitted) == obj
