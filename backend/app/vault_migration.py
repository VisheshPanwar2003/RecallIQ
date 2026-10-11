"""Explicit offline migration with local re-embedding; never imports Chroma.

All plans, progress, ownership, and intermediate embeddings reside in SQLCipher.
Each document's PDF, metadata, pages, chunks, and vectors become visible in one
SQL transaction. Task 10 publishes authenticated ciphertext before that commit.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import UUID, uuid4

from app.database import initialize_schema
from app.chunking import chunk_pages
from app.migration_source import ReadOnlySource, MigrationError, fingerprint
from app.pdf_extraction import extract_pdf_bytes
from app.storage import validate_path
from app.vault_document_format import document_id, MAX_PDF_BYTES, HEADER, seal_pdf
from app.vault_documents import VaultDocumentService
from app.vault_memory import operation_reservation
from app.vault_search import MAX_CHUNKS, MAX_TEXT_BYTES, vector_array, NumpyVectorIndex
from app.schemas import SemanticSearchRequest
from app.keyword import literal_query


MIGRATION_SCHEMA_VERSION = 4
MIGRATION_SCHEMA = (
    '''CREATE TABLE vault_migrations(
        migration_id TEXT PRIMARY KEY,format_version INTEGER NOT NULL CHECK(format_version=1),
        state TEXT NOT NULL CHECK(state IN ('COPYING','VERIFYING','VERIFIED','INTERRUPTED','FAILED','ROLLING_BACK','ROLLED_BACK')),
        source_database_hash TEXT NOT NULL, inventory TEXT NOT NULL,
        embedding_config TEXT NOT NULL, embedding_strategy TEXT NOT NULL CHECK(embedding_strategy='local-regeneration'))''',
    '''CREATE TABLE vault_migration_documents(
        document_id TEXT PRIMARY KEY,migration_id TEXT NOT NULL REFERENCES vault_migrations(migration_id),
        format_version INTEGER NOT NULL CHECK(format_version=1),plan TEXT NOT NULL,plan_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PLANNED','PREPARED','VERIFIED','ROLLBACK_PENDING','ROLLED_BACK')),
        ciphertext BLOB)''',
    '''CREATE TABLE vault_migration_vectors(
        document_id TEXT REFERENCES vault_migration_documents(document_id),chunk_id TEXT NOT NULL,
        chunk_order INTEGER NOT NULL,vector BLOB NOT NULL,
        PRIMARY KEY(document_id,chunk_id),UNIQUE(document_id,chunk_order))''',
    '''CREATE TABLE vault_migration_nonce_history(
        nonce BLOB PRIMARY KEY CHECK(length(nonce)=12))''',
)


def _normalized_sql(sql):
    return re.sub(r'\s+', ' ', sql or '').strip().lower()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_schema(connection):
    # Compare against this exact supported application schema, including FTS
    # shadows/triggers, without running initialization against the source.
    expected = sqlite3.connect(':memory:')
    try:
        initialize_schema(expected)
        def schema(c):
            return {row[0]: (row[1], ' '.join((row[2] or '').split())) for row in c.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if schema(connection) != schema(expected):
            raise MigrationError('unsupported_source_schema')
        if [r[0] for r in connection.execute('SELECT version FROM schema_migrations')] != [1]:
            raise MigrationError('unsupported_source_schema')
    finally:
        expected.close()


def read_plan(source, doc_id):
    c = source.connection
    row = c.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
    if row is None:
        raise MigrationError('missing_document')
    metadata = dict(row)
    document_id(doc_id)
    filename = metadata['original_filename']
    if not isinstance(filename, str) or not filename.lower().endswith('.pdf') or len(filename.encode()) > 255 or any(x in filename for x in ('/', '\\', '\x00')):
        raise MigrationError('invalid_metadata')
    datetime.fromisoformat(metadata['created_at'])
    if metadata['stored_filename'] != doc_id + '.pdf':
        raise MigrationError('invalid_metadata')
    path = source.hold(validate_path(source.uploads / metadata['stored_filename'], root=source.uploads))
    if not 1 <= path.stat().st_size <= MAX_PDF_BYTES:
        raise MigrationError('pdf_size')
    raw = path.read_bytes()
    if len(raw) != metadata['file_size_bytes'] or not raw.startswith(b'%PDF-'):
        raise MigrationError('pdf_metadata')
    pages = extract_pdf_bytes(raw, bounded=True)
    if len(pages) != metadata['total_pages'] or sum(p.character_count for p in pages) != metadata['total_characters']:
        raise MigrationError('extraction_mismatch')
    chunks = [dict(r) for r in c.execute('''SELECT chunk_id,document_id,generation,page_number,chunk_order,text
        FROM chunks WHERE document_id=? ORDER BY chunk_order''', (doc_id,))]
    keyword = c.execute('SELECT * FROM keyword_indexes WHERE document_id=?', (doc_id,)).fetchone()
    semantic = c.execute('SELECT * FROM document_indexes WHERE document_id=?', (doc_id,)).fetchone()
    if not chunks or not keyword or not semantic or keyword['generation'] != semantic['generation'] or len(chunks) != keyword['chunk_count']:
        raise MigrationError('source_index_incomplete')
    config = json.loads(semantic['embedding_config'])
    if not isinstance(config, dict) or not isinstance(config.get('model'), str) or type(config.get('dimensions')) is not int:
        raise MigrationError('source_model_config')
    ids = set()
    for order, chunk in enumerate(chunks):
        cid = chunk['chunk_id']
        if str(UUID(cid)) != cid or UUID(cid).version != 5 or cid in ids or chunk['chunk_order'] != order or chunk['generation'] != semantic['generation'] or not 1 <= chunk['page_number'] <= len(pages) or not chunk['text'] or len(chunk['text']) > 800 or chunk['text'] not in pages[chunk['page_number']-1].text.strip():
            raise MigrationError('source_chunk_integrity')
        ids.add(cid)
    generation = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for chunk in chunks:
        generation.update(chunk['chunk_id'].encode())
    if generation.hexdigest() != semantic['generation']:
        raise MigrationError('source_manifest_integrity')
    expected_chunks = chunk_pages(doc_id, pages)
    if [dict(chunk_id=x.chunk_id, document_id=x.document_id, generation=semantic['generation'],
             page_number=x.page_number, chunk_order=x.chunk_order, text=x.text)
        for x in expected_chunks] != chunks:
        raise MigrationError('source_chunk_inventory')
    return dict(version=1, metadata=metadata, pages=[p.model_dump() for p in pages],
                chunks=chunks, source_config=config, pdf_hash=hashlib.sha256(raw).hexdigest())


@contextmanager
def preflight(store, database, uploads):
    """No destination writes; bounded source inventory held until caller exits."""
    try:
        with operation_reservation(store.owner), ReadOnlySource(database, uploads, store.owner._root) as source:
            source_schema(source.connection)
            if source.database.stat().st_size > 128 * 1024 * 1024:
                raise MigrationError('source_database_limit')
            if source.connection.execute('SELECT count(*) FROM vector_cleanup').fetchone()[0]:
                raise MigrationError('source_cleanup_pending')
            if source.connection.execute('SELECT count(*) FROM chunks').fetchone()[0] > MAX_CHUNKS:
                raise MigrationError('chunk_limit')
            ids = [r[0] for r in source.connection.execute('SELECT id FROM documents ORDER BY id')]
            if not ids or len(ids) > MAX_CHUNKS:
                raise MigrationError('source_inventory')
            inventory, text_bytes, page_bytes, pdf_bytes = {}, 0, 0, 0
            for doc_id in ids:
                plan = read_plan(source, doc_id)
                text_bytes += sum(len(x['text'].encode()) + len(plan['metadata']['original_filename'].encode()) for x in plan['chunks'])
                page_bytes += sum(len(x['text'].encode()) for x in plan['pages'])
                pdf_bytes += plan['metadata']['file_size_bytes']
                inventory[doc_id] = digest(plan)
                if text_bytes > MAX_TEXT_BYTES or page_bytes > MAX_TEXT_BYTES:
                    raise MigrationError('text_limit')
            with store.connection() as c:
                # Existing source IDs may be verified records during resume.
                # Ownership/conflicts are checked before execution, not guessed.
                slots = ','.join('?' for _ in ids)
                existing = c.execute(f'''SELECT count(*),coalesce(sum(length(CAST(ch.text AS BLOB))+length(CAST(d.original_filename AS BLOB))),0)
                    FROM chunks ch JOIN documents d ON d.id=ch.document_id
                    WHERE ch.document_id NOT IN ({slots})''', ids).fetchone()
                if existing[0] + source.connection.execute('SELECT count(*) FROM chunks').fetchone()[0] > MAX_CHUNKS or existing[1] + text_bytes > MAX_TEXT_BYTES:
                    raise MigrationError('destination_capacity')
            # Ciphertext is retained in SQLCipher until verification completes;
            # budget a second PDF copy plus generous transaction/journal slack.
            if shutil.disk_usage(store.owner._root).free < 3 * pdf_bytes + 8 * page_bytes + 64 * 1024 * 1024:
                raise MigrationError('destination_space')
            source.verify_unchanged()
            yield source, inventory
            source.verify_unchanged()
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('preflight_failed') from None


class MigrationDocuments(VaultDocumentService):
    """Recovery uses encrypted plans; ordinary Task 10 recovery fails closed."""
    def _commit_ingest(self, c, doc_id):
        record = c.execute('SELECT * FROM vault_migration_documents WHERE document_id=?', (doc_id,)).fetchone()
        plan = load_plan(record)
        metadata = plan['metadata']
        c.execute('UPDATE documents SET created_at=? WHERE id=?', (metadata['created_at'], doc_id))
        generation = hashlib.sha256(self.store.config_json.encode())
        for chunk in plan['chunks']:
            generation.update(chunk['chunk_id'].encode())
        generation = generation.hexdigest()
        arrays = c.execute('SELECT chunk_id,vector FROM vault_migration_vectors WHERE document_id=? ORDER BY chunk_order', (doc_id,)).fetchall()
        if [r['chunk_id'] for r in arrays] != [r['chunk_id'] for r in plan['chunks']]:
            raise MigrationError('embedding_incomplete')
        for chunk, array in zip(plan['chunks'], arrays):
            import numpy as np
            vector = vector_array(np.frombuffer(array['vector'], dtype='<f4'), self.store.dimensions)
            if len(array['vector']) != self.store.dimensions * 4:
                raise MigrationError('embedding_format')
            cursor = c.execute('''INSERT INTO chunks(chunk_id,document_id,generation,page_number,chunk_order,text)
                VALUES(?,?,?,?,?,?)''', (chunk['chunk_id'], doc_id, generation, chunk['page_number'], chunk['chunk_order'], chunk['text']))
            c.execute('INSERT INTO embeddings VALUES(?,1,?,?)', (cursor.lastrowid, self.store.dimensions, vector.tobytes()))
        c.execute('INSERT INTO keyword_indexes VALUES(?,?,?)', (doc_id, generation, len(arrays)))
        c.execute('INSERT INTO document_indexes VALUES(?,?,?)', (doc_id, generation, self.store.config_json))
        # FTS external-content consistency check, encrypted DB integrity, exact
        # records and authenticated PDF are checked before the visibility commit.
        c.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
        if c.execute('PRAGMA foreign_key_check').fetchall():
            raise MigrationError('destination_relationships')
        validate_records(c, plan, self.store.config_json)
        row = self._row(doc_id)
        raw = self._verified(doc_id, self._path(doc_id), row, decrypt=True)
        if hashlib.sha256(raw).hexdigest() != plan['pdf_hash']:
            raise MigrationError('destination_pdf_hash')
        self._checkpoint('migration_before_visible')
        c.execute("UPDATE vault_migration_documents SET state='VERIFIED' WHERE document_id=?", (doc_id,))


def load_plan(record):
    if record is None or record['format_version'] != 1 or len(record['plan'].encode()) > 32 * 1024 * 1024:
        raise MigrationError('invalid_migration_plan')
    try:
        plan = json.loads(record['plan'])
        if set(plan) != {'version','metadata','pages','chunks','source_config','pdf_hash'} or plan['version'] != 1 or digest(plan) != record['plan_hash'] or plan['metadata']['id'] != record['document_id']:
            raise ValueError()
        document_id(record['document_id'])
        return plan
    except Exception:
        raise MigrationError('invalid_migration_plan') from None


def validate_records(c, plan, config_json):
    doc_id = plan['metadata']['id']
    row = c.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
    if row is None or dict(row) != plan['metadata']:
        raise MigrationError('destination_metadata')
    pages = [dict(r) for r in c.execute('SELECT page_number,text,characters FROM vault_document_pages WHERE document_id=? ORDER BY page_number', (doc_id,))]
    expected = [dict(page_number=p['page_number'], text=p['text'], characters=p['character_count']) for p in plan['pages']]
    if pages != expected:
        raise MigrationError('destination_pages')
    chunks = [dict(r) for r in c.execute('SELECT chunk_id,document_id,page_number,chunk_order,text FROM chunks WHERE document_id=? ORDER BY chunk_order', (doc_id,))]
    if chunks != [{k:v for k,v in chunk.items() if k != 'generation'} for chunk in plan['chunks']]:
        raise MigrationError('destination_chunks')
    manifest = c.execute('SELECT embedding_config FROM document_indexes WHERE document_id=?', (doc_id,)).fetchone()
    if not manifest or manifest[0] != config_json:
        raise MigrationError('destination_manifest')


class VaultMigration:
    def __init__(self, store, embedder):
        self.store, self.owner, self.embedder = store, store.owner, embedder
        if json.dumps(embedder.config, sort_keys=True) != store.config_json:
            raise MigrationError('model_config')

    def _schema(self):
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            versions = [row[0] for row in c.execute(
                'SELECT version FROM vault_schema_migrations ORDER BY version')]
            names = ('vault_migrations', 'vault_migration_documents',
                     'vault_migration_vectors', 'vault_migration_nonce_history')
            found = {row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", names)}
            if MIGRATION_SCHEMA_VERSION in versions:
                actual = {name: _normalized_sql(c.execute(
                    'SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)
                ).fetchone()[0]) for name in names if c.execute(
                    'SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)
                ).fetchone()}
                expected = {}
                probe = sqlite3.connect(':memory:')
                try:
                    for statement in MIGRATION_SCHEMA:
                        probe.execute(statement)
                    expected = {row[0]: _normalized_sql(row[1]) for row in probe.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='table'")}
                finally:
                    probe.close()
                if set(actual) != set(names) or any(actual[name] != expected[name] for name in names):
                    raise MigrationError('unsupported_migration_schema')
                return
            if versions != [1, 2, 3] or found:
                raise MigrationError('unsupported_migration_schema')
            for statement in MIGRATION_SCHEMA:
                c.execute(statement)
            c.execute('INSERT INTO vault_schema_migrations(version) VALUES(?)',
                      (MIGRATION_SCHEMA_VERSION,))

    def dry_run(self, database, uploads):
        with preflight(self.store, database, uploads) as (source, inventory):
            with self.store.connection() as c:
                tables = {row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for doc_id in inventory:
                    if c.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone():
                        raise MigrationError('destination_conflict')
                    if 'vault_document_nonces' in tables and c.execute(
                        'SELECT 1 FROM vault_document_nonces WHERE document_id=?', (doc_id,)
                    ).fetchone():
                        raise MigrationError('destination_conflict')
                    if 'vault_migration_documents' in tables and c.execute(
                        'SELECT 1 FROM vault_migration_documents WHERE document_id=?', (doc_id,)
                    ).fetchone():
                        raise MigrationError('destination_conflict')
                    for folder, suffix in (('documents', '.rpdf'), ('staging', '.rpdf.pending')):
                        candidate = validate_path(self.owner._root / folder / (doc_id + suffix),
                                                  root=self.owner._root)
                        if candidate.exists():
                            raise MigrationError('destination_conflict')
            for doc_id in inventory:
                if read_plan(source, doc_id)['source_config'] != self.embedder.config:
                    raise MigrationError('source_model_config')
            try:
                probe = self.embedder.encode(['RecallIQ offline migration preflight'])
                if len(probe) != 1:
                    raise ValueError()
                vector_array(probe[0], self.store.dimensions)
            except Exception:
                raise MigrationError('local_embedding_unavailable') from None
            return {'status':'PREFLIGHT', 'documents':len(inventory), 'source_schema':1,
                    'destination_schema':MIGRATION_SCHEMA_VERSION, 'embedding_strategy':'local-regeneration'}

    def execute(self, database, uploads, *, migration_id=None):
        try:
            with preflight(self.store, database, uploads) as (source, inventory):
                for doc_id in inventory:
                    if read_plan(source, doc_id)['source_config'] != self.embedder.config:
                        raise MigrationError('source_model_config')
                service = MigrationDocuments(self.store)
                self._schema()
                mid = str(UUID(migration_id)) if migration_id is not None else str(uuid4())
                with self.store.connection() as c:
                    existing = c.execute('SELECT * FROM vault_migrations WHERE migration_id=?', (mid,)).fetchone()
                    if existing:
                        if existing['format_version'] != 1 or existing['inventory'] != canonical(inventory) or existing['source_database_hash'] != source.files[source.database][2] or existing['embedding_config'] != self.store.config_json or existing['state'] in ('ROLLING_BACK','ROLLED_BACK'):
                            raise MigrationError('resume_incompatible')
                        owned = c.execute('SELECT document_id,plan_hash FROM vault_migration_documents WHERE migration_id=?', (mid,)).fetchall()
                        if {row['document_id'] for row in owned} != set(inventory) or any(
                            row['plan_hash'] != inventory[row['document_id']] for row in owned
                        ):
                            raise MigrationError('resume_incompatible')
                        for row in owned:
                            load_plan(c.execute(
                                'SELECT * FROM vault_migration_documents WHERE document_id=?',
                                (row['document_id'],),
                            ).fetchone())
                    else:
                        # Validate all ownership before creating any records.
                        for doc_id in inventory:
                            if c.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone() or c.execute('SELECT 1 FROM vault_document_nonces WHERE document_id=?', (doc_id,)).fetchone() or c.execute('SELECT 1 FROM vault_migration_documents WHERE document_id=?', (doc_id,)).fetchone() or service._path(doc_id).exists() or service._path(doc_id, True).exists():
                                raise MigrationError('destination_conflict')
                        c.execute('INSERT INTO vault_migrations VALUES(?,1,\'COPYING\',?,?,?,\'local-regeneration\')',
                                  (mid, source.files[source.database][2], canonical(inventory), self.store.config_json))
                        for doc_id in inventory:
                            plan = read_plan(source, doc_id)
                            c.execute("INSERT INTO vault_migration_documents VALUES(?,?,1,?,?,'PLANNED',NULL)",
                                      (doc_id, mid, canonical(plan), inventory[doc_id]))
                self._checkpoint('manifest', mid)
                for doc_id in inventory:
                    source.verify_unchanged()
                    self._copy(source, service, doc_id)
                self._checkpoint('before_verification', mid)
                with self.store.connection() as c:
                    c.execute("UPDATE vault_migrations SET state='VERIFYING' WHERE migration_id=?", (mid,))
                return self.verify(mid)
        except MigrationError:
            if 'mid' in locals():
                self._mark_interrupted(mid)
            raise
        except Exception:
            if 'mid' in locals():
                self._mark_interrupted(mid)
            raise MigrationError('migration_incomplete') from None

    def _mark_interrupted(self, mid):
        try:
            with self.store.connection() as c:
                c.execute("UPDATE vault_migrations SET state='INTERRUPTED' WHERE migration_id=? AND state NOT IN ('VERIFIED','ROLLING_BACK','ROLLED_BACK')", (mid,))
        except Exception:
            # No claimed recovery: the durable prior state remains authoritative.
            pass

    def _checkpoint(self, transition, migration_id):
        """Fault injection; no content or secret arguments."""

    def _copy(self, source, service, doc_id):
        with self.store.connection() as c:
            record = c.execute('SELECT * FROM vault_migration_documents WHERE document_id=?', (doc_id,)).fetchone()
            plan = load_plan(record)
            state = record['state']
        if digest(read_plan(source, doc_id)) != record['plan_hash']:
            raise MigrationError('source_changed')
        if state == 'VERIFIED':
            return
        if state not in ('PLANNED','PREPARED'):
            raise MigrationError('unknown_recovery_state')
        if state == 'PLANNED':
            # Batch generation, encrypted intermediate persistence. Resume uses
            # only a complete, identity-checked batch; no plaintext vector cache.
            with self.store.connection() as c:
                c.execute('DELETE FROM vault_migration_vectors WHERE document_id=?', (doc_id,))
            for start in range(0, len(plan['chunks']), 32):
                batch = plan['chunks'][start:start+32]
                encoded = self.embedder.encode([x['text'] for x in batch])
                if len(encoded) != len(batch):
                    raise MigrationError('embedding_incomplete')
                with self.store.connection() as c:
                    for chunk, value in zip(batch, encoded):
                        array = vector_array(value, self.store.dimensions)
                        c.execute('INSERT INTO vault_migration_vectors VALUES(?,?,?,?)', (doc_id, chunk['chunk_id'], chunk['chunk_order'], array.tobytes()))
                self._checkpoint('embedding_batch', record['migration_id'])
            raw = (source.uploads / plan['metadata']['stored_filename']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != plan['pdf_hash']:
                raise MigrationError('source_changed')
            nonce = os.urandom(12)
            with self.store.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT 1 FROM vault_migration_nonce_history WHERE nonce=?', (nonce,)).fetchone() or c.execute('SELECT 1 FROM vault_document_nonces WHERE nonce=?', (nonce,)).fetchone():
                    raise MigrationError('nonce_collision')
                c.execute('INSERT INTO vault_migration_nonce_history VALUES(?)', (nonce,))
                row = c.execute('SELECT state FROM vault_document_operations WHERE document_id=?', (doc_id,)).fetchone()
                if row is None:
                    c.execute('INSERT INTO vault_document_nonces VALUES(?,?)', (nonce, doc_id))
                    c.execute("INSERT INTO vault_document_operations(document_id,kind,state,nonce) VALUES(?,'INGEST','PREPARING',?)", (doc_id, nonce))
                elif row[0] == 'PREPARING' and not service._path(doc_id).exists() and not service._path(doc_id, True).exists():
                    # An interrupted pre-encryption attempt: old nonce stays in
                    # the immutable history, while the fresh attempt gets a new
                    # nonce. Never encrypt twice with a reserved nonce.
                    c.execute('UPDATE vault_document_nonces SET nonce=? WHERE document_id=?', (nonce, doc_id))
                    c.execute('UPDATE vault_document_operations SET nonce=? WHERE document_id=?', (nonce, doc_id))
                else:
                    raise MigrationError('ambiguous_recovery')
            self._checkpoint('nonce_reserved', record['migration_id'])
            cipher = seal_pdf(self.owner._subkey('document'), self.owner._vault_id, doc_id, nonce, raw)
            del raw
            metadata = plan['metadata']
            with self.store.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO vault_pending_documents VALUES(?,?,?,?,?,?)',
                          (doc_id, metadata['original_filename'], metadata['file_size_bytes'], metadata['total_pages'], metadata['total_characters'], metadata['created_at']))
                c.executemany('INSERT INTO vault_pending_pages VALUES(?,?,?,?)',
                              [(doc_id,p['page_number'],p['text'],p['character_count']) for p in plan['pages']])
                c.execute('UPDATE vault_document_operations SET digest=?,cipher_size=? WHERE document_id=?',
                          (hashlib.sha256(cipher).hexdigest(), len(cipher), doc_id))
                c.execute("UPDATE vault_migration_documents SET state='PREPARED',ciphertext=? WHERE document_id=?", (cipher, doc_id))
            self._checkpoint('prepared', record['migration_id'])
        with self.store.connection() as c:
            record = c.execute('SELECT * FROM vault_migration_documents WHERE document_id=?', (doc_id,)).fetchone()
            cipher = record['ciphertext']
        row = service._row(doc_id)
        final, staged = service._path(doc_id), service._path(doc_id, True)
        if not final.exists():
            if staged.exists():
                if not row['stage_owned']:
                    raise MigrationError('ambiguous_ownership')
                if staged.stat().st_size < row['cipher_size']:
                    partial = staged.read_bytes()
                    if partial != cipher[:len(partial)]:
                        raise MigrationError('staged_tampering')
                    staged.unlink()
                else:
                    service._verified(doc_id, staged, row)
            if not staged.exists():
                fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    with self.store.connection() as c:
                        c.execute('UPDATE vault_document_operations SET stage_owned=1 WHERE document_id=?', (doc_id,))
                    stream.write(cipher); stream.flush(); os.fsync(stream.fileno())
                service._state(doc_id, 'FILE_STAGED')
                self._checkpoint('file_staged', record['migration_id'])
        service._finish_ingest(doc_id)
        self._checkpoint('document_verified', record['migration_id'])

    def verify(self, mid):
        with operation_reservation(self.owner):
            try:
                return self._verify(mid)
            except Exception:
                self.store._snapshot = None
                raise MigrationError('verification_failed') from None

    def _verify(self, mid):
        from app.vault_search import VaultSearchService
        service = MigrationDocuments(self.store)
        with self.store.connection() as c:
            migration = c.execute('SELECT * FROM vault_migrations WHERE migration_id=?', (mid,)).fetchone()
            if not migration or migration['format_version'] != 1 or migration['state'] in ('ROLLING_BACK','ROLLED_BACK') or migration['embedding_config'] != self.store.config_json:
                raise MigrationError('unknown_migration')
            inventory = json.loads(migration['inventory'])
            records = c.execute('SELECT * FROM vault_migration_documents WHERE migration_id=? ORDER BY document_id', (mid,)).fetchall()
            if set(inventory) != {r['document_id'] for r in records} or not records:
                raise MigrationError('manifest_inventory')
            if any(r['state'] != 'VERIFIED' or r['plan_hash'] != inventory[r['document_id']] for r in records):
                raise MigrationError('migration_incomplete')
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA cipher_integrity_check').fetchall() or c.execute('PRAGMA foreign_key_check').fetchall():
                raise MigrationError('destination_integrity')
            c.execute("INSERT INTO chunks_fts(chunks_fts,rank) VALUES('integrity-check',1)")
            for record in records:
                validate_records(c, load_plan(record), self.store.config_json)
        search = VaultSearchService(self.store, self.embedder)
        chunks = 0
        for record in records:
            plan = load_plan(record); doc_id = record['document_id']
            if hashlib.sha256(service._decrypt(doc_id)).hexdigest() != plan['pdf_hash']:
                raise MigrationError('destination_pdf_hash')
            service._recover_one(doc_id)  # Finish only registered staging cleanup.
            chunks += len(plan['chunks'])
            # Source-linked retrieval with explicit document filtering. Semantic
            # ranks are newly generated, never compared with original Chroma.
            query = plan['chunks'][0]['text'][:500]
            request = SemanticSearchRequest(query=query, top_k=20, document_ids=[doc_id])
            for mode in ('semantic','keyword','hybrid'):
                results = search.search(request, mode)['results']
                allowed = {x['chunk_id']: x for x in plan['chunks']}
                if not results and (mode != 'keyword' or literal_query(query)):
                    raise MigrationError('retrieval_verification')
                for result in results:
                    result = result.model_dump() if hasattr(result, 'model_dump') else result
                    cid = str(result['chunk_id'])
                    if str(result['document_id']) != doc_id or cid not in allowed or result['filename'] != plan['metadata']['original_filename'] or result['page_number'] != allowed[cid]['page_number'] or result['chunk_text'] != allowed[cid]['text']:
                        raise MigrationError('retrieval_citation')
        self._checkpoint('verification_complete', mid)
        with self.store.connection() as c:
            c.execute("UPDATE vault_migrations SET state='VERIFIED' WHERE migration_id=?", (mid,))
            c.execute('UPDATE vault_migration_documents SET ciphertext=NULL WHERE migration_id=?', (mid,))
        return {'migration_id':mid, 'status':'VERIFIED','documents':len(records),
                'chunks':chunks,'embeddings':chunks,'embedding_strategy':'local-regeneration',
                'model':self.embedder.config['model'],'dimensions':self.store.dimensions}

    def rollback(self, mid, *, confirmed=False, completed_confirmed=False):
        if confirmed is not True:
            raise MigrationError('rollback_confirmation_required')
        with operation_reservation(self.owner):
            service = MigrationDocuments(self.store)
            with self.store.connection() as c:
                migration = c.execute('SELECT * FROM vault_migrations WHERE migration_id=?', (mid,)).fetchone()
                if not migration or migration['format_version'] != 1:
                    raise MigrationError('unknown_migration')
                if migration['state'] == 'VERIFIED' and completed_confirmed is not True:
                    raise MigrationError('completed_rollback_authorization_required')
                records = c.execute('SELECT * FROM vault_migration_documents WHERE migration_id=?', (mid,)).fetchall()
                inventory = json.loads(migration['inventory'])
                if set(inventory) != {r['document_id'] for r in records} or any(r['plan_hash'] != inventory[r['document_id']] for r in records):
                    raise MigrationError('ambiguous_ownership')
                for record in records:
                    load_plan(record)
            # Establish ownership for every item before making the first
            # destructive change. An unknown row or file stops the whole
            # rollback for manual inspection.
            for record in records:
                self._validate_rollback_ownership(record, service)
            with self.store.connection() as c:
                c.execute("UPDATE vault_migrations SET state='ROLLING_BACK' WHERE migration_id=?", (mid,))
            for record in records:
                doc_id = record['document_id']; row = service._row(doc_id)
                if record['state'] == 'ROLLED_BACK':
                    if row and row['state'] != 'DELETED':
                        raise MigrationError('ambiguous_ownership')
                    continue
                if row is not None:
                    # Both ownership and authenticated content must agree before
                    # logical deletion or ciphertext cleanup. Never file-scan.
                    for staged in (False, True):
                        path = service._path(doc_id, staged)
                        if path.exists():
                            if staged and not row['stage_owned']:
                                raise MigrationError('ambiguous_ownership')
                            service._verified(doc_id, path, row)
                    if row['state'] not in ('PREPARING','FILE_STAGED','FILE_PUBLISHED','COMMITTED','DELETE_PENDING','DELETED'):
                        raise MigrationError('unknown_recovery_state')
                    with self.store.connection() as c:
                        c.execute('BEGIN IMMEDIATE')
                        c.execute('DELETE FROM documents WHERE id=?', (doc_id,))
                        c.execute('DELETE FROM vault_pending_documents WHERE document_id=?', (doc_id,))
                        c.execute("UPDATE vault_document_operations SET kind='DELETE',state='DELETE_PENDING' WHERE document_id=?", (doc_id,))
                        c.execute("UPDATE vault_migration_documents SET state='ROLLBACK_PENDING' WHERE document_id=?", (doc_id,))
                    self.store._snapshot = None
                    service._finish_delete(doc_id)
                elif service._path(doc_id).exists() or service._path(doc_id, True).exists():
                    raise MigrationError('ambiguous_ownership')
                with self.store.connection() as c:
                    c.execute('DELETE FROM vault_migration_vectors WHERE document_id=?', (doc_id,))
                    c.execute("UPDATE vault_migration_documents SET state='ROLLED_BACK',ciphertext=NULL WHERE document_id=?", (doc_id,))
                self._checkpoint('rollback_document', mid)
            with self.store.connection() as c:
                c.execute("UPDATE vault_migrations SET state='ROLLED_BACK' WHERE migration_id=?", (mid,))
            return {'migration_id':mid,'status':'ROLLED_BACK','documents':len(records)}

    def _validate_rollback_ownership(self, record, service):
        doc_id = record['document_id']
        plan = load_plan(record)
        operation = service._row(doc_id)
        final, staged = service._path(doc_id), service._path(doc_id, True)
        final_exists, staged_exists = final.exists(), staged.exists()
        with self.store.connection() as c:
            visible = c.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone()
            file_record = c.execute(
                'SELECT digest,cipher_size,nonce FROM vault_document_files WHERE document_id=?',
                (doc_id,),
            ).fetchone()

        if record['state'] == 'ROLLED_BACK':
            if visible or final_exists or staged_exists or (operation and operation['state'] != 'DELETED'):
                raise MigrationError('ambiguous_ownership')
            return
        if record['state'] == 'VERIFIED':
            if not operation or operation['state'] not in ('COMMITTED', 'DELETE_PENDING', 'DELETED'):
                raise MigrationError('ambiguous_ownership')
            if operation['state'] == 'COMMITTED':
                if not visible or not file_record or dict(file_record) != {
                    'digest': operation['digest'], 'cipher_size': operation['cipher_size'],
                    'nonce': operation['nonce'],
                }:
                    raise MigrationError('ambiguous_ownership')
                with self.store.connection() as c:
                    try:
                        validate_records(c, plan, self.store.config_json)
                    except MigrationError:
                        raise MigrationError('ambiguous_ownership') from None
            elif visible:
                raise MigrationError('ambiguous_ownership')
        elif record['state'] == 'ROLLBACK_PENDING':
            if not operation or operation['state'] not in ('DELETE_PENDING', 'DELETED') or visible:
                raise MigrationError('ambiguous_ownership')
        elif record['state'] in ('PLANNED', 'PREPARED'):
            if visible:
                raise MigrationError('ambiguous_ownership')
            if record['state'] == 'PLANNED':
                if operation and (operation['state'] != 'PREPARING' or operation['digest'] is not None or final_exists or staged_exists):
                    raise MigrationError('ambiguous_ownership')
            elif not operation or operation['state'] not in ('PREPARING', 'FILE_STAGED', 'FILE_PUBLISHED'):
                raise MigrationError('ambiguous_ownership')
        else:
            raise MigrationError('unknown_recovery_state')

        if operation is None:
            if final_exists or staged_exists:
                raise MigrationError('ambiguous_ownership')
            return
        if staged_exists and not operation['stage_owned']:
            raise MigrationError('ambiguous_ownership')
        for path, exists in ((final, final_exists), (staged, staged_exists)):
            if exists:
                if not operation['digest'] or not operation['cipher_size']:
                    raise MigrationError('ambiguous_ownership')
                try:
                    plaintext = service._verified(doc_id, path, operation, decrypt=True)
                except Exception:
                    raise MigrationError('ambiguous_ownership') from None
                if hashlib.sha256(plaintext).hexdigest() != plan['pdf_hash']:
                    raise MigrationError('ambiguous_ownership')
        if operation['state'] == 'COMMITTED' and not final_exists:
            raise MigrationError('ambiguous_ownership')
        if record['state'] == 'PREPARED' and not (final_exists or staged_exists) and not record['ciphertext']:
            raise MigrationError('ambiguous_ownership')
