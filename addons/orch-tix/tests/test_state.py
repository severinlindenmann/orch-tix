import json

from orch_tix.state import State


def test_a_corrupt_file_reads_empty_and_is_kept_only_on_write(tmp_path):
    st = State(tmp_path)
    (tmp_path / "links.json").write_text("{not json", encoding="utf-8")
    assert st.links() == {}
    assert not (tmp_path / "links.json.broken").exists()        # a read (also during a render) writes nothing
    st.link("DEMO-0001", by="you", auto=False)
    assert (tmp_path / "links.json.broken").read_text(encoding="utf-8") == "{not json"
    assert set(json.loads((tmp_path / "links.json").read_text(encoding="utf-8"))) == {"DEMO-0001"}
    assert (tmp_path / "links.json").stat().st_mode & 0o777 == 0o600
