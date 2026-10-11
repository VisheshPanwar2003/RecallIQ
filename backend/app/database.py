from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from fastapi import Depends, HTTPException

from app.schemas import DocumentUploadResponse
from app.storage import (
    BACKEND_DIRECTORY, check_private_path, get_private_storage,
    validate_path, StorageUnavailable,
)


class FTSUnavailable(RuntimeError):
    pass


def migrate_keyword_schema(connection):
    """Additive, transactional migration; existing metadata/manifests stay intact."""
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(text)")
    except sqlite3.OperationalError as error:
        raise FTSUnavailable("SQLite FTS5 is required. Use a Python build with FTS5 support.") from error
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 1").fetchone():
        return
    connection.execute("""
        CREATE TABLE chunks (
            rowid INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            generation TEXT NOT NULL,
            page_number INTEGER NOT NULL CHECK(page_number >= 1),
            chunk_order INTEGER NOT NULL CHECK(chunk_order >= 0),
            text TEXT NOT NULL,
            UNIQUE(document_id, generation, chunk_id)
        )
    """)
    try:
        connection.execute("""
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                text, content='chunks', content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
    except sqlite3.OperationalError as error:
        raise FTSUnavailable("SQLite FTS5 is required. Use a Python build with FTS5 support.") from error
    connection.execute("""
        CREATE TABLE keyword_indexes (
            document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            generation TEXT NOT NULL,
            chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0)
        )
    """)
    connection.execute("""
        CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
        END
    """)
    connection.execute("""
        CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        END
    """)
    connection.execute("""
        CREATE TRIGGER chunks_update AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
            INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
        END
    """)
    connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")


def get_database_path() -> Path:
    return get_private_storage().database


@contextmanager
def connect(path: Path):
    storage = get_private_storage()
    path = validate_path(path, root=storage.root)
    for suffix in ("-journal", "-wal", "-shm"):
        validate_path(Path(str(path) + suffix), root=storage.root)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # This governs application SQLite, not Chroma's independent connection.
        connection.execute("PRAGMA temp_store = MEMORY")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(path: Path) -> None:
    path = check_private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        initialize_schema(connection)


def initialize_schema(connection):
    """Shared logical schema; caller owns transaction and connection policy."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY NOT NULL CHECK(length(id) = 36),
            original_filename TEXT NOT NULL CHECK(length(trim(original_filename)) > 0),
            stored_filename TEXT NOT NULL UNIQUE CHECK(stored_filename = id || '.pdf'),
            file_size_bytes INTEGER NOT NULL CHECK(typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
            total_pages INTEGER NOT NULL CHECK(typeof(total_pages) = 'integer' AND total_pages >= 0),
            total_characters INTEGER NOT NULL CHECK(typeof(total_characters) = 'integer' AND total_characters >= 0),
            created_at TEXT NOT NULL CHECK(length(created_at) > 0)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS document_indexes (
            document_id TEXT PRIMARY KEY NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            generation TEXT NOT NULL CHECK(length(generation) = 64),
            embedding_config TEXT NOT NULL CHECK(length(embedding_config) > 0)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS vector_cleanup (
            document_id TEXT PRIMARY KEY NOT NULL
        )
    """)
    migrate_keyword_schema(connection)


class DocumentStore:
    def __init__(self, path: Path):
        self.path = path
        # Explicit lazy initialization also supports clients without lifespan.
        initialize_database(path)

    def connection(self):
        return connect(self.path)

    def insert(self, result: DocumentUploadResponse, file_size_bytes: int) -> None:
        document_id = str(result.document_id)
        with self.connection() as connection:
            connection.execute("""
                INSERT INTO documents
                (id, original_filename, stored_filename, file_size_bytes,
                 total_pages, total_characters, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                document_id, result.filename, f"{document_id}.pdf", file_size_bytes,
                result.total_pages, result.total_characters,
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ))

    def list(self):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM documents ORDER BY created_at DESC, id DESC"
            )]

    def get(self, document_id: str):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            return dict(row) if row else None


def get_document_store(path: Path = Depends(get_database_path)) -> DocumentStore:
    try:
        return DocumentStore(path)
    except FTSUnavailable as error:
        raise HTTPException(503, str(error)) from error
    except StorageUnavailable as error:
        raise HTTPException(503, str(error)) from error
    except (sqlite3.Error, OSError) as error:
        raise HTTPException(500, "Document storage is unavailable.") from error
