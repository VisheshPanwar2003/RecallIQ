import hashlib
import json
import os
from pathlib import Path
from threading import RLock

from app.chunking import chunk_pages
from app.database import DocumentStore, connect
from app.document_management import DocumentNotFound, delete_document, storage_paths
from app.pdf_extraction import extract_pages
from app.vectors import IndexUnavailable


# Keep indexing/search/deletion from observing half-completed local operations.
semantic_lock = RLock()


def active_indexes(store: DocumentStore, document_ids=()):
    with connect(store.path) as connection:
        rows = connection.execute("""
            SELECT documents.id, documents.original_filename, document_indexes.generation,
                   document_indexes.embedding_config FROM documents
            JOIN document_indexes ON documents.id = document_indexes.document_id
        """).fetchall()
    requested = set(document_ids)
    return {row["id"]: dict(row) for row in rows if not requested or row["id"] in requested}


def drain_vector_cleanup(store, vectors, config):
    with connect(store.path) as connection:
        ids = [row[0] for row in connection.execute("SELECT document_id FROM vector_cleanup")]
    for document_id in ids:
        vectors.remove(document_id, config)
        with connect(store.path) as connection:
            connection.execute("DELETE FROM vector_cleanup WHERE document_id = ?", (document_id,))


def index_document(store, directory: Path, vectors, embedder, document_id: str):
    with semantic_lock:
        drain_vector_cleanup(store, vectors, embedder.config)
        document = store.get(document_id)
        if document is None:
            raise DocumentNotFound()
        path, _ = storage_paths(directory, document_id, document["stored_filename"])
        pages = extract_pages(path)
        size = int(os.environ.get("RECALLIQ_CHUNK_SIZE", "800"))
        overlap = int(os.environ.get("RECALLIQ_CHUNK_OVERLAP", "120"))
        chunks = chunk_pages(document_id, pages, size, overlap)
        config_json = json.dumps(embedder.config, sort_keys=True)
        digest = hashlib.sha256(config_json.encode())
        for chunk in chunks:
            digest.update(chunk.chunk_id.encode())
        generation = digest.hexdigest()
        current = active_indexes(store, [document_id]).get(document_id)
        # A complete matching generation is immutable and can be reused.
        if (current and current["generation"] == generation
                and vectors.complete(document_id, generation, len(chunks), embedder.config)):
            vectors.remove(document_id, embedder.config, keep_generation=generation)
            return {"document_id": document_id, "pages_processed": len(pages),
                    "chunks_indexed": len(chunks), "status": "indexed"}
        if current and current["generation"] == generation:
            # Lost/edited vector data must be hidden while reconstructing it.
            with connect(store.path) as connection:
                connection.execute("DELETE FROM document_indexes WHERE document_id = ?", (document_id,))
        embeddings = embedder.encode([chunk.text for chunk in chunks])
        # Partial generations are invisible; retry upserts deterministic IDs.
        # A different old active generation remains available if this fails.
        vectors.write(chunks, embeddings, generation, embedder.config)
        with connect(store.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
                raise DocumentNotFound()
            connection.execute("""
                INSERT INTO document_indexes (document_id, generation, embedding_config)
                VALUES (?, ?, ?) ON CONFLICT(document_id) DO UPDATE SET
                generation=excluded.generation, embedding_config=excluded.embedding_config
            """, (document_id, generation, config_json))
        vectors.remove(document_id, embedder.config, keep_generation=generation)
        return {"document_id": document_id, "pages_processed": len(pages),
                "chunks_indexed": len(chunks), "status": "indexed"}


def search_documents(store, vectors, embedder, request):
    with semantic_lock:
        drain_vector_cleanup(store, vectors, embedder.config)
        ids = [str(value) for value in request.document_ids]
        active = active_indexes(store, ids)
        if not active:
            return {"results": []}
        expected = json.dumps(embedder.config, sort_keys=True)
        if any(row["embedding_config"] != expected for row in active.values()):
            raise IndexUnavailable("Embedding configuration differs from the stored index.")
        embedding = embedder.encode([request.query])[0]
        matches = vectors.query(embedding, request.top_k, active, embedder.config)
        # Recheck SQLite after retrieval: deleted or superseded generations never surface.
        live = active_indexes(store, ids)
        results = []
        for metadata, text, distance in matches:
            document_id = metadata["document_id"]
            row = live.get(document_id)
            if not row or metadata["generation"] != row["generation"]:
                continue
            results.append({
                "document_id": document_id, "filename": row["original_filename"],
                "chunk_id": metadata["chunk_id"], "page_number": metadata["page_number"],
                "chunk_text": text, "distance": float(distance),
            })
        results.sort(key=lambda item: (item["distance"], item["chunk_id"]))
        return {"results": results[:request.top_k]}


def delete_indexed_document(store, directory, vectors, embedder, document_id):
    with semantic_lock:
        try:
            delete_document(store, directory, document_id)
        except DocumentNotFound:
            raise
        except Exception:
            # File-finalization can fail after commit; still attempt vector cleanup.
            if store.get(document_id) is None:
                drain_vector_cleanup(store, vectors, embedder.config)
            raise
        drain_vector_cleanup(store, vectors, embedder.config)
