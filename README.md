# RecallIQ

Privacy-Aware Intelligent Knowledge Assistant. A local-first application with
a FastAPI backend, PDF document management and local semantic retrieval.

## Development status and current features

The backend foundation, PDF extraction, SQLite management and semantic search are implemented:

- `GET /` and `GET /health`, Swagger UI and an OpenAPI schema.
- `POST /documents/upload` accepts one PDF up to 20 MiB (20 × 1024 × 1024 bytes).
- Case-insensitive extension, signature, PDF structure and encryption checks.
- Local UUID-based file storage and per-page text extraction with character counts.
- SQLite metadata persistence, document listing, details and recoverable deletion.
- Page-aware overlapping chunks, local Sentence Transformer embeddings and persistent ChromaDB search.
- Automated tests using generated PDFs and isolated temporary directories.

There is no authentication, encryption, LLM answer generation, RAG or agent integration.

## Planned features (not implemented)

- Broader document ingestion and knowledge management.
- Hybrid semantic and keyword retrieval and an encrypted local vault.
- System 1/System 2 routing and local LLM support through Ollama.
- Agentic RAG through LangGraph.
- Privacy guardrails and explicit consent for optional cloud requests.
- Source-grounded answers, evaluation and a React frontend.

## Technology stack and prerequisites

Current stack: Python, FastAPI, Uvicorn, PyMuPDF, python-multipart, built-in sqlite3,
Sentence Transformers, ChromaDB, pytest and httpx. SQLite requires no additional package.
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

## Local semantic search

Upload stores the PDF and metadata; indexing is an explicit separate action.
`POST /documents/{document_id}/index` safely reopens the stored PDF with PyMuPDF.
The chunker splits each page independently into up to 800 characters, with a target
120-character overlap, preferring paragraph, sentence and word boundaries. Blank
pages produce no chunks. UUID5 chunk identifiers are deterministic for the document,
page, offsets and text; chunk order starts at zero, page numbers at one.

Sentence Transformers runs `sentence-transformers/all-MiniLM-L6-v2` locally on CPU,
using normalized float32, 384-dimensional embeddings and batches of 32. Model weights
are pinned to revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`; the same settings
are used for documents and queries. No model is loaded during startup, upload, CRUD,
or an empty search. First indexing may download weights from Hugging Face; document
text is never sent there. Later requests reuse the model in-process and its disk cache.
Model failures return HTTP 503 with a client-safe explanation.

ChromaDB uses local persistent storage, explicit embeddings and cosine distance;
its default embedding function and usage telemetry are disabled. Configuration is
recorded in Chroma collection metadata and SQLite index manifests. A mismatched model
configuration is rejected rather than mixing incompatible vector spaces.
Optional PowerShell settings (paths relative to `backend/`):

```powershell
$env:RECALLIQ_VECTOR_DIRECTORY = "indexes/chroma"
$env:RECALLIQ_MODEL_CACHE = "data/models"
$env:RECALLIQ_CHUNK_SIZE = "800"
$env:RECALLIQ_CHUNK_OVERLAP = "120"
# Use after the pinned model has been downloaded:
$env:RECALLIQ_MODELS_OFFLINE = "1"
```

`RECALLIQ_CHUNK_SIZE` must be positive; overlap must be nonnegative and smaller.
Omit offline mode for the one-time model download. Cache and vector directories are
Git-ignored. No cloud embedding API, LLM or generated answer is involved.

In Swagger, upload a PDF, copy its UUID, call its `/index` endpoint, then call
`POST /search/semantic`. Equivalent PowerShell commands:

```powershell
$documentId = "<UUID returned by upload>"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/documents/$documentId/index"
$searchBody = @{
    query = "What methodology was used in the research?"
    top_k = 5
    document_ids = @($documentId)
} | ConvertTo-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/search/semantic" `
    -ContentType "application/json" -Body $searchBody
