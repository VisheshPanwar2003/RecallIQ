from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.database import DocumentStore, get_document_store
from app.embeddings import LocalEmbedder, ModelUnavailable, get_embedder
from app.schemas import SemanticSearchRequest, SemanticSearchResponse
from app.semantic import search_documents
from app.vectors import VectorIndex, IndexUnavailable, get_vector_index


router = APIRouter(prefix="/search", tags=["search"])


@router.post("/semantic", response_model=SemanticSearchResponse)
def semantic_search(
    request: SemanticSearchRequest,
    store: Annotated[DocumentStore, Depends(get_document_store)],
    vectors: Annotated[VectorIndex, Depends(get_vector_index)],
    embedder: Annotated[LocalEmbedder, Depends(get_embedder)],
):
    try:
        return search_documents(store, vectors, embedder, request)
    except (ModelUnavailable, IndexUnavailable) as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        raise HTTPException(500, "Semantic search is unavailable. Please retry.") from error
