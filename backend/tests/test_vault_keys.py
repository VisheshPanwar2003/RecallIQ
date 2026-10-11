import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import vault_keys as keys


PASSWORD = "generated-test-password-only"


def create(tmp_path):
    root = tmp_path / "vault"
    manager = keys.VaultKeyManager(root)
    manager.initialize(PASSWORD)
    return root, manager


def test_roundtrip_state_and_subkeys(tmp_path):
    root = tmp_path / "vault"
    manager = keys.VaultKeyManager(root)
    assert manager.state == keys.VaultState.UNINITIALIZED
    manager.initialize(PASSWORD)
    assert manager.state == keys.VaultState.LOCKED
    with pytest.raises(keys.VaultError):
        manager._subkey("database")
    manager.unlock(PASSWORD)
    database = manager._subkey("database")
    document = manager._subkey("document")
    assert len(database) == len(document) == 32 and database != document
    with pytest.raises(keys.VaultError):
        manager._subkey("unknown")
    assert PASSWORD.encode() not in (root / keys.ENVELOPE_NAME).read_bytes()
    assert manager._master not in (root / keys.ENVELOPE_NAME).read_bytes()
    manager.lock()
    assert manager.state == keys.VaultState.LOCKED
    assert manager._master is None
    reopened = keys.VaultKeyManager(root)
    reopened.unlock(PASSWORD)
    assert reopened._subkey("database") == database
    assert reopened._subkey("document") == document


def test_wrong_password_drops_existing_key_and_is_retryable(tmp_path):
    _, manager = create(tmp_path)
    manager.unlock(PASSWORD)
    with pytest.raises(keys.UnlockFailed, match="^Vault operation failed.$"):
        manager.unlock("wrong-generated-password")
    assert manager.state == keys.VaultState.LOCKED and manager._master is None
    manager.unlock(PASSWORD)
    assert manager.state == keys.VaultState.UNLOCKED


@pytest.mark.parametrize("field", ["vault_id", "nonce", "salt", "wrapped_key"])
def test_authenticated_tamper(tmp_path, field):
    root, manager = create(tmp_path)
    path = root / keys.ENVELOPE_NAME
    envelope = json.loads(path.read_bytes())
    if field == "vault_id":
        envelope["header"][field] = str(keys.uuid4())
    else:
        target = envelope if field == "wrapped_key" else envelope["header"]
        value = target[field]
        target[field] = ("A" if value[0] != "A" else "B") + value[1:]
    path.write_text(json.dumps(envelope))
    with pytest.raises(keys.UnlockFailed):
        manager.unlock(PASSWORD)
    assert manager._master is None


@pytest.mark.parametrize("name,value", [
    ("memory_kib", 2**40), ("memory_kib", 0), ("memory_kib", 65536.0),
    ("iterations", True), ("iterations", 100), ("parallelism", -1),
    ("length", 64), ("version", 16), ("algorithm", "argon2i"),
])
def test_kdf_rejected_before_derivation(tmp_path, monkeypatch, name, value):
    root, manager = create(tmp_path)
    path = root / keys.ENVELOPE_NAME
    envelope = json.loads(path.read_bytes())
    envelope["header"]["kdf"][name] = value
    path.write_text(json.dumps(envelope))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input reached expensive KDF")
    monkeypatch.setattr(keys, "hash_secret_raw", forbidden)
    with pytest.raises(keys.UnlockFailed):
        manager.unlock(PASSWORD)
    assert manager.state == keys.VaultState.ERROR


@pytest.mark.parametrize("raw", [b"{", b"x"*4097, b'{"header":{},"header":{},"wrapped_key":""}', b'[]'])
def test_malformed_bounded_envelope(tmp_path, monkeypatch, raw):
    root, manager = create(tmp_path)
    (root / keys.ENVELOPE_NAME).write_bytes(raw)
    monkeypatch.setattr(keys, "hash_secret_raw", lambda *a, **kw: pytest.fail("KDF called"))
    with pytest.raises(keys.UnlockFailed):
        manager.unlock(PASSWORD)
    assert keys.VaultKeyManager(root).state == keys.VaultState.ERROR


@pytest.mark.parametrize("field,value", [("version", 2), ("version", True), ("format", "other"), ("cipher", "AES-128-GCM"), ("nonce", "invalid"), ("salt", "")])
def test_header_rejected_before_kdf(tmp_path, monkeypatch, field, value):
    root, manager = create(tmp_path)
    path = root / keys.ENVELOPE_NAME
    envelope = json.loads(path.read_bytes()); envelope["header"][field] = value
    path.write_text(json.dumps(envelope))
    monkeypatch.setattr(keys, "hash_secret_raw", lambda *a, **kw: pytest.fail("KDF called"))
    with pytest.raises(keys.UnlockFailed):
        manager.unlock(PASSWORD)


@pytest.mark.parametrize("password", ["", "x"*1025, "😀"*300, None, "\ud800"])
def test_invalid_password_does_not_create_directory(tmp_path, monkeypatch, password):
    root = tmp_path / "vault"
    monkeypatch.setattr(keys, "hash_secret_raw", lambda *a, **kw: pytest.fail("KDF called"))
    manager = keys.VaultKeyManager(root)
    with pytest.raises(keys.VaultError):
        manager.initialize(password)
    assert not root.exists()


