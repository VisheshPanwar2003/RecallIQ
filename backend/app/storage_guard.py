"""Pure ASGI guard: reject before receiving a body or resolving dependencies."""
from contextvars import ContextVar
import tempfile

from starlette import formparsers
from starlette.responses import JSONResponse

from app.storage import get_private_storage, StorageUnavailable


_request_storage = ContextVar("recalliq_private_storage", default=None)
_original_spooled_file = tempfile.SpooledTemporaryFile


class PrivateSpooledFile(_original_spooled_file):
    def __init__(self, storage, *args, **kwargs):
        self.storage = storage
        kwargs["dir"] = str(storage.temporary_directory())
        super().__init__(*args, **kwargs)

    def rollover(self):
        # Revalidate immediately before a disk file is opened; never use OS fallback.
        self.storage.check(self.storage.temporary)
        return super().rollover()


def private_spooled_file(*args, **kwargs):
    storage = _request_storage.get()
    if storage is None:
        return _original_spooled_file(*args, **kwargs)
    return PrivateSpooledFile(storage, *args, **kwargs)


# Starlette's pinned parser does not expose a spool-directory option. This factory
# uses request-local context, leaving other applications in the process unchanged.
formparsers.SpooledTemporaryFile = private_spooled_file


class PrivateStorageGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        protected = any(path == prefix or path.startswith(prefix + "/")
                        for prefix in ("/documents", "/search"))
        if not protected:
            return await self.app(scope, receive, send)
        try:
            storage = get_private_storage()
        except StorageUnavailable as error:
            response = JSONResponse({"detail": str(error), "code": error.code}, status_code=503)
            return await response(scope, receive, send)
        token = _request_storage.set(storage)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_storage.reset(token)
