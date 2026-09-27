"""PySide6 desktop UI for decrypting and reviewing six Tesla cameras."""

from __future__ import annotations

import sys
import shutil
import threading
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
import struct
import time
from pathlib import Path

import av

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QCheckBox,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QSlider,
    QSizePolicy,
    QStyle,
    QStyleOptionSlider,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .api import fetch_keys
from .crypto import (
    CAMERAS,
    CAMERA_LABELS,
    ClipGroup,
    TeslaEvent,
    archive_encrypted_outputs,
    delete_verified_encrypted_source,
    discover_clips,
    discover_events,
    discover_groups,
    decrypt_file,
    is_plain_json,
    is_plain_mp4,
    read_file_header,
    teslacam_root,
)
from .telemetry import (
    TelemetrySample,
    autopilot_label,
    gear_label,
    gps_samples,
    nearest_sample,
    vehicle_motion_label,
)
from . import video_decode
from . import PROJECT_URL, __version__
from .app_paths import data_dir, data_file
from .drive_hud import DriveHud, is_driving
from .tile_map import TileMapWidget, TileProvider, openstreetmap, vworld
from .analysis_jobs import OBJECTS_CACHE_KIND
from .analysis_pool import AnalysisPool, Job
from .derived_cache import (
    CACHE_DIR_NAME,
    LEGACY_CACHE_DIR_NAMES,
    cache_listing,
    load_image,
    load_result,
    load_telemetry,
    save_image,
)
from .watch_progress import add_interval, coverage
from .memory_budget import (
    MIB,
    PRELOAD_ESTIMATE,
    WORKER_ESTIMATE,
    critical as memory_critical,
    excess_bytes as memory_excess,
    recovered as memory_recovered,
    system_low as memory_system_low,
    headroom as memory_headroom,
    memory_snapshot,
    over_budget,
    start_sampler as start_memory_sampler,
    playback_reserve,
    preload_slots,
    worker_slots,
)
from .motion_scan import (
    CACHE_VERSION,
    OBJECT_ICONS,
    adaptive_rate,
    analysis_group_order,
    category_totals,
    change_runs,
    inside_change,
    merge_runs,
    motion_known,
    next_change_ms,
)
from .ground_detector import GROUP_ORDER, motion_for_categories
from . import crash_log, window_style
from .app_settings import load_settings, save_settings
from .ui_widgets import AboutDialog, ChecklistPopup, CollapsibleSplitter, DateTimePopup, SettingsDialog


STORYBOARD_CELL = (96, 54)
COMPACT_BUTTON = "QPushButton { padding: 5px 7px; }"
PLAY_BUTTON_WIDTH = 50
EMPTY_LIST_TEXT = "📂 TeslaCam 폴더를 열거나" + chr(10) + "이 창에 폴더를 끌어 놓으세요."
EMPTY_FILTER_TEXT = "필터 조건에 맞는 장면이 없습니다." + chr(10) + "열 제목을 눌러 필터를 바꿔 보세요."
LIST_COLUMNS = ("일시", "복호", "폴더", "주행", "Event", "분석")
WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")
# Peak memory of decoding one full-resolution clip for a storyboard or preview.
DECODE_ESTIMATE = 128 * MIB
# Next-clip channels are opened in this order while memory allows.
PRELOAD_ORDER = ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar")
PRELOAD_TIMEOUT = 5.0
# Near the start and end of every clip the rate is held to at most 8x.
EDGE_FRACTION = 0.2
EDGE_MAX_RATE = 8.0
# Above this rate the user is skimming: background analysis steps back
# (Settings.analysis_fast) and unfinished storyboards wait.
FAST_PLAYBACK_RATE = 2.0
# Playback is capped at Settings.max_decode_load = channels x rate (see
# app_settings for the measurements behind the default of 24). Opening or
# starting preloaded decoders is safe up to this load (measured flat at 48,
# explosive at 72), so preload is skipped if the user sets a higher ceiling.
PRELOAD_MAX_DECODE_LOAD = 48
POSITION_UI_INTERVAL = 1 / 15
ANALYSIS_SPAWN_STEP = 2


STORYBOARD_TIP = ("스토리보드: 영상을 {count}구간으로 나눈 대표 장면입니다. 클릭하면 해당 시점부터 재생합니다.\n"
                  "아래 주황색 막대와 아이콘은 인식 대상이 실제로 움직인 구간입니다.")


CAMERA_DISPLAY_ORDER = (
    "left_pillar", "front", "right_pillar",
    "left_repeater", "back", "right_repeater",
)


EVENT_JSON_NOTE = (
    "event.json은 영상 복호화 키로 복호화되지 않으므로 복호화 대상에서 제외합니다. "
    "차량에서 암호화하지 않은 event.json만 그대로 복사해 이벤트 정보로 사용합니다."
)


class ClickSeekSlider(QSlider):
    clicked_value = Signal(int)

    def __init__(self, *args):
        super().__init__(*args)
        self._marks: list[tuple[int, int]] = []

    def set_marks(self, marks: list[tuple[int, int]]) -> None:
        """Highlight detected-change ranges (ms) on the groove."""
        if marks != self._marks:
            self._marks = marks
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._marks or self.maximum() <= self.minimum():
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        span = self.maximum() - self.minimum()
        painter = QPainter(self)
        for start, end in self._marks:
            left = groove.x() + int((start - self.minimum()) / span * groove.width())
            right = groove.x() + int((end - self.minimum()) / span * groove.width())
            painter.fillRect(left, groove.center().y() + 3, max(3, right - left), 4, QColor("#ffb547"))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self.orientation() != Qt.Horizontal:
            super().mousePressEvent(event)
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self)
        if handle.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        span = max(1, groove.width() - handle.width())
        offset = round(event.position().x() - groove.x() - handle.width() / 2)
        value = QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), offset, span, option.upsideDown
        )
        self.setValue(value)
        self.clicked_value.emit(value)
        event.accept()


