# RecallIQ

Privacy-Aware Intelligent Knowledge Assistant. A local-first application with
a FastAPI backend, PDF document management and local semantic, keyword and hybrid retrieval.

## Development status and current features

The backend foundation, PDF extraction, SQLite management and all three retrieval modes are implemented:

- `GET /` and `GET /health`, Swagger UI and an OpenAPI schema.
- `POST /documents/upload` accepts one PDF up to 20 MiB (20 × 1024 × 1024 bytes).
- Case-insensitive extension, signature, PDF structure and encryption checks.
- Local UUID-based file storage and per-page text extraction with character counts.
- SQLite metadata persistence, document listing, details and recoverable deletion.
- Page-aware overlapping chunks, local Sentence Transformer embeddings and persistent ChromaDB search.
- SQLite FTS5 keyword search with BM25 and Reciprocal Rank Fusion (RRF) hybrid search.
- Automated tests using generated PDFs and isolated temporary directories.
- Centralized private-storage configuration, early locked-vault guards and readiness status.

There is no authentication, encryption, LLM answer generation, RAG or agent integration.

## Planned features (not implemented)

- Broader document ingestion and knowledge management.
- An encrypted local vault.
- System 1/System 2 routing and local LLM support through Ollama.
- Agentic RAG through LangGraph.
- Privacy guardrails and explicit consent for optional cloud requests.
- Source-grounded answers, evaluation and a React frontend.

## Technology stack and prerequisites

