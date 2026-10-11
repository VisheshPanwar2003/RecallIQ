from pathlib import Path
from threading import Lock

import pymupdf

from app.schemas import ExtractedPage
from app.storage import check_private_path


class InvalidPDF(ValueError):
    """A client-safe explanation of why a PDF cannot be processed."""


# Serialize native PDF work, and avoid native diagnostics exposing file data.
_pdf_lock = Lock()
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


def extract_pages(path: Path) -> list[ExtractedPage]:
    path = check_private_path(path)
    with _pdf_lock:
        try:
            # Open capped upload bytes so a native open failure cannot keep a
            # Windows file handle alive through an exception traceback.
            with pymupdf.open(stream=path.read_bytes(), filetype="pdf") as document:
                if document.needs_pass or (document.metadata or {}).get("encryption"):
                    raise InvalidPDF("Password-protected PDFs are not supported.")
                if not document.is_pdf or document.is_repaired:
                    raise InvalidPDF("The PDF is corrupt or invalid.")
                if document.page_count == 0:
                    raise InvalidPDF("The PDF has no pages.")
                pages = []
                for number, page in enumerate(document, start=1):
                    text = page.get_text("text")
                    pages.append(ExtractedPage(
                        page_number=number, text=text, character_count=len(text)
                    ))
                if not any(page.text.strip() for page in pages):
                    raise InvalidPDF(
                        "The PDF has no extractable text. Image-only or scanned PDFs "
                        "require OCR, which is not supported yet."
                    )
                return pages
        except InvalidPDF:
            raise
        except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
            raise InvalidPDF("The PDF is corrupt or cannot be read.") from error
