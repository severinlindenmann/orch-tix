import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "fileshare" / "templates" / "onboarding.sh"
WRAPPER = REPO / "skill" / "sharing" / "sharing"
GOOD_SHAPE = "shr1." + "A" * 22
OLD_SHAPE = "shr1." + "A" * 22 + "." + "B" * 43

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash installer; Windows uses onboarding.ps1 (Task 23)")


def render(url: str = "https://tix.example.invalid") -> str:
    return TEMPLATE.read_text().replace("{{PUBLIC_URL}}", url)


@pytest.fixture
def script(tmp_path) -> Path:
    p = tmp_path / "onboarding.sh"
    p.write_text(render())
    return p


def run(script: Path, cwd: Path, *args: str, server: str = "http://127.0.0.1:9"):
    env = {**os.environ, "SHARING_SERVER": server}
    return subprocess.run(["bash", str(script), *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def test_template_is_valid_bash(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_nothing_runs_until_the_last_line():
    lines = [l for l in render().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert lines[-1] == 'main "$@"'


def test_placeholder_is_the_only_templating():
    text = TEMPLATE.read_text()
    assert "{{PUBLIC_URL}}" in text
    assert "{{" not in render()


def test_code_is_never_echoed_or_traced():
    allowed = ('code="$1"', '[ -n "$code" ]', '[[ "$code" =~', "printf '%s' \"$code\" |")
    for line in render().splitlines():
        if "$code" in line or "${code}" in line:
            assert any(a in line for a in allowed), line
    assert "set -x" not in render()


def test_preflight_checks_ecdh_too():
    text = render()
    assert "from cryptography.hazmat.primitives.asymmetric import ec" in text
    assert "ec.generate_private_key(ec.SECP256R1())" in text


def test_skill_folder_is_made_self_ignoring_before_the_handshake():
    text = render()
    assert text.index("printf '*\\n' > \"$dest/.gitignore\"") < text.index("_handshake")


@pytest.mark.parametrize("bogus", ["shr1.not-a-real-code-ZZZZZZZZZZ", OLD_SHAPE])
def test_bad_code_rejected_without_printing_it(script, tmp_path, bogus):
    r = run(script, tmp_path, bogus)
    assert r.returncode == 1
    assert bogus not in r.stdout + r.stderr
    assert "not a valid onboarding code" in r.stderr


def test_help(script, tmp_path):
    r = run(script, tmp_path, "--help")
    assert r.returncode == 0 and "--device" in r.stderr


def test_refuses_plain_http_remote(script, tmp_path):
    r = run(script, tmp_path, GOOD_SHAPE, server="http://example.com")
    assert r.returncode == 1 and "only https" in r.stderr
    assert GOOD_SHAPE not in r.stdout + r.stderr


def test_refuses_outside_git_without_project(script, tmp_path):
    work = tmp_path / "plain"
    work.mkdir()
    r = run(script, work, GOOD_SHAPE)
    assert r.returncode == 6 and "git repository" in r.stderr   # a local guard, as in onboarding.ps1


def test_refuses_existing_identity_before_using_the_code(script, tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".claude" / "skills" / "sharing").mkdir(parents=True)
    (repo / ".claude" / "skills" / "sharing" / "config.json").write_text("{}")
    r = run(script, repo, GOOD_SHAPE)
    assert r.returncode == 6 and "already onboarded" in r.stderr and "--force" in r.stderr


def test_refuses_when_git_tracks_the_skill_folder(script, tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    skill = repo / ".claude" / "skills" / "sharing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".claude/skills/sharing/SKILL.md"], check=True)
    r = run(script, repo, GOOD_SHAPE, "--force")
    assert r.returncode == 6 and "tracked by git" in r.stderr


def test_wrapper_is_executable_valid_bash():
    assert os.access(WRAPPER, os.X_OK)
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


# ---- rulings R2 / R3

def _tracked_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    skill = repo / ".claude" / "skills" / "sharing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".claude/skills/sharing/SKILL.md"], check=True)
    return repo


def test_tracked_skill_folder_message_names_the_fix(script, tmp_path):
    r = run(script, _tracked_repo(tmp_path), GOOD_SHAPE)
    assert r.returncode == 6
    assert ("files under .claude/skills/sharing are tracked by git — the device key would be committed. "
            "Run: git rm -r --cached .claude/skills/sharing") in r.stderr


SKILL_FILES = ("SKILL.md", "tickets-SKILL.md", "sharing.py", "sharing")


def _stub_path(tmp_path: Path, handshake_rc: int, uv_rc: int = 0, manifest=None, write_config=False) -> str:
    """No network: curl 'downloads' a stub wrapper whose _handshake exits handshake_rc, and a manifest
    that lists the stub's sha256 for every skill file unless `manifest` (a callable on it) edits it.
    With write_config, the stub's _handshake leaves a config.json beside itself, as a real one does."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    stub = tmp_path / "stub-sharing"
    cfg = 'echo {} > "$(dirname "$0")/config.json"; ' if write_config else ""
    stub.write_text(f'#!/bin/sh\nif [ "$1" = _handshake ]; then cat >/dev/null; {cfg}exit {handshake_rc}; fi\nexit 0\n')
    digest = hashlib.sha256(stub.read_bytes()).hexdigest()
    body = {"version": "1.0.0", "files": {f: digest for f in (*SKILL_FILES, "sharing.cmd")}}
    if manifest is not None:
        body = manifest(body)
    man = tmp_path / "stub-manifest.json"
    man.write_text(json.dumps(body, separators=(",", ":")))
    (bin_ / "uv").write_text(f"#!/bin/sh\nexit {uv_rc}\n")
    (bin_ / "curl").write_text(
        '#!/bin/sh\nout=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift; done\n'
        f'case "$url" in */skill/manifest.json) src="{man}" ;; *) src="{stub}" ;; esac\n'
        '[ -n "$out" ] && cp "$src" "$out"\nexit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    return f"{bin_}{os.pathsep}{os.environ['PATH']}"


def _run_stubbed(script: Path, repo: Path, path: str):
    env = {**os.environ, "SHARING_SERVER": "http://127.0.0.1:9", "PATH": path}
    return subprocess.run(["bash", str(script), GOOD_SHAPE, "--device", "d", "--project", "p"], cwd=repo, env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


@pytest.mark.parametrize("rc", [0, 3, 5])
def test_installer_propagates_the_handshake_exit_code(script, tmp_path, rc):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {**os.environ, "SHARING_SERVER": "http://127.0.0.1:9", "PATH": _stub_path(tmp_path, rc)}
    r = subprocess.run(["bash", str(script), GOOD_SHAPE, "--device", "d", "--project", "p"], cwd=repo, env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == rc, r.stderr
    assert GOOD_SHAPE not in r.stdout + r.stderr
    if rc:
        assert "onboarding did not complete" in r.stderr
        # the stub wrote no config.json: the folder this run created is removed as a partial install
        assert not (repo / ".claude" / "skills" / "sharing").exists()


def _tamper(body):
    body["files"]["sharing.py"] = "0" * 64
    return body


def _drop_skill_md(body):
    del body["files"]["SKILL.md"]
    return body


@pytest.mark.parametrize("edit,why", [(_tamper, "sha256 mismatch"), (_drop_skill_md, "does not list SKILL.md")],
                         ids=["mismatch", "missing-entry"])
def test_skill_files_are_verified_against_the_manifest_before_install(script, tmp_path, edit, why):
    repo = _git_repo(tmp_path)
    r = _run_stubbed(script, repo, _stub_path(tmp_path, 0, manifest=edit))
    assert r.returncode == 5, r.stderr
    assert why in r.stderr and "code was not used" in r.stderr
    assert not (repo / ".claude" / "skills" / "sharing").exists()   # nothing moved into place
    assert GOOD_SHAPE not in r.stdout + r.stderr


def test_every_skill_file_is_verified_and_installed(script, tmp_path):
    """tickets-SKILL.md (spec T8) goes through the same manifest check and lands beside the others."""
    repo = _git_repo(tmp_path)
    r = _run_stubbed(script, repo, _stub_path(tmp_path, 0, write_config=True))
    assert r.returncode == 0, r.stderr
    dest = repo / ".claude" / "skills" / "sharing"
    for f in SKILL_FILES:
        assert (dest / f).is_file(), f
    assert (dest / "tickets-SKILL.md").stat().st_mode & 0o777 == 0o644


def test_a_missing_tickets_skill_entry_fails_closed(script, tmp_path):
    def drop(body):
        del body["files"]["tickets-SKILL.md"]
        return body
    repo = _git_repo(tmp_path)
    r = _run_stubbed(script, repo, _stub_path(tmp_path, 0, manifest=drop))
    assert r.returncode == 5 and "does not list tickets-SKILL.md" in r.stderr
    assert not (repo / ".claude" / "skills" / "sharing").exists()


def test_installer_file_lists_match_the_server_list():
    from fileshare.routes.onboarding import SKILL_FILES as SERVED
    text = render()
    lists = [line for line in text.splitlines() if line.strip().startswith("for f in ")]
    assert len(lists) == 2
    for line in lists:   # the download/verify loop and the move loop; sharing.cmd is Windows only
        names = line.strip().removeprefix("for f in ").split(";")[0].split()
        assert set(names) == set(SERVED) - {"sharing.cmd"}, line


def test_manifest_check_runs_before_files_move_into_place():
    text = render()
    assert "/skill/manifest.json" in text
    assert text.index("sha256 mismatch") < text.index('mv -f "$SHARING_TMP/$f" "$dest/$f"')
    assert text.index("sha256 mismatch") < text.index("_handshake")


def test_no_sha256_tool_fails_closed(script, tmp_path):
    """With neither sha256sum nor shasum on PATH, the installer refuses rather than skip the check."""
    repo = _git_repo(tmp_path)
    stub_bin = _stub_path(tmp_path, 0).split(os.pathsep)[0]
    only = tmp_path / "only"
    only.mkdir()
    for d in [stub_bin, *os.environ["PATH"].split(os.pathsep)]:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            src = os.path.join(d, name)
            if name in ("sha256sum", "shasum") or (only / name).exists() or not os.access(src, os.X_OK):
                continue
            (only / name).symlink_to(src)
    r = _run_stubbed(script, repo, str(only))
    assert r.returncode == 1, r.stderr
    assert "sha256sum or shasum" in r.stderr
    assert not (repo / ".claude" / "skills" / "sharing").exists()


def test_missing_cryptography_is_exit_5_like_onboarding_ps1(script, tmp_path):
    r = _run_stubbed(script, _git_repo(tmp_path), _stub_path(tmp_path, 0, uv_rc=1))
    assert r.returncode == 5 and "cryptography" in r.stderr
