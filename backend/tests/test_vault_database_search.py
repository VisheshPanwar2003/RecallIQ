import json
import sqlite3
from uuid import uuid4

import numpy as np
import pytest

from app import vault_database as database
from app import vault_search as retrieval
from app.vault_keys import VaultKeyManager
from app.schemas import DocumentUploadResponse, ExtractedPage, SemanticSearchRequest
from app.chunking import chunk_pages
from app.vectors import IndexUnavailable


PASSWORD = 'generated-database-test-password'
CANARY = 'RECALLIQ_ENCRYPTED_ONLY_CANARY_81354'


class Embedder:
    config = {'model': 'generated-test-model', 'dimensions': 3, 'revision': 'fixture-v1'}
    def encode(self, texts):
        return [[float('solar' in t.lower()), float('garden' in t.lower()), 0.1] for t in texts]


@pytest.fixture
def vault(tmp_path):
    owner = VaultKeyManager(tmp_path / 'vault'); owner.initialize(PASSWORD); owner.unlock(PASSWORD)
    store = database.VaultDocumentStore(owner, Embedder.config, create=True)
    service = retrieval.VaultSearchService(store, Embedder())
    try:
        yield owner, store, service
    finally:
        store.close(); owner.lock()


def add(service, text='solar solar battery '+CANARY, filename='generated.pdf', pages=None):
    document_id = str(uuid4())
    pages = pages or [ExtractedPage(page_number=2, text=text, character_count=len(text))]
    result = DocumentUploadResponse(document_id=document_id, filename=filename, total_pages=len(pages),
                                    total_characters=sum(p.character_count for p in pages), pages=pages)
    service.store.insert(result, 123)
    service.index_pages(document_id, pages)
    return document_id, pages


def request(query='solar', ids=()):
    return SemanticSearchRequest(query=query, document_ids=list(ids), top_k=5)


def test_encrypted_reopen_and_pragmas(vault):
    owner, store, service = vault
    document_id, _ = add(service)
    with store.connection() as connection:
        assert connection.execute('PRAGMA cipher_version').fetchone()[0].startswith('4.12.')
        for name, value in [('temp_store',2),('foreign_keys',1),('cipher_use_hmac',1),('cipher_plaintext_header_size',0),('cipher_memory_security',1),('mmap_size',0)]:
            assert int(connection.execute('PRAGMA '+name).fetchone()[0])==value
    store.close()
    reopened = database.VaultDocumentStore(owner, Embedder.config)
    try:
        assert reopened.get(document_id)['original_filename']=='generated.pdf'
        result=retrieval.VaultSearchService(reopened,Embedder()).search(request())
        assert result['results'][0]['document_id']==document_id
        assert CANARY.encode() not in reopened.path.read_bytes()
        with sqlite3.connect(reopened.path) as ordinary:
            with pytest.raises(sqlite3.DatabaseError): ordinary.execute('SELECT * FROM documents').fetchall()
    finally: reopened.close()


def test_wrong_key_and_corruption_do_not_replace(vault, monkeypatch):
    owner, store, service=vault; add(service); store.close()
    before=store.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(owner,'_subkey',lambda purpose:b'x'*32)
        with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config)
    assert store.path.read_bytes()==before
    damaged=bytearray(before); damaged[100]^=1; store.path.write_bytes(damaged)
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config)
    assert store.path.read_bytes()==bytes(damaged)


def test_missing_database_never_created(vault):
    owner, store, _=vault; store.close(); store.path.unlink()
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config)
    assert not store.path.exists()


def test_dependency_missing_is_fail_closed(vault, monkeypatch):
    owner, store, _=vault; store.close(); before=store.path.read_bytes()
    monkeypatch.setattr(database,'sqlcipher_driver',lambda: (_ for _ in ()).throw(ImportError('generated failure')))
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config)
    assert store.path.read_bytes()==before


