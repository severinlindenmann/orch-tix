"""Static checks on infra/ — each one pins a lesson from the old deployment.

These run in the default suite: no VPS, no network, no root. They catch the
config mistakes that previously only showed up as outages on the box.
"""
import configparser
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from fileshare.security import RateLimiter

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
SCRIPTS = ["sync.sh", "prepare-vps.sh"]


def _unit(name: str) -> str:
    return (INFRA / name).read_text()


def _directive(unit: str, key: str) -> list[str]:
    return re.findall(rf"^{re.escape(key)}=(.*)$", unit, re.M)


# ---------------------------------------------------------------- systemd

def test_service_restarts_on_failure():
    # A unit with Restart=no turned one crash into a permanent outage.
    assert _directive(_unit("fileshare.service"), "Restart") == ["on-failure"]


def test_service_uses_factory_single_worker_loopback():
    unit = _unit("fileshare.service")
    exec_start = " ".join(
        line.rstrip("\\").strip()
        for line in unit.split("ExecStart=", 1)[1].splitlines()[:4]
    )
    assert "--factory fileshare.app:create_app" in exec_start
    assert "--workers 1" in exec_start          # SQLite: exactly one worker
    assert "--host 127.0.0.1" in exec_start     # X-Real-IP is trusted only because of this
    assert "--port 8808" in exec_start
    assert "--no-proxy-headers" in exec_start   # X-Real-IP is the single client-IP source
    assert "--frozen" in exec_start and "--no-dev" in exec_start


def test_service_writable_paths_cover_data_and_uv():
    unit = _unit("fileshare.service")
    (rw,) = _directive(unit, "ReadWritePaths")
    paths = {p.lstrip("-") for p in rw.split()}
    # ProtectSystem=strict makes everything else read-only; a missing entry here
    # means uploads (or uv) fail at runtime, not at install time.
    assert {"/var/lib/fileshare", "/opt/fileshare/.venv",
            "/opt/fileshare/.cache", "/opt/fileshare/.local"} <= paths
    assert _directive(unit, "ProtectSystem") == ["strict"]
    assert _directive(unit, "NoNewPrivileges") == ["true"]
    assert _directive(unit, "UMask") == ["0027"]
    assert _directive(unit, "User") == ["fileshare"]
    assert _directive(unit, "EnvironmentFile") == ["/etc/fileshare/fileshare.env"]


def test_service_spools_uploads_to_disk_not_tmpfs():
    # Starlette spools multipart bodies to TMPDIR. /tmp may be tmpfs, which would
    # hold a 200 MiB upload in RAM; point TMPDIR at a disk-backed writable dir.
    unit = _unit("fileshare.service")
    assert _directive(unit, "PrivateTmp") == ["yes"]
    assert "TMPDIR=/var/lib/fileshare/spool" in _directive(unit, "Environment")
    (rw,) = _directive(unit, "ReadWritePaths")
    assert any("/var/lib/fileshare/spool".startswith(p.lstrip("-")) for p in rw.split())
    # The dir must exist before Python resolves tempfile.gettempdir(), or it
    # silently falls back to /tmp -- also after a restore replaced the data dir.
    assert any("/var/lib/fileshare/spool" in pre for pre in _directive(unit, "ExecStartPre"))


def test_no_backup_units_remain():
    # Backups were dropped by decision; prepare-vps.sh removes what an older install left.
    assert not list(INFRA.glob("*backup*"))
    text = (INFRA / "prepare-vps.sh").read_text()
    assert "systemctl disable --now fileshare-backup.timer" in text
    assert "FS_BACKUP" not in text


# ---------------------------------------------------------------- fail2ban

def _filter() -> configparser.SectionProxy:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(INFRA / "fail2ban" / "filter.d" / "fileshare.conf")
    return cp["Definition"]


def _failregex() -> re.Pattern:
    rx = _filter()["failregex"].strip()
    assert not rx.startswith("^"), "anchored regexes match nothing on the systemd backend"
    return re.compile(rx.replace("<HOST>", r"(?P<host>\S+)"))


