"""Exact Win32 declarations used by the Windows platform adapter.

Importing this module is inert on non-Windows hosts.  DLL loading is an explicit
operation so platform composition can remain fail closed.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable


BYTE = ctypes.c_ubyte
BOOLEAN = BYTE
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
NTSTATUS = ctypes.c_int32
ULONG = ctypes.c_uint32
ULONGLONG = ctypes.c_uint64
ULONG_PTR = ctypes.c_size_t
BOOL = ctypes.c_int32
UINT = ctypes.c_uint32
HANDLE = ctypes.c_void_p
HLOCAL = ctypes.c_void_p
LPVOID = ctypes.c_void_p
LPCVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.c_wchar_p
PSID = ctypes.c_void_p
PACL = ctypes.c_void_p
PSECURITY_DESCRIPTOR = ctypes.c_void_p

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_INVALID_HANDLE = 6
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
DUPLICATE_SAME_ACCESS = 0x00000002
STATUS_INVALID_HANDLE = 0xC0000008
FILE_RENAME_INFORMATION_CLASS = 10


class FILE_ID_128(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Identifier", BYTE * 16)]


class FILE_ID_INFO(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("VolumeSerialNumber", ULONGLONG),
        ("FileId", FILE_ID_128),
    ]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("FileAttributes", DWORD),
        ("ReparseTag", DWORD),
    ]


class FILE_STANDARD_INFO(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("AllocationSize", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", DWORD),
        ("DeletePending", BOOLEAN),
        ("Directory", BOOLEAN),
    ]


class _LARGE_INTEGER_PARTS(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("LowPart", DWORD), ("HighPart", LONG)]


class LARGE_INTEGER(ctypes.Union):
    _layout_ = "ms"
    _anonymous_ = ("parts",)
    _fields_ = [("parts", _LARGE_INTEGER_PARTS), ("QuadPart", ctypes.c_int64)]


class FILE_BASIC_INFO(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("CreationTime", LARGE_INTEGER),
        ("LastAccessTime", LARGE_INTEGER),
        ("LastWriteTime", LARGE_INTEGER),
        ("ChangeTime", LARGE_INTEGER),
        ("FileAttributes", DWORD),
    ]


class _OVERLAPPED_OFFSET(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Offset", DWORD), ("OffsetHigh", DWORD)]


class _OVERLAPPED_UNION(ctypes.Union):
    _layout_ = "ms"
    _anonymous_ = ("offset",)
    _fields_ = [("offset", _OVERLAPPED_OFFSET), ("Pointer", LPVOID)]


class OVERLAPPED(ctypes.Structure):
    _layout_ = "ms"
    _anonymous_ = ("position",)
    _fields_ = [
        ("Internal", ULONG_PTR),
        ("InternalHigh", ULONG_PTR),
        ("position", _OVERLAPPED_UNION),
        ("hEvent", HANDLE),
    ]


class _FILE_RENAME_NAME(ctypes.Union):
    _layout_ = "ms"
    _fields_ = [("ReplaceIfExists", BOOLEAN), ("Flags", DWORD)]


class FILE_RENAME_INFO(ctypes.Structure):
    _layout_ = "ms"
    _anonymous_ = ("mode",)
    _fields_ = [
        ("mode", _FILE_RENAME_NAME),
        ("RootDirectory", HANDLE),
        ("FileNameLength", DWORD),
        ("FileName", ctypes.c_wchar * 1),
    ]


class FILE_RENAME_INFORMATION(ctypes.Structure):
    """Native FileRenameInformation layout used by NtSetInformationFile."""

    _layout_ = "ms"
    _fields_ = [
        ("ReplaceIfExists", BOOLEAN),
        ("RootDirectory", HANDLE),
        ("FileNameLength", ULONG),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _IO_STATUS_BLOCK_RESULT(ctypes.Union):
    _layout_ = "ms"
    _fields_ = [("Status", NTSTATUS), ("Pointer", LPVOID)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _layout_ = "ms"
    _anonymous_ = ("result",)
    _fields_ = [("result", _IO_STATUS_BLOCK_RESULT), ("Information", ULONG_PTR)]


class FILE_DISPOSITION_INFO(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("DeleteFile", BOOLEAN)]


class GENERIC_MAPPING(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("GenericRead", DWORD),
        ("GenericWrite", DWORD),
        ("GenericExecute", DWORD),
        ("GenericAll", DWORD),
    ]


class ACL_SIZE_INFORMATION(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("AceCount", DWORD),
        ("AclBytesInUse", DWORD),
        ("AclBytesFree", DWORD),
    ]


class LUID(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("LowPart", DWORD), ("HighPart", LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


class PRIVILEGE_SET(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("PrivilegeCount", DWORD),
        ("Control", DWORD),
        ("Privilege", LUID_AND_ATTRIBUTES * 1),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Sid", PSID), ("Attributes", DWORD)]


class TOKEN_USER(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class ACE_HEADER(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("AceType", BYTE), ("AceFlags", BYTE), ("AceSize", WORD)]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Header", ACE_HEADER), ("Mask", DWORD), ("SidStart", DWORD)]


class SYSTEM_MANDATORY_LABEL_ACE(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [("Header", ACE_HEADER), ("Mask", DWORD), ("SidStart", DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", BOOL),
    ]


class Win32CallError(OSError):
    """Internal Win32 call failure; adapters normalize it before public use."""

    __slots__ = ("function", "winerror")

    def __init__(self, function: str, winerror: int) -> None:
        self.function = function
        self.winerror = winerror
        super().__init__(winerror)


class NtStatusError(OSError):
    """Internal NTSTATUS failure; adapters normalize it before public use."""

    __slots__ = ("function", "ntstatus")

    def __init__(self, function: str, ntstatus: int) -> None:
        self.function = function
        self.ntstatus = int(ntstatus) & 0xFFFFFFFF
        super().__init__(self.ntstatus)


class _Win32HandleBorrow:
    """A lock-held HANDLE borrow that serializes native use with close."""

    __slots__ = ("__owner", "__entered")

    def __init__(self, owner: Win32Handle) -> None:
        self.__owner = owner
        self.__entered = False

    def __enter__(self) -> int:
        owner = self.__owner
        owner._Win32Handle__lock.acquire()
        if owner._Win32Handle__closed:
            owner._Win32Handle__lock.release()
            raise Win32CallError("Win32Handle", ERROR_INVALID_HANDLE)
        self.__entered = True
        return owner._Win32Handle__value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self.__entered:
            self.__entered = False
            self.__owner._Win32Handle__lock.release()


class Win32Handle:
    """One-way, non-copyable owner for one Win32 HANDLE."""

    __slots__ = ("__value", "__close", "__last_error", "__closed", "__lock")

    def __init__(
        self,
        value: int,
        *,
        _close: Callable[[int], int],
        _last_error: Callable[[], int] | None = None,
    ) -> None:
        if type(value) is not int:
            raise TypeError("Win32 handle must be an exact int")
        if value in {0, INVALID_HANDLE_VALUE}:
            raise ValueError("Win32 handle is invalid")
        if not callable(_close):
            raise TypeError("Win32 close function must be callable")
        if _last_error is not None and not callable(_last_error):
            raise TypeError("Win32 last-error reader must be callable")
        self.__value = value
        self.__close = _close
        self.__last_error = _last_error or (lambda: 0)
        self.__closed = False
        self.__lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self.__lock:
            return self.__closed

    def borrow(self) -> _Win32HandleBorrow:
        """Return the sole context guard for serialized native HANDLE use."""

        return _Win32HandleBorrow(self)

    def close(self) -> None:
        with self.__lock:
            if self.__closed:
                return
            value = self.__value
            self.__value = 0
            self.__closed = True
            if not self.__close(value):
                error = int(self.__last_error())
                raise Win32CallError("CloseHandle", error)

    def __enter__(self) -> Win32Handle:
        with self.borrow():
            pass
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def __copy__(self) -> object:
        raise TypeError("Win32Handle is an opaque live authority")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("Win32Handle is an opaque live authority")

    def __reduce__(self) -> object:
        raise TypeError("Win32Handle is an opaque live authority")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Win32Handle is an opaque live authority")

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


PDWORD = ctypes.POINTER(DWORD)
PWORD = ctypes.POINTER(WORD)
PBOOL = ctypes.POINTER(BOOL)
PHANDLE = ctypes.POINTER(HANDLE)
POVERLAPPED = ctypes.POINTER(OVERLAPPED)
PSECURITY_ATTRIBUTES = ctypes.POINTER(SECURITY_ATTRIBUTES)
PPRIVILEGE_SET = ctypes.POINTER(PRIVILEGE_SET)

# This table is the single audit surface for the ABI consumed by Tasks 3.1-3.6.
# Constructor declarations are checked against it before a function is exposed.
WIN32_SIGNATURES: dict[str, tuple[tuple[object, ...], object]] = {
    "CreateFileW": ((LPCWSTR, DWORD, DWORD, PSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE), HANDLE),
    "CreateDirectoryW": ((LPCWSTR, PSECURITY_ATTRIBUTES), BOOL),
    "CloseHandle": ((HANDLE,), BOOL),
    "DuplicateHandle": ((HANDLE, HANDLE, HANDLE, PHANDLE, DWORD, BOOL, DWORD), BOOL),
    "ReadFile": ((HANDLE, LPVOID, DWORD, PDWORD, POVERLAPPED), BOOL),
    "WriteFile": ((HANDLE, LPCVOID, DWORD, PDWORD, POVERLAPPED), BOOL),
    "GetFileInformationByHandleEx": ((HANDLE, ctypes.c_int, LPVOID, DWORD), BOOL),
    "GetFinalPathNameByHandleW": ((HANDLE, LPWSTR, DWORD, DWORD), DWORD),
    "SetFilePointerEx": ((HANDLE, LARGE_INTEGER, ctypes.POINTER(LARGE_INTEGER), DWORD), BOOL),
    "SetEndOfFile": ((HANDLE,), BOOL),
    "SetFileInformationByHandle": ((HANDLE, ctypes.c_int, LPVOID, DWORD), BOOL),
    "FlushFileBuffers": ((HANDLE,), BOOL),
    "LockFileEx": ((HANDLE, DWORD, DWORD, DWORD, DWORD, POVERLAPPED), BOOL),
    "UnlockFileEx": ((HANDLE, DWORD, DWORD, DWORD, POVERLAPPED), BOOL),
    "GetVolumePathNameW": ((LPCWSTR, LPWSTR, DWORD), BOOL),
    "GetVolumeNameForVolumeMountPointW": ((LPCWSTR, LPWSTR, DWORD), BOOL),
    "GetVolumeInformationW": ((LPCWSTR, LPWSTR, DWORD, PDWORD, PDWORD, PDWORD, LPWSTR, DWORD), BOOL),
    "GetVolumeInformationByHandleW": ((HANDLE, LPWSTR, DWORD, PDWORD, PDWORD, PDWORD, LPWSTR, DWORD), BOOL),
    "GetDriveTypeW": ((LPCWSTR,), UINT),
    "CreateEventW": ((PSECURITY_ATTRIBUTES, BOOL, BOOL, LPCWSTR), HANDLE),
    "WaitForSingleObject": ((HANDLE, DWORD), DWORD),
    "CancelIoEx": ((HANDLE, POVERLAPPED), BOOL),
    "GetOverlappedResult": ((HANDLE, POVERLAPPED, PDWORD, BOOL), BOOL),
    "GetCurrentProcess": ((), HANDLE),
    "IsWow64Process2": ((HANDLE, PWORD, PWORD), BOOL),
    "LocalFree": ((HLOCAL,), HLOCAL),
    "GetSecurityInfo": ((HANDLE, ctypes.c_int, DWORD, ctypes.POINTER(PSID), ctypes.POINTER(PSID), ctypes.POINTER(PACL), ctypes.POINTER(PACL), ctypes.POINTER(PSECURITY_DESCRIPTOR)), DWORD),
    "SetSecurityInfo": ((HANDLE, ctypes.c_int, DWORD, PSID, PSID, PACL, PACL), DWORD),
    "AccessCheck": ((PSECURITY_DESCRIPTOR, HANDLE, DWORD, ctypes.POINTER(GENERIC_MAPPING), PPRIVILEGE_SET, PDWORD, PDWORD, PBOOL), BOOL),
    "MapGenericMask": ((PDWORD, ctypes.POINTER(GENERIC_MAPPING)), None),
    "OpenProcessToken": ((HANDLE, DWORD, PHANDLE), BOOL),
    "DuplicateTokenEx": ((HANDLE, DWORD, PSECURITY_ATTRIBUTES, ctypes.c_int, ctypes.c_int, PHANDLE), BOOL),
    "IsTokenRestricted": ((HANDLE,), BOOL),
    "GetTokenInformation": ((HANDLE, ctypes.c_int, LPVOID, DWORD, PDWORD), BOOL),
    "GetSecurityDescriptorControl": ((PSECURITY_DESCRIPTOR, ctypes.POINTER(WORD), PDWORD), BOOL),
    "GetAclInformation": ((PACL, LPVOID, DWORD, ctypes.c_int), BOOL),
    "GetAce": ((PACL, DWORD, ctypes.POINTER(LPVOID)), BOOL),
    "GetLengthSid": ((PSID,), DWORD),
    "CreateWellKnownSid": ((ctypes.c_int, PSID, PSID, PDWORD), BOOL),
    "EqualSid": ((PSID, PSID), BOOL),
    "ConvertStringSecurityDescriptorToSecurityDescriptorW": ((LPCWSTR, DWORD, ctypes.POINTER(PSECURITY_DESCRIPTOR), PDWORD), BOOL),
    "GetSecurityDescriptorOwner": ((PSECURITY_DESCRIPTOR, ctypes.POINTER(PSID), PBOOL), BOOL),
    "GetSecurityDescriptorDacl": ((PSECURITY_DESCRIPTOR, PBOOL, ctypes.POINTER(PACL), PBOOL), BOOL),
    "GetSecurityDescriptorSacl": ((PSECURITY_DESCRIPTOR, PBOOL, ctypes.POINTER(PACL), PBOOL), BOOL),
    "IsValidSecurityDescriptor": ((PSECURITY_DESCRIPTOR,), BOOL),
    "IsValidAcl": ((PACL,), BOOL),
    "IsValidSid": ((PSID,), BOOL),
    "CopySid": ((DWORD, PSID, PSID), BOOL),
    "GetSidSubAuthorityCount": ((PSID,), ctypes.POINTER(BYTE)),
    "GetSidSubAuthority": ((PSID, DWORD), PDWORD),
}

NTDLL_SIGNATURES: dict[str, tuple[tuple[object, ...], object]] = {
    "NtSetInformationFile": (
        (
            HANDLE,
            ctypes.POINTER(IO_STATUS_BLOCK),
            LPVOID,
            ULONG,
            ctypes.c_int,
        ),
        NTSTATUS,
    ),
}


def _bind(dll: object, name: str, argtypes: list[object], restype: object) -> object:
    expected_argtypes, expected_restype = WIN32_SIGNATURES[name]
    if tuple(argtypes) != expected_argtypes or restype is not expected_restype:
        raise RuntimeError(f"Win32 signature declaration drift: {name}")
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _bind_ntdll(
    dll: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> object:
    expected_argtypes, expected_restype = NTDLL_SIGNATURES[name]
    if tuple(argtypes) != expected_argtypes or restype is not expected_restype:
        raise RuntimeError(f"Ntdll signature declaration drift: {name}")
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _ntstatus_value(status: object) -> int:
    value = status.value if isinstance(status, ctypes._SimpleCData) else status
    return int(value) & 0xFFFFFFFF


def _build_file_rename_information(
    component: str,
    *,
    replace_if_exists: bool,
) -> tuple[ctypes.Array[ctypes.c_char], int]:
    if type(component) is not str or not component:
        raise TypeError("component must be a non-empty exact str")
    if type(replace_if_exists) is not bool:
        raise TypeError("replace_if_exists must be exact bool")
    if any(value in component for value in ("\0", "/", "\\", ":")):
        raise ValueError("native rename accepts one relative path component")
    try:
        encoded = component.encode("utf-16-le")
    except UnicodeEncodeError:
        raise ValueError("component must be valid UTF-16") from None
    if not encoded or len(encoded) > 0xFFFFFFFF:
        raise ValueError("component length is outside native ABI")
    exact_length = FILE_RENAME_INFORMATION.FileName.offset + len(encoded)
    # The kernel receives only ``exact_length`` bytes.  Keep the local ctypes
    # backing allocation large enough for its declared one-wchar tail even for
    # a one-code-unit component.
    storage = ctypes.create_string_buffer(
        max(exact_length, ctypes.sizeof(FILE_RENAME_INFORMATION))
    )
    information = ctypes.cast(
        storage,
        ctypes.POINTER(FILE_RENAME_INFORMATION),
    ).contents
    information.ReplaceIfExists = int(replace_if_exists)
    information.RootDirectory = None
    information.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(storage) + FILE_RENAME_INFORMATION.FileName.offset,
        encoded,
        len(encoded),
    )
    return storage, exact_length


class WindowsFileAPI:
    """Loaded and signature-checked Kernel32/Advapi32/Ntdll function table."""

    def __init__(
        self,
        kernel32: object,
        advapi32: object,
        ntdll_loader: Callable[[], object],
    ) -> None:
        if not callable(ntdll_loader):
            raise TypeError("ntdll_loader must be callable")
        self._ntdll_loader = ntdll_loader
        self._ntdll_bind_lock = threading.Lock()
        pdword = ctypes.POINTER(DWORD)
        phandle = ctypes.POINTER(HANDLE)
        poverlapped = ctypes.POINTER(OVERLAPPED)
        psecurity_attributes = ctypes.POINTER(SECURITY_ATTRIBUTES)
        self.CreateFileW = _bind(
            kernel32,
            "CreateFileW",
            [LPCWSTR, DWORD, DWORD, psecurity_attributes, DWORD, DWORD, HANDLE],
            HANDLE,
        )
        self.CreateDirectoryW = _bind(
            kernel32,
            "CreateDirectoryW",
            [LPCWSTR, psecurity_attributes],
            BOOL,
        )
        self.CloseHandle = _bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self.DuplicateHandle = _bind(
            kernel32,
            "DuplicateHandle",
            [HANDLE, HANDLE, HANDLE, phandle, DWORD, BOOL, DWORD],
            BOOL,
        )
        self.ReadFile = _bind(
            kernel32, "ReadFile", [HANDLE, LPVOID, DWORD, pdword, poverlapped], BOOL
        )
        self.WriteFile = _bind(
            kernel32, "WriteFile", [HANDLE, LPCVOID, DWORD, pdword, poverlapped], BOOL
        )
        self.GetFileInformationByHandleEx = _bind(
            kernel32,
            "GetFileInformationByHandleEx",
            [HANDLE, ctypes.c_int, LPVOID, DWORD],
            BOOL,
        )
        self.GetFinalPathNameByHandleW = _bind(
            kernel32,
            "GetFinalPathNameByHandleW",
            [HANDLE, LPWSTR, DWORD, DWORD],
            DWORD,
        )
        self.SetFilePointerEx = _bind(
            kernel32,
            "SetFilePointerEx",
            [HANDLE, LARGE_INTEGER, ctypes.POINTER(LARGE_INTEGER), DWORD],
            BOOL,
        )
        self.SetEndOfFile = _bind(kernel32, "SetEndOfFile", [HANDLE], BOOL)
        self.SetFileInformationByHandle = _bind(
            kernel32,
            "SetFileInformationByHandle",
            [HANDLE, ctypes.c_int, LPVOID, DWORD],
            BOOL,
        )
        self.FlushFileBuffers = _bind(
            kernel32, "FlushFileBuffers", [HANDLE], BOOL
        )
        self.LockFileEx = _bind(
            kernel32,
            "LockFileEx",
            [HANDLE, DWORD, DWORD, DWORD, DWORD, poverlapped],
            BOOL,
        )
        self.UnlockFileEx = _bind(
            kernel32,
            "UnlockFileEx",
            [HANDLE, DWORD, DWORD, DWORD, poverlapped],
            BOOL,
        )
        self.GetVolumePathNameW = _bind(
            kernel32, "GetVolumePathNameW", [LPCWSTR, LPWSTR, DWORD], BOOL
        )
        self.GetVolumeNameForVolumeMountPointW = _bind(
            kernel32,
            "GetVolumeNameForVolumeMountPointW",
            [LPCWSTR, LPWSTR, DWORD],
            BOOL,
        )
        self.GetVolumeInformationW = _bind(
            kernel32,
            "GetVolumeInformationW",
            [LPCWSTR, LPWSTR, DWORD, pdword, pdword, pdword, LPWSTR, DWORD],
            BOOL,
        )
        self.GetVolumeInformationByHandleW = _bind(
            kernel32,
            "GetVolumeInformationByHandleW",
            [HANDLE, LPWSTR, DWORD, pdword, pdword, pdword, LPWSTR, DWORD],
            BOOL,
        )
        self.GetDriveTypeW = _bind(
            kernel32, "GetDriveTypeW", [LPCWSTR], UINT
        )
        self.CreateEventW = _bind(
            kernel32,
            "CreateEventW",
            [psecurity_attributes, BOOL, BOOL, LPCWSTR],
            HANDLE,
        )
        self.WaitForSingleObject = _bind(
            kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD
        )
        self.CancelIoEx = _bind(
            kernel32, "CancelIoEx", [HANDLE, poverlapped], BOOL
        )
        self.GetOverlappedResult = _bind(
            kernel32,
            "GetOverlappedResult",
            [HANDLE, poverlapped, pdword, BOOL],
            BOOL,
        )
        self.GetCurrentProcess = _bind(kernel32, "GetCurrentProcess", [], HANDLE)
        self.IsWow64Process2 = _bind(
            kernel32,
            "IsWow64Process2",
            [HANDLE, ctypes.POINTER(WORD), ctypes.POINTER(WORD)],
            BOOL,
        )
        self.LocalFree = _bind(kernel32, "LocalFree", [HLOCAL], HLOCAL)

        self.GetSecurityInfo = _bind(
            advapi32,
            "GetSecurityInfo",
            [
                HANDLE,
                ctypes.c_int,
                DWORD,
                ctypes.POINTER(PSID),
                ctypes.POINTER(PSID),
                ctypes.POINTER(PACL),
                ctypes.POINTER(PACL),
                ctypes.POINTER(PSECURITY_DESCRIPTOR),
            ],
            DWORD,
        )
        self.SetSecurityInfo = _bind(
            advapi32,
            "SetSecurityInfo",
            [HANDLE, ctypes.c_int, DWORD, PSID, PSID, PACL, PACL],
            DWORD,
        )
        self.AccessCheck = _bind(
            advapi32,
            "AccessCheck",
            [
                PSECURITY_DESCRIPTOR,
                HANDLE,
                DWORD,
                ctypes.POINTER(GENERIC_MAPPING),
                ctypes.POINTER(PRIVILEGE_SET),
                pdword,
                pdword,
                ctypes.POINTER(BOOL),
            ],
            BOOL,
        )
        self.MapGenericMask = _bind(
            advapi32,
            "MapGenericMask",
            [pdword, ctypes.POINTER(GENERIC_MAPPING)],
            None,
        )
        self.OpenProcessToken = _bind(
            advapi32, "OpenProcessToken", [HANDLE, DWORD, phandle], BOOL
        )
        self.DuplicateTokenEx = _bind(
            advapi32,
            "DuplicateTokenEx",
            [HANDLE, DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), ctypes.c_int, ctypes.c_int, phandle],
            BOOL,
        )
        self.IsTokenRestricted = _bind(
            advapi32,
            "IsTokenRestricted",
            [HANDLE],
            BOOL,
        )
        self.GetTokenInformation = _bind(
            advapi32,
            "GetTokenInformation",
            [HANDLE, ctypes.c_int, LPVOID, DWORD, pdword],
            BOOL,
        )
        self.GetSecurityDescriptorControl = _bind(
            advapi32,
            "GetSecurityDescriptorControl",
            [PSECURITY_DESCRIPTOR, ctypes.POINTER(WORD), pdword],
            BOOL,
        )
        self.GetAclInformation = _bind(
            advapi32,
            "GetAclInformation",
            [PACL, LPVOID, DWORD, ctypes.c_int],
            BOOL,
        )
        self.GetAce = _bind(
            advapi32, "GetAce", [PACL, DWORD, ctypes.POINTER(LPVOID)], BOOL
        )
        self.GetLengthSid = _bind(advapi32, "GetLengthSid", [PSID], DWORD)
        self.CreateWellKnownSid = _bind(
            advapi32,
            "CreateWellKnownSid",
            [ctypes.c_int, PSID, PSID, pdword],
            BOOL,
        )
        self.EqualSid = _bind(advapi32, "EqualSid", [PSID, PSID], BOOL)
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = _bind(
            advapi32,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [LPCWSTR, DWORD, ctypes.POINTER(PSECURITY_DESCRIPTOR), pdword],
            BOOL,
        )
        self.GetSecurityDescriptorOwner = _bind(
            advapi32,
            "GetSecurityDescriptorOwner",
            [PSECURITY_DESCRIPTOR, ctypes.POINTER(PSID), ctypes.POINTER(BOOL)],
            BOOL,
        )
        self.GetSecurityDescriptorDacl = _bind(
            advapi32,
            "GetSecurityDescriptorDacl",
            [
                PSECURITY_DESCRIPTOR,
                ctypes.POINTER(BOOL),
                ctypes.POINTER(PACL),
                ctypes.POINTER(BOOL),
            ],
            BOOL,
        )
        self.GetSecurityDescriptorSacl = _bind(
            advapi32,
            "GetSecurityDescriptorSacl",
            [
                PSECURITY_DESCRIPTOR,
                ctypes.POINTER(BOOL),
                ctypes.POINTER(PACL),
                ctypes.POINTER(BOOL),
            ],
            BOOL,
        )
        self.IsValidSecurityDescriptor = _bind(
            advapi32,
            "IsValidSecurityDescriptor",
            [PSECURITY_DESCRIPTOR],
            BOOL,
        )
        self.IsValidAcl = _bind(advapi32, "IsValidAcl", [PACL], BOOL)
        self.IsValidSid = _bind(advapi32, "IsValidSid", [PSID], BOOL)
        self.CopySid = _bind(
            advapi32, "CopySid", [DWORD, PSID, PSID], BOOL
        )
        self.GetSidSubAuthorityCount = _bind(
            advapi32,
            "GetSidSubAuthorityCount",
            [PSID],
            ctypes.POINTER(BYTE),
        )
        self.GetSidSubAuthority = _bind(
            advapi32,
            "GetSidSubAuthority",
            [PSID, DWORD],
            pdword,
        )
    @classmethod
    def load(cls, *, _dll_loader: object | None = None) -> WindowsFileAPI:
        if sys.platform != "win32":
            raise Win32CallError("WinDLL", 0)
        loader = _dll_loader or getattr(ctypes, "WinDLL", None)
        if not callable(loader):
            raise Win32CallError("WinDLL", 0)
        kernel32 = loader("kernel32.dll", use_last_error=True)
        advapi32 = loader("advapi32.dll", use_last_error=True)
        return cls(
            kernel32,
            advapi32,
            lambda: loader("ntdll.dll", use_last_error=True),
        )

    @staticmethod
    def last_error() -> int:
        reader = getattr(ctypes, "get_last_error", None)
        return int(reader()) if callable(reader) else 0

    def checked_bool(self, name: str, function: object, *args: object) -> None:
        result = function(*args)
        if not result:
            error = self.last_error()
            raise Win32CallError(name, error)

    def checked_handle_value(
        self,
        name: str,
        raw: object,
        *,
        invalid_value: int = INVALID_HANDLE_VALUE,
    ) -> int:
        value = int(raw) if raw is not None else 0
        if value in {0, invalid_value}:
            error = self.last_error()
            raise Win32CallError(name, error)
        return value

    @staticmethod
    def checked_status_zero(name: str, status: object) -> None:
        value = int(status)
        if value != 0:
            raise Win32CallError(name, value)

    @staticmethod
    def checked_ntstatus_zero(name: str, status: object) -> None:
        value = _ntstatus_value(status)
        if value != 0:
            raise NtStatusError(name, value)

    def _nt_set_information_file(self) -> object:
        function = getattr(self, "NtSetInformationFile", None)
        if callable(function):
            return function
        with self._ntdll_bind_lock:
            function = getattr(self, "NtSetInformationFile", None)
            if callable(function):
                return function
            ntdll = self._ntdll_loader()
            function = _bind_ntdll(
                ntdll,
                "NtSetInformationFile",
                [
                    HANDLE,
                    ctypes.POINTER(IO_STATUS_BLOCK),
                    LPVOID,
                    ULONG,
                    ctypes.c_int,
                ],
                NTSTATUS,
            )
            self.NtSetInformationFile = function
            return function

    def self_probe_nt_set_information_file(self) -> None:
        function = self._nt_set_information_file()
        if (
            ctypes.sizeof(FILE_RENAME_INFORMATION) != 24
            or FILE_RENAME_INFORMATION.ReplaceIfExists.offset != 0
            or FILE_RENAME_INFORMATION.RootDirectory.offset != 8
            or FILE_RENAME_INFORMATION.FileNameLength.offset != 16
            or FILE_RENAME_INFORMATION.FileName.offset != 20
            or ctypes.sizeof(IO_STATUS_BLOCK) != 16
            or IO_STATUS_BLOCK.Status.offset != 0
            or IO_STATUS_BLOCK.Information.offset != 8
            or tuple(function.argtypes)
            != NTDLL_SIGNATURES["NtSetInformationFile"][0]
            or function.restype
            is not NTDLL_SIGNATURES["NtSetInformationFile"][1]
        ):
            raise RuntimeError("native rename ABI self-probe failed")
        storage, exact_length = _build_file_rename_information(
            "__",
            replace_if_exists=False,
        )
        io_status = IO_STATUS_BLOCK()
        status = function(
            None,
            ctypes.byref(io_status),
            storage,
            exact_length,
            FILE_RENAME_INFORMATION_CLASS,
        )
        value = _ntstatus_value(status)
        if value != STATUS_INVALID_HANDLE:
            raise NtStatusError("NtSetInformationFile.self_probe", value)

    def rename_file_same_parent(
        self,
        raw_handle: int,
        component: str,
        *,
        replace_if_exists: bool,
    ) -> None:
        if type(raw_handle) is not int or raw_handle in {0, INVALID_HANDLE_VALUE}:
            raise TypeError("raw_handle must be an exact live HANDLE value")
        storage, exact_length = _build_file_rename_information(
            component,
            replace_if_exists=replace_if_exists,
        )
        io_status = IO_STATUS_BLOCK()
        status = self._nt_set_information_file()(
            raw_handle,
            ctypes.byref(io_status),
            storage,
            exact_length,
            FILE_RENAME_INFORMATION_CLASS,
        )
        self.checked_ntstatus_zero("NtSetInformationFile", status)
        self.checked_ntstatus_zero("NtSetInformationFile.IO_STATUS_BLOCK", io_status.Status)

    def checked_length(self, name: str, result: object) -> int:
        value = int(result)
        if value == 0:
            error = self.last_error()
            raise Win32CallError(name, error)
        return value

    def checked_wait(self, name: str, result: object) -> int:
        value = int(result)
        if value == WAIT_FAILED:
            error = self.last_error()
            raise Win32CallError(name, error)
        if value not in {WAIT_OBJECT_0, WAIT_TIMEOUT}:
            raise Win32CallError(name, value)
        return value

    def checked_nonnull_pointer(self, name: str, result: object) -> int:
        if result is None:
            value = 0
        elif isinstance(result, int):
            value = result
        else:
            value = int(ctypes.cast(result, ctypes.c_void_p).value or 0)
        if value == 0:
            error = self.last_error()
            raise Win32CallError(name, error)
        return value

    def checked_local_free(self, memory: object) -> None:
        result = self.LocalFree(memory)
        if result is None:
            value = 0
        elif isinstance(result, int):
            value = result
        else:
            value = int(ctypes.cast(result, ctypes.c_void_p).value or 0)
        if value != 0:
            error = self.last_error()
            raise Win32CallError("LocalFree", error)

    def open_handle(
        self,
        path: str,
        *,
        desired_access: int,
        share_mode: int,
        creation_disposition: int,
        flags: int,
        security_attributes: SECURITY_ATTRIBUTES | None = None,
    ) -> Win32Handle:
        if security_attributes is not None and type(security_attributes) is not SECURITY_ATTRIBUTES:
            raise TypeError("security_attributes must be exact SECURITY_ATTRIBUTES")
        security_pointer = (
            None
            if security_attributes is None
            else ctypes.byref(security_attributes)
        )
        raw = self.CreateFileW(
            path,
            desired_access,
            share_mode,
            security_pointer,
            creation_disposition,
            flags,
            None,
        )
        value = self.checked_handle_value("CreateFileW", raw)
        return Win32Handle(
            value,
            _close=self.CloseHandle,
            _last_error=self.last_error,
        )

    def duplicate_handle(self, source: Win32Handle) -> Win32Handle:
        if not isinstance(source, Win32Handle):
            raise TypeError("source must be Win32Handle")
        process = self.GetCurrentProcess()
        duplicate = HANDLE()
        with source.borrow() as raw_source:
            self.checked_bool(
                "DuplicateHandle",
                self.DuplicateHandle,
                process,
                raw_source,
                process,
                ctypes.byref(duplicate),
                0,
                0,
                DUPLICATE_SAME_ACCESS,
            )
        value = int(duplicate.value or 0)
        if value == 0:
            raise Win32CallError("DuplicateHandle", ERROR_INVALID_HANDLE)
        return Win32Handle(
            value,
            _close=self.CloseHandle,
            _last_error=self.last_error,
        )
