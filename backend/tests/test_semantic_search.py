import math
import sys
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.chunking import chunk_pages
from app.database import connect, get_database_path
from app.embeddings import LocalEmbedder, MODEL_ID, MODEL_REVISION, ModelUnavailable, get_embedder
from app.main import app
from app.schemas import ExtractedPage
from app.uploads import get_uploads_directory
from app.vectors import IndexUnavailable, VectorIndex, get_vector_index
from test_documents import make_pdf, upload


class FakeEmbedder:
    config = {"model": "deterministic-test", "dimensions": 3, "normalize": True}

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        result = []
        for text in texts:
            text = text.lower()
            values = [float(sum(text.count(word) for word in words)) for words in (
                ("cat", "kitten", "feline"), ("banana", "fruit", "apple"), ("rocket", "space", "orbit")
            )]
            norm = math.sqrt(sum(value * value for value in values))
            result.append([value / norm for value in values] if norm else [0.1, 0.1, 0.1])
        return result


def page(number, text):
    return ExtractedPage(page_number=number, text=text, character_count=len(text))


@pytest.fixture
def semantic_client():
    embedder = FakeEmbedder()
    previous = app.dependency_overrides.get(get_embedder)
    app.dependency_overrides[get_embedder] = lambda: embedder
    vectors = app.dependency_overrides[get_vector_index]()
    directory = app.dependency_overrides[get_uploads_directory]()
    database = app.dependency_overrides[get_database_path]()
    try:
        with TestClient(app) as client:
            yield client, embedder, vectors, directory, database
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_embedder, None)
        else:
            app.dependency_overrides[get_embedder] = previous


def add_indexed(client, text, filename="example.pdf"):
    response = upload(client, make_pdf((text,)), filename)
    assert response.status_code == 201
    document_id = response.json()["document_id"]
    result = client.post(f"/documents/{document_id}/index")
    assert result.status_code == 200, result.text
    return document_id, result.json()


def test_empty_and_whitespace_pages():
    assert chunk_pages(str(uuid4()), [page(1, ""), page(2, " \n\t ")]) == []


def test_short_page_metadata_and_stable_ids():
    document_id = str(uuid4())
    pages = [page(1, ""), page(2, "  Short text. \n")]
    first = chunk_pages(document_id, pages)
    assert first == chunk_pages(document_id, pages)
    assert len(first) == 1
    assert first[0].text == "Short text."
    assert first[0].page_number == 2
    assert first[0].document_id == document_id
    assert first[0].chunk_order == 0
    assert first[0].chunk_id != chunk_pages(str(uuid4()), pages)[0].chunk_id


def test_long_paragraph_overlap_and_progress():
    text = "0123456789" * 301
    chunks = chunk_pages(str(uuid4()), [page(3, text)])
    assert len(chunks) > 1
    assert all(0 < len(chunk.text) <= 800 for chunk in chunks)
    assert all(chunk.page_number == 3 for chunk in chunks)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert [chunk.chunk_order for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0].text[-120:] == chunks[1].text[:120]


def test_natural_boundary_and_pages_not_mixed():
    chunks = chunk_pages(str(uuid4()), [page(1, "A" * 600 + ". " + "B" * 600), page(2, "Second page.")])
    assert chunks[0].text.endswith(".")
    assert chunks[-1].text == "Second page."
    assert chunks[-1].page_number == 2


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, -1), (100, 100)])
def test_invalid_chunk_settings(size, overlap):
    with pytest.raises(ValueError):
        chunk_pages(str(uuid4()), [], size, overlap)


def test_index_stored_pdf_and_idempotence(semantic_client):
    client, embedder, vectors, _, _ = semantic_client
    document_id, result = add_indexed(client, "Cats are feline animals.")
    assert result == {"document_id": document_id, "pages_processed": 1,
                      "chunks_indexed": 1, "status": "indexed"}
    assert vectors.collection(embedder.config).count() == 1
    calls = embedder.calls
    assert client.post(f"/documents/{document_id}/index").json() == result
    assert embedder.calls == calls
    assert vectors.collection(embedder.config).count() == 1


