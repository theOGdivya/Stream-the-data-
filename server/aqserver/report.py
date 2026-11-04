"""Turn a finished test into white-paper material.

* ``methods_text`` - draft methods paragraph(s), shown on the Results page and used in the report.
* ``build_report`` - a Word document with the setup, events, statistics tables, figures
  (time series with event markers, scatter plots), humidity breakdown, reading accuracy,
  a lined-up picture of the monitors, and the methods text. Word can save it as PDF.

Times are written in the reader's time zone (``tz_min`` = minutes east of UTC, sent by the
browser), and the zone is named next to them.
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accuracy import reading_accuracy, wilson
from .config import Settings
from .models import GOOD_STATUSES, Camera, Device, Frame, Reading, ReferenceReading
from .ocr.pipeline import METRICS
from .results import compute

AVG_LABEL = {60: "1-minute", 300: "5-minute", 900: "15-minute", 3600: "1-hour", 86400: "24-hour"}
# Same colours as the dashboard charts (light theme), so figures match what the team sees.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER = "#7a7a7a"


class Clock:
    """Formats times in the reader's time zone."""

    def __init__(self, tz_min: int = 0):
        self.tz_min = max(-14 * 60, min(14 * 60, int(tz_min)))
        self.tz = timezone(timedelta(minutes=self.tz_min))

    @property
    def zone(self) -> str:
        if self.tz_min == 0:
            return "UTC"
        sign = "+" if self.tz_min > 0 else "-"
        h, m = divmod(abs(self.tz_min), 60)
        return f"UTC{sign}{h:02d}:{m:02d}"

    def dt(self, ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000, self.tz)

    def naive(self, ms: int) -> datetime:
        return self.dt(ms).replace(tzinfo=None)

    def fmt(self, ms: int | None) -> str:
        if ms is None:
            return "–"
        d = self.dt(ms)
        return f"{d.day} {d.strftime('%b %Y, %H:%M')}"

    def time(self, ms: int) -> str:
        return self.dt(ms).strftime("%H:%M")


