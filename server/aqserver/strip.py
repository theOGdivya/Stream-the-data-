"""The lined-up comparison strip: every monitor's straightened screen side by side, at one
moment, with the values read from each."""
from __future__ import annotations

import io
import threading
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Camera, Device, Field, Frame, Reading
from .ocr.pipeline import METRICS, load_image, warp_device
from .ocr_jobs import frame_corners
from .storage import iso

# --- decoded-image cache (a playing strip asks for the same frame several times) ---------------
_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_cache_lock = threading.Lock()


def cached_image(path: Path) -> np.ndarray:
    key = str(path)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    img = load_image(path)
    with _cache_lock:
        _cache[key] = img
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return img


def screen_image(settings: Settings, frame: Frame, device: Device, height: int | None = None) -> np.ndarray:
    img = warp_device(cached_image(settings.data_dir / frame.path), frame_corners(frame, device), device.warp_w, device.warp_h)
    if height and img.shape[0] != height:
        s = height / img.shape[0]
        img = cv2.resize(img, (max(1, round(img.shape[1] * s)), height), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    return img


# --- gathering one moment ----------------------------------------------------------------------

def strip_devices(db: Session) -> list[Device]:
    return list(db.scalars(select(Device).where(Device.active.is_(True))
                           .order_by(Device.position.is_(None), Device.position, Device.id)).all())


def _nearest(db: Session, camera_id: str, t_ms: int, tol_ms: int) -> Frame | None:
    q = select(Frame).where(Frame.camera_id == camera_id)
    before = db.scalars(q.where(Frame.captured_ms <= t_ms).order_by(Frame.captured_ms.desc()).limit(1)).first()
    after = db.scalars(q.where(Frame.captured_ms >= t_ms).order_by(Frame.captured_ms).limit(1)).first()
    best = min((f for f in (before, after) if f is not None), key=lambda f: abs(f.captured_ms - t_ms), default=None)
    return best if best is not None and abs(best.captured_ms - t_ms) <= tol_ms else None


def gather(db: Session, t_ms: int | None = None) -> dict:
    """Pick, per camera, the picture nearest ``t_ms`` (or the newest), and collect each
    monitor's values read from it."""
    devices = strip_devices(db)
    frames: dict[str, Frame | None] = {}
    for cam_id in dict.fromkeys(d.camera_id for d in devices):
        if t_ms is None:
            frames[cam_id] = db.scalars(select(Frame).where(Frame.camera_id == cam_id)
                                        .order_by(Frame.captured_ms.desc()).limit(1)).first()
        else:
            cam = db.get(Camera, cam_id)
            interval_ms = int((cam.still_interval_s if cam and cam.still_interval_s else 10) * 1000)
            frames[cam_id] = _nearest(db, cam_id, t_ms, max(30_000, 3 * interval_ms))
    tiles = []
    for d in devices:
        f = frames.get(d.camera_id)
        fields = db.scalars(select(Field).where(Field.device_id == d.id, Field.active.is_(True)).order_by(Field.id)).all()
        by_field = {}
        if f is not None:
            by_field = {r.field_id: r for r in db.scalars(select(Reading).where(Reading.frame_id == f.id,
                                                                                Reading.device_id == d.id)).all()}
        values = []
        for fl in fields:
            r = by_field.get(fl.id)
            meta = METRICS.get(fl.metric, {})
            values.append({
                "field_id": fl.id, "metric": fl.metric, "label": fl.label or meta.get("label", fl.metric),
                "unit": fl.unit if fl.unit is not None else meta.get("unit", ""),
                "value": None if r is None else (r.final_value if r.final_value is not None else r.value),
                "raw_text": None if r is None else r.raw_text,
                "status": None if r is None else r.status,
            })
        align = ((f.alignment or {}).get(str(d.id)) if f is not None else None) or {}
        tiles.append({
            "device_id": d.id, "name": d.name, "model": d.model, "is_own": d.is_own, "camera_id": d.camera_id,
            "position": d.position, "warp_w": d.warp_w, "warp_h": d.warp_h,
            "frame_id": f.id if f else None, "captured_ms": f.captured_ms if f else None,
            "masked": bool(f.masked) if f else False, "ocr_status": f.ocr_status if f else None,
            "alignment": {"status": align.get("status"), "shift_px": align.get("shift_px")},
            "values": values,
        })
    times = [t["captured_ms"] for t in tiles if t["captured_ms"]]
    return {"t_ms": t_ms if t_ms is not None else (max(times) if times else None),
            "spread_ms": (max(times) - min(times)) if times else 0, "tiles": tiles}


# --- rendering one moment as a picture ---------------------------------------------------------

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts", "/Library/Fonts"]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = ["DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold else ["DejaVuSans.ttf", "arial.ttf"]
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size), True
    return ImageFont.load_default(size=size), False  # basic font: no µ ³ ₂ glyphs


