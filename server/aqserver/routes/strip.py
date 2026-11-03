"""Lined-up comparison strip endpoints."""
from __future__ import annotations

import cv2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import audit, current_user, get_db, get_settings
from ..models import Device, Frame, TestRun
from ..storage import iso
from ..strip import gather, render_png, screen_image, strip_devices

router = APIRouter(prefix="/api", tags=["strip"])


@router.get("/strip")
def strip(t_ms: int | None = None, db: Session = Depends(get_db)):
    """Every monitor at one moment (newest if no time is given)."""
    return gather(db, t_ms)


@router.get("/strip/times")
def strip_times(since_ms: int, until_ms: int, max_points: int = 2000, db: Session = Depends(get_db)):
    """Moments that have pictures (from the camera of the first monitor), for the time slider."""
    devices = strip_devices(db)
    if not devices:
        return {"times": []}
    lead = devices[0].camera_id
    times = [t for (t,) in db.execute(
        select(Frame.captured_ms).where(Frame.camera_id == lead, Frame.captured_ms >= since_ms, Frame.captured_ms <= until_ms)
        .order_by(Frame.captured_ms)).all()]
    if len(times) > max_points:
        step = len(times) / max_points
        times = [times[int(i * step)] for i in range(max_points)]
    return {"times": times, "camera_id": lead}


@router.get("/frames/{frame_id}/screen/{device_id}.jpg")
def frame_screen(frame_id: int, device_id: int, h: int = 300, db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)):
    """One monitor's straightened screen from one stored picture (shift-corrected)."""
    frame, dev = db.get(Frame, frame_id), db.get(Device, device_id)
    if frame is None or dev is None or frame.camera_id != dev.camera_id:
        raise HTTPException(404, "no such monitor in that picture")
    img = screen_image(settings, frame, dev, max(60, min(h, 1200)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/strip.png")
def strip_png(t_ms: int | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The lined-up strip as one picture, e.g. for a report figure."""
    data = gather(db, t_ms)
    if not data["tiles"]:
        raise HTTPException(404, "no monitors marked yet")
    run = None
    if data["t_ms"]:
        run = db.scalars(select(TestRun).where(TestRun.started_ms <= data["t_ms"])
                         .where((TestRun.ended_ms.is_(None)) | (TestRun.ended_ms >= data["t_ms"]))
                         .order_by(TestRun.started_ms.desc()).limit(1)).first()
    png = render_png(settings, db, data, run.name if run else None)
    name = f"lineup-{iso(data['t_ms']).replace(':', '').replace('.', '-')}.png" if data["t_ms"] else "lineup.png"
    return Response(png, media_type="image/png", headers={"Content-Disposition": f'inline; filename="{name}"',
                                                          "Cache-Control": "private, max-age=60"})


class OrderIn(BaseModel):
    device_ids: list[int]


@router.post("/strip/order")
def set_order(body: OrderIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Set the left-to-right order of monitors in the strip."""
    for pos, dev_id in enumerate(body.device_ids):
        d = db.get(Device, dev_id)
        if d is not None:
            d.position = pos
    audit(db, user, "strip.order", device_ids=body.device_ids)
    db.commit()
    return {"ok": True}
