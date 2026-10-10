# RecallIQ

Privacy-Aware Intelligent Knowledge Assistant. A local-first application with
a FastAPI backend and local PDF upload and text extraction.

## Development status and current features

The backend foundation, PDF extraction and SQLite document management are implemented:

- `GET /` and `GET /health`, Swagger UI and an OpenAPI schema.
- `POST /documents/upload` accepts one PDF up to 20 MiB (20 × 1024 × 1024 bytes).
- Case-insensitive extension, signature, PDF structure and encryption checks.
- Local UUID-based file storage and per-page text extraction with character counts.
- SQLite metadata persistence, document listing, details and recoverable deletion.
- Automated tests using generated PDFs and isolated temporary directories.

There is no authentication, encryption of stored uploads or AI integration.

## Planned features (not implemented)

- Broader document ingestion and knowledge management.
- Hybrid semantic and keyword retrieval and an encrypted local vault.
- System 1/System 2 routing and local LLM support through Ollama.
- Agentic RAG through LangGraph.
- Privacy guardrails and explicit consent for optional cloud requests.
- Source-grounded answers, evaluation and a React frontend.

## Technology stack and prerequisites

Current stack: Python, FastAPI, Uvicorn, PyMuPDF, python-multipart, built-in sqlite3,
pytest and httpx. SQLite requires no additional package.
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
Paths have defaults; optional overrides are documented in `.env.example`.
The application does not automatically load `.env` files.

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

## SQLite document management

On startup, the application creates the database directory and `documents` table
if needed and recovers interrupted staged deletions. Importing modules creates no
database files. An explicit `initialize_database(path)` function supports tooling;
request stores also initialize lazily for clients that do not run the lifespan.
Each connection enables foreign keys, commits or rolls back its transaction,
and closes when finished. SQL values use parameterized queries.

The table stores `id` (UUID primary key), `original_filename`, `stored_filename`
(unique UUID filename), `file_size_bytes`, `total_pages`, `total_characters`, and
`created_at` (UTC). Required fields and nonnegative integer counts are constrained.
PDF bytes and extracted page text are not stored in SQLite. Upload responses remain
unchanged; metadata insertion failure removes the newly created PDF.

Default locations are `backend/data/recalliq.sqlite3` and `backend/uploads/`.
Relative overrides are resolved against `backend/`, regardless of the shell directory.
To override paths in PowerShell before starting the backend:

```powershell
$env:RECALLIQ_DATABASE_PATH = "data/custom.sqlite3"
$env:RECALLIQ_UPLOADS_DIRECTORY = "uploads"
```

Absolute paths are also supported. Keep the database and its uploads directory
together when backing up or changing configuration. Tests override both paths
independently and use isolated temporary databases and files.

| Endpoint | Behavior |
| --- | --- |
| `POST /documents/upload` | Validate, store, extract, persist metadata; HTTP 201 |
| `GET /documents` | Metadata list, newest first; UUID descending breaks timestamp ties |
| `GET /documents/{document_id}` | Metadata only; HTTP 404 if unknown, 422 for invalid UUID |
| `DELETE /documents/{document_id}` | Remove metadata and file; HTTP 204, 404 if unknown |

Metadata responses include `id`, `original_filename`, `file_size_bytes`,
`total_pages`, `total_characters`, and `created_at`; storage paths, stored filenames
and extracted text are excluded. Test all endpoints in Swagger, or use PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/documents
Invoke-RestMethod "http://127.0.0.1:8000/documents/<document-id>"
Invoke-RestMethod -Method Delete "http://127.0.0.1:8000/documents/<document-id>"
```

Deletion holds an SQLite write transaction, verifies the stored UUID filename stays
inside the uploads directory and rejects symlinks. It renames the PDF to
`<uuid>.pdf.deleting`, deletes the metadata and commits, then removes the staged file.
A missing original file is treated as already removed; metadata deletion still returns
204. A rename or database failure rolls back and restores the original file. If final
file removal fails, HTTP 500 reports failure: metadata is already removed and the staged
file remains for startup recovery. Recovery restores staged files with existing records
and removes staged files whose records were deleted. Recovery failures stop startup
rather than silently abandoning data; fix file permissions/locks and restart.

## Known limitations

- PDFs are stored locally **without encryption**. Processing makes no cloud requests.
- Image-only/scanned PDFs need OCR, which is not supported. In mixed PDFs, only
  embedded text is extracted; scanned pages may have empty text.
- Text layout and reading order depend on the PDF. No semantic processing is done.
- Full extracted text is returned temporarily by upload for development. Metadata
  endpoints do not return it. There is no indexing, retrieval or deduplication yet.
- SQLite and filesystem changes cannot be fully atomic. Staged deletion handles normal
  rollback and restart recovery; a process crash during upload can leave an unindexed
  file. Existing PDFs from before SQLite support are not automatically imported.
- There is no migration framework, pagination or concurrent backup mechanism yet.
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
- `backend/app/database.py`: SQLite schema, connections and metadata persistence.
- `backend/app/document_management.py`: safe staged deletion and restart recovery.
- `backend/tests/`: health, documentation and PDF tests using FastAPI's TestClient.
- `backend/requirements.txt`: direct dependencies with pinned versions;
  pip also installs their required dependencies.
- `backend/.env.example`: safe configuration example, with no secrets.
- `.gitignore`: excludes environments, secrets and generated local data.

`def` defines a function, and indentation defines its body. `dict[str, str]`
is a return type hint, similar to a TypeScript string-to-string record.
Simple routes use ordinary functions; upload is asynchronous and delegates blocking
file/PDF/database work to a worker thread.
Uvicorn loads `app.main:app`: the `app` object from the `app.main` module.
