"""FastAPI dependencies and small shared helpers."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from .config import Settings
from .db import now_ms
from .discovery import Discovery
from .live import LiveHub
from .models import AuditLog


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.Session()
    try:
        yield db
    finally:
        db.close()


def get_read_db(request: Request) -> Iterator[Session]:
    """For long read-only requests (Results, the report): every statement commits by itself, so
    no transaction stays open while numbers and figures are computed."""
    db: Session = request.app.state.Session()
    try:
        db.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
        yield db
    finally:
        db.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_live(request: Request) -> LiveHub:
    return request.app.state.live


def get_discovery(request: Request) -> Discovery:
    return request.app.state.discovery


def current_user(request: Request) -> str:
    return getattr(request.state, "user", "unknown")


def audit(db: Session, actor: str | None, action: str, /, **detail: Any) -> None:
    db.add(AuditLog(at_ms=now_ms(), actor=actor, action=action, detail=detail))
