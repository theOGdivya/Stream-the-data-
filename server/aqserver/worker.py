"""Background worker: reads numbers off new frames and runs health checks.

Run with ``python -m aqserver.worker``.
"""
from __future__ import annotations

import logging
import signal
import time

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .alerts import AlertManager
from .config import Settings, load_settings
from .db import init_db, make_engine, make_sessionmaker, now_ms
from .models import Frame
from .ocr_jobs import device_specs, process_frame

log = logging.getLogger("aqserver.worker")


def process_pending(Session: sessionmaker, settings: Settings, batch: int = 10) -> int:
    """Process a batch of pending frames. Newest frames (last 5 min) go first so the live
    dashboard stays current even while old frames are being re-processed."""
    with Session() as db:
        now = now_ms()
        recent = db.scalars(
            select(Frame).where(Frame.ocr_status == "pending", Frame.captured_ms >= now - 300_000)
            .order_by(Frame.captured_ms.desc()).limit(batch)
        ).all()
        older = db.scalars(
            select(Frame).where(Frame.ocr_status == "pending").order_by(Frame.captured_ms).limit(batch)
        ).all()
        frames = list({f.id: f for f in [*recent, *older]}.values())
        specs_cache: dict[str, list] = {}
        for frame in frames:
            try:
                if frame.camera_id not in specs_cache:
                    specs_cache[frame.camera_id] = device_specs(db, frame.camera_id)
                process_frame(db, frame, settings, specs_cache[frame.camera_id])
                db.commit()
            except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the queue
                log.exception("OCR failed for frame %s", frame.id)
                db.rollback()
                bad = db.get(Frame, frame.id)
                if bad is not None:
                    bad.ocr_status = "error"
                    bad.ocr_error = str(exc)[:1000]
                    db.commit()
        return len(frames)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    Session = make_sessionmaker(engine)
    alerts = AlertManager(settings)
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started (database: %s)", engine.url.render_as_string(hide_password=True))
    last_check = 0.0
    while not stop["flag"]:
        n = process_pending(Session, settings)
        if time.time() - last_check > 30:
            try:
                with Session() as db:
                    alerts.sync(db, now_ms())
            except Exception:  # noqa: BLE001
                log.exception("alert check failed")
            last_check = time.time()
        if n == 0:
            time.sleep(1.0)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
