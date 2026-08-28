"""Validation-only Win32 process and token construction declarations."""

from __future__ import annotations

import ctypes
import sys

from windows_file_api import (
    BOOL,
    BYTE,
    DWORD,
    HANDLE,
    LPCWSTR,
    LPVOID,
    LPWSTR,
    LUID_AND_ATTRIBUTES,
    SID_AND_ATTRIBUTES,
    WORD,
)


class STARTUPINFOW(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", WORD),
        ("cbReserved2", WORD),
        ("lpReserved2", ctypes.POINTER(BYTE)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


TEST_PROCESS_SIGNATURES: dict[str, tuple[tuple[object, ...], object]] = {
    "GetExitCodeProcess": ((HANDLE, ctypes.POINTER(DWORD)), BOOL),
    "CreateRestrictedToken": (
        (
            HANDLE,
            DWORD,
            DWORD,
            ctypes.POINTER(SID_AND_ATTRIBUTES),
            DWORD,
            ctypes.POINTER(LUID_AND_ATTRIBUTES),
            DWORD,
            ctypes.POINTER(SID_AND_ATTRIBUTES),
            ctypes.POINTER(HANDLE),
        ),
        BOOL,
    ),
    "SetTokenInformation": ((HANDLE, ctypes.c_int, LPVOID, DWORD), BOOL),
    "CreateProcessAsUserW": (
        (
            HANDLE,
            LPCWSTR,
            LPWSTR,
            LPVOID,
            LPVOID,
            BOOL,
            DWORD,
            LPVOID,
            LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(PROCESS_INFORMATION),
        ),
        BOOL,
    ),
}


def _bind(dll: object, name: str) -> object:
    argtypes, restype = TEST_PROCESS_SIGNATURES[name]
    function = getattr(dll, name)
    function.argtypes = list(argtypes)
    function.restype = restype
    return function


class WindowsTestProcessAPI:
    """Test-only process launcher and token mutation function table."""

    def __init__(self, kernel32: object, advapi32: object) -> None:
        self.GetExitCodeProcess = _bind(kernel32, "GetExitCodeProcess")
        self.CreateRestrictedToken = _bind(advapi32, "CreateRestrictedToken")
        self.SetTokenInformation = _bind(advapi32, "SetTokenInformation")
        self.CreateProcessAsUserW = _bind(advapi32, "CreateProcessAsUserW")

    @classmethod
    def load(cls, *, _dll_loader: object | None = None) -> WindowsTestProcessAPI:
        if sys.platform != "win32":
            raise RuntimeError("validation-only process API requires Windows")
        loader = _dll_loader or getattr(ctypes, "WinDLL", None)
        if not callable(loader):
            raise RuntimeError("WinDLL is unavailable")
        return cls(
            loader("kernel32.dll", use_last_error=True),
            loader("advapi32.dll", use_last_error=True),
        )
