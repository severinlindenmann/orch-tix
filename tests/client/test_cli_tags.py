"""CLI side of tags (spec §19): put/share --tag, list/latest --tag, tag, untag, tags."""
import pytest

from tests.test_tags_api import BAD_TAGS, GOOD_TAGS   # the server tests' full good and bad sets


def _put(cli, dev_repo, name, *tags, extra=()):
    p = dev_repo.root / name
    p.write_text(f"content of {name}\n")
    args = ["put", name, "-m", "test"]
    for t in tags:
        args += ["--tag", t]
    r = cli(dev_repo.root, *args, *extra, "--json")
    assert r.code == 0, r.err
    return r.json()


def _server_tags(sim, ref):
    return sim.get_file(ref)["tags"]


# ---------------------------------------------------------------- normalisation (mirrors the server)

@pytest.mark.parametrize("raw,want", GOOD_TAGS)
def test_client_normalises_like_the_server(sharing, raw, want):
    from fileshare import tags as server_tags
    assert sharing.normalize_tag(raw) == want == server_tags.normalize_tag(raw)


@pytest.mark.parametrize("raw", BAD_TAGS)
def test_client_rejects_what_the_server_rejects(sharing, raw):
    from fileshare import tags as server_tags
    with pytest.raises(sharing.UsageError):
        sharing.normalize_tag(raw)
    with pytest.raises(server_tags.BadTag):
        server_tags.normalize_tag(raw)


# ---------------------------------------------------------------- put / share --tag

def test_put_with_tags(cli, dev_repo, sim):
    j = _put(cli, dev_repo, "r.md", "Weekly Report", "notes", "notes")
    assert j["tags"] == ["notes", "weekly-report"]
    assert _server_tags(sim, j["id"]) == ["notes", "weekly-report"]


def test_share_is_the_same_command(cli, dev_repo, sim):
    (dev_repo.root / "a.txt").write_text("a")
    r = cli(dev_repo.root, "share", "a.txt", "-m", "x", "--tag", "debugging", "--json")
    assert r.code == 0, r.err
    assert r.json()["tags"] == ["debugging"] and _server_tags(sim, r.json()["id"]) == ["debugging"]


def test_put_without_tags_has_an_empty_list(cli, dev_repo):
    assert _put(cli, dev_repo, "a.txt")["tags"] == []


@pytest.mark.parametrize("bad", ["no/slash", "-lead", "a" * 41, ""])
def test_put_bad_tag_exits_1_and_uploads_nothing(cli, dev_repo, sim, bad):
    (dev_repo.root / "a.txt").write_text("a")
    r = cli(dev_repo.root, "put", "a.txt", "-m", "x", "--tag", bad, "--json")
    assert r.code == 1 and r.json()["error"] == "usage"
    assert sim.http.get("/api/files?acked=1").json()["files"] == []


def test_put_more_than_ten_tags_exits_1(cli, dev_repo, sim):
    (dev_repo.root / "a.txt").write_text("a")
    args = []
    for i in range(11):
        args += ["--tag", f"t{i}"]
    r = cli(dev_repo.root, "put", "a.txt", "-m", "x", *args)
    assert r.code == 1 and "10" in r.err
    assert sim.http.get("/api/files?acked=1").json()["files"] == []


# ---------------------------------------------------------------- list / latest --tag

def test_list_tag_all_limit_3_returns_the_newest_3_even_when_acknowledged(cli, dev_repo, sim):
    """The SKILL.md example: reports already fetched (so acknowledged) still count as the newest 3."""
    ids = []
    for i in range(5):
        ids.append(_put(cli, dev_repo, f"report{i}.md", "weekly-report")["id"])
        _put(cli, dev_repo, f"other{i}.txt", "notes")
    newest3 = list(reversed(ids))[:3]
    # the user already fetched the newest and the third-newest report: `get` acknowledged them
    for ref in (newest3[0], newest3[2]):
        g = cli(dev_repo.root, "get", ref, "--json")
        assert g.code == 0 and g.json()["acked"] is True, g.err
    r = cli(dev_repo.root, "list", "--tag", "weekly-report", "--all", "--limit", "3", "--json")
    assert r.code == 0, r.err
    rows = r.json()
    assert [x["id"] for x in rows] == newest3
    assert all(x["tags"] == ["weekly-report"] for x in rows)
    assert [bool(x["acked_at"]) for x in rows] == [True, False, True]
    # without --all the acknowledged ones are skipped: not "the newest 3"
    plain = [x["id"] for x in cli(dev_repo.root, "list", "--tag", "weekly-report", "--limit", "3", "--json").json()]
    assert plain == [newest3[1]] + list(reversed(ids))[3:4] + list(reversed(ids))[4:5]
    assert plain != newest3
    assert [x["id"] for x in cli(dev_repo.root, "list", "--tag", "Weekly Report", "--all", "-n", "3",
                                 "--json").json()] == newest3


