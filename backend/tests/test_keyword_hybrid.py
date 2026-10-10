import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import semantic
from app.database import FTSUnavailable, connect, initialize_database
from app.hybrid import fuse_results
from app.keyword import literal_query
from app.main import app
from app.vectors import VectorIndex
from test_documents import make_pdf, upload
from test_semantic_search import semantic_client, add_indexed


def search(client, mode, query, **options):
    response = client.post(f"/search/{mode}", json={"query": query, **options})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_fts_initialization_and_idempotence(tmp_path):
    path = tmp_path / "schema.sqlite3"
    initialize_database(path)
    initialize_database(path)
    with connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall()[0][0] == 1
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()[0]
        assert "content='chunks'" in sql
        assert connection.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ('"hello"',)).fetchone()[0] == 0


def test_missing_fts_fails_clearly_and_rolls_back(tmp_path, monkeypatch):
    original = sqlite3.connect

    class MissingFTS(sqlite3.Connection):
        def execute(self, sql, *args):
            if "CREATE VIRTUAL TABLE" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: original(*args, **kwargs, factory=MissingFTS))
    path = tmp_path / "unsupported.sqlite3"
    with pytest.raises(FTSUnavailable, match="SQLite FTS5 is required"):
        initialize_database(path)
    with original(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='chunks'").fetchone() is None
    client = TestClient(app)  # No lifespan: exercise the lazy dependency's HTTP error.
    response = client.get("/documents")
    assert response.status_code == 503
    assert "FTS5" in response.json()["detail"]
    client.close()


def test_migrate_existing_document_and_semantic_index(semantic_client):
    client, embedder, vectors, _, database = semantic_client
    document_id, _ = add_indexed(client, "Cats and ResNet-50.")
    # Simulate the previous schema without changing the existing metadata or vectors.
    with connect(database) as connection:
        before = dict(connection.execute("SELECT * FROM documents").fetchone())
        manifest = dict(connection.execute("SELECT * FROM document_indexes").fetchone())
        connection.execute("DROP TABLE chunks_fts")
        connection.execute("DROP TABLE chunks")
        connection.execute("DROP TABLE keyword_indexes")
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("INSERT INTO vector_cleanup VALUES (?)", (str(uuid4()),))
    initialize_database(database)
    with connect(database) as connection:
        assert dict(connection.execute("SELECT * FROM documents").fetchone()) == before
        assert dict(connection.execute("SELECT * FROM document_indexes").fetchone()) == manifest
        assert connection.execute("SELECT count(*) FROM vector_cleanup").fetchone()[0] == 1
    assert search(client, "semantic", "cats")[0]["document_id"] == document_id
    assert search(client, "keyword", "ResNet-50") == []
    assert search(client, "hybrid", "cats") == []
    calls = embedder.calls
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert embedder.calls == calls
    assert search(client, "keyword", "ResNet-50")[0]["document_id"] == document_id
    assert vectors.collection(embedder.config).count() == 1


@pytest.mark.parametrize("query", ["ResNet-50", '"ResNet-50"', "resnet_50", "(ResNet-50)!", "NASA", "NASA gradients", '"skip connections"', '"skip connections', "NASA OR", "NASA*", "NASA:)"])
def test_keyword_technical_identifiers_and_safe_syntax(semantic_client, query):
    client, embedder, _, _, _ = semantic_client
    document_id, _ = add_indexed(client, "NASA uses ResNet-50 and skip connections for gradients.", "technical.pdf")
    calls = embedder.calls
    matches = search(client, "keyword", query)
    assert matches[0]["document_id"] == document_id
    assert matches[0]["filename"] == "technical.pdf"
    assert matches[0]["page_number"] == 1
    assert matches[0]["bm25_score"] < 0
    assert embedder.calls == calls


@pytest.mark.parametrize("query", ["*", '"', "():+-", "💡", "' ; DROP TABLE documents; --", "text:nonexistent NEAR("])
def test_malformed_queries_are_safe(semantic_client, query):
    client, _, _, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats and kittens.")
    assert search(client, "keyword", query) == []
    assert client.get(f"/documents/{document_id}").status_code == 200


def test_literal_fts_construction():
    assert literal_query('ResNet-50 "skip connections" OR x*') == '"ResNet 50" OR "skip connections" OR "OR" OR "x"'
    assert literal_query('"" -:*') == ""


def test_phrase_requires_adjacency_and_multiword_uses_or(semantic_client):
    client, _, _, _, _ = semantic_client
    exact, _ = add_indexed(client, "Cats skip connections.")
    separated, _ = add_indexed(client, "Cats skip some connections.")
    assert {row["document_id"] for row in search(client, "keyword", '"skip connections"')} == {exact}
    assert {row["document_id"] for row in search(client, "keyword", "skip unmatched")} == {exact, separated}


def test_bm25_ranks_repeated_term_above_equal_length_control(semantic_client):
    client, _, _, _, _ = semantic_client
    strong, _ = add_indexed(client, "rocket rocket rocket rocket filler filler")
    weak, _ = add_indexed(client, "rocket filler filler filler filler filler")
    results = search(client, "keyword", "rocket")
    assert [row["document_id"] for row in results] == [strong, weak]
    assert results[0]["bm25_score"] < results[1]["bm25_score"]


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_empty_index_filters_and_page_citations(semantic_client, mode):
    client, embedder, _, _, _ = semantic_client
    assert search(client, mode, "cats") == []
    assert embedder.calls == 0
    response = upload(client, make_pdf(("Banana fruit.", "Cats orbit rockets.")), "pages.pdf")
    target = response.json()["document_id"]
    assert client.post(f"/documents/{target}/index").status_code == 200
    other, _ = add_indexed(client, "Cats cats cats.", "other.pdf")
    results = search(client, mode, "rockets", document_ids=[target])
    assert results[0]["document_id"] == target
    assert results[0]["filename"] == "pages.pdf"
    assert results[0]["page_number"] == 2
    assert {row["document_id"] for row in results} == {target}
    assert search(client, mode, "cats", document_ids=[str(uuid4())]) == []
    assert all(row["document_id"] == other for row in search(client, mode, "cats", document_ids=[other]))


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
@pytest.mark.parametrize("body", [{"query": " "}, {"query": "x" * 2001}, {"query": "x", "top_k": 0},
                                   {"query": "x", "top_k": 21}, {"query": "x", "top_k": True},
                                   {"query": "x", "document_ids": ["invalid"]}])
def test_validation(semantic_client, mode, body):
    client, _, _, _, _ = semantic_client
    assert client.post(f"/search/{mode}", json=body).status_code == 422


def test_chunk_identity_idempotence_and_keyword_only_repair(semantic_client):
    client, embedder, vectors, _, database = semantic_client
    document_id, result = add_indexed(client, "Cats and kittens.")
    with connect(database) as connection:
        stored = dict(connection.execute("SELECT * FROM chunks").fetchone())
        connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    calls = embedder.calls
    assert search(client, "keyword", "cats") == []
    assert search(client, "hybrid", "cats") == []
    assert client.post(f"/documents/{document_id}/index").json() == result
    assert embedder.calls == calls
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        assert dict(connection.execute("SELECT * FROM chunks").fetchone()) == stored
        connection.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
    vector = vectors.collection(embedder.config).get(include=["metadatas"])
    assert vector["metadatas"][0]["chunk_id"] == stored["chunk_id"]
    assert vector["ids"][0] == stored["generation"] + ":" + stored["chunk_id"]
    assert search(client, "keyword", "cats")[0]["chunk_id"] == stored["chunk_id"]


def match(chunk_id):
    return {"chunk_id": chunk_id, "document_id": "doc", "filename": "sample.pdf", "page_number": 1, "chunk_text": chunk_id}


def test_rrf_formula_merging_and_tie_breaking():
    keyword = [match("b"), match("a")]
    semantic = [match("a"), match("c")]
    results = fuse_results(keyword, semantic, 3)
    assert [row["chunk_id"] for row in results] == ["a", "b", "c"]
    assert results[0]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert results[0]["keyword_rank"] == 2
    assert results[0]["semantic_rank"] == 1
    assert fuse_results([match("b")], [match("a")], 2)[0]["chunk_id"] == "a"
    assert len(fuse_results([match("a"), match("a")], [], 5)) == 1
    assert fuse_results([], [], 5) == []
    assert fuse_results([], [match("a")], 1)[0]["keyword_rank"] is None


def test_hybrid_no_lexical_hits_and_determinism(semantic_client):
    client, _, _, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats and kittens.")
    assert search(client, "keyword", "feline") == []
    first = search(client, "hybrid", "feline")
    assert first[0]["document_id"] == document_id
    assert first[0]["keyword_rank"] is None
    assert first[0]["semantic_rank"] == 1
    assert first == search(client, "hybrid", "feline")


def test_hybrid_no_semantic_hits(semantic_client, monkeypatch):
    client, _, vectors, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    monkeypatch.setattr(VectorIndex, "query", lambda *args: [])
    results = search(client, "hybrid", "cats")
    assert results[0]["document_id"] == document_id
    assert results[0]["semantic_rank"] is None


def test_candidate_pool_config(semantic_client, monkeypatch):
    client, _, _, _, _ = semantic_client
    add_indexed(client, "Cats.")
    original = VectorIndex.query
    limits = []

    def query(self, embedding, top_k, active, config):
        limits.append(top_k)
        return original(self, embedding, top_k, active, config)

    monkeypatch.setattr(VectorIndex, "query", query)
    monkeypatch.setenv("RECALLIQ_SEARCH_CANDIDATES", "30")
    search(client, "hybrid", "cats", top_k=2)
    assert limits == [30]
    monkeypatch.setenv("RECALLIQ_SEARCH_CANDIDATES", "1")
    search(client, "hybrid", "cats", top_k=5)
    assert limits[-1] == 5
    monkeypatch.setenv("RECALLIQ_SEARCH_CANDIDATES", "invalid")
    assert client.post("/search/hybrid", json={"query": "cats"}).status_code == 503


def test_keyword_write_failure_hides_partial_generation_and_retry_reuses_vectors(semantic_client, monkeypatch):
    client, embedder, vectors, _, database = semantic_client
    document_id = upload(client, make_pdf(("Cats and kittens.",))).json()["document_id"]
    original = semantic.write_keyword_chunks

    def fail(*args):
        original(*args)
        raise RuntimeError("private database path and content")

    with monkeypatch.context() as patch:
        patch.setattr(semantic, "write_keyword_chunks", fail)
        response = client.post(f"/documents/{document_id}/index")
        assert response.status_code == 500
        assert "private" not in response.text
    for mode in ("keyword", "hybrid", "semantic"):
        assert search(client, mode, "cats") == []
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM keyword_indexes").fetchone()[0] == 0
    calls = embedder.calls
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert embedder.calls == calls
    assert vectors.collection(embedder.config).count() == 1
    assert search(client, "hybrid", "cats")


def test_failed_changed_keyword_write_preserves_old_generation(semantic_client, monkeypatch):
    client, _, _, directory, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    (directory / f"{document_id}.pdf").write_bytes(make_pdf(("Bananas fruit.",)))
    with monkeypatch.context() as patch:
        patch.setattr(semantic, "write_keyword_chunks", lambda *args: (_ for _ in ()).throw(RuntimeError("failure")))
        assert client.post(f"/documents/{document_id}/index").status_code == 500
    assert search(client, "keyword", "cats")
    assert search(client, "keyword", "bananas") == []
    assert "Cats" in search(client, "hybrid", "cats")[0]["chunk_text"]
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert search(client, "keyword", "cats") == []
    assert search(client, "keyword", "bananas")


def test_missing_vectors_hidden_from_hybrid_and_repaired(semantic_client):
    client, embedder, vectors, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    vectors.collection(embedder.config).delete(where={"document_id": document_id})
    assert search(client, "hybrid", "cats") == []
    assert search(client, "keyword", "cats")  # Independent keyword mode still works.
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert search(client, "hybrid", "cats")


def test_delete_all_indexes_and_vector_retry(semantic_client, monkeypatch):
    client, embedder, vectors, directory, database = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "remove", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
        assert client.delete(f"/documents/{document_id}").status_code == 500
        assert search(client, "keyword", "cats") == []
        with connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM keyword_indexes").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'cats'").fetchone()[0] == 0
    assert not (directory / f"{document_id}.pdf").exists()
    assert search(client, "hybrid", "cats") == []
    assert vectors.collection(embedder.config).count() == 0
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM vector_cleanup").fetchone()[0] == 0


