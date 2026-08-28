"""Task 3.2 Windows rooted-handle read authority tests."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import platform_fs_windows
from platform_fs_contracts import (
    LedgerEnumerationLimits,
    LockPolicy,
    LockWait,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    ProcessFileLock,
    PublishMode,
    RootedFileSystem,
)
from platform_fs_windows import WindowsPlatformAdapter, WindowsRootedFileSystem
from windows_file_api import Win32Handle


ROOT = Path(__file__).resolve().parents[1]


def _assert_platform_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode,
) -> None:
    case.assertEqual(caught.exception.code, code.value)
    case.assertEqual(caught.exception.args, (code.value,))
    case.assertIsNone(caught.exception.__cause__)


class WindowsRootedStaticTests(unittest.TestCase):
    def test_task_3_2_mints_only_rooted_file_system_shape(self) -> None:
        rooted = WindowsRootedFileSystem()
        self.assertIsInstance(rooted, RootedFileSystem)
        self.assertNotIsInstance(rooted, ProcessFileLock)
        self.assertNotIsInstance(rooted, PrivateStorageProof)
        self.assertNotIsInstance(rooted, PlatformFileBackend)

    def test_ledger_observation_uses_retained_directory_api_not_path_listing(self) -> None:
        source = (ROOT / "platform_fs_windows.py").read_text(encoding="utf-8")
        contracts = (ROOT / "platform_fs_contracts.py").read_text(encoding="utf-8")
        self.assertIn("FILE_ID_EXTD_DIR_INFO_CLASS", source)
        self.assertIn("GetFileInformationByHandleEx", source)
        self.assertNotIn("FindFirstFile", source)
        self.assertNotIn("FindNextFile", source)
        self.assertNotIn("os.scandir", source)
        self.assertNotIn("Win32Handle", contracts)

    def test_directory_entry_abi_and_record_bounds_fail_closed(self) -> None:
        self.assertEqual(platform_fs_windows._FILE_ID_EXTD_DIR_HEADER.size, 88)
        platform_fs_windows._validate_directory_record_bounds(
            offset=0,
            name_length=2,
            next_offset=96,
            buffer_size=192,
        )
        for name_length, next_offset in ((2, 88), (3, 96), (2, 94)):
            with self.subTest(
                name_length=name_length,
                next_offset=next_offset,
            ), self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._validate_directory_record_bounds(
                    offset=0,
                    name_length=name_length,
                    next_offset=next_offset,
                    buffer_size=192,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )

    def test_component_grammar_is_fail_closed(self) -> None:
        hostile = (
            "",
            ".",
            "..",
            "stream:name",
            "bad<name",
            "bad>name",
            'bad"name',
            "bad/name",
            "bad\\name",
            "bad|name",
            "bad?name",
            "bad*name",
            "trail.",
            "trail ",
            "control\x1f",
            "CON",
            "con.txt",
            "CONIN$",
            "CONOUT$.txt",
            "COM1.log",
            "LPT9",
            "COM¹.txt",
            "LPT³.bin",
        )
        for component in hostile:
            with self.subTest(component=component), self.assertRaises(
                PlatformFileError
            ) as caught:
                platform_fs_windows._validate_windows_component(
                    component,
                    maximum_units=255,
                )
            _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)

    def test_live_component_and_extended_path_limits_precede_open(self) -> None:
        with self.assertRaises(PlatformFileError) as caught:
            platform_fs_windows._validate_windows_component(
                "a" * 6,
                maximum_units=5,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)

        parent = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\" + (
            "a" * 32740
        )
        with self.assertRaises(PlatformFileError) as caught:
            platform_fs_windows._append_component(
                parent,
                "tail",
                maximum_units=255,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)

    def test_full_volume_id_mismatch_is_rejected(self) -> None:
        identity = platform_fs_windows.FileObjectIdentity(
            platform="windows",
            volume_id=b"a" * 8,
            file_id=b"f" * 16,
            kind="regular",
            link_count=1,
        )
        with self.assertRaises(PlatformFileError) as caught:
            platform_fs_windows._require_volume(identity, b"b" * 8, stale=False)
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )

    def test_chain_reproof_rejects_forged_full_volume_id(self) -> None:
        original = platform_fs_windows.FileObjectIdentity(
            platform="windows",
            volume_id=b"a" * 8,
            file_id=b"f" * 16,
            kind="directory",
            link_count=1,
        )
        forged = platform_fs_windows.FileObjectIdentity(
            platform="windows",
            volume_id=b"b" * 8,
            file_id=b"f" * 16,
            kind="directory",
            link_count=1,
        )
        snapshot = platform_fs_windows.EntrySnapshot(
            identity=forged,
            byte_count=0,
            modified_token=b"m",
            reparse_free=True,
        )
        record = platform_fs_windows._WindowsDirectoryRecord(
            Win32Handle(123, _close=lambda value: 1),
            "expected",
            original,
        )
        proof = platform_fs_windows._WindowsHandleProof(forged, snapshot, "expected")
        try:
            with mock.patch.object(
                platform_fs_windows,
                "_capture_handle_proof",
                return_value=proof,
            ), self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._reprove_directory_chain(object(), (record,))
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
        finally:
            record.handle.close()

    def test_normalized_final_path_rejects_alias_spelling(self) -> None:
        def query(
            api: object,
            raw: int,
            info_class: int,
            result: object,
        ) -> None:
            del api, raw
            if info_class == platform_fs_windows.FILE_STANDARD_INFO_CLASS:
                result.NumberOfLinks = 1
                result.Directory = 0
                result.EndOfFile = 1
            elif info_class == platform_fs_windows.FILE_ATTRIBUTE_TAG_INFO_CLASS:
                result.FileAttributes = 0
                result.ReparseTag = 0
            elif info_class == platform_fs_windows.FILE_BASIC_INFO_CLASS:
                result.FileAttributes = 0
            elif info_class == platform_fs_windows.FILE_ID_INFO_CLASS:
                result.VolumeSerialNumber = 1
                result.FileId.Identifier[0] = 1

        alias_path = r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\LONGFI~1.TXT"
        normalized_path = r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\LongFileName.txt"
        with mock.patch.object(
            platform_fs_windows,
            "_query_file_info",
            side_effect=query,
        ), mock.patch.object(
            platform_fs_windows,
            "_final_path",
            return_value=normalized_path,
        ), self.assertRaises(PlatformFileError) as caught:
            platform_fs_windows._capture_handle_proof(
                object(),
                7,
                expected_final_path=alias_path,
                expected_kind="regular",
                stale=False,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)

    def test_wrong_kind_uses_stage_specific_stable_failure(self) -> None:
        def query(
            api: object,
            raw: int,
            info_class: int,
            result: object,
        ) -> None:
            del api, raw
            if info_class == platform_fs_windows.FILE_STANDARD_INFO_CLASS:
                result.NumberOfLinks = 1
                result.Directory = 1
            elif info_class == platform_fs_windows.FILE_ATTRIBUTE_TAG_INFO_CLASS:
                result.FileAttributes = platform_fs_windows.FILE_ATTRIBUTE_DIRECTORY
                result.ReparseTag = 0
            elif info_class == platform_fs_windows.FILE_BASIC_INFO_CLASS:
                result.FileAttributes = platform_fs_windows.FILE_ATTRIBUTE_DIRECTORY
            elif info_class == platform_fs_windows.FILE_ID_INFO_CLASS:
                result.VolumeSerialNumber = 1
                result.FileId.Identifier[0] = 1

        path = r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\directory"
        for stale, expected_code in (
            (False, PlatformFileErrorCode.CAPABILITY_UNAVAILABLE),
            (True, PlatformFileErrorCode.IDENTITY_STALE),
        ):
            with self.subTest(stale=stale), mock.patch.object(
                platform_fs_windows,
                "_query_file_info",
                side_effect=query,
            ), mock.patch.object(
                platform_fs_windows,
                "_final_path",
                return_value=path,
            ), self.assertRaises(PlatformFileError) as caught:
                platform_fs_windows._capture_handle_proof(
                    object(),
                    7,
                    expected_final_path=path,
                    expected_kind="regular",
                    stale=stale,
                )
            _assert_platform_error(self, caught, expected_code)

    def test_bound_file_constructor_preserves_original_baseexception_on_close_fault(self) -> None:
        directory_identity = platform_fs_windows.FileObjectIdentity(
            platform="windows",
            volume_id=b"v" * 8,
            file_id=b"d" * 16,
            kind="directory",
            link_count=1,
        )
        source_identity = platform_fs_windows.FileObjectIdentity(
            platform="windows",
            volume_id=b"v" * 8,
            file_id=b"f" * 16,
            kind="regular",
            link_count=1,
        )
        close_order: list[int] = []
        records = tuple(
            platform_fs_windows._WindowsDirectoryRecord(
                Win32Handle(value, _close=lambda raw: close_order.append(raw) or 1),
                f"directory-{value}",
                directory_identity,
            )
            for value in (1, 2)
        )

        def source_close(raw: int) -> int:
            del raw
            raise platform_fs_windows.Win32CallError("CloseHandle", 6)

        source = Win32Handle(3, _close=source_close)

        class StopConstruction(BaseException):
            pass

        with mock.patch.object(
            platform_fs_windows._WindowsBoundRegularFile,
            "_reprove",
            side_effect=StopConstruction(),
        ), self.assertRaises(StopConstruction):
            platform_fs_windows._WindowsBoundRegularFile(
                object(),
                records,
                source,
                "source",
                source_identity,
                255,
                None,
            )
        self.assertTrue(source.closed)
        self.assertTrue(all(record.handle.closed for record in records))
        self.assertEqual(close_order, [2, 1])


@unittest.skipUnless(sys.platform == "win32", "real rooted tests require Windows")
class WindowsRootedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-rooted-",
            dir=ROOT.parent,
        )
        self.container = Path(self._temporary.name)
        self.root_path = self.container / "RootCase"
        self.nested = self.root_path / "NestedCase"
        self.nested.mkdir(parents=True)
        self.payload = (b"rooted-read\x00" * 7000) + b"tail"
        self.source = self.nested / "sample.txt"
        self.source.write_bytes(self.payload)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _assert_concurrent_close_is_identity_stale(
        self,
        authority: object,
        target_handle: Win32Handle,
        public_call: object,
    ) -> None:
        entered = threading.Event()
        closed = threading.Event()
        close_errors: list[BaseException] = []
        real_borrow = Win32Handle.borrow

        def interleaved_borrow(handle: Win32Handle) -> object:
            if handle is target_handle:
                entered.set()
                if not closed.wait(5.0):
                    raise AssertionError("concurrent close did not complete")
            return real_borrow(handle)

        def close_authority() -> None:
            if not entered.wait(5.0):
                close_errors.append(AssertionError("reproof did not reach target handle"))
                closed.set()
                return
            try:
                authority.close()
            except BaseException as error:
                close_errors.append(error)
            finally:
                closed.set()

        closer = threading.Thread(target=close_authority, daemon=True)
        closer.start()
        with mock.patch.object(Win32Handle, "borrow", new=interleaved_borrow), self.assertRaises(
            PlatformFileError
        ) as caught:
            public_call()
        closer.join(5.0)
        self.assertFalse(closer.is_alive())
        self.assertEqual(close_errors, [])
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.IDENTITY_STALE,
        )
        self.assertTrue(authority.closed)
        authority.close()

    def test_nested_read_survives_root_close_and_is_repeatable(self) -> None:
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        source = file_system.open_regular(
            root,
            PureWindowsPath("NestedCase", "sample.txt"),
        )
        before = source.snapshot()
        identity = source.identity()
        root.close()

        self.assertEqual(source.read_all(), self.payload)
        self.assertEqual(source.read_all(), self.payload)
        self.assertEqual(source.snapshot(), before)
        self.assertEqual(identity.kind, "regular")
        self.assertEqual(identity.link_count, 1)
        source.close()

    def test_root_reprove_concurrent_close_is_stable_and_idempotent(self) -> None:
        root = WindowsRootedFileSystem().bind_root(self.root_path)
        self._assert_concurrent_close_is_identity_stale(
            root,
            root._records[0].handle,
            root.reprove,
        )
        self.assertTrue(all(record.handle.closed for record in root._records))

    def test_parent_inspect_concurrent_close_is_stable_before_body(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            parent = file_system.bind_parent(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        with mock.patch.object(
            parent._api,
            "ReadFile",
            wraps=parent._api.ReadFile,
        ) as read_file:
            self._assert_concurrent_close_is_identity_stale(
                parent,
                parent._records[0].handle,
                lambda: parent.inspect_entry("sample.txt"),
            )
        read_file.assert_not_called()
        self.assertTrue(all(record.handle.closed for record in parent._records))

    def test_file_identity_and_snapshot_concurrent_close_are_stable_before_body(self) -> None:
        for operation in ("identity", "snapshot"):
            with self.subTest(operation=operation):
                file_system = WindowsRootedFileSystem()
                with file_system.bind_root(self.root_path) as root:
                    source = file_system.open_regular(
                        root,
                        PureWindowsPath("NestedCase", "sample.txt"),
                    )
                with mock.patch.object(
                    source._api,
                    "ReadFile",
                    wraps=source._api.ReadFile,
                ) as read_file:
                    self._assert_concurrent_close_is_identity_stale(
                        source,
                        source._handle,
                        getattr(source, operation),
                    )
                read_file.assert_not_called()
                self.assertTrue(source._handle.closed)
                self.assertTrue(
                    all(record.handle.closed for record in source._records)
                )

    def test_reproof_preserves_platform_error_and_propagates_baseexception(self) -> None:
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        target = root._records[0].handle
        real_borrow = Win32Handle.borrow

        def platform_failure(handle: Win32Handle) -> object:
            if handle is target:
                raise platform_fs_windows._capability_unavailable()
            return real_borrow(handle)

        class StopReproof(BaseException):
            pass

        def base_failure(handle: Win32Handle) -> object:
            if handle is target:
                raise StopReproof()
            return real_borrow(handle)

        try:
            with mock.patch.object(
                Win32Handle,
                "borrow",
                new=platform_failure,
            ), self.assertRaises(PlatformFileError) as caught:
                root.reprove()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )
            with mock.patch.object(
                Win32Handle,
                "borrow",
                new=base_failure,
            ), self.assertRaises(StopReproof):
                root.reprove()
        finally:
            root.close()

    def test_public_bind_root_normalizes_native_loader_failure(self) -> None:
        file_system = WindowsRootedFileSystem()
        with mock.patch.object(
            platform_fs_windows.WindowsFileAPI,
            "load",
            side_effect=platform_fs_windows.Win32CallError("LoadLibraryW", 126),
        ), self.assertRaises(PlatformFileError) as caught:
            file_system.bind_root(self.root_path)
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )

    def test_public_derived_operations_normalize_duplicate_failure_and_cleanup(self) -> None:
        for operation in ("open_regular", "bind_parent"):
            with self.subTest(operation=operation):
                file_system = WindowsRootedFileSystem()
                root = file_system.bind_root(self.root_path)
                real_duplicate = root._api.duplicate_handle
                duplicates: list[object] = []

                def fail_second(handle: object) -> object:
                    if not duplicates:
                        duplicate = real_duplicate(handle)
                        duplicates.append(duplicate)
                        return duplicate
                    raise platform_fs_windows.Win32CallError("DuplicateHandle", 6)

                try:
                    with mock.patch.object(
                        root._api,
                        "duplicate_handle",
                        side_effect=fail_second,
                    ), self.assertRaises(PlatformFileError) as caught:
                        getattr(file_system, operation)(
                            root,
                            PureWindowsPath("NestedCase", "sample.txt"),
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    )
                    self.assertEqual(len(duplicates), 1)
                    self.assertTrue(duplicates[0].closed)
                    root.reprove()
                finally:
                    root.close()

    def test_public_duplicate_cleanup_close_fault_remains_body_free(self) -> None:
        for operation in ("open_regular", "bind_parent"):
            with self.subTest(operation=operation):
                file_system = WindowsRootedFileSystem()
                root = file_system.bind_root(self.root_path)

                def close_failure(raw: int) -> int:
                    del raw
                    raise platform_fs_windows.Win32CallError("CloseHandle", 6)

                first = Win32Handle(987, _close=close_failure)
                calls = 0

                def duplicate_then_fail(handle: object) -> object:
                    nonlocal calls
                    del handle
                    calls += 1
                    if calls == 1:
                        return first
                    raise platform_fs_windows.Win32CallError("DuplicateHandle", 6)

                try:
                    with mock.patch.object(
                        root._api,
                        "duplicate_handle",
                        side_effect=duplicate_then_fail,
                    ), self.assertRaises(PlatformFileError) as caught:
                        getattr(file_system, operation)(
                            root,
                            PureWindowsPath("NestedCase", "sample.txt"),
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    )
                    self.assertTrue(first.closed)
                finally:
                    root.close()

    def test_public_preflight_reproof_fault_is_normalized_before_body(self) -> None:
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        try:
            with mock.patch.object(
                platform_fs_windows,
                "_capture_handle_proof",
                side_effect=platform_fs_windows.Win32CallError(
                    "GetFileInformationByHandleEx",
                    6,
                ),
            ), mock.patch.object(
                root._api,
                "ReadFile",
                wraps=root._api.ReadFile,
            ) as read_file:
                for operation in ("open_regular", "bind_parent"):
                    with self.subTest(operation=operation), self.assertRaises(
                        PlatformFileError
                    ) as caught:
                        getattr(file_system, operation)(
                            root,
                            PureWindowsPath("NestedCase", "sample.txt"),
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.IDENTITY_STALE,
                    )
            read_file.assert_not_called()
        finally:
            root.close()

    def test_retained_ancestor_blocks_container_rename(self) -> None:
        moved = self.container.with_name(self.container.name + "-moved")
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        renamed = False
        error: OSError | None = None
        try:
            try:
                os.rename(self.container, moved)
                renamed = True
            except OSError as caught:
                error = caught
        finally:
            root.close()
            if renamed:
                os.rename(moved, self.container)
        self.assertIsInstance(error, PermissionError)
        self.assertEqual(error.winerror, 32)

    def test_intermediate_probe_to_retained_swap_is_identity_rejected(self) -> None:
        alien = self.root_path / "AlienCase"
        alien.mkdir()
        (alien / "sample.txt").write_bytes(b"alien-body")
        backup = self.root_path / "OriginalNested"
        api = platform_fs_windows.WindowsFileAPI.load()
        with api.open_handle(
            str(self.nested),
            desired_access=(
                platform_fs_windows.FILE_READ_ATTRIBUTES
                | platform_fs_windows.SYNCHRONIZE
            ),
            share_mode=(
                platform_fs_windows.FILE_SHARE_READ
                | platform_fs_windows.FILE_SHARE_WRITE
                | platform_fs_windows.FILE_SHARE_DELETE
            ),
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=(
                platform_fs_windows.FILE_FLAG_BACKUP_SEMANTICS
                | platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT
            ),
        ) as initial_handle:
            with initial_handle.borrow() as raw_initial:
                initial = platform_fs_windows._capture_handle_proof(
                    api,
                    raw_initial,
                    expected_final_path=None,
                    expected_kind="directory",
                    stale=False,
                )
        real_open = api.open_handle
        swapped = False
        observed_opens: list[tuple[str, int]] = []

        def swap_before_retained(path: str, **kwargs: object) -> object:
            nonlocal swapped
            observed_opens.append((path, int(kwargs["desired_access"])))
            if (
                not swapped
                and path.endswith("\\NestedCase")
                and int(kwargs["desired_access"])
                & platform_fs_windows.FILE_LIST_DIRECTORY
            ):
                os.rename(self.nested, backup)
                try:
                    os.rename(alien, self.nested)
                except BaseException:
                    os.rename(backup, self.nested)
                    raise
                swapped = True
            return real_open(path, **kwargs)

        opened = None
        caught_error = None
        try:
            with mock.patch.object(
                api,
                "open_handle",
                side_effect=swap_before_retained,
            ), mock.patch.object(
                api,
                "ReadFile",
                wraps=api.ReadFile,
            ) as read_file:
                try:
                    opened = platform_fs_windows._open_directory_record(
                        api,
                        initial.final_path,
                        initial.final_path,
                        stale=False,
                        expected_volume_id=initial.identity.volume_id,
                    )
                except PlatformFileError as error:
                    caught_error = error
            self.assertTrue(swapped, observed_opens)
            self.assertIsNotNone(caught_error)
            self.assertEqual(
                caught_error.code,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
            )
            self.assertEqual(
                caught_error.args,
                (PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,),
            )
            self.assertIsNone(caught_error.__cause__)
            read_file.assert_not_called()
        finally:
            if opened is not None:
                opened.handle.close()
            if swapped:
                os.rename(self.nested, alien)
                os.rename(backup, self.nested)

    def test_bind_root_walks_from_drive_root_with_approved_directory_access(self) -> None:
        api = platform_fs_windows.WindowsFileAPI.load()
        real_open = api.open_handle
        opens: list[tuple[str, int]] = []

        def recording_open(path: str, **kwargs: object) -> object:
            opens.append((path, int(kwargs["desired_access"])))
            return real_open(path, **kwargs)

        file_system = WindowsRootedFileSystem(_api=api)
        with mock.patch.object(api, "open_handle", side_effect=recording_open):
            with file_system.bind_root(self.root_path):
                pass
        self.assertEqual(opens[0][0], f"{self.root_path.drive}\\")
        self.assertEqual(len(opens), 1 + 2 * len(self.root_path.parts[1:]))
        self.assertTrue(opens[0][1] & platform_fs_windows.FILE_LIST_DIRECTORY)
        for index in range(1, len(opens), 2):
            probe_path, probe_access = opens[index]
            retained_path, retained_access = opens[index + 1]
            self.assertEqual(probe_path, retained_path)
            self.assertFalse(probe_access & platform_fs_windows.FILE_LIST_DIRECTORY)
            self.assertTrue(
                retained_access & platform_fs_windows.FILE_LIST_DIRECTORY,
                retained_path,
            )
        guid_paths = [path for path, _ in opens[2::2]]
        self.assertTrue(all(path.startswith("\\\\?\\Volume{") for path in guid_paths))
        self.assertEqual(guid_paths[-1].split("\\")[-1], self.root_path.name)

    def test_bound_parent_survives_root_close_for_read_and_candidate_creation(self) -> None:
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        parent = file_system.bind_parent(
            root,
            PureWindowsPath("NestedCase", "sample.txt"),
        )
        root.close()
        self.assertEqual(parent.inspect_entry("sample.txt").byte_count, len(self.payload))
        self.assertIsNone(parent.inspect_entry("missing.txt"))
        candidate = parent.create_candidate("candidate.tmp", private=False)
        candidate.write_all(b"candidate")
        candidate.flush_content()
        self.assertEqual(candidate.identity().kind, "regular")
        candidate.close()
        parent.close()

    def test_candidate_streams_one_shot_chunks_without_whole_payload_write(self) -> None:
        file_system = WindowsRootedFileSystem()
        generated: list[int] = []
        chunks = (b"a" * 65536, b"b" * 17, b"c" * 4096)

        def stream() -> object:
            for chunk in chunks:
                generated.append(len(chunk))
                yield chunk

        with file_system.bind_root(self.root_path) as root:
            with file_system.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                candidate = parent.create_candidate("stream.tmp", private=False)
                facts = candidate.write_chunks(  # type: ignore[arg-type]
                    stream(),
                    maximum_bytes=sum(map(len, chunks)),
                )
                candidate.flush_content()
                candidate.close()
        payload = b"".join(chunks)
        self.assertEqual(generated, [65536, 17, 4096])
        self.assertEqual(facts.byte_count, len(payload))
        self.assertEqual((self.nested / "stream.tmp").read_bytes(), payload)

    def test_candidate_stream_completes_short_native_writes_and_publishes(self) -> None:
        payload = b"short-native-write" * 37
        adapter = WindowsPlatformAdapter()
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                candidate = parent.create_candidate("partial-write.tmp", private=False)
                real_write = candidate._api.WriteFile
                write_calls = 0

                def short_write(
                    handle: int,
                    buffer: object,
                    requested: int,
                    written: object,
                    overlapped: object,
                ) -> int:
                    nonlocal write_calls
                    write_calls += 1
                    return real_write(
                        handle,
                        buffer,
                        min(int(requested), 7),
                        written,
                        overlapped,
                    )

                with mock.patch.object(
                    candidate._api,
                    "WriteFile",
                    side_effect=short_write,
                ):
                    facts = candidate.write_chunks((payload,), maximum_bytes=len(payload))
                self.assertGreater(write_calls, 1)
                self.assertEqual(facts.byte_count, len(payload))
                candidate.flush_content()
                pending = parent.begin_publish(
                    candidate,
                    "partial-write.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
                try:
                    self.assertEqual(pending.retained_destination().read_all(), payload)
                    self.assertEqual(
                        pending.terminal_reproof(),
                        pending.preliminary_facts(),
                    )
                finally:
                    pending.close()

    def test_candidate_stream_fault_is_fail_closed_and_cannot_be_retried(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            with file_system.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                candidate = parent.create_candidate("faulted-stream.tmp", private=False)
                real_write = candidate._api.WriteFile
                calls = 0

                def fail_second(*args: object) -> int:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise platform_fs_windows.Win32CallError("WriteFile", 5)
                    return real_write(*args)

                with mock.patch.object(
                    candidate._api,
                    "WriteFile",
                    side_effect=fail_second,
                ), self.assertRaises(PlatformFileError) as caught:
                    candidate.write_chunks(
                        (b"a" * 65536, b"b"),
                        maximum_bytes=65537,
                    )
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.PUBLISH_FAILED,
                )
                with self.assertRaises(ValueError):
                    candidate.flush_content()
                with self.assertRaises(ValueError):
                    candidate.write_all(b"retry")
                candidate.close()

    def test_candidate_stream_preserves_contract_validation_errors(self) -> None:
        file_system = WindowsRootedFileSystem()
        cases = (
            ("empty", (b"",), 1, ValueError),
            ("oversize", (b"x" * 65537,), 65537, ValueError),
            ("maximum", (b"ab",), 1, ValueError),
            ("type", (bytearray(b"x"),), 1, TypeError),
        )
        with file_system.bind_root(self.root_path) as root:
            with file_system.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                for label, chunks, maximum_bytes, expected_error in cases:
                    with self.subTest(label=label):
                        candidate = parent.create_candidate(
                            f"invalid-{label}.tmp",
                            private=False,
                        )
                        with self.assertRaises(expected_error):
                            candidate.write_chunks(  # type: ignore[arg-type]
                                chunks,
                                maximum_bytes=maximum_bytes,
                            )
                        with self.assertRaises(ValueError):
                            candidate.flush_content()
                        with self.assertRaises(ValueError):
                            candidate.write_all(b"retry")
                        candidate.close()

    def test_locked_ledger_observation_is_bounded_and_rejects_unsafe_children(self) -> None:
        (self.nested / "alpha.json").write_bytes(b"alpha")
        (self.nested / "beta.json").write_bytes(b"beta")
        adapter = WindowsPlatformAdapter()
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                lease = adapter.acquire(
                    parent,
                    ".ledger.lock",
                    b"ledger-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                try:
                    observations = parent.observe_ledger_entries(
                        lease,
                        LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024),
                    )
                    names = tuple(item.name for item in observations)
                    self.assertEqual(names, tuple(sorted(names)))
                    self.assertIn("alpha.json", names)
                    self.assertIn("beta.json", names)
                    self.assertTrue(
                        all(item.snapshot.identity.link_count == 1 for item in observations)
                    )

                    with self.assertRaises(PlatformFileError) as limited:
                        parent.observe_ledger_entries(
                            lease,
                            LedgerEnumerationLimits(1, 255 * 4, 1024 * 1024),
                        )
                    _assert_platform_error(
                        self,
                        limited,
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    )

                    unsafe_dir = self.nested / "unsafe-dir"
                    unsafe_dir.mkdir()
                    try:
                        with self.assertRaises(PlatformFileError) as unsafe:
                            parent.observe_ledger_entries(
                                lease,
                                LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024),
                            )
                        _assert_platform_error(
                            self,
                            unsafe,
                            PlatformFileErrorCode.REPARSE_REJECTED,
                        )
                    finally:
                        unsafe_dir.rmdir()

                finally:
                    lease.close()

        alias = self.nested / "alpha-alias.json"
        os.link(self.nested / "alpha.json", alias)
        try:
            adapter = WindowsPlatformAdapter()
            with adapter.bind_root(self.root_path) as root:
                with adapter.bind_parent(
                    root,
                    PureWindowsPath("NestedCase", "placeholder.bin"),
                ) as parent:
                    lease = adapter.acquire(
                        parent,
                        ".ledger.lock",
                        b"ledger-v1",
                        LockPolicy(LockWait.FAIL_FAST),
                    )
                    try:
                        with self.assertRaises(PlatformFileError) as hardlink:
                            parent.observe_ledger_entries(
                                lease,
                                LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024),
                            )
                        _assert_platform_error(
                            self,
                            hardlink,
                            PlatformFileErrorCode.IDENTITY_STALE,
                        )
                    finally:
                        lease.close()
        finally:
            alias.unlink()

    def test_locked_ledger_observation_reproves_live_lease_at_terminal(self) -> None:
        (self.nested / "alpha.json").write_bytes(b"alpha")
        adapter = WindowsPlatformAdapter()
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                lease = adapter.acquire(
                    parent,
                    ".ledger-terminal.lock",
                    b"ledger-terminal-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                real_query = parent._api.GetFileInformationByHandleEx
                closed = False

                def close_during_first_directory_query(*args: object) -> int:
                    nonlocal closed
                    if (
                        not closed
                        and args[1]
                        == platform_fs_windows.FILE_ID_EXTD_DIR_INFO_CLASS
                    ):
                        lease.close()
                        closed = True
                    return real_query(*args)

                with mock.patch.object(
                    parent._api,
                    "GetFileInformationByHandleEx",
                    side_effect=close_during_first_directory_query,
                ), self.assertRaises(PlatformFileError) as caught:
                    parent.observe_ledger_entries(
                        lease,
                        LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024),
                    )
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                )
                self.assertTrue(closed)

    def test_locked_ledger_observation_rejects_first_second_scan_drift(self) -> None:
        alpha = self.nested / "alpha.json"
        alpha.write_bytes(b"alpha")
        adapter = WindowsPlatformAdapter()
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(
                root,
                PureWindowsPath("NestedCase", "placeholder.bin"),
            ) as parent:
                lease = adapter.acquire(
                    parent,
                    ".ledger-drift.lock",
                    b"ledger-drift-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                try:
                    real_query = parent._api.GetFileInformationByHandleEx
                    directory_queries = 0

                    def drift_before_second_scan(*args: object) -> int:
                        nonlocal directory_queries
                        if args[1] == platform_fs_windows.FILE_ID_EXTD_DIR_INFO_CLASS:
                            directory_queries += 1
                            if directory_queries == 3:
                                alpha.write_bytes(b"alpha-drifted")
                        return real_query(*args)

                    with mock.patch.object(
                        parent._api,
                        "GetFileInformationByHandleEx",
                        side_effect=drift_before_second_scan,
                    ), self.assertRaises(PlatformFileError) as caught:
                        parent.observe_ledger_entries(
                            lease,
                            LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024),
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.IDENTITY_STALE,
                    )
                    self.assertGreaterEqual(directory_queries, 3)
                finally:
                    lease.close()

    def test_hostile_component_is_rejected_before_any_native_open(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root, mock.patch.object(
            root._api,
            "open_handle",
            wraps=root._api.open_handle,
        ) as native_open:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(root, PureWindowsPath("COM1.txt"))
            with self.assertRaises(PlatformFileError) as overlong:
                file_system.open_regular(root, PureWindowsPath("x" * 256))
        _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)
        _assert_platform_error(self, overlong, PlatformFileErrorCode.OUTSIDE_ROOT)
        native_open.assert_not_called()

    def test_hardlink_is_allowed_and_link_count_is_recorded(self) -> None:
        hardlink = self.nested / "hardlink.txt"
        os.link(self.source, hardlink)
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            with file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            ) as source:
                self.assertEqual(source.read_all(), self.payload)
                self.assertGreaterEqual(source.identity().link_count, 2)

    def test_bounded_read_uses_exact_offsets_and_only_shortens_at_eof(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            with file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            ) as source:
                expected = source.snapshot()
                self.assertEqual(
                    source.read_at(0, 64 * 1024, expected),
                    self.payload[: 64 * 1024],
                )
                self.assertEqual(
                    source.read_at(64 * 1024, 64 * 1024, expected),
                    self.payload[64 * 1024 :],
                )
                self.assertEqual(source.read_at(len(self.payload), 1, expected), b"")
                with self.assertRaises(ValueError):
                    source.read_at(len(self.payload) + 10, 1, expected)

    def test_bounded_read_rejects_wrong_baseline_and_accumulates_partial_reads(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        expected = source.snapshot()
        wrong = type(expected)(
            identity=expected.identity,
            byte_count=expected.byte_count + 1,
            modified_token=expected.modified_token,
            reparse_free=expected.reparse_free,
        )
        real_read = source._api.ReadFile

        def partial_read(
            handle: int,
            buffer: object,
            requested: int,
            read: object,
            overlapped: object,
        ) -> int:
            return real_read(handle, buffer, min(int(requested), 97), read, overlapped)

        partial_calls = 0

        def zero_mid_chunk(
            handle: int,
            buffer: object,
            requested: int,
            read: object,
            overlapped: object,
        ) -> int:
            nonlocal partial_calls
            partial_calls += 1
            if partial_calls == 1:
                return real_read(handle, buffer, min(int(requested), 97), read, overlapped)
            read._obj.value = 0
            return 1

        try:
            with mock.patch.object(
                source._api,
                "ReadFile",
                wraps=source._api.ReadFile,
            ) as read_file, self.assertRaises(PlatformFileError) as stale:
                source.read_at(0, 1024, wrong)
            _assert_platform_error(self, stale, PlatformFileErrorCode.IDENTITY_STALE)
            read_file.assert_not_called()

            with mock.patch.object(
                source._api,
                "ReadFile",
                side_effect=partial_read,
            ):
                self.assertEqual(source.read_at(0, 1024, expected), self.payload[:1024])
            with mock.patch.object(
                source._api,
                "ReadFile",
                side_effect=zero_mid_chunk,
            ), self.assertRaises(PlatformFileError) as zero:
                source.read_at(0, 1024, expected)
            _assert_platform_error(self, zero, PlatformFileErrorCode.IDENTITY_STALE)
        finally:
            source.close()

    def test_bounded_read_serializes_seek_and_read_across_offsets(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        expected = source.snapshot()
        first_at_seek = threading.Event()
        release_first = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        seek_calls = 0
        results: dict[int, bytes] = {}
        failures: list[BaseException] = []
        real_seek = source._api.SetFilePointerEx
        real_read = source._api.ReadFile

        class GateLock:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self._guard = threading.Lock()
                self._acquire_calls = 0
                self.first_acquired = threading.Event()
                self.second_acquire_entered = threading.Event()

            def acquire(self, *args: object, **kwargs: object) -> bool:
                with self._guard:
                    self._acquire_calls += 1
                    current = self._acquire_calls
                if current == 2:
                    self.second_acquire_entered.set()
                acquired = self._lock.acquire(*args, **kwargs)
                if current == 1 and acquired:
                    self.first_acquired.set()
                return acquired

            def release(self) -> None:
                self._lock.release()

            def locked(self) -> bool:
                return self._lock.locked()

            def __enter__(self) -> GateLock:
                self.acquire()
                return self

            def __exit__(
                self,
                _exc_type: object,
                _exc: object,
                _traceback: object,
            ) -> None:
                self.release()

        gate = GateLock()
        source._read_lock = gate

        def interleaved_seek(*args: object) -> int:
            nonlocal active, maximum_active, seek_calls
            result = real_seek(*args)
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                seek_calls += 1
                current = seek_calls
            if current == 1:
                first_at_seek.set()
                if not release_first.wait(5.0):
                    raise AssertionError("second offset read did not attempt to interleave")
            return result

        def tracked_read(*args: object) -> int:
            nonlocal active
            try:
                return real_read(*args)
            finally:
                with state_lock:
                    active -= 1

        def read_offset(offset: int) -> None:
            try:
                results[offset] = source.read_at(offset, 257, expected)
            except BaseException as error:
                failures.append(error)

        first = threading.Thread(target=read_offset, args=(0,))
        second = threading.Thread(target=read_offset, args=(1000,))
        try:
            with mock.patch.object(
                source._api,
                "SetFilePointerEx",
                side_effect=interleaved_seek,
            ), mock.patch.object(
                source._api,
                "ReadFile",
                side_effect=tracked_read,
            ):
                first.start()
                self.assertTrue(first_at_seek.wait(5.0))
                self.assertTrue(gate.first_acquired.is_set())
                second.start()
                self.assertTrue(gate.second_acquire_entered.wait(5.0))
                self.assertTrue(gate.locked())
                with state_lock:
                    self.assertEqual(seek_calls, 1)
                    self.assertEqual(active, 1)
                release_first.set()
                first.join(5.0)
                second.join(5.0)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(maximum_active, 1)
            self.assertEqual(results[0], self.payload[:257])
            self.assertEqual(results[1000], self.payload[1000:1257])
        finally:
            release_first.set()
            first.join(5.0)
            second.join(5.0)
            source.close()

    def test_source_and_ancestor_handles_block_replacement(self) -> None:
        replacement = self.nested / "replacement.txt"
        replacement.write_bytes(b"replacement")
        file_system = WindowsRootedFileSystem()
        root = file_system.bind_root(self.root_path)
        source = file_system.open_regular(
            root,
            PureWindowsPath("NestedCase", "sample.txt"),
        )
        with self.assertRaises(PermissionError):
            os.replace(replacement, self.source)
        with self.assertRaises(PermissionError):
            os.rename(self.nested, self.root_path / "MovedCase")
        source.close()
        root.close()

    def test_retained_parent_blocks_probe_to_source_swap(self) -> None:
        replacement = self.nested / "replacement.txt"
        replacement.write_bytes(b"replacement")
        phases: list[str] = []
        swap_errors: list[OSError] = []

        def swap(phase: str) -> None:
            phases.append(phase)
            if phase == "windows_after_entry_probe":
                try:
                    os.replace(replacement, self.source)
                except OSError as error:
                    swap_errors.append(error)

        file_system = WindowsRootedFileSystem(_fault_injector=swap)
        with file_system.bind_root(self.root_path) as root:
            with file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            ) as opened:
                self.assertEqual(opened.read_all(), self.payload)
        self.assertEqual(len(swap_errors), 1)
        self.assertIsInstance(swap_errors[0], PermissionError)
        self.assertEqual(phases[0], "windows_after_entry_probe")
        self.assertIn("windows_before_body_read", phases)
        self.assertIn("windows_after_body_read", phases)

    def test_probe_to_retained_in_place_rewrite_is_identity_stale(self) -> None:
        rewritten = b"in-place-rewrite"
        released = b"authority-released"
        phases: list[str] = []

        def rewrite(phase: str) -> None:
            phases.append(phase)
            if phase == "windows_after_entry_probe":
                self.source.write_bytes(rewritten)

        file_system = WindowsRootedFileSystem(_fault_injector=rewrite)
        with file_system.bind_root(self.root_path) as root, mock.patch.object(
            root._api,
            "ReadFile",
            wraps=root._api.ReadFile,
        ) as read_file:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "sample.txt"),
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
            self.assertEqual(self.source.read_bytes(), rewritten)
            read_file.assert_not_called()
            self.source.write_bytes(released)
            self.assertEqual(self.source.read_bytes(), released)
        self.assertEqual(phases, ["windows_after_entry_probe"])

    def test_baseexception_during_ownership_transfer_closes_every_handle(self) -> None:
        api = platform_fs_windows.WindowsFileAPI.load()
        real_open = api.open_handle
        real_duplicate = api.duplicate_handle
        real_close = api.CloseHandle
        opened = 0
        closed = 0

        def recording_open(path: str, **kwargs: object) -> object:
            nonlocal opened
            handle = real_open(path, **kwargs)
            opened += 1
            return handle

        def recording_duplicate(handle: object) -> object:
            nonlocal opened
            duplicate = real_duplicate(handle)
            opened += 1
            return duplicate

        def recording_close(raw: int) -> int:
            nonlocal closed
            closed += 1
            return real_close(raw)

        class StopTransfer(BaseException):
            pass

        def stop(phase: str) -> None:
            if phase == "windows_after_entry_probe":
                raise StopTransfer()

        file_system = WindowsRootedFileSystem(_api=api, _fault_injector=stop)
        with mock.patch.object(api, "open_handle", side_effect=recording_open), mock.patch.object(
            api,
            "duplicate_handle",
            side_effect=recording_duplicate,
        ), mock.patch.object(api, "CloseHandle", side_effect=recording_close):
            root = file_system.bind_root(self.root_path)
            try:
                with self.assertRaises(StopTransfer):
                    file_system.open_regular(
                        root,
                        PureWindowsPath("NestedCase", "sample.txt"),
                    )
            finally:
                root.close()
        self.assertEqual(closed, opened)

    def test_alternate_case_is_rejected_by_exact_final_path_policy(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(
                    root,
                    PureWindowsPath("nestedcase", "sample.txt"),
                )
        _assert_platform_error(self, caught, PlatformFileErrorCode.OUTSIDE_ROOT)

    def test_missing_and_directory_source_are_body_free_failures(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            with mock.patch.object(
                root._api,
                "ReadFile",
                wraps=root._api.ReadFile,
            ) as read_file:
                with self.assertRaises(PlatformFileError) as missing:
                    file_system.open_regular(
                        root,
                        PureWindowsPath("NestedCase", "missing.txt"),
                    )
                with self.assertRaises(PlatformFileError) as directory:
                    file_system.open_regular(root, PureWindowsPath("NestedCase"))
            read_file.assert_not_called()
        _assert_platform_error(
            self,
            missing,
            PlatformFileErrorCode.ENTRY_UNAVAILABLE,
        )
        _assert_platform_error(
            self,
            directory,
            PlatformFileErrorCode.REPARSE_REJECTED,
        )

    def test_initial_entry_access_denied_is_not_root_capability_failure(self) -> None:
        denied = self.nested / "denied.txt"
        denied.write_bytes(b"denied")
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            real_open = root._api.open_handle

            def deny_entry(path: str, **kwargs: object) -> object:
                if path.endswith("\\denied.txt"):
                    raise platform_fs_windows.Win32CallError("CreateFileW", 5)
                return real_open(path, **kwargs)

            with mock.patch.object(
                root._api,
                "open_handle",
                side_effect=deny_entry,
            ), self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "denied.txt"),
                )
        _assert_platform_error(self, caught, PlatformFileErrorCode.ENTRY_UNAVAILABLE)

    def test_entry_disappearing_after_probe_is_identity_stale(self) -> None:
        removed = False

        def remove_after_probe(phase: str) -> None:
            nonlocal removed
            if phase == "windows_after_entry_probe":
                self.source.unlink()
                removed = True

        file_system = WindowsRootedFileSystem(_fault_injector=remove_after_probe)
        with file_system.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "sample.txt"),
                )
        self.assertTrue(removed)
        _assert_platform_error(self, caught, PlatformFileErrorCode.IDENTITY_STALE)

    def test_entry_becoming_directory_after_probe_is_identity_stale(self) -> None:
        swapped = False

        def replace_with_directory(phase: str) -> None:
            nonlocal swapped
            if phase == "windows_after_entry_probe":
                self.source.unlink()
                self.source.mkdir()
                swapped = True

        file_system = WindowsRootedFileSystem(
            _fault_injector=replace_with_directory,
        )
        with file_system.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "sample.txt"),
                )
        self.assertTrue(swapped)
        _assert_platform_error(self, caught, PlatformFileErrorCode.IDENTITY_STALE)

    def test_junction_component_is_rejected_without_reading_target(self) -> None:
        outside = self.container / "Outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"must-not-read")
        junction = self.root_path / "Junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        try:
            file_system = WindowsRootedFileSystem()
            with file_system.bind_root(self.root_path) as root:
                with self.assertRaises(PlatformFileError) as caught:
                    file_system.open_regular(
                        root,
                        PureWindowsPath("Junction", "secret.txt"),
                    )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.REPARSE_REJECTED,
            )
        finally:
            os.rmdir(junction)

    def test_real_symlink_tag_is_rejected_before_body_read(self) -> None:
        outside = self.container / "outside-secret.txt"
        outside.write_bytes(b"must-not-read")
        link = self.nested / "source-link.txt"
        try:
            os.symlink(outside, link)
        except OSError as error:
            if error.winerror != 1314:
                raise
            link = Path(r"C:\Users\All Users")
            rooted_path = Path(r"C:\Users")
            relative = PureWindowsPath("All Users", "never-read.fixture")
        else:
            rooted_path = self.root_path
            relative = PureWindowsPath("NestedCase", "source-link.txt")

        api = platform_fs_windows.WindowsFileAPI.load()
        with api.open_handle(
            str(link),
            desired_access=(
                platform_fs_windows.FILE_READ_ATTRIBUTES
                | platform_fs_windows.SYNCHRONIZE
            ),
            share_mode=(
                platform_fs_windows.FILE_SHARE_READ
                | platform_fs_windows.FILE_SHARE_WRITE
                | platform_fs_windows.FILE_SHARE_DELETE
            ),
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=(
                platform_fs_windows.FILE_FLAG_BACKUP_SEMANTICS
                | platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT
            ),
        ) as link_handle:
            tag = platform_fs_windows.FILE_ATTRIBUTE_TAG_INFO()
            with link_handle.borrow() as raw_link:
                platform_fs_windows._query_file_info(
                    api,
                    raw_link,
                    platform_fs_windows.FILE_ATTRIBUTE_TAG_INFO_CLASS,
                    tag,
                )
        self.assertTrue(
            int(tag.FileAttributes)
            & platform_fs_windows.FILE_ATTRIBUTE_REPARSE_POINT
        )
        self.assertEqual(int(tag.ReparseTag), 0xA000000C)

        file_system = WindowsRootedFileSystem(_api=api)
        with mock.patch.object(
            api,
            "ReadFile",
            wraps=api.ReadFile,
        ) as read_file, file_system.bind_root(rooted_path) as root:
            with self.assertRaises(PlatformFileError) as caught:
                file_system.open_regular(root, relative)
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.REPARSE_REJECTED,
        )
        read_file.assert_not_called()

    def test_partial_read_loop_reaches_zero_eof_and_repeated_read_reseeks(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        real_read = source._api.ReadFile
        counts: list[int] = []

        def partial_read(
            handle: int,
            buffer: object,
            requested: int,
            read: object,
            overlapped: object,
        ) -> int:
            limited = min(int(requested), 97)
            result = real_read(handle, buffer, limited, read, overlapped)
            counts.append(read._obj.value)
            return result

        try:
            with mock.patch.object(source._api, "ReadFile", side_effect=partial_read):
                self.assertEqual(source.read_all(), self.payload)
                first_counts = tuple(counts)
                counts.clear()
                self.assertEqual(source.read_all(), self.payload)
            self.assertGreater(len(first_counts), 2)
            self.assertEqual(first_counts[-1], 0)
            self.assertEqual(counts[-1], 0)
        finally:
            source.close()

    def test_all_proofs_complete_before_first_body_read(self) -> None:
        events: list[str] = []

        def record_phase(phase: str) -> None:
            events.append(phase)

        file_system = WindowsRootedFileSystem(_fault_injector=record_phase)
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        real_read = source._api.ReadFile

        def recording_read(*args: object) -> int:
            events.append("ReadFile")
            return real_read(*args)

        try:
            with mock.patch.object(source._api, "ReadFile", side_effect=recording_read):
                self.assertEqual(source.read_all(), self.payload)
            before_index = events.index("windows_before_body_read")
            first_read_index = events.index("ReadFile")
            after_index = events.index("windows_after_body_read")
            self.assertLess(before_index, first_read_index)
            self.assertLess(first_read_index, after_index)
        finally:
            source.close()

    def test_proof_failure_prevents_first_body_read(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        try:
            with mock.patch.object(
                platform_fs_windows,
                "_capture_handle_proof",
                side_effect=platform_fs_windows._identity_stale(),
            ), mock.patch.object(
                source._api,
                "ReadFile",
                wraps=source._api.ReadFile,
            ) as read_file, self.assertRaises(PlatformFileError) as caught:
                source.read_all()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
            read_file.assert_not_called()
        finally:
            source.close()

    def test_snapshot_drift_after_body_is_not_returned(self) -> None:
        drifted = False

        def mark_after_body(phase: str) -> None:
            nonlocal drifted
            if phase == "windows_after_body_read":
                drifted = True

        file_system = WindowsRootedFileSystem(_fault_injector=mark_after_body)
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        real_capture = platform_fs_windows._capture_handle_proof

        def drifting_capture(*args: object, **kwargs: object) -> object:
            proof = real_capture(*args, **kwargs)
            if drifted and kwargs.get("expected_kind") == "regular":
                changed = platform_fs_windows.EntrySnapshot(
                    identity=proof.identity,
                    byte_count=proof.snapshot.byte_count + 1,
                    modified_token=proof.snapshot.modified_token,
                    reparse_free=True,
                )
                return platform_fs_windows._WindowsHandleProof(
                    proof.identity,
                    changed,
                    proof.final_path,
                )
            return proof

        try:
            with mock.patch.object(
                platform_fs_windows,
                "_capture_handle_proof",
                side_effect=drifting_capture,
            ), self.assertRaises(PlatformFileError) as caught:
                source.read_all()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
        finally:
            source.close()

    def test_readfile_failure_is_body_free_identity_stale(self) -> None:
        file_system = WindowsRootedFileSystem()
        with file_system.bind_root(self.root_path) as root:
            source = file_system.open_regular(
                root,
                PureWindowsPath("NestedCase", "sample.txt"),
            )
        try:
            with mock.patch.object(
                source._api,
                "ReadFile",
                side_effect=platform_fs_windows.Win32CallError("ReadFile", 5),
            ), self.assertRaises(PlatformFileError) as caught:
                source.read_all()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
        finally:
            source.close()

    def test_read_loop_rejects_early_eof_oversized_count_and_extra_byte(self) -> None:
        def run_case(mode: str) -> PlatformFileError:
            file_system = WindowsRootedFileSystem()
            with file_system.bind_root(self.root_path) as root:
                source = file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "sample.txt"),
                )
            real_read = source._api.ReadFile

            def adversarial_read(
                handle: int,
                buffer: object,
                requested: int,
                read: object,
                overlapped: object,
            ) -> int:
                if mode == "early_eof":
                    read._obj.value = 0
                    return 1
                if mode == "oversized":
                    read._obj.value = int(requested) + 1
                    return 1
                if int(requested) == 1:
                    read._obj.value = 1
                    return 1
                return real_read(handle, buffer, requested, read, overlapped)

            try:
                with mock.patch.object(
                    source._api,
                    "ReadFile",
                    side_effect=adversarial_read,
                ), self.assertRaises(PlatformFileError) as caught:
                    source.read_all()
                return caught.exception
            finally:
                source.close()

        for mode in ("early_eof", "oversized", "extra_byte"):
            with self.subTest(mode=mode):
                error = run_case(mode)
                self.assertEqual(error.code, PlatformFileErrorCode.IDENTITY_STALE.value)
                self.assertEqual(error.args, (PlatformFileErrorCode.IDENTITY_STALE.value,))
                self.assertIsNone(error.__cause__)
