"""Private path policy. Verification is a trusted integration, never an API flag."""
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Protocol


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
STORAGE_MESSAGE = "Private storage is unavailable or the external vault is not verified."


class StorageUnavailable(RuntimeError):
    def __init__(self, code="storage_unavailable"):
        super().__init__(STORAGE_MESSAGE)
        self.code = code


class VaultVerifier(Protocol):
    def is_verified(self, root: Path) -> bool:
        """Confirm the expected mounted volume, identity and current verification."""
        ...


class UnverifiedVault:
    def is_verified(self, root: Path) -> bool:
        return False


def get_vault_verifier() -> VaultVerifier:
    # Future OS integration belongs here. No password, marker-file or env bypass.
    return UnverifiedVault()


def validate_path(path: Path, *, root: Path | None = None) -> Path:
    """Reject traversal and existing links/reparse points BEFORE resolving."""
    path = Path(path)
    try:
        if ".." in path.parts or not path.is_absolute():
            raise StorageUnavailable()
        for part in reversed((path, *path.parents)):
            # The drive itself can be mounted; verification is the provider's job.
            if part == Path(part.anchor):
                continue
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise StorageUnavailable()
        resolved = path.resolve()
        if root is not None and not resolved.is_relative_to(root):
            raise StorageUnavailable()
        return resolved
    except (OSError, ValueError, RuntimeError) as error:
        if isinstance(error, StorageUnavailable):
            raise
        raise StorageUnavailable() from error


def validate_tree(path: Path, root: Path):
    """Inspect names/attributes only; do not follow junctions or read contents."""
    validate_path(path, root=root)
    if path.is_dir():
        def unreadable(error):
            raise error
        for directory, directories, files in os.walk(path, followlinks=False, onerror=unreadable):
            for name in (*directories, *files):
                validate_path(Path(directory) / name, root=root)


def development_path(variable, default):
    raw = os.environ.get(variable, default)
    if not raw.strip():
        raise StorageUnavailable()
    path = Path(raw)
    return validate_path(path if path.is_absolute() else BACKEND_DIRECTORY / path)


@dataclass(frozen=True)
class PrivateStorage:
    mode: str
    root: Path | None
    uploads: Path
    database: Path
    vectors: Path
    temporary: Path
    recovery: Path

    @classmethod
    def from_environment(cls):
        mode = os.environ.get("RECALLIQ_VAULT_MODE", "disabled")
        if mode == "disabled":
            return cls(mode, None,
                       development_path("RECALLIQ_UPLOADS_DIRECTORY", "uploads"),
                       development_path("RECALLIQ_DATABASE_PATH", "data/recalliq.sqlite3"),
                       development_path("RECALLIQ_VECTOR_DIRECTORY", "indexes/chroma"),
                       development_path("RECALLIQ_TEMP_DIRECTORY", "tmp/private"),
                       development_path("RECALLIQ_RECOVERY_DIRECTORY", "data/recovery"))
        if mode != "external":
            raise StorageUnavailable()
        raw = os.environ.get("RECALLIQ_VAULT_ROOT", "")
        root = Path(raw)
        if not raw.strip() or not root.is_absolute() or root.drive.startswith("\\\\"):
            raise StorageUnavailable("vault_locked")
        # External paths are fixed beneath one root; reject conflicting old config.
        if any(os.environ.get(name) for name in (
            "RECALLIQ_UPLOADS_DIRECTORY", "RECALLIQ_DATABASE_PATH", "RECALLIQ_VECTOR_DIRECTORY",
            "RECALLIQ_TEMP_DIRECTORY", "RECALLIQ_RECOVERY_DIRECTORY",
        )):
            raise StorageUnavailable("vault_locked")
        try:
            root = validate_path(root)
        except StorageUnavailable as error:
            raise StorageUnavailable("vault_locked") from error
        return cls(mode, root, root / "uploads", root / "database/recalliq.sqlite3",
                   root / "indexes/chroma", root / "temporary", root / "recovery")

    def require_ready(self):
        try:
            if self.mode == "external":
                validate_path(self.root)
                if not self.root.is_dir() or get_vault_verifier().is_verified(self.root) is not True:
                    raise StorageUnavailable("vault_locked")
            for path in (self.uploads, self.database, self.vectors, self.temporary, self.recovery):
                validate_path(path, root=self.root)
                if path.exists() and ((path == self.database and not path.is_file())
                                      or (path != self.database and not path.is_dir())):
                    raise StorageUnavailable()
            if self.root is not None:
                # SQLite companions and nested Chroma files are part of the boundary.
                for path in (self.uploads, self.database.parent, self.vectors.parent, self.temporary, self.recovery):
                    validate_tree(path, self.root)
        except Exception as error:
            raise StorageUnavailable("vault_locked" if self.mode == "external" else "storage_unavailable") from error
        return self

    def check(self, path: Path):
        self.require_ready()
        return validate_path(path, root=self.root)

    def temporary_directory(self):
        try:
            self.check(self.temporary)
            self.temporary.mkdir(parents=True, exist_ok=True)
            return self.check(self.temporary)
        except OSError as error:
            raise StorageUnavailable() from error


def get_private_storage():
    return PrivateStorage.from_environment().require_ready()


def check_private_path(path: Path):
    storage = get_private_storage()
    return validate_path(path, root=storage.root)


def configure_process_temporary_storage(storage: PrivateStorage):
    """Call in the launcher before native libraries initialize; never guess/fallback."""
    directory = str(storage.temporary_directory())
    for name in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
        os.environ[name] = directory
    # Python caches its selection; environment variables alone do not reset it.
    tempfile.tempdir = directory
