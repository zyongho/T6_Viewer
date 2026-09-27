import pytest


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch, tmp_path):
    """Tests must never read or overwrite the user's settings, watch history
    or caches (normally in %LOCALAPPDATA%\T6Viewer)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("T6VIEWER_HOME", str(tmp_path / "appdata"))
