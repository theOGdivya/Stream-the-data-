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
from .routes import agent, calibration, data, monitor, results, review, strip

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

    for module in (agent, monitor, calibration, review, data, strip, results):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    app.add_middleware(AuthMiddleware, settings=settings)
    return app