def test_list_tag_filter_is_server_side(cli, dev_repo, sim, sharing, monkeypatch):
    _put(cli, dev_repo, "a.md", "weekly-report")
    seen = []
    real = sharing.Api.get_json

    def spy(self, path):
        seen.append(path)
        return real(self, path)
    monkeypatch.setattr(sharing.Api, "get_json", spy)
    cli(dev_repo.root, "list", "--tag", "weekly-report", "--tag", "b", "--json")
    files_calls = [p for p in seen if p.startswith("/api/files")]
    assert files_calls and all("tag=weekly-report" in p and "tag=b" in p for p in files_calls)


def test_list_multiple_tags_must_all_match(cli, dev_repo):
    a = _put(cli, dev_repo, "a.md", "x", "y")["id"]
    _put(cli, dev_repo, "b.md", "x")
    assert [r["id"] for r in cli(dev_repo.root, "list", "--tag", "x", "--tag", "y", "--json").json()] == [a]


def test_list_bad_tag_exits_1(cli, dev_repo):
    r = cli(dev_repo.root, "list", "--tag", "a/b", "--json")
    assert r.code == 1 and r.json()["error"] == "usage"


def test_list_limit_keeps_working(cli, dev_repo):
    for i in range(4):
        _put(cli, dev_repo, f"f{i}.txt")
    assert len(cli(dev_repo.root, "list", "--limit", "2", "--json").json()) == 2
    assert len(cli(dev_repo.root, "list", "-n", "3", "--json").json()) == 3


def test_get_latest_with_a_tag(cli, dev_repo):
    want = _put(cli, dev_repo, "r.md", "weekly-report")["id"]
    _put(cli, dev_repo, "newer.txt", "notes")
    g = cli(dev_repo.root, "get", "latest", "--tag", "weekly-report", "--json")
    assert g.code == 0, g.err
    assert g.json()["id"] == want and g.json()["tags"] == ["weekly-report"]
    assert cli(dev_repo.root, "get", "latest", "--tag", "weekly-report").code == 2   # acknowledged now


def test_get_tag_needs_latest(cli, dev_repo):
    fid = _put(cli, dev_repo, "r.md", "a")["id"]
    r = cli(dev_repo.root, "get", fid, "--tag", "a")
    assert r.code == 1 and "latest" in r.err


# ---------------------------------------------------------------- text output shows tags

def test_list_and_info_text_show_tags(cli, dev_repo):
    fid = _put(cli, dev_repo, "r.md", "weekly-report", "notes")["id"]
    other = _put(cli, dev_repo, "plain.txt")["id"]
    out = cli(dev_repo.root, "list").out
    lines = out.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(fid + " "))
    assert "tags: notes, weekly-report" in "\n".join(lines[i:i + 3])
    j = next(k for k, ln in enumerate(lines) if ln.startswith(other + " "))
    assert "tags:" not in lines[j] and (j + 1 >= len(lines) or "tags:" not in lines[j + 1])
    info = cli(dev_repo.root, "info", fid).out
    assert "tags     notes, weekly-report" in info
    assert "tags     —" in cli(dev_repo.root, "info", other).out
    assert cli(dev_repo.root, "info", fid, "--json").json()["tags"] == ["notes", "weekly-report"]


def test_tags_from_the_server_pass_through_clean(sharing):
    """Tags are clean ASCII by construction; anything else from a hostile server is still scrubbed."""
    assert sharing._tags_text(["ok", "bad\x1b[31m"]) == "ok, bad?[31m"
    assert sharing._tags_text([]) == "—"
    assert sharing._file_tags({"tags": ["a", 7, None, "b\x9b"]}) == ["a", "b"]
    assert sharing._file_tags({}) == [] and sharing._file_tags({"tags": "abc"}) == []


