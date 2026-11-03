"""Keep the computer awake while the agent runs (the screen may still turn off)."""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess

log = logging.getLogger(__name__)


def prevent_sleep() -> object | None:
    system = platform.system()
    if system == "Windows":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            log.info("Windows sleep blocked while the agent runs")
        else:
            log.warning("could not block Windows sleep; set Power options -> Sleep: Never")
        return None
    if system == "Darwin" and shutil.which("caffeinate"):
        log.info("macOS sleep blocked with caffeinate while the agent runs")
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    log.info("on Linux, run the agent under systemd (see agent/scripts) and disable suspend in power settings")
    return None
