"""tests/vectors/addon-docs.json is what the orch-tix addon seals at each redaction level (the PWA's tests
read it). If the addon's mapping changes, regenerate it: tests/vectors/make_addon_docs.py."""
import hashlib
import json
from pathlib import Path

from tests.vectors.make_addon_docs import levels

VECTORS = json.loads((Path(__file__).parent / "vectors" / "addon-docs.json").read_text(encoding="utf-8"))


def test_the_fixture_is_what_the_addon_makes_of_its_source():
    assert levels(VECTORS["source"], VECTORS["context_artifacts"], VECTORS["widgets"], VECTORS["history"]) == VECTORS["levels"]


def test_the_fixture_covers_every_level_with_its_redaction():
    assert {k: v["redaction"] for k, v in VECTORS["levels"].items()} == {
        "full": "full", "full+widgets": "full", "full+log": "full", "title": "title", "key-only": "key-only"}


def test_widgets_ride_along_at_full_only():
    lv = VECTORS["levels"]
    assert lv["full+widgets"]["widgets"] == VECTORS["widgets"] and lv["full+widgets"]["widgets_format"] == "orch.widgets.v1"
    assert all("widgets" not in lv[k] for k in ("full", "full+log", "title", "key-only"))
    core, template = VECTORS["widgets"]
    assert core["doc"].startswith("<!doctype html>") and template["file"] == "FILE8" and "doc" not in template


def test_the_template_pin_is_the_digest_of_the_template_file_beside_it():
    """#14: regenerating widget-template.html without the vector (or the reverse) leaves a pin no file matches,
    and the phone then refuses to draw the template. make_addon_docs.py writes the file first, then pins it."""
    template = (Path(__file__).parent / "vectors" / "widget-template.html").read_bytes()
    pinned = VECTORS["widgets"][1]["sha256"]
    assert pinned == hashlib.sha256(template).hexdigest()
    assert VECTORS["levels"]["full+widgets"]["widgets"][1]["sha256"] == pinned
    assert b"--term-bg" in template          # the terminal design tokens orch-core's frame assembler now carries


def test_the_fixture_source_is_the_current_orch_schema_example():
    """Final review M6: where orch-core is installed, the stored source is `orch schema example` plus the
    generator's additions, so a schema change shows up here instead of drifting silently."""
    import pytest
    schema = pytest.importorskip("orch.core.schema")
    from tests.vectors.make_addon_docs import source_from
    assert VECTORS["source"] == source_from(schema.example_document())
