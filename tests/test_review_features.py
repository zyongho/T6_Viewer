"""Header filters, change-point jumping and settings."""

import datetime as dt

from PySide6.QtWidgets import QApplication

from tesla_viewer.app_settings import Settings, estimated_peak_gib, load_settings, rate_for_channels, save_settings
from tesla_viewer.crypto import ClipGroup, ClipInfo
from tesla_viewer.main_window import MainWindow
from tesla_viewer.motion_scan import change_runs, merge_runs, next_change_ms


def _window(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = MainWindow()
    window.memory_timer.stop()
    window._settings_path = tmp_path / "settings.json"
    return window


def _group(tmp_path, stamp: str, folder: str, cameras=("front",), encrypted=False):
    when = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    clips = {}
    for camera in cameras:
        path = tmp_path / folder / f"{stamp.replace(':', '-')}-{camera}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"clip")
        clips[camera] = ClipInfo(path, camera, when, encrypted)
    return ClipGroup(stamp, clips, tmp_path / folder)


def test_change_runs_merge_across_cameras_and_jump_one_second_early():
    front = {"level": "moderate", "counts": [[0, 0, 0, 0]] * 10 + [[1, 0, 0, 0]] + [[0, 0, 0, 0]] * 5}
    back = {"level": "moderate", "counts": [[0, 0, 0, 0]] * 4 + [[0, 1, 0, 0], [0, 1, 0, 0]] + [[0, 0, 0, 0]] * 10}
    everything = {"person", "car", "motorcycle", "bicycle"}
    runs = merge_runs([change_runs(front, everything), change_runs(back, everything)])
    assert [(run["start"], run["end"]) for run in runs] == [(4000, 6000), (10000, 11000)]
    assert next_change_ms(runs, 0) == 3000
    assert next_change_ms(runs, 3000) == 9000  # already at the first one
    assert next_change_ms(runs, 9500) is None
    # Only people selected: the car run disappears.
    assert [run["start"] for run in merge_runs([change_runs(back, {"person"})])] == []


def test_header_filters_hide_rows_like_a_spreadsheet(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    recent = _group(tmp_path, "2026-09-21 08:00:00", "RecentClips")      # Monday morning
    sentry = _group(tmp_path, "2026-09-26 23:30:00", "SentryClips")      # Saturday night
    locked = _group(tmp_path, "2026-09-22 12:00:00", "SavedClips", encrypted=True)
    window.all_groups = [sentry, locked, recent]
    window.vehicle_states = {window.group_key(recent): "이동", window.group_key(sentry): "주차"}
    monkeypatch.setattr(window, "load_group", lambda *args, **kwargs: None)
    window.apply_filters()
    assert window.groups == [sentry, locked, recent]
    assert window.group_list.topLevelItem(1).text(1) == "🔒"
    assert window.group_list.topLevelItem(0).text(3) == "정지"
    assert window.group_list.topLevelItem(0).text(4) == "No"
    window._set_list_filter("category", {"감시", "최근"})
    assert window.groups == [sentry, recent]
    assert "🔽" in window.group_list.headerItem().text(2)
    window._set_list_filter("motion", {"이동"})
    assert window.groups == [recent]
    window._set_list_filter("motion", None)
    window._set_list_filter("category", None)
    window._set_list_filter("decrypt", {"locked"})
    assert window.groups == [locked]
    window._set_list_filter("decrypt", None)
    window._set_list_filter("datetime", {"date": None, "time": (dt.time(22, 0), dt.time(6, 0)),
                                         "weekdays": None})
    assert window.groups == [sentry]  # overnight time range wraps midnight
    window._set_list_filter("datetime", {"date": None, "time": None, "weekdays": {0}})
    assert window.groups == [recent]
    window.close()


def _scanned(window, group, counts):
    for clip in group.clips.values():
        key = window._motion_cache_key(clip.path)
        window._motion_cache[str(clip.path)] = (key[1], key[2], {"level": "moderate", "intervals": [],
                                                                   "counts": counts})


def test_next_change_seeks_in_clip_then_skips_quiet_clips(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    first = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips")
    quiet = _group(tmp_path, "2026-09-26 08:01:00", "SentryClips")
    busy = _group(tmp_path, "2026-09-26 08:02:00", "SentryClips")
    window.groups = [busy, quiet, first]
    window.selected_cameras = {"front"}
    _scanned(window, first, [[0, 0, 0, 0]] * 20 + [[1, 0, 0, 0]] + [[0, 0, 0, 0]] * 5)
    _scanned(window, quiet, [[0, 0, 0, 0]] * 30)
    _scanned(window, busy, [[0, 0, 0, 0]] * 7 + [[0, 1, 0, 0]])
    window.current_group = first
    tile = window.tiles_by_camera["front"]
    tile.path = first.clips["front"].path

    class Master:
        position_ms = 0
        def position(self):
            return self.position_ms
        def duration(self):
            return 30000
        def playbackRate(self):
            return 1.0
    window.master = Master()
    window.preview_mode = False
    window._play_requested = True
    seeks, loads = [], []
    monkeypatch.setattr(window, "set_all_positions", lambda position, **kwargs: seeks.append(position))
    monkeypatch.setattr(window, "load_group", lambda group, **kwargs: loads.append((group, kwargs)))
    assert window.jump_to_next_change()
    assert seeks == [19000]
    window.master.position_ms = 19000
    assert window.jump_to_next_change()
    assert loads == [(busy, {"pending_seek_ms": 6000, "autoplay": True})]
    window.close()


def test_auto_jump_only_skips_analysed_quiet_stretches(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    group = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips")
    window.groups = [group]
    window.selected_cameras = {"front"}
    window.current_group = group
    window.tiles_by_camera["front"].path = group.clips["front"].path

    class Master:
        def position(self):
            return 2000
        def duration(self):
            return 30000
        def playbackRate(self):
            return 1.0
    window.master = Master()
    window.preview_mode = False
    window._play_requested = True
    window.pending_seek_ms = None
    jumps = []
    monkeypatch.setattr(window, "jump_to_next_change", lambda automatic=False: jumps.append(automatic))
    window.auto_jump_button.setChecked(True)
    jumps.clear()
    window._auto_jump_tick()
    assert jumps == []  # not analysed yet: plays normally
    _scanned(window, group, [[0, 0, 0, 0]] * 10 + [[1, 0, 0, 0]])
    window._auto_jump_tick()
    assert jumps == [True]
    assert load_settings(window._settings_path).auto_jump  # remembered
    window.close()


def test_settings_round_trip_and_memory_estimate(tmp_path):
    path = tmp_path / "settings.json"
    assert load_settings(path) == Settings()
    save_settings(path, Settings(max_decode_load=36, analysis_playing=4, analysis_idle=10))
    loaded = load_settings(path)
    assert (loaded.max_decode_load, loaded.analysis_playing, loaded.analysis_idle) == (36, 4, 10)
    assert rate_for_channels(24, 6) == 4.0 and rate_for_channels(24, 1) == 16.0
    assert estimated_peak_gib(24, 8) < 4.5 < estimated_peak_gib(48, 8)
    assert estimated_peak_gib(24, 0) < estimated_peak_gib(24, 8)


def test_analysis_settings_choose_scope_and_items(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    current = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips", cameras=("front", "back"))
    other = _group(tmp_path, "2026-09-26 08:01:00", "SentryClips", cameras=("front", "back"))
    window.all_groups = window.groups = [other, current]
    window.current_root = tmp_path
    window.current_group = current
    window._explicit_selection = current
    window._index_progress()
    queued = {}
    monkeypatch.setattr(window._analysis, "replace", lambda kind, jobs: queued.__setitem__(kind, jobs))

    def queue():
        queued.clear()
        window.queue_background_work()
        window.start_motion_scan()
        return {kind: sorted({window._group_by_path[job.args[0]].timestamp for job in jobs})
                for kind, jobs in queued.items()}

    everything = queue()
    assert everything["motion"] == everything["telemetry"] == [current.timestamp, other.timestamp]
    assert everything["storyboard"] == [current.timestamp, other.timestamp]
    window.settings.analysis_background = False          # only the clip on screen
    assert queue() == {"storyboard": [], "telemetry": [], "motion": [current.timestamp]}
    window.settings.analysis_objects = False
    window.settings.analysis_storyboard = False
    window.settings.analysis_sei = False                  # playback only
    assert queue() == {"storyboard": [], "telemetry": [], "motion": []}
    assert window._progress(current)[:2] == (0, 0)
    window.close()


def test_settings_dialog_returns_analysis_choices(monkeypatch, tmp_path):
    from tesla_viewer.ui_widgets import SettingsDialog
    window = _window(monkeypatch, tmp_path)
    dialog = SettingsDialog(Settings(), window)
    dialog.background.setChecked(False)
    dialog.items["analysis_objects"].setChecked(False)
    assert "보고 있는 영상만" in dialog.scope.text()
    chosen = dialog.settings()
    assert not chosen.analysis_background and not chosen.analysis_objects and chosen.analysis_sei
    for box in dialog.items.values():
        box.setChecked(False)
    assert "재생만" in dialog.scope.text()
    assert not dialog.idle.isEnabled()
    window.close()


def test_object_targets_live_in_settings_and_one_always_stays(monkeypatch, tmp_path):
    from tesla_viewer.ui_widgets import SettingsDialog
    window = _window(monkeypatch, tmp_path)
    dialog = SettingsDialog(Settings(object_categories=["person", "car"]), window)
    assert [name for name, box in dialog.targets.items() if box.isChecked()] == ["person", "car"]
    dialog.targets["car"].setChecked(False)
    dialog.targets["person"].setChecked(False)          # the last one cannot be cleared
    assert dialog.settings().object_categories == ["person"]
    dialog.items["analysis_objects"].setChecked(False)
    assert not dialog.targets["person"].isEnabled()
    window.set_object_categories({"bicycle"})
    assert window.object_categories == {"bicycle"} and window.settings.object_categories == ["bicycle"]
    window.close()


def test_decrypt_button_is_hidden_for_a_decrypted_folder(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    window._repair_source_root = None
    window.all_groups = [_group(tmp_path, "2026-09-26 08:00:00", "RecentClips")]
    window._update_decrypt_action()
    assert window.decrypt_button.isHidden()
    window.all_groups = [_group(tmp_path, "2026-09-26 08:01:00", "RecentClips", encrypted=True)]
    window._update_decrypt_action()
    assert not window.decrypt_button.isHidden()
    window.close()


def test_camera_header_shows_no_detection_comment_when_recognition_is_off(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    group = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips")
    tile = window.tiles_by_camera["front"]
    tile.path = group.clips["front"].path
    window.selected_cameras = {"front"}
    _scanned(window, group, [[0, 0, 0, 0]] * 3 + [[0, 1, 0, 0]])
    window._refresh_motion_display()
    assert "●" in tile.title.text() and tile.storyboard.changes
    window.settings.analysis_objects = False
    window._refresh_motion_display()
    assert tile.title.text() == "Front"
    assert tile.storyboard.changes == [] and window.slider._marks == []
    window.close()


def test_previous_and_next_clip_buttons_step_through_time(monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    early = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips")
    middle = _group(tmp_path, "2026-09-26 08:01:00", "SentryClips")
    late = _group(tmp_path, "2026-09-26 08:02:00", "SentryClips")
    window.groups = [late, middle, early]          # list shows newest first
    window.selected_cameras = {"front"}
    from PySide6.QtWidgets import QTreeWidgetItem
    for _ in window.groups:
        window.group_list.addTopLevelItem(QTreeWidgetItem())
    loads = []
    def fake_load(group, **kwargs):
        loads.append((group, kwargs))
        window.current_group = group
    monkeypatch.setattr(window, "load_group", fake_load)
    window.current_group = middle
    window._play_requested = True
    window.next_clip_button.click()
    assert loads[-1] == (late, {"autoplay": True})     # keeps playing
    assert window.group_list.currentIndex().row() == 0
    window._play_requested = False
    window.previous_clip_button.click()
    window.previous_clip_button.click()
    assert [group for group, _ in loads[-2:]] == [middle, early]
    assert loads[-1][1] == {"autoplay": False}          # paused stays paused
    window.previous_clip_button.click()                  # nothing earlier
    assert len(loads) == 3
    window.close()


def test_about_dialog_lists_notices_and_license_texts(monkeypatch, tmp_path):
    from tesla_viewer.ui_widgets import AboutDialog
    window = _window(monkeypatch, tmp_path)
    dialog = AboutDialog(window)
    names = list(dialog.documents)
    assert names[:2] == ["버전 기록", "오픈소스 고지"]
    assert "Ver 0.1.1" in dialog.text.toPlainText()
    dialog.choice.setCurrentText("오픈소스 고지")
    assert any("LGPL-3.0" in name for name in names) and any("GPL-3.0" in name for name in names)
    assert "PySide6" in dialog.text.toPlainText() and "Tesla, Inc." in dialog.text.toPlainText()
    dialog.choice.setCurrentText(next(name for name in names if "LGPL-3.0" in name))
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in dialog.text.toPlainText()
    window.close()


def test_clear_app_data_removes_the_data_folder_and_caches_then_closes(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    from tesla_viewer import main_window as module
    from tesla_viewer.app_paths import data_dir
    window = _window(monkeypatch, tmp_path)
    folder = data_dir()
    (folder / "sub").mkdir(exist_ok=True)
    (folder / "settings.json").write_text("{}", encoding="utf-8")
    group = _group(tmp_path, "2026-09-26 08:00:00", "SentryClips")
    cache = group.clips["front"].path.parent / ".myteslaviewer_cache"
    cache.mkdir()
    window.current_root = tmp_path
    def confirm(box):
        box.checkBox().setChecked(True)
        delete = next(button for button in box.buttons() if button.text() == "삭제하고 종료")
        monkeypatch.setattr(box, "clickedButton", lambda: delete)
        return 0
    monkeypatch.setattr(QMessageBox, "exec", confirm)
    shown = []
    monkeypatch.setattr(module.QMessageBox, "information", lambda *args: shown.append(args[2]))
    closed = []
    monkeypatch.setattr(window, "close", lambda: closed.append(True))
    window.clear_app_data()
    assert not folder.exists() and not cache.exists()
    assert closed and "모두 지웠습니다" in shown[0] and "1곳" in shown[0]
    assert window._data_cleared  # nothing is written back on exit


def test_version_and_legacy_files_move_into_the_data_folder(monkeypatch, tmp_path):
    import tesla_viewer
    from tesla_viewer.app_paths import data_file
    assert tesla_viewer.__version__ == "0.1.1"
    (tmp_path / ".tesla_viewer_settings.json").write_text('{"auto_jump": true}', encoding="utf-8")
    target = data_file("settings.json", ".tesla_viewer_settings.json")
    assert target.read_text(encoding="utf-8") == '{"auto_jump": true}'
    assert not (tmp_path / ".tesla_viewer_settings.json").exists()   # moved, not copied
