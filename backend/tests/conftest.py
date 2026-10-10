import pytest

from app.database import get_database_path
from app.main import app
from app.uploads import get_uploads_directory


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path):
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_database_path] = lambda: tmp_path / "database" / "test.sqlite3"
    app.dependency_overrides[get_uploads_directory] = lambda: tmp_path / "uploads"
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
