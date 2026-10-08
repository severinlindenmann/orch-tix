"""The fake host's Factory subject texts keep the shape of orch-core's factory_remote.py (see tests/support/factory_subjects.py)."""
import pytest

from tests.support import factory_subjects as fs

SAMPLES = [
    ("charter", fs.start("B-0007", "Billing cleanup", 3)),
    ("charter", fs.start("B-0007", "Zürich \U0001f600 Rechnungen", 25)),
    ("permission", fs.permission("PR-3", "B-0007", "git push origin factory/b-0007")),
    ("permission", fs.permission("PR-4", "B-0007", "echo \"ü\"\n", "epic")),
    ("verdict", fs.verdict("B-0007", "Billing cleanup", ["B-0008", "B-0009"])),
    ("verdict", fs.verdict("B-0007", "Billing cleanup", ["B-0008"], "looks good")),
]


@pytest.mark.parametrize("kind,text", SAMPLES)
def test_a_sample_has_the_real_shape_and_is_printable_ascii(kind, text):
    assert fs.SHAPES[kind].match(text), text
    assert all(" " <= c <= "~" for c in text)


@pytest.mark.parametrize("kind,text", [
    ("charter", "Begin the Factory on epic B-0007 T with 3 open children."),
    ("charter", "Start the AI Factory on epic B-0007 T with 3 children."),
    ("permission", "Allow request PR-3 on epic B-0007: ls"),
    ("verdict", "Accept epic B-0007 T: done B-0008"),
    ("permission", "Allow once request PR-3 on epic B-0007: \u00fc is a raw ü"),
])
def test_a_drifted_text_is_caught(kind, text):
    assert not fs.SHAPES[kind].match(text)


# Golden strings, written out by hand from orch-core's plugins/orch-core/src/orch/dashboard/factory_remote.py at origin/main
# 9378465 (the permission, charter, verdict and action builders), epics.normalize_delegate with DELEGATE_DEFAULTS and
# FACTORY_DEFAULTS, and shown() (ensure_ascii json escapes). They are NOT produced by our builders: if orch-core changes a
# wording, update both on purpose.
GOLDEN = [
    (fs.permission("PR-3", "B-0007", "git push origin factory/b-0007"), "Allow once request PR-3 on epic B-0007: git push origin factory/b-0007"),
    (fs.permission("PR-4", "B-0007", "git push origin factory/b-0007", "epic"), "Allow for the whole epic request PR-4 on epic B-0007: git push origin factory/b-0007"),
    (fs.start("B-0007", "Billing cleanup", 3), "Start the AI Factory on epic B-0007 Billing cleanup with 3 open children. Limits: factory True, max_children 25, max_hours 72, max_size m"),
    (fs.delegate("B-0007", "Billing cleanup", 3), "Delegate to agents on epic B-0007 Billing cleanup with 3 open children. Limits: max_children 10, max_size m"),
    (fs.plain("B-0007", "Billing cleanup", 3), "Approve the requirements, with no delegation, of epic B-0007 Billing cleanup with 3 open children."),
    (fs.verdict("B-0007", "Billing cleanup", ["B-0008", "B-0009"]), "Accept epic B-0007 Billing cleanup: mark done B-0008, B-0009"),
    (fs.verdict("B-0007", "Billing cleanup", ["B-0008"], "all fine"), "Accept epic B-0007 Billing cleanup: mark done B-0008. all fine"),
    (fs.ticket_verdict("B-0008", "Fix login", "done"), "Verdict done on B-0008 Fix login"),
    (fs.ticket_verdict("B-0008", "Fix login", "follow-up", ["AC1", "AC2"], "later"), "Verdict follow-up on B-0008 Fix login (criteria AC1, AC2). later"),
    (fs.action("POST", "/t/B-0008/edit", "title=Fix+login"), "POST /t/B-0008/edit under a running AI Factory epic: title=Fix+login"),
    (fs.start("B-0007", "Zürich", 1), "Start the AI Factory on epic B-0007 Z\\u00fcrich with 1 open children. Limits: factory True, max_children 25, max_hours 72, max_size m"),
    (fs.action("POST", "/t/B-0008/edit", "a\nb"), "POST /t/B-0008/edit under a running AI Factory epic: a\\nb"),
]


@pytest.mark.parametrize("built,golden", GOLDEN)
def test_the_fake_hosts_subjects_equal_the_real_hosts_golden_strings(built, golden):
    assert built == golden


def test_the_start_text_is_the_real_one_word_for_word():
    assert fs.start("B-0007", "Billing", 3) == ("Start the AI Factory on epic B-0007 Billing with 3 open children. "
                                                "Limits: factory True, max_children 25, max_hours 72, max_size m")
