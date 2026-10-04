import json
from pathlib import Path

from orch.addons.runner import RunResult
from orch.testing import FakeRunner

ADDON = Path(__file__).resolve().parents[1]
REC = Path(__file__).resolve().parent / "recordings"
SHARING = "/opt/sharing/sharing"
PUSH = [SHARING, "mirror", "push", "--file", "*", "--json"]


class CapturingRunner(FakeRunner):
    """A FakeRunner that keeps every --file payload (the addon deletes it right after the call) and lets a test
    answer one subcommand with a function (for `get`, which must write a file)."""

    def __init__(self, recordings=(), *, strict: bool = True):
        super().__init__(recordings, strict=strict)
        self.files: list[dict] = []
        self.handlers: dict[str, object] = {}

    @classmethod
    def from_dir(cls, folder, *, strict: bool = True) -> "CapturingRunner":
        recs = []
        for path in sorted(Path(folder).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            recs.extend(data if isinstance(data, list) else [data])
        return cls(recs, strict=strict)

    def __call__(self, argv, timeout):
        if "--file" in argv:
            self.files.append(json.loads(Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")))
        handler = self.handlers.get(argv[1]) if len(argv) > 1 else None
        if handler is not None:
            self.calls.append(tuple(argv))
            return handler(argv, timeout)
        return super().__call__(argv, timeout)


def ok(argv, stdout_json) -> RunResult:
    return RunResult(tuple(argv), 0, json.dumps(stdout_json), "")
