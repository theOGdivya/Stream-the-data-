import time

import cv2
import numpy as np
from sqlalchemy import create_engine, inspect, select, text

from aqserver.alerts import AlertManager
from aqserver.db import add_missing_columns
from aqserver.models import Alert, Device, Frame
from aqserver.ocr.align import Aligner
from aqserver.worker import process_pending
from render import SCENE_CORNERS, render_scene
from test_api import heartbeat, jpeg, upload

BG = (60, 55, 50)


def bump(img, dx, dy, angle=0.0):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    m[0, 2] += dx
    m[1, 2] += dy
    return cv2.warpAffine(img, m, (w, h), borderValue=BG), m


def test_aligner_recovers_camera_bump():
    ref, _ = render_scene("37", "812", seed=1)
    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    al = Aligner()
    for dx, dy, angle in [(0, 0, 0), (12, -6, 0), (-35, 20, 2.0)]:
        cur, m = bump(render_scene("105", "1499", seed=2)[0], dx, dy, angle)  # different digits on screen
        res = al.align("k", lambda: ref_gray, cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), SCENE_CORNERS)
        truth = cv2.transform(np.float32(SCENE_CORNERS).reshape(-1, 1, 2), m).reshape(-1, 2)
        assert res.status == ("ok" if dx == 0 else "adjusted")
        assert np.abs(np.float32(res.corners) - truth).max() < 2.0


def test_aligner_follows_a_moved_monitor_and_flags_a_covered_one():
    ref, _ = render_scene("37", "812", seed=1)
    ref_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    cur = np.full_like(ref, BG)
    cur[100:470, 160:680] = ref[100:470, 140:660]  # someone slid the monitor 20 px to the right
    res = Aligner().align("k", lambda: ref_gray, cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), SCENE_CORNERS)
    assert res.status == "adjusted"
    assert np.abs(np.float32(res.corners) - (np.float32(SCENE_CORNERS) + [20, 0])).max() < 2.0

    covered = ref.copy()
    cv2.rectangle(covered, (120, 90), (700, 480), (205, 205, 200), -1)
    res = Aligner().align("k2", lambda: ref_gray, cv2.cvtColor(covered, cv2.COLOR_BGR2GRAY), SCENE_CORNERS)
    assert res.status == "lost"


def _calibrate(client, boxes, ref_frame_id):
    dev = client.post("/api/cameras/cam1/devices",
                      json={"name": "Brand X", "corners": SCENE_CORNERS, "ref_frame_id": ref_frame_id}).json()
    for metric, lo, hi in (("pm25", 0, 1000), ("co2", 300, 5000)):
        client.post(f"/api/devices/{dev['id']}/fields",
                    json={"metric": metric, "box": boxes[metric], "min_value": lo, "max_value": hi,
                          "recognizer": "sevenseg"})
    return dev


def _readings(client, frame_id):
    return {r["metric"]: r for r in client.get(f"/api/frames/{frame_id}").json()["readings"]}


def test_shift_is_corrected_alerted_and_can_be_accepted(client, agent_client, app, settings):
    heartbeat(agent_client)
    t0 = int(time.time() * 1000) - 120_000
    ref, boxes = render_scene("37", "812", seed=1)
    ref_id = upload(agent_client, jpeg(ref), captured_ms=t0).json()["id"]
    dev = _calibrate(client, boxes, ref_id)
    assert dev["ref_frame_id"] == ref_id

    moved, _ = bump(render_scene("41", "905", seed=2)[0], 30, 12)
    moved_id = upload(agent_client, jpeg(moved), captured_ms=t0 + 10_000).json()["id"]
    process_pending(app.state.Session, settings)

    r = _readings(client, moved_id)
    assert (r["pm25"]["value"], r["co2"]["value"]) == (41, 905)  # read correctly despite the shift
    assert r["pm25"]["status"] == "ok"
    with app.state.Session() as db:
        info = db.get(Frame, moved_id).alignment[str(dev["id"])]
        assert info["status"] == "adjusted" and 28 < info["shift_px"] < 36
        d = db.get(Device, dev["id"])
        assert d.align_status == "adjusted"
        mgr = AlertManager(settings)
        mgr.sync(db, int(time.time() * 1000))
        mgr.sync(db, int(time.time() * 1000) + 61_000)
        kinds = {a.kind for a in db.scalars(select(Alert)).all()}
    assert "view_shifted" in kinds
    ov = client.get("/api/overview").json()
    assert ov["cameras"][0]["devices"][0]["alignment"]["status"] == "adjusted"
    assert client.get(f"/api/readings/{r['pm25']['id']}/crop.png").status_code == 200

    # Somebody moved it on purpose: accept the new position.
    acc = client.post(f"/api/devices/{dev['id']}/accept-alignment").json()
    assert acc["ref_frame_id"] == moved_id and acc["alignment"]["status"] is None
    assert abs(acc["corners"][0][0] - (SCENE_CORNERS[0][0] + 30)) < 2
    again, _ = bump(render_scene("44", "910", seed=3)[0], 30, 12)
    again_id = upload(agent_client, jpeg(again), captured_ms=t0 + 20_000).json()["id"]
    process_pending(app.state.Session, settings)
    with app.state.Session() as db:
        assert db.get(Frame, again_id).alignment[str(dev["id"])]["status"] == "ok"
    assert _readings(client, again_id)["pm25"]["value"] == 44
    assert client.post(f"/api/devices/{dev['id']}/accept-alignment").status_code == 400


