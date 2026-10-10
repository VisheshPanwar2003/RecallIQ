from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.schemas import DocumentMetadata, DocumentUploadResponse, IndexingResponse
from app.database import DocumentStore, get_document_store
from app.document_management import DocumentNotFound
from app.embeddings import LocalEmbedder, ModelUnavailable, get_embedder
from app.vectors import VectorIndex, IndexUnavailable, get_vector_index
from app.semantic import delete_indexed_document, index_document
from app.pdf_extraction import InvalidPDF
from app.uploads import UploadRejected, get_uploads_directory, store_and_extract


router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File(description="One PDF, up to 20 MiB")],
    directory: Annotated[Path, Depends(get_uploads_directory)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
) -> DocumentUploadResponse:
    form = await request.form()
    files = [value for _, value in form.multi_items() if isinstance(value, StarletteUploadFile)]
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="Upload exactly one PDF file.")
    try:
        return await run_in_threadpool(
            store_and_extract, file.file, file.filename or "", directory, store
        )
    except UploadRejected as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception as error:
        # Never include native errors, document text or local paths in responses.
        raise HTTPException(
            status_code=500, detail="The document could not be processed. Please try again."
        ) from error


@router.get("", response_model=list[DocumentMetadata])
def list_documents(store: Annotated[DocumentStore, Depends(get_document_store)]):
    try:
        return store.list()
    except Exception as error:
        raise HTTPException(500, "Document metadata could not be read.") from error


@router.get("/{document_id}", response_model=DocumentMetadata)
def get_document(document_id: UUID, store: Annotated[DocumentStore, Depends(get_document_store)]):
    try:
        document = store.get(str(document_id))
    except Exception as error:
        raise HTTPException(500, "Document metadata could not be read.") from error
    if document is None:
        raise HTTPException(404, "Document not found.")
    return document


@router.delete("/{document_id}", status_code=204)
def remove_document(
    document_id: UUID,
    store: Annotated[DocumentStore, Depends(get_document_store)],
    directory: Annotated[Path, Depends(get_uploads_directory)],
    vectors: Annotated[VectorIndex, Depends(get_vector_index)],
    embedder: Annotated[LocalEmbedder, Depends(get_embedder)],
):
    try:
        delete_indexed_document(store, directory, vectors, embedder, str(document_id))
    except DocumentNotFound as error:
        raise HTTPException(404, "Document not found.") from error
    except Exception as error:
        raise HTTPException(500, "Document deletion failed. Recovery may be required.") from error
    return Response(status_code=204)


@router.post("/{document_id}/index", response_model=IndexingResponse)
def index_stored_document(
    document_id: UUID,
    store: Annotated[DocumentStore, Depends(get_document_store)],
    directory: Annotated[Path, Depends(get_uploads_directory)],
    vectors: Annotated[VectorIndex, Depends(get_vector_index)],
    embedder: Annotated[LocalEmbedder, Depends(get_embedder)],
):
    try:
        return index_document(store, directory, vectors, embedder, str(document_id))
    except DocumentNotFound as error:
        raise HTTPException(404, "Document not found.") from error
    except FileNotFoundError as error:
        raise HTTPException(409, "Stored PDF is missing. Re-upload the document.") from error
    except InvalidPDF as error:
        raise HTTPException(400, str(error)) from error
    except (ModelUnavailable, IndexUnavailable) as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        raise HTTPException(500, "Document indexing failed. Safe reindexing can be retried.") from error