def test_ordinary_sqlite_driver_rejected_before_creation(tmp_path,monkeypatch):
    owner=VaultKeyManager(tmp_path/'vault'); owner.initialize(PASSWORD); owner.unlock(PASSWORD)
    monkeypatch.setattr(database,'sqlcipher_driver',lambda:sqlite3)
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config,create=True)
    assert not (owner._root/'database.sqlite3').exists()


def test_creation_cannot_overwrite(vault):
    owner,store,service=vault; add(service); before=store.path.read_bytes()
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config,create=True)
    assert store.path.read_bytes()==before


@pytest.mark.parametrize('mode',['semantic','keyword','hybrid'])
def test_ranking_filter_and_citations(vault,mode):
    _,store,service=vault
    first,pages=add(service,filename='solar-sample.pdf')
    second,_=add(service,'garden garden plants','garden-sample.pdf')
    result=service.search(request(),mode)['results']
    assert result[0]['document_id']==first
    assert result[0]['filename']=='solar-sample.pdf' and result[0]['page_number']==2
    assert result[0]['chunk_id']==chunk_pages(first,pages)[0].chunk_id
    filtered=service.search(request(ids=[second]),mode)['results']
    assert all(row['document_id']==second for row in filtered)
    if mode=='keyword': assert result[0]['bm25_score']<0
    if mode=='hybrid':
        assert result[0]['keyword_rank']==result[0]['semantic_rank']==1
        assert result[0]['rrf_score']==pytest.approx(2/61)


def test_idempotent_updates_deletion_reload(vault):
    owner,store,service=vault; did,pages=add(service)
    first=service.search(request())['results'][0]['chunk_id']
    service.index_pages(did,pages)
    with store.connection() as connection:
        assert connection.execute('SELECT count(*) FROM embeddings').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]==1
        row=connection.execute('SELECT dimensions,vector FROM embeddings').fetchone()
        assert row[0]==3 and len(row[1])==12
        assert np.linalg.norm(np.frombuffer(row[1],dtype='<f4'))==pytest.approx(1)
    updated=[ExtractedPage(page_number=3,text='garden plants',character_count=13)]
    service.index_pages(did,updated)
    assert service.search(request(),'keyword')['results']==[]
    assert service.search(request('garden'))['results'][0]['chunk_id']!=first
    service.delete(did)
    for mode in ('semantic','keyword','hybrid'): assert service.search(request(),mode)=={'results':[]}
    assert store.list()==[]
    store.close(); reopened=database.VaultDocumentStore(owner,Embedder.config)
    try: assert retrieval.VaultSearchService(reopened,Embedder()).search(request())=={'results':[]}
    finally: reopened.close()


def test_deterministic_ties(vault):
    _,_,service=vault; add(service); add(service)
    for mode in ('semantic','keyword','hybrid'):
        rows=service.search(request(),mode)['results']; assert [r['chunk_id'] for r in rows]==sorted(r['chunk_id'] for r in rows)


@pytest.mark.parametrize('vector',[[1,2],[0,0,0],[float('nan'),1,1],[float('inf'),1,1]])
def test_invalid_vectors_roll_back(vault,monkeypatch,vector):
    _,store,service=vault; did,pages=add(service); before=service.search(request())
    monkeypatch.setattr(service.embedder,'encode',lambda texts:[vector]*len(texts))
    with pytest.raises(IndexUnavailable): service.index_pages(did,pages)
    monkeypatch.setattr(service.embedder,'encode',Embedder().encode)
    assert service.search(request())==before


def test_incompatible_model_dimensions_schema(vault):
    owner,store,service=vault; add(service)
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,{**Embedder.config,'dimensions':4})
    with pytest.raises(IndexUnavailable): service.vectors._load({**Embedder.config,'revision':'other'})
    with store.connection() as connection: connection.execute('INSERT INTO vault_schema_migrations VALUES(99)')
    with pytest.raises(database.VaultDatabaseUnavailable): database.VaultDocumentStore(owner,Embedder.config)


