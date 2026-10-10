from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

from fastapi import Depends, HTTPException

from app.schemas import DocumentUploadResponse


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]


def get_database_path() -> Path:
    value = Path(os.environ.get("RECALLIQ_DATABASE_PATH", "data/recalliq.sqlite3"))
    return value if value.is_absolute() else BACKEND_DIRECTORY / value


@contextmanager
def connect(path: Path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
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


class DocumentStore:
    def __init__(self, path: Path):
        self.path = path
        # Explicit lazy initialization also supports clients without lifespan.
        initialize_database(path)

    def insert(self, result: DocumentUploadResponse, file_size_bytes: int) -> None:
        document_id = str(result.document_id)
        with connect(self.path) as connection:
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
        with connect(self.path) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM documents ORDER BY created_at DESC, id DESC"
            )]

    def get(self, document_id: str):
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            return dict(row) if row else None


def get_document_store(path: Path = Depends(get_database_path)) -> DocumentStore:
    try:
        return DocumentStore(path)
    except (sqlite3.Error, OSError) as error:
        raise HTTPException(500, "Document storage is unavailable.") from error
