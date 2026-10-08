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


def test_the_start_text_is_the_real_one_word_for_word():
    assert fs.start("B-0007", "Billing", 3) == ("Start the AI Factory on epic B-0007 Billing with 3 open children. "
                                                "Limits: factory True, max_children 25, max_hours 72, max_size m")
