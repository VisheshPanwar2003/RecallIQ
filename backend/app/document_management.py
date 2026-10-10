from pathlib import Path
from uuid import UUID

from app.database import DocumentStore, connect


class DocumentNotFound(ValueError):
    pass


def storage_paths(directory: Path, document_id: str, stored_filename: str):
    # Validate database values too: damaged or edited metadata must not escape storage.
    if str(UUID(document_id)) != document_id or stored_filename != f"{document_id}.pdf":
        raise ValueError("Invalid stored document filename")
    root = directory.resolve()
    original = root / stored_filename
    staged = root / f"{stored_filename}.deleting"
    for path in (original, staged):
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Unsafe document storage path")
    return original, staged


def delete_document(store: DocumentStore, directory: Path, document_id: str) -> None:
    moved = False
    original = staged = None
    with connect(store.path) as connection:
        # Serialize deletions/recovery across connections and processes.
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise DocumentNotFound()
            original, staged = storage_paths(directory, document_id, row["stored_filename"])
            if staged.exists():
                raise OSError("Pending deletion must be recovered first")
            if original.exists() and not original.is_file():
                raise OSError("Stored document is not a regular file")
            try:
                original.rename(staged)
                moved = True
            except FileNotFoundError:
                # A missing source file is an already-completed filesystem deletion.
                pass
            connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            connection.execute(
                "INSERT OR IGNORE INTO vector_cleanup (document_id) VALUES (?)", (document_id,)
            )
            connection.commit()
        except Exception:
            # Restore before rollback releases the lock to another delete/recovery.
            if moved:
                staged.rename(original)
            raise
    if moved:
        # A failure here remains visible as HTTP 500; startup recovery retries it.
        staged.unlink(missing_ok=True)


def recover_deletions(store: DocumentStore, directory: Path) -> None:
    if not directory.exists():
        return
    with connect(store.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for staged in directory.glob("*.pdf.deleting"):
            document_id = staged.name.removesuffix(".pdf.deleting")
            original, safe_staged = storage_paths(directory, document_id, f"{document_id}.pdf")
            row = connection.execute(
                "SELECT stored_filename FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is not None:
                if row["stored_filename"] != original.name or original.exists():
                    raise OSError("Cannot safely restore staged document")
                safe_staged.rename(original)
            else:
                safe_staged.unlink(missing_ok=True)
