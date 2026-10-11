"""Disposable SQLite databases only; no user source data is opened."""
import hashlib
import sqlite3
import threading

import pytest

from app.migration_source import ReadOnlySource, MigrationError
from app.source_exclusive import SourceBusy, exclusive_source
from app.database import get_database_path
from app.main import app
from app.uploads import get_uploads_directory
from fastapi.testclient import TestClient


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / 'source'; folder.mkdir()
    uploads = folder / 'uploads'; uploads.mkdir()
    database = folder / 'source.sqlite3'
    with sqlite3.connect(database) as c:
        c.execute('CREATE TABLE sample(value)')
        c.execute("INSERT INTO sample VALUES('synthetic')")
    c.close()
    return folder, database, uploads, tmp_path / 'destination'


def test_source_immutable_query_no_writes(source):
    folder, database, uploads, destination = source
    before = inventory(folder)
    with ReadOnlySource(database, uploads, destination) as reader:
        assert reader.connection.execute('SELECT * FROM sample').fetchone()[0] == 'synthetic'
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute("INSERT INTO sample VALUES('forbidden')")
        with pytest.raises(PermissionError):
            database.open('wb')
        with pytest.raises(PermissionError):
            database.unlink()
    assert inventory(folder) == before


@pytest.mark.parametrize('suffix', ['-wal', '-journal'])
def test_nonempty_journal_rejected_unchanged(source, suffix):
    folder, database, uploads, destination = source
    database.with_name(database.name + suffix).write_bytes(b'synthetic journal')
    before = inventory(folder)
    with pytest.raises(MigrationError):
        with ReadOnlySource(database, uploads, destination):
            pass
    assert inventory(folder) == before


def test_active_writer_rejected(source):
    folder, database, uploads, destination = source
    writer = sqlite3.connect(database)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO sample VALUES('wal value')"); writer.commit()
        before = inventory(folder)
        with pytest.raises(MigrationError):
            with ReadOnlySource(database, uploads, destination):
                pass
        assert inventory(folder) == before
    finally:
        writer.close()


def test_checkpointed_closed_wal_database(source):
    folder, database, uploads, destination = source
    writer = sqlite3.connect(database)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute("INSERT INTO sample VALUES('checkpointed')"); writer.commit(); writer.close()
    before = inventory(folder)
    with ReadOnlySource(database, uploads, destination) as reader:
        assert reader.connection.execute('SELECT count(*) FROM sample').fetchone()[0] == 2
    assert inventory(folder) == before


def test_missing_or_corrupt_database(source):
    folder, database, uploads, destination = source
    database.write_bytes(b'not sqlite')
    before = inventory(folder)
    with pytest.raises(MigrationError):
        with ReadOnlySource(database, uploads, destination):
            pass
    assert inventory(folder) == before


def test_overlap_refused(source):
    _, database, uploads, _ = source
    with pytest.raises(MigrationError):
        ReadOnlySource(database, uploads, uploads / 'vault')


def test_source_store_mutex_blocks_another_process_thread(source, tmp_path):
    _, database, uploads, _ = source
    other_uploads = tmp_path / 'other-uploads'; other_uploads.mkdir()
    outcome = []

    def contend():
        try:
            with exclusive_source(database, other_uploads):
                outcome.append('acquired')
        except SourceBusy:
            outcome.append('blocked')

    with exclusive_source(database, uploads):
        thread = threading.Thread(target=contend)
        thread.start(); thread.join(timeout=3)
        assert not thread.is_alive()
    assert outcome == ['blocked']


def test_development_http_request_fails_while_migration_owns_source(source):
    _, database, uploads, _ = source
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_database_path] = lambda: database
    app.dependency_overrides[get_uploads_directory] = lambda: uploads
    client = TestClient(app)
    try:
        with exclusive_source(database, uploads):
            assert client.get('/').status_code == 503
        assert client.get('/').status_code == 200
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
