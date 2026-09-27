"""Leave a trace when the app dies: native faults, Qt messages, Python errors.

Written to ``.tesla_viewer_crash.log`` in the working directory. A Qt abort
(e.g. a failed allocation inside the video pipeline) otherwise leaves no
message at all, only a Windows Error Reporting entry.
"""

from __future__ import annotations

import datetime as dt
import faulthandler
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler

_MAX_BYTES = 2 * 1024 * 1024
_stream = None
_lock = threading.Lock()


def _write(text: str) -> None:
    if _stream is None:
        return
    with _lock:
        try:
            _stream.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
            _stream.flush()
        except (OSError, ValueError):
            pass


def install(path: Path) -> None:
    global _stream
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_suffix(".old.log"))
        _stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    _write(f"--- start (Python {sys.version.split()[0]}) ---")
    faulthandler.enable(_stream, all_threads=True)

    def on_qt_message(mode, context, message) -> None:
        # ffmpeg chatter goes to stderr directly; keep warnings and worse.
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            _write(f"QT {mode.name}: {message}")

    qInstallMessageHandler(on_qt_message)
    previous = sys.excepthook

    def on_exception(kind, value, tb) -> None:
        _write("PYTHON " + "".join(traceback.format_exception(kind, value, tb)).rstrip())
        previous(kind, value, tb)

    sys.excepthook = on_exception


def note(text: str) -> None:
    """Record an app-level event useful for diagnosing a later crash."""
    _write(text)


def close() -> None:
    """Stop writing (e.g. before the data folder is deleted)."""
    global _stream
    with _lock:
        if _stream is None:
            return
        try:
            faulthandler.disable()
            _stream.close()
        except (OSError, ValueError):
            pass
        _stream = None
