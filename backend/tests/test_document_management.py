from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app import document_management
from app.database import DocumentStore, connect, get_database_path, initialize_database
from app.main import app
from app.uploads import get_uploads_directory
from test_documents import make_pdf, upload


@pytest.fixture
def managed_client():
    database = app.dependency_overrides[get_database_path]()
    directory = app.dependency_overrides[get_uploads_directory]()
    with TestClient(app) as client:
        yield client, database, directory


def test_database_initialization_and_constraints(tmp_path):
    path = tmp_path / "nested" / "documents.sqlite3"
    initialize_database(path)
    initialize_database(path)
    with connect(path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
        assert columns == {"id", "original_filename", "stored_filename", "file_size_bytes",
                           "total_pages", "total_characters", "created_at"}
        document_id = str(uuid4())
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (document_id, "example.pdf", f"{document_id}.pdf", -1, 1, 1, "now"))


def test_upload_persists_metadata(managed_client):
    client, database, directory = managed_client
    content = make_pdf(("First.", "Second."))
    response = upload(client, content, "example.pdf")
    assert response.status_code == 201
    result = response.json()
    with connect(database) as connection:
        row = dict(connection.execute("SELECT * FROM documents").fetchone())
    assert row["id"] == result["document_id"]
    assert row["original_filename"] == "example.pdf"
    assert row["stored_filename"] == f'{row["id"]}.pdf'
    assert row["file_size_bytes"] == len(content)
    assert row["total_pages"] == result["total_pages"] == 2
    assert row["total_characters"] == result["total_characters"]
    assert datetime.fromisoformat(row["created_at"]).utcoffset().total_seconds() == 0
    assert (directory / row["stored_filename"]).is_file()


def test_empty_list(managed_client):
    client, _, _ = managed_client
    assert client.get("/documents").json() == []


def test_list_order_and_no_private_paths(managed_client):
    client, database, directory = managed_client
    ids = [upload(client, make_pdf(), f"{name}.pdf").json()["document_id"]
           for name in ("first", "second", "third")]
    assert [row["id"] for row in client.get("/documents").json()] == ids[::-1]
    with connect(database) as connection:
        connection.execute("UPDATE documents SET created_at = ?", ("2026-01-01T00:00:00+00:00",))
    result = client.get("/documents")
    assert [row["id"] for row in result.json()] == sorted(ids, reverse=True)
    assert str(directory) not in result.text
    assert str(database) not in result.text
    for row in result.json():
        assert set(row) == {"id", "original_filename", "file_size_bytes", "total_pages",
                            "total_characters", "created_at"}


def test_get_existing_and_unknown_document(managed_client):
    client, _, _ = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    assert response.json()["id"] == document_id
    assert "pages" not in response.json()
    assert "stored_filename" not in response.json()
    assert client.get(f"/documents/{uuid4()}").status_code == 404


@pytest.mark.parametrize("method", ["get", "delete"])
def test_invalid_identifier(managed_client, method):
    client, _, _ = managed_client
    assert getattr(client, method)("/documents/not-a-uuid").status_code == 422


def test_successful_delete(managed_client):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    response = client.delete(f"/documents/{document_id}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/documents/{document_id}").status_code == 404
    assert client.get("/documents").json() == []
    assert list(directory.iterdir()) == []
    assert client.delete(f"/documents/{document_id}").status_code == 404


def test_delete_unknown(managed_client):
    client, _, _ = managed_client
    assert client.delete(f"/documents/{uuid4()}").status_code == 404


def test_missing_file_delete(managed_client):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    (directory / f"{document_id}.pdf").unlink()
    assert client.delete(f"/documents/{document_id}").status_code == 204
    assert client.get(f"/documents/{document_id}").status_code == 404


def test_insert_failure_removes_file_and_record(managed_client, monkeypatch):
    client, _, directory = managed_client

    def fail(*args):
        raise sqlite3.OperationalError("Generated database failure")

    monkeypatch.setattr(DocumentStore, "insert", fail)
    response = upload(client, make_pdf())
    assert response.status_code == 500
    assert list(directory.iterdir()) == []
    assert client.get("/documents").json() == []


def test_rename_failure_preserves_record_and_file(managed_client, monkeypatch):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    original = directory / f"{document_id}.pdf"
    rename = Path.rename

    def fail(path, target):
        if path == original:
            raise PermissionError("Generated locked file")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    response = client.delete(f"/documents/{document_id}")
    assert response.status_code == 500
    assert original.is_file()
    assert client.get(f"/documents/{document_id}").status_code == 200


