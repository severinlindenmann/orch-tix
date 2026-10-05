import base64
import hashlib
from pathlib import Path

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

PREVIEW_CSS = (Path(__file__).parent / "static" / "css" / "preview.css").read_text(encoding="utf-8")
PREVIEW_CSS_SHA256 = base64.b64encode(hashlib.sha256(PREVIEW_CSS.encode("utf-8")).digest()).decode("ascii")


def csp() -> str:
    return (
        "default-src 'self'; script-src 'self'; "
        f"style-src 'self' 'sha256-{PREVIEW_CSS_SHA256}'; "
        "img-src 'self' blob:; media-src 'self' blob:; frame-src 'self'; "
        # Spec §20: the browser sends audio straight to Deepgram for transcription.
        "connect-src 'self' https://api.deepgram.com; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )


# Spec T15 / plan "Global Constraints": the sandboxed frame that renders an agent's HTML attachment.
# It is the one path exempt from `csp()` above — everything else, including every other /static/
# file, keeps the global policy unchanged.
SANDBOX_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com data:; "
    "img-src data: blob:; media-src data: blob:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)

SANDBOX_PATH = "/sandbox/html"

# The frame a ticket widget is drawn in (orch.widgets.v1, orch-core docs/widgets.md "The frame"): orch's frame
# policy. The frame's own script is inline in sandbox-widget.html (it writes the posted document), so script-src has
# no 'self': no file of this origin runs there. Nothing loads from anywhere else and nothing leaves; `sandbox`
# without allow-same-origin gives it an opaque origin.
WIDGET_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)

WIDGET_PATH = "/sandbox/widget"
FRAME_CSP = {SANDBOX_PATH: SANDBOX_CSP, WIDGET_PATH: WIDGET_CSP}


class SecurityHeadersMiddleware:
    """Pure ASGI so streamed FileResponses pass through untouched."""

    def __init__(self, app):
        self.app = app
        self._csp = csp()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")
        csp_value = FRAME_CSP.get(scope["path"], self._csp)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = csp_value
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                if is_api:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CompressionMiddleware(GZipMiddleware):
    """gzip for the text the app serves (JSON lists, HTML, JS, CSS). Not for the encrypted blobs
    (application/octet-stream: incompressible, and streamed); images, audio and video are excluded by default.
    The /api/mirrors list was ~0.5 MB of JSON per open, uncompressed."""

    def __init__(self, app):
        super().__init__(app, minimum_size=1000, compresslevel=6,
                         exclude_content_types=DEFAULT_EXCLUDED_CONTENT_TYPES + ("application/octet-stream",))


def _etag_matches(header: str, etag: str) -> bool:
    """Weak comparison of an If-None-Match value with our (weak) ETag."""
    bare = etag.removeprefix("W/")
    return header.strip() == "*" or any(t.strip().removeprefix("W/") == bare for t in header.split(","))


class ConditionalMiddleware:
    """ETag + 304 for the revalidated shell (HTML pages, /sw.js, the manifest). Those are `Cache-Control: no-cache`
    and small, but had no validator, so every open downloaded them in full. Only single-body 200 GET answers that
    are `no-cache` and carry no ETag of their own are touched (StaticFiles already sends its own)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        wanted = Headers(scope=scope).get("if-none-match", "")
        held = {}

        async def send_with_etag(message):
            if message["type"] == "http.response.start":
                h = Headers(raw=message["headers"])
                if message["status"] == 200 and h.get("cache-control") == "no-cache" and "etag" not in h:
                    held["start"] = message          # hold it until the body message arrives
                    return
            elif message["type"] == "http.response.body" and "start" in held:
                start = held.pop("start")
                if message.get("more_body"):         # not a small buffered page: pass through untouched
                    await send(start)
                    await send(message)
                    return
                etag = 'W/"' + hashlib.sha256(message.get("body", b"")).hexdigest()[:32] + '"'
                headers = MutableHeaders(raw=start["headers"])
                headers["ETag"] = etag
                if wanted and _etag_matches(wanted, etag):
                    for name in ("content-length", "content-type"):
                        del headers[name]
                    start["status"] = 304
                    await send(start)
                    await send({"type": "http.response.body", "body": b""})
                    return
                await send(start)
            await send(message)

        await self.app(scope, receive, send_with_etag)
