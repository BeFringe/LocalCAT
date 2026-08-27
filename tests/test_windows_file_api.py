"""Task 3.1 Win32 FFI declarations and RAII-handle tests."""

from __future__ import annotations

import ast
import copy
import ctypes
from pathlib import Path
import pickle
import sys
import threading
import unittest
from unittest import mock

import windows_file_api as api_module
from windows_file_api import (
    ACCESS_ALLOWED_ACE,
    ACL_SIZE_INFORMATION,
    ACE_HEADER,
    FILE_ATTRIBUTE_TAG_INFO,
    FILE_ID_128,
    FILE_ID_INFO,
    FILE_RENAME_INFO,
    FILE_STANDARD_INFO,
    GENERIC_MAPPING,
    OVERLAPPED,
    STORAGE_DEVICE_DESCRIPTOR,
    STORAGE_DESCRIPTOR_HEADER,
    STORAGE_PROPERTY_QUERY,
    STORAGE_WRITE_CACHE_PROPERTY,
    SYSTEM_MANDATORY_LABEL_ACE,
    TOKEN_MANDATORY_LABEL,
    TOKEN_USER,
    Win32CallError,
    Win32Handle,
    WindowsFileAPI,
)


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "windows_file_api.py"


class Win32APIStaticTests(unittest.TestCase):
    def test_non_windows_import_does_not_load_any_dll(self) -> None:
        source = API_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_calls = [
            node
            for statement in tree.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "WinDLL"
        ]
        self.assertEqual(top_level_calls, [])

    @unittest.skipUnless(sys.platform == "win32", "exact Win32 ABI requires Windows")
    def test_structure_sizes_match_windows_x64_abi(self) -> None:
        for structure in (
            FILE_ID_128,
            FILE_ID_INFO,
            FILE_ATTRIBUTE_TAG_INFO,
            FILE_STANDARD_INFO,
            FILE_RENAME_INFO,
            OVERLAPPED,
            GENERIC_MAPPING,
            ACL_SIZE_INFORMATION,
            TOKEN_USER,
            TOKEN_MANDATORY_LABEL,
            ACE_HEADER,
            ACCESS_ALLOWED_ACE,
            SYSTEM_MANDATORY_LABEL_ACE,
            STORAGE_PROPERTY_QUERY,
            STORAGE_DESCRIPTOR_HEADER,
            STORAGE_DEVICE_DESCRIPTOR,
            STORAGE_WRITE_CACHE_PROPERTY,
        ):
            self.assertEqual(structure._layout_, "ms")
        self.assertEqual(ctypes.sizeof(FILE_ID_128), 16)
        self.assertEqual(ctypes.sizeof(FILE_ID_INFO), 24)
        self.assertEqual(ctypes.sizeof(FILE_ATTRIBUTE_TAG_INFO), 8)
        self.assertEqual(ctypes.sizeof(FILE_STANDARD_INFO), 24)
        self.assertEqual(ctypes.sizeof(FILE_RENAME_INFO), 24)
        self.assertEqual(ctypes.sizeof(OVERLAPPED), 32)
        self.assertEqual(ctypes.sizeof(GENERIC_MAPPING), 16)
        self.assertEqual(ctypes.sizeof(ACL_SIZE_INFORMATION), 12)
        self.assertEqual(ctypes.sizeof(TOKEN_USER), 16)
        self.assertEqual(ctypes.sizeof(TOKEN_MANDATORY_LABEL), 16)
        self.assertEqual(ctypes.sizeof(ACE_HEADER), 4)
        self.assertEqual(ctypes.sizeof(ACCESS_ALLOWED_ACE), 12)
        self.assertEqual(ctypes.sizeof(SYSTEM_MANDATORY_LABEL_ACE), 12)
        self.assertEqual(ctypes.sizeof(STORAGE_PROPERTY_QUERY), 12)
        self.assertEqual(ctypes.sizeof(STORAGE_DESCRIPTOR_HEADER), 8)
        self.assertEqual(ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR), 40)
        self.assertEqual(ctypes.sizeof(STORAGE_WRITE_CACHE_PROPERTY), 28)

        m = api_module
        layout_oracle = {
            m.FILE_ID_128: (16, {"Identifier": 0}),
            m.FILE_ID_INFO: (24, {"VolumeSerialNumber": 0, "FileId": 8}),
            m.FILE_ATTRIBUTE_TAG_INFO: (8, {"FileAttributes": 0, "ReparseTag": 4}),
            m.FILE_STANDARD_INFO: (24, {"AllocationSize": 0, "EndOfFile": 8, "NumberOfLinks": 16, "DeletePending": 20, "Directory": 21}),
            m._LARGE_INTEGER_PARTS: (8, {"LowPart": 0, "HighPart": 4}),
            m.LARGE_INTEGER: (8, {"parts": 0, "QuadPart": 0}),
            m.FILE_BASIC_INFO: (40, {"CreationTime": 0, "LastAccessTime": 8, "LastWriteTime": 16, "ChangeTime": 24, "FileAttributes": 32}),
            m._OVERLAPPED_OFFSET: (8, {"Offset": 0, "OffsetHigh": 4}),
            m._OVERLAPPED_UNION: (8, {"offset": 0, "Pointer": 0}),
            m.OVERLAPPED: (32, {"Internal": 0, "InternalHigh": 8, "position": 16, "hEvent": 24}),
            m._FILE_RENAME_NAME: (4, {"ReplaceIfExists": 0, "Flags": 0}),
            m.FILE_RENAME_INFO: (24, {"mode": 0, "RootDirectory": 8, "FileNameLength": 16, "FileName": 20}),
            m.GENERIC_MAPPING: (16, {"GenericRead": 0, "GenericWrite": 4, "GenericExecute": 8, "GenericAll": 12}),
            m.ACL_SIZE_INFORMATION: (12, {"AceCount": 0, "AclBytesInUse": 4, "AclBytesFree": 8}),
            m.LUID: (8, {"LowPart": 0, "HighPart": 4}),
            m.LUID_AND_ATTRIBUTES: (12, {"Luid": 0, "Attributes": 8}),
            m.PRIVILEGE_SET: (20, {"PrivilegeCount": 0, "Control": 4, "Privilege": 8}),
            m.SID_AND_ATTRIBUTES: (16, {"Sid": 0, "Attributes": 8}),
            m.TOKEN_USER: (16, {"User": 0}),
            m.TOKEN_MANDATORY_LABEL: (16, {"Label": 0}),
            m.ACE_HEADER: (4, {"AceType": 0, "AceFlags": 1, "AceSize": 2}),
            m.ACCESS_ALLOWED_ACE: (12, {"Header": 0, "Mask": 4, "SidStart": 8}),
            m.SYSTEM_MANDATORY_LABEL_ACE: (12, {"Header": 0, "Mask": 4, "SidStart": 8}),
            m.SECURITY_ATTRIBUTES: (24, {"nLength": 0, "lpSecurityDescriptor": 8, "bInheritHandle": 16}),
            m.STORAGE_PROPERTY_QUERY: (12, {"PropertyId": 0, "QueryType": 4, "AdditionalParameters": 8}),
            m.STORAGE_DESCRIPTOR_HEADER: (8, {"Version": 0, "Size": 4}),
            m.STORAGE_DEVICE_DESCRIPTOR: (40, {"Version": 0, "Size": 4, "DeviceType": 8, "VendorIdOffset": 12, "BusType": 28, "RawPropertiesLength": 32, "RawDeviceProperties": 36}),
            m.STORAGE_WRITE_CACHE_PROPERTY: (28, {"Version": 0, "Size": 4, "WriteCacheType": 8, "WriteCacheEnabled": 12, "WriteCacheChangeable": 16, "WriteThroughSupported": 20, "FlushCacheSupported": 24, "UserDefinedPowerProtection": 25, "NVCacheEnabled": 26}),
        }
        for structure, (expected_size, offsets) in layout_oracle.items():
            with self.subTest(structure=structure.__name__):
                self.assertEqual(structure._layout_, "ms")
                self.assertEqual(ctypes.sizeof(structure), expected_size)
                self.assertEqual(
                    {name: getattr(structure, name).offset for name in offsets},
                    offsets,
                )

    @unittest.skipUnless(sys.platform == "win32", "exact Win32 ABI requires Windows")
    def test_exact_functions_are_bound_with_argtypes_restype_and_last_error(self) -> None:
        loaded: list[tuple[str, bool]] = []
        real_loader = ctypes.WinDLL

        def recording_loader(name: str, *, use_last_error: bool) -> object:
            loaded.append((name, use_last_error))
            return real_loader(name, use_last_error=use_last_error)

        api = WindowsFileAPI.load(_dll_loader=recording_loader)
        self.assertEqual(
            loaded,
            [("kernel32.dll", True), ("advapi32.dll", True)],
        )
        m = api_module
        pointer = ctypes.POINTER
        expected = {
            "CreateFileW": ((m.LPCWSTR, m.DWORD, m.DWORD, pointer(m.SECURITY_ATTRIBUTES), m.DWORD, m.DWORD, m.HANDLE), m.HANDLE),
            "CloseHandle": ((m.HANDLE,), m.BOOL),
            "DuplicateHandle": ((m.HANDLE, m.HANDLE, m.HANDLE, pointer(m.HANDLE), m.DWORD, m.BOOL, m.DWORD), m.BOOL),
            "ReadFile": ((m.HANDLE, m.LPVOID, m.DWORD, pointer(m.DWORD), pointer(m.OVERLAPPED)), m.BOOL),
            "WriteFile": ((m.HANDLE, m.LPCVOID, m.DWORD, pointer(m.DWORD), pointer(m.OVERLAPPED)), m.BOOL),
            "GetFileInformationByHandleEx": ((m.HANDLE, ctypes.c_int, m.LPVOID, m.DWORD), m.BOOL),
            "GetFinalPathNameByHandleW": ((m.HANDLE, m.LPWSTR, m.DWORD, m.DWORD), m.DWORD),
            "SetFilePointerEx": ((m.HANDLE, m.LARGE_INTEGER, pointer(m.LARGE_INTEGER), m.DWORD), m.BOOL),
            "SetFileInformationByHandle": ((m.HANDLE, ctypes.c_int, m.LPVOID, m.DWORD), m.BOOL),
            "FlushFileBuffers": ((m.HANDLE,), m.BOOL),
            "LockFileEx": ((m.HANDLE, m.DWORD, m.DWORD, m.DWORD, m.DWORD, pointer(m.OVERLAPPED)), m.BOOL),
            "UnlockFileEx": ((m.HANDLE, m.DWORD, m.DWORD, m.DWORD, pointer(m.OVERLAPPED)), m.BOOL),
            "GetVolumePathNameW": ((m.LPCWSTR, m.LPWSTR, m.DWORD), m.BOOL),
            "GetVolumeNameForVolumeMountPointW": ((m.LPCWSTR, m.LPWSTR, m.DWORD), m.BOOL),
            "GetVolumeInformationW": ((m.LPCWSTR, m.LPWSTR, m.DWORD, pointer(m.DWORD), pointer(m.DWORD), pointer(m.DWORD), m.LPWSTR, m.DWORD), m.BOOL),
            "GetVolumeInformationByHandleW": ((m.HANDLE, m.LPWSTR, m.DWORD, pointer(m.DWORD), pointer(m.DWORD), pointer(m.DWORD), m.LPWSTR, m.DWORD), m.BOOL),
            "GetDriveTypeW": ((m.LPCWSTR,), m.UINT),
            "DeviceIoControl": ((m.HANDLE, m.DWORD, m.LPVOID, m.DWORD, m.LPVOID, m.DWORD, pointer(m.DWORD), pointer(m.OVERLAPPED)), m.BOOL),
            "CreateEventW": ((pointer(m.SECURITY_ATTRIBUTES), m.BOOL, m.BOOL, m.LPCWSTR), m.HANDLE),
            "WaitForSingleObject": ((m.HANDLE, m.DWORD), m.DWORD),
            "CancelIoEx": ((m.HANDLE, pointer(m.OVERLAPPED)), m.BOOL),
            "GetOverlappedResult": ((m.HANDLE, pointer(m.OVERLAPPED), pointer(m.DWORD), m.BOOL), m.BOOL),
            "GetCurrentProcess": ((), m.HANDLE),
            "IsWow64Process2": ((m.HANDLE, pointer(m.WORD), pointer(m.WORD)), m.BOOL),
            "LocalFree": ((m.HLOCAL,), m.HLOCAL),
            "GetSecurityInfo": ((m.HANDLE, ctypes.c_int, m.DWORD, pointer(m.PSID), pointer(m.PSID), pointer(m.PACL), pointer(m.PACL), pointer(m.PSECURITY_DESCRIPTOR)), m.DWORD),
            "SetSecurityInfo": ((m.HANDLE, ctypes.c_int, m.DWORD, m.PSID, m.PSID, m.PACL, m.PACL), m.DWORD),
            "AccessCheck": ((m.PSECURITY_DESCRIPTOR, m.HANDLE, m.DWORD, pointer(m.GENERIC_MAPPING), pointer(m.PRIVILEGE_SET), pointer(m.DWORD), pointer(m.DWORD), pointer(m.BOOL)), m.BOOL),
            "MapGenericMask": ((pointer(m.DWORD), pointer(m.GENERIC_MAPPING)), None),
            "OpenProcessToken": ((m.HANDLE, m.DWORD, pointer(m.HANDLE)), m.BOOL),
            "DuplicateTokenEx": ((m.HANDLE, m.DWORD, pointer(m.SECURITY_ATTRIBUTES), ctypes.c_int, ctypes.c_int, pointer(m.HANDLE)), m.BOOL),
            "GetTokenInformation": ((m.HANDLE, ctypes.c_int, m.LPVOID, m.DWORD, pointer(m.DWORD)), m.BOOL),
            "GetSecurityDescriptorControl": ((m.PSECURITY_DESCRIPTOR, pointer(m.WORD), pointer(m.DWORD)), m.BOOL),
            "GetAclInformation": ((m.PACL, m.LPVOID, m.DWORD, ctypes.c_int), m.BOOL),
            "GetAce": ((m.PACL, m.DWORD, pointer(m.LPVOID)), m.BOOL),
            "GetLengthSid": ((m.PSID,), m.DWORD),
            "CreateWellKnownSid": ((ctypes.c_int, m.PSID, m.PSID, pointer(m.DWORD)), m.BOOL),
            "EqualSid": ((m.PSID, m.PSID), m.BOOL),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": ((m.LPCWSTR, m.DWORD, pointer(m.PSECURITY_DESCRIPTOR), pointer(m.DWORD)), m.BOOL),
            "GetSecurityDescriptorOwner": ((m.PSECURITY_DESCRIPTOR, pointer(m.PSID), pointer(m.BOOL)), m.BOOL),
            "GetSecurityDescriptorDacl": ((m.PSECURITY_DESCRIPTOR, pointer(m.BOOL), pointer(m.PACL), pointer(m.BOOL)), m.BOOL),
            "GetSecurityDescriptorSacl": ((m.PSECURITY_DESCRIPTOR, pointer(m.BOOL), pointer(m.PACL), pointer(m.BOOL)), m.BOOL),
            "IsValidSecurityDescriptor": ((m.PSECURITY_DESCRIPTOR,), m.BOOL),
            "IsValidAcl": ((m.PACL,), m.BOOL),
            "IsValidSid": ((m.PSID,), m.BOOL),
            "CopySid": ((m.DWORD, m.PSID, m.PSID), m.BOOL),
            "GetSidSubAuthorityCount": ((m.PSID,), pointer(m.BYTE)),
            "GetSidSubAuthority": ((m.PSID, m.DWORD), pointer(m.DWORD)),
        }
        self.assertEqual(m.WIN32_SIGNATURES, expected)
        for name, (argtypes, restype) in expected.items():
            with self.subTest(name=name):
                function = getattr(api, name)
                self.assertEqual(tuple(function.argtypes), argtypes)
                self.assertIs(function.restype, restype)
        self.assertEqual(
            api.CreateFileW.argtypes,
            [
                api_module.LPCWSTR,
                api_module.DWORD,
                api_module.DWORD,
                ctypes.POINTER(api_module.SECURITY_ATTRIBUTES),
                api_module.DWORD,
                api_module.DWORD,
                api_module.HANDLE,
            ],
        )
        self.assertIs(api.CreateFileW.restype, api_module.HANDLE)
        self.assertEqual(
            api.CreateEventW.argtypes[0],
            ctypes.POINTER(api_module.SECURITY_ATTRIBUTES),
        )
        self.assertIs(api.GetSecurityInfo.restype, api_module.DWORD)


