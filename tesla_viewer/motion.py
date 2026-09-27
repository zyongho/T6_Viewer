"""Conservative motion based speed control for parked TeslaCam clips."""

from __future__ import annotations

import time

import numpy as np


class ParkedMotionDetector:
    """Compare small grayscale video frames and keep a slow playback window."""

    def __init__(self, hold_seconds: float = 3.0):
        self.previous: dict[str, np.ndarray] = {}
        self.last_seen: dict[str, float] = {}
        self.first_seen: float | None = None
        self.observations = 0
        self.slow_until = 0.0
        self.hold_seconds = hold_seconds

    def reset(self) -> None:
        self.previous.clear()
        self.last_seen.clear()
        self.first_seen = None
        self.observations = 0
        self.slow_until = 0.0

    def observe(self, camera: str, gray: np.ndarray, now: float | None = None) -> bool:
        """Return True when substantial change appears below the sky band."""
        now = time.monotonic() if now is None else now
        if gray.ndim != 2 or gray.size == 0:
            return False
        current = np.asarray(gray, dtype=np.uint8)
        previous = self.previous.get(camera)
        self.previous[camera] = current.copy()
        self.last_seen[camera] = now
        if self.first_seen is None:
            self.first_seen = now
        self.observations += 1
        if previous is None or previous.shape != current.shape:
            return False
        upper = current.shape[0] // 5
        difference = np.abs(current[upper:].astype(np.int16) - previous[upper:].astype(np.int16))
        changed = difference > 28
        # Require a visible cluster as well as area. This suppresses most
        # compression speckles while slowing down for people/vehicles.
        fraction = float(changed.mean())
        height, width = changed.shape
        cell_h, cell_w = max(1, height // 8), max(1, width // 10)
        clustered = any(
            changed[y:y + cell_h, x:x + cell_w].mean() > 0.12
            for y in range(0, height, cell_h)
            for x in range(0, width, cell_w)
        )
        moving = fraction > 0.007 and clustered
        if moving:
            self.slow_until = max(self.slow_until, now + self.hold_seconds)
        return moving

    def speed(self, base_speed: float, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if now < self.slow_until:
            return base_speed
        # Stay at the user's speed until the detector has a recent frame.
        if (not self.last_seen or self.observations < 2 or self.first_seen is None
                or now - self.first_seen < 1.5 or now - max(self.last_seen.values()) > 1.5):
            return base_speed
        return max(base_speed, 16.0)
