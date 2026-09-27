"""Dark title bars and the app icon on Windows.

Windows 11 lets an app colour its caption through DWM; Windows 10 (1809+)
only honours the dark-mode flag. Other systems are left untouched.
"""

from __future__ import annotations

import ctypes
import os

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QWidget

from .app_paths import resource

ICON_PATH = resource("tesla_viewer", "assets", "app_icon.png")  # made by tools/make_icon.py
CAPTION = "#1c222b"   # same as the toolbar
CAPTION_TEXT = "#edf2f7"
BORDER = "#2a3441"

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def _colorref(hex_color: str) -> int:
    value = hex_color.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return red | green << 8 | blue << 16


def style_title_bar(widget: QWidget) -> None:
    if os.name != "nt":
        return
    try:
        dwm = ctypes.WinDLL("dwmapi")
        hwnd = ctypes.c_void_p(int(widget.winId()))
        for attribute, value in ((_DWMWA_USE_IMMERSIVE_DARK_MODE, 1),
                                 (_DWMWA_CAPTION_COLOR, _colorref(CAPTION)),
                                 (_DWMWA_TEXT_COLOR, _colorref(CAPTION_TEXT)),
                                 (_DWMWA_BORDER_COLOR, _colorref(BORDER))):
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))
    except (AttributeError, OSError, ValueError):
        pass


class _TitleBarStyler(QObject):
    """Style every top-level window (main window, dialogs, message boxes)."""

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.Show and isinstance(watched, QWidget) and watched.isWindow()
                and watched.windowType() not in (Qt.Popup, Qt.ToolTip, Qt.SplashScreen)
                and not watched.property("_dark_title")):
            watched.setProperty("_dark_title", True)
            style_title_bar(watched)
        return False


UI_FONT = "Pretendard"
UI_FONT_SIZE = 10  # points


def install_font(app: QApplication) -> None:
    """Pretendard (SIL OFL) for the whole UI: clean, calm and very legible
    for Korean and Latin text. Emoji fall back to the system emoji font."""
    loaded = False
    for path in sorted(resource("tesla_viewer", "assets", "fonts").glob("Pretendard-*.otf")):
        loaded |= QFontDatabase.addApplicationFont(str(path)) >= 0
    if loaded:
        font = QFont(UI_FONT, UI_FONT_SIZE)
        font.setHintingPreference(QFont.PreferNoHinting)
        app.setFont(font)


def install(app: QApplication) -> None:
    if os.name == "nt":
        try:
            # Own taskbar entry and icon instead of python.exe's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MyTeslaViewer.App")
        except (AttributeError, OSError):
            pass
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    install_font(app)
    styler = _TitleBarStyler(app)
    app.installEventFilter(styler)
    app._title_bar_styler = styler  # keep alive