@pytest.mark.parametrize('column,value',[('vector',b'\x00'*12),('vector',np.array([float('nan'),1,1],dtype='<f4').tobytes()),('dimensions',2),('format_version',2)])
def test_malformed_records_invalidate_cached_index(vault,column,value):
    _,store,service=vault; add(service); assert store._snapshot is not None
    with store.connection() as connection:
        connection.execute('PRAGMA ignore_check_constraints=ON')
        connection.execute('UPDATE embeddings SET '+column+'=?',(value,))
    with pytest.raises((database.VaultDatabaseUnavailable,IndexUnavailable)): service.search(request())
    assert store._snapshot is None


def test_missing_vector_or_manifest_rejected(vault):
    _,store,service=vault; add(service)
    with store.connection() as connection: connection.execute('DELETE FROM embeddings')
    with pytest.raises((database.VaultDatabaseUnavailable,IndexUnavailable)): service.search(request())


def test_lock_and_close_invalidate(vault):
    owner,store,service=vault; add(service); assert store._snapshot is not None
    owner.lock(); assert store._closed and store._snapshot is None
    for mode in ('semantic','keyword','hybrid'):
        with pytest.raises(database.VaultDatabaseUnavailable): service.search(request(),mode)
    owner.unlock(PASSWORD)
    with pytest.raises(database.VaultDatabaseUnavailable): service.search(request())
    reopened=database.VaultDocumentStore(owner,Embedder.config)
    try: assert retrieval.VaultSearchService(reopened,Embedder()).search(request())['results']
    finally: reopened.close()


def test_memory_limit_refuses_before_loading(vault,monkeypatch):
    _,store,service=vault; add(service)
    monkeypatch.setattr(retrieval,'MAX_CHUNKS',0); store._snapshot=None
    with pytest.raises((IndexUnavailable,database.VaultDatabaseUnavailable)): service.search(request())
    assert store._snapshot is None


def test_text_limit_rolls_back_update(vault,monkeypatch):
    _,store,service=vault; did,pages=add(service); before=service.search(request())
    monkeypatch.setattr(retrieval,'MAX_TEXT_BYTES',1)
    with pytest.raises((IndexUnavailable,database.VaultDatabaseUnavailable)): service.index_pages(did,pages)
    monkeypatch.setattr(retrieval,'MAX_TEXT_BYTES',16*1024*1024)
    assert service.search(request())==before


def test_missing_keyword_manifest_fails_closed(vault):
    _,store,service=vault; add(service)
    with store.connection() as connection: connection.execute('DELETE FROM keyword_indexes')
    with pytest.raises((IndexUnavailable,database.VaultDatabaseUnavailable)): service.search(request())


def test_changed_configuration_rejects_cached_index(vault):
    _,store,service=vault; add(service)
    with store.connection() as connection: connection.execute("UPDATE vault_info SET embedding_config='{}'")
    with pytest.raises((IndexUnavailable,database.VaultDatabaseUnavailable)): service.search(request())
    assert store._snapshot is None


def test_lock_waits_for_active_connection(vault):
    import threading
    from concurrent.futures import ThreadPoolExecutor,TimeoutError
    owner,store,_=vault
    entered=threading.Event(); release=threading.Event(); locking=threading.Event()
    def operation():
        with store.connection() as connection:
            entered.set()
            assert release.wait(5)
            connection.execute('SELECT count(*) FROM documents')
    def lock(): locking.set(); owner.lock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        active=pool.submit(operation); assert entered.wait(5)
        future=pool.submit(lock); assert locking.wait(5)
        try:
            with pytest.raises(TimeoutError): future.result(timeout=0.05)
        finally: release.set()
        active.result(timeout=5); future.result(timeout=5)
    assert store._closed and store._snapshot is None


