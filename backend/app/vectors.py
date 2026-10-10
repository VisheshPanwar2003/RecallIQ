import json
import os
from pathlib import Path

from app.database import BACKEND_DIRECTORY


class IndexUnavailable(RuntimeError):
    pass


def get_vector_directory() -> Path:
    value = Path(os.environ.get("RECALLIQ_VECTOR_DIRECTORY", "indexes/chroma"))
    return value if value.is_absolute() else BACKEND_DIRECTORY / value


class VectorIndex:
    """Lazy local Chroma access with all embeddings supplied explicitly."""

    def __init__(self, path: Path):
        self.path = path

    def collection(self, config, *, create=True):
        if not create and not self.path.exists():
            return None
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        metadata = {"hnsw:space": "cosine", "embedding_config": json.dumps(config, sort_keys=True)}
        if create:
            collection = client.get_or_create_collection(
                "recalliq_chunks", metadata=metadata, embedding_function=None
            )
        else:
            try:
                collection = client.get_collection("recalliq_chunks", embedding_function=None)
            except chromadb.errors.NotFoundError:
                return None
        if collection.metadata.get("embedding_config") != metadata["embedding_config"]:
            raise IndexUnavailable("Embedding configuration differs from the stored index.")
        return collection

    def write(self, chunks, embeddings, generation, config):
        collection = self.collection(config)
        for start in range(0, len(chunks), 128):
            batch = chunks[start:start + 128]
            collection.upsert(
                ids=[f"{generation}:{chunk.chunk_id}" for chunk in batch],
                documents=[chunk.text for chunk in batch],
                embeddings=embeddings[start:start + 128],
                metadatas=[{
                    "document_id": chunk.document_id, "chunk_id": chunk.chunk_id,
                    "page_number": chunk.page_number, "chunk_order": chunk.chunk_order,
                    "generation": generation,
                } for chunk in batch],
            )

    def complete(self, document_id, generation, chunk_count, config):
        collection = self.collection(config, create=False)
        if collection is None:
            return False
        result = collection.get(
            where={"$and": [{"document_id": document_id}, {"generation": generation}]},
            include=[],
        )
        return len(result["ids"]) == chunk_count

    def remove(self, document_id, config, keep_generation=None):
        collection = self.collection(config, create=False)
        if collection is None:
            return
        where = {"document_id": document_id}
        if keep_generation is not None:
            where = {"$and": [where, {"generation": {"$ne": keep_generation}}]}
        collection.delete(where=where)

    def query(self, embedding, top_k, active, config):
        collection = self.collection(config, create=False)
        if collection is None or collection.count() == 0:
            return []
        filters = [{"$and": [{"document_id": key}, {"generation": value["generation"]}]}
                   for key, value in active.items()]
        where = filters[0] if len(filters) == 1 else {"$or": filters}
        result = collection.query(
            query_embeddings=[embedding], n_results=min(top_k, collection.count()),
            where=where, include=["documents", "metadatas", "distances"],
        )
        return list(zip(result["metadatas"][0], result["documents"][0], result["distances"][0]))


def get_vector_index() -> VectorIndex:
    return VectorIndex(get_vector_directory())
