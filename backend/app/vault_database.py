"""Internal SQLCipher store. No HTTP activation, migration or SQLite fallback."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

from app.database import DocumentStore, initialize_schema
from app.storage import validate_path
from app.vault_keys import VaultState


class VaultDatabaseUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Encrypted vault database is unavailable.")


def sqlcipher_driver():
    try:
        from sqlcipher3 import dbapi2
        return dbapi2
    except (ImportError, OSError):
        raise VaultDatabaseUnavailable() from None


def verify_driver(connection):
    version = connection.execute("PRAGMA cipher_version").fetchone()
    options = {row[0] for row in connection.execute("PRAGMA compile_options")}
    if not version or not version[0].startswith("4.12.") or not {"HAS_CODEC", "ENABLE_FTS5", "TEMP_STORE=2"} <= options:
        raise VaultDatabaseUnavailable()


class VaultDocumentStore(DocumentStore):
    """Short-lived keyed connections; all operations share the key owner's lock.

    Creation must be explicit; reopen never creates, repairs or migrates a file.
    Instances close permanently on lock/re-unlock. Construct a new one afterward.
    """
    def __init__(self, owner, config, *, create=False):
        self.owner = owner
        self._closed = False
        self._snapshot = None
        self.config = json.loads(json.dumps(config, sort_keys=True))
        self.config_json = json.dumps(self.config, sort_keys=True)
        dimensions = self.config.get("dimensions")
        if type(dimensions) is not int or not 1 <= dimensions <= 384 or not isinstance(self.config.get("model"), str) or not self.config['model'].strip() or len(self.config_json.encode('utf-8')) > 4096:
            raise VaultDatabaseUnavailable()
        self.dimensions = dimensions
        self.path = Path(owner._root) / "database.sqlite3"
        with owner._mutex:
            try:
                self._check()
                # Reject an unavailable/non-codec driver BEFORE creating any file.
                driver = sqlcipher_driver()
                probe = driver.connect(':memory:')
                try:
                    verify_driver(probe)
                finally:
                    probe.close()
                if create:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                elif not self.path.is_file() or self.path.stat().st_size == 0:
                    raise VaultDatabaseUnavailable()
                with self.connection() as connection:
                    if create:
                        self._create_schema(connection)
                    else:
                        self._validate_schema(connection)
                    if connection.execute("PRAGMA cipher_integrity_check").fetchall():
                        raise VaultDatabaseUnavailable()
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise VaultDatabaseUnavailable()
                owner._observe_lock(self.close)
            except Exception:
                self.close()
                raise VaultDatabaseUnavailable() from None

    def _check(self):
        if self._closed or self.owner.state != VaultState.UNLOCKED:
            raise VaultDatabaseUnavailable()
        root = validate_path(self.owner._root)
        if root.drive.startswith("\\\\"):
            raise VaultDatabaseUnavailable()
        for suffix in ("", "-journal", "-wal", "-shm"):
            validate_path(Path(str(self.path) + suffix), root=root)

    @contextmanager
    def connection(self):
        with self.owner._mutex:
            connection = None
            try:
                self._check()
                driver = sqlcipher_driver()
                connection = driver.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
                connection.row_factory = driver.Row
                verify_driver(connection)
                connection.enable_load_extension(False)
                connection.execute("PRAGMA cipher_log_level=NONE")
                # Hex comes only from the internal 32-byte subkey, never input.
                connection.execute('PRAGMA key="x\'' + self.owner._subkey("database").hex() + '\'"')
                connection.execute("PRAGMA cipher_compatibility=4")
                settings = {"cipher_use_hmac": 1, "cipher_plaintext_header_size": 0,
                            "cipher_memory_security": 1, "cipher_page_size": 4096,
                            "temp_store": 2, "foreign_keys": 1, "mmap_size": 0,
                            "synchronous": 2}
                for name, value in settings.items():
                    connection.execute(f"PRAGMA {name}={value}")
                    actual = connection.execute(f"PRAGMA {name}").fetchone()
                    if not actual or int(actual[0]) != value:
                        raise VaultDatabaseUnavailable()
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                with connection:
                    yield connection
            except Exception:
                self._snapshot = None
                raise VaultDatabaseUnavailable() from None
            finally:
                if connection is not None:
                    connection.close()

    def _create_schema(self, connection):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE vault_schema_migrations(version INTEGER PRIMARY KEY)")
        initialize_schema(connection)
        connection.execute("INSERT INTO vault_schema_migrations VALUES (1)")
        connection.execute("""CREATE TABLE vault_info(
            id INTEGER PRIMARY KEY CHECK(id=1), vault_id TEXT NOT NULL,
            embedding_config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)""")
        connection.execute("INSERT INTO vault_info(id,vault_id,embedding_config) VALUES(1,?,?)",
                           (self.owner._vault_id, self.config_json))
        connection.execute("""CREATE TABLE embeddings(
            chunk_rowid INTEGER PRIMARY KEY REFERENCES chunks(rowid) ON DELETE CASCADE,
            format_version INTEGER NOT NULL CHECK(format_version=1),
            dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 384),
            vector BLOB NOT NULL CHECK(typeof(vector)='blob' AND length(vector)=dimensions*4))""")
        # Revision changes for all relevant tables, including internal raw edits.
        for table in ("documents", "chunks", "embeddings", "document_indexes", "keyword_indexes"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER revision_{table}_{event} AFTER {event} ON {table} BEGIN UPDATE vault_info SET revision=revision+1 WHERE id=1; END")
        connection.execute("INSERT INTO vault_schema_migrations VALUES (2)")

    def _validate_schema(self, connection):
        if [row[0] for row in connection.execute("SELECT version FROM vault_schema_migrations ORDER BY version")] != [1, 2]:
            raise VaultDatabaseUnavailable()
        info = connection.execute("SELECT vault_id,embedding_config FROM vault_info WHERE id=1").fetchone()
        if not info or info[0] != self.owner._vault_id or info[1] != self.config_json:
            raise VaultDatabaseUnavailable()
        if [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] != [1]:
            raise VaultDatabaseUnavailable()

    def close(self):
        with self.owner._mutex:
            self._closed = True
            self._snapshot = None
