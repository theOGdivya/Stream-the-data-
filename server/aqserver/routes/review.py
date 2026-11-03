"""Human review of uncertain readings. Every decision is written to the audit log."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import now_ms
from ..deps import audit, current_user, get_db
from ..models import CORRECTED, OK, REJECTED, REVIEW, VERIFIED, Device, Field, Reading

router = APIRouter(prefix="/api", tags=["review"])


def _reading_dict(r: Reading, d: Device | None, f: Field | None) -> dict:
    return {"id": r.id, "t_ms": r.t_ms, "frame_id": r.frame_id, "device_id": r.device_id,
            "device_name": d.name if d else None, "camera_id": d.camera_id if d else None, "field_id": r.field_id,
            "metric": r.metric, "unit": f.unit if f else None, "raw_text": r.raw_text, "value": r.value,
            "final_value": r.final_value, "confidence": r.confidence, "status": r.status, "note": r.note,
            "reviewed_by": r.reviewed_by, "reviewed_ms": r.reviewed_ms}


@router.get("/review")
def review_queue(limit: int = 50, camera_id: str | None = None, metric: str | None = None,
                 order: Literal["newest", "oldest"] = "newest", db: Session = Depends(get_db)):
    q = select(Reading, Device, Field).join(Device, Device.id == Reading.device_id).join(Field, Field.id == Reading.field_id)
    q = q.where(Reading.status == REVIEW)
    if camera_id:
        q = q.where(Device.camera_id == camera_id)
    if metric:
        q = q.where(Reading.metric == metric)
    q = q.order_by(Reading.t_ms.desc() if order == "newest" else Reading.t_ms).limit(min(limit, 500))
    items = [_reading_dict(r, d, f) for r, d, f in db.execute(q).all()]
    total = db.scalar(select(func.count()).select_from(Reading).where(Reading.status == REVIEW)) or 0
    return {"total": total, "items": items}


@router.get("/review/stats")
def review_stats(since_ms: int | None = None, until_ms: int | None = None, db: Session = Depends(get_db)):
    """How many readings were accepted automatically vs. needed a person. Useful for the
    methods section of a report ("x % of readings were machine-read without correction")."""
    q = select(Reading.metric, Reading.status, func.count()).group_by(Reading.metric, Reading.status)
    if since_ms is not None:
        q = q.where(Reading.t_ms >= since_ms)
    if until_ms is not None:
        q = q.where(Reading.t_ms <= until_ms)
    by_metric: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for metric, status, n in db.execute(q).all():
        by_metric.setdefault(metric, {})[status] = n
        totals[status] = totals.get(status, 0) + n
    return {"totals": totals, "by_metric": by_metric}


class ReviewIn(BaseModel):
    action: Literal["accept", "correct", "reject"]
    value: float | None = None


@router.post("/readings/{reading_id}/review")
def review_reading(reading_id: int, body: ReviewIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    r = db.get(Reading, reading_id)
    if r is None:
        raise HTTPException(404, "reading not found")
    before = {"status": r.status, "final_value": r.final_value}
    if body.action == "accept":
        if r.value is None:
            raise HTTPException(400, "nothing to accept: OCR could not read a number; type the value instead")
        r.status, r.final_value = VERIFIED, r.value
    elif body.action == "correct":
        if body.value is None:
            raise HTTPException(400, "value is required")
        r.status, r.final_value = (VERIFIED if r.value == body.value else CORRECTED), body.value
    else:
        r.status, r.final_value = REJECTED, None
    r.reviewed_by, r.reviewed_ms = user, now_ms()
    audit(db, user, "reading.review", reading_id=r.id, action=body.action, before=before,
          after={"status": r.status, "final_value": r.final_value}, ocr_text=r.raw_text)
    db.commit()
    return {"id": r.id, "status": r.status, "final_value": r.final_value}


@router.post("/readings/{reading_id}/flag")
def flag_reading(reading_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Send an automatically accepted reading back to the review queue (e.g. a suspicious spike)."""
    r = db.get(Reading, reading_id)
    if r is None:
        raise HTTPException(404, "reading not found")
    if r.status != OK:
        raise HTTPException(400, f"reading is already {r.status}")
    r.status, r.final_value, r.note = REVIEW, None, f"flagged by {user}"
    audit(db, user, "reading.flag", reading_id=r.id)
    db.commit()
    return {"id": r.id, "status": r.status}


@router.get("/readings")
def list_readings(device_id: int | None = None, metric: str | None = None, since_ms: int | None = None,
                  until_ms: int | None = None, limit: int = 200, db: Session = Depends(get_db)):
    q = select(Reading, Device, Field).join(Device, Device.id == Reading.device_id).join(Field, Field.id == Reading.field_id)
    if device_id is not None:
        q = q.where(Reading.device_id == device_id)
    if metric:
        q = q.where(Reading.metric == metric)
    if since_ms is not None:
        q = q.where(Reading.t_ms >= since_ms)
    if until_ms is not None:
        q = q.where(Reading.t_ms <= until_ms)
    rows = db.execute(q.order_by(Reading.t_ms.desc()).limit(min(limit, 2000))).all()
    return [_reading_dict(r, d, f) for r, d, f in rows]
