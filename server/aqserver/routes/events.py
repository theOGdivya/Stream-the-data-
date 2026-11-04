"""Events during a test ("Incense lit", "Window opened"): marked by the team, drawn on the
charts, listed in exports and in the report. Every change is written to the activity log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PField
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import now_ms
from ..deps import audit, current_user, get_db
from ..models import Event
from ..storage import iso
from .data import _attachment, _csv_stream, _range, _txt

router = APIRouter(prefix="/api", tags=["events"])

# Offered as one-click buttons on the Live page; anything else can be typed.
PRESETS = ["Incense lit", "Candle lit", "Cooking started", "Cooking stopped", "Window opened", "Window closed",
           "Door opened", "Door closed", "Purifier on", "Purifier off", "People entered", "People left",
           "Monitors moved", "Power cut"]


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


class EventIn(BaseModel):
    t_ms: int | None = None  # default: now
    end_ms: int | None = None
    label: str = PField(min_length=1, max_length=128)
    notes: str | None = PField(default=None, max_length=2000)


class EventPatch(BaseModel):
    t_ms: int | None = None
    end_ms: int | None = None
    clear_end: bool = False
    label: str | None = PField(default=None, min_length=1, max_length=128)
    notes: str | None = PField(default=None, max_length=2000)


def _check(t_ms: int, end_ms: int | None) -> None:
    if end_ms is not None and end_ms <= t_ms:
        raise HTTPException(400, "the end must be after the start")


@router.get("/events")
def list_events(run_id: int | None = None, since_ms: int | None = None, until_ms: int | None = None,
                db: Session = Depends(get_db)):
    start, end, _ = _range(db, run_id, since_ms, until_ms)
    return {"presets": PRESETS, "events": [event_dict(e) for e in events_between(db, start, end)]}


@router.post("/events")
def add_event(body: EventIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    t = body.t_ms if body.t_ms is not None else now_ms()
    _check(t, body.end_ms)
    e = Event(t_ms=t, end_ms=body.end_ms, label=body.label.strip(), notes=(body.notes or "").strip() or None,
              created_by=user, created_ms=now_ms())
    db.add(e)
    db.flush()
    audit(db, user, "event.add", event_id=e.id, label=e.label, t_ms=e.t_ms, end_ms=e.end_ms)
    db.commit()
    return event_dict(e)


@router.patch("/events/{event_id}")
def edit_event(event_id: int, body: EventPatch, db: Session = Depends(get_db), user: str = Depends(current_user)):
    e = db.get(Event, event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    before = event_dict(e)
    if body.t_ms is not None:
        e.t_ms = body.t_ms
    if body.clear_end:
        e.end_ms = None
    elif body.end_ms is not None:
        e.end_ms = body.end_ms
    if body.label is not None:
        e.label = body.label.strip()
    if body.notes is not None:
        e.notes = body.notes.strip() or None
    _check(e.t_ms, e.end_ms)
    audit(db, user, "event.edit", event_id=e.id, before=before, after=event_dict(e))
    db.commit()
    return event_dict(e)


@router.delete("/events/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    e = db.get(Event, event_id)
    if e is None:
        raise HTTPException(404, "event not found")
    audit(db, user, "event.delete", event_id=e.id, before=event_dict(e))
    db.delete(e)
    db.commit()
    return {"deleted": event_id}


@router.get("/export/events.csv")
def export_events(run_id: int | None = None, since_ms: int | None = None,
                  until_ms: int | None = None, db: Session = Depends(get_db)):
    start, end, slug = _range(db, run_id, since_ms, until_ms)
    header = ["start_utc", "start_ms", "end_utc", "end_ms", "label", "notes", "marked_by", "marked_utc"]

    # Few rows: read them now, while the database session is open.
    rows = [[iso(e.t_ms), e.t_ms, iso(e.end_ms), e.end_ms or "", _txt(e.label), _txt(e.notes), _txt(e.created_by),
             iso(e.created_ms)] for e in events_between(db, start, end)]
    return StreamingResponse(_csv_stream(header, iter(rows)), media_type="text/csv",
                             headers=_attachment(f"events-{slug}.csv"))
