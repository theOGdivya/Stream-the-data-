"""Where frame images live on disk."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def valid_camera_id(camera_id: str) -> bool:
    return bool(CAMERA_ID_RE.match(camera_id)) and camera_id not in {".", ".."}


def frame_relpath(camera_id: str, captured_ms: int, sha256: str) -> str:
    dt = datetime.fromtimestamp(captured_ms / 1000, tz=timezone.utc)
    return f"frames/{camera_id}/{dt:%Y/%m/%d}/{dt:%H%M%S}_{captured_ms % 1000:03d}_{sha256[:10]}.jpg"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def disk_usage(path: Path) -> dict:
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "free": usage.free, "used_fraction": round(1 - usage.free / usage.total, 4)}


def iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
