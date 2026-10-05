"""R15 (F1): `sharing bridge-key` hands the host K_ws, never the master key (docs/bridge-protocol.md §2.2)."""
import json
from pathlib import Path

import pytest

VECTORS = json.loads((Path(__file__).parents[1] / "bridge_vectors.json").read_text())
FAKE_MK = bytes.fromhex(VECTORS["keys"]["mk"])


@pytest.fixture
def desk(make_device, cli):
    d = make_device("desk", "acme")
    assert cli(d.root, "space", "create", "--label", "Acme", "--json").code == 0
    return d


def _space(d, cli) -> str:
    return cli(d.root, "space", "show", "--json").json()["space_id"]


def _set_mk(d, sharing, mk: bytes):
    data = json.loads(d.config_path.read_text())
    data["mk"] = sharing.b64u(mk)
    d.config_path.write_text(json.dumps(data))
    d.config_path.chmod(0o600)


def _mk(d, sharing) -> bytes:
    return sharing.unb64u(json.loads(d.config_path.read_text())["mk"])


def test_matches_the_specification_vector(sharing):
    v = VECTORS["hkdf"]
    assert sharing.bridge_key(FAKE_MK, VECTORS["keys"]["workspace"]).hex() == v[0]["okm"]
    other = bytes.fromhex(v[1]["info"]).decode().split("|", 1)[1]
    assert sharing.bridge_key(bytes.fromhex(v[1]["ikm"]), other).hex() == v[1]["okm"]


def test_prints_only_the_key_and_never_the_master_key(desk, cli, sharing, caplog):
    _set_mk(desk, sharing, FAKE_MK)
    ws = _space(desk, cli)
    expected = sharing.bridge_key(FAKE_MK, ws).hex()
    with caplog.at_level("DEBUG"):
        plain = cli(desk.root, "bridge-key", "--workspace", ws)
        js = cli(desk.root, "bridge-key", "--workspace", ws, "--json")
    assert plain.code == 0 and plain.out == expected + "\n" and plain.err == ""
    assert js.json() == {"key": expected}
    seen = plain.out + plain.err + js.out + js.err + caplog.text
    assert FAKE_MK.hex() not in seen and sharing.b64u(FAKE_MK) not in seen


def test_refuses_a_terminal_unless_asked(desk, cli, sharing, monkeypatch):
    ws = _space(desk, cli)
    key = sharing.bridge_key(_mk(desk, sharing), ws).hex()
    monkeypatch.setattr(sharing, "_stdout_is_tty", lambda: True)
    r = cli(desk.root, "bridge-key", "--workspace", ws, "--json")
    assert r.code == 6 and "terminal" in r.json()["detail"]
    assert key not in r.out + r.err
    ok = cli(desk.root, "bridge-key", "--workspace", ws, "--allow-terminal")
    assert ok.code == 0 and ok.out.strip() == key


def test_refuses_an_unknown_space_and_bad_arguments(desk, cli):
    r = cli(desk.root, "bridge-key", "--workspace", "0" * 32, "--json")
    assert r.code == 2 and r.json()["error"] == "no_space"
    assert cli(desk.root, "bridge-key", "--workspace", "nope", "--json").code == 1
    assert cli(desk.root, "bridge-key", "--json").code == 1


def test_refuses_a_device_that_does_not_own_the_space(desk, make_device, cli, sharing):
    ws = _space(desk, cli)
    other = make_device("other", "acme")
    r = cli(other.root, "bridge-key", "--workspace", ws, "--json")
    assert r.code == 6 and r.json()["error"] == "not_owner"
    assert sharing.b64u(_mk(other, sharing)) not in r.out + r.err


def test_refuses_a_device_that_is_not_approved(make_device, cli):
    d = make_device("waiting", "acme", approve=False)
    r = cli(d.root, "bridge-key", "--workspace", "0" * 32, "--json")
    assert r.code == 3 and r.json()["error"] == "pending"
