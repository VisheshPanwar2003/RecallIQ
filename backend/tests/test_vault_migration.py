"""End-to-end migration tests use only generated PDFs and disposable stores."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pymupdf
import pytest

from app.chunking import chunk_pages
from app.database import initialize_schema
from app.keyword import write_keyword_chunks
from app.pdf_extraction import extract_pdf_bytes
from app.schemas import ExtractedPage
from app.vault_database import VaultDocumentStore
from app.vault_keys import VaultKeyManager
from app.vault_migration import MigrationError, VaultMigration
from app.vault_documents import VaultDocumentService
from app.vault_search import VaultSearchService
from app.schemas import SemanticSearchRequest


PASSWORD = "generated-migration-test-password"
CONFIG = {"model": "generated-migration-test", "dimensions": 3, "normalize": True}


class Embedder:
    config = CONFIG

    def encode(self, texts):
        return [[float("solar" in text.lower()), float("garden" in text.lower()), 0.25]
                for text in texts]


def make_pdf():
    with pymupdf.open() as document:
        document.new_page().insert_text((60, 60), "Solar panels save energy. Synthetic canary MIGRATION_CANARY_8f3d.")
        document.new_page().insert_text((60, 60), "Garden plants need water and sunlight.")
        return document.tobytes()


def add_source_document(case, text, filename):
    with pymupdf.open() as document:
        document.new_page().insert_text((60, 60), text)
        raw = document.tobytes()
    document_id = str(uuid4())
    (case["uploads"] / f"{document_id}.pdf").write_bytes(raw)
    pages = extract_pdf_bytes(raw, bounded=True)
    chunks = chunk_pages(document_id, pages)
    config_json = json.dumps(CONFIG, sort_keys=True)
    digest = hashlib.sha256(config_json.encode())
    for chunk in chunks:
        digest.update(chunk.chunk_id.encode())
    connection = sqlite3.connect(case["database"])
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", (
            document_id, filename, document_id + ".pdf", len(raw), len(pages),
            sum(page.character_count for page in pages), datetime.now(timezone.utc).isoformat(),
        ))
        connection.execute("INSERT INTO document_indexes VALUES(?,?,?)",
                           (document_id, digest.hexdigest(), json.dumps(CONFIG, sort_keys=True)))
        write_keyword_chunks(connection, document_id, digest.hexdigest(), chunks)
        connection.commit()
    finally:
        connection.close()
    return document_id, raw, chunks


@pytest.fixture
def migration_case(tmp_path):
    source_root = tmp_path / "legacy"
    uploads = source_root / "uploads"
    uploads.mkdir(parents=True)
    database = source_root / "documents.sqlite3"
    document_id = str(uuid4())
    raw = make_pdf()
    (uploads / f"{document_id}.pdf").write_bytes(raw)
    pages = extract_pdf_bytes(raw, bounded=True)
    extracted = [ExtractedPage(page_number=p.page_number, text=p.text,
                               character_count=p.character_count) for p in pages]
    chunks = chunk_pages(document_id, extracted)
    config_json = json.dumps(CONFIG, sort_keys=True)
    generation_hash = hashlib.sha256(config_json.encode())
    for chunk in chunks:
        generation_hash.update(chunk.chunk_id.encode())
    generation = generation_hash.hexdigest()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_schema(connection)
        connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", (
            document_id, "synthetic.pdf", document_id + ".pdf", len(raw), len(pages),
            sum(p.character_count for p in pages), datetime.now(timezone.utc).isoformat(),
        ))
        connection.execute("INSERT INTO document_indexes VALUES(?,?,?)",
                           (document_id, generation, config_json))
        write_keyword_chunks(connection, document_id, generation, chunks)
        connection.commit()
    finally:
        connection.close()

    owner = VaultKeyManager(tmp_path / "vault")
    owner.initialize(PASSWORD)
    owner.unlock(PASSWORD)
    store = VaultDocumentStore(owner, CONFIG, create=True)
    embedder = Embedder()
    migration = VaultMigration(store, embedder)
    try:
        yield {
            "tmp_path": tmp_path, "source_root": source_root, "database": database,
            "uploads": uploads, "document_id": document_id, "raw": raw,
            "pages": pages, "chunks": chunks, "owner": owner, "store": store,
            "migration": migration,
        }
    finally:
        store.close()
        owner.lock()


def test_migration_reembeds_and_verifies_all_search_modes(migration_case):
    case = migration_case
    before_db = hashlib.sha256(case["database"].read_bytes()).hexdigest()
    before_pdf = hashlib.sha256((case["uploads"] / f'{case["document_id"]}.pdf').read_bytes()).hexdigest()
    report = case["migration"].dry_run(case["database"], case["uploads"])
    assert report["status"] == "PREFLIGHT"
    result = case["migration"].execute(case["database"], case["uploads"])
    assert result["status"] == "VERIFIED"
    assert result["documents"] == 1
    assert result["chunks"] == result["embeddings"] == len(case["chunks"])
    with case["store"].connection() as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == len(case["chunks"])
        assert connection.execute("SELECT count(*) FROM embeddings").fetchone()[0] == len(case["chunks"])
        assert connection.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'solar'").fetchone()[0] == 1
        migrated = [dict(row) for row in connection.execute(
            "SELECT chunk_id,page_number,chunk_order,text FROM chunks WHERE document_id=? ORDER BY chunk_order",
            (case["document_id"],),
        )]
    assert migrated == [dict(chunk_id=chunk.chunk_id, page_number=chunk.page_number,
                              chunk_order=chunk.chunk_order, text=chunk.text)
                        for chunk in case["chunks"]]
    search = VaultSearchService(case["store"], Embedder())
    request = SemanticSearchRequest(query="How can solar provide clean electricity?",
                                    document_ids=[case["document_id"]])
    for mode in ("semantic", "keyword", "hybrid"):
        result = search.search(request, mode)["results"][0]
        assert str(result["document_id"]) == case["document_id"]
        assert result["filename"] == "synthetic.pdf"
        assert result["page_number"] == 1
    assert hashlib.sha256(case["database"].read_bytes()).hexdigest() == before_db
    assert hashlib.sha256((case["uploads"] / f'{case["document_id"]}.pdf').read_bytes()).hexdigest() == before_pdf
    for path in case["owner"]._root.rglob("*"):
        if path.is_file():
            assert b"MIGRATION_CANARY_8f3d" not in path.read_bytes()


def test_multiple_documents_keep_ids_pages_and_search_filters(migration_case):
    case = migration_case
    second_id, _, second_chunks = add_source_document(
        case, "Ocean tides shape coastlines and estuaries.", "ocean-notes.pdf")
    source_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [case["database"], *case["uploads"].glob("*.pdf")]
    }
    result = case["migration"].execute(case["database"], case["uploads"])
    assert result["status"] == "VERIFIED"
    assert result["documents"] == 2
    assert set(row["id"] for row in case["store"].list()) == {case["document_id"], second_id}
    search = VaultSearchService(case["store"], Embedder())
    response = search.search(SemanticSearchRequest(query="How do ocean tides influence coasts?",
                                   document_ids=[second_id]), "keyword")["results"]
    assert response[0]["document_id"] == second_id
    assert response[0]["filename"] == "ocean-notes.pdf"
    assert response[0]["page_number"] == 1
    with case["store"].connection() as connection:
        found = [row[0] for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE document_id=?", (second_id,))]
    assert found == [chunk.chunk_id for chunk in second_chunks]
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
               for path, digest in source_hashes.items())


def test_destination_document_id_conflict_is_preserved(migration_case):
    case = migration_case
    with case["store"].connection() as connection:
        connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)", (
            case["document_id"], "unrelated.pdf", case["document_id"] + ".pdf",
            10, 1, 10, datetime.now(timezone.utc).isoformat(),
        ))
    with pytest.raises(MigrationError) as error:
        case["migration"].dry_run(case["database"], case["uploads"])
    assert error.value.code == "destination_conflict"
    with case["store"].connection() as connection:
        assert connection.execute(
            "SELECT original_filename FROM documents WHERE id=?", (case["document_id"],)
        ).fetchone()[0] == "unrelated.pdf"
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='vault_migrations'"
        ).fetchone()[0] == 0


def test_interrupted_migration_resumes_without_duplicate_documents(migration_case, monkeypatch):
    case = migration_case
    migration_id = str(uuid4())

    class SimulatedCrash(BaseException):
        pass

    def crash(transition, *_):
        if transition == "document_verified":
            raise SimulatedCrash()

    monkeypatch.setattr(case["migration"], "_checkpoint", crash)
    with pytest.raises(SimulatedCrash):
        case["migration"].execute(case["database"], case["uploads"], migration_id=migration_id)
    monkeypatch.setattr(case["migration"], "_checkpoint", lambda *_: None)
    result = case["migration"].execute(case["database"], case["uploads"], migration_id=migration_id)
    assert result["status"] == "VERIFIED"
    assert case["store"].list()[0]["id"] == case["document_id"]
    with case["store"].connection() as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


@pytest.mark.parametrize("transition,owner", [
    ("prepared", "migration"),
    ("file_staged", "migration"),
    ("committed", "document"),
])
def test_migration_recovers_at_file_and_database_boundaries(migration_case, monkeypatch, transition, owner):
    case = migration_case
    migration_id = str(uuid4())

    class SimulatedCrash(BaseException):
        pass

    def checkpoint(*args):
        event = args[-1] if owner == "document" else args[0]
        if event == transition:
            raise SimulatedCrash()

    target = case["migration"] if owner == "migration" else VaultDocumentService
    monkeypatch.setattr(target, "_checkpoint", checkpoint)
    with pytest.raises(SimulatedCrash):
        case["migration"].execute(case["database"], case["uploads"], migration_id=migration_id)
    monkeypatch.setattr(target, "_checkpoint", lambda *_: None)
    result = case["migration"].execute(case["database"], case["uploads"], migration_id=migration_id)
    assert result["status"] == "VERIFIED"
    assert len(case["store"].list()) == 1


def test_rollback_requires_confirmation_and_removes_only_owned_document(migration_case):
    case = migration_case
    report = case["migration"].execute(case["database"], case["uploads"])
    with pytest.raises(MigrationError, match="Migration refused"):
        case["migration"].rollback(report["migration_id"], confirmed=True)
    result = case["migration"].rollback(report["migration_id"], confirmed=True,
                                        completed_confirmed=True)
    assert result["status"] == "ROLLED_BACK"
    assert case["store"].list() == []
    assert not list((case["owner"]._root / "documents").glob("*.rpdf"))


def test_rollback_refuses_changed_destination_records(migration_case):
    case = migration_case
    report = case["migration"].execute(case["database"], case["uploads"])
    with case["store"].connection() as connection:
        connection.execute("UPDATE documents SET original_filename='unrelated.pdf' WHERE id=?",
                           (case["document_id"],))
    with pytest.raises(MigrationError) as error:
        case["migration"].rollback(report["migration_id"], confirmed=True,
                                   completed_confirmed=True)
    assert error.value.code == "ambiguous_ownership"
    assert case["store"].get(case["document_id"])["original_filename"] == "unrelated.pdf"


def test_rollback_retries_after_interruption(migration_case, monkeypatch):
    case = migration_case
    report = case["migration"].execute(case["database"], case["uploads"])

    class SimulatedCrash(BaseException):
        pass

    monkeypatch.setattr(case["migration"], "_checkpoint",
                        lambda transition, *_: (_ for _ in ()).throw(SimulatedCrash())
                        if transition == "rollback_document" else None)
    with pytest.raises(SimulatedCrash):
        case["migration"].rollback(report["migration_id"], confirmed=True,
                                    completed_confirmed=True)
    monkeypatch.setattr(case["migration"], "_checkpoint", lambda *_: None)
    result = case["migration"].rollback(report["migration_id"], confirmed=True,
                                        completed_confirmed=True)
    assert result["status"] == "ROLLED_BACK"
    assert case["store"].list() == []


def test_missing_source_pdf_fails_before_destination_migration_writes(migration_case):
    case = migration_case
    original_hash = hashlib.sha256(case["database"].read_bytes()).hexdigest()
    (case["uploads"] / f'{case["document_id"]}.pdf').unlink()
    with pytest.raises(MigrationError):
        case["migration"].execute(case["database"], case["uploads"])
    with case["store"].connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='vault_migrations'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert hashlib.sha256(case["database"].read_bytes()).hexdigest() == original_hash


def test_partial_destination_migration_schema_fails_closed(migration_case):
    case = migration_case
    from app.vault_migration import MigrationDocuments
    MigrationDocuments(case["store"])
    case["migration"]._schema()
    with case["store"].connection() as connection:
        connection.execute("DROP TABLE vault_migration_vectors")
    with pytest.raises(MigrationError) as error:
        case["migration"]._schema()
    assert error.value.code == "unsupported_migration_schema"
