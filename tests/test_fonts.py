"""Self-hosted fonts (TIX spec §10): Figtree (headings) and Manrope (text) as in orch-core's Mission
Control, IBM Plex Mono for ids. The woff2 files, their licences, and how app.css loads them; no
Google Fonts request.

Sources: Figtree and Manrope are the latin variable-weight woff2 files orch-core ships
(src/orch/dashboard/static/fonts, SIL OFL 1.1, OFL-Figtree.txt / OFL-Manrope.txt). IBM Plex Mono:
npm @ibm/plex-mono 2.5.0, fonts/complete/woff2, licence LICENSE.txt (SIL OFL 1.1) as OFL.txt.
"""
import hashlib
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "fileshare" / "static"
FONTS = STATIC / "fonts"
FACES = {
    ("Figtree", "300 900"): "figtree-latin-wght.woff2",
    ("Manrope", "200 800"): "manrope-latin-wght.woff2",
    ("IBM Plex Mono", "400"): "IBMPlexMono-Regular.woff2",
    ("IBM Plex Mono", "500"): "IBMPlexMono-Medium.woff2",
}
LICENCES = {"OFL.txt", "OFL-Figtree.txt", "OFL-Manrope.txt"}


def test_font_files_match_sha256sums():
    lines = [l for l in (FONTS / "SHA256SUMS").read_text().splitlines() if l.strip()]
    names = {line.split()[1] for line in lines}
    assert names == {*FACES.values(), *LICENCES}
    for line in lines:
        digest, name = line.split()
        assert hashlib.sha256((FONTS / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("name", sorted(FACES.values()))
def test_every_font_is_woff2(name):
    assert (FONTS / name).read_bytes()[:4] == b"wOF2"


def test_the_ofl_licences_ship_with_the_fonts():
    text = (FONTS / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in text
    assert 'Reserved Font Name "Plex"' in text
    assert "The Figtree Project Authors" in (FONTS / "OFL-Figtree.txt").read_text(encoding="utf-8")
    assert "The Manrope Project Authors" in (FONTS / "OFL-Manrope.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License, Version 1.1" in (FONTS / "OFL-Manrope.txt").read_text(encoding="utf-8")


def test_only_the_shipped_fonts_are_in_the_folder():
    assert {p.name for p in FONTS.iterdir()} == {*FACES.values(), *LICENCES, "SHA256SUMS"}


def _font_faces(css: str) -> list[str]:
    return re.findall(r"@font-face\s*\{([^}]*)\}", css)


def test_app_css_declares_each_face_once_with_swap_and_a_local_url():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    seen = {}
    for body in _font_faces(css):
        family = re.search(r'font-family:\s*"([^"]+)"', body).group(1)
        weight = re.search(r"font-weight:\s*(\d+(?: \d+)?)", body).group(1)
        url = re.search(r'url\("([^"]+)"\)\s*format\("woff2"\)', body).group(1)
        assert "font-display: swap" in body, (family, weight)
        seen[(family, weight)] = url
    assert seen == {k: f"../fonts/{v}" for k, v in FACES.items()}
    assert "fonts.googleapis" not in css and "fonts.gstatic" not in css
    assert not re.search(r"url\(\s*[\"']?(https?:)?//", css), "app.css must not load anything off-site"


def test_font_stacks_fall_back_to_system_fonts():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    sans = re.search(r"--font:\s*([^;]+);", css).group(1)
    heading = re.search(r"--heading:\s*([^;]+);", css).group(1)
    mono = re.search(r"--mono:\s*([^;]+);", css).group(1)
    assert sans.startswith('"Manrope"') and "system-ui" in sans and "sans-serif" in sans
    assert heading.startswith('"Figtree"') and "system-ui" in heading and "sans-serif" in heading
    assert mono.startswith('"IBM Plex Mono"') and "ui-monospace" in mono and "monospace" in mono


def test_fonts_are_served_as_woff2(client):
    for name in FACES.values():
        r = client.get(f"/static/fonts/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"] == "font/woff2", name
        assert r.content[:4] == b"wOF2"
