"""App factory: settings check, database, routes, CORS, request log, worker (SERVER_PLAN.md sections 4, 8 and 9).

Run (from the server/ folder):  uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1
One worker process only: a second one would break the single job queue."""
import asyncio
import logging
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from lecturekit import api as lk

from . import errors
from .db import make_session_factory
from .daily_log import setup_daily_logs
from .mail import make_mailer
from .events import Hub, sync
from .routes import auth, courses, deeper, health, live, media, notifications, results, uploads, ws
from .security import LoginGuard
from .settings import Settings
from .worker import Worker

logger = logging.getLogger("lecturekit.server")


class SpaFiles(StaticFiles):
    """The built web app. An address with no file behind it (a page of the app, such as /sessions/12) gets index.html,
    so the app can use normal addresses without a # in them. Missing files (.js, .png ...) and unknown /api paths stay 404."""

    def __init__(self, *args, api_prefix: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.api_prefix = api_prefix

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            is_api = bool(self.api_prefix) and (path == self.api_prefix or path.startswith(self.api_prefix + "/"))
            is_file = "." in path.rsplit("/", 1)[-1]
            if e.status_code == 404 and not is_api and not is_file:
                return await super().get_response("index.html", scope)
            raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()                                   # refuses to start with a weak JWT secret
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    session_factory = make_session_factory(settings)
    hub = Hub()
    worker = Worker(settings, session_factory)
    worker.hub = hub

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.bind(asyncio.get_running_loop())              # events from the worker threads are handed to this loop
        lk.log.setup(quiet=True)                          # lecturekit's log files go to DATA_DIR/logs
        setup_daily_logs(settings.log_dir, settings.log_retention_days)     # logs/server-DATE.log and logs/error-DATE.log
        recovered = worker.recover()
        if recovered:
            logger.warning("marked %d unfinished job(s) from the last run as failed (server_restarted)", recovered)
        worker.start()
        if settings.preload_whisper:                      # load the model now, so the first job is not slow
            threading.Thread(target=lambda: lk.asr.get_model(), name="whisper-preload", daemon=True).start()
        yield
        worker.stop()

    # the interactive API docs live under the API prefix too, so a reverse proxy only has to forward /api/*
    prefix = settings.api_prefix
    app = FastAPI(title="lecturekit server", lifespan=lifespan, redoc_url=None,
                  docs_url=f"{prefix}/docs" if prefix else "/docs",
                  openapi_url=f"{prefix}/openapi.json" if prefix else "/openapi.json")
    app.state.settings, app.state.session_factory, app.state.worker, app.state.hub = settings, session_factory, worker, hub
    app.state.mailer, app.state.resend_times = make_mailer(settings), {}
    app.state.login_guard = LoginGuard(settings.login_max_failures, settings.login_window_seconds)
    errors.register(app)

    if settings.cors_origins:                             # never "*" (SERVER_PLAN.md section 9)
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                           allow_headers=["Authorization", "Content-Type"])

    biggest = max(settings.max_recording_mb, settings.max_slides_mb, settings.max_chunk_mb) * 1024 * 1024

    @app.middleware("http")
    async def limits_and_log(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > biggest + 1024 * 1024:      # refuse before reading the body
            return JSONResponse({"error": {"code": "too_large", "message": f"File too large (limit {biggest // 1024 // 1024} MB)"}},
                                status_code=413)
        response = await call_next(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 300 and getattr(request.state, "user_id", None):
            sync(hub, request.state.user_id, "change")        # whatever this request changed, the user's other open clients refresh it
        logger.info("%s %s %s user=%s", request.method, re.sub(r"/media/[^/?]+", "/media/...", request.url.path), response.status_code,
                    getattr(request.state, "user_id", "-"))            # no bodies, no tokens
        return response

    for module in (auth, courses, uploads, live, results, media, deeper, notifications, ws, health):
        app.include_router(module.router, prefix=prefix)
    if prefix:
        app.include_router(health.router)                 # /health also at the root, for monitoring and Caddy

    if settings.web_dist and settings.web_dist.is_dir():  # the web app, built to static files (served last)
        app.mount("/", SpaFiles(directory=settings.web_dist, html=True, api_prefix=prefix.strip("/")), name="web")
    else:                                                 # no web app yet: say what this is instead of a bare 404
        @app.get("/", include_in_schema=False)
        def root():
            return {"name": "lecturekit server", "api": prefix, "docs": f"{prefix}/docs", "health": "/health",
                    "web_app": "not installed"}
    return app


def _default_app() -> FastAPI:
    return create_app()


def __getattr__(name):                                    # `uvicorn server.main:app` builds the app on first use
    if name == "app":
        global app
        app = _default_app()
        return app
    raise AttributeError(name)
