from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.documents import router as documents_router
from app.database import DocumentStore, get_database_path
from app.document_management import recover_deletions
from app.uploads import get_uploads_directory
from app.search import router as search_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Use the same path overrides as request dependencies in isolated tests.
    database_path = application.dependency_overrides.get(get_database_path, get_database_path)()
    directory = application.dependency_overrides.get(get_uploads_directory, get_uploads_directory)()
    recover_deletions(DocumentStore(database_path), directory)
    yield


app = FastAPI(title="RecallIQ API", lifespan=lifespan)
app.include_router(documents_router)
app.include_router(search_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to RecallIQ API"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "recalliq-backend"}
