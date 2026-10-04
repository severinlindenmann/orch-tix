import sys
from pathlib import Path

import pytest

pytest.importorskip("orch.addons.api", reason="needs orch-core with A4-P0 (see the plan header)")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ADDON, REC, SHARING  # noqa: E402  (SHARING: an absolute path FakeRunner never executes)
from orch.testing.pytest_plugin import orch_user_dir  # noqa: E402,F401

sys.path.insert(0, str(ADDON))


@pytest.fixture
def tix_ws(tmp_path, orch_user_dir):
    from orch.testing import fake_workspace
    fw = fake_workspace(tmp_path / "acme", customer="Acme Energy", prefix="DEMO")
    fw.enable("orch-tix", {"sharing_path": SHARING})
    return fw


@pytest.fixture
def runner():
    from helpers import CapturingRunner
    return CapturingRunner.from_dir(REC, strict=True)


@pytest.fixture
def tix(tix_ws, runner):
    return tix_ws.load(ADDON, runner=runner)
