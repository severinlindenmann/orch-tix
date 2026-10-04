import pytest

from fileshare.ids import format_id, parse_ref


def test_format_id():
    assert format_id(7) == "FILE7"
    assert format_id(1042) == "FILE1042"


@pytest.mark.parametrize("ref,expected", [
    ("FILE7", 7), ("file7", 7), ("File7", 7), ("fIlE7", 7), ("7", 7),
    ("FILE1042", 1042), ("999999999999", 999999999999),
])
def test_parse_ref_accepts(ref, expected):
    assert parse_ref(ref) == expected


@pytest.mark.parametrize("ref", [
    "", "FILE", "FILE0", "0", "007", "FILE07", "-1", "FILE-1", "FILE 7", " 7", "7 ",
    "FILE7x", "F7", "FILES7", "1e3", "１", "FILE1000000000000", "../7", "7/blob",
])
def test_parse_ref_rejects(ref):
    assert parse_ref(ref) is None
