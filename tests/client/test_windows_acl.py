"""The Windows branch of the §8.2 permission guard, against real icacls (Task 13's code, verified here)."""
import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real icacls ACLs exist only on Windows")

EVERYONE = "*S-1-1-0"   # SID form, so the test is locale-independent ("Everyone" is "Jeder" on German Windows)


def _new(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"v": 1}', encoding="utf-8")
    return p


def test_fresh_file_with_inherited_acl_is_refused(sharing, tmp_path):
    # A new file under %TEMP% inherits SYSTEM and Administrators ACEs: not owner-only.
    with pytest.raises(sharing.Refused):
        sharing.check_permissions(_new(tmp_path))


def test_secure_file_then_check_passes(sharing, tmp_path):
    p = _new(tmp_path)
    sharing.secure_file(p)
    sharing.check_permissions(p)


def test_everyone_read_is_refused(sharing, tmp_path):
    p = _new(tmp_path)
    sharing.secure_file(p)
    subprocess.run(["icacls", str(p), "/grant", f"{EVERYONE}:(R)"], check=True, capture_output=True)
    with pytest.raises(sharing.Refused):
        sharing.check_permissions(p)


def test_write_config_result_is_owner_only(sharing, tmp_path):
    p = tmp_path / "config.json"
    sharing.write_config(p, {"v": 1, "device_id": "dev_000000000000"})
    sharing.check_permissions(p)
    assert json.loads(p.read_text(encoding="utf-8"))["device_id"] == "dev_000000000000"
