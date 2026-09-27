from tesla_viewer.ground_detector import Detection, GroundMotionTracker, motion_for_categories


def test_stationary_detection_does_not_claim_ground_motion():
    tracker = GroundMotionTracker()
    car = Detection(2, 100, 200, 80, 55)
    assert tracker.update([car]) == 0
    assert tracker.update([Detection(2, 102, 201, 80, 55)]) == 0
    assert tracker.result()["level"] == "quiet"


def test_moving_ground_objects_create_adaptive_playback_intervals():
    tracker = GroundMotionTracker()
    tracker.update([Detection(0, 100, 200, 40, 110), Detection(2, 300, 220, 90, 65)])
    assert tracker.update([Detection(0, 130, 200, 40, 110), Detection(2, 340, 220, 90, 65)]) == 2
    result = tracker.result()
    assert result["level"] == "high"
    assert result["intervals"] == [[0, 4000]]
    assert result["engine"] == "yolox"


def test_overhead_pixel_changes_have_no_detection_to_track():
    tracker = GroundMotionTracker()
    tracker.update([])
    tracker.update([])
    assert tracker.result()["level"] == "quiet"


def test_selected_object_types_recalculate_cached_motion():
    tracker = GroundMotionTracker()
    tracker.update([Detection(2, 100, 200, 80, 55)])
    tracker.update([Detection(2, 150, 200, 80, 55)])
    result = tracker.result()
    assert result["level"] == "moderate"
    assert motion_for_categories(result, {"car"})["level"] == "moderate"
    assert motion_for_categories(result, {"person"}) == {"level": "quiet", "intervals": []}


def test_local_yolox_model_loads_without_network():
    import numpy as np
    from tesla_viewer.ground_detector import FRAME_SIDE, GroundObjectDetector

    detector = GroundObjectDetector()
    blank = np.full((FRAME_SIDE, FRAME_SIDE, 3), 114, dtype=np.uint8)
    assert detector.detect(blank) == []


def test_damaged_category_cache_is_reported_unknown():
    assert motion_for_categories({"counts": [[1]]}, {"person"}) == {
        "level": "unknown", "intervals": [],
    }


def test_parked_car_seen_only_now_and_then_is_not_motion():
    from tesla_viewer.ground_detector import moving_counts
    parked = [2, 163, 221, 86, 44]
    jittered = [2, 166, 219, 83, 46]
    # Behind trees: found in a few seconds only, with box jitter.
    frames = [[], [parked], [], [], [jittered], [], [parked], [jittered], [], []]
    assert all(sum(row) == 0 for row in moving_counts(frames))


def test_car_leaving_its_space_counts_while_it_moves():
    from tesla_viewer.ground_detector import moving_counts
    frames = [[[2, 100, 200, 80, 50]]] * 5 + [[[2, 100 + 25 * step, 200, 80, 50]] for step in range(1, 5)] \
        + [[[2, 200, 200, 80, 50]]] * 3
    counts = [row[1] for row in moving_counts(frames)]
    assert counts[:5] == [0] * 5            # parked
    assert counts[5] == 1 or counts[6] == 1  # starts moving
    assert counts[7:9] == [1, 1]            # driving away
    assert counts[10:] == [0, 0]            # stopped again


def test_duplicate_box_of_big_parked_car_does_not_jump_to_a_small_track():
    from tesla_viewer.ground_detector import moving_counts
    big, small = [2, 13, 121, 618, 299], [2, -2, 159, 154, 100]
    frames = [[small, big]] * 3 + [[small, big, [7, 15, 127, 618, 291]]] + [[small, big]] * 3
    assert all(sum(row) == 0 for row in moving_counts(frames))
