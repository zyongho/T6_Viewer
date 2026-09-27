from pathlib import Path

from t6_viewer import derived_cache
from t6_viewer.telemetry import TelemetrySample
from t6_viewer.watch_progress import add_interval, coverage
from t6_viewer.memory_budget import (
    MIB, GROWTH_CEILING, TOTAL_BUDGET, WORKER_ESTIMATE, critical, memory_snapshot, over_budget,
    playback_reserve, preload_slots, worker_slots,
)


def test_per_clip_results_reused_and_invalidated(tmp_path: Path):
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"first")
    result = {"level": "moderate", "intervals": [[1000, 2000]]}
    assert derived_cache.save_result(clip, "objects_v6", result)
    assert derived_cache.load_result(clip, "objects_v6") == result
    clip.write_bytes(b"changed video")
    assert derived_cache.load_result(clip, "objects_v6") is None


def test_telemetry_is_parsed_once_then_loaded_from_clip_folder(monkeypatch, tmp_path: Path):
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"video")
    calls = []
    def parse(path):
        calls.append(path)
        return [TelemetrySample(1000, vehicle_speed_mps=4.5)]
    monkeypatch.setattr(derived_cache, "extract_telemetry", parse)
    assert derived_cache.load_telemetry(clip) == [TelemetrySample(1000, vehicle_speed_mps=4.5)]
    assert derived_cache.load_telemetry(clip) == [TelemetrySample(1000, vehicle_speed_mps=4.5)]
    assert calls == [clip]
    assert (tmp_path / ".t6_viewer_cache" / "front.mp4.sei.json.gz").is_file()


def test_image_cache_and_actual_playback_coverage(tmp_path: Path):
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"video")
    jpeg = b"\xff\xd8frame\xff\xd9"
    assert derived_cache.save_image(clip, "storyboard20", jpeg, 20000)
    assert derived_cache.load_image(clip, "storyboard20") == (jpeg, 20000)
    intervals = add_interval([], 0, 4000)
    intervals = add_interval(intervals, 6000, 10000)
    intervals = add_interval(intervals, 2000, 7000)
    assert intervals == [[0, 10000]]
    assert coverage(intervals, 20000) == 0.5


def test_memory_budget_limits_extra_decoders_before_pressure():
    assert preload_slots(2 * 1024 * MIB, 8 * 1024 * MIB, 6) == 6
    assert preload_slots(4 * 1024 * MIB, 8 * 1024 * MIB, 6) == 0
    assert preload_slots(2 * 1024 * MIB, 1536 * MIB, 6) == 0
    # Fast six-channel playback reserves room, so fewer preloads fit.
    assert preload_slots(3 * 1024 * MIB, 8 * 1024 * MIB, 6, playback_reserve(16, 6)) <         preload_slots(3 * 1024 * MIB, 8 * 1024 * MIB, 6, playback_reserve(1, 6))
    assert over_budget(TOTAL_BUDGET - 64 * MIB, 8 * 1024 * MIB)
    assert not over_budget(3 * 1024 * MIB, 8 * 1024 * MIB)
    assert over_budget(2 * 1024 * MIB, 512 * MIB)
    assert critical(2 * 1024 * MIB, 256 * MIB)
    assert not critical(4 * 1024 * MIB, 8 * 1024 * MIB)
    used, available = memory_snapshot()
    assert used is None or used > 0
    assert available is None or available > 0


def test_analysis_workers_fill_only_the_remaining_budget():
    plenty = 16 * 1024 * MIB
    assert worker_slots(1024 * MIB, plenty, 0, 12) == 12
    room = GROWTH_CEILING - 3584 * MIB
    assert worker_slots(3584 * MIB, plenty, 0, 12) == room // WORKER_ESTIMATE
    # Workers started moments ago are charged before their memory shows up.
    assert worker_slots(3584 * MIB, plenty, 2, 12, starting=2) == room // WORKER_ESTIMATE
    # Growth stops short of the shedding threshold (no admit/shed oscillation).
    assert worker_slots(4400 * MIB, plenty, 4, 12) == 4
    assert not over_budget(GROWTH_CEILING, plenty)
    assert worker_slots(1024 * MIB, 1800 * MIB, 0, 12) == 1  # keep 1.5 GiB free system-wide
    assert worker_slots(1024 * MIB, plenty, 0, 12, reserve=3 * 1024 * MIB) == 1