def test_search_relevance_and_filtering(semantic_client):
    client, _, _, _, _ = semantic_client
    cats, _ = add_indexed(client, "Cats and kittens are feline animals.", "cats.pdf")
    fruit, _ = add_indexed(client, "Bananas and apples are fruit.", "fruit.pdf")
    response = client.post("/search/semantic", json={"query": "feline kitten", "top_k": 2})
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["document_id"] == cats
    assert results[0]["filename"] == "cats.pdf"
    assert results[0]["page_number"] == 1
    assert results[0]["distance"] < results[1]["distance"]
    assert results[0]["distance"] == pytest.approx(0.0, abs=1e-5)
    assert results[1]["distance"] == pytest.approx(1.0, abs=1e-5)
    filtered = client.post("/search/semantic", json={"query": "kitten", "document_ids": [fruit]})
    assert {row["document_id"] for row in filtered.json()["results"]} == {fruit}
    assert client.post("/search/semantic", json={"query": "kitten", "document_ids": []}).json()["results"]


def test_empty_index_and_unknown_filter_do_not_load_model(semantic_client):
    client, embedder, _, _, _ = semantic_client
    assert client.post("/search/semantic", json={"query": "cats"}).json() == {"results": []}
    document_id, _ = add_indexed(client, "Cats.")
    before = embedder.calls
    assert client.post("/search/semantic", json={"query": "cats", "document_ids": [str(uuid4())]}).json() == {"results": []}
    assert embedder.calls == before
    assert client.get(f"/documents/{document_id}").status_code == 200


@pytest.mark.parametrize("body", [
    {"query": ""}, {"query": " \n\t"}, {"query": "x" * 2001},
    {"query": "cats", "top_k": 0}, {"query": "cats", "top_k": 21},
    {"query": "cats", "top_k": True}, {"query": "cats", "document_ids": ["not-a-uuid"]},
])
def test_search_validation(semantic_client, body):
    client, embedder, _, _, _ = semantic_client
    assert client.post("/search/semantic", json=body).status_code == 422
    assert embedder.calls == 0


def test_unknown_and_invalid_index_ids(semantic_client):
    client, embedder, _, _, _ = semantic_client
    assert client.post(f"/documents/{uuid4()}/index").status_code == 404
    assert client.post("/documents/invalid/index").status_code == 422
    assert embedder.calls == 0


def test_missing_pdf(semantic_client):
    client, _, _, directory, _ = semantic_client
    document_id = upload(client, make_pdf()).json()["document_id"]
    (directory / f"{document_id}.pdf").unlink()
    response = client.post(f"/documents/{document_id}/index")
    assert response.status_code == 409
    assert str(directory) not in response.text


def test_partial_index_failure_and_retry(semantic_client, monkeypatch):
    client, embedder, vectors, _, _ = semantic_client
    text = "\n".join(["Cats " * 15] * 30)
    document_id = upload(client, make_pdf((text,))).json()["document_id"]
    original = VectorIndex.write

    def fail(self, chunks, embeddings, generation, config):
        original(self, chunks[:1], embeddings[:1], generation, config)
        raise RuntimeError("Generated vector write failure")

    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "write", fail)
        assert client.post(f"/documents/{document_id}/index").status_code == 500
    assert client.post("/search/semantic", json={"query": "cats"}).json() == {"results": []}
    result = client.post(f"/documents/{document_id}/index")
    assert result.status_code == 200
    assert result.json()["chunks_indexed"] > 1
    assert vectors.collection(embedder.config).count() == result.json()["chunks_indexed"]


def test_old_index_remains_active_after_failed_reindex(semantic_client, monkeypatch):
    client, _, _, directory, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats and kittens.")
    (directory / f"{document_id}.pdf").write_bytes(make_pdf(("Bananas and apples.",)))
    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "write", lambda *args: (_ for _ in ()).throw(RuntimeError("Generated failure")))
        assert client.post(f"/documents/{document_id}/index").status_code == 500
    assert "Cats" in client.post("/search/semantic", json={"query": "cats"}).json()["results"][0]["chunk_text"]
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert "Bananas" in client.post("/search/semantic", json={"query": "banana"}).json()["results"][0]["chunk_text"]


