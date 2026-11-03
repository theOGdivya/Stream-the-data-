import csv
import hashlib
import io
import json
import time

import cv2
from fastapi.testclient import TestClient
from sqlalchemy import select

from aqserver.alerts import AlertManager
from aqserver.models import Alert
from aqserver.worker import process_pending
from render import SCENE_CORNERS, render_scene


def jpeg(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()


def heartbeat(agent_client, camera_id="cam1", connected=True, **extra):
    body = {
        "agent_id": "laptop-1", "hostname": "lab-pc", "version": "test", "agent_time_ms": int(time.time() * 1000),
        "backlog": 0, "disk_free_bytes": 50 * 1024**3,
        "cameras": [{"id": camera_id, "name": "Bench 1", "connected": connected, "brightness": 120.0,
                     "still_interval_s": 10, **extra}],
    }
    r = agent_client.post("/api/agent/heartbeat", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def upload(agent_client, img_bytes, camera_id="cam1", captured_ms=None, sha=None):
    meta = {"camera_id": camera_id, "captured_ms": captured_ms or int(time.time() * 1000),
            "sha256": sha or hashlib.sha256(img_bytes).hexdigest(), "width": 1280, "height": 720}
    return agent_client.post("/api/agent/frames", data={"meta": json.dumps(meta)},
                             files={"image": ("f.jpg", img_bytes, "image/jpeg")})


def calibrate(client, boxes, camera_id="cam1"):
    r = client.post(f"/api/cameras/{camera_id}/devices",
                    json={"name": "Brand X", "model": "X-100", "corners": SCENE_CORNERS})
    assert r.status_code == 200, r.text
    dev = r.json()
    for metric, lo, hi in (("pm25", 0, 1000), ("co2", 300, 5000)):
        r = client.post(f"/api/devices/{dev['id']}/fields",
                        json={"metric": metric, "box": boxes[metric], "unit": "x", "min_value": lo, "max_value": hi,
                              "recognizer": "sevenseg"})
        assert r.status_code == 200, r.text
    return dev


def test_auth_required(app):
    with TestClient(app) as anon:
        assert anon.get("/healthz").status_code == 200
        r = anon.get("/api/overview")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        assert anon.get("/").status_code == 401
        assert anon.post("/api/agent/heartbeat", json={}).status_code == 401
        assert anon.post("/api/agent/heartbeat", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    with TestClient(app, headers={"Authorization": "Basic dGVzdGVyOndyb25n"}) as bad:
        assert bad.get("/api/overview").status_code == 401


def test_upload_verifies_hash_and_dedupes(agent_client, settings):
    data = jpeg(render_scene("12", "600")[0])
    assert upload(agent_client, data, sha="0" * 64).status_code == 400
    r1 = upload(agent_client, data)
    assert r1.status_code == 200 and r1.json()["duplicate"] is False
    r2 = upload(agent_client, data)  # agent retry after a lost response
    assert r2.json() == {"id": r1.json()["id"], "duplicate": True}
    files = list((settings.data_dir / "frames" / "cam1").rglob("*.jpg"))
    assert len(files) == 1 and files[0].read_bytes() == data


def test_live_view_requests_live_frames(client, agent_client):
    assert heartbeat(agent_client)["live"] == []
    data = jpeg(render_scene("12", "600")[0])
    upload(agent_client, data)
    r = client.get("/api/cameras/cam1/live.jpg")  # nobody streaming yet: newest still
    assert r.status_code == 200 and r.headers["x-kind"] == "still"
    assert heartbeat(agent_client)["live"] == ["cam1"]  # viewer present -> agent asked to stream
    r = agent_client.post("/api/agent/live/cam1", content=b"\xff\xd8live", headers={"X-Captured-Ms": "123"})
    assert r.json() == {"watching": True}
    r = client.get("/api/cameras/cam1/live.jpg")
    assert r.headers["x-kind"] == "live" and r.content == b"\xff\xd8live"


def test_end_to_end_ocr_review_export(client, agent_client, app, settings):
    heartbeat(agent_client)
    t0 = int(time.time() * 1000) - 60_000
    frame, boxes = render_scene("37", "812")
    upload(agent_client, jpeg(frame), captured_ms=t0)
    Session = app.state.Session

    # Not calibrated yet: frame is skipped, then reprocessed after calibration.
    assert process_pending(Session, settings) == 1
    dev = calibrate(client, boxes)
    test = client.post("/api/ocr/test", json={"camera_id": "cam1"}).json()
    assert {r["metric"]: r["value"] for r in test["results"]} == {"pm25": 37, "co2": 812}
    assert test["results"][0]["crop"].startswith("data:image/png;base64,")
    assert client.post("/api/cameras/cam1/reprocess", json={}).json() == {"frames_queued": 1}

    frame2, _ = render_scene("41", "8123")  # CO2 8123 is above the plausible range (5000) -> review
    upload(agent_client, jpeg(frame2), captured_ms=t0 + 10_000)
    process_pending(Session, settings)

    ov = client.get("/api/overview").json()
    fields = {f["metric"]: f for f in ov["cameras"][0]["devices"][0]["fields"]}
    assert fields["pm25"]["latest"]["value"] == 41
    assert fields["co2"]["latest"]["status"] == "review"
    assert ov["review_pending"] == 1

    q = client.get("/api/review").json()
    assert q["total"] == 1
    item = q["items"][0]
    assert client.get(f"/api/readings/{item['id']}/crop.png").headers["content-type"] == "image/png"
    r = client.post(f"/api/readings/{item['id']}/review", json={"action": "correct", "value": 1234})
    assert r.json()["status"] == "corrected"

    s = client.get("/api/series", params={"metric": "co2", "since_ms": t0 - 1000}).json()
    pts = s["series"][0]["points"]
    assert [p[1] for p in pts] == [812, 1234]

    csv_text = client.get("/api/export/readings.csv").text
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 4
    corrected = next(r for r in rows if r["status"] == "corrected")
    assert corrected["value"] == "1234.0" and corrected["ocr_value"] == "8123.0" and corrected["reviewed_by"] == "tester"
    assert len(corrected["frame_sha256"]) == 64

    manifest = list(csv.DictReader(io.StringIO(client.get("/api/export/frames.csv").text)))
    assert len(manifest) == 2
    audit = client.get("/api/audit").json()
    assert any(a["action"] == "reading.review" and a["actor"] == "tester" for a in audit)

    # Reprocessing keeps the human decision.
    client.post("/api/cameras/cam1/reprocess", json={})
    process_pending(Session, settings)
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/readings.csv").text)))
    assert sum(r["status"] == "corrected" for r in rows) == 1 and len(rows) == 4

    stats = client.get("/api/review/stats").json()
    assert stats["totals"]["corrected"] == 1


def test_reference_import_and_push(client, agent_client):
    t = "2026-11-03 10:00:00"
    csv_body = f"timestamp,device,pm2.5 (ug/m3),co2\n{t},AQ-1,12,640\n2026-11-03 10:01:00,AQ-1,13,\n"
    r = client.post("/api/reference/import", data={"source": "own-cloud", "tz": "+05:30"},
                    files={"file": ("x.csv", csv_body, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json() == {"rows": 3, "added": 3}
    # same file again: nothing new
    r = client.post("/api/reference/import", data={"source": "own-cloud", "tz": "+05:30"},
                    files={"file": ("x.csv", csv_body, "text/csv")})
    assert r.json()["added"] == 0
    r = agent_client.post("/api/agent/reference", json={"source": "ref-instrument", "rows": [
        {"device": "BAM", "metric": "PM2.5", "t": "2026-11-03T04:30:00Z", "value": 11.5}]})
    assert r.json() == {"received": 1, "added": 1}
    since = 1793600000000  # well before
    s = client.get("/api/series", params={"metric": "pm25", "since_ms": since, "until_ms": 1893456000000}).json()
    by_label = {x["label"]: x for x in s["series"]}
    # 10:00 at +05:30 == 04:30Z
    assert by_label["AQ-1 (own-cloud)"]["points"][0][0] <= 1793939400000
    assert by_label["BAM (ref-instrument)"]["points"][0][1] == 11.5
    metrics = [m["metric"] for m in client.get("/api/metrics").json()]
    assert metrics == ["pm25", "co2"]


def test_runs_and_alerts(client, agent_client, app, settings):
    heartbeat(agent_client)
    run = client.post("/api/runs", json={"name": "Office 7-day", "notes": "window closed"}).json()
    assert client.get("/api/overview").json()["run"]["id"] == run["id"]
    run2 = client.post("/api/runs", json={"name": "Second"}).json()
    runs = {r["id"]: r for r in client.get("/api/runs").json()}
    assert runs[run["id"]]["ended_ms"] is not None and runs[run2["id"]]["ended_ms"] is None

    # A run is active and the camera has delivered no stills: alert. Camera also reports dark.
    heartbeat(agent_client, brightness=5.0)
    mgr = AlertManager(settings)
    with app.state.Session() as db:
        now = int(time.time() * 1000)
        assert mgr.sync(db, now) == []  # a problem has to last a minute before it alerts
        sent = mgr.sync(db, now + 61_000)
        kinds = {a.kind for a in db.scalars(select(Alert)).all()}
    assert {"no_frames", "dark"} <= kinds and len(sent) >= 2
    ov = client.get("/api/overview").json()
    assert len(ov["alerts"]) >= 2

    heartbeat(agent_client, brightness=150.0)
    upload(agent_client, jpeg(render_scene("1", "500")[0]))
    with app.state.Session() as db:
        now = int(time.time() * 1000)
        mgr.sync(db, now)  # condition cleared, but alerts wait 2 minutes before resolving
        assert db.scalars(select(Alert).where(Alert.resolved_ms.is_(None))).all()
        mgr.sync(db, now + 130_000)
        still_open = {a.kind for a in db.scalars(select(Alert).where(Alert.resolved_ms.is_(None))).all()}
    assert "dark" not in still_open


def test_calibration_validation(client, agent_client):
    heartbeat(agent_client)
    bad = client.post("/api/cameras/cam1/devices", json={"name": "x", "corners": [[0, 0], [1, 1]]})
    assert bad.status_code == 422
    dev = client.post("/api/cameras/cam1/devices", json={"name": "x", "corners": SCENE_CORNERS}).json()
    r = client.post(f"/api/devices/{dev['id']}/fields", json={"metric": "pm25", "box": [0.9, 0.1, 0.5, 0.1]})
    assert r.status_code == 422
    r = client.post(f"/api/devices/{dev['id']}/fields",
                    json={"metric": "pm25", "box": [0.1, 0.1, 0.5, 0.1], "options": {"polarity": "sideways"}})
    assert r.status_code == 422
    f = client.post(f"/api/devices/{dev['id']}/fields",
                    json={"metric": "temp", "box": [0.1, 0.1, 0.5, 0.1], "options": {"decimals": 1, "slant": 8}}).json()
    r = client.patch(f"/api/fields/{f['id']}", json={"options": {"decimals": "auto"}})
    assert r.json()["options"]["decimals"] == "auto"
    assert client.delete(f"/api/devices/{dev['id']}").json() == {"ok": True}
    assert client.get("/api/cameras").json()[0]["devices"] == []


def test_export_neutralises_spreadsheet_formulas(client, agent_client, app, settings):
    heartbeat(agent_client)
    frame, boxes = render_scene("37", "812")
    upload(agent_client, jpeg(frame))
    r = client.post("/api/cameras/cam1/devices", json={"name": "=HYPERLINK(\"x\")", "corners": SCENE_CORNERS})
    client.post(f"/api/devices/{r.json()['id']}/fields", json={"metric": "pm25", "box": boxes["pm25"]})
    process_pending(app.state.Session, settings)
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/readings.csv").text)))
    assert rows[0]["device_name"].startswith("'=")