```

Indexing returns `document_id`, `pages_processed`, `chunks_indexed` and `status`
(`indexed`). Missing documents return 404, malformed UUIDs 422, missing PDF files
409, invalid PDFs 400, model/configuration failures 503, and other indexing failures 500.
Search accepts a nonblank query of at most 2000 characters and integer `top_k` from
1 to 20. Optional `document_ids` accepts at most 100 UUIDs; empty or omitted means
all indexed documents. Unknown filters and empty indexes return `{"results": []}`.
Matches contain `document_id`, `filename`, `chunk_id`, `page_number`, `chunk_text`
and `distance`, ranked by ascending cosine distance (`1 - cosine similarity`):
lower is more similar, approximately 0 for identical normalized embeddings. Scores
are not confidence probabilities or evidence of correctness. Citations are metadata
from stored chunks; there is no conversational answer generation.

## Indexing and deletion consistency

SQLite `document_indexes` records the active content/configuration generation;
`vector_cleanup` is a durable queue of deleted document UUIDs. Chroma upserts
deterministic generation/chunk IDs before SQLite activates the generation. Partial
new generations remain hidden; reindexing retries the same IDs. A failed changed-content
reindex preserves the previous active generation. Success removes old/partial generations.
A cleanup failure after activation can return 500 even though the new generation is
complete; retrying indexing finishes cleanup without duplicating chunks.
Reindexing unchanged content verifies chunk count, reuses a complete generation and
retries cleanup; missing vector data can be reconstructed by reindexing.

Deletion preserves the staged-file transaction and queues vector removal in the same
SQLite transaction. It then removes document vectors. If vector cleanup fails, the API
returns 500 and leaves the queue entry. The next indexing, search or deletion request
retries queued removals without needing to load an embedding model. Startup recovers
PDF deletion; vector cleanup stays lazy. Searches filter against live SQLite metadata
and active generations before and after querying, so deleted/partial chunks never
become eligible results even while physical vector cleanup is pending.

These operations are not one atomic transaction across all three stores. Use a single
Uvicorn process: a process-local lock serializes indexing/search/deletion. Concurrent
multi-process writers are not supported. Crash-abandoned inactive generations may occupy
disk until safe reindexing or document deletion removes them. Back up SQLite, uploads
and Chroma together, and keep configuration stable between restarts.

## Optional real-model verification

Normal tests use deterministic fake embeddings with real, temporary persistent ChromaDB.
They need no model download or network. To manually verify the real model:

1. Install requirements and run the backend using the commands above.
2. Upload two non-sensitive text PDFs: one about fruit and one about spacecraft.
3. Index both through Swagger; allow the one-time download on the first request.
4. Search for a paraphrase about fruit and check its source filename, page and text.
5. Filter to the spacecraft UUID and verify no fruit-document result is returned.
6. Stop/restart with `RECALLIQ_MODELS_OFFLINE=1`; search again without reindexing.
7. Delete one document and verify it disappears from both listing and search.

This procedure is optional and is separate from the fake-embedding test results.

## Known limitations

- PDFs, SQLite metadata, chunk text, embeddings and vector indexes are stored locally
  **without encryption**. Only optional model-weight downloads use the network;
  extraction, embedding inference and vector search are local. Derived text/vector data
  will need protection alongside PDFs in the future encrypted vault.
- Image-only/scanned PDFs need OCR, which is not supported. In mixed PDFs, only
  embedded text is extracted; scanned pages may have empty text.
- Text layout and reading order depend on the PDF; extraction does not reconstruct
  document structure or verify the accuracy of retrieved text.
- Full extracted text is returned temporarily by upload for development. Metadata
  endpoints do not return it. Search returns matching chunk text; there is no LLM
  generation, keyword/hybrid retrieval or upload deduplication yet.
- Character-based chunks may exceed the model's 256-token window and be truncated,
  especially for non-English text. Retrieval is approximate and has no relevance cutoff.
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
- `backend/app/chunking.py`: deterministic page-aware text splitting.
- `backend/app/embeddings.py`: lazy local model loading and batched embeddings.
- `backend/app/vectors.py`: persistent Chroma storage and retrieval.
- `backend/app/semantic.py`: indexing manifests, filtering and cleanup recovery.
- `backend/app/search.py`: validated semantic retrieval endpoint.
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
