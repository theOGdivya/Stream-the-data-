"""Harder test pictures: what a webcam really sees of a monitor's number.

Normal fonts (TFT screens) and seven-segment LCDs, with blur, JPEG artifacts, noise,
uneven lighting, glare and small unit labels inside the box.
"""
from __future__ import annotations

import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from render import render_display

FONT_FILES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
FONTS = [f for f in FONT_FILES if Path(f).exists()]

# (digit colour, background colour) as RGB
TFT_COLOURS = [((255, 255, 255), (12, 12, 14)), ((80, 230, 120), (8, 14, 10)), ((90, 220, 255), (10, 30, 70)),
               ((255, 170, 40), (15, 10, 5)), ((30, 30, 30), (225, 228, 230)), ((240, 70, 60), (20, 10, 10))]
LCD_COLOURS = [((40, 45, 40), (175, 190, 170)), ((55, 55, 60), (150, 155, 150)), ((20, 25, 30), (200, 205, 190))]


def tft_number(text: str, rng: random.Random, unit: bool) -> np.ndarray:
    font = ImageFont.truetype(rng.choice(FONTS), rng.choice([36, 48, 64, 80]))
    fg, bg = rng.choice(TFT_COLOURS)
    pad = rng.randint(6, 20)
    l, t, r, b = font.getbbox(text)
    unit_font = ImageFont.truetype(FONTS[0], max(10, font.size // 4))
    unit_w = unit_font.getbbox("ug/m3")[2] + 8 if unit else 0
    img = Image.new("RGB", (r - l + 2 * pad + unit_w, b - t + 2 * pad), bg)
    d = ImageDraw.Draw(img)
    d.text((pad - l, pad - t), text, font=font, fill=fg)
    if unit:
        d.text((pad + r - l + 6, img.height - pad - unit_font.size - 2), "ug/m3", font=unit_font, fill=fg)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def lcd_number(text: str, rng: random.Random) -> np.ndarray:
    fg, bg = rng.choice(LCD_COLOURS)
    cells = max(3, len(text.replace(".", "")))
    img = render_display(text, cells=cells, digit_h=rng.choice([30, 44, 60]), fg=fg[::-1], bg=bg[::-1],
                         slant_deg=0, ghost=rng.random() < 0.6, aspect=rng.choice([0.5, 0.55, 0.6]))
    return img


def degrade(img: np.ndarray, rng: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    # uneven light: a gradient across the screen, sometimes a glare spot
    gx = np.linspace(1 - rng.uniform(0, 0.35), 1 + rng.uniform(0, 0.2), w)[None, :, None]
    out *= gx
    if rng.random() < 0.35:
        cx, cy = rng.uniform(0.2, 0.8) * w, rng.uniform(0.2, 0.8) * h
        yy, xx = np.mgrid[0:h, 0:w]
        out += (55 * np.exp(-(((xx - cx) / (0.25 * w)) ** 2 + ((yy - cy) / (0.4 * h)) ** 2)))[..., None]
    out += np.random.default_rng(rng.randint(0, 9999)).normal(0, rng.uniform(2, 8), out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    # camera resolution: scale down a bit, blur, then JPEG
    s = rng.uniform(0.55, 1.0)
    out = cv2.resize(out, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    k = rng.choice([1, 3, 3, 5])
    if k > 1:
        out = cv2.GaussianBlur(out, (k, k), 0)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(55, 85)])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def cases(n: int = 120, seed: int = 7):
    """(image, expected_text, kind) samples."""
    rng = random.Random(seed)
    values = ["12", "35", "407", "9", "88", "1023", "56", "731", "24.6", "19.8", "600", "1250", "3", "47", "105"]
    out = []
    for i in range(n):
        text = rng.choice(values)
        if i % 2 == 0:
            img = tft_number(text, rng, unit=rng.random() < 0.4)
            kind = "tft"
        else:
            img = lcd_number(text, rng)
            kind = "lcd"
        out.append((degrade(img, rng), text, kind))
    return out
