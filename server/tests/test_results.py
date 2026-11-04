import csv
import io
import math

import numpy as np
import pytest

from aqserver.models import Camera, Device, Field, Frame, Reading, ReferenceReading, TestRun
from aqserver.results import estimate_lag, pair_stats

T0 = 1_793_000_000_000  # a fixed start time (ms)
STEP = 60_000


def test_pair_stats_known_values():
    x = np.array([10.0, 20, 30, 40, 50])
    s = pair_stats(x, 2 * x + 1)
    assert (s["slope"], s["intercept"], s["r2"]) == (2.0, 1.0, 1.0)
    assert s["bias"] == pytest.approx(31.0) and s["bias_pct"] == pytest.approx(103.3)
    assert s["mae"] == pytest.approx(31.0)
    assert s["rmse"] == pytest.approx(math.sqrt(np.mean((x + 1) ** 2)), rel=1e-3)
    assert pair_stats(np.array([]), np.array([]))["n"] == 0


def _event(t):  # background plus a pollution event (e.g. incense) around minute 90
    return 15 + 3 * math.sin(t / 900_000) + 80 * math.exp(-(((t - T0) / 60_000 - 90) / 8) ** 2)


def test_lag_estimate_finds_a_two_minute_delay():
    ref = [[T0 + i * 30_000, _event(T0 + i * 30_000)] for i in range(360)]
    late = [[t, _event(t - 120_000)] for t, _ in ref]
    lag, r = estimate_lag(ref, late, 30_000)
    assert lag == 120 and r > 0.99
    flat = [[t, 5.0] for t, _ in ref]
    assert estimate_lag(ref, flat, 30_000)[0] is None  # no variation: no claim
    short = ref[:2]  # shorter than the largest shift tried
    assert estimate_lag(short, short, 30_000) == (None, None)


@pytest.fixture()
def dataset(app):
    """3 hours, one reading per minute: a reference instrument, our monitor (1.1x + 2, also
    sent to our cloud) and a competitor that reacts 2 minutes late."""
    with app.state.Session() as db:
        db.add(Camera(id="cam1", status={}, created_ms=T0))
        db.flush()
        ours = Device(camera_id="cam1", name="Ours", model="AQ-1", is_own=True, external_id="AQ-0042",
                      corners=[[0, 0], [1, 0], [1, 1], [0, 1]], warp_w=600, warp_h=400)
        rival = Device(camera_id="cam1", name="Brand X", model="X-100", corners=[[0, 0], [1, 0], [1, 1], [0, 1]],
                       warp_w=600, warp_h=400)
        db.add_all([ours, rival])
        db.flush()
        f_ours = Field(device_id=ours.id, metric="pm25", box=[0, 0, 1, 1], options={})
        f_rival = Field(device_id=rival.id, metric="pm25", box=[0, 0, 1, 1], options={})
        db.add_all([f_ours, f_rival])
        db.flush()
        for i in range(180):
            t = T0 + i * STEP
            frame = Frame(camera_id="cam1", captured_ms=t, received_ms=t, path="x.jpg", sha256=f"{i:064d}", size_bytes=1,
                          ocr_status="done")
            db.add(frame)
            db.flush()
            ref = _event(t)
            v_ours = round(1.1 * ref + 2)
            v_rival = round(_event(t - 120_000))
            db.add(Reading(frame_id=frame.id, field_id=f_ours.id, device_id=ours.id, metric="pm25", t_ms=t,
                           raw_text=str(v_ours), value=v_ours, final_value=v_ours, confidence=1, status="ok"))
            db.add(Reading(frame_id=frame.id, field_id=f_rival.id, device_id=rival.id, metric="pm25", t_ms=t,
                           raw_text=str(v_rival), value=v_rival, final_value=v_rival, confidence=1, status="ok"))
            db.add(ReferenceReading(source="ref-instrument", device_ref="BAM", metric="pm25", t_ms=t, value=ref))
            # Our cloud: same as the screen, except every 10th value differs (timing, rounding)
            db.add(ReferenceReading(source="own-cloud", device_ref="AQ-0042", metric="pm25", t_ms=t + 5_000,
                                    value=v_ours + (3 if i % 10 == 0 else 0)))
        run = TestRun(name="Lab test", started_ms=T0, ended_ms=T0 + 180 * STEP)
        db.add(run)
        db.commit()
        return {"ours": ours.id, "rival": rival.id, "run": run.id}


def test_results_against_reference(client, dataset):
    res = client.get("/api/results", params={"run_id": dataset["run"], "metric": "pm25", "avg_s": 300,
                                             "reference": "ref:ref-instrument:BAM"}).json()
    assert res["reference"]["label"] == "BAM (ref-instrument)" and res["run"]["name"] == "Lab test"
    rows = {r["label"]: r for r in res["rows"]}
    ours, rival = rows["Ours"], rows["Brand X"]
    assert ours["n"] == 37 and ours["completeness_pct"] == 100.0  # 180 min touch 37 five-minute buckets
    assert ours["slope"] == pytest.approx(1.1, abs=0.02) and ours["intercept"] == pytest.approx(2, abs=0.6)
    assert ours["r2"] > 0.99
    assert ours["lag_s"] in (0, None) and rival["lag_s"] == 120
    assert rows["BAM (ref-instrument)"]["is_reference"] is True
    assert len(res["scatter"][f"dev:{dataset['ours']}"]) == 37

    check, = res["own_checks"]
    assert check["external_id"] == "AQ-0042" and check["n"] == 180 and check["exact_pct"] == 90.0
    assert res["ocr"]["totals"] == {"ok": 360}


