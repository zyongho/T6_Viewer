from tesla_viewer.telemetry import TelemetrySample, vehicle_motion_label


def test_motion_label_uses_telemetry_not_storage_folder():
    assert vehicle_motion_label([]) == "정보 없음"
    assert vehicle_motion_label([TelemetrySample(0, gear_state=0, vehicle_speed_mps=0.0)]) == "주차"
    assert vehicle_motion_label([TelemetrySample(0, gear_state=1, vehicle_speed_mps=0.0)]) == "정차"
    assert vehicle_motion_label([TelemetrySample(0, gear_state=1, vehicle_speed_mps=2.0)]) == "이동"
    assert vehicle_motion_label([TelemetrySample(0, vehicle_speed_mps=0.0)]) == "정지"