Current stack: Python, FastAPI, Uvicorn, PyMuPDF, python-multipart, built-in sqlite3,
Sentence Transformers, ChromaDB, pytest and httpx. SQLite requires no additional package.
PyMuPDF opens PDFs and extracts text; python-multipart parses upload form data.
All direct dependencies and their versions are listed in `backend/requirements.txt`.
Install Python 3.11 or newer with SQLite FTS5 support, and Git. Commands below use Windows PowerShell.
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
.\.venv\Scripts\python.exe -m app.run --reload
```

Open http://127.0.0.1:8000/docs for interactive Swagger UI. Stop the server
with Ctrl+C. `--reload` restarts it when source files change.
The launcher configures process temporary storage before importing Uvicorn or the
application. Direct `uvicorn app.main:app` still runs, but bypasses that early process
configuration; prefer the launcher for the storage policy described below.

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
PDF bytes are not stored in SQLite; indexing stores searchable chunk text there. Upload responses remain
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
`keyword_indexes` records the matching keyword generation and expected chunk count.
`vector_cleanup` is a durable queue of deleted document UUIDs. Chroma upserts
deterministic generation/chunk IDs before SQLite writes chunks, updates FTS through
triggers and activates both manifests in one SQLite transaction. The vector ID is
`<generation>:<chunk_id>`; FTS resolves the same logical chunk UUID through its rowid.
Partial
new generations remain hidden; reindexing retries the same IDs. A failed changed-content
reindex preserves the previous active generation. Success removes old/partial generations.
A cleanup failure after activation can return 500 even though the new generation is
complete; retrying indexing finishes cleanup without duplicating chunks.
Reindexing verifies expected vector IDs, reuses a complete generation and rewrites
keyword rows transactionally, then retries cleanup. This repairs missing keyword
data without re-embedding, including after a failed SQLite commit. Missing vector
data is reconstructed by reindexing; its old matching manifests are first hidden.
Old semantic-only indexes remain usable through semantic search after migration,
but require explicit reindexing before keyword or hybrid search can use them.
Hybrid checks matching manifests, keyword row counts and vector counts; incomplete
documents are excluded. Keyword search works without loading an embedding model.

Deletion preserves the staged-file transaction, cascades removal of chunk and keyword
manifest rows, updates FTS through deletion triggers and queues vector removal in the same
SQLite transaction. A keyword cleanup failure rolls back metadata and restores the PDF;
fix the underlying storage issue and retry deletion. It then removes document vectors. If vector cleanup fails, the API
returns 500 and leaves the queue entry. The next indexing, semantic/hybrid search or deletion request
retries queued removals without needing to load an embedding model. Startup recovers
PDF deletion; vector cleanup stays lazy. Searches filter against live SQLite metadata
and active generations before and after querying, so deleted/partial chunks never
become eligible results even while physical vector cleanup is pending.

These operations are not one atomic transaction across all three stores. Use a single
Uvicorn process: a process-local lock serializes indexing/search/deletion. Concurrent
multi-process writers are not supported. Crash-abandoned inactive generations may occupy
disk until safe reindexing or document deletion removes them. Back up SQLite, uploads
and Chroma together, and keep configuration stable between restarts.

## Keyword and hybrid search

`POST /search/keyword` matches literal words and phrases with SQLite FTS5.
`POST /search/semantic` matches embedding similarity and keeps its existing response.
`POST /search/hybrid` combines the two ranked candidate lists. All accept the same
nonblank query (at most 2000 characters), strict integer `top_k` (1..20), and optional
`document_ids` (at most 100 UUIDs; omitted/empty means all eligible documents).
Unknown document filters and empty indexes return `{"results": []}`.

Example keyword request:

```json
{"query": "ResNet-50", "top_k": 5, "document_ids": []}
```

Keyword response example (IDs/text/scores are illustrative):

```json
{
  "results": [{
    "document_id": "70ca7e53-3448-4162-952c-d4d14a1d344e",
    "filename": "residual_network_sample.pdf",
    "chunk_id": "7f904f13-fd5e-55fe-ba8c-693e0a77be56",
    "page_number": 2,
    "chunk_text": "ResNet-50 uses residual skip connections.",
    "bm25_score": -0.4377
  }]
}
```

BM25 is ordered ascending: SQLite's negative scores make lower values better.
Scores depend on corpus term statistics and document length, even when filtering
to selected documents. They are not confidence probabilities and should not be
compared directly with cosine distances. Chunk UUID breaks equal-score ties.
See the [SQLite FTS5 documentation](https://www.sqlite.org/fts5.html) for tokenizer,
external-content table and BM25 details.

The tokenizer is `unicode61 remove_diacritics 2`: case-insensitive with Latin
diacritic normalization and punctuation separators. `ResNet-50` and `resnet_50`
become the adjacent-token phrase `resnet 50`; acronyms such as NASA work as words.
Whitespace-separated terms are combined with OR; double quotes require adjacent
tokens (`"skip connections"`). Query values are SQL-bound parameters. A small literal
query builder strips FTS operators and quotes each token group, so `*`, column
selectors, SQL fragments, unmatched quotes and punctuation cannot execute SQL or
FTS syntax. Punctuation-only queries produce no lexical matches. Operators such as
AND/OR are treated as words; advanced FTS expressions are intentionally unsupported.
There is no stemming, prefix matching, stopword removal, synonym expansion or fuzzy
matching: `rocket` does not match `rockets`. The alphanumeric query splitter and
Unicode61 tokenizer are not language-specific word segmenters; combining marks,
symbol-heavy identifiers such as C++, and scripts without spaces can lose distinctions
or match poorly. Quote a multiword phrase when adjacency is important.

Hybrid example in PowerShell:

```powershell
$body = @{
    query = "How does ResNet-50 solve vanishing gradients?"
    top_k = 5
    document_ids = @($documentId)
} | ConvertTo-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/search/hybrid" `
    -ContentType "application/json" -Body $body
