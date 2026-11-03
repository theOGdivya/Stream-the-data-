"""Command line: ``python -m aqagent <command>``.

Commands:
  run          capture and upload until stopped (Ctrl+C)
  check        test the server address and token
  list-cameras find webcam numbers on this computer
  snapshot     save one picture from a camera, to check framing and focus
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path

import cv2

from . import __version__
from .camera import CameraWorker, open_source
from .config import CameraConfig, load_config
from .net import CaptureState, Heartbeat, LiveSender, LiveState, Uploader
from .power import prevent_sleep
from .spool import Spool

log = logging.getLogger("aqagent")


def setup_logging(log_dir: Path | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_dir / "agent.log", maxBytes=5_000_000, backupCount=5,
                                                             encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.spool_dir, args.verbose)
    log.info("aqagent %s starting as %r with %d camera(s); server %s", __version__, cfg.agent_id, len(cfg.cameras),
             cfg.server_url)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    keepawake = prevent_sleep() if cfg.prevent_sleep else None

    spool = Spool(cfg.spool_dir, cfg.keep_sent_days)
    live = LiveState()
    capture = CaptureState()
    sender = LiveSender(cfg, live, stop)
    cameras = [CameraWorker(c, spool, live.is_live, sender.submit, stop, capture.get) for c in cfg.cameras]
    threads = [*cameras, sender, Uploader(cfg, spool, stop),
               Heartbeat(cfg, spool, lambda: [c.status() for c in cameras], live, stop, capture)]
    for t in threads:
        t.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        log.info("stopping; %d stills still waiting to upload (they will be sent next time)", spool.backlog())
        stop.set()
        for t in threads:
            t.join(timeout=5)
        if keepawake is not None:
            keepawake.terminate()
    return 0


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    setup_logging(None, args.verbose)
    spool = Spool(cfg.spool_dir, cfg.keep_sent_days)
    hb = Heartbeat(cfg, spool, lambda: [], LiveState(), threading.Event())
    try:
        resp = hb.beat()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to reach {cfg.server_url}: {exc}")
        return 1
    offset = (resp["server_time_ms"] - int(time.time() * 1000)) / 1000
    print(f"OK: server reachable and token accepted. Clock difference vs server: {offset:+.2f} s")
    if abs(offset) > 2:
        print("WARNING: turn on automatic time sync on this computer; timestamps must match the other devices.")
    return 0


def cmd_list(args) -> int:
    print("Probing camera numbers 0..%d (this can take a few seconds)..." % (args.max - 1))
    found = 0
    for i in range(args.max):
        cap, _ = open_source(CameraConfig(id="probe", source=i, backend=args.backend, fourcc=None,
                                          autofocus=None, width=640, height=480))
        if cap is None:
            continue
        ok, frame = cap.read()
        if ok and frame is not None:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"  source: {i}   -> working ({w}x{h} by default)")
            found += 1
        cap.release()
    if not found:
        print("No cameras found. Check the USB cable, close other apps using the camera, or try --backend dshow / msmf.")
    return 0


def cmd_snapshot(args) -> int:
    cfg = load_config(args.config)
    setup_logging(None, args.verbose)
    cams = [c for c in cfg.cameras if c.id == args.camera] if args.camera else cfg.cameras[:1]
    if not cams:
        print(f"camera {args.camera!r} not in config")
        return 1
    cam = cams[0]
    cap, _ = open_source(cam)
    if cap is None:
        print(f"cannot open {cam.source!r}")
        return 1
    deadline = time.time() + 3  # let auto-exposure settle
    frame = None
    while time.time() < deadline:
        ok, f = cap.read()
        if ok:
            frame = f
    cap.release()
    if frame is None:
        print("camera opened but gave no image")
        return 1
    out = Path(args.out or f"snapshot-{cam.id}.jpg")
    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    h, w = frame.shape[:2]
    print(f"saved {out} ({w}x{h}). Check that every screen is sharp and digits are at least ~25 px tall.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aqagent", description="Air-quality monitor capture agent")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="capture and upload until stopped")
    r.add_argument("-c", "--config", default="config.yaml")
    c = sub.add_parser("check", help="test server address and token")
    c.add_argument("-c", "--config", default="config.yaml")
    ls = sub.add_parser("list-cameras", help="find webcam numbers")
    ls.add_argument("--max", type=int, default=6)
    ls.add_argument("--backend", default="auto")
    s = sub.add_parser("snapshot", help="save one picture to check framing/focus")
    s.add_argument("-c", "--config", default="config.yaml")
    s.add_argument("--camera", help="camera id from the config (default: first)")
    s.add_argument("--out")
    args = p.parse_args(argv)
    try:
        return {"run": cmd_run, "check": cmd_check, "list-cameras": cmd_list, "snapshot": cmd_snapshot}[args.cmd](args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 2
