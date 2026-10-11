import asyncio
import os
from pathlib import Path
import subprocess
import tempfile

from fastapi.testclient import TestClient
import pytest
from starlette import formparsers

from app import storage, storage_guard
from app.database import connect, get_database_path, initialize_database
from app.embeddings import get_embedder
from app.main import app
from app.storage import PrivateStorage, StorageUnavailable
from app.storage_guard import PrivateStorageGuard
from app.uploads import get_uploads_directory
from app.vectors import get_vector_index
from test_documents import make_pdf, make_sized_pdf, upload
from test_semantic_search import FakeEmbedder


PRIVATE_VARIABLES = ("RECALLIQ_UPLOADS_DIRECTORY", "RECALLIQ_DATABASE_PATH",
                     "RECALLIQ_VECTOR_DIRECTORY", "RECALLIQ_TEMP_DIRECTORY",
                     "RECALLIQ_RECOVERY_DIRECTORY")


def external(monkeypatch, root, *, verified=False):
    monkeypatch.setenv("RECALLIQ_VAULT_MODE", "external")
    monkeypatch.setenv("RECALLIQ_VAULT_ROOT", str(root))
    for name in PRIVATE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    if verified:
        class TrustedTestVerifier:
            # This is an internal test fixture, not an encryption/mount adapter.
            def is_verified(self, expected):
                return expected == root.resolve()
        monkeypatch.setattr(storage, "get_vault_verifier", lambda: TrustedTestVerifier())


