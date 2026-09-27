"""A lightweight slippy map drawn with Qt (no web engine).

Raster tiles in the standard Web Mercator z/x/y scheme are downloaded with
Qt's network stack, cached on disk, and painted together with the clip routes
and a heading arrow. Same interface as the former Leaflet map:
``set_group_routes``, ``set_position`` and the ``group_clicked`` signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QWidget

TILE = 256
HOME = (37.5665, 126.9780)  # Seoul
HOME_ZOOM = 12
MIN_ZOOM, FIT_MAX_ZOOM = 5, 17
CURRENT_ROUTE = QColor("#e53935")
OTHER_ROUTE = QColor("#1f6bff")
ARROW = QColor("#ff6a13")


@dataclass(frozen=True)
class TileProvider:
    name: str
    url: str            # with {z} {x} {y} (and {key})
    attribution: str
    max_zoom: int = 19
    key: str = ""

    def tile_url(self, z: int, x: int, y: int) -> str:
        return self.url.format(z=z, x=x, y=y, key=self.key)


def vworld(key: str) -> TileProvider:
    """브이월드 (국토교통부 공간정보 오픈플랫폼): free Korean base map, needs an API key."""
    return TileProvider("브이월드", "https://api.vworld.kr/req/wmts/1.0.0/{key}/Base/{z}/{y}/{x}.png",
                        "© 국토교통부 브이월드", 19, key)


def openstreetmap() -> TileProvider:
    """OSM's own servers: fine for testing; public apps need permission (tile usage policy)."""
    return TileProvider("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                        "© OpenStreetMap contributors", 19)


def to_world(lat: float, lon: float) -> tuple[float, float]:
    """Web Mercator in 0..1 (x east, y south)."""
    lat = max(-85.05112878, min(85.05112878, lat))
    x = (lon + 180.0) / 360.0
    sin = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)
    return x, y