```

Hybrid matches use the same source fields, with `rrf_score`, `keyword_rank` and
`semantic_rank` instead of BM25/distance. Ranks start at 1; an absent retriever's
rank is null. Each retriever contributes `1 / (60 + rank)`, summed by chunk UUID.
For a chunk ranked first in both lists, the score is `2/61`, about `0.032787`:

```json
{
  "results": [{
    "document_id": "70ca7e53-3448-4162-952c-d4d14a1d344e",
    "filename": "residual_network_sample.pdf",
    "chunk_id": "7f904f13-fd5e-55fe-ba8c-693e0a77be56",
    "page_number": 2,
    "chunk_text": "ResNet-50 uses residual skip connections.",
    "rrf_score": 0.03278688524590164,
    "keyword_rank": 1,
    "semantic_rank": 1
  }]
}
```

Higher RRF scores rank first; chunk UUID breaks ties. RRF is a ranking score,
not a probability or cosine similarity. A retriever with no hits contributes nothing;
both empty yields no results. Retrieval failures return safe errors rather than
silently disguising an unavailable model/store as an empty list. There is no LLM reranker.
`RECALLIQ_SEARCH_CANDIDATES` controls candidates per retriever (default 50, allowed
1..200, raised to at least `top_k`). Larger pools improve fusion coverage at extra cost.
Semantic nearest-neighbor search has no relevance threshold, so it can return weak
matches even when the keyword list is empty. This is retrieval, not answer generation.

### Additive SQLite migration

Startup/lazy initialization checks FTS5 with a temporary virtual table. Unsupported
builds fail with a clear message; use a Python distribution compiled with FTS5.
No dependency or database server is added. `schema_migrations` records additive
migration version 1. A write transaction creates `chunks`, `keyword_indexes`, the
external-content `chunks_fts` table and insert/update/delete triggers, then records
the version. Repeated initialization is safe; failure rolls back the migration.
Existing documents, semantic manifests and deletion recovery records are preserved.

`chunks` stores one SQLite copy of text plus chunk UUID, document UUID, generation,
page number and order. A unique document/generation/chunk key prevents duplicate
rows, and document foreign keys cascade deletion. FTS uses `chunks.rowid` and stores
the inverted index without an extra full-text content table. Chroma still holds its
own passage copy for semantic responses; derived plaintext duplication across stores
remains. Existing PDFs require calling `/documents/{id}/index` once after upgrading
to populate keyword data; no upload or destructive metadata rebuild is needed.

FTS text and posting lists, Chroma data, SQLite and PDFs are **unencrypted**.
All three retrieval modes run locally and do not send queries or text to AI APIs.
The future vault must protect source files, derived text, indexes, database journals
and backups. Deleting a document removes its logical records but does not guarantee
forensic erasure of SQLite/Chroma pages or backups. Manual corruption of FTS posting
lists is not detected on every search; restore a consistent backup or reindex affected
documents. This project supports normal retries and crash recovery, not arbitrary
manual edits to storage internals.

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

Real-model verification completed on 2026-10-10 using the actual pinned model,
API handlers through TestClient and isolated persistent ChromaDB. A generated
two-page `solar_rover_sample.pdf` placed garden text on page 1 and a solar-powered
Mars rover on page 2. The paraphrased query "How does the vehicle exploring the red
planet get power?" ranked the page-2 photovoltaic-panel passage first (cosine
distance about 0.494), with the correct document UUID and filename. Reindexing kept
two chunks; deletion returned 204 and left zero vectors and no search results.
No generated verification artifacts or model weights are tracked in Git.

The real tokenizer also confirmed that character size does not guarantee token size:

| 800-character sample | Tokens including special tokens | After the 256-token limit |
| --- | --- | --- |
| Repeated ordinary English sentence about solar panels | 119 | 119 |
| Repeated `qzxv_9137_jkmb_4826` identifiers | 562 | 256 |

For these measurements each repeated string was sliced to exactly 800 characters.
The actual encoder handled the longer sample by truncating it and returning a
384-dimensional embedding. Words beyond that prefix do not influence its embedding,
even though search returns the full chunk text. Identifier-heavy, technical or
non-English chunks can therefore lose retrieval relevance for facts near their end.
The 120-character overlap does not guarantee coverage of every truncated token.
Token-aware splitting is not implemented; smaller character chunks can reduce but
cannot guarantee elimination of this limitation.

Keyword/hybrid real-model verification also passed on 2026-10-10, offline using
cached pinned weights and isolated persistent stores. `ResNet-50`, the paraphrase
"What allows very deep networks to learn without their training signal fading?",
and a hybrid ResNet question all ranked page 2 of a generated
`residual_network_sample.pdf` first. The passage explains residual skip connections
and shortcut pathways carrying error signals through many layers. Correct UUID,
filename, page, strict filtering, reindexing (three total chunks across two PDFs,
unchanged in both stores) and deletion were verified. Generated artifacts are ignored.

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
  generation or upload deduplication yet.
- Character-based chunks may exceed the model's 256-token window and be truncated,
  especially for non-English text. Retrieval is approximate and has no relevance cutoff.
- SQLite and filesystem changes cannot be fully atomic. Staged deletion handles normal
  rollback and restart recovery; a process crash during upload can leave an unindexed
  file. Existing PDFs from before SQLite support are not automatically imported.
- Schema migrations are small, explicit additive steps; there is no heavyweight
  migration framework, pagination or concurrent backup mechanism yet.
- The limit is enforced while copying from FastAPI's spooled upload in 64 KiB chunks.
  Multipart parsing happens first; this is not an incoming request-body quota.
- There are no page-count or extracted-output limits yet; keep this development
  server on localhost and use trusted, non-sensitive sample PDFs.
- PDFs requiring structural repair are rejected as corrupt rather than repaired.
- The existing httpx TestClient integration emits a dependency deprecation warning.

## Secure storage boundary and vault guards

This milestone prepares the storage boundary. **It implements no encryption,
VeraCrypt mounting, passwords, migration, unlocking API or worker orchestration.**
An ordinary directory is not an encrypted volume. Source PDFs, metadata, FTS terms,
chunk text and embeddings remain plaintext with the current development setup.

`RECALLIQ_VAULT_MODE` accepts exactly `disabled` or `external`; unset defaults to
`disabled` for compatibility. Unknown values fail closed for private operations.

| Mode | Behavior |
| --- | --- |
| `disabled` | Existing development paths and upload/CRUD/retrieval behavior; no encryption guarantee |
| `external` | Fixed private paths beneath an existing absolute `RECALLIQ_VAULT_ROOT`; access requires trusted independent verification |

Development configuration remains explicit and supports existing path overrides:

```powershell
$env:RECALLIQ_VAULT_MODE = "disabled"
# Optional; relative values resolve against backend/:
$env:RECALLIQ_TEMP_DIRECTORY = "tmp/private"
```

The configuration service derives uploads, database, vectors, private temporary
storage and recovery paths. Existing defaults remain `backend/uploads`,
`backend/data/recalliq.sqlite3` and `backend/indexes/chroma`; new development defaults
are `backend/tmp/private` and `backend/data/recovery`. Recovery currently continues
to use existing `.pdf.deleting` files alongside uploads and SQLite cleanup tables;
the designated recovery location is available for application-owned artifacts and
is not created unless needed. No existing data is moved or copied.

External configuration example (it will remain locked in this milestone):

```powershell
$env:RECALLIQ_VAULT_MODE = "external"
$env:RECALLIQ_VAULT_ROOT = "V:\RecallIQ"
# Remove development overrides; external locations cannot be independently overridden.
foreach ($name in @("RECALLIQ_DATABASE_PATH", "RECALLIQ_UPLOADS_DIRECTORY",
                    "RECALLIQ_VECTOR_DIRECTORY", "RECALLIQ_TEMP_DIRECTORY",
                    "RECALLIQ_RECOVERY_DIRECTORY")) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
