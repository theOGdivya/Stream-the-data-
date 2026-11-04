"""FastAPI app. Run with: ``uvicorn aqserver.main:create_app --factory``."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthMiddleware
from .config import Settings, load_settings
from .db import init_db, make_engine, make_sessionmaker
from .live import LiveHub
from .discovery import Discovery
from .routes import agent, calibration, data, events, monitor, results, review, setup, strip

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)

    app = FastAPI(title="AQ side-by-side test platform", version=__version__,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.engine = engine
    app.state.Session = make_sessionmaker(engine)
    app.state.live = LiveHub(settings.live_viewer_ttl_s)
    app.state.discovery = Discovery()

    for module in (agent, setup, monitor, calibration, review, data, events, strip, results):
        app.include_router(module.router)
    app.include_router(agent.browser)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(SecurityHeaders)
    return app


# Same protections whether or not Caddy is in front (local demo, Codespaces). The interactive
# API docs load their scripts from a CDN, so they get no script policy.
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
BASE_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                (b"referrer-policy", b"no-referrer")]


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = list(BASE_HEADERS)
        if not scope["path"].startswith("/api/docs"):
            extra.append((b"content-security-policy", CSP.encode()))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k, v) for k, v in extra if k not in names]
            await send(message)

        return await self.app(scope, receive, send_with_headers)
