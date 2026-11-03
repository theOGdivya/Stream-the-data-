"""FastAPI dependencies and small shared helpers."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .config import Settings
from .db import now_ms
from .live import LiveHub
from .models import AuditLog


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.Session()
    try:
        yield db
    finally:
        db.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_live(request: Request) -> LiveHub:
    return request.app.state.live


def current_user(request: Request) -> str:
    return getattr(request.state, "user", "unknown")


def audit(db: Session, actor: str | None, action: str, /, **detail: Any) -> None:
    db.add(AuditLog(at_ms=now_ms(), actor=actor, action=action, detail=detail))