def test_whole_pc_memory_limits_scale_with_ram(monkeypatch):
    from t6_viewer import memory_budget
    from t6_viewer.memory_budget import excess_bytes, recovered, system_low
    gib = 1024 * MIB
    monkeypatch.setattr(memory_budget, "_total_physical", 16 * gib)
    plenty = 12 * gib
    # New work only while the PC keeps 20% (3.2 GiB) free, even with app room left.
    assert worker_slots(1024 * MIB, plenty, 0, 12) == 12
    assert worker_slots(1024 * MIB, int(3.2 * gib) + 2 * WORKER_ESTIMATE, 0, 12) == 2
    assert worker_slots(1024 * MIB, 3 * gib, 0, 12) == 0
    # Other programs pushing the PC past 90% use shed analysis; past 95% is critical.
    assert not over_budget(1024 * MIB, 2 * gib)
    assert system_low(int(1.5 * gib)) and over_budget(1024 * MIB, int(1.5 * gib))
    assert excess_bytes(1024 * MIB, int(1.5 * gib)) == int(16 * gib * 0.10) - int(1.5 * gib)
    assert critical(1024 * MIB, 700 * MIB) and not critical(1024 * MIB, gib)
    assert not recovered(1024 * MIB, 3 * gib) and recovered(1024 * MIB, 4 * gib)
    # A small PC keeps the fixed floors.
    monkeypatch.setattr(memory_budget, "_total_physical", 4 * gib)
    assert memory_budget.low_system_memory() == 1536 * MIB


def test_memory_snapshot_counts_child_processes():
    import os
    import subprocess
    import sys
    import time
    from t6_viewer import memory_budget
    if os.name != "nt":
        return
    kernel, psapi = memory_budget._windows_api()
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; b = bytearray(300 * 1024 * 1024); time.sleep(30)"])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            handle = kernel.OpenProcess(0x1000 | 0x0010, False, child.pid)
            child_bytes = memory_budget._private_bytes(psapi, handle) or 0
            kernel.CloseHandle(handle)
            if child_bytes > 250 * MIB:
                break
            time.sleep(0.2)
        assert child.pid in memory_budget._descendant_pids(kernel, os.getpid())
        time.sleep(memory_budget._TREE_REFRESH + 0.1)  # the process list is reused for 1 s
        own = memory_budget._private_bytes(psapi, kernel.GetCurrentProcess())
        total, _available = memory_snapshot(0)
        assert total >= own + child_bytes * 0.9 and child_bytes > 250 * MIB
    finally:
        child.kill()
        child.wait()



def test_watched_color_requires_more_than_75_percent_actual_playback(monkeypatch, tmp_path: Path):
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication, QTreeWidgetItem
    from t6_viewer.crypto import ClipGroup, ClipInfo
    from t6_viewer.main_window import MainWindow
    from t6_viewer import main_window as module

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._watched_path = tmp_path / "watched.json"
    window._watched_progress = {}
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"video")
    group = ClipGroup("2026-09-26 08:00:00", {"front": ClipInfo(clip, "front", None, False)}, tmp_path)
    window.current_group = group
    window._play_requested = True
    window.preview_mode = False
    item = QTreeWidgetItem()
    window.group_items[window.group_key(group)] = item
    class Player:
        duration = staticmethod(lambda: 100000)
        playbackState = staticmethod(lambda: QMediaPlayer.PlayingState)
        playbackRate = staticmethod(lambda: 16.0)
    ticks = iter(index * 0.25 for index in range(30))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    for position in range(0, 72000, 4000):
        window._record_watched_position(Player(), position)
    assert not window._is_fully_watched(group)
    window._watch_last = None  # Seeking does not bridge the skipped range.
    window._record_watched_position(Player(), 96000)
    assert not window._is_fully_watched(group)
    window._record_watched_position(Player(), 100000)
    assert not window._is_fully_watched(group)
    window._watch_last = None
    for position in (68000, 72000, 76000, 80000):
        window._record_watched_position(Player(), position)
    assert window._is_fully_watched(group)
    window.close()


