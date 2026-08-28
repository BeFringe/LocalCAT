"""Task 3.1 supported Windows host/volume gate tests."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
import platform_fs_windows
from platform_fs_windows import (
    WindowsHostFacts,
    probe_windows_host_facts,
)
from windows_file_api import Win32CallError, Win32Handle, WindowsFileAPI


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ADAPTER_PATH = ROOT / "platform_fs_windows.py"


def _assert_capability_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
) -> None:
    case.assertEqual(
        caught.exception.code,
        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
    )
    case.assertEqual(
        caught.exception.args,
        (PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,),
    )
    case.assertIsNone(caught.exception.__cause__)


class WindowsHostGateStaticTests(unittest.TestCase):
    def test_facts_are_diagnostic_only_and_expose_no_handle_or_authority_gate(self) -> None:
        names = {field.name for field in fields(WindowsHostFacts)}
        self.assertFalse(
            names
            & {
                "available",
                "validated",
                "authority",
                "capability",
                "handle",
                "token",
            }
        )
        source = WINDOWS_ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertIn("WindowsPlatformAdapter", classes)
        self.assertNotIn("GetVolumePathNameW", source)
        self.assertNotIn("GetVolumeNameForVolumeMountPointW", source)
        self.assertNotIn("GetVolumeInformationW", source)

    def test_unknown_host_and_relative_root_fail_before_loading_native_api(self) -> None:
        for host, root in (("linux", ROOT), ("win32", Path("relative"))):
            with self.subTest(host=host), mock.patch.object(
                platform_fs_windows.sys, "platform", host
            ), mock.patch.object(
                platform_fs_windows.WindowsFileAPI, "load"
            ) as load:
                with self.assertRaises(PlatformFileError) as caught:
                    probe_windows_host_facts(root)
            _assert_capability_error(self, caught)
            load.assert_not_called()

    def test_native_fault_is_normalized_without_path_or_body(self) -> None:
        with mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            side_effect=Win32CallError("CreateFileW", 5),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                probe_windows_host_facts(ROOT)
        _assert_capability_error(self, caught)

    def test_final_path_query_grows_buffer_before_accepting_facts(self) -> None:
        sizes: list[int] = []

        class API:
            def GetFinalPathNameByHandleW(
                self,
                handle: int,
                buffer: object,
                size: int,
                flags: int,
            ) -> int:
                del handle, flags
                sizes.append(size)
                if buffer is None:
                    return 4
                if len(sizes) == 2:
                    return 12
                buffer.value = r"\\?\E:\root"
                return len(buffer.value)

            @staticmethod
            def checked_length(name: str, result: int) -> int:
                del name
                if result == 0:
                    raise AssertionError("unexpected zero length")
                return result

        result = platform_fs_windows._final_path(
            API(),
            7,
            platform_fs_windows.VOLUME_NAME_GUID,
        )
        self.assertEqual(sizes, [0, 5, 13])
        self.assertEqual(result, r"\\?\E:\root")

    def test_final_path_rejects_initial_and_grown_oversize_before_allocation(self) -> None:
        class API:
            def __init__(self, results: list[int]) -> None:
                self.results = iter(results)

            def GetFinalPathNameByHandleW(
                self,
                handle: int,
                buffer: object,
                size: int,
                flags: int,
            ) -> int:
                del handle, buffer, size, flags
                return next(self.results)

            @staticmethod
            def checked_length(name: str, result: int) -> int:
                del name
                return result

        real_allocate = platform_fs_windows.ctypes.create_unicode_buffer
        for results, expected_allocations in (
            ([platform_fs_windows._MAX_PATH_BUFFER], []),
            ([4, platform_fs_windows._MAX_PATH_BUFFER], [5]),
        ):
            allocations: list[int] = []

            def allocate(size: int) -> object:
                allocations.append(size)
                return real_allocate(size)

            with self.subTest(results=results), mock.patch.object(
                platform_fs_windows.ctypes,
                "create_unicode_buffer",
                side_effect=allocate,
            ), self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._final_path(
                    API(results),
                    7,
                    platform_fs_windows.VOLUME_NAME_GUID,
                )
            _assert_capability_error(self, caught)
            self.assertEqual(allocations, expected_allocations)

    def test_runtime_gate_rejects_wrong_python_or_emulated_architecture(self) -> None:
        valid = {
            "windows_major": 10,
            "windows_build": 22000,
            "product_type": platform_fs_windows.VER_NT_WORKSTATION,
            "python_implementation": "cpython",
            "python_version": (3, 14),
            "python_bits": 64,
            "process_machine": platform_fs_windows.IMAGE_FILE_MACHINE_UNKNOWN,
            "native_machine": platform_fs_windows.IMAGE_FILE_MACHINE_AMD64,
        }
        platform_fs_windows._validate_runtime_facts(**valid)
        for field, invalid in (
            ("python_implementation", "pypy"),
            ("python_version", (3, 13)),
            ("python_bits", 32),
            ("process_machine", platform_fs_windows.IMAGE_FILE_MACHINE_AMD64),
            ("native_machine", 0xAA64),
        ):
            facts = dict(valid)
            facts[field] = invalid
            with self.subTest(field=field), self.assertRaises(
                PlatformFileError
            ) as caught:
                platform_fs_windows._validate_runtime_facts(**facts)
            _assert_capability_error(self, caught)

    def test_drive_type_enum_failure_does_not_consult_last_error(self) -> None:
        class API:
            GetDriveTypeW = staticmethod(lambda path: 0)

            @staticmethod
            def last_error() -> int:
                raise AssertionError("GetDriveTypeW has no reliable last-error contract")

        with self.assertRaises(PlatformFileError) as caught:
            platform_fs_windows._fixed_drive_type(API(), "guid-root")
        _assert_capability_error(self, caught)

    def test_guid_root_extraction_is_strict(self) -> None:
        guid_root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"
        self.assertEqual(
            platform_fs_windows._guid_root_from_final_path(guid_root + "folder"),
            guid_root,
        )
        for invalid in (
            r"E:\folder",
            r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdeg}\folder",
            r"\\?\Volume{0123456789ab-cdef-0123-456789abcdef}\folder",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(PlatformFileError):
                platform_fs_windows._guid_root_from_final_path(invalid)

    def test_guid_volume_root_fact_mismatch_fails_closed(self) -> None:
        paths: list[str] = []
        guid_root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"

        class API:
            GetDriveTypeW = staticmethod(lambda path: platform_fs_windows.DRIVE_FIXED)

            @staticmethod
            def open_handle(path: str, **kwargs: object) -> Win32Handle:
                del kwargs
                paths.append(path)
                return Win32Handle(100 + len(paths), _close=lambda value: 1)

        root_volume = (
            1,
            "NTFS",
            platform_fs_windows.FILE_PERSISTENT_ACLS
            | platform_fs_windows.FILE_SUPPORTS_REPARSE_POINTS,
            255,
        )
        with mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            return_value=API(),
        ), mock.patch.object(
            platform_fs_windows,
            "_root_handle_facts",
            return_value=(2, b"f" * 16, 0, guid_root),
        ), mock.patch.object(
            platform_fs_windows,
            "_machine_facts",
            return_value=(
                platform_fs_windows.IMAGE_FILE_MACHINE_UNKNOWN,
                platform_fs_windows.IMAGE_FILE_MACHINE_AMD64,
            ),
        ), mock.patch.object(
            platform_fs_windows,
            "_volume_facts_by_handle",
            side_effect=(root_volume, (2, "NTFS", root_volume[2], 255)),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._probe_windows(ROOT)
        _assert_capability_error(self, caught)
        self.assertEqual(paths, [str(ROOT), guid_root])


@unittest.skipUnless(sys.platform == "win32", "real Windows gate requires Windows")
class WindowsHostGateRuntimeTests(unittest.TestCase):
    def test_real_windows_11_x64_local_ntfs_probe_returns_host_facts_only(self) -> None:
        before = tuple(ROOT.iterdir())
        facts = probe_windows_host_facts(ROOT)
        after = tuple(ROOT.iterdir())
        self.assertIs(type(facts), WindowsHostFacts)
        self.assertGreaterEqual(facts.windows_build, 22000)
        self.assertEqual(facts.python_implementation, "cpython")
        self.assertEqual((facts.python_major, facts.python_minor), (3, 14))
        self.assertEqual(facts.python_bits, 64)
        self.assertEqual(
            facts.process_machine,
            platform_fs_windows.IMAGE_FILE_MACHINE_UNKNOWN,
        )
        self.assertEqual(
            facts.native_machine,
            platform_fs_windows.IMAGE_FILE_MACHINE_AMD64,
        )
        self.assertEqual(facts.drive_type, platform_fs_windows.DRIVE_FIXED)
        self.assertEqual(facts.file_system, "NTFS")
        self.assertGreaterEqual(facts.maximum_component_length, 1)
        self.assertTrue(facts.volume_flags & platform_fs_windows.FILE_PERSISTENT_ACLS)
        self.assertTrue(
            facts.volume_flags & platform_fs_windows.FILE_SUPPORTS_REPARSE_POINTS
        )
        self.assertEqual(len(facts.root_file_id), 16)
        self.assertEqual(facts.root_reparse_tag, 0)
        self.assertEqual(after, before)

    def test_root_handle_is_closed_when_first_native_reproof_fails(self) -> None:
        api = WindowsFileAPI.load()
        original_close = api.CloseHandle
        with mock.patch.object(
            api,
            "GetFileInformationByHandleEx",
            return_value=0,
        ), mock.patch.object(
            api,
            "CloseHandle",
            wraps=original_close,
        ) as close_handle, mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            return_value=api,
        ):
            with self.assertRaises(PlatformFileError) as caught:
                probe_windows_host_facts(ROOT)
        _assert_capability_error(self, caught)
        close_handle.assert_called_once()

    def test_regular_file_and_unc_roots_fail_without_reading_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.write_bytes(b"do not read this body")
            for root in (source, Path(r"\\server\share")):
                with self.subTest(root=root), self.assertRaises(PlatformFileError) as caught:
                    probe_windows_host_facts(root)
                _assert_capability_error(self, caught)
            self.assertEqual(source.read_bytes(), b"do not read this body")

    def test_completed_private_backend_is_composed_after_live_root_probe(self) -> None:
        backend = compose_platform_file_backend(ROOT)
        self.assertIs(type(backend), platform_fs_windows.WindowsPlatformAdapter)
        with backend.bind_root(ROOT) as authority:
            authority.reprove()


if __name__ == "__main__":
    unittest.main()
