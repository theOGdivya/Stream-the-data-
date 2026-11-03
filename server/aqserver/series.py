"""Time series averaged into fixed time buckets (shared by the Live charts and Results)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import GOOD_STATUSES, REVIEW, Device, Reading, ReferenceReading

NICE_BUCKETS_S = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400]


def choose_bucket_ms(span_ms: int, max_points: int) -> int:
    want = span_ms / max(1, max_points)
    for s in NICE_BUCKETS_S:
        if s * 1000 >= want:
            return s * 1000
    return NICE_BUCKETS_S[-1] * 1000


def bucketed(db: Session, metric: str, since_ms: int, until_ms: int, bucket_ms: int,
             include_review: bool = False) -> list[dict]:
    """One series per monitor read by camera (only accepted/confirmed/corrected values unless
    ``include_review``) and per reference source+device. Points are [bucket_start_ms, mean]."""
    statuses = list(GOOD_STATUSES) + ([REVIEW] if include_review else [])
    value = func.coalesce(Reading.final_value, Reading.value)
    b = (Reading.t_ms // bucket_ms).label("b")
    rows = db.execute(
        select(Reading.device_id, b, func.avg(value))
        .where(Reading.metric == metric, Reading.t_ms >= since_ms, Reading.t_ms <= until_ms,
               Reading.status.in_(statuses), value.is_not(None))
        .group_by(Reading.device_id, b)
        .order_by(b)
    ).all()
    devices = {d.id: d for d in db.scalars(select(Device)).all()}
    out: dict[str, dict] = {}
    for device_id, bucket_no, avg in rows:
        d = devices.get(device_id)
        key = f"dev:{device_id}"
        s = out.setdefault(key, {
            "key": key, "kind": "camera", "device_id": device_id,
            "label": d.name if d else f"device {device_id}", "model": d.model if d else None,
            "is_own": bool(d and d.is_own), "external_id": d.external_id if d else None,
            "camera_id": d.camera_id if d else None, "points": [],
        })
        s["points"].append([int(bucket_no) * bucket_ms, round(float(avg), 3)])

    rb = (ReferenceReading.t_ms // bucket_ms).label("b")
    ref_rows = db.execute(
        select(ReferenceReading.source, ReferenceReading.device_ref, rb, func.avg(ReferenceReading.value))
        .where(ReferenceReading.metric == metric, ReferenceReading.t_ms >= since_ms, ReferenceReading.t_ms <= until_ms)
        .group_by(ReferenceReading.source, ReferenceReading.device_ref, rb)
        .order_by(rb)
    ).all()
    for source, device_ref, bucket_no, avg in ref_rows:
        key = f"ref:{source}:{device_ref}"
        s = out.setdefault(key, {"key": key, "kind": "reference", "label": f"{device_ref} ({source})",
                                 "source": source, "device_ref": device_ref, "points": []})
        s["points"].append([int(bucket_no) * bucket_ms, round(float(avg), 3)])
    return list(out.values())
