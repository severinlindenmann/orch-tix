"""The shapes of the assertion subjects orch-core's dashboard/factory_remote.py builds for the AI Factory (read from
orch-core origin/main, PR #224). The fake host's Factory subjects are written in these shapes; tests/test_factory_subjects.py
fails when a sample drifts from them. Every text is printable ASCII: the host passes it through permits.shown (every
other character becomes a json escape such as \\u00e4), so it has no invisible character and no newline.
Limits of a Start come from epics.normalize_delegate({"factory": True}) sorted by key: the defaults are 25 children,
72 hours and size m."""
import json
import re

LIMITS = "factory True, max_children 25, max_hours 72, max_size m"


def shown(text: str) -> str:
    """permits.shown: ensure_ascii json escapes, without the quotes."""
    return json.dumps(str(text), ensure_ascii=True)[1:-1]


def start(epic: str, title: str, children: int) -> str:
    return f"Start the AI Factory on epic {epic} {shown(title)} with {children} open children. Limits: {LIMITS}"


def permission(rid: str, epic: str, command: str, scope: str = "once") -> str:
    what = "for the whole epic" if scope == "epic" else "once"
    return f"Allow {what} request {rid} on epic {epic}: {shown(command)}"


def verdict(epic: str, title: str, children: list[str], note: str = "") -> str:
    return f"Accept epic {epic} {shown(title)}: mark done " + ", ".join(children) + (f". {shown(note)}" if note else "")


SHAPES = {
    "charter": re.compile(r"^(Start the AI Factory on|Delegate to agents on|Approve the requirements, with no delegation, of) "
                          r"epic \S+ [ -~]* with \d+ open children\.( Limits: \w+ \w+(, \w+ \w+)*)?$"),
    "permission": re.compile(r"^Allow (once|for the whole epic) request \S+ on epic \S+: [ -~]*$"),
    "verdict": re.compile(r"^Accept epic \S+ [ -~]*: mark done [A-Z]+-\d+(, [A-Z]+-\d+)*(\. [ -~]*)?$"),
}
