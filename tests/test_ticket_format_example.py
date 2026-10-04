"""docs/ticket-format-example.md is `orch schema example` pasted under a fixed header (Task 12)."""
import json
import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "ticket-format-example.md"


def test_the_example_is_a_generated_schema_1_document():
    text = DOC.read_text(encoding="utf-8")
    m = re.search(r"Generated with `orch schema example` \(orch-core schema (1\.\d+\.\d+)\)\. Do not edit by hand\.",
                  text)
    assert m, "the header line is missing"
    (block,) = re.findall(r"```json\n(.*?)\n```", text, re.S)
    doc = json.loads(block)
    assert doc["schema_version"].startswith("1.") and doc["schema_version"] == m.group(1)
    assert {"questions", "gates", "tasks", "needs", "sections"} <= set(doc)
    assert doc["tasks"]["format"] == "orch.tasks.v1"
    assert doc["questions"][0]["hash"].startswith("sha256:")


def test_the_example_matches_the_installed_orch_core():
    """Only where orch-core is installed (e.g. `uv run --with "orch-core @ file://…" pytest …`)."""
    import pytest
    schema = pytest.importorskip("orch.core.schema")
    (block,) = re.findall(r"```json\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)
    assert json.loads(block) == schema.example_document(), "regenerate docs/ticket-format-example.md"
