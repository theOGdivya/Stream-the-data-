"""In-memory hub for live preview frames.

Agents only send live preview frames while somebody is watching that camera, so the
upload stays small. A viewer "watches" a camera by polling its live image; the agent
learns which cameras are watched from its heartbeat response.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class LiveFrame:
    jpeg: bytes
    captured_ms: int
    received_at: float  # time.time()


class LiveHub:
    def __init__(self, viewer_ttl_s: float = 15.0):
        self.viewer_ttl_s = viewer_ttl_s
        self._lock = threading.Lock()
        self._frames: dict[str, LiveFrame] = {}
        self._viewed: dict[str, float] = {}

    def put(self, camera_id: str, jpeg: bytes, captured_ms: int) -> None:
        with self._lock:
            self._frames[camera_id] = LiveFrame(jpeg, captured_ms, time.time())

    def get(self, camera_id: str, max_age_s: float) -> LiveFrame | None:
        with self._lock:
            frame = self._frames.get(camera_id)
        if frame is None or time.time() - frame.received_at > max_age_s:
            return None
        return frame

    def touch(self, camera_id: str) -> None:
        with self._lock:
            self._viewed[camera_id] = time.monotonic()

    def is_watched(self, camera_id: str) -> bool:
        with self._lock:
            seen = self._viewed.get(camera_id)
        return seen is not None and time.monotonic() - seen < self.viewer_ttl_s

    def watched(self, camera_ids: list[str]) -> list[str]:
        return [c for c in camera_ids if self.is_watched(c)]
