"""Reference data: your own monitors' cloud readings, or a reference-grade instrument.

Comes in either through ``POST /api/agent/reference`` (JSON, token auth) or a CSV upload
on the Data page. Both end up in ``reference_readings`` and are drawn on the charts next
to the camera-read values.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ReferenceReading

METRIC_ALIASES = {
    "pm25": "pm25", "pm2.5": "pm25", "pm2_5": "pm25", "pm2,5": "pm25",
    "pm10": "pm10", "pm1": "pm1", "pm1.0": "pm1", "pm1_0": "pm1",
    "co2": "co2", "tvoc": "tvoc", "voc": "tvoc", "hcho": "hcho", "formaldehyde": "hcho",
    "temp": "temp", "temperature": "temp", "rh": "rh", "humidity": "rh", "aqi": "aqi",
}
TIME_COLUMNS = ("timestamp", "time", "datetime", "date_time", "date", "ts", "t")
DEVICE_COLUMNS = ("device", "device_id", "deviceid", "sensor", "serial", "id")


class RefRow(BaseModel):
    device: str
    metric: str
    t: str | int | float
    value: float


def normalize_metric(name: str) -> str | None:
    key = re.sub(r"\s*\(.*\)$", "", name.strip().lower())  # drop "(µg/m³)" style suffixes
    key = key.replace(" ", "").replace("-", "_")
    return METRIC_ALIASES.get(key)


def parse_time_ms(value: str | int | float, tz_offset_min: int = 0) -> int:
    """ISO 8601 (with or without zone), or epoch seconds / milliseconds."""
    if isinstance(value, (int, float)):
        n = float(value)
        return int(n if n > 1e11 else n * 1000)
    s = str(value).strip()
    try:
        n = float(s)
        return int(n if n > 1e11 else n * 1000)
    except ValueError:
        pass
    if len(s) > 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=tz_offset_min)))
    return int(dt.timestamp() * 1000)


def store_reference_rows(db: Session, source: str, rows: list[RefRow], tz_offset_min: int = 0) -> int:
    parsed = []
    for r in rows:
        metric = normalize_metric(r.metric) or r.metric.strip().lower()[:32]
        parsed.append((r.device.strip()[:255], metric, parse_time_ms(r.t, tz_offset_min), float(r.value)))
    if not parsed:
        return 0
    t_min = min(p[2] for p in parsed)
    t_max = max(p[2] for p in parsed)
    existing = set(
        db.execute(
            select(ReferenceReading.device_ref, ReferenceReading.metric, ReferenceReading.t_ms).where(
                ReferenceReading.source == source,
                ReferenceReading.t_ms >= t_min,
                ReferenceReading.t_ms <= t_max,
            )
        ).all()
    )
    added = 0
    for device, metric, t_ms, value in parsed:
        key = (device, metric, t_ms)
        if key in existing:
            continue
        existing.add(key)
        db.add(ReferenceReading(source=source, device_ref=device, metric=metric, t_ms=t_ms, value=value))
        added += 1
    return added


def parse_reference_csv(text: str, default_device: str | None) -> list[RefRow]:
    """Accepts long format (timestamp, device, metric, value) or wide format
    (timestamp, [device], pm25, co2, ...)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    cols = {c.strip().lower(): c for c in reader.fieldnames if c}
    tcol = next((cols[c] for c in TIME_COLUMNS if c in cols), None)
    if tcol is None:
        raise ValueError(f"CSV needs a time column (one of: {', '.join(TIME_COLUMNS)})")
    dcol = next((cols[c] for c in DEVICE_COLUMNS if c in cols), None)
    long_format = "metric" in cols and "value" in cols
    metric_cols = {}
    if not long_format:
        for low, orig in cols.items():
            m = normalize_metric(low)
            if m and orig not in (tcol, dcol):
                metric_cols[orig] = m
        if not metric_cols:
            raise ValueError("no metric columns recognised (expected e.g. pm25, pm10, co2, temp, rh)")
    if dcol is None and not default_device:
        raise ValueError("CSV has no device column; enter a device name in the form")

    rows: list[RefRow] = []
    for rec in reader:
        t = (rec.get(tcol) or "").strip()
        if not t:
            continue
        device = ((rec.get(dcol) or "").strip() if dcol else "") or (default_device or "")
        if long_format:
            items = [(rec.get(cols["metric"]) or "", rec.get(cols["value"]))]
        else:
            items = [(m, rec.get(orig)) for orig, m in metric_cols.items()]
        for metric, raw in items:
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if metric:
                rows.append(RefRow(device=device, metric=metric, t=t, value=value))
    return rows