def duration(ms: int) -> str:
    m = int(ms // 60_000)
    d, rest = divmod(m, 1440)
    h, mi = divmod(rest, 60)
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    if h:
        return f"{h} h {mi} min" if mi else f"{h} h"
    return f"{mi} min"


def num(v, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    s = f"{v:,.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fixed(v, nd: int = 3) -> str:
    """Fixed decimals, for slope and R² (1.000, not 1)."""
    return "–" if v is None else f"{v:.{nd}f}"


def lag_text(v) -> str:
    return "–" if v is None else ("0 s" if v == 0 else f"{v:+g} s")


def signed(v, nd: int = 2) -> str:
    return "–" if v is None else (("+" if v > 0 else "") + num(v, nd))


def _p(n, total) -> str:
    return f"{100 * (n or 0) / (total or 1):.1f}%"


def metric_label(metric: str) -> str:
    return METRICS.get(metric, {}).get("label", metric)


def metric_unit(metric: str) -> str:
    return METRICS.get(metric, {}).get("unit", "")


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# --- methods text ------------------------------------------------------------------------------

def methods_text(*, run: dict | None, since_ms: int, until_ms: int, avg_s: int, devices: list[dict],
                 comparisons: list[tuple[str, str | None]], ocr: dict, accuracy: list[dict], events: list[dict],
                 humidity_source: str | None, own_checks: list[dict], clock: Clock) -> str:
    """Draft methods text. ``comparisons``: (metric, reference label) for each reading analysed."""
    parts = []
    intervals = sorted({d["still_interval_s"] for d in devices if d.get("still_interval_s")})
    cams = len({d["camera_id"] for d in devices})
    labels = [metric_label(m) for m, _ in comparisons] or ["readings"]
    n = len(devices)
    who = f"Test “{run['name']}”" if run else "The test"
    every = ", storing a still picture every " + "/".join(num(i, 1) for i in intervals) + " s" if intervals else ""
    parts.append(
        f"{who} ran from {clock.fmt(since_ms)} to {clock.fmt(until_ms)} "
        f"({clock.zone}; {duration(until_ms - since_ms)}). {n} monitor{' was' if n == 1 else 's were'} filmed side by side by "
        f"{cams} camera{'' if cams == 1 else 's'}{every}. "
        f"The {_join(labels)} values shown on each screen were read from the pictures by optical character recognition (OCR). "
        "Each picture was fingerprinted (SHA-256) on the capture computer and verified on arrival at the server; each monitor "
        "was re-located in every picture to correct for small camera movements.")
    t = ocr.get("totals", {})
    total = sum(t.values())
    if total:
        parts.append(
            f"Of {total:,} readings in the period, {_p(t.get('ok'), total)} were accepted automatically, "
            f"{_p(t.get('verified'), total)} were confirmed and {_p(t.get('corrected'), total)} corrected by a reviewer, "
            f"and {_p(t.get('rejected'), total)} were rejected as unreadable; only accepted, confirmed or corrected values were analysed. "
            "Readings were sent to a reviewer when the reading software was unsure, when a value was outside the plausible range, "
            "or when it jumped away from the readings just before and after it and straight back"
            + (lambda j: f" ({j:,} such jump{'' if j == 1 else 's'})." if j else ".")(sum(a["odd_jumps"] for a in accuracy)))
    spot_n = sum(a["spot_checked"] for a in accuracy)
    if spot_n:
        right = sum(a["spot_right"] for a in accuracy)
        lo, hi = wilson(right, spot_n)
        parts.append(
            f"To measure reading accuracy, {spot_n:,} automatically accepted reading{' was' if spot_n == 1 else 's were'} "
            "picked at random and checked against "
            f"the pictures by a person: {right:,} ({100 * right / spot_n:.1f}%; 95% confidence interval {lo}–{hi}%, Wilson) "
            "matched the screen.")
    refs = [(metric_label(m), r) for m, r in comparisons if r]
    if refs:
        against = (f"the reference ({'; '.join(f'{label}: {ref}' for label, ref in refs)})" if len(refs) > 1
                   else refs[0][1])
        parts.append(
            f"Values were averaged over {AVG_LABEL.get(avg_s, f'{avg_s}-second')} periods and each monitor was compared with "
            f"{against} using ordinary least-squares regression (slope, intercept, R²), root-mean-square error (RMSE, also "
            "normalised by the reference mean), mean absolute error and mean bias. Data completeness is the share of averaging "
            "periods with at least one valid value. Response delay was estimated by cross-correlating 30-second averages over "
            "shifts of up to ±10 minutes.")
    if humidity_source:
        parts.append(f"Agreement was also broken down by relative humidity (under 50%, 50–70%, 70% and above), using the "
                     f"humidity reported by {humidity_source}.")
    if events:
        names = list(dict.fromkeys(e["label"] for e in events))
        shown = ", ".join(f"“{x}”" for x in names[:4]) + (" and others" if len(names) > 4 else "")
        parts.append(f"{len(events)} event{' was' if len(events) == 1 else 's were'} recorded by the team during the test "
                     f"({shown}); they are marked on the time-series figures.")
    for c in own_checks:
        parts.append(
            f"As a check on the reading process, values read from the screen of {c['name']} were compared with the values it "
            f"reported to the cloud: {c['exact_pct']}% matched exactly (n = {c['n']}; mean absolute difference {num(c['mae'])}"
            f"{(' ' + metric_unit(c.get('metric', ''))) if c.get('metric') and metric_unit(c['metric']) else ''}).")
    return "\n\n".join(parts)


# --- shared data for the page and the report ---------------------------------------------------

def device_list(db: Session) -> list[dict]:
    cams = {c.id: c for c in db.scalars(select(Camera)).all()}
    return [{"id": d.id, "name": d.name, "model": d.model, "is_own": d.is_own, "external_id": d.external_id,
             "camera_id": d.camera_id, "camera_name": (cams[d.camera_id].name if d.camera_id in cams else None) or d.camera_id,
             "still_interval_s": cams[d.camera_id].still_interval_s if d.camera_id in cams else None}
            for d in db.scalars(select(Device).where(Device.active.is_(True)).order_by(Device.id)).all()]


def colour_order(devices: list[dict], series: list[dict]) -> dict[str, str]:
    keys = [f"dev:{d['id']}" for d in devices] + sorted(s["key"] for s in series if s["kind"] == "reference")
    return {k: PALETTE[i] if i < len(PALETTE) else OTHER for i, k in enumerate(keys)}


def metrics_in_period(db: Session, since_ms: int, until_ms: int) -> list[str]:
    cam = {m for (m,) in db.execute(select(Reading.metric).where(
        Reading.t_ms >= since_ms, Reading.t_ms <= until_ms, Reading.status.in_(GOOD_STATUSES)).distinct()).all()}
    ref = {m for (m,) in db.execute(select(ReferenceReading.metric).where(
        ReferenceReading.t_ms >= since_ms, ReferenceReading.t_ms <= until_ms).distinct()).all()}
    order = list(METRICS)
    return sorted(cam | ref, key=lambda m: (order.index(m) if m in order else 99, m))


def event_dicts(db: Session, since_ms: int, until_ms: int) -> list[dict]:
    from .routes.events import event_dict, events_between
    return [event_dict(e) for e in events_between(db, since_ms, until_ms)]


# --- figures (matplotlib) ----------------------------------------------------------------------

def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 7, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#888888", "axes.labelcolor": "#333333", "xtick.color": "#555555",
                         "ytick.color": "#555555"})
    return plt


