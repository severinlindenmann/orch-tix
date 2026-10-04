"""The shared design tokens (design-system spec §5.3): orch-core generates static/tokens.css from tokens.json;
TIX copies it as is with scripts/sync_tokens.py and records its sha256 in static/css/SHA256SUMS. A copy that
drifts from the recorded hash fails here, as the vendored scripts and fonts do. app.css keeps no token block of
its own: only the touch-size and mono overrides and the older names mapped onto the shared ones."""
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "fileshare" / "static"
CSS = STATIC / "css"
TOKENS = CSS / "tokens.css"


def _load_sync():
    spec = importlib.util.spec_from_file_location("sync_tokens", ROOT / "scripts" / "sync_tokens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sums() -> dict[str, str]:
    lines = [l for l in (CSS / "SHA256SUMS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    return {line.split()[1]: line.split()[0] for line in lines}


# ---- the copy and its recorded hash

def test_tokens_css_matches_its_recorded_sha256():
    sums = _sums()
    assert set(sums) == {"tokens.css"}
    assert hashlib.sha256(TOKENS.read_bytes()).hexdigest() == sums["tokens.css"], \
        "tokens.css changed by hand: run scripts/sync_tokens.py against orch-core instead"


def test_sha256sums_names_the_token_version():
    head = (CSS / "SHA256SUMS").read_text().splitlines()[0]
    version = re.search(r"\bv(\d+\.\d+\.\d+)\b", TOKENS.read_text(encoding="utf-8")[:400]).group(1)
    assert head.startswith("# orch-core tokens v") and f"v{version}" in head


def test_tokens_css_is_orch_cores_generated_file_and_holds_custom_properties_only():
    css = TOKENS.read_text(encoding="utf-8")
    assert css.startswith("/* GENERATED from static/tokens.json")
    _load_sync().validate(css)          # no url(), @import, @font-face or markup: nothing it could load


@pytest.mark.parametrize("bad", ['  --x: url("https://e.example/a.png");', '@import "x.css";',
                                 '@font-face { font-family: X; }', "  --x: </style>;", "  color: red;",
                                 "  --x: expression(alert(1));"])
def test_validate_refuses_anything_but_custom_properties(bad):
    css = "/* GENERATED from static/tokens.json (DTCG 2025.10) v1.0.0 */\n:root {\n" + bad + "\n}\n"
    with pytest.raises(ValueError):
        _load_sync().validate(css)


def test_sync_copies_the_file_and_records_hash_version_and_commit(tmp_path):
    sync = _load_sync()
    core = tmp_path / "orch-core"
    src = core / sync.SOURCE
    src.parent.mkdir(parents=True)
    body = "/* GENERATED from static/tokens.json (DTCG 2025.10) v9.8.7 by x. */\n:root {\n  --bg: #FFFFFF;\n}\n"
    src.write_text(body, encoding="utf-8")
    out = tmp_path / "css"
    out.mkdir()
    sync.sync(core, out, commit="abc1234")
    assert (out / "tokens.css").read_text(encoding="utf-8") == body
    digest = hashlib.sha256(body.encode()).hexdigest()
    assert (out / "SHA256SUMS").read_text() == f"# orch-core tokens v9.8.7 (orch-core abc1234)\n{digest}  tokens.css\n"
    assert sync.check(core, out) == []
    src.write_text(body.replace("#FFFFFF", "#FFFFFE"), encoding="utf-8")
    assert sync.check(core, out) == ["tokens.css differs from orch-core's"]


def test_sync_refuses_a_file_that_is_not_the_generated_one(tmp_path):
    sync = _load_sync()
    src = tmp_path / sync.SOURCE
    src.parent.mkdir(parents=True)
    src.write_text(":root { --bg: #FFF; }\n", encoding="utf-8")
    out = tmp_path / "css"
    out.mkdir()
    with pytest.raises(ValueError):
        sync.sync(tmp_path, out, commit=None)
    assert not (out / "tokens.css").exists()


# ---- wiring: every page loads the tokens before app.css, the worker precaches them, deploy ships them

def test_every_page_with_app_css_loads_tokens_css_first():
    pages = [p for p in STATIC.glob("*.html") if "/static/css/app.css" in p.read_text(encoding="utf-8")]
    assert len(pages) >= 9
    for p in pages:
        text = p.read_text(encoding="utf-8")
        tokens = '<link rel="stylesheet" href="/static/css/tokens.css?v={{BUILD}}">'
        assert tokens in text, p.name
        assert text.index(tokens) < text.index('href="/static/css/app.css'), p.name


def test_tokens_css_is_precached_and_shipped():
    assert "/static/css/tokens.css" in json.loads((STATIC / "precache.json").read_text())["assets"]
    assert "fileshare/static/css/tokens.css" in (ROOT / "infra" / "sync.sh").read_text()


# ---- app.css: no token block of its own

APP = (CSS / "app.css").read_text(encoding="utf-8") if (CSS / "app.css").exists() else ""
SHARED = re.findall(r"^\s*(--[a-z0-9-]+):", TOKENS.read_text(encoding="utf-8"), re.M) if TOKENS.exists() else []


def test_app_css_redefines_no_shared_colour_token():
    # app.css may override sizes (--tap, --control-h, --radius-sheet) and the mono stack, never a colour
    overrides = {name for name, value in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", APP) if name in set(SHARED)}
    assert overrides <= {"--tap", "--control-h", "--font-mono"}, overrides
    assert not re.search(r"--[a-z0-9-]+:\s*#[0-9A-Fa-f]{3,8}\b", APP), "colour values live in tokens.css"
    assert "prefers-color-scheme" not in APP


def test_mono_override_puts_plex_first():
    assert re.search(r"--font-mono:\s*var\(--mono\);", APP)
    mono = re.search(r"--mono:\s*([^;]+);", APP).group(1)
    assert mono.startswith('"IBM Plex Mono"') and "ui-monospace" in mono


def test_the_primary_button_is_ink_and_mint_is_never_a_fill():
    """D3: the primary button is ink (--primary); mint is the brand and the focus ring, never a button fill."""
    rule = APP[APP.index(".btn-primary, .btn-accent {"):]
    rule = rule[:rule.index("}")]
    assert "background: var(--primary)" in rule and "color: var(--on-primary)" in rule
    assert not re.search(r"background(-color)?:\s*var\(--mint\)", APP)
    assert "--onmint" not in APP


# ---- contrast from the copied tokens (WCAG 2.x)

def _block(css: str, selector: str) -> dict[str, str]:
    start = css.index(selector + " {")
    body = css[start:css.index("}", start)]
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", body))


def _lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background, minimum): what TIX paints, including its count badge (--you-fg fill, --surface text)
PAIRS = [("--text", "--bg", 4.5), ("--text", "--surface", 4.5), ("--muted", "--surface", 4.5), ("--muted", "--bg", 4.5),
         ("--link", "--surface", 4.5), ("--on-primary", "--primary", 4.5), ("--on-danger", "--danger", 4.5),
         ("--surface", "--you-fg", 4.5),
         *[(f"--{r}-fg", f"--{r}-bg", 4.5) for r in ("ok", "info", "you", "warn", "err", "neu")],
         ("--focus", "--surface", 3.0), ("--ctl", "--surface", 3.0)]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg,minimum", PAIRS)
def test_contrast(theme, fg, bg, minimum):
    tokens = _block(TOKENS.read_text(encoding="utf-8"), f"[data-theme={theme}]")
    assert _ratio(tokens[fg], tokens[bg]) >= minimum, (theme, fg, bg, round(_ratio(tokens[fg], tokens[bg]), 2))


def test_the_count_badge_uses_the_checked_pair():
    rules = [m.group(0) for m in re.finditer(r"\.count-badge \{[^}]*\}", APP) if "background" in m.group(0)]
    assert rules and all("background: var(--you-fg)" in r and "color: var(--surface)" in r for r in rules)
