"""Tesseract-based reader for colour/TFT and dot-matrix screens with normal fonts."""
from __future__ import annotations

import cv2
import numpy as np

from .sevenseg import OcrOutput


def read(crop: np.ndarray, options: dict) -> OcrOutput:
    try:
        import pytesseract
    except ImportError:  # pragma: no cover
        return OcrOutput("", 0.0, "pytesseract not installed", None)

    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if gray.shape[0] < 2 or gray.shape[1] < 2:
        return OcrOutput("", 0.0, "empty crop", None)
    scale = 80 / gray.shape[0]
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    if float(gray.std()) < 6.0:
        return OcrOutput("", 0.0, "blank display", None)
    threshold = options.get("threshold")
    if threshold is None:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _, bw = cv2.threshold(gray, int(threshold), 255, cv2.THRESH_BINARY)
    polarity = options.get("polarity", "auto")
    # Tesseract wants dark text on a light background.
    if polarity == "light_on_dark" or (polarity == "auto" and bw.mean() < 127):
        bw = 255 - bw
    bw = cv2.copyMakeBorder(bw, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)

    try:
        data = pytesseract.image_to_data(
            bw,
            config="--psm 7 -c tessedit_char_whitelist=0123456789.-",
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError:
        return OcrOutput("", 0.0, "tesseract binary not installed", 255 - bw)

    words = [(t.strip(), float(c)) for t, c in zip(data["text"], data["conf"]) if t.strip() and float(c) >= 0]
    if not words:
        return OcrOutput("", 0.0, "no text found", 255 - bw)
    text = "".join(t for t, _ in words)
    conf = min(c for _, c in words) / 100.0
    decimals = options.get("decimals", 0)
    if isinstance(decimals, int) and decimals > 0 and "." not in text and text.lstrip("-").isdigit():
        sign = "-" if text.startswith("-") else ""
        body = text.lstrip("-").rjust(decimals + 1, "0")
        text = f"{sign}{body[:-decimals]}.{body[-decimals:]}"
    return OcrOutput(text, conf, None, 255 - bw)
