"""HTML pages and /static assets. Pages are stamped with the build so a deploy busts caches."""
import functools
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from fileshare.db import get_meta
from fileshare.deps import api_error, get_db
from fileshare.headers import PREVIEW_CSS
from fileshare.sessions import cookie_name, renew_session

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_STAMP_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
# The HTML of the app shell holds no user data; the service worker caches it for offline use (§16).
SHELL_CACHE = "no-cache"

router = APIRouter(include_in_schema=False)


@functools.lru_cache(maxsize=1)
def build_stamp() -> str:
    env = os.environ.get("FS_BUILD", "").strip()
    if _STAMP_RE.fullmatch(env):
        return env
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=STATIC_DIR,
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return "dev"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and _STAMP_RE.fullmatch(sha) else "dev"


def _initialized(conn) -> bool:
    return get_meta(conn, "auth_hash") is not None


def render_page(name: str, settings, cache: str = "no-store") -> HTMLResponse:
    """Serve static/<name>.html with its three placeholders filled in.

    `cache`: the app-shell pages the service worker precaches (§16) are "no-cache" (the worker
    never stores a no-store response); setup and the public viewer stay "no-store".

    {{BUILD}}       build stamp for ?v= cache busting (this task)
    {{PREVIEW_CSS}} the exact bytes hashed into the CSP (used by index.html from Task 19)
    {{MAX_UPLOAD}}  settings.max_upload (used by index.html from Task 21)
    """
    path = STATIC_DIR / f"{name}.html"
    if not path.is_file():
        raise api_error(404, "not_found", "page not built yet")
    html = path.read_text(encoding="utf-8")
    for placeholder, value in (("{{BUILD}}", build_stamp()),
                               ("{{PREVIEW_CSS}}", PREVIEW_CSS),
                               ("{{MAX_UPLOAD}}", str(settings.max_upload))):
        html = html.replace(placeholder, value)
    # X-Build: the service worker caches a page only for its own build (an older worker never stores a newer
    # build's page, whose ?v= assets it doesn't have).
    return HTMLResponse(html, headers={"Cache-Control": cache, "X-Build": build_stamp()})


def login_url(next_path: str) -> str:
    """The one spelling of the login redirect (same as loginHref() in nav.js): next is percent-encoded and
    only ever a same-origin path starting with a single "/"; anything else falls back to "/"."""
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    return f"/login?next={quote(next_path, safe='')}"


def _to_setup() -> RedirectResponse:
    return RedirectResponse("/setup", status_code=307)


@router.get("/login")
def login(request: Request, conn=Depends(get_db)):
    return render_page("login", request.app.state.settings, SHELL_CACHE) if _initialized(conn) else _to_setup()


def _gated(request: Request, conn, name: str, next_path: str):
    """A page gated on the server too: without a live session it goes straight to login."""
    if not _initialized(conn):
        return _to_setup()
    token = request.cookies.get(cookie_name(request.app.state.settings))
    h, renewed = renew_session(conn, token)
    if h is None:
        return RedirectResponse(login_url(next_path), status_code=307)
    if renewed:
        request.state.renew_cookie = token     # CookieRefreshMiddleware re-sends the cookie
    return render_page(name, request.app.state.settings, SHELL_CACHE)


# TIX on orch-core (spec §10): the phone's four tabs. Needs you is "/" (Tickets is "/?view=tickets"),
# one mirrored ticket is /t/<n>, Files moved to /files, Settings holds Devices.
@router.get("/")
def index(request: Request, conn=Depends(get_db)):
    # A link from before Task 9 (/?f=FILE7, /?tag=…) opens the Files page it meant.
    if "f" in request.query_params or "tag" in request.query_params:
        return RedirectResponse("/files?" + request.url.query, status_code=301)
    return _gated(request, conn, "index", "/")


@router.get("/workspaces")
def workspaces_page(request: Request, conn=Depends(get_db)):
    """The status page (Remote R9): every workspace with its state. ?open=<space> is the snapshot placeholder."""
    return _gated(request, conn, "workspaces", "/workspaces")


@router.get("/files")
def files_page(request: Request, conn=Depends(get_db)):
    # As "/" was before Task 9: the shell needs setup only; files.js sends a signed-out browser to login.
    return render_page("files", request.app.state.settings, SHELL_CACHE) if _initialized(conn) else _to_setup()


@router.get("/settings")
def settings_page(request: Request, conn=Depends(get_db)):
    return _gated(request, conn, "settings", "/settings")


_TICKET_N_RE = re.compile(r"[0-9]{1,12}", re.ASCII)


