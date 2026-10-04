"""Copy orch-core's design tokens into TIX (design-system spec §5.3).

orch-core generates `static/tokens.css` from `tokens.json`. TIX deploys on its own, so it never loads that file
from orch-core at runtime: this script copies it as is to `fileshare/static/css/tokens.css` and records its
sha256, the token version and the orch-core commit in `fileshare/static/css/SHA256SUMS`. tests/test_tokens.py
fails when the copy drifts from the recorded hash.

    uv run python scripts/sync_tokens.py [--from ../orch-core] [--check]

--from: an orch-core checkout (default: $ORCH_CORE, else ../orch-core next to this repo).
--check: change nothing; exit 1 when the copy differs from orch-core's file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "fileshare" / "static" / "css"
SOURCE = Path("plugins/orch-core/src/orch/dashboard/static/tokens.css")
NAME = "tokens.css"

_HEADER = re.compile(r"\A/\* GENERATED from static/tokens\.json \(DTCG [^)]*\) v(\d+\.\d+\.\d+)\b")
# What a line after the header comment may be: a theme selector or @media opening a block, a custom property
# (or the theme's color-scheme), a closing brace. Nothing that loads, imports or declares a font.
_OPEN = re.compile(r"\s*(?::root|\[data-[a-z-]+=[a-z]+\]|:root:not\(\[data-theme=light\]\)|,|\s|"
                   r"@media \([a-z-]+: [a-z0-9]+\))+\{\s*")
_PROP = re.compile(r"\s*(?:--[a-z0-9-]+:\s*[^;{}<>\\@]+|color-scheme: (?:light|dark));\s*")
_FORBIDDEN = re.compile(r"url\(|@import|@font-face|expression\(|</|javascript:", re.I)


def version(css: str) -> str:
    m = _HEADER.match(css)
    if not m:
        raise ValueError("not orch-core's generated tokens.css (header missing)")
    return m.group(1)


def validate(css: str) -> str:
    """The token version, or ValueError when `css` is anything but custom properties in theme and media blocks."""
    v = version(css)
    if _FORBIDDEN.search(css):
        raise ValueError("tokens.css must not load or import anything")
    body = css[css.index("*/") + 2:]
    for n, line in enumerate(body.splitlines(), 1):
        if not line.strip() or line.strip() == "}" or _OPEN.fullmatch(line) or _PROP.fullmatch(line):
            continue
        raise ValueError(f"tokens.css line {n} is not a custom property: {line.strip()[:60]}")
    return v


def _commit(core: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(core), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def sums_text(css: bytes, ver: str, commit: str | None) -> str:
    where = f" (orch-core {commit})" if commit else ""
    return f"# orch-core tokens v{ver}{where}\n{hashlib.sha256(css).hexdigest()}  {NAME}\n"


def sync(core: Path, dest: Path = DEST, commit: str | None = None) -> str:
    """Copy the checkout's tokens.css into `dest` and record it. Returns the token version."""
    raw = (Path(core) / SOURCE).read_bytes()
    ver = validate(raw.decode("utf-8"))
    (dest / NAME).write_bytes(raw)
    (dest / "SHA256SUMS").write_text(sums_text(raw, ver, commit))
    return ver


def check(core: Path, dest: Path = DEST) -> list[str]:
    """What differs between the copy and the checkout's file ([] when they match)."""
    raw = (Path(core) / SOURCE).read_bytes()
    have = (dest / NAME).read_bytes() if (dest / NAME).exists() else b""
    return [] if raw == have else [f"{NAME} differs from orch-core's"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="core", default=os.environ.get("ORCH_CORE") or str(ROOT.parent / "orch-core"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    core = Path(args.core).expanduser()
    if not (core / SOURCE).is_file():
        print(f"no {SOURCE} under {core}; pass --from <orch-core checkout>", file=sys.stderr)
        return 2
    if args.check:
        problems = check(core)
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    ver = sync(core, commit=_commit(core))
    print(f"copied orch-core tokens v{ver} to {DEST / NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
