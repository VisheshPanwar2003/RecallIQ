"""Internal key owner; deliberately disconnected from HTTP and storage readiness.

Python bytes cannot be reliably zeroized. A future storage worker must own this
object and terminate on lock to bound the lifetime of decrypted process state.
"""
import base64
import json
import os
from pathlib import Path
from threading import RLock
from enum import Enum
from uuid import UUID, uuid4
from weakref import WeakMethod

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.storage import validate_path


ENVELOPE_NAME = "key-envelope.json"
MAX_ENVELOPE_BYTES = 4096
KDF = {"algorithm": "argon2id", "version": 19, "memory_kib": 65536,
       "iterations": 3, "parallelism": 4, "length": 32}


class VaultState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"
    ERROR = "ERROR"


class VaultError(RuntimeError):
    def __init__(self):
        super().__init__("Vault operation failed.")


class VaultExists(VaultError):
    pass


class UnlockFailed(VaultError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value, length):
    if not isinstance(value, str):
        raise VaultError()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise VaultError() from None
    if len(decoded) != length or _encode(decoded) != value:
        raise VaultError()
    return decoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VaultError()
        result[key] = value
    return result


def _validate(header):
    # V1 accepts one reviewed profile, rather than attacker-selected costs.
    # Unknown profiles/versions require a future explicit format migration.
    if not isinstance(header, dict) or set(header) != {"format", "version", "vault_id", "cipher", "kdf", "salt", "nonce"}:
        raise VaultError()
    if header["format"] != "RecallIQ-key-envelope" or type(header["version"]) is not int or header["version"] != 1 or header["cipher"] != "AES-256-GCM":
        raise VaultError()
    kdf = header["kdf"]
    if not isinstance(kdf, dict) or set(kdf) != set(KDF):
        raise VaultError()
    for name, expected in KDF.items():
        if type(kdf[name]) is not type(expected) or kdf[name] != expected:
            raise VaultError()
    try:
        identity = UUID(header["vault_id"])
        if identity.version != 4 or str(identity) != header["vault_id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise VaultError() from None
    return _decode(header["salt"], 16), _decode(header["nonce"], 12)


def _password_bytes(password):
    # Bound characters BEFORE UTF-8 encoding, then bound bytes. No normalization.
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise VaultError()
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        raise VaultError() from None
    if len(encoded) > 1024:
        raise VaultError()
    return encoded


def _wrapping_key(password, salt):
    return hash_secret_raw(password, salt, time_cost=KDF["iterations"],
                           memory_cost=KDF["memory_kib"], parallelism=KDF["parallelism"],
                           hash_len=32, type=Type.ID, version=19)


class VaultKeyManager:
    """One internal owner; operations serialized, no public key-returning methods.

    A newly initialized vault is LOCKED. Unlock is explicit. Failed unlock never
    retains a previously unlocked key. No readiness verifier uses this state.
    """
    def __init__(self, root: Path, *, expected_vault_id: str | None = None):
        self._root = Path(root)
        self._expected_id = expected_vault_id
        self._master = None
        self._vault_id = None
        self._mutex = RLock()
        self._lock_observers = []
        self._state = VaultState.UNINITIALIZED
        try:
            self._root = validate_path(self._root)
            if self._root.exists():
                self._read_envelope()
                self._state = VaultState.LOCKED
        except Exception:
            self._state = VaultState.ERROR

    @property
    def state(self):
        with self._mutex:
            return self._state

    def _read_envelope(self):
        root = validate_path(self._root)
        path = validate_path(root / ENVELOPE_NAME, root=root)
        if not path.is_file() or path.stat().st_size > MAX_ENVELOPE_BYTES:
            raise VaultError()
        with path.open("rb") as stream:
            raw = stream.read(MAX_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise VaultError()
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"header", "wrapped_key"}:
            raise VaultError()
        _validate(envelope["header"])
        _decode(envelope["wrapped_key"], 48)
        if self._expected_id is not None and envelope["header"]["vault_id"] != self._expected_id:
            raise VaultError()
        return envelope

    def initialize(self, password: str):
        with self._mutex:
            # Existing directory (even empty) is never adopted or modified.
            if self._state != VaultState.UNINITIALIZED:
                raise VaultExists()
            if self._root.exists():
                # Another owner may have initialized since this object was made.
                self._refresh_existing_state()
                raise VaultExists()
            published = False
            try:
                encoded = _password_bytes(password)
                root = validate_path(self._root)
                if not root.parent.is_dir():
                    raise VaultError()
                identity = str(uuid4())
                if self._expected_id is not None:
                    raise VaultError()
                header = {"format": "RecallIQ-key-envelope", "version": 1,
                          "vault_id": identity, "cipher": "AES-256-GCM", "kdf": dict(KDF),
                          "salt": _encode(os.urandom(16)), "nonce": _encode(os.urandom(12))}
                salt, nonce = _validate(header)
                # Exactly one wrapping encryption per fresh salt/key. V1 does
                # not offer rewrapping, so it cannot reuse an envelope nonce.
                master = AESGCM.generate_key(bit_length=256)
                wrapping = _wrapping_key(encoded, salt)
                payload = _canonical({"header": header, "wrapped_key": _encode(
                    AESGCM(wrapping).encrypt(nonce, master, _canonical(header)))})
                root.mkdir(mode=0o700)  # exclusive ownership; never exist_ok
                validate_path(root)
                pending = validate_path(root / "key-envelope.pending", root=root)
                descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Atomic no-overwrite publication on supported local filesystems.
                # Failure before publication leaves an ERROR directory for review.
                validate_path(root)
                os.link(pending, validate_path(root / ENVELOPE_NAME, root=root))
                # Publication is the commit point. Failure to remove the extra
                # encrypted hard link must not misclassify a valid vault.
                published = True
                self._expected_id = identity
                self._state = VaultState.LOCKED
                pending.unlink()
            except FileExistsError:
                self._refresh_existing_state()
                raise VaultExists() from None
            except Exception:
                self._master = None
                self._state = VaultState.LOCKED if published else VaultState.ERROR
                raise VaultError() from None

    def _refresh_existing_state(self):
        # Read only: incomplete creation is ERROR; a published envelope is LOCKED.
        try:
            self._read_envelope()
            self._state = VaultState.LOCKED
        except Exception:
            self._state = VaultState.ERROR

    def unlock(self, password: str):
        with self._mutex:
            self._invalidate_dependents()
            self._master = None
            self._vault_id = None
            try:
                encoded = _password_bytes(password)
                envelope = self._read_envelope()
                header = envelope["header"]
                salt, nonce = _validate(header)
                wrapping = _wrapping_key(encoded, salt)
                master = AESGCM(wrapping).decrypt(nonce, _decode(envelope["wrapped_key"], 48), _canonical(header))
                if len(master) != 32:
                    raise VaultError()
                self._master = master
                self._vault_id = header["vault_id"]
                self._expected_id = self._vault_id
                self._state = VaultState.UNLOCKED
            except InvalidTag:
                self._state = VaultState.LOCKED
                raise UnlockFailed() from None
            except Exception:
                self._state = VaultState.ERROR
                raise UnlockFailed() from None

    def lock(self):
        with self._mutex:
            self._invalidate_dependents()
            self._master = None  # reference release, NOT secure memory erasure
            self._vault_id = None
            if self._state == VaultState.UNLOCKED:
                self._state = VaultState.LOCKED

    def _observe_lock(self, callback):
        """Internal weak observer: dependent storage must close on key revocation."""
        with self._mutex:
            self._lock_observers.append(WeakMethod(callback))

    def _invalidate_dependents(self):
        live = []
        for reference in self._lock_observers:
            callback = reference()
            if callback is not None:
                callback()
                live.append(reference)
        self._lock_observers = live

    def _subkey(self, purpose: str):
        """Storage-worker integration seam; never expose this through HTTP."""
        with self._mutex:
            if self._state != VaultState.UNLOCKED or self._master is None or purpose not in {"database", "document"}:
                raise VaultError()
            return HKDF(algorithm=hashes.SHA256(), length=32,
                        salt=UUID(self._vault_id).bytes,
                        info=f"RecallIQ/vault/v1/{purpose}".encode("ascii")).derive(self._master)