def test_fail2ban_matches_the_exact_rate_limiter_line(caplog):
    limiter = RateLimiter(limit=10, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        limiter.fail("203.0.113.9")
    msg = caplog.records[-1].getMessage()
    assert msg == "auth: failed attempt 1/10 from 203.0.113.9"
    # What the systemd backend actually hands the filter: timestamp, host,
    # "<identifier>[<pid>]:" all precede the message.
    rendered = f"2026-09-24T03:15:00+02:00 vps-1 uv[4242]: {msg}"
    m = _failregex().search(rendered)
    assert m is not None and m.group("host") == "203.0.113.9"


@pytest.mark.parametrize("line", [
    "2026-09-24T03:15:00+02:00 vps-1 uv[4242]: auth: login ok from 203.0.113.9",
    "2026-09-24T03:15:00+02:00 vps-1 uv[4242]: GET /healthz 200",
])
def test_fail2ban_ignores_other_lines(line):
    assert _failregex().search(line) is None


def test_fail2ban_matches_a_real_ipv6_line(caplog):
    limiter = RateLimiter(limit=10, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        limiter.fail("2001:db8::7")
    msg = caplog.records[-1].getMessage()
    m = _failregex().search(f"2026-09-24T03:15:00+02:00 vps-1 uv[4242]: {msg}")
    assert m is not None and m.group("host") == "2001:db8::7"


def test_fail2ban_ignoreregex_skips_unknown_client(caplog):
    # client_ip() yields "unknown" when neither X-Real-IP nor the peer is a valid
    # IP. That is not bannable; ignoring it also spares fail2ban a DNS lookup.
    limiter = RateLimiter(limit=10, window_s=300)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        limiter.fail("unknown")
    msg = caplog.records[-1].getMessage()
    ignore = re.compile(_filter()["ignoreregex"].strip())
    assert ignore.search(f"2026-09-24T03:15:00+02:00 vps-1 uv[4242]: {msg}")
    assert not ignore.search(
        "2026-09-24T03:15:00+02:00 vps-1 uv[4242]: "
        "auth: failed attempt 1/10 from 203.0.113.9")


def test_fail2ban_journalmatch_and_jail():
    assert _filter()["journalmatch"].strip() == "_SYSTEMD_UNIT=fileshare.service"
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(INFRA / "fail2ban" / "jail.d" / "fileshare.conf")
    jail = cp["fileshare"]
    assert jail["enabled"].strip() == "true"
    assert jail["backend"].strip() == "systemd"
    assert jail["filter"].strip() == "fileshare"


# ---------------------------------------------------------------- caddy

def _caddy_code() -> list[str]:
    lines = []
    for raw in (INFRA / "Caddyfile.fileshare").read_text().splitlines():
        code = raw.split("#", 1)[0].strip()
        if code:
            lines.append(code)
    return lines


def test_caddyfile_has_no_log_directive():
    # Hardened Caddy on this box cannot write /var/log/caddy; a log block breaks
    # the reload for every site on the box.
    assert not any(re.match(r"^log\b", line) for line in _caddy_code())


def test_caddyfile_limits_body_and_proxies_to_loopback():
    code = "\n".join(_caddy_code())
    assert "max_size 210MB" in code
    assert "reverse_proxy 127.0.0.1:8808" in code
    assert "header_up X-Real-IP {remote_host}" in code
    assert "FS_DOMAIN {" in code          # placeholder substituted by prepare-vps.sh
    assert "Content-Security-Policy" not in code   # the app owns CSP (it embeds a hash)


def _caddy_header_blocks() -> dict[str, dict[str, str]]:
    """Map each `header [matcher] {` block to its {name: value} lines ('' = no matcher)."""
    blocks: dict[str, dict[str, str]] = {}
    current = None
    for line in _caddy_code():
        m = re.match(r"^header(?:\s+(@\w+))?\s*\{$", line)
        if m:
            current = blocks.setdefault(m.group(1) or "", {})
            continue
        if current is not None:
            if line == "}":
                current = None
                continue
            name, _, value = line.partition(" ")
            current[name] = value.strip()
    return blocks


def test_caddyfile_frames_only_the_sandbox_same_origin():
    # tix frames /sandbox/html itself; DENY there would blank every HTML preview.
    code = _caddy_code()
    assert "@sandbox path /sandbox/html" in code
    assert "@notsandbox not path /sandbox/html" in code
    blocks = _caddy_header_blocks()
    assert set(blocks) == {"@sandbox", "@notsandbox"}, blocks
    assert blocks["@sandbox"]["X-Frame-Options"] == '"SAMEORIGIN"'
    assert blocks["@notsandbox"]["X-Frame-Options"] == '"DENY"'
    same = {k: v for k, v in blocks["@sandbox"].items() if k != "X-Frame-Options"}
    other = {k: v for k, v in blocks["@notsandbox"].items() if k != "X-Frame-Options"}
    assert same == other
    assert same == {
        "Strict-Transport-Security": '"max-age=31536000; includeSubDomains"',
        "X-Content-Type-Options": '"nosniff"',
        "Referrer-Policy": '"no-referrer"',
        "-Server": "",
    }


def test_runbook_says_the_caddy_site_is_reinstalled_on_deploy():
    text = (INFRA / "RUNBOOK.md").read_text()
    deploy = text[text.index("## 3. Deploy"):text.index("## 4.")]
    assert "/etc/caddy/sites/fileshare.caddy" in deploy
    assert "sync.sh" in deploy and "does not copy" in deploy


# ---------------------------------------------------------------- scripts

@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_strict_and_parse(name):
    text = (INFRA / name).read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text[:2000]
    subprocess.run(["bash", "-n", str(INFRA / name)], check=True)


def test_sync_refuses_to_ship_too_little():
    text = (INFRA / "sync.sh").read_text()
    for must in ["fileshare/static/vendor/purify.min.js",
                 "fileshare/static/vendor/markdown-it.min.js",
                 "fileshare/static/fonts/figtree-latin-wght.woff2", "fileshare/static/fonts/manrope-latin-wght.woff2",
                 "fileshare/static/fonts/OFL-Figtree.txt", "fileshare/static/fonts/OFL-Manrope.txt",
                 "fileshare/static/css/tokens.css",
                 "skill/sharing/sharing.py", "fileshare/templates/onboarding.sh"]:
        assert must in text
    # uv, its caches and the venv live in the service user's HOME = the prefix.
    for keep in ["'.venv/'", "'.local/'", "'.cache/'"]:
        assert keep in text
    # The box has no .git: without FS_BUILD every page is stamped ?v=dev forever.
    assert "FS_BUILD=$BUILD" in text


def test_prepare_vps_env_template_keys():
    text = (INFRA / "prepare-vps.sh").read_text()
    for key in ["FS_DATA_DIR=", "FS_PUBLIC_URL=", "FS_MAX_UPLOAD=209715200",
                "FS_SKILL_DIR=", "FS_COOKIE_SECURE=1", "FS_BUILD="]:
        assert key in text
    assert "chmod 0640" in text
    assert "orchestrator.caddy" in text     # refuses to double-serve the domain


def _shell_code(name: str) -> list[str]:
    """Logical lines of a script: full-line comments dropped, `\\` continuations joined."""
    out, buf = [], ""
    for raw in (INFRA / name).read_text().splitlines():
        if raw.strip().startswith("#") and not buf:
            continue
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((buf + raw).strip())
        buf = ""
    return [line for line in out if line]


_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\$\(|\bthen\b|\belse\b|\bdo\b")
_FS_WRITABLE = re.compile(r'^"?(\$UV|\$PREFIX|\$\{PREFIX\}|/opt/fileshare)\b')


def test_prepare_vps_root_never_trusts_fileshare_writable_files():
    # /opt/fileshare is chown'd to the service user. Anything root installs into
    # /etc or /usr/local, or executes, must come from the operator's staging copy.
    for line in _shell_code("prepare-vps.sh"):
        assert "$PREFIX/infra" not in line and "/opt/fileshare/infra" not in line, line
        if re.search(r"\b(install|cp|sed)\b", line) and re.search(r"/etc/|/usr/local/", line):
            assert "$PREFIX" not in line and "/opt/fileshare" not in line, line
        for seg in _SEGMENT_SPLIT.split(line):
            seg = seg.strip().lstrip("!{( ").strip()
            if re.match(r"(sudo|runuser) -u fileshare\b", seg):
                continue                      # runs as the service user: fine
            words = seg.split()
            if words and words[0] in {"bash", "sh", "env", "exec", "command"}:
                words = [w for w in words[1:] if "=" not in w] or [""]
            assert not (words and _FS_WRITABLE.match(words[0])), f"root executes {seg!r}"
    text = (INFRA / "prepare-vps.sh").read_text()
    assert '"$STAGE/infra/fileshare.service"' in text


def test_prepare_vps_caddy_section_always_rolls_back():
    text = (INFRA / "prepare-vps.sh").read_text()
    caddy = text[text.index('say "caddy"'):]
    # No drop-in -> cat exits 1 -> pipefail + set -e abort mid-change. Guard it.
    assert re.search(r"\{ cat /etc/systemd/system/caddy\.service\.d/\*\.conf 2>/dev/null \|\| true; \}",
                     caddy)
    # Any exit before a successful validate + reload restores both files.
    assert re.search(r"^trap '[^']*caddy_rollback[^']*' EXIT", caddy, re.M)
    assert "caddy_committed=1" in caddy
    assert caddy.index("trap ") < caddy.index('> "$CADDY_SITES/fileshare.caddy"')
    assert caddy.index("caddy validate") < caddy.index("caddy_committed=1")


def test_prepare_vps_validates_the_domain():
    text = (INFRA / "prepare-vps.sh").read_text()
    assert "^[a-z0-9.-]+$" in text
    assert text.index("tr '[:upper:]' '[:lower:]'") < text.index("^[a-z0-9.-]+$")


def test_sync_never_ships_ignored_or_env_files():
    text = (INFRA / "sync.sh").read_text()
    staging = text[text.index('say "sync -> '):text.index("--stage-only\" ]")]
    assert "--filter=':- .gitignore'" in staging
    for pat in ["'.env'", "'.env.*'", "'*.bak'"]:
        assert f"--exclude {pat}" in staging


# ---------------------------------------------------------------- docs

def test_runbook_carries_the_operational_notes():
    text = (INFRA / "RUNBOOK.md").read_text()
    assert "lowercase" in text and ":443" in text        # Origin is compared literally
    assert "pending" in text and "Devices page" in text  # crash mid-onboarding
    assert "shell history" in text                       # onboarding code in argv, by design
    deploy = text[text.index("## 3. Deploy"):text.index("## 4.")]
    assert "prepare-vps.sh" in deploy                    # units/fail2ban need a re-run


def test_readme_threat_model_is_honest_and_dev_notes_current():
    readme = (ROOT / "README.md").read_text()
    assert "once tests/js exists" not in readme
    assert "docs/threat-model.md" in readme and "Deepgram" in readme   # the README points at the full model
    text = (ROOT / "docs" / "threat-model.md").read_text()
    model = text[text.index("## Threat model"):]
    assert "strong passphrase" in model and "PBKDF2" in model        # stolen DB => offline guessing
    assert "curl | bash" in model and "irm | iex" in model and "sharing update" in model
    # §20: browser uploads of audio go to Deepgram in the clear (over HTTPS) while auto-transcribe is on
    flat = " ".join(model.split())
    assert "sent to Deepgram (api.deepgram.com) unencrypted, over HTTPS" in flat
    assert "Transcribe audio uploads automatically" in flat


def test_runbook_logs_section_matches_the_caddyfile():
    # The site has no `log` block, so Caddy writes no access log; requests are only in uvicorn's.
    text = (INFRA / "RUNBOOK.md").read_text()
    logs = text[text.index("## 5. Logs"):text.index("## 6.")]
    assert "access logs live here" not in logs
    assert "journalctl -u fileshare" in logs
    assert re.search(r"journalctl -u caddy.*errors", logs), logs


# ---------------------------------------------------------------- ci

def test_ci_has_no_empty_suite_fallbacks_and_uses_node_22():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "[ $? -eq 5 ]" not in text and "no tests collected" not in text
    assert "if [ -d tests/js ]" not in text
    assert 'node-version: "22"' in text and 'node-version: "20"' not in text
    for cmd in ("uv run pytest -m e2e", "uv run pytest -m browser",
                "node --test --test-timeout=60000 tests/js"):
        assert cmd in text, cmd


def test_ci_js_timeout_precedes_the_files_and_jobs_are_bounded():
    # Node ignores options placed after the file arguments.
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "node --test --test-timeout=60000 tests/js/*.test.mjs" in text
    assert "*.test.mjs --test-timeout" not in text
    jobs = text[text.index("jobs:"):]
    # Windows jobs were removed on purpose; the PowerShell installer keeps its static checks in `client`.
    assert "windows-latest" not in jobs and "e2e-windows" not in jobs
    want = {"unit": 20, "client": 30, "e2e": 20, "browser-shard": 30, "browser": 5, "addon": 20}
    found = set(re.findall(r"^  ([a-z0-9-]+):$", jobs, re.M))
    assert found == set(want), found
    for job, minutes in want.items():
        m = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", jobs, re.M | re.S)
        assert m, job
        assert re.search(rf"^    timeout-minutes: {minutes}$", m.group(1), re.M), job


def _collect_browser(shard=None):
    env = {**os.environ, "PYTEST_ADDOPTS": ""}
    env.pop("BROWSER_SHARD", None)
    if shard:
        env["BROWSER_SHARD"] = f"{shard}/4"
    out = subprocess.run([sys.executable, "-m", "pytest", "-m", "browser", "tests/browser", "--collect-only", "-q"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if "::" in line]


def test_ci_browser_shards_cover_every_browser_test_once_and_browser_aggregates():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    m = re.search(r"^  browser-shard:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert m and "shard: [1, 2, 3, 4]" in m.group(1)
    # the selection the workflow runs: every test goes to one shard, split by measured duration (tests/browser/shard.py)
    assert "BROWSER_SHARD=${{ matrix.shard }}/4" in m.group(1) and "pytest -m browser tests/browser" in m.group(1)
    everything = _collect_browser()
    parts = [_collect_browser(s) for s in (1, 2, 3, 4)]
    assert all(parts)
    assert sorted(sum(parts, [])) == sorted(everything) and len(set(everything)) == len(everything)
    agg = re.search(r"^  browser:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S).group(1)
    assert "if: always()" in agg and "needs: browser-shard" in agg
    assert 'needs.browser-shard.result }}" = success' in agg


def test_browser_shard_split_is_balanced_deterministic_and_takes_new_files_in():
    from tests.browser.shard import DEFAULT_TEST_SECONDS, assign
    durations = {"slow.py": 400, "a.py": 100, "b.py": 100}
    tests = [(f"{f}::t{i}", f) for f, n in (("slow.py", 40), ("a.py", 10), ("b.py", 10), ("new.py", 3)) for i in range(n)]
    shards = assign(tests, 4, durations)
    assert shards == assign(list(reversed(tests)), 4, durations)
    assert sorted(sum(shards, [])) == sorted(n for n, _ in tests)
    weight = {n: durations[f] / sum(1 for _, g in tests if g == f) if f in durations else DEFAULT_TEST_SECONDS for n, f in tests}
    loads = [sum(weight[n] for n in sh) for sh in shards]
    assert max(loads) - min(loads) <= max(weight.values())


def test_ci_runs_on_prs_and_main_only_and_needs_no_secrets():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    head = text[:text.index("jobs:")]
    assert "push:\n    branches: [main]" in head and "pull_request:\n    branches: [main]" in head
    assert "secrets." not in text and "token:" not in text      # orch-core is checked out anonymously
    assert "repository: severinlindenmann/orch-core" in text and "pytest addons/orch-tix/tests" in text


def test_readme_tickets_on_orch_core_and_the_mirror_threat_model():
    """Task 12: tickets live in orch-core; the threat model names the mirror cleartext and its limits."""
    text = (ROOT / "docs" / "operations.md").read_text()
    tickets = " ".join(text[text.index("## Tickets on orch-core"):text.index("## Upload links")].split())
    for phrase in ("source of truth", "The phone writes decisions, never tickets.", "The desktop applies them.",
                   "out of scope", "docs/ticket-format-example.md"):
        assert phrase in tickets, phrase
    text = (ROOT / "docs" / "threat-model.md").read_text()
    model = " ".join(text[text.index("## Threat model"):].split())
    for phrase in ("Mirror reads are S|D, so they are not a confidentiality boundary.", "GET /api/mirrors",
                   "Cross-workspace exposure.", "key-only", "A4.1", "remote-humans.json", "non-extractable",
                   "TIX never writes tickets.", "payload v2", "no title, text, client name or local key",
                   "single-use", "never in the browser",
                   # migration 008's cleartext columns, all of them
                   "the browser session's name", "the space id", "the sender's name", "the FILE ids",
                   "the TIX number", "join requests"):
        assert phrase in model, phrase


def test_runbook_has_the_a4_switch_over_in_order():
    """Final review I6: the switch-over steps, in their order, with the exact flag spelling."""
    text = (INFRA / "RUNBOOK.md").read_text()
    head = "## 8. Connect orch-core workspaces\n"
    section = text[text.index(head) + len(head):]
    steps = ["bash infra/sync.sh", "migration 008", "orch-core", "marketplace name", "sharing update",
             "2.0.0", "sharing space create", "orch addon install"]
    at = [section.index(s) for s in steps]
    assert at == sorted(at), [s for s, a in sorted(zip(steps, at), key=lambda x: x[1])]


def test_readme_describes_the_shared_key_line_and_the_switch_over_details():
    """Final review I5, M2, M3."""
    ops = (ROOT / "docs" / "operations.md").read_text()
    text = (ROOT / "docs" / "threat-model.md").read_text()
    flat = " ".join(text.split())
    assert "The addon warns in Mission Control when a workspace runs at `full`" not in flat
    assert '"Every TIX device can read synced tickets"' in flat and "browser-only" in flat
    tickets = " ".join(ops[ops.index("## Tickets on orch-core"):ops.index("## Upload links")].split())
    assert "infra/RUNBOOK.md" in tickets
    model = " ".join(text[text.index("## Threat model"):].split())
    for phrase in ("`project`", "`created_by_name`", "`type`"):
        assert phrase in model, phrase
    assert "sharing whoami" in ops
