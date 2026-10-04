from pathlib import Path

from fileshare.settings import DEFAULT_SKILL_DIR, Settings


def test_from_env_defaults():
    s = Settings.from_env({})
    assert s.data_dir == Path("/var/lib/fileshare")
    assert s.public_url == "https://tix.severin.io"
    assert s.max_upload == 209_715_200
    assert s.skill_dir == DEFAULT_SKILL_DIR
    assert s.cookie_secure is True
    assert s.db_path == Path("/var/lib/fileshare/fileshare.db")


def test_from_env_overrides_and_strips_trailing_slash(tmp_path):
    s = Settings.from_env({
        "FS_DATA_DIR": str(tmp_path),
        "FS_PUBLIC_URL": "http://127.0.0.1:9999/",
        "FS_MAX_UPLOAD": "1024",
        "FS_SKILL_DIR": str(tmp_path / "skill"),
        "FS_COOKIE_SECURE": "0",
    })
    assert s.data_dir == tmp_path
    assert s.public_url == "http://127.0.0.1:9999"
    assert s.max_upload == 1024
    assert s.skill_dir == tmp_path / "skill"
    assert s.cookie_secure is False


def test_cookie_secure_false_spellings():
    for v in ("0", "false", "False", "no", "off"):
        assert Settings.from_env({"FS_COOKIE_SECURE": v}).cookie_secure is False
    assert Settings.from_env({"FS_COOKIE_SECURE": "1"}).cookie_secure is True
