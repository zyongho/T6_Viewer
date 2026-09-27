"""Local YOLOX-s detection and motion tracking for stationary TeslaCam views.

The model recognizes COCO object classes; it does not know why Tesla saved an
event, and a missed detection must not be treated as proof of an empty scene.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .app_paths import resource

MODEL_PATH = resource("models", "yolox_s_int8.onnx")
FRAME_SIDE = 640
FRAME_BYTES = FRAME_SIDE * FRAME_SIDE * 3
DETECTION_GROUPS = {
    "person": (0,),
    "car": (2, 5, 7),  # car, bus, truck
    "motorcycle": (3,),
    "bicycle": (1,),
}
GROUP_ORDER = tuple(DETECTION_GROUPS)
GROUND_CLASSES = tuple(class_id for group in GROUP_ORDER for class_id in DETECTION_GROUPS[group])
CLASS_GROUP = {class_id: group for group, ids in DETECTION_GROUPS.items() for class_id in ids}


def _cv2():
    try:
        import cv2
    except ImportError:
        vendor = Path(__file__).resolve().parent.parent / ".vendor"
        if not vendor.is_dir():
            raise
        sys.path.insert(0, str(vendor))
        import cv2
    return cv2


@dataclass(frozen=True)
class Detection:
    class_id: int
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


class GroundObjectDetector:
    def __init__(self, model_path: Path = MODEL_PATH):
        if not model_path.is_file():
            raise FileNotFoundError(f"객체 인식 모델 없음: {model_path}")
        self.cv2 = _cv2()
        self.cv2.setNumThreads(1)
        self.net = self.cv2.dnn.readNet(str(model_path))
        grids = []
        strides = []
        for stride in (8, 16, 32):
            width = FRAME_SIDE // stride
            xv, yv = np.meshgrid(np.arange(width), np.arange(width))
            grids.append(np.stack((xv, yv), axis=-1).reshape(-1, 2))
            strides.append(np.full((width * width, 1), stride))
        self.grids = np.concatenate(grids)
        self.strides = np.concatenate(strides)

    def detect(self, rgb_frame: np.ndarray) -> list[Detection]:
        if rgb_frame.shape != (FRAME_SIDE, FRAME_SIDE, 3):
            raise ValueError("YOLOX input must be a 640x640 RGB frame")
        blob = rgb_frame.transpose(2, 0, 1)[None].astype(np.float32)
        self.net.setInput(blob)
        output = self.net.forward()[0]
        classes = np.asarray(GROUND_CLASSES, dtype=np.int64)
        class_scores = output[:, 4:5] * output[:, 5 + classes]
        best = np.argmax(class_scores, axis=1)
        confidence = class_scores[np.arange(len(output)), best]
        selected = np.flatnonzero(confidence >= 0.35)
        if not len(selected):
            return []
        centers = (output[selected, :2] + self.grids[selected]) * self.strides[selected]
        sizes = np.exp(np.clip(output[selected, 2:4], -10, 10)) * self.strides[selected]
        boxes = np.column_stack((centers - sizes / 2, sizes))
        scores = confidence[selected]
        labels = classes[best[selected]]
        keep = self.cv2.dnn.NMSBoxesBatched(
            boxes.tolist(), scores.tolist(), labels.tolist(), 0.35, 0.5
        )
        result = []
        for raw_index in np.asarray(keep).reshape(-1):
            index = int(raw_index)
            x, y, width, height = boxes[index]
            # Skip tiny detections and objects entirely in the overhead band.
            if width < 5 or height < 7 or y + height < FRAME_SIDE * 0.28:
                continue
            result.append(Detection(int(labels[index]), float(x), float(y), float(width), float(height)))
        return result


# Tracking rules (detections are sampled once per second).
TRACK_MISSING_FRAMES = 3      # a track survives this many missed detections
MATCH_RADIUS = 1.0            # x object size: same object in the next frame
JUMP_RADIUS = 2.5             # x object size: fast mover that skipped ahead
PROVEN_MOVE = 0.25            # x object size away from where it was first seen
STEP_MOVE = 0.08              # x object size per second to count as moving now


class _Tracks:
    """Follow detections over time; report objects that really travel.

    A parked car whose box jitters, or that the detector only finds in some
    frames (behind trees, poor light), stays near where it was first seen and
    is never reported. An object counts once its track has moved more than
    ``PROVEN_MOVE`` of its size from its first position, and then in every
    second in which it keeps moving.
    """

    def __init__(self):
        self.tracks: list[dict] = []
        self.index = -1

    def step(self, detections: list[tuple[int, float, float, float, float]]) -> list[int]:
        self.index += 1
        index = self.index
        self.tracks = [track for track in self.tracks if index - track["seen"] <= TRACK_MISSING_FRAMES]
        free = set(range(len(self.tracks)))
        counts = [0] * len(GROUP_ORDER)
        unmatched = []
        for group, x, y, width, height in _deduplicate(detections):
            cx, cy, size = x + width / 2, y + height / 2, max(width, height)
            best = self._nearest(free, group, cx, cy, size, MATCH_RADIUS, 30)
            if best is None:
                unmatched.append((group, cx, cy, size))
                continue
            free.discard(best[1])
            if self._advance(self.tracks[best[1]], cx, cy, size, best[0]):
                counts[GROUP_ORDER.index(group)] += 1
        for group, cx, cy, size in unmatched:
            # A track lost this very frame, a little further away: something
            # moving fast between samples.
            lost = {i for i in free if self.tracks[i]["seen"] == index - 1}
            best = self._nearest(lost, group, cx, cy, size, JUMP_RADIUS, 60)
            if best is not None:
                free.discard(best[1])
                track = self.tracks[best[1]]
                track["proven"] = True
                self._advance(track, cx, cy, size, best[0])
                counts[GROUP_ORDER.index(group)] += 1
            else:
                self.tracks.append({"group": group, "ax": cx, "ay": cy, "cx": cx, "cy": cy,
                                    "size": size, "seen": index, "proven": False})
        return counts

    def _nearest(self, candidates, group, cx, cy, size, factor, floor):
        best = None
        for i in candidates:
            track = self.tracks[i]
            if track["group"] != group or not 0.5 <= size / max(track["size"], 1) <= 2.0:
                continue  # a different object, not the same one seen again
            distance = ((cx - track["cx"]) ** 2 + (cy - track["cy"]) ** 2) ** 0.5
            if distance <= max(floor, factor * min(size, track["size"])) and (best is None or distance < best[0]):
                best = (distance, i)
        return best

    def _advance(self, track: dict, cx: float, cy: float, size: float, step: float) -> bool:
        track["size"] = 0.7 * track["size"] + 0.3 * size
        track["cx"], track["cy"], track["seen"] = cx, cy, self.index
        just_proven = False
        if not track["proven"]:
            travelled = ((cx - track["ax"]) ** 2 + (cy - track["ay"]) ** 2) ** 0.5
            if travelled > max(15, PROVEN_MOVE * track["size"]):
                track["proven"] = just_proven = True
        return track["proven"] and (just_proven or step > max(4, STEP_MOVE * track["size"]))


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    overlap_w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = overlap_w * overlap_h
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _deduplicate(detections) -> list[tuple[str, float, float, float, float]]:
    """One box per object: the detector may label one vehicle car and truck."""
    kept: list[tuple[str, float, float, float, float]] = []
    for class_id, x, y, width, height in sorted(detections, key=lambda item: -item[3] * item[4]):
        group = CLASS_GROUP.get(class_id)
        if group is None:
            continue
        if any(item[0] == group and _iou(item[1:], (x, y, width, height)) > 0.6 for item in kept):
            continue
        kept.append((group, x, y, width, height))
    return kept


def moving_counts(frames: list[list[list[float]]]) -> list[list[int]]:
    """Per-second counts of moving objects per group from stored detections."""
    tracks = _Tracks()
    return [tracks.step([tuple(item) for item in detections]) for detections in frames]


class GroundMotionTracker:
    def __init__(self):
        self._tracks = _Tracks()
        self.detections: list[list[list[int]]] = []
        self.counts: list[list[int]] = []

    def update(self, detections: list[Detection]) -> int:
        rows = [[d.class_id, round(d.x), round(d.y), round(d.width), round(d.height)] for d in detections]
        self.detections.append(rows)
        counts = self._tracks.step([tuple(row) for row in rows])
        self.counts.append(counts)
        moving = sum(counts)
        return 2 if moving >= 2 else 1 if moving else 0

    def result(self) -> dict[str, object]:
        if len(self.counts) < 2:
            return {"level": "unknown", "intervals": [], "engine": "yolox"}
        summary = motion_for_categories({"counts": self.counts}, set(GROUP_ORDER))
        # Raw detections are kept so the moving-object rule can change
        # without decoding the clip again.
        return {**summary, "engine": "yolox", "counts": self.counts, "detections": self.detections,
                "duration_ms": len(self.counts) * 1000}


def motion_for_categories(result: dict[str, object], enabled: set[str]) -> dict[str, object]:
    """Re-evaluate a cached model scan without decoding the clip again."""
    counts = result.get("counts")
    if not isinstance(counts, list):
        return {"level": result.get("level", "unknown"), "intervals": result.get("intervals", [])}
    if not counts or any(
        not isinstance(row, list) or len(row) != len(GROUP_ORDER)
        or any(not isinstance(value, int) or value < 0 for value in row)
        for row in counts
    ):
        return {"level": "unknown", "intervals": []}
    indices = [index for index, group in enumerate(GROUP_ORDER) if group in enabled]
    seconds = [sum(row[index] for index in indices) for row in counts]
    max_level = max((2 if value >= 2 else 1 if value else 0 for value in seconds), default=0)
    level = ("quiet", "moderate", "high")[max_level]
    intervals: list[list[int]] = []
    for second, value in enumerate(seconds):
        if not value:
            continue
        start = max(0, second * 1000 - 2000)
        end = (second + 1) * 1000 + 2000
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], end)
        else:
            intervals.append([start, end])
    return {"level": level, "intervals": intervals}