```

External private paths are fixed beneath that root:

```text
uploads/                     original PDFs and staged deletion files
database/recalliq.sqlite3     metadata, chunks, FTS, manifests and cleanup queue
indexes/chroma/              persistent vectors, text, metadata and Chroma files
temporary/                   application-owned temporary files and upload spools
recovery/                    designated application-owned recovery artifacts
```

The root must already exist, be a directory and use an absolute local path. It is
never created for an unavailable vault. Directory existence, a drive letter, a
marker file or an environment flag cannot establish verification. The internal
`VaultVerifier` protocol is the seam for a later VeraCrypt integration that checks
the actual mounted volume, identity and current state. Its production implementation
currently always declines verification, so **external mode remains locked even if
you manually mount a volume**. Tests inject a trusted test-only verifier; that fixture
does not demonstrate encryption or provide a user unlock mechanism.

External mode rejects conflicting development path variables, network/UNC roots,
traversal and existing symlinks or Windows reparse points/junctions. Paths are checked
before resolution, and external private trees are inspected without following links.
SQLite companion names and nested Chroma paths are included. The drive mount itself
must be checked by the future verifier. Development overrides also reject traversal
and links on directly accessed paths; development mode is not a confined vault.
No invalid external configuration falls back to the old plaintext locations.

### Locked request behavior and readiness

A pure ASGI middleware guards `/documents`, `/search` and their subpaths before
calling the request receiver, multipart parser or FastAPI dependencies. Locked or
invalid storage returns HTTP 503 with a safe response such as:

```json
{
  "detail": "Private storage is unavailable or the external vault is not verified.",
  "code": "vault_locked"
}
```

Invalid development/configuration states use `storage_unavailable`. Locked startup
does not initialize SQLite, open Chroma or attempt deletion recovery. Public routes
`/`, `/health`, `/docs`, `/redoc` and `/openapi.json` remain available. `GET /health`
keeps its original response and indicates that the API is alive; it does not certify
private storage availability. `GET /ready` returns HTTP 503 with
`{"status":"locked","storage_ready":false}` or an unavailable state, or HTTP 200
with `{"status":"ready","storage_ready":true,"mode":"disabled"}`. Readiness
checks configuration/attributes but does not initialize databases or write files.
Readiness never asserts that data is encrypted.

### Temporary-file policy and its limits

Inspection of the pinned Starlette parser found a 1 MiB upload spool threshold;
larger uploads can reach disk before the existing 20 MiB handler check. The guard
rejects locked uploads before parsing. Accepted RecallIQ requests use an explicit
private `dir` in a request-context-aware spool factory, with path revalidation at
disk rollover. This narrowly replaces the parser's factory because its pinned API
offers no directory parameter; requests in other applications without RecallIQ's
context keep the original factory. Framework upgrades must rerun these integration
tests. There is still no incoming request-body quota: accepted multipart bodies can
consume disk before file-size validation.

`python -m app.run` establishes `TEMP`, `TMP`, `TMPDIR`, `SQLITE_TMPDIR` and Python's
cached `tempfile.tempdir` before importing Uvicorn and the application. Environment
variables alone are insufficient when Python has cached its directory. If private
storage is unavailable, the launcher configures no private temporary directory and
starts only the guarded API. Changing the storage root/configuration requires a
process restart; live reconfiguration is not supported.

Each application SQLite connection uses `PRAGMA temp_store=MEMORY` for temporary
tables/sorts. Journaling remains enabled beside the validated database; this setting
does not govern Chroma's independent SQLite/Rust connections. Chroma is given an
explicit persistent directory, and the launcher sets inherited native-library temp
variables. These measures are **not proof that every native-library write is contained**.
The SQLite and [Python tempfile documentation](https://docs.python.org/3.11/library/tempfile.html)
describe their separate policies; see also [SQLite temporary files](https://www.sqlite.org/tempfiles.html).

Tests monitor a real 2 MiB multipart spool rollover and its actual Windows temporary
filename, verify cleanup, test memory-backed SQLite temporary tables, and exercise
real Windows junctions. They do not trace every Chroma/native OS write. Future volume
integration needs OS-level tracing (for example Process Monitor) of upload, extraction,
indexing, search and deletion before claiming complete disk containment. Paging,
hibernation, crash dumps, antivirus, filesystem caches and backups remain outside
application-level control. Python does not guarantee secure memory erasure.

FastAPI's native telemetry and automatic exporter setup are explicitly disabled;
Chroma usage telemetry remains disabled. Existing access logs can still contain
request URLs/document IDs; do not add request-body, document-content or secret logging.
Git ignores uploaded PDFs, database files/journals, indexes, temporary data, model
caches, dump files and prospective `.hc` containers. Ignore rules are not encryption.

The boundary uses path checks, not OS handle-based capabilities: a malicious process
can race filesystem changes after validation. An active mount disappearing mid-operation,
native handles surviving in-process, and malicious same-account activity are not solved
by this milestone. Existing staged deletion and index-generation recovery remain in
place; no cross-store atomicity or safe dismount/worker lifecycle is claimed.

## Test

```powershell
Set-Location D:\RecallIQ\backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover existing endpoints, documentation, PDF extraction and rejection cases,
size boundaries and cleanup failures. PDFs are generated in memory, and storage
is redirected to pytest temporary directories; real uploads are never used.
Keyword/hybrid tests use real temporary FTS5 and Chroma stores with deterministic
fake embeddings, covering migration, query syntax, BM25, RRF, filtering, stable IDs,
partial failures, retries, persistence and deletion. Normal tests need no internet.

