"""Windows named mutex for cooperative cross-process vault ownership."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os


class ExclusiveUnavailable(RuntimeError):
    pass


@contextmanager
def exclusive_vault(root):
    if os.name != 'nt':
        # Existing portable development/test behavior; offline migration itself
        # rejects non-Windows platforms through its source reader.
        yield
        return
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    name = 'Local\\RecallIQ-vault-' + hashlib.sha256(str(root).casefold().encode()).hexdigest()
    handle = api.CreateMutexW(None, False, name)
    if not handle:
        raise ExclusiveUnavailable('Vault exclusive access unavailable.')
    acquired = False
    try:
        result = api.WaitForSingleObject(handle, 0)
        if result not in (0, 0x80):
            raise ExclusiveUnavailable('Vault exclusive access unavailable.')
        # Abandoned mutex means a prior process terminated. Acquisition does
        # not imply recovery success: authenticated journals must be verified.
        acquired = True
        yield
    finally:
        if acquired:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)
