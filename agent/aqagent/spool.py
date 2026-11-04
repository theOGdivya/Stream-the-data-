"""Store-and-forward queue on local disk.

Every still is written here first, then uploaded. If the internet drops for an hour,
nothing is lost: the uploader catches up when it is back. Sent stills are kept for
``keep_sent_days`` as a local backup.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _write_atomic(path: Path, data: bytes) -> None:
    # The folder may have been removed while running (cleanup tools, a user tidying up):
    # recreate it rather than lose pictures.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Spool:
    def __init__(self, root: Path, keep_sent_days: float = 7.0):
        self.root = root
        self.pending = root / "pending"
        self.sent = root / "sent"
        self.rejected = root / "rejected"
        self.keep_sent_days = keep_sent_days
        for d in (self.pending, self.sent, self.rejected):
            d.mkdir(parents=True, exist_ok=True)

    def put(self, camera_id: str, captured_ms: int, jpeg: bytes, meta: dict) -> Path:
        stem = f"{captured_ms:013d}_{camera_id}"
        _write_atomic(self.pending / f"{stem}.jpg", jpeg)
        # The .json is written last; the uploader only looks at .json files, so it never
        # sees a half-written image.
        _write_atomic(self.pending / f"{stem}.json", json.dumps(meta).encode())
        return self.pending / f"{stem}.json"

    def pending_items(self, limit: int = 50) -> list[Path]:
        if not self.pending.is_dir():
            return []
        return sorted(self.pending.glob("*.json"))[:limit]

    def backlog(self) -> int:
        if not self.pending.is_dir():
            return 0
        return sum(1 for _ in self.pending.glob("*.json"))

    def _move(self, meta_path: Path, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        img = meta_path.with_suffix(".jpg")
        if img.exists():
            shutil.move(str(img), dest_dir / img.name)
        shutil.move(str(meta_path), dest_dir / meta_path.name)

    def mark_sent(self, meta_path: Path) -> None:
        if self.keep_sent_days <= 0:
            meta_path.with_suffix(".jpg").unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._move(meta_path, self.sent / day)

    def mark_rejected(self, meta_path: Path, reason: str) -> None:
        log.error("server rejected %s: %s (kept in %s)", meta_path.name, reason, self.rejected)
        self._move(meta_path, self.rejected)
        (self.rejected / f"{meta_path.stem}.reason.txt").write_text(reason, encoding="utf-8")

    def prune_sent(self) -> None:
        if self.keep_sent_days <= 0:
            return
        cutoff = time.time() - self.keep_sent_days * 86400
        if not self.sent.is_dir():
            return
        for day_dir in self.sent.iterdir():
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if day + 86400 < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                log.info("pruned local backup %s", day_dir.name)