def test_database_delete_failure_restores_file(managed_client, monkeypatch):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    real_connect = document_management.connect

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql.startswith("DELETE"):
                raise sqlite3.OperationalError("Generated delete failure")
            return self.connection.execute(sql, parameters)

    @contextmanager
    def fail_delete(path):
        with real_connect(path) as connection:
            yield FailingConnection(connection)

    monkeypatch.setattr(document_management, "connect", fail_delete)
    assert client.delete(f"/documents/{document_id}").status_code == 500
    assert (directory / f"{document_id}.pdf").is_file()
    assert not (directory / f"{document_id}.pdf.deleting").exists()
    assert client.get(f"/documents/{document_id}").status_code == 200


def test_final_unlink_failure_is_recovered_at_startup(managed_client, monkeypatch):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    staged = directory / f"{document_id}.pdf.deleting"
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == staged:
            raise PermissionError("Generated deletion failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        assert client.delete(f"/documents/{document_id}").status_code == 500
    assert client.get(f"/documents/{document_id}").status_code == 404
    assert staged.exists()
    with TestClient(app):
        assert not staged.exists()


def test_restart_preserves_metadata_and_restores_uncommitted_delete(managed_client):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    original = directory / f"{document_id}.pdf"
    staged = original.with_name(original.name + ".deleting")
    original.rename(staged)
    with TestClient(app) as restarted:
        assert restarted.get(f"/documents/{document_id}").status_code == 200
        assert original.exists()
        assert not staged.exists()


def test_tampered_storage_path_is_rejected(managed_client):
    client, database, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    outside = directory.parent / "outside.pdf"
    outside.write_bytes(b"non-sensitive test marker")
    # Simulate an edited database that bypassed its filename CHECK constraint.
    with connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("UPDATE documents SET stored_filename = ? WHERE id = ?",
                           ("../outside.pdf", document_id))
    assert client.delete(f"/documents/{document_id}").status_code == 500
    assert outside.read_bytes() == b"non-sensitive test marker"
    assert (directory / f"{document_id}.pdf").exists()


def test_sql_special_characters_in_filename(managed_client):
    client, _, _ = managed_client
    filename = "example'); DROP TABLE documents;--.pdf"
    assert upload(client, make_pdf(), filename).status_code == 201
    assert client.get("/documents").json()[0]["original_filename"] == filename


@pytest.mark.parametrize("content", [b"", b"not a PDF", b"%PDF-1.7\ninvalid"])
def test_rejected_upload_does_not_insert(managed_client, content):
    client, _, _ = managed_client
    assert upload(client, content).status_code == 400
    assert client.get("/documents").json() == []


def test_path_configuration_is_backend_relative(monkeypatch):
    monkeypatch.setenv("RECALLIQ_DATABASE_PATH", "custom/example.sqlite3")
    monkeypatch.setenv("RECALLIQ_UPLOADS_DIRECTORY", "custom/uploads")
    assert get_database_path().parts[-3:] == ("backend", "custom", "example.sqlite3")
    assert get_uploads_directory().parts[-3:] == ("backend", "custom", "uploads")


def test_commit_failure_restores_file_and_record(managed_client, monkeypatch):
    client, _, directory = managed_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    real_connect = document_management.connect

    class CommitFailure:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args):
            return self.connection.execute(*args)

        def commit(self):
            raise sqlite3.OperationalError("Generated commit failure")

    @contextmanager
    def fail_commit(path):
        with real_connect(path) as connection:
            yield CommitFailure(connection)

    monkeypatch.setattr(document_management, "connect", fail_commit)
    assert client.delete(f"/documents/{document_id}").status_code == 500
    assert (directory / f"{document_id}.pdf").exists()
    assert client.get(f"/documents/{document_id}").status_code == 200


def test_database_unavailable_has_safe_response(managed_client, monkeypatch):
    client, _, directory = managed_client
    from app import database

    def fail(path):
        raise sqlite3.OperationalError("Private internal database location")

    monkeypatch.setattr(database, "initialize_database", fail)
    response = client.get("/documents")
    assert response.status_code == 500
    assert response.json() == {"detail": "Document storage is unavailable."}
    assert not directory.exists()


def test_noninteger_counts_rejected(tmp_path):
    path = tmp_path / "test.sqlite3"
    initialize_database(path)
    document_id = str(uuid4())
    with connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (document_id, "sample.pdf", f"{document_id}.pdf", 1.5, 1, 1, "now"))