def test_failed_refresh_cannot_return_old_results(vault,monkeypatch):
    _,store,service=vault; did,_=add(service)
    original=service.vectors._load
    monkeypatch.setattr(service.vectors,'_load',lambda config: (_ for _ in ()).throw(IndexUnavailable('generated failure')))
    pages=[ExtractedPage(page_number=4,text='garden',character_count=6)]
    with pytest.raises(IndexUnavailable): service.index_pages(did,pages)
    assert store._snapshot is None
    monkeypatch.setattr(service.vectors,'_load',original)
    assert service.search(request(),'keyword')['results']==[]
    assert service.search(request('garden'))['results'][0]['page_number']==4


@pytest.mark.parametrize('mode',['DELETE','WAL'])
def test_journal_wal_no_plaintext_canaries(vault,mode):
    _,store,service=vault; did,_=add(service)
    with store.connection() as connection:
        connection.execute('PRAGMA journal_mode='+mode)
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE documents SET original_filename=? WHERE id=?',(CANARY+'changed',did))
        if mode=='WAL': connection.commit()
        companion=store.path.with_name(store.path.name+('-wal' if mode=='WAL' else '-journal'))
        assert companion.exists() and companion.stat().st_size>0
        assert CANARY.encode() not in companion.read_bytes()
        assert CANARY.encode() not in store.path.read_bytes()
        if mode=='DELETE': connection.rollback()


def test_locked_http_remains_blocked(vault,monkeypatch):
    owner,_,_=vault
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv('RECALLIQ_VAULT_MODE','external')
    monkeypatch.setenv('RECALLIQ_VAULT_ROOT',str(owner._root))
    for name in ('RECALLIQ_DATABASE_PATH','RECALLIQ_UPLOADS_DIRECTORY','RECALLIQ_VECTOR_DIRECTORY','RECALLIQ_TEMP_DIRECTORY','RECALLIQ_RECOVERY_DIRECTORY'):
        monkeypatch.delenv(name,raising=False)
    with TestClient(app) as client:
        assert client.post('/documents/upload',content=b'generated rejected body').status_code==503
        for mode in ('semantic','keyword','hybrid'):
            assert client.post('/search/'+mode,json={'query':'solar'}).status_code==503


def test_no_plaintext_artifacts_or_secret_output(vault,capsys,caplog):
    owner,store,service=vault; add(service)
    files=list(owner._root.iterdir())
    assert set(p.name for p in files)<={'key-envelope.json','database.sqlite3','database.sqlite3-wal','database.sqlite3-shm'}
    assert all(CANARY.encode() not in p.read_bytes() for p in files)
    for mode in ('semantic','keyword','hybrid'): service.search(request(),mode)
    assert caplog.text==''
    out=capsys.readouterr(); assert out.out==out.err==''


def test_many_documents_do_not_exceed_sql_expression_depth(vault):
    import hashlib
    from app.keyword import write_keyword_chunks
    _,store,service=vault
    with store.connection() as connection:
        for _ in range(1001):
            did=str(uuid4())
            page=ExtractedPage(page_number=1,text='solar',character_count=5)
            chunks=chunk_pages(did,[page])
            generation=hashlib.sha256(store.config_json.encode()+chunks[0].chunk_id.encode()).hexdigest()
            connection.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                               (did,'generated.pdf',did+'.pdf',0,1,5,'2026-10-11T00:00:00Z'))
            write_keyword_chunks(connection,did,generation,chunks)
            connection.execute('INSERT INTO document_indexes VALUES(?,?,?)',(did,generation,store.config_json))
            vector=retrieval.vector_array([1,0,0.1],3).tobytes()
            connection.execute('INSERT INTO embeddings SELECT rowid,1,3,? FROM chunks WHERE document_id=?',(vector,did))
    result=service.search(request(),'keyword')['results']
    assert len(result)==5 and all(r['chunk_text']=='solar' for r in result)