class StoryboardStrip(QWidget):
    """A twenty-frame contact sheet shown as twenty or ten time segments."""

    seek_requested = Signal(int)
    SOURCE_SEGMENTS = 20

    def __init__(self, parent: QWidget | None = None, height: int = 62):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.camera = "front"
        self.duration_ms = 0
        self.position_ms = 0
        self.image = QPixmap()
        self.segments: dict[int, QPixmap] = {}
        self.changes: list[dict[str, object]] = []
        self.has_clip = False
        self.segment_count = 10
        self.setToolTip(STORYBOARD_TIP.format(count=20))

    def set_segment_count(self, count: int) -> None:
        if count not in (10, 20):
            raise ValueError("Storyboard segment count must be 10 or 20")
        self.segment_count = count
        self.setToolTip(STORYBOARD_TIP.format(count=count))
        self.update()

    def set_storyboard(self, camera: str, image_bytes: bytes | None, duration_ms: int = 0) -> None:
        self.camera = camera
        self.duration_ms = max(0, duration_ms)
        image = QPixmap()
        if image_bytes:
            image.loadFromData(image_bytes)
        self.image = image
        self.segments: dict[int, QPixmap] = {}
        self.changes = [] if image_bytes is None else self.changes
        self.update()

    def set_segment(self, index: int, image_bytes: bytes, duration_ms: int) -> None:
        """Show one of the twenty frames before the whole sheet is ready."""
        if not self.image.isNull():
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image_bytes):
            self.duration_ms = max(0, duration_ms)
            self.segments[index] = pixmap
            self.update()

    def set_position(self, position_ms: int) -> None:
        if abs(position_ms - self.position_ms) >= 250:
            self.position_ms = position_ms
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101318"))
        if self.image.isNull() and not self.segments:
            if self.has_clip:
                painter.setPen(QColor("#acb9c6"))
                painter.drawText(self.rect(), Qt.AlignCenter, "스토리보드 준비 중…")
            return
        cell_width = self.width() / self.segment_count
        source_width = self.image.width() / self.SOURCE_SEGMENTS
        for index in range(self.segment_count):
            source_index = index * self.SOURCE_SEGMENTS // self.segment_count
            left = int(index * cell_width)
            width = int((index + 1) * cell_width) - left
            if not self.image.isNull():
                painter.drawPixmap(
                    left, 0, width, self.height(), self.image,
                    int(source_index * source_width), 0,
                    int((source_index + 1) * source_width) - int(source_index * source_width), self.image.height(),
                )
            elif source_index in self.segments:
                painter.drawPixmap(left, 0, width, self.height(), self.segments[source_index])
            painter.fillRect(left, 0, 1, self.height(), QColor("#475668"))
        if self.duration_ms and self.changes:
            # Detected moving objects: an amber band plus the main object icon.
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            for run in self.changes:
                left = int(run["start"] / self.duration_ms * self.width())
                right = int(run["end"] / self.duration_ms * self.width())
                painter.fillRect(left, self.height() - 5, max(3, right - left), 5, QColor("#ffb547"))
                icons = "".join(OBJECT_ICONS[name] for name in category_totals([run])[:2])
                painter.fillRect(left, 0, 2 + 14 * len(icons) // 2, 15, QColor(16, 19, 24, 170))
                painter.setPen(QColor("#ffffff"))
                painter.drawText(left + 1, 12, icons)
        if self.duration_ms:
            marker_x = min(self.width() - 2, int(self.position_ms / self.duration_ms * self.width()))
            painter.fillRect(max(0, marker_x), 0, 2, self.height(), QColor("#ff5b5b"))

    def set_changes(self, runs: list[dict[str, object]], duration_ms: int = 0) -> None:
        self.changes = runs
        if duration_ms and not self.duration_ms:
            self.duration_ms = duration_ms
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.duration_ms and (not self.image.isNull() or self.segments):
            segment = min(self.segment_count - 1, max(0, int(event.position().x() / max(1, self.width()) * self.segment_count)))
            self.seek_requested.emit(round(segment * self.duration_ms / self.segment_count))
            event.accept()
            return
        super().mousePressEvent(event)


class VideoTile(QWidget):
    double_clicked = Signal(str)
    thumbnail_clicked = Signal(str)
    storyboard_seek_requested = Signal(str, int)

    def __init__(self, camera: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.camera = camera
        self.player = QMediaPlayer(self)
        self.video = QVideoWidget(self)
        self.video.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)
        self.thumbnail = QLabel("")
        self.thumbnail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setStyleSheet("background: #050608; color: #9baab8;")
        self._thumbnail_image = None
        self._motion_level = "unknown"
        self.path: Path | None = None
        self.display = QStackedWidget()
        self.display.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.display.addWidget(self.thumbnail)
        self.display.addWidget(self.video)
        self.video.installEventFilter(self)
        self.thumbnail.installEventFilter(self)
        self.title = QLabel(f"{CAMERA_LABELS[camera]} (no clip)")
        self.title.setObjectName("cameraTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setMinimumHeight(25)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(self.title)
        layout.addWidget(self.display, 1)
        self.storyboard = StoryboardStrip(self, height=38)
        self.storyboard.seek_requested.connect(
            lambda position: self.storyboard_seek_requested.emit(self.camera, position)
        )
        layout.addWidget(self.storyboard)
        self.player.setVideoOutput(self.video)

    def set_clip(self, path: Path | None, preview: bool = True,
                 prepared_player: QMediaPlayer | None = None) -> QMediaPlayer | None:
        """Show ``path``; with ``prepared_player`` return the replaced player.

        The caller must dispose the returned player. Tearing a decoder down
        blocks ~40-60 ms, so it is done later, one at a time, instead of in
        the middle of the clip switch.
        """
        old_player = None
        if prepared_player is not None:
            # Keep the previous clip's last frame on screen until the
            # already-opened decoder delivers its next frame: no black flash.
            old_player = self.player
            old_player.pause()
            old_player.setVideoOutput(None)
            self.player = prepared_player
            prepared_player.setParent(self)
            prepared_player.setVideoOutput(self.video)
        else:
            self.release_video()
        self.path = path
        self.set_motion_level("unknown")
        self.storyboard.has_clip = path is not None
        self.storyboard.set_storyboard(self.camera, None)
        self._thumbnail_image = None
        self.thumbnail.setPixmap(QPixmap())
        self.thumbnail.setText("썸네일 준비 중…" if path else "영상 없음")
        self.display.setCurrentWidget(self.thumbnail if preview else self.video)
        if path is not None and not preview:
            self.show_video()
        return old_player

    def release_video(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.video.videoSink().setVideoFrame(QVideoFrame())

    def set_motion_level(self, level: str, icons: str = "") -> None:
        self._motion_level = level
        label = CAMERA_LABELS[self.camera]
        if self.path is None:
            self.title.setText(f"{label} (no clip)")
            return
        if level == "off":  # object recognition turned off in the settings
            self.title.setText(label)
            self.title.setToolTip("")
            return
        color, comment = {
            "quiet": ("#48cf76", "변화 없음"),
            "moderate": ("#ffb547", "변화 미미함"),
            "high": ("#ff5b5b", "변화 많음"),
            "driving": ("#8795a3", "주행영상/분석대상아님"),
            "undetermined": ("#8795a3", "판정 불가"),
        }.get(level, ("#8795a3", "분석 중"))
        suffix = f" {icons}" if icons else ""
        self.title.setText(f'{label} <span style="color:{color}">●</span> ({comment}){suffix}')
        self.title.setToolTip("사람·차량 등 지면 객체의 움직임을 추정합니다. 미검출은 움직임이 없다는 보장이 아닙니다.")

    def set_thumbnail_bytes(self, image_bytes: bytes) -> None:
        image = QPixmap()
        if image.loadFromData(image_bytes):
            self._thumbnail_image = image
            self._show_thumbnail()

    def _show_thumbnail(self) -> None:
        if self._thumbnail_image is not None:
            self.thumbnail.setPixmap(self._thumbnail_image.scaled(
                self.thumbnail.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._show_thumbnail()

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.video, self.thumbnail) and event.type() == QEvent.MouseButtonDblClick:
            self.double_clicked.emit(self.camera)
            return True
        if (watched is self.thumbnail and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton):
            self.thumbnail_clicked.emit(self.camera)
            return True
        return super().eventFilter(watched, event)

    def has_source(self, path: Path | None) -> bool:
        # QUrl.toLocalFile() uses forward slashes on Windows; compare paths,
        # not strings, or an opened (or preloaded) decoder is reopened.
        local = self.player.source().toLocalFile()
        return bool(path is not None and local and Path(local) == path)

    def show_video(self) -> None:
        if self.path is not None and not self.has_source(self.path):
            self.player.setSource(QUrl.fromLocalFile(str(self.path)))
        self.display.setCurrentWidget(self.video)

    def show_thumbnail(self) -> None:
        self.release_video()
        self.display.setCurrentWidget(self.thumbnail)


class DecryptWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str, int, int, object)

    def __init__(self, source_root: Path, output_root: Path, token: str, delete_source: bool):
        super().__init__()
        self.source_root = source_root
        self.output_root = output_root
        self.token = token
        self.delete_source = delete_source

    @Slot()
    def run(self) -> None:
        errors: list[str] = []
        all_clips = discover_clips(self.source_root)
        encrypted = [
            clip for clip in all_clips if clip.encrypted
            and not is_plain_mp4(self.output_root / clip.path.relative_to(self.source_root))
        ]
        if not encrypted:
            self.finished.emit("복호화할 암호화 영상이 없습니다.", 0, 0, errors)
            return
        self.progress.emit(0, EVENT_JSON_NOTE)
        # Keep the six-camera groups complete when a drive contains a mixture
        # of plain and encrypted clips from the same recording minute.
        for clip in all_clips:
            if clip.encrypted:
                continue
            destination = self.output_root / clip.path.relative_to(self.source_root)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(clip.path, destination)
            except OSError as exc:
                errors.append(f"{clip.path.name}: 일반 영상 복사 실패 ({exc})")
        # event.json is not decrypted: Tesla's per-file video keys do not
        # apply to it. Only files the car left unencrypted are usable; copy
        # those so event information survives in the output. Encrypted ones
        # are skipped silently and left where they are.
        for sidecar in self.source_root.rglob("*.json"):
            if sidecar.name.lower() not in {"event.json", "events.json"} or not is_plain_json(sidecar):
                continue
            destination = self.output_root / sidecar.relative_to(self.source_root)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(sidecar, destination)
            except OSError as exc:
                errors.append(f"{sidecar.name}: 이벤트 정보 복사 실패 ({exc})")
        headers: list[tuple[dict, Path]] = []
        for index, clip in enumerate(encrypted):
            try:
                headers.append((read_file_header(clip.path), clip.path))
            except Exception as exc:  # a damaged clip should not abort others
                errors.append(f"{clip.path.name}: 헤더 읽기 실패 ({exc})")
            self.progress.emit(round((index + 1) * 20 / len(encrypted)), f"헤더 확인 {index + 1}/{len(encrypted)}")

        def key_progress(done: int, total: int) -> None:
            self.progress.emit(20 + round(done * 20 / max(total, 1)), f"Tesla 영상 키 요청 {done}/{total} 배치")

        self.progress.emit(20, f"Tesla 복호화 키 요청 중: 영상 {len(headers)}개")
        try:
            keys = fetch_keys(self.token, [header for header, _path in headers], progress=key_progress) if headers else {}
        except Exception as exc:
            errors.append(f"Tesla 영상 키 요청 실패: {exc}")
            self.finished.emit("영상 키 요청 실패", 0, len(encrypted), errors)
            return
        self.progress.emit(40, f"영상 키 수신 완료: {len(keys)}개")
        success = 0
        missing_keys = 0
        for index, (header, source) in enumerate(headers, 1):
            key = keys.get(header["id"])
            destination = self.output_root / source.relative_to(self.source_root)
            if key is None:
                missing_keys += 1
            else:
                try:
                    decrypt_file(source, destination, key)
                    if self.delete_source:
                        delete_verified_encrypted_source(source, destination)
                    success += 1
                except Exception as exc:
                    errors.append(f"{source.name}: 복호화 실패 ({exc})")
            self.progress.emit(40 + round(index * 60 / max(len(headers), 1)), f"영상 복호화 {index}/{len(headers)}")
        if missing_keys:
            errors.append(f"영상 {missing_keys}개: Tesla가 복호화 키를 반환하지 않았습니다.")
        failed = len(encrypted) - success
        self.finished.emit(f"완료: 영상 {success}개 복호화, {failed}개 실패", success, failed, errors)


class AnalysisBridge(QObject):
    """Deliver process-pool results (from pool threads) to the GUI thread."""

    result = Signal(object, object)


class SelectedTelemetryWorker(QObject):
    finished = Signal(object, object)

    def __init__(self, group: ClipGroup):
        super().__init__()
        self.group = group

    @Slot()
    def run(self) -> None:
        clip = self.group.clips.get("front") or next(iter(self.group.clips.values()), None)
        samples: list[TelemetrySample] = []
        if clip is not None:
            try:
                samples = load_telemetry(clip.path)
            except (OSError, ValueError, struct.error):
                pass
        self.finished.emit(self.group, samples)


def _jpeg(rgb) -> bytes:
    """Encode an RGB numpy image as JPEG with Qt (usable off the GUI thread)."""
    height, width = rgb.shape[:2]
    image = QImage(rgb.data, width, height, width * 3, QImage.Format_RGB888)
    output = QByteArray()
    buffer = QBuffer(output)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPG", 85)
    return bytes(output.data())


class _ParallelDecodeWorker(QObject):
    """Decode several clips at once off the GUI thread (PyAV, bundled FFmpeg).

    The decoding runs in C with Python's lock released, so ``parallel``
    threads use several cores. The caller sizes ``parallel`` from the memory
    budget.
    """

    finished = Signal()

    def __init__(self, parallel: int = 1):
        super().__init__()
        self.parallel = max(1, parallel)
        self.stop_event = threading.Event()

    def _run_all(self, items: list, handler) -> None:
        def guarded(item) -> None:
            if self.stop_event.is_set():
                return
            try:
                handler(item)
            except (OSError, ValueError, IndexError, av.FFmpegError):
                pass
        try:
            if self.parallel == 1 or len(items) <= 1:
                for item in items:
                    guarded(item)
            else:
                with ThreadPoolExecutor(min(self.parallel, len(items))) as executor:
                    list(executor.map(guarded, items))
        finally:
            self.finished.emit()

    def stop(self) -> None:
        self.stop_event.set()


class ThumbnailWorker(_ParallelDecodeWorker):
    image_ready = Signal(str, str, object)

    def __init__(self, paths: dict[str, Path], parallel: int = 1):
        super().__init__(parallel)
        self.paths = paths

    @Slot()
    def run(self) -> None:
        def thumbnail(item: tuple[str, Path]) -> None:
            camera, path = item
            cached = load_image(path, "thumbnail")
            if cached is not None:
                self.image_ready.emit(camera, str(path), cached[0])
                return
            picture = video_decode.first_frame(path)
            if picture is not None and not self.stop_event.is_set():
                output = _jpeg(picture)
                save_image(path, "thumbnail", output)
                self.image_ready.emit(camera, str(path), output)

        self._run_all(list(self.paths.items()), thumbnail)


class StoryboardWorker(_ParallelDecodeWorker):
    """Decode keyframe-based twenty-frame contact sheets off the GUI thread.

    Frames are emitted one by one as they are decoded (``partial``) so the
    strip fills in progressively; the finished sheet is cached on disk.
    Paths whose sheet was completed are recorded in ``completed``.
    """

    ready = Signal(str, str, object, int)
    partial = Signal(str, str, int, object, int)

    def __init__(self, jobs: list[tuple[str, Path]], parallel: int = 1):
        super().__init__(parallel)
        self.jobs = jobs
        self.completed: set[str] = set()

    @staticmethod
    def _compose(frames: dict[int, bytes]) -> bytes:
        sheet = QImage(STORYBOARD_CELL[0] * StoryboardStrip.SOURCE_SEGMENTS, STORYBOARD_CELL[1],
                       QImage.Format_RGB32)
        sheet.fill(QColor("#101318"))
        painter = QPainter(sheet)
        for index, data in frames.items():
            image = QImage.fromData(data)
            if not image.isNull():
                painter.drawImage(index * STORYBOARD_CELL[0], 0, image)
        painter.end()
        output = QByteArray()
        buffer = QBuffer(output)
        buffer.open(QIODevice.WriteOnly)
        sheet.save(buffer, "JPG", 80)
        return bytes(output.data())

    @Slot()
    def run(self) -> None:
        def storyboard(item: tuple[str, Path]) -> None:
            camera, path = item
            cached = load_image(path, "storyboard20")
            if cached is not None:
                self.completed.add(str(path))
                self.ready.emit(camera, str(path), cached[0], cached[1])
                return
            duration = video_decode.probe_duration(path)
            if not duration or not 0 < duration < 86400:
                self.completed.add(str(path))  # unreadable: nothing will succeed
                return
            duration_ms = round(duration * 1000)
            frames: dict[int, bytes] = {}
            times = video_decode.storyboard_times(duration, StoryboardStrip.SOURCE_SEGMENTS)
            for index, frame in video_decode.keyframes(path, times):
                if self.stop_event.is_set():
                    return  # interrupted: the background queue finishes it later
                data = _jpeg(video_decode.scaled(frame, *STORYBOARD_CELL))
                frames[index] = data
                self.partial.emit(camera, str(path), index, data, duration_ms)
            if not frames:
                return
            image = self._compose(frames)
            save_image(path, "storyboard20", image, duration_ms)
            self.completed.add(str(path))
            self.ready.emit(camera, str(path), image, duration_ms)

        self._run_all(list(self.jobs), storyboard)


class TokenDialog(QDialog):
    start_requested = Signal()

    def __init__(self, default_output: Path, parent: QWidget | None = None):
        super().__init__(parent)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        width = min(760, max(320, available.width() - 40)) if available else 760
        height = min(620, max(320, available.height() - 40)) if available else 620
        self.setFixedSize(width, height)
        self.setModal(False)
        self.setStyleSheet(
            """
            QDialog { background: #151a21; color: #edf2f7; }
            QLabel { color: #edf2f7; }
            QLineEdit, QPlainTextEdit { color: #edf2f7; background: #202832; border: 1px solid #536477; padding: 5px; }
            QPushButton { color: #ffffff; background: #2f6f9f; border: 1px solid #6b9bc2; padding: 7px 12px; border-radius: 4px; }
            QPushButton:hover { background: #3e87bb; }
            QCheckBox { color: #edf2f7; }
            QProgressBar { color: #ffffff; background: #202832; border: 1px solid #536477; text-align: center; }
            QProgressBar::chunk { background: #2f9bd1; }
            """
        )
        self.setWindowTitle("Tesla 영상 복호화")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("dashcam.tesla.com의 Bearer 토큰")
        self.output = QLineEdit(str(default_output))
        self.choose_button = None
        choose = QPushButton("찾기")
        choose.clicked.connect(self.choose_output)
        self.choose_button = choose
        guide_button = QPushButton("🌐 dashcam.tesla.com 열기")
        guide_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://dashcam.tesla.com")))
        output_row = QHBoxLayout()
        output_row.addWidget(self.output, 1)
        output_row.addWidget(choose)
        note = QLabel(
            "토큰은 저장하지 않으며 Tesla에 파일별 복호화 키를 요청할 때만 사용합니다. "
            "영상 파일은 업로드하지 않고 이 PC에서만 복호화합니다."
        )
        note.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Bearer 토큰", self.token)
        form.addRow("출력 폴더", output_row)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        instructions = QLabel(
            "Bearer 토큰 얻는 법 (Chrome·Edge)\n"
            "① 아래 버튼으로 dashcam.tesla.com을 열고 Tesla 계정으로 로그인합니다.\n"
            "② F12로 개발자 도구를 열고 '네트워크(Network)' 탭을 선택합니다.\n"
            "③ 사이트에서 암호화된 영상 1개를 열어 복호화가 진행되게 합니다.\n"
            "④ 목록에서 'decrypt' 요청을 누르고, 요청 헤더(Request Headers)의 "
            "'Authorization: Bearer …'에서 'Bearer ' 뒤의 값을 복사해 아래 칸에 붙여 넣습니다.\n"
            "토큰은 로그인 계정의 권한이므로 다른 사람에게 알려 주지 마세요. 일정 시간이 지나면 만료됩니다."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        layout.addLayout(form)
        layout.addWidget(guide_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumHeight(48)
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._last_logged_progress = -1
        layout.addWidget(self.progress_bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("복호화 단계별 로그가 표시됩니다.")
        self.log.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)
        self.delete_source = QCheckBox("복호화 성공 후 원본 암호화 .mp4 삭제")
        self.delete_source.setChecked(True)
        self.delete_source.setToolTip("정상 MP4 출력이 확인된 파일만 삭제합니다. 삭제 후 복구할 수 없습니다.")
        layout.addWidget(self.delete_source)
        delete_note = QLabel("삭제 대상은 암호화된 영상 파일만이며 event.json 등 부가 파일은 남깁니다.")
        delete_note.setWordWrap(True)
        layout.addWidget(delete_note)
        event_note = QLabel("※ " + EVENT_JSON_NOTE)
        event_note.setWordWrap(True)
        event_note.setStyleSheet("color: #aebdca;")
        layout.addWidget(event_note)
        buttons = QDialogButtonBox()
        self.start_button = buttons.addButton("복호화 시작", QDialogButtonBox.AcceptRole)
        self.close_button = buttons.addButton("닫기", QDialogButtonBox.RejectRole)
        self.start_button.clicked.connect(self.start_requested.emit)
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "복호화 출력 폴더", self.output.text())
        if folder:
            self.output.setText(folder)

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.append_log(message)

    def append_log(self, message: str) -> None:
        if message:
            self.log.appendPlainText(message)

    def set_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, value)))
        self.status_label.setText(message)
        if value != self._last_logged_progress:
            self._last_logged_progress = value
            self.append_log(f"{value:3d}%  {message}")

    def set_running(self) -> None:
        self.start_button.setEnabled(False)
        self.close_button.setText("백그라운드로 닫기")
        self.token.setEnabled(False)
        self.output.setEnabled(False)
        if self.choose_button:
            self.choose_button.setEnabled(False)
        self.delete_source.setEnabled(False)

    def set_finished(self, message: str, success: int, failed: int) -> None:
        self.progress_bar.setValue(100)
        self.set_status(message)
        self.close_button.setText("닫기")
        self.close_button.setEnabled(True)
        self.start_button.setEnabled(False)

    def set_token(self, token: str) -> None:
        self.token.setText(token)
        self.token.setCursorPosition(0)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"T6 Viewer {__version__} — Tesla dashcam smart viewer")
        self.resize(1800, 980)
        self.setAcceptDrops(True)
        self.current_root: Path | None = None
        self._repair_source_root: Path | None = None
        self.all_groups: list[ClipGroup] = []
        self.groups: list[ClipGroup] = []
        self.events: list[TeslaEvent] = []
        self.all_event_targets: list[tuple[TeslaEvent, ClipGroup, int]] = []
        self.event_targets: list[tuple[TeslaEvent, ClipGroup, int]] = []
        self.pending_seek_ms: int | None = None
        self._syncing = False
        self._thread: QThread | None = None
        self._worker: DecryptWorker | None = None
        self._last_output: Path | None = None
        self._decrypt_dialog: TokenDialog | None = None
        self._decryption_started = False
        # Per-clip post-processing (object motion, GPS/vehicle state) runs in
        # a pool of worker processes sized by the memory governor.
        self._analysis_bridge = AnalysisBridge(self)
        self._analysis_bridge.result.connect(self._analysis_result)
        self._analysis = AnalysisPool(self._analysis_bridge.result.emit)
        self._telemetry_total = 0
        self._thumbnail_thread: QThread | None = None
        self._thumbnail_worker: ThumbnailWorker | None = None
        self._thumbnail_pending: dict[str, Path] | None = None
        self._storyboard_thread: QThread | None = None
        self._storyboard_worker: StoryboardWorker | None = None
        self._storyboard_pending: dict[str, Path] | None = None
        self._storyboard_active_paths: dict[str, Path] | None = None
        self._storyboard_cache: dict[str, tuple[int, int, bytes, int]] = {}
        # Per-clip analysis progress for the 분석 column and the status bar.
        self._done: dict[str, set[str]] = {"objects": set(), "storyboard": set(), "sei": set()}
        self._group_by_path: dict[str, ClipGroup] = {}
        self._pie_icons: dict[tuple[int, int], QIcon] = {}
        self.storyboard_camera = "front"
        self._viewed_groups: set[str] = set()
        self._explicit_selection: ClipGroup | None = None
        self._data_cleared = False
        self._settings_path = data_file("settings.json", ".tesla_viewer_settings.json")
        self.settings = load_settings(self._settings_path)
        self.object_categories = set(self.settings.object_categories)
        self._watched_path = data_file("watched_items.json", ".tesla_watched_items.json")
        self._watched_progress = self._load_watched_items()
        self._watch_last: tuple[int, float] | None = None
        self._watch_signature: tuple[str, list[str] | None] | None = None
        self._last_position_ui = 0.0
        self._tile_seeks: dict[QMediaPlayer, int] = {}
        self._last_resync: dict[str, float] = {}
        self._playable_order: tuple[tuple, dict[int, ClipGroup], dict[int, ClipGroup]] | None = None
        self.watched_timer = QTimer(self)
        self.watched_timer.setSingleShot(True)
        self.watched_timer.setInterval(5000)
        self.watched_timer.timeout.connect(self._save_watched_items)
        self._motion_cache_path = data_file(f"motion_cache_v{CACHE_VERSION}.json",
                                            f".tesla_ground_motion_cache_v{CACHE_VERSION}.json")
        self._motion_cache = self._load_motion_cache()
        self._motion_cache_dirty = False
        self.motion_cache_timer = QTimer(self)
        self.motion_cache_timer.setSingleShot(True)
        self.motion_cache_timer.setInterval(2000)
        self.motion_cache_timer.timeout.connect(self._save_motion_cache)
        self._telemetry_thread: QThread | None = None
        self._telemetry_worker: SelectedTelemetryWorker | None = None
        self._telemetry_pending: ClipGroup | None = None
        self._selection_pending: ClipGroup | None = None
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.setInterval(100)
        self.selection_timer.timeout.connect(self._load_pending_selection)
        self.telemetry_routes: dict[str, list[list[float]]] = {}
        self._telemetry_indexed_keys: set[str] = set()
        self.vehicle_states: dict[str, str] = {}
        self.group_items: dict[str, QTreeWidgetItem] = {}
        self.map_refresh_timer = QTimer(self)
        self.map_refresh_timer.setSingleShot(True)
        self.map_refresh_timer.setInterval(250)
        self.map_refresh_timer.timeout.connect(self.refresh_map_routes)
        self.current_group: ClipGroup | None = None
        self.focus_camera: str | None = None
        self.selected_cameras = set(CAMERAS)
        self.camera_buttons: dict[str, QPushButton] = {}
        self.preview_mode = True
        self._play_requested = False
        self._preload_group: ClipGroup | None = None
        self._preload_channels: dict[str, dict[str, object]] = {}
        self._memory_pressured = False
        self._memory_rate_cap: float | None = None
        self._memory_status = ""
        self.preload_timer = QTimer(self)
        self.preload_timer.setSingleShot(True)
        self.preload_timer.setInterval(1200)
        self.preload_timer.timeout.connect(lambda: self._prepare_next_player(self.current_group))
        start_memory_sampler()
        self._dispose_queue: list[QMediaPlayer] = []
        self.dispose_timer = QTimer(self)
        self.dispose_timer.setInterval(120)
        self.dispose_timer.timeout.connect(self._dispose_next_player)
        self.memory_timer = QTimer(self)
        self.memory_timer.setInterval(500)
        self.memory_timer.timeout.connect(self._check_memory_budget)
        self.memory_timer.start()
        self._layout_signature: tuple | None = None
        self.layout_timer = QTimer(self)
        self.layout_timer.setSingleShot(True)
        self.layout_timer.setInterval(80)
        self.layout_timer.timeout.connect(self.relayout_tiles)
        self.base_speed = 1.0
        self.adaptive_speed = False
        self.current_telemetry: list[TelemetrySample] = []
        self.current_event_point: list[float] | None = None

        self.tiles = [VideoTile(camera, self) for camera in CAMERAS]
        self.tiles_by_camera = {tile.camera: tile for tile in self.tiles}
        self.master = self.tiles[0].player
        for tile in self.tiles:
            self._connect_tile_player(tile)
            tile.double_clicked.connect(self.tile_double_clicked)
            tile.thumbnail_clicked.connect(self.thumbnail_clicked)
            tile.storyboard_seek_requested.connect(self.seek_storyboard)

        self.group_list = QTreeWidget()
        self.group_list.setColumnCount(len(LIST_COLUMNS))
        self.list_filters: dict[str, object] = {
            "datetime": {"date": None, "time": None, "weekdays": None},
            "decrypt": None, "category": None, "motion": None, "event": None, "analysis": None,
        }
        self._update_header_labels()
        header = self.group_list.header()
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._open_column_filter)
        header.setToolTip("열 제목을 누르면 필터를 설정합니다.")
        self.group_list.setMinimumWidth(300)
        self.group_list.setUniformRowHeights(True)
        self.group_list.setRootIsDecorated(False)
        self.group_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.group_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        for column, width in enumerate((176, 42, 48, 66, 50, 40)):
            self.group_list.setColumnWidth(column, width)
        self.group_list.itemClicked.connect(self.group_selected)
        self.group_list.itemDoubleClicked.connect(self.group_double_clicked)
        self.group_list.setToolTip("같은 시각에 녹화된 카메라 6개 묶음")
        # Empty-list message, centred in the list itself (not behind the videos).
        self.empty_label = QLabel(EMPTY_LIST_TEXT, self.group_list.viewport())
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #8795a3; background: transparent; padding: 16px;")
        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.group_list.viewport().installEventFilter(self)

        grid = QWidget()
        self.grid_widget = grid
        grid.installEventFilter(self)
        grid_layout = QGridLayout(grid)
        self.grid_layout = grid_layout
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(3)
        for tile in self.tiles:
            tile.setParent(grid)
        self.relayout_tiles()

        self.focus_page = QWidget()
        self.focus_layout = QVBoxLayout(self.focus_page)
        self.focus_layout.setContentsMargins(0, 0, 0, 0)
        self.focus_layout.setSpacing(0)
        self.video_stack = QStackedWidget()
        self.video_stack.addWidget(grid)
        self.video_stack.addWidget(self.focus_page)


        # Three side-by-side panes: 탐색 (list) | 영상 | 지도 (map + telemetry).
        video_panel = QWidget()
        video_panel_layout = QVBoxLayout(video_panel)
        video_panel_layout.setContentsMargins(0, 0, 0, 0)
        video_panel_layout.setSpacing(2)
        direction_bar = QHBoxLayout()
        direction_bar.setContentsMargins(0, 4, 0, 0)
        # The camera row spans only the video pane, so its end buttons sit on
        # the list/video and video/map borders and never cover a picture.
        self.list_toggle = self._pane_button("목록 숨기기")
        direction_bar.addWidget(self.list_toggle)
        direction_bar.addWidget(QLabel("카메라"))
        for camera in CAMERA_DISPLAY_ORDER:
            if camera == "left_repeater":
                direction_bar.addWidget(self._vertical_separator())
            button = QPushButton(CAMERA_LABELS[camera])
            button.setCheckable(True)
            button.setChecked(True)
            button.toggled.connect(lambda checked, c=camera: self.camera_toggled(c, checked))
            self.camera_buttons[camera] = button
            direction_bar.addWidget(button)
        direction_bar.addStretch(1)
        self.map_toggle = self._pane_button("지도 숨기기")
        direction_bar.addWidget(self.map_toggle)
        video_panel_layout.addLayout(direction_bar)
        # Driving-state strip: shown only for clips recorded while moving,
        # at the top of the Front camera (see _place_hud).
        self.drive_hud = DriveHud()
        self.drive_hud.hide()
        self._hud_tile: VideoTile | None = None
        self._place_hud()
        video_panel_layout.addWidget(self.video_stack, 1)
        self.map_widget = TileMapWidget(self._map_provider(), str(data_dir() / "tile_cache"),
                                        f"T6Viewer/{__version__} (+{PROJECT_URL})")
        self.map_widget.group_clicked.connect(self.map_group_selected)
        self.telemetry_labels: dict[str, QLabel] = {}
        telemetry_panel = self.create_telemetry_panel()

        splitter = CollapsibleSplitter(Qt.Horizontal)
        splitter.addWidget(self.make_browse_panel())
        splitter.addWidget(video_panel)
        splitter.addWidget(telemetry_panel)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(1, False)
        splitter.setSizes([430, 1000, 370])
        self.main_splitter = splitter
        self.setCentralWidget(splitter)
        self.list_toggle.clicked.connect(lambda: self.main_splitter.toggle(0))
        self.map_toggle.clicked.connect(lambda: self.main_splitter.toggle(2))
        self.main_splitter.collapsed_changed.connect(self._update_pane_toggles)
        self._update_pane_toggles()
        self.setStatusBar(QStatusBar())
        self.activity_label = QLabel()
        self.activity_label.setToolTip("백그라운드에서 진행 중인 작업")
        self.statusBar().addPermanentWidget(self.activity_label)
        self._activity_tick = 0

        self.slider = ClickSeekSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self.seek)
        self.slider.clicked_value.connect(self.seek)
        # 84 px -> 40 % narrower, icon only; previous/next clip at half its width.
        self.play_button = QPushButton()
        self.play_button.setFixedWidth(PLAY_BUTTON_WIDTH)
        self.play_button.setStyleSheet(COMPACT_BUTTON)
        self.play_button.clicked.connect(self.toggle_play)
        self._set_play_icon(False)
        self.previous_clip_button = QPushButton("|◀")
        self.next_clip_button = QPushButton("▶|")
        for button, tip, step in ((self.previous_clip_button, "이전 영상 (더 이른 시각)", -1),
                                  (self.next_clip_button, "다음 영상 (더 늦은 시각)", 1)):
            button.setFixedWidth(PLAY_BUTTON_WIDTH // 2)
            button.setStyleSheet("QPushButton { padding: 5px 0; }")
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, direction=step: self.step_clip(direction))
        self.time_label = QLabel("00:00 / 00:00")
        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(self.previous_clip_button)
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.next_clip_button)
        self.speed_buttons: dict[float, QPushButton] = {}
        for speed in (1.0, 2.0, 4.0, 8.0, 16.0):
            button = QPushButton(f"{speed:g}×")
            button.setCheckable(True)
            button.setChecked(speed == 1.0)
            button.clicked.connect(lambda _checked, rate=speed: self.set_base_speed(rate))
            button.setStyleSheet(COMPACT_BUTTON)
            self.speed_buttons[speed] = button
            controls_layout.addWidget(button)
        self.adaptive_button = QPushButton("가변")
        self.adaptive_button.setCheckable(True)
        self.adaptive_button.setToolTip("선택한 검출 대상의 움직임이 없을 때 최대 16×, 움직임 구간은 선택한 속도. 판정 불가 시 기본 속도")
        self.adaptive_button.toggled.connect(self.set_adaptive_speed)
        self.adaptive_button.setStyleSheet(COMPACT_BUTTON)
        controls_layout.addWidget(self.adaptive_button)
        self.next_change_button = QPushButton("⏭ 스킵")
        self.next_change_button.setStyleSheet(COMPACT_BUTTON)
        self.next_change_button.setToolTip(
            "스킵: 선택한 인식 대상이 움직인 다음 장면의 1초 전으로 이동합니다.\n"
            "이 영상에 더 없으면 움직임이 있는 다음 영상으로 넘어갑니다.\n"
            "(주행하지 않은 영상만 객체 인식을 합니다.)")
        self.next_change_button.clicked.connect(lambda: self.jump_to_next_change())
        controls_layout.addWidget(self.next_change_button)
        self.auto_jump_button = QPushButton("⏩ 자동 스킵")
        self.auto_jump_button.setCheckable(True)
        self.auto_jump_button.setStyleSheet(COMPACT_BUTTON)
        self.auto_jump_button.setToolTip(
            "자동 스킵: 켜 두면 재생 중 움직임이 없는 구간을 자동으로 건너뛰어\n"
            "다음 움직임의 1초 전으로 이동합니다. 영상 끝까지 움직임이 없으면\n"
            "움직임이 있는 다음 영상으로 넘어갑니다.\n"
            "아직 분석되지 않은 영상과 주행 영상은 그대로 재생합니다.")
        self.auto_jump_button.setChecked(self.settings.auto_jump)
        self.auto_jump_button.toggled.connect(self.set_auto_jump)
        controls_layout.addWidget(self.auto_jump_button)
        for button in (self.next_change_button, self.auto_jump_button):
            button.setEnabled(self.settings.analysis_objects)
        self.actual_speed_label = QLabel("1×")
        controls_layout.addWidget(self.actual_speed_label)
        controls_layout.addWidget(self.slider, 1)
        controls_layout.addWidget(self.time_label)
        video_panel_layout.addWidget(controls)
        self.motion_timer = QTimer(self)
        self.motion_timer.setInterval(350)
        self.motion_timer.timeout.connect(self.update_playback_speed)
        self.motion_timer.timeout.connect(self._auto_jump_tick)
        self.motion_timer.timeout.connect(self._resync_tiles)
        self._auto_jump_cooldown = 0.0
        self.motion_timer.start()
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.seek_relative(-5000))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.seek_relative(5000))
        self.apply_style()

    @staticmethod
    def _pane_button(tooltip: str) -> QPushButton:
        button = QPushButton()
        button.setToolTip(tooltip)
        button.setFixedSize(16, 26)
        button.setStyleSheet("QPushButton { padding: 0; font-size: 10px; border-radius: 3px; }")
        return button

    @staticmethod
    def _vertical_separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet("color: #536477;")
        line.setFixedWidth(9)
        return line

    def _update_pane_toggles(self) -> None:
        list_hidden = self.main_splitter.is_collapsed(0)
        self.list_toggle.setText("▶" if list_hidden else "◀")
        self.list_toggle.setToolTip("목록 보이기" if list_hidden else "목록 숨기기")
        map_hidden = self.main_splitter.is_collapsed(2)
        self.map_toggle.setText("◀" if map_hidden else "▶")
        self.map_toggle.setToolTip("지도 보이기" if map_hidden else "지도 숨기기")

    def _connect_tile_player(self, tile: VideoTile) -> None:
        tile.player.errorOccurred.connect(lambda error, message, t=tile: self.video_error(t, message))
        tile.player.positionChanged.connect(lambda position, p=tile.player: self.position_changed(p, position))
        tile.player.mediaStatusChanged.connect(
            lambda status, p=tile.player: self.media_status_changed_from(p, status)
        )
        tile.player.durationChanged.connect(
            lambda duration, p=tile.player: self.duration_changed_from(p, duration)
        )

    def create_telemetry_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.map_widget, 1)
        details = QWidget()
        details_layout = QFormLayout(details)
        details_layout.setContentsMargins(8, 5, 8, 5)
        details_layout.setHorizontalSpacing(12)
        details_layout.setVerticalSpacing(2)
        fields = (
            ("gps", "GPS"),
            ("speed", "속도"),
            ("gear", "기어"),
            ("pedal", "가속 페달"),
            ("steering", "조향각"),
            ("blinker", "방향지시등"),
            ("brake", "브레이크"),
            ("autopilot", "주행 보조"),
            ("accel", "가속도"),
        )
        for key, title in fields:
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.telemetry_labels[key] = value
            details_layout.addRow(title, value)
        layout.addWidget(details)
        return panel

    def make_browse_panel(self) -> QWidget:
        """탐색 pane: folder / decrypt / settings buttons above the clip list.

        Replaces a full-width toolbar, so the videos and the map get the height.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 0, 0)
        layout.setSpacing(4)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        open_button = QPushButton("📂 TeslaCam 열기")
        open_button.setToolTip("TeslaCam 폴더나 USB 드라이브를 엽니다(암호화·복호화 폴더 모두 가능).")
        open_button.clicked.connect(self.open_folder)
        self.decrypt_button = QPushButton("🔓 복호화")
        self.decrypt_button.setToolTip("암호화된 영상을 Tesla 계정 키로 이 PC에서 복호화합니다.")
        self.decrypt_button.clicked.connect(self.decrypt_dialog)
        settings_button = QPushButton("⚙ 설정")
        settings_button.setToolTip("분석 항목·객체 인식 대상·배속 상한·분석 프로세스 수, 프로그램 정보")
        settings_button.clicked.connect(self.open_settings)
        about_button = QPushButton("ⓘ 정보")
        about_button.setToolTip("버전·버전 기록·오픈소스 고지·도움말")
        about_button.clicked.connect(lambda: AboutDialog(self).exec())
        buttons.addWidget(open_button)
        buttons.addWidget(self.decrypt_button)
        buttons.addStretch(1)
        buttons.addWidget(settings_button)
        buttons.addWidget(about_button)
        layout.addLayout(buttons)
        layout.addWidget(self.group_list, 1)
        return panel

    def apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #101318; }
            QDialog, QMessageBox { background: #151a21; }
            QMessageBox QLabel { color: #edf2f7; }
            QToolBar { background: #1c222b; border: 0; spacing: 6px; padding: 5px; }
            QToolButton, QPushButton { color: #edf2f7; background: #2a3441; border: 1px solid #435164; padding: 6px 10px; border-radius: 4px; }
            QToolButton:hover, QPushButton:hover { background: #354456; }
            QPushButton:checked { background: #b43632; border-color: #f27770; color: #ffffff; }
            QTabWidget::pane { background: #171c24; border: 1px solid #344150; }
            QTabBar::tab { background: #202832; color: #c9d4df; padding: 7px 16px; border: 1px solid #344150; border-bottom: 0; }
            QTabBar::tab:selected { background: #265a82; color: #ffffff; }
            QTabBar::tab:hover { background: #354456; color: #ffffff; }
            QLabel, QCheckBox { color: #e8edf2; }
            QLineEdit, QComboBox, QDateEdit, QTimeEdit { color: #edf2f7; background: #202832; border: 1px solid #435164; padding: 4px 6px; border-radius: 3px; }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus { border: 1px solid #59b5ff; }
            QComboBox QAbstractItemView { color: #edf2f7; background: #202832; selection-background-color: #265a82; selection-color: #ffffff; }
            QCheckBox::indicator { width: 15px; height: 15px; }
            QCheckBox::indicator:unchecked { background: #202832; border: 1px solid #66788b; border-radius: 3px; }
            QCheckBox::indicator:checked { background: #2f8dcc; border: 1px solid #59b5ff; border-radius: 3px; }
            QTreeWidget { background: #171c24; color: #dce4ed; border: 0; }
            QTreeWidget::item { padding: 2px 5px; border-bottom: 1px solid #29313c; font-size: 11px; }
            QTreeWidget::item:selected { background: #265a82; }
            QHeaderView::section { color: #edf2f7; background: #25303c; border: 0; border-right: 1px solid #435164; padding: 4px; }
            QScrollBar:vertical { background: #171c24; width: 12px; margin: 0; }
            QScrollBar::handle:vertical { background: #536477; min-height: 28px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #6e8298; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QLabel#cameraTitle { color: #e8edf2; background: #202832; font-weight: bold; }
            QVideoWidget { background: #050608; }
            QSlider::groove:horizontal { background: #3b4653; height: 5px; }
            QSlider::handle:horizontal { background: #59b5ff; width: 12px; margin: -4px 0; border-radius: 6px; }
            QStatusBar { color: #aebdca; background: #171c24; }
            """
        )

    def group_events(self, group: ClipGroup) -> list[TeslaEvent]:
        return [event for event, target, _offset in self.all_event_targets if target is group]

    # -- list columns and header filters -----------------------------------
    @staticmethod
    def _group_start(group: ClipGroup):
        starts = [clip.timestamp for clip in group.clips.values() if clip.timestamp]
        return min(starts) if starts else None

    @staticmethod
    def _group_category(group: ClipGroup) -> str:
        folder_name = str(group.folder or "").lower()
        if "sentryclips" in folder_name:
            return "감시"
        if "savedclips" in folder_name:
            return "저장"
        if "recentclips" in folder_name:
            return "최근"
        return "미상"

    @staticmethod
    def _decrypt_status(group: ClipGroup) -> str:
        if group.encrypted_count == len(group.clips):
            return "locked"
        return "partial" if group.encrypted_count else "open"

    def _motion_class(self, group: ClipGroup) -> str:
        state = self.vehicle_states.get(self.group_key(group), "")
        if state == "이동":
            return "이동"
        if state in ("주차", "정차", "정지"):
            return "정지"
        return "정보 없음"

    def _event_flag(self, group: ClipGroup) -> str:
        return "Yes" if self.group_events(group) else "No"

    def _passes_filters(self, group: ClipGroup) -> bool:
        filters = self.list_filters
        start = self._group_start(group)
        if start is None:
            return False
        when = filters["datetime"]
        if when.get("date") and not when["date"][0] <= start.date() <= when["date"][1]:
            return False
        if when.get("time"):
            low, high = when["time"]
            moment = start.time().replace(second=0, microsecond=0)
            inside = low <= moment <= high if low <= high else (moment >= low or moment <= high)
            if not inside:
                return False
        if when.get("weekdays") is not None and start.weekday() not in when["weekdays"]:
            return False
        checks = (("decrypt", self._decrypt_status), ("category", self._group_category),
                  ("motion", self._motion_class), ("event", self._event_flag),
                  ("analysis", self._progress_state))
        return all(filters[name] is None or read(group) in filters[name] for name, read in checks)

    def _update_header_labels(self) -> None:
        active = (
            any(value is not None for value in self.list_filters["datetime"].values()),
            self.list_filters["decrypt"] is not None,
            self.list_filters["category"] is not None,
            self.list_filters["motion"] is not None,
            self.list_filters["event"] is not None,
            self.list_filters["analysis"] is not None,
        )
        self.group_list.setHeaderLabels(
            [f"{name} {'🔽' if on else '▾'}" for name, on in zip(LIST_COLUMNS, active)]
        )

    def _open_column_filter(self, column: int) -> None:
        header = self.group_list.header()
        point = header.mapToGlobal(header.rect().bottomLeft())
        point.setX(header.mapToGlobal(header.rect().topLeft()).x() + header.sectionViewportPosition(column))
        if column == 0:
            starts = [start for group in self.all_groups if (start := self._group_start(group))]
            popup = DateTimePopup(self.list_filters["datetime"],
                                  min(starts).date() if starts else None,
                                  max(starts).date() if starts else None, self)
            popup.applied.connect(lambda value: self._set_list_filter("datetime", value))
        else:
            name, title, options = {
                1: ("decrypt", "복호", [("open", "🔓 복호됨"), ("partial", "🔓🔒 일부 복호"),
                                         ("locked", "🔒 암호화")]),
                2: ("category", "폴더", [(value, value) for value in ("최근", "저장", "감시", "미상")
                                             if any(self._group_category(group) == value
                                                    for group in self.all_groups)]),
                3: ("motion", "주행", [("이동", "주행"), ("정지", "정지(주차·정차 포함)"),
                                          ("정보 없음", "정보 없음·분석 중")]),
                4: ("event", "Event (event.json)", [("Yes", "Yes · 이벤트 정보 있음"),
                                                     ("No", "No · 없음")]),
                5: ("analysis", "분석 (객체 인식·스토리보드·SEI)", [
                    ("done", "완료"), ("partial", "진행 중"), ("none", "시작 전")]),
            }[column]
            popup = ChecklistPopup(title, options, self.list_filters[name], self)
            popup.applied.connect(lambda value, key=name: self._set_list_filter(key, value))
        popup.show_below(point)

    def _set_list_filter(self, name: str, value) -> None:
        self.list_filters[name] = value
        self.apply_filters()

    def apply_filters(self) -> None:
        self._update_header_labels()
        filtered = [group for group in self.all_groups if self._passes_filters(group)]
        self.groups = filtered
        if not any(group is self._explicit_selection for group in filtered):
            self._explicit_selection = None
        self.event_targets = self.build_event_targets(filtered)
        self.refresh_group_list()
        if self.current_root is not None:
            self.start_telemetry_index()

    def refresh_group_list(self) -> None:
        self.selection_timer.stop()
        self._selection_pending = None
        current = self.current_group
        self.group_list.clear()
        self.group_items.clear()
        lock_icons = {"open": "🔓", "partial": "🔓🔒", "locked": "🔒"}
        lock_tips = {"open": "복호됨", "partial": "일부 카메라만 복호됨", "locked": "암호화됨"}
        for index, group in enumerate(self.groups):
            start = self._group_start(group)
            when = (f"{start:%Y-%m-%d}({WEEKDAY_NAMES[start.weekday()]}) {start:%H:%M:%S}"
                    if start else str(group.timestamp))
            status = self._decrypt_status(group)
            events = self.group_events(group)
            key = self.group_key(group)
            item = QTreeWidgetItem((
                when, lock_icons[status], self._group_category(group),
                self._motion_label(key), "Yes" if events else "No", "",
            ))
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(0, f"{len(group.clips)}/6 채널")
            item.setToolTip(1, lock_tips[status])
            item.setToolTip(2, str(group.folder or ""))
            item.setToolTip(3, "SEI 속도·기어 기준. 정보가 없으면 운행 상태를 추정하지 않습니다.")
            item.setToolTip(4, ", ".join(dict.fromkeys(event.reason_label for event in events))
                            if events else "복호화된 event.json 정보 없음")
            self._show_progress(item, group)
            for column in (1, 4, 5):
                item.setTextAlignment(column, Qt.AlignCenter)
            self._set_watched_appearance(item, group)
            self.group_list.addTopLevelItem(item)
            self.group_items[key] = item
        self.group_list.resizeColumnToContents(0)
        self.empty_label.setText(EMPTY_FILTER_TEXT if self.all_groups else EMPTY_LIST_TEXT)
        self.empty_label.setVisible(not self.groups)
        self._place_empty_label()
        if not self.groups:
            self.current_group = None
            for tile in self.tiles:
                tile.set_clip(None)
            self.map_widget.set_group_routes({}, None, None)
            self.statusBar().showMessage("필터 조건에 맞는 장면이 없습니다.")
            return
        selected_index = next((index for index, group in enumerate(self.groups) if group is current), -1)
        if selected_index >= 0 and self._play_requested:
            # Filtering while playing keeps the clip playing.
            self.group_list.setCurrentItem(self.group_list.topLevelItem(selected_index))
        else:
            selected_index = max(0, selected_index)
            self.group_list.setCurrentItem(self.group_list.topLevelItem(selected_index))
            self.load_group(self.groups[selected_index])
        self.statusBar().showMessage(f"필터 결과: {len(self.groups)}개 장면")

    def _index_progress(self) -> None:
        """Read which clips already have cached results (one listdir per folder)."""
        self._done = {"objects": set(), "storyboard": set(), "sei": set()}
        self._group_by_path = {}
        listings: dict[Path, set[str]] = {}  # clip folder -> cached result files
        suffixes = {"objects": f".{OBJECTS_CACHE_KIND}.json.gz", "storyboard": ".storyboard20.json.gz",
                    "sei": ".sei.json.gz"}
        for group in self.all_groups:
            for clip in group.clips.values():
                self._group_by_path[str(clip.path)] = group
                folder = clip.path.parent
                if folder not in listings:
                    listings[folder] = cache_listing(folder)
                names = listings[folder]
                for kind, suffix in suffixes.items():
                    if clip.path.name + suffix in names:
                        self._done[kind].add(str(clip.path))

    def _progress(self, group: ClipGroup) -> tuple[int, int, str]:
        """Done items, total items and a tooltip for one group."""
        clips = [clip for clip in group.clips.values() if not clip.encrypted]
        if not clips:
            return 0, 0, "암호화된 영상은 분석할 수 없습니다."
        reference = group.clips.get("front") or next(iter(group.clips.values()))
        done = total = 0
        parts = []
        if self.settings.analysis_objects:
            objects = sum(str(clip.path) in self._done["objects"] for clip in clips)
            done, total = done + objects, total + len(clips)
            parts.append(f"객체 인식 {objects}/{len(clips)}")
        if self.settings.analysis_storyboard:
            sheets = sum(str(clip.path) in self._done["storyboard"] for clip in clips)
            done, total = done + sheets, total + len(clips)
            parts.append(f"스토리보드 {sheets}/{len(clips)}")
        if self.settings.analysis_sei:
            sei = int(str(reference.path) in self._done["sei"])
            done, total = done + sei, total + 1
            parts.append(f"SEI(주행 정보) {'완료' if sei else '대기'}")
        return done, total, " · ".join(parts) if parts else "설정에서 분석을 모두 껐습니다."

    def _pie_icon(self, done: int, total: int) -> QIcon:
        key = (done, total)
        if key not in self._pie_icons:
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QColor("#66788b"))
            painter.setBrush(QColor("#202832"))
            painter.drawEllipse(1, 1, 14, 14)
            if done:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#48cf76" if done >= total else "#59b5ff"))
                if done >= total:
                    painter.drawEllipse(1, 1, 14, 14)
                else:
                    painter.drawPie(1, 1, 14, 14, 90 * 16, -round(360 * 16 * done / total))
            painter.end()
            self._pie_icons[key] = QIcon(pixmap)
        return self._pie_icons[key]

    def _progress_state(self, group: ClipGroup) -> str:
        done, total, _tip = self._progress(group)
        return "done" if total and done >= total else "partial" if done else "none"

    def _show_progress(self, item: QTreeWidgetItem, group: ClipGroup) -> None:
        done, total, tip = self._progress(group)
        column = LIST_COLUMNS.index("분석")
        if total:
            item.setIcon(column, self._pie_icon(done, total))
            item.setText(column, "")
        else:
            item.setText(column, "-")
        item.setToolTip(column, tip)

    def _mark_done(self, kind: str, path: str) -> None:
        if path in self._done[kind]:
            return
        self._done[kind].add(path)
        group = self._group_by_path.get(path)
        item = self.group_items.get(self.group_key(group)) if group is not None else None
        if item is not None:
            self._show_progress(item, group)

    def _update_activity_label(self, used: int | None) -> None:
        """Bottom-right: what the app is doing right now."""
        parts = []
        workers = self._analysis.worker_count()
        pending = self._analysis.pending_count() + self._analysis.running_count()
        if workers or pending:
            parts.append(f"분석 프로세스 {workers}개 · 남은 작업 {pending:,}")
        clips = sum(1 for group in self.all_groups for clip in group.clips.values() if not clip.encrypted)
        settings = self.settings
        if not (settings.analysis_objects or settings.analysis_storyboard or settings.analysis_sei):
            parts.append("분석 꺼짐(재생만)")
        elif clips:
            groups = sum(1 for group in self.all_groups if any(not c.encrypted for c in group.clips.values()))
            items = []
            if settings.analysis_objects:
                items.append(f"객체 {len(self._done['objects']):,}/{clips:,}")
            if settings.analysis_storyboard:
                items.append(f"스토리보드 {len(self._done['storyboard']):,}/{clips:,}")
            if settings.analysis_sei:
                items.append(f"SEI {len(self._done['sei']):,}/{groups:,}")
            if not settings.analysis_background:
                items.append("보고 있는 영상만 분석")
            parts.append(" · ".join(items))
        if self._storyboard_thread is not None:
            parts.append("현재 영상 스토리보드 생성 중")
        if self._preload_channels:
            ready = sum(1 for entry in self._preload_channels.values() if entry["ready"])
            parts.append(f"다음 영상 준비 {ready}/{len(self._preload_channels)}")
        if self._fast_playback():
            parts.append("고배속: 분석 축소")
        if self._memory_pressured:
            parts.append("메모리 절약 중")
        if used:
            parts.append(f"앱 메모리 {used / 2 ** 30:.1f}/4.5GB")
        self.activity_label.setText("  ·  ".join(parts))

    def _motion_label(self, key: str) -> str:
        state = self.vehicle_states.get(key)
        if state is None:
            return "분석 중"
        return {"주차": "정지", "정차": "정지", "정지": "정지", "이동": "주행"}.get(state, "-")

    def _update_decrypt_action(self) -> None:
        """Only offer decryption when something here is still encrypted."""
        pending = (self._repair_source_root is not None
                   or any(group.encrypted_count for group in self.all_groups))
        # A fully decrypted folder has nothing to offer here: hide the button.
        self.decrypt_button.setHidden(not pending)

    def open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "TeslaCam 폴더 선택 — 암호화(Encrypted) 원본·복호화(Decrypted) 폴더 모두 열 수 있습니다")
        if folder:
            self.load_folder(Path(folder))

    def load_folder(self, path: Path) -> None:
        try:
            root = teslacam_root(path)
            archive_root, archived_count, archive_errors = archive_encrypted_outputs(root)
            groups = discover_groups(root)
            events = discover_events(root)
        except Exception as exc:
            QMessageBox.critical(self, "폴더 오류", str(exc))
            return
        self._analysis.clear()
        self.current_root = root
        self._repair_source_root = None
        if archive_root and archive_root.is_dir():
            pending_videos = any(
                clip.encrypted and not is_plain_mp4(root / clip.path.relative_to(archive_root))
                for clip in discover_clips(archive_root)
            )
            if pending_videos:
                self._repair_source_root = archive_root
        self.all_groups = groups
        self.groups = groups
        self.events = events
        self.current_group = None
        self.telemetry_routes = {}
        self._telemetry_indexed_keys = set()
        self._explicit_selection = None
        self.vehicle_states = {}
        self.all_event_targets = self.build_event_targets(groups)
        self._index_progress()
        self._update_decrypt_action()
        self.apply_filters()
        self.start_telemetry_index()
        if archived_count:
            self.statusBar().showMessage(f"암호화 파일 {archived_count}개를 복호화 폴더 밖에 분리 보관: {archive_root}")
        if archive_errors:
            QMessageBox.warning(self, "암호화 파일 분리 실패", "\n".join(archive_errors[:8]))

    @staticmethod
    def group_key(group: ClipGroup) -> str:
        return f"{group.folder or ''}|{group.timestamp}"

    @staticmethod
    def _group_signature(group: ClipGroup) -> list[str] | None:
        signature = []
        try:
            for camera, clip in sorted(group.clips.items()):
                stat = clip.path.stat()
                signature.append(f"{camera}|{clip.path}|{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            return None
        return signature or None

    def _load_watched_items(self) -> dict[str, dict]:
        try:
            payload = json.loads(self._watched_path.read_text(encoding="utf-8"))
            return {key: record for key, record in payload.items()
                    if isinstance(key, str) and isinstance(record, dict)
                    and isinstance(record.get("signature"), list)
                    and isinstance(record.get("intervals"), list)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _is_fully_watched(self, group: ClipGroup) -> bool:
        recorded = self._watched_progress.get(self.group_key(group))
        return bool(recorded and recorded.get("signature") == self._group_signature(group)
                    and coverage(recorded.get("intervals", []), recorded.get("duration_ms", 0)) > 0.75)

    def _set_watched_appearance(self, item: QTreeWidgetItem, group: ClipGroup) -> None:
        if not self._is_fully_watched(group):
            return
        for column in range(self.group_list.columnCount()):
            item.setForeground(column, QBrush(QColor("#8cdaa4")))
        item.setToolTip(0, "재생하여 확인한 구간이 전체의 75%를 넘었습니다.")

    def _record_watched_position(self, player: QMediaPlayer, position: int) -> None:
        group = self.current_group
        if group is None or not self._play_requested or self.preview_mode or player.duration() <= 0:
            self._watch_last = None
            return
        if player.playbackState() != QMediaPlayer.PlayingState:
            self._watch_last = None
            return
        now = time.monotonic()
        previous = self._watch_last
        self._watch_last = (position, now)
        if previous is None:
            return
        delta = position - previous[0]
        wall_ms = max(0.0, now - previous[1]) * 1000
        if delta <= 0 or delta > max(3000, wall_ms * max(1.0, player.playbackRate()) * 2 + 1000):
            return
        key = self.group_key(group)
        if self._watch_signature is None or self._watch_signature[0] != key:
            self._watch_signature = (key, self._group_signature(group))
        signature = self._watch_signature[1]
        if signature is None:
            return
        record = self._watched_progress.get(key)
        if record is None or record.get("signature") != signature:
            record = {"signature": signature, "duration_ms": player.duration(), "intervals": []}
            self._watched_progress[key] = record
        was_complete = coverage(record["intervals"], record["duration_ms"]) > 0.75
        record["duration_ms"] = player.duration()
        record["intervals"] = add_interval(record["intervals"], previous[0], position)
        if not was_complete and coverage(record["intervals"], record["duration_ms"]) > 0.75:
            item = self.group_items.get(key)
            if item is not None:
                self._set_watched_appearance(item, group)
        self.watched_timer.start()

    def _save_watched_items(self) -> None:
        if not self._watched_progress:
            return
        temporary = self._watched_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(self._watched_progress, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self._watched_path)
        except OSError:
            self.statusBar().showMessage("시청 완료 기록을 저장하지 못했습니다. 현재 실행에서는 표시를 유지합니다.")

    def refresh_map_routes(self, focus: bool = False) -> None:
        routes: dict[str, list[list[float]]] = {}
        for group in self.groups:
            key = self.group_key(group)
            route = self.telemetry_routes.get(key, [])
            if not route and group is self.current_group and self.current_telemetry:
                route = [
                    [sample.latitude_deg, sample.longitude_deg, sample.position_ms]
                    for sample in gps_samples(self.current_telemetry)
                ]
            if not route:
                route = [
                    [event.latitude, event.longitude, max(0, round((event.timestamp - min(
                        clip.timestamp for clip in group.clips.values() if clip.timestamp
                    )).total_seconds() * 1000))]
                    for event in self.group_events(group)
                    if event.latitude is not None and event.longitude is not None
                ]
            if route:
                routes[key] = route
        selected = self.group_key(self.current_group) if self.current_group else None
        self.map_widget.set_group_routes(routes, selected, self.current_event_point, focus)

    def queue_background_work(self) -> None:
        """GPS/vehicle state and storyboards for every clip, current first."""
        self.start_telemetry_index()

    def _ordered_for_analysis(self) -> tuple[list[ClipGroup], ClipGroup | None, ClipGroup | None]:
        selected = self._explicit_selection
        ordered = analysis_group_order(self.groups, selected)
        upcoming = self._upcoming_group()
        if upcoming is not None and any(group is upcoming for group in ordered):
            ordered = [group for group in ordered if group is not upcoming]
            ordered.insert(1 if ordered and ordered[0] is selected else 0, upcoming)
        return ordered, selected, upcoming

    def _queue_storyboards(self, ordered: list[ClipGroup], selected, upcoming) -> None:
        """Contact sheets for every clip, built by the process pool.

        Clips on screen are handled by the progressive GUI worker instead.
        """
        on_screen = {str(tile.path) for tile in self.active_tiles() if tile.path}
        jobs = []
        if not (self.settings.analysis_storyboard and self.settings.analysis_background):
            # Off, or foreground only: the on-screen clip has its own worker.
            self._analysis.replace("storyboard", jobs)
            return
        for index, group in enumerate(ordered):
            tier = 0 if group is upcoming or (selected is not None and index < 4) else 2
            for camera, clip in group.clips.items():
                key = str(clip.path)
                if clip.encrypted or key in self._done["storyboard"] or key in on_screen:
                    continue
                # Cheap (~2 s): after GPS, ahead of object scans in each tier.
                jobs.append(Job((tier, 0.5, index), f"storyboard|{key}", "storyboard", (key,), camera))
        self._analysis.replace("storyboard", jobs)

    def start_telemetry_index(self) -> None:
        """Queue GPS route / vehicle-state extraction for unindexed groups."""
        ordered, selected, upcoming = self._ordered_for_analysis()
        self._queue_storyboards(ordered, selected, upcoming)
        jobs = []
        if not (self.settings.analysis_sei and self.settings.analysis_background):
            # The clip on screen reads its own SEI (SelectedTelemetryWorker).
            self._analysis.replace("telemetry", jobs)
            self._govern_analysis()
            return
        for index, group in enumerate(ordered):
            key = self.group_key(group)
            clip = group.clips.get("front") or next(iter(group.clips.values()), None)
            if key in self._telemetry_indexed_keys or clip is None:
                continue
            # Selected + three predecessors first, then ahead of the (much
            # slower) object scans of the remaining groups.
            tier = 0 if group is upcoming or (selected is not None and index < 4) else 2
            jobs.append(Job((tier, 0, index), f"telemetry|{key}", "telemetry", (str(clip.path),), key))
        self._telemetry_total = len(jobs) + len(self._telemetry_indexed_keys)
        self._analysis.replace("telemetry", jobs)
        self._govern_analysis()

    @Slot(object, object)
    def _analysis_result(self, job: Job, result: object) -> None:
        if job.kind == "telemetry":
            if not isinstance(result, dict):
                return
            self._map_route_ready(str(job.meta), result.get("route", []))
            self._vehicle_state_ready(str(job.meta), str(result.get("state", "정보 없음")))
            done = len(self._telemetry_indexed_keys)
            total = max(done, self._telemetry_total)
            if done >= total:
                self.statusBar().showMessage(f"지도 경로 준비 완료: {total}개 장면")
            elif done % 10 == 0:
                self.statusBar().showMessage(f"지도 경로 분석 {done}/{total} ({done * 100 // max(total, 1)}%)")
            group = self._group_by_path.get(job.args[0])
            if group is not None:
                self._mark_done("sei", job.args[0])
        elif job.kind == "motion":
            camera, _group_key = job.meta
            self._motion_result(camera, job.args[0], result)
        elif job.kind == "storyboard":
            if isinstance(result, dict) and isinstance(result.get("image"), bytes):
                self._storyboard_ready(str(job.meta), job.args[0], result["image"],
                                       int(result.get("duration_ms", 0)))

    @Slot(str, object)
    def _map_route_ready(self, group_key: str, route: object) -> None:
        if isinstance(route, list):
            self._telemetry_indexed_keys.add(group_key)
            self.telemetry_routes[group_key] = route
            if self.current_group is not None and group_key == self.group_key(self.current_group):
                self.refresh_map_routes(focus=True)
            else:
                self.map_refresh_timer.start()

    @Slot(str, str)
    def _vehicle_state_ready(self, group_key: str, state: str) -> None:
        self.vehicle_states[group_key] = state
        item = self.group_items.get(group_key)
        if item is not None:
            item.setText(3, self._motion_label(group_key))

    def _reprioritize_analysis(self) -> None:
        if self.current_root is not None:
            self.start_telemetry_index()
        if self.current_group is not None:
            self.start_motion_scan()

    def _decode_parallelism(self, count: int) -> int:
        """How many clips may be decoded at once within the memory budget."""
        used, available = memory_snapshot()
        room = memory_headroom(used, available, self._playback_reserve())
        return max(1, min(count, 6, room // DECODE_ESTIMATE))

    def start_thumbnails(self, paths: dict[str, Path]) -> None:
        if self._memory_pressured or not self.settings.analysis_preview:
            return
        if self._thumbnail_thread is not None:
            self._thumbnail_pending = paths
            if self._thumbnail_worker:
                self._thumbnail_worker.stop()
            return
        self._thumbnail_pending = None
        if not paths:
            return
        self._thumbnail_thread = QThread(self)
        self._thumbnail_worker = ThumbnailWorker(paths, self._decode_parallelism(len(paths)))
        self._thumbnail_worker.moveToThread(self._thumbnail_thread)
        self._thumbnail_thread.started.connect(self._thumbnail_worker.run)
        self._thumbnail_worker.image_ready.connect(self._thumbnail_ready)
        self._thumbnail_worker.finished.connect(self._thumbnail_thread.quit)
        self._thumbnail_worker.finished.connect(self._thumbnail_worker.deleteLater)
        self._thumbnail_thread.finished.connect(self._thumbnail_thread.deleteLater)
        self._thumbnail_thread.finished.connect(self._thumbnails_finished)
        self._thumbnail_thread.start()

    @Slot(str, str, object)
    def _thumbnail_ready(self, camera: str, path: str, image_bytes: object) -> None:
        tile = self.tiles_by_camera[camera]
        if isinstance(image_bytes, bytes) and tile.path == Path(path) and tile.display.currentWidget() is tile.thumbnail:
            tile.set_thumbnail_bytes(image_bytes)

    def _thumbnails_finished(self) -> None:
        self._thumbnail_thread = None
        self._thumbnail_worker = None
        if self._thumbnail_pending is not None:
            pending = self._thumbnail_pending
            self._thumbnail_pending = None
            self.start_thumbnails(pending)

    def request_storyboard(self, camera: str, path: Path) -> None:
        self.storyboard_camera = camera
        paths = {tile.camera: tile.path for tile in self.active_tiles() if tile.path}
        self.start_storyboards(paths)

    def start_storyboards(self, paths: dict[str, Path]) -> None:
        if self._memory_pressured or not self.settings.analysis_storyboard:
            return
        for camera, path in paths.items():
            key = self._motion_cache_key(path)
            cached = self._storyboard_cache.get(str(path))
            if key and cached and cached[:2] == key[1:]:
                self.tiles_by_camera[camera].storyboard.set_storyboard(camera, cached[2], cached[3])
        if self._storyboard_thread is not None:
            if paths != self._storyboard_active_paths:
                self._storyboard_pending = paths
                if self._storyboard_worker:
                    self._storyboard_worker.stop()
            return
        self._storyboard_pending = None
        jobs = [(camera, path) for camera, path in paths.items()
                if not (key := self._motion_cache_key(path))
                or not (cached := self._storyboard_cache.get(str(path)))
                or cached[:2] != key[1:]]
        if not jobs:
            return
        jobs.sort(key=lambda job: (job[0] != self.storyboard_camera, job[0] != "front"))
        self._run_storyboard_worker(jobs, paths)

    def _run_storyboard_worker(self, jobs: list[tuple[str, Path]], paths: dict[str, Path]) -> None:
        thread = QThread(self)
        worker = StoryboardWorker(jobs, self._decode_parallelism(len(jobs)))
        self._storyboard_thread = thread
        self._storyboard_worker = worker
        self._storyboard_active_paths = paths
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._storyboard_ready)
        worker.partial.connect(self._storyboard_partial)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._storyboard_finished)
        thread.start()

    @Slot(str, str, object, int)
    def _storyboard_ready(self, camera: str, path_str: str, image: object, duration_ms: int) -> None:
        if not isinstance(image, bytes):
            return
        self._mark_done("storyboard", path_str)
        key = self._motion_cache_key(Path(path_str))
        if key:
            self._storyboard_cache[path_str] = (key[1], key[2], image, duration_ms)
            if len(self._storyboard_cache) > 12:
                self._storyboard_cache.pop(next(iter(self._storyboard_cache)))
        tile = self.tiles_by_camera[camera]
        if tile.path == Path(path_str):
            tile.storyboard.set_storyboard(camera, image, duration_ms)

    @Slot(str, str, int, object, int)
    def _storyboard_partial(self, camera: str, path_str: str, index: int, image: object, duration_ms: int) -> None:
        tile = self.tiles_by_camera[camera]
        if isinstance(image, bytes) and tile.path == Path(path_str):
            tile.storyboard.set_segment(index, image, duration_ms)

    def _storyboard_finished(self) -> None:
        # Sheets left unfinished (the user moved on) stay in the background
        # queue: queue_background_work() lists every clip without a sheet.
        self._storyboard_thread = None
        self._storyboard_worker = None
        self._storyboard_active_paths = None
        pending = self._storyboard_pending
        self._storyboard_pending = None
        if pending and self.current_group:
            current = {camera: path for camera, path in pending.items()
                       if self.tiles_by_camera[camera].path == path}
            self.start_storyboards(current)
        self.queue_background_work()

    def seek_storyboard(self, camera: str, position_ms: int) -> None:
        if not self.current_group:
            return
        tile = self.tiles_by_camera[camera]
        if tile.path:
            self.request_storyboard(camera, tile.path)
        self.set_all_positions(position_ms)
        if self.preview_mode:
            self.toggle_play()

    @staticmethod
    def _motion_cache_key(path: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return str(path), stat.st_size, stat.st_mtime_ns

    def _load_motion_cache(self) -> dict[str, tuple[int, int, dict[str, object]]]:
        try:
            data = json.loads(self._motion_cache_path.read_text(encoding="utf-8"))
            if data.get("version") != CACHE_VERSION or not isinstance(data.get("entries"), dict):
                return {}
            return {
                path: tuple(entry)
                for path, entry in data["entries"].items()
                if isinstance(path, str) and isinstance(entry, list) and len(entry) == 3
                and isinstance(entry[2], dict)
                and entry[2].get("level") in ("quiet", "moderate", "high", "unknown")
                and isinstance(entry[2].get("intervals"), list)
            }
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_motion_cache(self) -> None:
        if not self._motion_cache_dirty:
            return
        temporary = self._motion_cache_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps({"version": CACHE_VERSION, "entries": self._motion_cache}, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self._motion_cache_path)
            self._motion_cache_dirty = False
        except OSError:
            self.statusBar().showMessage("움직임 분석 캐시를 저장하지 못했습니다. 현재 실행에서는 결과를 유지합니다.")

    def _cached_motion(self, path: Path, local: bool = True) -> dict[str, object] | None:
        key = self._motion_cache_key(path)
        if key is None:
            return None
        entry = self._motion_cache.get(key[0])
        if entry and entry[:2] == key[1:]:
            return entry[2]
        if entry:
            del self._motion_cache[key[0]]
            self._motion_cache_dirty = True
            self.motion_cache_timer.start()
        if local:
            result = load_result(path, OBJECTS_CACHE_KIND)
            if isinstance(result, dict) and result.get("level") in ("quiet", "moderate", "high", "unknown"):
                self._motion_cache[key[0]] = (key[1], key[2], result)
                return result
        return None

    def _motion_jobs(self, selected_only: bool = False) -> list[tuple]:
        if self.current_group is None:
            return []
        groups = self.groups if any(group is self.current_group for group in self.groups) else [self.current_group]
        selected = self._explicit_selection if any(group is self._explicit_selection for group in groups) else None
        ordered = analysis_group_order(groups, selected)
        predecessors = {id(group) for group in ordered[1:4]} if selected is not None else set()
        upcoming = self._upcoming_group()
        if upcoming is not None and any(group is upcoming for group in ordered):
            ordered = [group for group in ordered if group is not upcoming]
            ordered.insert(1 if ordered and ordered[0] is selected else 0, upcoming)
        jobs: list[tuple] = []
        for group in ordered:
            if selected_only and group is not self.current_group:
                continue
            reference = group.clips.get("front") or next(iter(group.clips.values()), None)
            if reference is None:
                continue
            tier = (0 if group is selected or group is upcoming
                    else 1 if id(group) in predecessors else 2)
            for camera in CAMERAS:
                clip = group.clips.get(camera)
                if clip is None or clip.encrypted:
                    continue
                path = clip.path
                if group is self.current_group:
                    if self._cached_motion(path) is not None:
                        continue
                elif str(path) in self._motion_cache:
                    # Queue building must stay cheap (thousands of clips):
                    # no stat() here. A changed file is re-validated when its
                    # group is shown, and workers validate their disk cache.
                    continue
                jobs.append((camera, path, tier, self.group_key(group), reference.path))
        return jobs

    def _refresh_motion_display(self) -> None:
        """Traffic light + object icons per camera, marks on strip and timeline."""
        runs_by_tile = []
        if not self.settings.analysis_objects:
            for tile in self.active_tiles():
                tile.set_motion_level("off")
                tile.storyboard.set_changes([])
            self.slider.set_marks([])
            return
        for tile in self.active_tiles():
            result = self._cached_motion(tile.path)
            filtered = motion_for_categories(result, self.object_categories) if result else None
            runs = change_runs(result, self.object_categories)
            icons = "".join(OBJECT_ICONS[name] for name in category_totals(runs))
            driving = (bool(result and result.get("engine") == "ego-motion")
                       or (self.current_group is not None
                           and self.vehicle_states.get(self.group_key(self.current_group)) == "이동"))
            level = "driving" if driving else str(filtered["level"]) if filtered else "unknown"
            if level == "unknown" and result is not None:
                level = "undetermined"  # scanned, but the scene could not be judged
            tile.set_motion_level(level, icons)
            tile.storyboard.set_changes(runs, int(result.get("duration_ms", 0)) if result else 0)
            runs_by_tile.append(runs)
        merged = merge_runs(runs_by_tile)
        self.slider.set_marks([(int(run["start"]), int(run["end"])) for run in merged])
        self.next_change_button.setEnabled(self.settings.analysis_objects)

    def _visible_runs(self) -> tuple[list[dict[str, object]], int, int]:
        """Merged change runs of the shown cameras that were scanned.

        Returns the runs, how many cameras were scanned and how many are shown.
        """
        results = [self._motion_cache.get(str(tile.path)) for tile in self.active_tiles()]
        results = [entry[2] for entry in results if entry and motion_known(entry[2])]
        runs = merge_runs([change_runs(result, self.object_categories) for result in results])
        return runs, len(results), len(self.active_tiles())

    def _group_runs(self, group: ClipGroup) -> list[dict[str, object]] | None:
        """Change runs of another group from the in-memory cache (None = not scanned)."""
        visible = {self.focus_camera} if self.focus_camera else self.selected_cameras
        run_lists = []
        for camera, clip in group.clips.items():
            if camera not in visible or clip.encrypted:
                continue
            entry = self._motion_cache.get(str(clip.path))
            if not entry or not motion_known(entry[2]):
                return None
            run_lists.append(change_runs(entry[2], self.object_categories))
        return merge_runs(run_lists) if run_lists else None

    def jump_to_next_change(self, automatic: bool = False) -> bool:
        """Seek to 1 s before the next detected change, in this or a later clip."""
        if self.current_group is None:
            return False
        position = self.master.position()
        runs, _known, _shown = self._visible_runs()
        target = next_change_ms(runs, position)
        if target is not None:
            self._auto_jump_cooldown = time.monotonic() + 1.5
            if self.preview_mode:
                self.set_all_positions(target)
                self.toggle_play()
            else:
                self.set_all_positions(target)
            self.statusBar().showMessage(f"스킵: 다음 움직임으로 이동 {target // 60000:02d}:{target // 1000 % 60:02d}")
            return True
        group = self._next_playable_group(self.current_group)
        skipped = 0
        while group is not None:
            runs = self._group_runs(group)
            if runs is None and automatic:
                break  # not analysed yet: keep playing instead of skipping it
            if runs:
                target = max(0, int(runs[0]["start"]) - 1000)
                self._auto_jump_cooldown = time.monotonic() + 2.5
                self.selection_timer.stop()
                self._selection_pending = None
                self._explicit_selection = group
                self.load_group(group, pending_seek_ms=target, autoplay=True)
                index = next((i for i, item in enumerate(self.groups) if item is group), -1)
                if index >= 0:
                    self.group_list.setCurrentItem(self.group_list.topLevelItem(index))
                self.statusBar().showMessage(
                    f"변화가 있는 다음 장면으로 이동: {group.timestamp}" + (f" (조용한 장면 {skipped}개 건너뜀)" if skipped else ""))
                return True
            if runs is not None:
                skipped += 1
            group = self._next_playable_group(group)
        if not automatic:
            self.statusBar().showMessage("이후 분석된 장면에서 선택한 대상의 변화가 없습니다(분석이 끝나지 않은 장면은 제외).")
        return False

    def _auto_jump_tick(self) -> None:
        """While playing, skip quiet stretches of analysed clips."""
        if (not self.auto_jump_button.isChecked() or not self.settings.analysis_objects
                or not self._play_requested or self.preview_mode
                or self.current_group is None or time.monotonic() < self._auto_jump_cooldown
                or self.pending_seek_ms is not None or self._tile_seeks):
            return
        runs, known, shown = self._visible_runs()
        if not known:
            # Nothing analysed on screen yet (its scan is queued first): a
            # missing result is not proof of a quiet scene, so keep playing.
            self._show_memory_status("자동 스킵: 이 영상은 아직 분석 중이라 그대로 재생합니다.")
            return
        position = self.master.position()
        if inside_change(runs, position):
            return
        if not self.jump_to_next_change(automatic=True):
            self._auto_jump_cooldown = time.monotonic() + 3
        elif known < shown:
            self.statusBar().showMessage(
                self.statusBar().currentMessage() + f" (분석된 카메라 {known}/{shown}개 기준)")

    def set_auto_jump(self, enabled: bool) -> None:
        self.settings.auto_jump = enabled
        save_settings(self._settings_path, self.settings)
        if enabled:
            self._auto_jump_cooldown = 0.0
            self._auto_jump_tick()

    def start_motion_scan(self, selected_only: bool = False) -> None:
        if self.current_group is None:
            return
        self._refresh_motion_display()
        jobs = []
        if self.settings.analysis_objects:
            background = self.settings.analysis_background and not selected_only
            jobs = [
                Job((tier, 1, index), f"motion|{path}", "motion", (str(path), str(reference)), (camera, group_key))
                for index, (camera, path, tier, group_key, reference) in enumerate(self._motion_jobs(not background))
            ]
        self._analysis.replace("motion", jobs)
        self._govern_analysis()

    @Slot(str, str, object)
    def _motion_result(self, camera: str, path_str: str, result: object) -> None:
        if not isinstance(result, dict):
            return
        level = str(result.get("level", "error"))
        path = Path(path_str)
        key = self._motion_cache_key(path)
        if key and (level in ("quiet", "moderate", "high") or result.get("engine") == "ego-motion"):
            self._mark_done("objects", path_str)
            self._motion_cache[key[0]] = (key[1], key[2], result)
            self._motion_cache_dirty = True
            self.motion_cache_timer.start()
        tile = self.tiles_by_camera[camera]
        if tile.path == path:
            self._refresh_motion_display()
            self.update_playback_speed()
        if result.get("reason"):
            self.statusBar().showMessage(f"객체 인식 분석 불가: {result['reason']}")

    def _stop_background_preview(self, keep_motion: bool = False) -> None:
        self._thumbnail_pending = None
        if self._thumbnail_worker:
            self._thumbnail_worker.stop()

    @Slot(str, int)
    def map_group_selected(self, group_key: str, position_ms: int) -> None:
        index = next(
            (idx for idx, group in enumerate(self.groups) if self.group_key(group) == group_key),
            -1,
        )
        if index >= 0:
            self.selection_timer.stop()
            self._selection_pending = None
            self._explicit_selection = self.groups[index]
            self._reprioritize_analysis()
            self.group_list.setCurrentItem(self.group_list.topLevelItem(index))
            self.load_group(self.groups[index], pending_seek_ms=position_ms, autoplay=True)

    def camera_toggled(self, camera: str, checked: bool) -> None:
        if checked:
            self.selected_cameras.add(camera)
            self.storyboard_camera = camera
        elif len(self.selected_cameras) > 1:
            self.selected_cameras.discard(camera)
        else:
            self.camera_buttons[camera].setChecked(True)
            return
        self.focus_camera = None
        self.relayout_tiles()
        if self.current_group:
            self.load_group(self.current_group, pending_seek_ms=self.master.position(), autoplay=not self.preview_mode)

    def set_object_categories(self, categories: set[str]) -> None:
        """Which moving objects count; cached scans are re-counted, not redone."""
        categories = {name for name in categories if name in GROUP_ORDER} or set(GROUP_ORDER)
        self.object_categories = categories
        self.settings.object_categories = [name for name in GROUP_ORDER if name in categories]
        self._refresh_motion_display()
        self.update_playback_speed()

    def tile_double_clicked(self, camera: str) -> None:
        self.storyboard_camera = camera
        self.focus_camera = None if self.focus_camera == camera else camera
        old_position = self.master.position()
        self.relayout_tiles()
        if self.current_group:
            self.load_group(self.current_group, pending_seek_ms=old_position, autoplay=not self.preview_mode)

    def thumbnail_clicked(self, camera: str) -> None:
        tile = self.tiles_by_camera[camera]
        if tile.path:
            self.request_storyboard(camera, tile.path)
        if self.current_group and self.preview_mode:
            self.toggle_play()

    def set_base_speed(self, rate: float) -> None:
        self.base_speed = rate
        for value, button in self.speed_buttons.items():
            button.setChecked(value == rate)
        self.update_playback_speed()

    def set_adaptive_speed(self, enabled: bool) -> None:
        self.adaptive_speed = enabled
        if enabled and self.current_group:
            self.start_motion_scan()
        self.update_playback_speed()

    def update_playback_speed(self) -> None:
        if self.adaptive_speed:
            position = self.master.position() if hasattr(self, "master") else 0
            results = []
            for tile in self.active_tiles():
                cached = self._motion_cache.get(str(tile.path))
                results.append(motion_for_categories(cached[2], self.object_categories) if cached else None)
            rate = adaptive_rate(results, position, self.base_speed)
            if self.current_group:
                near_event = any(
                    group is self.current_group and abs(position - offset) <= 5000
                    for _event, group, offset in self.all_event_targets
                )
                if near_event:
                    rate = self.base_speed
        else:
            rate = self.base_speed
        note = ""
        active = self.active_tiles()
        decode_cap = max(1.0, self.settings.max_decode_load / max(1, len(active)))
        decode_cap = round(decode_cap, 1)
        if rate > decode_cap:
            # Six full-resolution channels cannot be decoded at 16x anyway;
            # past this load Qt's pipeline grows by gigabytes on any GUI stall.
            rate = decode_cap
            note = f" ({len(active)}채널 최대)"
        duration = self.master.duration() if active else 0
        position = self.master.position() if active else 0
        if (rate > EDGE_MAX_RATE and duration > 0
                and not EDGE_FRACTION * duration <= position <= (1 - EDGE_FRACTION) * duration):
            # First/last 20% of a clip: decoders are being opened (next-clip
            # preload, clip switch), which is when high rates are riskiest.
            rate = EDGE_MAX_RATE
            note = " (시작·끝 구간)"
        if self._memory_rate_cap is not None and rate > self._memory_rate_cap:
            # Slow down rather than stop when playback alone would exceed
            # the memory budget.
            rate = self._memory_rate_cap
            note = " (메모리 제한)"
        for tile in active:
            if abs(tile.player.playbackRate() - rate) > 0.01:
                tile.player.setPlaybackRate(rate)
        self.actual_speed_label.setText(f"실제 {rate:g}×{note}")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        dialog.clear_requested.connect(lambda: QTimer.singleShot(0, self.clear_app_data))
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings = dialog.settings()
        self.settings.auto_jump = self.auto_jump_button.isChecked()
        self.set_object_categories(set(self.settings.object_categories))
        if not save_settings(self._settings_path, self.settings):
            self.statusBar().showMessage("설정을 파일에 저장하지 못했습니다. 이번 실행에만 적용합니다.")
        self.map_widget.set_provider(self._map_provider())
        self._apply_analysis_settings()

    def clear_app_data(self) -> None:
        """Delete everything this app stored, then quit (for uninstalling)."""
        box = QMessageBox(QMessageBox.Warning, "모든 설정·기록 삭제",
                          "설정, 시청 기록, 움직임 분석 목록, 오류 기록이 있는\n"
                          f"{data_dir()}\n폴더를 통째로 지우고 프로그램을 종료합니다. 되돌릴 수 없습니다.",
                          QMessageBox.NoButton, self)
        delete_button = box.addButton("삭제하고 종료", QMessageBox.DestructiveRole)
        cancel_button = box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(cancel_button)
        caches = QCheckBox(f"열려 있는 폴더의 영상별 분석 결과({CACHE_DIR_NAME})도 삭제")
        caches.setEnabled(self.current_root is not None)
        box.setCheckBox(caches)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        self._data_cleared = True
        for timer in (self.memory_timer, self.watched_timer, self.motion_cache_timer, self.preload_timer):
            timer.stop()
        self._analysis.shutdown()            # workers write result files
        for worker in (self._storyboard_worker, self._thumbnail_worker):
            if worker:
                worker.stop()
        crash_log.close()
        folder = data_dir()
        shutil.rmtree(folder, ignore_errors=True)
        removed_caches = 0
        if caches.isChecked() and self.current_root is not None:
            for name in (CACHE_DIR_NAME, *LEGACY_CACHE_DIR_NAMES):
                for cache in self.current_root.rglob(name):
                    shutil.rmtree(cache, ignore_errors=True)
                    removed_caches += 1
        if folder.exists():
            message = ("일부 파일은 사용 중이라 지우지 못했습니다. 프로그램 종료 후 이 폴더를 직접 지워 주세요:\n"
                       f"{folder}")
        else:
            message = "설정과 기록을 모두 지웠습니다."
        if removed_caches:
            message += f"\n영상 폴더의 분석 결과 {removed_caches}곳도 지웠습니다."
        QMessageBox.information(self, "삭제 완료", message + "\n프로그램을 종료합니다.")
        self.close()

    def _apply_analysis_settings(self) -> None:
        self.update_playback_speed()
        if self.current_root is not None:
            self.queue_background_work()
        if self.current_group is not None:
            self.start_motion_scan()
            active = self.active_tiles()
            self.start_storyboards({tile.camera: tile.path for tile in active if tile.path})
            if self.settings.analysis_sei and not self.current_telemetry:
                self._request_selected_telemetry(self.current_group)
        for button in (self.next_change_button, self.auto_jump_button):
            button.setEnabled(self.settings.analysis_objects)
        if self.current_root is not None:
            for key, item in self.group_items.items():
                group = next((g for g in self.groups if self.group_key(g) == key), None)
                if group is not None:
                    self._show_progress(item, group)
        self._govern_analysis()

    def _map_provider(self) -> TileProvider:
        key = self.settings.vworld_key.strip()
        return vworld(key) if key else openstreetmap()

    def _current_rate(self) -> float:
        return self.master.playbackRate() if self._play_requested else 0.0

    def _fast_playback(self) -> bool:
        """Fast review: only the video matters; background work steps back."""
        return self._play_requested and max(self._current_rate(), self.base_speed,
                                            16.0 if self.adaptive_speed else 0.0) > FAST_PLAYBACK_RATE

    def _upcoming_group(self) -> ClipGroup | None:
        if not self._play_requested or self.current_group is None:
            return None
        return self._next_playable_group(self.current_group)

    def _preload_allowed(self) -> bool:
        channels = max(1, len(self.active_tiles()))
        # Highest rate this playback may reach: adaptive mode jumps to 16x.
        requested = 16.0 if self.adaptive_speed else self.base_speed
        rate = min(max(self._current_rate(), requested), self.settings.max_decode_load / channels)
        return channels * rate <= PRELOAD_MAX_DECODE_LOAD

    def _playback_reserve(self) -> int:
        if not self._play_requested:
            return 0
        return playback_reserve(self._current_rate(), len(self.active_tiles()))

    def relayout_tiles(self) -> None:
        self._relayout_tiles()
        if hasattr(self, "drive_hud"):
            self._place_hud()

    def _relayout_tiles(self) -> None:
        selected = [camera for camera in CAMERA_DISPLAY_ORDER if camera in self.selected_cameras]
        segment_count = 20 if self.focus_camera or len(selected) == 1 else 10
        for tile in self.tiles:
            tile.storyboard.set_segment_count(segment_count)
        columns = self._columns_for_layout(len(selected), self.grid_widget.width(), self.grid_widget.height())
        signature = (self.focus_camera, tuple(selected), columns)
        if signature == self._layout_signature:
            return
        previous = self._layout_signature
        self._layout_signature = signature
        if previous and previous[0] is None and self.focus_camera is None and previous[1] == tuple(selected):
            # A narrow/wide resize only changes grid positions. Keep native
            # video widgets parented to the same window while decoding.
            for camera in selected:
                self.grid_layout.removeWidget(self.tiles_by_camera[camera])
            for index, camera in enumerate(selected):
                self.grid_layout.addWidget(self.tiles_by_camera[camera], index // columns, index % columns)
            return
        for tile in self.tiles:
            self.grid_layout.removeWidget(tile)
            if hasattr(self, "focus_layout"):
                self.focus_layout.removeWidget(tile)
            tile.hide()
            tile.setParent(self.grid_widget)
        if self.focus_camera:
            tile = self.tiles_by_camera[self.focus_camera]
            tile.setParent(self.focus_page)
            self.focus_layout.addWidget(tile, 1)
            tile.show()
            self.video_stack.setCurrentWidget(self.focus_page)
            return
        for index, camera in enumerate(selected):
            tile = self.tiles_by_camera[camera]
            self.grid_layout.addWidget(tile, index // columns, index % columns)
            tile.show()
        if hasattr(self, "video_stack"):
            self.video_stack.setCurrentWidget(self.grid_widget)

    @staticmethod
    def _columns_for_layout(count: int, width: int, height: int) -> int:
        if count <= 1:
            return 1
        if count == 2:
            # Two 16:10-ish camera frames need a wide canvas to sit side by
            # side; in a half-screen window use the available vertical room.
            return 2 if width >= 2.6 * max(height, 1) else 1
        return 2 if count == 4 else 3

    def _place_empty_label(self) -> None:
        self.empty_label.setGeometry(self.group_list.viewport().rect())

    def eventFilter(self, watched, event) -> bool:
        if watched is self.group_list.viewport() and event.type() == QEvent.Resize:
            self._place_empty_label()
        if watched is getattr(self, "grid_widget", None) and event.type() == QEvent.Resize:
            if len(self.selected_cameras) == 2 and self.focus_camera is None:
                self.layout_timer.start()
        return super().eventFilter(watched, event)

    def _place_hud(self) -> None:
        """Put the driving HUD at the top of Front, else the first shown camera."""
        visible = [self.tiles_by_camera[camera] for camera in CAMERA_DISPLAY_ORDER
                   if camera in ({self.focus_camera} if self.focus_camera else self.selected_cameras)]
        target = next((tile for tile in visible if tile.camera == "front"), visible[0] if visible else None)
        if target is None or target is self._hud_tile:
            return
        if self._hud_tile is not None:
            self._hud_tile.layout().removeWidget(self.drive_hud)
        target.layout().insertWidget(1, self.drive_hud)   # below the camera title
        self._hud_tile = target

    def active_tiles(self) -> list[VideoTile]:
        cameras = {self.focus_camera} if self.focus_camera else self.selected_cameras
        return [tile for tile in self.tiles if tile.camera in cameras and tile.path is not None]

    def group_selected(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        index = item.data(0, Qt.UserRole)
        if isinstance(index, int) and 0 <= index < len(self.groups):
            self._explicit_selection = self.groups[index]
            self._selection_pending = self.groups[index]
            self._reprioritize_analysis()
            self.selection_timer.start()

    def _load_pending_selection(self) -> None:
        group = self._selection_pending
        self._selection_pending = None
        if group is not None:
            self.load_group(group)

    def group_double_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        index = item.data(0, Qt.UserRole)
        if isinstance(index, int) and 0 <= index < len(self.groups):
            self.selection_timer.stop()
            self._selection_pending = None
            self._explicit_selection = self.groups[index]
            self._reprioritize_analysis()
            self.load_group(self.groups[index], autoplay=True)

    def build_event_targets(self, candidate_groups: list[ClipGroup]) -> list[tuple[TeslaEvent, ClipGroup, int]]:
        targets: list[tuple[TeslaEvent, ClipGroup, int]] = []
        for event in self.events:
            candidates = [
                group
                for group in candidate_groups
                if group.folder == event.folder and group.clips
            ]
            if not candidates:
                # A copied/flattened folder may not preserve the sidecar's
                # parent relationship; fall back to the nearest clip group.
                candidates = [group for group in candidate_groups if group.clips]
            if not candidates:
                continue
            starts = {
                id(group): min(clip.timestamp for clip in group.clips.values() if clip.timestamp)
                for group in candidates
            }
            before = [group for group in candidates if starts[id(group)] <= event.timestamp]
            target = max(before or candidates, key=lambda group: starts[id(group)])
            offset_ms = max(0, int((event.timestamp - starts[id(target)]).total_seconds() * 1000))
            targets.append((event, target, offset_ms))
        return targets

    def load_group(self, group: ClipGroup, pending_seek_ms: int | None = None, autoplay: bool = False,
                   prepared: dict[str, tuple[Path, QMediaPlayer]] | None = None) -> None:
        self._play_requested = False
        self.preload_timer.stop()
        self._tile_seeks.clear()
        if prepared is None:
            self._clear_preload()
        prepared = prepared or {}
        previous_group = self.current_group
        self._watch_last = None
        visible = {self.focus_camera} if self.focus_camera else self.selected_cameras
        paths: dict[str, Path | None] = {}
        for tile in self.tiles:
            clip = group.clips.get(tile.camera) if tile.camera in visible else None
            paths[tile.camera] = clip.path if clip and not clip.encrypted else None
        if previous_group is not group:
            # Release every old decoder before opening any source for the new
            # six-camera group, avoiding an old+new texture/buffer peak.
            # Tiles taking over a preloaded decoder release theirs in set_clip.
            for tile in self.tiles:
                warmed = prepared.get(tile.camera)
                if tile.path is not None and not (warmed and warmed[0] == paths[tile.camera]):
                    tile.release_video()
        self.current_group = group
        self.pending_seek_ms = pending_seek_ms
        self.preview_mode = not autoplay
        self._viewed_groups.add(self.group_key(group))
        if autoplay:
            self._stop_background_preview(keep_motion=self.adaptive_speed)
        for tile in self.tiles:
            path = paths[tile.camera]
            warmed = prepared.get(tile.camera)
            if warmed is not None and path == warmed[0]:
                self._dispose_queue.append(tile.set_clip(path, preview=False, prepared_player=warmed[1]))
                self._connect_tile_player(tile)
            elif tile.path == path:
                if path is not None and not autoplay:
                    tile.show_thumbnail()
            else:
                tile.set_clip(path, preview=True)
            tile.setVisible(tile.camera in visible)
        active = self.active_tiles()
        self._play_requested = autoplay and bool(active)
        self.master = next((tile.player for tile in active if tile.camera == "front"),
                           active[0].player if active else self.tiles[0].player)
        self.time_label.setText("00:00 / --:--")
        self.slider.setValue(pending_seek_ms or 0)
        for tile in self.tiles:
            tile.storyboard.set_position(pending_seek_ms or 0)
        self._set_play_icon(False)
        if self._play_requested:
            # Start decoding first; the rest only updates side panels.
            self.update_playback_speed()
            for tile in sorted(active, key=lambda item: item.camera not in prepared):
                tile.show_video()
                tile.player.play()
            self._set_play_icon(True)
            if self.master.duration() > 0:
                # A preloaded master reported its duration before we connected.
                self.duration_changed(self.master.duration())
            self.preload_timer.start()
            if self._dispose_queue:
                QTimer.singleShot(400, self.dispose_timer.start)
        else:
            self.start_thumbnails({
                tile.camera: tile.path for tile in active
                if tile.path is not None and tile._thumbnail_image is None
            })
        self.start_motion_scan()
        storyboard_tile = next((tile for tile in active if tile.camera == self.storyboard_camera and tile.path), None)
        if storyboard_tile is None:
            storyboard_tile = next((tile for tile in active if tile.camera == "front" and tile.path), None)
        if storyboard_tile is None:
            storyboard_tile = next((tile for tile in active if tile.path), None)
        if storyboard_tile:
            self.request_storyboard(storyboard_tile.camera, storyboard_tile.path)
        self.load_telemetry(group)
        self.update_playback_speed()
        self._dispatch_pending_seek()

    def position_changed(self, player: QMediaPlayer, position: int) -> None:
        if player is not self.master or self._syncing:
            return
        # The master reports every decoded frame (~200/s at 8x). Refreshing
        # the panels at ~15 Hz looks identical and keeps the GUI thread free
        # to present video; a stalled GUI thread makes Qt buffer frames.
        now = time.monotonic()
        if (player.playbackState() == QMediaPlayer.PlayingState
                and now - self._last_position_ui < POSITION_UI_INTERVAL):
            return
        self._last_position_ui = now
        self._record_watched_position(player, position)
        self.slider.setValue(position)
        for tile in self.active_tiles():
            tile.storyboard.set_position(position)
        self.update_time_label(position, self.slider.maximum())
        self.update_telemetry(position)

    def load_telemetry(self, group: ClipGroup) -> None:
        """Keep selection responsive while SEI is read off the UI thread."""
        self.current_telemetry = []
        self.drive_hud.hide()
        self.drive_hud.set_sample(None)
        self.current_event_point = None
        event_points = [
            [event.latitude, event.longitude]
            for event in self.group_events(group)
            if event.latitude is not None and event.longitude is not None
        ]
        if event_points:
            self.current_event_point = event_points[0]
        self.refresh_map_routes(focus=True)
        self.update_telemetry(0)
        self._request_selected_telemetry(group)

    def _request_selected_telemetry(self, group: ClipGroup) -> None:
        if not self.settings.analysis_sei:
            return
        if self._telemetry_thread is not None:
            self._telemetry_pending = group
            return
        self._telemetry_pending = None
        thread = QThread(self)
        worker = SelectedTelemetryWorker(group)
        self._telemetry_thread = thread
        self._telemetry_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._selected_telemetry_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._selected_telemetry_finished)
        thread.start()

    @Slot(object, object)
    def _selected_telemetry_ready(self, group: ClipGroup, samples: list[TelemetrySample]) -> None:
        if group is not self.current_group:
            return
        self.current_telemetry = samples
        self.drive_hud.setVisible(is_driving(samples))
        reference = group.clips.get("front") or next(iter(group.clips.values()), None)
        if reference is not None:
            self._mark_done("sei", str(reference.path))
        self._vehicle_state_ready(self.group_key(group), vehicle_motion_label(samples))
        gps = gps_samples(samples)
        if gps:
            self.current_event_point = None
        if self.group_key(group) not in self.telemetry_routes and gps:
            self.telemetry_routes[self.group_key(group)] = [
                [sample.latitude_deg, sample.longitude_deg, sample.position_ms] for sample in gps
            ]
            self.refresh_map_routes(focus=True)
        self.update_telemetry(self.pending_seek_ms if self.pending_seek_ms is not None else self.master.position())
        if samples:
            self.statusBar().showMessage(f"SEI 텔레메트리 {len(samples):,}개 · GPS {len(gps):,}개")

    def _selected_telemetry_finished(self) -> None:
        self._telemetry_thread = None
        self._telemetry_worker = None
        pending = self._telemetry_pending
        self._telemetry_pending = None
        if pending is not None and pending is self.current_group:
            self._request_selected_telemetry(pending)

    def update_telemetry(self, position_ms: int) -> None:
        sample = nearest_sample(self.current_telemetry, position_ms)
        if self.drive_hud.isVisible():
            self.drive_hud.set_sample(sample)
        if sample is None:
            for label in self.telemetry_labels.values():
                label.setText("텔레메트리 없음")
            self.map_widget.set_position(self.current_event_point)
            return

        def value(text: str) -> str:
            return text if text else "-"

        gps_text = (
            f"{sample.latitude_deg:.6f}, {sample.longitude_deg:.6f}"
            if sample.has_gps
            else "GPS 없음"
        )
        speed = "-" if sample.vehicle_speed_mps is None else f"{sample.vehicle_speed_mps * 3.6:.1f} km/h"
        pedal = "-" if sample.accelerator_pedal_position is None else f"{sample.accelerator_pedal_position:.1f} %"
        steering = "-" if sample.steering_wheel_angle is None else f"{sample.steering_wheel_angle:.1f}°"
        blinkers = []
        if sample.blinker_on_left:
            blinkers.append("좌")
        if sample.blinker_on_right:
            blinkers.append("우")
        blinker_text = (
            ", ".join(blinkers)
            if blinkers
            else ("꺼짐" if sample.blinker_on_left is not None or sample.blinker_on_right is not None else "-")
        )
        brake = "작동" if sample.brake_applied else ("꺼짐" if sample.brake_applied is not None else "-")
        acceleration = "-"
        if all(
            component is not None
            for component in (
                sample.linear_acceleration_mps2_x,
                sample.linear_acceleration_mps2_y,
                sample.linear_acceleration_mps2_z,
            )
        ):
            acceleration = (
                f"x {sample.linear_acceleration_mps2_x:.2f}, "
                f"y {sample.linear_acceleration_mps2_y:.2f}, "
                f"z {sample.linear_acceleration_mps2_z:.2f} m/s²"
            )
        self.telemetry_labels["gps"].setText(gps_text)
        self.telemetry_labels["speed"].setText(speed)
        self.telemetry_labels["gear"].setText(gear_label(sample.gear_state))
        self.telemetry_labels["pedal"].setText(pedal)
        self.telemetry_labels["steering"].setText(steering)
        self.telemetry_labels["blinker"].setText(blinker_text)
        self.telemetry_labels["brake"].setText(brake)
        self.telemetry_labels["autopilot"].setText(autopilot_label(sample.autopilot_state))
        self.telemetry_labels["accel"].setText(value(acceleration))
        point = None
        if sample.has_gps:
            point = [sample.latitude_deg, sample.longitude_deg, sample.position_ms,
                     self._heading_at(sample)]
        self.map_widget.set_position(point)

    def _heading_at(self, sample: TelemetrySample) -> float | None:
        """Vehicle heading in degrees; from the route when SEI has none."""
        if sample.heading_deg is not None and math.isfinite(sample.heading_deg):
            return round(sample.heading_deg % 360, 1)
        gps = [item for item in gps_samples(self.current_telemetry)]
        index = next((i for i, item in enumerate(gps) if item.position_ms >= sample.position_ms), len(gps) - 1)
        for other in gps[index + 1:]:
            lat1, lon1 = math.radians(sample.latitude_deg), math.radians(sample.longitude_deg)
            lat2, lon2 = math.radians(other.latitude_deg), math.radians(other.longitude_deg)
            if abs(lat2 - lat1) + abs(lon2 - lon1) < 1e-7:
                continue
            y = math.sin(lon2 - lon1) * math.cos(lat2)
            x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
            return round(math.degrees(math.atan2(y, x)) % 360, 1)
        return None

    def duration_changed_from(self, player: QMediaPlayer, duration: int) -> None:
        if player is self.master:
            self.duration_changed(duration)

    def _dispatch_pending_seek(self) -> None:
        """Give every shown channel the pending position.

        Each player seeks as soon as its own media is ready (or right away
        when it already is). Waiting for the reference channel alone left
        channels that were reopened, e.g. after leaving a single-camera
        view, at 0 while an already-open reference kept its position.
        """
        if self.pending_seek_ms is None or self.preview_mode:
            return
        position = self.pending_seek_ms
        self.pending_seek_ms = None
        ready = (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia, QMediaPlayer.BufferingMedia)
        for tile in self.active_tiles():
            if tile.player.mediaStatus() in ready and tile.has_source(tile.path):
                tile.player.setPosition(min(position, max(0, tile.player.duration())))
            else:
                self._tile_seeks[tile.player] = position
        self.slider.setValue(position)
        self.update_time_label(position, self.slider.maximum())
        self.update_telemetry(position)

    def _resync_tiles(self) -> None:
        """Pull drifting channels back to the reference channel while playing."""
        if (not self._play_requested or self.preview_mode or self._tile_seeks
                or self.pending_seek_ms is not None or self._syncing):
            return
        master = self.master
        reference = master.position()
        rate = max(1.0, master.playbackRate())
        tolerance = max(400, 120 * rate)
        now = time.monotonic()
        for tile in self.active_tiles():
            player = tile.player
            if player is master or player.playbackState() != QMediaPlayer.PlayingState:
                continue
            if player.duration() and reference >= player.duration() - 1000:
                continue  # this channel's clip is shorter; it just ends first
            if abs(player.position() - reference) > tolerance and now - self._last_resync.get(tile.camera, 0) > 2:
                self._last_resync[tile.camera] = now
                player.setPosition(reference)

    def media_status_changed_from(self, player: QMediaPlayer, status: QMediaPlayer.MediaStatus) -> None:
        if player in self._tile_seeks and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            position = self._tile_seeks.pop(player)
            player.setPosition(min(position, max(0, player.duration())))
        if (player is self.master and status == QMediaPlayer.EndOfMedia
                and self._play_requested and not self.preview_mode
                and player.duration() > 0
                and player.position() >= max(0, player.duration() - 750)):
            group = self.current_group
            QTimer.singleShot(0, lambda: self._advance_after_end(group, player))

    def _next_playable_group(self, group: ClipGroup) -> ClipGroup | None:
        visible = frozenset({self.focus_camera} if self.focus_camera else self.selected_cameras)
        signature = (id(self.groups), len(self.groups), visible)
        if self._playable_order is None or self._playable_order[0] != signature:
            chronological = sorted(
                (item for item in self.groups
                 if any(camera in visible and not clip.encrypted for camera, clip in item.clips.items())),
                key=lambda item: (item.timestamp, str(item.folder)),
            )
            following = {id(item): after for item, after in zip(chronological, chronological[1:])}
            preceding = {id(after): item for item, after in zip(chronological, chronological[1:])}
            self._playable_order = (signature, following, preceding)
        return self._playable_order[1].get(id(group))

    def _previous_playable_group(self, group: ClipGroup) -> ClipGroup | None:
        self._next_playable_group(group)  # refresh the cached order
        return self._playable_order[2].get(id(group))

    def _set_play_icon(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")
        self.play_button.setToolTip("일시정지 (Space)" if playing else "재생 (Space)")

    def step_clip(self, direction: int) -> None:
        """Previous (-1) or next (+1) clip in time; keeps playing if it was."""
        if self.current_group is None:
            return
        target = (self._next_playable_group(self.current_group) if direction > 0
                  else self._previous_playable_group(self.current_group))
        if target is None:
            self.statusBar().showMessage("다음 영상이 없습니다." if direction > 0 else "이전 영상이 없습니다.")
            return
        playing = self._play_requested
        self.selection_timer.stop()
        self._selection_pending = None
        self._explicit_selection = target
        self.load_group(target, autoplay=playing)
        index = next((i for i, item in enumerate(self.groups) if item is target), -1)
        if index >= 0:
            self.group_list.setCurrentItem(self.group_list.topLevelItem(index))
            self.group_list.scrollToItem(self.group_list.topLevelItem(index))

    def _check_memory_budget(self) -> None:
        """Keep the whole process tree under the 4.5 GiB budget, and the PC
        as a whole (other programs included) away from running out of memory.

        Optional memory is released in priority order (analysis processes,
        short-lived decoders, next-clip preload) and, only if playback
        alone is too large, the playback rate is capped. Playback itself,
        and with it automatic advance to the next clip, is never stopped.
        """
        used, available = memory_snapshot()
        if over_budget(used, available) or memory_critical(used, available):
            self._shed_memory(used, available)
        elif self._memory_pressured and memory_recovered(used, available):
            self._memory_pressured = False
            self._memory_rate_cap = None
            self._memory_status = ""
            self.update_playback_speed()
            if self.current_group:
                active = self.active_tiles()
                if not self._play_requested:
                    self.start_thumbnails({tile.camera: tile.path for tile in active
                                           if tile._thumbnail_image is None})
                self.start_storyboards({tile.camera: tile.path for tile in active})
            self.statusBar().showMessage("메모리 여유가 생겨 미리 열기와 백그라운드 분석을 재개했습니다.")
        self._govern_analysis(used, available)
        self._activity_tick = (self._activity_tick + 1) % 2
        if not self._activity_tick:
            self._update_activity_label(used)
        if self._play_requested and not self.preload_timer.isActive():
            self._prepare_next_player(self.current_group)

    def _shed_memory(self, used: int | None, available: int | None) -> None:
        critical = memory_critical(used, available)
        crash_log.note(f"memory shed: tree={(used or 0) >> 20}MiB available={(available or 0) >> 20}MiB "
                       f"critical={critical} rate={self._current_rate():g} workers={self._analysis.worker_count()}")
        self._memory_pressured = True
        message = ""
        excess = memory_excess(used, available)
        reason = "PC 전체 메모리가 부족해" if memory_system_low(available) else "앱 메모리 한도(4.5GB)에 가까워"
        workers = self._analysis.worker_count()
        if workers:
            count = workers if critical else max(1, -(-excess // WORKER_ESTIMATE))
            self._analysis.shed(count)
            message = f"{reason} 분석 프로세스를 줄였습니다."
            if not critical:
                self._show_memory_status(message)
                return
        for worker in (self._thumbnail_worker, self._storyboard_worker):
            if worker:
                worker.stop()
        self._storyboard_cache.clear()
        if self._preload_channels:
            self._clear_preload()
            message = f"{reason} 다음 영상 미리 열기를 해제했습니다."
            if not critical:
                self._show_memory_status(message)
                return
        if self._play_requested:
            rate = self._current_rate()
            if rate > 1.0:
                self._memory_rate_cap = max(1.0, min(rate / 2, 4.0 if critical else 8.0))
                self.update_playback_speed()
                message = f"{reason} 재생 속도를 {self._memory_rate_cap:g}×로 제한했습니다."
        if message:
            self._show_memory_status(message)

    def _show_memory_status(self, message: str) -> None:
        if message != self._memory_status:
            self._memory_status = message
            self.statusBar().showMessage(message)

    def _pending_preload_bytes(self) -> int:
        """Memory still needed to preload the next clip (reserved ahead of analysis)."""
        if not self._play_requested or self.current_group is None or not self._preload_allowed():
            return 0
        upcoming = self._next_playable_group(self.current_group)
        if upcoming is None:
            return 0
        wanted = len(self._preload_paths(upcoming))
        opened = len(self._preload_channels) if self._preload_group is upcoming else 0
        return max(0, wanted - opened) * PRELOAD_ESTIMATE

    def _govern_analysis(self, used: int | None = None, available: int | None = None) -> None:
        """Size the analysis process pool to the CPU and the memory headroom."""
        if not self._analysis.pending_count():
            # Nothing queued: let idle processes exit and return their memory.
            self._analysis.set_target(self._analysis.running_count())
            return
        if used is None and available is None:
            used, available = memory_snapshot()
        running = self._analysis.worker_count()
        if self._fast_playback():
            maximum = self.settings.analysis_fast
        elif self._play_requested:
            maximum = self.settings.analysis_playing
        else:
            maximum = self.settings.analysis_idle
        if self._memory_pressured:
            target = min(running, maximum)
        else:
            reserve = self._playback_reserve() + self._pending_preload_bytes()
            target = worker_slots(used, available, running, maximum, reserve,
                                  self._analysis.starting_count())
            # Ramp up gradually: a burst of interpreters importing NumPy and
            # OpenCV at once would compete with video decoding.
            target = min(target, running + ANALYSIS_SPAWN_STEP)
        self._analysis.set_target(target)

    def _dispose_next_player(self) -> None:
        """Release one retired decoder per tick to keep the GUI thread smooth."""
        if not self._dispose_queue:
            self.dispose_timer.stop()
            return
        player = self._dispose_queue.pop(0)
        player.stop()
        player.setSource(QUrl())
        player.deleteLater()

    def _clear_preload(self) -> None:
        entries = self._preload_channels
        self._preload_channels = {}
        self._preload_group = None
        for entry in entries.values():
            entry["player"].pause()
            entry["player"].setVideoOutput(None)
            entry["sink"].deleteLater()
            self._dispose_queue.append(entry["player"])
        if entries and not self.dispose_timer.isActive():
            self.dispose_timer.start()

    def _preload_paths(self, group: ClipGroup) -> dict[str, Path]:
        visible = {self.focus_camera} if self.focus_camera else self.selected_cameras
        return {camera: group.clips[camera].path for camera in PRELOAD_ORDER
                if camera in visible and camera in group.clips and not group.clips[camera].encrypted}

    def _prepare_next_player(self, current: ClipGroup | None) -> None:
        """Open the next clip's decoders one channel at a time.

        Each player is paused at frame 0, so at the clip boundary playback
        continues immediately and all prepared channels start in sync.
        Opening one decoder at a time avoids CPU/memory spikes that would
        stutter the clip currently playing; every channel is admitted only
        if it fits the memory budget next to the playing channels.
        """
        if (current is None or current is not self.current_group or not self._play_requested
                or self._memory_pressured):
            return
        if not self._preload_allowed():
            self._clear_preload()
            return
        upcoming = self._next_playable_group(current)
        if upcoming is None:
            self._clear_preload()
            return
        paths = self._preload_paths(upcoming)
        if self._preload_group is not upcoming or any(
                paths.get(camera) != entry["path"] for camera, entry in self._preload_channels.items()):
            self._clear_preload()
            self._preload_group = upcoming
        now = time.monotonic()
        for camera, entry in list(self._preload_channels.items()):
            if not entry["ready"]:
                if now - entry["started"] < PRELOAD_TIMEOUT:
                    return  # wait for the decoder being opened
                self._preload_failed(camera, entry["player"])
        remaining = [camera for camera in paths if camera not in self._preload_channels]
        if not remaining:
            return
        used, available = memory_snapshot()
        if preload_slots(used, available, 1, self._playback_reserve()) < 1:
            return  # retried by the memory governor when room appears
        camera = remaining[0]
        player = QMediaPlayer(self)
        sink = QVideoSink(self)
        self._preload_channels[camera] = {"path": paths[camera], "player": player, "sink": sink,
                                          "ready": False, "started": now}
        player.setVideoSink(sink)
        sink.videoFrameChanged.connect(lambda frame, c=camera, p=player: self._preload_frame_ready(c, p, frame))
        player.errorOccurred.connect(lambda _error, _message, c=camera, p=player: self._preload_failed(c, p))
        player.setSource(QUrl.fromLocalFile(str(paths[camera])))
        player.pause()  # decode and hold frame 0 without playing

    def _preload_frame_ready(self, camera: str, player: QMediaPlayer, frame: QVideoFrame) -> None:
        entry = self._preload_channels.get(camera)
        if entry is not None and entry["player"] is player and frame.isValid() and not entry["ready"]:
            entry["ready"] = True
            player.pause()
            QTimer.singleShot(50, lambda: self._prepare_next_player(self.current_group))

    def _preload_failed(self, camera: str, player: QMediaPlayer) -> None:
        entry = self._preload_channels.get(camera)
        if entry is not None and entry["player"] is player:
            del self._preload_channels[camera]
            player.stop()
            player.setVideoOutput(None)
            player.setSource(QUrl())
            player.deleteLater()
            entry["sink"].deleteLater()
            QTimer.singleShot(50, lambda: self._prepare_next_player(self.current_group))

    def _take_preload(self, group: ClipGroup) -> dict[str, tuple[Path, QMediaPlayer]] | None:
        if self._preload_group is not group:
            self._clear_preload()
            return None
        prepared = {}
        for camera, entry in list(self._preload_channels.items()):
            if not entry["ready"]:
                continue
            entry["player"].setVideoOutput(None)
            entry["sink"].deleteLater()
            prepared[camera] = (entry["path"], entry["player"])
            del self._preload_channels[camera]
        self._clear_preload()
        return prepared or None

    def _advance_after_end(self, group: ClipGroup | None, player: QMediaPlayer) -> None:
        if group is None or group is not self.current_group or player is not self.master or not self._play_requested:
            return
        self._watch_last = None
        next_group = self._next_playable_group(group)
        if next_group is None:
            self._play_requested = False
            self._clear_preload()
            for tile in self.active_tiles():
                tile.player.pause()
            self._set_play_icon(False)
            return
        # At high decode load, starting prepared decoders is far more memory
        # hungry than opening fresh ones (see PRELOAD_MAX_DECODE_LOAD).
        prepared = None
        if self._preload_allowed():
            prepared = self._take_preload(next_group)
        else:
            self._clear_preload()
        self.selection_timer.stop()
        self._selection_pending = None
        self._explicit_selection = next_group
        # Switch decoders first; list selection and queue reordering after.
        if prepared is None:
            self.load_group(next_group, autoplay=True)
        else:
            self.load_group(next_group, autoplay=True, prepared=prepared)
        list_index = next((index for index, item in enumerate(self.groups) if item is next_group), -1)
        if list_index >= 0:
            self.group_list.setCurrentItem(self.group_list.topLevelItem(list_index))
        if self.current_root is not None:
            QTimer.singleShot(0, self.start_telemetry_index)

    def duration_changed(self, duration: int) -> None:
        self.slider.setRange(0, max(0, duration))
        self.update_time_label(self.pending_seek_ms or self.master.position(), duration)

    def update_time_label(self, position: int, duration: int) -> None:
        def fmt(value: int) -> str:
            total = max(0, value // 1000)
            return f"{total // 60:02d}:{total % 60:02d}"
        self.time_label.setText(f"{fmt(position)} / {fmt(duration)}")

    def seek(self, position: int) -> None:
        self.pending_seek_ms = None
        self.set_all_positions(position)

    def seek_relative(self, delta: int) -> None:
        self.set_all_positions(max(0, self.master.position() + delta))

    def set_all_positions(self, position: int, manual: bool = True) -> None:
        self._watch_last = None
        if self.preview_mode:
            self.pending_seek_ms = position
            self.slider.setValue(position)
            for tile in self.active_tiles():
                tile.storyboard.set_position(position)
            self.update_time_label(position, self.slider.maximum())
            self.update_telemetry(position)
            return
        self._syncing = True
        try:
            for tile in self.active_tiles():
                tile.player.setPosition(position)
        finally:
            self._syncing = False
        self.slider.setValue(position)
        for tile in self.active_tiles():
            tile.storyboard.set_position(position)
        self.update_time_label(position, self.slider.maximum())
        self.update_telemetry(position)

    def toggle_play(self) -> None:
        players = [tile.player for tile in self.active_tiles()]
        if not players:
            return
        if self._play_requested:
            self._play_requested = False
            self._watch_last = None
            for player in players:
                player.pause()
            self._set_play_icon(False)
            self.preload_timer.stop()
            self.start_motion_scan()
        else:
            self._play_requested = True
            self.preview_mode = False
            self._stop_background_preview(keep_motion=self.adaptive_speed)
            for tile in self.active_tiles():
                tile.show_video()
                tile.player.play()
            self._dispatch_pending_seek()
            self._set_play_icon(True)
            self.update_playback_speed()
            if self.current_group:
                self.preload_timer.start()

    def video_error(self, tile: VideoTile, message: str) -> None:
        if message:
            self.statusBar().showMessage(f"{tile.camera}: {message}")

    def decrypt_dialog(self) -> None:
        self.open_decrypt_dialog()

    def open_decrypt_dialog(self) -> None:
        if not self.current_root:
            QMessageBox.information(self, "폴더 선택 필요", "먼저 TeslaCam 폴더를 열어 주세요.")
            return
        source_root = self._repair_source_root or self.current_root
        encrypted = [clip for clip in discover_clips(source_root) if clip.encrypted]
        if source_root != self.current_root:
            encrypted = [
                clip for clip in encrypted
                if not is_plain_mp4(self.current_root / clip.path.relative_to(source_root))
            ]
        if not encrypted:
            QMessageBox.information(self, "암호화 영상 없음", "현재 폴더에서 암호화된 영상을 찾지 못했습니다.")
            return
        default_output = self.current_root if source_root != self.current_root else self.current_root.parent / "TeslaCam_Decryped"
        if not default_output.exists():
            existing_correct_name = self.current_root.parent / "TeslaCam_Decrypted"
            if existing_correct_name.exists():
                default_output = existing_correct_name
        dialog = TokenDialog(default_output, self)
        self._decrypt_dialog = dialog
        self._decryption_started = False
        dialog.start_requested.connect(lambda: self.begin_decryption_from_dialog(dialog))
        dialog.append_log(f"암호화 영상 {len(encrypted)}개를 찾았습니다.")
        dialog.append_log(EVENT_JSON_NOTE)
        if source_root != self.current_root:
            dialog.delete_source.setChecked(False)
            dialog.delete_source.setEnabled(False)
            dialog.output.setReadOnly(True)
            dialog.choose_button.setEnabled(False)
            dialog.append_log(f"분리 보관된 암호화 영상을 복호화합니다: {source_root}")
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def begin_decryption_from_dialog(self, dialog: TokenDialog) -> None:
        if self._decryption_started or (self._thread and self._thread.isRunning()):
            dialog.set_status("이미 복호화를 진행 중입니다.")
            return
        token = dialog.token.text().strip()
        output = Path(dialog.output.text().strip()).expanduser()
        if not token:
            dialog.set_status("Bearer 토큰을 붙여 넣어 주세요. 얻는 방법은 창 위쪽 안내를 참고하세요.")
            return
        if not output:
            dialog.set_status("출력 폴더를 지정해 주세요.")
            return
        try:
            resolved_output = output.resolve()
            resolved_source = (self._repair_source_root or self.current_root).resolve()
            if self._repair_source_root and resolved_output != self.current_root.resolve():
                dialog.set_status("분리 보관된 영상의 복호화 출력은 현재 TeslaCam_Decrypted 폴더로 고정됩니다.")
                return
            if resolved_output == resolved_source or resolved_output.is_relative_to(resolved_source):
                dialog.set_status("출력 폴더는 원본 TeslaCam 폴더 바깥에 지정해야 합니다.")
                return
        except OSError as exc:
            dialog.set_status(str(exc))
            return
        if dialog.delete_source.isChecked():
            answer = QMessageBox.warning(
                self,
                "원본 암호화 영상 삭제 확인",
                "복호화가 성공적으로 확인된 원본 암호화 MP4만 삭제합니다. 계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                dialog.set_status("복호화를 시작하지 않았습니다.")
                return
        dialog.set_running()
        self._decryption_started = True
        dialog.append_log("복호화를 시작합니다. 암호화 파일은 PC 밖으로 전송하지 않습니다.")
        self.start_decryption(token, output, dialog.delete_source.isChecked(), dialog)

    def start_decryption(
        self,
        token: str,
        output: Path,
        delete_source: bool,
        dialog: TokenDialog | None = None,
    ) -> None:
        self._last_output = output
        self._decrypt_dialog = dialog
        self._thread = QThread(self)
        self._worker = DecryptWorker(self._repair_source_root or self.current_root, output, token, delete_source)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda value, text: self.statusBar().showMessage(f"{text} ({value}%)"))
        if dialog:
            self._worker.progress.connect(dialog.set_progress)
        self._worker.finished.connect(self.decryption_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.statusBar().showMessage("복호화를 시작했습니다…")

    @Slot(str, int, int, object)
    def decryption_finished(self, message: str, success: int, failed: int, errors: object) -> None:
        details = "\n".join(errors) if errors else ""
        if self._decrypt_dialog:
            self._decrypt_dialog.set_finished(message, success, failed)
            if details:
                self._decrypt_dialog.append_log(details[:10000])
            self._thread = None
            self._worker = None
            self._decryption_started = False
            if success and self._last_output and self._last_output.exists():
                self.load_folder(self._last_output)
                self._decrypt_dialog.append_log("복호화된 출력 폴더를 목록에 연결했습니다.")
            return
        QMessageBox.information(self, "복호화 결과", message)
        self._thread = None
        self._worker = None
        self._decryption_started = False
        if success and self._last_output and self._last_output.exists():
            self.load_folder(self._last_output)

    def closeEvent(self, event) -> None:
        if self._thread and self._thread.isRunning():
            QMessageBox.information(self, "복호화 진행 중", "복호화가 끝난 뒤 프로그램을 종료해 주세요.")
            event.ignore()
            return
        if self._thumbnail_worker:
            self._thumbnail_worker.stop()
        self.memory_timer.stop()
        self._clear_preload()
        while self._dispose_queue:
            self._dispose_next_player()
        if self._storyboard_worker:
            self._storyboard_worker.stop()
        self._analysis.shutdown()
        threads = (self._thumbnail_thread, self._storyboard_thread, self._telemetry_thread)
        if any(thread and thread.isRunning() for thread in threads):
            self.statusBar().showMessage("백그라운드 작업을 종료하는 중입니다…")
            event.ignore()
            QTimer.singleShot(500, self.close)
            return
        self.motion_cache_timer.stop()
        self.watched_timer.stop()
        if not self._data_cleared:
            self._save_motion_cache()
            self._save_watched_items()
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            path = Path(urls[0].toLocalFile())
            if path.is_dir():
                self.load_folder(path)
                event.acceptProposedAction()


def main() -> None:
    crash_log.install(data_file("crash.log"))
    app = QApplication(sys.argv)
    app.setApplicationName("T6 Viewer")
    window_style.install(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
