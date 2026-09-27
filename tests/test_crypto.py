from pathlib import Path
import hashlib
import json
import struct

from Crypto.Cipher import AES

from t6_viewer.crypto import CHUNK_SIZE, archive_encrypted_outputs, delete_verified_encrypted_source, discover_encrypted_sidecars, discover_events, discover_groups, decrypt_file, is_plain_json, is_plain_mp4


def _write_plain(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0\0\0\x18ftypmp42" + b"video")


def test_plain_mp4_detection(tmp_path: Path):
    path = tmp_path / "2026-09-26_12-00-00-front.mp4"
    _write_plain(path)
    assert is_plain_mp4(path)


def test_groups_six_camera_names(tmp_path: Path):
    root = tmp_path / "TeslaCam" / "RecentClips"
    for camera in ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"):
        _write_plain(root / f"2026-09-26_12-00-00-{camera}.mp4")
    groups = discover_groups(tmp_path / "TeslaCam")
    assert len(groups) == 1
    assert [clip.camera for clip in groups[0].ordered()] == [
        "front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"
    ]


def test_event_sidecar_is_discovered_and_labeled(tmp_path: Path):
    folder = tmp_path / "TeslaCam" / "SentryClips" / "2026-09-26_12-01-00"
    folder.mkdir(parents=True)
    (folder / "event.json").write_text(
        '{"timestamp":"2026-09-26T12:01:23","reason":"user_interaction_honk","city":"Seoul"}',
        encoding="utf-8",
    )
    events = discover_events(tmp_path / "TeslaCam")
    assert len(events) == 1
    assert events[0].reason_label == "경적 저장"
    assert "Seoul" in events[0].display_label


def test_encrypted_event_sidecar_is_detected(tmp_path: Path):
    folder = tmp_path / "TeslaCam" / "SentryClips" / "2026-09-26_12-01-00"
    folder.mkdir(parents=True)
    path = folder / "event.json"
    path.write_bytes(b"\0\0\0\0encrypted-container")
    assert discover_encrypted_sidecars(tmp_path / "TeslaCam") == [path]


def test_loading_decrypted_tree_archives_only_encrypted_files(tmp_path: Path):
    root = tmp_path / "TeslaCam_Decrypted" / "EncryptedClips"
    folder = root / "SentryClips" / "2026-09-26_12-01-00"
    folder.mkdir(parents=True)
    encrypted_event = folder / "event.json"
    encrypted_event.write_bytes(b"encrypted-event")
    encrypted_video = folder / "2026-09-26_12-01-00-front.mp4"
    encrypted_video.write_bytes(b"encrypted-video")
    plain_video = folder / "2026-09-26_12-01-00-back.mp4"
    _write_plain(plain_video)
    archive, count, errors = archive_encrypted_outputs(root)
    assert count == 2 and not errors
    assert archive == tmp_path / "TeslaCam_Decrypted_EncryptedBackup" / "EncryptedClips"
    assert not encrypted_event.exists() and not encrypted_video.exists()
    assert (archive / encrypted_event.relative_to(root)).read_bytes() == b"encrypted-event"
    assert (archive / encrypted_video.relative_to(root)).read_bytes() == b"encrypted-video"
    assert plain_video.exists()
    assert archive_encrypted_outputs(root)[1] == 0


def test_archive_collision_never_overwrites_or_removes_source(tmp_path: Path):
    root = tmp_path / "TeslaCam_Decrypted"
    root.mkdir()
    encrypted_event = root / "event.json"
    encrypted_event.write_bytes(b"new-encrypted")
    archive = tmp_path / "TeslaCam_Decrypted_EncryptedBackup"
    archive.mkdir()
    (archive / "event.json").write_bytes(b"older-encrypted")
    _, count, errors = archive_encrypted_outputs(root)
    assert count == 0 and errors
    assert encrypted_event.read_bytes() == b"new-encrypted"
    assert (archive / "event.json").read_bytes() == b"older-encrypted"


def test_synthetic_encrypted_clip_roundtrip(tmp_path: Path):
    key = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    plaintext = b"synthetic tesla video" * 300
    padded = plaintext + bytes([16 - len(plaintext) % 16]) * (16 - len(plaintext) % 16)
    encrypted_chunks = []
    for offset in range(0, len(padded), 4096):
        block = padded[offset : offset + 4096]
        encrypted_chunks.append(iv + AES.new(key, AES.MODE_CBC, iv).encrypt(block))
    source = tmp_path / "2026-09-26_12-00-00-front.mp4"
    source.write_bytes(b"TSLC" + bytes(16) + b"".join(encrypted_chunks))
    assert not is_plain_mp4(source)
    destination = tmp_path / "out" / source.name
    assert decrypt_file(source, destination, key) == len(plaintext)
    assert destination.read_bytes() == plaintext


def test_real_page_encrypted_clip_roundtrip(tmp_path: Path):
    key = b"0123456789abcdef"
    plaintext = b"real page payload" * 500
    header = bytearray(0x2000)
    struct.pack_into(">Q", header, 0, len(plaintext))
    struct.pack_into(">I", header, 0x14, 0x1000)
    struct.pack_into(">I", header, 0x1000, 1)
    root_iv = hashlib.md5(key).digest()
    pages = []
    for page, offset in enumerate(range(0, len(plaintext), CHUNK_SIZE)):
        block = plaintext[offset : offset + CHUNK_SIZE]
        block = block.ljust(CHUNK_SIZE, b"\0")
        material = bytearray(32)
        material[:16] = root_iv
        number = str(page).encode("ascii")
        material[16 : 16 + len(number)] = number
        iv = hashlib.md5(material).digest()
        pages.append(AES.new(key, AES.MODE_CBC, iv).encrypt(block))
    source = tmp_path / "2026-09-26_12-00-00-front.mp4"
    source.write_bytes(header + b"".join(pages))
    destination = tmp_path / "out" / source.name
    assert decrypt_file(source, destination, key) == len(plaintext)
    assert destination.read_bytes() == plaintext


def test_legacy_event_json_can_be_repaired_in_place(tmp_path: Path):
    key = b"0123456789abcdef"
    plaintext = b'{"timestamp":"2026-09-26T12:01:23","city":"\\uC11C\\uC6B8","reason":"sentry_aware_object_detection"}'
    header = bytearray(0x2000)
    struct.pack_into(">Q", header, 0, len(plaintext))
    block = plaintext.ljust(CHUNK_SIZE, b"\0")
    material = bytearray(32)
    material[:16] = hashlib.md5(key).digest()
    material[16:17] = b"0"
    iv = hashlib.md5(material).digest()
    path = tmp_path / "event.json"
    path.write_bytes(header + AES.new(key, AES.MODE_CBC, iv).encrypt(block))
    assert not is_plain_json(path)
    assert decrypt_file(path, path, key) > 0
    assert is_plain_json(path)
    assert json.loads(path.read_text(encoding="utf-8"))["city"] == "서울"
    assert '"city": "서울"' in path.read_text(encoding="utf-8")


def test_delete_only_after_playable_output_is_verified(tmp_path: Path):
    source = tmp_path / "encrypted.mp4"
    destination = tmp_path / "out.mp4"
    source.write_bytes(b"encrypted")
    destination.write_bytes(b"\0\0\0\x18ftypmp42" + b"video")
    delete_verified_encrypted_source(source, destination)
    assert not source.exists()


def test_delete_refuses_invalid_output(tmp_path: Path):
    source = tmp_path / "encrypted.mp4"
    destination = tmp_path / "out.mp4"
    source.write_bytes(b"encrypted")
    destination.write_bytes(b"not an mp4")
    try:
        delete_verified_encrypted_source(source, destination)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid output must not delete the encrypted source")
    assert source.exists()
