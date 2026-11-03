"""Agent configuration (YAML file, see config.example.yaml)."""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class CameraConfig:
    id: str
    name: str | None = None
    # Webcam index (0, 1, ...), an RTSP/HTTP URL, a video file, or "synthetic" (fake monitors, for testing).
    source: int | str = 0
    backend: str = "auto"  # auto | dshow | msmf | v4l2 | avfoundation | ffmpeg | gstreamer
    width: int = 1920
    height: int = 1080
    fourcc: str | None = "MJPG"  # MJPG lets several USB webcams share one USB bus at 1080p
    still_interval_s: float = 10.0
    jpeg_quality: int = 90
    live_width: int = 960
    live_fps: float = 2.0
    live_quality: int = 70
    rotate: int = 0  # 0, 90, 180 or 270
    # Locking focus/exposure stops the camera "hunting", which ruins OCR. None = leave as is.
    autofocus: bool | None = False
    focus: float | None = None
    auto_exposure: float | None = None
    exposure: float | None = None


@dataclass
class AgentConfig:
    server_url: str
    agent_token: str
    agent_id: str
    spool_dir: Path
    cameras: list[CameraConfig] = field(default_factory=list)
    keep_sent_days: float = 7.0
    heartbeat_s: float = 5.0
    prevent_sleep: bool = True
    # True (normal), False (testing only), or a path to a CA bundle for a private certificate.
    verify_tls: bool | str = True


def load_config(path: str | Path) -> AgentConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    token = os.environ.get("AQ_AGENT_TOKEN") or raw.get("agent_token") or ""
    if not token:
        raise ValueError("agent_token missing (set it in the config or the AQ_AGENT_TOKEN environment variable)")
    server_url = str(raw.get("server_url") or "").rstrip("/")
    if not server_url.startswith(("http://", "https://")):
        raise ValueError("server_url must start with https:// (or http:// for local testing)")
    agent_id = str(raw.get("agent_id") or socket.gethostname().split(".")[0])
    agent_id = re.sub(r"[^A-Za-z0-9._-]", "-", agent_id)[:64]
    spool = Path(raw.get("spool_dir") or "spool")
    if not spool.is_absolute():
        spool = (path.parent / spool).resolve()

    cameras = []
    seen = set()
    for i, c in enumerate(raw.get("cameras") or []):
        cam = CameraConfig(**c)
        if not ID_RE.match(cam.id):
            raise ValueError(f"camera #{i + 1}: id may only contain letters, digits, '.', '_' and '-'")
        if cam.id in seen:
            raise ValueError(f"camera id {cam.id!r} is used twice")
        if cam.still_interval_s < 1:
            raise ValueError(f"camera {cam.id}: still_interval_s must be at least 1 second")
        if cam.rotate not in (0, 90, 180, 270):
            raise ValueError(f"camera {cam.id}: rotate must be 0, 90, 180 or 270")
        seen.add(cam.id)
        cameras.append(cam)
    # No cameras in the file is fine: they can be chosen on the dashboard's Cameras page.

    return AgentConfig(
        server_url=server_url,
        agent_token=token,
        agent_id=agent_id,
        spool_dir=spool,
        cameras=cameras,
        keep_sent_days=float(raw.get("keep_sent_days", 7)),
        heartbeat_s=float(raw.get("heartbeat_s", 5)),
        prevent_sleep=bool(raw.get("prevent_sleep", True)),
        verify_tls=raw.get("verify_tls", True) if isinstance(raw.get("verify_tls", True), (bool, str)) else True,
    )