# ---------------------------------------------------------------- tag / untag

def test_tag_adds_to_the_existing_tags(cli, dev_repo, sim):
    fid = _put(cli, dev_repo, "r.md", "notes")["id"]
    r = cli(dev_repo.root, "tag", fid, "Weekly Report", "notes", "--json")
    assert r.code == 0, r.err
    assert r.json() == {"id": fid, "tags": ["notes", "weekly-report"]}
    assert _server_tags(sim, fid) == ["notes", "weekly-report"]
    h = cli(dev_repo.root, "tag", fid.lower(), "zeta")
    assert h.code == 0 and h.out.strip() == f"{fid}: notes, weekly-report, zeta"


def test_untag_removes_only_the_given_tags(cli, dev_repo, sim):
    fid = _put(cli, dev_repo, "r.md", "a", "b", "c")["id"]
    r = cli(dev_repo.root, "untag", fid, "b", "C", "not-there", "--json")
    assert r.code == 0, r.err
    assert r.json() == {"id": fid, "tags": ["a"]}
    assert _server_tags(sim, fid) == ["a"]
    assert cli(dev_repo.root, "untag", fid, "a").out.strip() == f"{fid}: —"
    assert _server_tags(sim, fid) == []


def test_tag_on_another_devices_file(cli, dev_repo, sim):
    fid = sim.upload("web.txt", b"x")["id"]          # a browser upload
    assert cli(dev_repo.root, "tag", fid, "notes").code == 0
    assert _server_tags(sim, fid) == ["notes"]


def test_tag_bad_or_too_many_exits_1(cli, dev_repo, sim):
    fid = _put(cli, dev_repo, "r.md", *[f"t{i}" for i in range(9)])["id"]
    assert cli(dev_repo.root, "tag", fid, "a/b").code == 1
    assert cli(dev_repo.root, "tag", fid, "x", "y").code == 1          # 11 in all
    assert len(_server_tags(sim, fid)) == 9
    assert cli(dev_repo.root, "tag", fid, "t0", "x").code == 0          # 10 in all
    assert cli(dev_repo.root, "tag", fid).code == 1                     # no tags given


def test_tag_missing_or_deleted_exits_2(cli, dev_repo, sim):
    fid = _put(cli, dev_repo, "r.md")["id"]
    assert cli(dev_repo.root, "tag", "FILE999", "a").code == 2
    sim.delete(fid)
    assert cli(dev_repo.root, "tag", fid, "a").code == 2
    assert cli(dev_repo.root, "untag", fid, "a").code == 2


# ---------------------------------------------------------------- tags

def test_tags_lists_counts(cli, dev_repo, sim):
    _put(cli, dev_repo, "a.md", "weekly-report", "notes")
    _put(cli, dev_repo, "b.md", "weekly-report")
    gone = _put(cli, dev_repo, "c.md", "gone")["id"]
    sim.delete(gone)
    r = cli(dev_repo.root, "tags", "--json")
    assert r.code == 0, r.err
    rows = r.json()
    assert [(x["tag"], x["count"]) for x in rows] == [("weekly-report", 2), ("notes", 1)]
    assert set(rows[0]) == {"tag", "count", "last_used"} and rows[0]["last_used"].endswith("Z")
    h = cli(dev_repo.root, "tags").out.splitlines()
    assert h[0].split() == ["TAG", "COUNT", "LAST", "USED"]
    assert h[1].split()[:2] == ["weekly-report", "2"] and h[2].split()[:2] == ["notes", "1"]


def test_tags_empty(cli, dev_repo):
    assert cli(dev_repo.root, "tags", "--json").json() == []
    assert cli(dev_repo.root, "tags").out.strip() == "no tags"


def test_list_json_shape_has_tags(cli, dev_repo, sim):
    sim.upload("w.txt", b"w", tags=["x"])
    rows = cli(dev_repo.root, "list", "--json").json()
    assert rows[0]["tags"] == ["x"]
    assert {"id", "name", "mime", "size", "note", "device", "project", "created_at", "deleted_at",
            "acked_at", "acked_by", "expires_at", "transcript", "tags"} <= set(rows[0])
