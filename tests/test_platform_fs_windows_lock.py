"""Task 3.3 Windows persistent LockFileEx lease tests."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.windows_native_helper_build import (
    NativeEvidenceRecord,
    NativeHelperBuild,
    build_native_helper,
    decode_native_evidence,
)
from tests.windows_process_token_helper import (
    PROCESS_INFORMATION,
    STARTUPINFOEXW,
    WindowsTestProcessAPI,
)

from platform_fs_contracts import (
    LockPolicy,
    LockWait,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    ProcessFileLock,
    RootedFileSystem,
)
import platform_fs_windows
from platform_fs_windows import WindowsProcessFileLock, WindowsRootedFileSystem
from windows_file_api import OVERLAPPED
from windows_file_api import (
    ACL_SIZE_INFORMATION,
    DWORD,
    HANDLE,
    LPVOID,
    SECURITY_ATTRIBUTES,
    SID_AND_ATTRIBUTES,
    TOKEN_MANDATORY_LABEL,
    Win32CallError,
    Win32Handle,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (
    b"LOCALCAT-PROTOCOL-CONTROL-LOCK\x00"
    b"schema=1\nresource-family=0123456789abcdef\nrange-map=exclusive-byte-0\n"
)


def _assert_platform_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode,
) -> None:
    case.assertEqual(caught.exception.code, code.value)
    case.assertEqual(caught.exception.args, (code.value,))
    case.assertIsNone(caught.exception.__cause__)


@unittest.skipUnless(sys.platform == "win32", "Task 3.3 requires real Windows")
class WindowsPersistentLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        diagnostics = (
            ROOT
            / "artifacts"
            / "windows"
            / "task3-3-restricted-token-diagnostics"
        )
        cls.native_helper_build = build_native_helper(
            ROOT / "tests" / "windows_lock_access_helper.c",
            diagnostics / "full",
        )
        cls.native_matrix_path = diagnostics / "native-matrix.json"
        cls.current_primary_evidence_path = diagnostics / "current-primary.json"

    @classmethod
    def tearDownClass(cls) -> None:
        pass

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name)
        self.rooted_fs = WindowsRootedFileSystem()
        self.root = self.rooted_fs.bind_root(self.root_path)
        self.parent = self.rooted_fs.bind_parent(
            self.root,
            PureWindowsPath("resource.lock"),
        )
        self.test_process_api = WindowsTestProcessAPI.load()

    def tearDown(self) -> None:
        self.parent.close()
        self.root.close()
        self.temporary.cleanup()

    def _close_authorities(self) -> None:
        self.parent.close()
        self.root.close()

    def _rebind(self) -> None:
        self.root = self.rooted_fs.bind_root(self.root_path)
        self.parent = self.rooted_fs.bind_parent(
            self.root,
            PureWindowsPath("resource.lock"),
        )

    def _seed_protocol_file(self) -> None:
        lease = self._acquire()
        lease.close()

    def _acquire(
        self,
        *,
        payload: bytes = EXPECTED,
        policy: LockPolicy | None = None,
    ):
        return WindowsProcessFileLock().acquire(
            self.parent,
            "resource.lock",
            payload,
            policy or LockPolicy(LockWait.FAIL_FAST),
        )

    def test_service_mints_only_process_lock_shape(self) -> None:
        service = WindowsProcessFileLock()
        self.assertIsInstance(service, ProcessFileLock)
        self.assertNotIsInstance(service, RootedFileSystem)
        self.assertNotIsInstance(service, PrivateStorageProof)
        self.assertNotIsInstance(service, PlatformFileBackend)

    def test_acl_parser_requires_exact_ace_size_and_closed_used_bytes(self) -> None:
        sid = platform_fs_windows._canonical_sid(5, 18)

        class FakeAclAPI:
            def __init__(self, base: int, *, ace_offset: int, used: int) -> None:
                self.base = base
                self.ace_offset = ace_offset
                self.used = used

            @staticmethod
            def checked_bool(name: str, function: object, *arguments: object) -> None:
                del name
                if not function(*arguments):
                    raise AssertionError("fake Win32 call failed")

            @staticmethod
            def IsValidAcl(pointer: int) -> int:
                return int(pointer != 0)

            @staticmethod
            def IsValidSid(pointer: int) -> int:
                return int(pointer != 0)

            @staticmethod
            def GetLengthSid(pointer: int) -> int:
                count = ctypes.string_at(pointer + 1, 1)[0]
                return 8 + count * 4

            def GetAclInformation(
                self,
                acl: int,
                output: object,
                size: int,
                information_class: int,
            ) -> int:
                del acl, size, information_class
                information = ctypes.cast(
                    output,
                    ctypes.POINTER(ACL_SIZE_INFORMATION),
                ).contents
                information.AceCount = 1
                information.AclBytesInUse = self.used
                information.AclBytesFree = 0
                return 1

            def GetAce(self, acl: int, index: int, output: object) -> int:
                del acl, index
                ctypes.cast(output, ctypes.POINTER(LPVOID)).contents.value = (
                    self.base + self.ace_offset
                )
                return 1

        def acl_bytes(*, ace_size: int, acl_size: int) -> bytes:
            ace = (
                bytes((0, 0))
                + ace_size.to_bytes(2, "little")
                + (0x001F01FF).to_bytes(4, "little")
                + sid
            )
            return (
                bytes((2, 0))
                + acl_size.to_bytes(2, "little")
                + (1).to_bytes(2, "little")
                + b"\0\0"
                + ace
                + b"\0" * max(0, acl_size - 8 - len(ace))
            )

        valid = ctypes.create_string_buffer(acl_bytes(ace_size=20, acl_size=28))
        self.assertEqual(
            platform_fs_windows._acl_entries(
                FakeAclAPI(ctypes.addressof(valid), ace_offset=8, used=28),
                ctypes.addressof(valid),
            ),
            ((0, 0, 0x001F01FF, sid),),
        )
        cases = (
            (acl_bytes(ace_size=24, acl_size=32), 8, 32),
            (acl_bytes(ace_size=20, acl_size=32), 8, 32),
            (acl_bytes(ace_size=20, acl_size=28), 12, 28),
        )
        for raw, ace_offset, used in cases:
            buffer = ctypes.create_string_buffer(raw)
            with self.subTest(ace_offset=ace_offset, used=used), self.assertRaises(
                PlatformFileError
            ) as caught:
                platform_fs_windows._acl_entries(
                    FakeAclAPI(
                        ctypes.addressof(buffer),
                        ace_offset=ace_offset,
                        used=used,
                    ),
                    ctypes.addressof(buffer),
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
            )

    def test_first_creator_persists_exact_payload_and_release_reacquires(self) -> None:
        first = self._acquire()
        first.close()
        self._close_authorities()
        self.assertEqual((self.root_path / "resource.lock").read_bytes(), EXPECTED)
        self._rebind()
        second = self._acquire()
        second.close()

    def test_create_new_accepts_only_file_exists_as_existing_protocol(self) -> None:
        api = self.parent._api
        original_open = api.open_handle

        def already_exists(path: str, **keywords: object):
            if int(keywords["creation_disposition"]) == 1:
                raise Win32CallError("CreateFileW", 183)
            return original_open(path, **keywords)

        with mock.patch.object(api, "open_handle", side_effect=already_exists):
            with self.assertRaises(PlatformFileError) as caught:
                self._acquire()
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.LOCK_UNAVAILABLE,
        )
        self.assertFalse((self.root_path / "resource.lock").exists())

    def test_empty_and_strict_prefix_are_recovered_but_unknown_is_not(self) -> None:
        lock_path = self.root_path / "resource.lock"
        for recoverable in (b"", EXPECTED[:17]):
            with self.subTest(recoverable=recoverable):
                self._seed_protocol_file()
                self._close_authorities()
                lock_path.write_bytes(recoverable)
                self._rebind()
                lease = self._acquire()
                lease.close()
                self._close_authorities()
                self.assertEqual(lock_path.read_bytes(), EXPECTED)
                lock_path.unlink()
                self._rebind()

        for hostile in (EXPECTED + b"x", b"unknown", EXPECTED[:-1] + b"X"):
            with self.subTest(hostile=hostile):
                self._seed_protocol_file()
                self._close_authorities()
                lock_path.write_bytes(hostile)
                self._rebind()
                with self.assertRaises(PlatformFileError) as caught:
                    self._acquire()
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                )
                self._close_authorities()
                self.assertEqual(lock_path.read_bytes(), hostile)
                lock_path.unlink()
                self._rebind()

    def test_hardlink_and_acl_or_mic_tamper_fail_closed(self) -> None:
        self._seed_protocol_file()
        self._close_authorities()
        lock_path = self.root_path / "resource.lock"
        alias = self.root_path / "alias.lock"
        os.link(lock_path, alias)
        self._rebind()
        with self.assertRaises(PlatformFileError) as caught:
            self._acquire()
        _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)
        self._close_authorities()
        alias.unlink()
        self._rebind()

        for arguments in (("/inheritance:e",), ("/setintegritylevel", "L")):
            with self.subTest(arguments=arguments):
                self._close_authorities()
                result = subprocess.run(
                    ["icacls.exe", str(lock_path), *arguments],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self._rebind()
                with self.assertRaises(PlatformFileError) as caught:
                    self._acquire()
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                )
                self._close_authorities()
                lock_path.unlink()
                self._rebind()
                rebuilt = self._acquire()
                rebuilt.close()

    def _run_native_access_helper(
        self,
        path: Path,
        token: Win32Handle,
    ) -> tuple[int, NativeEvidenceRecord]:
        api = self.parent._api
        helper = self.native_helper_build.executable
        self.assertTrue(helper.is_file())
        self.assertNotIn('"', str(path))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_pipe = kernel32.CreatePipe
        create_pipe.argtypes = [
            ctypes.POINTER(HANDLE),
            ctypes.POINTER(HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            DWORD,
        ]
        create_pipe.restype = ctypes.c_int32
        set_handle_information = kernel32.SetHandleInformation
        set_handle_information.argtypes = [HANDLE, DWORD, DWORD]
        set_handle_information.restype = ctypes.c_int32
        get_handle_information = kernel32.GetHandleInformation
        get_handle_information.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
        get_handle_information.restype = ctypes.c_int32
        initialize_attributes = kernel32.InitializeProcThreadAttributeList
        initialize_attributes.argtypes = [
            LPVOID,
            DWORD,
            DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        initialize_attributes.restype = ctypes.c_int32
        update_attributes = kernel32.UpdateProcThreadAttribute
        update_attributes.argtypes = [
            LPVOID,
            DWORD,
            ctypes.c_size_t,
            LPVOID,
            ctypes.c_size_t,
            LPVOID,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        update_attributes.restype = ctypes.c_int32
        delete_attributes = kernel32.DeleteProcThreadAttributeList
        delete_attributes.argtypes = [LPVOID]
        delete_attributes.restype = None

        security = SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=None,
            bInheritHandle=1,
        )
        read_value = HANDLE()
        write_value = HANDLE()
        api.checked_bool(
            "CreatePipe",
            create_pipe,
            ctypes.byref(read_value),
            ctypes.byref(write_value),
            ctypes.byref(security),
            0,
        )
        pipe_read = Win32Handle(
            int(read_value.value or 0),
            _close=api.CloseHandle,
            _last_error=api.last_error,
        )
        pipe_write = Win32Handle(
            int(write_value.value or 0),
            _close=api.CloseHandle,
            _last_error=api.last_error,
        )
        retained_target = api.open_handle(
            str(path),
            desired_access=platform_fs_windows.GENERIC_READ,
            share_mode=platform_fs_windows.FILE_SHARE_READ
            | platform_fs_windows.FILE_SHARE_WRITE
            | platform_fs_windows.FILE_SHARE_DELETE,
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
        )
        process = None
        thread = None
        attributes_initialized = False
        attribute_storage = None
        with pipe_read.borrow() as raw_read:
            api.checked_bool(
                "SetHandleInformation",
                set_handle_information,
                raw_read,
                1,
                0,
            )
        with retained_target.borrow() as raw_target:
            target_flags = DWORD()
            api.checked_bool(
                "GetHandleInformation",
                get_handle_information,
                raw_target,
                ctypes.byref(target_flags),
            )
            self.assertEqual(int(target_flags.value) & 1, 0)

        attribute_size = ctypes.c_size_t()
        ctypes.set_last_error(0)
        self.assertFalse(
            initialize_attributes(
                None,
                1,
                0,
                ctypes.byref(attribute_size),
            )
        )
        self.assertEqual(ctypes.get_last_error(), 122)
        self.assertGreater(int(attribute_size.value), 0)
        attribute_storage = ctypes.create_string_buffer(int(attribute_size.value))
        attribute_pointer = ctypes.cast(attribute_storage, LPVOID)
        api.checked_bool(
            "InitializeProcThreadAttributeList",
            initialize_attributes,
            attribute_pointer,
            1,
            0,
            ctypes.byref(attribute_size),
        )
        attributes_initialized = True
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        startup.lpAttributeList = attribute_pointer
        information = PROCESS_INFORMATION()
        try:
            with pipe_write.borrow() as raw_write:
                write_flags = DWORD()
                api.checked_bool(
                    "GetHandleInformation",
                    get_handle_information,
                    raw_write,
                    ctypes.byref(write_flags),
                )
                self.assertEqual(int(write_flags.value) & 1, 1)
                handle_list = (HANDLE * 1)(raw_write)
                api.checked_bool(
                    "UpdateProcThreadAttribute",
                    update_attributes,
                    attribute_pointer,
                    0,
                    0x00020002,
                    ctypes.cast(handle_list, LPVOID),
                    ctypes.sizeof(handle_list),
                    None,
                    None,
                )
                self.assertEqual(startup.StartupInfo.dwFlags, 0)
                self.assertFalse(startup.StartupInfo.hStdInput)
                self.assertFalse(startup.StartupInfo.hStdOutput)
                self.assertFalse(startup.StartupInfo.hStdError)
                command_buffer = ctypes.create_unicode_buffer(
                    f'"{helper}" {int(raw_write)} "{path}"'
                )
                with token.borrow() as raw_token:
                    api.checked_bool(
                        "CreateProcessAsUserW",
                        self.test_process_api.CreateProcessAsUserW,
                        raw_token,
                        str(helper),
                        command_buffer,
                        None,
                        None,
                        1,
                        0x00080000,
                        None,
                        str(helper.parent),
                        ctypes.byref(startup.StartupInfo),
                        ctypes.byref(information),
                    )
            process = Win32Handle(
                int(information.hProcess or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )
            thread = Win32Handle(
                int(information.hThread or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )
            pipe_write.close()
            thread.close()
            thread = None
            with process.borrow() as raw_process:
                self.assertEqual(
                    api.checked_wait(
                        "WaitForSingleObject",
                        api.WaitForSingleObject(raw_process, 10000),
                    ),
                    0,
                )
                exit_code = DWORD()
                api.checked_bool(
                    "GetExitCodeProcess",
                    self.test_process_api.GetExitCodeProcess,
                    raw_process,
                    ctypes.byref(exit_code),
                )
            raw_evidence = bytearray()
            with pipe_read.borrow() as raw_read:
                while len(raw_evidence) < ctypes.sizeof(NativeEvidenceRecord):
                    remaining = ctypes.sizeof(NativeEvidenceRecord) - len(raw_evidence)
                    buffer = ctypes.create_string_buffer(remaining)
                    read = DWORD()
                    if not api.ReadFile(
                        raw_read,
                        buffer,
                        remaining,
                        ctypes.byref(read),
                        None,
                    ):
                        error = api.last_error()
                        if error == 109:
                            break
                        raise Win32CallError("ReadFile", error)
                    if read.value == 0:
                        break
                    self.assertLessEqual(int(read.value), remaining)
                    raw_evidence.extend(buffer.raw[: int(read.value)])
            self.assertEqual(
                len(raw_evidence),
                ctypes.sizeof(NativeEvidenceRecord),
                f"native helper exited {int(exit_code.value):#x} before evidence record",
            )
            return int(exit_code.value), decode_native_evidence(bytes(raw_evidence))
        finally:
            if attributes_initialized:
                delete_attributes(attribute_pointer)
            for authority in (thread, process):
                if authority is not None:
                    authority.close()

            for authority in (retained_target, pipe_write, pipe_read):
                authority.close()

    @staticmethod
    def _sid_bytes(value: object, length: int) -> bytes:
        return bytes(value[:length])

    def _parent_token_facts(self, token: Win32Handle) -> dict[str, object]:
        api = self.parent._api

        def variable_information(raw_token: int, information_class: int):
            needed = DWORD()
            self.assertFalse(
                api.GetTokenInformation(
                    raw_token,
                    information_class,
                    None,
                    0,
                    ctypes.byref(needed),
                )
            )
            self.assertEqual(api.last_error(), 122)
            self.assertGreater(int(needed.value), 0)
            buffer = ctypes.create_string_buffer(int(needed.value))
            api.checked_bool(
                "GetTokenInformation",
                api.GetTokenInformation,
                raw_token,
                information_class,
                buffer,
                len(buffer),
                ctypes.byref(needed),
            )
            return buffer

        with token.borrow() as raw_token:
            user_sid = platform_fs_windows._token_user_sid(api, raw_token)
            token_type = DWORD()
            elevation_type = DWORD()
            sandbox_inert = DWORD()
            has_restrictions = DWORD(0xA5A5A5A5)
            for information_class, output in (
                (8, token_type),
                (18, elevation_type),
                (15, sandbox_inert),
            ):
                returned = DWORD()
                self.assertTrue(
                    api.GetTokenInformation(
                        raw_token,
                        information_class,
                        ctypes.byref(output),
                        ctypes.sizeof(output),
                        ctypes.byref(returned),
                    ),
                    (
                        information_class,
                        api.last_error(),
                        int(returned.value),
                    ),
                )
                self.assertEqual(
                    int(returned.value),
                    ctypes.sizeof(output),
                    (information_class, int(returned.value)),
                )
            returned = DWORD()
            self.assertTrue(
                api.GetTokenInformation(
                    raw_token,
                    21,
                    ctypes.byref(has_restrictions),
                    ctypes.sizeof(has_restrictions),
                    ctypes.byref(returned),
                ),
                (21, api.last_error(), int(returned.value)),
            )
            has_restrictions_bytes = ctypes.string_at(
                ctypes.byref(has_restrictions),
                ctypes.sizeof(has_restrictions),
            )
            if int(returned.value) == 1:
                self.assertIn(has_restrictions_bytes[0], (0, 1))
                self.assertEqual(has_restrictions_bytes[1:], b"\xA5" * 3)
                has_restrictions_value = has_restrictions_bytes[0]
            else:
                self.assertEqual(int(returned.value), ctypes.sizeof(has_restrictions))
                self.assertIn(int(has_restrictions.value), (0, 1))
                has_restrictions_value = int(has_restrictions.value)
            integrity_buffer = variable_information(raw_token, 25)
            integrity = ctypes.cast(
                integrity_buffer,
                ctypes.POINTER(TOKEN_MANDATORY_LABEL),
            ).contents
            integrity_sid = platform_fs_windows._sid_bytes(api, integrity.Label.Sid)
            restrictions_buffer = variable_information(raw_token, 11)
            restriction_count = int.from_bytes(
                restrictions_buffer.raw[:4],
                "little",
            )
            restrictions = sorted(
                platform_fs_windows._sid_bytes(
                    api,
                    SID_AND_ATTRIBUTES.from_buffer_copy(
                        restrictions_buffer.raw,
                        8 + index * ctypes.sizeof(SID_AND_ATTRIBUTES),
                    ).Sid,
                ).hex()
                for index in range(restriction_count)
            )
            return {
                "user_sid": user_sid.hex(),
                "token_type": int(token_type.value),
                "elevation_type": int(elevation_type.value),
                "integrity_sid": integrity_sid.hex(),
                "is_restricted": bool(api.IsTokenRestricted(raw_token)),
                "restriction_sids": restrictions,
                "sandbox_inert": int(sandbox_inert.value),
                "has_restrictions": has_restrictions_value,
            }

    def _assert_native_denied(self, record: NativeEvidenceRecord) -> None:
        for operation, phase in (
            (record.open_result, 1),
            (record.write_result, 1),
            (record.delete_result, 5),
        ):
            self.assertEqual(
                (
                    int(operation.status),
                    int(operation.phase),
                    int(operation.winerror),
                    int(operation.transferred),
                ),
                (2, phase, 5, 0),
            )

    def _assert_restricted_native_report(
        self,
        record: NativeEvidenceRecord,
        *,
        expected_user_sid: bytes,
        intended_restrictions: tuple[bytes, ...],
        read_allowed: bool,
    ) -> list[bytes]:
        self.assertEqual(record.helper_status, 0)
        self.assertEqual(
            self._sid_bytes(record.user_sid, record.user_sid_length),
            expected_user_sid,
        )
        self.assertEqual(record.token_type, 1)
        self.assertEqual(record.elevation_type, 3)
        self.assertEqual(record.integrity_rid, 8192)
        self.assertEqual(
            self._sid_bytes(record.integrity_sid, record.integrity_sid_length),
            platform_fs_windows._canonical_sid(16, 8192),
        )
        self.assertEqual(record.is_restricted, 1)
        self.assertEqual(record.sandbox_inert, 0)
        self.assertNotEqual(record.has_restrictions, 0)
        actual_restrictions = sorted(
            self._sid_bytes(
                record.restricted_sids[index].bytes,
                record.restricted_sids[index].length,
            )
            for index in range(record.restricted_sid_count)
        )
        self.assertEqual(actual_restrictions, sorted(intended_restrictions))
        actual_read = (
            int(record.read_result.status),
            int(record.read_result.phase),
            int(record.read_result.winerror),
        )
        self.assertEqual(actual_read, (1, 1, 0) if read_allowed else (2, 1, 5))
        self._assert_native_denied(record)
        return actual_restrictions

    def _native_report_evidence(
        self,
        exit_code: int,
        record: NativeEvidenceRecord,
        actual_restrictions: list[bytes],
    ) -> dict[str, object]:
        return {
            "exit": exit_code,
            "user_sid": self._sid_bytes(
                record.user_sid,
                record.user_sid_length,
            ).hex(),
            "token_type": int(record.token_type),
            "elevation_type": int(record.elevation_type),
            "integrity_sid": self._sid_bytes(
                record.integrity_sid,
                record.integrity_sid_length,
            ).hex(),
            "is_restricted": int(record.is_restricted),
            "sandbox_inert": int(record.sandbox_inert),
            "has_restrictions": int(record.has_restrictions),
            "restriction_sids": [sid.hex() for sid in actual_restrictions],
            "read": {
                "status": int(record.read_result.status),
                "phase": int(record.read_result.phase),
                "winerror": int(record.read_result.winerror),
                "transferred": int(record.read_result.transferred),
            },
            "write_open": {
                "status": int(record.open_result.status),
                "phase": int(record.open_result.phase),
                "winerror": int(record.open_result.winerror),
                "transferred": int(record.open_result.transferred),
            },
            "write": {
                "status": int(record.write_result.status),
                "phase": int(record.write_result.phase),
                "winerror": int(record.write_result.winerror),
                "transferred": int(record.write_result.transferred),
            },
            "delete": {
                "status": int(record.delete_result.status),
                "phase": int(record.delete_result.phase),
                "winerror": int(record.delete_result.winerror),
                "transferred": int(record.delete_result.transferred),
            },
        }

    def test_native_helper_build_evidence_and_import_closure(self) -> None:
        build: NativeHelperBuild = self.native_helper_build
        evidence = build.evidence
        self.assertTrue(build.executable.is_file())
        self.assertEqual(evidence["compile_returncode"], 0)
        self.assertEqual(evidence["link_returncode"], 0)
        self.assertIn("/nodefaultlib", evidence["link_command"])
        self.assertIn("/entry:LocalCatTestEntry", evidence["link_command"])
        self.assertIn("/subsystem:windows", evidence["link_command"])
        self.assertEqual(evidence["pe"]["machine"], 0x8664)
        self.assertEqual(evidence["pe"]["bound_import_directory"], (0, 0))
        self.assertEqual(evidence["pe"]["delay_import_directory"], (0, 0))
        self.assertEqual(
            evidence["pe"]["imports"],
            {
                "KERNEL32.DLL": (
                    "WriteFile",
                    "CloseHandle",
                    "SetFilePointerEx",
                    "GetCurrentProcess",
                    "ExitProcess",
                    "SetEndOfFile",
                    "DeleteFileW",
                    "CreateFileW",
                    "GetLastError",
                    "GetCommandLineW",
                ),
                "ADVAPI32.DLL": (
                    "GetLengthSid",
                    "OpenProcessToken",
                    "IsTokenRestricted",
                    "GetTokenInformation",
                ),
            },
        )
        for key in (
            "source_sha256",
            "compiler_sha256",
            "linker_sha256",
            "sdk_windows_h_sha256",
            "sdk_kernel32_lib_sha256",
            "sdk_advapi32_lib_sha256",
            "compile_command_sha256",
            "link_command_sha256",
            "binary_sha256",
        ):
            self.assertEqual(len(evidence[key]), 64)
            int(evidence[key], 16)

    def test_current_primary_positive_control_supports_ordinary_and_elevated(self) -> None:
        api = self.parent._api
        expected_user_sid = platform_fs_windows._current_primary_user_sid(api)
        token_access = 0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100
        positive_path = self.root_path / "worker-positive.lock"
        positive = WindowsProcessFileLock().acquire(
            self.parent,
            positive_path.name,
            EXPECTED,
            LockPolicy(LockWait.FAIL_FAST),
        )
        positive.close()
        primary = platform_fs_windows._open_current_primary_token(api, token_access)
        try:
            parent_facts = self._parent_token_facts(primary)
            exit_code, report = self._run_native_access_helper(positive_path, primary)
        finally:
            primary.close()
        self.assertEqual(exit_code, 0)
        self.assertEqual(report.helper_status, 0)
        self.assertEqual(
            self._sid_bytes(report.user_sid, report.user_sid_length),
            expected_user_sid,
        )
        self.assertEqual(report.token_type, 1)
        self.assertEqual(report.is_restricted, 0)
        self.assertEqual(report.sandbox_inert, parent_facts["sandbox_inert"])
        self.assertEqual(report.has_restrictions, parent_facts["has_restrictions"])
        self.assertEqual(report.restricted_sid_count, 0)
        integrity_sid = self._sid_bytes(
            report.integrity_sid,
            report.integrity_sid_length,
        )
        elevation_pair = (
            int(report.elevation_type),
            integrity_sid,
        )
        self.assertIn(
            elevation_pair,
            {
                (1, platform_fs_windows._canonical_sid(16, 8192)),
                (3, platform_fs_windows._canonical_sid(16, 8192)),
                (2, platform_fs_windows._canonical_sid(16, 12288)),
            },
        )
        self.assertEqual(report.integrity_rid, int.from_bytes(integrity_sid[-4:], "little"))
        self.assertEqual(parent_facts["user_sid"], expected_user_sid.hex())
        self.assertEqual(parent_facts["token_type"], int(report.token_type))
        self.assertEqual(parent_facts["elevation_type"], int(report.elevation_type))
        self.assertEqual(parent_facts["integrity_sid"], integrity_sid.hex())
        self.assertFalse(parent_facts["is_restricted"])
        self.assertEqual(parent_facts["restriction_sids"], [])
        if report.elevation_type == 2:
            ordinary_reference = os.environ.get(
                "LOCALCAT_EXPECTED_ORDINARY_USER_SID"
            )
            self.assertIsNotNone(
                ordinary_reference,
                "elevated evidence requires ordinary TokenUser SID reference",
            )
            self.assertEqual(expected_user_sid.hex(), ordinary_reference)
        self.assertEqual(
            (
                int(report.read_result.status),
                int(report.read_result.phase),
                int(report.read_result.winerror),
            ),
            (1, 1, 0),
        )
        self.assertEqual(
            (
                int(report.open_result.status),
                int(report.open_result.phase),
                int(report.open_result.winerror),
            ),
            (1, 1, 0),
        )
        self.assertEqual(
            (
                int(report.write_result.status),
                int(report.write_result.phase),
                int(report.write_result.winerror),
                int(report.write_result.transferred),
            ),
            (1, 4, 0, 19),
        )
        self.assertEqual(
            (
                int(report.delete_result.status),
                int(report.delete_result.phase),
                int(report.delete_result.winerror),
            ),
            (1, 5, 0),
        )
        self.assertFalse(positive_path.exists())
        self.current_primary_evidence_path.write_text(
            json.dumps(
                {
                    "build": self.native_helper_build.evidence,
                    "parent_prelaunch": parent_facts,
                    "child": self._native_report_evidence(exit_code, report, []),
                    "target_deleted": True,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_mic_and_restricted_sid_denials_use_child_process_primary(self) -> None:
        api = self.parent._api
        expected_user_sid = platform_fs_windows._current_primary_user_sid(api)
        self._seed_protocol_file()
        lock_path = self.root_path / "resource.lock"
        original = lock_path.read_bytes()
        original_stat = lock_path.stat()
        original_identity = (original_stat.st_dev, original_stat.st_ino)
        token_access = 0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100
        primary = platform_fs_windows._open_current_primary_token(api, token_access)
        low = None
        restricted = None
        write_restricted = None
        try:
            low_value = HANDLE()
            with primary.borrow() as raw_primary:
                api.checked_bool(
                    "DuplicateTokenEx",
                    api.DuplicateTokenEx,
                    raw_primary,
                    token_access,
                    None,
                    2,
                    1,
                    ctypes.byref(low_value),
                )
            low = Win32Handle(
                int(low_value.value or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )
            low_sid = platform_fs_windows._well_known_sid(
                api,
                66,
                platform_fs_windows._canonical_sid(16, 4096),
            )
            low_sid_buffer = ctypes.create_string_buffer(low_sid)
            low_label = TOKEN_MANDATORY_LABEL()
            low_label.Label.Sid = ctypes.addressof(low_sid_buffer)
            low_label.Label.Attributes = 0x20
            with low.borrow() as raw_low:
                api.checked_bool(
                    "SetTokenInformation",
                    self.test_process_api.SetTokenInformation,
                    raw_low,
                    25,
                    ctypes.byref(low_label),
                    ctypes.sizeof(low_label) + len(low_sid),
                )

            with primary.borrow() as raw_primary:
                needed_groups = DWORD()
                self.assertFalse(
                    api.GetTokenInformation(
                        raw_primary,
                        2,
                        None,
                        0,
                        ctypes.byref(needed_groups),
                    )
                )
                self.assertEqual(api.last_error(), 122)
                groups_buffer = ctypes.create_string_buffer(int(needed_groups.value))
                api.checked_bool(
                    "GetTokenInformation",
                    api.GetTokenInformation,
                    raw_primary,
                    2,
                    groups_buffer,
                    len(groups_buffer),
                    ctypes.byref(needed_groups),
                )
            logon_sids = [
                platform_fs_windows._sid_bytes(
                    api,
                    SID_AND_ATTRIBUTES.from_buffer_copy(
                        groups_buffer.raw,
                        8 + index * ctypes.sizeof(SID_AND_ATTRIBUTES),
                    ).Sid,
                )
                for index in range(int.from_bytes(groups_buffer.raw[:4], "little"))
                if int(
                    SID_AND_ATTRIBUTES.from_buffer_copy(
                        groups_buffer.raw,
                        8 + index * ctypes.sizeof(SID_AND_ATTRIBUTES),
                    ).Attributes
                )
                & 0xC0000000
                == 0xC0000000
            ]
            self.assertEqual(len(logon_sids), 1)
            intended_restrictions = (
                platform_fs_windows._canonical_sid(5, 32, 545),
                platform_fs_windows._canonical_sid(1, 0),
                platform_fs_windows._canonical_sid(5, 11),
                platform_fs_windows._canonical_sid(5, 4),
                logon_sids[0],
            )
            restriction_buffers = [
                ctypes.create_string_buffer(sid) for sid in intended_restrictions
            ]
            restrictions = (SID_AND_ATTRIBUTES * len(restriction_buffers))(
                *(
                    SID_AND_ATTRIBUTES(Sid=ctypes.addressof(buffer), Attributes=0)
                    for buffer in restriction_buffers
                )
            )
            restricted_value = HANDLE()
            with primary.borrow() as raw_primary:
                api.checked_bool(
                    "CreateRestrictedToken",
                    self.test_process_api.CreateRestrictedToken,
                    raw_primary,
                    0,
                    0,
                    None,
                    0,
                    None,
                    len(restrictions),
                    restrictions,
                    ctypes.byref(restricted_value),
                )
            restricted = Win32Handle(
                int(restricted_value.value or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )
            write_restricted_value = HANDLE()
            with primary.borrow() as raw_primary:
                api.checked_bool(
                    "CreateRestrictedToken",
                    self.test_process_api.CreateRestrictedToken,
                    raw_primary,
                    0x00000008,
                    0,
                    None,
                    0,
                    None,
                    len(restrictions),
                    restrictions,
                    ctypes.byref(write_restricted_value),
                )
            write_restricted = Win32Handle(
                int(write_restricted_value.value or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )

            low_parent_facts = self._parent_token_facts(low)
            low_exit, low_report = self._run_native_access_helper(lock_path, low)
            self.assertEqual(low_exit, 0)
            self.assertEqual(low_report.helper_status, 0)
            self.assertEqual(
                self._sid_bytes(low_report.user_sid, low_report.user_sid_length),
                expected_user_sid,
            )
            self.assertEqual(low_report.token_type, 1)
            self.assertEqual(low_report.elevation_type, 3)
            self.assertEqual(low_report.integrity_rid, 4096)
            self.assertEqual(
                self._sid_bytes(
                    low_report.integrity_sid,
                    low_report.integrity_sid_length,
                ),
                low_sid,
            )
            self.assertEqual(low_report.is_restricted, 0)
            self.assertEqual(
                low_report.sandbox_inert,
                low_parent_facts["sandbox_inert"],
            )
            self.assertEqual(
                low_report.has_restrictions,
                low_parent_facts["has_restrictions"],
            )
            self.assertEqual(low_report.restricted_sid_count, 0)
            self.assertEqual(low_parent_facts["user_sid"], expected_user_sid.hex())
            self.assertEqual(low_parent_facts["token_type"], 1)
            self.assertEqual(low_parent_facts["elevation_type"], 3)
            self.assertEqual(low_parent_facts["integrity_sid"], low_sid.hex())
            self.assertFalse(low_parent_facts["is_restricted"])
            self.assertEqual(low_parent_facts["restriction_sids"], [])
            self.assertEqual(
                (
                    int(low_report.read_result.status),
                    int(low_report.read_result.phase),
                    int(low_report.read_result.winerror),
                ),
                (1, 1, 0),
            )
            self._assert_native_denied(low_report)
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.read_bytes(), original)

            diagnostic_evidence = {
                "build": self.native_helper_build.evidence,
                "low_child": self._native_report_evidence(
                    low_exit,
                    low_report,
                    [],
                ),
                "low_parent_prelaunch": low_parent_facts,
                "create_restricted_token": {
                    "flags_0": {
                        "flags": 0,
                        "disable_sid_count": 0,
                        "delete_privilege_count": 0,
                        "sids_to_restrict": sorted(
                            sid.hex() for sid in intended_restrictions
                        ),
                        "parent_prelaunch": self._parent_token_facts(restricted),
                    },
                    "write_restricted": {
                        "flags": 0x00000008,
                        "disable_sid_count": 0,
                        "delete_privilege_count": 0,
                        "sids_to_restrict": sorted(
                            sid.hex() for sid in intended_restrictions
                        ),
                        "parent_prelaunch": self._parent_token_facts(
                            write_restricted
                        ),
                    },
                },
                "target_before": {
                    "bytes": original.hex(),
                    "identity": list(original_identity),
                },
            }
            for profile in ("flags_0", "write_restricted"):
                prelaunch = diagnostic_evidence["create_restricted_token"][profile][
                    "parent_prelaunch"
                ]
                self.assertEqual(prelaunch["user_sid"], expected_user_sid.hex())
                self.assertEqual(prelaunch["token_type"], 1)
                self.assertEqual(prelaunch["elevation_type"], 3)
                self.assertEqual(
                    prelaunch["integrity_sid"],
                    platform_fs_windows._canonical_sid(16, 8192).hex(),
                )
                self.assertTrue(prelaunch["is_restricted"])
                self.assertEqual(
                    prelaunch["restriction_sids"],
                    sorted(sid.hex() for sid in intended_restrictions),
                )
                self.assertEqual(prelaunch["sandbox_inert"], 0)
                self.assertNotEqual(prelaunch["has_restrictions"], 0)
            for profile, profile_token, read_allowed in (
                ("flags_0_child", restricted, False),
                ("write_restricted_child", write_restricted, True),
            ):
                try:
                    restricted_exit, restricted_report = (
                        self._run_native_access_helper(lock_path, profile_token)
                    )
                except AssertionError as error:
                    diagnostic_evidence[f"{profile}_error"] = str(error)
                    self.native_matrix_path.write_text(
                        json.dumps(
                            diagnostic_evidence,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    raise
                self.assertEqual(restricted_exit, 0)
                actual_restrictions = self._assert_restricted_native_report(
                    restricted_report,
                    expected_user_sid=expected_user_sid,
                    intended_restrictions=intended_restrictions,
                    read_allowed=read_allowed,
                )
                diagnostic_evidence[profile] = self._native_report_evidence(
                    restricted_exit,
                    restricted_report,
                    actual_restrictions,
                )
                self.assertTrue(lock_path.exists())
                self.assertEqual(lock_path.read_bytes(), original)
                current_stat = lock_path.stat()
                self.assertEqual(
                    (current_stat.st_dev, current_stat.st_ino),
                    original_identity,
                )
            final_stat = lock_path.stat()
            diagnostic_evidence["target_after"] = {
                "bytes": lock_path.read_bytes().hex(),
                "identity": [final_stat.st_dev, final_stat.st_ino],
            }
            self.native_matrix_path.write_text(
                json.dumps(
                    diagnostic_evidence,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        finally:
            for authority in (write_restricted, restricted, low, primary):
                if authority is not None:
                    authority.close()
        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.read_bytes(), original)
        final_stat = lock_path.stat()
        self.assertEqual((final_stat.st_dev, final_stat.st_ino), original_identity)
        verified = self._acquire()
        verified.close()

    def test_parent_can_close_after_lease_mints(self) -> None:
        lease = self._acquire()
        self.parent.close()
        self.root.close()
        lease.close()

    def test_init_and_lock_handle_profiles_and_label_projection_are_exact(self) -> None:
        api = self.parent._api
        opens: list[dict[str, object]] = []
        security_sets: list[int] = []
        security_reads: list[int] = []
        lock_ranges: list[tuple[int, int, int, int, int]] = []
        unlock_ranges: list[tuple[int, int, int, int]] = []
        well_known_types: list[int] = []
        duplicate_profiles: list[tuple[int, int, int]] = []
        mapped_access: list[int] = []
        access_checks: list[tuple[int, int, int]] = []
        original_open = api.open_handle
        original_set = api.SetSecurityInfo
        original_get = api.GetSecurityInfo
        original_lock = api.LockFileEx
        original_unlock = api.UnlockFileEx
        original_create_sid = api.CreateWellKnownSid
        original_duplicate = api.DuplicateTokenEx
        original_map = api.MapGenericMask
        original_access = api.AccessCheck

        def recording_open(path: str, **keywords: object):
            opens.append(dict(keywords))
            return original_open(path, **keywords)

        def recording_set(*arguments: object):
            security_sets.append(int(arguments[2]))
            return original_set(*arguments)

        def recording_get(*arguments: object):
            security_reads.append(int(arguments[2]))
            return original_get(*arguments)

        def recording_lock(*arguments: object):
            overlapped = ctypes.cast(
                arguments[5],
                ctypes.POINTER(OVERLAPPED),
            ).contents
            lock_ranges.append(
                (
                    int(arguments[1]),
                    int(arguments[3]),
                    int(arguments[4]),
                    int(overlapped.Offset),
                    int(overlapped.OffsetHigh),
                )
            )
            return original_lock(*arguments)

        def recording_unlock(*arguments: object):
            overlapped = ctypes.cast(
                arguments[4],
                ctypes.POINTER(OVERLAPPED),
            ).contents
            unlock_ranges.append(
                (
                    int(arguments[2]),
                    int(arguments[3]),
                    int(overlapped.OffsetHigh),
                )
            )
            return original_unlock(*arguments)

        def recording_create_sid(*arguments: object):
            well_known_types.append(int(arguments[0]))
            return original_create_sid(*arguments)

        def recording_duplicate(*arguments: object):
            duplicate_profiles.append(
                (int(arguments[1]), int(arguments[3]), int(arguments[4]))
            )
            return original_duplicate(*arguments)

        def recording_map(*arguments: object):
            result = original_map(*arguments)
            mapped_access.append(
                int(ctypes.cast(arguments[0], ctypes.POINTER(DWORD)).contents.value)
            )
            return result

        def recording_access(*arguments: object):
            result = original_access(*arguments)
            access_checks.append(
                (
                    int(arguments[2]),
                    int(ctypes.cast(arguments[6], ctypes.POINTER(DWORD)).contents.value),
                    int(ctypes.cast(arguments[7], ctypes.POINTER(ctypes.c_int32)).contents.value),
                )
            )
            return result

        with mock.patch.object(api, "open_handle", side_effect=recording_open), mock.patch.object(
            api,
            "SetSecurityInfo",
            side_effect=recording_set,
        ), mock.patch.object(
            api,
            "GetSecurityInfo",
            side_effect=recording_get,
        ), mock.patch.object(
            api,
            "LockFileEx",
            side_effect=recording_lock,
        ), mock.patch.object(
            api,
            "UnlockFileEx",
            side_effect=recording_unlock,
        ), mock.patch.object(
            api,
            "CreateWellKnownSid",
            side_effect=recording_create_sid,
        ), mock.patch.object(
            api,
            "DuplicateTokenEx",
            side_effect=recording_duplicate,
        ), mock.patch.object(
            api,
            "MapGenericMask",
            side_effect=recording_map,
        ), mock.patch.object(
            api,
            "AccessCheck",
            side_effect=recording_access,
        ):
            lease = self._acquire()
            lease.close()

        protocol_opens = [
            call
            for call in opens
            if int(call["desired_access"]) & 0x40000000
        ]
        self.assertGreaterEqual(len(protocol_opens), 2)
        init = protocol_opens[0]
        normal = protocol_opens[-1]
        self.assertEqual(init["creation_disposition"], 1)
        self.assertEqual(init["share_mode"], 0)
        self.assertIsNotNone(init["security_attributes"])
        self.assertFalse(int(init["desired_access"]) & 0x00080000)
        self.assertEqual(normal["creation_disposition"], 3)
        self.assertEqual(normal["share_mode"], 0x3)
        self.assertIsNone(normal["security_attributes"])
        self.assertFalse(int(normal["desired_access"]) & 0x00080000)
        for call in protocol_opens:
            self.assertTrue(int(call["flags"]) & 0x00200000)
        self.assertTrue(int(init["flags"]) & 0x80000000)
        self.assertFalse(int(normal["flags"]) & 0x80000000)
        self.assertEqual(security_sets, [])
        self.assertTrue(security_reads)
        self.assertEqual(set(security_reads), {0x7, 0x15})
        self.assertIn(0x15, security_reads)
        self.assertIn(0x7, security_reads)
        self.assertTrue(all(not value & 0x8 for value in security_reads))
        self.assertEqual(lock_ranges, [(0x3, 1, 0, 0, 1)])
        self.assertEqual(unlock_ranges, [(1, 0, 1)])
        self.assertTrue({22, 26, 67}.issubset(set(well_known_types)))
        self.assertTrue(duplicate_profiles)
        self.assertTrue(all(profile == (0x8, 2, 2) for profile in duplicate_profiles))
        self.assertTrue(mapped_access)
        self.assertTrue(all(value == 0x001F01FF for value in mapped_access))
        self.assertTrue(access_checks)
        self.assertTrue(
            all(check == (0x001F01FF, 0x001F01FF, 1) for check in access_checks)
        )

    def test_faults_are_body_free_and_baseexception_cleanup_preserves_primary(self) -> None:
        class Sentinel(BaseException):
            pass

        stable = WindowsProcessFileLock(
            _fault_injector=lambda phase: (
                (_ for _ in ()).throw(RuntimeError("secret body"))
                if phase == "lock_init_after_create"
                else None
            )
        )
        with self.assertRaises(PlatformFileError) as caught:
            stable.acquire(
                self.parent,
                "runtime.lock",
                EXPECTED,
                LockPolicy(LockWait.FAIL_FAST),
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)

        sentinel = Sentinel()

        def interrupt(phase: str) -> None:
            if phase == "lock_init_after_create":
                raise sentinel

        interrupted = WindowsProcessFileLock(_fault_injector=interrupt)
        with self.assertRaises(Sentinel) as caught_base:
            interrupted.acquire(
                self.parent,
                "base.lock",
                EXPECTED,
                LockPolicy(LockWait.FAIL_FAST),
            )
        self.assertIs(caught_base.exception, sentinel)
        recovered = WindowsProcessFileLock().acquire(
            self.parent,
            "base.lock",
            EXPECTED,
            LockPolicy(LockWait.FAIL_FAST),
        )
        recovered.close()

    def test_parent_or_entry_proof_drift_is_normalized_to_lock_unavailable(self) -> None:
        with mock.patch.object(
            platform_fs_windows._WindowsBoundDirectory,
            "_reprove",
            side_effect=platform_fs_windows._identity_stale(),
        ), self.assertRaises(PlatformFileError) as caught:
            self._acquire()
        _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)

    def test_existing_complete_loser_uses_lock_profile_before_contending(self) -> None:
        held = self._acquire()
        api = self.parent._api
        calls: list[dict[str, object]] = []
        original_open = api.open_handle

        def recording_open(path: str, **keywords: object):
            calls.append(dict(keywords))
            return original_open(path, **keywords)

        try:
            with mock.patch.object(api, "open_handle", side_effect=recording_open):
                with self.assertRaises(PlatformFileError) as caught:
                    self._acquire()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.LOCK_CONTENDED,
            )
            protocol_opens = [
                call
                for call in calls
                if int(call["desired_access"]) & 0x40000000
            ]
            self.assertEqual(
                [
                    (call["creation_disposition"], call["share_mode"])
                    for call in protocol_opens
                ],
                [(1, 0), (3, 0x3)],
            )
        finally:
            held.close()

    def test_unlock_failure_revokes_lease_and_is_stable(self) -> None:
        lease = self._acquire()
        api = self.parent._api
        with mock.patch.object(api, "UnlockFileEx", return_value=0), mock.patch.object(
            api,
            "last_error",
            return_value=5,
        ), self.assertRaises(PlatformFileError) as caught:
            lease.close()
        _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)
        self.assertTrue(lease.closed)
        recovered = self._acquire()
        recovered.close()

    def test_lease_close_detects_payload_tamper_but_still_releases(self) -> None:
        lease = self._acquire()
        lock_path = self.root_path / "resource.lock"
        lock_path.write_bytes(b"foreign")
        with self.assertRaises(PlatformFileError) as caught:
            lease.close()
        _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)
        self.assertTrue(lease.closed)
        self._close_authorities()
        lock_path.unlink()
        self._rebind()
        recovered = self._acquire()
        recovered.close()

    def test_live_lease_blocks_delete_recreate_file_id_swap(self) -> None:
        lease = self._acquire()
        lock_path = self.root_path / "resource.lock"
        replacement = self.root_path / "replacement.lock"
        replacement.write_bytes(EXPECTED)
        with self.assertRaises(PermissionError):
            lock_path.unlink()
        with self.assertRaises(PermissionError):
            os.replace(replacement, lock_path)
        self.assertTrue(lock_path.exists())
        lease.close()
        self._close_authorities()
        replacement.unlink()
        self._rebind()

    def test_real_resource_range_does_not_block_payload_bootstrap_range(self) -> None:
        lease = self._acquire()
        lock_path = self.root_path / "resource.lock"

        def range_result(offset_high: int) -> int:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.windows_lock_worker",
                    "range-once",
                    str(lock_path),
                    str(offset_high),
                ],
                cwd=ROOT,
                capture_output=True,
                check=False,
                timeout=10,
            )
            return result.returncode

        try:
            self.assertEqual(range_result(0), 0)
            self.assertEqual(range_result(1), 33)
        finally:
            lease.close()

    def _assert_two_process_handoff(self) -> None:
        command = [
            sys.executable,
            "-m",
            "tests.windows_lock_worker",
            "hold",
            str(self.root_path),
            "resource.lock",
            EXPECTED.hex(),
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        ready = [threading.Event(), threading.Event()]

        def read_ready(index: int) -> None:
            if processes[index].stdout.readline().strip() == "READY":
                ready[index].set()

        readers = [
            threading.Thread(target=read_ready, args=(index,), daemon=True)
            for index in range(2)
        ]
        for reader in readers:
            reader.start()
        try:
            deadline = time.monotonic() + 5.0
            while not any(event.is_set() for event in ready):
                if time.monotonic() >= deadline:
                    self.fail("neither first creator acquired the lock")
                time.sleep(0.01)
            time.sleep(0.08)
            self.assertEqual(sum(event.is_set() for event in ready), 1)
            winner = 0 if ready[0].is_set() else 1
            loser = 1 - winner
            processes[winner].kill()
            processes[winner].wait(timeout=10)
            self.assertTrue(ready[loser].wait(timeout=5))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            for reader in readers:
                reader.join(timeout=2)
            for process in processes:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        released = self._acquire(
            policy=LockPolicy(LockWait.TIMEOUT, timeout_seconds=5.0)
        )
        released.close()

    def test_two_first_creators_serialize_and_loser_acquires_after_kill(self) -> None:
        self._assert_two_process_handoff()

    def test_two_recoverers_serialize_partial_residue_recovery(self) -> None:
        self._seed_protocol_file()
        self._close_authorities()
        lock_path = self.root_path / "resource.lock"
        lock_path.write_bytes(EXPECTED[:19])
        self._rebind()
        self._assert_two_process_handoff()
        self._close_authorities()
        self.assertEqual(lock_path.read_bytes(), EXPECTED)
        self._rebind()

    def test_creator_process_crash_boundaries_recover(self) -> None:
        for phase in (
            "lock_init_after_create",
            "lock_init_after_write",
            "lock_init_after_flush",
            "lock_init_after_readback",
            "lock_init_after_close",
        ):
            with self.subTest(phase=phase):
                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.windows_lock_worker",
                        "crash-init",
                        str(self.root_path),
                        "resource.lock",
                        EXPECTED.hex(),
                        phase,
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(process.returncode, 87)
                lease = self._acquire(
                    policy=LockPolicy(LockWait.TIMEOUT, timeout_seconds=5.0)
                )
                lease.close()
                self.assertEqual((self.root_path / "resource.lock").read_bytes(), EXPECTED)
                (self.root_path / "resource.lock").unlink()

    def test_terminate_process_eventually_releases_kernel_lease(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.windows_lock_worker",
                "hold",
                str(self.root_path),
                "resource.lock",
                EXPECTED.hex(),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            with self.assertRaises(PlatformFileError) as caught:
                self._acquire()
            _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_CONTENDED)

            started = time.monotonic()
            with self.assertRaises(PlatformFileError) as caught:
                self._acquire(
                    policy=LockPolicy(LockWait.TIMEOUT, timeout_seconds=0.08)
                )
            elapsed = time.monotonic() - started
            _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_CONTENDED)
            self.assertGreaterEqual(elapsed, 0.05)
            self.assertLess(elapsed, 1.0)

            result: list[object] = []

            def blocking_acquire() -> None:
                try:
                    result.append(
                        self._acquire(policy=LockPolicy(LockWait.BLOCK))
                    )
                except BaseException as error:
                    result.append(error)

            waiter = threading.Thread(target=blocking_acquire)
            waiter.start()
            time.sleep(0.08)
            self.assertTrue(waiter.is_alive())
            process.kill()
            process.wait(timeout=10)
            waiter.join(timeout=5)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(len(result), 1)
            if isinstance(result[0], BaseException):
                raise result[0]
            lease = result[0]
            lease.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
