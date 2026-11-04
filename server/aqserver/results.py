"""Comparison statistics for a test run: each monitor against a chosen reference.

Metrics follow common air-sensor evaluation practice (e.g. US EPA 2021 PM2.5 sensor
protocol, AQ-SPEC): averaged pairs, OLS slope/intercept, R², RMSE, normalised RMSE,
MAE, mean bias, data completeness. A response delay is estimated by shifting the
monitor's fine-grained series against the reference and taking the best correlation.
"""
from __future__ import annotations

import math

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import GOOD_STATUSES, Device, Reading, ReferenceReading
from .series import bucketed

AVG_CHOICES_S = (60, 300, 900, 3600, 86400)
LAG_BUCKET_S = 30
LAG_MAX_STEPS = 20  # +/- 10 minutes
LAG_MIN_PAIRS = 20
LAG_MIN_R = 0.5


def _r(x, nd=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


def pair_stats(xs: np.ndarray, ys: np.ndarray) -> dict:
    """x = reference, y = monitor."""
    n = int(len(xs))
    out = {"n": n, "mean_ref": None, "mean": None, "slope": None, "intercept": None, "r2": None, "rmse": None,
           "nrmse_pct": None, "mae": None, "bias": None, "bias_pct": None}
    if n == 0:
        return out
    mx, my = float(xs.mean()), float(ys.mean())
    diff = ys - xs
    out.update(mean_ref=_r(mx), mean=_r(my), rmse=_r(math.sqrt(float((diff ** 2).mean()))),
               mae=_r(float(np.abs(diff).mean())), bias=_r(float(diff.mean())))
    if mx:
        out["nrmse_pct"] = _r(100 * out["rmse"] / abs(mx), 1)
        out["bias_pct"] = _r(100 * out["bias"] / abs(mx), 1)
    if n >= 3 and xs.std() > 0:
        slope, intercept = np.polyfit(xs, ys, 1)
        out.update(slope=_r(slope), intercept=_r(intercept))
        if ys.std() > 0:
            out["r2"] = _r(float(np.corrcoef(xs, ys)[0, 1]) ** 2)
    return out


def estimate_lag(ref_points: list, dev_points: list, bucket_ms: int,
                 max_steps: int = LAG_MAX_STEPS) -> tuple[float | None, float | None]:
    """Seconds the monitor trails the reference (+) or leads it (-), and the correlation at
    that shift. None when there is not enough variation or no clear best shift."""
    if not ref_points or not dev_points:
        return None, None
    t0 = min(ref_points[0][0], dev_points[0][0])
    t1 = max(ref_points[-1][0], dev_points[-1][0])
    n = int((t1 - t0) // bucket_ms) + 1
    x = np.full(n, np.nan)
    y = np.full(n, np.nan)
    for t, v in ref_points:
        x[int((t - t0) // bucket_ms)] = v
    for t, v in dev_points:
        y[int((t - t0) // bucket_ms)] = v
    best = None
    for k in range(-max_steps, max_steps + 1):
        if abs(k) >= n:
            continue  # series too short for this shift
        if k >= 0:
            xs, ys = x[: n - k], y[k:]
        else:
            xs, ys = x[-k:], y[: n + k]
        ok = ~np.isnan(xs) & ~np.isnan(ys)
        if ok.sum() < LAG_MIN_PAIRS:
            continue
        a, b = xs[ok], ys[ok]
        if a.std() == 0 or b.std() == 0:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if best is None or r > best[1]:
            best = (k, r)
    if best is None or best[1] < LAG_MIN_R or abs(best[0]) == max_steps:
        return None, (_r(best[1]) if best else None)
    return best[0] * bucket_ms / 1000, _r(best[1])


def default_reference(series: list[dict]) -> str | None:
    """Prefer independent reference data (e.g. a reference instrument) over our own
    monitors' cloud data, then our own monitor as read by camera, then anything."""
    own_ids = {s.get("external_id") for s in series if s.get("is_own") and s.get("external_id")}
    refs = sorted((s for s in series if s["kind"] == "reference"),
                  key=lambda s: (s.get("device_ref") in own_ids, -len(s["points"])))
    if refs:
        return refs[0]["key"]
    own = [s for s in series if s.get("is_own")]
    if own:
        return own[0]["key"]
    return series[0]["key"] if series else None


def own_device_check(db: Session, metric: str, since_ms: int, until_ms: int, window_ms: int = 30_000) -> list[dict]:
    """For our own monitors (with a cloud ID): compare what the camera read off the screen
    with what the monitor sent to the cloud, nearest values within ``window_ms``."""
    out = []
    for d in db.scalars(select(Device).where(Device.is_own.is_(True), Device.external_id.is_not(None))).all():
        cam = db.execute(
            select(Reading.t_ms, func.coalesce(Reading.final_value, Reading.value))
            .where(Reading.device_id == d.id, Reading.metric == metric, Reading.status.in_(GOOD_STATUSES),
                   Reading.t_ms >= since_ms, Reading.t_ms <= until_ms).order_by(Reading.t_ms)).all()
        ref = db.execute(
            select(ReferenceReading.t_ms, ReferenceReading.value)
            .where(ReferenceReading.device_ref == d.external_id, ReferenceReading.metric == metric,
                   ReferenceReading.t_ms >= since_ms - window_ms, ReferenceReading.t_ms <= until_ms + window_ms)
            .order_by(ReferenceReading.t_ms)).all()
        if not cam or not ref:
            continue
        rt = np.array([t for t, _ in ref], dtype=np.int64)
        rv = np.array([v for _, v in ref], dtype=float)
        diffs = []
        for t, v in cam:
            i = int(np.searchsorted(rt, t))
            cands = [j for j in (i - 1, i) if 0 <= j < len(rt)]
            j = min(cands, key=lambda j: abs(int(rt[j]) - t))
            if abs(int(rt[j]) - t) <= window_ms:
                diffs.append(v - rv[j])
        if not diffs:
            continue
        a = np.abs(np.array(diffs))
        out.append({"device_id": d.id, "name": d.name, "external_id": d.external_id, "n": len(diffs),
                    "exact_pct": _r(100 * float((a < 0.5).mean()), 1), "mae": _r(float(a.mean())),
                    "within_5pct": _r(100 * float((a <= 0.05 * np.maximum(1, np.abs(rv.mean()))).mean()), 1)})
    return out


def ocr_quality(db: Session, since_ms: int, until_ms: int) -> dict:
    rows = db.execute(select(Reading.metric, Reading.status, func.count())
                      .where(Reading.t_ms >= since_ms, Reading.t_ms <= until_ms)
                      .group_by(Reading.metric, Reading.status)).all()
    totals: dict[str, int] = {}
    by_metric: dict[str, dict[str, int]] = {}
    for metric, status, n in rows:
        totals[status] = totals.get(status, 0) + n
        by_metric.setdefault(metric, {})[status] = n
    return {"totals": totals, "by_metric": by_metric}


# Humidity bands for the breakdown: low-cost PM sensors are known to read high in humid air.
RH_BANDS = [(None, 50.0, "under 50%"), (50.0, 70.0, "50–70%"), (70.0, None, "70% and above")]
NO_RH_BREAKDOWN = {"rh", "temp"}


def humidity_series(db: Session, since_ms: int, until_ms: int, bucket_ms: int,
                    ref_key: str | None) -> tuple[str | None, dict[int, float]]:
    """Relative humidity per averaging period, and where it came from: the reference's own
    humidity if it has one, else imported humidity data, else the average of the monitors."""
    rh = [s for s in bucketed(db, "rh", since_ms, until_ms, bucket_ms) if s["points"]]
    if not rh:
        return None, {}
    by_key = {s["key"]: s for s in rh}
    refs = [s for s in rh if s["kind"] == "reference"]
    if ref_key in by_key:
        s = by_key[ref_key]
        return f"{s['label']}, the reference", {t: v for t, v in s["points"]}
    if refs:
        s = max(refs, key=lambda s: len(s["points"]))
        return s["label"], {t: v for t, v in s["points"]}
    acc: dict[int, list[float]] = {}
    for s in rh:
        for t, v in s["points"]:
            acc.setdefault(t, []).append(v)
    return f"average of {len(rh)} monitor{'s' if len(rh) > 1 else ''}", {t: sum(v) / len(v) for t, v in acc.items()}


def humidity_breakdown(db: Session, metric: str, since_ms: int, until_ms: int, bucket_ms: int, ref_key: str | None,
                       pairs: dict[str, list[tuple[float, float, int]]], labels: dict[str, str]) -> dict | None:
    """Agreement with the reference split by humidity band, for every monitor."""
    if metric in NO_RH_BREAKDOWN or not pairs:
        return None
    source, rh = humidity_series(db, since_ms, until_ms, bucket_ms, ref_key)
    if not rh:
        return None
    rows = []
    for key, pts in pairs.items():
        bands = []
        for lo, hi, _ in RH_BANDS:
            sel = [(x, y) for x, y, t in pts if t in rh and (lo is None or rh[t] >= lo) and (hi is None or rh[t] < hi)]
            xs = np.array([p[0] for p in sel], dtype=float)
            ys = np.array([p[1] for p in sel], dtype=float)
            st = pair_stats(xs, ys)
            bands.append({k: st[k] for k in ("n", "mean_ref", "mean", "bias", "bias_pct", "mae")})
        rows.append({"key": key, "label": labels[key], "bands": bands,
                     "without_rh": sum(1 for *_, t in pts if t not in rh)})
    return {"source": source, "bands": [b[2] for b in RH_BANDS], "rows": rows}


def compute(db: Session, metric: str, since_ms: int, until_ms: int, avg_s: int, reference: str | None) -> dict:
    bucket_ms = avg_s * 1000
    series = bucketed(db, metric, since_ms, until_ms, bucket_ms)
    by_key = {s["key"]: s for s in series}
    ref_key = reference if reference in by_key else default_reference(series)
    # Every time bucket the period touches (the first and last may be partial).
    expected = max(1, (until_ms - 1) // bucket_ms - since_ms // bucket_ms + 1)
    rows, scatter, paired, pairs = [], {}, {}, {}
    ref = by_key.get(ref_key)
    fine = {s["key"]: s["points"] for s in bucketed(db, metric, since_ms, until_ms, LAG_BUCKET_S * 1000)} if ref else {}
    for s in series:
        info = {"key": s["key"], "label": s["label"], "kind": s["kind"], "model": s.get("model"),
                "is_own": s.get("is_own", False), "points": len(s["points"]),
                "completeness_pct": _r(100 * min(1.0, len(s["points"]) / expected), 1),
                "is_reference": s["key"] == ref_key}
        if s["key"] == ref_key or ref is None:
            vals = [v for _, v in s["points"]]
            info["mean"] = _r(sum(vals) / len(vals)) if vals else None
            rows.append(info)
            continue
        rmap = dict((t, v) for t, v in ref["points"])
        pts = [(rmap[t], v, t) for t, v in s["points"] if t in rmap]
        xs = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        info.update(pair_stats(xs, ys))
        lag, lag_r = estimate_lag(fine.get(ref_key, []), fine.get(s["key"], []), LAG_BUCKET_S * 1000)
        info.update(lag_s=lag, lag_r=lag_r)
        rows.append(info)
        sample = pts if len(pts) <= 3000 else [pts[int(i * len(pts) / 3000)] for i in range(3000)]
        scatter[s["key"]] = [[p[0], p[1]] for p in sample]
        paired[s["key"]] = {p[2]: p[1] for p in pts}
        pairs[s["key"]] = pts
    labels = {s["key"]: s["label"] for s in series}
    return {
        "metric": metric, "since_ms": since_ms, "until_ms": until_ms, "avg_s": avg_s, "expected_points": expected,
        "reference": {"key": ref_key, "label": ref["label"] if ref else None},
        "series": series, "rows": rows, "scatter": scatter,
        "own_checks": own_device_check(db, metric, since_ms, until_ms),
        "ocr": ocr_quality(db, since_ms, until_ms),
        "humidity": humidity_breakdown(db, metric, since_ms, until_ms, bucket_ms, ref_key, pairs, labels),
        "_paired": paired, "_ref_points": ref["points"] if ref else [],
    }
