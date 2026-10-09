from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.schemas import DocumentUploadResponse
from app.uploads import UploadRejected, get_uploads_directory, store_and_extract


router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File(description="One PDF, up to 20 MiB")],
    directory: Annotated[Path, Depends(get_uploads_directory)],
) -> DocumentUploadResponse:
    form = await request.form()
    files = [value for _, value in form.multi_items() if isinstance(value, StarletteUploadFile)]
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="Upload exactly one PDF file.")
    try:
        return await run_in_threadpool(
            store_and_extract, file.file, file.filename or "", directory
        )
    except UploadRejected as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception as error:
        # Never include native errors, document text or local paths in responses.
        raise HTTPException(
            status_code=500, detail="The document could not be processed. Please try again."
        ) from error
