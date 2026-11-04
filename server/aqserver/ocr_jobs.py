"""Glue between the database and the OCR pipeline (alignment + reading)."""
from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import OK, REVIEW, Device, Field, Frame, Reading
from .ocr.align import ALIGNER, AlignResult
from .ocr.pipeline import DeviceSpec, FieldSpec, FieldResult, load_image, run_frame
from .plausibility import check_around

LOST_NOTE = "monitor not found where it was marked (camera moved or view blocked)"
# A monitor that can't be matched is still read where it was last seen ("kept") only for this
# long after its last good match. Longer than that, the camera may really have moved (and the
# boxes may be on other digits), so readings go to review and an alert asks for a new reference.
KEEP_FOR_MS = 30 * 60_000


def field_spec(f: Field) -> FieldSpec:
    return FieldSpec(
        id=f.id, metric=f.metric, box=list(f.box), recognizer=f.recognizer, options=dict(f.options or {}),
        min_value=f.min_value, max_value=f.max_value,
    )


def device_spec(d: Device, fields: list[Field]) -> DeviceSpec:
    return DeviceSpec(id=d.id, corners=[list(c) for c in d.corners], warp_w=d.warp_w, warp_h=d.warp_h,
                      fields=[field_spec(f) for f in fields], ref_frame_id=d.ref_frame_id)


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


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def align_specs(db: Session, settings: Settings, frame: Frame, img: np.ndarray,
                specs: list[DeviceSpec]) -> tuple[list[DeviceSpec], dict[int, AlignResult]]:
    """Find each monitor again in this picture. Returns specs with corrected corners and
    the alignment result per device."""
    gray = _gray(img)
    out: list[DeviceSpec] = []
    results: dict[int, AlignResult] = {}
    for spec in specs:
        if spec.ref_frame_id is None or spec.ref_frame_id == frame.id:
            results[spec.id] = AlignResult("ok", spec.corners)
            out.append(spec)
            continue

        def load_ref(ref_id=spec.ref_frame_id):
            ref = db.get(Frame, ref_id)
            if ref is None:
                return None
            try:
                return _gray(load_image(settings.data_dir / ref.path))
            except (OSError, ValueError):
                return None

        key = (spec.id, spec.ref_frame_id, tuple(tuple(c) for c in spec.corners))
        res = ALIGNER.align(key, load_ref, gray, spec.corners)
        if res.status == "lost":
            # Matching can fail when the room light or screen brightness changes a lot, while the
            # camera has not moved at all. Keep reading where the monitor was last seen.
            d = db.get(Device, spec.id)
            last = d.align_corners if d is not None and d.align_corners else spec.corners
            res = AlignResult("lost", last, (d.align_shift_px or 0.0) if d is not None else 0.0, res.inliers)
            good_ms = (d.align_good_ms or d.updated_ms) if d is not None else None  # never matched yet: since marked
            recent_match = good_ms is not None and abs(frame.captured_ms - good_ms) <= KEEP_FOR_MS
            res.may_keep = recent_match
        results[spec.id] = res
        out.append(replace(spec, corners=res.corners) if res.status in ("adjusted", "lost") else spec)
    return out, results


def read_frame(db: Session, settings: Settings, frame: Frame, img: np.ndarray, specs: list[DeviceSpec],
               debug: bool = False, read_only: bool = False) -> tuple[list[FieldResult], dict[int, AlignResult]]:
    """Align, then read every field. When a monitor could not be matched, its numbers are still
    read where it was last seen: if every one of them reads cleanly the reading stands (status
    "kept"); otherwise they all go to review."""
    aligned, align = align_specs(db, settings, frame, img, specs)
    if read_only:
        db.rollback()  # nothing will be written: don't hold a transaction open during the reading
    results = run_frame(img, aligned, settings.review_confidence, debug=debug)
    for dev_id, res in list(align.items()):
        if res.status != "lost":
            continue
        mine = [r for r in results if r.device_id == dev_id]
        if getattr(res, "may_keep", False) and mine and all(r.status == "ok" for r in mine):
            align[dev_id] = AlignResult("kept", res.corners, res.shift_px, res.inliers)
        else:
            for r in mine:
                r.status, r.note = REVIEW, LOST_NOTE
    return results, align


def _bootstrap_reference(db: Session, frame: Frame) -> None:
    """Monitors marked before shift detection existed get this picture as their reference."""
    for d in db.scalars(select(Device).where(Device.camera_id == frame.camera_id, Device.active.is_(True),
                                             Device.ref_frame_id.is_(None))).all():
        d.ref_frame_id = frame.id


def process_frame(db: Session, frame: Frame, settings: Settings, specs: list[DeviceSpec] | None = None) -> int:
    """Read every calibrated field in one stored frame. Returns how many readings were added.
    Fields that already have a reading for this frame (e.g. a human correction) are left alone."""
    if specs is None:
        specs = device_specs(db, frame.camera_id)
    if not specs:
        frame.ocr_status = "skipped"  # camera not calibrated yet; reprocess later
        return 0
    if any(s.ref_frame_id is None for s in specs):
        _bootstrap_reference(db, frame)
        db.flush()
        specs = device_specs(db, frame.camera_id)
    img = load_image(settings.data_dir / frame.path)
    results, align = read_frame(db, settings, frame, img, specs)

    frame.alignment = {str(dev_id): res.as_dict() for dev_id, res in align.items()}
    for dev_id, res in align.items():
        d = db.get(Device, dev_id)
        if d is not None and (d.align_ms is None or frame.captured_ms >= d.align_ms):
            d.align_status = res.status
            d.align_ms = frame.captured_ms
            if res.status in ("kept", "lost"):
                continue  # keep the last known position
            d.align_shift_px = round(res.shift_px, 1)
            d.align_corners = res.corners if res.status == "adjusted" else None
            if res.status in ("ok", "adjusted"):
                d.align_good_ms = frame.captured_ms

    existing = {fid for (fid,) in db.execute(select(Reading.field_id).where(Reading.frame_id == frame.id)).all()}
    added = 0
    for r in results:
        if r.field_id in existing:
            continue
        db.add(Reading(
            frame_id=frame.id, field_id=r.field_id, device_id=r.device_id, metric=r.metric, t_ms=frame.captured_ms,
            raw_text=(r.raw_text or "")[:64], value=r.value, confidence=r.confidence, status=r.status,
            final_value=r.value if r.status == OK else None, note=(r.note or None) and r.note[:255],
        ))
        added += 1
    # A number that jumps away and straight back is almost certainly a misread: send it to review.
    for r in results:
        if r.field_id not in existing:
            check_around(db, r.field_id, r.metric, frame.captured_ms)
    frame.ocr_status = "done"
    frame.ocr_error = None
    return added


def frame_corners(frame: Frame, device: Device) -> list[list[float]]:
    """Corners to cut a device's screen out of a stored frame: the shift-corrected ones
    used when the frame was read, or the marked ones."""
    info = (frame.alignment or {}).get(str(device.id)) or {}
    return info.get("corners") or device.corners