def test_keyword_delete_failure_restores_file_and_metadata(semantic_client):
    client, _, _, directory, database = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    with connect(database) as connection:
        connection.execute("""CREATE TRIGGER fail_keyword_delete BEFORE DELETE ON chunks BEGIN
                              SELECT RAISE(ABORT, 'generated cleanup failure'); END""")
    assert client.delete(f"/documents/{document_id}").status_code == 500
    assert (directory / f"{document_id}.pdf").exists()
    assert client.get(f"/documents/{document_id}").status_code == 200
    assert search(client, "keyword", "cats")
    with connect(database) as connection:
        connection.execute("DROP TRIGGER fail_keyword_delete")
    assert client.delete(f"/documents/{document_id}").status_code == 204
    assert search(client, "keyword", "cats") == []
    assert search(client, "hybrid", "cats") == []


def test_keyword_database_errors_are_safe(semantic_client):
    client, _, _, _, database = semantic_client
    add_indexed(client, "Cats.")
    with connect(database) as connection:
        connection.execute("DROP TABLE chunks_fts")
    response = client.post("/search/keyword", json={"query": "cats"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Keyword search is unavailable. Please retry."}


def test_search_routes_documented_and_semantic_unchanged(semantic_client):
    client, _, _, _, _ = semantic_client
    add_indexed(client, "Cats.")
    assert set(search(client, "semantic", "cats")[0]) == {
        "document_id", "filename", "page_number", "chunk_id", "chunk_text", "distance"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "/search/keyword" in paths and "/search/hybrid" in paths


def test_silent_incomplete_vector_write_is_not_activated(semantic_client, monkeypatch):
    client, _, _, _, database = semantic_client
    document_id = upload(client, make_pdf(("Cats.", "Rocket space."))).json()["document_id"]
    original = VectorIndex.write
    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "write", lambda self, chunks, embeddings, generation, config:
                      original(self, chunks[:1], embeddings[:1], generation, config))
        assert client.post(f"/documents/{document_id}/index").status_code == 500
    for mode in ("keyword", "hybrid", "semantic"):
        assert search(client, mode, "cats") == []
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM document_indexes").fetchone()[0] == 0
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert search(client, "hybrid", "cats")


def test_post_activation_cleanup_failure_can_be_retried_without_embedding(semantic_client, monkeypatch):
    client, embedder, _, _, _ = semantic_client
    document_id = upload(client, make_pdf(("Cats.",))).json()["document_id"]
    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "remove", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
        assert client.post(f"/documents/{document_id}/index").status_code == 500
    assert search(client, "keyword", "cats")
    assert search(client, "hybrid", "cats")
    calls = embedder.calls
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert embedder.calls == calls


def test_keyword_persistence_after_reopening_client(semantic_client):
    client, _, _, _, _ = semantic_client
    document_id, _ = add_indexed(client, "ResNet-50 uses skip connections.")
    with TestClient(app) as reopened:
        assert search(reopened, "keyword", "ResNet-50")[0]["document_id"] == document_id
        assert search(reopened, "hybrid", "ResNet-50")[0]["document_id"] == document_id


def test_unicode_case_diacritics_and_no_stemming(semantic_client):
    client, _, _, _, _ = semantic_client
    add_indexed(client, "NASA cafe rockets.")
    assert search(client, "keyword", "nasa")
    assert search(client, "keyword", "café")
    assert search(client, "keyword", "rocket") == []


def test_fts_update_trigger(semantic_client):
    client, _, _, _, database = semantic_client
    add_indexed(client, "Cats.")
    with connect(database) as connection:
        connection.execute("UPDATE chunks SET text = ?", ("Bananas.",))
    assert search(client, "keyword", "cats") == []
    assert search(client, "keyword", "bananas")


def test_maximum_length_literal_query(semantic_client):
    client, _, _, _, _ = semantic_client
    add_indexed(client, "Cats.")
    # Many distinct clauses exercise FTS parsing near the API's character limit.
    query = " ".join(f"t{number}" for number in range(421))[:2000]
    assert search(client, "keyword", query) == []
