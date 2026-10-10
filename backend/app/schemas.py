from uuid import UUID
from datetime import datetime

from pydantic import BaseModel


class ExtractedPage(BaseModel):
    page_number: int
    text: str
    character_count: int


class DocumentUploadResponse(BaseModel):
    document_id: UUID
    filename: str
    total_pages: int
    total_characters: int
    pages: list[ExtractedPage]


class DocumentMetadata(BaseModel):
    id: UUID
    original_filename: str
    file_size_bytes: int
    total_pages: int
    total_characters: int
    created_at: datetime
