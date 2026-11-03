"""Draw fake seven-segment displays for tests."""
from __future__ import annotations

import math

import cv2
import numpy as np

SEGS = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg",
    "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
}


def draw_digit(img, ch, x, y, w, h, t, color):
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
    for s in SEGS[ch]:
        x0, y0, x1, y1 = rects[s]
        cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), color, -1)


def render_display(
    text: str,
    cells: int = 4,
    digit_h: int = 60,
    fg=(35, 40, 35),
    bg=(185, 200, 180),
    slant_deg: float = 0.0,
    ghost: bool = False,
    aspect: float = 0.55,
) -> np.ndarray:
    """Right-aligned seven-segment number, e.g. render_display("23.5", cells=3)."""
    digit_w = int(digit_h * aspect)
    t = max(2, int(digit_h * 0.13))
    gap = int(digit_h * 0.25)
    pad = int(digit_h * 0.3)
    width = pad * 2 + cells * digit_w + (cells - 1) * gap
    height = digit_h + pad * 2
    img = np.full((height, width, 3), bg, dtype=np.uint8)
    chars, dots = [], set()
    for ch in text:
        if ch == ".":
            dots.add(len(chars) - 1)
        else:
            chars.append(ch)
    chars = [" "] * (cells - len(chars)) + chars
    offset = cells - len([c for c in text if c != "."])
    if ghost:  # faint unlit segments, as on real LCDs
        ghost_col = tuple(int(b * 0.9 + f * 0.1) for b, f in zip(bg, fg))
        for i in range(cells):
            draw_digit(img, "8", pad + i * (digit_w + gap), pad, digit_w, digit_h, t, ghost_col)
    for i, ch in enumerate(chars):
        x = pad + i * (digit_w + gap)
        draw_digit(img, ch, x, pad, digit_w, digit_h, t, fg)
        if (i - offset) in dots:
            cx = x + digit_w + gap // 2
            cv2.rectangle(img, (cx - t // 2, pad + digit_h - t), (cx + t // 2, pad + digit_h - 1), fg, -1)
    if slant_deg:
        tan = math.tan(math.radians(slant_deg))
        extra = math.ceil(tan * height)
        m = np.float32([[1, -tan, tan * height], [0, 1, 0]])
        img = cv2.warpAffine(img, m, (width + extra, height), borderValue=bg)
    return img


def add_noise(img: np.ndarray, sigma: float = 6.0, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noisy = img.astype(np.float32) + rng.normal(0, sigma, img.shape)
    return np.clip(cv2.GaussianBlur(noisy, (3, 3), 0), 0, 255).astype(np.uint8)


def render_screen(pm25: str, co2: str, bg=(185, 200, 180), fg=(35, 40, 35)):
    """A 2-field LCD screen (PM2.5 on top, CO2 below). Returns (image, {metric: box})."""
    top = render_display(pm25, cells=3, digit_h=70, fg=fg, bg=bg)
    bottom = render_display(co2, cells=4, digit_h=50, fg=fg, bg=bg)
    w = max(top.shape[1], bottom.shape[1]) + 120
    h = top.shape[0] + bottom.shape[0] + 60
    screen = np.full((h, w, 3), bg, dtype=np.uint8)
    cv2.putText(screen, "PM2.5", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, fg, 2)
    cv2.putText(screen, "CO2", (8, top.shape[0] + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, fg, 2)
    x_top = w - top.shape[1] - 10
    y_top = 20
    screen[y_top:y_top + top.shape[0], x_top:x_top + top.shape[1]] = top
    x_bot = w - bottom.shape[1] - 10
    y_bot = y_top + top.shape[0] + 30
    screen[y_bot:y_bot + bottom.shape[0], x_bot:x_bot + bottom.shape[1]] = bottom
    boxes = {
        "pm25": [x_top / w, y_top / h, top.shape[1] / w, top.shape[0] / h],
        "co2": [x_bot / w, y_bot / h, bottom.shape[1] / w, bottom.shape[0] / h],
    }
    return screen, boxes


SCENE_CORNERS = [[210, 140], [610, 160], [600, 420], [200, 400]]  # TL, TR, BR, BL (slight perspective)


def render_scene(pm25: str, co2: str, seed: int = 0):
    """A 1280x720 'camera frame' with one monitor seen at a slight angle."""
    screen, boxes = render_screen(pm25, co2)
    frame = np.full((720, 1280, 3), (60, 55, 50), dtype=np.uint8)
    cv2.rectangle(frame, (170, 110), (650, 450), (30, 30, 30), -1)  # monitor body
    h, w = screen.shape[:2]
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), np.float32(SCENE_CORNERS))
    warped = cv2.warpPerspective(screen, m, (1280, 720))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), m, (1280, 720)) > 0
    frame[mask] = warped[mask]
    return add_noise(frame, sigma=4, seed=seed), boxes