## Files and concepts

### Internal vault key foundation (Task 8)

`app.vault_keys.VaultKeyManager` is an internal interface with
`initialize(password)`, `unlock(password)` and `lock()`. There is no HTTP password
endpoint or automatic vault creation. Supply an absolute path to a **new** vault
directory beneath an existing parent; initialization refuses every existing
directory, including an empty one. Use an ignored private location such as
`vault/`. Initialization finishes LOCKED; unlocking is a separate operation.

The v1 JSON envelope contains only public format/version, random UUID vault ID,
Argon2id profile and salt, AES-GCM nonce, and the authenticated encrypted master
key. All header fields are authenticated using canonical JSON as associated data.
The sole accepted KDF profile is Argon2id v19, 64 MiB, three passes, four lanes,
32-byte output and 16-byte salt. Parsing is bounded to 4096 bytes; duplicate JSON
fields, unknown versions/profiles, malformed encoding and excessive password
inputs are rejected before expensive derivation. Passwords use exact UTF-8 bytes,
without normalization, and must contain 1–1024 encoded bytes.

Each initialization generates a random 256-bit master key and fresh 96-bit nonce.
V1 wraps once per fresh password-derived key and does not support password changes
or rewrapping. HKDF-SHA256 separates database and document purposes using vault
identity and versioned labels. Passwords and unwrapped keys are never written to
disk. An optional trusted `expected_vault_id` pins identity when reopening;
without an external trusted pin, replacing an entire valid envelope cannot be
detected from that envelope alone.

