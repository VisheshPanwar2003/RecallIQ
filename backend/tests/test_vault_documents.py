"""Disposable generated PDFs / vaults. No user documents or real passwords."""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from contextlib import contextmanager
import io
import os
from pathlib import Path
import sqlite3
from threading import Event
import time
from uuid import uuid4

import pymupdf
import pytest

from app import vault_documents as documents
from app.vault_document_format import (
    EncryptedDocumentError, HEADER, MAX_PDF_BYTES, open_pdf, seal_pdf,
)
from app.vault_database import VaultDocumentStore, VaultDatabaseUnavailable
from app.vault_keys import VaultKeyManager, VaultError, VaultState
from app.vault_search import VaultSearchService
from app.schemas import SemanticSearchRequest

PASSWORD = 'generated-task10-fixture-password'
CANARY = 'RECALLIQ_GENERATED_PDF_CANARY_52973'


class Embedder:
    config = {'model': 'generated-task10-fixture', 'dimensions': 3, 'revision': 'v1'}

    def encode(self, texts):
        return [[float('solar' in text.lower()), float('garden' in text.lower()), .1] for text in texts]


@lru_cache(maxsize=1)
def pdf_bytes():
    with pymupdf.open() as pdf:
        for text in ('solar panels store clean energy ' + CANARY, 'garden plants need water'):
            pdf.new_page().insert_text((60, 60), text)
        return pdf.tobytes()


@pytest.fixture
def vault(tmp_path):
    owner = VaultKeyManager(tmp_path / 'vault')
    owner.initialize(PASSWORD)
    owner.unlock(PASSWORD)
    store = VaultDocumentStore(owner, Embedder.config, create=True)
    search = VaultSearchService(store, Embedder())
    service = documents.VaultDocumentService(store, search)
    try:
        yield owner, store, search, service
    finally:
        store.close()
        owner.lock()


def ingest(service, data=None, **kwargs):
    return service.ingest(io.BytesIO(pdf_bytes() if data is None else data), 'generated.pdf', **kwargs)


class Crash(BaseException):
    pass


def crash_at(service, monkeypatch, transition):
    def checkpoint(event):
        if event == transition:
            raise Crash()
    monkeypatch.setattr(service, '_checkpoint', checkpoint)


def clear_crash(service, monkeypatch):
    monkeypatch.setattr(service, '_checkpoint', lambda event: None)


def test_round_trip_persistence_no_plaintext(vault):
    owner, store, search, service = vault
    raw = pdf_bytes()
    result = ingest(service, raw)
    did = str(result.document_id)
    assert service.decrypt(did) == raw
    assert result.total_pages == 2
    assert result.pages[1].page_number == 2
    assert CANARY in result.pages[0].text
    assert service.extract(did) == result.pages
    assert store.get(did)['original_filename'] == 'generated.pdf'
    with store.connection() as c:
        assert c.execute('SELECT count(*) FROM vault_document_pages').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM vault_pending_documents').fetchone()[0] == 0
        # Recovery markers carry identifiers/hash/nonce only.
        row = dict(c.execute('SELECT * FROM vault_document_operations').fetchone())
        assert CANARY not in repr(row) and 'generated.pdf' not in repr(row)
    assert not list((owner._root / 'staging').iterdir())
    for path in owner._root.rglob('*'):
        if path.is_file():
            content = path.read_bytes()
            assert CANARY.encode() not in content and raw not in content
    with sqlite3.connect(store.path) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute('SELECT * FROM documents')
    store.close()
    reopened = VaultDocumentStore(owner, Embedder.config)
    try:
        new_service = documents.VaultDocumentService(reopened)
        new_service.recover()
        assert new_service.decrypt(did) == raw
    finally:
        reopened.close()


@pytest.mark.parametrize('mutation', ['wrong-key', 'ciphertext', 'version', 'magic', 'nonce',
                                      'length', 'truncated', 'trailing', 'short', 'document', 'vault'])
