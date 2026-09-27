import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from tesla_viewer.crypto import ClipGroup, ClipInfo
from tesla_viewer import analysis_jobs
from tesla_viewer.analysis_pool import AnalysisPool, Job
from tesla_viewer.main_window import MainWindow, MapWidget
from tesla_viewer.motion_scan import adaptive_rate, analyze_motion_frames, classify_motion_frames, analysis_group_order
from tesla_viewer.telemetry import TelemetrySample, nearest_sample


def test_rapid_list_clicks_load_only_latest(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    first, second = object(), object()
    window.groups = [first, second]
    loaded = []
    monkeypatch.setattr(window, "load_group", lambda group, **kwargs: loaded.append((group, kwargs)))
    first_item, second_item = QTreeWidgetItem(), QTreeWidgetItem()
    first_item.setData(0, Qt.UserRole, 0)
    second_item.setData(0, Qt.UserRole, 1)
    window.group_selected(first_item)
    window.group_selected(second_item)
    window._load_pending_selection()
    assert loaded == [(second, {})]
    window.group_selected(first_item)
    window.group_double_clicked(second_item)
    assert loaded[-1] == (second, {"autoplay": True})
    assert not window.selection_timer.isActive()
    window.close()


def test_map_selection_does_not_resend_unchanged_routes(monkeypatch):
    calls = []
    class Page:
        runJavaScript = staticmethod(calls.append)

    class FakeMap:
        page = staticmethod(lambda: Page())

    widget = FakeMap()
    widget._ready = True
    widget._sent_routes = {}
    routes = {"one": [[37.0, 127.0, 0]], "two": [[38.0, 128.0, 0]]}
    MapWidget.set_group_routes(widget, routes, "one")
    MapWidget.set_group_routes(widget, routes, "two", focus=True)
    assert '"one"' in calls[0] and '"two"' in calls[0]
    assert calls[1].startswith("window.updateRoutes([],[],")


def test_motion_signal_levels_and_nearest_sample():
    blank = np.zeros((54, 96), dtype=np.uint8)
    moderate = blank.copy()
    moderate[25:35, 30:40] = 255
    flag = blank.copy()
    flag[2:14, 35:60] = 255
    high = blank.copy()
    high[27:39, 10:23] = 255
    high[28:42, 43:57] = 255
    high[26:40, 72:88] = 255
    assert classify_motion_frames(blank.tobytes() * 3) == "quiet"
    assert classify_motion_frames(blank.tobytes() * 40) == "unknown"
    assert classify_motion_frames(blank.tobytes() + moderate.tobytes()) == "moderate"
    assert classify_motion_frames(blank.tobytes() + flag.tobytes()) == "quiet"
    assert classify_motion_frames(blank.tobytes() + high.tobytes()) == "high"
    assert classify_motion_frames(blank.tobytes() + np.full_like(blank, 255).tobytes()) == "unknown"
    assert classify_motion_frames(blank.tobytes()) == "unknown"
    result = analyze_motion_frames(blank.tobytes() + moderate.tobytes())
    assert result["intervals"] == [[0, 2000]]
    assert adaptive_rate([result], 1000, 2.0) == 2.0
    assert adaptive_rate([result], 5000, 2.0) == 16.0
    assert adaptive_rate([{"level": "quiet", "intervals": []}], 1000, 1.0) == 16.0
    assert adaptive_rate([{"level": "unknown", "intervals": []}], 1000, 1.0) == 1.0
    assert adaptive_rate([None], 1000, 1.0) == 1.0
    samples = [TelemetrySample(0), TelemetrySample(1000), TelemetrySample(2000)]
    assert nearest_sample(samples, 1499) is samples[1]
    assert nearest_sample(samples, 1500) is samples[1]
    assert nearest_sample(samples, 1501) is samples[2]


def test_motion_queue_prioritizes_selected_then_unseen_neighbors(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    groups = []
    for index in range(9):
        clips = {
            camera: ClipInfo(tmp_path / f"{index}-{camera}.mp4", camera, None, False)
            for camera in ("front", "back")
        }
        groups.append(ClipGroup(str(index), clips, tmp_path))
    window.groups = groups
    window.current_group = groups[4]
    window._viewed_groups = {window.group_key(groups[index]) for index in (3, 4, 5)}
    window._explicit_selection = groups[4]
    jobs = window._motion_jobs()
    assert [job[1].name for job in jobs[:2]] == ["4-front.mp4", "4-back.mp4"]
    assert [job[1].name for job in jobs[2:8]] == [
        "1-front.mp4", "1-back.mp4", "2-front.mp4", "2-back.mp4", "3-front.mp4", "3-back.mp4"
    ]
    assert all(job[2] == 1 for job in jobs[2:8])
    assert analysis_group_order(groups, groups[4])[:4] == [groups[4], groups[1], groups[2], groups[3]]
    pool = AnalysisPool(lambda job, result: None)
    as_jobs = [Job((job[2], 1, index), f"motion|{job[1]}", "motion", (str(job[1]), str(job[4])))
               for index, job in enumerate(jobs)]
    pool.replace("motion", as_jobs[2:])
    assert pool._pop_best().args[0].endswith("1-front.mp4")
    # Reprioritizing replaces the pending order; a running job is not queued twice.
    pool._running[as_jobs[2].key] = as_jobs[2]
    pool.replace("motion", as_jobs)
    assert pool._pop_best().args[0].endswith("4-front.mp4")
    assert as_jobs[2].key not in {job.key for job in pool._pending["motion"]}
    # Cheap GPS jobs of the same tier run before slow object scans.
    pool.replace("telemetry", [Job((0, 0, 0), "telemetry|x", "telemetry", ("x",))])
    assert pool._pop_best().kind == "telemetry"
    assert window.adaptive_button.isEnabled()
    monkeypatch.setattr(window, "start_motion_scan", lambda **_kwargs: None)
    window.set_adaptive_speed(True)
    assert window.adaptive_speed
    window.close()


def test_ground_motion_cache_reuses_result_and_invalidates_changed_file(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._motion_cache_path = tmp_path / "motion.json"
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"first")
    result = {"level": "moderate", "intervals": [[1000, 3000]]}
    window._motion_result("front", str(clip), result)
    window._save_motion_cache()
    window._motion_cache.clear()
    window._motion_cache = window._load_motion_cache()
    assert window._cached_motion(clip) == result
    clip.write_bytes(b"changed content")
    assert window._cached_motion(clip) is None
    window.close()


def test_moving_camera_is_not_marked_as_ground_object_motion(monkeypatch, tmp_path):
    clip = tmp_path / "front.mp4"
    rear = tmp_path / "back.mp4"
    clip.write_bytes(b"front")
    rear.write_bytes(b"back")
    checked = []
    def moving_samples(path):
        checked.append(path)
        return [TelemetrySample(0, vehicle_speed_mps=2.0)]
    monkeypatch.setattr(analysis_jobs, "load_telemetry", moving_samples)
    monkeypatch.setattr(analysis_jobs, "_ego_motion", {})
    def no_scan(*_args):
        raise AssertionError("ego-motion clips must not be scanned")
    monkeypatch.setattr(analysis_jobs, "_detect_file", no_scan)
    results = [analysis_jobs.scan_objects(str(path), str(clip)) for path in (clip, rear)]
    assert results == [{"level": "unknown", "intervals": [], "engine": "ego-motion"}] * 2
    assert checked == [clip]  # the group's reference telemetry is read once
    # Saved beside the clip, so another process or the next run reuses it.
    monkeypatch.setattr(analysis_jobs, "load_telemetry", lambda _path: 1 / 0)
    monkeypatch.setattr(analysis_jobs, "_ego_motion", {})
    assert analysis_jobs.scan_objects(str(rear), str(clip))["engine"] == "ego-motion"


def test_object_checkbox_reuses_classified_clip_without_scan(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    clip = tmp_path / "front.mp4"
    clip.write_bytes(b"sample")
    tile = window.tiles_by_camera["front"]
    tile.set_clip(clip)
    key = window._motion_cache_key(clip)
    # Only the car category has one moving object at second 1.
    result = {"level": "moderate", "intervals": [[0, 4000]], "engine": "yolox",
              "counts": [[0, 0, 0, 0], [0, 1, 0, 0]]}
    window._motion_cache[str(clip)] = (key[1], key[2], result)
    window.set_object_categories({"person", "motorcycle", "bicycle"})
    assert "변화 없음" in tile.title.text()
    window.set_object_categories({"person", "car", "motorcycle", "bicycle"})
    assert "변화 미미함" in tile.title.text()
    window.close()
