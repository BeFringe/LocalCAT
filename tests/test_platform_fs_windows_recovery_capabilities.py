"""Windows recovery-only platform capability tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest import mock

import platform_fs_windows
from platform_fs import (
    narrow_windows_existing_file_retirement,
    narrow_windows_locked_descendant_namespace_inspection,
)
from platform_fs_contracts import (
    CandidateContentFacts,
    ExistingFileRetirement,
    LedgerEnumerationLimits,
    LockPolicy,
    LockWait,
    LockedDescendantNamespaceInspection,
    PlatformFileError,
    PlatformFileErrorCode,
)
from platform_fs_windows import WindowsPlatformAdapter, WindowsRootedFileSystem
from windows_file_api import Win32CallError


ROOT = Path(__file__).resolve().parents[1]
LIMITS = LedgerEnumerationLimits(16, 255 * 4, 1024 * 1024)


def _assert_platform_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode,
) -> None:
    case.assertEqual(caught.exception.code, code.value)
    case.assertEqual(caught.exception.args, (code.value,))
    case.assertIsNone(caught.exception.__cause__)


@unittest.skipUnless(sys.platform == "win32", "real capability tests require Windows")
class WindowsLockedDescendantInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-descendant-",
            dir=ROOT.parent,
        )
        self.container = Path(self._temporary.name)
        self.root_path = self.container / "RootCase"
        self.descendant_path = self.root_path / "NestedCase"
        self.descendant_path.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _open(
        self,
        adapter: WindowsPlatformAdapter,
        *,
        lock_name: str = ".ancestor.lock",
    ) -> tuple[object, object, object]:
        root = adapter.bind_root(self.root_path)
        descendant = adapter.bind_parent(
            root,
            PureWindowsPath("NestedCase", "placeholder.bin"),
        )
        lease = adapter.acquire(
            root,
            lock_name,
            b"ancestor-v1",
            LockPolicy(LockWait.FAIL_FAST),
        )
        return root, descendant, lease

    def test_narrow_returns_same_exact_backend_and_observes_unknown_names(self) -> None:
        (self.descendant_path / "known.json").write_bytes(b"known")
        (self.descendant_path / "foreign.bin").write_bytes(b"foreign")
        adapter = WindowsPlatformAdapter()
        narrowed = narrow_windows_locked_descendant_namespace_inspection(
            adapter,
            self.root_path,
        )
        self.assertIs(narrowed, adapter)
        self.assertIsInstance(narrowed, LockedDescendantNamespaceInspection)
        root, descendant, lease = self._open(adapter)
        try:
            observations = narrowed.observe_descendant_entries(
                root,
                lease,
                descendant,
                LIMITS,
            )
            self.assertEqual(
                tuple(item.name for item in observations),
                ("foreign.bin", "known.json"),
            )
        finally:
            lease.close()
            descendant.close()
            root.close()

    def test_limit_and_unsafe_descendant_entry_fail_closed(self) -> None:
        (self.descendant_path / "alpha.bin").write_bytes(b"a")
        (self.descendant_path / "beta.bin").write_bytes(b"b")
        adapter = WindowsPlatformAdapter()
        root, descendant, lease = self._open(adapter)
        try:
            with self.assertRaises(PlatformFileError) as limited:
                adapter.observe_descendant_entries(
                    root,
                    lease,
                    descendant,
                    LedgerEnumerationLimits(1, 255 * 4, 1024 * 1024),
                )
            _assert_platform_error(
                self,
                limited,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )
            unsafe = self.descendant_path / "foreign-directory"
            unsafe.mkdir()
            try:
                with self.assertRaises(PlatformFileError) as rejected:
                    adapter.observe_descendant_entries(
                        root,
                        lease,
                        descendant,
                        LIMITS,
                    )
                _assert_platform_error(
                    self,
                    rejected,
                    PlatformFileErrorCode.REPARSE_REJECTED,
                )
            finally:
                unsafe.rmdir()
        finally:
            lease.close()
            descendant.close()
            root.close()

    def test_root_lease_issuer_and_strict_descendant_must_match(self) -> None:
        adapter = WindowsPlatformAdapter()
        root, descendant, lease = self._open(adapter)
        other = WindowsPlatformAdapter(_api=adapter._native_api())
        other_root, other_descendant, other_lease = self._open(
            other,
            lock_name=".other-ancestor.lock",
        )
        descendant_lease = adapter.acquire(
            descendant,
            ".wrong-parent.lock",
            b"wrong-parent-v1",
            LockPolicy(LockWait.FAIL_FAST),
        )
        try:
            cases = (
                (root, other_lease, descendant),
                (root, descendant_lease, descendant),
                (root, lease, other_descendant),
                (root, lease, root),
            )
            for candidate_root, candidate_lease, candidate_descendant in cases:
                with self.subTest(
                    lease=id(candidate_lease),
                    descendant=id(candidate_descendant),
                ), self.assertRaises(PlatformFileError) as caught:
                    adapter.observe_descendant_entries(
                        candidate_root,
                        candidate_lease,
                        candidate_descendant,
                        LIMITS,
                    )
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.LOCK_UNAVAILABLE,
                )
        finally:
            descendant_lease.close()
            other_lease.close()
            other_descendant.close()
            other_root.close()
            lease.close()
            descendant.close()
            root.close()

    def test_terminal_lease_reproof_and_programmer_errors_are_not_weakened(self) -> None:
        (self.descendant_path / "alpha.bin").write_bytes(b"alpha")
        adapter = WindowsPlatformAdapter()
        root, descendant, lease = self._open(adapter)
        real_query = descendant._api.GetFileInformationByHandleEx
        closed = False

        def close_during_query(*args: object) -> int:
            nonlocal closed
            if (
                not closed
                and args[1] == platform_fs_windows.FILE_ID_EXTD_DIR_INFO_CLASS
            ):
                lease.close()
                closed = True
            return real_query(*args)

        try:
            with mock.patch.object(
                descendant._api,
                "GetFileInformationByHandleEx",
                side_effect=close_during_query,
            ), self.assertRaises(PlatformFileError) as caught:
                adapter.observe_descendant_entries(root, lease, descendant, LIMITS)
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
            )
            self.assertTrue(closed)
        finally:
            descendant.close()
            root.close()

        for programming_error in (
            TypeError("type"),
            AssertionError("assert"),
            AttributeError("attribute"),
        ):
            with self.subTest(error=type(programming_error).__name__):
                adapter = WindowsPlatformAdapter()
                root, descendant, lease = self._open(adapter)
                try:
                    with mock.patch.object(
                        platform_fs_windows._WindowsBoundDirectory,
                        "_observe_entries_under_lease_records",
                        side_effect=programming_error,
                    ), self.assertRaises(type(programming_error)):
                        adapter.observe_descendant_entries(
                            root,
                            lease,
                            descendant,
                            LIMITS,
                        )
                finally:
                    lease.close()
                    descendant.close()
                    root.close()

    def test_terminal_root_reproof_rejects_post_enumeration_drift(self) -> None:
        (self.descendant_path / "alpha.bin").write_bytes(b"alpha")
        adapter = WindowsPlatformAdapter()
        root, descendant, lease = self._open(adapter)
        real_reprove = root._reprove
        calls = 0

        def drift_on_terminal_reproof() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PlatformFileError(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            real_reprove()

        try:
            with mock.patch.object(
                platform_fs_windows._WindowsRootedDirectory,
                "_reprove",
                side_effect=drift_on_terminal_reproof,
            ), self.assertRaises(PlatformFileError) as caught:
                adapter.observe_descendant_entries(root, lease, descendant, LIMITS)
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.IDENTITY_STALE,
            )
            self.assertEqual(calls, 2)
        finally:
            lease.close()
            descendant.close()
            root.close()


@unittest.skipUnless(sys.platform == "win32", "real capability tests require Windows")
class WindowsExistingFileRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-existing-retirement-",
            dir=ROOT.parent,
        )
        self.container = Path(self._temporary.name)
        self.root_path = self.container / "RootCase"
        self.parent_path = self.root_path / "PrivateCase"
        self.parent_path.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _facts(payload: bytes) -> CandidateContentFacts:
        return CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())

    def _authorities(
        self,
        adapter: WindowsPlatformAdapter,
        *,
        source_name: str,
        target_directory: str = "quarantine",
    ) -> tuple[object, object, object]:
        root = adapter.bind_root(self.root_path)
        parent = adapter.bind_parent(
            root,
            PureWindowsPath("PrivateCase", source_name),
        )
        target = adapter.bind_or_create_child_directory(parent, target_directory)
        return root, parent, target

    def _assert_write_and_delete_access_blocked(
        self,
        adapter: WindowsPlatformAdapter,
        path: Path,
    ) -> None:
        for desired_access in (
            platform_fs_windows.GENERIC_WRITE,
            platform_fs_windows.DELETE,
        ):
            opened = None
            try:
                with self.subTest(desired_access=desired_access), self.assertRaises(
                    Win32CallError
                ):
                    opened = adapter._native_api().open_handle(
                        str(path),
                        desired_access=(
                            desired_access
                            | platform_fs_windows.FILE_READ_ATTRIBUTES
                            | platform_fs_windows.SYNCHRONIZE
                        ),
                        share_mode=(
                            platform_fs_windows.FILE_SHARE_READ
                            | platform_fs_windows.FILE_SHARE_WRITE
                            | platform_fs_windows.FILE_SHARE_DELETE
                        ),
                        creation_disposition=platform_fs_windows.OPEN_EXISTING,
                        flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
                    )
            finally:
                if opened is not None:
                    opened.close()

    def test_live_source_moves_no_clobber_and_transfers_close_ownership(self) -> None:
        payload = b"portable-prepared-v3"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        capability = narrow_windows_existing_file_retirement(adapter, self.root_path)
        self.assertIs(capability, adapter)
        self.assertIsInstance(capability, ExistingFileRetirement)
        root, parent, target = self._authorities(adapter, source_name=source_name)
        source = capability.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(payload),
        )
        try:
            self._assert_write_and_delete_access_blocked(adapter, source_path)
            retained = capability.retire_existing_exclusive(
                parent,
                source_name,
                source,
                target,
                source_name,
            )
            try:
                self.assertTrue(source.closed)
                self.assertFalse(source_path.exists())
                self.assertEqual(retained.reprove().byte_count, len(payload))
                self._assert_write_and_delete_access_blocked(adapter, target_path)
            finally:
                retained.close()
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            if not source.closed:
                source.close()
            target.close()
            parent.close()
            root.close()

    def test_fresh_backend_rebinds_only_source_absent_exact_target(self) -> None:
        payload = b"fresh-rebind-v3"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        first = WindowsPlatformAdapter()
        root, parent, target = self._authorities(first, source_name=source_name)
        source = first.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(payload),
        )
        retained = first.retire_existing_exclusive(
            parent,
            source_name,
            source,
            target,
            source_name,
        )
        retained.close()
        target.close()
        parent.close()
        root.close()

        fresh = WindowsPlatformAdapter()
        fresh_root, fresh_parent, fresh_target = self._authorities(
            fresh,
            source_name=source_name,
        )
        try:
            rebound = fresh.rebind_existing_retirement(
                fresh_parent,
                source_name,
                fresh_target,
                source_name,
                self._facts(payload),
            )
            try:
                self.assertFalse(source_path.exists())
                self.assertEqual(rebound.reprove().byte_count, len(payload))
                self._assert_write_and_delete_access_blocked(fresh, target_path)
            finally:
                rebound.close()
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            fresh_target.close()
            fresh_parent.close()
            fresh_root.close()

    def test_source_and_target_coexist_never_clobbers_or_consumes_source(self) -> None:
        source_payload = b"source"
        target_payload = b"foreign-target"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(source_payload)
        target_path.parent.mkdir()
        target_path.write_bytes(target_payload)
        adapter = WindowsPlatformAdapter()
        root, parent, target = self._authorities(adapter, source_name=source_name)
        source = adapter.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(source_payload),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
                    source_name,
                    source,
                    target,
                    source_name,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertFalse(source.closed)
            self.assertEqual(
                source.reprove().content_sha256,
                hashlib.sha256(source_payload).digest(),
            )
            self.assertEqual(target_path.read_bytes(), target_payload)
        finally:
            source.close()
            target.close()
            parent.close()
            root.close()

    def test_content_drift_and_unsafe_target_shape_fail_closed(self) -> None:
        payload = b"expected"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        source_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        with adapter.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as wrong_source:
                adapter.open_existing_retirement_source(
                    root,
                    PureWindowsPath("PrivateCase", source_name),
                    self._facts(b"different"),
                )
            _assert_platform_error(
                self,
                wrong_source,
                PlatformFileErrorCode.IDENTITY_STALE,
            )

        source_path.unlink()
        target_path = self.parent_path / "quarantine" / source_name
        target_path.parent.mkdir()
        target_path.write_bytes(b"foreign")
        root, parent, target = self._authorities(adapter, source_name=source_name)
        try:
            with self.assertRaises(PlatformFileError) as wrong_target:
                adapter.rebind_existing_retirement(
                    parent,
                    source_name,
                    target,
                    source_name,
                    self._facts(payload),
                )
            _assert_platform_error(
                self,
                wrong_target,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertEqual(target_path.read_bytes(), b"foreign")
        finally:
            target.close()
            parent.close()
            root.close()

    def test_fresh_rebind_rejects_coexist_absent_and_hardlinked_target(self) -> None:
        payload = b"fresh-state-matrix"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        target_path.parent.mkdir()
        adapter = WindowsPlatformAdapter()

        source_path.write_bytes(payload)
        target_path.write_bytes(payload)
        root, parent, target = self._authorities(adapter, source_name=source_name)
        try:
            with self.assertRaises(PlatformFileError) as coexist:
                adapter.rebind_existing_retirement(
                    parent,
                    source_name,
                    target,
                    source_name,
                    self._facts(payload),
                )
            _assert_platform_error(
                self,
                coexist,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertEqual(source_path.read_bytes(), payload)
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            target.close()
            parent.close()
            root.close()

        source_path.unlink()
        target_path.unlink()
        root, parent, target = self._authorities(adapter, source_name=source_name)
        try:
            with self.assertRaises(PlatformFileError) as absent:
                adapter.rebind_existing_retirement(
                    parent,
                    source_name,
                    target,
                    source_name,
                    self._facts(payload),
                )
            _assert_platform_error(
                self,
                absent,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertFalse(source_path.exists())
            self.assertFalse(target_path.exists())
        finally:
            target.close()
            parent.close()
            root.close()

        target_path.write_bytes(payload)
        alias_path = self.parent_path / "quarantine" / "activation-alias.journal"
        os.link(target_path, alias_path)
        try:
            root, parent, target = self._authorities(adapter, source_name=source_name)
            try:
                with self.assertRaises(PlatformFileError) as hardlink:
                    adapter.rebind_existing_retirement(
                        parent,
                        source_name,
                        target,
                        source_name,
                        self._facts(payload),
                    )
                _assert_platform_error(
                    self,
                    hardlink,
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                )
                self.assertEqual(target_path.read_bytes(), payload)
                self.assertEqual(alias_path.read_bytes(), payload)
            finally:
                target.close()
                parent.close()
                root.close()
        finally:
            alias_path.unlink()

        target_path.unlink()
        target_path.mkdir()
        root, parent, target = self._authorities(adapter, source_name=source_name)
        try:
            with self.assertRaises(PlatformFileError) as unsafe_target:
                adapter.rebind_existing_retirement(
                    parent,
                    source_name,
                    target,
                    source_name,
                    self._facts(payload),
                )
            _assert_platform_error(
                self,
                unsafe_target,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertTrue(target_path.is_dir())
        finally:
            target.close()
            parent.close()
            root.close()

    def test_shared_api_foreign_issuer_is_rejected_without_mutation(self) -> None:
        payload = b"issuer"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        owner = WindowsPlatformAdapter()
        foreign = WindowsPlatformAdapter(_api=owner._native_api())
        root, parent, target = self._authorities(foreign, source_name=source_name)
        source = foreign.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(payload),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                owner.retire_existing_exclusive(
                    parent,
                    source_name,
                    source,
                    target,
                    source_name,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )
            self.assertFalse(source.closed)
            self.assertEqual(
                source.reprove().content_sha256,
                hashlib.sha256(payload).digest(),
            )
            self.assertFalse(target_path.exists())
        finally:
            source.close()
            target.close()
            parent.close()
            root.close()

    def test_arm_uncertainty_is_rebound_fresh_and_programmer_errors_propagate(self) -> None:
        payload = b"arm-uncertainty"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        fired = False

        def after_arm(point: str) -> None:
            nonlocal fired
            if point == "existing_retirement_after_arm" and not fired:
                fired = True
                raise PlatformFileError(
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                )

        adapter = WindowsPlatformAdapter(_fault_injector=after_arm)
        root, parent, target = self._authorities(adapter, source_name=source_name)
        source = adapter.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(payload),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
                    source_name,
                    source,
                    target,
                    source_name,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertFalse(source.closed)
            self.assertFalse(source_path.exists())
            source.close()
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            source.close()
            target.close()
            parent.close()
            root.close()

        fresh = WindowsPlatformAdapter()
        fresh_root, fresh_parent, fresh_target = self._authorities(
            fresh,
            source_name=source_name,
        )
        try:
            rebound = fresh.rebind_existing_retirement(
                fresh_parent,
                source_name,
                fresh_target,
                source_name,
                self._facts(payload),
            )
            rebound.close()
        finally:
            fresh_target.close()
            fresh_parent.close()
            fresh_root.close()

        for index, programming_error in enumerate(
            (
                TypeError("type"),
                AssertionError("assert"),
                AttributeError("attribute"),
            )
        ):
            case_name = f"program-{index}.journal"
            case_path = self.parent_path / case_name
            case_target = self.parent_path / f"program-quarantine-{index}" / case_name
            case_path.write_bytes(payload)

            def before_arm(point: str) -> None:
                if point == "existing_retirement_before_arm":
                    raise programming_error

            adapter = WindowsPlatformAdapter(_fault_injector=before_arm)
            root, parent, target = self._authorities(
                adapter,
                source_name=case_name,
                target_directory=f"program-quarantine-{index}",
            )
            source = adapter.open_existing_retirement_source(
                root,
                PureWindowsPath("PrivateCase", case_name),
                self._facts(payload),
            )
            try:
                with self.subTest(error=type(programming_error).__name__), self.assertRaises(
                    type(programming_error)
                ):
                    adapter.retire_existing_exclusive(
                        parent,
                        case_name,
                        source,
                        target,
                        case_name,
                )
                self.assertFalse(source.closed)
                self.assertEqual(
                    source.reprove().content_sha256,
                    hashlib.sha256(payload).digest(),
                )
                self.assertFalse(case_target.exists())
            finally:
                source.close()
                target.close()
                parent.close()
                root.close()

    def test_prearm_operational_failures_preserve_live_source_and_zero_mutation(self) -> None:
        payload = b"prearm-operational"
        failures: tuple[BaseException, ...] = (
            PlatformFileError(
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            ),
            OSError("prearm operational"),
        )
        for index, failure in enumerate(failures):
            source_name = f"prearm-{index}.journal"
            source_path = self.parent_path / source_name
            target_path = self.parent_path / f"prearm-quarantine-{index}" / source_name
            source_path.write_bytes(payload)

            def prearm_fault(point: str) -> None:
                if point == "existing_retirement_before_arm":
                    raise failure

            adapter = WindowsPlatformAdapter(_fault_injector=prearm_fault)
            root, parent, target = self._authorities(
                adapter,
                source_name=source_name,
                target_directory=f"prearm-quarantine-{index}",
            )
            source = adapter.open_existing_retirement_source(
                root,
                PureWindowsPath("PrivateCase", source_name),
                self._facts(payload),
            )
            try:
                with self.subTest(failure=type(failure).__name__), self.assertRaises(
                    PlatformFileError
                ) as caught:
                    adapter.retire_existing_exclusive(
                        parent,
                        source_name,
                        source,
                        target,
                        source_name,
                    )
                _assert_platform_error(
                    self,
                    caught,
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                )
                self.assertFalse(source.closed)
                self.assertEqual(
                    source.reprove().content_sha256,
                    hashlib.sha256(payload).digest(),
                )
                self.assertTrue(source_path.exists())
                self.assertFalse(target_path.exists())
            finally:
                source.close()
                target.close()
                parent.close()
                root.close()
            self.assertEqual(source_path.read_bytes(), payload)

    def test_terminal_reproof_failure_preserves_exact_target_for_fresh_rebind(self) -> None:
        payload = b"terminal-reproof"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        fired = False

        def terminal_fault(point: str) -> None:
            nonlocal fired
            if point == "existing_retirement_before_terminal_reproof" and not fired:
                fired = True
                raise PlatformFileError(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )

        adapter = WindowsPlatformAdapter(_fault_injector=terminal_fault)
        root, parent, target = self._authorities(adapter, source_name=source_name)
        source = adapter.open_existing_retirement_source(
            root,
            PureWindowsPath("PrivateCase", source_name),
            self._facts(payload),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
                    source_name,
                    source,
                    target,
                    source_name,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertTrue(source.closed)
            self.assertFalse(source_path.exists())
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            if not source.closed:
                source.close()
            target.close()
            parent.close()
            root.close()

        fresh = WindowsPlatformAdapter()
        fresh_root, fresh_parent, fresh_target = self._authorities(
            fresh,
            source_name=source_name,
        )
        try:
            rebound = fresh.rebind_existing_retirement(
                fresh_parent,
                source_name,
                fresh_target,
                source_name,
                self._facts(payload),
            )
            rebound.close()
        finally:
            fresh_target.close()
            fresh_parent.close()
            fresh_root.close()


class WindowsRecoveryCapabilityStaticTests(unittest.TestCase):
    def test_rooted_only_backend_is_not_accepted_by_exact_windows_narrow(self) -> None:
        if sys.platform != "win32":
            self.skipTest("exact Windows narrow requires Windows")
        with tempfile.TemporaryDirectory(
            prefix="localcat-descendant-static-",
            dir=ROOT.parent,
        ) as temporary:
            root = Path(temporary) / "RootCase"
            root.mkdir()
            with self.assertRaises(PlatformFileError) as caught:
                narrow_windows_locked_descendant_namespace_inspection(
                    WindowsRootedFileSystem(),  # type: ignore[arg-type]
                    root,
                )
        _assert_platform_error(
            self,
            caught,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )


if __name__ == "__main__":
    unittest.main()