def test_container_rejection(mutation):
    key, vid, did = os.urandom(32), str(uuid4()), str(uuid4())
    blob = seal_pdf(key, vid, did, os.urandom(12), pdf_bytes())
    if mutation == 'wrong-key': key = os.urandom(32)
    elif mutation == 'document': did = str(uuid4())
    elif mutation == 'vault': vid = str(uuid4())
    elif mutation == 'truncated': blob = blob[:-1]
    elif mutation == 'trailing': blob += b'x'
    elif mutation == 'short': blob = b'RIQ'
    else:
        value = bytearray(blob)
        position = {'ciphertext': -1, 'version': 8, 'magic': 0, 'nonce': 41, 'length': 60}[mutation]
        value[position] ^= 1
        blob = bytes(value)
    with pytest.raises(EncryptedDocumentError) as error:
        open_pdf(key, vid, did, blob)
    assert CANARY not in str(error.value)


@pytest.mark.parametrize('transition', ['reserved', 'prepared', 'staged_unmarked', 'staged',
                                       'published_unmarked', 'published', 'before_commit', 'committed', 'cleaned'])
def test_ingestion_crash_recovery(vault, monkeypatch, transition):
    _, store, _, service = vault
    did = str(uuid4())
    crash_at(service, monkeypatch, transition)
    with pytest.raises(Crash):
        ingest(service, _document_id=did)
    if transition not in ('committed', 'cleaned'):
        assert store.get(did) is None
    clear_crash(service, monkeypatch)
    service.recover()
    service.recover()
    if transition in ('reserved', 'prepared'):
        assert store.get(did) is None
        assert service._row(did)['state'] == 'FAILED'
    else:
        assert store.get(did) is not None
        assert service.decrypt(did) == pdf_bytes()
        assert service._row(did)['state'] == 'COMMITTED'
    assert not list((service.owner._root / 'staging').iterdir())


@pytest.mark.parametrize('transition', ['before_delete_commit', 'deleted_logically', 'deleted_file'])
def test_deletion_crash_recovery(vault, monkeypatch, transition):
    _, store, search, service = vault
    did = str(ingest(service).document_id)
    service.index(did)
    crash_at(service, monkeypatch, transition)
    with pytest.raises(Crash): service.delete(did)
    if transition == 'before_delete_commit':
        assert store.get(did) is not None and service._path(did).exists()
    else:
        assert store.get(did) is None
        for mode in ('semantic', 'keyword', 'hybrid'):
            assert search.search(SemanticSearchRequest(query='solar'), mode)['results'] == []
    clear_crash(service, monkeypatch)
    service.recover()
    service.delete(did)
    service.recover()
    service.delete(did)
    assert store.get(did) is None and not service._path(did).exists()


def test_indexing_idempotency_citations_and_all_mode_cleanup(vault):
    _, store, search, service = vault
    first, second = str(ingest(service).document_id), str(ingest(service).document_id)
    service.index(first)
    service.index(first)
    service.index(second)
    with store.connection() as c:
        assert c.execute('SELECT count(*) FROM embeddings').fetchone()[0] == 4
    for mode in ('semantic', 'keyword', 'hybrid'):
        matches = search.search(SemanticSearchRequest(query='solar', document_ids=[first]), mode)['results']
        assert matches[0]['document_id'] == first
        assert matches[0]['filename'] == 'generated.pdf' and matches[0]['page_number'] == 1
    with pytest.raises(VaultDatabaseUnavailable): search.delete(first)
    service.delete(first)
    with store.connection() as c:
        for table in ('chunks', 'embeddings', 'chunks_fts'):
            assert c.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM vault_document_pages').fetchone()[0] == 2
    for mode in ('semantic', 'keyword', 'hybrid'):
        assert all(r['document_id'] == second for r in search.search(SemanticSearchRequest(query='solar'), mode)['results'])
    assert service.decrypt(second) == pdf_bytes()


