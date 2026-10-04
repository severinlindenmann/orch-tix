from helpers import ADDON, REC
from orch.testing import AddonContract, FakeRunner


class TestOrchTix(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner.from_dir(REC, strict=False)
