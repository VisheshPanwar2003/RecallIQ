from io import BytesIO
import re
from uuid import UUID

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import uploads
from app.uploads import CHUNK_SIZE, MAX_FILE_SIZE, get_uploads_directory


def make_pdf(texts=("RecallIQ example text.",), *, encrypted=False, image_only=False):
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page()
            if image_only:
                image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16))
                image.clear_with(200)
                page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=image)
            elif text:
                page.insert_text((72, 72), text)
        if encrypted:
            return document.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="generated-test-owner",
                user_pw="generated-test-user",
            )
        return document.tobytes()


def make_sized_pdf(size):
    # Add an unreferenced comment before the cross-reference table and update
    # startxref. This stays valid without making the parser read huge text fields.
    content = make_pdf()
    match = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", content)
    assert match is not None
    offset = int(match.group(1))
    padding = size - len(content)
    for _ in range(10):
        new_offset = str(offset + padding).encode("ascii")
        tail = content[offset:match.start(1)] + new_offset + content[match.end(1):]
        total = offset + padding + len(tail)
        if total == size:
            return content[:offset] + b"\n%" + b"x" * (padding - 3) + b"\n" + tail
        padding += size - total
    raise AssertionError("Could not generate an exact-size PDF")


@pytest.fixture
def isolated_client(tmp_path):
    directory = tmp_path / "uploads"
    app.dependency_overrides[get_uploads_directory] = lambda: directory
    try:
        with TestClient(app) as client:
            yield client, directory
    finally:
        app.dependency_overrides.pop(get_uploads_directory, None)


def upload(client, content, filename="sample.pdf"):
    return client.post(
        "/documents/upload", files={"file": (filename, content, "application/pdf")}
    )


def assert_no_files(directory):
    assert not directory.exists() or list(directory.iterdir()) == []


def test_successful_upload(isolated_client):
    client, directory = isolated_client
    content = make_pdf()
    response = upload(client, content)
    assert response.status_code == 201
    result = response.json()
    UUID(result["document_id"])
    assert result["filename"] == "sample.pdf"
    assert result["total_pages"] == 1
    assert result["pages"] == [{
        "page_number": 1,
        "text": "RecallIQ example text.\n",
        "character_count": len("RecallIQ example text.\n"),
    }]
    assert result["total_characters"] == len("RecallIQ example text.\n")
    assert set(result) == {"document_id", "filename", "total_pages", "total_characters", "pages"}
    assert (directory / f'{result["document_id"]}.pdf').read_bytes() == content


def test_multipage_numbering_and_character_counts(isolated_client):
    client, _ = isolated_client
    response = upload(client, make_pdf(("First page.", "", "Third page.")))
    assert response.status_code == 201
    result = response.json()
    assert result["total_pages"] == 3
    assert [page["page_number"] for page in result["pages"]] == [1, 2, 3]
    assert [page["text"] for page in result["pages"]] == ["First page.\n", "", "Third page.\n"]
    for page in result["pages"]:
        assert page["character_count"] == len(page["text"])
    assert result["total_characters"] == sum(len(page["text"]) for page in result["pages"])


@pytest.mark.parametrize("filename", ["sample.PDF", "../../outside.pdf", r"C:\private\sample.PdF"])
def test_extension_case_and_safe_storage_names(isolated_client, filename):
    client, directory = isolated_client
    response = upload(client, make_pdf(), filename)
    assert response.status_code == 201
    result = response.json()
    # The multipart parser strips Windows directory prefixes before routing.
    expected = filename.rsplit("\\", 1)[-1]
    assert result["filename"] == expected
    assert [path.name for path in directory.iterdir()] == [f'{result["document_id"]}.pdf']
    assert not (directory.parent / "outside.pdf").exists()


@pytest.mark.parametrize(("content", "filename", "message"), [
    (b"", "empty.pdf", "empty"),
    (b"%PDF-1.7\nnot a document", "file.txt", ".pdf extension"),
    (b"plain text", "fake.pdf", "signature"),
    (b"%PDF-1.7\nnot a document", "corrupt.pdf", "corrupt"),
])
def test_rejected_files_leave_no_uploads(isolated_client, content, filename, message):
    client, directory = isolated_client
    response = upload(client, content, filename)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert_no_files(directory)


def test_password_protected_pdf(isolated_client):
    client, directory = isolated_client
    response = upload(client, make_pdf(encrypted=True))
    assert response.status_code == 400
    assert "Password-protected" in response.json()["detail"]
    assert_no_files(directory)


def test_owner_password_only_pdf(isolated_client):
    client, directory = isolated_client
    with pymupdf.open(stream=make_pdf(), filetype="pdf") as document:
        content = document.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="generated-owner", user_pw=""
        )
    response = upload(client, content)
    assert response.status_code == 400
    assert "Password-protected" in response.json()["detail"]
    assert_no_files(directory)


