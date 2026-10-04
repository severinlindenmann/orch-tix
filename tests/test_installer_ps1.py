"""Static checks on the PowerShell installer and the Windows wrapper. They run on every OS; the
real parser checks at the bottom run wherever pwsh / powershell.exe exist (always on the Windows runner)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "fileshare" / "templates" / "onboarding.ps1"
CMD = ROOT / "skill" / "sharing" / "sharing.cmd"

# Every line that mentions $Code (PowerShell names are case-insensitive) must be one of these.
# Anything else, such as Write-Host $Code or Set-Content … $Code, is a leak.
CODE_LINE_ALLOW = [
    re.compile(r"^\s*\[Parameter\(Position = 0\)\]\[string\]\$Code = '',$"),
    re.compile(r"^\s*if \(-not \$Code\) \{"),
    re.compile(r"^\s*if \(\$Code -notmatch '"),
    re.compile(r"^\s*try \{ \$Code \| & uv @hs; \$hsExit = \$LASTEXITCODE \}"),
]


@pytest.fixture
def script(client):
    r = client.get("/onboarding.ps1")
    assert r.status_code == 200
    return r


def test_served_as_text_with_public_url(script, settings):
    assert script.headers["content-type"].startswith("text/plain")
    assert "{{PUBLIC_URL}}" not in script.text
    assert f"$server = '{settings.public_url}'" in script.text


def test_ascii_only_so_windows_powershell_5_decodes_it_correctly():
    # irm on 5.1 may decode a charset-less body as Latin-1; ASCII survives every decoder.
    TEMPLATE.read_bytes().decode("ascii")


def test_strict_mode_tls_and_quiet_progress():
    t = TEMPLATE.read_text()
    for needle in ("$ErrorActionPreference = 'Stop'",
                   "Set-StrictMode -Version 3",
                   "[Net.SecurityProtocolType]::Tls12",
                   "$ProgressPreference = 'SilentlyContinue'"):
        assert needle in t, needle


def test_code_regex_is_the_contract_regex():
    assert r"'^shr1\.[A-Za-z0-9_-]{22}$'" in TEMPLATE.read_text()


def test_code_is_never_echoed_or_written():
    lines = [ln for ln in TEMPLATE.read_text().splitlines() if re.search(r"(?i)\$code\b", ln)]
    assert lines, "the template must reference $Code"
    for line in lines:
        assert any(p.search(line) for p in CODE_LINE_ALLOW), f"unexpected use of $Code: {line.strip()}"
    assert sum("$Code | & uv" in ln for ln in lines) == 1


def test_code_travels_on_stdin_to_handshake_dash():
    t = TEMPLATE.read_text()
    assert "'_handshake', '--code', '-'," in t


def test_no_powershell7_only_syntax():
    t = TEMPLATE.read_text()
    for pattern, what in [
        (r"\?\?", "null-coalescing ??"),
        (r"\?\.[A-Za-z_\[]", "null-conditional ?."),
        (r"&&|\|\|", "pipeline chain && / ||"),
        (r"\s\?\s[^\n:]*\s:\s", "ternary ? :"),
        (r"-Parallel\b", "ForEach-Object -Parallel"),
    ]:
        assert not re.search(pattern, t), f"PowerShell 7-only syntax: {what}"


def test_never_calls_exit():
    # The one-liner runs inside the user's session: `exit` would close their window.
    assert not re.search(r"(?im)^\s*exit\b", TEMPLATE.read_text())
    assert not re.search(r"(?i)[;{]\s*exit\b(?!\s*=)", TEMPLATE.read_text())   # `Exit = $rc` is a hash key


def test_fail_sets_lastexitcode_before_throwing():
    # `throw` makes powershell.exe -Command exit 1 whatever happened; $LASTEXITCODE carries the
    # bash installer's code to the caller's session, and is never stale from an earlier command.
    t = TEMPLATE.read_text()
    fail = t[t.index("function Fail("):t.index("function Invoke-Native")]
    assert "[int]$ExitCode = 1" in fail
    assert fail.index("$global:LASTEXITCODE = $ExitCode") < fail.index("throw \"sharing: ")


def _fail_line(t: str, needle: str) -> str:
    lines = [ln for ln in t.splitlines() if "Fail " in ln and needle in ln]
    assert len(lines) == 1, needle
    return lines[0].rstrip()


@pytest.mark.parametrize("needle, code", [
    ("inside a git repository", "6"),                       # local guards
    ("already onboarded", "6"),
    ("could not ask git whether", "6"),
    ("git tracks files under", "6"),
    ("could not load 'cryptography'", "5"),                 # integrity
    ("sha256 mismatch", "5"),
    ("manifest does not list", "5"),
    ("not approved yet", "$hsExit"),                        # the handshake's own code
    ("onboarding failed (exit", "$hsExit"),
    ("'sharing whoami' failed", "$whoamiExit"),
])
def test_each_fail_passes_the_bash_exit_code(needle, code):
    line = _fail_line(TEMPLATE.read_text(), needle)
    assert re.search(r"[\"')]\s+" + re.escape(code) + r"(\s*\})?$", line), line


def test_uv_installer_child_enables_tls12_itself():
    # The child powershell.exe does not inherit this session's SecurityProtocol; stock 5.1 would
    # fail with "Could not create SSL/TLS secure channel".
    t = TEMPLATE.read_text()
    line = next(ln for ln in t.splitlines() if "irm https://astral.sh/uv/install.ps1 | iex" in ln)
    prefix = "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
    assert "'" + prefix + "irm https://astral.sh/uv/install.ps1 | iex'" in line


def test_session_path_is_restored_after_the_run():
    # Update-SessionPath prepends user-writable dirs to find uv; that must not outlive the script.
    t = TEMPLATE.read_text()
    tail = t[t.rindex("Invoke-SharingInstall") - 200:]
    assert tail.index("$savedPath = $env:Path") < tail.index("Invoke-SharingInstall")
    fin = tail[tail.index("} finally {"):]
    assert "$env:Path = $savedPath" in fin


def test_root_uses_the_filesystem_provider_path():
    # .Path of a PSDrive / UNC location is not a path git or .NET can use.
    t = TEMPLATE.read_text()
    assert "(Get-Location).ProviderPath" in t and "(Get-Location).Path" not in t


def test_gitignore_files_are_written_without_a_bom():
    # Set-Content -Encoding UTF8 on 5.1 writes a BOM; "\ufeff*" would no longer match anything.
    t = TEMPLATE.read_text()
    assert "UTF8Encoding($false)" in t
    assert "Set-Content" not in t and "Out-File" not in t
    assert t.count('"*`n"') == 2   # both .gitignore files: '*' + LF


def test_preflight_runs_before_the_code_is_spent():
    t = TEMPLATE.read_text()
    spend = t.index("'_handshake'")
    for marker in ("'rev-parse'", "'ls-files'", "already onboarded", "cryptography>=43", "/skill/manifest.json",
                   "Get-FileHash"):
        assert t.index(marker) < spend, marker


def test_uv_is_installed_only_with_consent():
    t = TEMPLATE.read_text()
    assert "irm https://astral.sh/uv/install.ps1 | iex" in t
    assert t.index("Confirm-Yes \"uv") < t.index("irm https://astral.sh/uv/install.ps1 | iex")


def test_http_only_for_loopback():
    assert r"'^http://(127\.0\.0\.1|localhost)(:\d+)?$'" in TEMPLATE.read_text()


def test_downloads_are_verified_against_the_manifest():
    t = TEMPLATE.read_text()
    assert "Get-FileHash -Algorithm SHA256" in t
    assert "$SkillFiles = @('SKILL.md', 'tickets-SKILL.md', 'sharing.py', 'sharing.cmd', 'sharing')" in t
    # the loop that downloads is the loop that hashes: nothing is installed unverified
    loop = t[t.index("foreach ($f in $SkillFiles)"):t.index("# 4. Stage")]
    assert "Invoke-WebRequest" in loop and "Get-FileHash" in loop and "Fail" in loop


def test_skill_files_match_the_server_list():
    from fileshare.routes.onboarding import SKILL_FILES
    m = re.search(r"\$SkillFiles = @\(([^)]*)\)", TEMPLATE.read_text())
    assert set(re.findall(r"'([^']+)'", m.group(1))) == set(SKILL_FILES)


def test_sharing_cmd_is_one_crlf_line_and_safe_to_replace_while_running():
    # cmd.exe re-reads a batch file line by line; `sharing update` swaps sharing.cmd under a running
    # copy. One parsed line whose last command is `exit /b` never reads past the uv call.
    assert CMD.read_bytes() == b'@uv run --quiet --script "%~dp0sharing.py" %* & exit /b\r\n'


def test_wrapper_line_endings_are_pinned():
    r = subprocess.run(["git", "-C", str(ROOT), "check-attr", "eol", "--",
                        "skill/sharing/sharing.cmd", "skill/sharing/sharing"],
                       capture_output=True, text=True, check=True)
    assert "skill/sharing/sharing.cmd: eol: crlf" in r.stdout
    assert "skill/sharing/sharing: eol: lf" in r.stdout


PARSE = ("$errs = $null; "
         "$null = [System.Management.Automation.Language.Parser]::ParseFile("
         "$env:PS1_UNDER_TEST, [ref]$null, [ref]$errs); "
         "if ($errs.Count) { $errs | ForEach-Object { $_.ToString() }; exit 1 }")


@pytest.mark.parametrize("shell", ["pwsh", "powershell.exe"])
def test_parses_without_errors(shell, script, tmp_path):
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    f = tmp_path / "onboarding.ps1"
    f.write_bytes(script.content)
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", PARSE],
                       env={**os.environ, "PS1_UNDER_TEST": str(f)},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
