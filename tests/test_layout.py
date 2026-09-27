from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QSplitter, QTreeWidgetItem

from tesla_viewer.main_window import CAMERA_DISPLAY_ORDER, MainWindow, StoryboardStrip
from tesla_viewer.crypto import ClipGroup, ClipInfo


def test_camera_rows_match_pillar_front_and_repeater_rear(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert CAMERA_DISPLAY_ORDER == (
        "left_pillar", "front", "right_pillar",
        "left_repeater", "back", "right_repeater",
    )
    for index, camera in enumerate(CAMERA_DISPLAY_ORDER):
        position = window.grid_layout.getItemPosition(
            window.grid_layout.indexOf(window.tiles_by_camera[camera])
        )
        assert position[:2] == (index // 3, index % 3)
    window.close()


def test_thumbnail_grid_stays_within_video_pane_when_map_is_collapsed(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1438, 900)
    window.show()
    (splitter,) = window.findChildren(QSplitter)   # list | video | map
    splitter.setSizes([420, 1014, 0])
    for tile in window.tiles:
        tile._thumbnail_image = QPixmap(640, 400)
        tile._show_thumbnail()
    app.processEvents()
    assert sum(splitter.sizes()) <= window.width()
    assert splitter.sizes()[2] == 0
    assert window.map_toggle.text() == "◀"            # map hidden: arrow brings it back
    window.map_toggle.click()
    assert splitter.sizes()[2] > 0 and window.map_toggle.text() == "▶"
    window.list_toggle.click()
    assert splitter.sizes()[0] == 0 and window.list_toggle.text() == "▶"
    assert all(tile.x() + tile.width() <= window.grid_widget.width() for tile in window.tiles)
    window.close()


def test_speed_buttons_follow_play_button(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    layout = window.play_button.parentWidget().layout()
    assert [layout.itemAt(index).widget() for index in range(9)] == [
        window.previous_clip_button,
        window.play_button,
        window.next_clip_button,
        window.speed_buttons[1.0],
        window.speed_buttons[2.0],
        window.speed_buttons[4.0],
        window.speed_buttons[8.0],
        window.speed_buttons[16.0],
        window.adaptive_button,
    ]
    assert layout.itemAt(9).widget() is window.next_change_button
    assert layout.itemAt(10).widget() is window.auto_jump_button
    assert layout.itemAt(12).widget() is window.slider
    # 40 % narrower play button; previous/next at half its width
    assert window.play_button.width() == 50
    assert window.previous_clip_button.width() == window.next_clip_button.width() == 25
    window.close()


def test_front_and_rear_stack_in_narrow_space(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    assert window._columns_for_layout(2, 500, 700) == 1
    assert window._columns_for_layout(2, 1800, 500) == 2
    window.selected_cameras = {"front", "back"}
    window.grid_widget.resize(500, 700)
    window.relayout_tiles()
    front_index = window.grid_layout.indexOf(window.tiles_by_camera["front"])
    rear_index = window.grid_layout.indexOf(window.tiles_by_camera["back"])
    assert window.grid_layout.getItemPosition(front_index)[:2] == (0, 0)
    assert window.grid_layout.getItemPosition(rear_index)[:2] == (1, 0)
    assert window.tiles_by_camera["back"].title.text() == "Rear (no clip)"
    window.close()


def test_storyboard_has_twenty_clickable_time_segments(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    strip = StoryboardStrip()
    strip.set_segment_count(20)
    strip.resize(1000, 62)
    strip.image = QPixmap(2000, 54)
    strip.image.fill(Qt.darkGray)
    strip.duration_ms = 20000
    values = []
    strip.seek_requested.connect(values.append)
    strip.show()
    app.processEvents()
    QTest.mouseClick(strip, Qt.LeftButton, pos=QPoint(525, 30))
    assert values == [10000]
    strip.close()


def test_playback_end_advances_to_later_playable_group(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    def group(stamp, encrypted=False):
        return ClipGroup(stamp, {"front": ClipInfo(tmp_path / f"{stamp}.mp4", "front", None, encrypted)}, tmp_path)
    earlier = group("2026-09-26 08:00:00")
    encrypted = group("2026-09-26 08:01:00", encrypted=True)
    later = group("2026-09-26 08:02:00")
    window.groups = [later, encrypted, earlier]  # displayed newest first
    for _ in window.groups:
        window.group_list.addTopLevelItem(QTreeWidgetItem())
    window.current_group = earlier
    window._play_requested = True
    calls = []
    monkeypatch.setattr(window, "load_group", lambda item, **kwargs: calls.append((item, kwargs)))
    window._advance_after_end(earlier, window.master)
    assert calls == [(later, {"autoplay": True})]
    assert window.group_list.currentIndex().row() == 0
    calls.clear()
    window.current_group = later
    window._advance_after_end(later, window.master)
    assert calls == []
    assert not window._play_requested
    window.close()


def test_only_actual_master_clip_end_schedules_auto_advance(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    class Player:
        def __init__(self, position):
            self.current_position = position
        def duration(self):
            return 20000
        def position(self):
            return self.current_position
    master = Player(500)
    other = Player(20000)
    window.master = master
    window.current_group = object()
    window._play_requested = True
    window.preview_mode = False
    advances = []
    monkeypatch.setattr(window, "_advance_after_end", lambda group, player: advances.append((group, player)))
    window.media_status_changed_from(other, QMediaPlayer.EndOfMedia)
    window.media_status_changed_from(master, QMediaPlayer.EndOfMedia)
    app.processEvents()
    assert advances == []
    master.current_position = 20000
    window.media_status_changed_from(master, QMediaPlayer.EndOfMedia)
    app.processEvents()
    assert advances == [(window.current_group, master)]
    window.close()


def test_empty_message_sits_in_the_list_not_behind_the_videos(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    label = window.empty_label
    assert label.parentWidget() is window.group_list.viewport()
    assert window.grid_layout.indexOf(label) == -1
    assert label.isVisible() and "TeslaCam" in label.text()
    assert label.geometry() == window.group_list.viewport().rect()
    window.close()
