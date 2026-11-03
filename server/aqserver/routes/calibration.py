"""Calibration: tell the system where each monitor's screen and numbers are in a camera image."""
from __future__ import annotations

import base64
from typing import Literal

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field as PField, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import now_ms
from ..deps import audit, current_user, get_db, get_settings
from ..models import OK, REVIEW, Camera, Device, Field, Frame, Reading
from ..ocr.pipeline import METRICS, RECOGNIZERS, crop_box, load_image, warp_device, warp_size
from ..ocr_jobs import device_specs, frame_corners, read_frame

router = APIRouter(prefix="/api", tags=["calibration"])

Polarity = Literal["auto", "dark_on_light", "light_on_dark"]


class FieldOptions(BaseModel):
    polarity: Polarity = "auto"
    threshold: int | None = PField(default=None, ge=0, le=255)
    slant: float = PField(default=0.0, ge=-30, le=30)
    decimals: int | Literal["auto"] = 0

    @field_validator("decimals")
    @classmethod
    def _dec(cls, v):
        if isinstance(v, int) and not 0 <= v <= 4:
            raise ValueError("decimals must be 0-4 or 'auto'")
        return v


def _check_corners(v: list[list[float]]) -> list[list[float]]:
    if len(v) != 4 or any(len(p) != 2 for p in v):
        raise ValueError("corners must be 4 [x, y] points: top-left, top-right, bottom-right, bottom-left")
    return [[float(p[0]), float(p[1])] for p in v]


