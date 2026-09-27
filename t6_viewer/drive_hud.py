"""Graphical driving-state strip (speed, gear, steering, signals, pedals, G).

Shown at the top of the Front camera tile while a clip with SEI driving data
plays. It cannot be drawn on top of the picture itself: Qt shows video in a
native window that always covers widgets placed over it. Elements that do not
fit the tile width are left out, least important first.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .telemetry import TelemetrySample

DIM = QColor("#3a4655")
TEXT = QColor("#e8edf2")
MUTED = QColor("#8795a3")
ACCENT = QColor("#59b5ff")
GREEN = QColor("#48cf76")
RED = QColor("#ff5b5b")
AMBER = QColor("#ffb547")
MAX_SPEED_KMH = 160
GEARS = (("P", 0), ("R", 2), ("N", 3), ("D", 1))
AUTOPILOT = {1: "FSD", 2: "오토스티어", 3: "TACC"}


def is_driving(samples: list[TelemetrySample]) -> bool:
    """Show the strip only for clips recorded while the car was moving."""
    return any(abs(sample.vehicle_speed_mps or 0.0) > 0.5 for sample in samples)


class DriveHud(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(62)
        self.sample: TelemetrySample | None = None
        self._blink = QTimer(self)
        self._blink.setInterval(330)
        self._blink.timeout.connect(self.update)

    def set_sample(self, sample: TelemetrySample | None) -> None:
        if sample is self.sample:
            return
        self.sample = sample
        blinking = bool(sample and (sample.blinker_on_left or sample.blinker_on_right))
        if blinking != self._blink.isActive():
            self._blink.start() if blinking else self._blink.stop()
        self.update()

    # -- drawing -------------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#141920"))
        painter.setPen(QPen(QColor("#2a3441"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        sample = self.sample
        if sample is None:
            return
        height = self.height()
        # (display order, draw function, width); kept in priority order.
        parts = [(0, self._speed, 52), (1, self._gear, 76), (2, self._signals_and_wheel, 90),
                 (3, self._pedals, 50), (5, self._autopilot, 96), (4, self._g_meter, 48)]
        gap = 8 if self.width() < 420 else 14
        chosen, used = [], 8
        for part in parts:
            if used + part[2] + (gap if chosen else 0) <= self.width() - 8:
                chosen.append(part)
                used += part[2] + (gap if len(chosen) > 1 else 0)
        chosen.sort(key=lambda part: part[0])
        x = max(4.0, (self.width() - used) / 2 + 4)
        for _order, draw, width in chosen:
            draw(painter, sample, x, height)
            x += width + gap

    def _font(self, painter: QPainter, size: float, bold: bool = False) -> None:
        font = QFont(self.font())
        font.setPointSizeF(size)
        font.setBold(bold)
        painter.setFont(font)

    def _speed(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> float:
        speed = abs(sample.vehicle_speed_mps or 0.0) * 3.6
        radius = 25
        center = QPointF(x + radius, height / 2 + 4)
        rect = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.setPen(QPen(DIM, 5, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, 210 * 16, -240 * 16)
        fraction = min(1.0, speed / MAX_SPEED_KMH)
        color = GREEN if speed < 60 else AMBER if speed < 100 else RED
        painter.setPen(QPen(color, 5, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, 210 * 16, -round(240 * 16 * fraction))
        painter.setPen(TEXT)
        self._font(painter, 15, True)
        painter.drawText(QRectF(center.x() - radius, center.y() - 17, radius * 2, 24), Qt.AlignCenter, f"{speed:.0f}")
        painter.setPen(MUTED)
        self._font(painter, 8)
        painter.drawText(QRectF(center.x() - radius, center.y() + 6, radius * 2, 14), Qt.AlignCenter, "km/h")
        return x + radius * 2

    def _gear(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> float:
        self._font(painter, 13, True)
        for letter, code in GEARS:
            active = sample.gear_state == code
            color = (RED if code == 2 else ACCENT) if active else DIM
            if active:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), 45))
                painter.drawRoundedRect(QRectF(x - 1, height / 2 - 13, 19, 26), 5, 5)
            painter.setPen(color if active else DIM)
            painter.drawText(QRectF(x - 1, height / 2 - 13, 19, 26), Qt.AlignCenter, letter)
            x += 19
        return x

    def _arrow(self, painter: QPainter, center: QPointF, left: bool, on: bool) -> None:
        lit = on and int(time.monotonic() / 0.33) % 2 == 0
        painter.setPen(Qt.NoPen)
        painter.setBrush(GREEN if lit else DIM)
        d = -1 if left else 1
        painter.drawPolygon(QPolygonF([
            QPointF(center.x() + 11 * d, center.y()), QPointF(center.x(), center.y() - 9),
            QPointF(center.x(), center.y() - 4), QPointF(center.x() - 9 * d, center.y() - 4),
            QPointF(center.x() - 9 * d, center.y() + 4), QPointF(center.x(), center.y() + 4),
            QPointF(center.x(), center.y() + 9),
        ]))

    def _signals_and_wheel(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> float:
        middle = height / 2 - 6
        self._arrow(painter, QPointF(x + 10, middle), True, bool(sample.blinker_on_left))
        wheel = QPointF(x + 45, middle)
        angle = sample.steering_wheel_angle or 0.0
        painter.save()
        painter.translate(wheel)
        painter.rotate(angle)
        painter.setPen(QPen(TEXT, 3))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 15, 15)
        painter.setPen(QPen(TEXT, 3, Qt.SolidLine, Qt.RoundCap))
        for spoke in (90, 210, 330):
            rad = math.radians(spoke)
            painter.drawLine(QPointF(0, 0), QPointF(13 * math.cos(rad), 13 * math.sin(rad)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(ACCENT)
        painter.drawEllipse(QPointF(0, -15), 3, 3)   # top marker shows the turn
        painter.restore()
        self._arrow(painter, QPointF(x + 80, middle), False, bool(sample.blinker_on_right))
        painter.setPen(TEXT)
        self._font(painter, 10, True)
        painter.drawText(QRectF(wheel.x() - 40, height - 19, 80, 17), Qt.AlignCenter, f"{angle:+.0f}°")
        return x + 90

    def _pedals(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> float:
        """Brake on the left and accelerator on the right, as in the car."""
        top, bottom = 7, height - 22
        braking = bool(sample.brake_applied)
        painter.setPen(Qt.NoPen)
        painter.setBrush(RED if braking else DIM)
        painter.drawRoundedRect(QRectF(x, top, 22, bottom - top), 4, 4)
        painter.setPen(TEXT if braking else MUTED)
        self._font(painter, 9, True)
        painter.drawText(QRectF(x, top, 22, bottom - top), Qt.AlignCenter, "B")
        pedal = max(0.0, min(100.0, sample.accelerator_pedal_position or 0.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(DIM)
        painter.drawRoundedRect(QRectF(x + 30, top, 10, bottom - top), 3, 3)
        filled = (bottom - top) * pedal / 100
        painter.setBrush(GREEN)
        painter.drawRoundedRect(QRectF(x + 30, bottom - filled, 10, filled), 3, 3)
        painter.setPen(TEXT)
        self._font(painter, 10, True)
        painter.drawText(QRectF(x - 8, height - 19, 66, 17), Qt.AlignCenter, f"{pedal:.0f}%")
        return x + 50

    def _g_meter(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> float:
        radius = 18
        center = QPointF(x + 24, height / 2 - 7)
        painter.setPen(QPen(DIM, 1.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, radius, radius)
        painter.drawEllipse(center, radius / 2, radius / 2)
        painter.drawLine(QPointF(center.x() - radius, center.y()), QPointF(center.x() + radius, center.y()))
        painter.drawLine(QPointF(center.x(), center.y() - radius), QPointF(center.x(), center.y() + radius))
        # x: forward (+ accelerating), y: lateral; 0.5 g at the rim.
        forward = (sample.linear_acceleration_mps2_x or 0.0) / 9.81
        lateral = (sample.linear_acceleration_mps2_y or 0.0) / 9.81
        scale = radius / 0.5
        dx = max(-radius, min(radius, lateral * scale))
        dy = max(-radius, min(radius, -forward * scale))
        painter.setPen(Qt.NoPen)
        painter.setBrush(AMBER)
        painter.drawEllipse(QPointF(center.x() + dx, center.y() + dy), 4.5, 4.5)
        painter.setPen(TEXT)
        self._font(painter, 10, True)
        g = math.hypot(forward, lateral)
        painter.drawText(QRectF(center.x() - 34, height - 19, 68, 17), Qt.AlignCenter, f"{g:.2f}G")
        return x + 48

    def _autopilot(self, painter: QPainter, sample: TelemetrySample, x: float, height: float) -> None:
        label = AUTOPILOT.get(sample.autopilot_state or 0)
        active = label is not None
        text = label or "수동 운전"
        self._font(painter, 9, True)
        width = painter.fontMetrics().horizontalAdvance(text) + 22
        rect = QRectF(x, height / 2 - 12, width, 24)
        painter.setPen(QPen(ACCENT if active else DIM, 1.5))
        painter.setBrush(QColor(89, 181, 255, 50) if active else Qt.NoBrush)
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(ACCENT if active else MUTED)
        painter.drawText(rect, Qt.AlignCenter, text)
