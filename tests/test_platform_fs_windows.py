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
    WindowsNativeFacts,
    probe_windows_host_facts,
    probe_windows_native_facts,
)
from windows_file_api import Win32CallError, Win32Handle, WindowsFileAPI
from windows_file_api import (
    STORAGE_DESCRIPTOR_HEADER,
    STORAGE_DEVICE_DESCRIPTOR,
    STORAGE_WRITE_CACHE_PROPERTY,
)


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


def _assert_durability_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
) -> None:
    case.assertEqual(
        caught.exception.code,
        PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
    )
    case.assertEqual(
        caught.exception.args,
        (PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,),
    )
    case.assertIsNone(caught.exception.__cause__)


class WindowsHostGateStaticTests(unittest.TestCase):
    def test_facts_are_diagnostic_only_and_expose_no_handle_or_authority_gate(self) -> None:
        for facts_type in (WindowsHostFacts, WindowsNativeFacts):
            names = {field.name for field in fields(facts_type)}
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
        classes = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }
        self.assertNotIn("WindowsPlatformAdapter", classes)
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
                    probe_windows_native_facts(root)
            _assert_capability_error(self, caught)
            load.assert_not_called()

    def test_native_fault_is_normalized_without_path_or_body(self) -> None:
        with mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            side_effect=Win32CallError("CreateFileW", 5),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                probe_windows_native_facts(ROOT)
        _assert_capability_error(self, caught)

    def test_storage_or_cache_unknown_is_durability_not_host_capability_failure(self) -> None:
        with mock.patch.object(
            platform_fs_windows,
            "_probe_windows",
            side_effect=platform_fs_windows._durability_unavailable(),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                probe_windows_native_facts(ROOT)
        _assert_durability_error(self, caught)

    def test_storage_structure_version_size_and_enums_are_fail_closed(self) -> None:
        device = STORAGE_DEVICE_DESCRIPTOR(
            Version=platform_fs_windows.ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR),
            Size=platform_fs_windows.ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR),
            BusType=17,
        )
        cache = STORAGE_WRITE_CACHE_PROPERTY(
            Version=platform_fs_windows.ctypes.sizeof(STORAGE_WRITE_CACHE_PROPERTY),
            Size=platform_fs_windows.ctypes.sizeof(STORAGE_WRITE_CACHE_PROPERTY),
            WriteCacheType=2,
            WriteCacheEnabled=2,
            WriteCacheChangeable=2,
            WriteThroughSupported=2,
            FlushCacheSupported=1,
            UserDefinedPowerProtection=0,
            NVCacheEnabled=0,
        )
        platform_fs_windows._validate_storage_facts(device, cache)
        for target, field, invalid in (
            (device, "Version", 1),
            (device, "Size", 1),
            (device, "BusType", 0),
            (cache, "Version", 1),
            (cache, "Size", 1),
            (cache, "WriteCacheChangeable", 0),
            (cache, "WriteThroughSupported", 99),
            (cache, "FlushCacheSupported", 2),
        ):
            original = getattr(target, field)
            setattr(target, field, invalid)
            with self.subTest(field=field), self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._validate_storage_facts(device, cache)
            _assert_durability_error(self, caught)
            setattr(target, field, original)

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

    def test_storage_query_two_stage_closes_size_version_and_returned_bytes(self) -> None:
        expected = platform_fs_windows.ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR)

        def run_case(
            *,
            header_returned: int = 8,
            header_version: int = expected,
            header_size: int = 64,
            repeated_version: int | None = None,
            repeated_size: int | None = None,
            full_returned: int | None = None,
        ) -> STORAGE_DEVICE_DESCRIPTOR:
            calls = 0

            def device_io(
                api: object,
                raw_handle: int,
                query: object,
                output: object,
                output_size: int,
            ) -> int:
                nonlocal calls
                del api, raw_handle, query
                calls += 1
                if calls == 1:
                    self.assertEqual(output_size, 8)
                    header = platform_fs_windows.ctypes.cast(
                        output,
                        platform_fs_windows.ctypes.POINTER(STORAGE_DESCRIPTOR_HEADER),
                    ).contents
                    header.Version = header_version
                    header.Size = header_size
                    return header_returned
                self.assertEqual(output_size, header_size)
                descriptor = STORAGE_DEVICE_DESCRIPTOR(
                    Version=(
                        header_version
                        if repeated_version is None
                        else repeated_version
                    ),
                    Size=header_size if repeated_size is None else repeated_size,
                    BusType=17,
                )
                platform_fs_windows.ctypes.memmove(
                    output,
                    platform_fs_windows.ctypes.byref(descriptor),
                    min(output_size, platform_fs_windows.ctypes.sizeof(descriptor)),
                )
                return output_size if full_returned is None else full_returned

            with mock.patch.object(
                platform_fs_windows,
                "_device_io_control",
                side_effect=device_io,
            ):
                return platform_fs_windows._storage_query(
                    object(),
                    7,
                    platform_fs_windows.STORAGE_DEVICE_PROPERTY,
                    STORAGE_DEVICE_DESCRIPTOR,
                    exact_size=False,
                )

        result = run_case()
        self.assertEqual(result.Version, expected)
        self.assertEqual(result.Size, 64)
        for kwargs in (
            {"header_returned": 7},
            {"header_version": expected + 1},
            {"header_size": platform_fs_windows._MAX_STORAGE_DESCRIPTOR + 1},
            {"full_returned": 63},
            {"repeated_size": 63},
            {"repeated_version": expected + 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                PlatformFileError
            ) as caught:
                run_case(**kwargs)
            _assert_durability_error(self, caught)

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
                platform_fs_windows._probe_windows(ROOT, include_storage=False)
        _assert_capability_error(self, caught)
        self.assertEqual(paths, [str(ROOT), guid_root])

    def test_volume_device_open_failure_is_durability_unavailable(self) -> None:
        paths: list[str] = []
        guid_root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"

        class API:
            GetDriveTypeW = staticmethod(lambda path: platform_fs_windows.DRIVE_FIXED)

            @staticmethod
            def open_handle(path: str, **kwargs: object) -> Win32Handle:
                del kwargs
                paths.append(path)
                if len(paths) == 3:
                    raise Win32CallError("CreateFileW", 5)
                return Win32Handle(110 + len(paths), _close=lambda value: 1)

        volume = (
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
            side_effect=(volume, volume),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._probe_windows(ROOT, include_storage=True)
        _assert_durability_error(self, caught)
        self.assertEqual(
            paths,
            [str(ROOT), guid_root, guid_root.rstrip("\\")],
        )


@unittest.skipUnless(sys.platform == "win32", "real Windows gate requires Windows")
class WindowsHostGateRuntimeTests(unittest.TestCase):
    def test_real_windows_11_x64_local_ntfs_probe_returns_native_facts_only(self) -> None:
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

    def test_real_storage_probe_returns_complete_facts_or_stable_durability_error(self) -> None:
        try:
            facts = probe_windows_native_facts(ROOT)
        except PlatformFileError as error:
            self.assertEqual(
                error.code,
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
            )
            self.assertEqual(error.args, (error.code,))
        else:
            self.assertIs(type(facts), WindowsNativeFacts)
            self.assertGreater(facts.storage_bus_type, 0)
            self.assertGreater(facts.write_cache_type, 0)
            self.assertGreater(facts.write_cache_enabled, 0)
            self.assertGreater(facts.write_cache_changeable, 0)
            self.assertGreater(facts.write_through_supported, 0)

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

    def test_real_native_probe_uses_directory_and_device_guid_handles(self) -> None:
        api = WindowsFileAPI.load()
        real_open = api.open_handle
        opened: list[str] = []

        def recording_open(path: str, **kwargs: object) -> Win32Handle:
            opened.append(path)
            return real_open(path, **kwargs)

        with mock.patch.object(
            api,
            "open_handle",
            side_effect=recording_open,
        ), mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            return_value=api,
        ):
            try:
                probe_windows_native_facts(ROOT)
            except PlatformFileError as error:
                self.assertEqual(
                    error.code,
                    PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
                )
        self.assertEqual(opened[0], str(ROOT))
        self.assertTrue(opened[1].startswith(r"\\?\Volume{"))
        self.assertTrue(opened[1].endswith("}\\"))
        self.assertEqual(opened[2], opened[1].rstrip("\\"))

    def test_regular_file_and_unc_roots_fail_without_reading_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.write_bytes(b"do not read this body")
            for root in (source, Path(r"\\server\share")):
                with self.subTest(root=root), self.assertRaises(PlatformFileError) as caught:
                    probe_windows_host_facts(root)
                _assert_capability_error(self, caught)
            self.assertEqual(source.read_bytes(), b"do not read this body")

    def test_incomplete_windows_backend_keeps_full_composition_unavailable(self) -> None:
        with self.assertRaises(PlatformFileError) as caught:
            compose_platform_file_backend(ROOT)
        _assert_capability_error(self, caught)


if __name__ == "__main__":
    unittest.main()
