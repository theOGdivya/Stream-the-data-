"""Cameras found on each capture computer (in memory; they report again on every scan)."""
from __future__ import annotations

import threading


class Discovery:
    def __init__(self):
        self._lock = threading.Lock()
        self._found: dict[str, dict] = {}
        self._previews: dict[tuple[str, int], bytes] = {}
        self._requested: set[str] = set()

    def store(self, agent_id: str, probed_ms: int, cameras: list[dict], previews: dict[int, bytes]) -> None:
        with self._lock:
            self._found[agent_id] = {"probed_ms": probed_ms, "cameras": cameras}
            for key in [k for k in self._previews if k[0] == agent_id]:
                del self._previews[key]
            for index, jpeg in previews.items():
                self._previews[(agent_id, index)] = jpeg
            self._requested.discard(agent_id)

    def found(self, agent_id: str) -> dict | None:
        with self._lock:
            f = self._found.get(agent_id)
            return {"probed_ms": f["probed_ms"], "cameras": [dict(c) for c in f["cameras"]]} if f else None

    def preview(self, agent_id: str, index: int) -> bytes | None:
        with self._lock:
            return self._previews.get((agent_id, index))

    def request_scan(self, agent_id: str) -> None:
        with self._lock:
            self._requested.add(agent_id)

    def scanning(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._requested

    def take_request(self, agent_id: str) -> bool:
        """Whether a scan is wanted. Stays true until the results arrive (the agent ignores
        repeats while a scan is running, and a failed scan is simply asked for again)."""
        with self._lock:
            return agent_id in self._requested
