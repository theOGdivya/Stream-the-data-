"""Reader for normal fonts (colour/TFT and dot-matrix screens), using Tesseract.

Each clean-up variant from prep.py is tidied (labels and unit text removed), read in two
Tesseract modes, and the readings are combined with the same agreement rule as the
seven-segment reader.
"""
from __future__ import annotations

import cv2
import numpy as np

from .prep import variants
from .sevenseg import OcrOutput, choose

WHITELIST = "0123456789.-"


def digits_only(bw: np.ndarray) -> np.ndarray:
    """Keep the tall characters (the number) and decimal points; drop smaller text such as
    "PM2.5" above it or "ug/m3" beside it."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats((bw > 0).astype(np.uint8), connectivity=8)
    if n <= 1:
        return bw
    min_area = max(4, int(0.001 * bw.size))
    comps = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area]
    if not comps:
        return np.zeros_like(bw)
    hmax = max(int(stats[i, cv2.CC_STAT_HEIGHT]) for i in comps)
    tall = [i for i in comps if stats[i, cv2.CC_STAT_HEIGHT] >= 0.55 * hmax]
    x0 = min(int(stats[i, cv2.CC_STAT_LEFT]) for i in tall)
    x1 = max(int(stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH]) for i in tall)
    y0 = min(int(stats[i, cv2.CC_STAT_TOP]) for i in tall)
    y1 = max(int(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]) for i in tall)
    keep = set(tall)
    for i in comps:
        if i in keep:
            continue
        x, y, w, h = (int(v) for v in stats[i, :4])
        cx, cy = x + w / 2, y + h / 2
        small = max(w, h) < 0.3 * (y1 - y0)
        if small and x0 < cx < x1 and cy > y0 + 0.6 * (y1 - y0):
            keep.add(i)  # a decimal point between the digits
        elif cx > x0 and cx < x1 and y >= y0 - 2 and y + h <= y1 + 2 and h >= 0.35 * hmax:
            keep.add(i)  # a minus sign or a broken piece of a digit
    out = np.isin(labels, list(keep)).astype(np.uint8) * 255
    return out[max(0, y0 - 4):y1 + 4, max(0, x0 - 4):x1 + 4]


def _tesseract(bw_ink_white: np.ndarray, psm: int) -> OcrOutput:
    import pytesseract

    h = bw_ink_white.shape[0]
    if h < 8:
        return OcrOutput("", 0.0, "nothing to read", None)
    s = 64 / h  # Tesseract reads best at roughly 30-60 px character height
    img = cv2.resize(bw_ink_white, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST if s < 1 else cv2.INTER_LINEAR)
    img = 255 - cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)[1]  # dark text on white
    img = cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    data = pytesseract.image_to_data(img, config=f"--psm {psm} -c tessedit_char_whitelist={WHITELIST}",
                                     output_type=pytesseract.Output.DICT)
    words = [(t.strip(), float(c)) for t, c in zip(data["text"], data["conf"]) if t.strip() and float(c) >= 0]
    if not words:
        return OcrOutput("", 0.0, "no text found", 255 - img)
    text = "".join(t for t, _ in words)
    return OcrOutput(text, min(c for _, c in words) / 100.0, None, 255 - img)


def _apply_decimals(text: str, decimals) -> str:
    if isinstance(decimals, int) and decimals > 0 and "." not in text and text.lstrip("-").isdigit():
        sign = "-" if text.startswith("-") else ""
        body = text.lstrip("-").rjust(decimals + 1, "0")
        return f"{sign}{body[:-decimals]}.{body[-decimals:]}"
    return text


def read(crop: np.ndarray, options: dict) -> OcrOutput:
    try:
        import pytesseract
    except ImportError:  # pragma: no cover
        return OcrOutput("", 0.0, "pytesseract not installed", None)
    decimals = options.get("decimals", 0)
    if decimals != "auto":
        try:
            decimals = int(decimals or 0)
        except (TypeError, ValueError):
            decimals = 0
    outs: list[tuple[str, OcrOutput]] = []
    try:
        for name, bw in variants(crop, options.get("polarity", "auto"), options.get("threshold")):
            clean = digits_only(bw)
            if not clean.any():
                continue
            for psm in (7, 8):
                o = _tesseract(clean, psm)
                o = OcrOutput(_apply_decimals(o.text, decimals), o.confidence, o.note, o.binary)
                outs.append((name if psm == 7 else f"{name}-word", o))
            # Stop early once the trusted method and one more agree confidently.
            good = [o.text for _, o in outs if o.text and o.confidence >= 0.75]
            if any(good.count(t) >= 3 for t in set(good)):
                break
    except pytesseract.TesseractNotFoundError:
        return OcrOutput("", 0.0, "tesseract is not installed on the server", None)
    best = choose(outs)
    return best if best is not None else OcrOutput("", 0.0, "blank display", None)
