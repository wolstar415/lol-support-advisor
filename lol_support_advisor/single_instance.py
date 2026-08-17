from __future__ import annotations

import ctypes
import os


ERROR_ALREADY_EXISTS = 183
DEFAULT_MUTEX_NAME = r"Local\LOL-Pick-Advisor-Single-Instance"


class SingleInstanceLock:
    """Process-lifetime Windows mutex used before any Tk window is created."""

    def __init__(self, name: str = DEFAULT_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None
        self._kernel32: object | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if os.name != "nt":
            # The distributed app targets Windows. Keep source-mode startup
            # usable on other platforms without pretending to hold a mutex.
            return True

        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            raise OSError(last_error, "단일 실행 잠금을 만들지 못했습니다.")
        if last_error == ERROR_ALREADY_EXISTS:
            close_handle(handle)
            return False
        self._kernel32 = kernel32
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if self._handle is None or os.name != "nt":
            self._handle = None
            return
        assert self._kernel32 is not None
        self._kernel32.CloseHandle(self._handle)
        self._handle = None
        self._kernel32 = None

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError("LOL Pick Advisor가 이미 실행 중입니다.")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()
