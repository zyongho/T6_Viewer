"""User-adjustable performance settings, stored beside the other app files."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CPU_COUNT = os.cpu_count() or 4
# The reference machine all measured numbers below come from.
REFERENCE_PC = "개발 테스트 PC(Intel i5-1340P 노트북, 내장 그래픽, RAM 16GB)"
OBJECT_TARGETS = ("person", "car", "motorcycle", "bicycle")

# Whole-app peak memory (GiB) with six 2896x1876 channels playing at the
# decode-load ceiling, next-clip preload included, analysis processes
# excluded. Measured on an i5-1340P laptop (Iris Xe, 16 GB): 1x 2.5, 4x 2.7,
# 6x 3.3 (occasional multi-GB spikes), 8x ~4.9, 12x/16x run away (8-15 GB).
_MEASURED_PEAK_GIB = ((6, 2.5), (24, 2.7), (36, 3.3), (48, 4.9), (72, 8.0), (96, 12.0))
WORKER_GIB = 0.19  # one analysis process including its video decoder
RECOMMENDED_DECODE_LOAD = 24
RISKY_DECODE_LOAD = 36  # above this, spikes of several GB were observed


@dataclass
class Settings:
    max_decode_load: int = RECOMMENDED_DECODE_LOAD
    analysis_playing: int = max(1, min(8, CPU_COUNT // 2))
    analysis_idle: int = max(1, min(12, CPU_COUNT - 4))
    analysis_fast: int = 2
    auto_jump: bool = False
    # What to analyse. With background analysis off, the checked items are
    # done only for the clip being viewed; all unchecked = playback only.
    analysis_background: bool = True
    analysis_objects: bool = True
    analysis_storyboard: bool = True
    analysis_sei: bool = True
    analysis_preview: bool = True
    # Which moving objects count as a change (non-driving clips only).
    object_categories: list[str] = field(default_factory=lambda: list(OBJECT_TARGETS))

    def clamp(self) -> "Settings":
        self.max_decode_load = max(6, min(96, int(self.max_decode_load)))
        self.analysis_playing = max(0, min(CPU_COUNT, int(self.analysis_playing)))
        self.analysis_idle = max(1, min(CPU_COUNT, int(self.analysis_idle)))
        self.analysis_fast = max(0, min(CPU_COUNT, int(self.analysis_fast)))
        for name in ("auto_jump", "analysis_background", "analysis_objects", "analysis_storyboard",
                     "analysis_sei", "analysis_preview"):
            setattr(self, name, bool(getattr(self, name)))
        chosen = self.object_categories if isinstance(self.object_categories, (list, tuple, set)) else []
        self.object_categories = [name for name in OBJECT_TARGETS if name in chosen] or list(OBJECT_TARGETS)
        return self


def load_settings(path: Path) -> Settings:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        known = {field.name for field in fields(Settings)}
        return Settings(**{key: value for key, value in payload.items() if key in known}).clamp()
    except (OSError, ValueError, TypeError, AttributeError):
        return Settings()


def save_settings(path: Path, settings: Settings) -> bool:
    try:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def rate_for_channels(max_decode_load: int, channels: int) -> float:
    return round(max(1.0, min(16.0, max_decode_load / max(1, channels))), 1)


def estimated_peak_gib(max_decode_load: int, analysis_processes: int, channels: int = 6) -> float:
    """Rough whole-app peak for ``channels`` playing at the ceiling."""
    load = channels * rate_for_channels(max_decode_load, channels)
    points = _MEASURED_PEAK_GIB
    if load <= points[0][0]:
        playback = points[0][1]
    elif load >= points[-1][0]:
        playback = points[-1][1]
    else:
        for (left, low), (right, high) in zip(points, points[1:]):
            if left <= load <= right:
                playback = low + (high - low) * (load - left) / (right - left)
                break
    # Fewer channels decode fewer pixels: scale the part above the idle app.
    playback = 1.25 + (playback - 1.25) * min(1.0, channels / 6)
    return round(playback + analysis_processes * WORKER_GIB, 1)