@pytest.fixture
def boundary_client(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    external(monkeypatch, root, verified=True)
    app.dependency_overrides.clear()
    embedder = FakeEmbedder()
    app.dependency_overrides[get_embedder] = lambda: embedder
    with TestClient(app) as client:
        yield client, root, embedder


def test_default_development_paths(monkeypatch):
    monkeypatch.delenv("RECALLIQ_VAULT_MODE", raising=False)
    for name in PRIVATE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    config = storage.get_private_storage()
    assert config.mode == "disabled" and config.root is None
    assert config.uploads == storage.BACKEND_DIRECTORY / "uploads"
    assert config.database == storage.BACKEND_DIRECTORY / "data/recalliq.sqlite3"
    assert config.vectors == storage.BACKEND_DIRECTORY / "indexes/chroma"
    assert config.temporary == storage.BACKEND_DIRECTORY / "tmp/private"


def test_development_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("RECALLIQ_DATABASE_PATH", str(tmp_path / "metadata.sqlite3"))
    monkeypatch.setenv("RECALLIQ_UPLOADS_DIRECTORY", str(tmp_path / "pdfs"))
    assert get_database_path() == tmp_path / "metadata.sqlite3"
    assert get_uploads_directory() == tmp_path / "pdfs"


@pytest.mark.parametrize("kind", ["missing", "ordinary", "file"])
def test_external_roots_never_unlock_by_existence(monkeypatch, tmp_path, kind):
    root = tmp_path / "vault"
    if kind == "ordinary":
        root.mkdir()
    elif kind == "file":
        root.write_text("not a volume")
    external(monkeypatch, root)
    monkeypatch.setenv("RECALLIQ_VAULT_VERIFIED", "true")  # Not a supported bypass.
    with pytest.raises(StorageUnavailable):
        storage.get_private_storage()
    for getter in (get_database_path, get_uploads_directory, get_vector_index):
        with pytest.raises(StorageUnavailable):
            getter()
    if kind == "missing":
        assert not root.exists()


@pytest.mark.parametrize("root", ["", "relative/vault", "../vault", "D:relative"])
def test_invalid_external_root(monkeypatch, root):
    external(monkeypatch, Path(root))
    with pytest.raises(StorageUnavailable):
        storage.get_private_storage()


@pytest.mark.parametrize("variable", PRIVATE_VARIABLES)
def test_external_mode_rejects_legacy_path_overrides(monkeypatch, tmp_path, variable):
    root = tmp_path / "vault"
    root.mkdir()
    external(monkeypatch, root, verified=True)
    monkeypatch.setenv(variable, str(tmp_path / "outside"))
    with pytest.raises(StorageUnavailable):
        storage.get_private_storage()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("mode", ["", "externall", "EXTERNAL"])
def test_configuration_errors_fail_closed_but_health_is_available(monkeypatch, mode):
    monkeypatch.setenv("RECALLIQ_VAULT_MODE", mode)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/ready").json() == {"status": "unavailable", "storage_ready": False}
        assert client.get("/documents").status_code == 503


@pytest.mark.parametrize("path", ["/documents", "/documents/upload", "/documents/invalid/index",
                                  "/search/semantic", "/search/keyword", "/search/hybrid",
                                  "/documents/unknown", "/documents/", "/search/keyword/"])
def test_locked_requests_before_body_or_dependencies(monkeypatch, tmp_path, path):
    external(monkeypatch, tmp_path / "missing")
    calls = []

    async def downstream(scope, receive, send):
        calls.append("downstream")

    async def receive():
        calls.append("body")
        raise AssertionError("A locked guard must not receive the body")

    async def send(message):
        calls.append(message)

    guard = PrivateStorageGuard(downstream)
    asyncio.run(guard({"type": "http", "path": path, "method": "POST", "headers": []}, receive, send))
    assert "body" not in calls and "downstream" not in calls
    assert calls[0]["status"] == 503


def test_mount_prefix_still_guarded(monkeypatch, tmp_path):
    external(monkeypatch, tmp_path / "missing")
    client = TestClient(app, root_path="/api")
    assert client.post("/api/documents/upload", content=b"private").status_code == 503
    client.close()


def test_locked_large_multipart_never_spools_or_initializes(monkeypatch, tmp_path):
    root = tmp_path / "missing"
    external(monkeypatch, root)

    def forbidden(*args, **kwargs):
        raise AssertionError("Locked storage must not parse or initialize")

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", forbidden)
    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", forbidden)
    monkeypatch.setattr("app.database.initialize_database", forbidden)
    with TestClient(app) as client:
        response = client.post("/documents/upload", files={"file": ("large.pdf", b"x" * (3 * 1024 * 1024), "application/pdf")})
        assert response.status_code == 503
        assert response.json()["code"] == "vault_locked"
        assert str(root) not in response.text
        for path in ("/", "/health", "/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 200
        assert client.get("/ready").json() == {"status": "locked", "storage_ready": False}
    assert not root.exists()


def test_verified_paths_and_end_to_end_compatibility(boundary_client):
    client, root, embedder = boundary_client
    config = storage.get_private_storage()
    assert config.database == root / "database/recalliq.sqlite3"
    assert config.uploads == root / "uploads"
    assert config.vectors == root / "indexes/chroma"
    assert config.temporary == root / "temporary"
    assert config.recovery == root / "recovery"
    assert client.get("/ready").json() == {"status": "ready", "storage_ready": True, "mode": "external"}
    response = upload(client, make_pdf(("Banana fruit.", "Cats fly into space on rockets.")), "citations.pdf")
    assert response.status_code == 201, response.text
    document_id = response.json()["document_id"]
    assert client.get(f"/documents/{document_id}").status_code == 200
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    calls = embedder.calls
    assert client.post(f"/documents/{document_id}/index").status_code == 200
    assert embedder.calls == calls
    for mode in ("semantic", "keyword", "hybrid"):
        result = client.post(f"/search/{mode}", json={"query": "rockets", "document_ids": [document_id]})
        assert result.status_code == 200, result.text
        match = result.json()["results"][0]
        assert match["document_id"] == document_id and match["page_number"] == 2
        assert match["filename"] == "citations.pdf"
    assert client.delete(f"/documents/{document_id}").status_code == 204
    for mode in ("semantic", "keyword", "hybrid"):
        assert client.post(f"/search/{mode}", json={"query": "cats"}).json() == {"results": []}
    assert not (root / "uploads" / f"{document_id}.pdf").exists()


def test_revalidation_revokes_access(boundary_client, monkeypatch):
    client, _, _ = boundary_client
    assert client.get("/documents").status_code == 200
    monkeypatch.setattr(storage, "get_vault_verifier", lambda: storage.UnverifiedVault())
    assert client.get("/documents").status_code == 503
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_missing_verified_root_is_not_recreated(monkeypatch, tmp_path):
    root = tmp_path / "missing"
    external(monkeypatch, root, verified=True)
    with TestClient(app) as client:
        assert client.get("/documents").status_code == 503
    assert not root.exists()


def test_traversal_and_outside_paths(boundary_client):
    _, root, _ = boundary_client
    config = storage.get_private_storage()
    for path in (root / ".." / "outside.pdf", root.parent / "outside.pdf"):
        with pytest.raises(StorageUnavailable):
            config.check(path)
    with pytest.raises(StorageUnavailable):
        initialize_database(root.parent / "outside.sqlite3")
    assert not (root.parent / "outside.sqlite3").exists()


def junction(link, target):
    if os.name == "nt":
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("relative", ["uploads", "database", "indexes/chroma", "temporary", "recovery"])
def test_junction_escapes_are_rejected(monkeypatch, tmp_path, relative):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    junction(link, outside)
    try:
        external(monkeypatch, root, verified=True)
        with pytest.raises(StorageUnavailable):
            storage.get_private_storage()
        with TestClient(app) as client:
            assert client.get("/documents").status_code == 503
        assert list(outside.iterdir()) == []
    finally:
        # Remove only the checked link itself; never recurse through its target.
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_nested_chroma_junction_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    nested = root / "indexes/chroma"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = nested / "segment"
    junction(link, outside)
    try:
        external(monkeypatch, root, verified=True)
        with pytest.raises(StorageUnavailable):
            storage.get_private_storage()
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_verified_upload_rollover_writes_inside_boundary(boundary_client, monkeypatch):
    client, root, _ = boundary_client
    original = tempfile.TemporaryFile
    opened = []

    def monitored(*args, **kwargs):
        file = original(*args, **kwargs)
        assert Path(kwargs["dir"]) == root / "temporary"
        # On Windows this is an actual on-disk NamedTemporaryFile.
        if isinstance(file.name, str):
            assert Path(file.name).resolve().is_relative_to(root / "temporary")
        opened.append(file)
        return file

    monkeypatch.setattr(tempfile, "TemporaryFile", monitored)
    response = upload(client, make_sized_pdf(2 * 1024 * 1024))
    assert response.status_code == 201, response.text
    assert opened and all(file.closed for file in opened)
    assert list((root / "temporary").iterdir()) == []


def test_spool_does_not_fall_back_when_directory_removed(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    external(monkeypatch, root, verified=True)
    config = storage.get_private_storage()
    spool = storage_guard.PrivateSpooledFile(config, max_size=1)
    config.temporary.rmdir()
    try:
        with pytest.raises(OSError):
            spool.write(b"private bytes")
        assert not config.temporary.exists()
    finally:
        spool.close()


def test_sqlite_temp_memory(boundary_client):
    _, root, _ = boundary_client
    with connect(storage.get_private_storage().database) as connection:
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        connection.execute("CREATE TEMP TABLE private_text(value TEXT)")
        connection.execute("INSERT INTO private_text VALUES (?)", ("synthetic private canary",))
        assert connection.execute("SELECT value FROM private_text").fetchone()[0] == "synthetic private canary"


def test_bootstrap_configures_cached_python_temp_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("RECALLIQ_TEMP_DIRECTORY", str(tmp_path / "owned-temp"))
    for name in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "wrong-temp"))
    previous = tempfile.tempdir
    try:
        tempfile.tempdir = str(tmp_path / "cached-wrong-temp")
        config = storage.get_private_storage()
        storage.configure_process_temporary_storage(config)
        assert tempfile.gettempdir() == str(config.temporary)
        assert all(os.environ[name] == str(config.temporary) for name in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"))
        with tempfile.NamedTemporaryFile() as file:
            assert Path(file.name).parent == config.temporary
    finally:
        tempfile.tempdir = previous


def test_other_apps_spool_factory_has_no_request_context():
    spool = formparsers.SpooledTemporaryFile(max_size=1024)
    assert type(spool) is tempfile.SpooledTemporaryFile
    spool.close()


def test_native_telemetry_explicitly_disabled():
    for option in ("tracing", "metrics", "logs", "operation_spans", "auto_configure"):
        assert app._telemetry[option] is False


def test_verifier_exception_is_safe(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    external(monkeypatch, root)

    class BrokenVerifier:
        def is_verified(self, root):
            raise RuntimeError("private path and unlock secret must not escape")

    monkeypatch.setattr(storage, "get_vault_verifier", lambda: BrokenVerifier())
    with TestClient(app) as client:
        response = client.get("/documents")
        assert response.status_code == 503
        assert "secret" not in response.text and str(root) not in response.text


def test_verified_directory_is_not_assumed_writable(boundary_client, monkeypatch):
    _, root, _ = boundary_client
    config = storage.get_private_storage()
    original = Path.mkdir

    def fail(self, *args, **kwargs):
        if self == root / "temporary":
            raise PermissionError("private local path")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail)
    with pytest.raises(StorageUnavailable) as error:
        config.temporary_directory()
    assert "local path" not in str(error.value)


def test_symlink_attribute_rejected_before_resolution(monkeypatch, tmp_path):
    import stat
    from types import SimpleNamespace
    target = tmp_path / "link"
    original = Path.lstat

    def linked(self, *args, **kwargs):
        if self == target:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(StorageUnavailable):
        storage.validate_path(target)


def test_root_junction_rejected(monkeypatch, tmp_path):
    target = tmp_path / "ordinary"
    target.mkdir()
    root = tmp_path / "vault"
    junction(root, target)
    try:
        external(monkeypatch, root, verified=True)
        with pytest.raises(StorageUnavailable):
            storage.get_private_storage()
        assert list(target.iterdir()) == []
    finally:
        root.rmdir() if os.name == "nt" else root.unlink()


def test_database_companion_junction_rejected(boundary_client, tmp_path):
    _, root, _ = boundary_client
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "database/recalliq.sqlite3-wal"
    junction(link, outside)
    try:
        with pytest.raises(StorageUnavailable):
            connect(storage.get_private_storage().database).__enter__()
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_locked_launcher_does_not_create_private_storage(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from app.run import main
    root = tmp_path / "missing"
    external(monkeypatch, root)
    calls = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs))))
    monkeypatch.setattr(sys, "argv", ["app.run"])
    main()
    assert calls[0][1]["host"] == "127.0.0.1"
    assert not root.exists()


