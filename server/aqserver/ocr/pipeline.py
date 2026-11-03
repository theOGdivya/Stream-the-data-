"""Frame -> straightened screens -> field crops -> numbers."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import sevenseg, tess
from .sevenseg import OcrOutput

def read_auto(crop: np.ndarray, options: dict) -> OcrOutput:
    """Display type not set: try both readers. Agreement is accepted; one sure reading while
    the other finds nothing is accepted; two sure but different readings go to review (set
    the display type on Calibrate to settle it)."""
    seg = sevenseg.read(crop, options)
    txt = tess.read(crop, options)
    sv, tv = parse_value(seg.text), parse_value(txt.text)
    out = _auto_choice(seg, txt, sv, tv)
    out.extra = {"sevenseg": seg.text or None, "tesseract": txt.text or None,
                 "sevenseg_confidence": round(seg.confidence, 2), "tesseract_confidence": round(txt.confidence, 2)}
    return out


def _auto_choice(seg: OcrOutput, txt: OcrOutput, sv: float | None, tv: float | None) -> OcrOutput:
    seg_sure = sv is not None and seg.confidence >= 0.6
    txt_sure = tv is not None and txt.confidence >= 0.6
    if sv is not None and sv == tv:
        return OcrOutput(seg.text, max(seg.confidence, txt.confidence), None, seg.binary)
    if seg_sure and txt_sure:
        # One reader saw only part of the number (e.g. "2" of "812": the text reader often drops
        # segment digits whose strokes are not joined). Trust the full reading if it is very sure.
        for full, part in ((seg, txt), (txt, seg)):
            if len(full.text) > len(part.text) and part.text in full.text and full.confidence >= 0.9:
                return OcrOutput(full.text, min(full.confidence, 0.8), None, full.binary)
        best = seg if seg.confidence >= txt.confidence else txt
        return OcrOutput(best.text, min(best.confidence, 0.5),
                         f"segment reader saw {seg.text}, text reader saw {txt.text}: set the display type", best.binary)
    # One reader is sure and the other is not (it usually saw part of the number): trust the sure one.
    if seg_sure:
        return OcrOutput(seg.text, min(seg.confidence, 0.8), seg.note, seg.binary)
    if txt_sure:
        return OcrOutput(txt.text, min(txt.confidence, 0.8), txt.note, txt.binary)
    best = seg if seg.confidence >= txt.confidence else txt
    return OcrOutput(best.text, min(best.confidence, 0.5), best.note or "unsure", best.binary)


RECOGNIZERS = {
    "auto": read_auto,
    "sevenseg": sevenseg.read,
    "tesseract": tess.read,
}

# Metric catalogue shown in the calibration UI (unit and plausible range are defaults).
METRICS: dict[str, dict] = {
    "pm25": {"label": "PM2.5", "unit": "µg/m³", "min": 0, "max": 1000},
    "pm10": {"label": "PM10", "unit": "µg/m³", "min": 0, "max": 2000},
    "pm1": {"label": "PM1.0", "unit": "µg/m³", "min": 0, "max": 1000},
    "co2": {"label": "CO₂", "unit": "ppm", "min": 300, "max": 10000},
    "tvoc": {"label": "TVOC", "unit": "ppb", "min": 0, "max": 60000},
    "hcho": {"label": "HCHO", "unit": "mg/m³", "min": 0, "max": 5},
    "temp": {"label": "Temperature", "unit": "°C", "min": -20, "max": 60},
    "rh": {"label": "Humidity", "unit": "%", "min": 0, "max": 100},
    "aqi": {"label": "AQI", "unit": "", "min": 0, "max": 999},
    "other": {"label": "Other", "unit": "", "min": None, "max": None},
}


@dataclass
class FieldSpec:
    id: int
    metric: str
    box: list[float]
    recognizer: str = "auto"
    options: dict = field(default_factory=dict)
    min_value: float | None = None
    max_value: float | None = None


@dataclass
class DeviceSpec:
    id: int
    corners: list[list[float]]
    warp_w: int
    warp_h: int
    fields: list[FieldSpec]
    ref_frame_id: int | None = None  # picture the corners were marked on (for shift detection)


@dataclass
class FieldResult:
    device_id: int
    field_id: int
    metric: str
    raw_text: str
    value: float | None
    confidence: float
    status: str
    note: str | None
    crop: np.ndarray | None = None
    binary: np.ndarray | None = None
    candidates: dict | None = None  # Automatic mode: what each reader saw


def load_image(path: Path | str) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot decode image {path}")
    return img


def warp_size(corners: list[list[float]]) -> tuple[int, int]:
    """Natural size of the straightened screen, kept between 600 and 1600 px on the long side."""
    (tl, tr, br, bl) = [np.array(c, dtype=float) for c in corners]
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    if w < 1 or h < 1:
        raise ValueError("corners do not form a screen")
    long_side = max(w, h)
    scale = min(max(1.0, 600 / long_side), 1600 / long_side)
    return max(1, round(w * scale)), max(1, round(h * scale))


def warp_device(frame: np.ndarray, corners: list[list[float]], w: int, h: int) -> np.ndarray:
    src = np.float32(corners)
    dst = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(frame, m, (w, h), flags=cv2.INTER_CUBIC)


def crop_box(img: np.ndarray, box: list[float]) -> np.ndarray:
    h, w = img.shape[:2]
    x, y, bw, bh = box
    x0 = min(max(0, math.floor(x * w)), w - 1)
    y0 = min(max(0, math.floor(y * h)), h - 1)
    x1 = min(w, max(x0 + 1, math.ceil((x + bw) * w)))
    y1 = min(h, max(y0 + 1, math.ceil((y + bh) * h)))
    return img[y0:y1, x0:x1]


def parse_value(text: str) -> float | None:
    if not text or "?" in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def judge(
    value: float | None,
    confidence: float,
    note: str | None,
    min_value: float | None,
    max_value: float | None,
    review_confidence: float,
) -> tuple[str, str | None]:
    if value is None:
        return "review", note or "could not read a number"
    if min_value is not None and value < min_value:
        return "review", f"below plausible range ({min_value:g})"
    if max_value is not None and value > max_value:
        return "review", f"above plausible range ({max_value:g})"
    if confidence < review_confidence:
        return "review", note or f"low confidence ({confidence:.2f})"
    return "ok", note


def read_field(crop: np.ndarray, spec: FieldSpec) -> OcrOutput:
    fn = RECOGNIZERS.get(spec.recognizer)
    if fn is None:
        return OcrOutput("", 0.0, f"unknown recognizer {spec.recognizer!r}", None)
    return fn(crop, spec.options or {})


def run_frame(
    frame: np.ndarray,
    devices: list[DeviceSpec],
    review_confidence: float,
    debug: bool = False,
) -> list[FieldResult]:
    results: list[FieldResult] = []
    for dev in devices:
        warped = warp_device(frame, dev.corners, dev.warp_w, dev.warp_h)
        for spec in dev.fields:
            crop = crop_box(warped, spec.box)
            out = read_field(crop, spec)
            value = parse_value(out.text)
            status, note = judge(value, out.confidence, out.note, spec.min_value, spec.max_value, review_confidence)
            results.append(
                FieldResult(
                    device_id=dev.id,
                    field_id=spec.id,
                    metric=spec.metric,
                    raw_text=out.text,
                    value=value,
                    confidence=round(float(out.confidence), 3),
                    status=status,
                    note=note,
                    crop=crop if debug else None,
                    binary=out.binary if debug else None,
                    candidates=out.extra,
                )
            )
    return results