def test_lost_monitor_goes_to_review(client, agent_client, app, settings):
    heartbeat(agent_client)
    t0 = int(time.time() * 1000) - 60_000
    ref, boxes = render_scene("37", "812", seed=1)
    ref_id = upload(agent_client, jpeg(ref), captured_ms=t0).json()["id"]
    _calibrate(client, boxes, ref_id)
    covered = ref.copy()
    cv2.rectangle(covered, (120, 90), (700, 480), (205, 205, 200), -1)
    fid = upload(agent_client, jpeg(covered), captured_ms=t0 + 10_000).json()["id"]
    process_pending(app.state.Session, settings)
    r = _readings(client, fid)
    assert r["pm25"]["status"] == "review" and "not found" in r["pm25"]["note"]
    with app.state.Session() as db:
        mgr = AlertManager(settings)
        mgr.sync(db, int(time.time() * 1000))
        mgr.sync(db, int(time.time() * 1000) + 61_000)
        assert any(a.kind == "view_lost" for a in db.scalars(select(Alert)).all())


def test_failed_match_keeps_readings_flowing(client, agent_client, app, settings, monkeypatch):
    """Matching can fail without the camera moving (e.g. a big lighting change). If the numbers
    still read cleanly where the monitor was, they are accepted and nothing goes to review."""
    import aqserver.ocr_jobs as jobs
    from aqserver.ocr.align import AlignResult

    heartbeat(agent_client)
    t0 = int(time.time() * 1000) - 60_000
    ref, boxes = render_scene("37", "812", seed=1)
    ref_id = upload(agent_client, jpeg(ref), captured_ms=t0).json()["id"]
    dev = _calibrate(client, boxes, ref_id)
    monkeypatch.setattr(jobs.ALIGNER, "align", lambda key, load_ref, gray, corners: AlignResult("lost", corners, inliers=3))
    fid = upload(agent_client, jpeg(render_scene("41", "905", seed=2)[0]), captured_ms=t0 + 10_000).json()["id"]
    process_pending(app.state.Session, settings)
    r = _readings(client, fid)
    assert (r["pm25"]["value"], r["pm25"]["status"]) == (41, "ok")
    assert (r["co2"]["value"], r["co2"]["status"]) == (905, "ok")
    with app.state.Session() as db:
        assert db.get(Frame, fid).alignment[str(dev["id"])]["status"] == "kept"
        assert db.get(Device, dev["id"]).align_status == "kept"
        mgr = AlertManager(settings)
        mgr.sync(db, int(time.time() * 1000))
        mgr.sync(db, int(time.time() * 1000) + 61_000)
        assert not any(a.kind == "view_lost" for a in db.scalars(select(Alert)).all())
    acc = client.post(f"/api/devices/{dev['id']}/accept-alignment").json()
    assert acc["ref_frame_id"] == fid and acc["corners"] == SCENE_CORNERS


def test_monitors_marked_before_upgrade_get_a_reference(client, agent_client, app, settings):
    heartbeat(agent_client)
    frame, boxes = render_scene("37", "812")
    fid = upload(agent_client, jpeg(frame)).json()["id"]
    dev = _calibrate(client, boxes, fid)
    with app.state.Session() as db:
        db.get(Device, dev["id"]).ref_frame_id = None  # as if marked by the previous version
        db.commit()
    client.post("/api/cameras/cam1/reprocess", json={})
    process_pending(app.state.Session, settings)
    with app.state.Session() as db:
        assert db.get(Device, dev["id"]).ref_frame_id == fid


def test_old_database_gets_new_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE cameras (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255))"))
    added = add_missing_columns(engine)
    assert "cameras.mask_enabled" in added and "cameras.full_every_s" in added
    cols = {c["name"] for c in inspect(engine).get_columns("cameras")}
    assert {"mask_enabled", "full_every_s", "agent_id"} <= cols
