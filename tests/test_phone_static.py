"""Static rules for the phone screens (design system: no browser popups; TIX: no HTML parsing of decrypted data)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "fileshare" / "static"
POPUP = re.compile(r"(?<![\w.$])(?:window\.|globalThis\.|self\.)?(alert|confirm|prompt)\s*\(")
PHONE = ["needs.js", "ticket.js", "ticket-card.js", "decision-send.js", "phone-inbox.js", "mirror-model.js", "textsafe.js", "images.js"]


def _code(text: str) -> str:
    """The text without // line comments and /* */ blocks (a comment may name what it never calls)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", text)


def test_no_browser_confirm_alert_or_prompt_anywhere():
    files = [*STATIC.glob("js/*.js"), STATIC / "sw.js", *STATIC.glob("*.html")]
    hits = [f"{p.name}: {m.group(0)}" for p in files for m in POPUP.finditer(_code(p.read_text(encoding="utf-8")))]
    assert hits == []


def test_the_popup_rule_catches_what_it_should():
    assert POPUP.search("if (confirm('x')) go();") and POPUP.search("window.alert(1)")
    assert not POPUP.search("await confirmSheet({})") and not POPUP.search("dlg.confirm(") and not POPUP.search("confirmDialog(")


def test_phone_modules_never_parse_html():
    for name in PHONE:
        code = _code((STATIC / "js" / name).read_text(encoding="utf-8"))
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment"):
            assert bad not in code, (name, bad)


def test_decision_text_keeps_its_own_direction_and_hidden_badges_are_isolated():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"\.section-text, \.gate-text, \.hist li[^{]*\{([^}]*)\}", css)
    assert rule and "unicode-bidi: plaintext" in rule.group(1)
    badge = re.search(r"\.hidden-char \{([^}]*)\}", css).group(1)
    assert "unicode-bidi: isolate" in badge
