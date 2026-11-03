"""One thread per camera: keeps the newest frame fresh, saves stills, feeds the live view."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import platform
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .config import CameraConfig
from .spool import Spool
from .synthetic import SyntheticSource

log = logging.getLogger(__name__)

BACKENDS = {
    "auto": cv2.CAP_ANY,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "v4l2": cv2.CAP_V4L2,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "ffmpeg": cv2.CAP_FFMPEG,
    "gstreamer": cv2.CAP_GSTREAMER,
}
ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
# For webcams, each system's native driver interface: it opens faster and numbers cameras
# the same way the camera picker's names do (DirectShow order on Windows).
NATIVE_BACKEND = {"Windows": cv2.CAP_DSHOW, "Darwin": cv2.CAP_AVFOUNDATION, "Linux": cv2.CAP_V4L2}


def resolve_backend(name: str, source) -> int:
    if name == "auto" and isinstance(source, int):
        return NATIVE_BACKEND.get(platform.system(), cv2.CAP_ANY)
    return BACKENDS.get(name, cv2.CAP_ANY)


def open_index(index: int, backend: str = "auto"):
    """Open webcam ``index`` for a quick look (camera picker). Returns the capture or None."""
    cap = cv2.VideoCapture(index, resolve_backend(backend, index))
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def open_source(cfg: CameraConfig):
    """Open a webcam index, stream URL, video file or the synthetic source. Returns (cap, is_file)."""
    src = cfg.source
    if src == "synthetic":
        return SyntheticSource(), False
    if src == "synthetic-bump":  # demo: the fake camera gets knocked after 2 minutes
        return SyntheticSource(bump_after_s=120), False
    if isinstance(src, str) and src.strip().isdigit():
        src = int(src)
    cap = cv2.VideoCapture(src, resolve_backend(cfg.backend, src))
    if not cap.isOpened():
        cap.release()
        return None, False
    if isinstance(src, int):
        if cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if cfg.autofocus is not None:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if cfg.autofocus else 0)
        if cfg.focus is not None:
            cap.set(cv2.CAP_PROP_FOCUS, cfg.focus)
        if cfg.auto_exposure is not None:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, cfg.auto_exposure)
        if cfg.exposure is not None:
            cap.set(cv2.CAP_PROP_EXPOSURE, cfg.exposure)
    return cap, isinstance(src, str) and os.path.isfile(src)


def apply_mask(frame: np.ndarray, regions: list[list[float]]) -> np.ndarray:
    """Black out everything outside the given rectangles (x, y, w, h in frame pixels)."""
    out = np.zeros_like(frame)
    h, w = frame.shape[:2]
    for x, y, rw, rh in regions:
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(w, int(x + rw)), min(h, int(y + rh))
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = frame[y0:y1, x0:x1]
    return out


def thumb(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)


class CameraWorker(threading.Thread):
    def __init__(
        self,
        cfg: CameraConfig,
        spool: Spool,
        is_live: Callable[[str], bool],
        send_live: Callable[[str, bytes, int], None],
        stop: threading.Event,
        capture: Callable[[str], dict | None] = lambda _cam: None,
    ):
        super().__init__(name=f"camera-{cfg.id}", daemon=True)
        self.cfg = cfg
        self.spool = spool
        self.is_live = is_live
        self.send_live = send_live
        self.stop_event = stop
        self._lock = threading.Lock()
        self._status = {"connected": False, "error": "starting"}
        self._prev_thumb: np.ndarray | None = None
        self._same_count = 0
        self.capture = capture
        self._last_full = 0.0

    # --- status reported in heartbeats -----------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            s = dict(self._status)
        s.update({"id": self.cfg.id, "name": self.cfg.name, "still_interval_s": self.cfg.still_interval_s,
                  "live_fps": self.cfg.live_fps, "source": str(self.cfg.source)})
        return s

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    # --- main loop -------------------------------------------------------------------------
    def run(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            cap, is_file = open_source(self.cfg)
            if cap is None:
                self._set(connected=False, error=f"cannot open source {self.cfg.source!r}")
                log.warning("camera %s: cannot open %r, retrying in %.0fs", self.cfg.id, self.cfg.source, backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 2.0
            log.info("camera %s: opened %r", self.cfg.id, self.cfg.source)
            try:
                self._loop(cap, is_file)
            except Exception as exc:  # noqa: BLE001 - keep the camera thread alive no matter what
                log.exception("camera %s crashed; reopening", self.cfg.id)
                self._set(connected=False, error=str(exc)[:200])
            finally:
                cap.release()
            self.stop_event.wait(1.0)

    def _loop(self, cap, is_file: bool) -> None:
        cfg = self.cfg
        interval = cfg.still_interval_s
        next_still = math.floor(time.time() / interval) * interval + interval  # wall-clock aligned
        next_live = 0.0
        last_ok = time.time()
        fps_count, fps_t0 = 0, time.time()
        file_delay = 0.0
        if is_file:
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            file_delay = 1.0 / fps
        while not self.stop_event.is_set():
            if file_delay:
                time.sleep(file_delay)
            ok = cap.grab()
            now = time.time()
            if not ok:
                if is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop test videos
                    continue
                if now - last_ok > 10:
                    self._set(connected=False, error="camera stopped delivering frames")
                    log.warning("camera %s: no frames for 10 s, reopening", cfg.id)
                    return
                time.sleep(0.05)
                continue
            last_ok = now
            fps_count += 1
            if now - fps_t0 >= 5:
                self._set(fps=round(fps_count / (now - fps_t0), 1))
                fps_count, fps_t0 = 0, now

            want_still = now >= next_still
            want_live = now >= next_live and self.is_live(cfg.id)
            if not (want_still or want_live):
                continue
            captured_ms = int(now * 1000)
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            if cfg.rotate:
                frame = cv2.rotate(frame, ROTATIONS[cfg.rotate])
            h, w = frame.shape[:2]
            self._set(connected=True, error=None, width=w, height=h)
            if want_still:
                self._save_still(frame, captured_ms)
                next_still = math.floor(now / interval) * interval + interval
            if want_live:
                self._push_live(frame, captured_ms)
                next_live = now + 1.0 / max(0.2, cfg.live_fps)

    def _save_still(self, frame: np.ndarray, captured_ms: int) -> None:
        # "Store only the marked monitors": black out the rest before encoding and hashing,
        # except for a full picture every few minutes (context, and to find moved monitors).
        settings = self.capture(self.cfg.id) or {}
        regions = settings.get("regions")
        now = captured_ms / 1000
        masked = bool(regions) and now - self._last_full < float(settings.get("full_every_s") or 300)
        if not masked:
            self._last_full = now
        stored = apply_mask(frame, regions) if masked else frame
        ok, buf = cv2.imencode(".jpg", stored, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        if not ok:
            log.error("camera %s: JPEG encode failed", self.cfg.id)
            return
        data = buf.tobytes()
        t = thumb(frame)
        brightness = float(t.mean())
        if self._prev_thumb is not None and float(np.abs(t - self._prev_thumb).mean()) < 0.05:
            self._same_count += 1  # pixel-identical frames: the driver is handing back a stale buffer
        else:
            self._same_count = 0
        self._prev_thumb = t
        h, w = frame.shape[:2]
        meta = {"camera_id": self.cfg.id, "captured_ms": captured_ms, "sha256": hashlib.sha256(data).hexdigest(),
                "width": w, "height": h, "brightness": round(brightness, 1), "masked": masked}
        self.spool.put(self.cfg.id, captured_ms, data, meta)
        self._set(last_capture_ms=captured_ms, brightness=round(brightness, 1), frozen=self._same_count >= 5)

    def _push_live(self, frame: np.ndarray, captured_ms: int) -> None:
        h, w = frame.shape[:2]
        if w > self.cfg.live_width:
            s = self.cfg.live_width / w
            frame = cv2.resize(frame, (self.cfg.live_width, int(h * s)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.live_quality])
        if ok:
            self.send_live(self.cfg.id, buf.tobytes(), captured_ms)
