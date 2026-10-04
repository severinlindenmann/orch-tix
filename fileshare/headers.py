import base64
import hashlib
from pathlib import Path

from starlette.datastructures import MutableHeaders

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
        csp_value = SANDBOX_CSP if scope["path"] == SANDBOX_PATH else self._csp

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