def _check_box(v: list[float]) -> list[float]:
    if len(v) != 4:
        raise ValueError("box must be [x, y, w, h]")
    x, y, w, h = (float(n) for n in v)
    if not (0 <= x < 1 and 0 <= y < 1 and 0.003 < w <= 1 and 0.003 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
        raise ValueError("box must be inside the screen, as fractions 0..1")
    return [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]


class DeviceIn(BaseModel):
    name: str = PField(min_length=1, max_length=255)
    model: str | None = None
    is_own: bool = False
    external_id: str | None = None
    corners: list[list[float]]
    ref_frame_id: int | None = None  # the picture the corners were clicked on

    @field_validator("corners")
    @classmethod
    def _c(cls, v):
        return _check_corners(v)


class DevicePatch(BaseModel):
    name: str | None = None
    model: str | None = None
    is_own: bool | None = None
    external_id: str | None = None
    corners: list[list[float]] | None = None
    ref_frame_id: int | None = None
    position: int | None = None

    @field_validator("corners")
    @classmethod
    def _c(cls, v):
        return None if v is None else _check_corners(v)


class FieldIn(BaseModel):
    metric: str = PField(min_length=1, max_length=32)
    label: str | None = None
    unit: str | None = None
    box: list[float]
    recognizer: Literal["sevenseg", "tesseract"] = "sevenseg"
    options: FieldOptions = FieldOptions()
    min_value: float | None = None
    max_value: float | None = None

    @field_validator("box")
    @classmethod
    def _b(cls, v):
        return _check_box(v)


class FieldPatch(BaseModel):
    metric: str | None = None
    label: str | None = None
    unit: str | None = None
    box: list[float] | None = None
    recognizer: Literal["sevenseg", "tesseract"] | None = None
    options: FieldOptions | None = None
    min_value: float | None = None
    max_value: float | None = None

    @field_validator("box")
    @classmethod
    def _b(cls, v):
        return None if v is None else _check_box(v)


class CameraPatch(BaseModel):
    name: str | None = None
    mask_enabled: bool | None = None
    full_every_s: int | None = PField(default=None, ge=30, le=86400)


def field_dict(f: Field) -> dict:
    return {"id": f.id, "device_id": f.device_id, "metric": f.metric, "label": f.label, "unit": f.unit, "box": f.box,
            "recognizer": f.recognizer, "options": f.options or {}, "min_value": f.min_value, "max_value": f.max_value}


def device_dict(db: Session, d: Device) -> dict:
    fields = db.scalars(select(Field).where(Field.device_id == d.id, Field.active.is_(True)).order_by(Field.id)).all()
    return {"id": d.id, "camera_id": d.camera_id, "name": d.name, "model": d.model, "is_own": d.is_own,
            "external_id": d.external_id, "corners": d.corners, "warp_w": d.warp_w, "warp_h": d.warp_h,
            "position": d.position, "ref_frame_id": d.ref_frame_id,
            "alignment": {"status": d.align_status, "shift_px": d.align_shift_px, "corners": d.align_corners,
                          "checked_ms": d.align_ms},
            "fields": [field_dict(f) for f in fields]}


def latest_frame(db: Session, camera_id: str, full: bool = False) -> Frame | None:
    """Newest stored frame; with ``full`` the newest one that is not blacked out (if any)."""
    q = select(Frame).where(Frame.camera_id == camera_id)
    if full:
        f = db.scalars(q.where(Frame.masked.is_not(True)).order_by(Frame.captured_ms.desc()).limit(1)).first()
        if f is not None:
            return f
    return db.scalars(q.order_by(Frame.captured_ms.desc()).limit(1)).first()


def _frame_or_latest(db: Session, camera_id: str, frame_id: int | None) -> Frame:
    frame = db.get(Frame, frame_id) if frame_id else latest_frame(db, camera_id, full=True)
    if frame is None or frame.camera_id != camera_id:
        raise HTTPException(404, "no frame for this camera yet")
    return frame


def _reference_frame_id(db: Session, camera_id: str, requested: int | None) -> int | None:
    if requested is not None:
        f = db.get(Frame, requested)
        if f is None or f.camera_id != camera_id:
            raise HTTPException(400, "ref_frame_id is not a picture from this camera")
        return f.id
    f = latest_frame(db, camera_id, full=True)
    return f.id if f else None


def _reset_alignment(d: Device) -> None:
    d.align_status = d.align_shift_px = d.align_corners = d.align_ms = None


def _png_data_url(img: np.ndarray | None) -> str | None:
    if img is None or img.size == 0:
        return None
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode() if ok else None


@router.get("/catalog")
def catalog():
    return {"metrics": METRICS, "recognizers": list(RECOGNIZERS)}


@router.get("/cameras")
def list_cameras(db: Session = Depends(get_db)):
    out = []
    for cam in db.scalars(select(Camera).order_by(Camera.id)).all():
        latest = latest_frame(db, cam.id, full=True)
        devices = db.scalars(select(Device).where(Device.camera_id == cam.id, Device.active.is_(True))
                             .order_by(Device.position.is_(None), Device.position, Device.id)).all()
        out.append({"id": cam.id, "name": cam.name, "agent_id": cam.agent_id, "last_frame_ms": cam.last_frame_ms,
                    "still_interval_s": cam.still_interval_s, "mask_enabled": bool(cam.mask_enabled),
                    "full_every_s": cam.full_every_s or 300,
                    "latest_frame_id": latest.id if latest else None,
                    "latest_frame_size": [latest.width, latest.height] if latest else None,
                    "devices": [device_dict(db, d) for d in devices]})
    return out


@router.patch("/cameras/{camera_id}")
def edit_camera(camera_id: str, body: CameraPatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    cam = db.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(404, "camera not found")
    changes = body.model_dump(exclude_unset=True)
    for key, val in changes.items():
        setattr(cam, key, val)
    audit(db, user, "camera.edit", camera_id=camera_id, changes=changes)
    db.commit()
    return {"id": cam.id, "name": cam.name, "mask_enabled": bool(cam.mask_enabled), "full_every_s": cam.full_every_s or 300}


@router.post("/cameras/{camera_id}/devices")
def create_device(camera_id: str, body: DeviceIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    if db.get(Camera, camera_id) is None:
        raise HTTPException(404, "camera not found")
    try:
        w, h = warp_size(body.corners)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    now = now_ms()
    d = Device(camera_id=camera_id, name=body.name, model=body.model, is_own=body.is_own, external_id=body.external_id,
               corners=body.corners, warp_w=w, warp_h=h, created_ms=now, updated_ms=now,
               ref_frame_id=_reference_frame_id(db, camera_id, body.ref_frame_id))
    db.add(d)
    db.flush()
    audit(db, user, "device.create", device_id=d.id, camera_id=camera_id, name=body.name, corners=body.corners,
          ref_frame_id=d.ref_frame_id)
    db.commit()
    return device_dict(db, d)


@router.patch("/devices/{device_id}")
def edit_device(device_id: int, body: DevicePatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    d = db.get(Device, device_id)
    if d is None or not d.active:
        raise HTTPException(404, "device not found")
    before = {"name": d.name, "corners": d.corners, "ref_frame_id": d.ref_frame_id}
    changes = body.model_dump(exclude_unset=True)
    ref = changes.pop("ref_frame_id", None)
    for key, val in changes.items():
        setattr(d, key, val)
    if "corners" in changes:
        try:
            d.warp_w, d.warp_h = warp_size(d.corners)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        d.ref_frame_id = _reference_frame_id(db, d.camera_id, ref)
        _reset_alignment(d)
    d.updated_ms = now_ms()
    audit(db, user, "device.edit", device_id=d.id, before=before, changes=changes)
    db.commit()
    return device_dict(db, d)


@router.post("/devices/{device_id}/accept-alignment")
def accept_alignment(device_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """The monitor or camera was moved on purpose: make the automatically found position the
    new marked position, with the picture it was found in as the new reference."""
    d = db.get(Device, device_id)
    if d is None or not d.active:
        raise HTTPException(404, "device not found")
    if d.align_status != "adjusted" or not d.align_corners or d.align_ms is None:
        raise HTTPException(400, "nothing to accept: this monitor has not shifted")
    frame = db.scalars(select(Frame).where(Frame.camera_id == d.camera_id, Frame.captured_ms == d.align_ms).limit(1)).first()
    before = {"corners": d.corners, "ref_frame_id": d.ref_frame_id}
    d.corners = [[round(x, 1), round(y, 1)] for x, y in d.align_corners]
    d.warp_w, d.warp_h = warp_size(d.corners)
    d.ref_frame_id = frame.id if frame else _reference_frame_id(db, d.camera_id, None)
    _reset_alignment(d)
    d.updated_ms = now_ms()
    audit(db, user, "device.accept_alignment", device_id=d.id, name=d.name, before=before,
          after={"corners": d.corners, "ref_frame_id": d.ref_frame_id})
    db.commit()
    return device_dict(db, d)


@router.delete("/devices/{device_id}")
def remove_device(device_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    d = db.get(Device, device_id)
    if d is None:
        raise HTTPException(404, "device not found")
    d.active = False  # keep the row: past readings point at it
    db.execute(update(Field).where(Field.device_id == d.id).values(active=False))
    audit(db, user, "device.remove", device_id=d.id, name=d.name)
    db.commit()
    return {"ok": True}


@router.post("/devices/{device_id}/fields")
def create_field(device_id: int, body: FieldIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    d = db.get(Device, device_id)
    if d is None or not d.active:
        raise HTTPException(404, "device not found")
    f = Field(device_id=d.id, metric=body.metric, label=body.label, unit=body.unit, box=body.box,
              recognizer=body.recognizer, options=body.options.model_dump(), min_value=body.min_value,
              max_value=body.max_value, updated_ms=now_ms())
    db.add(f)
    db.flush()
    audit(db, user, "field.create", field=field_dict(f))
    db.commit()
    return field_dict(f)


@router.patch("/fields/{field_id}")
def edit_field(field_id: int, body: FieldPatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    f = db.get(Field, field_id)
    if f is None or not f.active:
        raise HTTPException(404, "field not found")
    before = field_dict(f)
    changes = body.model_dump(exclude_unset=True)
    for key, val in changes.items():
        setattr(f, key, val)
    f.updated_ms = now_ms()
    audit(db, user, "field.edit", field_id=f.id, before=before, after=field_dict(f))
    db.commit()
    return field_dict(f)


@router.delete("/fields/{field_id}")
def remove_field(field_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    f = db.get(Field, field_id)
    if f is None:
        raise HTTPException(404, "field not found")
    f.active = False
    audit(db, user, "field.remove", field_id=f.id)
    db.commit()
    return {"ok": True}


@router.get("/devices/{device_id}/screen.jpg")
def device_screen(device_id: int, frame_id: int | None = None, db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)):
    """The device's screen, straightened, from a frame (default: newest full picture).
    Uses the shift-corrected corners for that frame when known. Field boxes are drawn on this."""
    d = db.get(Device, device_id)
    if d is None:
        raise HTTPException(404, "device not found")
    frame = _frame_or_latest(db, d.camera_id, frame_id)
    warped = warp_device(load_image(settings.data_dir / frame.path), frame_corners(frame, d), d.warp_w, d.warp_h)
    ok, buf = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return Response(buf.tobytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-Id": str(frame.id), "X-Captured-Ms": str(frame.captured_ms)})


class OcrTestIn(BaseModel):
    camera_id: str
    frame_id: int | None = None


@router.post("/ocr/test")
def ocr_test(body: OcrTestIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Run the current calibration (including shift detection) on one frame without saving anything."""
    frame = _frame_or_latest(db, body.camera_id, body.frame_id)
    specs = device_specs(db, body.camera_id)
    img = load_image(settings.data_dir / frame.path)
    results, align = read_frame(db, settings, frame, img, specs, debug=True)
    return {
        "frame_id": frame.id,
        "captured_ms": frame.captured_ms,
        "alignment": {str(k): v.as_dict() for k, v in align.items()},
        "results": [
            {"device_id": r.device_id, "field_id": r.field_id, "metric": r.metric, "raw_text": r.raw_text,
             "value": r.value, "confidence": r.confidence, "status": r.status, "note": r.note,
             "crop": _png_data_url(r.crop), "binary": _png_data_url(r.binary)}
            for r in results
        ],
    }


class ReprocessIn(BaseModel):
    since_ms: int | None = None
    until_ms: int | None = None


@router.post("/cameras/{camera_id}/reprocess")
def reprocess(camera_id: str, body: ReprocessIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Re-read past frames with the current calibration. Human-reviewed readings are kept."""
    cond = [Frame.camera_id == camera_id]
    if body.since_ms is not None:
        cond.append(Frame.captured_ms >= body.since_ms)
    if body.until_ms is not None:
        cond.append(Frame.captured_ms <= body.until_ms)
    frame_ids = select(Frame.id).where(*cond)
    db.execute(delete(Reading).where(Reading.frame_id.in_(frame_ids), Reading.status.in_([OK, REVIEW])))
    result = db.execute(update(Frame).where(*cond).values(ocr_status="pending", ocr_error=None))
    audit(db, user, "camera.reprocess", camera_id=camera_id, since_ms=body.since_ms, until_ms=body.until_ms,
          frames=result.rowcount)
    db.commit()
    return {"frames_queued": result.rowcount}


@router.get("/cameras/{camera_id}/latest-frame")
def latest_frame_info(camera_id: str, full: bool = True, db: Session = Depends(get_db)):
    f = latest_frame(db, camera_id, full=full)
    if f is None:
        raise HTTPException(404, "no frames yet")
    return {"id": f.id, "captured_ms": f.captured_ms, "width": f.width, "height": f.height, "masked": bool(f.masked)}


@router.get("/readings/{reading_id}/crop.png")
def reading_crop(reading_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """The screen region a reading came from (re-cut from the stored frame with the field's box)."""
    r = db.get(Reading, reading_id)
    if r is None:
        raise HTTPException(404, "reading not found")
    frame, field, dev = db.get(Frame, r.frame_id), db.get(Field, r.field_id), db.get(Device, r.device_id)
    warped = warp_device(load_image(settings.data_dir / frame.path), frame_corners(frame, dev), dev.warp_w, dev.warp_h)
    x, y, w, h = field.box
    pad_x, pad_y = w * 0.15, h * 0.25  # a little context around the number
    crop = crop_box(warped, [max(0, x - pad_x), max(0, y - pad_y), min(1 - max(0, x - pad_x), w + 2 * pad_x),
                             min(1 - max(0, y - pad_y), h + 2 * pad_y)])
    if crop.shape[0] < 120:
        s = 120 / crop.shape[0]
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", crop)
    return Response(buf.tobytes(), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
