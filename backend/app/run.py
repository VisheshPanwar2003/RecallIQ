"""Recommended launcher: configure temporary storage before importing Uvicorn."""
import argparse

from app.storage import configure_process_temporary_storage, get_private_storage, StorageUnavailable


def main():
    parser = argparse.ArgumentParser(description="Run the RecallIQ development backend")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()
    try:
        configure_process_temporary_storage(get_private_storage())
    except StorageUnavailable:
        # A locked/misconfigured backend may still serve health, readiness and docs.
        # No private temp directory is created and protected operations are denied.
        pass
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=options.port, reload=options.reload)


if __name__ == "__main__":
    main()
