from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import ClassVar

from .errors import OperationalError


class OperationLockError(OperationalError):
    pass


class OperationLock:
    """A non-blocking, process-scoped lock for shared-state mutations."""

    _process_guard: ClassVar[threading.Lock] = threading.Lock()
    _process_owned: ClassVar[set[str]] = set()

    def __init__(self, path: Path):
        self.path = path
        self._handle = None
        self._mutex = None
        self._identity = None

    def __enter__(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            identity = hashlib.sha256(
                str(self.path.resolve()).casefold().encode("utf-8")
            ).hexdigest()
            with self._process_guard:
                if identity in self._process_owned:
                    raise OperationLockError("another yande-sync operation is already running")
            mutex = kernel32.CreateMutexW(None, False, f"Global\\YandeSync-{identity}")
            if not mutex:
                raise OperationLockError("cannot create the yande-sync operation mutex")
            result = kernel32.WaitForSingleObject(mutex, 0)
            if result not in {0x00000000, 0x00000080}:
                kernel32.CloseHandle(mutex)
                if result == 0x00000102:
                    raise OperationLockError("another yande-sync operation is already running")
                raise OperationLockError("cannot acquire the yande-sync operation mutex")
            with self._process_guard:
                self._process_owned.add(identity)
            self._mutex = mutex
            self._identity = identity
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the supported runtime
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise OperationLockError("another yande-sync operation is already running") from exc
        self._handle = handle
        return self

    def materialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

    def __exit__(self, *_args):
        if self._mutex is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.ReleaseMutex(self._mutex)
            kernel32.CloseHandle(self._mutex)
            self._mutex = None
            with self._process_guard:
                self._process_owned.discard(self._identity)
            self._identity = None
            return
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the supported runtime
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
