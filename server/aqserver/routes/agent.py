"""Endpoints used by capture agents (and by systems pushing reference data)."""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field as PField
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import now_ms
from ..deps import get_db, get_live, get_settings
from ..live import LiveHub
from ..models import Agent, Camera, Device, Frame
from ..reference import RefRow, store_reference_rows
from ..storage import frame_relpath, valid_camera_id, write_atomic

router = APIRouter(prefix="/api/agent", tags=["agent"])

MAX_FRAME_BYTES = 20 * 1024 * 1024


class CameraStatus(BaseModel):
    id: str
    name: str | None = None
    connected: bool = False
    fps: float | None = None
    brightness: float | None = None
    frozen: bool = False
    error: str | None = None
    width: int | None = None
    height: int | None = None
    still_interval_s: float | None = None
    last_capture_ms: int | None = None
    live_fps: float | None = None


class Heartbeat(BaseModel):
    agent_id: str = PField(min_length=1, max_length=64)
    hostname: str | None = None
    version: str | None = None
    agent_time_ms: int
    backlog: int = 0
    disk_free_bytes: int | None = None
    cameras: list[CameraStatus] = []


def _ensure_camera(db: Session, camera_id: str, agent_id: str | None = None) -> Camera:
    cam = db.get(Camera, camera_id)
    if cam is None:
        cam = Camera(id=camera_id, agent_id=agent_id, status={}, created_ms=now_ms())
        db.add(cam)
        db.flush()
    return cam


@router.post("/heartbeat")
def heartbeat(
    hb: Heartbeat,
    db: Session = Depends(get_db),
    live: LiveHub = Depends(get_live),
):
    if not valid_camera_id(hb.agent_id):
        raise HTTPException(400, "agent_id may only contain letters, digits, '.', '_' and '-'")
    now = now_ms()
    agent = db.get(Agent, hb.agent_id)
    if agent is None:
        agent = Agent(id=hb.agent_id)
        db.add(agent)
    agent.hostname = hb.hostname
    agent.version = hb.version
    agent.last_seen_ms = now
    agent.clock_offset_ms = now - hb.agent_time_ms
    agent.backlog = hb.backlog
    agent.disk_free_bytes = hb.disk_free_bytes
    db.flush()
    camera_ids = []
    for cs in hb.cameras:
        if not valid_camera_id(cs.id):
            continue
        cam = _ensure_camera(db, cs.id, hb.agent_id)
        cam.agent_id = hb.agent_id
        cam.name = cs.name or cam.name
        cam.still_interval_s = cs.still_interval_s
        cam.status = cs.model_dump(exclude={"id", "name", "still_interval_s"})
        cam.status_ms = now
        camera_ids.append(cs.id)
    capture = capture_settings(db, camera_ids)
    db.commit()
    return {"live": live.watched(camera_ids), "server_time_ms": now, "capture": capture}


MASK_MARGIN = 0.25  # share of the screen size kept around each monitor (bezel, labels, room for shifts)


def capture_settings(db: Session, camera_ids: list[str]) -> dict:
    """For cameras set to "store only the marked monitors": the rectangles the capture
    computer keeps (everything else is blacked out). They follow detected shifts; if a
    monitor is lost, full pictures are requested until it is found again."""
    out: dict[str, dict] = {}
    for cam in db.scalars(select(Camera).where(Camera.id.in_(camera_ids), Camera.mask_enabled.is_(True))).all():
        devices = db.scalars(select(Device).where(Device.camera_id == cam.id, Device.active.is_(True))).all()
        if not devices:
            continue
        full_every = cam.full_every_s or 300
        if any(d.align_status == "lost" for d in devices):
            out[cam.id] = {"regions": None, "full_every_s": full_every, "reason": "monitor not found: sending full pictures"}
            continue
        regions = []
        for d in devices:
            pts = d.align_corners or d.corners
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            m = max(30.0, MASK_MARGIN * max(max(xs) - min(xs), max(ys) - min(ys)))
            x0, y0 = max(0, int(min(xs) - m)), max(0, int(min(ys) - m))
            regions.append([x0, y0, int(max(xs) + m) - x0, int(max(ys) + m) - y0])
        out[cam.id] = {"regions": regions, "full_every_s": full_every}
    return out


@router.post("/frames")
def upload_frame(
    meta: str = Form(...),
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Store one still. ``meta`` is JSON: camera_id, captured_ms, sha256, width, height, brightness."""
    try:
        m = json.loads(meta)
        camera_id = str(m["camera_id"])
        captured_ms = int(m["captured_ms"])
        claimed_sha = str(m["sha256"]).lower()
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, f"bad meta: {exc}") from exc
    if not valid_camera_id(camera_id):
        raise HTTPException(400, "bad camera_id")
    data = image.file.read(MAX_FRAME_BYTES + 1)
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(413, "frame too large")
    if not data:
        raise HTTPException(400, "empty image")
    sha = hashlib.sha256(data).hexdigest()
    if sha != claimed_sha:
        raise HTTPException(400, "sha256 mismatch: the image was altered or truncated in transit")

    existing = db.scalar(select(Frame.id).where(Frame.camera_id == camera_id, Frame.sha256 == sha))
    if existing is not None:
        return {"id": existing, "duplicate": True}

    cam = _ensure_camera(db, camera_id)
    rel = frame_relpath(camera_id, captured_ms, sha)
    write_atomic(settings.data_dir / rel, data)
    frame = Frame(
        camera_id=camera_id,
        captured_ms=captured_ms,
        received_ms=now_ms(),
        path=rel,
        sha256=sha,
        width=m.get("width"),
        height=m.get("height"),
        size_bytes=len(data),
        brightness=m.get("brightness"),
        masked=bool(m.get("masked")),
        ocr_status="pending",
    )
    db.add(frame)
    if cam.last_frame_ms is None or captured_ms > cam.last_frame_ms:
        cam.last_frame_ms = captured_ms
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Frame.id).where(Frame.camera_id == camera_id, Frame.sha256 == sha))
        return {"id": existing, "duplicate": True}
    return {"id": frame.id, "duplicate": False}


@router.post("/live/{camera_id}")
async def upload_live(camera_id: str, request: Request, live: LiveHub = Depends(get_live)):
    """Live preview frame (raw JPEG body). Not stored; only kept in memory for viewers."""
    if not valid_camera_id(camera_id):
        raise HTTPException(400, "bad camera_id")
    body = await request.body()
    if not body or len(body) > MAX_FRAME_BYTES:
        raise HTTPException(400, "bad live frame")
    try:
        captured_ms = int(request.headers.get("x-captured-ms") or now_ms())
    except ValueError:
        captured_ms = now_ms()
    live.put(camera_id, body, captured_ms)
    return {"watching": live.is_watched(camera_id)}


class ReferencePush(BaseModel):
    source: str = PField(min_length=1, max_length=64)
    rows: list[RefRow]


@router.post("/reference")
def push_reference(payload: ReferencePush, db: Session = Depends(get_db)):
    """Let your own cloud (or any script) push readings to compare against.

    Example body: {"source": "own-cloud", "rows": [{"device": "AQ-0042", "metric": "pm25",
    "t": "2026-11-03T10:00:00Z", "value": 12.0}]}
    """
    added = store_reference_rows(db, payload.source, payload.rows)
    db.commit()
    return {"received": len(payload.rows), "added": added}