def test_model_failure_has_safe_error(semantic_client, monkeypatch):
    client, embedder, _, _, _ = semantic_client
    document_id = upload(client, make_pdf()).json()["document_id"]

    def fail(texts):
        raise ModelUnavailable("Local embedding model unavailable.")

    monkeypatch.setattr(embedder, "encode", fail)
    response = client.post(f"/documents/{document_id}/index")
    assert response.status_code == 503
    assert "model unavailable" in response.json()["detail"]


def test_delete_cleans_vectors_and_manifest(semantic_client):
    client, embedder, vectors, _, database = semantic_client
    document_id, _ = add_indexed(client, "Cats and kittens.")
    assert client.delete(f"/documents/{document_id}").status_code == 204
    assert vectors.collection(embedder.config).count() == 0
    assert client.post("/search/semantic", json={"query": "cats"}).json() == {"results": []}
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM document_indexes").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM vector_cleanup").fetchone()[0] == 0


def test_failed_vector_cleanup_is_journaled_and_retried(semantic_client, monkeypatch):
    client, embedder, vectors, _, database = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    with monkeypatch.context() as patch:
        patch.setattr(VectorIndex, "remove", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Generated cleanup failure")))
        assert client.delete(f"/documents/{document_id}").status_code == 500
        with connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM vector_cleanup").fetchone()[0] == 1
    assert client.post("/search/semantic", json={"query": "cats"}).json() == {"results": []}
    assert vectors.collection(embedder.config).count() == 0


def test_persistent_chroma_after_client_restart(semantic_client):
    client, embedder, vectors, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    reopened = VectorIndex(vectors.path)
    assert reopened.collection(embedder.config).count() == 1
    with TestClient(app) as restarted:
        assert restarted.post("/search/semantic", json={"query": "cats"}).json()["results"][0]["document_id"] == document_id


def test_embedding_compatibility_is_checked(semantic_client):
    client, embedder, vectors, _, _ = semantic_client
    add_indexed(client, "Cats.")
    embedder.config = {**embedder.config, "model": "different-test-model"}
    assert client.post("/search/semantic", json={"query": "cats"}).status_code == 503
    with pytest.raises(IndexUnavailable):
        vectors.collection(embedder.config)


def test_unrelated_routes_do_not_load_embedding_model(semantic_client):
    client, embedder, _, _, _ = semantic_client
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/documents").status_code == 200
    assert upload(client, make_pdf()).status_code == 201
    assert embedder.calls == 0


def test_reindex_repairs_missing_vector_data(semantic_client):
    client, embedder, vectors, _, _ = semantic_client
    document_id, _ = add_indexed(client, "Cats.")
    vectors.collection(embedder.config).delete(where={"document_id": document_id})
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert vectors.collection(embedder.config).count() == 1


def test_real_embedder_configuration_and_lazy_reuse(monkeypatch, tmp_path):
    loads = []
    encodes = []

    class DummyModel:
        def encode(self, texts, **kwargs):
            encodes.append(kwargs)
            return SimpleNamespace(tolist=lambda: [[0.0] * 384 for _ in texts])

    def factory(model_id, **kwargs):
        loads.append((model_id, kwargs))
        return DummyModel()

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    monkeypatch.setenv("RECALLIQ_MODEL_CACHE", str(tmp_path / "models"))
    embedder = LocalEmbedder()
    assert loads == []
    assert len(embedder.encode(["local test"])[0]) == 384
    embedder.encode(["local query"])
    assert len(loads) == 1
    assert loads[0][0] == MODEL_ID
    assert loads[0][1]["revision"] == MODEL_REVISION
    assert loads[0][1]["device"] == "cpu"
    assert loads[0][1]["local_files_only"] is True
    assert encodes[0]["batch_size"] == 32
    assert encodes[0]["normalize_embeddings"] is True


def test_real_embedder_loading_failure_is_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Private cache path in generated failure")

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=fail))
    with pytest.raises(ModelUnavailable) as result:
        LocalEmbedder().encode(["local test"])
    assert "Private cache" not in str(result.value)
