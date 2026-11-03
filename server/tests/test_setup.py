import base64

import cv2
import numpy as np

from test_api import heartbeat


def _jpeg_b64():
    ok, buf = cv2.imencode(".jpg", np.full((90, 160, 3), 120, np.uint8))
    return base64.b64encode(buf.tobytes()).decode()


def test_scan_request_and_detected_cameras(client, agent_client):
    assert heartbeat(agent_client)["probe"] is False
    assert client.post("/api/agents/laptop-1/scan").json() == {"ok": True}
    assert heartbeat(agent_client)["probe"] is True  # the next heartbeat asks the computer to scan
    assert client.get("/api/setup").json()["agents"][0]["scanning"] is True

    r = agent_client.post("/api/agent/detected", json={"agent_id": "laptop-1", "probed_ms": 123, "cameras": [
        {"index": 0, "name": "Integrated Webcam", "name_certain": True, "width": 1280, "height": 720, "preview": _jpeg_b64()},
        {"index": 1, "name": None, "width": 640, "height": 480},
    ]})
    assert r.json() == {"ok": True}
    assert heartbeat(agent_client)["probe"] is False
    agent = client.get("/api/setup").json()["agents"][0]
    assert agent["scanning"] is False
    found = agent["detected"]["cameras"]
    assert [(c["index"], c["name"], c["has_preview"]) for c in found] == [(0, "Integrated Webcam", True), (1, None, False)]
    img = client.get("/api/agents/laptop-1/detected/0.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get("/api/agents/laptop-1/detected/1.jpg").status_code == 404


def test_choose_stop_and_restart_a_camera(client, agent_client):
    heartbeat(agent_client)
    cam = client.post("/api/agents/laptop-1/cameras", json={"source": 0, "name": "Desk webcam", "still_interval_s": 15}).json()
    assert cam["id"] == "desk-webcam" and cam["managed"] is True
    sent = heartbeat(agent_client)["cameras"]
    assert sent == [{"id": "desk-webcam", "name": "Desk webcam", "source": 0, "still_interval_s": 15.0}]

    # Choosing the same webcam again re-uses its entry (and history).
    again = client.post("/api/agents/laptop-1/cameras", json={"source": "0", "name": "Bench A"}).json()
    assert again["id"] == "desk-webcam" and again["name"] == "Bench A"
    fake = client.post("/api/agents/laptop-1/cameras", json={"source": "synthetic", "name": "Desk webcam"}).json()
    assert fake["id"] == "desk-webcam-2"  # a different camera with a clashing name gets its own id

    client.post("/api/cameras/desk-webcam/stop")
    assert [c["id"] for c in heartbeat(agent_client)["cameras"]] == ["desk-webcam-2"]
    cams = {c["id"]: c for c in client.get("/api/overview").json()["cameras"]}
    assert cams["desk-webcam"]["stopped"] is True and cams["desk-webcam-2"]["stopped"] is False
    client.post("/api/cameras/desk-webcam/start")
    assert {c["id"] for c in heartbeat(agent_client)["cameras"]} == {"desk-webcam", "desk-webcam-2"}

    setup = client.get("/api/setup").json()["agents"][0]["cameras"]
    by_id = {c["id"]: c for c in setup}
    assert by_id["desk-webcam"]["source"] == 0 and by_id["desk-webcam-2"]["source"] == "synthetic"
    assert by_id["cam1"]["managed"] is False  # from the config file (reported by heartbeat)


def test_camera_choice_validation(client, agent_client):
    heartbeat(agent_client)
    assert client.post("/api/agents/laptop-1/cameras", json={"source": "C:/video.mp4", "name": "x"}).status_code == 422
    assert client.post("/api/agents/laptop-1/cameras", json={"source": -1, "name": "x"}).status_code == 422
    assert client.post("/api/agents/nobody/cameras", json={"source": 0, "name": "x"}).status_code == 404
    assert client.post("/api/agents/laptop-1/cameras",
                       json={"source": "rtsp://10.0.0.5/stream", "name": "IP cam"}).status_code == 200
    assert client.post("/api/cameras/cam1/stop").status_code == 400  # config-file cameras are managed there


def test_browser_capture_uses_dashboard_login(app, client):
    """A browser tab records through /api/capture/* with the dashboard login, not the agent token."""
    import hashlib
    import json
    import time

    from fastapi.testclient import TestClient

    with TestClient(app) as anon:
        assert anon.post("/api/capture/heartbeat", json={}).status_code == 401
    hb = {"agent_id": "browser-office", "hostname": "Chrome on Windows", "version": "browser",
          "agent_time_ms": int(time.time() * 1000), "cameras": [{"id": "web-desk-cam", "name": "Desk cam",
          "connected": True, "still_interval_s": 10, "source": "browser"}]}
    assert client.post("/api/capture/heartbeat", json=hb).json()["live"] == []
    ok, buf = cv2.imencode(".jpg", np.full((90, 160, 3), 120, np.uint8))
    data = buf.tobytes()
    meta = {"camera_id": "web-desk-cam", "captured_ms": int(time.time() * 1000), "sha256": hashlib.sha256(data).hexdigest()}
    r = client.post("/api/capture/frames", data={"meta": json.dumps(meta)}, files={"image": ("s.jpg", data, "image/jpeg")})
    assert r.json()["duplicate"] is False
    client.get("/api/cameras/web-desk-cam/live.jpg")  # someone opens the live view
    assert client.post("/api/capture/heartbeat", json=hb).json()["live"] == ["web-desk-cam"]
    assert client.post("/api/capture/live/web-desk-cam", content=data, headers={"X-Captured-Ms": "1"}).json() == {"watching": True}
    agent = client.get("/api/setup").json()["agents"][0]
    assert agent["browser"] is True and agent["cameras"][0]["source"] == "browser"
