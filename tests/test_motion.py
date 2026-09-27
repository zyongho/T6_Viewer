import numpy as np

from tesla_viewer.motion import ParkedMotionDetector


def test_parked_motion_switches_to_base_speed_near_change():
    detector = ParkedMotionDetector(hold_seconds=3)
    blank = np.zeros((90, 160), dtype=np.uint8)
    changed = blank.copy()
    changed[45:65, 55:85] = 220
    detector.observe("front", blank, now=1)
    assert detector.speed(2.0, now=1.2) == 2.0
    detector.observe("front", blank, now=2.6)
    assert detector.speed(2.0, now=2.7) == 16.0
    assert detector.observe("front", changed, now=3)
    assert detector.speed(2.0, now=3.5) == 2.0
    assert detector.speed(2.0, now=6) == 2.0  # stale frames must not skip unseen footage


def test_sky_only_change_does_not_trigger_slow_motion():
    detector = ParkedMotionDetector()
    blank = np.zeros((90, 160), dtype=np.uint8)
    sky = blank.copy()
    sky[:10] = 255
    detector.observe("front", blank, now=1)
    assert not detector.observe("front", sky, now=2)


def test_quiet_frames_resume_fast_playback_after_motion_hold():
    detector = ParkedMotionDetector(hold_seconds=3)
    blank = np.zeros((90, 160), dtype=np.uint8)
    moving = blank.copy()
    moving[40:65, 50:90] = 200
    detector.observe("front", blank, now=1)
    detector.observe("front", moving, now=2)
    assert detector.speed(1.0, now=3) == 1.0
    detector.observe("front", moving, now=5.2)
    assert detector.speed(1.0, now=5.3) == 16.0


def test_adaptive_ceiling_reaches_sixteen_times():
    detector = ParkedMotionDetector()
    blank = np.zeros((90, 160), dtype=np.uint8)
    detector.observe("front", blank, now=1)
    detector.observe("front", blank, now=3)
    assert detector.speed(8.0, now=3.1) == 16.0
