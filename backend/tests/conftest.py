import pytest

from app.database import get_database_path
from app.main import app
from app.uploads import get_uploads_directory
from app.vectors import VectorIndex, get_vector_index


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path, monkeypatch):
    # Routine tests must never fall back to model downloads.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("RECALLIQ_MODELS_OFFLINE", "1")
    monkeypatch.setenv("RECALLIQ_VAULT_MODE", "disabled")
    monkeypatch.setenv("RECALLIQ_TEMP_DIRECTORY", str(tmp_path / "private-temp"))
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_database_path] = lambda: tmp_path / "database" / "test.sqlite3"
    app.dependency_overrides[get_uploads_directory] = lambda: tmp_path / "uploads"
    app.dependency_overrides[get_vector_index] = lambda: VectorIndex(tmp_path / "vectors")
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
