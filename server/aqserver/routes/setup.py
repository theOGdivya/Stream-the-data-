"""Cameras page: which cameras each capture computer has, and which ones it should record."""
from __future__ import annotations

import base64
import binascii
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field as PField, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import now_ms
from ..deps import audit, current_user, get_db, get_discovery, get_settings
from ..discovery import Discovery
from ..models import Agent, Camera
from ..storage import valid_camera_id

router = APIRouter(prefix="/api", tags=["setup"])

DEMO_SOURCES = ("synthetic", "synthetic-bump")


class DetectedCamera(BaseModel):
    index: int = PField(ge=0, le=99)
    name: str | None = None
    name_certain: bool = False
    width: int | None = None
    height: int | None = None
    in_use_by: str | None = None
    preview: str | None = None  # base64 JPEG


class DetectedIn(BaseModel):
    agent_id: str
    probed_ms: int
    cameras: list[DetectedCamera] = PField(default_factory=list, max_length=32)


@router.post("/agent/detected")
def agent_detected(body: DetectedIn, discovery: Discovery = Depends(get_discovery)):
    """A capture computer reports the cameras it found (agent token)."""
    previews: dict[int, bytes] = {}
    cams = []
    for c in body.cameras:
        if c.preview:
            try:
                data = base64.b64decode(c.preview, validate=True)
                if len(data) <= 2_000_000:
                    previews[c.index] = data
            except (binascii.Error, ValueError):
                pass
        cams.append({**c.model_dump(exclude={"preview"}), "has_preview": c.index in previews})
    discovery.store(body.agent_id, body.probed_ms, cams, previews)
    return {"ok": True}


def camera_dict(cam: Camera, online: bool) -> dict:
    st = cam.status or {}
    m = cam.managed
    return {
        "id": cam.id, "name": cam.name, "agent_id": cam.agent_id,
        "source": m.get("source") if m else st.get("source"),
        "managed": m is not None, "enabled": bool(m.get("enabled", True)) if m else True,
        "still_interval_s": (m.get("still_interval_s") if m else None) or cam.still_interval_s,
        "online": online, "status": st, "last_frame_ms": cam.last_frame_ms,
    }


@router.get("/setup")
def setup(db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
          discovery: Discovery = Depends(get_discovery)):
    now = now_ms()
    offline_ms = settings.offline_after_s * 1000
    cams_by_agent: dict[str, list[Camera]] = {}
    for cam in db.scalars(select(Camera).order_by(Camera.created_ms, Camera.id)).all():
        cams_by_agent.setdefault(cam.agent_id or "", []).append(cam)
    agents = []
    for a in db.scalars(select(Agent).order_by(Agent.id)).all():
        online = bool(a.last_seen_ms and now - a.last_seen_ms < offline_ms)
        cams = []
        for cam in cams_by_agent.get(a.id, []):
            cam_online = bool(online and (cam.status or {}).get("connected") and cam.status_ms
                              and now - cam.status_ms < offline_ms)
            cams.append(camera_dict(cam, cam_online))
        agents.append({"id": a.id, "hostname": a.hostname, "version": a.version, "browser": a.version == "browser",
                       "online": online, "last_seen_ms": a.last_seen_ms,
                       "scanning": discovery.scanning(a.id), "detected": discovery.found(a.id), "cameras": cams})
    return {"server_time_ms": now, "agents": agents}


@router.post("/agents/{agent_id}/scan")
def scan(agent_id: str, db: Session = Depends(get_db), discovery: Discovery = Depends(get_discovery)):
    if db.get(Agent, agent_id) is None:
        raise HTTPException(404, "capture computer not found")
    discovery.request_scan(agent_id)
    return {"ok": True}


@router.get("/agents/{agent_id}/detected/{index}.jpg")
def detected_preview(agent_id: str, index: int, discovery: Discovery = Depends(get_discovery)):
    data = discovery.preview(agent_id, index)
    if data is None:
        raise HTTPException(404, "no preview")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class CameraIn(BaseModel):
    source: int | str
    name: str = PField(min_length=1, max_length=255)
    still_interval_s: float = PField(default=10, ge=2, le=3600)

    @field_validator("source")
    @classmethod
    def _source(cls, v):
        if isinstance(v, int):
            if v < 0 or v > 99:
                raise ValueError("camera number must be 0-99")
            return v
        v = v.strip()
        if v.isdigit():
            return int(v)
        if v in DEMO_SOURCES or v.startswith(("rtsp://", "http://", "https://")):
            return v
        raise ValueError("source must be a camera number, a stream URL, or 'synthetic'")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def _unique_id(db: Session, base: str) -> str:
    base = base if valid_camera_id(base) else "camera"
    cid, n = base, 2
    while db.get(Camera, cid) is not None:
        cid = f"{base}-{n}"
        n += 1
    return cid


@router.post("/agents/{agent_id}/cameras")
def add_camera(agent_id: str, body: CameraIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Record this camera on that computer. Choosing the same camera again re-uses its
    earlier entry (and history) instead of creating a new one."""
    if db.get(Agent, agent_id) is None:
        raise HTTPException(404, "capture computer not found")
    mine = db.scalars(select(Camera).where(Camera.agent_id == agent_id)).all()
    cam = next((c for c in mine if c.managed and c.managed.get("source") == body.source), None)
    if cam is None:
        cam = Camera(id=_unique_id(db, _slug(body.name) or f"{agent_id}-cam"), agent_id=agent_id, status={},
                     created_ms=now_ms())
        db.add(cam)
    cam.name = body.name
    cam.managed = {"source": body.source, "still_interval_s": body.still_interval_s, "enabled": True}
    audit(db, user, "camera.add", camera_id=cam.id, agent_id=agent_id, source=body.source, name=body.name)
    db.commit()
    return camera_dict(cam, False)


def _managed(db: Session, camera_id: str) -> Camera:
    cam = db.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(404, "camera not found")
    if not cam.managed:
        raise HTTPException(400, "this camera is set in the config file on its capture computer")
    return cam


@router.post("/cameras/{camera_id}/stop")
def stop_camera(camera_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    cam = _managed(db, camera_id)
    cam.managed = {**cam.managed, "enabled": False}
    audit(db, user, "camera.stop", camera_id=camera_id)
    db.commit()
    return {"ok": True}


@router.post("/cameras/{camera_id}/start")
def start_camera(camera_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)):
    cam = _managed(db, camera_id)
    cam.managed = {**cam.managed, "enabled": True}
    audit(db, user, "camera.start", camera_id=camera_id)
    db.commit()
    return {"ok": True}


def managed_for_agent(db: Session, agent_id: str) -> list[dict]:
    """What the capture computer should record (sent in every heartbeat response)."""
    out = []
    for cam in db.scalars(select(Camera).where(Camera.agent_id == agent_id)).all():
        m = cam.managed
        if m and m.get("enabled", True):
            out.append({"id": cam.id, "name": cam.name, "source": m.get("source"),
                        "still_interval_s": m.get("still_interval_s", 10)})
    return out