def test_next_group_preloads_every_visible_channel(monkeypatch, tmp_path: Path):
    from PySide6.QtWidgets import QApplication
    from t6_viewer.crypto import ClipGroup, ClipInfo
    from t6_viewer.main_window import MainWindow
    from t6_viewer import main_window as module

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    class Signal:
        def connect(self, _callback):
            pass
    class FakePlayer:
        def __init__(self, _parent):
            self.errorOccurred = Signal()
            self.paused = False
            self.played = False
        def setVideoSink(self, _sink):
            pass
        def setVideoOutput(self, _output):
            pass
        def setSource(self, _url):
            pass
        def play(self):
            self.played = True
        def pause(self):
            self.paused = True
        def stop(self):
            pass
        def deleteLater(self):
            pass
    class FakeSink:
        def __init__(self, _parent):
            self.videoFrameChanged = Signal()
        def deleteLater(self):
            pass
    monkeypatch.setattr(module, "QMediaPlayer", FakePlayer)
    monkeypatch.setattr(module, "QVideoSink", FakeSink)
    monkeypatch.setattr(module, "memory_snapshot", lambda: (1024 * MIB, 8 * 1024 * MIB))
    earlier = ClipGroup("2026-09-26 08:00:00", {"front": ClipInfo(tmp_path / "old.mp4", "front", None, False)}, tmp_path)
    next_group = ClipGroup("2026-09-26 08:01:00", {
        camera: ClipInfo(tmp_path / f"{camera}.mp4", camera, None, False)
        for camera in ("front", "back")}, tmp_path)
    window.groups = [next_group, earlier]
    window.current_group = earlier
    window.selected_cameras = {"front", "back"}
    window._play_requested = True
    class Frame:
        isValid = staticmethod(lambda: True)
    # Decoders open one at a time: the next waits until the previous has
    # its first frame, so opening does not stutter the clip playing now.
    window._prepare_next_player(earlier)
    assert set(window._preload_channels) == {"front"}
    window._prepare_next_player(earlier)
    assert set(window._preload_channels) == {"front"}
    front = window._preload_channels["front"]
    assert front["player"].paused and not front["player"].played  # held at frame 0
    window._preload_frame_ready("front", front["player"], Frame())
    window._prepare_next_player(earlier)
    assert set(window._preload_channels) == {"front", "back"}
    back = window._preload_channels["back"]
    window._preload_frame_ready("back", back["player"], Frame())
    # No room in the 4.5 GiB budget: nothing more is admitted.
    window._clear_preload()
    monkeypatch.setattr(module, "memory_snapshot", lambda: (4400 * MIB, 8 * 1024 * MIB))
    window._prepare_next_player(earlier)
    assert window._preload_channels == {}
    monkeypatch.setattr(module, "memory_snapshot", lambda: (1024 * MIB, 8 * 1024 * MIB))
    for _ in range(2):
        window._prepare_next_player(earlier)
        for camera, entry in window._preload_channels.items():
            window._preload_frame_ready(camera, entry["player"], Frame())
    prepared = window._take_preload(next_group)
    assert set(prepared) == {"front", "back"}
    window.close()


def test_results_in_the_old_cache_folder_are_renamed_and_reused(tmp_path: Path, monkeypatch):
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"clip")
    monkeypatch.setattr(derived_cache, "_adopted", set())
    legacy = tmp_path / ".myteslaviewer_cache"
    legacy.mkdir()
    # write a result with the current format into the old folder name
    assert derived_cache.save_result(clip, "objects_v7", {"level": "quiet"})
    (tmp_path / ".t6_viewer_cache").rename(legacy)
    monkeypatch.setattr(derived_cache, "_adopted", set())
    assert derived_cache.load_result(clip, "objects_v7") == {"level": "quiet"}
    assert (tmp_path / ".t6_viewer_cache").is_dir() and not legacy.exists()


def test_old_cache_folder_is_still_read_when_it_cannot_be_renamed(tmp_path: Path, monkeypatch):
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"clip")
    monkeypatch.setattr(derived_cache, "_adopted", set())
    assert derived_cache.save_result(clip, "sei", [1, 2])
    (tmp_path / ".t6_viewer_cache").rename(tmp_path / ".myteslaviewer_cache")
    monkeypatch.setattr(derived_cache, "_adopted", set())
    monkeypatch.setattr(derived_cache.os, "rename", lambda *args: (_ for _ in ()).throw(OSError("in use")))
    assert derived_cache.load_result(clip, "sei") == [1, 2]
    assert "front.mp4.sei.json.gz" in derived_cache.cache_listing(tmp_path)


def test_data_folder_of_the_old_app_name_is_adopted(tmp_path: Path, monkeypatch):
    from t6_viewer import app_paths
    monkeypatch.delenv("T6VIEWER_HOME", raising=False)
    monkeypatch.setattr(app_paths.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / "MyTeslaViewer"
    old.mkdir()
    (old / "settings.json").write_text('{"vworld_key": "abc"}', encoding="utf-8")
    folder = app_paths.data_dir()
    assert folder == tmp_path / "T6Viewer"
    assert (folder / "settings.json").read_text(encoding="utf-8") == '{"vworld_key": "abc"}'
