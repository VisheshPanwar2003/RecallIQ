"""Internal synthetic-page indexing and existing retrieval over encrypted rows."""
import hashlib
import json
from uuid import UUID

import numpy as np

from app.chunking import chunk_pages
from app.keyword import write_keyword_chunks, search_keyword
from app.semantic import search_documents, semantic_lock
from app.hybrid import search_hybrid
from app.vectors import IndexUnavailable
from app.vault_database import VaultDatabaseUnavailable


MAX_CHUNKS = 10_000
MAX_TEXT_BYTES = 16 * 1024 * 1024


def vector_array(value, dimensions):
    try:
        array = np.asarray(value, dtype="<f4")
        if array.shape != (dimensions,) or not np.isfinite(array).all():
            raise ValueError()
        norm = np.linalg.norm(array.astype(np.float64))
        if not np.isfinite(norm) or norm <= 0:
            raise ValueError()
        return (array / norm).astype("<f4")
    except (ValueError, TypeError, OverflowError):
        raise IndexUnavailable("Vault embedding is incompatible or malformed.") from None


class NumpyVectorIndex:
    """Exact cosine index; no serialization, persistence or Chroma client."""
    def __init__(self, store):
        self.store = store

    def _load(self, config):
        with self.store.owner._mutex:
            try:
                return self._load_locked(config)
            except (IndexUnavailable, VaultDatabaseUnavailable):
                self.store._snapshot = None
                raise
            except Exception:
                self.store._snapshot = None
                raise IndexUnavailable("Vault index is incompatible or malformed.") from None

    def _load_locked(self, config):
        if json.dumps(config, sort_keys=True) != self.store.config_json:
            raise IndexUnavailable("Embedding configuration differs from the stored index.")
        self.store._check()
        with self.store.connection() as connection:
            connection.execute("BEGIN")  # One revision/rows snapshot across SELECTs.
            info = connection.execute("SELECT revision,vault_id,embedding_config FROM vault_info WHERE id=1").fetchone()
            if not info or info['vault_id'] != self.store.owner._vault_id or info['embedding_config'] != self.store.config_json:
                self.store._snapshot = None
                raise IndexUnavailable("Vault index configuration is incompatible.")
            revision = info['revision']
            cached = self.store._snapshot
            if cached is not None and cached[0] == revision:
                return cached[1:]
            # Invalidate BEFORE loading: invalid data cannot leave an old cache.
            self.store._snapshot = None
            count, text_bytes = connection.execute("""SELECT count(*),
                coalesce(sum(length(CAST(c.text AS BLOB))+length(CAST(d.original_filename AS BLOB))),0)
                FROM chunks c JOIN documents d ON d.id=c.document_id""").fetchone()
            if count > MAX_CHUNKS or text_bytes > MAX_TEXT_BYTES:
                raise IndexUnavailable("Vault index exceeds the MVP memory limit.")
            manifests = connection.execute("""SELECT k.document_id,k.generation,k.chunk_count,
                i.embedding_config,i.generation AS semantic_generation,
                count(c.rowid) AS actual,count(e.chunk_rowid) AS vectors
                FROM document_indexes i LEFT JOIN keyword_indexes k ON i.document_id=k.document_id
                LEFT JOIN chunks c ON c.document_id=k.document_id AND c.generation=k.generation
                LEFT JOIN embeddings e ON e.chunk_rowid=c.rowid GROUP BY k.document_id""").fetchall()
            for row in manifests:
                if row['document_id'] is None or row['embedding_config'] != self.store.config_json or row['generation'] != row['semantic_generation'] or row['actual'] != row['chunk_count'] or row['vectors'] != row['chunk_count']:
                    raise IndexUnavailable("Vault index is incomplete.")
            if connection.execute("""SELECT count(*) FROM chunks c LEFT JOIN document_indexes i
                ON i.document_id=c.document_id WHERE i.document_id IS NULL OR c.generation!=i.generation""").fetchone()[0]:
                raise IndexUnavailable("Vault index is incomplete.")
            rows = connection.execute("""SELECT c.chunk_id,c.document_id,c.generation,c.page_number,
                c.chunk_order,c.text,e.format_version,e.dimensions,e.vector
                FROM chunks c JOIN embeddings e ON e.chunk_rowid=c.rowid
                ORDER BY c.chunk_id""").fetchall()
            matrix = np.empty((len(rows), self.store.dimensions), dtype=np.float32)
            metadata = []
            for i, row in enumerate(rows):
                if row['format_version'] != 1 or row['dimensions'] != self.store.dimensions or not isinstance(row['vector'], bytes) or len(row['vector']) != self.store.dimensions * 4:
                    raise IndexUnavailable("Vault embedding is incompatible or malformed.")
                if str(UUID(row['chunk_id'])) != row['chunk_id'] or str(UUID(row['document_id'])) != row['document_id'] or type(row['page_number']) is not int or type(row['chunk_order']) is not int or row['page_number'] < 1 or not 0 <= row['chunk_order'] or not isinstance(row['text'], str) or len(row['text']) > 800:
                    raise IndexUnavailable("Vault chunk is malformed.")
                matrix[i] = vector_array(np.frombuffer(row['vector'], dtype='<f4'), self.store.dimensions)
                metadata.append((dict(document_id=row['document_id'], chunk_id=row['chunk_id'],
                                      page_number=row['page_number'], chunk_order=row['chunk_order'],
                                      generation=row['generation']), row['text']))
        matrix.flags.writeable = False
        self.store._snapshot = (revision, metadata, matrix)
        return metadata, matrix

    def complete(self, document_id, generation, chunk_count, config, *, chunk_ids=None):
        with self.store.owner._mutex:
            metadata, _ = self._load(config)
            ids = {row[0]['chunk_id'] for row in metadata if row[0]['document_id'] == document_id and row[0]['generation'] == generation}
            return ids == set(chunk_ids) if chunk_ids is not None else len(ids) == chunk_count

    def query(self, embedding, top_k, active, config):
        with self.store.owner._mutex:
            metadata, matrix = self._load(config)
            query = vector_array(embedding, self.store.dimensions)
            selected = [i for i, (row, _) in enumerate(metadata) if row['document_id'] in active and row['generation'] == active[row['document_id']]['generation']]
            if not selected:
                return []
            distances = np.clip(1 - matrix[selected] @ query, 0, 2)
            ranked = sorted(zip(selected, distances), key=lambda value: (float(value[1]), metadata[value[0]][0]['chunk_id']))[:top_k]
            return [(metadata[i][0].copy(), metadata[i][1], float(distance)) for i, distance in ranked]

    def remove(self, document_id, config, keep_generation=None):
        with self.store.owner._mutex:
            self.store._snapshot = None
            with self.store.connection() as connection:
                sql = "DELETE FROM embeddings WHERE chunk_rowid IN (SELECT rowid FROM chunks WHERE document_id=?"
                values = [document_id]
                if keep_generation is not None:
                    sql += " AND generation != ?"; values.append(keep_generation)
                connection.execute(sql + ")", values)


