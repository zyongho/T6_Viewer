"""Frame extraction with the FFmpeg libraries bundled in PyAV (LGPL build).

Replaces calls to an external ffmpeg/ffprobe executable, so nothing has to
be installed separately. Only keyframes are decoded (Tesla clips have about
one per second), which keeps storyboards and object scans fast: measured on
a 60 s 2896x1876 clip, 2.8 s here versus 1.9 s for the ffmpeg command line
and 7-9 s for OpenCV, which has no keyframe-only mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import av
import numpy as np
from av.video.reformatter import Interpolation

PAD_GREY = 114  # YOLOX letterbox colour (0x72)


def _open(path: Path):
    container = av.open(str(path))
    stream = container.streams.video[0]
    stream.thread_count = 1
    return container, stream


def probe_duration(path: Path) -> float | None:
    """Clip length in seconds, or None when it cannot be read."""
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
            if container.duration:
                return container.duration / av.time_base
    except (av.FFmpegError, OSError, IndexError, ValueError):
        pass
    return None


def keyframes(path: Path, times: list[float]) -> Iterator[tuple[int, av.VideoFrame]]:
    """For each wanted time (s, ascending), the first keyframe at or after it.

    Yields ``(index into times, frame)``. Keyframes past the last wanted time
    are not decoded.
    """
    container, stream = _open(path)
    try:
        stream.codec_context.skip_frame = "NONKEY"
        wanted = 0
        for frame in container.decode(stream):
            if wanted >= len(times):
                break
            moment = frame.time if frame.time is not None else 0.0
            if moment + 0.05 < times[wanted]:
                continue
            # One keyframe may satisfy several close targets (short clips).
            while wanted < len(times) and moment + 0.05 >= times[wanted]:
                yield wanted, frame
                wanted += 1
    finally:
        container.close()


def scaled(frame: av.VideoFrame, width: int, height: int) -> np.ndarray:
    """RGB array of the frame resized with fast bilinear filtering."""
    return frame.reformat(width=width, height=height, format="rgb24",
                          interpolation=Interpolation.FAST_BILINEAR).to_ndarray()


def fit(frame: av.VideoFrame, box: int) -> np.ndarray:
    """Resize to fit a square box keeping the aspect ratio (no padding)."""
    ratio = min(box / frame.width, box / frame.height)
    return scaled(frame, max(2, round(frame.width * ratio)), max(2, round(frame.height * ratio)))


def detection_frames(path: Path, side: int = 640, limit: int = 300) -> Iterator[np.ndarray]:
    """One letterboxed ``side`` x ``side`` RGB frame per second of video.

    Matches the previous ``ffmpeg -vf fps=1,scale=..:force_original_aspect_ratio
    =decrease,pad=..:0:0`` output: picture top-left, grey padding.
    """
    duration = probe_duration(path) or limit
    times = [float(second) for second in range(min(limit, int(duration) + 1))]
    for _index, frame in keyframes(path, times):
        picture = fit(frame, side)
        canvas = np.full((side, side, 3), PAD_GREY, dtype=np.uint8)
        canvas[:picture.shape[0], :picture.shape[1]] = picture
        yield canvas


def storyboard_times(duration: float, count: int) -> list[float]:
    return [duration * index / count for index in range(count)]


def first_frame(path: Path, width: int = 640, at: float = 0.2) -> np.ndarray | None:
    """A preview picture near the start of the clip, ``width`` pixels wide."""
    try:
        for _index, frame in keyframes(path, [at]):
            height = max(2, round(frame.height * width / frame.width))
            return scaled(frame, width, height - height % 2)
        # No keyframe after ``at``: take the very first one.
        for _index, frame in keyframes(path, [0.0]):
            height = max(2, round(frame.height * width / frame.width))
            return scaled(frame, width, height - height % 2)
    except (av.FFmpegError, OSError, IndexError, ValueError):
        pass
    return None
