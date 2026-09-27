"""Generate the T6 Viewer icon: a camera body filling the icon, a large lens,
and the letters "T6" laid over the lens (Pretendard Bold, bundled).

    python tools/make_icon.py

Writes t6_viewer/assets/app_icon.png (512 px) and a multi-size app_icon.ico
(16-256 px, so Windows picks a sharp image for every place it shows it).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath,
    QPen, QRadialGradient,
)

SIZE = 512
CENTER = SIZE / 2
LENS_R = 196          # outer barrel
GLASS_R = 146         # visible glass
OUT = Path(__file__).resolve().parent.parent / "t6_viewer" / "assets"
FONT_FILE = OUT / "fonts" / "Pretendard-Bold.otf"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def lettering(painter: QPainter, center: QPointF) -> None:
    """ "T6" across the whole lens: white T, red 6, with a soft shadow."""
    family = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(FONT_FILE)))
    font = QFont(family[0] if family else "Arial")
    font.setWeight(QFont.Black if not family else QFont.Bold)
    font.setPixelSize(10)
    path_t, path_6 = QPainterPath(), QPainterPath()
    path_t.addText(0, 0, font, "T")
    path_6.addText(0, 0, font, "6")
    # measure at 10 px, then scale so the pair spans the lens diameter
    gap = -0.6
    width = path_t.boundingRect().width() + gap + path_6.boundingRect().width()
    height = max(path_t.boundingRect().height(), path_6.boundingRect().height())
    scale = (LENS_R * 2 * 0.98) / width
    left = center.x() - width * scale / 2
    top = center.y() - height * scale / 2
    painter.save()
    for path, color, offset in ((path_t, QColor("#ffffff"), 0.0),
                                (path_6, QColor("#ff3b3b"), path_t.boundingRect().width() + gap)):
        box = path.boundingRect()
        painter.save()
        painter.translate(left + (offset - box.left()) * scale, top - box.top() * scale)
        painter.scale(scale, scale)
        # shadow, dark outline, then the gradient fill
        painter.translate(0.12, 0.16)
        painter.fillPath(path, QColor(0, 0, 0, 120))
        painter.translate(-0.12, -0.16)
        painter.setPen(QPen(QColor("#0b0e12"), 0.38, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        fill = QLinearGradient(0, box.top(), 0, box.bottom())
        fill.setColorAt(0.0, color.lighter(115))
        fill.setColorAt(1.0, color.darker(118))
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)
        painter.restore()
    painter.restore()


def write_ico(image: QImage, target: Path) -> None:
    """A Windows .ico holding PNG images of every size in ICO_SIZES."""
    blobs = []
    for size in ICO_SIZES:
        scaled = image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        scaled.save(buffer, "PNG")
        blobs.append((size, bytes(data.data())))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for size, blob in blobs:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    target.write_bytes(header + entries + payload)


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
    lettering(painter, center)
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
    write_ico(icon, OUT / "app_icon.ico")
    print("wrote", OUT / "app_icon.png")


if __name__ == "__main__":
    main()
