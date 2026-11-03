"""Find the cameras connected to this computer, for the camera picker on the dashboard."""
from __future__ import annotations

import base64
import json
import logging
import platform
import subprocess
import time
from pathlib import Path

import cv2

log = logging.getLogger(__name__)


def camera_names() -> tuple[dict[int, str], bool]:
    """Best-effort camera names by OpenCV index. The bool says whether the index-to-name
    match is reliable (Linux, Windows) or only a likely guess (macOS)."""
    system = platform.system()
    try:
        if system == "Linux":
            names = {}
            for p in Path("/sys/class/video4linux").glob("video*"):
                if p.name[5:].isdigit() and (p / "name").exists():
                    names[int(p.name[5:])] = (p / "name").read_text(encoding="utf-8", errors="replace").strip()
            return names, True
        if system == "Windows":
            from pygrabber.dshow_graph import FilterGraph  # DirectShow order == OpenCV DSHOW index

            return dict(enumerate(FilterGraph().get_input_devices())), True
        if system == "Darwin":
            out = subprocess.run(["system_profiler", "SPCameraDataType", "-json"], capture_output=True, text=True,
                                 timeout=15).stdout
            items = json.loads(out or "{}").get("SPCameraDataType", [])
            return {i: item.get("_name", "") for i, item in enumerate(items)}, False
    except Exception as exc:  # noqa: BLE001 - names are a nicety, never fatal
        log.debug("could not read camera names: %s", exc)
    return {}, False


def _preview(frame) -> str:
    h, w = frame.shape[:2]
    if w > 480:
        frame = cv2.resize(frame, (480, int(h * 480 / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def probe(open_camera, max_index: int = 6, in_use: dict[int, str] | None = None) -> list[dict]:
    """Try camera numbers 0..max_index-1. ``open_camera(index)`` returns an opened capture
    or None. Cameras already recorded by this agent (``in_use``) are listed but not opened."""
    in_use = in_use or {}
    names, certain = camera_names()
    found: list[dict] = []
    for i in range(max_index):
        if i in in_use:
            found.append({"index": i, "name": names.get(i), "name_certain": certain, "in_use_by": in_use[i]})
            continue
        cap = open_camera(i)
        if cap is None:
            continue
        try:
            frame = None
            deadline = time.time() + 2.5
            while time.time() < deadline:
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
                    if float(f.mean()) > 8:  # the first frames are often black while the camera starts
                        break
            if frame is None:
                continue
            h, w = frame.shape[:2]
            found.append({"index": i, "name": names.get(i), "name_certain": certain, "width": w, "height": h,
                          "preview": _preview(frame)})
        finally:
            cap.release()
    if not certain and names and len(names) != len(found):
        for f in found:  # can't tell which name goes with which camera
            f["name"] = None
    return found
