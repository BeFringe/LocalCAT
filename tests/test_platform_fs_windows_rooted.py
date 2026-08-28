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
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    ProcessFileLock,
    RootedFileSystem,
)
from platform_fs_windows import WindowsRootedFileSystem
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
            with self.assertRaises(PlatformFileError) as missing:
                file_system.open_regular(
                    root,
                    PureWindowsPath("NestedCase", "missing.txt"),
                )
            with self.assertRaises(PlatformFileError) as directory:
                file_system.open_regular(root, PureWindowsPath("NestedCase"))
        _assert_platform_error(
            self,
            missing,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )
        _assert_platform_error(
            self,
            directory,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )

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