def _png(fig) -> bytes:
    plt = _plt()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def timeseries_png(res: dict, colours: dict[str, str], events: list[dict], clock: Clock) -> bytes | None:
    series = [s for s in res["series"] if s["points"]]
    if not series:
        return None
    plt = _plt()
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(6.8, 2.7))
    bucket = res["avg_s"] * 1000
    ref_key = res["reference"]["key"]
    for s in series:
        xs, ys, prev = [], [], None
        for t, v in s["points"]:
            if prev is not None and t - prev > 3 * bucket:
                xs.append(clock.naive(prev + bucket))
                ys.append(float("nan"))  # a real gap: do not draw across it
            xs.append(clock.naive(t))
            ys.append(v)
            prev = t
        label = s["label"] + (" (reference)" if s["key"] == ref_key else "")
        ax.plot(xs, ys, color=colours.get(s["key"], OTHER), lw=1.3, ls="--" if s["kind"] == "reference" else "-",
                label=label, marker="o" if len(xs) < 40 else None, ms=2.5)
    for e in events:
        x = clock.naive(e["t_ms"])
        ax.axvline(x, color="#444444", lw=0.7, ls=(0, (4, 3)), zorder=0)
        if e["end_ms"]:
            ax.axvspan(x, clock.naive(e["end_ms"]), color="#444444", alpha=0.07, lw=0, zorder=0)
        ax.text(x, 0.99, " " + e["label"], transform=ax.get_xaxis_transform(), rotation=90, va="top", ha="right",
                fontsize=5.5, color="#333333")
    loc = mdates.AutoDateLocator(minticks=4, maxticks=9)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_ylabel(f"{metric_label(res['metric'])} ({metric_unit(res['metric'])})" if metric_unit(res["metric"])
                  else metric_label(res["metric"]))
    ax.set_xlabel(f"Time ({clock.zone})")
    ax.grid(color="#dddddd", lw=0.5)
    ax.set_ylim(bottom=min(0, ax.get_ylim()[0]))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=min(4, len(series)), frameon=False, fontsize=6.5)
    return _png(fig)


def scatter_png(res: dict, colours: dict[str, str]) -> bytes | None:
    rows = [r for r in res["rows"] if not r["is_reference"] and len(res["scatter"].get(r["key"]) or []) >= 3]
    if not rows:
        return None
    plt = _plt()
    cols = min(3, len(rows))
    nrows = math.ceil(len(rows) / cols)
    fig, axes = plt.subplots(nrows, cols, figsize=(6.8, 2.35 * nrows), squeeze=False)
    unit = metric_unit(res["metric"])
    for ax, r in zip(axes.flat, rows):
        pts = res["scatter"][r["key"]]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        lo = min(0.0, min(xs + ys))
        hi = max(xs + ys) * 1.05 or 1.0
        c = colours.get(r["key"], OTHER)
        ax.scatter(xs, ys, s=5, alpha=0.55, color=c, lw=0)
        ax.plot([lo, hi], [lo, hi], ls="--", color="#888888", lw=0.8)
        if r.get("slope") is not None:
            ax.plot([lo, hi], [r["slope"] * lo + r["intercept"], r["slope"] * hi + r["intercept"]], color=c, lw=1.2)
            title = f"{r['label']}\ny = {fixed(r['slope'])}x {'+' if r['intercept'] >= 0 else '−'} {num(abs(r['intercept']))}" \
                    f" · R² {fixed(r['r2'])} · n {r['n']}"
        else:
            title = f"{r['label']}\nn {r['n']}"
        ax.set_title(title, fontsize=6.5)
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel(f"Reference{f' ({unit})' if unit else ''}", fontsize=6.5)
        ax.set_ylabel(r["label"], fontsize=6.5)
        ax.grid(color="#eeeeee", lw=0.5)
    for ax in list(axes.flat)[len(rows):]:
        ax.axis("off")
    fig.tight_layout()
    return _png(fig)


