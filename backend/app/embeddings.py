import os
from pathlib import Path
from threading import Lock

from app.database import BACKEND_DIRECTORY


MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Pin weights so cache refreshes cannot silently change the vector space.
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
EMBEDDING_CONFIG = {
    "model": MODEL_ID, "revision": MODEL_REVISION, "dimensions": 384,
    "normalize": True, "precision": "float32", "max_sequence_length": 256,
}


class ModelUnavailable(RuntimeError):
    pass


class LocalEmbedder:
    config = EMBEDDING_CONFIG

    def __init__(self):
        self._model = None
        self._lock = Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            try:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    cache = Path(os.environ.get("RECALLIQ_MODEL_CACHE", "data/models"))
                    if not cache.is_absolute():
                        cache = BACKEND_DIRECTORY / cache
                    self._model = SentenceTransformer(
                        MODEL_ID, revision=MODEL_REVISION, device="cpu", cache_folder=str(cache),
                        trust_remote_code=False,
                        local_files_only=os.environ.get("RECALLIQ_MODELS_OFFLINE", "0") == "1",
                    )
                    self._model.max_seq_length = 256
                return self._model.encode(
                    texts, batch_size=32, normalize_embeddings=True, precision="float32",
                    convert_to_numpy=True, show_progress_bar=False,
                ).tolist()
            except Exception as error:
                raise ModelUnavailable(
                    "Local embedding model unavailable. Check installation, model cache "
                    "and one-time download access."
                ) from error


_embedder = LocalEmbedder()


def get_embedder() -> LocalEmbedder:
    return _embedder