def test_file_cleanup_failure_is_durable_and_retryable(vault, monkeypatch):
    _, store, _, service = vault
    did = str(ingest(service).document_id)
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == service._path(did): raise PermissionError('generated failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(EncryptedDocumentError): service.delete(did)
    assert store.get(did) is None and service._row(did)['state'] == 'DELETE_PENDING'
    assert service._path(did).exists()
    service.recover()
    assert service._row(did)['state'] == 'DELETED'


def test_duplicate_id_and_ciphertext_are_never_overwritten(vault):
    _, _, _, service = vault
    did = str(ingest(service).document_id)
    original = service._path(did).read_bytes()
    with pytest.raises(EncryptedDocumentError): ingest(service, _document_id=did)
    assert service._path(did).read_bytes() == original
    service.delete(did)
    with pytest.raises(EncryptedDocumentError): ingest(service, _document_id=did)
    assert not service._path(did).exists()


def test_nonce_collision_rejected_before_encryption(vault, monkeypatch):
    _, _, _, service = vault
    nonce = b'generated123'
    random_bytes = os.urandom
    monkeypatch.setattr(documents.os, 'urandom', lambda size: nonce if size == 12 else random_bytes(size))
    ingest(service)
    with pytest.raises(EncryptedDocumentError): ingest(service)
    assert len(list((service.owner._root / 'documents').iterdir())) == 1


@pytest.mark.parametrize('failure', ['encryption', 'extraction', 'publication', 'transaction'])
def test_ingestion_failures_do_not_publish_partial_records(vault, monkeypatch, failure):
    _, store, _, service = vault
    did = str(uuid4())
    def fail(*args, **kwargs): raise RuntimeError('generated failure')
    with monkeypatch.context() as patch:
        if failure == 'encryption': patch.setattr(documents, 'seal_pdf', fail)
        elif failure == 'extraction': patch.setattr(documents, 'extract_pdf_bytes', fail)
        elif failure == 'publication': patch.setattr(documents.os, 'link', fail)
        else:
            def checkpoint(event):
                if event == 'before_commit': fail()
            patch.setattr(service, '_checkpoint', checkpoint)
        with pytest.raises(EncryptedDocumentError): ingest(service, _document_id=did)
    assert store.get(did) is None
    service.recover()
    if failure in ('publication', 'transaction'): assert store.get(did) is not None


@pytest.mark.parametrize('kind', ['oversized', 'truncated', 'invalid', 'empty', 'wrong-size', 'cancelled', 'timeout', 'disconnect'])
def test_stream_rejection_without_files(vault, kind):
    _, store, _, service = vault
    kwargs = {}
    raw = pdf_bytes()
    if kind == 'oversized': raw += b' ' * MAX_PDF_BYTES
    elif kind == 'truncated': raw = raw[:150]
    elif kind == 'invalid': raw = b'%PDF-this-is-not-a-PDF'
    elif kind == 'empty': raw = b''
    elif kind == 'wrong-size': kwargs['expected_size'] = len(raw) + 1
    elif kind == 'cancelled':
        cancel = Event(); cancel.set(); kwargs['cancel'] = cancel
    elif kind == 'timeout': kwargs['timeout'] = 1e-12
    stream = io.BytesIO(raw)
    if kind == 'disconnect':
        class Disconnected:
            def read(self, size): raise ConnectionError('generated disconnect')
        stream = Disconnected()
    with pytest.raises(EncryptedDocumentError): service.ingest(stream, 'generated.pdf', **kwargs)
    with store.connection() as c:
        assert c.execute('SELECT count(*) FROM documents').fetchone()[0] == 0
    assert not list((service.owner._root / 'documents').iterdir())
    assert not list((service.owner._root / 'staging').iterdir())


@pytest.mark.parametrize('size', [1024 * 1024 + 500, MAX_PDF_BYTES])
def test_large_and_maximum_pdf_never_spool(vault, monkeypatch, size):
    import tempfile
    _, _, _, service = vault
    def forbidden(*args, **kwargs): raise AssertionError('Plaintext spool attempted')
    monkeypatch.setattr(tempfile, 'SpooledTemporaryFile', forbidden)
    monkeypatch.setattr(tempfile, 'TemporaryFile', forbidden)
    from test_documents import make_sized_pdf
    raw = make_sized_pdf(size)
    did = str(ingest(service, raw, expected_size=size).document_id)
    assert len(service.decrypt(did)) == size


@pytest.mark.parametrize('action', ['ingest', 'delete', 'decrypt', 'index', 'recover'])
def test_locked_operations_rejected(vault, action):
    owner, _, _, service = vault
    did = str(ingest(service).document_id)
    owner.lock()
    with pytest.raises((VaultError, EncryptedDocumentError)):
        if action == 'ingest': ingest(service)
        else: getattr(service, action)(*(() if action == 'recover' else (did,)))


@pytest.mark.parametrize('active', ['ingest', 'delete'])
def test_lock_waits_and_new_work_rejected(vault, monkeypatch, active):
    owner, store, _, service = vault
    did = str(ingest(service).document_id)
    entered, release = Event(), Event()
    transition = 'staged' if active == 'ingest' else 'deleted_logically'
    def checkpoint(event):
        if event == transition:
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(service, '_checkpoint', checkpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(ingest, service) if active == 'ingest' else pool.submit(service.delete, did)
        assert entered.wait(5)
        locking = pool.submit(owner.lock)
        assert owner._locking.wait(5)
        try:
            assert not locking.done()
            with pytest.raises(VaultError): ingest(service)
        finally: release.set()
        work.result(timeout=8)
        locking.result(timeout=8)
    assert owner.state == VaultState.LOCKED and store._closed and store._snapshot is None
    assert owner._active_operations == 0


def test_concurrency_budget_released_on_crash(vault, monkeypatch):
    _, _, _, service = vault
    entered, release = Event(), Event()
    def checkpoint(event):
        if event == 'reserved':
            entered.set(); assert release.wait(5); raise Crash()
    monkeypatch.setattr(service, '_checkpoint', checkpoint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ingest, service)
        assert entered.wait(5)
        try:
            with pytest.raises(EncryptedDocumentError): ingest(service)
        finally: release.set()
        with pytest.raises(Crash): first.result(timeout=8)
    clear_crash(service, monkeypatch)
    assert ingest(service).total_pages == 2


def test_recovery_and_deletion_preserve_substituted_files(vault):
    _, store, _, service = vault
    first, second = str(ingest(service).document_id), str(ingest(service).document_id)
    substitute = service._path(second).read_bytes()
    service._path(first).write_bytes(substitute)
    with pytest.raises(EncryptedDocumentError): service.decrypt(first)
    with pytest.raises(EncryptedDocumentError): service.delete(first)
    assert store.get(first) is None and service._row(first)['state'] == 'DELETE_PENDING'
    with pytest.raises(EncryptedDocumentError): service.recover()
    assert service._path(first).read_bytes() == substitute
    assert service.decrypt(second) == pdf_bytes()


@pytest.mark.parametrize('bad_id', ['../escape', str(uuid4()) + '/x', 'x', str(uuid4()).upper()])
def test_path_input_rejected(vault, bad_id):
    _, _, _, service = vault
    with pytest.raises(EncryptedDocumentError): ingest(service, _document_id=bad_id)


def test_lock_inside_lease_rejected_instead_of_deadlock(vault):
    owner, _, _, _ = vault
    with owner._operation():
        with pytest.raises(VaultError): owner.lock()
        with pytest.raises(VaultError): owner.unlock(PASSWORD)


@pytest.mark.parametrize('phase', ['empty', 'header', 'partial', 'unrelated'])
def test_partial_stage_recovery(vault, monkeypatch, phase):
    _, store, _, service = vault
    did = str(uuid4())
    crash_at(service, monkeypatch, 'staged')
    with pytest.raises(Crash): ingest(service, _document_id=did)
    clear_crash(service, monkeypatch)
    path = service._path(did, True)
    original = path.read_bytes()
    content = {'empty': b'', 'header': original[:20], 'partial': original[:HEADER.size + 50],
               'unrelated': b'generated unrelated file'}[phase]
    path.write_bytes(content)
    if phase == 'unrelated':
        with pytest.raises(EncryptedDocumentError): service.recover()
        assert path.read_bytes() == content
    else:
        service.recover(); service.recover()
        assert not path.exists() and service._row(did)['state'] == 'FAILED'
    assert store.get(did) is None


def test_ingestion_cleanup_failure_after_commit(vault, monkeypatch):
    _, store, _, service = vault
    did = str(uuid4())
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path == service._path(did, True): raise PermissionError('generated failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(EncryptedDocumentError): ingest(service, _document_id=did)
    assert store.get(did) is not None
    assert service._row(did)['state'] == 'COMMITTED' and service._row(did)['stage_owned'] == 1
    service.recover(); service.recover()
    assert not service._path(did, True).exists()
    assert service.decrypt(did) == pdf_bytes()


@pytest.mark.parametrize('folder', ['documents', 'staging'])
def test_actual_junction_escape_rejected(vault, tmp_path, folder):
    from test_storage_boundary import junction
    _, _, _, service = vault
    outside = tmp_path / 'outside'; outside.mkdir()
    link = service.owner._root / folder
    link.rmdir()  # Own empty test directory only, no recursion.
    junction(link, outside)
    try:
        with pytest.raises(EncryptedDocumentError): ingest(service)
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


@pytest.mark.parametrize('headers', [[], [(b'content-length', b'1')],
                                    [(b'content-length', b'999999999')],
                                    [(b'content-type', b'multipart/form-data; boundary=bad')]])
def test_disabled_http_rejects_before_body_or_parser(monkeypatch, tmp_path, headers):
    import asyncio
    from starlette import formparsers
    from app.storage_guard import PrivateStorageGuard
    monkeypatch.setenv('RECALLIQ_VAULT_MODE', 'external')
    monkeypatch.setenv('RECALLIQ_VAULT_ROOT', str(tmp_path / 'unmounted'))
    messages = []
    async def forbidden(*args, **kwargs):
        raise AssertionError('Vault HTTP body must remain unread')
    monkeypatch.setattr(formparsers.MultiPartParser, 'parse', forbidden)
    async def send(message): messages.append(message)
    asyncio.run(PrivateStorageGuard(forbidden)(
        {'type': 'http', 'path': '/documents/upload', 'method': 'POST', 'headers': headers}, forbidden, send))
    assert messages[0]['status'] == 503


def test_actual_large_multipart_http_remains_disabled(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from starlette import formparsers
    from app.main import app
    from test_documents import make_sized_pdf
    monkeypatch.setenv('RECALLIQ_VAULT_MODE', 'external')
    monkeypatch.setenv('RECALLIQ_VAULT_ROOT', str(tmp_path / 'unmounted'))
    def forbidden(*args, **kwargs): raise AssertionError('Multipart parser must not be reached')
    monkeypatch.setattr(formparsers.MultiPartParser, 'parse', forbidden)
    with TestClient(app) as client:
        response = client.post('/documents/upload', files={'file': ('generated.pdf', make_sized_pdf(2 * 1024 * 1024), 'application/pdf')})
    assert response.status_code == 503


@pytest.mark.parametrize('active', ['ingest', 'index'])
def test_simultaneous_delete_rejected_by_global_budget(vault, monkeypatch, active):
    _, _, search, service = vault
    did = str(ingest(service).document_id)
    entered, release = Event(), Event()
    def pause():
        entered.set(); assert release.wait(5)
    if active == 'ingest':
        def checkpoint(event):
            if event == 'reserved': pause()
        monkeypatch.setattr(service, '_checkpoint', checkpoint)
    else:
        encode = search.embedder.encode
        def blocking_encode(texts): pause(); return encode(texts)
        monkeypatch.setattr(search.embedder, 'encode', blocking_encode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ingest, service) if active == 'ingest' else pool.submit(service.index, did)
        assert entered.wait(5)
        try:
            with pytest.raises(EncryptedDocumentError): service.delete(did)
        finally: release.set()
        future.result(timeout=8)
    assert service.decrypt(did) == pdf_bytes()


def test_admission_stays_closed_for_multiple_lock_requests(vault):
    owner, _, _, _ = vault
    entered, release = Event(), Event()
    def operation():
        with owner._operation():
            entered.set(); assert release.wait(5)
    with ThreadPoolExecutor(max_workers=3) as pool:
        work = pool.submit(operation)
        assert entered.wait(5)
        locks = [pool.submit(owner.lock) for _ in range(2)]
        deadline = time.monotonic() + 3
        while owner._transition_requests != 2 and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            assert owner._transition_requests == 2
            with pytest.raises(VaultError):
                with owner._operation(): pass
        finally: release.set()
        work.result(timeout=8)
        for future in locks: future.result(timeout=8)
    assert owner.state == VaultState.LOCKED and not owner._locking.is_set()


def test_copied_context_cannot_borrow_lease_in_another_thread(vault):
    from contextvars import copy_context
    owner, _, _, _ = vault
    with owner._operation():
        copied = copy_context()
        owner._locking.set()
        try:
            def operation():
                with owner._operation(): pass
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(VaultError): pool.submit(copied.run, operation).result(timeout=3)
        finally:
            owner._locking.clear()


@pytest.mark.parametrize('transition', ['lock', 'unlock'])
def test_observer_failure_still_revokes_keys_and_closes_stores(vault, transition):
    owner, store, _, _ = vault
    class FailedObserver:
        def close(self): raise RuntimeError('generated failure')
    failing = FailedObserver()
    owner._lock_observers.insert(0, __import__('weakref').WeakMethod(failing.close))
    with pytest.raises(VaultError):
        owner.lock() if transition == 'lock' else owner.unlock(PASSWORD)
    assert owner._master is None and owner._vault_id is None and store._closed
    assert owner.state in (VaultState.LOCKED, VaultState.ERROR)


@pytest.mark.parametrize('kind', ['password', 'image-only', 'too-many-pages'])
def test_native_pdf_validation(vault, kind):
    _, _, _, service = vault
    with pymupdf.open() as pdf:
        if kind == 'password':
            pdf.new_page().insert_text((60, 60), 'generated text')
            raw = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='generated', user_pw='generated')
        else:
            for _ in range(201 if kind == 'too-many-pages' else 1): pdf.new_page()
            raw = pdf.tobytes()
    with pytest.raises(EncryptedDocumentError): ingest(service, raw)
    assert not list((service.owner._root / 'staging').iterdir())


def test_expired_copied_context_cannot_revive_lease(vault):
    from contextvars import copy_context
    owner, _, _, _ = vault
    with owner._operation(): copied = copy_context()
    owner._locking.set()
    try:
        def operation():
            with owner._operation(): pass
        with pytest.raises(VaultError): copied.run(operation)
    finally:
        owner._locking.clear()


def test_expired_copied_context_cannot_bypass_memory_budget():
    from contextvars import copy_context
    from app.vault_memory import memory_reservation
    with memory_reservation(): copied = copy_context()
    entered, release = Event(), Event()
    def reserved():
        with memory_reservation():
            entered.set(); assert release.wait(5)
    def stale_operation():
        with memory_reservation(): pass
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(reserved)
        assert entered.wait(5)
        try:
            with pytest.raises(EncryptedDocumentError): copied.run(stale_operation)
        finally: release.set()
        work.result(timeout=8)


@pytest.mark.parametrize('operation', ['extract', 'index'])
def test_native_failure_cannot_leak_plaintext_in_diagnostics(vault, monkeypatch, operation):
    import traceback
    _, _, search, service = vault
    did = str(ingest(service).document_id)
    def leaking_failure(*args, **kwargs): raise RuntimeError(CANARY)
    if operation == 'extract': monkeypatch.setattr(documents, 'extract_pdf_bytes', leaking_failure)
    else: monkeypatch.setattr(search.embedder, 'encode', leaking_failure)
    with pytest.raises(EncryptedDocumentError) as caught: getattr(service, operation)(did)
    diagnostic = ''.join(traceback.format_exception(caught.value))
    assert CANARY not in diagnostic and caught.value.__suppress_context__