@pytest.mark.parametrize("image_only", [True, False])
def test_image_only_and_blank_pdf(isolated_client, image_only):
    client, directory = isolated_client
    response = upload(client, make_pdf(("",), image_only=image_only))
    assert response.status_code == 400
    assert "OCR" in response.json()["detail"]
    assert_no_files(directory)


def test_over_size_limit_and_cleanup(isolated_client):
    client, directory = isolated_client
    content = make_pdf()
    content += b" " * (MAX_FILE_SIZE + 1 - len(content))
    response = upload(client, content)
    assert response.status_code == 413
    assert_no_files(directory)


def test_exact_size_limit(isolated_client):
    client, directory = isolated_client
    content = make_sized_pdf(MAX_FILE_SIZE)
    response = upload(client, content)
    assert response.status_code == 201
    assert next(directory.iterdir()).stat().st_size == MAX_FILE_SIZE


def test_extraction_failure_cleanup_and_safe_error(isolated_client, monkeypatch):
    client, directory = isolated_client

    def fail(path):
        raise RuntimeError(f"Internal extraction failure at {path}")

    monkeypatch.setattr(uploads, "extract_pages", fail)
    response = upload(client, make_pdf())
    assert response.status_code == 500
    assert response.json() == {"detail": "The document could not be processed. Please try again."}
    assert str(directory) not in response.text
    assert_no_files(directory)


def test_bounded_reads_and_interrupted_upload_cleanup(tmp_path):
    class InterruptedStream(BytesIO):
        reads = 0

        def read(self, size=-1):
            assert size == CHUNK_SIZE
            self.reads += 1
            if self.reads == 3:
                raise OSError("Simulated interrupted upload")
            return super().read(size)

    content = make_pdf() + b" " * (CHUNK_SIZE * 3)
    directory = tmp_path / "uploads"
    with pytest.raises(OSError, match="interrupted"):
        uploads.store_and_extract(InterruptedStream(content), "sample.pdf", directory)
    assert_no_files(directory)


def test_missing_file(isolated_client):
    client, directory = isolated_client
    assert client.post("/documents/upload").status_code == 422
    assert_no_files(directory)


def test_multiple_files_rejected(isolated_client):
    client, directory = isolated_client
    response = client.post("/documents/upload", files=[
        ("file", ("one.pdf", make_pdf(), "application/pdf")),
        ("file", ("two.pdf", make_pdf(), "application/pdf")),
    ])
    assert response.status_code == 400
    assert_no_files(directory)


def test_upload_schema_is_documented(isolated_client):
    client, _ = isolated_client
    schema = client.get("/openapi.json").json()
    endpoint = schema["paths"]["/documents/upload"]["post"]
    assert "multipart/form-data" in endpoint["requestBody"]["content"]
    assert "201" in endpoint["responses"]


def test_repaired_pdf_rejected(isolated_client):
    client, directory = isolated_client
    content = re.sub(rb"startxref\s+\d+", b"startxref\n0", make_pdf())
    response = upload(client, content)
    assert response.status_code == 400
    assert "corrupt" in response.json()["detail"]
    assert_no_files(directory)


def test_zero_page_pdf_rejected(isolated_client):
    client, directory = isolated_client
    content = b"%PDF-1.7\n"
    offsets = [0]
    for number, value in enumerate([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",
    ], start=1):
        offsets.append(len(content))
        content += f"{number} 0 obj\n".encode() + value + b"\nendobj\n"
    startxref = len(content)
    content += b"xref\n0 3\n0000000000 65535 f \n"
    content += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    content += f"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode()
    response = upload(client, content)
    assert response.status_code == 400
    assert "no pages" in response.json()["detail"]
    assert_no_files(directory)


def test_repeated_uploads_get_distinct_ids(isolated_client):
    client, directory = isolated_client
    content = make_pdf()
    first = upload(client, content)
    second = upload(client, content)
    assert first.status_code == second.status_code == 201
    assert first.json()["document_id"] != second.json()["document_id"]
    assert len(list(directory.iterdir())) == 2


def test_mixed_text_and_image_only_pages(isolated_client):
    client, _ = isolated_client
    with pymupdf.open(stream=make_pdf(), filetype="pdf") as document:
        with pymupdf.open(stream=make_pdf(image_only=True), filetype="pdf") as scanned:
            document.insert_pdf(scanned)
        content = document.tobytes()
    response = upload(client, content)
    assert response.status_code == 201
    assert response.json()["total_pages"] == 2
    assert response.json()["pages"][1] == {"page_number": 2, "text": "", "character_count": 0}