def test_readiness_does_not_initialize_storage(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    external(monkeypatch, root, verified=True)
    client = TestClient(app)  # No lifespan, so readiness must itself remain read-only.
    assert client.get("/ready").status_code == 200
    assert list(root.iterdir()) == []
    client.close()


def test_generated_artifacts_not_tracked():
    import re
    project = storage.BACKEND_DIRECTORY.parent
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=project).decode().split("\0")
    forbidden = re.compile(r"(?:^|/)(?:uploads|data|indexes|models|tmp|\.venv|__pycache__)/|\.(?:pdf|sqlite3?|db|hc|dmp|log)$")
    assert not [path for path in tracked if forbidden.search(path)]
    examples = ["backend/tmp/private/spool.tmp", "backend/data/recalliq.sqlite3-wal",
                "backend/uploads/example.pdf", "vault.hc", "native.dmp"]
    ignored = subprocess.check_output(["git", "check-ignore", "--stdin"], cwd=project,
                                      input=("\n".join(examples) + "\n").encode(), stderr=subprocess.DEVNULL)
    assert set(ignored.decode().splitlines()) == set(examples)


def test_launcher_configures_temps_before_uvicorn_import(monkeypatch, tmp_path):
    import builtins
    import sys
    from types import SimpleNamespace
    from app.run import main
    monkeypatch.setenv("RECALLIQ_TEMP_DIRECTORY", str(tmp_path / "owned"))
    for name in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "old-temp"))
    original_import = builtins.__import__
    previous = tempfile.tempdir
    called = []

    def intercepted(name, *args, **kwargs):
        if name == "uvicorn":
            expected = str(tmp_path / "owned")
            assert tempfile.gettempdir() == expected
            assert os.environ["SQLITE_TMPDIR"] == expected
            called.append("imported_after_configuration")
            return SimpleNamespace(run=lambda *args, **kwargs: None)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", intercepted)
    monkeypatch.setattr(sys, "argv", ["app.run"])
    try:
        main()
        assert called == ["imported_after_configuration"]
    finally:
        tempfile.tempdir = previous
