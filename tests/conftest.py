import pytest


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch, tmp_path):
    """Tests must never read or overwrite the user's settings, watch history
    or caches (normally in %LOCALAPPDATA%\T6Viewer)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("T6VIEWER_HOME", str(tmp_path / "appdata"))


@pytest.fixture(autouse=True)
def fixed_physical_memory(monkeypatch):
    """Memory thresholds scale with the PC's RAM; tests use the fixed floors
    unless they set a RAM size themselves."""
    from t6_viewer import memory_budget
    monkeypatch.setattr(memory_budget, "_total_physical", 0)
