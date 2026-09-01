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
    DEVICE_SECRET_SIZE_BYTES,
    ExistingCandidateRecovery,
    ExistingFileRetirement,
    LedgerEnumerationLimits,
    LockPolicy,
    LockWait,
    LockedDescendantNamespaceInspection,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateProofContext,
    PrivateProofObjectRole,
    PublishMode,
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

    def _live_authorities(
        self,
        adapter: WindowsPlatformAdapter,
        *,
        source_name: str,
        payload: bytes,
        target_directory: str = "quarantine",
    ) -> tuple[object, object, object, object]:
        root, ordinary_parent, target = self._authorities(
            adapter,
            source_name=source_name,
            target_directory=target_directory,
        )
        ordinary_parent.close()
        source_parent = adapter.bind_retirement_source_directory(
            root,
            PureWindowsPath("PrivateCase", source_name),
        )
        source = adapter.open_existing_retirement_source(
            source_parent,
            self._facts(payload),
        )
        return root, source_parent, target, source

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
        root, parent, target, source = self._live_authorities(
            adapter,
            source_name=source_name,
            payload=payload,
        )
        try:
            self._assert_write_and_delete_access_blocked(adapter, source_path)
            retained = capability.retire_existing_exclusive(
                parent,
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

    def test_live_source_moves_across_root_sibling_and_cousin_topologies(self) -> None:
        cases = (
            (
                "root",
                PureWindowsPath("root-source.bin"),
                self.root_path / "root-source.bin",
                None,
            ),
            (
                "sibling",
                PureWindowsPath("PrivateCase", "sibling-source.bin"),
                self.parent_path / "sibling-source.bin",
                None,
            ),
            (
                "cousin",
                PureWindowsPath("PrivateCase", "Nested", "cousin-source.bin"),
                self.parent_path / "Nested" / "cousin-source.bin",
                "OtherCase",
            ),
        )
        for label, relative, source_path, target_parent_name in cases:
            with self.subTest(topology=label):
                payload = f"cross-parent-{label}".encode()
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(payload)
                adapter = WindowsPlatformAdapter()
                root = adapter.bind_root(self.root_path)
                ordinary_target_parent = quarantine = target = source_parent = None
                source = retained = None
                try:
                    target_base = root
                    if target_parent_name is not None:
                        (self.root_path / target_parent_name).mkdir(exist_ok=True)
                        ordinary_target_parent = adapter.bind_parent(
                            root,
                            PureWindowsPath(target_parent_name, "placeholder"),
                        )
                        target_base = ordinary_target_parent
                    quarantine = adapter.bind_or_create_child_directory(
                        target_base,
                        f"quarantine-{label}",
                    )
                    target = adapter.bind_or_create_child_directory(
                        quarantine,
                        "attempt",
                    )
                    if ordinary_target_parent is not None:
                        ordinary_target_parent.close()
                        ordinary_target_parent = None
                    source_parent = (
                        adapter.bind_retirement_source_directory(
                            root,
                            relative,
                        )
                    )
                    source = adapter.open_existing_retirement_source(
                        source_parent,
                        self._facts(payload),
                    )
                    retained = adapter.retire_existing_exclusive(
                        source_parent,
                        source,
                        target,
                        relative.name,
                    )
                    source = None
                    retained.reprove()
                    target_path = (
                        self.root_path
                        / target_parent_name
                        if target_parent_name is not None
                        else self.root_path
                    ) / f"quarantine-{label}" / "attempt" / relative.name
                    self.assertFalse(source_path.exists())
                    retained.close()
                    retained = None
                    self.assertEqual(target_path.read_bytes(), payload)
                finally:
                    if retained is not None:
                        retained.close()
                    if source is not None:
                        source.close()
                    if source_parent is not None:
                        source_parent.close()
                    if target is not None:
                        target.close()
                    if quarantine is not None:
                        quarantine.close()
                    if ordinary_target_parent is not None:
                        ordinary_target_parent.close()
                    root.close()

    def test_private_pending_and_w2_authorities_close_before_sibling_retirement(self) -> None:
        payload = b"private-published-retirement"
        adapter = WindowsPlatformAdapter()
        root = adapter.bind_root(self.root_path)
        owner_parent = adapter.bind_parent(root, PureWindowsPath("placeholder.bin"))
        private = target = source_parent = source = retained = None
        pending = evidence = private_evidence = key_evidence = secret = None
        key_pending = None
        try:
            private = adapter.create_private_directory(owner_parent, "PrivatePublished")
            owner_parent.close()
            owner_parent = None
            candidate = private.create_candidate("source.candidate", private=True)
            candidate.write_all(payload)
            candidate.flush_content()
            facts = self._facts(payload)
            pending = private.begin_publish(
                candidate,
                "source.bin",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            destination = pending.retained_destination()
            evidence = adapter.prove_private(destination)
            self.assertEqual(destination.read_all(), payload)
            evidence.close()
            evidence = None
            pending.terminal_reproof()
            pending.close()
            pending = None

            key_payload = b"k" * DEVICE_SECRET_SIZE_BYTES
            key_candidate = private.create_candidate("device.key.candidate", private=True)
            key_candidate.write_all(key_payload)
            key_candidate.flush_content()
            key_pending = private.begin_publish(
                key_candidate,
                "device.key",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            key_file = key_pending.retained_destination()
            key_evidence = adapter.prove_private(key_file)
            secret = adapter.bind_device_secret(key_file)
            private_evidence = adapter.prove_private(private)
            context = PrivateProofContext(
                PrivateProofObjectRole.PRIVATE_DIRECTORY,
                hashlib.sha256(b"private-lifecycle").digest(),
            )
            proof = adapter.mint(private_evidence, secret, context)
            verified = adapter.verify(private_evidence, secret, proof, context)
            adapter.consume_verified(verified, context)
            key_evidence.close()
            key_evidence = None
            secret.close()
            secret = None
            private_evidence.close()
            private_evidence = None
            key_pending.terminal_reproof()
            key_pending.close()
            key_pending = None
            private.close()
            private = None

            quarantine = adapter.bind_or_create_child_directory(root, "quarantine-private")
            try:
                target = adapter.bind_or_create_child_directory(quarantine, "attempt")
            finally:
                quarantine.close()
            source_parent = adapter.bind_retirement_source_directory(
                root,
                PureWindowsPath("PrivatePublished", "source.bin"),
            )
            source = adapter.open_existing_retirement_source(source_parent, facts)
            retained = adapter.retire_existing_exclusive(
                source_parent,
                source,
                target,
                "source.bin",
            )
            source = None
            retained.reprove()
            retained.close()
            retained = None
            self.assertEqual(
                (self.root_path / "quarantine-private" / "attempt" / "source.bin").read_bytes(),
                payload,
            )
        finally:
            if evidence is not None:
                evidence.close()
            if pending is not None:
                pending.close()
            if key_pending is not None:
                key_pending.close()
            if key_evidence is not None:
                key_evidence.close()
            if secret is not None:
                secret.close()
            if private_evidence is not None:
                private_evidence.close()
            if retained is not None:
                retained.close()
            if source is not None:
                source.close()
            if source_parent is not None:
                source_parent.close()
            if target is not None:
                target.close()
            if private is not None:
                private.close()
            if owner_parent is not None:
                owner_parent.close()
            root.close()

    def test_live_retirement_rejects_same_adapter_different_root_before_arm(self) -> None:
        payload = b"different-root"
        source_name = "different-root.bin"
        source_path = self.parent_path / source_name
        source_path.write_bytes(payload)
        other_root_path = self.container / "OtherRoot"
        other_root_path.mkdir()
        adapter = WindowsPlatformAdapter()
        source_root = adapter.bind_root(self.root_path)
        target_root = adapter.bind_root(other_root_path)
        target_parent = adapter.bind_parent(
            target_root,
            PureWindowsPath("placeholder.bin"),
        )
        target = adapter.bind_or_create_child_directory(
            target_parent,
            "quarantine",
        )
        target_parent.close()
        source_parent = adapter.bind_retirement_source_directory(
            source_root,
            PureWindowsPath("PrivateCase", source_name),
        )
        source = adapter.open_existing_retirement_source(
            source_parent,
            self._facts(payload),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    source_parent,
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
            self.assertEqual(source.reprove().snapshot.byte_count, len(payload))
            self.assertTrue(source_path.exists())
            self.assertIsNone(target.inspect_entry(source_name))
        finally:
            source.close()
            source_parent.close()
            target.close()
            target_root.close()
            source_root.close()

    def test_fresh_backend_rebinds_only_source_absent_exact_target(self) -> None:
        payload = b"fresh-rebind-v3"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        target_path = self.parent_path / "quarantine" / source_name
        source_path.write_bytes(payload)
        first = WindowsPlatformAdapter()
        root, parent, target, source = self._live_authorities(
            first,
            source_name=source_name,
            payload=payload,
        )
        retained = first.retire_existing_exclusive(
            parent,
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

    def test_fresh_rebind_rejects_same_adapter_different_root_before_arm(self) -> None:
        payload = b"different-root-fresh-rebind"
        source_name = "activation.journal"
        source_path = self.parent_path / source_name
        other_root_path = self.container / "OtherFreshRoot"
        target_path = other_root_path / "quarantine" / source_name
        target_path.parent.mkdir(parents=True)
        target_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        source_root = adapter.bind_root(self.root_path)
        source_parent = adapter.bind_parent(
            source_root,
            PureWindowsPath("PrivateCase", source_name),
        )
        target_root = adapter.bind_root(other_root_path)
        target_parent = adapter.bind_parent(
            target_root,
            PureWindowsPath("placeholder.bin"),
        )
        target = adapter.bind_or_create_child_directory(
            target_parent,
            "quarantine",
        )
        target_parent.close()
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.rebind_existing_retirement(
                    source_parent,
                    source_name,
                    target,
                    source_name,
                    self._facts(payload),
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )
            self.assertFalse(source_path.exists())
            self.assertEqual(target_path.read_bytes(), payload)
        finally:
            target.close()
            target_root.close()
            source_parent.close()
            source_root.close()

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
        root, parent, target, source = self._live_authorities(
            adapter,
            source_name=source_name,
            payload=source_payload,
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
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
            with adapter.bind_retirement_source_directory(
                root,
                PureWindowsPath("PrivateCase", source_name),
            ) as source_parent, self.assertRaises(PlatformFileError) as wrong_source:
                adapter.open_existing_retirement_source(
                    source_parent,
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
        root, parent, target, source = self._live_authorities(
            foreign,
            source_name=source_name,
            payload=payload,
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                owner.retire_existing_exclusive(
                    parent,
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
        root, parent, target, source = self._live_authorities(
            adapter,
            source_name=source_name,
            payload=payload,
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
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
            root, parent, target, source = self._live_authorities(
                adapter,
                source_name=case_name,
                payload=payload,
                target_directory=f"program-quarantine-{index}",
            )
            try:
                with self.subTest(error=type(programming_error).__name__), self.assertRaises(
                    type(programming_error)
                ):
                    adapter.retire_existing_exclusive(
                        parent,
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
            root, parent, target, source = self._live_authorities(
                adapter,
                source_name=source_name,
                payload=payload,
                target_directory=f"prearm-quarantine-{index}",
            )
            try:
                with self.subTest(failure=type(failure).__name__), self.assertRaises(
                    PlatformFileError
                ) as caught:
                    adapter.retire_existing_exclusive(
                        parent,
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
        root, parent, target, source = self._live_authorities(
            adapter,
            source_name=source_name,
            payload=payload,
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.retire_existing_exclusive(
                    parent,
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


@unittest.skipUnless(sys.platform == "win32", "real capability tests require Windows")
class WindowsExistingCandidateRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="localcat-existing-candidate-",
        )
        self.container = Path(self._temporary.name)
        self.root_path = self.container / "RootCase"
        self.parent_path = self.root_path / "CandidateCase"
        self.target_path = self.parent_path / "quarantine"
        self.target_path.mkdir(parents=True)
        self._owner_serial = 0

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _facts(payload: bytes) -> CandidateContentFacts:
        return CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())

    def _authorities(
        self,
        adapter: WindowsPlatformAdapter,
        source_name: str,
    ) -> tuple[object, object, object]:
        root = adapter.bind_root(self.root_path)
        parent = adapter.bind_parent(
            root,
            PureWindowsPath("CandidateCase", source_name),
        )
        target = adapter.bind_or_create_child_directory(parent, "quarantine")
        return root, parent, target

    def _owner_authorities(
        self,
        adapter: WindowsPlatformAdapter,
        root: object,
        source_parent: object,
    ) -> tuple[object, object]:
        self._owner_serial += 1
        token = f"{self._owner_serial:04d}"
        sidecar_name = f".candidate-owner-{token}.sqlite3"
        if source_parent is root:
            sidecar_path = self.root_path / sidecar_name
            sidecar_relative = PureWindowsPath(sidecar_name)
        else:
            sidecar_path = self.parent_path / sidecar_name
            sidecar_relative = PureWindowsPath("CandidateCase", sidecar_name)
        sidecar_path.write_bytes(b"canonical-sidecar")
        lease = adapter.acquire(
            source_parent,
            f".candidate-owner-{token}.lock",
            b"candidate-owner-v1",
            LockPolicy(LockWait.FAIL_FAST),
        )
        try:
            guard = adapter.guard_existing_for_mutation(
                root, sidecar_relative
            )
        except BaseException:
            lease.close()
            raise
        return lease, guard

    def _recover_and_publish(
        self,
        *,
        source_only: bool,
    ) -> None:
        payload = b'exact internal candidate\n{"schema":1}\n'
        source_name = "manifest.candidate"
        target_name = "manifest.retired"
        source_path = self.parent_path / source_name
        retired_path = self.target_path / target_name
        (source_path if source_only else retired_path).write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        self.assertIsInstance(adapter, ExistingCandidateRecovery)
        root, parent, target = self._authorities(adapter, source_name)
        lease, guard = self._owner_authorities(adapter, root, parent)
        candidate = pending = None
        try:
            candidate = adapter.recover_existing_candidate(
                parent,
                source_name,
                target,
                target_name,
                self._facts(payload),
                owner_lease=lease,
                mutation_guard=guard,
            )
            self.assertFalse(retired_path.exists())
            pending = parent.begin_publish(
                candidate,
                "manifest.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            candidate = None
            facts = pending.terminal_reproof()
            self.assertEqual(facts.byte_count, len(payload))
            self.assertEqual(facts.content_sha256, hashlib.sha256(payload).digest())
        finally:
            if pending is not None:
                pending.close()
            if candidate is not None:
                candidate.close()
            guard.close()
            lease.close()
            target.close()
            parent.close()
            root.close()
        self.assertFalse(source_path.exists())
        self.assertFalse(retired_path.exists())
        self.assertEqual((self.parent_path / "manifest.json").read_bytes(), payload)

    def test_source_only_exact_candidate_is_flushed_and_publishable(self) -> None:
        self._recover_and_publish(source_only=True)

    def test_target_only_exact_candidate_is_restored_without_overwrite(self) -> None:
        self._recover_and_publish(source_only=False)

    def test_rooted_source_parent_is_recovered_by_the_same_contract(self) -> None:
        payload = b"rooted internal candidate"
        source_name = "root-manifest.candidate"
        target_name = "root-manifest.retired"
        final_name = "root-manifest.json"
        source_path = self.root_path / source_name
        source_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        root = adapter.bind_root(self.root_path)
        target = adapter.bind_or_create_child_directory(root, "root-quarantine")
        lease, guard = self._owner_authorities(adapter, root, root)
        candidate = pending = None
        try:
            candidate = adapter.recover_existing_candidate(
                root,
                source_name,
                target,
                target_name,
                self._facts(payload),
                owner_lease=lease,
                mutation_guard=guard,
            )
            pending = root.begin_publish(
                candidate,
                final_name,
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            candidate = None
            facts = pending.terminal_reproof()
            self.assertEqual(facts.byte_count, len(payload))
            self.assertEqual(facts.content_sha256, hashlib.sha256(payload).digest())
        finally:
            if pending is not None:
                pending.close()
            if candidate is not None:
                candidate.close()
            guard.close()
            lease.close()
            target.close()
            root.close()
        self.assertFalse(source_path.exists())
        self.assertEqual((self.root_path / final_name).read_bytes(), payload)

    def test_rooted_target_only_candidate_is_restored_without_overwrite(self) -> None:
        payload = b"rooted retired internal candidate"
        source_name = "root-target-manifest.candidate"
        target_name = source_name
        source_path = self.root_path / source_name
        retired_path = (
            self.root_path / "root-quarantine" / "receipt" / target_name
        )
        retired_path.parent.mkdir(parents=True)
        retired_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        root = adapter.bind_root(self.root_path)
        retirement_root = adapter.bind_or_create_child_directory(
            root, "root-quarantine"
        )
        target = adapter.bind_or_create_child_directory(
            retirement_root, "receipt"
        )
        lease, guard = self._owner_authorities(adapter, root, root)
        candidate = None
        try:
            candidate = adapter.recover_existing_candidate(
                root,
                source_name,
                target,
                target_name,
                self._facts(payload),
                owner_lease=lease,
                mutation_guard=guard,
            )
            self.assertTrue(source_path.is_file())
            self.assertFalse(retired_path.exists())
        finally:
            if candidate is not None:
                candidate.close()
            guard.close()
            lease.close()
            target.close()
            retirement_root.close()
            root.close()

    def test_rooted_target_restore_keeps_w1_and_sidecar_guard_live(self) -> None:
        payload = b"rooted candidate with live owner authorities"
        source_name = "root-live-owner.candidate"
        sidecar_name = "canonical.sqlite3"
        source_path = self.root_path / source_name
        sidecar_path = self.root_path / sidecar_name
        retired_path = (
            self.root_path / "root-quarantine" / "receipt" / source_name
        )
        sidecar_path.write_bytes(b"sqlite-sidecar")
        retired_path.parent.mkdir(parents=True)
        retired_path.write_bytes(payload)
        rename_window_handles: dict[str, object] = {}

        def observe_window(point: str) -> None:
            if point == "existing_candidate_recovery_after_restore":
                rename_window_handles["source"] = root._records[-1].handle
                rename_window_handles["lease"] = lease._records[-1].handle
                rename_window_handles["guard"] = guard._records[-1].handle

        adapter = WindowsPlatformAdapter(_fault_injector=observe_window)
        root = adapter.bind_root(self.root_path)
        lease = adapter.acquire(
            root,
            ".resource.lock",
            b"resource-lock-v1",
            LockPolicy(LockWait.FAIL_FAST),
        )
        guard = adapter.guard_existing_for_mutation(
            root, PureWindowsPath(sidecar_name)
        )
        retirement_root = adapter.bind_or_create_child_directory(
            root, "root-quarantine"
        )
        target = adapter.bind_or_create_child_directory(
            retirement_root, "receipt"
        )
        candidate = None
        try:
            candidate = adapter.recover_existing_candidate(
                root,
                source_name,
                target,
                source_name,
                self._facts(payload),
                owner_lease=lease,
                mutation_guard=guard,
            )
            lease.reprove()
            guard.reprove()
            self.assertEqual(
                set(rename_window_handles), {"source", "lease", "guard"}
            )
            self.assertIsNot(
                root._records[-1].handle,
                rename_window_handles["source"],
            )
            self.assertIsNot(
                lease._records[-1].handle,
                rename_window_handles["lease"],
            )
            self.assertIsNot(
                guard._records[-1].handle,
                rename_window_handles["guard"],
            )
            self.assertTrue(source_path.is_file())
            self.assertFalse(retired_path.exists())
        finally:
            if candidate is not None:
                candidate.close()
            target.close()
            retirement_root.close()
            if guard is not None:
                guard.close()
            lease.close()
            root.close()

    def test_each_owner_window_restores_strict_records_and_rejects_root_drift(self) -> None:
        source_name = "owner-window.candidate"
        adapter = WindowsPlatformAdapter()
        root = adapter.bind_root(self.root_path)
        lease, guard = self._owner_authorities(adapter, root, root)
        foreign_path = self.container / "ForeignRoot"
        foreign_path.mkdir()
        original_open = platform_fs_windows._open_directory_record

        def open_foreign_record(
            api: object,
            _path: str,
            _expected_final_path: str,
            **kwargs: object,
        ) -> object:
            return original_open(
                api,
                str(foreign_path),
                str(foreign_path),
                **kwargs,
            )

        try:
            for label, authority in (("lease", lease), ("guard", guard)):
                with self.subTest(authority=label):
                    strict_handle = authority._records[-1].handle
                    window = authority._begin_directory_rename_window(root)
                    compatible_handle = authority._records[-1].handle
                    self.assertIsNot(compatible_handle, strict_handle)
                    with (
                        mock.patch.object(
                            platform_fs_windows,
                            "_open_directory_record",
                            side_effect=open_foreign_record,
                        ),
                        self.assertRaises(PlatformFileError) as caught,
                    ):
                        window.restore()
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.IDENTITY_STALE,
                    )
                    self.assertIs(
                        authority._records[-1].handle,
                        compatible_handle,
                    )
                    window.restore()
                    self.assertIsNot(
                        authority._records[-1].handle,
                        compatible_handle,
                    )
                    authority.reprove()
        finally:
            guard.close()
            lease.close()
            root.close()

    def test_owner_authorities_from_a_different_parent_are_rejected(self) -> None:
        payload = b"wrong-owner-parent"
        source_name = "wrong-owner.candidate"
        source_path = self.parent_path / source_name
        source_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        root, parent, target = self._authorities(adapter, source_name)
        lease, guard = self._owner_authorities(adapter, root, root)
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.recover_existing_candidate(
                    parent,
                    source_name,
                    target,
                    "wrong-owner.retired",
                    self._facts(payload),
                    owner_lease=lease,
                    mutation_guard=guard,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )
            self.assertEqual(source_path.read_bytes(), payload)
        finally:
            guard.close()
            lease.close()
            target.close()
            parent.close()
            root.close()

    def test_restore_fault_boundaries_replay_exact_target_or_source(self) -> None:
        payload = b"recoverable-retired-candidate"
        facts = self._facts(payload)
        cases = (
            ("existing_candidate_recovery_before_restore", False),
            ("existing_candidate_recovery_after_restore", True),
            ("existing_candidate_recovery_before_terminal", True),
        )
        for index, (fault_point, expect_source) in enumerate(cases):
            with self.subTest(fault_point=fault_point):
                source_name = f"fault-{index}.candidate"
                target_name = f"fault-{index}.retired"
                final_name = f"fault-{index}.json"
                source_path = self.parent_path / source_name
                retired_path = self.target_path / target_name
                retired_path.write_bytes(payload)

                def fault(point: str) -> None:
                    if point == fault_point:
                        raise OSError("simulated process boundary")

                adapter = WindowsPlatformAdapter(_fault_injector=fault)
                root, parent, target = self._authorities(adapter, source_name)
                lease, guard = self._owner_authorities(adapter, root, parent)
                try:
                    with self.assertRaises(PlatformFileError) as caught:
                        adapter.recover_existing_candidate(
                            parent,
                            source_name,
                            target,
                            target_name,
                            facts,
                            owner_lease=lease,
                            mutation_guard=guard,
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                    )
                finally:
                    guard.close()
                    lease.close()
                    target.close()
                    parent.close()
                    root.close()
                self.assertEqual(source_path.exists(), expect_source)
                self.assertEqual(retired_path.exists(), not expect_source)
                self.assertEqual(
                    (source_path if expect_source else retired_path).read_bytes(),
                    payload,
                )

                fresh = WindowsPlatformAdapter()
                fresh_root, fresh_parent, fresh_target = self._authorities(
                    fresh,
                    source_name,
                )
                fresh_lease, fresh_guard = self._owner_authorities(
                    fresh, fresh_root, fresh_parent
                )
                candidate = pending = None
                try:
                    candidate = fresh.recover_existing_candidate(
                        fresh_parent,
                        source_name,
                        fresh_target,
                        target_name,
                        facts,
                        owner_lease=fresh_lease,
                        mutation_guard=fresh_guard,
                    )
                    pending = fresh_parent.begin_publish(
                        candidate,
                        final_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                    candidate = None
                    self.assertEqual(
                        pending.terminal_reproof().content_sha256,
                        facts.content_sha256,
                    )
                finally:
                    if pending is not None:
                        pending.close()
                    if candidate is not None:
                        candidate.close()
                    fresh_guard.close()
                    fresh_lease.close()
                    fresh_target.close()
                    fresh_parent.close()
                    fresh_root.close()
                self.assertEqual((self.parent_path / final_name).read_bytes(), payload)

    def test_owner_reproof_failure_closes_recovered_handles_for_fresh_retry(self) -> None:
        payload = b"owner-reproof-failure-candidate"
        source_name = "owner-reproof.candidate"
        target_name = "owner-reproof.retired"
        source_path = self.parent_path / source_name
        retired_path = self.target_path / target_name
        retired_path.write_bytes(payload)
        facts = self._facts(payload)

        adapter = WindowsPlatformAdapter()
        root, parent, target = self._authorities(adapter, source_name)
        lease, guard = self._owner_authorities(adapter, root, parent)
        original_reprove = type(guard)._reprove_guard
        guard_reproofs = 0

        def fail_final_guard_reproof(authority: object) -> object:
            nonlocal guard_reproofs
            if authority is guard:
                guard_reproofs += 1
                if guard_reproofs == 3:
                    raise OSError("simulated post-window owner reproof failure")
            return original_reprove(authority)

        try:
            with (
                mock.patch.object(
                    type(guard),
                    "_reprove_guard",
                    fail_final_guard_reproof,
                ),
                self.assertRaises(PlatformFileError) as caught,
            ):
                adapter.recover_existing_candidate(
                    parent,
                    source_name,
                    target,
                    target_name,
                    facts,
                    owner_lease=lease,
                    mutation_guard=guard,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertEqual(guard_reproofs, 3)
            lease.reprove()
            guard.reprove()
        finally:
            guard.close()
            lease.close()
            target.close()
            parent.close()
            root.close()

        self.assertEqual(source_path.read_bytes(), payload)
        self.assertFalse(retired_path.exists())
        fresh = WindowsPlatformAdapter()
        fresh_root, fresh_parent, fresh_target = self._authorities(
            fresh, source_name
        )
        fresh_lease, fresh_guard = self._owner_authorities(
            fresh, fresh_root, fresh_parent
        )
        candidate = None
        try:
            candidate = fresh.recover_existing_candidate(
                fresh_parent,
                source_name,
                fresh_target,
                target_name,
                facts,
                owner_lease=fresh_lease,
                mutation_guard=fresh_guard,
            )
            fresh_lease.reprove()
            fresh_guard.reprove()
        finally:
            if candidate is not None:
                candidate.close()
            fresh_guard.close()
            fresh_lease.close()
            fresh_target.close()
            fresh_parent.close()
            fresh_root.close()

    def test_source_only_flush_failure_is_durability_unavailable_and_non_mutating(self) -> None:
        payload = b"flush-before-adopt"
        source_name = "flush.candidate"
        target_name = "flush.retired"
        source_path = self.parent_path / source_name
        retired_path = self.target_path / target_name
        source_path.write_bytes(payload)
        adapter = WindowsPlatformAdapter()
        root, parent, target = self._authorities(adapter, source_name)
        lease, guard = self._owner_authorities(adapter, root, parent)
        original_checked_bool = parent._api.checked_bool

        def checked_bool(operation: str, function: object, *args: object) -> None:
            if operation == "FlushFileBuffers":
                raise Win32CallError(operation, 5)
            original_checked_bool(operation, function, *args)

        try:
            with (
                mock.patch.object(parent._api, "checked_bool", side_effect=checked_bool),
                self.assertRaises(PlatformFileError) as caught,
            ):
                adapter.recover_existing_candidate(
                    parent,
                    source_name,
                    target,
                    target_name,
                    self._facts(payload),
                    owner_lease=lease,
                    mutation_guard=guard,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
            )
        finally:
            guard.close()
            lease.close()
            target.close()
            parent.close()
            root.close()
        self.assertEqual(source_path.read_bytes(), payload)
        self.assertFalse(retired_path.exists())

    def test_coexist_mismatch_unknown_and_unsafe_states_are_non_mutating(self) -> None:
        payload = b"expected-candidate"
        facts = self._facts(payload)
        cases = (
            "coexist",
            "source-mismatch",
            "target-mismatch",
            "unknown",
            "unsafe",
            "hardlink",
        )
        for case in cases:
            with self.subTest(case=case):
                source_name = f"{case}.candidate"
                target_name = f"{case}.retired"
                source_path = self.parent_path / source_name
                retired_path = self.target_path / target_name
                unknown_path = self.target_path / f"{case}.unknown"
                alias_path = self.parent_path / f"{case}.alias"
                if case == "coexist":
                    source_path.write_bytes(payload)
                    retired_path.write_bytes(payload)
                elif case == "source-mismatch":
                    source_path.write_bytes(b"wrong-source")
                elif case == "target-mismatch":
                    retired_path.write_bytes(b"wrong-target")
                elif case == "unknown":
                    unknown_path.write_bytes(b"foreign")
                elif case == "unsafe":
                    retired_path.mkdir()
                else:
                    source_path.write_bytes(payload)
                    os.link(source_path, alias_path)
                before = {
                    path.name: ("directory" if path.is_dir() else path.read_bytes())
                    for path in (source_path, retired_path, unknown_path, alias_path)
                    if path.exists()
                }
                adapter = WindowsPlatformAdapter()
                root, parent, target = self._authorities(adapter, source_name)
                lease, guard = self._owner_authorities(adapter, root, parent)
                try:
                    with self.assertRaises(PlatformFileError) as caught:
                        adapter.recover_existing_candidate(
                            parent,
                            source_name,
                            target,
                            target_name,
                            facts,
                            owner_lease=lease,
                            mutation_guard=guard,
                        )
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                    )
                finally:
                    guard.close()
                    lease.close()
                    target.close()
                    parent.close()
                    root.close()
                after = {
                    path.name: ("directory" if path.is_dir() else path.read_bytes())
                    for path in (source_path, retired_path, unknown_path, alias_path)
                    if path.exists()
                }
                self.assertEqual(after, before)


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
