"""Read Tesla's optional H.264 SEI telemetry from decrypted MP4 files.

Tesla publishes the SEI schema, but requiring protoc/generated protobuf code
would make the desktop viewer unnecessarily difficult to install.  The small
wire-format reader below implements the fields in that public schema directly.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


@dataclass(frozen=True)
class TelemetrySample:
    """A telemetry record and its approximate position on the video timeline."""

    position_ms: int
    frame_seq_no: int | None = None
    gear_state: int | None = None
    vehicle_speed_mps: float | None = None
    accelerator_pedal_position: float | None = None
    steering_wheel_angle: float | None = None
    blinker_on_left: bool | None = None
    blinker_on_right: bool | None = None
    brake_applied: bool | None = None
    autopilot_state: int | None = None
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    heading_deg: float | None = None
    linear_acceleration_mps2_x: float | None = None
    linear_acceleration_mps2_y: float | None = None
    linear_acceleration_mps2_z: float | None = None

    @property
    def has_gps(self) -> bool:
        return (
            self.latitude_deg is not None
            and self.longitude_deg is not None
            and math.isfinite(self.latitude_deg)
            and math.isfinite(self.longitude_deg)
            and abs(self.latitude_deg) <= 90
            and abs(self.longitude_deg) <= 180
            and not (self.latitude_deg == 0 and self.longitude_deg == 0)
        )


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _decode_proto(payload: bytes) -> dict[int, object]:
    values: dict[int, object] = {}
    offset = 0
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field_no, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
        elif wire_type == 1:
            if offset + 8 > len(payload):
                raise ValueError("truncated fixed64")
            value = struct.unpack_from("<Q", payload, offset)[0]
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(payload, offset)
            value = payload[offset : offset + length]
            if len(value) != length:
                raise ValueError("truncated bytes")
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(payload):
                raise ValueError("truncated fixed32")
            value = struct.unpack_from("<I", payload, offset)[0]
            offset += 4
        else:
            raise ValueError("unsupported protobuf wire type")
        values[field_no] = value
    return values


def _float(values: dict[int, object], field_no: int, double: bool = False) -> float | None:
    raw = values.get(field_no)
    if raw is None:
        return None
    if not isinstance(raw, int):
        return None
    try:
        return struct.unpack("<d" if double else "<f", struct.pack("<Q" if double else "<I", raw))[0]
    except struct.error:
        return None


def _bool(values: dict[int, object], field_no: int) -> bool | None:
    raw = values.get(field_no)
    return None if raw is None else bool(raw)


def _message_to_sample(values: dict[int, object], position_ms: int) -> TelemetrySample:
    integer = lambda field: int(values[field]) if field in values and isinstance(values[field], int) else None
    return TelemetrySample(
        position_ms=position_ms,
        frame_seq_no=integer(3),
        gear_state=integer(2),
        vehicle_speed_mps=_float(values, 4),
        accelerator_pedal_position=_float(values, 5),
        steering_wheel_angle=_float(values, 6),
        blinker_on_left=_bool(values, 7),
        blinker_on_right=_bool(values, 8),
        brake_applied=_bool(values, 9),
        autopilot_state=integer(10),
        latitude_deg=_float(values, 11, double=True),
        longitude_deg=_float(values, 12, double=True),
        heading_deg=_float(values, 13, double=True),
        linear_acceleration_mps2_x=_float(values, 14, double=True),
        linear_acceleration_mps2_y=_float(values, 15, double=True),
        linear_acceleration_mps2_z=_float(values, 16, double=True),
    )


def _strip_emulation_prevention(data: bytes) -> bytes:
    output = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        output.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(output)


def _sei_payload(nal: bytes) -> bytes | None:
    # Tesla's public extractor looks for repeated 0x42 followed by 0x69.
    if len(nal) < 4 or (nal[0] & 0x1F) != 6 or nal[1] != 5:
        return None
    index = 3
    while index < len(nal) and nal[index] == 0x42:
        index += 1
    if index <= 3 or index + 1 >= len(nal) or nal[index] != 0x69:
        return None
    return _strip_emulation_prevention(nal[index + 1 : -1])


def _find_mdat(fp: BinaryIO) -> tuple[int, int]:
    fp.seek(0)
    while True:
        header = fp.read(8)
        if len(header) < 8:
            raise ValueError("MP4 mdat atom not found")
        size32, atom_type = struct.unpack(">I4s", header)
        if size32 == 1:
            large = fp.read(8)
            if len(large) != 8:
                raise ValueError("truncated MP4 atom size")
            atom_size = struct.unpack(">Q", large)[0]
            header_size = 16
        else:
            atom_size = size32
            header_size = 8
        if atom_type == b"mdat":
            return fp.tell(), max(0, atom_size - header_size) if atom_size else 0
        if atom_size and atom_size >= header_size:
            fp.seek(atom_size - header_size, 1)
        else:
            raise ValueError("invalid MP4 atom size")


def _child_box(data: bytes, start: int, end: int, wanted: bytes) -> tuple[int, int] | None:
    """Find one direct child atom and return its payload range."""
    cursor = start
    while cursor + 8 <= end:
        size = struct.unpack_from(">I", data, cursor)[0]
        atom_type = data[cursor + 4 : cursor + 8]
        header_size = 8
        if size == 1:
            if cursor + 16 > end:
                return None
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header_size = 16
        elif size == 0:
            size = end - cursor
        if size < header_size or cursor + size > end:
            return None
        if atom_type == wanted:
            return cursor + header_size, cursor + size
        cursor += size
    return None


def _frame_times_ms(fp: BinaryIO, mdat_offset: int, default_fps: float) -> list[int]:
    """Read video frame timestamps from the MP4 stts table when present."""
    try:
        # Some Tesla files place moov after the large mdat atom, so scan atom
        # headers and read only the small moov atom instead of loading a video.
        fp.seek(0)
        moov_payload: bytes | None = None
        while True:
            atom_start = fp.tell()
            header = fp.read(8)
            if len(header) < 8:
                break
            size32, atom_type = struct.unpack(">I4s", header)
            header_size = 8
            if size32 == 1:
                large = fp.read(8)
                if len(large) != 8:
                    break
                atom_size = struct.unpack(">Q", large)[0]
                header_size = 16
            else:
                atom_size = size32
            payload_size = atom_size - header_size if atom_size else 0
            if atom_type == b"moov" and 0 <= payload_size <= 64 * 1024 * 1024:
                moov_payload = fp.read(payload_size)
                break
            if atom_size < header_size or not fp.seek(atom_size - header_size, 1):
                break
        if moov_payload is None:
            return []
        prefix = struct.pack(">I4s", len(moov_payload) + 8, b"moov") + moov_payload
        moov = _child_box(prefix, 0, len(prefix), b"moov")
        if not moov:
            return []
        trak = _child_box(prefix, *moov, b"trak")
        mdia = _child_box(prefix, *trak, b"mdia") if trak else None
        mdhd = _child_box(prefix, *mdia, b"mdhd") if mdia else None
        minf = _child_box(prefix, *mdia, b"minf") if mdia else None
        stbl = _child_box(prefix, *minf, b"stbl") if minf else None
        stts = _child_box(prefix, *stbl, b"stts") if stbl else None
        if not mdhd or not stts:
            return []
        version = prefix[mdhd[0]]
        timescale_offset = mdhd[0] + (20 if version == 1 else 12)
        timescale = struct.unpack_from(">I", prefix, timescale_offset)[0]
        if not timescale:
            return []
        count = struct.unpack_from(">I", prefix, stts[0] + 4)[0]
        cursor = stts[0] + 8
        result: list[int] = []
        current_ticks = 0
        for _ in range(count):
            sample_count, delta = struct.unpack_from(">II", prefix, cursor)
            cursor += 8
            for _ in range(sample_count):
                result.append(round(current_ticks * 1000 / timescale))
                current_ticks += delta
        return result
    except (OSError, struct.error, ValueError):
        return []


def _iter_nals(fp: BinaryIO, offset: int, size: int) -> Iterator[bytes]:
    fp.seek(offset)
    consumed = 0
    while size == 0 or consumed < size:
        header = fp.read(4)
        if len(header) != 4:
            return
        nal_size = struct.unpack(">I", header)[0]
        consumed += 4 + nal_size
        if nal_size < 1:
            fp.seek(nal_size, 1)
            continue
        nal = fp.read(nal_size)
        if len(nal) != nal_size:
            return
        yield nal


def extract_telemetry(path: Path, default_fps: float = 30.0) -> list[TelemetrySample]:
    """Extract Tesla SEI records, returning an empty list when absent.

    The record is attached to the next H.264 slice, which gives a stable
    approximate timeline even when the MP4 has no data track.  Tesla clips are
    normally 30 fps; this fallback only affects the marker's horizontal time,
    not the values themselves.
    """
    samples: list[TelemetrySample] = []
    frame_index = 0
    pending: bytes | None = None
    with path.open("rb") as fp:
        offset, size = _find_mdat(fp)
        frame_times = _frame_times_ms(fp, offset, default_fps)
        for nal in _iter_nals(fp, offset, size):
            nal_type = nal[0] & 0x1F if nal else 0
            if nal_type == 6:
                payload = _sei_payload(nal)
                if payload:
                    pending = payload
            elif nal_type in (1, 5):
                if pending:
                    try:
                        position_ms = (
                            frame_times[frame_index]
                            if frame_index < len(frame_times)
                            else round(frame_index * 1000 / default_fps)
                        )
                        samples.append(_message_to_sample(_decode_proto(pending), position_ms))
                    except (ValueError, struct.error):
                        pass
                    pending = None
                frame_index += 1
    return samples


def nearest_sample(samples: list[TelemetrySample], position_ms: int) -> TelemetrySample | None:
    if not samples:
        return None
    # SEI records are emitted in frame order. Avoid a full scan on every
    # playback position update (and on each map-marker refresh).
    low, high = 0, len(samples)
    while low < high:
        middle = (low + high) // 2
        if samples[middle].position_ms < position_ms:
            low = middle + 1
        else:
            high = middle
    if low == 0:
        return samples[0]
    if low == len(samples):
        return samples[-1]
    before, after = samples[low - 1], samples[low]
    return before if position_ms - before.position_ms <= after.position_ms - position_ms else after


def gps_samples(samples: list[TelemetrySample]) -> list[TelemetrySample]:
    return [sample for sample in samples if sample.has_gps]


def vehicle_motion_label(samples: list[TelemetrySample]) -> str:
    """Classify observed vehicle motion, not the clip's storage folder."""
    speeds = [
        abs(sample.vehicle_speed_mps)
        for sample in samples
        if sample.vehicle_speed_mps is not None and math.isfinite(sample.vehicle_speed_mps)
    ]
    gears = [sample.gear_state for sample in samples if sample.gear_state is not None]
    if any(speed >= 0.5 for speed in speeds):
        return "이동"
    if gears and all(gear == 0 for gear in gears):
        return "주차"
    if any(gear in (1, 2, 3) for gear in gears):
        return "정차"
    if speeds:
        return "정지"
    return "정보 없음"


def gear_label(value: int | None) -> str:
    return {0: "PARK", 1: "DRIVE", 2: "REVERSE", 3: "NEUTRAL"}.get(value, "-")


def autopilot_label(value: int | None) -> str:
    return {0: "NONE", 1: "SELF_DRIVING", 2: "AUTOSTEER", 3: "TACC"}.get(value, "-")
