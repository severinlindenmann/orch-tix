"""Load skill/sharing/sharing.py once, as the module named 'sharing' (shared with the conftest fixture)."""
import importlib.util
import sys
from pathlib import Path

SHARING_PY = Path(__file__).resolve().parents[2] / "skill" / "sharing" / "sharing.py"


def load():
    if "sharing" in sys.modules:
        return sys.modules["sharing"]
    spec = importlib.util.spec_from_file_location("sharing", SHARING_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sharing"] = mod
    spec.loader.exec_module(mod)
    return mod
