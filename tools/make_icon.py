"""Generate the app icon: a square camera body filling the icon, a large
lens in the middle, and a Tesla-style "T" sticker on the glass bulged by
fisheye (barrel) distortion.

    python tools/make_icon.py

Writes tesla_viewer/assets/app_icon.png (512 px) and app_icon.ico.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)

SIZE = 512
CENTER = SIZE / 2
LENS_R = 196          # outer barrel
GLASS_R = 146         # visible glass
EMBLEM_SCALE = 2.35 * GLASS_R / 176
OUT = Path(__file__).resolve().parent.parent / "tesla_viewer" / "assets"


def emblem_path(scale: float, dx: float, dy: float) -> QPainterPath:
    """A Tesla-style "T": curved top bar over a shield-shaped head and stem."""
    def p(x, y):
        return QPointF(dx + x * scale, dy + y * scale)
    path = QPainterPath()
    # top bar
    path.moveTo(p(-100, -58))
    path.quadTo(p(0, -104), p(100, -58))
    path.lineTo(p(94, -44))
    path.quadTo(p(0, -82), p(-94, -44))
    path.closeSubpath()
    # head and stem
    path.moveTo(p(-86, -34))
    path.quadTo(p(0, -66), p(86, -34))
    path.lineTo(p(76, -16))
    path.quadTo(p(34, -26), p(16, -22))
    path.lineTo(p(6, 104))
    path.quadTo(p(0, 114), p(-6, 104))
    path.lineTo(p(-16, -22))
    path.quadTo(p(-34, -26), p(-76, -16))
    path.closeSubpath()
    return path


def sticker_layer() -> np.ndarray:
    """The flat sticker (RGBA), before the lens bends it."""
    image = QImage(SIZE, SIZE, QImage.Format_RGBA8888)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    # Big and a little off-centre, so part of it runs off the glass.
    path = emblem_path(EMBLEM_SCALE, CENTER - 18 * GLASS_R / 176, CENTER + 36 * GLASS_R / 176)
    painter.setPen(QPen(QColor("#ffffff"), 16, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(path)                       # white sticker margin
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#e31937"))
    painter.drawPath(path)
    painter.end()
    buffer = image.constBits()
    return np.frombuffer(buffer, np.uint8).reshape(SIZE, SIZE, 4).copy()


def fisheye(layer: np.ndarray, strength: float = 2.1) -> np.ndarray:
    """Barrel distortion inside the glass: centre magnified, rim squeezed."""
    ys, xs = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    dx, dy = (xs - CENTER) / GLASS_R, (ys - CENTER) / GLASS_R
    radius = np.sqrt(dx * dx + dy * dy)
    source_radius = np.where(radius > 0, radius ** strength, 0)
    factor = np.where(radius > 0, source_radius / np.maximum(radius, 1e-6), 0)
    sx = CENTER + dx * factor * GLASS_R * 1.25
    sy = CENTER + dy * factor * GLASS_R * 1.25
    x0, y0 = np.floor(sx).astype(int), np.floor(sy).astype(int)
    fx, fy = (sx - x0)[..., None], (sy - y0)[..., None]
    def sample(x, y):
        inside = (x >= 0) & (x < SIZE) & (y >= 0) & (y < SIZE)
        out = np.zeros((SIZE, SIZE, 4), np.float32)
        out[inside] = layer[y[inside], x[inside]]
        return out
    result = (sample(x0, y0) * (1 - fx) * (1 - fy) + sample(x0 + 1, y0) * fx * (1 - fy)
              + sample(x0, y0 + 1) * (1 - fx) * fy + sample(x0 + 1, y0 + 1) * fx * fy)
    result[radius > 1.0] = 0
    return result.clip(0, 255).astype(np.uint8)


def compose() -> QImage:
    icon = QImage(SIZE, SIZE, QImage.Format_ARGB32_Premultiplied)
    icon.fill(Qt.transparent)
    painter = QPainter(icon)
    painter.setRenderHint(QPainter.Antialiasing)
    center = QPointF(CENTER, CENTER)
    # camera body: a square plate filling the whole icon
    body = QLinearGradient(0, 0, 0, SIZE)
    body.setColorAt(0.0, QColor("#4a5666"))
    body.setColorAt(0.5, QColor("#2a3441"))
    body.setColorAt(1.0, QColor("#151a21"))
    painter.setPen(QPen(QColor("#9aa8b8"), 6))
    painter.setBrush(QBrush(body))
    painter.drawRoundedRect(QRectF(6, 6, SIZE - 12, SIZE - 12), 92, 92)
    # a thin highlight along the top edge and a viewfinder window
    painter.setPen(QPen(QColor(255, 255, 255, 45), 4))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(22, 22, SIZE - 44, SIZE - 44), 78, 78)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#0b0e12"))
    painter.drawRoundedRect(QRectF(62, 44, 92, 36), 12, 12)
    painter.setBrush(QColor(90, 170, 230, 90))
    painter.drawRoundedRect(QRectF(70, 50, 44, 14), 6, 6)
    # lens barrel: dark metal with a bright bevel
    barrel = QRadialGradient(center, LENS_R)
    barrel.setColorAt(0.70, QColor("#3a4655"))
    barrel.setColorAt(0.93, QColor("#161b22"))
    barrel.setColorAt(1.00, QColor("#0b0e12"))
    painter.setPen(QPen(QColor("#8a99aa"), 6))
    painter.setBrush(QBrush(barrel))
    painter.drawEllipse(center, LENS_R, LENS_R)
    painter.setPen(QPen(QColor("#0b0e12"), 10))
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(center, GLASS_R + 18, GLASS_R + 18)
    painter.setPen(QPen(QColor("#5c6b7c"), 3))
    painter.drawEllipse(center, GLASS_R + 8, GLASS_R + 8)
    # glass: deep blue with a coated sheen
    glass = QRadialGradient(QPointF(CENTER - 50, CENTER - 60), GLASS_R * 1.35)
    glass.setColorAt(0.0, QColor("#2f7fbf"))
    glass.setColorAt(0.45, QColor("#123a5c"))
    glass.setColorAt(1.0, QColor("#050b12"))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(glass))
    painter.drawEllipse(center, GLASS_R, GLASS_R)
    # the bent sticker on the glass
    bent = fisheye(sticker_layer())
    sticker = QImage(bent.data, SIZE, SIZE, SIZE * 4, QImage.Format_RGBA8888).copy()
    painter.drawImage(0, 0, sticker)
    # reflections over everything, so it sits under the glass surface
    k = GLASS_R / 176
    glass_clip = QPainterPath()
    glass_clip.addEllipse(center, GLASS_R, GLASS_R)
    painter.setClipPath(glass_clip)
    shine = QPainterPath()
    shine.addEllipse(QRectF(CENTER - 130 * k, CENTER - 150 * k, 150 * k, 90 * k))
    painter.setBrush(QColor(255, 255, 255, 70))
    painter.drawPath(shine)
    painter.setBrush(QColor(255, 255, 255, 150))
    painter.drawEllipse(QPointF(CENTER - 88 * k, CENTER - 104 * k), 22 * k, 14 * k)
    painter.setBrush(QColor(120, 200, 255, 60))
    painter.drawEllipse(QPointF(CENTER + 96 * k, CENTER + 104 * k), 36 * k, 20 * k)
    painter.setClipping(False)
    # recording light in the body's top-right corner
    painter.setPen(QPen(QColor("#0b0e12"), 6))
    painter.setBrush(QColor("#ff3b30"))
    painter.drawEllipse(QPointF(SIZE - 84, 72), 24, 24)
    painter.end()
    return icon


def main() -> None:
    QGuiApplication.instance() or QGuiApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    icon = compose()
    icon.save(str(OUT / "app_icon.png"))
    icon.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(OUT / "app_icon.ico"))
    print("wrote", OUT / "app_icon.png")


if __name__ == "__main__":
    main()
