"""Results of a test run: comparison statistics, figures data and CSVs for a report."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import now_ms
from ..deps import get_db
from ..models import Camera, Device, Field, ReferenceReading, TestRun
from ..ocr.pipeline import METRICS
from ..results import AVG_CHOICES_S, compute
from ..storage import iso

router = APIRouter(prefix="/api", tags=["results"])


def _params(db: Session, run_id: int | None, since_ms: int | None, until_ms: int | None, metric: str | None,
            avg_s: int) -> tuple[int, int, str, dict | None]:
    run = None
    if run_id is not None:
        r = db.get(TestRun, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        since_ms, until_ms = r.started_ms, r.ended_ms or now_ms()
        run = {"id": r.id, "name": r.name, "notes": r.notes, "started_ms": r.started_ms, "ended_ms": r.ended_ms,
               "created_by": r.created_by}
    if since_ms is None or until_ms is None or until_ms <= since_ms:
        raise HTTPException(400, "choose a test run or a valid time range")
    if avg_s not in AVG_CHOICES_S:
        raise HTTPException(400, f"avg_s must be one of {AVG_CHOICES_S}")
    if not metric:
        used = [m for (m,) in db.execute(select(Field.metric).where(Field.active.is_(True)).distinct()).all()]
        used += [m for (m,) in db.execute(select(ReferenceReading.metric).distinct()).all()]
        order = list(METRICS)
        metric = sorted(set(used), key=lambda m: order.index(m) if m in order else 99)[0] if used else "pm25"
    return since_ms, until_ms, metric, run


@router.get("/results")
def results(run_id: int | None = None, since_ms: int | None = None, until_ms: int | None = None,
            metric: str | None = None, avg_s: int = 3600, reference: str | None = None, db: Session = Depends(get_db)):
    since, until, metric, run = _params(db, run_id, since_ms, until_ms, metric, avg_s)
    res = compute(db, metric, since, until, avg_s, reference)
    res.pop("_paired")
    res.pop("_ref_points")
    devices = db.scalars(select(Device).where(Device.active.is_(True)).order_by(Device.id)).all()
    cams = {c.id: c for c in db.scalars(select(Camera)).all()}
    res["run"] = run
    res["unit"] = METRICS.get(metric, {}).get("unit", "")
    res["metric_label"] = METRICS.get(metric, {}).get("label", metric)
    res["devices"] = [{"id": d.id, "name": d.name, "model": d.model, "is_own": d.is_own, "external_id": d.external_id,
                       "camera_id": d.camera_id,
                       "still_interval_s": cams[d.camera_id].still_interval_s if d.camera_id in cams else None}
                      for d in devices]
    return res


def _csv(rows: list[list], name: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _txt(v):
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


@router.get("/results/stats.csv")
def results_stats_csv(run_id: int | None = None, since_ms: int | None = None, until_ms: int | None = None,
                      metric: str | None = None, avg_s: int = 3600, reference: str | None = None,
                      db: Session = Depends(get_db)):
    since, until, metric, run = _params(db, run_id, since_ms, until_ms, metric, avg_s)
    res = compute(db, metric, since, until, avg_s, reference)
    cols = ["label", "model", "is_reference", "n", "completeness_pct", "mean_ref", "mean", "slope", "intercept", "r2",
            "rmse", "nrmse_pct", "mae", "bias", "bias_pct", "lag_s", "lag_r"]
    rows = [["metric", metric, "unit", METRICS.get(metric, {}).get("unit", ""), "averaging_s", avg_s,
             "reference", res["reference"]["label"], "from_utc", iso(since), "to_utc", iso(until)], cols]
    rows += [[_txt(r.get(c)) if r.get(c) is not None else "" for c in cols] for r in res["rows"]]
    return _csv(rows, f"results-{metric}-{avg_s}s.csv")


@router.get("/results/pairs.csv")
def results_pairs_csv(run_id: int | None = None, since_ms: int | None = None, until_ms: int | None = None,
                      metric: str | None = None, avg_s: int = 3600, reference: str | None = None,
                      db: Session = Depends(get_db)):
    """The averaged values behind the statistics: one row per time bucket, reference first."""
    since, until, metric, run = _params(db, run_id, since_ms, until_ms, metric, avg_s)
    res = compute(db, metric, since, until, avg_s, reference)
    keys = list(res["_paired"])
    labels = {r["key"]: r["label"] for r in res["rows"]}
    rows = [["timestamp_utc", f"{res['reference']['label']} (reference)", *[_txt(labels[k]) for k in keys]]]
    for t, v in res["_ref_points"]:
        rows.append([iso(t), v, *[res["_paired"][k].get(t, "") for k in keys]])
    return _csv(rows, f"paired-{metric}-{avg_s}s.csv")
