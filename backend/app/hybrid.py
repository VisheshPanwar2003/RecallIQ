"""Rank-based fusion; lexical and vector scores are not numerically comparable."""
import os

from app.keyword import keyword_results, ready_indexes
from app.semantic import active_indexes, drain_vector_cleanup, search_documents, semantic_lock
from app.vectors import IndexUnavailable


RRF_K = 60


def fuse_results(keyword, semantic, top_k):
    merged = {}
    for source, results in (("keyword", keyword), ("semantic", semantic)):
        seen = set()
        for rank, result in enumerate(results, 1):
            chunk_id = result["chunk_id"]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            match = merged.setdefault(chunk_id, {
                key: result[key] for key in
                ("document_id", "filename", "page_number", "chunk_id", "chunk_text")
            })
            match.setdefault("rrf_score", 0.0)
            match.setdefault("keyword_rank", None)
            match.setdefault("semantic_rank", None)
            match["rrf_score"] += 1 / (RRF_K + rank)
            match[f"{source}_rank"] = rank
    return sorted(merged.values(), key=lambda row: (-row["rrf_score"], row["chunk_id"]))[:top_k]


def search_hybrid(store, vectors, embedder, request):
    try:
        pool = int(os.environ.get("RECALLIQ_SEARCH_CANDIDATES", "50"))
        if not 1 <= pool <= 200:
            raise ValueError()
    except ValueError as error:
        raise IndexUnavailable("RECALLIQ_SEARCH_CANDIDATES must be an integer from 1 to 200.") from error
    pool = max(pool, request.top_k)
    with semantic_lock:
        drain_vector_cleanup(store, vectors, embedder.config)
        active = ready_indexes(store, active_indexes(store, [str(value) for value in request.document_ids]))
        # Also hide missing vector data, even if its SQLite manifest survived.
        active = {key: row for key, row in active.items()
                  if vectors.complete(key, row["generation"], row["chunk_count"], embedder.config)}
        keyword = keyword_results(store, request.query, pool, active)
        semantic = search_documents(store, vectors, embedder, request, eligible=active, limit=pool)["results"]
        return {"results": fuse_results(keyword, semantic, request.top_k)}
