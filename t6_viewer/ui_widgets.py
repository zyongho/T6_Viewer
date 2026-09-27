"""Small reusable widgets: settings dialog, header filter popups, splitter."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QDate, QPoint, QTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QComboBox,
    QMessageBox,
    QSplitter,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from dataclasses import replace

from .app_settings import (
    CPU_COUNT,
    REFERENCE_PC,
    OBJECT_TARGETS,
    RECOMMENDED_DECODE_LOAD,
    RISKY_DECODE_LOAD,
    Settings,
    estimated_peak_gib,
    rate_for_channels,
)

POPUP_STYLE = """
QFrame#filterPopup { background: #1c222b; border: 1px solid #59b5ff; border-radius: 4px; }
QLabel, QCheckBox { color: #e8edf2; }
QDateEdit, QTimeEdit { color: #edf2f7; background: #202832; border: 1px solid #435164; padding: 3px; }
QPushButton { color: #edf2f7; background: #2a3441; border: 1px solid #435164; padding: 4px 10px; border-radius: 3px; }
QPushButton:hover { background: #354456; }
"""


class SettingsDialog(QDialog):
    """Decode-load ceiling and analysis process counts, with guidance."""

    clear_requested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("재생·분석 설정")
        self.setMinimumWidth(620)
        self.setStyleSheet(
            "QDialog { background: #151a21; } QLabel, QCheckBox { color: #edf2f7; }"
            "QCheckBox::indicator { width: 15px; height: 15px; }"
            "QCheckBox::indicator:unchecked { background: #202832; border: 1px solid #66788b; border-radius: 3px; }"
            "QCheckBox::indicator:checked { background: #2f8dcc; border: 1px solid #59b5ff; border-radius: 3px; }"
            "QSpinBox { color: #edf2f7; background: #202832; border: 1px solid #536477; padding: 3px; }"
            "QPushButton { color: #ffffff; background: #2f6f9f; border: 1px solid #6b9bc2; padding: 5px 12px; border-radius: 4px; }"
        )
        self.load = QSpinBox()
        self.load.setRange(6, 96)
        self.load.setValue(settings.max_decode_load)
        self.playing = QSpinBox()
        self.playing.setRange(0, CPU_COUNT)
        self.playing.setValue(settings.analysis_playing)
        self.fast = QSpinBox()
        self.fast.setRange(0, CPU_COUNT)
        self.fast.setValue(settings.analysis_fast)
        self.idle = QSpinBox()
        self.idle.setRange(1, CPU_COUNT)
        self.idle.setValue(settings.analysis_idle)
        self._original = settings
        self.background = QCheckBox("백그라운드 분석 허용 (보고 있지 않은 영상도 미리 분석)")
        self.background.setChecked(settings.analysis_background)
        self.items = {
            "analysis_objects": QCheckBox("객체 인식 (비주행 영상의 사람·차량 등 움직임 → 신호등·스킵·자동 스킵)"),
            "analysis_storyboard": QCheckBox("스토리보드 (영상 아래 20구간 대표 장면)"),
            "analysis_sei": QCheckBox("SEI 주행 정보 (지도 경로·주행 여부·속도 등 텔레메트리)"),
            "analysis_preview": QCheckBox("썸네일 (목록에서 선택했을 때 영상 영역에 보이는 대표 화면 한 장)"),
        }
        for name, box in self.items.items():
            box.setChecked(getattr(settings, name))
        labels = {"person": "🚶 사람", "car": "🚗 자동차(버스·트럭 포함)", "motorcycle": "🏍 오토바이",
                  "bicycle": "🚲 자전거·PM"}
        self.targets = {}
        for name in OBJECT_TARGETS:
            box = QCheckBox(labels[name])
            box.setChecked(name in settings.object_categories)
            box.toggled.connect(lambda checked, item=name: self._keep_one_target(item, checked))
            self.targets[name] = box
        self.targets["bicycle"].setToolTip("킥보드·카트는 모델에 별도 클래스가 없어 놓칠 수 있습니다.")
        self.items["analysis_objects"].toggled.connect(
            lambda checked: [box.setEnabled(checked) for box in self.targets.values()])
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.scope.setStyleSheet("color: #aebdca;")
        self.background.toggled.connect(self._refresh_scope)
        for box in self.items.values():
            box.toggled.connect(self._refresh_scope)
        self.rates = QLabel()
        self.estimate = QLabel()
        self.estimate.setWordWrap(True)

        form = QFormLayout()
        load_row = QHBoxLayout()
        load_row.addWidget(self.load)
        recommend = QPushButton(f"기본값 {RECOMMENDED_DECODE_LOAD}")
        recommend.clicked.connect(lambda: self.load.setValue(RECOMMENDED_DECODE_LOAD))
        load_row.addWidget(recommend)
        load_row.addStretch(1)
        form.addRow("채널 수 × 배속 상한", load_row)
        form.addRow("채널별 최대 배속", self.rates)
        form.addRow("예상 최대 메모리(참고)", self.estimate)
        load_help = QLabel(
            "동시에 재생하는 카메라 수 × 배속이 이 값을 넘지 않게 배속을 낮춥니다. "
            f"기본값 {RECOMMENDED_DECODE_LOAD}는 {REFERENCE_PC}에서 앱 메모리 4.5GB 안에 "
            "안정적으로 머문 값이며, 그 PC에서는 36 이상일 때 메모리가 수 초 만에 수 GB씩 늘어났습니다. "
            "적정값은 그래픽 성능에 따라 다릅니다. 외장 그래픽카드가 있는 PC는 더 높여도 될 수 있으니, "
            "올릴 때는 오른쪽 아래 상태 표시줄의 메모리 사용량을 보며 조금씩 올려 보세요."
        )
        load_help.setWordWrap(True)
        load_help.setStyleSheet("color: #aebdca;")
        form.addRow("", load_help)
        form.addRow("객체 인식 프로세스 · 재생 중(2× 이하)", self.playing)
        form.addRow("객체 인식 프로세스 · 고배속(2× 초과) 재생 중", self.fast)
        form.addRow("객체 인식 프로세스 · 정지·일시정지 시 최대", self.idle)
        defaults = Settings()
        worker_help = QLabel(
            f"지금 PC의 논리 코어는 {CPU_COUNT}개이며, 이에 맞춘 기본값은 재생 중 "
            f"{defaults.analysis_playing}, 고배속 {defaults.analysis_fast}, 정지 시 {defaults.analysis_idle}개입니다. "
            "프로세스를 늘릴수록 분석이 빨라지다가 코어 수 근처에서 더 빨라지지 않습니다"
            f"(참고: {REFERENCE_PC}, 16스레드에서 1개 대비 12개일 때 약 4배, 그 이상은 동일).\n"
            "프로세스 하나당 메모리 약 190MB를 씁니다. 분석은 낮은 우선순위로 동작해 재생을 방해하지 않고, "
            "앱 메모리 한도(4.5GB)에 가까우면 이 설정보다 적게 실행됩니다."
        )
        worker_help.setWordWrap(True)
        worker_help.setStyleSheet("color: #aebdca;")
        form.addRow("", worker_help)
        analysis = QVBoxLayout()
        analysis.addWidget(QLabel("<b>분석</b>"))
        analysis.addWidget(self.background)
        for name, box in self.items.items():
            analysis.addWidget(box)
            if name == "analysis_objects":
                targets = QHBoxLayout()
                targets.setContentsMargins(24, 0, 0, 2)
                caption = QLabel("인식 대상(비주행 영상):")
                caption.setToolTip("주차·정차 중 녹화된 영상에서만 인식합니다. 주행 영상은 카메라가 움직여\n"
                                   "배경 이동과 물체 이동을 구분할 수 없어 분석하지 않습니다.")
                targets.addWidget(caption)
                for target in self.targets.values():
                    target.setEnabled(box.isChecked())
                    targets.addWidget(target)
                targets.addStretch(1)
                analysis.addLayout(targets)
        analysis.addWidget(self.scope)
        layout = QVBoxLayout(self)
        layout.addLayout(analysis)
        layout.addSpacing(8)
        layout.addWidget(QLabel("<b>재생 성능</b>"))
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        help_button = buttons.addButton("❓ 도움말", QDialogButtonBox.HelpRole)
        help_button.clicked.connect(open_help)
        layout.addSpacing(8)
        layout.addWidget(QLabel("<b>지도</b>"))
        map_row = QHBoxLayout()
        map_row.addWidget(QLabel("브이월드 API 키"))
        self.vworld_key = QLineEdit(settings.vworld_key)
        self.vworld_key.setPlaceholderText("비워 두면 OpenStreetMap 지도(시험용)로 표시")
        self.vworld_key.setStyleSheet("color: #edf2f7; background: #202832; border: 1px solid #536477; padding: 3px;")
        map_row.addWidget(self.vworld_key, 1)
        issue = QPushButton("키 발급 안내")
        issue.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://www.vworld.kr/dev/v4api.do")))
        map_row.addWidget(issue)
        layout.addLayout(map_row)
        map_note = QLabel("브이월드는 국토교통부가 무료로 제공하는 국내 지도입니다(회원 가입 후 무료 API 키 발급). "
                          "키가 없으면 OpenStreetMap 타일을 쓰는데, 이는 개인 시험용이며 공개 배포 앱에는 허가가 필요합니다.")
        map_note.setWordWrap(True)
        map_note.setStyleSheet("color: #aebdca;")
        layout.addWidget(map_note)
        layout.addSpacing(8)
        layout.addWidget(QLabel("<b>저장 위치</b>"))
        layout.addLayout(self._storage_rows())
        layout.addWidget(buttons)
        for box in (self.load, self.playing):
            box.valueChanged.connect(self._refresh)
        self._refresh()
        self._refresh_scope()

    def _storage_rows(self) -> QVBoxLayout:
        from .app_paths import data_dir

        rows = QVBoxLayout()
        folder = data_dir()
        path_row = QHBoxLayout()
        path_label = QLabel(f"설정·시청 기록·분석 목록·오류 기록: <code>{folder}</code>")
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(path_label, 1)
        open_button = QPushButton("폴더 열기")
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))))
        path_row.addWidget(open_button)
        rows.addLayout(path_row)
        cache_row = QHBoxLayout()
        cache_note = QLabel("영상별 분석 결과(썸네일·스토리보드·객체 인식·SEI): 각 영상 폴더의 "
                            "<code>.t6_viewer_cache</code>")
        cache_note.setStyleSheet("color: #aebdca;")
        cache_row.addWidget(cache_note, 1)
        clear_button = QPushButton("모든 설정·기록 삭제…")
        clear_button.setStyleSheet("QPushButton { background: #8a2b28; border-color: #f27770; }")
        clear_button.setToolTip("위 폴더를 통째로 지우고 프로그램을 종료합니다. "
                                "프로그램을 더 쓰지 않을 때 흔적을 지우는 용도입니다.")
        clear_button.clicked.connect(self._request_clear)
        cache_row.addWidget(clear_button)
        rows.addLayout(cache_row)
        return rows

    def _request_clear(self) -> None:
        self.reject()
        self.clear_requested.emit()

    def _keep_one_target(self, name: str, checked: bool) -> None:
        if not checked and not any(box.isChecked() for box in self.targets.values()):
            self.targets[name].setChecked(True)  # at least one object type

    def _refresh_scope(self) -> None:
        chosen = [box.text().split(" (")[0] for box in self.items.values() if box.isChecked()]
        if not chosen:
            text = "분석하지 않고 재생만 합니다. 이미 만들어 둔 결과는 그대로 표시합니다."
        elif self.background.isChecked():
            text = f"모든 영상: {', '.join(chosen)} — 보고 있는 영상을 먼저 처리합니다."
        else:
            text = f"보고 있는 영상만: {', '.join(chosen)} — 다른 영상은 미리 분석하지 않습니다."
        self.scope.setText(text)
        workers_needed = self.items["analysis_objects"].isChecked() or self.items["analysis_storyboard"].isChecked()
        for box in (self.playing, self.fast, self.idle):
            box.setEnabled(workers_needed)

    def _refresh(self) -> None:
        load = self.load.value()
        self.rates.setText(" · ".join(
            f"{channels}채널 {rate_for_channels(load, channels):g}×" for channels in (6, 4, 3, 2, 1)
        ))
        peak = estimated_peak_gib(load, self.playing.value())
        color = "#ff6b6b" if peak > 4.5 or load > RISKY_DECODE_LOAD else "#ffb547" if peak > 4.0 else "#48cf76"
        warning = ""
        if load > RISKY_DECODE_LOAD:
            warning = " · 급증 위험 구간"
        elif peak > 4.5:
            warning = " · 앱 메모리 한도(4.5GB) 초과 예상 — 초과하면 분석 프로세스부터 줄입니다"
        self.estimate.setText(
            f'<span style="color:{color}">약 {peak:.1f} GB</span> '
            f"(6채널 최고 배속 재생 + 재생 중 분석 {self.playing.value()}개, 개발 테스트 PC 측정값 기반 추정){warning}"
        )

    def settings(self) -> Settings:
        return replace(
            self._original,
            max_decode_load=self.load.value(), analysis_playing=self.playing.value(),
            analysis_idle=self.idle.value(), analysis_fast=self.fast.value(),
            analysis_background=self.background.isChecked(),
            vworld_key=self.vworld_key.text().strip(),
            object_categories=[name for name, box in self.targets.items() if box.isChecked()],
            **{name: box.isChecked() for name, box in self.items.items()},
        ).clamp()


def open_help() -> None:
    """Open the bundled HTML help in the default browser."""
    from .app_paths import resource

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(resource("docs", "help.html"))))


class _Popup(QFrame):
    applied = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("filterPopup")
        self.setStyleSheet(POPUP_STYLE)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(10, 8, 10, 8)

    def _buttons(self, reset) -> None:
        row = QHBoxLayout()
        clear = QPushButton("전체")
        clear.clicked.connect(reset)
        apply = QPushButton("적용")
        apply.setDefault(True)
        apply.clicked.connect(self._apply)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(apply)
        self.body.addLayout(row)

    def _apply(self) -> None:
        self.applied.emit(self.value())
        self.close()

    def show_below(self, global_point: QPoint) -> None:
        self.adjustSize()
        self.move(global_point)
        self.show()


class ChecklistPopup(_Popup):
    """Excel-like value filter: ``None`` means everything is shown."""

    def __init__(self, title: str, options: list[tuple[str, str]], selected: set[str] | None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.body.addWidget(QLabel(f"<b>{title}</b>"))
        self.boxes: dict[str, QCheckBox] = {}
        for value, label in options:
            box = QCheckBox(label)
            box.setChecked(selected is None or value in selected)
            self.boxes[value] = box
            self.body.addWidget(box)
        self._buttons(lambda: [box.setChecked(True) for box in self.boxes.values()])

    def value(self) -> set[str] | None:
        chosen = {value for value, box in self.boxes.items() if box.isChecked()}
        return None if len(chosen) == len(self.boxes) else chosen


WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


class DateTimePopup(_Popup):
    """Date range, weekdays and time-of-day range for the 일시 column."""

    def __init__(self, current: dict, first: dt.date | None, last: dt.date | None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        today = dt.date.today()
        first, last = first or today, last or today
        self.body.addWidget(QLabel("<b>일시</b>"))
        self.use_date = QCheckBox("날짜")
        self.date_from = QDateEdit(QDate(*(current.get("date") or (first, last))[0].timetuple()[:3]))
        self.date_to = QDateEdit(QDate(*(current.get("date") or (first, last))[1].timetuple()[:3]))
        for edit in (self.date_from, self.date_to):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        self.use_date.setChecked(current.get("date") is not None)
        grid = QGridLayout()
        grid.addWidget(self.use_date, 0, 0)
        grid.addWidget(self.date_from, 0, 1)
        grid.addWidget(QLabel("~"), 0, 2)
        grid.addWidget(self.date_to, 0, 3)
        self.use_time = QCheckBox("시간")
        time_range = current.get("time") or (dt.time(0, 0), dt.time(23, 59))
        self.time_from = QTimeEdit(QTime(time_range[0].hour, time_range[0].minute))
        self.time_to = QTimeEdit(QTime(time_range[1].hour, time_range[1].minute))
        for edit in (self.time_from, self.time_to):
            edit.setDisplayFormat("HH:mm")
        self.use_time.setChecked(current.get("time") is not None)
        grid.addWidget(self.use_time, 1, 0)
        grid.addWidget(self.time_from, 1, 1)
        grid.addWidget(QLabel("~"), 1, 2)
        grid.addWidget(self.time_to, 1, 3)
        self.body.addLayout(grid)
        self.body.addWidget(QLabel("요일"))
        weekdays = current.get("weekdays")
        row = QHBoxLayout()
        self.weekday_boxes = []
        for index, name in enumerate(WEEKDAYS):
            box = QCheckBox(name)
            box.setChecked(weekdays is None or index in weekdays)
            self.weekday_boxes.append(box)
            row.addWidget(box)
        self.body.addLayout(row)
        self.date_from.dateChanged.connect(lambda: self.use_date.setChecked(True))
        self.date_to.dateChanged.connect(lambda: self.use_date.setChecked(True))
        self.time_from.timeChanged.connect(lambda: self.use_time.setChecked(True))
        self.time_to.timeChanged.connect(lambda: self.use_time.setChecked(True))
        self._buttons(self._reset)

    def _reset(self) -> None:
        self.use_date.setChecked(False)
        self.use_time.setChecked(False)
        for box in self.weekday_boxes:
            box.setChecked(True)

    def value(self) -> dict:
        weekdays = {index for index, box in enumerate(self.weekday_boxes) if box.isChecked()}
        return {
            "date": (self.date_from.date().toPython(), self.date_to.date().toPython())
            if self.use_date.isChecked() else None,
            "time": (self.time_from.time().toPython(), self.time_to.time().toPython())
            if self.use_time.isChecked() else None,
            "weekdays": None if len(weekdays) == len(WEEKDAYS) else weekdays,
        }


class CollapsibleSplitter(QSplitter):
    """A splitter whose side panes can be hidden and restored by a button.

    ``collapsed_changed`` fires whenever a pane becomes hidden or visible,
    whether by :meth:`toggle` or by dragging the handle to the edge.
    """

    collapsed_changed = Signal()

    def __init__(self, orientation, parent: QWidget | None = None):
        super().__init__(orientation, parent)
        self._restore_sizes: dict[int, int] = {}
        self._last_state: tuple[bool, ...] = ()
        self.splitterMoved.connect(lambda *_args: self._check_state())

    def setSizes(self, sizes) -> None:
        super().setSizes(sizes)
        self._check_state()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._check_state()

    def is_collapsed(self, index: int) -> bool:
        sizes = self.sizes()
        return index < len(sizes) and sizes[index] == 0

    def toggle(self, index: int) -> None:
        sizes = self.sizes()
        neighbour = 1 if index == 0 else index - 1
        if sizes[index] == 0:
            wanted = self._restore_sizes.get(index) or max(260, self.width() // 4)
            sizes[index] = wanted
            sizes[neighbour] = max(100, sizes[neighbour] - wanted)
        else:
            self._restore_sizes[index] = sizes[index]
            sizes[neighbour] += sizes[index]
            sizes[index] = 0
        self.setSizes(sizes)
        self._check_state()

    def _check_state(self) -> None:
        sizes = self.sizes()
        for index, size in enumerate(sizes):
            if size:
                self._restore_sizes[index] = size
        state = tuple(size == 0 for size in sizes)
        if state != self._last_state:
            self._last_state = state
            self.collapsed_changed.emit()


class AboutDialog(QDialog):
    """Program information and the notices required to distribute it."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from . import APP_LICENSE, AUTHOR, PROJECT_URL, __version__
        from .app_paths import resource

        self.setWindowTitle("프로그램 정보")
        self.resize(720, 620)
        self.setStyleSheet(
            "QDialog { background: #151a21; } QLabel { color: #edf2f7; }"
            "QTextBrowser { color: #dce4ed; background: #1c222b; border: 1px solid #435164; }"
            "QComboBox { color: #edf2f7; background: #202832; border: 1px solid #435164; padding: 3px; }"
            "QPushButton { color: #ffffff; background: #2f6f9f; border: 1px solid #6b9bc2;"
            " padding: 5px 12px; border-radius: 4px; }"
        )
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(QIcon(str(resource("t6_viewer", "assets", "app_icon.png"))).pixmap(72, 72))
        header.addWidget(icon)
        details = []
        if AUTHOR:
            details.append(f"개발: {AUTHOR}")
        if APP_LICENSE:
            details.append(f"라이선스: {APP_LICENSE}")
        if PROJECT_URL:
            details.append(f"<a style='color:#59b5ff' href='{PROJECT_URL}'>{PROJECT_URL}</a> (소스·문의·업데이트)")
        title = QLabel(
            f"<b style='font-size:16px'>T6 Viewer</b> &nbsp; Ver {__version__}<br>"
            "Tesla dashcam smart viewer — 6채널 동시 재생 · 움직임 스킵 · 주행 정보 · 로컬 복호화<br>"
            + ("<br>".join(details) + "<br>" if details else "")
            + "<span style='color:#aebdca'>Tesla, Inc.와 관련이 없는 비공식 프로그램입니다. "
            "영상은 이 PC에서만 처리하며 외부로 전송하지 않습니다. 이 프로그램은 있는 그대로 제공되며, "
            "사용으로 생긴 결과에 대해 보증하지 않습니다.</span>"
        )
        title.setWordWrap(True)
        title.setOpenExternalLinks(True)
        header.addWidget(title, 1)
        self.documents = {"버전 기록": resource("docs", "CHANGELOG.md"),
                          "이 프로그램의 라이선스 (AGPL-3.0)": resource("LICENSE"),
                          "오픈소스 고지": resource("licenses", "THIRD_PARTY_NOTICES.md")}
        for path in sorted(resource("licenses").glob("*.txt")):
            self.documents[f"라이선스 전문: {path.stem}"] = path
        self.choice = QComboBox()
        self.choice.addItems(list(self.documents))
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        self.text.document().setDefaultStyleSheet("a { color: #59b5ff; }")
        self.choice.currentTextChanged.connect(self._show)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        help_button = buttons.addButton("❓ 도움말", QDialogButtonBox.HelpRole)
        help_button.clicked.connect(open_help)
        about_qt = buttons.addButton("Qt 정보", QDialogButtonBox.HelpRole)
        about_qt.clicked.connect(lambda: QMessageBox.aboutQt(self, "Qt 정보"))
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.choice)
        layout.addWidget(self.text, 1)
        layout.addWidget(buttons)
        self._show(self.choice.currentText())

    def _show(self, name: str) -> None:
        path = self.documents.get(name)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, AttributeError):
            self.text.setPlainText("고지 파일을 찾을 수 없습니다.")
            return
        if path.suffix == ".md":
            self.text.setMarkdown(content)
        else:
            self.text.setPlainText(content)
