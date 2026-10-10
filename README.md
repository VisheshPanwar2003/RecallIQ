# RecallIQ

Privacy-Aware Intelligent Knowledge Assistant. A local-first application with
a FastAPI backend and local PDF upload and text extraction.

## Development status and current features

The backend foundation and PDF Upload & Text Extraction are implemented:

- `GET /` and `GET /health`, Swagger UI and an OpenAPI schema.
- `POST /documents/upload` accepts one PDF up to 20 MiB (20 × 1024 × 1024 bytes).
- Case-insensitive extension, signature, PDF structure and encryption checks.
- Local UUID-based file storage and per-page text extraction with character counts.
- Automated tests using generated PDFs and isolated temporary directories.

There is no database, authentication, encryption of stored uploads or AI integration.

## Planned features (not implemented)

- Broader document ingestion and knowledge management.
- Hybrid semantic and keyword retrieval and an encrypted local vault.
- System 1/System 2 routing and local LLM support through Ollama.
- Agentic RAG through LangGraph.
- Privacy guardrails and explicit consent for optional cloud requests.
- Source-grounded answers, evaluation and a React frontend.

## Technology stack and prerequisites

Current stack: Python, FastAPI, Uvicorn, PyMuPDF, python-multipart, pytest and httpx.
PyMuPDF opens PDFs and extracts text; python-multipart parses upload form data.
All direct dependencies and their versions are listed in `backend/requirements.txt`.
Install Python 3.11 or newer and Git. Commands below use Windows PowerShell.
For a new checkout, clone the repository and enter its root:

```powershell
git clone https://github.com/VisheshPanwar2003/RecallIQ.git
Set-Location RecallIQ
```

The setup examples use `D:\RecallIQ`; substitute your checkout path if different.

## Setup (Windows PowerShell, Python 3.11+)

From the project root (`D:\RecallIQ`):

```powershell
Set-Location D:\RecallIQ
py -3.11 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

Use an installed newer Python version in place of `-3.11` if needed.
Calling the virtual environment's Python directly requires no activation.
No environment variables are required; `.env.example` documents this.

## Run

```powershell
Set-Location D:\RecallIQ\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger UI. Stop the server
with Ctrl+C. `--reload` restarts it when source files change.

In another PowerShell terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
Invoke-RestMethod http://127.0.0.1:8000/health
```

`GET /` returns `{"message":"Welcome to RecallIQ API"}`.
`GET /health` returns `{"status":"ok","service":"recalliq-backend"}`.

## Upload a PDF

In http://127.0.0.1:8000/docs, expand `POST /documents/upload`, click
**Try it out**, choose one non-sensitive PDF in the `file` field, and click
**Execute**. Successful uploads return HTTP 201. For a command-line upload:

```powershell
curl.exe -X POST http://127.0.0.1:8000/documents/upload -F "file=@C:\path\sample.pdf"
```

Example for a PDF containing `Hello RecallIQ!` followed by an extracted newline:

```json
{
  "document_id": "8f9b9933-8cfe-4c3e-9168-a90789f2a123",
  "filename": "sample.pdf",
  "total_pages": 1,
  "total_characters": 16,
  "pages": [
    {"page_number": 1, "text": "Hello RecallIQ!\n", "character_count": 16}
  ]
}
```

Counts include whitespace and newlines in the extracted text. Blank pages are
included when other pages contain text. Each successful request gets a new UUID.
PDFs are saved to `backend/uploads/<uuid>.pdf`, independent of the working
directory. Original filenames are returned as metadata and never used as paths.
The uploads directory is created on demand and excluded from Git.

Errors use a JSON `detail` message: HTTP 400 for invalid, empty, corrupt,
encrypted or textless PDFs and multiple files; HTTP 413 for oversized files;
HTTP 422 for a missing file; HTTP 500 for processing or storage failures.
Files created during failed processing are removed.

## Known limitations

- PDFs are stored locally **without encryption**. Processing makes no cloud requests.
- Image-only/scanned PDFs need OCR, which is not supported. In mixed PDFs, only
  embedded text is extracted; scanned pages may have empty text.
- Text layout and reading order depend on the PDF. No semantic processing is done.
- Full extracted text is returned temporarily for development. There is no database,
  indexing, retrieval, deduplication, document listing or deletion endpoint yet.
- The limit is enforced while copying from FastAPI's spooled upload in 64 KiB chunks.
  Multipart parsing happens first; this is not an incoming request-body quota.
- There are no page-count or extracted-output limits yet; keep this development
  server on localhost and use trusted, non-sensitive sample PDFs.
- PDFs requiring structural repair are rejected as corrupt rather than repaired.
- The existing httpx TestClient integration emits a dependency deprecation warning.

## Test

```powershell
Set-Location D:\RecallIQ\backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover existing endpoints, documentation, PDF extraction and rejection cases,
size boundaries and cleanup failures. PDFs are generated in memory, and storage
is redirected to pytest temporary directories; real uploads are never used.

## Files and concepts

- `backend/app/__init__.py`: marks `app` as a Python package.
- `backend/app/main.py`: creates the FastAPI application and registers routes.
  `@app.get(...)` is similar to Express `app.get(path, handler)`; returning
  a dictionary serializes it to JSON, like `res.json(...)`.
- `backend/app/documents.py`: upload route, dependency injection and safe HTTP errors.
- `backend/app/uploads.py`: bounded copying, UUID storage and failure cleanup.
- `backend/app/pdf_extraction.py`: local PDF validation and text extraction.
- `backend/app/schemas.py`: Pydantic response models.
- `backend/tests/`: health, documentation and PDF tests using FastAPI's TestClient.
- `backend/requirements.txt`: direct dependencies with pinned versions;
  pip also installs their required dependencies.
- `backend/.env.example`: safe configuration example, with no secrets.
- `.gitignore`: excludes environments, secrets and generated local data.

`def` defines a function, and indentation defines its body. `dict[str, str]`
is a return type hint, similar to a TypeScript string-to-string record.
These routes use ordinary functions because they do no asynchronous I/O.
Uvicorn loads `app.main:app`: the `app` object from the `app.main` module.
