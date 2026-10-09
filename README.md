# RecallIQ

Privacy-Aware Intelligent Knowledge Assistant. Brick 1 provides a minimal
FastAPI backend; document ingestion, retrieval and AI features come later.

## Development status and current features

Brick 1: Project Setup & Backend Foundation is implemented. Current features
are `GET /`, `GET /health`, Swagger UI, an OpenAPI schema and automated tests.
There is no database, authentication, document processing or AI integration yet.

## Planned features (not implemented)

- Local document ingestion and knowledge management.
- Hybrid semantic and keyword retrieval and an encrypted local vault.
- System 1/System 2 routing and local LLM support through Ollama.
- Agentic RAG through LangGraph.
- Privacy guardrails and explicit consent for optional cloud requests.
- Source-grounded answers, evaluation and a React frontend.

## Technology stack and prerequisites

Current stack: Python, FastAPI, Uvicorn, pytest and httpx.
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
Brick 1 needs no environment variables; `.env.example` documents this.

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

## Test

```powershell
Set-Location D:\RecallIQ\backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests check both endpoint responses, Swagger UI and the OpenAPI schema
without starting a separate server.

## Files and concepts

- `backend/app/__init__.py`: marks `app` as a Python package.
- `backend/app/main.py`: creates the FastAPI application and registers routes.
  `@app.get(...)` is similar to Express `app.get(path, handler)`; returning
  a dictionary serializes it to JSON, like `res.json(...)`.
- `backend/tests/test_health.py`: pytest tests using FastAPI's TestClient.
- `backend/requirements.txt`: the four direct dependencies with pinned versions;
  pip also installs their required dependencies.
- `backend/.env.example`: safe configuration example, with no secrets.
- `.gitignore`: excludes environments, secrets and generated local data.

`def` defines a function, and indentation defines its body. `dict[str, str]`
is a return type hint, similar to a TypeScript string-to-string record.
These routes use ordinary functions because they do no asynchronous I/O.
Uvicorn loads `app.main:app`: the `app` object from the `app.main` module.
