"""Health checks that raise alerts on the dashboard and (optionally) a chat webhook.

Offline / no-frames alerts only fire while a test run is active, so a laptop that is
switched off between tests doesn't page anyone.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Agent, Alert, Camera, TestRun
from .storage import disk_usage

log = logging.getLogger(__name__)

RESOLVE_AFTER_MS = 120_000  # a condition must stay clear this long before its alert closes
AGENT_DISK_LOW_BYTES = 2 * 1024**3


@dataclass
class Condition:
    key: str
    kind: str
    message: str
    camera_id: str | None = None


def evaluate(db: Session, settings: Settings, now: int) -> list[Condition]:
    conds: list[Condition] = []
    run_active = db.scalar(select(TestRun.id).where(TestRun.ended_ms.is_(None)).limit(1)) is not None
    offline_ms = settings.offline_after_s * 1000
    online_agents = set()
    for a in db.scalars(select(Agent)).all():
        online = bool(a.last_seen_ms and now - a.last_seen_ms < offline_ms)
        if not online:
            if run_active and a.last_seen_ms and now - a.last_seen_ms < 7 * 86_400_000:
                mins = (now - a.last_seen_ms) // 60_000
                conds.append(Condition(f"agent_offline:{a.id}", "agent_offline",
                                       f"Capture computer '{a.id}' has been silent for {mins} min"))
            continue
        online_agents.add(a.id)
        if a.clock_offset_ms is not None and abs(a.clock_offset_ms) > settings.clock_skew_warn_s * 1000:
            conds.append(Condition(f"clock_skew:{a.id}", "clock_skew",
                                   f"Clock on '{a.id}' is off by {a.clock_offset_ms / 1000:+.1f} s; turn on automatic time sync"))
        if a.backlog and a.backlog > settings.backlog_warn:
            conds.append(Condition(f"backlog:{a.id}", "backlog",
                                   f"'{a.id}' has {a.backlog} frames waiting to upload (slow or no internet?)"))
        if a.disk_free_bytes is not None and a.disk_free_bytes < AGENT_DISK_LOW_BYTES:
            conds.append(Condition(f"agent_disk:{a.id}", "agent_disk_low",
                                   f"'{a.id}' is low on disk ({a.disk_free_bytes / 1024**3:.1f} GB free)"))

    for cam in db.scalars(select(Camera)).all():
        if cam.agent_id not in online_agents:
            continue
        st = cam.status or {}
        label = cam.name or cam.id
        if not st.get("connected"):
            err = f": {st.get('error')}" if st.get("error") else ""
            conds.append(Condition(f"camera_disconnected:{cam.id}", "camera_disconnected",
                                   f"Camera '{label}' is not delivering images{err}", cam.id))
            continue
        if st.get("brightness") is not None and st["brightness"] < 25:
            conds.append(Condition(f"dark:{cam.id}", "dark", f"Camera '{label}' image is very dark (lights off?)", cam.id))
        if st.get("frozen"):
            conds.append(Condition(f"frozen:{cam.id}", "frozen", f"Camera '{label}' image is frozen", cam.id))
        if run_active:
            interval_ms = (cam.still_interval_s or 10) * 1000
            limit = max(120_000, settings.no_frames_factor * interval_ms)
            if cam.last_frame_ms is None or now - cam.last_frame_ms > limit:
                conds.append(Condition(f"no_frames:{cam.id}", "no_frames",
                                       f"No new stills from '{label}' for over {int(limit // 60_000)} min", cam.id))

    usage = disk_usage(settings.data_dir)
    if usage["free"] / usage["total"] < settings.disk_low_fraction:
        conds.append(Condition("server_disk", "disk_low",
                               f"Server disk is {usage['used_fraction'] * 100:.0f}% full; archive old frames"))
    return conds


class AlertManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._clear_since: dict[str, int] = {}

    def sync(self, db: Session, now: int) -> list[str]:
        """Open/resolve alerts. Returns the notification texts that were sent."""
        conds = {c.key: c for c in evaluate(db, self.settings, now)}
        open_alerts = {a.key: a for a in db.scalars(select(Alert).where(Alert.resolved_ms.is_(None))).all()}
        messages = []
        for key, c in conds.items():
            self._clear_since.pop(key, None)
            if key not in open_alerts:
                db.add(Alert(key=key, kind=c.kind, camera_id=c.camera_id, message=c.message[:512], opened_ms=now))
                messages.append(f"[AQ platform] ALERT: {c.message}")
        for key, a in open_alerts.items():
            if key in conds:
                continue
            since = self._clear_since.setdefault(key, now)
            if now - since >= RESOLVE_AFTER_MS:
                a.resolved_ms = now
                self._clear_since.pop(key, None)
                messages.append(f"[AQ platform] RESOLVED: {a.message}")
        db.commit()
        for m in messages:
            self.notify(m)
        return messages

    def notify(self, text: str) -> None:
        url = self.settings.alert_webhook_url
        if not url:
            return
        body = json.dumps({"text": text, "content": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the worker
            log.warning("alert webhook failed: %s", exc)
