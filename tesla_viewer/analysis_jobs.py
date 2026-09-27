"""Per-clip post-processing executed inside analysis worker processes.

Nothing here imports Qt: these functions run in spawned processes managed by
``analysis_pool`` and return plain picklable data. Results are also written
to the per-clip disk cache so any process (or the next run) can reuse them.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import av
import numpy as np

from . import video_decode
from .derived_cache import load_image, load_result, load_telemetry, save_image, save_result
from .ground_detector import FRAME_SIDE, GroundMotionTracker, GroundObjectDetector, _cv2
from .telemetry import gps_samples, vehicle_motion_label

OBJECTS_CACHE_KIND = "objects_v7"  # v7: stationary objects excluded
VALID_LEVELS = ("quiet", "moderate", "high", "unknown")

_detector: GroundObjectDetector | None = None
_detector_error = ""
_ego_motion: dict[str, bool] = {}


def _get_detector() -> GroundObjectDetector | None:
    global _detector, _detector_error
    if _detector is None and not _detector_error:
        try:
            _detector = GroundObjectDetector()
        except Exception as exc:
            _detector_error = str(exc)
    return _detector


def _vehicle_moving(reference: Path) -> bool:
    key = str(reference)
    if key not in _ego_motion:
        try:
            samples = load_telemetry(reference)
        except (OSError, ValueError, struct.error):
            samples = []
        _ego_motion[key] = vehicle_motion_label(samples) == "이동"
    return _ego_motion[key]


def _detect_file(path: Path, detector: GroundObjectDetector) -> dict[str, object]:
    tracker = GroundMotionTracker()
    try:
        for frame in video_decode.detection_frames(path, FRAME_SIDE):
            tracker.update(detector.detect(frame))
    except (av.FFmpegError, OSError, ValueError) as exc:
        return {"level": "error", "intervals": [], "reason": str(exc)}
    return tracker.result()


def scan_objects(path: str, reference: str) -> dict[str, object]:
    """Classify ground-object motion for one camera clip."""
    clip = Path(path)
    cached = load_result(clip, OBJECTS_CACHE_KIND)
    if isinstance(cached, dict) and cached.get("level") in VALID_LEVELS:
        return cached
    if _vehicle_moving(Path(reference)):
        # Ego-motion makes background subtraction look like many moving
        # objects; do not turn that into red.
        result: dict[str, object] = {"level": "unknown", "intervals": [], "engine": "ego-motion"}
    else:
        detector = _get_detector()
        if detector is None:
            return {"level": "unknown", "intervals": [], "reason": _detector_error}
        result = _detect_file(clip, detector)
    if result.get("level") in ("quiet", "moderate", "high") or result.get("engine") == "ego-motion":
        save_result(clip, OBJECTS_CACHE_KIND, result)
    return result


def telemetry_summary(path: str) -> dict[str, object]:
    """Map route (at most ~200 points) and vehicle state for one clip."""
    route: list[list[float]] = []
    state = "정보 없음"
    try:
        telemetry = load_telemetry(Path(path))
        state = vehicle_motion_label(telemetry)
        samples = gps_samples(telemetry)
        route = [[sample.latitude_deg, sample.longitude_deg, sample.position_ms] for sample in samples]
        if route:
            first_lat, first_lon = route[0][:2]
            if max(abs(point[0] - first_lat) + abs(point[1] - first_lon) for point in route) < 0.00002:
                route = [route[0]]
        if len(route) > 200:
            stride = math.ceil(len(route) / 200)
            route = route[::stride]
            if samples and route[-1][2] != samples[-1].position_ms:
                route.append([samples[-1].latitude_deg, samples[-1].longitude_deg, samples[-1].position_ms])
    except (OSError, ValueError, struct.error):
        route = []
    return {"route": route, "state": state}


STORYBOARD_FRAMES = 20
STORYBOARD_CELL = (96, 54)


def make_storyboard(path: str) -> dict[str, object]:
    """Twenty evenly spaced keyframes as one cached contact sheet.

    Background counterpart of the GUI's progressive storyboard: same cache
    entry, decoded with PyAV and encoded with OpenCV (no Qt needed).
    """
    clip = Path(path)
    cached = load_image(clip, "storyboard20")
    if cached is not None:
        return {"image": cached[0], "duration_ms": cached[1]}
    duration = video_decode.probe_duration(clip)
    if not duration or not 0 < duration < 86400:
        return {"error": "영상 길이를 읽지 못했습니다."}
    width, height = STORYBOARD_CELL
    sheet = np.zeros((height, width * STORYBOARD_FRAMES, 3), dtype=np.uint8)
    try:
        for index, frame in video_decode.keyframes(clip, video_decode.storyboard_times(duration, STORYBOARD_FRAMES)):
            sheet[:, index * width:(index + 1) * width] = video_decode.scaled(frame, width, height)
    except (av.FFmpegError, OSError, ValueError):
        return {"error": "스토리보드를 만들지 못했습니다."}
    cv2 = _cv2()
    ok, encoded = cv2.imencode(".jpg", cv2.cvtColor(sheet, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return {"error": "스토리보드를 만들지 못했습니다."}
    image = encoded.tobytes()
    duration_ms = round(duration * 1000)
    save_image(clip, "storyboard20", image, duration_ms)
    return {"image": image, "duration_ms": duration_ms}


HANDLERS = {"motion": scan_objects, "telemetry": telemetry_summary, "storyboard": make_storyboard}
