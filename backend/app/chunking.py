from dataclasses import dataclass
from uuid import UUID, uuid5

from app.schemas import ExtractedPage


@dataclass(frozen=True)
class TextChunk:
    chunk_id: str
    document_id: str
    page_number: int
    chunk_order: int
    text: str


def chunk_pages(
    document_id: str, pages: list[ExtractedPage], size: int = 800, overlap: int = 120
) -> list[TextChunk]:
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Chunk size must be positive and overlap smaller than size")
    namespace = UUID(document_id)
    chunks = []
    for page in pages:
        text = page.text.strip()
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                # Prefer a paragraph, sentence or word boundary in the final third.
                lower = start + max(overlap + 1, size * 2 // 3)
                for separator in ("\n\n", ". ", "\n", " "):
                    boundary = text.rfind(separator, lower, end)
                    if boundary >= lower:
                        end = boundary + len(separator)
                        break
            value = text[start:end].strip()
            if value:
                chunk_id = str(uuid5(namespace, f"{page.page_number}:{start}:{end}:{value}"))
                chunks.append(TextChunk(chunk_id, str(namespace), page.page_number, len(chunks), value))
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks
