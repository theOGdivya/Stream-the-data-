"""Events during a test (shared by the events API, Results and the report)."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Event


def event_dict(e: Event) -> dict:
    return {"id": e.id, "t_ms": e.t_ms, "end_ms": e.end_ms, "label": e.label, "notes": e.notes,
            "created_by": e.created_by, "created_ms": e.created_ms}


def events_between(db: Session, since_ms: int, until_ms: int) -> list[Event]:
    """Events that happen in the period (or, for ones that lasted, overlap it)."""
    return db.scalars(
        select(Event).where(Event.t_ms <= until_ms,
                            or_(Event.t_ms >= since_ms, Event.end_ms >= since_ms))
        .order_by(Event.t_ms, Event.id)
    ).all()
