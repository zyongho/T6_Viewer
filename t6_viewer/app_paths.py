"""Where the app keeps its own files, and where bundled resources live.

User data (settings, watch history, the motion-result index, crash log) goes
to a fixed per-user folder, %LOCALAPPDATA%\T6Viewer, so it does not
depend on the folder the program was started from (a shortcut or the
taskbar may start it anywhere, even in a read-only folder).

Resources (the detection model, the icon) are read from the package, or
from PyInstaller's unpacked bundle when running as an .exe.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "T6Viewer"
LEGACY_APP_NAMES = ("MyTeslaViewer",)  # earlier name of the app
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("T6VIEWER_HOME")
    if override:
        folder = Path(override)
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        folder = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
        if not folder.exists():
            _adopt_legacy_folder(folder)
    else:
        folder = Path.home() / f".{APP_NAME.lower()}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _adopt_legacy_folder(folder: Path) -> None:
    """Take over the data folder of the app's earlier name (settings, keys,
    watch history). Renamed when possible; files copied if it is in use."""
    for name in LEGACY_APP_NAMES:
        legacy = folder.parent / name
        if not legacy.is_dir():
            continue
        try:
            os.rename(legacy, folder)
            return
        except OSError:
            folder.mkdir(parents=True, exist_ok=True)
            for item in legacy.iterdir():
                if item.is_file():
                    try:
                        shutil.copy2(item, folder / item.name)
                    except OSError:
                        pass
            return


def data_file(name: str, legacy_name: str | None = None) -> Path:
    """A file in the data folder; moves an older copy from the working
    directory (where earlier versions kept it) the first time. Moving, not
    copying, so that deleting the data folder really removes everything."""
    target = data_dir() / name
    if legacy_name and not target.exists():
        legacy = Path.cwd() / legacy_name
        if legacy.is_file():
            try:
                shutil.move(str(legacy), str(target))
            except OSError:
                pass
    return target


def resource(*parts: str) -> Path:
    """A file shipped with the app (inside the .exe when frozen)."""
    base = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return base.joinpath(*parts)
