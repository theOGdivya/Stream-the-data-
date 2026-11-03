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
