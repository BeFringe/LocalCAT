"""Disposable real-process worker for Task 3.3 crash/lease tests."""

from __future__ import annotations

import os
import ctypes
from pathlib import Path, PureWindowsPath
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platform_fs_contracts import LockPolicy, LockWait
from platform_fs_windows import WindowsProcessFileLock, WindowsRootedFileSystem
import platform_fs_windows
from windows_file_api import (
    DWORD,
    LARGE_INTEGER,
    OVERLAPPED,
    Win32CallError,
    WindowsFileAPI,
)


def main() -> int:
    if sys.argv[1] == "open-carrier":
        api = WindowsFileAPI.load()
        handle = api.open_handle(
            sys.argv[2],
            desired_access=platform_fs_windows.GENERIC_READ
            | platform_fs_windows.GENERIC_WRITE
            | platform_fs_windows.SYNCHRONIZE,
            share_mode=platform_fs_windows.FILE_SHARE_READ
            | platform_fs_windows.FILE_SHARE_WRITE,
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
        )
        print("READY", flush=True)
        try:
            while True:
                time.sleep(60)
        finally:
            handle.close()

    if sys.argv[1] == "range-once":
        path = sys.argv[2]
        offset_high = int(sys.argv[3])
        api = WindowsFileAPI.load()
        handle = api.open_handle(
            path,
            desired_access=platform_fs_windows.GENERIC_READ
            | platform_fs_windows.GENERIC_WRITE
            | platform_fs_windows.SYNCHRONIZE,
            share_mode=platform_fs_windows.FILE_SHARE_READ
            | platform_fs_windows.FILE_SHARE_WRITE,
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
        )
        overlapped = OVERLAPPED()
        overlapped.OffsetHigh = offset_high
        try:
            try:
                with handle.borrow() as raw:
                    api.checked_bool(
                        "LockFileEx",
                        api.LockFileEx,
                        raw,
                        0x3,
                        0,
                        1,
                        0,
                        ctypes.byref(overlapped),
                    )
                    api.checked_bool(
                        "UnlockFileEx",
                        api.UnlockFileEx,
                        raw,
                        0,
                        1,
                        0,
                        ctypes.byref(overlapped),
                    )
                return 0
            except Win32CallError as error:
                return 33 if error.winerror == 33 else 34
        finally:
            handle.close()

    mode, root_text, name, payload_hex, *rest = sys.argv[1:]
    root_path = Path(root_text)
    rooted_fs = WindowsRootedFileSystem()
    root = rooted_fs.bind_root(root_path)
    parent = rooted_fs.bind_parent(root, PureWindowsPath(name))
    fault_phase = rest[0] if rest else None

    def fault(phase: str) -> None:
        if phase == fault_phase:
            os._exit(87)

    service = WindowsProcessFileLock(_fault_injector=fault if fault_phase else None)
    lease = service.acquire(
        parent,
        name,
        bytes.fromhex(payload_hex),
        LockPolicy(LockWait.BLOCK),
    )
    if mode == "hold":
        print("READY", flush=True)
        while True:
            time.sleep(60)
    lease.close()
    parent.close()
    root.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
