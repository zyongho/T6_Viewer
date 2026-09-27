"""Count unique timeline intervals actually traversed during playback."""

from __future__ import annotations


def add_interval(intervals: list[list[int]], start: int, end: int) -> list[list[int]]:
    if end <= start:
        return intervals
    merged: list[list[int]] = []
    for left, right in sorted([*intervals, [start, end]]):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return merged


def coverage(intervals: list[list[int]], duration_ms: int) -> float:
    if duration_ms <= 0:
        return 0.0
    return min(1.0, sum(max(0, end - start) for start, end in intervals) / duration_ms)
