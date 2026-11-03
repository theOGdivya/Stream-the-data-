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
import platform
import signal
import sys
import threading
import time
from pathlib import Path

import cv2

from . import __version__
from .camera import CameraWorker, open_index, open_source
from .config import ID_RE, CameraConfig, load_config
from .net import CaptureState, Heartbeat, LiveSender, LiveState, ManagedState, Prober, Uploader
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


class CameraSet:
    """One capture thread per wanted camera: the cameras in the config file plus the ones
    chosen on the dashboard's Cameras page. Changes on the dashboard apply within seconds."""

    def __init__(self, cfg, spool, live, sender, capture, managed):
        self.cfg, self.spool, self.live, self.sender, self.capture, self.managed = cfg, spool, live, sender, capture, managed
        self._lock = threading.Lock()
        self._workers: dict[str, tuple[CameraWorker, threading.Event, CameraConfig]] = {}

    def desired(self) -> dict[str, CameraConfig]:
        want = {c.id: c for c in self.cfg.cameras}
        for m in self.managed.get():
            cid = str(m.get("id") or "")
            if not ID_RE.match(cid) or cid in want:
                continue  # the config file wins on a name clash
            try:
                want[cid] = CameraConfig(id=cid, name=m.get("name"), source=m.get("source", 0),
                                         still_interval_s=max(1.0, float(m.get("still_interval_s") or 10)),
                                         width=int(m.get("width") or 1920), height=int(m.get("height") or 1080))
            except (TypeError, ValueError):
                continue
        return want

    def reconcile(self) -> None:
        want = self.desired()
        stopped = []
        with self._lock:
            for cid in list(self._workers):
                if cid not in want or want[cid] != self._workers[cid][2]:
                    worker, ev, _ = self._workers.pop(cid)
                    ev.set()
                    stopped.append(worker)
        for worker in stopped:
            worker.join(timeout=5)  # releases the webcam before anything else opens it
            log.info("camera %s stopped", worker.cfg.id)
        with self._lock:
            for cid, c in want.items():
                if cid not in self._workers:
                    ev = threading.Event()
                    worker = CameraWorker(c, self.spool, self.live.is_live, self.sender.submit, ev, self.capture.get)
                    worker.start()
                    self._workers[cid] = (worker, ev, c)
                    log.info("camera %s started (source %r)", cid, c.source)

    def statuses(self) -> list[dict]:
        with self._lock:
            return [w.status() for w, _, _ in self._workers.values()]

    def in_use(self) -> dict[int, str]:
        with self._lock:
            return {c.source: cid for cid, (_, _, c) in self._workers.items() if isinstance(c.source, int)}

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for _, ev, _ in workers:
            ev.set()
        for w, _, _ in workers:
            w.join(timeout=5)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg.spool_dir, args.verbose)
    log.info("aqagent %s starting as %r with %d camera(s) from the config file; server %s", __version__, cfg.agent_id,
             len(cfg.cameras), cfg.server_url)
    if not cfg.cameras:
        log.info("no cameras in the config file: choose them on the dashboard's Cameras page")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    keepawake = prevent_sleep() if cfg.prevent_sleep else None
    if platform.system() == "Darwin":
        # macOS asks for camera permission only from the main thread: ask now, once.
        cap = open_index(0)
        if cap is not None:
            cap.release()

    spool = Spool(cfg.spool_dir, cfg.keep_sent_days)
    live = LiveState()
    capture = CaptureState()
    managed = ManagedState()
    sender = LiveSender(cfg, live, stop)
    cams = CameraSet(cfg, spool, live, sender, capture, managed)
    prober = Prober(cfg, cams.in_use)
    threads = [sender, Uploader(cfg, spool, stop),
               Heartbeat(cfg, spool, cams.statuses, live, stop, capture, managed, prober.request)]
    for t in threads:
        t.start()
    prober.request()  # tell the dashboard which cameras this computer has
    try:
        while not stop.is_set():
            cams.reconcile()
            stop.wait(1.0)
    finally:
        log.info("stopping; %d stills still waiting to upload (they will be sent next time)", spool.backlog())
        stop.set()
        cams.stop_all()
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
    from .discover import probe

    print(f"Looking for cameras 0..{args.max - 1} (this can take a few seconds)...")
    cams = probe(lambda i: open_index(i, args.backend), max_index=args.max)
    for c in cams:
        name = c.get("name") or "camera"
        print(f"  source: {c['index']}   {name}  ({c['width']}x{c['height']} when opened)")
    if not cams:
        print("No cameras found. Check the USB cable, close other apps using the camera (Zoom, Teams...),")
        print("and allow camera access (Windows: Settings > Privacy > Camera; Mac: Privacy & Security > Camera).")
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
    try:  # OpenCV warns loudly about every camera number that doesn't exist while scanning
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
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
