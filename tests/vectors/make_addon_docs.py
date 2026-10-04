"""Regenerate tests/vectors/addon-docs.json: the mirror doc the orch-tix addon seals, at every redaction level.

    ORCH_CORE=~/…/plugins/orch-core uv run python tests/vectors/make_addon_docs.py

The source is `orch schema example` (orch-core) with a Verification section, Log lines, an artifact and two
widget blocks in Findings added; each level is addons/orch-tix/orch_tix/mapping.py `redact()` of it. "full+widgets"
carries `widgets`, the list orch_tix.ticket_widgets builds from orch-core's ctx.ticket_widgets (drawn here with
orch.widgets directly): the core block inline, the template as a shared FILE. The PWA's tests read the
levels (tests/js/mirror-model.test.mjs); tests/test_addon_doc_vectors.py checks the levels still equal
what the addon makes of the stored source."""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).with_name("addon-docs.json")


def mapping():
    # mapping.py imports its sibling history.py relatively: load the package (no orch import needed for these two)
    import sys
    pkg = REPO / "addons" / "orch-tix" / "orch_tix"
    spec = importlib.util.spec_from_file_location("orch_tix_vec", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    if "orch_tix_vec" not in sys.modules:
        parent = importlib.util.module_from_spec(spec)
        sys.modules["orch_tix_vec"] = parent      # never executed: only its path serves the relative imports
    sub = importlib.util.spec_from_file_location("orch_tix_vec.mapping", pkg / "mapping.py")
    mod = importlib.util.module_from_spec(sub)
    sys.modules["orch_tix_vec.mapping"] = mod
    sub.loader.exec_module(mod)
    return mod


# The ticket's history from orch events as the addon keeps it (orch_tix/history.py entries).
HISTORY = [
    {"seq": 11, "at": "2026-10-02T09:00Z", "who": "claude-code", "what": "created the ticket"},
    {"seq": 12, "at": "2026-10-02T09:05Z", "who": "human (desktop log)", "what": "approved the requirements"},
    {"seq": 13, "at": "2026-10-02T09:10Z", "who": "human (desktop log)", "what": "asked for changes on the plan", "text": "split the exporter"},
    {"seq": 14, "at": "2026-10-02T09:20Z", "who": "human (desktop log)", "what": "approved the plan"},
    {"seq": 15, "at": "2026-10-02T09:24Z", "who": "claude-code", "what": "started T2"},
    {"seq": 16, "at": "2026-10-02T09:30Z", "who": "claude-code", "what": "asked Q1"},
]


def levels(source: dict, context: list, widgets: list, history: list = HISTORY) -> dict:
    m = mapping()
    kw = {"context_artifacts": context, "history": history}
    return {"full": m.redact(source, "full", sync_log=False, **kw),
            "full+widgets": m.redact(source, "full", sync_log=False, widgets=widgets, **kw),
            "full+log": m.redact(source, "full", sync_log=True, **kw),
            "title": m.redact(source, "title", sync_log=False, **kw),
            "key-only": m.redact(source, "key-only", sync_log=False, **kw)}


def source_from(example: dict) -> dict:
    """`orch schema example` plus what the fixture adds: a Verification section, Log lines and an artifact."""
    import copy
    source = copy.deepcopy(example)
    source["sections"]["Verification"] = "All 14 jobs export; Excel opens every file.\nDetails in the report."
    source["sections"]["Log"] = "\n".join(f"2026-10-02T09:{i:02d}Z agent: step {i}" for i in range(25))
    source["artifacts"] = ["report.md"]
    source["sections"]["Findings"] = FINDINGS
    return source


STATS = {"type": "stats", "title": "Export run", "source": "agent-measured",
         "items": [{"label": "files", "value": 14, "role": "ok"}, {"label": "largest", "value": "2.1 MB"}]}
MATRIX = {"widget": "decision-matrix@1", "title": "Timestamp format", "data": {
    "scale": {"min": 0, "max": 10}, "pick": "iso",
    "criteria": [{"id": "excel", "label": "Excel parses it", "weight": 3}],
    "options": [{"id": "iso", "label": "ISO 8601", "scores": [9]}, {"id": "local", "label": "Local time", "scores": [6]}]}}
FINDINGS = ("Measured on the staging copy.\n\n```orch\n" + json.dumps(STATS) + "\n```\n\nThe two formats:\n\n```orch\n"
            + json.dumps(MATRIX) + "\n```\n\nExcel 16 tested.")

# Run inside orch-core: each Findings block as ctx.ticket_widgets reports it (a chrome-less document: the
# PWA card draws title, chip, source and text itself).
_DRAW = """
import hashlib, json, sys
from orch import widgets as W
from orch.widgets.render import text_of
ctx = W.Ctx(theme="system")
out = []
for b in W.parse_blocks(sys.stdin.read(), "Findings"):
    d = b.data
    out.append({"section": b.section, "index": len(out), "key": str(len(out)), "layer": b.layer,
                "name": d[b.layer], "title": d.get("title", ""), "source": d.get("source", ""),
                "text": text_of(b, ctx), "document": W.render_document(b, ctx, chrome=False)})
print(json.dumps(out))
"""


# An agent-HTML document as orch-core's frame assembler builds it (the built-in decision-matrix@1 with its
# example data and the real orch-kit): the browser tests draw it in the PWA's widget frame.
TEMPLATE_OUT = Path(__file__).with_name("widget-template.html")
_TEMPLATE = """
import json, sys
from pathlib import Path
from orch.widgets import assemble as A
spec = A.BUILTIN / "decision-matrix"
data = json.loads((spec / "example.json").read_text())["1"]
kit, tokens = A.read_static()
sys.stdout.write(A.assemble((spec / "v1.html").read_text(), data, nonce="tixtest0001", kit_js=kit,
                            tokens_css=tokens, title="Decision matrix"))
"""


def template_doc(core: Path) -> str:
    return subprocess.run(["uv", "run", "--project", str(core), "python", "-c", _TEMPLATE], check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout


def widgets_from(core: Path) -> list:
    """orch_tix.ticket_widgets.entries for the Findings blocks: a core document inline, the template shared."""
    blocks = json.loads(subprocess.run(["uv", "run", "--project", str(core), "python", "-c", _DRAW], check=True,
                                       capture_output=True, text=True, input=FINDINGS).stdout)
    raws = re.findall(r"```orch\n(.*?)\n```", FINDINGS, re.S)
    template = TEMPLATE_OUT.read_bytes()
    out = []
    for b, raw in zip(blocks, raws):
        e = {k: b[k] for k in ("section", "index", "key", "layer", "name", "title", "text")}
        e["raw_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if b["source"]:
            e["source"] = b["source"]
        if b["layer"] == "type":
            e["doc"] = b["document"]
            e["sha256"] = hashlib.sha256(b["document"].encode("utf-8")).hexdigest()
        else:
            e["file"] = "FILE8"
            e["sha256"] = hashlib.sha256(template).hexdigest()   # the FILE is the template page saved beside this
        out.append(e)
    return out


def main() -> None:
    core = Path(os.environ["ORCH_CORE"]).expanduser()
    raw = subprocess.run(["uv", "run", "--project", str(core), "orch", "schema", "example"], check=True,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    source = source_from(json.loads(raw))
    context = [{"name": "report.md", "file": "FILE7"}]
    widgets = widgets_from(core)
    TEMPLATE_OUT.write_text(template_doc(core), encoding="utf-8")
    OUT.write_text(json.dumps({"source": source, "context_artifacts": context, "widgets": widgets, "history": HISTORY,
                               "levels": levels(source, context, widgets)},
                              indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
