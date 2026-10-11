from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.documents import router as documents_router
from app.database import DocumentStore, get_database_path
from app.document_management import recover_deletions
from app.uploads import get_uploads_directory
from app.search import router as search_router
from app.storage import get_private_storage, StorageUnavailable
from app.storage_guard import PrivateStorageGuard
from app.source_exclusive import SourceLeaseMiddleware, exclusive_source


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        get_private_storage()
    except StorageUnavailable:
        # Locked startup never initializes SQLite or attempts deletion recovery.
        yield
        return
    # Use the same path overrides as request dependencies in isolated tests.
    database_path = application.dependency_overrides.get(get_database_path, get_database_path)()
    directory = application.dependency_overrides.get(get_uploads_directory, get_uploads_directory)()
    if get_private_storage().mode == "disabled":
        with exclusive_source(database_path, directory):
            recover_deletions(DocumentStore(database_path), directory)
    else:
        recover_deletions(DocumentStore(database_path), directory)
    yield


app = FastAPI(title="RecallIQ API", lifespan=lifespan,
              telemetry={"tracing": False, "metrics": False, "logs": False,
                         "operation_spans": False, "auto_configure": False})
app.add_middleware(
    SourceLeaseMiddleware,
    database_dependency=get_database_path,
    uploads_dependency=get_uploads_directory,
    enabled=lambda: _development_storage_enabled(),
)
app.add_middleware(PrivateStorageGuard)
app.include_router(documents_router)
app.include_router(search_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to RecallIQ API"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "recalliq-backend"}


@app.get("/ready", tags=["status"])
def readiness():
    from fastapi.responses import JSONResponse
    try:
        storage = get_private_storage()
    except StorageUnavailable as error:
        state = "locked" if error.code == "vault_locked" else "unavailable"
        return JSONResponse({"status": state, "storage_ready": False}, status_code=503)
    return {"status": "ready", "storage_ready": True, "mode": storage.mode}


def _development_storage_enabled():
    try:
        return get_private_storage().mode == "disabled"
    except StorageUnavailable:
        return False
