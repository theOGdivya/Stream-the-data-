"""A fake camera that films two fake air-quality monitors.

Use ``source: synthetic`` in the config to try the whole platform without a webcam.
``LAYOUT`` lists where the screens and numbers are, so you can calibrate quickly.
"""
from __future__ import annotations

import math
import random
import time

import cv2
import numpy as np

SEGS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg",
    "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
}

W, H = 1280, 720
# Screen corners (TL, TR, BR, BL) in the 1280x720 frame, and field boxes as fractions of each screen.
LAYOUT = {
    "Synthetic LCD": {
        "corners": [[150, 170], [560, 185], [555, 470], [145, 460]],
        "polarity": "dark_on_light",
        "fields": {"pm25": [0.38, 0.08, 0.58, 0.42], "co2": [0.38, 0.58, 0.58, 0.34]},
    },
    "Synthetic TFT": {
        "corners": [[720, 185], [1130, 170], [1135, 460], [725, 470]],
        "polarity": "light_on_dark",
        "fields": {"pm25": [0.38, 0.08, 0.58, 0.42], "temp": [0.38, 0.58, 0.58, 0.34]},
    },
}


def _digit(img, ch, x, y, w, h, t, color):
    g = max(1, t // 4)
    half = h // 2
    rects = {
        "a": (x + t + g, y, x + w - t - g, y + t),
        "g": (x + t + g, y + half - t // 2, x + w - t - g, y + half - t // 2 + t),
        "d": (x + t + g, y + h - t, x + w - t - g, y + h),
        "f": (x, y + g, x + t, y + half - g),
        "b": (x + w - t, y + g, x + w, y + half - g),
        "e": (x, y + half + g, x + t, y + h - g),
        "c": (x + w - t, y + half + g, x + w, y + h - g),
    }
    for s in SEGS.get(ch, ""):
        x0, y0, x1, y1 = rects[s]
        cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), color, -1)


