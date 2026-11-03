"""Server settings, read from environment variables (see deploy/.env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _users(raw: str) -> dict[str, str]:
    users: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, password = item.partition(":")
        if not sep or not name or not password:
            raise ValueError("DASHBOARD_USERS must look like 'alice:secret,bob:secret2'")
        users[name.strip()] = password
    return users


@dataclass
class Settings:
    data_dir: Path
    database_url: str
    agent_token: str
    dashboard_users: dict[str, str]
    alert_webhook_url: str | None = None
    # A camera/agent is "offline" after this many seconds without a heartbeat.
    offline_after_s: int = 90
    # Alert when no stored frame arrives for max(120 s, factor x still interval).
    no_frames_factor: float = 6.0
    clock_skew_warn_s: float = 5.0
    disk_low_fraction: float = 0.10
    backlog_warn: int = 200
    # A live view counts as "being watched" for this long after the last poll.
    live_viewer_ttl_s: float = 15.0
    # A live preview frame older than this is not shown; the latest still is shown instead.
    live_max_age_s: float = 10.0
    # OCR readings below this confidence go to the review queue.
    review_confidence: float = 0.6

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database_url = os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'aq.db'}"
    agent_token = os.environ.get("AGENT_TOKEN", "")
    if len(agent_token) < 16:
        raise RuntimeError("AGENT_TOKEN must be set to a random string of at least 16 characters")
    users = _users(os.environ.get("DASHBOARD_USERS", ""))
    if not users:
        raise RuntimeError("DASHBOARD_USERS must be set, e.g. 'admin:a-long-password'")
    return Settings(
        data_dir=data_dir,
        database_url=database_url,
        agent_token=agent_token,
        dashboard_users=users,
        alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL") or None,
        review_confidence=float(os.environ.get("REVIEW_CONFIDENCE", "0.6")),
    )
