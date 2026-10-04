import os

import pytest


@pytest.mark.parametrize("name,want", [
    ("notes.md", "notes.md"),
    ("ünïcode report.txt", "ünïcode report.txt"),
    (".hidden", ".hidden"),
    ("../../.ssh/authorized_keys", "authorized_keys"),
    ("..\\..\\evil.bat", "evil.bat"),
    ("/etc/passwd", "passwd"),
    ("dir/", "FILE7.bin"),
    ("-rf", "FILE7.bin"),
    ("..", "FILE7.bin"),
    (".", "FILE7.bin"),
    ("", "FILE7.bin"),
    ("a\x00b", "FILE7.bin"),
    ("line\nbreak", "FILE7.bin"),
    ("bell\x07", "FILE7.bin"),
    ("x" * 300, "FILE7.bin"),
    (None, "FILE7.bin"),
])
def test_safe_name(sharing, name, want):
    assert sharing.safe_name(name, "FILE7.bin") == want


def test_choose_target_prefers_plain_name_then_prefixed(sharing, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    assert sharing.choose_target(share, "FILE7", "a.md", False) == (share / "a.md").resolve()
    (share / "a.md").write_text("x")
    assert sharing.choose_target(share, "FILE7", "a.md", False) == (share / "FILE7-a.md").resolve()
    (share / "FILE7-a.md").write_text("x")
    with pytest.raises(sharing.Refused):
        sharing.choose_target(share, "FILE7", "a.md", False)
    assert sharing.choose_target(share, "FILE7", "a.md", True) == (share / "a.md").resolve()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_choose_target_treats_dangling_symlink_as_taken(sharing, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    os.symlink(tmp_path / "nowhere", share / "a.md")
    assert sharing.choose_target(share, "FILE7", "a.md", False).name == "FILE7-a.md"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_choose_target_stays_inside_a_symlinked_share_dir(sharing, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "repo").mkdir()
    os.symlink(real, tmp_path / "repo" / "share")
    t = sharing.choose_target(tmp_path / "repo" / "share", "FILE7", "a.md", False)
    assert t.parent == real.resolve()


def test_choose_target_refuses_force_over_a_directory(sharing, tmp_path):
    (tmp_path / "share" / "a.md").mkdir(parents=True)
    with pytest.raises(sharing.Refused):
        sharing.choose_target(tmp_path / "share", "FILE7", "a.md", True)


@pytest.mark.parametrize("name", [".env", ".env.local", ".envrc", "prod.pem", "server.key", "id_rsa",
                                  "id_ed25519.pub", "cert.p12", ".netrc", "credentials.json", "CREDENTIALS",
                                  "Server.KEY"])
def test_is_secret_path_flags_secret_names(sharing, tmp_path, name):
    assert sharing.is_secret_path(tmp_path / name, tmp_path)


@pytest.mark.parametrize("name", ["notes.md", "env.md", "keynote.txt", "keys.md", "README"])
def test_is_secret_path_allows_normal_names(sharing, tmp_path, name):
    assert not sharing.is_secret_path(tmp_path / name, tmp_path)


def test_is_secret_path_protects_the_whole_skill_dir(sharing, tmp_path):
    skill = tmp_path / ".claude" / "skills" / "sharing"
    for rel in ("SKILL.md", "config.json", "config.json.old", "sharing.py", ".gitignore"):
        assert sharing.is_secret_path(skill / rel, tmp_path)


def test_is_secret_path_skill_dir_by_other_case_or_symlink(sharing, tmp_path):
    skill = tmp_path / ".claude" / "skills" / "sharing"
    skill.mkdir(parents=True)
    (skill / "config.json.old").write_text("{}")
    other_case = tmp_path / ".CLAUDE" / "Skills" / "Sharing" / "config.json.old"
    if other_case.exists():   # a case-insensitive disk (macOS, Windows default)
        assert sharing.is_secret_path(other_case, tmp_path)
    if os.name != "nt":
        link = tmp_path / "alias"
        os.symlink(skill, link)
        assert sharing.is_secret_path(link / "config.json.old", tmp_path)
    assert not sharing.is_secret_path(tmp_path / "notes.md", tmp_path)
