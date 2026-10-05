"""CLI side of the v2 features (spec §14): C acknowledge, E expiry, G transcripts."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def _acked_view(sim) -> dict:
    """Every file the server knows, acknowledged ones included, keyed by ID."""
    return {f["id"]: f for f in sim.http.get("/api/files?acked=1").json()["files"]}


def _default_view(sim) -> list:
    return [f["id"] for f in sim.http.get("/api/files").json()["files"]]


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ================================================================== C: acknowledge

def test_get_acknowledges_after_writing(cli, dev_repo, sim):
    fid = sim.upload("a.txt", b"a")["id"]
    g = cli(dev_repo.root, "get", fid, "--json")
    assert g.code == 0, g.err
    assert g.json()["acked"] is True
    f = _acked_view(sim)[fid]
    assert f["acked_at"] and f["acked_by"] == "dev-a"
    assert fid not in _default_view(sim)


def test_get_no_ack_leaves_the_file_unacknowledged(cli, dev_repo, sim):
    fid = sim.upload("a.txt", b"a")["id"]
    g = cli(dev_repo.root, "get", fid, "--no-ack", "--json")
    assert g.code == 0, g.err
    assert g.json()["acked"] is False
    assert _acked_view(sim)[fid]["acked_at"] is None
    assert fid in _default_view(sim)


def test_get_does_not_ack_when_the_write_fails(cli, dev_repo, sim, live_server, sharing):
    fid = sim.upload("t.txt", b"precious" * 100)["id"]
    p = live_server.blob_path(sim.get_file(fid)["uuid"])
    raw = bytearray(p.read_bytes())
    raw[sharing.HEADER_LEN + 3] ^= 0x01
    p.write_bytes(bytes(raw))
    assert cli(dev_repo.root, "get", fid).code == 5
    assert _acked_view(sim)[fid]["acked_at"] is None


@pytest.mark.parametrize("failure", [(0, "network"), (503, "http_503")])
def test_ack_failure_after_a_successful_write_warns_and_exits_0(cli, dev_repo, sim, sharing, monkeypatch, failure):
    fid = sim.upload("a.txt", b"payload")["id"]
    real = sharing.Api._req

    def flaky(self, method, path, *a, **kw):
        if path.endswith("/ack"):
            raise sharing.ApiError(failure[0], failure[1], "boom")
        return real(self, method, path, *a, **kw)

    monkeypatch.setattr(sharing.Api, "_req", flaky)
    g = cli(dev_repo.root, "get", fid, "--json")
    assert g.code == 0, g.err
    assert g.json()["acked"] is False
    assert Path(g.json()["path"]).read_bytes() == b"payload"
    assert "warning" in g.err and "acknowledge" in g.err
    h = cli(dev_repo.root, "get", fid)          # human output: the path still printed, warning on stderr
    assert h.code == 0 and "share" in h.out and "warning" in h.err


def test_ack_and_unack_commands(cli, dev_repo, sim):
    fid = sim.upload("a.txt", b"a")["id"]
    r = cli(dev_repo.root, "ack", fid.lower(), "--json")
    assert r.code == 0, r.err
    assert r.json() == {"id": fid, "acked": True}
    assert _acked_view(sim)[fid]["acked_by"] == "dev-a"
    assert cli(dev_repo.root, "ack", fid).code == 0          # idempotent
    r = cli(dev_repo.root, "unack", fid, "--json")
    assert r.code == 0 and r.json() == {"id": fid, "acked": False}
    assert _acked_view(sim)[fid]["acked_at"] is None
    assert "unacknowledged" in cli(dev_repo.root, "unack", fid).out


def test_ack_and_unack_exit_2_for_missing_or_deleted(cli, dev_repo, sim):
    fid = sim.upload("a.txt", b"a")["id"]
    sim.delete(fid)
    for cmd in ("ack", "unack"):
        r = cli(dev_repo.root, cmd, fid)
        assert r.code == 2 and "deleted" in r.err
        r = cli(dev_repo.root, cmd, "FILE999")
        assert r.code == 2 and "does not exist" in r.err
    assert cli(dev_repo.root, "ack", "abc").code == 1


def test_list_hides_acknowledged_unless_all(cli, dev_repo, sim):
    a = sim.upload("a.txt", b"a")["id"]
    b = sim.upload("b.txt", b"b")["id"]
    assert cli(dev_repo.root, "ack", a).code == 0
    assert [r["id"] for r in cli(dev_repo.root, "list", "--json").json()] == [b]
    rows = {r["id"]: r for r in cli(dev_repo.root, "list", "--all", "--json").json()}
    assert set(rows) == {a, b}
    assert rows[a]["acked_by"] == "dev-a" and rows[a]["acked_at"]
    assert rows[b]["acked_by"] is None and rows[b]["acked_at"] is None
    assert "expires_at" in rows[a]
    human = cli(dev_repo.root, "list", "--all").out
    line = next(ln for ln in human.splitlines() if ln.startswith(a + " "))
    assert "acked" in line and "dev-a" in line
    other = next(ln for ln in human.splitlines() if ln.startswith(b + " "))
    assert "acked" not in other


def test_list_all_shows_acknowledged_tombstones_too(cli, dev_repo, sim):
    a = sim.upload("a.txt", b"a")["id"]
    cli(dev_repo.root, "ack", a)
    sim.delete(a)
    assert cli(dev_repo.root, "list", "--json").json() == []
    assert [r["id"] for r in cli(dev_repo.root, "list", "--all", "--json").json()] == [a]


def test_latest_skips_acknowledged_files(cli, dev_repo, sim):
    one = sim.upload("one.txt", b"1")["id"]
    two = sim.upload("two.txt", b"2")["id"]
    g = cli(dev_repo.root, "get", "latest", "--json")
    assert g.json()["id"] == two
    g = cli(dev_repo.root, "get", "latest", "--json")
    assert g.json()["id"] == one                       # two was acknowledged by the first get
    assert cli(dev_repo.root, "get", "latest").code == 2   # everything acknowledged: nothing new
    g = cli(dev_repo.root, "get", "latest", "--all", "--force", "--json")
    assert g.code == 0 and g.json()["id"] == two


def test_get_help_explains_latest_and_ack(cli, tmp_path):
    r = cli(tmp_path, "get", "--help")
    assert r.code == 0
    text = " ".join(r.out.split())
    assert "--no-ack" in text and "unacknowledged" in text and "--all" in text


# ================================================================== E: expiry

@pytest.mark.parametrize("ttl,delta", [("1d", timedelta(days=1)), ("7d", timedelta(days=7)),
                                       ("30d", timedelta(days=30)), ("never", None), (None, timedelta(days=7))])
def test_share_ttl_sets_the_expiry(cli, dev_repo, sim, ttl, delta):
    (dev_repo.root / "x.txt").write_text("x")
    r = cli(dev_repo.root, "share", "x.txt", *(("--ttl", ttl) if ttl else ()), "--json")
    assert r.code == 0, r.err
    f = sim.get_file(r.json()["id"])
    if delta is None:
        assert f["expires_at"] is None and r.json()["expires_at"] is None
    else:
        assert _ts(f["expires_at"]) - _ts(f["created_at"]) == delta
        assert r.json()["expires_at"] == f["expires_at"]


@pytest.mark.parametrize("bad", ["2d", "7", "forever", "", "7D"])
def test_share_bad_ttl_is_a_usage_error(cli, dev_repo, sim, bad):
    (dev_repo.root / "x.txt").write_text("x")
    r = cli(dev_repo.root, "share", "x.txt", "--ttl", bad, "--json")
    assert r.code == 1 and r.json()["error"] == "usage"
    assert sim.http.get("/api/files").json()["files"] == []


@pytest.mark.parametrize("secs,want", [
    (None, "never"), (6 * 86400 + 5, "expires in 6d"), (86400, "expires in 1d"), (5 * 3600 + 1, "expires in 5h"),
    (59 * 60, "expires in 59m"), (30, "expires in 1m"), (0, "expired"), (-100, "expired"),
])
def test_expiry_text(sharing, secs, want):
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    at = None if secs is None else (now + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert sharing.expiry_text(at, now) == want


def test_info_and_list_show_the_expiry(cli, dev_repo, sim):
    (dev_repo.root / "x.txt").write_text("x")
    (dev_repo.root / "y.txt").write_text("y")
    x = cli(dev_repo.root, "share", "x.txt").out.strip()
    y = cli(dev_repo.root, "share", "y.txt", "--ttl", "never").out.strip()
    info = cli(dev_repo.root, "info", x).out
    assert "expires in 6d" in info
    assert "never" in cli(dev_repo.root, "info", y).out
    j = cli(dev_repo.root, "info", x, "--json").json()
    assert j["expires_at"] == sim.get_file(x)["expires_at"]
    human = cli(dev_repo.root, "list").out
    assert "expires in 6d" in next(ln for ln in human.splitlines() if ln.startswith(x + " "))
    assert "never" in next(ln for ln in human.splitlines() if ln.startswith(y + " "))


def test_ttl_command_changes_the_expiry(cli, dev_repo, sim):
    (dev_repo.root / "x.txt").write_text("x")
    fid = cli(dev_repo.root, "share", "x.txt").out.strip()
    r = cli(dev_repo.root, "ttl", fid, "never", "--json")
    assert r.code == 0, r.err
    assert r.json() == {"id": fid, "ttl": "never", "expires_at": None}
    assert sim.get_file(fid)["expires_at"] is None
    r = cli(dev_repo.root, "ttl", fid, "30d")
    assert r.code == 0 and "expires in 29d" in r.out
    left = _ts(sim.get_file(fid)["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)


def test_ttl_command_bad_value_and_missing_file(cli, dev_repo, sim):
    (dev_repo.root / "x.txt").write_text("x")
    fid = cli(dev_repo.root, "share", "x.txt").out.strip()
    assert cli(dev_repo.root, "ttl", fid, "2d").code == 1
    assert cli(dev_repo.root, "ttl", "FILE999", "1d").code == 2
    sim.delete(fid)
    assert cli(dev_repo.root, "ttl", fid, "1d").code == 2


def test_ttl_on_another_devices_file_exits_6(cli, make_device, sim):
    a, b = make_device("dev-a"), make_device("dev-b")
    (a.root / "x.txt").write_text("x")
    fid = cli(a.root, "share", "x.txt").out.strip()
    before = sim.get_file(fid)["expires_at"]
    r = cli(b.root, "ttl", fid, "never", "--json")
    assert r.code == 6, r.out + r.err
    assert r.json()["error"] == "forbidden"
    assert "only the device that shared" in r.json()["detail"]
    assert sim.get_file(fid)["expires_at"] == before
    browser = sim.upload("p.txt", b"p")["id"]
    assert cli(b.root, "ttl", browser, "1d").code == 6    # a browser upload: only the web UI may change it


# ================================================================== G: transcripts (spec §20: web-only)

AUDIO = b"ID3\x04" + b"x" * 3000


def _meta(sharing, sim, fid):
    return sharing.open_file_meta(sim.mk, sim.get_file(fid))[0]


def _set_transcript(sharing, sim, fid, **fields):
    """Inject a transcript directly into a file's encrypted meta, the way the browser does (§20) —
    this CLI can no longer create one itself."""
    up = sim.get_file(fid)
    meta, dek = sharing.open_file_meta(sim.mk, up)
    t = {"text": "one two three", "language": "en", "model": "nova-3",
         "created_at": "2026-01-01T00:00:00Z", "by": "browser/dev-a", **fields}
    meta["transcript"] = t
    enc = sharing.seal_file_meta(dek, bytes.fromhex(up["uuid"]), meta)
    assert sim.request("PATCH", f"/api/files/{fid}/meta", json={"enc_meta": enc}).status_code == 204
    return t


@pytest.mark.parametrize("meta,want", [
    ({"name": "a.mp3", "mime": "audio/mpeg"}, "audio/mpeg"),
    ({"name": "a.bin", "mime": "audio/ogg; codecs=opus"}, "audio/ogg"),
    ({"name": "a.m4a", "mime": "application/octet-stream"}, "audio/mp4"),
    ({"name": "a.opus", "mime": ""}, "audio/ogg"),
    ({"name": "a.webm", "mime": ""}, "audio/webm"),
    ({"name": "a.webm", "mime": "audio/webm"}, "audio/webm"),
    ({"name": "a.webm", "mime": "video/webm"}, None),
    ({"name": "a.webm", "mime": "application/octet-stream"}, None),
    ({"name": "a.mp4", "mime": "video/mp4"}, None),
    ({"name": "a.m4a", "mime": "video/mp4"}, None),
    ({"name": "a.txt", "mime": "text/plain"}, None),
])
def test_audio_type_follows_previewkind(sharing, meta, want):
    assert sharing.audio_type({"note": "", **meta}) == want


def test_seal_file_meta_roundtrip_and_new_file_crypto_uses_it(sharing):
    mk = os.urandom(32)
    fc = sharing.new_file_crypto(mk, "a.mp3", "audio/mpeg", "n")
    file_out = {"uuid": fc["uuid_hex"], "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"]}
    meta, dek = sharing.open_file_meta(mk, file_out)
    meta["transcript"] = {"text": "hi ü", "language": "de", "model": "nova-3", "created_at": "x", "by": "d"}
    enc = sharing.seal_file_meta(dek, fc["uuid"], meta)
    enc2 = sharing.seal_file_meta(dek, fc["uuid"], meta)
    assert enc != enc2                                                   # a fresh nonce every time
    again, _ = sharing.open_file_meta(mk, {**file_out, "enc_meta": enc})
    assert again == meta
    nonce = bytes(12)
    assert sharing.seal_file_meta(dek, fc["uuid"], {"b": 1, "a": "ü"}, _nonce=nonce) == sharing.b64u(
        sharing.seal(dek, '{"a":"ü","b":1}'.encode(), sharing.aad_meta(fc["uuid"]), nonce=nonce))


def test_transcribe_was_removed(cli, dev_repo, sim):
    fid = sim.upload("memo.mp3", AUDIO)["id"]
    for extra in ((), ("--json",)):
        r = cli(dev_repo.root, "transcribe", fid, *extra)
        assert r.code == 1, r.out + r.err
        assert "transcribe was removed" in r.out + r.err
        assert "web app" in r.out + r.err


def test_transcribe_with_no_args_is_still_removed(cli, dev_repo):
    r = cli(dev_repo.root, "transcribe")
    assert r.code == 1
    assert "transcribe was removed" in r.out + r.err


def test_info_shows_the_transcript_and_get_does_not_print_it(cli, dev_repo, sim, sharing):
    fid = sim.upload("memo.mp3", AUDIO)["id"]
    assert "transcript: no" in cli(dev_repo.root, "info", fid).out
    t = _set_transcript(sharing, sim, fid, text="one two three", language="en")
    assert "transcript: yes (en, 3 words)" in cli(dev_repo.root, "info", fid).out
    j = cli(dev_repo.root, "info", fid, "--json").json()
    assert j["transcript"] == {"language": "en", "model": "nova-3", "words": 3, "by": t["by"],
                               "created_at": t["created_at"]}
    g = cli(dev_repo.root, "get", fid, "--json")
    assert g.code == 0 and "one two three" not in g.out + g.err
    assert "transcript" not in cli(dev_repo.root, "info", sim.upload("n.txt", b"x")["id"]).out


@pytest.mark.parametrize("args", [("config", "deepgram-key"), ("config", "deepgram-key", "--clear"), ("config",)])
def test_config_deepgram_key_is_gone(cli, dev_repo, args):
    secret = "not-a-real-key-0123456789"
    before = dev_repo.config_path.read_bytes()
    r = cli(dev_repo.root, *args, stdin=(secret + "\n").encode())
    assert r.code == 1
    assert secret not in r.out + r.err
    assert dev_repo.config_path.read_bytes() == before


def test_help_no_longer_mentions_config(sharing):
    assert "config" not in sharing.build_parser().format_help()


def test_redacts_leftover_deepgram_key_and_env_var_on_failure(cli, dev_repo, sharing, monkeypatch):
    """§20: transcribe (and the settings key lookup) are gone, but old repos may still hold a
    deepgram_api_key in config.json, and a device may still have DEEPGRAM_API_KEY set. Both keep
    being redacted from every failure, even one raised before config.json is read."""
    legacy = "dgk_" + "Lg7" * 12
    data = json.loads(dev_repo.config_path.read_text())
    sharing.write_config(dev_repo.config_path, {**data, "deepgram_api_key": legacy})
    before = dev_repo.config_path.read_bytes()
    env_key = "0123456789abcdef0123456789abcdef01234567"   # the old 40-hex Deepgram key shape

    def boom(args):
        raise RuntimeError(f"kaboom {legacy} and {env_key} and {'f' * 40}")
    monkeypatch.setattr(sharing, "cmd_whoami", boom)
    monkeypatch.setattr(sharing, "_SECRETS", set())            # as in a fresh process
    monkeypatch.setenv("DEEPGRAM_API_KEY", env_key)
    for extra in ((), ("--json",)):
        r = cli(dev_repo.root, "whoami", *extra)
        assert r.code == 1 and "kaboom" in r.out + r.err
        assert legacy not in r.out + r.err
        assert env_key not in r.out + r.err
        assert "f" * 40 not in r.out + r.err
    assert dev_repo.config_path.read_bytes() == before          # the leftover key is never rewritten


def test_whoami_no_longer_shows_a_deepgram_line(cli, dev_repo):
    h = cli(dev_repo.root, "whoami")
    j = cli(dev_repo.root, "whoami", "--json")
    assert h.code == 0 and j.code == 0
    assert "deepgram" not in h.out
    assert "deepgram" not in j.json()


def test_get_again_reuses_an_identical_copy_and_reports_fresh_ack_metadata(cli, dev_repo, sim):
    """QA TF-19: no FILE7-name pile-up, no refusal on the third get, acked_at agrees with acked."""
    fid = sim.upload("a.txt", b"same bytes")["id"]
    first = cli(dev_repo.root, "get", fid, "--no-ack", "--json")
    assert first.code == 0, first.err
    second = cli(dev_repo.root, "get", fid, "--json")
    third = cli(dev_repo.root, "get", fid, "--json")
    assert second.code == third.code == 0, (second.err, third.err)
    assert second.json()["path"] == first.json()["path"] == third.json()["path"]
    assert second.json()["acked"] is True and second.json()["acked_at"]
    share = Path(first.json()["path"]).parent
    assert sorted(p.name for p in share.iterdir() if not p.name.startswith(".")) == ["a.txt"]


def test_get_again_with_different_content_still_keeps_both(cli, dev_repo, sim):
    fid = sim.upload("a.txt", b"one")["id"]
    p = cli(dev_repo.root, "get", fid, "--no-ack", "--json").json()["path"]
    Path(p).write_bytes(b"edited locally")                       # same name, other content: never overwritten
    again = cli(dev_repo.root, "get", fid, "--no-ack", "--json")
    assert again.code == 0 and Path(again.json()["path"]).name == f"{fid}-a.txt"
    assert Path(p).read_bytes() == b"edited locally"


def test_the_copy_comparison_hashes_in_chunks(sharing, tmp_path):
    """The reuse check hashes in chunks (a big file would otherwise be held in memory twice)."""
    import hashlib
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 * (1 << 20) + 5))
    assert sharing._sha256_of(big) == hashlib.sha256(big.read_bytes()).digest()
