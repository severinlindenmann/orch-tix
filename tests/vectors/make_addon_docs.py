"""Regenerate tests/vectors/addon-docs.json: the mirror doc the orch-tix addon seals, at every redaction level.

    ORCH_CORE=~/…/plugins/orch-core uv run python tests/vectors/make_addon_docs.py

The source is `orch schema example` (orch-core) with a Verification section, Log lines and an artifact
added; each level is addons/orch-tix/orch_tix/mapping.py `redact()` of it. The PWA's tests read the
levels (tests/js/mirror-model.test.mjs); tests/test_addon_doc_vectors.py checks the levels still equal
what the addon makes of the stored source."""
import importlib.util
import json
import os
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


def levels(source: dict, context: list, history: list = HISTORY) -> dict:
    m = mapping()
    kw = {"context_artifacts": context, "history": history}
    return {"full": m.redact(source, "full", sync_log=False, **kw),
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
    return source


def main() -> None:
    core = Path(os.environ["ORCH_CORE"]).expanduser()
    raw = subprocess.run(["uv", "run", "--project", str(core), "orch", "schema", "example"], check=True,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    source = source_from(json.loads(raw))
    context = [{"name": "report.md", "file": "FILE7"}]
    OUT.write_text(json.dumps({"source": source, "context_artifacts": context, "history": HISTORY,
                               "levels": levels(source, context)},
                              indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
