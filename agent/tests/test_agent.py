"""Agent tests: spool, config, synthetic camera, and a real upload/heartbeat round trip
against the server app (served in-process with uvicorn on a free port)."""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))

from aqagent.camera import CameraWorker  # noqa: E402
from aqagent.config import load_config  # noqa: E402
from aqagent.net import Heartbeat, LiveSender, LiveState, Uploader  # noqa: E402
from aqagent.spool import Spool  # noqa: E402
from aqagent.synthetic import SyntheticSource  # noqa: E402


def test_spool_roundtrip(tmp_path):
    sp = Spool(tmp_path, keep_sent_days=1)
    sp.put("cam1", 2000, b"jpeg-2", {"camera_id": "cam1"})
    sp.put("cam1", 1000, b"jpeg-1", {"camera_id": "cam1"})
    items = sp.pending_items()
    assert [p.name for p in items] == ["0000000001000_cam1.json", "0000000002000_cam1.json"]
    assert sp.backlog() == 2
    sp.mark_sent(items[0])
    sp.mark_rejected(items[1], "bad")
    assert sp.backlog() == 0
    assert len(list((tmp_path / "sent").rglob("*.jpg"))) == 1
    assert (tmp_path / "rejected" / "0000000002000_cam1.reason.txt").read_text() == "bad"


def test_config_validation(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("server_url: https://x.example\nagent_token: t\ncameras:\n  - id: 'bad id'\n")
    with pytest.raises(ValueError, match="id may only"):
        load_config(cfg)
    cfg.write_text("server_url: x.example\nagent_token: t\ncameras: [{id: a}]\n")
    with pytest.raises(ValueError, match="https"):
        load_config(cfg)
    cfg.write_text("server_url: https://x.example/\nagent_token: t\nagent_id: lab 1\ncameras: [{id: a, source: synthetic}]\n")
    c = load_config(cfg)
    assert c.server_url == "https://x.example" and c.agent_id == "lab-1" and c.spool_dir == tmp_path / "spool"


def test_synthetic_source_renders():
    src = SyntheticSource(fps=100, seed=3)
    ok, frame = src.read()
    assert ok and frame.shape == (720, 1280, 3)
    assert set(src.values) == {"lcd_pm25", "lcd_co2", "tft_pm25", "tft_temp"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_server(tmp_path):
    uvicorn = pytest.importorskip("uvicorn")
    sys.path.insert(0, str(ROOT / "server"))
    from aqserver.config import Settings
    from aqserver.main import create_app

    settings = Settings(data_dir=tmp_path / "server", database_url=f"sqlite:///{tmp_path / 'server.db'}",
                        agent_token="agent-token-for-tests-1", dashboard_users={"u": "p"})
    settings.data_dir.mkdir()
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    t.join(timeout=5)


def test_agent_uploads_and_streams_live(tmp_path, live_server):
    url, app = live_server
    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(
        f"server_url: {url}\nagent_token: agent-token-for-tests-1\nagent_id: test-agent\nprevent_sleep: false\n"
        "heartbeat_s: 0.5\ncameras:\n  - id: cam-a\n    source: synthetic\n    still_interval_s: 1\n    live_fps: 5\n"
    )
    cfg = load_config(cfg_path)
    stop = threading.Event()
    spool = Spool(cfg.spool_dir, cfg.keep_sent_days)
    live = LiveState()
    sender = LiveSender(cfg, live, stop)
    cams = [CameraWorker(c, spool, live.is_live, sender.submit, stop) for c in cfg.cameras]
    threads = [*cams, sender, Uploader(cfg, spool, stop), Heartbeat(cfg, spool, lambda: [c.status() for c in cams], live, stop)]
    for t in threads:
        t.start()
    try:
        app.state.live.touch("cam-a")  # a dashboard viewer opens the camera
        deadline = time.time() + 15
        frames = 0
        while time.time() < deadline:
            from aqserver.models import Frame
            from sqlalchemy import func, select

            with app.state.Session() as db:
                frames = db.scalar(select(func.count()).select_from(Frame))
            app.state.live.touch("cam-a")
            if frames >= 2 and app.state.live.get("cam-a", 5) is not None:
                break
            time.sleep(0.3)
        assert frames >= 2, "stills were not uploaded"
        assert app.state.live.get("cam-a", 5) is not None, "live frames were not streamed"
        assert spool.backlog() == 0
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
