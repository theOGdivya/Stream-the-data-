"""Reader for seven-segment digits (the classic LCD "calculator" font).

General OCR engines such as Tesseract are poor at this font, so we decode it directly:

1. Threshold the crop so the lit segments are white.
2. Optionally undo the italic slant most LCD fonts have.
3. Split into digits on empty columns (a "1" is recognised by being narrow).
4. For each digit, check which of the 7 segment zones contain ink and look the
   pattern up in a table. Confidence comes from how clearly each zone is on/off.

Segment names:      a
                  f   b
                    g
                  e   c
                    d
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

# (a, b, c, d, e, f, g) -> character
PATTERNS: dict[tuple[int, ...], str] = {
    (1, 1, 1, 1, 1, 1, 0): "0",
    (0, 1, 1, 0, 0, 0, 0): "1",
    (0, 0, 0, 0, 1, 1, 0): "1",
    (1, 1, 0, 1, 1, 0, 1): "2",
    (1, 1, 1, 1, 0, 0, 1): "3",
    (0, 1, 1, 0, 0, 1, 1): "4",
    (1, 0, 1, 1, 0, 1, 1): "5",
    (1, 0, 1, 1, 1, 1, 1): "6",
    (0, 0, 1, 1, 1, 1, 1): "6",
    (1, 1, 1, 0, 0, 0, 0): "7",
    (1, 1, 1, 0, 0, 1, 0): "7",
    (1, 1, 1, 1, 1, 1, 1): "8",
    (1, 1, 1, 1, 0, 1, 1): "9",
    (1, 1, 1, 0, 0, 1, 1): "9",
    (0, 0, 0, 0, 0, 0, 1): "-",
}

ON_THRESHOLD = 0.5


@dataclass
class OcrOutput:
    text: str
    confidence: float
    note: str | None
    binary: np.ndarray | None = None  # debug image, ink = white
    support: int = 1  # how many clean-up methods gave this reading
    extra: dict | None = None  # e.g. what each reader saw (Automatic mode)


def binarize(crop: np.ndarray, polarity: str = "auto", threshold: int | None = None) -> np.ndarray | None:
    """Return a binary image with the digits white, or None if the crop is blank."""
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if gray.shape[0] < 2 or gray.shape[1] < 2:
        return None
    if gray.shape[0] < 96:
        scale = 96 / gray.shape[0]
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    if float(gray.std()) < 6.0:  # flat image: nothing displayed
        return None
    if threshold is None:
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _, bw = cv2.threshold(gray, int(threshold), 255, cv2.THRESH_BINARY)
    if polarity == "dark_on_light" or (polarity == "auto" and bw.mean() > 127):
        bw = 255 - bw
    k = max(1, round(gray.shape[0] / 64))
    if k > 1:
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    return bw


def deslant(bw: np.ndarray, slant_deg: float) -> np.ndarray:
    """Undo italic slant. Positive = digits lean right (the usual LCD style)."""
    if not slant_deg:
        return bw
    h, w = bw.shape
    t = math.tan(math.radians(slant_deg))
    offset = -t * h if t < 0 else 0.0
    m = np.float32([[1, t, offset], [0, 1, 0]])
    return cv2.warpAffine(bw, m, (w + math.ceil(abs(t) * h), h), flags=cv2.INTER_NEAREST, borderValue=0)


def _runs(mask_1d: np.ndarray) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, on in enumerate(mask_1d):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask_1d)))
    return runs


def _segments(sub: np.ndarray) -> list[float]:
    """Coverage (0..1) of each segment zone a..g inside one digit box."""
    h, w = sub.shape

    def span(lo: float, hi: float, n: int) -> slice:
        a = int(lo * n)
        return slice(a, max(int(hi * n), a + 1))

    def horiz(y0: float, y1: float) -> float:
        return float(sub[span(y0, y1, h), span(0.3, 0.7, w)].any(axis=0).mean())

    def vert(x0: float, x1: float, y0: float, y1: float) -> float:
        return float(sub[span(y0, y1, h), span(x0, x1, w)].any(axis=1).mean())

    return [
        horiz(0.0, 0.22),  # a
        vert(0.65, 1.0, 0.18, 0.42),  # b
        vert(0.65, 1.0, 0.58, 0.82),  # c
        horiz(0.78, 1.0),  # d
        vert(0.0, 0.35, 0.58, 0.82),  # e
        vert(0.0, 0.35, 0.18, 0.42),  # f
        horiz(0.39, 0.61),  # g
    ]


def decode(bw: np.ndarray, decimals: int | str = 0) -> OcrOutput:
    """Decode a binarised (ink = white) seven-segment field."""
    h_img, w_img = bw.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats((bw > 0).astype(np.uint8), connectivity=8)
    min_area = max(4, int(0.0015 * h_img * w_img))
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        keep[i] = stats[i, cv2.CC_STAT_AREA] >= min_area
    # The digits are the tallest things in the box. Drop anything wholly above or below
    # them (labels such as "PM2.5" or "ug/m3" that got inside the box).
    idx = [i for i in range(1, n) if keep[i]]
    if idx:
        hmax = max(int(stats[i, cv2.CC_STAT_HEIGHT]) for i in idx)
        tall = [i for i in idx if stats[i, cv2.CC_STAT_HEIGHT] >= 0.35 * hmax]
        btop = min(int(stats[i, cv2.CC_STAT_TOP]) for i in tall)
        bbot = max(int(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]) for i in tall)
        margin = 0.1 * (bbot - btop)
        for i in idx:
            y, h = int(stats[i, cv2.CC_STAT_TOP]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if y + h < btop - margin or y > bbot + margin:
                keep[i] = False
    mask = keep[labels]
    rows = np.where(mask.any(axis=1))[0]
    if rows.size == 0:
        return OcrOutput("", 0.0, "blank display", bw)
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    hd = bottom - top
    if hd < 8:
        return OcrOutput("", 0.0, "digits too small or only dashes", bw)

    # Decimal points: small, square blobs sitting on the baseline. Take them out
    # before splitting digits, because they often touch the gap between digits.
    dots: list[float] = []
    for i in range(1, n):
        if not keep[i]:
            continue
        x, y, w, h = (int(v) for v in stats[i, :4])
        if max(w, h) < 0.25 * hd and max(w, h) <= 1.5 * min(w, h) and (y - top) > 0.65 * hd:
            keep[i] = False
            dots.append(x + w / 2)
    mask = keep[labels]

    band = mask[top:bottom]
    runs = _runs(band.any(axis=0))
    # Merge runs split by tiny gaps inside a digit.
    merge_gap = max(1, round(0.08 * hd))
    merged: list[list[int]] = []
    for x0, x1 in runs:
        if merged and x0 - merged[-1][1] < merge_gap:
            merged[-1][1] = x1
        else:
            merged.append([x0, x1])

    wide = [x1 - x0 for x0, x1 in merged if (x1 - x0) >= 0.3 * hd]
    ref_w = float(np.median(wide)) if len(wide) >= 2 else 0.6 * hd
    boxes: list[tuple[int, int]] = []
    for x0, x1 in merged:
        w = x1 - x0
        parts = round(w / ref_w) if w > 1.6 * ref_w else 1
        step = w / parts
        for k in range(parts):
            boxes.append((int(x0 + k * step), int(x0 + (k + 1) * step)))

    # Digits without left-hand segments (3, 7) look narrower than the cell they sit in.
    # Every digit has a right-hand edge, so right-anchor narrow digits and widen them to
    # the full digit width before sampling segment zones.
    single = [x1 - x0 for x0, x1 in boxes if 0.3 * hd <= x1 - x0 <= 0.9 * hd]
    full_w = max(single + [0.5 * hd])

    chars: list[str] = []
    confs: list[float] = []
    right_edges: list[int] = []
    prev_x1 = 0
    for x0, x1 in boxes:
        w = x1 - x0
        sub = band[:, x0:x1]
        ink_rows = np.where(sub.any(axis=1))[0]
        if ink_rows.size == 0:
            continue
        vcov = ink_rows.size / hd
        ink_h = int(ink_rows[-1] - ink_rows[0] + 1)
        if ink_rows[0] > 0.3 * hd and ink_rows[-1] < 0.7 * hd and w >= ink_h:
            # A bar at mid-height only: a minus sign. It can only lead the number; anywhere
            # else it is unexpected and must go to review.
            chars.append("-" if not chars else "?")
            confs.append(0.8 if len(chars) == 1 else 0.0)
            right_edges.append(x1)
            prev_x1 = x1
            continue
        if w >= 0.3 * hd and ink_h < 0.55 * hd:
            continue  # short and wide: unit text such as "ppm" next to the digits
        narrow_digit = False
        if w < 0.3 * hd:
            # A "1" is a plain vertical bar: every row's ink is about one stroke wide. A narrow
            # "3" or "7" (thin fonts) has horizontal strokes much wider than its vertical ones.
            widths = [int(np.ptp(np.where(row)[0])) + 1 for row in sub if row.any()]
            is_bar = max(widths) <= 1.8 * float(np.median(widths))
            if vcov >= 0.6 and is_bar:
                chars.append("1")
                confs.append(min(1.0, (vcov - 0.5) * 2))
                right_edges.append(x1)
                prev_x1 = x1
                continue
            if vcov >= 0.6:
                narrow_digit = True  # fall through: read it as a full digit
            elif ink_rows[0] > 0.65 * hd:
                dots.append((x0 + x1) / 2)  # decimal point that survived the blob filter
                continue
            else:
                continue  # a thin speck: ignore
        if narrow_digit or w < 0.9 * full_w:
            x0 = max(prev_x1, x1 - round(full_w))
            sub = band[:, x0:x1]
        prev_x1 = x1
        cov = _segments(sub)
        pattern = tuple(int(c >= ON_THRESHOLD) for c in cov)
        ch = PATTERNS.get(pattern, "?")
        chars.append(ch)
        confs.append(0.0 if ch == "?" else min(min(1.0, abs(c - ON_THRESHOLD) * 2) for c in cov))
        right_edges.append(x1)

    if not chars:
        return OcrOutput("", 0.0, "no digits found", bw)

    digits = "".join(chars)
    if decimals == "auto":
        if dots:
            dot_x = max(dots)
            pos = sum(1 for e in right_edges if e <= dot_x)
            if 0 < pos < len(digits):
                digits = digits[:pos] + "." + digits[pos:]
    elif isinstance(decimals, int) and decimals > 0 and "?" not in digits:
        sign = "-" if digits.startswith("-") else ""
        body = digits.lstrip("-").rjust(decimals + 1, "0")
        digits = f"{sign}{body[:-decimals]}.{body[-decimals:]}"
    conf = min(confs)
    note = "unrecognised digit" if "?" in digits else None
    return OcrOutput(digits, conf, note, bw)


TRUSTED = "otsu"  # the original method, tested most; any other needs a second method to agree


def choose(candidates: list[tuple[str, OcrOutput]]) -> OcrOutput | None:
    """Pick the reading the clean-up variants agree on. Agreement raises confidence; a lone
    reading from a less-tested method, or a confident disagreement, sends it to review."""
    valid = [(n, c) for n, c in candidates if c.text and "?" not in c.text]
    if not valid:
        return max((c for _, c in candidates), key=lambda c: c.confidence, default=None)
    texts = {c.text for _, c in valid}
    # Most methods agreeing wins; then the trusted method; then confidence.
    def score(t):
        members = [(n, c) for n, c in valid if c.text == t and c.confidence >= 0.3]
        return (len(members), any(n == TRUSTED for n, _ in members),
                max((c.confidence for _, c in valid if c.text == t), default=0.0))
    best_text = max(texts, key=score)
    group = [(n, c) for n, c in valid if c.text == best_text]
    name, best = max(group, key=lambda x: x[1].confidence)
    conf = min(1.0, best.confidence + 0.1 * (len(group) - 1))
    rivals = [t for t in texts if t != best_text]
    rival_conf = max((c.confidence for _, c in valid if c.text in rivals), default=0.0)
    rival_support = max((sum(1 for _, c in valid if c.text == t and c.confidence >= 0.6) for t in rivals), default=0)
    support = len({n.removesuffix("-word") for n, _ in group})
    if len(group) == 1 and name != TRUSTED:
        return OcrOutput(best.text, min(conf, 0.55), "only one reading method was sure", best.binary, support)
    if rival_conf >= 0.8 and rival_support >= len(group):
        return OcrOutput(best.text, min(conf, 0.5), "reading methods disagree", best.binary, support)
    return OcrOutput(best.text, conf, best.note, best.binary, support)


def read(crop: np.ndarray, options: dict) -> OcrOutput:
    from .prep import variants

    decimals = options.get("decimals", 0)
    if decimals != "auto":
        try:
            decimals = int(decimals or 0)
        except (TypeError, ValueError):
            decimals = 0
    slant = float(options.get("slant") or 0)
    outs = [(name, decode(deslant(bw, slant), decimals))
            for name, bw in variants(crop, options.get("polarity", "auto"), options.get("threshold"))]
    best = choose(outs)
    return best if best is not None else OcrOutput("", 0.0, "blank display", None)
