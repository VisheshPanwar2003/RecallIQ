"""Windows-only offline source reader. Never opens Chroma or initializes SQLite."""
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sqlite3

from app.storage import validate_path
from app.source_exclusive import SourceBusy, exclusive_source


class MigrationError(RuntimeError):
    def __init__(self, code='migration_integrity'):
        self.code = code
        super().__init__('Migration refused: ' + code)


@contextmanager
def protected_path(path, *, directory=False):
    """Kernel sharing rules deny write/delete handles, not an advisory lease.

    Local trusted administrator/kernel interference is outside this boundary.
    Directory handles deny replacement; child files require their own handles.
    """
    if os.name != 'nt':
        raise MigrationError('windows_required')
    path = validate_path(Path(path))
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                               wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_SHARE_READ is the only sharing mode that prevents a concurrent
    # writer or deleter from opening this file/directory while the migration
    # holds its guard. FILE_SHARE_WRITE here would make the directory guard
    # advisory and could permit a new source file or namespace change.
    handle = api.CreateFileW(str(path), 0 if directory else 0x80000000,
                             1, None, 3,
                             0x02000000 if directory else 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise MigrationError('exclusive_access_required')
    try:
        validate_path(path)
        yield
    finally:
        api.CloseHandle(handle)


def fingerprint(path):
    info = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    return [info.st_size, info.st_mtime_ns, digest.hexdigest()]


class ReadOnlySource:
    """Requires stopped app; denies database/PDF writers for the whole session.

    Non-empty journals/WAL are rejected, including checkpointed WAL files. No
    checkpoint, sidecar cleanup, plaintext copy, or source maintenance is done.
    """
    def __init__(self, database, uploads, destination):
        self.database = validate_path(Path(database))
        self.uploads = validate_path(Path(uploads))
        self.destination = validate_path(Path(destination))
        for path in (self.database.parent, self.uploads):
            if path.is_relative_to(self.destination) or self.destination.is_relative_to(path):
                raise MigrationError('source_destination_overlap')
        self.stack = ExitStack()
        self.files = {}

    def __enter__(self):
        try:
            self.stack.enter_context(exclusive_source(self.database, self.uploads))
            database_directory = self.database.parent
            upload_directory = self.uploads
            common = next((path for path in (database_directory, *database_directory.parents)
                           if upload_directory == path or upload_directory.is_relative_to(path)),
                          Path(database_directory.anchor))
            guarded = set()
            for start in (database_directory, upload_directory):
                directory = start
                while directory != common and directory != Path(directory.anchor):
                    guarded.add(directory)
                    directory = directory.parent
            if common != Path(common.anchor):
                guarded.add(common)
            for directory in sorted(guarded, key=lambda path: (len(path.parts), str(path).casefold())):
                self.stack.enter_context(protected_path(directory, directory=True))
            self.hold(self.database)
            for suffix in ('-journal', '-wal', '-shm'):
                path = Path(str(self.database) + suffix)
                validate_path(path)
                if path.exists():
                    self.hold(path)
                    if suffix != '-shm' and path.stat().st_size:
                        raise MigrationError('nonempty_source_journal')
            self.connection = sqlite3.connect(self.database.as_uri() + '?mode=ro&immutable=1', uri=True)
            self.stack.callback(self.connection.close)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute('PRAGMA query_only=ON')
            self.connection.execute('PRAGMA temp_store=MEMORY')
            self.connection.execute('BEGIN')
            if self.connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise MigrationError('source_integrity')
            if self.connection.execute('PRAGMA foreign_key_check').fetchall():
                raise MigrationError('source_relationships')
            return self
        except SourceBusy:
            self.stack.close()
            raise MigrationError('source_busy') from None
        except MigrationError:
            self.stack.close()
            raise
        except Exception:
            self.stack.close()
            raise MigrationError('source_unavailable') from None

    def hold(self, path):
        path = validate_path(path)
        if path not in self.files:
            self.stack.enter_context(protected_path(path))
            self.files[path] = fingerprint(path)
        return path

    def verify_unchanged(self):
        if any(fingerprint(path) != value for path, value in self.files.items()):
            raise MigrationError('source_changed')
        for suffix in ('-journal', '-wal', '-shm'):
            path = Path(str(self.database) + suffix)
            if path.exists() != (path in self.files):
                raise MigrationError('source_changed')

    def __exit__(self, kind, value, traceback):
        try:
            self.verify_unchanged()
        finally:
            self.stack.close()
