from uuid import UUID

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
