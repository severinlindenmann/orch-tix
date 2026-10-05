import asyncio
import contextlib
import logging
import mimetypes
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fileshare import messages as messages_mod
from fileshare import mirrors as mirrors_mod
from fileshare import presence as presence_mod
from fileshare.bridge import Bridge
from fileshare.blobs import BlobStore
from fileshare.db import backfill_device_fingerprints, connect, ensure_epoch, migrate
from fileshare.expiry import expire_files, expire_upload_links, sweep_forever
from fileshare.headers import CompressionMiddleware, ConditionalMiddleware, SecurityHeadersMiddleware
from fileshare.heldpush import HeldStore
from fileshare.push import SubprocessPusher
from fileshare.routes import auth as auth_routes
from fileshare.routes import decisions as decisions_routes
from fileshare.routes import bridge as bridge_routes
from fileshare.routes import presence as presence_routes
from fileshare.routes import devices, files, links, messages, mirrors, onboarding, uploadlinks
from fileshare.routes import push as push_routes
from fileshare.routes import tickets as tickets_routes
from fileshare.routes import settings as settings_routes
from fileshare.routes import pages as pages_routes
from fileshare.security import RateLimiter, WindowLimiter
from fileshare.sessions import CookieRefreshMiddleware
from fileshare.settings import Settings
from fileshare.tickets import Bus

# Not every Python or OS mime table knows woff2; the self-hosted fonts (spec §18) need it.
mimetypes.add_type("font/woff2", ".woff2")


def configure_logging() -> None:
    logger = logging.getLogger("fileshare")
    if not any(getattr(h, "_fileshare", False) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(message)s"))
        h._fileshare = True
        logger.addHandler(h)
    logger.setLevel(logging.INFO)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    blobs = BlobStore(settings.data_dir)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        ensure_epoch(conn)
        backfill_device_fingerprints(conn)  # rewrite pre-2026-09-25 8-char fingerprints (§4.6)
        expire_files(conn, blobs)          # startup expiry sweep (§14 E), before the orphan sweep
        expire_upload_links(conn, blobs)  # same, for dead upload links (upload-links spec, Task 3)
        live = {r["uuid"] for r in conn.execute("SELECT uuid FROM files WHERE deleted_at IS NULL")}
        # A pending upload's blob has no `files` row yet (the owner hasn't adopted it), so without
        # this it would look orphaned and the sweep below would destroy it on every restart
        # (global-constraints.md Review Focus 1).
        live |= {r["file_uuid"] for r in conn.execute(
            "SELECT file_uuid FROM upload_links WHERE file_uuid IS NOT NULL AND file_n IS NULL")}
        conn.execute("DELETE FROM bridge_msgs")    # the bridge's routes live in memory: rows from before a restart are orphans
        last_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ticket_events").fetchone()[0]
        last_decision = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM decisions").fetchone()[0]
        last_message = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0]
    finally:
        conn.close()
    blobs.sweep(live)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # what the previous process still held inside a push window goes out now (QA N-08)
        await asyncio.to_thread(mirrors_mod.recover_held, app)
        await asyncio.to_thread(messages_mod.recover_held, app)
        # the hourly expiry sweep: one task, cancelled on shutdown
        task = asyncio.create_task(sweep_forever(settings.db_path, blobs, app.state.ticket_bus))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="fileshare", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.blobs = blobs
    app.state.limiter = RateLimiter()
    app.state.public_limiter = WindowLimiter(links.PUBLIC_LIMIT, links.PUBLIC_WINDOW_S)
    app.state.ticket_create_limiter = WindowLimiter(60, 3600)   # per device (spec T6)
    app.state.space_create_limiter = WindowLimiter(mirrors_mod.SPACE_CREATES_PER_HOUR, 3600)    # per device
    app.state.mirror_create_limiter = WindowLimiter(mirrors_mod.MIRROR_CREATES_PER_HOUR, 3600)  # per device
    app.state.mirror_update_limiter = WindowLimiter(mirrors_mod.MIRROR_UPDATES_PER_HOUR, 3600)  # per device
    app.state.decision_limiter = WindowLimiter(decisions_routes.DECISIONS_PER_HOUR, 3600)  # per session (TIX §4.2)
    app.state.message_limiter = WindowLimiter(messages_mod.MESSAGES_PER_HOUR, 3600)       # per sender
    app.state.message_push_gate = messages_mod.PushGate(messages_mod.HUMAN_PUSH_EVERY_S)  # per sending device
    app.state.held_store = HeldStore(settings.db_path)           # pushes held in a window survive a restart (QA N-08)
    app.state.needs_push_gate = mirrors_mod.NeedsPushGate(mirrors_mod.NEEDS_PUSH_EVERY_S,
                                                          app.state.held_store)  # per workspace (round B)
    app.state.ticket_bus = Bus(last_seq)                        # long-poll wakeups (spec T6)
    app.state.inbox_bus = Bus(last_decision)                    # decision long-poll wakeups (TIX spec §9)
    app.state.message_bus = Bus(last_message)                   # message long-poll wakeups (TIX spec §8)
    app.state.presence_limiter = WindowLimiter(presence_mod.BEATS_PER_MIN, 60)   # per host device (R9)
    app.state.bridge = Bridge()                                # remote bridge mailboxes (R8)
    app.state.pusher = SubprocessPusher(settings, settings.db_path)  # Web Push through a subprocess (spec T7)
    links.install_log_redaction()        # access lines carry /p/<token> and /api/public/<token>
    app.add_middleware(ConditionalMiddleware)
    app.add_middleware(CookieRefreshMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CompressionMiddleware)      # outermost: compresses what the others produced

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            body = exc.detail
        else:
            body = {"error": f"http_{exc.status_code}", "detail": str(exc.detail or "")}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "bad_request", "detail": "invalid request"}, status_code=400)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # --- routers --- (later tasks append app.include_router(...) here)
    app.include_router(auth_routes.router)
    app.include_router(onboarding.router)
    app.include_router(devices.router)
    app.include_router(files.router)
    app.include_router(settings_routes.router)
    app.include_router(uploadlinks.router)      # before links.router: it owns /api/public/u/{token}
    app.include_router(links.router)
    app.include_router(mirrors.router)          # before tickets_routes.router (TIX spec §9)
    app.include_router(decisions_routes.router)
    app.include_router(messages.router)
    app.include_router(bridge_routes.router)
    app.include_router(presence_routes.router)
    app.include_router(tickets_routes.router)
    app.include_router(push_routes.router)
    app.include_router(pages_routes.router)
    app.mount("/static", pages_routes.static_app(), name="static")

    return app
