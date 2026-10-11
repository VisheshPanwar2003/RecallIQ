"""Offline CLI. Passwords are read from the console, never args/environment."""
import argparse
import getpass
import json
import os
from pathlib import Path
from uuid import uuid4

from app.embeddings import LocalEmbedder
from app.migration_source import MigrationError
from app.storage import validate_path
from app.vault_database import VaultDocumentStore
from app.vault_keys import VaultKeyManager
from app.vault_migration import VaultMigration


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline migration with local re-embedding. Stop RecallIQ first.')
    parser.add_argument('command', choices=['preflight','migrate','verify','resume','rollback'])
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--source-database', type=Path)
    parser.add_argument('--source-uploads', type=Path)
    parser.add_argument('--migration-id')
    args = parser.parse_args(argv)
    if args.command in ('preflight','migrate','resume') and (args.source_database is None or args.source_uploads is None):
        parser.error('source database and uploads are required')
    if args.command in ('verify','resume','rollback') and not args.migration_id:
        parser.error('migration ID is required')
    owner = None
    try:
        root = validate_path(args.vault)
        # Do not initialize/create a vault or database implicitly.
        owner = VaultKeyManager(root)
        owner.unlock(getpass.getpass('Vault password: '))
        os.environ['RECALLIQ_MODELS_OFFLINE'] = '1'
        embedder = LocalEmbedder()
        store = VaultDocumentStore(owner, embedder.config)
        migration = VaultMigration(store, embedder)
        if args.command == 'preflight':
            result = migration.dry_run(args.source_database, args.source_uploads)
        elif args.command in ('migrate','resume'):
            mid = args.migration_id or str(uuid4())
            print(json.dumps({'migration_id':mid,'status':'REQUESTED'}), flush=True)
            result = migration.execute(args.source_database, args.source_uploads, migration_id=mid)
        elif args.command == 'verify':
            result = migration.verify(args.migration_id)
        else:
            # Always separate confirmations: completed migration removal is an
            # additional explicit authorization, not implied by ordinary rollback.
            confirmed = input('Type ROLLBACK to remove migration-owned destination records: ') == 'ROLLBACK'
            with store.connection() as c:
                row = c.execute('SELECT state FROM vault_migrations WHERE migration_id=?', (args.migration_id,)).fetchone()
            completed = bool(row and row[0] == 'VERIFIED')
            completed_confirmed = completed and input('Completed migration: type REMOVE VERIFIED to authorize removal: ') == 'REMOVE VERIFIED'
            result = migration.rollback(args.migration_id, confirmed=confirmed, completed_confirmed=completed_confirmed)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({'status':'FAILED','code':error.code if isinstance(error, MigrationError) else 'vault_or_migration_unavailable'}))
        return 1
    finally:
        if owner is not None:
            owner.lock()


if __name__ == '__main__':
    raise SystemExit(main())
