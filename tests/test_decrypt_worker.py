from pathlib import Path

from t6_viewer.main_window import DecryptWorker


def _encrypted_event(path: Path, index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 8 + index.to_bytes(4, "big") + b"x" * 20)


def test_encrypted_event_json_is_skipped_without_key_requests_or_errors(tmp_path: Path, monkeypatch):
    source = tmp_path / "TeslaCam"
    output = tmp_path / "TeslaCam_Decrypted"
    for index in range(3):
        _encrypted_event(source / "SentryClips" / str(index) / "event.json", index)
    calls = []
    monkeypatch.setattr("t6_viewer.main_window.fetch_keys",
                        lambda _token, headers, progress=None: calls.append(list(headers)) or {})
    results = []
    worker = DecryptWorker(source, output, "unused", False)
    worker.finished.connect(lambda *args: results.append(args))
    worker.run()
    # No encrypted video: nothing to do, and event.json never reaches the key API.
    assert calls == []
    assert results == [("복호화할 암호화 영상이 없습니다.", 0, 0, [])]
    assert all((source / "SentryClips" / str(index) / "event.json").exists() for index in range(3))
    assert not list(output.rglob("event.json"))


def test_plain_event_json_is_copied_and_encrypted_one_is_left_alone(tmp_path: Path, monkeypatch):
    source = tmp_path / "TeslaCam"
    output = tmp_path / "TeslaCam_Decrypted"
    plain = source / "SentryClips" / "a" / "event.json"
    plain.parent.mkdir(parents=True)
    plain.write_text('{"reason":"sentry_aware_object_detection"}', encoding="utf-8")
    _encrypted_event(source / "SentryClips" / "b" / "event.json", 1)
    video = source / "SentryClips" / "a" / "2026-09-26_08-00-00-front.mp4"
    video.write_bytes(b"\0" * 64)  # not a plain MP4: counts as encrypted
    headers = []
    monkeypatch.setattr("t6_viewer.main_window.read_file_header", lambda path: {"id": path.name})
    monkeypatch.setattr("t6_viewer.main_window.fetch_keys",
                        lambda _token, items, progress=None: headers.extend(items) or {})
    progress, results = [], []
    worker = DecryptWorker(source, output, "unused", False)
    worker.progress.connect(lambda value, text: progress.append(text))
    worker.finished.connect(lambda *args: results.append(args))
    worker.run()
    assert headers == [{"id": video.name}]            # only the video asks for a key
    assert (output / "SentryClips" / "a" / "event.json").exists()
    assert not (output / "SentryClips" / "b" / "event.json").exists()
    errors = results[0][3]
    assert not any("event" in error.lower() or "이벤트" in error for error in errors)
    assert any("event.json은 영상 복호화 키로 복호화되지 않" in text for text in progress)
