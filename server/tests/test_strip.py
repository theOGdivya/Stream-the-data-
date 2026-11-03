import time

import cv2
import numpy as np

from aqserver.worker import process_pending
from render import SCENE_CORNERS, render_scene
from test_api import heartbeat, jpeg, upload


def _setup(client, agent_client, app, settings, cam="cam1", pm="37", co2="812", t=None):
    heartbeat(agent_client, camera_id=cam)
    frame, boxes = render_scene(pm, co2)
    fid = upload(agent_client, jpeg(frame), camera_id=cam, captured_ms=t).json()["id"]
    dev = client.post(f"/api/cameras/{cam}/devices",
                      json={"name": f"Monitor on {cam}", "model": "X", "corners": SCENE_CORNERS, "ref_frame_id": fid}).json()
    for metric in ("pm25", "co2"):
        client.post(f"/api/devices/{dev['id']}/fields", json={"metric": metric, "box": boxes[metric]})
    client.post(f"/api/cameras/{cam}/reprocess", json={})
    process_pending(app.state.Session, settings)
    return dev, fid


def test_heartbeat_sends_capture_regions_only_when_enabled(client, agent_client, app, settings):
    dev, _ = _setup(client, agent_client, app, settings)
    assert heartbeat(agent_client)["capture"] == {}
    r = client.patch("/api/cameras/cam1", json={"mask_enabled": True, "full_every_s": 120})
    assert r.json()["mask_enabled"] is True
    cap = heartbeat(agent_client)["capture"]["cam1"]
    assert cap["full_every_s"] == 120
    (x, y, w, h), = cap["regions"]
    xs, ys = [p[0] for p in SCENE_CORNERS], [p[1] for p in SCENE_CORNERS]
    assert x < min(xs) and y < min(ys) and x + w > max(xs) and y + h > max(ys)
    # A lost monitor makes the capture computer send full pictures until it is found again.
    from aqserver.models import Device
    with app.state.Session() as db:
        db.get(Device, dev["id"]).align_status = "lost"
        db.commit()
    assert heartbeat(agent_client)["capture"]["cam1"]["regions"] is None


def test_masked_pictures_are_read_and_marked(client, agent_client, app, settings):
    _setup(client, agent_client, app, settings)
    frame, _ = render_scene("41", "905")
    masked = np.zeros_like(frame)
    masked[80:480, 120:700] = frame[80:480, 120:700]  # what the agent stores
    data = jpeg(masked)
    import hashlib
    import json
    meta = {"camera_id": "cam1", "captured_ms": int(time.time() * 1000), "sha256": hashlib.sha256(data).hexdigest(),
            "masked": True}
    fid = agent_client.post("/api/agent/frames", data={"meta": json.dumps(meta)},
                            files={"image": ("f.jpg", data, "image/jpeg")}).json()["id"]
    process_pending(app.state.Session, settings)
    readings = {r["metric"]: r["value"] for r in client.get(f"/api/frames/{fid}").json()["readings"]}
    assert readings == {"pm25": 41, "co2": 905}
    latest_full = client.get("/api/cameras/cam1/latest-frame").json()
    assert latest_full["masked"] is False and latest_full["id"] != fid  # calibration keeps using full pictures
    assert client.get("/api/cameras/cam1/latest-frame", params={"full": False}).json()["id"] == fid


def test_strip_lines_up_monitors_from_two_cameras(client, agent_client, app, settings):
    t0 = int(time.time() * 1000) - 60_000
    d1, _ = _setup(client, agent_client, app, settings, cam="cam1", pm="37", co2="812", t=t0)
    d2, _ = _setup(client, agent_client, app, settings, cam="cam2", pm="29", co2="640", t=t0 + 400)
    later, _ = render_scene("55", "999")
    upload(agent_client, jpeg(later), camera_id="cam1", captured_ms=t0 + 10_000)
    process_pending(app.state.Session, settings)

    s = client.get("/api/strip", params={"t_ms": t0}).json()
    assert [t["device_id"] for t in s["tiles"]] == [d1["id"], d2["id"]]
    vals = [{v["metric"]: v["value"] for v in t["values"]} for t in s["tiles"]]
    assert vals == [{"pm25": 37, "co2": 812}, {"pm25": 29, "co2": 640}]
    assert s["spread_ms"] == 400

    newest = client.get("/api/strip").json()
    assert {v["metric"]: v["value"] for v in newest["tiles"][0]["values"]} == {"pm25": 55, "co2": 999}

    tile = newest["tiles"][0]
    img = client.get(f"/api/frames/{tile['frame_id']}/screen/{tile['device_id']}.jpg", params={"h": 200})
    decoded = cv2.imdecode(np.frombuffer(img.content, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[0] == 200
    png = client.get("/api/strip.png", params={"t_ms": t0})
    assert png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n"

    client.post("/api/strip/order", json={"device_ids": [d2["id"], d1["id"]]})
    assert [t["device_id"] for t in client.get("/api/strip").json()["tiles"]] == [d2["id"], d1["id"]]
    times = client.get("/api/strip/times", params={"since_ms": t0 - 1, "until_ms": t0 + 20_000}).json()
    assert times["camera_id"] == "cam2" and len(times["times"]) == 1
