"""Task 11.2a: optional retained-directory facts and recovery synchronization."""

from __future__ import annotations

from contextlib import ExitStack
import copy
from dataclasses import replace
import errno
from functools import cached_property
import os
from pathlib import Path, PurePath
import pickle
import sys
import tempfile
from typing import Callable, TYPE_CHECKING, cast, get_protocol_members
import unittest
from unittest import mock

import platform_fs_contracts as fs

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    import platform_fs_posix as posix


class PosixDirectoryCodecContractTests(unittest.TestCase):
    def test_facts_are_live_only_numeric_observations(self) -> None:
        facts = fs.PosixBoundDirectoryFacts(0, 7)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(operation=operation.__name__), self.assertRaises(TypeError):
                _ = operation(facts)
        for field in ("device", "inode"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                _ = replace(facts, **{field: True})
            with self.subTest(field=field), self.assertRaises(ValueError):
                _ = replace(facts, **{field: -1})

    def test_directory_operations_are_optional(self) -> None:
        required = get_protocol_members(fs.PlatformFileBackend)
        self.assertNotIn("observe_bound_directory", required)
        self.assertNotIn("synchronize_bound_directory", required)
        self.assertNotIsInstance(object(), fs.PosixBoundDirectoryOperations)


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX native directory codec")
class PosixDirectoryCodecTests(unittest.TestCase):
    @cached_property
    def base(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    @cached_property
    def path(self) -> Path:
        path = self.base / "root"
        (path / "data").mkdir(parents=True)
        return path

    @cached_property
    def backend(self) -> posix.PosixPlatformAdapter:
        return posix.PosixPlatformAdapter()

    @cached_property
    def root(self) -> fs.RootedDirectoryAuthority:
        return self.bind(self.backend, self.path)

    @cached_property
    def parent(self) -> fs.BoundDirectoryAuthority:
        parent = self.backend.bind_parent(self.root, PurePath("data/artifact"))
        self.addCleanup(parent.close)
        return parent

    @cached_property
    def root_lease(self) -> fs.LockLease:
        return self.acquire(self.backend, self.root)

    @cached_property
    def parent_lease(self) -> fs.LockLease:
        return self.acquire(self.backend, self.parent)

    def bind(self, backend: posix.PosixPlatformAdapter, path: Path) -> fs.RootedDirectoryAuthority:
        root = backend.bind_root(path)
        self.addCleanup(root.close)
        return root

    def acquire(self, backend: posix.PosixPlatformAdapter, parent: fs.BoundDirectoryAuthority,
                name: str = "family.lock") -> fs.LockLease:
        lease = backend.acquire(parent, name, b"family", fs.LockPolicy(fs.LockWait.FAIL_FAST))
        self.addCleanup(lease.close)
        return lease

    def assertFailure(self, code: fs.PlatformFileErrorCode, operation: Callable[[], object]) -> None:
        with self.assertRaises(fs.PlatformFileError) as caught:
            _ = operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)

    def test_root_and_derived_parent_observe_and_sync_retained_directories(self) -> None:
        targets = ((self.root, self.root_lease, self.path),
                   (self.parent, self.parent_lease, self.path / "data"))
        real_fstat, real_fsync = os.fstat, os.fsync
        self.assertIsInstance(self.backend, fs.PosixBoundDirectoryOperations)
        for directory, lease, path in targets:
            expected = path.stat()
            sampled: set[int] = set()
            synced: list[int] = []

            def fstat(descriptor: int) -> os.stat_result:
                result = real_fstat(descriptor)
                if (result.st_dev, result.st_ino) == (expected.st_dev, expected.st_ino):
                    sampled.add(descriptor)
                return result

            def fsync(descriptor: int) -> None:
                result = real_fstat(descriptor)
                self.assertEqual((result.st_dev, result.st_ino), (expected.st_dev, expected.st_ino))
                self.assertIn(descriptor, sampled)
                synced.append(descriptor)
                real_fsync(descriptor)

            with self.subTest(path=path.name), ExitStack() as stack:
                for name in ("open", "dup", "mkdir", "unlink", "rename", "replace"):
                    _ = stack.enter_context(mock.patch.object(os, name, side_effect=AssertionError(name)))
                _ = stack.enter_context(mock.patch.object(os, "fstat", side_effect=fstat))
                _ = stack.enter_context(mock.patch.object(os, "fsync", side_effect=fsync))
                facts = self.backend.observe_bound_directory(directory)
                self.assertEqual((facts.device, facts.inode), (expected.st_dev, expected.st_ino))
                self.assertIsNone(self.backend.synchronize_bound_directory(directory, lease))
            self.assertEqual(len(synced), 1)
            self.assertFalse(directory.closed)
            self.assertFalse(lease.closed)
            directory.reprove()
            lease.reprove_binding(directory, "family.lock", b"family")

    @unittest.skipUnless(sys.platform == "darwin", "macOS initial system alias")
    def test_initial_macos_system_alias_preserves_directory_facts(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            alias = Path(temporary)
            self.assertEqual(alias.parts[1], "tmp")
            expected = alias.resolve().stat()
            root = self.backend.bind_root(alias)
            with root:
                facts = self.backend.observe_bound_directory(root)
                self.assertEqual((facts.device, facts.inode), (expected.st_dev, expected.st_ino))
                lease = self.backend.acquire(root, "family.lock", b"family", fs.LockPolicy(fs.LockWait.FAIL_FAST))
                with lease:
                    self.backend.synchronize_bound_directory(root, lease)

    def test_unlinked_artifact_can_retry_sync_after_failure_without_a_sentinel(self) -> None:
        parent, lease = self.parent, self.parent_lease
        artifact = self.path / "data" / "artifact"
        _ = artifact.write_bytes(b"temporary")
        artifact.unlink()
        expected_entries = set(artifact.parent.iterdir())
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "injected")):
            self.assertFailure(fs.PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                               lambda: self.backend.synchronize_bound_directory(parent, lease))
        self.assertFalse(parent.closed)
        self.assertFalse(lease.closed)
        parent.reprove()
        lease.reprove_binding(parent, "family.lock", b"family")
        with ExitStack() as stack:
            for name in ("open", "mkdir", "unlink", "rename", "replace"):
                _ = stack.enter_context(mock.patch.object(os, name, side_effect=AssertionError(name)))
            self.backend.synchronize_bound_directory(parent, lease)
        self.assertFalse(artifact.exists())
        self.assertEqual(set(artifact.parent.iterdir()), expected_entries)

    def test_foreign_and_foreign_derived_directories_are_rejected(self) -> None:
        foreign = posix.PosixPlatformAdapter()
        root = self.bind(foreign, self.path)
        for backend, source in ((foreign, self.root), (self.backend, root)):
            with self.subTest(backend=backend):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                   lambda: backend.observe_bound_directory(source))
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                   lambda: backend.synchronize_bound_directory(source, self.root_lease))
        crossed = self.backend.bind_parent(root, PurePath("data/artifact"))
        self.addCleanup(crossed.close)
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                           lambda: self.backend.observe_bound_directory(crossed))

    def test_wrong_parent_foreign_and_fresh_root_leases_are_rejected(self) -> None:
        root, root_lease, parent, parent_lease = self.root, self.root_lease, self.parent, self.parent_lease
        fresh = self.bind(self.backend, self.path)
        fresh_lease = self.acquire(self.backend, fresh, "fresh.lock")
        foreign = posix.PosixPlatformAdapter()
        foreign_root = self.bind(foreign, self.path)
        foreign_lease = self.acquire(foreign, foreign_root, "foreign.lock")
        for directory, lease in ((root, parent_lease), (parent, root_lease),
                                 (root, fresh_lease), (root, foreign_lease)):
            with self.subTest(directory=directory, lease=lease), mock.patch.object(
                os, "fsync", side_effect=AssertionError("wrong lease synchronized"),
            ):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                   lambda: self.backend.synchronize_bound_directory(directory, lease))
        root.reprove_location()
        root_lease.reprove_binding(root, "family.lock", b"family")

    def test_closed_root_and_parent_are_rejected(self) -> None:
        targets = ((self.root, self.root_lease), (self.parent, self.parent_lease))
        for directory, lease in targets:
            directory.close()
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                               lambda: self.backend.observe_bound_directory(directory))
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                               lambda: self.backend.synchronize_bound_directory(directory, lease))
            self.assertFalse(lease.closed)

    def test_closed_lease_is_rejected_without_closing_directory(self) -> None:
        self.parent_lease.close()
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                           lambda: self.backend.synchronize_bound_directory(self.parent, self.parent_lease))
        self.parent.reprove()

    def test_paths_raw_descriptors_and_historical_facts_are_not_authority(self) -> None:
        facts = self.backend.observe_bound_directory(self.root)
        for value in (self.path, 42, facts):
            directory = cast(fs.BoundDirectoryAuthority, cast(object, value))
            with self.subTest(value=type(value).__name__), self.assertRaises(TypeError):
                _ = self.backend.observe_bound_directory(directory)
            with self.subTest(value=type(value).__name__), self.assertRaises(TypeError):
                self.backend.synchronize_bound_directory(directory, self.root_lease)
        with self.assertRaises(TypeError):
            self.backend.synchronize_bound_directory(self.root, cast(fs.LockLease, cast(object, facts)))

    def test_observed_root_or_parent_replacement_refuses_success(self) -> None:
        for replaced in ("root", "parent"):
            with self.subTest(replaced=replaced), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "root"
                (path / "data").mkdir(parents=True)
                root = self.backend.bind_root(path)
                with root:
                    parent = self.backend.bind_parent(root, PurePath("data/artifact"))
                    with parent:
                        lease = self.backend.acquire(parent, "family.lock", b"family", fs.LockPolicy(fs.LockWait.FAIL_FAST))
                        with lease:
                            target = path if replaced == "root" else path / "data"
                            _ = target.rename(target.with_name("moved"))
                            target.mkdir()
                            with mock.patch.object(os, "fsync", side_effect=AssertionError("stale sync")):
                                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                                                   lambda: self.backend.observe_bound_directory(parent))
                                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                                                   lambda: self.backend.synchronize_bound_directory(parent, lease))
                            self.assertFalse(parent.closed)
                            self.assertFalse(lease.closed)

    def test_directory_drift_after_fstat_is_rejected(self) -> None:
        def change_location(point: str) -> None:
            if point == "bound_directory_after_stat":
                _ = self.path.rename(self.base / "moved")
                (self.path / "data").mkdir(parents=True)

        backend = posix.PosixPlatformAdapter(_fault_injector=change_location)
        root = backend.bind_root(self.path)
        with root:
            parent = backend.bind_parent(root, PurePath("data/artifact"))
            with parent:
                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                                   lambda: backend.observe_bound_directory(parent))
                self.assertFalse(parent.closed)

    def test_directory_drift_during_fsync_is_rejected(self) -> None:
        parent, lease = self.parent, self.parent_lease
        real_fsync = os.fsync

        def move_after_sync(descriptor: int) -> None:
            real_fsync(descriptor)
            _ = self.path.rename(self.base / "moved")
            (self.path / "data").mkdir(parents=True)

        with mock.patch.object(os, "fsync", side_effect=move_after_sync):
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                               lambda: self.backend.synchronize_bound_directory(parent, lease))
        self.assertFalse(parent.closed)
        self.assertFalse(lease.closed)

    def test_lease_drift_during_fsync_is_rejected(self) -> None:
        parent, lease = self.parent, self.parent_lease
        real_fsync = os.fsync

        def change_lock(descriptor: int) -> None:
            real_fsync(descriptor)
            _ = (self.path / "data" / "family.lock").write_bytes(b"changed")

        with mock.patch.object(os, "fsync", side_effect=change_lock):
            self.assertFailure(fs.PlatformFileErrorCode.LOCK_UNAVAILABLE,
                               lambda: self.backend.synchronize_bound_directory(parent, lease))
        self.assertFalse(parent.closed)
        self.assertFalse(lease.closed)


if __name__ == "__main__":
    _ = unittest.main()
