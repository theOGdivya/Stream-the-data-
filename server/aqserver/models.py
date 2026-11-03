"""Database tables.

All timestamps are UTC epoch milliseconds (``*_ms`` columns). This keeps time maths
identical on SQLite and PostgreSQL; exports convert them to ISO 8601.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now_ms

# Reading statuses
OK = "ok"  # accepted automatically
REVIEW = "review"  # waiting for a person
VERIFIED = "verified"  # a person confirmed the OCR value
CORRECTED = "corrected"  # a person typed a different value
REJECTED = "rejected"  # unreadable / not a valid reading
GOOD_STATUSES = (OK, VERIFIED, CORRECTED)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hostname: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(32))
    last_seen_ms: Mapped[int | None] = mapped_column(BigInteger)
    clock_offset_ms: Mapped[int | None] = mapped_column(BigInteger)
    backlog: Mapped[int | None] = mapped_column(Integer)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"))
    name: Mapped[str | None] = mapped_column(String(255))
    still_interval_s: Mapped[float | None] = mapped_column(Float)
    last_frame_ms: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class TestRun(Base):
    """A side-by-side test (e.g. "Office, 7 days, March"). Frames belong to it by time."""

    __tablename__ = "test_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    started_ms: Mapped[int] = mapped_column(BigInteger)
    ended_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str | None] = mapped_column(String(64))


class Frame(Base):
    __tablename__ = "frames"
    __table_args__ = (
        UniqueConstraint("camera_id", "sha256", name="uq_frame_camera_sha"),
        Index("ix_frames_camera_time", "camera_id", "captured_ms"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id"))
    captured_ms: Mapped[int] = mapped_column(BigInteger)
    received_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    brightness: Mapped[float | None] = mapped_column(Float)
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    ocr_error: Mapped[str | None] = mapped_column(Text)


class Device(Base):
    """One air-quality monitor as seen by one camera (its screen corners in the frame)."""

    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    is_own: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    corners: Mapped[list[list[float]]] = mapped_column(JSON)  # TL, TR, BR, BL in frame pixels
    warp_w: Mapped[int] = mapped_column(Integer)
    warp_h: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Field(Base):
    """One number on a device's screen (e.g. PM2.5), as a box on the straightened screen."""

    __tablename__ = "fields"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    metric: Mapped[str] = mapped_column(String(32))
    label: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(32))
    box: Mapped[list[float]] = mapped_column(JSON)  # x, y, w, h as fractions of the screen
    recognizer: Mapped[str] = mapped_column(String(32), default="sevenseg")
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("frame_id", "field_id", name="uq_reading_frame_field"),
        Index("ix_readings_field_time", "field_id", "t_ms"),
        Index("ix_readings_metric_time", "metric", "t_ms"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    frame_id: Mapped[int] = mapped_column(ForeignKey("frames.id"), index=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id"))
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    metric: Mapped[str] = mapped_column(String(32))
    t_ms: Mapped[int] = mapped_column(BigInteger)
    raw_text: Mapped[str] = mapped_column(String(64), default="")
    value: Mapped[float | None] = mapped_column(Float)  # what OCR read
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), index=True)
    final_value: Mapped[float | None] = mapped_column(Float)  # value used in charts/exports
    note: Mapped[str | None] = mapped_column(String(255))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_ms: Mapped[int | None] = mapped_column(BigInteger)


class ReferenceReading(Base):
    """Data that did not come from a camera: your own monitors' cloud data, a reference instrument."""

    __tablename__ = "reference_readings"
    __table_args__ = (
        UniqueConstraint("source", "device_ref", "metric", "t_ms", name="uq_reference_point"),
        Index("ix_reference_metric_time", "metric", "t_ms"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    device_ref: Mapped[str] = mapped_column(String(255))
    metric: Mapped[str] = mapped_column(String(32))
    t_ms: Mapped[int] = mapped_column(BigInteger)
    value: Mapped[float] = mapped_column(Float)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms, index=True)
    actor: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    camera_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(512))
    opened_ms: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    resolved_ms: Mapped[int | None] = mapped_column(BigInteger)
