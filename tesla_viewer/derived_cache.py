"""Best-effort, source-validated derived data beside each decrypted clip."""

from __future__ import annotations

import gzip
import base64
import json
import os
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from .telemetry import TelemetrySample, extract_telemetry


CACHE_VERSION = 1
_locks_guard = threading.Lock()
_telemetry_locks: dict[str, threading.Lock] = {}


def _signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _cache_path(path: Path, kind: str) -> Path:
    return path.parent / ".myteslaviewer_cache" / f"{path.name}.{kind}.json.gz"


def load_result(path: Path, kind: str):
    try:
        signature = _signature(path)
        with gzip.open(_cache_path(path, kind), "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload.get("version") == CACHE_VERSION and payload.get("source") == signature:
            return payload.get("result")
    except (OSError, ValueError, TypeError, AttributeError, EOFError):
        pass
    return None


def save_result(path: Path, kind: str, result) -> bool:
    temporary = None
    try:
        signature = _signature(path)
        target = _cache_path(path, kind)
        target.parent.mkdir(exist_ok=True)
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump({"version": CACHE_VERSION, "source": signature, "result": result},
                      stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if _signature(path) != signature:
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, target)
        return True
    except (OSError, ValueError, TypeError):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def load_telemetry(path: Path) -> list[TelemetrySample]:
    key = str(path.resolve())
    with _locks_guard:
        lock = _telemetry_locks.setdefault(key, threading.Lock())
    with lock:
        cached = load_result(path, "sei")
        if isinstance(cached, list):
            try:
                return [TelemetrySample(**row) for row in cached]
            except (TypeError, ValueError):
                pass
        samples = extract_telemetry(path)
        save_result(path, "sei", [asdict(sample) for sample in samples])
        return samples


def load_image(path: Path, kind: str) -> tuple[bytes, int] | None:
    cached = load_result(path, kind)
    if not isinstance(cached, dict):
        return None
    try:
        image = base64.b64decode(cached["image"], validate=True)
        duration = int(cached.get("duration_ms", 0))
        return (image, duration) if image and image[:2] == b"\xff\xd8" else None
    except (KeyError, ValueError, TypeError):
        return None


def save_image(path: Path, kind: str, image: bytes, duration_ms: int = 0) -> bool:
    return save_result(path, kind, {"image": base64.b64encode(image).decode("ascii"),
                                    "duration_ms": duration_ms})