States are UNINITIALIZED, LOCKED, UNLOCKED and ERROR. Failed unlock drops retained
keys; authentication failure is retryable LOCKED, malformed/unavailable storage is
ERROR. Operations on one manager are serialized. Key ownership is encapsulated
for a future dedicated storage worker; underscore methods are internal integration
seams, not a Python security boundary. Lock releases references and **does not
promise secure memory erasure**.

Publication uses an exclusive directory and pending file, fsync, then an atomic
no-overwrite hard link. A filesystem supporting hard links is required (tested on
Windows NTFS). An interruption before publication leaves an ERROR directory for
manual inspection; incomplete pending files are never adopted. Publication is the
commit point: if removal of the extra encrypted pending hard link fails, the
operation reports an error but the published vault stays LOCKED and can be
unlocked normally. Initialization still refuses to overwrite it. The implementation
never silently repairs a vault. Windows access control inherits
the parent directory ACL; Unix creation modes do not establish a private Windows
ACL. Use a trusted user-owned parent. These path checks do not defeat a privileged
local attacker racing filesystem changes or guarantee power-loss durability.

Development endpoints remain available. External vault mode stays locked even
when this internal manager is UNLOCKED. Existing uploads, SQLite and Chroma are
still plaintext: this task does not encrypt documents, migrate SQLCipher, integrate
production retrieval, implement password recovery, or contain paging/crash dumps.

- Cryptography reference: [AES-GCM](https://cryptography.io/en/latest/hazmat/primitives/aead/).
- KDF reference: [Argon2 parameters](https://argon2-cffi.readthedocs.io/en/stable/parameters.html).

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
- `backend/app/keyword.py`: literal queries, SQLite chunk storage and BM25 retrieval.
- `backend/app/hybrid.py`: candidate selection and Reciprocal Rank Fusion.
- `backend/app/search.py`: validated semantic, keyword and hybrid retrieval endpoints.
- `backend/app/storage.py`: private path configuration and trusted verification interface.
- `backend/app/storage_guard.py`: early ASGI guards and request-local spool placement.
- `backend/app/run.py`: localhost launcher with early process temporary configuration.
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
