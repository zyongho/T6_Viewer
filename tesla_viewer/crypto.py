"""TeslaCam file discovery and local decryption helpers.

Plain TeslaCam clips are ordinary MP4 files.  Encrypted clips keep the
``.mp4`` suffix but do not contain an ``ftyp`` box at offset 4.  Tesla's
2026.20+ browser flow returns a per-file AES key; this module performs only
the local file decryption step after that key has been obtained.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - reported through a friendly error
    AES = None

CAMERAS = (
    "front",
    "back",
    "left_repeater",
    "right_repeater",
    "left_pillar",
    "right_pillar",
)
CAMERA_LABELS = {
    "front": "Front",
    "back": "Rear",
    "left_repeater": "Left repeater",
    "right_repeater": "Right repeater",
    "left_pillar": "Left pillar",
    "right_pillar": "Right pillar",
}

CHUNK_SIZE = 4096
EXTENDED_HEADER_OFFSET = 0x1000
REAL_CIPHERTEXT_OFFSET = 0x2000
UUID_OFFSET = 4
KEY_ID_OFFSET = EXTENDED_HEADER_OFFSET
PUBLIC_KEY_OFFSET = KEY_ID_OFFSET + 4
PUBLIC_KEY_SIZE = 65
VIN_OFFSET = PUBLIC_KEY_OFFSET + PUBLIC_KEY_SIZE
VIN_SIZE = 17
TIMESTAMP_OFFSET = VIN_OFFSET + VIN_SIZE
WRAPPED_KEY_OFFSET = TIMESTAMP_OFFSET + 8
WRAPPED_KEY_SIZE = 44

CLIP_RE = re.compile(
    r"(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(?P<camera>[a-z0-9_]+)\.mp4$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClipInfo:
    path: Path
    camera: str
    timestamp: datetime | None
    encrypted: bool

    @property
    def label(self) -> str:
        return CAMERA_LABELS.get(self.camera, self.camera.replace("_", " ").title())


@dataclass
class ClipGroup:
    timestamp: str
    clips: dict[str, ClipInfo]
    folder: Path | None = None

    def ordered(self) -> list[ClipInfo | None]:
        return [self.clips.get(camera) for camera in CAMERAS]

    @property
    def encrypted_count(self) -> int:
        return sum(clip.encrypted for clip in self.clips.values())


@dataclass(frozen=True)
class TeslaEvent:
    path: Path
    folder: Path
    timestamp: datetime
    reason: str
    city: str = ""
    street: str = ""
    camera: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def reason_label(self) -> str:
        labels = {
            "user_interaction_honk": "경적 저장",
            "user_interaction_dashcam_icon_tapped": "대시캠 저장",
            "user_interaction_dashcam_panel_save": "대시캠 패널 저장",
            "sentry_aware_object_detection": "Sentry 물체 감지",
        }
        if self.reason in labels:
            return labels[self.reason]
        if self.reason.startswith("sentry_aware_accel_"):
            return f"Sentry 충격 감지 ({self.reason.removeprefix('sentry_aware_accel_')})"
        return self.reason.replace("_", " ").strip() or "이벤트"

    @property
    def display_label(self) -> str:
        location = ", ".join(part for part in (self.city, self.street) if part)
        suffix = f" · {location}" if location else ""
        camera = f" · 감지 카메라 ID {self.camera}" if self.camera else " · 감지 카메라 미상"
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} · {self.reason_label}{camera}{suffix}"


def teslacam_root(path: Path) -> Path:
    """Return the selected TeslaCam directory or its TeslaCam child."""
    path = path.expanduser().resolve()
    if path.name.lower() == "teslacam":
        return path
    child = path / "TeslaCam"
    return child if child.is_dir() else path


def is_plain_mp4(path: Path) -> bool:
    """Detect the normal ISO-BMFF signature without decoding the whole file."""
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
    except OSError:
        return False
    return len(header) >= 8 and header[4:8] == b"ftyp"


def is_plain_json(path: Path) -> bool:
    """Return True when an event sidecar is readable JSON, not an encrypted container."""
    try:
        with path.open("rb") as stream:
            sample = stream.read()
        json.loads(sample.decode("utf-8-sig"))
        return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False


def parse_clip(path: Path) -> ClipInfo | None:
    match = CLIP_RE.search(path.name)
    if not match:
        return None
    stamp = match.group("stamp")
    try:
        timestamp = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        timestamp = None
    return ClipInfo(
        path=path,
        camera=match.group("camera").lower(),
        timestamp=timestamp,
        encrypted=not is_plain_mp4(path),
    )


def discover_clips(root: Path) -> list[ClipInfo]:
    root = teslacam_root(root)
    if not root.exists():
        raise FileNotFoundError(f"TeslaCam folder not found: {root}")
    result = []
    for path in root.rglob("*.mp4"):
        clip = parse_clip(path)
        if clip:
            result.append(clip)
    return sorted(result, key=lambda clip: (clip.timestamp or datetime.min, str(clip.path)))


def discover_groups(root: Path) -> list[ClipGroup]:
    groups: dict[str, ClipGroup] = {}
    for clip in discover_clips(root):
        if not clip.timestamp:
            continue
        timestamp = clip.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        key = f"{clip.path.parent}\0{timestamp}"
        group = groups.setdefault(key, ClipGroup(timestamp=timestamp, clips={}, folder=clip.path.parent))
        # Prefer the first occurrence; duplicate camera names can exist in
        # separate event folders and should not silently replace one another.
        group.clips.setdefault(clip.camera, clip)
    return sorted(groups.values(), key=lambda group: group.timestamp, reverse=True)


def discover_events(root: Path) -> list[TeslaEvent]:
    """Read plaintext event.json/events.json sidecars from a TeslaCam tree."""
    root = teslacam_root(root)
    candidates = sorted(root.rglob("*.json"))
    by_folder: dict[Path, Path] = {}
    for path in candidates:
        if path.name.lower() not in {"event.json", "events.json"}:
            continue
        if not is_plain_json(path):
            continue
        # Prefer the newer events.json when both files are present.
        if path.parent not in by_folder or path.name.lower() == "events.json":
            by_folder[path.parent] = path
    events: list[TeslaEvent] = []

    def number(payload: dict, *names: str) -> float | None:
        for name in names:
            raw = payload.get(name)
            if raw in (None, ""):
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    for path in by_folder.values():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            timestamp_text = str(payload.get("timestamp", "")).strip()
            if timestamp_text.endswith("Z"):
                timestamp_text = timestamp_text[:-1] + "+00:00"
            timestamp = datetime.fromisoformat(timestamp_text).replace(tzinfo=None)
            events.append(
                TeslaEvent(
                    path=path,
                    folder=path.parent,
                    timestamp=timestamp,
                    reason=str(payload.get("reason", "")),
                    city=str(payload.get("city", "")),
                    street=str(payload.get("street", "")),
                    camera=str(payload.get("camera", "")),
                    latitude=number(payload, "latitude", "lat", "est_lat"),
                    longitude=number(payload, "longitude", "lon", "lng", "est_lon"),
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            # Encrypted or damaged sidecars are ignored; they are not event
            # metadata that this viewer can safely interpret.
            continue
    return sorted(events, key=lambda event: event.timestamp, reverse=True)


def discover_encrypted_sidecars(root: Path) -> list[Path]:
    """Find encrypted event.json/events.json files that need the normal key API."""
    root = teslacam_root(root)
    result = []
    for path in root.rglob("*.json"):
        if path.name.lower() in {"event.json", "events.json"} and not is_plain_json(path):
            result.append(path)
    return sorted(result)


def decrypted_archive_root(root: Path) -> Path | None:
    """Return a sibling archive for a selected TeslaCam_Decrypted subtree."""
    root = root.resolve()
    for anchor in (root, *root.parents):
        if anchor.name.casefold() in {"teslacam_decrypted", "teslacam_decryped"}:
            return anchor.parent / f"{anchor.name}_EncryptedBackup" / root.relative_to(anchor)
    return None


def archive_encrypted_outputs(root: Path) -> tuple[Path | None, int, list[str]]:
    """Move encrypted files out of a decrypted tree without losing originals.

    Valid plaintext files are untouched. A conflicting archive path is never
    overwritten, and an unsuccessful move leaves its source in place.
    """
    root = root.resolve()
    archive_root = decrypted_archive_root(root)
    if archive_root is None or not root.is_dir():
        return archive_root, 0, []
    encrypted = discover_encrypted_sidecars(root)
    encrypted.extend(clip.path for clip in discover_clips(root) if clip.encrypted)
    moved = 0
    errors: list[str] = []
    for source in encrypted:
        destination = archive_root / source.relative_to(root)
        if destination.exists():
            errors.append(f"보관 대상이 이미 존재합니다: {destination}")
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            moved += 1
        except OSError as exc:
            errors.append(f"암호화 파일 분리 실패: {source} ({exc})")
    return archive_root, moved, errors


def read_file_uuid(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(UUID_OFFSET)
        raw = stream.read(16)
    if len(raw) != 16:
        raise ValueError(f"Encrypted file header is too short: {path.name}")
    return "-".join((raw[0:4].hex(), raw[4:6].hex(), raw[6:8].hex(), raw[8:10].hex(), raw[10:16].hex()))


def has_extended_header(path: Path) -> bool:
    with path.open("rb") as stream:
        probe = stream.read(EXTENDED_HEADER_OFFSET + 4)
    if probe.startswith(b"TSLC") or len(probe) < EXTENDED_HEADER_OFFSET + 4:
        return False
    metadata_offset = struct.unpack(">I", probe[0x14:0x18])[0]
    return metadata_offset == EXTENDED_HEADER_OFFSET and probe[KEY_ID_OFFSET:KEY_ID_OFFSET + 4] != b"\0" * 4


def read_file_header(path: Path) -> dict:
    """Read the metadata Tesla expects for a key request."""
    header = {"id": read_file_uuid(path)}
    if not has_extended_header(path):
        return header
    with path.open("rb") as stream:
        stream.seek(KEY_ID_OFFSET)
        key_id = stream.read(4)
        public_key = stream.read(PUBLIC_KEY_SIZE)
        vin = stream.read(VIN_SIZE)
        timestamp = stream.read(8)
        wrapped_key = stream.read(WRAPPED_KEY_SIZE)
    vin_text = vin.decode("ascii", errors="ignore").rstrip("\0")
    if len(wrapped_key) != WRAPPED_KEY_SIZE or not public_key.startswith(b"\x04"):
        raise ValueError(f"Invalid Tesla ownership header: {path.name}")
    header.update(
        {
            "vin": vin_text,
            "key_id": struct.unpack(">I", key_id)[0],
            "timestamp": struct.unpack(">Q", timestamp)[0],
            "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
            "public_key": base64.b64encode(public_key).decode("ascii"),
        }
    )
    return header


def _read_plaintext_size(path: Path) -> int:
    with path.open("rb") as stream:
        raw = stream.read(8)
    size = struct.unpack(">Q", raw)[0] if len(raw) == 8 else 0
    if size <= 0:
        raise ValueError(f"Invalid decrypted size in {path.name}")
    return size


def decrypt_file(source: Path, destination: Path, key: bytes) -> int:
    """Decrypt one clip into *destination* and return bytes written.

    The real container uses 0x2000 bytes of headers followed by 4096-byte
    AES-CBC pages whose IV is derived from the file key and page number.
    ``TSLC`` is also accepted for the small synthetic format used by tests.
    """
    if AES is None:
        raise RuntimeError("pycryptodome is required for decryption")
    if len(key) != 16:
        raise ValueError(f"Expected a 16-byte AES key, got {len(key)} bytes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with source.open("rb") as source_stream:
            synthetic = source_stream.read(4) == b"TSLC"
        if synthetic:
            written = _decrypt_synthetic(source, temporary, key)
        else:
            written = _decrypt_real(source, temporary, key)
        if destination.suffix.lower() == ".json":
            try:
                payload = json.loads(temporary.read_text(encoding="utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Decrypted JSON is invalid: {destination.name}") from exc
            if not isinstance(payload, (dict, list)):
                raise ValueError(f"Decrypted JSON has an unexpected shape: {destination.name}")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written = temporary.stat().st_size
        temporary.replace(destination)
        return written
    finally:
        if temporary.exists():
            temporary.unlink()


def delete_verified_encrypted_source(source: Path, destination: Path) -> None:
    """Delete an encrypted source only after a valid playable MP4 exists."""
    if source.resolve() == destination.resolve():
        raise ValueError("원본과 출력 파일이 같아서 삭제를 중단했습니다.")
    if not source.is_file():
        raise FileNotFoundError(f"암호화 원본을 찾지 못했습니다: {source}")
    if not destination.is_file() or destination.stat().st_size <= 0 or not is_plain_mp4(destination):
        raise ValueError(f"복호화된 출력 파일 검증 실패: {destination.name}")
    source.unlink()


def _decrypt_real(source: Path, destination: Path, key: bytes) -> int:
    target_size = _read_plaintext_size(source)
    root_iv = hashlib.md5(key).digest()
    written = 0
    page = 0
    with source.open("rb") as source_stream, destination.open("wb") as output:
        source_stream.seek(REAL_CIPHERTEXT_OFFSET)
        while written < target_size:
            encrypted_page = source_stream.read(CHUNK_SIZE)
            if len(encrypted_page) != CHUNK_SIZE:
                raise ValueError(f"Encrypted page is not 4096 bytes: {source.name}")
            iv_material = bytearray(32)
            iv_material[:16] = root_iv
            page_bytes = str(page).encode("ascii")
            iv_material[16:16 + len(page_bytes)] = page_bytes
            iv = hashlib.md5(iv_material).digest()
            plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted_page)
            output.write(plaintext[: target_size - written])
            written += min(len(plaintext), target_size - written)
            page += 1
    if written != target_size:
        raise ValueError(f"Decrypted output is incomplete: {source.name}")
    return written


def _decrypt_synthetic(source: Path, destination: Path, key: bytes) -> int:
    written = 0
    with source.open("rb") as source_stream, destination.open("wb") as output:
        source_stream.seek(20)
        while True:
            chunk = source_stream.read(CHUNK_SIZE + 16)
            if not chunk:
                break
            if len(chunk) < 17:
                raise ValueError(f"Truncated encrypted chunk: {source.name}")
            iv, ciphertext = chunk[:16], chunk[16:]
            if len(ciphertext) % 16:
                raise ValueError(f"Encrypted chunk is not block aligned: {source.name}")
            plaintext = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
            if len(chunk) < CHUNK_SIZE + 16:
                padding = plaintext[-1]
                if 1 <= padding <= 16 and plaintext.endswith(bytes([padding]) * padding):
                    plaintext = plaintext[:-padding]
            output.write(plaintext)
            written += len(plaintext)
    return written
