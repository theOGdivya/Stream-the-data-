#!/usr/bin/env python3
"""Try the whole platform on this computer with one command (or double-click start-demo).

    python demo.py               opens the Cameras page: pick your webcam from the dropdown
                                 (or the fake camera, if you have no webcam)
    python demo.py --webcam 0    skip the picker and record webcam 0 straight away
    python demo.py --bump        fake camera that gets knocked after 2 minutes

It installs what it needs into ./demo-data (first run takes a few minutes), starts the
server, the OCR worker and the capture program, and opens http://localhost:8000
(login: demo / demo). Press Ctrl+C to stop. Your demo data is kept for next time;
delete the demo-data folder to start fresh.

Needs Python 3.11 or newer. This is for trying things out only; for real tests deploy
the server as described in docs/deploy.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "demo-data"
VENV = DEMO / ".venv"
TOKEN = "demo-agent-token-not-a-secret"
USER, PASSWORD = "demo", "demo"
CAMERA_ID = "demo-cam"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_environment() -> Path:
    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11 or newer is needed (this is {sys.version.split()[0]}). Get it from python.org.")
    DEMO.mkdir(exist_ok=True)
    py = venv_python()
    if not py.exists():
        print("Creating a private Python environment in demo-data/.venv ...")
        venv.create(VENV, with_pip=True)
    reqs = [ROOT / "server" / "requirements.txt", ROOT / "agent" / "requirements.txt"]
    stamp = "".join(r.read_text(encoding="utf-8") for r in reqs)
    marker = DEMO / ".installed"
    if not marker.exists() or marker.read_text(encoding="utf-8") != stamp:
        print("Installing packages (first run only; a few minutes) ...")
        subprocess.check_call([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                               "-r", str(reqs[0]), "-r", str(reqs[1])])
        marker.write_text(stamp, encoding="utf-8")
    return py


def api(port: int, path: str, body: dict | None = None, method: str | None = None):
    auth = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method=method or ("GET" if body is None else "POST"),
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def wait_for(check, seconds: float, what: str) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if check():
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {what}; see the logs in demo-data/")


def write_agent_config(source, port: int) -> Path:
    """With ``source`` None, no camera is set here: you pick one on the Cameras page."""
    cfg = DEMO / "agent.yaml"
    text = (f"server_url: http://127.0.0.1:{port}\n"
            f"agent_token: {TOKEN}\n"
            "agent_id: this-computer\n"
            "spool_dir: ./spool\n"
            "keep_sent_days: 1\n"
            "prevent_sleep: false\n")
    if source is None:
        text += "cameras: []\n"
    else:
        name = "Fake monitors (demo)" if str(source).startswith("synthetic") else f"Webcam {source}"
        text += ("cameras:\n"
                 f"  - id: {CAMERA_ID}\n"
                 f"    name: {json.dumps(name)}\n"
                 f"    source: {json.dumps(source)}\n"
                 "    still_interval_s: 5\n"
                 "    live_fps: 2\n")
    cfg.write_text(text, encoding="utf-8")
    return cfg


def preset_fake_lcd(py: Path, port: int, camera_id: str = CAMERA_ID) -> None:
    """Mark the fake LCD monitor so readings appear straight away. The second fake
    monitor is left for you to mark yourself on the Calibrate page."""
    cams = api(port, "/api/cameras")
    if any(d for c in cams if c["id"] == camera_id for d in c["devices"]):
        return  # already calibrated in an earlier demo session
    layout = json.loads(subprocess.check_output(
        [str(py), "-c", "import json; from aqagent.synthetic import LAYOUT; print(json.dumps(LAYOUT))"],
        cwd=ROOT / "agent"))
    lcd = layout["Synthetic LCD"]
    dev = api(port, f"/api/cameras/{camera_id}/devices",
              {"name": "Fake LCD monitor", "model": "Demo", "corners": lcd["corners"]})
    for metric, box, unit, lo, hi in (("pm25", lcd["fields"]["pm25"], "µg/m³", 0, 1000),
                                     ("co2", lcd["fields"]["co2"], "ppm", 300, 5000)):
        api(port, f"/api/devices/{dev['id']}/fields", {
            "metric": metric, "box": box, "unit": unit, "min_value": lo, "max_value": hi,
            "options": {"polarity": "dark_on_light", "decimals": 0}})
    api(port, f"/api/cameras/{camera_id}/reprocess", {})


def preset_new_fake_cameras(py: Path, port: int, done: set[str]) -> None:
    """When the fake camera is picked on the Cameras page, mark its first monitor too."""
    for agent in api(port, "/api/setup")["agents"]:
        for cam in agent["cameras"]:
            if cam["id"] in done or not str(cam.get("source")).startswith("synthetic") or not cam.get("last_frame_ms"):
                continue
            preset_fake_lcd(py, port, cam["id"])
            done.add(cam["id"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webcam", type=int, metavar="N", help="record webcam number N straight away (no picker)")
    ap.add_argument("--bump", action="store_true",
                    help="fake camera gets knocked after 2 minutes, to watch shift detection correct it")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    # Ctrl+C or a terminate request both stop everything cleanly (and the children with us).
    def _stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    py = ensure_environment()
    source = "synthetic-bump" if args.bump else args.webcam  # None: choose on the Cameras page
    agent_cfg = write_agent_config(source, args.port)
    server_env = {**os.environ, "DATA_DIR": str(DEMO / "server"), "AGENT_TOKEN": TOKEN,
                  "DASHBOARD_USERS": f"{USER}:{PASSWORD}"}
    procs: list[tuple[str, subprocess.Popen]] = []

    def start(name: str, cmd: list[str], cwd: Path, env: dict) -> None:
        log = open(DEMO / f"{name}.log", "a", encoding="utf-8")
        procs.append((name, subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)))

    try:
        print("Starting the server ...")
        start("server", [str(py), "-m", "uvicorn", "aqserver.main:create_app", "--factory",
                         "--host", "127.0.0.1", "--port", str(args.port)], ROOT / "server", server_env)
        wait_for(lambda: api(args.port, "/api/overview") is not None, 60, "the server to start")
        start("worker", [str(py), "-m", "aqserver.worker"], ROOT / "server", server_env)
        print("Starting the capture program (it looks for your cameras) ...")
        start("agent", [str(py), "-m", "aqagent", "run", "-c", str(agent_cfg)], ROOT / "agent", dict(os.environ))
        if source is not None:
            wait_for(lambda: api(args.port, f"/api/cameras/{CAMERA_ID}/latest-frame"), 60, "the first camera picture")
            if str(source).startswith("synthetic"):
                preset_fake_lcd(py, args.port)

        url = f"http://localhost:{args.port}" + ("/cameras.html" if source is None else "")
        print(f"""
  Dashboard: {url}
  Login:     {USER} / {PASSWORD}

  1. Cameras page: pick your webcam from the dropdown (you see a preview), press
     "Start recording". No webcam? Pick "Fake camera with demo monitors".
  2. Calibrate: click the 4 corners of a monitor's screen, then drag a box around
     each number. (With the fake camera, the first monitor is already marked.)
  3. Live, Lined up, Results: watch the numbers come in.

  Logs are in demo-data/. Press Ctrl+C (or close this window) to stop.
""")
        if not args.no_browser:
            webbrowser.open(url)
        preset_done: set[str] = set()
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"The {name} stopped unexpectedly; see demo-data/{name}.log")
                    return 1
            if source is None:
                try:
                    preset_new_fake_cameras(py, args.port, preset_done)
                except (urllib.error.URLError, OSError, KeyError, ValueError):
                    pass
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    except RuntimeError as exc:
        print(f"Problem: {exc}")
        return 1
    finally:
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
