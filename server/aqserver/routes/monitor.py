"""Dashboard API: overview, live images, frames, time series, test runs, alerts."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field as PField
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import now_ms
from ..deps import audit, current_user, get_db, get_live, get_settings
from ..live import LiveHub
from ..models import (
    REVIEW,
    Agent,
    Alert,
    Camera,
    Device,
    Field,
    Frame,
    Reading,
    ReferenceReading,
    TestRun,
)
from ..series import bucketed, choose_bucket_ms
from ..storage import disk_usage

router = APIRouter(prefix="/api", tags=["monitor"])

def _run_dict(r: TestRun | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "name": r.name, "notes": r.notes, "started_ms": r.started_ms, "ended_ms": r.ended_ms,
            "created_by": r.created_by}


def _frame_file(settings: Settings, frame: Frame) -> Path:
    path = (settings.data_dir / frame.path).resolve()
    if settings.data_dir not in path.parents or not path.exists():
        raise HTTPException(404, "frame file missing")
    return path


@router.get("/overview")
def overview(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    live: LiveHub = Depends(get_live),
):
    now = now_ms()
    offline_ms = settings.offline_after_s * 1000
    agents = db.scalars(select(Agent).order_by(Agent.id)).all()
    agent_online = {a.id: bool(a.last_seen_ms and now - a.last_seen_ms < offline_ms) for a in agents}

    devices = db.scalars(select(Device).where(Device.active.is_(True)).order_by(Device.id)).all()
    fields = db.scalars(select(Field).where(Field.active.is_(True)).order_by(Field.id)).all()
    fields_by_device: dict[int, list[Field]] = {}
    for f in fields:
        fields_by_device.setdefault(f.device_id, []).append(f)

    def latest_reading(field_id: int) -> dict | None:
        r = db.scalars(
            select(Reading).where(Reading.field_id == field_id).order_by(Reading.t_ms.desc()).limit(1)
        ).first()
        if r is None:
            return None
        return {"id": r.id, "t_ms": r.t_ms, "raw_text": r.raw_text, "value": r.final_value if r.final_value is not None else r.value,
                "status": r.status, "confidence": r.confidence, "note": r.note}

    hour_ago = now - 3_600_000
    frames_last_hour, avg_size = {}, {}
    for cam_id, n, avg in db.execute(
        select(Frame.camera_id, func.count(), func.avg(Frame.size_bytes))
        .where(Frame.captured_ms >= hour_ago).group_by(Frame.camera_id)
    ).all():
        frames_last_hour[cam_id] = n
        avg_size[cam_id] = int(avg) if avg is not None else None
    cameras = []
    for cam in db.scalars(select(Camera).order_by(Camera.id)).all():
        status = cam.status or {}
        online = bool(
            cam.agent_id
            and agent_online.get(cam.agent_id)
            and status.get("connected")
            and cam.status_ms
            and now - cam.status_ms < offline_ms
        )
        latest = db.execute(
            select(Frame.id, Frame.captured_ms).where(Frame.camera_id == cam.id).order_by(Frame.captured_ms.desc()).limit(1)
        ).first()
        cam_devices = []
        for d in devices:
            if d.camera_id != cam.id:
                continue
            cam_devices.append({
                "id": d.id, "name": d.name, "model": d.model, "is_own": d.is_own, "external_id": d.external_id,
                "alignment": {"status": d.align_status, "shift_px": d.align_shift_px, "checked_ms": d.align_ms},
                "fields": [
                    {"id": f.id, "metric": f.metric, "label": f.label, "unit": f.unit, "latest": latest_reading(f.id)}
                    for f in fields_by_device.get(d.id, [])
                ],
            })
        # Expected stills in the last hour (or since the camera first appeared, if sooner).
        window_s = min(3600.0, max(0.0, (now - cam.created_ms) / 1000))
        expected = window_s / cam.still_interval_s if cam.still_interval_s else None
        cameras.append({
            "id": cam.id,
            "name": cam.name,
            "agent_id": cam.agent_id,
            "online": online,
            "status": status,
            "status_ms": cam.status_ms,
            "still_interval_s": cam.still_interval_s,
            "last_frame_ms": cam.last_frame_ms,
            "latest_frame_id": latest[0] if latest else None,
            "frames_last_hour": frames_last_hour.get(cam.id, 0),
            "avg_still_bytes": avg_size.get(cam.id),
            "stopped": bool(cam.managed and not cam.managed.get("enabled", True)),
            "source": (cam.managed or {}).get("source", status.get("source")),
            "mask_enabled": bool(cam.mask_enabled),
            "expected_last_hour": round(expected) if expected else None,
            "watched": live.is_watched(cam.id),
            "devices": cam_devices,
        })

    active_run = db.scalars(select(TestRun).where(TestRun.ended_ms.is_(None)).order_by(TestRun.started_ms.desc())).first()
    alerts = db.scalars(select(Alert).where(Alert.resolved_ms.is_(None)).order_by(Alert.opened_ms.desc())).all()
    pending = db.scalar(select(func.count()).select_from(Frame).where(Frame.ocr_status == "pending")) or 0
    review = db.scalar(select(func.count()).select_from(Reading).where(Reading.status == REVIEW)) or 0
    return {
        "server_time_ms": now,
        "user": getattr(request.state, "user", None),
        "run": _run_dict(active_run),
        "disk": disk_usage(settings.data_dir),
        "agents": [
            {"id": a.id, "hostname": a.hostname, "version": a.version, "online": agent_online[a.id],
             "last_seen_ms": a.last_seen_ms, "clock_offset_ms": a.clock_offset_ms, "backlog": a.backlog,
             "disk_free_bytes": a.disk_free_bytes}
            for a in agents
        ],
        "cameras": cameras,
        "alerts": [{"id": a.id, "kind": a.kind, "camera_id": a.camera_id, "message": a.message, "opened_ms": a.opened_ms}
                   for a in alerts],
        "ocr_pending_frames": pending,
        "review_pending": review,
    }


@router.get("/cameras/{camera_id}/live.jpg")
def live_image(
    camera_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    live: LiveHub = Depends(get_live),
):
    """Newest picture of a camera. Polling this marks the camera as watched, which asks
    the agent to send live preview frames (every ~0.5 s) instead of only stills."""
    live.touch(camera_id)
    lf = live.get(camera_id, settings.live_max_age_s)
    headers = {"Cache-Control": "no-store"}
    if lf is not None:
        headers.update({"X-Kind": "live", "X-Captured-Ms": str(lf.captured_ms)})
        return Response(lf.jpeg, media_type="image/jpeg", headers=headers)
    frame = db.scalars(select(Frame).where(Frame.camera_id == camera_id).order_by(Frame.captured_ms.desc()).limit(1)).first()
    if frame is None:  # camera just started: "nothing yet" is normal, not an error
        return Response(status_code=204, headers={**headers, "X-Kind": "none"})
    headers.update({"X-Kind": "still", "X-Captured-Ms": str(frame.captured_ms), "X-Frame-Id": str(frame.id)})
    return FileResponse(_frame_file(settings, frame), media_type="image/jpeg", headers=headers)


@router.get("/frames")
def list_frames(camera_id: str, before_ms: int | None = None, limit: int = 30, db: Session = Depends(get_db)):
    q = select(Frame).where(Frame.camera_id == camera_id)
    if before_ms is not None:
        q = q.where(Frame.captured_ms < before_ms)
    frames = db.scalars(q.order_by(Frame.captured_ms.desc()).limit(min(limit, 500))).all()
    return [_frame_dict(f) for f in frames]


@router.get("/frames/nearest")
def nearest_frame(camera_id: str, t_ms: int, db: Session = Depends(get_db)):
    before = db.scalars(
        select(Frame).where(Frame.camera_id == camera_id, Frame.captured_ms <= t_ms).order_by(Frame.captured_ms.desc()).limit(1)
    ).first()
    after = db.scalars(
        select(Frame).where(Frame.camera_id == camera_id, Frame.captured_ms >= t_ms).order_by(Frame.captured_ms).limit(1)
    ).first()
    options = [f for f in (before, after) if f is not None]
    if not options:
        raise HTTPException(404, "no frames")
    best = min(options, key=lambda f: abs(f.captured_ms - t_ms))
    return _frame_dict(best)


def _frame_dict(f: Frame) -> dict:
    return {"id": f.id, "camera_id": f.camera_id, "captured_ms": f.captured_ms, "received_ms": f.received_ms,
            "sha256": f.sha256, "width": f.width, "height": f.height, "size_bytes": f.size_bytes,
            "brightness": f.brightness, "ocr_status": f.ocr_status, "ocr_error": f.ocr_error}


@router.get("/frames/{frame_id}")
def frame_detail(frame_id: int, db: Session = Depends(get_db)):
    frame = db.get(Frame, frame_id)
    if frame is None:
        raise HTTPException(404, "frame not found")
    readings = db.scalars(select(Reading).where(Reading.frame_id == frame_id).order_by(Reading.device_id, Reading.field_id)).all()
    out = _frame_dict(frame)
    out["readings"] = [
        {"id": r.id, "device_id": r.device_id, "field_id": r.field_id, "metric": r.metric, "raw_text": r.raw_text,
         "value": r.value, "final_value": r.final_value, "status": r.status, "confidence": r.confidence, "note": r.note}
        for r in readings
    ]
    return out


@router.get("/frames/{frame_id}/image.jpg")
def frame_image(frame_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    frame = db.get(Frame, frame_id)
    if frame is None:
        raise HTTPException(404, "frame not found")
    return FileResponse(_frame_file(settings, frame), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400", "X-Sha256": frame.sha256})


@router.get("/series")
def series(
    metric: str,
    since_ms: int,
    until_ms: int | None = None,
    include_review: bool = False,
    max_points: int = 1200,
    db: Session = Depends(get_db),
):
    """Time series for one metric: every device read by camera, plus reference data,
    averaged into buckets so the chart never gets more than ~max_points per line."""
    until = until_ms or now_ms()
    bucket = choose_bucket_ms(max(1, until - since_ms), min(max(50, max_points), 5000))
    return {"metric": metric, "since_ms": since_ms, "until_ms": until, "bucket_ms": bucket,
            "series": bucketed(db, metric, since_ms, until, bucket, include_review)}


@router.get("/metrics")
def metrics_in_use(db: Session = Depends(get_db)):
    """Metrics that have at least one active field or reference data (for chart pickers)."""
    from ..ocr.pipeline import METRICS

    used = {m for (m,) in db.execute(select(Field.metric).where(Field.active.is_(True)).distinct()).all()}
    used |= {m for (m,) in db.execute(select(ReferenceReading.metric).distinct()).all()}
    order = list(METRICS)
    return [
        {"metric": m, "label": METRICS.get(m, {}).get("label", m), "unit": METRICS.get(m, {}).get("unit", "")}
        for m in sorted(used, key=lambda m: order.index(m) if m in order else 99)
    ]


# --- test runs -----------------------------------------------------------------------------

class RunIn(BaseModel):
    name: str = PField(min_length=1, max_length=255)
    notes: str | None = None


class RunPatch(BaseModel):
    name: str | None = None
    notes: str | None = None


@router.get("/runs")
def list_runs(db: Session = Depends(get_db)):
    return [_run_dict(r) for r in db.scalars(select(TestRun).order_by(TestRun.started_ms.desc())).all()]


@router.post("/runs")
def start_run(body: RunIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    now = now_ms()
    for r in db.scalars(select(TestRun).where(TestRun.ended_ms.is_(None))).all():
        r.ended_ms = now
        audit(db, user, "run.stop", run_id=r.id, reason="new run started")
    run = TestRun(name=body.name, notes=body.notes, started_ms=now, created_by=user)
    db.add(run)
    db.flush()
    audit(db, user, "run.start", run_id=run.id, name=body.name)
    db.commit()
    return _run_dict(run)


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    run = db.get(TestRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run.ended_ms is None:
        run.ended_ms = now_ms()
        audit(db, user, "run.stop", run_id=run.id)
        db.commit()
    return _run_dict(run)


@router.patch("/runs/{run_id}")
def edit_run(run_id: int, body: RunPatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    run = db.get(TestRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if body.name is not None:
        run.name = body.name
    if body.notes is not None:
        run.notes = body.notes
    audit(db, user, "run.edit", run_id=run.id)
    db.commit()
    return _run_dict(run)


@router.get("/alerts")
def list_alerts(include_resolved: bool = False, limit: int = 100, db: Session = Depends(get_db)):
    q = select(Alert)
    if not include_resolved:
        q = q.where(Alert.resolved_ms.is_(None))
    alerts = db.scalars(q.order_by(Alert.opened_ms.desc()).limit(min(limit, 1000))).all()
    return [{"id": a.id, "kind": a.kind, "camera_id": a.camera_id, "message": a.message,
             "opened_ms": a.opened_ms, "resolved_ms": a.resolved_ms} for a in alerts]