def test_results_defaults_and_csv(client, dataset):
    res = client.get("/api/results", params={"run_id": dataset["run"]}).json()
    assert res["metric"] == "pm25" and res["avg_s"] == 3600
    # Independent reference data is preferred over our own monitor's cloud data
    assert res["reference"]["key"] == "ref:ref-instrument:BAM"
    stats = list(csv.reader(io.StringIO(client.get("/api/results/stats.csv", params={
        "run_id": dataset["run"], "avg_s": 300, "reference": "ref:ref-instrument:BAM"}).text)))
    assert stats[1][:4] == ["label", "model", "is_reference", "n"]
    assert {r[0] for r in stats[2:]} >= {"Ours", "Brand X"}
    pairs = list(csv.reader(io.StringIO(client.get("/api/results/pairs.csv", params={
        "run_id": dataset["run"], "avg_s": 300, "reference": "ref:ref-instrument:BAM"}).text)))
    assert pairs[0][1] == "BAM (ref-instrument) (reference)" and len(pairs) == 38
    assert client.get("/api/results", params={"run_id": dataset["run"], "avg_s": 7}).status_code == 400
    assert client.get("/api/results").status_code == 400


def test_humidity_breakdown(client, app, dataset):
    res = client.get("/api/results", params={"run_id": dataset["run"], "avg_s": 300}).json()
    assert res["humidity"] is None  # no humidity data yet
    with app.state.Session() as db:
        for i in range(180):  # dry for the first hour, humid after
            t = T0 + i * STEP
            db.add(ReferenceReading(source="ref-instrument", device_ref="BAM", metric="rh", t_ms=t,
                                    value=40.0 if i < 60 else 80.0))
        db.commit()
    res = client.get("/api/results", params={"run_id": dataset["run"], "avg_s": 300,
                                             "reference": "ref:ref-instrument:BAM"}).json()
    hum = res["humidity"]
    assert hum["source"] == "BAM (ref-instrument), the reference"
    assert hum["bands"] == ["under 50%", "50–70%", "70% and above"]
    ours = next(r for r in hum["rows"] if r["label"] == "Ours")
    # The 5-minute period that spans the change averages ~56% and lands in the middle band.
    assert [b["n"] for b in ours["bands"]] == [12, 1, 24] and ours["without_rh"] == 0
    assert ours["bands"][0]["bias"] is not None and ours["bands"][2]["mae"] is not None
    stats = client.get("/api/results/stats.csv", params={"run_id": dataset["run"], "avg_s": 300}).text
    assert "humidity_breakdown" in stats and "Ours,70% and above,24," in stats
    # Humidity itself gets no humidity breakdown.
    assert client.get("/api/results", params={"run_id": dataset["run"], "metric": "rh"}).json()["humidity"] is None


def test_word_report(client, app, dataset):
    import docx

    with app.state.Session() as db:
        for i in range(180):
            db.add(ReferenceReading(source="ref-instrument", device_ref="BAM", metric="rh", t_ms=T0 + i * STEP,
                                    value=40.0 if i < 60 else 80.0))
        db.commit()
    client.post("/api/events", json={"label": "Incense lit", "t_ms": T0 + 85 * STEP, "end_ms": T0 + 95 * STEP})
    client.post("/api/events", json={"label": "Window opened", "t_ms": T0 + 120 * STEP})
    res = client.get("/api/results", params={"run_id": dataset["run"], "avg_s": 300, "tz": 330}).json()
    assert [e["label"] for e in res["events"]] == ["Incense lit", "Window opened"]
    assert "UTC+05:30" in res["methods"] and "2 events were recorded" in res["methods"]
    assert "relative humidity" in res["methods"]

    r = client.get("/api/results/report.docx", params={"run_id": dataset["run"], "avg_s": 300, "tz": 330})
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="report-lab-test.docx"'
    doc = docx.Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert doc.core_properties.author == "tester"
    for heading in ("1. Key figures", "2. Test setup", "3. Events during the test", "4. Results", "5. Reading quality",
                    "7. Methods (draft)"):
        assert heading in text
    import re
    m = re.search(r"Ours: slope (\d\.\d{3}), R² (\d\.\d{3})", text)
    assert m and abs(float(m.group(1)) - 1.1) < 0.02 and float(m.group(2)) > 0.99 and "UTC+05:30" in text
    tables = ["\n".join("|".join(c.text for c in row.cells) for row in t.rows) for t in doc.tables]
    assert any("Incense lit" in t for t in tables)
    assert any("70% and above" in t for t in tables)  # humidity breakdown
    assert any("Odd jumps caught" in t for t in tables)
    assert len(doc.inline_shapes) >= 2  # time series and scatter figures
