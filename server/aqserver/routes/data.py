"""CSV exports, reference-data import, audit log."""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import now_ms
from ..deps import audit, current_user, get_db
from ..models import AuditLog, Device, Field, Frame, Reading, ReferenceReading, TestRun
from ..reference import parse_reference_csv, store_reference_rows
from ..storage import iso

router = APIRouter(prefix="/api", tags=["data"])


def _range(db: Session, run_id: int | None, since_ms: int | None, until_ms: int | None) -> tuple[int, int, str]:
    if run_id is not None:
        run = db.get(TestRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        slug = re.sub(r"[^a-z0-9]+", "-", run.name.lower()).strip("-") or f"run-{run.id}"
        return run.started_ms, run.ended_ms or now_ms(), slug
    return since_ms or 0, until_ms or now_ms(), "all"


def _csv_stream(header: list[str], rows: Iterator[list]) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for i, row in enumerate(rows, 1):
        writer.writerow(row)
        if i % 2000 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def _txt(value: str | None) -> str:
    """Neutralise user-typed text that a spreadsheet would run as a formula."""
    if not value:
        return ""
    return "'" + value if value[0] in "=+-@\t\r" else value


def _attachment(name: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"}


@router.get("/export/readings.csv")
def export_readings(request: Request, run_id: int | None = None, since_ms: int | None = None,
                    until_ms: int | None = None, db: Session = Depends(get_db)):
    """Every reading with its full audit trail. ``value`` is the value to analyse (empty for
    rejected / unreviewed readings); ``ocr_text``/``ocr_value`` show what the machine read."""
    start, end, slug = _range(db, run_id, since_ms, until_ms)
    Session = request.app.state.Session
    header = ["timestamp_utc", "t_ms", "camera_id", "device_id", "device_name", "device_model", "is_own_device",
              "external_id", "metric", "unit", "value", "status", "ocr_text", "ocr_value", "confidence", "note",
              "reviewed_by", "reviewed_utc", "frame_id", "frame_sha256"]

    def rows() -> Iterator[list]:
        with Session() as s:
            q = (select(Reading, Device, Field.unit, Frame.sha256)
                 .join(Device, Device.id == Reading.device_id)
                 .join(Field, Field.id == Reading.field_id)
                 .join(Frame, Frame.id == Reading.frame_id)
                 .where(Reading.t_ms >= start, Reading.t_ms <= end)
                 .order_by(Reading.t_ms, Reading.device_id, Reading.field_id)
                 .execution_options(yield_per=2000))
            for r, d, unit, sha in s.execute(q):
                yield [iso(r.t_ms), r.t_ms, d.camera_id, d.id, _txt(d.name), _txt(d.model), int(d.is_own), _txt(d.external_id),
                       r.metric, unit or "", "" if r.final_value is None else r.final_value, r.status, r.raw_text,
                       "" if r.value is None else r.value, r.confidence, _txt(r.note), _txt(r.reviewed_by),
                       iso(r.reviewed_ms), r.frame_id, sha]

    return StreamingResponse(_csv_stream(header, rows()), media_type="text/csv",
                             headers=_attachment(f"readings-{slug}.csv"))


@router.get("/export/frames.csv")
def export_frames(request: Request, run_id: int | None = None, since_ms: int | None = None,
                  until_ms: int | None = None, camera_id: str | None = None, db: Session = Depends(get_db)):
    """Manifest of stored images with SHA-256 hashes, to prove the evidence was not altered."""
    start, end, slug = _range(db, run_id, since_ms, until_ms)
    Session = request.app.state.Session
    header = ["frame_id", "camera_id", "captured_utc", "captured_ms", "received_utc", "sha256", "size_bytes",
              "width", "height", "path", "ocr_status"]

    def rows() -> Iterator[list]:
        with Session() as s:
            q = select(Frame).where(Frame.captured_ms >= start, Frame.captured_ms <= end)
            if camera_id:
                q = q.where(Frame.camera_id == camera_id)
            for f in s.scalars(q.order_by(Frame.captured_ms).execution_options(yield_per=2000)):
                yield [f.id, f.camera_id, iso(f.captured_ms), f.captured_ms, iso(f.received_ms), f.sha256, f.size_bytes,
                       f.width, f.height, f.path, f.ocr_status]

    return StreamingResponse(_csv_stream(header, rows()), media_type="text/csv",
                             headers=_attachment(f"frames-{slug}.csv"))


@router.get("/export/reference.csv")
def export_reference(request: Request, run_id: int | None = None, since_ms: int | None = None,
                     until_ms: int | None = None, db: Session = Depends(get_db)):
    start, end, slug = _range(db, run_id, since_ms, until_ms)
    Session = request.app.state.Session

    def rows() -> Iterator[list]:
        with Session() as s:
            q = (select(ReferenceReading).where(ReferenceReading.t_ms >= start, ReferenceReading.t_ms <= end)
                 .order_by(ReferenceReading.t_ms).execution_options(yield_per=2000))
            for r in s.scalars(q):
                yield [iso(r.t_ms), r.t_ms, _txt(r.source), _txt(r.device_ref), r.metric, r.value]

    return StreamingResponse(_csv_stream(["timestamp_utc", "t_ms", "source", "device", "metric", "value"], rows()),
                             media_type="text/csv", headers=_attachment(f"reference-{slug}.csv"))


def _tz_minutes(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw or raw.upper() in ("Z", "UTC"):
        return 0
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", raw)
    if not m:
        raise HTTPException(400, "time zone must look like +05:30 or -04:00")
    minutes = int(m.group(2)) * 60 + int(m.group(3) or 0)
    return minutes if m.group(1) == "+" else -minutes


@router.post("/reference/import")
def import_reference(
    file: UploadFile = File(...),
    source: str = Form("own-cloud"),
    device: str = Form(""),
    tz: str = Form("UTC"),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
):
    """Upload a CSV of readings from your own cloud or a reference instrument."""
    source = source.strip()[:64] or "own-cloud"
    raw = file.file.read(50 * 1024 * 1024 + 1)
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, "CSV larger than 50 MB; split it")
    try:
        rows = parse_reference_csv(raw.decode("utf-8-sig", errors="replace"), device.strip() or None)
        added = store_reference_rows(db, source, rows, _tz_minutes(tz))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(db, user, "reference.import", source=source, filename=file.filename, rows=len(rows), added=added)
    db.commit()
    return {"rows": len(rows), "added": added}


@router.get("/reference/sources")
def reference_sources(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ReferenceReading.source, ReferenceReading.device_ref, ReferenceReading.metric, func.count(),
               func.min(ReferenceReading.t_ms), func.max(ReferenceReading.t_ms))
        .group_by(ReferenceReading.source, ReferenceReading.device_ref, ReferenceReading.metric)
        .order_by(ReferenceReading.source, ReferenceReading.device_ref, ReferenceReading.metric)
    ).all()
    return [{"source": s, "device": d, "metric": m, "count": n, "first_ms": a, "last_ms": b} for s, d, m, n, a, b in rows]


@router.delete("/reference")
def delete_reference(source: str, device: str | None = None, db: Session = Depends(get_db),
                     user: str = Depends(current_user)):
    q = delete(ReferenceReading).where(ReferenceReading.source == source)
    if device:
        q = q.where(ReferenceReading.device_ref == device)
    n = db.execute(q).rowcount
    audit(db, user, "reference.delete", source=source, device=device, rows=n)
    db.commit()
    return {"deleted": n}


@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.at_ms.desc()).limit(min(limit, 1000))).all()
    return [{"id": a.id, "at_ms": a.at_ms, "actor": a.actor, "action": a.action, "detail": a.detail} for a in rows]