@router.get("/t")
def ticket_shell(request: Request, conn=Depends(get_db)):
    """The ticket page without a number: what the service worker precaches for an offline /t/<n>."""
    return _gated(request, conn, "ticket", "/")


@router.get("/t/{n}")
def ticket_page(n: str, request: Request, conn=Depends(get_db)):
    if not _TICKET_N_RE.fullmatch(n):
        raise api_error(404, "not_found", "no such ticket")
    return _gated(request, conn, "ticket", f"/t/{n}")


# The retired board and Devices page: old bookmarks and notifications land on the new screens.
@router.get("/tickets")
def tickets_redirect():
    return RedirectResponse("/", status_code=301)


_OLD_REF_RE = re.compile(r"(?:TIX-)?([1-9][0-9]{0,11})", re.ASCII | re.IGNORECASE)


@router.get("/tickets/{ref}")
def ticket_redirect(ref: str):
    m = _OLD_REF_RE.fullmatch(ref)
    return RedirectResponse(f"/t/{m.group(1)}" if m else "/", status_code=301)


@router.get("/devices")
def devices_redirect():
    return RedirectResponse("/settings#devices", status_code=301)


@router.get("/manifest.webmanifest")
def manifest():
    """The PWA manifest. Public and never redirected: the browser fetches it before setup and login."""
    body = (STATIC_DIR / "manifest.webmanifest").read_bytes()
    return Response(body, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@router.get("/pair")
def pair_page(request: Request):
    """Pairing with a desktop (orch-core remote humans, spec §6.4): static, no session, not even setup
    required. The pairing key is only in the URL fragment, which no browser sends; pair.js reads it and
    strips it from the address bar at once. The page itself holds no data, so the service worker precaches it
    (no-cache) and serves only this shell for /pair, never another page's (review fix round 1)."""
    return render_page("pair", request.app.state.settings, SHELL_CACHE)


@router.get("/setup")
def setup(request: Request):
    return render_page("setup", request.app.state.settings)


_LINK_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@router.get("/p/{token}")
def public_link_page(token: str, request: Request):
    """The public-link viewer (§17): static, no session, not even setup required. Its JS reads the
    key from the fragment. A malformed token gets the very same page with a 404 status, and the
    page's own API call then reports the link as expired or revoked."""
    response = render_page("public", request.app.state.settings)
    if not _LINK_TOKEN_RE.fullmatch(token):
        response.status_code = 404
    return response


@router.get("/u/{token}")
def drop_page(token: str, request: Request):
    """The upload-link drop page (upload-links spec): static, no session, not even setup required.
    Its JS reads the link's public key from the fragment. A malformed token gets the very same page
    with a 404 status, and the page's own API call then reports the link as expired or revoked."""
    response = render_page("drop", request.app.state.settings)
    if not _LINK_TOKEN_RE.fullmatch(token):
        response.status_code = 404
    return response


@router.get("/sandbox/html")
def sandbox_html(request: Request):
    """Spec T15: the isolated frame for a ticket's HTML attachment. It carries its own CSP
    (SecurityHeadersMiddleware, per-path override) and needs no session: the browser loads it in a
    sandboxed iframe before the parent posts the HTML to render over postMessage."""
    return render_page("sandbox", request.app.state.settings, "no-cache")


@router.get("/sandbox/widget")
def sandbox_widget(request: Request):
    """The frame a ticket widget is drawn in (js/widgets.js): like /sandbox/html, its own CSP (WIDGET_CSP), no
    session; the ticket page posts the widget's document over postMessage."""
    return render_page("sandbox-widget", request.app.state.settings, "no-cache")


@router.get("/sw.js")
def service_worker():
    """Served from the root so its scope can be / (§16)."""
    body = (STATIC_DIR / "sw.js").read_bytes()
    return Response(body, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


IMMUTABLE = "public, max-age=31536000, immutable"


class _RevalidatingStatic(StaticFiles):
    """Revalidated (ETag/304) static files. A file requested as `?v=<build>` is immutable: the page that names it
    is stamped with the same build, so its bytes never change under that URL. Unstamped URLs (dynamic imports,
    fonts), another build's stamp and a checkout without FS_BUILD ("dev", or a git sha that stays put while
    the files are edited) stay `no-cache`."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        stamp = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [""])[0]
        deployed = os.environ.get("FS_BUILD", "").strip()      # set by infra/sync.sh on a deploy, never on a checkout
        stamped = stamp == deployed != "dev" and bool(_STAMP_RE.fullmatch(stamp))
        response.headers["Cache-Control"] = IMMUTABLE if stamped and response.status_code == 200 else "no-cache"
        return response


def static_app() -> StaticFiles:
    return _RevalidatingStatic(directory=STATIC_DIR)
