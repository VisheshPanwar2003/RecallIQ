from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


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


class IndexingResponse(BaseModel):
    document_id: UUID
    pages_processed: int
    chunks_indexed: int
    status: str


class SemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Query must contain non-whitespace text")
        return value


class SemanticMatch(BaseModel):
    document_id: UUID
    filename: str
    chunk_id: UUID
    page_number: int
    chunk_text: str
    distance: float


class SemanticSearchResponse(BaseModel):
    results: list[SemanticMatch]
