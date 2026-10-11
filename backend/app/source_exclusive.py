"""Cross-process exclusion between the development server and offline migration."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path

from app.storage import validate_path


class SourceBusy(RuntimeError):
    """Another RecallIQ process owns this development store."""


def source_identity(database, uploads):
    database = validate_path(Path(database))
    uploads = validate_path(Path(uploads))
    if database.is_relative_to(uploads) or uploads.is_relative_to(database):
        raise SourceBusy("Source database and upload paths overlap.")
    resources = ("database:" + str(database).casefold(),
                 "uploads:" + str(uploads).casefold())
    return tuple(sorted(hashlib.sha256(item.encode("utf-8")).hexdigest()
                         for item in resources))


@contextmanager
def exclusive_source(database, uploads):
    """Acquire a nonblocking per-store Windows mutex for the whole operation.

    RecallIQ's development server acquires this during startup recovery and
    around each HTTP request; migration holds it for the complete source read.
    This supplements Windows per-file sharing guards for existing source files.
    """
    if os.name != "nt":
        raise SourceBusy("Offline source migration requires Windows.")
    identities = source_identity(database, uploads)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        # Lock database and upload roots independently, in a stable order.
        # Different configurations that share either resource still collide.
        for identity in identities:
            handle = api.CreateMutexW(None, False, "Local\\RecallIQ-source-" + identity)
            if not handle:
                raise SourceBusy("Source storage is unavailable.")
            result = api.WaitForSingleObject(handle, 0)
            if result not in (0, 0x80):
                api.CloseHandle(handle)
                raise SourceBusy("Another RecallIQ process is using source storage.")
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            api.ReleaseMutex(handle)
            api.CloseHandle(handle)


class SourceLeaseMiddleware:
    """Hold the source mutex only while startup recovery or an HTTP request runs.

    An idle server must not block a second server instance used for restart or
    recovery. Offline migration still holds the same mutex for its full source
    snapshot, so active API requests fail closed while migration owns it.
    """

    def __init__(self, app, *, database_dependency, uploads_dependency, enabled):
        self.app = app
        self.database_dependency = database_dependency
        self.uploads_dependency = uploads_dependency
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.enabled():
            await self.app(scope, receive, send)
            return

        application = scope.get("app", self.app)
        overrides = getattr(application, "dependency_overrides", {})
        database_getter = overrides.get(self.database_dependency, self.database_dependency)
        uploads_getter = overrides.get(self.uploads_dependency, self.uploads_dependency)
        try:
            with exclusive_source(database_getter(), uploads_getter()):
                await self.app(scope, receive, send)
        except SourceBusy:
            body = json.dumps({"detail": "Source storage is busy"}).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json"),
                             (b"content-length", str(len(body)).encode("ascii"))],
            })
            await send({"type": "http.response.body", "body": body})
