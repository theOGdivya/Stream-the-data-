"""Talking to the server: uploads, heartbeats and live frames, each in its own thread."""
from __future__ import annotations

import json
import logging
import shutil
import socket
import threading
import time
from collections.abc import Callable

import requests

from . import __version__
from .config import AgentConfig
from .spool import Spool

log = logging.getLogger(__name__)


class _Session(requests.Session):
    # requests lets REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE override a session-level verify
    # setting; passing it on every request makes the config value win.
    def request(self, *args, **kwargs):
        kwargs.setdefault("verify", self.verify)
        return super().request(*args, **kwargs)


def make_session(cfg: AgentConfig) -> requests.Session:
    s = _Session()
    s.headers["Authorization"] = f"Bearer {cfg.agent_token}"
    s.headers["User-Agent"] = f"aqagent/{__version__}"
    s.verify = cfg.verify_tls
    return s


class Uploader(threading.Thread):
    """Uploads spooled stills oldest-first. Retries forever with backoff; never drops data."""

    def __init__(self, cfg: AgentConfig, spool: Spool, stop: threading.Event):
        super().__init__(name="uploader", daemon=True)
        self.cfg, self.spool, self.stop_event = cfg, spool, stop
        self.session = make_session(cfg)
        self.last_error: str | None = None

    def run(self) -> None:
        backoff = 2.0
        last_prune = 0.0
        while not self.stop_event.is_set():
            if time.time() - last_prune > 3600:
                self.spool.prune_sent()
                last_prune = time.time()
            items = self.spool.pending_items(50)
            if not items:
                self.stop_event.wait(1.0)
                continue
            for item in items:
                if self.stop_event.is_set():
                    return
                result = self._upload(item)
                if result == "retry":
                    log.warning("upload failed (%s); %d waiting; retry in %.0fs", self.last_error,
                                self.spool.backlog(), backoff)
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    break
                backoff = 2.0

    def _upload(self, meta_path) -> str:
        img_path = meta_path.with_suffix(".jpg")
        try:
            meta = meta_path.read_text(encoding="utf-8")
            data = img_path.read_bytes()
        except FileNotFoundError:
            self.spool.mark_rejected(meta_path, "image file missing from spool")
            return "rejected"
        try:
            r = self.session.post(
                f"{self.cfg.server_url}/api/agent/frames",
                data={"meta": meta},
                files={"image": (img_path.name, data, "image/jpeg")},
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            self.last_error = type(exc).__name__
            return "retry"
        if r.status_code == 200:
            self.spool.mark_sent(meta_path)
            self.last_error = None
            return "sent"
        if r.status_code in (400, 413, 422):
            self.spool.mark_rejected(meta_path, f"HTTP {r.status_code}: {r.text[:300]}")
            return "rejected"
        self.last_error = f"HTTP {r.status_code}"
        return "retry"  # 401 (token), 5xx, 429...: keep the file and try again later


class CaptureState:
    """Per camera, what the server wants stored: {"regions": [[x, y, w, h], ...] | None,
    "full_every_s": 300}. No entry means full pictures."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_cam: dict[str, dict] = {}

    def set(self, by_cam: dict) -> None:
        with self._lock:
            self._by_cam = dict(by_cam or {})

    def get(self, cam: str) -> dict | None:
        with self._lock:
            return self._by_cam.get(cam)


class LiveState:
    """Which of our cameras somebody is watching right now (from heartbeat responses)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._live: set[str] = set()
        self._updated = 0.0

    def set(self, cams: list[str]) -> None:
        with self._lock:
            self._live = set(cams)
            self._updated = time.time()

    def discard(self, cam: str) -> None:
        with self._lock:
            self._live.discard(cam)

    def is_live(self, cam: str) -> bool:
        with self._lock:
            return cam in self._live and time.time() - self._updated < 30


class Heartbeat(threading.Thread):
    def __init__(self, cfg: AgentConfig, spool: Spool, statuses: Callable[[], list[dict]], live: LiveState,
                 stop: threading.Event, capture: CaptureState | None = None):
        super().__init__(name="heartbeat", daemon=True)
        self.cfg, self.spool, self.statuses, self.live, self.stop_event = cfg, spool, statuses, live, stop
        self.capture = capture
        self.session = make_session(cfg)
        self.ok = False

    def beat(self) -> dict:
        body = {
            "agent_id": self.cfg.agent_id,
            "hostname": socket.gethostname(),
            "version": __version__,
            "agent_time_ms": int(time.time() * 1000),
            "backlog": self.spool.backlog(),
            "disk_free_bytes": shutil.disk_usage(self.spool.root).free,
            "cameras": self.statuses(),
        }
        r = self.session.post(f"{self.cfg.server_url}/api/agent/heartbeat", data=json.dumps(body),
                              headers={"Content-Type": "application/json"}, timeout=(5, 10))
        r.raise_for_status()
        return r.json()

    def run(self) -> None:
        last_warn = 0.0
        while not self.stop_event.is_set():
            try:
                resp = self.beat()
                self.live.set(resp.get("live") or [])
                if self.capture is not None:
                    self.capture.set(resp.get("capture") or {})
                if not self.ok:
                    log.info("connected to %s", self.cfg.server_url)
                self.ok = True
            except Exception as exc:  # noqa: BLE001
                self.live.set([])
                if self.ok or time.time() - last_warn > 60:
                    log.warning("heartbeat failed: %s", exc)
                    last_warn = time.time()
                self.ok = False
            self.stop_event.wait(self.cfg.heartbeat_s)


class LiveSender(threading.Thread):
    """Sends the newest live frame per camera; older unsent frames are simply replaced."""

    def __init__(self, cfg: AgentConfig, live: LiveState, stop: threading.Event):
        super().__init__(name="live-sender", daemon=True)
        self.cfg, self.live, self.stop_event = cfg, live, stop
        self.session = make_session(cfg)
        self._cond = threading.Condition()
        self._latest: dict[str, tuple[bytes, int]] = {}

    def submit(self, cam: str, jpeg: bytes, captured_ms: int) -> None:
        with self._cond:
            self._latest[cam] = (jpeg, captured_ms)
            self._cond.notify()

    def run(self) -> None:
        while not self.stop_event.is_set():
            with self._cond:
                if not self._latest:
                    self._cond.wait(timeout=1.0)
                    continue
                cam, (jpeg, captured_ms) = self._latest.popitem()
            try:
                r = self.session.post(f"{self.cfg.server_url}/api/agent/live/{cam}", data=jpeg,
                                      headers={"Content-Type": "image/jpeg", "X-Captured-Ms": str(captured_ms)},
                                      timeout=(5, 10))
                if r.ok and not r.json().get("watching", True):
                    self.live.discard(cam)  # viewer left: stop streaming until the next heartbeat says otherwise
            except Exception as exc:  # noqa: BLE001
                log.debug("live frame for %s failed: %s", cam, exc)
                self.stop_event.wait(1.0)