class Win32HandleTests(unittest.TestCase):
    def test_handle_is_one_way_noncopyable_and_closes_once(self) -> None:
        calls: list[int] = []
        handle = Win32Handle(41, _close=lambda value: calls.append(value) or 1)
        self.assertFalse(handle.closed)
        with handle.borrow() as raw:
            self.assertEqual(raw, 41)
        for operation in (
            lambda: copy.copy(handle),
            lambda: copy.deepcopy(handle),
            lambda: pickle.dumps(handle),
        ):
            with self.assertRaises(TypeError):
                operation()
        handle.close()
        handle.close()
        self.assertTrue(handle.closed)
        self.assertEqual(calls, [41])
        with self.assertRaises(Win32CallError):
            with handle.borrow():
                pass

    def test_close_failure_still_revokes_handle(self) -> None:
        handle = Win32Handle(52, _close=lambda value: 0, _last_error=lambda: 6)
        with self.assertRaises(Win32CallError) as caught:
            handle.close()
        self.assertTrue(handle.closed)
        self.assertEqual(caught.exception.function, "CloseHandle")
        self.assertEqual(caught.exception.winerror, 6)
        with self.assertRaises(Win32CallError):
            with handle.borrow():
                pass

    def test_invalid_values_never_construct_an_authority(self) -> None:
        for value in (0, api_module.INVALID_HANDLE_VALUE, True, "1"):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                Win32Handle(value, _close=lambda handle: 1)

    def test_concurrent_close_calls_close_handle_exactly_once(self) -> None:
        barrier = threading.Barrier(3)
        close_entered = threading.Event()
        release_close = threading.Event()
        calls: list[int] = []

        def close(value: int) -> int:
            calls.append(value)
            close_entered.set()
            self.assertTrue(release_close.wait(2))
            return 1

        handle = Win32Handle(61, _close=close)

        def closer() -> None:
            barrier.wait()
            handle.close()

        threads = [threading.Thread(target=closer) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        self.assertTrue(close_entered.wait(2))
        release_close.set()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [61])
        self.assertTrue(handle.closed)

    def test_close_waits_for_active_borrow_guard(self) -> None:
        close_done = threading.Event()
        handle = Win32Handle(62, _close=lambda value: 1)

        def closer() -> None:
            handle.close()
            close_done.set()

        with handle.borrow() as raw:
            self.assertEqual(raw, 62)
            thread = threading.Thread(target=closer)
            thread.start()
            self.assertFalse(close_done.wait(0.05))
        self.assertTrue(close_done.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(handle.closed)

    @unittest.skipUnless(sys.platform == "win32", "real last-error requires Windows")
    def test_create_file_failure_uses_invalid_handle_and_captured_last_error(self) -> None:
        api = WindowsFileAPI.load()
        with self.assertRaises(Win32CallError) as caught:
            api.open_handle(
                r"\\?\invalid-device\missing",
                desired_access=0,
                share_mode=0,
                creation_disposition=3,
                flags=0,
            )
        self.assertEqual(caught.exception.function, "CreateFileW")
        self.assertGreater(caught.exception.winerror, 0)


class Win32CheckedConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = object.__new__(WindowsFileAPI)

    def test_bool_and_invalid_handle_capture_last_error_immediately(self) -> None:
        events: list[str] = []
        self.api.last_error = lambda: events.append("last-error") or 123

        def failed_bool() -> int:
            events.append("call")
            return 0

        with self.assertRaises(Win32CallError) as caught:
            self.api.checked_bool("BoolCall", failed_bool)
        self.assertEqual(events, ["call", "last-error"])
        self.assertEqual(caught.exception.winerror, 123)

        events.clear()
        with self.assertRaises(Win32CallError) as caught:
            self.api.checked_handle_value(
                "CreateFileW",
                api_module.INVALID_HANDLE_VALUE,
            )
        self.assertEqual(events, ["last-error"])
        self.assertEqual(caught.exception.winerror, 123)

    def test_direct_status_never_reads_thread_last_error(self) -> None:
        self.api.last_error = lambda: self.fail("direct DWORD status used last-error")
        with self.assertRaises(Win32CallError) as caught:
            self.api.checked_status_zero("GetSecurityInfo", 5)
        self.assertEqual(caught.exception.winerror, 5)
        self.api.checked_status_zero("GetSecurityInfo", 0)

    def test_length_wait_and_pointer_sentinels_are_closed(self) -> None:
        self.api.last_error = lambda: 87
        for operation in (
            lambda: self.api.checked_length("LengthCall", 0),
            lambda: self.api.checked_wait("WaitCall", api_module.WAIT_FAILED),
            lambda: self.api.checked_nonnull_pointer("PointerCall", None),
        ):
            with self.subTest(operation=operation), self.assertRaises(
                Win32CallError
            ) as caught:
                operation()
            self.assertEqual(caught.exception.winerror, 87)
        self.assertEqual(self.api.checked_length("LengthCall", 3), 3)
        self.assertEqual(
            self.api.checked_wait("WaitCall", api_module.WAIT_TIMEOUT),
            api_module.WAIT_TIMEOUT,
        )
        self.assertEqual(self.api.checked_nonnull_pointer("PointerCall", 7), 7)
        value = ctypes.c_uint32(9)
        pointer = ctypes.pointer(value)
        self.assertEqual(
            self.api.checked_nonnull_pointer("PointerCall", pointer),
            ctypes.addressof(value),
        )
        with self.assertRaises(Win32CallError) as caught:
            self.api.checked_nonnull_pointer(
                "PointerCall",
                ctypes.POINTER(ctypes.c_uint32)(),
            )
        self.assertEqual(caught.exception.winerror, 87)

    def test_local_free_uses_inverse_sentinel_and_immediate_last_error(self) -> None:
        events: list[str] = []
        self.api.last_error = lambda: events.append("last-error") or 8

        def failed_local_free(memory: object) -> int:
            del memory
            events.append("LocalFree")
            return 44

        self.api.LocalFree = failed_local_free
        with self.assertRaises(Win32CallError) as caught:
            self.api.checked_local_free(44)
        self.assertEqual(events, ["LocalFree", "last-error"])
        self.assertEqual(caught.exception.winerror, 8)

        events.clear()
        self.api.LocalFree = lambda memory: events.append("LocalFree") or None
        self.api.checked_local_free(44)
        self.assertEqual(events, ["LocalFree"])


if __name__ == "__main__":
    unittest.main()
