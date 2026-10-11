"""Internal bounded ingestion. Public vault HTTP routes remain disabled.

SQLCipher pending records are separate from visible documents. A durable UUID /
nonce ledger prevents ID reuse and AES-GCM nonce reuse, including failed work.
Only registered, authenticated ciphertext is eligible for recovery or deletion.
"""
from datetime import datetime, timezone
import hashlib
import os
import math
import time
from uuid import uuid4

from app.pdf_extraction import extract_pdf_bytes
from app.schemas import DocumentUploadResponse
from app.semantic import semantic_lock
from app.storage import validate_path
from app.vault_document_format import (
    EncryptedDocumentError, HEADER, MAGIC, MAX_CONTAINER_BYTES, MAX_PDF_BYTES,
    document_id, inspect_container, open_pdf, seal_pdf,
)
from app.vault_memory import operation_reservation

class VaultDocumentService:
    """Trusted internal interface; caller owns streams and returned RAM bytes.

One process-wide heavy operation is admitted. Lock waits for its key lease;
new leases are denied as soon as lock begins. No HTTP/password APIs are added.
"""
    def __init__(self, store, search=None):
        self.store = store
        self.owner = store.owner
        self.search = search
        with operation_reservation(self.owner):
            self._directories()
            self._schema()

    def _directories(self):
        self.store._check()
        for name in ('documents', 'staging'):
            path = validate_path(self.owner._root / name, root=self.owner._root)
            path.mkdir(mode=0o700, exist_ok=True)
            validate_path(path, root=self.owner._root)

    def _path(self, doc_id, staged=False):
        self.store._check()
        document_id(doc_id)
        folder = 'staging' if staged else 'documents'
        suffix = '.rpdf.pending' if staged else '.rpdf'
        return validate_path(self.owner._root / folder / (doc_id + suffix), root=self.owner._root)

    def _schema(self):
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM vault_schema_migrations WHERE version=3').fetchone():
                # Fail closed if a declared migration is incomplete.
                for table in ('vault_document_operations', 'vault_document_nonces',
                              'vault_pending_documents', 'vault_document_pages', 'vault_document_files'):
                    c.execute('SELECT * FROM ' + table + ' LIMIT 0')
                return
            c.execute('''CREATE TABLE vault_document_nonces(
                nonce BLOB PRIMARY KEY CHECK(length(nonce)=12), document_id TEXT UNIQUE NOT NULL)''')
            c.execute('''CREATE TABLE vault_document_operations(
                document_id TEXT PRIMARY KEY REFERENCES vault_document_nonces(document_id),
                kind TEXT NOT NULL CHECK(kind IN ('INGEST','DELETE')),
                state TEXT NOT NULL CHECK(state IN ('PREPARING','FILE_STAGED','FILE_PUBLISHED',
                    'COMMITTED','FAILED','DELETE_PENDING','DELETED')),
                nonce BLOB NOT NULL CHECK(length(nonce)=12),
                digest TEXT CHECK(digest IS NULL OR length(digest)=64),
                cipher_size INTEGER CHECK(cipher_size IS NULL OR cipher_size BETWEEN 78 AND 20971597),
                stage_owned INTEGER NOT NULL DEFAULT 0 CHECK(stage_owned IN (0,1)))''')
            c.execute('''CREATE TABLE vault_pending_documents(
                document_id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                pages INTEGER NOT NULL, characters INTEGER NOT NULL, created_at TEXT NOT NULL)''')
            c.execute('''CREATE TABLE vault_pending_pages(
                document_id TEXT REFERENCES vault_pending_documents(document_id) ON DELETE CASCADE,
                page_number INTEGER, text TEXT NOT NULL, characters INTEGER NOT NULL,
                PRIMARY KEY(document_id,page_number))''')
            c.execute('''CREATE TABLE vault_document_pages(
                document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                page_number INTEGER, text TEXT NOT NULL, characters INTEGER NOT NULL,
                PRIMARY KEY(document_id,page_number))''')
            c.execute('''CREATE TABLE vault_document_files(
                document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                digest TEXT NOT NULL, cipher_size INTEGER NOT NULL, nonce BLOB NOT NULL)''')
            c.execute('INSERT INTO vault_schema_migrations VALUES(3)')

    def _checkpoint(self, transition):
        """Fault-injection seam; never receives keys, filenames or document text."""

    def _row(self, doc_id):
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM vault_document_operations WHERE document_id=?', (doc_id,)).fetchone()
            return dict(row) if row else None

    def _state(self, doc_id, state):
        with self.store.connection() as c:
            c.execute('UPDATE vault_document_operations SET state=? WHERE document_id=?', (state, doc_id))

    def _read_cipher(self, path):
        validate_path(path, root=self.owner._root)
        if not path.is_file() or not 1 <= path.stat().st_size <= MAX_CONTAINER_BYTES:
            raise EncryptedDocumentError()
        with path.open('rb') as stream:
            data = stream.read(MAX_CONTAINER_BYTES + 1)
        if len(data) > MAX_CONTAINER_BYTES:
            raise EncryptedDocumentError()
        return data

    def _verified(self, doc_id, path, row, *, decrypt=False):
        data = self._read_cipher(path)
        if len(data) != row['cipher_size'] or hashlib.sha256(data).hexdigest() != row['digest']:
            raise EncryptedDocumentError()
        if inspect_container(data, self.owner._vault_id, doc_id) != row['nonce']:
            raise EncryptedDocumentError()
        plaintext = open_pdf(self.owner._subkey('document'), self.owner._vault_id, doc_id, data)
        return plaintext if decrypt else None

    def _unlink_verified(self, doc_id, path, row):
        if path.exists():
            self._verified(doc_id, path, row)
            validate_path(path, root=self.owner._root).unlink()

    def ingest(self, stream, filename, *, expected_size=None, cancel=None, timeout=30,
               _document_id=None):
        """Read trusted BinaryIO, bounded and cooperative. Never multipart-parse.

Timeout/cancellation are checked between reads; a blocking stream must provide
its own read deadline. No promise of interrupting arbitrary synchronous I/O.
"""
        with operation_reservation(self.owner):
            try:
                return self._ingest(stream, filename, expected_size, cancel, timeout, _document_id)
            except Exception:
                raise EncryptedDocumentError() from None

    def _ingest(self, stream, filename, expected_size, cancel, timeout, requested_id):
        if not isinstance(filename, str) or not filename.lower().endswith('.pdf') or len(filename.encode('utf-8')) > 255 or any(x in filename for x in ('/', '\\', '\x00')):
            raise EncryptedDocumentError()
        if expected_size is not None and (type(expected_size) is not int or not 1 <= expected_size <= MAX_PDF_BYTES):
            raise EncryptedDocumentError()
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise EncryptedDocumentError()
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while True:
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                raise EncryptedDocumentError()
            part = stream.read(min(65536, MAX_PDF_BYTES + 1 - len(buffer)))
            if type(part) is not bytes or len(part) > min(65536, MAX_PDF_BYTES + 1 - len(buffer)):
                raise EncryptedDocumentError()
            if not part:
                break
            buffer.extend(part)
            if len(buffer) > MAX_PDF_BYTES:
                raise EncryptedDocumentError()
        if not buffer.startswith(b'%PDF-') or (expected_size is not None and len(buffer) != expected_size):
            raise EncryptedDocumentError()
        data = bytes(buffer)
        del buffer
        pages = extract_pdf_bytes(data, bounded=True)
        if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
            raise EncryptedDocumentError()
        doc_id = document_id(requested_id) if requested_id is not None else str(uuid4())
        staged, final = self._path(doc_id, True), self._path(doc_id)
        if staged.exists() or final.exists():
            raise EncryptedDocumentError()
        nonce = os.urandom(12)
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone():
                raise EncryptedDocumentError()
            # Uniqueness is checked before ANY AES encryption. Failed attempts
            # retain their nonce and ID reservations permanently.
            c.execute('INSERT INTO vault_document_nonces VALUES(?,?)', (nonce, doc_id))
            c.execute("INSERT INTO vault_document_operations(document_id,kind,state,nonce) VALUES(?,'INGEST','PREPARING',?)", (doc_id, nonce))
        self._checkpoint('reserved')
        ciphertext = seal_pdf(self.owner._subkey('document'), self.owner._vault_id, doc_id, nonce, data)
        del data
        digest = hashlib.sha256(ciphertext).hexdigest()
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO vault_pending_documents VALUES(?,?,?,?,?,?)',
                      (doc_id, filename, len(ciphertext) - HEADER.size - 16, len(pages), sum(p.character_count for p in pages), datetime.now(timezone.utc).isoformat()))
            c.executemany('INSERT INTO vault_pending_pages VALUES(?,?,?,?)',
                          [(doc_id, p.page_number, p.text, p.character_count) for p in pages])
            c.execute('UPDATE vault_document_operations SET digest=?,cipher_size=? WHERE document_id=?', (digest, len(ciphertext), doc_id))
        self._checkpoint('prepared')
        fd = os.open(self._path(doc_id, True), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            # Own only an exclusively created file. If this commit fails, leave
            # the ambiguous file untouched; recovery reports it for inspection.
            with self.store.connection() as c:
                c.execute('UPDATE vault_document_operations SET stage_owned=1 WHERE document_id=?', (doc_id,))
            out.write(ciphertext)
            out.flush()
            os.fsync(out.fileno())
        del ciphertext
        self._checkpoint('staged_unmarked')
        self._state(doc_id, 'FILE_STAGED')
        self._checkpoint('staged')
        self._finish_ingest(doc_id)
        return DocumentUploadResponse(document_id=doc_id, filename=filename, total_pages=len(pages),
                                      total_characters=sum(p.character_count for p in pages), pages=pages)

    def _finish_ingest(self, doc_id):
        row = self._row(doc_id)
        staged, final = self._path(doc_id, True), self._path(doc_id)
        if final.exists():
            self._verified(doc_id, final, row)
        else:
            self._verified(doc_id, staged, row)
            os.link(self._path(doc_id, True), self._path(doc_id))
        self._checkpoint('published_unmarked')
        self._state(doc_id, 'FILE_PUBLISHED')
        self._checkpoint('published')
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            pending = c.execute('SELECT * FROM vault_pending_documents WHERE document_id=?', (doc_id,)).fetchone()
            if pending is None:
                raise EncryptedDocumentError()
            # stored_filename is an existing logical schema field, not a path;
            # the trusted physical path is ALWAYS derived as UUID.rpdf above.
            c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                      (doc_id, pending['filename'], doc_id + '.pdf', pending['size'], pending['pages'], pending['characters'], pending['created_at']))
            c.execute('INSERT INTO vault_document_pages SELECT * FROM vault_pending_pages WHERE document_id=?', (doc_id,))
            c.execute('INSERT INTO vault_document_files VALUES(?,?,?,?)', (doc_id, row['digest'], row['cipher_size'], row['nonce']))
            self._commit_ingest(c, doc_id)
            c.execute('DELETE FROM vault_pending_documents WHERE document_id=?', (doc_id,))
            c.execute("UPDATE vault_document_operations SET state='COMMITTED' WHERE document_id=?", (doc_id,))
            self._checkpoint('before_commit')
        self._checkpoint('committed')
        self._unlink_verified(doc_id, self._path(doc_id, True), row)
        with self.store.connection() as c:
            c.execute('UPDATE vault_document_operations SET stage_owned=0 WHERE document_id=?', (doc_id,))
        self._checkpoint('cleaned')

    def _commit_ingest(self, connection, doc_id):
        """Internal transaction seam: migration must verify its indexes first.

        Ordinary recovery cannot accidentally publish a migration-owned PDF
        without its preserved chunks and regenerated embeddings.
        """
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='vault_migration_documents'").fetchone():
            if connection.execute('SELECT 1 FROM vault_migration_documents WHERE document_id=?', (doc_id,)).fetchone():
                raise EncryptedDocumentError()

    def decrypt(self, doc_id):
        with operation_reservation(self.owner):
            try:
                return self._decrypt(document_id(doc_id))
            except Exception:
                raise EncryptedDocumentError() from None

    def _decrypt(self, doc_id):
        with self.store.connection() as c:
            row = c.execute('SELECT * FROM vault_document_files WHERE document_id=?', (doc_id,)).fetchone()
            if row is None:
                raise EncryptedDocumentError()
            row = dict(row)
        return self._verified(doc_id, self._path(doc_id), row, decrypt=True)

    def extract(self, doc_id):
        with operation_reservation(self.owner):
            try:
                return extract_pdf_bytes(self._decrypt(document_id(doc_id)), bounded=True)
            except Exception:
                raise EncryptedDocumentError() from None

    def index(self, doc_id):
        with operation_reservation(self.owner):
            try:
                if self.search is None:
                    raise EncryptedDocumentError()
                pages = extract_pdf_bytes(self._decrypt(document_id(doc_id)), bounded=True)
                return self.search.index_pages(doc_id, pages)
            except Exception:
                raise EncryptedDocumentError() from None

    def delete(self, doc_id):
        with operation_reservation(self.owner):
            try:
                self._delete(document_id(doc_id))
            except Exception:
                raise EncryptedDocumentError() from None

    def _delete(self, doc_id):
        with semantic_lock, self.owner._mutex:
            row = self._row(doc_id)
            if row is None:
                raise EncryptedDocumentError()
            if row['state'] == 'DELETED':
                return
            if row['state'] not in ('COMMITTED', 'DELETE_PENDING'):
                raise EncryptedDocumentError()
            self.store._snapshot = None
            with self.store.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE vault_document_operations SET kind='DELETE',state='DELETE_PENDING' WHERE document_id=?", (doc_id,))
                c.execute('DELETE FROM documents WHERE id=?', (doc_id,))
                c.execute('DELETE FROM vector_cleanup WHERE document_id=?', (doc_id,))
                self._checkpoint('before_delete_commit')
            self._checkpoint('deleted_logically')
        self._finish_delete(doc_id)

    def _finish_delete(self, doc_id):
        row = self._row(doc_id)
        self._unlink_verified(doc_id, self._path(doc_id), row)
        self._checkpoint('deleted_file')
        if row['stage_owned']:
            self._unlink_verified(doc_id, self._path(doc_id, True), row)
        with self.store.connection() as c:
            c.execute("UPDATE vault_document_operations SET state='DELETED',stage_owned=0 WHERE document_id=?", (doc_id,))

    def recover(self):
        """Explicit, repeatable recovery; never scans or deletes unregistered files.

Missing/partial PREPARING files are aborted. Ambiguous or altered files are
preserved and reported, not blindly removed. Caller must resolve such cases.
"""
        with operation_reservation(self.owner):
            last, failed = '', False
            while True:
                with self.store.connection() as c:
                    ids = [r[0] for r in c.execute('SELECT document_id FROM vault_document_operations WHERE document_id>? ORDER BY document_id LIMIT 128', (last,))]
                if not ids:
                    break
                for doc_id in ids:
                    try:
                        self._recover_one(doc_id)
                    except Exception:
                        failed = True
                last = ids[-1]
            if failed:
                # IDs are non-sensitive; no raw exception or plaintext output.
                raise EncryptedDocumentError()

    def _recover_one(self, doc_id):
        row = self._row(doc_id)
        staged, final = self._path(doc_id, True), self._path(doc_id)
        if row['state'] == 'DELETE_PENDING':
            self._finish_delete(doc_id)
        elif row['state'] == 'COMMITTED':
            self._verified(doc_id, final, row)
            if staged.exists():
                if not row['stage_owned']:
                    raise EncryptedDocumentError()
                self._unlink_verified(doc_id, staged, row)
            with self.store.connection() as c:
                c.execute('UPDATE vault_document_operations SET stage_owned=0 WHERE document_id=?', (doc_id,))
        elif row['state'] in ('PREPARING', 'FILE_STAGED', 'FILE_PUBLISHED'):
            if final.exists() or staged.exists():
                if not row['digest'] or (staged.exists() and not row['stage_owned']):
                    raise EncryptedDocumentError()
                if not final.exists() and staged.stat().st_size < row['cipher_size']:
                    self._abort_partial(doc_id, staged, row)
                    return
                self._finish_ingest(doc_id)
            else:
                with self.store.connection() as c:
                    c.execute('DELETE FROM vault_pending_documents WHERE document_id=?', (doc_id,))
                    c.execute("UPDATE vault_document_operations SET state='FAILED',stage_owned=0 WHERE document_id=?", (doc_id,))
        elif row['state'] not in ('FAILED', 'DELETED'):
            raise EncryptedDocumentError()

    def _abort_partial(self, doc_id, path, row):
        # Exclusive creation was durably recorded BEFORE writing bytes. Do not
        # unlink arbitrary content, even at an owned staging name: its available
        # header prefix must match the expected UUIDs, nonce and length exactly.
        from uuid import UUID
        data = self._read_cipher(path) if path.stat().st_size else b''
        expected = HEADER.pack(MAGIC, 1, UUID(self.owner._vault_id).bytes,
                               UUID(doc_id).bytes, row['nonce'], row['cipher_size'] - HEADER.size - 16)
        if data[:HEADER.size] != expected[:min(len(data), HEADER.size)]:
            raise EncryptedDocumentError()
        validate_path(path, root=self.owner._root).unlink()
        with self.store.connection() as c:
            c.execute('DELETE FROM vault_pending_documents WHERE document_id=?', (doc_id,))
            c.execute("UPDATE vault_document_operations SET state='FAILED',stage_owned=0 WHERE document_id=?", (doc_id,))
