"""Background worker: reads numbers off new frames and runs health checks.

Run with ``python -m aqserver.worker``.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .alerts import AlertManager
from .config import Settings, load_settings
from .db import init_db, make_engine, make_sessionmaker, now_ms
from .models import Frame
from .ocr_jobs import device_specs, process_frame

log = logging.getLogger("aqserver.worker")


def _process_camera(Session: sessionmaker, settings: Settings, frame_ids: list[int]) -> None:
    """Read one camera's frames in order (in its own database session)."""
    with Session() as db:
        specs = None
        for frame_id in frame_ids:
            frame = db.get(Frame, frame_id)
            if frame is None or frame.ocr_status != "pending":
                continue
            try:
                if specs is None:
                    specs = device_specs(db, frame.camera_id)
                process_frame(db, frame, settings, specs)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - one bad frame must not stop the queue
                log.exception("OCR failed for frame %s", frame_id)
                db.rollback()
                bad = db.get(Frame, frame_id)
                if bad is not None:
                    bad.ocr_status = "error"
                    bad.ocr_error = str(exc)[:1000]
                    db.commit()


def process_pending(Session: sessionmaker, settings: Settings, batch: int = 10, threads: int | None = None) -> int:
    """Process a batch of pending frames. Newest frames (last 5 min) go first so the live
    dashboard stays current even while old frames are being re-processed. Cameras are read
    in parallel (one thread each, frames of one camera in order), so several cameras do not
    queue behind each other."""
    with Session() as db:
        now = now_ms()
        recent = db.execute(
            select(Frame.id, Frame.camera_id).where(Frame.ocr_status == "pending", Frame.captured_ms >= now - 300_000)
            .order_by(Frame.captured_ms.desc()).limit(batch)
        ).all()
        older = db.execute(
            select(Frame.id, Frame.camera_id).where(Frame.ocr_status == "pending").order_by(Frame.captured_ms).limit(batch)
        ).all()
    by_camera: dict[str, list[int]] = {}
    for frame_id, camera_id in dict.fromkeys([*recent, *older]):
        by_camera.setdefault(camera_id, []).append(frame_id)
    total = sum(len(v) for v in by_camera.values())
    if not total:
        return 0
    workers = min(len(by_camera), threads or max(1, min(4, os.cpu_count() or 1)))
    if workers <= 1:
        for ids in by_camera.values():
            _process_camera(Session, settings, ids)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr") as pool:
            for f in [pool.submit(_process_camera, Session, settings, ids) for ids in by_camera.values()]:
                f.result()
    return total


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