def test_existing_vault_and_empty_directory_untouched(tmp_path):
    root, manager = create(tmp_path)
    before = (root / keys.ENVELOPE_NAME).read_bytes()
    for owner in (manager, keys.VaultKeyManager(root)):
        with pytest.raises(keys.VaultExists):
            owner.initialize("replacement")
    assert (root / keys.ENVELOPE_NAME).read_bytes() == before
    empty = tmp_path / "empty"; empty.mkdir()
    with pytest.raises(keys.VaultExists):
        keys.VaultKeyManager(empty).initialize(PASSWORD)
    assert list(empty.iterdir()) == []


def test_distinct_salts_nonces_and_keys(tmp_path):
    envelopes=[]; derived=[]
    for i in range(2):
        root=tmp_path / str(i); manager=keys.VaultKeyManager(root)
        manager.initialize(PASSWORD); manager.unlock(PASSWORD)
        envelopes.append(json.loads((root / keys.ENVELOPE_NAME).read_bytes())["header"])
        derived.append(manager._subkey("database"))
    for field in ("salt", "nonce", "vault_id"):
        assert envelopes[0][field] != envelopes[1][field]
    assert derived[0] != derived[1]


def test_publication_failure_is_fail_closed(tmp_path, monkeypatch):
    root=tmp_path / "vault"; manager=keys.VaultKeyManager(root)
    def fail(*args): raise OSError("generated failure")
    monkeypatch.setattr(keys.os, "link", fail)
    with pytest.raises(keys.VaultError): manager.initialize(PASSWORD)
    assert manager.state == keys.VaultState.ERROR
    assert manager._master is None and not (root / keys.ENVELOPE_NAME).exists()
    assert PASSWORD.encode() not in (root / "key-envelope.pending").read_bytes()
    with pytest.raises(keys.VaultExists): manager.initialize(PASSWORD)


def test_concurrent_initialization_has_one_winner(tmp_path):
    root=tmp_path / "vault"; owners=[keys.VaultKeyManager(root) for _ in range(2)]
    def initialize(owner):
        try: owner.initialize(PASSWORD); return True
        except keys.VaultExists: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(initialize,owners)) == [False,True]
    keys.VaultKeyManager(root).unlock(PASSWORD)
    assert all(owner.state in {keys.VaultState.LOCKED, keys.VaultState.ERROR} for owner in owners)


def test_owner_created_before_another_initialization_updates_state(tmp_path):
    root = tmp_path / "vault"
    stale = keys.VaultKeyManager(root)
    winner = keys.VaultKeyManager(root)
    winner.initialize(PASSWORD)
    before = (root / keys.ENVELOPE_NAME).read_bytes()
    with pytest.raises(keys.VaultExists):
        stale.initialize(PASSWORD)
    assert stale.state == keys.VaultState.LOCKED
    assert (root / keys.ENVELOPE_NAME).read_bytes() == before


def test_failure_after_publication_preserves_locked_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    manager = keys.VaultKeyManager(root)
    original_unlink = Path.unlink
    def fail_pending(path, *args, **kwargs):
        if path.name == "key-envelope.pending":
            raise OSError("generated cleanup failure")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_pending)
    with pytest.raises(keys.VaultError):
        manager.initialize(PASSWORD)
    assert manager.state == keys.VaultState.LOCKED
    assert manager._expected_id is not None and manager._master is None
    assert (root / keys.ENVELOPE_NAME).read_bytes() == (root / "key-envelope.pending").read_bytes()
    manager.unlock(PASSWORD)
    assert manager.state == keys.VaultState.UNLOCKED
    reopened = keys.VaultKeyManager(root)
    reopened.unlock(PASSWORD)
    assert reopened._subkey("database") == manager._subkey("database")
    with pytest.raises(keys.VaultExists):
        manager.initialize(PASSWORD)


def test_interrupted_pending_write_is_not_adopted(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    pending = root / "key-envelope.pending"
    pending.write_bytes(b'{"incomplete":')
    manager = keys.VaultKeyManager(root)
    assert manager.state == keys.VaultState.ERROR
    with pytest.raises(keys.VaultExists):
        manager.initialize(PASSWORD)
    with pytest.raises(keys.UnlockFailed):
        manager.unlock(PASSWORD)
    assert pending.read_bytes() == b'{"incomplete":'


def test_key_and_password_absent_from_error_output(tmp_path, caplog, capsys):
    _, manager = create(tmp_path)
    manager.unlock(PASSWORD)
    secret = manager._master
    try:
        manager.unlock("wrong-generated-password")
    except keys.UnlockFailed as error:
        import traceback
        rendered = "".join(traceback.format_exception(error))
    assert PASSWORD not in rendered and repr(secret) not in rendered
    assert caplog.text == ""
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_identity_pin_rejects_replacement(tmp_path):
    root, manager=create(tmp_path)
    with pytest.raises(keys.UnlockFailed):
        keys.VaultKeyManager(root,expected_vault_id=str(keys.uuid4())).unlock(PASSWORD)


def test_unsafe_paths_rejected(tmp_path):
    owner=keys.VaultKeyManager(tmp_path / ".." / "vault")
    assert owner.state == keys.VaultState.ERROR
    with pytest.raises(keys.VaultError): owner.initialize(PASSWORD)


def test_unlock_does_not_change_external_storage_readiness(tmp_path, monkeypatch):
    root, manager=create(tmp_path); manager.unlock(PASSWORD)
    from app.storage import PrivateStorage, StorageUnavailable
    monkeypatch.setenv("RECALLIQ_VAULT_MODE","external")
    monkeypatch.setenv("RECALLIQ_VAULT_ROOT",str(root))
    with pytest.raises(StorageUnavailable): PrivateStorage.from_environment().require_ready()


def test_no_password_routes():
    from app.main import app
    assert not any("vault" in path or "unlock" in path for path in app.openapi()["paths"])
