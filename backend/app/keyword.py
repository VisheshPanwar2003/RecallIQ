"""SQLite chunk storage and literal (not SQL/FTS syntax) keyword retrieval."""
import re


def literal_query(query: str) -> str:
    # Quotes group phrases. Hyphens/underscores separate tokens within a phrase.
    # Operators, prefixes and column selectors are never interpreted as FTS syntax.
    clauses = []
    for phrase, word in re.findall(r'"([^"]*)"|([^\s"]+)', query):
        tokens = re.findall(r"[^\W_]+", phrase or word, flags=re.UNICODE)
        if tokens:
            clauses.append('"' + " ".join(tokens) + '"')
    return " OR ".join(dict.fromkeys(clauses))


def write_keyword_chunks(connection, document_id, generation, chunks):
    """Caller commits these rows, FTS triggers and both manifests together."""
    connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    connection.executemany("""
        INSERT INTO chunks(chunk_id, document_id, generation, page_number, chunk_order, text)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [(chunk.chunk_id, document_id, generation, chunk.page_number, chunk.chunk_order, chunk.text)
          for chunk in chunks])
    connection.execute("""
        INSERT INTO keyword_indexes(document_id, generation, chunk_count) VALUES (?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
        generation=excluded.generation, chunk_count=excluded.chunk_count
    """, (document_id, generation, len(chunks)))


def ready_indexes(store, active):
    """Old semantic-only manifests are ineligible until explicit reindexing."""
    with store.connection() as connection:
        rows = connection.execute("""
            SELECT k.document_id, k.generation, k.chunk_count, count(c.rowid) AS actual
            FROM keyword_indexes k LEFT JOIN chunks c
              ON c.document_id=k.document_id AND c.generation=k.generation
            GROUP BY k.document_id
        """).fetchall()
    return {row["document_id"]: {**active[row["document_id"]], "chunk_count": row["chunk_count"]}
            for row in rows if row["document_id"] in active
            and row["generation"] == active[row["document_id"]]["generation"]
            and row["actual"] == row["chunk_count"]}


def keyword_results(store, query, top_k, active):
    expression = literal_query(query)
    if not expression or not active:
        return []
    # Only placeholders are constructed; user input is always bound as values.
    # A row-value IN list avoids an OR expression deeper than SQLite's limit
    # when a bounded vault contains many documents with just one chunk each.
    filters = ", ".join("(?, ?)" for _ in active)
    parameters = [expression]
    for document_id, row in active.items():
        parameters.extend((document_id, row["generation"]))
    parameters.append(top_k)
    with store.connection() as connection:
        rows = connection.execute(f"""
            SELECT c.document_id, d.original_filename AS filename, c.chunk_id,
                   c.page_number, c.text AS chunk_text, bm25(chunks_fts) AS bm25_score
            FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid
            JOIN documents d ON d.id=c.document_id
            WHERE chunks_fts MATCH ? AND (c.document_id, c.generation) IN (VALUES {filters})
            ORDER BY bm25_score ASC, c.chunk_id ASC LIMIT ?
        """, parameters).fetchall()
    return [dict(row) for row in rows]


def search_keyword(store, request):
    # Imported here to avoid coupling storage initialization to semantic services.
    from app.semantic import active_indexes, semantic_lock
    with semantic_lock:
        active = ready_indexes(store, active_indexes(store, [str(value) for value in request.document_ids]))
        return {"results": keyword_results(store, request.query, request.top_k, active)}
