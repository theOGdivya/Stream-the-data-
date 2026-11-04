"""How accurate are the numbers read off the screens?

Two sources of truth, both from people:

* **Spot checks** - automatically accepted readings picked at random and checked by a person.
  Because they are a random sample, the share found right is an unbiased estimate of how
  accurate the automatic readings are (reported with a 95 % Wilson confidence interval).
* **The review queue** - readings the machine was unsure about. Not a random sample (these
  are the hard cases), so they show how much the review step corrects, not the accuracy.
"""
from __future__ import annotations

import math
import random

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import CORRECTED, OK, REJECTED, VERIFIED, Device, Field, Reading
from .plausibility import NOTE as ODD_NOTE

SPOT = "spot"
SPOT_UNCLEAR = "spot-unclear"
SPOT_TARGET = 50  # spot checks per monitor and reading that give a useful confidence interval


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95 % confidence interval for a proportion k/n, in percent."""
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)


def reading_accuracy(db: Session, since_ms: int, until_ms: int) -> list[dict]:
    """One row per monitor and reading (e.g. Brand X, PM2.5)."""
    spot = Reading.check_kind == SPOT
    queue = Reading.check_kind.is_(None) & Reading.reviewed_ms.is_not(None)
    n = func.count
    rows = db.execute(
        select(
            Reading.device_id, Reading.metric,
            n(),
            func.sum(case((Reading.status == OK, 1), else_=0)),
            func.sum(case((spot, 1), else_=0)),
            func.sum(case((spot & (Reading.status == VERIFIED), 1), else_=0)),
            func.sum(case((queue, 1), else_=0)),
            func.sum(case((queue & (Reading.status == VERIFIED), 1), else_=0)),
            func.sum(case((queue & (Reading.status == CORRECTED), 1), else_=0)),
            func.sum(case((queue & (Reading.status == REJECTED), 1), else_=0)),
            func.sum(case((Reading.note.like(f"{ODD_NOTE}%"), 1), else_=0)),
        )
        .where(Reading.t_ms >= since_ms, Reading.t_ms <= until_ms)
        .group_by(Reading.device_id, Reading.metric)
    ).all()
    devices = {d.id: d for d in db.scalars(select(Device)).all()}
    out = []
    for dev_id, metric, total, ok, spot_n, spot_right, q_n, q_right, q_corr, q_rej, odd in rows:
        spot_n, spot_right = int(spot_n or 0), int(spot_right or 0)
        lo, hi = wilson(spot_right, spot_n)
        d = devices.get(dev_id)
        out.append({
            "device_id": dev_id, "device_name": d.name if d else f"device {dev_id}", "metric": metric,
            "readings": int(total), "auto_accepted": int(ok or 0) + spot_n,
            "spot_checked": spot_n, "spot_right": spot_right,
            "accuracy_pct": round(100 * spot_right / spot_n, 1) if spot_n else None, "ci_low": lo, "ci_high": hi,
            "reviewed": int(q_n or 0), "review_machine_right": int(q_right or 0), "review_corrected": int(q_corr or 0),
            "review_unreadable": int(q_rej or 0), "odd_jumps": int(odd or 0),
        })
    out.sort(key=lambda r: (r["device_name"], r["metric"]))
    return out


def pick_spot_checks(db: Session, since_ms: int, until_ms: int, limit: int = 10,
                     rng: random.Random | None = None) -> list[Reading]:
    """Automatically accepted readings, picked at random, from the monitors and readings with
    the fewest spot checks so far (so every one reaches the target)."""
    rng = rng or random.Random()
    fields = db.execute(
        select(Field.id, func.sum(case((Reading.check_kind == SPOT, 1), else_=0)), func.count(Reading.id))
        .join(Reading, Reading.field_id == Field.id)
        .where(Reading.t_ms >= since_ms, Reading.t_ms <= until_ms)
        .group_by(Field.id)
    ).all()
    # Fewest checks first; ties in random order.
    fields = sorted(fields, key=lambda f: (int(f[1] or 0), rng.random()))
    fields = [f for f in fields if int(f[1] or 0) < SPOT_TARGET] or fields
    picked: list[Reading] = []
    seen: set[int] = set()
    counts: dict[int, int] = {}
    for attempt in range(4 * limit):
        if len(picked) >= limit or not fields:
            break
        field_id = fields[attempt % len(fields)][0]  # round-robin, fewest checks first
        eligible = (select(Reading).where(Reading.field_id == field_id, Reading.status == OK, Reading.check_kind.is_(None),
                                          Reading.value.is_not(None), Reading.t_ms >= since_ms, Reading.t_ms <= until_ms))
        if field_id not in counts:
            counts[field_id] = db.scalar(select(func.count()).select_from(eligible.subquery())) or 0
        if counts[field_id] == 0:
            continue
        # Uniform over the readings themselves (not over time, which would favour readings after gaps).
        r = db.scalars(eligible.order_by(Reading.t_ms, Reading.id).offset(rng.randrange(counts[field_id])).limit(1)).first()
        if r is not None and r.id not in seen:
            seen.add(r.id)
            picked.append(r)
    return picked
