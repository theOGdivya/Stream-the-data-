"""Catch odd readings automatically.

A number that jumps far away from the readings just before and after it, and straight back
(812, 2, 815 when the reader saw only part of the screen; 12, 812, 13 with a ghost digit),
is almost certainly a misread. It goes to review with a note instead of into the results.

A real change in the air looks different: it lasts (a step) or builds up over several
pictures (a rise), so its neighbours on one side follow it. A reading is checked once it has
neighbours on both sides, so the decision trails the newest picture by one still. Only
readings accepted automatically are checked; anything a person confirmed is left alone.
"""
from __future__ import annotations

from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import GOOD_STATUSES, OK, REVIEW, Reading

BEFORE = 3  # neighbours used on each side
AFTER = 2
WINDOW_MS = 10 * 60_000  # neighbours further away than this are not used
REL_TOL = 0.5  # a jump must exceed half the typical value...
# ...and at least this much, so normal sensor noise at low levels is never flagged.
FLOORS = {"pm1": 15, "pm25": 15, "pm10": 20, "co2": 150, "tvoc": 200, "hcho": 0.05, "temp": 3, "rh": 10, "aqi": 25}
ABSOLUTE_ONLY = {"temp", "rh"}  # "half the value" means nothing for °C or % RH
NOTE = "unusual jump"


def odd_against(value: float, before: list[float], after: list[float], metric: str) -> float | None:
    """The typical neighbouring value when ``value`` is an isolated jump, else None."""
    if not before or not after or len(before) + len(after) < 3:
        return None
    n = before + after
    lo, hi, mid = min(n), max(n), median(n)
    # Neighbours that disagree among themselves (noisy sensor, a change under way) widen the
    # tolerance, so only a point far outside everything around it is flagged.
    rel = 0.0 if metric in ABSOLUTE_ONLY else REL_TOL * abs(mid)
    tol = max(FLOORS.get(metric, 0.0), rel, 2 * (hi - lo))
    return mid if value > hi + tol or value < lo - tol else None


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def check_around(db: Session, field_id: int, metric: str, t_ms: int) -> list[int]:
    """Check the readings of one number next to ``t_ms`` (the new one and the ones just before
    and after it). Returns the ids sent to review."""
    db.flush()
    value = func.coalesce(Reading.final_value, Reading.value)
    rows = db.execute(
        select(Reading, value)
        .where(Reading.field_id == field_id, Reading.status.in_(GOOD_STATUSES), value.is_not(None),
               Reading.t_ms >= t_ms - 2 * WINDOW_MS, Reading.t_ms <= t_ms + 2 * WINDOW_MS)
        .order_by(Reading.t_ms)
    ).all()
    if len(rows) < 3:
        return []
    times = [r.t_ms for r, _ in rows]
    # Position of the new reading, or of where it would be if it is not usable itself.
    i = next((k for k, t in enumerate(times) if t >= t_ms), len(times))
    exact = i < len(times) and times[i] == t_ms
    candidates = [i - 1, i, i + 1] if exact else [i - 1, i]
    flagged: list[int] = []
    removed: set[int] = set()
    for c in candidates:
        if not 0 <= c < len(rows):
            continue
        reading, v = rows[c]
        if reading.status != OK:
            continue
        before = [float(rows[k][1]) for k in range(c - 1, -1, -1)
                  if k not in removed and times[c] - times[k] <= WINDOW_MS][:BEFORE]
        after = [float(rows[k][1]) for k in range(c + 1, len(rows))
                 if k not in removed and times[k] - times[c] <= WINDOW_MS][:AFTER]
        typical = odd_against(float(v), before, after, metric)
        if typical is None:
            continue
        reading.status, reading.final_value = REVIEW, None
        reading.note = f"{NOTE}: {_fmt(float(v))} between readings of about {_fmt(typical)}"
        removed.add(c)
        flagged.append(reading.id)
    return flagged