# --- the Word document -------------------------------------------------------------------------

def _compared(res: dict) -> bool:
    """There is a reference and at least one other series paired with it."""
    return bool(res["reference"]["label"]) and any(not r["is_reference"] and r.get("n") for r in res["rows"])


def _table(doc, header: list[str], rows: list[list], numeric_from: int = 1, font_pt: float = 8):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(str(h))
        run.bold = True
        run.font.size = Pt(font_pt)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            par = cells[i].paragraphs[0]
            run = par.add_run("" if v is None else str(v))
            run.font.size = Pt(font_pt)
            if i >= numeric_from:
                par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph()
    return t


def _caption(doc, text: str) -> None:
    from docx.shared import Pt

    p = doc.add_paragraph()
    run = p.add_run(text)
    run.italic = True
    run.font.size = Pt(8)


def build_report(db: Session, settings: Settings, *, run: dict | None, since_ms: int, until_ms: int, avg_s: int,
                 metric: str | None, reference: str | None, tz_min: int, user: str) -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt

    clock = Clock(tz_min)
    devices = device_list(db)
    events = event_dicts(db, since_ms, until_ms)
    accuracy = reading_accuracy(db, since_ms, until_ms)
    metrics = metrics_in_period(db, since_ms, until_ms)
    if metric in metrics:  # the reading chosen on the page comes first
        metrics.remove(metric)
        metrics.insert(0, metric)
    results = [compute(db, m, since_ms, until_ms, avg_s, reference if m == metric else None) for m in metrics]
    used_ids = {s["device_id"] for res in results for s in res["series"] if s["kind"] == "camera"}
    used = [d for d in devices if d["id"] in used_ids]

    doc = Document()
    for sec in doc.sections:
        sec.left_margin = sec.right_margin = Inches(0.75)
        sec.top_margin = sec.bottom_margin = Inches(0.7)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)
    title = f"Side-by-side test: {run['name']}" if run else "Side-by-side test results"
    doc.core_properties.title = title
    doc.core_properties.author = user
    doc.add_heading(title, 0)
    doc.add_paragraph(
        f"Period: {clock.fmt(since_ms)} – {clock.fmt(until_ms)} ({clock.zone}), {duration(until_ms - since_ms)}. "
        f"{AVG_LABEL.get(avg_s, f'{avg_s}-second')} averages. Report generated {clock.fmt(int(datetime.now().timestamp() * 1000))} "
        f"by {user}." + (f"\nNotes: {run['notes']}" if run and run.get("notes") else ""))

    fig = 0

    # 1. Key figures
    doc.add_heading("1. Key figures", 1)
    any_line = False
    for res in results:
        ref_label = res["reference"]["label"]
        rows = [r for r in res["rows"] if not r["is_reference"] and r.get("n")]
        if not _compared(res):
            continue
        unit = metric_unit(res["metric"])
        p = doc.add_paragraph()
        p.add_run(f"{metric_label(res['metric'])} against {ref_label}").bold = True
        for r in rows:
            doc.add_paragraph(
                f"{r['label']}: slope {fixed(r.get('slope'))}, R² {fixed(r.get('r2'))}, mean bias {signed(r.get('bias'))}"
                f"{' ' + unit if unit else ''} ({signed(r.get('bias_pct'), 1)}%), RMSE {num(r.get('rmse'))}{' ' + unit if unit else ''}, "
                f"data completeness {num(r.get('completeness_pct'), 1)}%"
                + (f", reacts {abs(r['lag_s']):g} s {'later' if r['lag_s'] > 0 else 'earlier'} than the reference" if r.get("lag_s") else "")
                + ".", style="List Bullet")
            any_line = True
    if not any_line:
        doc.add_paragraph("No reference data in this period, so no agreement statistics. Import reference data on the Data page, "
                          "or choose one of the monitors as the reference on the Results page.")

    # 2. Setup
    doc.add_heading("2. Test setup", 1)
    _table(doc, ["Monitor", "Model", "Ours", "Camera", "Picture every"],
           [[d["name"], d["model"] or "", "yes" if d["is_own"] else "", d["camera_name"],
             f"{num(d['still_interval_s'], 1)} s" if d.get("still_interval_s") else ""] for d in (used or devices)], numeric_from=4)
    refs = [(metric_label(r["metric"]), r["reference"]["label"]) for r in results if _compared(r)]
    if refs:
        doc.add_paragraph("Reference: " + "; ".join(f"{m}: {ref}" for m, ref in refs) + ".")

    # 3. Events
    doc.add_heading("3. Events during the test", 1)
    if events:
        _table(doc, ["Start", "End", "What happened", "Notes", "Marked by"],
               [[clock.fmt(e["t_ms"]), clock.fmt(e["end_ms"]) if e["end_ms"] else "", e["label"], e["notes"] or "",
                 e["created_by"] or ""] for e in events], numeric_from=99)
    else:
        doc.add_paragraph("No events were marked. (Mark them on the Live page while the test runs.)")

    # 4. Results per reading
    doc.add_heading("4. Results", 1)
    for res in results:
        m = res["metric"]
        unit = metric_unit(m)
        colours = colour_order(devices, res["series"])
        doc.add_heading(f"{metric_label(m)}{f' ({unit})' if unit else ''}", 2)
        rows = [r for r in res["rows"] if not r["is_reference"]]
        if _compared(res):
            doc.add_paragraph(f"Compared with {res['reference']['label']}, {AVG_LABEL.get(avg_s, '')} averages.")
            _table(doc, ["Monitor", "Pairs", "Complete", "Mean", "Slope", "Intercept", "R²", "RMSE", "NRMSE", "MAE", "Bias", "Delay"],
                   [[r["label"], r.get("n", "–"), f"{num(r.get('completeness_pct'), 1)}%", num(r.get("mean")),
                     fixed(r.get("slope")), num(r.get("intercept")), fixed(r.get("r2")), num(r.get("rmse")),
                     f"{num(r.get('nrmse_pct'), 1)}%", num(r.get("mae")),
                     f"{signed(r.get('bias'))} ({signed(r.get('bias_pct'), 1)}%)" if r.get("bias") is not None else "–",
                     lag_text(r.get("lag_s"))] for r in rows], font_pt=7.5)
        png = timeseries_png(res, colours, events, clock)
        if png:
            doc.add_picture(io.BytesIO(png), width=Inches(6.8))
            fig += 1
            _caption(doc, f"Figure {fig}. {metric_label(m)} over the test, {AVG_LABEL.get(avg_s, '')} averages. Solid lines: read from "
                          "the monitors' screens; dashed: reference data. Dashed vertical lines and shaded bands: events.")
        png = scatter_png(res, colours) if _compared(res) else None
        if png:
            doc.add_picture(io.BytesIO(png), width=Inches(6.8))
            fig += 1
            _caption(doc, f"Figure {fig}. Each monitor against {res['reference']['label']}. Each dot is one averaging period; "
                          "dashed grey line: perfect agreement (1:1); coloured line: least-squares fit.")
        hum = res.get("humidity")
        if hum:
            doc.add_paragraph(f"By relative humidity (from {hum['source']}): mean bias against the reference, with the number of "
                              "averaging periods in brackets.")
            _table(doc, ["Monitor", *hum["bands"]],
                   [[r["label"], *[f"{signed(b['bias'])} ({signed(b['bias_pct'], 1)}%) [{b['n']}]" if b["n"] else "–"
                                   for b in r["bands"]]] for r in hum["rows"]])

    # 5. Reading quality
    doc.add_heading("5. Reading quality", 1)
    t = results[0]["ocr"]["totals"] if results else {}
    total = sum(t.values())
    if total:
        doc.add_paragraph(
            f"{total:,} readings: {_p(t.get('ok'), total)} accepted automatically, {_p(t.get('verified'), total)} confirmed by a "
            f"person, {_p(t.get('corrected'), total)} corrected, {_p(t.get('rejected'), total)} unreadable, "
            f"{_p(t.get('review'), total)} still waiting for review.")
    if accuracy:
        _table(doc, ["Monitor", "Reading", "Readings", "Spot-checked", "Accuracy (95% CI)", "Sent to review",
                     "…machine was right", "…corrected", "…unreadable", "Odd jumps caught"],
               [[a["device_name"], metric_label(a["metric"]), f"{a['readings']:,}", a["spot_checked"],
                 f"{a['accuracy_pct']}% ({a['ci_low']}–{a['ci_high']}%)" if a["spot_checked"] else "not checked",
                 a["reviewed"], a["review_machine_right"], a["review_corrected"], a["review_unreadable"], a["odd_jumps"]]
                for a in accuracy], numeric_from=2, font_pt=7.5)
        if not any(a["spot_checked"] for a in accuracy):
            doc.add_paragraph("No spot checks yet: use Review → Spot check to measure reading accuracy (about 50 per monitor and reading).")
    for res in results:
        for c in res["own_checks"]:
            doc.add_paragraph(f"{metric_label(res['metric'])}, {c['name']}: the number read off the screen matched the value the "
                              f"monitor sent to the cloud in {c['exact_pct']}% of {c['n']} comparisons (within 5%: {c['within_5pct']}%).")

    # 6. Lined-up picture
    strip_png = _strip_picture(db, settings, events, since_ms, until_ms, run)
    if strip_png:
        doc.add_heading("6. The monitors side by side", 1)
        doc.add_picture(io.BytesIO(strip_png[0]), width=Inches(6.8))
        fig += 1
        _caption(doc, f"Figure {fig}. Each monitor's screen at {clock.fmt(strip_png[1])} ({clock.zone}), cut from the stored camera "
                      "pictures and straightened, with the values read from them.")

    # 7. Methods and data
    doc.add_heading("7. Methods (draft)", 1)
    hum_src = next((r["humidity"]["source"] for r in results if r.get("humidity")), None)
    text = methods_text(run=run, since_ms=since_ms, until_ms=until_ms, avg_s=avg_s, devices=used or devices,
                        comparisons=[(r["metric"], r["reference"]["label"] if _compared(r) else None) for r in results],
                        ocr=results[0]["ocr"] if results else {},
                        accuracy=accuracy, events=events, humidity_source=hum_src,
                        own_checks=[dict(c, metric=r["metric"]) for r in results for c in r["own_checks"]], clock=clock)
    for para in text.split("\n\n"):
        doc.add_paragraph(para)
    frames = db.scalar(select(func.count()).select_from(Frame).where(Frame.captured_ms >= since_ms,
                                                                    Frame.captured_ms <= until_ms)) or 0
    doc.add_heading("Data available", 2)
    doc.add_paragraph(
        f"{frames:,} stored camera pictures, each with its SHA-256 fingerprint (Data & exports → Pictures manifest). "
        "Every reading with what the machine read, the final value, review status and reviewer (Readings CSV); the averaged "
        "data behind the statistics (Results → Averaged data CSV); reference data and events (Reference CSV, Events CSV). "
        "All decisions by reviewers are in the activity log.")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _strip_picture(db: Session, settings: Settings, events: list[dict], since_ms: int, until_ms: int,
                   run: dict | None) -> tuple[bytes, int] | None:
    """The lined-up screens at an interesting moment: a few minutes into the first event, or
    the middle of the test."""
    from .strip import gather, render_png

    moments = [min(until_ms, e["t_ms"] + 5 * 60_000) for e in events[:1]] + [(since_ms + until_ms) // 2]
    for t in moments:
        try:
            data = gather(db, t)
            if data["tiles"] and any(tile["frame_id"] for tile in data["tiles"]):
                return render_png(settings, db, data, run["name"] if run else None), t
        except (OSError, ValueError):
            continue
    return None
