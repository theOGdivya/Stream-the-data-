"""Ways to turn a camera crop of a number into a clean black-and-white image (ink = white).

Real webcam pictures have uneven light, glare, coloured digits, faint "ghost" LCD segments,
blur and JPEG noise; no single method handles all of them. Each reader tries these
variants in order and keeps the result they agree on.
"""
from __future__ import annotations

from collections.abc import Iterator

import cv2
import numpy as np

TARGET_HEIGHT = 110  # crops are scaled to about this height before reading


def scaled(crop: np.ndarray, height: int = TARGET_HEIGHT) -> np.ndarray:
    h = crop.shape[0]
    if h < 2 or crop.shape[1] < 2:
        return crop
    s = height / h
    if 0.8 < s < 1.25:
        return crop
    return cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)


def _separability(gray: np.ndarray) -> float:
    """Otsu's between-class / total variance: how cleanly the image splits into two levels."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.0
    p = hist / total
    levels = np.arange(256)
    mu_t = (p * levels).sum()
    var_t = (p * (levels - mu_t) ** 2).sum()
    if var_t < 1e-6:
        return 0.0
    w0 = np.cumsum(p)
    mu0 = np.cumsum(p * levels)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu_t * w0 - mu0) ** 2 / (w0 * (1 - w0))
    return float(np.nanmax(between) / var_t)


def best_channel(crop: np.ndarray) -> np.ndarray:
    """The grey version with the most contrast between digits and background (helps
    with coloured digits such as green-on-black or red-on-black)."""
    if crop.ndim == 2:
        return crop
    b, g, r = cv2.split(crop)
    options = [cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), b, g, r, np.maximum(np.maximum(b, g), r)]
    return max(options, key=_separability)


def _otsu(img: np.ndarray, threshold: int | None = None) -> np.ndarray:
    if threshold is not None:
        return cv2.threshold(img, int(threshold), 255, cv2.THRESH_BINARY)[1]
    return cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def _ink_white(bw: np.ndarray, polarity: str, dark_on_light_hint: bool) -> np.ndarray:
    dark_ink = polarity == "dark_on_light" or (polarity == "auto" and dark_on_light_hint)
    return 255 - bw if dark_ink else bw


def _clean(bw: np.ndarray) -> np.ndarray:
    k = max(1, round(bw.shape[0] / 64))
    if k > 1:
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    return bw


def _hat(gray: np.ndarray, dark_ink: bool) -> np.ndarray:
    """Remove slow light changes and glare: keep only strokes thinner than ~half the height."""
    k = max(3, int(0.45 * gray.shape[0]) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    op = cv2.MORPH_BLACKHAT if dark_ink else cv2.MORPH_TOPHAT
    return cv2.morphologyEx(gray, op, kernel)


def _guard(bw: np.ndarray, polarity: str) -> np.ndarray:
    """Digits never cover most of the box: if a picture says they do, it came out inverted."""
    if polarity == "auto" and bw.mean() > 127:
        return 255 - bw
    return bw


def variants(crop: np.ndarray, polarity: str = "auto", threshold: int | None = None) -> Iterator[tuple[str, np.ndarray]]:
    """Yield (name, binary image with white ink), most generally useful first."""
    for name, bw in _variants(crop, polarity, threshold):
        yield name, _guard(bw, polarity)


def _variants(crop: np.ndarray, polarity: str, threshold: int | None) -> Iterator[tuple[str, np.ndarray]]:
    img = scaled(crop)
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    if float(gray.std()) < 5.0:
        return  # flat image: nothing on the screen
    # Which way round? Digits cover less of the box than the background does.
    plain = _otsu(gray, threshold)
    dark_hint = plain.mean() > 127
    yield "otsu", _clean(_ink_white(plain, polarity, dark_hint))
    if threshold is not None:
        return
    # Glare and uneven light fool the "which way round" guess, so with polarity "auto" the
    # light-removing method is tried both ways and the readers' agreement decides.
    ways = [polarity == "dark_on_light"] if polarity != "auto" else [dark_hint, not dark_hint]
    for name, g in (("hat", gray), ("hat-colour", cv2.GaussianBlur(best_channel(img), (3, 3), 0))):
        options = []
        for dark_ink in ways:
            ink = _hat(g, dark_ink)
            if float(ink.std()) >= 3.0:
                options.append((dark_ink, _clean(_otsu(ink))))
        if options:
            # The wrong way round turns part of the background into "ink": keep the version
            # with less ink.
            dark_ink, bw = min(options, key=lambda o: float(o[1].mean()))
            yield f"{name}-{'dark' if dark_ink else 'light'}", bw
    block = max(11, int(0.6 * gray.shape[0]) | 1)
    dark_ink = polarity == "dark_on_light" or (polarity == "auto" and dark_hint)
    # Ink is whatever is clearly darker (or brighter) than its surroundings.
    if dark_ink:
        adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 6)
    else:
        adaptive = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, -6)
    yield "adaptive", _clean(adaptive)
