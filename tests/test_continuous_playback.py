"""End-to-end: continuous playback across clips, and the analysis process pool."""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from t6_viewer.analysis_pool import AnalysisPool, Job
from t6_viewer.crypto import ClipGroup, ClipInfo
from t6_viewer.main_window import MainWindow, VideoTile


def _make_clip(path: Path, seconds: float) -> None:
    subprocess.run(
        [shutil.which("ffmpeg"), "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc=size=320x240:rate=24:duration={seconds}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "24", str(path)],
        check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait(app, condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_tile_recognises_its_source_despite_url_slashes(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    tile = VideoTile("front")
    clip = tmp_path / "sub" / "front.mp4"
    tile.player.setSource(QUrl.fromLocalFile(str(clip)))
    # toLocalFile() returns forward slashes on Windows; that is the same file.
    assert tile.has_source(clip)
    assert not tile.has_source(tmp_path / "other.mp4")
    tile.path = clip
    source = tile.player.source()
    tile.show_video()
    assert tile.player.source() == source  # not reopened (resume keeps position)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed to generate clips")
def test_clip_end_continues_into_preloaded_next_clip(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    groups = []
    for index, stamp in enumerate(("2026-09-26 08:00:00", "2026-09-26 08:01:00", "2026-09-26 08:02:00")):
        clips = {}
        for camera in ("front", "back"):
            path = tmp_path / f"{index}-{camera}.mp4"
            _make_clip(path, 2.5)
            clips[camera] = ClipInfo(path, camera, None, False)
        groups.append(ClipGroup(stamp, clips, tmp_path))
    window = MainWindow()
    window.memory_timer.stop()  # memory shedding has its own tests
    window.preload_timer.setInterval(100)
    for camera in list(window.camera_buttons):
        window.camera_buttons[camera].setChecked(camera in ("front", "back"))
    window.groups = list(reversed(groups))  # list shows newest first
    window.load_group(groups[0], autoplay=True)

    assert _wait(app, lambda: window._preload_group is groups[1] and len(window._preload_channels) == 2
                 and all(entry["ready"] for entry in window._preload_channels.values()), 10)
    preloaded = {camera: entry["player"] for camera, entry in window._preload_channels.items()}
    assert all(player.position() == 0 for player in preloaded.values())
    statuses = []
    for player in preloaded.values():
        player.mediaStatusChanged.connect(statuses.append)

    assert _wait(app, lambda: window.current_group is groups[1], 10), "did not advance at clip end"
    for camera, player in preloaded.items():
        tile = window.tiles_by_camera[camera]
        assert tile.player is player  # the prepared decoder was reused...
    assert QMediaPlayer.NoMedia not in statuses  # ...and not reopened
    assert QMediaPlayer.LoadingMedia not in statuses
    assert _wait(app, lambda: window.master.position() > 300, 5)
    assert window.master.playbackState() == QMediaPlayer.PlayingState
    assert window._play_requested

    # ...and it keeps going past the next boundary as well.
    assert _wait(app, lambda: window.current_group is groups[2], 10)
    assert _wait(app, lambda: window.master.position() > 300, 5)
    # The last clip ends playback cleanly.
    assert _wait(app, lambda: not window._play_requested, 10)
    window.close()


def test_analysis_pool_runs_jobs_in_parallel_processes_and_sheds(tmp_path):
    results: list[tuple[str, object]] = []
    done = threading.Event()
    clips = []
    for index in range(4):
        clip = tmp_path / f"{index}.mp4"
        clip.write_bytes(b"not a real video")
        clips.append(clip)

    def on_result(job, result):
        results.append((job.key, result))
        if len(results) == len(clips):
            done.set()

    pool = AnalysisPool(on_result)
    try:
        pool.replace("telemetry", [Job((0, 0, index), f"telemetry|{clip}", "telemetry", (str(clip),))
                                   for index, clip in enumerate(clips)])
        pool.set_target(2)
        assert pool.worker_count() == 2
        assert done.wait(60)
        assert sorted(key for key, _ in results) == sorted(f"telemetry|{clip}" for clip in clips)
        assert all(result == {"route": [], "state": "정보 없음"} for _, result in results)
        # Nothing queued: idle processes are retired.
        pool.set_target(pool.running_count())
        assert pool.worker_count() == 0
        # Shedding kills processes immediately and re-queues their work.
        pool.replace("telemetry", [Job((0, 0, 0), "telemetry|x", "telemetry", (str(clips[0]),))])
        pool.set_target(1)
        processes = [slot.process for slot in pool._slots]
        assert pool.shed(1) == 1
        deadline = time.monotonic() + 10
        while any(process.is_alive() for process in processes) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(process.is_alive() for process in processes)
    finally:
        pool.shutdown()


def test_playback_rate_is_capped_by_decode_load(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = MainWindow()
    for tile in window.tiles:
        tile.path = tmp_path / f"{tile.camera}.mp4"
    window.set_base_speed(16.0)
    # Six full-resolution channels: 24 / 6 = 4x.
    assert {tile.player.playbackRate() for tile in window.active_tiles()} == {4.0}
    assert "6채널 최대" in window.actual_speed_label.text()
    window.selected_cameras = {"front", "back"}
    window.update_playback_speed()
    assert {tile.player.playbackRate() for tile in window.active_tiles()} == {12.0}
    window.close()


def test_first_and_last_fifth_of_a_clip_are_held_to_8x(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = MainWindow()
    tile = window.tiles_by_camera["front"]
    tile.path = tmp_path / "front.mp4"
    window.selected_cameras = {"front"}

    class Master:
        position_ms = 0
        def duration(self):
            return 60000
        def position(self):
            return self.position_ms
    master = Master()
    window.master = master
    window.base_speed = 16.0
    rates = {}
    for position in (5000, 11999, 12000, 30000, 48000, 48001, 59000):
        master.position_ms = position
        window.update_playback_speed()
        rates[position] = tile.player.playbackRate()
    assert rates == {5000: 8.0, 11999: 8.0, 12000: 16.0, 30000: 16.0, 48000: 16.0, 48001: 8.0, 59000: 8.0}
    window.base_speed = 4.0
    master.position_ms = 1000
    window.update_playback_speed()
    assert tile.player.playbackRate() == 4.0  # slower settings are untouched
    window.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg needed to generate clips")
def test_storyboard_frames_arrive_progressively_then_as_one_cached_sheet(monkeypatch, tmp_path):
    from PySide6.QtGui import QImage
    from t6_viewer import derived_cache
    from t6_viewer.main_window import StoryboardWorker

    QApplication.instance() or QApplication([])
    clip = tmp_path / "front.mp4"
    _make_clip(clip, 10)
    worker = StoryboardWorker([("front", clip)])
    partial, ready = [], []
    worker.partial.connect(lambda camera, path, index, image, duration: partial.append(index))
    worker.ready.connect(lambda camera, path, image, duration: ready.append((image, duration)))
    worker.run()
    # Evenly spaced slots arrive one by one, in order, before the sheet.
    assert partial == list(range(len(partial))) and 15 <= len(partial) <= 20
    assert str(clip) in worker.completed
    image = QImage.fromData(ready[0][0])
    assert (image.width(), image.height()) == (96 * 20, 54)
    assert abs(ready[0][1] - 10000) < 200
    assert derived_cache.load_image(clip, "storyboard20") is not None


def test_clips_without_storyboard_are_queued_for_the_background_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = MainWindow()
    window.memory_timer.stop()
    clips = {camera: ClipInfo(tmp_path / f"{camera}.mp4", camera, None, False) for camera in ("front", "back")}
    for clip in clips.values():
        clip.path.write_bytes(b"clip")
    group = ClipGroup("2026-09-26 08:00:00", clips, tmp_path)
    window.all_groups = window.groups = [group]
    window._index_progress()
    window._done["storyboard"].add(str(clips["back"].path))
    queued = {}
    monkeypatch.setattr(window._analysis, "replace", lambda kind, jobs: queued.__setitem__(kind, jobs))
    window.start_telemetry_index()
    assert [job.args[0] for job in queued["storyboard"]] == [str(clips["front"].path)]
    # Progress: 1 of 2 sheets, no objects, no SEI -> 1 of 5 items.
    assert window._progress(group)[:2] == (1, 5)
    window.close()


def test_memory_governor_sheds_optional_work_but_never_stops_playback(monkeypatch, tmp_path):
    from t6_viewer import main_window as module
    from t6_viewer.memory_budget import MIB, TOTAL_BUDGET

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    window = MainWindow()
    window.memory_timer.stop()
    tile = window.tiles_by_camera["front"]
    tile.path = tmp_path / "front.mp4"
    window.selected_cameras = {"front"}
    window.master = tile.player
    window._play_requested = True
    window.preview_mode = False
    window.set_base_speed(16.0)

    class Pool:
        def __init__(self):
            self.workers, self.shed_calls = 4, []
        def worker_count(self):
            return self.workers
        def shed(self, count):
            self.shed_calls.append(count)
            self.workers -= min(count, self.workers)
            return count
        def pending_count(self, kind=None):
            return 0
        def running_count(self):
            return 0
        def set_target(self, count):
            pass
        def shutdown(self):
            pass
    pool = Pool()
    window._analysis.shutdown()
    window._analysis = pool
    cleared = []
    window._preload_channels = {"front": object()}
    monkeypatch.setattr(window, "_clear_preload", lambda: (cleared.append(True), window._preload_channels.clear()))
    over = (TOTAL_BUDGET, 8 * 1024 * MIB)
    monkeypatch.setattr(module, "memory_snapshot", lambda *args: over)
    # 1st: analysis processes go first.
    window._check_memory_budget()
    assert pool.shed_calls and not cleared and window._memory_rate_cap is None
    pool.workers = 0
    # 2nd: then the next-clip preload.
    window._check_memory_budget()
    assert cleared and window._memory_rate_cap is None
    # 3rd: only then slow down playback, which keeps going.
    window._check_memory_budget()
    assert window._memory_rate_cap == 8.0
    assert tile.player.playbackRate() == 8.0
    assert window._play_requested and not window.preview_mode
    # Memory back down: restrictions are lifted.
    monkeypatch.setattr(module, "memory_snapshot", lambda *args: (2048 * MIB, 8 * 1024 * MIB))
    window._check_memory_budget()
    assert window._memory_rate_cap is None and not window._memory_pressured
    assert tile.player.playbackRate() == 16.0
    window.close()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed to generate clips")
def test_leaving_single_camera_view_keeps_all_channels_on_its_timeline(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    clips = {}
    for camera in ("front", "back", "left_pillar"):
        path = tmp_path / f"{camera}.mp4"
        _make_clip(path, 12)
        clips[camera] = ClipInfo(path, camera, None, False)
    group = ClipGroup("2026-09-26 08:00:00", clips, tmp_path)
    window = MainWindow()
    window.memory_timer.stop()
    for camera in list(window.camera_buttons):
        window.camera_buttons[camera].setChecked(camera in clips)
    window.groups = [group]
    window.load_group(group, autoplay=True)
    assert _wait(app, lambda: all(tile.player.position() > 300 for tile in window.active_tiles()), 10)
    window.tile_double_clicked("front")          # zoom into Front
    window.seek(7000)                             # move its timeline
    assert _wait(app, lambda: window.master.position() >= 7000, 5)
    window.tile_double_clicked("front")          # back to all channels
    positions = {}
    def synced():
        positions.update({tile.camera: tile.player.position() for tile in window.active_tiles()})
        return len(positions) == 3 and min(positions.values()) >= 6800
    assert _wait(app, synced, 10), positions
    assert max(positions.values()) - min(positions.values()) < 800
    window.close()