class VaultSearchService:
    """Explicit internal interface. Input pages already in memory, never PDFs.

    One service per store. The global retrieval lock is acquired BEFORE the key
    owner's lock, consistently with existing retrieval helpers. Locking the vault
    waits for an active operation, then permanently closes its store and cache.
    """
    def __init__(self, store, embedder):
        self.store = store
        self.embedder = embedder
        self.vectors = NumpyVectorIndex(store)
        with store.owner._mutex:
            self.vectors._load(embedder.config)

    def index_pages(self, document_id, pages):
        with semantic_lock, self.store.owner._mutex:
            self.store._check()
            if self.store.get(document_id) is None:
                raise VaultDatabaseUnavailable()
            if len(pages) > MAX_CHUNKS or sum(len(page.text.encode('utf-8')) for page in pages) > MAX_TEXT_BYTES:
                raise IndexUnavailable("Vault input exceeds the MVP memory limit.")
            chunks = chunk_pages(document_id, pages)
            if len(chunks) > MAX_CHUNKS or not chunks:
                raise IndexUnavailable("Vault input has no usable chunks or exceeds the limit.")
            if json.dumps(self.embedder.config, sort_keys=True) != self.store.config_json:
                raise IndexUnavailable("Embedding configuration differs from the stored index.")
            digest = hashlib.sha256(self.store.config_json.encode())
            for chunk in chunks:
                digest.update(chunk.chunk_id.encode())
            generation = digest.hexdigest()
            arrays = []
            for start in range(0, len(chunks), 32):
                batch = chunks[start:start+32]
                encoded = self.embedder.encode([chunk.text for chunk in batch])
                if len(encoded) != len(batch):
                    raise IndexUnavailable("Embedding batch is incomplete.")
                arrays.extend(vector_array(value, self.store.dimensions) for value in encoded)
            self.store._snapshot = None
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                count = connection.execute("SELECT count(*) FROM chunks WHERE document_id != ?", (document_id,)).fetchone()[0]
                if count + len(chunks) > MAX_CHUNKS:
                    raise IndexUnavailable("Vault index exceeds the MVP memory limit.")
                other_bytes = connection.execute("""SELECT coalesce(sum(length(CAST(c.text AS BLOB))+length(CAST(d.original_filename AS BLOB))),0)
                    FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.document_id!=?""", (document_id,)).fetchone()[0]
                filename = connection.execute("SELECT original_filename FROM documents WHERE id=?", (document_id,)).fetchone()[0]
                if other_bytes + sum(len(chunk.text.encode('utf-8')) + len(filename.encode('utf-8')) for chunk in chunks) > MAX_TEXT_BYTES:
                    raise IndexUnavailable("Vault index exceeds the MVP memory limit.")
                write_keyword_chunks(connection, document_id, generation, chunks)
                connection.execute("""INSERT INTO document_indexes VALUES(?,?,?)
                    ON CONFLICT(document_id) DO UPDATE SET generation=excluded.generation,embedding_config=excluded.embedding_config""", (document_id, generation, self.store.config_json))
                for chunk, array in zip(chunks, arrays):
                    connection.execute("""INSERT INTO embeddings SELECT rowid,1,?,? FROM chunks
                        WHERE document_id=? AND generation=? AND chunk_id=?""",
                                       (self.store.dimensions, array.tobytes(), document_id, generation, chunk.chunk_id))
            self.vectors._load(self.embedder.config)
            return {"document_id": document_id, "pages_processed": len(pages), "chunks_indexed": len(chunks), "status": "indexed"}

    def search(self, request, mode="semantic"):
        with semantic_lock, self.store.owner._mutex:
            self.store._check()
            # Validate even keyword-only and empty searches; no partial fallback.
            self.vectors._load(self.embedder.config)
            if mode == "semantic":
                return search_documents(self.store, self.vectors, self.embedder, request)
            if mode == "keyword":
                return search_keyword(self.store, request)
            if mode == "hybrid":
                return search_hybrid(self.store, self.vectors, self.embedder, request)
            raise ValueError("Unsupported retrieval mode.")

    def delete(self, document_id):
        with semantic_lock, self.store.owner._mutex:
            self.store._snapshot = None
            with self.store.connection() as connection:
                if not connection.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
                    raise VaultDatabaseUnavailable()
                connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
                connection.execute("DELETE FROM vector_cleanup WHERE document_id=?", (document_id,))
            self.vectors._load(self.embedder.config)