def _number(img, text, box, color, cells):
    """Draw right-aligned seven-segment digits filling ``box`` (x, y, w, h in pixels)."""
    bx, by, bw, bh = box
    digit_h = int(bh * 0.8)
    digit_w = int(digit_h * 0.55)
    gap = int(digit_h * 0.25)
    t = max(2, int(digit_h * 0.13))
    total = cells * digit_w + (cells - 1) * gap
    x0 = bx + bw - total - int(bw * 0.04)
    y0 = by + (bh - digit_h) // 2
    chars = list(text.replace(".", ""))
    chars = [" "] * (cells - len(chars)) + chars
    dot_after = len(chars) - (len(text) - text.index(".")) if "." in text else None
    for i, ch in enumerate(chars):
        x = x0 + i * (digit_w + gap)
        _digit(img, ch, x, y0, digit_w, digit_h, t, color)
        if dot_after is not None and i == dot_after:
            cx = x + digit_w + gap // 2
            cv2.rectangle(img, (cx - t // 2, y0 + digit_h - t), (cx + t // 2, y0 + digit_h - 1), color, -1)


class SyntheticSource:
    """Mimics cv2.VideoCapture: grab()/retrieve()/read()/release()/isOpened()/get()."""

    def __init__(self, fps: float = 10.0, update_every_s: float = 5.0, seed: int | None = None,
                 bump_after_s: float | None = None):
        """``bump_after_s``: after this many seconds the "camera gets knocked": the whole view
        shifts by about 30 px and turns 1.5 degrees, to demonstrate shift detection."""
        self.bump_at = time.time() + bump_after_s if bump_after_s else None
        self.fps = fps
        self.update_every_s = update_every_s
        self.rng = random.Random(seed)
        self._last_grab = 0.0
        self._last_update = 0.0
        self.pm = 35.0
        self.co2 = 650.0
        self.temp = 24.5
        self.values = {}
        self._update_values(time.time())

    def _update_values(self, now: float) -> None:
        r = self.rng
        self.pm = max(2.0, self.pm + r.gauss(0, 2.5) + (r.random() < 0.02) * r.uniform(30, 120))
        self.pm += (35 - self.pm) * 0.05
        self.co2 = min(2500, max(420, self.co2 + r.gauss(0, 12) + 40 * math.sin(now / 900)))
        self.temp = min(30, max(20, self.temp + r.gauss(0, 0.05)))
        self.values = {
            "lcd_pm25": f"{round(self.pm)}",
            "lcd_co2": f"{round(self.co2)}",
            "tft_pm25": f"{round(self.pm * 0.88 + r.gauss(0, 1.5))}",
            "tft_temp": f"{self.temp:.1f}",
        }
        self._last_update = now

    def isOpened(self) -> bool:  # noqa: N802 - mimic OpenCV
        return True

    def get(self, prop: int) -> float:
        return {cv2.CAP_PROP_FRAME_WIDTH: W, cv2.CAP_PROP_FRAME_HEIGHT: H, cv2.CAP_PROP_FPS: self.fps}.get(prop, 0.0)

    def set(self, prop: int, value: float) -> bool:
        return False

    def grab(self) -> bool:
        wait = 1.0 / self.fps - (time.time() - self._last_grab)
        if wait > 0:
            time.sleep(wait)
        self._last_grab = time.time()
        if self._last_grab - self._last_update >= self.update_every_s:
            self._update_values(self._last_grab)
        return True

    def retrieve(self):
        return True, self.render()

    def read(self):
        self.grab()
        return self.retrieve()

    def release(self) -> None:
        pass

    def render(self) -> np.ndarray:
        frame = np.full((H, W, 3), (70, 62, 55), np.uint8)
        cv2.rectangle(frame, (0, 520), (W, H), (95, 110, 125), -1)  # desk
        screens = [
            ("Synthetic LCD", (185, 200, 180), (35, 40, 35), ("lcd_pm25", 3), ("lcd_co2", 4)),
            ("Synthetic TFT", (60, 30, 15), (240, 240, 235), ("tft_pm25", 3), ("tft_temp", 3)),
        ]
        for name, bg, fg, top, bottom in screens:
            sw, sh = 420, 300
            screen = np.full((sh, sw, 3), bg, np.uint8)
            fields = list(LAYOUT[name]["fields"].values())
            cv2.putText(screen, "PM2.5", (12, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, fg, 2)
            cv2.putText(screen, "ug/m3", (12, 72), cv2.FONT_HERSHEY_SIMPLEX, 0.55, fg, 1)
            cv2.putText(screen, "CO2 ppm" if bottom[0].endswith("co2") else "TEMP C", (12, 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, fg, 2)
            for (key, cells), (fx, fy, fw, fh) in zip((top, bottom), fields):
                _number(screen, self.values[key], (int(fx * sw), int(fy * sh), int(fw * sw), int(fh * sh)), fg, cells)
            corners = np.float32(LAYOUT[name]["corners"])
            m = cv2.getPerspectiveTransform(np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), corners)
            body = corners.astype(np.int32)
            cv2.fillPoly(frame, [body + np.array([[-30, -30], [30, -30], [30, 40], [-30, 40]])], (28, 28, 30))
            warped = cv2.warpPerspective(screen, m, (W, H))
            mask = cv2.warpPerspective(np.full((sh, sw), 255, np.uint8), m, (W, H)) > 0
            frame[mask] = warped[mask]
        if self.bump_at and time.time() >= self.bump_at:
            m = cv2.getRotationMatrix2D((W / 2, H / 2), 1.5, 1.0)
            m[0, 2] += 26
            m[1, 2] += 14
            frame = cv2.warpAffine(frame, m, (W, H), borderValue=(70, 62, 55))
        noise = np.random.default_rng().normal(0, 3, frame.shape)
        cv2.putText(frame, time.strftime("%H:%M:%S"), (20, 690), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (230, 230, 230), 2)
        return np.clip(frame + noise, 0, 255).astype(np.uint8)
