"""Camera-shift detection and automatic re-alignment.

Each monitor's corners were marked on one reference picture. For every new picture we
find that monitor again by matching image features (ORB keypoints on the bezel, labels,
icons and surroundings) between the reference and the new picture, then fit a
rotation + scale + shift (RANSAC ignores features on the changing digits).

* shift below ``NOISE_PX``  -> "ok": use the marked corners as they are
* a clean fit                -> "adjusted": read the numbers at the moved corners
* no reliable fit            -> "lost": the monitor is not where it was (camera knocked
                                over, view blocked, monitor removed); readings go to review
* too little texture to check -> "unchecked": treated like "ok"
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass

import cv2
import numpy as np

MIN_INLIERS = 12
NOISE_PX = 1.5
MAX_SCALE_CHANGE = 0.2
MAX_ROTATION_DEG = 15.0
REF_MARGIN = 0.12  # share of the screen size added around it for reference features
SEARCH_MARGIN = 0.35  # how far (share of screen size) the monitor may have moved


@dataclass
class AlignResult:
    status: str  # ok | adjusted | lost | unchecked | kept (lost, but read fine where it last was)
    corners: list[list[float]]
    shift_px: float = 0.0
    inliers: int = 0

    def as_dict(self) -> dict:
        d = {"status": self.status, "shift_px": round(self.shift_px, 1), "inliers": self.inliers}
        if self.status in ("adjusted", "kept"):
            d["corners"] = [[round(x, 1), round(y, 1)] for x, y in self.corners]
        return d


def _rect(corners: list[list[float]], shape: tuple[int, ...], margin: float, min_margin: int) -> tuple[int, int, int, int]:
    pts = np.asarray(corners, dtype=float)
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    m = max(min_margin, margin * max(x1 - x0, y1 - y0))
    h, w = shape[:2]
    return int(max(0, x0 - m)), int(max(0, y0 - m)), int(min(w, x1 + m)), int(min(h, y1 + m))


class Aligner:
    def __init__(self, nfeatures: int = 1500, cache_size: int = 64):
        self._orb = cv2.ORB_create(nfeatures=nfeatures)
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._cache: OrderedDict[Hashable, tuple] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()  # OpenCV feature objects are not thread-safe

    def _features(self, gray: np.ndarray, rect: tuple[int, int, int, int]):
        x0, y0, x1, y1 = rect
        roi = gray[y0:y1, x0:x1]
        if roi.size == 0:
            return np.empty((0, 2), np.float32), None
        roi = self._clahe.apply(roi)  # evens out day/night lighting differences
        kps, des = self._orb.detectAndCompute(roi, None)
        pts = np.float32([kp.pt for kp in kps]).reshape(-1, 2) + np.float32([x0, y0])
        return pts, des

    def _reference(self, key: Hashable, load_ref: Callable[[], np.ndarray | None], corners):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        ref = load_ref()
        if ref is None:
            entry = (None, None)
        else:
            entry = self._features(ref, _rect(corners, ref.shape, REF_MARGIN, 20))
        self._cache[key] = entry
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return entry

    def align(self, key: Hashable, load_ref: Callable[[], np.ndarray | None], gray: np.ndarray,
              corners: list[list[float]]) -> AlignResult:
        with self._lock:
            ref_pts, ref_des = self._reference(key, load_ref, corners)
            if ref_des is None or len(ref_pts) < 3 * MIN_INLIERS:
                return AlignResult("unchecked", corners)
            pts = np.asarray(corners, dtype=float)
            size = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])))
            search = _rect(corners, gray.shape, REF_MARGIN + SEARCH_MARGIN, int(max(80, 0.3 * size)))
            cur_pts, cur_des = self._features(gray, search)
            if cur_des is None or len(cur_pts) < MIN_INLIERS:
                return AlignResult("lost", corners)
            pairs = self._matcher.knnMatch(ref_des, cur_des, k=2)
        good = [m[0] for m in pairs if len(m) == 2 and m[0].distance < 0.8 * m[1].distance]
        if len(good) < MIN_INLIERS:
            return AlignResult("lost", corners, inliers=len(good))
        src = np.float32([ref_pts[m.queryIdx] for m in good])
        dst = np.float32([cur_pts[m.trainIdx] for m in good])
        m, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0,
                                             maxIters=3000, confidence=0.995)
        inliers = int(inl.sum()) if inl is not None else 0
        if m is None or inliers < MIN_INLIERS:
            return AlignResult("lost", corners, inliers=inliers)
        scale = math.hypot(m[0, 0], m[1, 0])
        rotation = math.degrees(math.atan2(m[1, 0], m[0, 0]))
        if abs(scale - 1) > MAX_SCALE_CHANGE or abs(rotation) > MAX_ROTATION_DEG:
            return AlignResult("lost", corners, inliers=inliers)
        moved = cv2.transform(pts.reshape(-1, 1, 2), m).reshape(-1, 2)
        shift = float(np.linalg.norm(moved - pts, axis=1).max())
        if shift < NOISE_PX:
            return AlignResult("ok", corners, shift, inliers)
        return AlignResult("adjusted", moved.tolist(), shift, inliers)


ALIGNER = Aligner()
