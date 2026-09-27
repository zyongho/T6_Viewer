"""Generate the app icon: a camera body filling the icon and a large lens
whose six-blade aperture hints at the six car cameras. No brand marks.

    python tools/make_icon.py

Writes tesla_viewer/assets/app_icon.png (512 px) and app_icon.ico.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)

SIZE = 512
CENTER = SIZE / 2
LENS_R = 196          # outer barrel
GLASS_R = 146         # visible glass
OUT = Path(__file__).resolve().parent.parent / "tesla_viewer" / "assets"


def aperture(painter: QPainter, center: QPointF) -> None:
    """Six overlapping blades leaving a hexagonal opening in the glass."""
    opening = GLASS_R * 0.42
    reach = GLASS_R * 1.35
    clip = QPainterPath()
    clip.addEllipse(center, GLASS_R, GLASS_R)
    painter.save()
    painter.setClipPath(clip)
    for blade in range(6):
        start = math.radians(blade * 60 - 90 + 12)
        end = math.radians((blade + 1) * 60 - 90 + 12)
        corner_a = QPointF(center.x() + opening * math.cos(start), center.y() + opening * math.sin(start))
        corner_b = QPointF(center.x() + opening * math.cos(end), center.y() + opening * math.sin(end))
        # each blade runs past the rim and is tilted a little, like a real iris
        far_a = QPointF(center.x() + reach * math.cos(start + 0.55), center.y() + reach * math.sin(start + 0.55))
        far_b = QPointF(center.x() + reach * math.cos(end + 0.55), center.y() + reach * math.sin(end + 0.55))
        shade = QLinearGradient(corner_a, far_b)
        tone = 58 + (blade % 2) * 14
        shade.setColorAt(0.0, QColor(tone + 40, tone + 48, tone + 60))
        shade.setColorAt(1.0, QColor(tone - 20, tone - 14, tone - 6))
        blade_path = QPainterPath(corner_a)
        for point in (corner_b, far_b, far_a):
            blade_path.lineTo(point)
        blade_path.closeSubpath()
        painter.setPen(QPen(QColor(210, 220, 232, 150), 2.5))
        painter.setBrush(QBrush(shade))
        painter.drawPath(blade_path)
    painter.restore()


def compose() -> QImage:
    icon = QImage(SIZE, SIZE, QImage.Format_ARGB32_Premultiplied)
    icon.fill(Qt.transparent)
    painter = QPainter(icon)
    painter.setRenderHint(QPainter.Antialiasing)
    center = QPointF(CENTER, CENTER)
    # camera body: a square plate filling the whole icon
    body = QLinearGradient(0, 0, 0, SIZE)
    body.setColorAt(0.0, QColor("#c9d2dc"))
    body.setColorAt(0.5, QColor("#9aa6b4"))
    body.setColorAt(1.0, QColor("#6d7888"))
    painter.setPen(QPen(QColor("#e8edf2"), 6))
    painter.setBrush(QBrush(body))
    painter.drawRoundedRect(QRectF(6, 6, SIZE - 12, SIZE - 12), 92, 92)
    # a thin highlight along the top edge and a viewfinder window
    painter.setPen(QPen(QColor(255, 255, 255, 110), 4))
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
    aperture(painter, center)
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