def _ascii_units(text: str) -> str:
    return text.replace("µ", "u").replace("³", "3").replace("₂", "2")


def _fit(draw, text: str, font, max_w: float) -> str:
    """Shorten text with an ellipsis so it fits in ``max_w`` pixels."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def render_png(settings: Settings, db: Session, data: dict, title: str | None) -> bytes:
    from PIL import Image, ImageDraw

    tile_h, label_h, gap, pad, per_row = 260, 58 + 34 * 3, 16, 24, 4
    f_title, full = _font(26, bold=True)
    f_sub, _ = _font(18)
    f_name, _ = _font(20, bold=True)
    f_val, _ = _font(26, bold=True)
    f_unit, _ = _font(16)
    fix = (lambda s: s) if full else _ascii_units

    tiles = []
    for t in data["tiles"]:
        w = max(200, min(560, round(tile_h * t["warp_w"] / max(1, t["warp_h"]))))
        img = None
        if t["frame_id"]:
            frame, dev = db.get(Frame, t["frame_id"]), db.get(Device, t["device_id"])
            bgr = screen_image(settings, frame, dev, tile_h)
            img = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            w = img.width
        tiles.append((t, img, w))
    rows = [tiles[i:i + per_row] for i in range(0, len(tiles), per_row)] or [[]]
    width = max(pad * 2 + sum(w for _, _, w in r) + gap * max(0, len(r) - 1) for r in rows)
    width = max(width, 640)
    header = 86
    height = header + len(rows) * (tile_h + label_h + gap) + pad
    canvas = Image.new("RGB", (width, height), (252, 252, 251))
    draw = ImageDraw.Draw(canvas)
    when = iso(data["t_ms"]).replace("T", " ").replace("Z", " UTC") if data["t_ms"] else "no pictures"
    draw.text((pad, 18), fix(title or "Side-by-side comparison"), font=f_title, fill=(11, 11, 11))
    sub = f"{when}"
    if data.get("spread_ms", 0) > 1500:
        sub += f"  (cameras up to {data['spread_ms'] / 1000:.0f} s apart)"
    draw.text((pad, 54), sub, font=f_sub, fill=(82, 81, 78))
    y = header
    for row in rows:
        x = pad
        for t, img, w in row:
            if img is not None:
                canvas.paste(img, (x, y))
            else:
                draw.rectangle([x, y, x + w, y + tile_h], fill=(225, 224, 217))
                draw.text((x + 12, y + tile_h // 2 - 10), "no picture", font=f_sub, fill=(82, 81, 78))
            draw.text((x, y + tile_h + 8), _fit(draw, fix(t["name"]), f_name, w), font=f_name, fill=(11, 11, 11))
            sub = " · ".join(p for p in (t["model"], "ours" if t["is_own"] else None) if p)
            if sub:
                draw.text((x, y + tile_h + 34), _fit(draw, fix(sub), f_unit, w), font=f_unit, fill=(82, 81, 78))
            vy = y + tile_h + 66
            for v in t["values"][:3]:
                if v["status"] in ("review",):
                    shown = f"{v['raw_text'] or '?'} (to review)"
                elif v["value"] is None:
                    shown = "–"
                else:
                    shown = f"{v['value']:g}"
                draw.text((x, vy), fix(v["label"]), font=f_unit, fill=(82, 81, 78))
                lw = draw.textlength(fix(v["label"]) + "  ", font=f_unit)
                draw.text((x + lw, vy - 6), shown, font=f_val, fill=(11, 11, 11))
                vw = draw.textlength(shown + " ", font=f_val)
                draw.text((x + lw + vw, vy), fix(v["unit"] or ""), font=f_unit, fill=(82, 81, 78))
                vy += 34
            x += w + gap
        y += tile_h + label_h + gap
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
