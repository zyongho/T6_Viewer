"""Scan prioritization and adaptive playback, plus legacy pixel heuristics.

The production scan uses the local object detector in ``ground_detector``;
the pixel-only helpers remain for compatibility with earlier callers.
"""

from __future__ import annotations

from collections import deque

import numpy as np

FRAME_WIDTH = 96
FRAME_HEIGHT = 54
CELL_SIZE = 6
CACHE_VERSION = 7


def analysis_group_order(groups: list, selected=None) -> list:
    """Selected clip, its three chronological predecessors, then newest first.

    A programmatically displayed initial clip is not an explicit selection:
    pass ``None`` to scan the whole set newest-to-oldest.
    """
    newest_first = sorted(groups, key=lambda group: (group.timestamp, str(group.folder)), reverse=True)
    if selected is None:
        return newest_first
    index = next((index for index, group in enumerate(newest_first) if group is selected), -1)
    if index < 0:
        return newest_first
    prior = list(reversed(newest_first[index + 1:index + 4]))
    priority_ids = {id(group) for group in prior}
    priority_ids.add(id(selected))
    return [selected, *prior, *(group for group in newest_first if id(group) not in priority_ids)]


def _ground_components(cells: np.ndarray) -> int:
    """Count small moving regions whose lower edge reaches the ground band."""
    rows, columns = cells.shape
    active = cells.copy()
    active[:3] = False  # sky, branches, banners and most flags
    visited = np.zeros_like(active)
    candidates = 0
    for y in range(3, rows):
        for x in range(columns):
            if not active[y, x] or visited[y, x]:
                continue
            queue = deque([(y, x)])
            visited[y, x] = True
            count = 0
            left = right = x
            bottom = y
            while queue:
                cy, cx = queue.popleft()
                count += 1
                left, right = min(left, cx), max(right, cx)
                bottom = max(bottom, cy)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < rows and 0 <= nx < columns and active[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
            if bottom >= 5 and count >= 2 and right - left < columns * 0.7 and count < rows * columns * 0.35:
                candidates += 1
    return candidates


def analyze_motion_frames(raw: bytes) -> dict[str, object]:
    """Estimate activity and return a coarse timeline of candidate motion.

    A moving camera or broad illumination change cannot be distinguished
    reliably from objects, so it returns ``unknown`` instead of false green.
    """
    frame_bytes = FRAME_WIDTH * FRAME_HEIGHT
    count = len(raw) // frame_bytes
    if count < 2:
        return {"level": "unknown", "intervals": []}
    frames = np.frombuffer(raw[:count * frame_bytes], dtype=np.uint8).reshape(
        count, FRAME_HEIGHT, FRAME_WIDTH
    )
    # Keyframe-only decoding can duplicate a single source frame for the
    # entire clip. Do not call that a motion-free scene.
    if count >= 20:
        distinct_steps = np.count_nonzero(
            np.any(np.abs(np.diff(frames.astype(np.int16), axis=0)) > 2, axis=(1, 2))
        )
        if distinct_steps < max(3, count // 20):
            return {"level": "unknown", "intervals": []}
    brightness = frames[:-1].mean(axis=(1, 2))
    threshold = np.where(brightness < 55, 10, 22)[:, None, None]
    changed = np.abs(np.diff(frames.astype(np.int16), axis=0)) > threshold
    lower_fraction = changed[:, FRAME_HEIGHT // 3:, :].mean(axis=(1, 2))
    if np.count_nonzero(lower_fraction > 0.48) >= max(1, round(len(lower_fraction) * 0.2)):
        return {"level": "unknown", "intervals": []}
    max_candidates = 0
    multi_intervals = 0
    intervals: list[list[int]] = []
    for index, mask in enumerate(changed):
        cells = mask.reshape(
            FRAME_HEIGHT // CELL_SIZE, CELL_SIZE,
            FRAME_WIDTH // CELL_SIZE, CELL_SIZE,
        ).mean(axis=(1, 3)) >= 0.12
        candidates = _ground_components(cells)
        max_candidates = max(max_candidates, candidates)
        if candidates >= 2:
            multi_intervals += 1
        if candidates:
            start = max(0, index * 500 - 1500)
            end = (index + 1) * 500 + 1500
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], end)
            else:
                intervals.append([start, end])
    if max_candidates >= 3 or (max_candidates >= 2 and multi_intervals >= 3):
        level = "high"
    else:
        level = "moderate" if max_candidates else "quiet"
    return {"level": level, "intervals": intervals}


def classify_motion_frames(raw: bytes) -> str:
    """Compatibility helper returning only the traffic-light level."""
    return str(analyze_motion_frames(raw)["level"])


def adaptive_rate(results: list[dict[str, object] | None], position_ms: int, base_speed: float) -> float:
    """Fast-forward only when *every* selected view was safely classified."""
    if not results or any(
        result is None or result.get("level") not in ("quiet", "moderate", "high")
        for result in results
    ):
        return base_speed
    for result in results:
        if any(start <= position_ms <= end for start, end in result.get("intervals", [])):
            return base_speed
    return max(base_speed, 16.0)


# ---------------------------------------------------------------------------
# Change points from the object tracker's per-second counts.

OBJECT_CATEGORIES = ("person", "car", "motorcycle", "bicycle")
OBJECT_ICONS = {"person": "🚶", "car": "🚗", "motorcycle": "🏍", "bicycle": "🚲"}


def motion_known(result: dict[str, object] | None) -> bool:
    """True when a clip was actually scanned (quiet included)."""
    return bool(result and result.get("level") in ("quiet", "moderate", "high")
                and isinstance(result.get("counts"), list))


def change_runs(result: dict[str, object] | None, enabled: set[str],
                merge_gap_s: int = 2) -> list[dict[str, object]]:
    """Seconds where enabled objects moved, merged into runs.

    Each run is ``{"start": ms, "end": ms, "categories": {name: count}}``.
    Gaps of up to ``merge_gap_s`` quiet seconds are bridged.
    """
    if not motion_known(result):
        return []
    indices = [(index, name) for index, name in enumerate(OBJECT_CATEGORIES) if name in enabled]
    runs: list[dict[str, object]] = []
    for second, row in enumerate(result["counts"]):
        if not isinstance(row, list) or len(row) != len(OBJECT_CATEGORIES):
            return []
        moving = {name: row[index] for index, name in indices if row[index]}
        if not moving:
            continue
        if runs and second * 1000 - runs[-1]["end"] <= merge_gap_s * 1000:
            run = runs[-1]
        else:
            run = {"start": second * 1000, "end": second * 1000, "categories": {}}
            runs.append(run)
        run["end"] = (second + 1) * 1000
        for name, count in moving.items():
            run["categories"][name] = run["categories"].get(name, 0) + count
    return runs


def merge_runs(run_lists: list[list[dict[str, object]]]) -> list[dict[str, object]]:
    """Union of several cameras' runs on the shared timeline."""
    merged: list[dict[str, object]] = []
    for run in sorted((run for runs in run_lists for run in runs), key=lambda item: item["start"]):
        if merged and run["start"] <= merged[-1]["end"]:
            last = merged[-1]
            last["end"] = max(last["end"], run["end"])
            for name, count in run["categories"].items():
                last["categories"][name] = last["categories"].get(name, 0) + count
        else:
            merged.append({"start": run["start"], "end": run["end"],
                           "categories": dict(run["categories"])})
    return merged


def next_change_ms(runs: list[dict[str, object]], position_ms: int, lead_ms: int = 1000) -> int | None:
    """Seek target ``lead_ms`` before the next change that starts after now."""
    for run in runs:
        if run["start"] > position_ms + lead_ms + 250:
            return max(0, int(run["start"]) - lead_ms)
    return None


def inside_change(runs: list[dict[str, object]], position_ms: int,
                  lead_ms: int = 1000, tail_ms: int = 1000) -> bool:
    return any(run["start"] - lead_ms <= position_ms <= run["end"] + tail_ms for run in runs)


def category_totals(runs: list[dict[str, object]]) -> list[str]:
    """Categories seen, most frequent first."""
    totals: dict[str, int] = {}
    for run in runs:
        for name, count in run["categories"].items():
            totals[name] = totals.get(name, 0) + count
    return sorted(totals, key=lambda name: (-totals[name], OBJECT_CATEGORIES.index(name)))
