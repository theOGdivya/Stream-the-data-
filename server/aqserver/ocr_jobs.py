"""Glue between the database and the OCR pipeline."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import OK, Device, Field, Frame, Reading
from .ocr.pipeline import DeviceSpec, FieldSpec, load_image, run_frame


def field_spec(f: Field) -> FieldSpec:
    return FieldSpec(
        id=f.id, metric=f.metric, box=list(f.box), recognizer=f.recognizer, options=dict(f.options or {}),
        min_value=f.min_value, max_value=f.max_value,
    )


def device_spec(d: Device, fields: list[Field]) -> DeviceSpec:
    return DeviceSpec(id=d.id, corners=[list(c) for c in d.corners], warp_w=d.warp_w, warp_h=d.warp_h,
                      fields=[field_spec(f) for f in fields])


def device_specs(db: Session, camera_id: str) -> list[DeviceSpec]:
    devices = db.scalars(
        select(Device).where(Device.camera_id == camera_id, Device.active.is_(True)).order_by(Device.id)
    ).all()
    specs = []
    for d in devices:
        fields = db.scalars(select(Field).where(Field.device_id == d.id, Field.active.is_(True)).order_by(Field.id)).all()
        if fields:
            specs.append(device_spec(d, list(fields)))
    return specs


def process_frame(db: Session, frame: Frame, settings: Settings, specs: list[DeviceSpec] | None = None) -> int:
    """Read every calibrated field in one stored frame. Returns how many readings were added.
    Fields that already have a reading for this frame (e.g. a human correction) are left alone."""
    if specs is None:
        specs = device_specs(db, frame.camera_id)
    if not specs:
        frame.ocr_status = "skipped"  # camera not calibrated yet; reprocess later
        return 0
    img = load_image(settings.data_dir / frame.path)
    existing = {fid for (fid,) in db.execute(select(Reading.field_id).where(Reading.frame_id == frame.id)).all()}
    added = 0
    for r in run_frame(img, specs, settings.review_confidence):
        if r.field_id in existing:
            continue
        db.add(Reading(
            frame_id=frame.id, field_id=r.field_id, device_id=r.device_id, metric=r.metric, t_ms=frame.captured_ms,
            raw_text=(r.raw_text or "")[:64], value=r.value, confidence=r.confidence, status=r.status,
            final_value=r.value if r.status == OK else None, note=(r.note or None) and r.note[:255],
        ))
        added += 1
    frame.ocr_status = "done"
    frame.ocr_error = None
    return added
