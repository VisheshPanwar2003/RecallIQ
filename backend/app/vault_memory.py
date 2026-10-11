"""Process-wide admission for internal vault document/index work.

This budgets managed buffers, not native-library RSS, paging or model residency.
Only synchronous calls in the same thread may share an existing reservation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock, current_thread

from app.vault_document_format import EncryptedDocumentError
from app.vault_keys import VaultError

MEMORY_RESERVATION_BYTES = 256 * 1024 * 1024
_heavy_operation = Lock()
_reserved_context = ContextVar('vault_memory_reservation', default=None)
_active_reservation = None


@contextmanager
def memory_reservation():
    global _active_reservation
    inherited = _reserved_context.get()
    if inherited is not None and inherited is _active_reservation and inherited[1] is current_thread():
        yield
        return
    if not _heavy_operation.acquire(blocking=False):
        raise EncryptedDocumentError()
    _active_reservation = (object(), current_thread())
    token = _reserved_context.set(_active_reservation)
    try:
        yield
    finally:
        _reserved_context.reset(token)
        _active_reservation = None
        _heavy_operation.release()


@contextmanager
def operation_reservation(owner):
    # Check locking without blocking, then reserve memory BEFORE the key mutex.
    # The lease rechecks state, closing the race with a transition starting here.
    if not owner._available():
        raise VaultError()
    with memory_reservation(), owner._operation():
        yield
