from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.pdf_extraction import InvalidPDF, extract_pages
from app.schemas import DocumentUploadResponse


MAX_FILE_SIZE = 20 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
UPLOADS_DIRECTORY = Path(__file__).resolve().parents[1] / "uploads"


class UploadRejected(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def get_uploads_directory() -> Path:
    return UPLOADS_DIRECTORY


def store_and_extract(
    stream: BinaryIO, filename: str, directory: Path
) -> DocumentUploadResponse:
    if not filename.lower().endswith(".pdf"):
        raise UploadRejected("Only files with a .pdf extension are supported.")

    first_chunk = stream.read(CHUNK_SIZE)
    if not first_chunk:
        raise UploadRejected("The uploaded file is empty.")
    if not first_chunk.startswith(b"%PDF-"):
        raise UploadRejected("The file does not have a valid PDF signature.")

    document_id = uuid4()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document_id}.pdf"
    created = False
    retained = False
    try:
        # Exclusive creation prevents accidentally replacing another document.
        with path.open("xb") as destination:
            created = True
            size = 0
            chunk = first_chunk
            while chunk:
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise UploadRejected("PDF files must not exceed 20 MiB (20 MB limit).", 413)
                destination.write(chunk)
                chunk = stream.read(CHUNK_SIZE)
        try:
            pages = extract_pages(path)
        except InvalidPDF as error:
            raise UploadRejected(str(error)) from error
        result = DocumentUploadResponse(
            document_id=document_id,
            filename=filename,
            total_pages=len(pages),
            total_characters=sum(page.character_count for page in pages),
            pages=pages,
        )
        retained = True
        return result
    finally:
        if created and not retained:
            path.unlink(missing_ok=True)