def to_latlon(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lat, lon


class TileMapWidget(QWidget):
    group_clicked = Signal(str, int)

    def __init__(self, provider: TileProvider, cache_dir: str | None = None,
                 user_agent: str = "T6Viewer", parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumWidth(300)
        self.setMouseTracking(True)
        self.provider = provider
        self.zoom = HOME_ZOOM
        self.center = to_world(*HOME)
        self._routes: dict[str, list[tuple[float, float, int]]] = {}
        self._bounds: dict[str, tuple[float, float, float, float]] = {}
        self._selected: str | None = None
        self._position: tuple[float, float, int, float | None] | None = None
        self._message = "영상 위치를 읽는 중..."
        self._tiles: dict[tuple[int, int, int], QPixmap] = {}
        self._pending: dict[tuple[int, int, int], QNetworkReply] = {}
        self._failed: set[tuple[int, int, int]] = set()
        self._base: QPixmap | None = None      # tiles + routes, redrawn only on change
        self._paths: dict[tuple[str, int], QPainterPath] = {}   # route paths per zoom
        self._drag: QPointF | None = None
        self._drag_moved = False
        self._user_agent = user_agent.encode()
        self._network = QNetworkAccessManager(self)
        if cache_dir:
            cache = QNetworkDiskCache(self)
            cache.setCacheDirectory(cache_dir)
            cache.setMaximumCacheSize(300 * 1024 * 1024)
            self._network.setCache(cache)

    # -- public interface (same as the former web map) -----------------------
    def set_provider(self, provider: TileProvider) -> None:
        if provider != self.provider:
            self.provider = provider
            for reply in self._pending.values():
                reply.abort()
            self._pending.clear()
            self._tiles.clear()
            self._failed.clear()
            self._invalidate()

    def set_group_routes(self, routes: dict[str, list[list[float]]], selected: str | None = None,
                         current: list[float] | None = None, focus: bool = False) -> None:
        converted = {}
        for key, points in routes.items():
            if key in self._routes and self._routes.get(key) and len(self._routes[key]) == len(points):
                converted[key] = self._routes[key]
                continue
            converted[key] = [(*to_world(point[0], point[1]), int(point[2]) if len(point) > 2 else 0)
                              for point in points]
        if set(converted) != set(self._routes) or any(converted[k] is not self._routes.get(k) for k in converted):
            self._paths.clear()
        self._routes = converted
        self._bounds = {key: (min(p[0] for p in points), min(p[1] for p in points),
                              max(p[0] for p in points), max(p[1] for p in points))
                        for key, points in converted.items() if points}
        self._selected = selected
        selected_points = converted.get(selected) or []
        if selected_points:
            self._message = ""
            if focus:
                self._fit(self._bounds[selected])
        elif selected:
            # No location for this clip (e.g. parked recording): back to the
            # start-up view so an old position is not mistaken for it.
            if focus:
                self.center, self.zoom = to_world(*HOME), HOME_ZOOM
            self._message = "현재 영상에는 위치 정보가 없습니다 (주차 중 녹화 등)."
        else:
            self._message = "" if converted else "표시할 GPS 텔레메트리가 없습니다."
        self.set_position(current)
        self._invalidate()

    def set_position(self, current: list[float] | None) -> None:
        if not current:
            new = None
        else:
            heading = current[3] if len(current) > 3 else None
            new = (*to_world(current[0], current[1]), int(current[2]) if len(current) > 2 else 0, heading)
        if new != self._position:
            self._position = new
            self.update()

    # -- geometry --------------------------------------------------------------
    def _scale(self) -> float:
        return TILE * (2 ** self.zoom)

    def _to_screen(self, x: float, y: float) -> QPointF:
        scale = self._scale()
        return QPointF((x - self.center[0]) * scale + self.width() / 2,
                       (y - self.center[1]) * scale + self.height() / 2)

    def _to_world(self, point: QPointF) -> tuple[float, float]:
        scale = self._scale()
        return (self.center[0] + (point.x() - self.width() / 2) / scale,
                self.center[1] + (point.y() - self.height() / 2) / scale)

    def _fit(self, bounds: tuple[float, float, float, float]) -> None:
        left, top, right, bottom = bounds
        self.center = ((left + right) / 2, (top + bottom) / 2)
        width, height = max(1, self.width() - 48), max(1, self.height() - 48)
        zoom = FIT_MAX_ZOOM
        while zoom > MIN_ZOOM and ((right - left) * TILE * 2 ** zoom > width
                                   or (bottom - top) * TILE * 2 ** zoom > height):
            zoom -= 1
        self.zoom = min(zoom, self.provider.max_zoom)

    def _set_zoom(self, zoom: int, anchor: QPointF | None = None) -> None:
        zoom = max(MIN_ZOOM, min(self.provider.max_zoom, zoom))
        if zoom == self.zoom:
            return
        anchor = anchor or QPointF(self.width() / 2, self.height() / 2)
        world = self._to_world(anchor)
        self.zoom = zoom
        after = self._to_world(anchor)
        self.center = (self.center[0] + world[0] - after[0], self.center[1] + world[1] - after[1])
        self._invalidate()

    # -- tiles -----------------------------------------------------------------
    def _visible_tiles(self) -> list[tuple[int, int, int]]:
        count = 2 ** self.zoom
        top_left = self._to_world(QPointF(0, 0))
        bottom_right = self._to_world(QPointF(self.width(), self.height()))
        x0, y0 = int(math.floor(top_left[0] * count)), int(math.floor(top_left[1] * count))
        x1, y1 = int(math.floor(bottom_right[0] * count)), int(math.floor(bottom_right[1] * count))
        return [(self.zoom, x, y) for y in range(max(0, y0), min(count - 1, y1) + 1)
                for x in range(x0, x1 + 1)]

    def _request(self, key: tuple[int, int, int]) -> None:
        if key in self._pending or key in self._failed:
            return
        z, x, y = key
        count = 2 ** z
        request = QNetworkRequest(QUrl(self.provider.tile_url(z, x % count, y)))
        request.setRawHeader(b"User-Agent", self._user_agent)
        request.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferCache)
        reply = self._network.get(request)
        self._pending[key] = reply
        reply.finished.connect(lambda k=key, r=reply: self._tile_arrived(k, r))

    def _tile_arrived(self, key: tuple[int, int, int], reply: QNetworkReply) -> None:
        self._pending.pop(key, None)
        if reply.error() == QNetworkReply.NoError:
            pixmap = QPixmap()
            if pixmap.loadFromData(bytes(reply.readAll())):
                self._tiles[key] = pixmap
                if len(self._tiles) > 400:  # keep memory small
                    for old in list(self._tiles)[:100]:
                        del self._tiles[old]
                self._invalidate()
        elif reply.error() != QNetworkReply.OperationCanceledError:
            self._failed.add(key)
            if not self.provider.key and "{key}" in self.provider.url:
                self._message = "브이월드 API 키가 없습니다. 설정에서 키를 입력하세요."
        reply.deleteLater()

    # -- painting ----------------------------------------------------------------
    def _invalidate(self) -> None:
        self._base = None
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._base = None

    def paintEvent(self, event) -> None:
        # The arrow moves many times a second while a clip plays; everything
        # under it is drawn once into a cached image and only redrawn when the
        # view, tiles or routes change, so the GUI thread stays free for video.
        if self._base is None or self._base.size() != self.size() * self.devicePixelRatioF():
            self._base = self._render_base()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._base)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_arrow(painter)
        self._paint_overlay(painter)

    def _render_base(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        base = QPixmap(self.size() * ratio)
        base.setDevicePixelRatio(ratio)
        painter = QPainter(base)
        painter.fillRect(QRectF(0, 0, self.width(), self.height()), QColor("#dfe3e6"))
        visible = self._visible_tiles()
        wanted = set(visible)
        for key, reply in list(self._pending.items()):
            if key not in wanted:
                reply.abort()
        for key in visible:
            z, x, y = key
            pixmap = self._tiles.get(key)
            corner = self._to_screen(x / 2 ** z, y / 2 ** z)
            if pixmap is None:
                self._request(key)
                pixmap = self._parent_tile(key)
                if pixmap is None:
                    continue
                source, pixmap = pixmap
                painter.drawPixmap(QRectF(corner.x(), corner.y(), TILE, TILE), pixmap, source)
                continue
            painter.drawPixmap(int(round(corner.x())), int(round(corner.y())), pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_routes(painter)
        painter.end()
        return base

    def _parent_tile(self, key: tuple[int, int, int]):
        """While a tile loads, stretch the already loaded lower-zoom tile."""
        z, x, y = key
        for up in range(1, 4):
            parent = (z - up, x >> up, y >> up)
            pixmap = self._tiles.get(parent)
            if pixmap is not None:
                size = TILE / 2 ** up
                return QRectF((x - (parent[1] << up)) * size, (y - (parent[2] << up)) * size, size, size), pixmap
        return None

    def _paint_routes(self, painter: QPainter) -> None:
        view = QRectF(self.rect()).adjusted(-20, -20, 20, 20)
        ordered = [key for key in self._routes if key != self._selected]
        if self._selected in self._routes:
            ordered.append(self._selected)  # current route on top
        for key in ordered:
            points = self._routes[key]
            bounds = self._bounds.get(key)
            if not points or bounds is None:
                continue
            top_left, bottom_right = self._to_screen(bounds[0], bounds[1]), self._to_screen(bounds[2], bounds[3])
            if not view.intersects(QRectF(top_left, bottom_right).normalized().adjusted(-8, -8, 8, 8)):
                continue
            current = key == self._selected
            color = CURRENT_ROUTE if current else OTHER_ROUTE
            if len(points) == 1:
                center = self._to_screen(points[0][0], points[0][1])
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.setBrush(color)
                painter.drawEllipse(center, 7 if current else 5, 7 if current else 5)
                continue
            path = self._paths.get((key, self.zoom))
            if path is None:
                scale = self._scale()
                path = QPainterPath(QPointF(points[0][0] * scale, points[0][1] * scale))
                for x, y, _ms in points[1:]:
                    path.lineTo(QPointF(x * scale, y * scale))
                self._paths[(key, self.zoom)] = path
            origin = self._to_screen(0.0, 0.0)
            painter.save()
            painter.translate(origin)
            painter.setBrush(Qt.NoBrush)
            if current:
                painter.setPen(QPen(QColor(255, 255, 255, 220), 10, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                painter.drawPath(path)
            painter.setPen(QPen(color, 6 if current else 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
            painter.restore()

    def _paint_arrow(self, painter: QPainter) -> None:
        if self._position is None:
            return
        x, y, _ms, heading = self._position
        center = self._to_screen(x, y)
        painter.save()
        painter.translate(center)
        painter.rotate(heading or 0.0)
        shape = QPolygonF([QPointF(0, -15), QPointF(11, 13), QPointF(0, 7), QPointF(-11, 13)])
        painter.setPen(QPen(QColor("#ffffff"), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(ARROW)
        painter.drawPolygon(shape)
        painter.restore()

    def _paint_overlay(self, painter: QPainter) -> None:
        font = QFont(self.font())
        font.setPointSize(8)
        painter.setFont(font)
        text = self.provider.attribution
        metrics = painter.fontMetrics()
        box = QRectF(self.width() - metrics.horizontalAdvance(text) - 12, self.height() - 18,
                     metrics.horizontalAdvance(text) + 12, 18)
        painter.fillRect(box, QColor(255, 255, 255, 200))
        painter.setPen(QColor("#333333"))
        painter.drawText(box, Qt.AlignCenter, text)
        for index, label in enumerate(("+", "−")):
            rect = self._zoom_button(index)
            painter.setPen(QPen(QColor("#9aa4ad"), 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(rect, 4, 4)
            font.setPointSize(13)
            painter.setFont(font)
            painter.setPen(QColor("#222222"))
            painter.drawText(rect, Qt.AlignCenter, label)
        if self._message:
            font.setPointSize(10)
            painter.setFont(font)
            box = QRectF(46, 10, max(80, self.width() - 56), 32)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(16, 19, 24, 225))
            painter.drawRoundedRect(box, 5, 5)
            painter.setPen(QColor("#dce4ed"))
            painter.drawText(box, Qt.AlignCenter | Qt.TextWordWrap, self._message)

    @staticmethod
    def _zoom_button(index: int) -> QRectF:
        return QRectF(10, 10 + index * 32, 28, 28)

    # -- interaction -------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            for index, step in enumerate((1, -1)):
                if self._zoom_button(index).contains(event.position()):
                    self._set_zoom(self.zoom + step)
                    return
            self._drag = event.position()
            self._drag_moved = False

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            delta = event.position() - self._drag
            if abs(delta.x()) + abs(delta.y()) > 3:
                self._drag_moved = True
            scale = self._scale()
            self.center = (self.center[0] - delta.x() / scale, self.center[1] - delta.y() / scale)
            self._drag = event.position()
            self._invalidate()
        else:
            self.setCursor(Qt.PointingHandCursor if self._hit(event.position()) else Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag is not None:
            if not self._drag_moved:
                hit = self._hit(event.position())
                if hit is not None:
                    self.group_clicked.emit(*hit)
            self._drag = None

    def mouseDoubleClickEvent(self, event) -> None:
        self._set_zoom(self.zoom + 1, event.position())

    def wheelEvent(self, event) -> None:
        step = 1 if event.angleDelta().y() > 0 else -1
        self._set_zoom(self.zoom + step, event.position())

    def _hit(self, point: QPointF) -> tuple[str, int] | None:
        """Route (and the nearest recorded moment on it) under the cursor."""
        if self._position is not None and self._selected:
            arrow = self._to_screen(self._position[0], self._position[1])
            if (arrow - point).manhattanLength() < 16:
                return self._selected, self._position[2]
        best = None
        wx, wy = self._to_world(point)
        slack = 10 / self._scale()
        for key, points in self._routes.items():
            left, top, right, bottom = self._bounds.get(key, (1, 1, 0, 0))
            if not (left - slack <= wx <= right + slack and top - slack <= wy <= bottom + slack):
                continue
            for x, y, ms in points:
                screen = self._to_screen(x, y)
                distance = math.hypot(screen.x() - point.x(), screen.y() - point.y())
                if distance < 10 and (best is None or distance < best[0]):
                    best = (distance, key, ms)
        return (best[1], best[2]) if best else None

    def sizeHint(self) -> QSize:
        return QSize(380, 400)
