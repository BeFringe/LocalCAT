"""原 root/W1 直接子项 metadata 观察的闭集与资源生命周期反例。"""

from __future__ import annotations

from contextlib import ExitStack
import errno
from functools import cached_property
import os
from pathlib import Path, PurePath
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, Callable, cast, get_protocol_members
import unittest
from unittest import mock

import platform_fs_contracts as fs

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    import platform_fs_posix as posix


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX native observation")
class PosixRootObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        _ = self.lease

    @cached_property
    def base(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    @cached_property
    def path(self) -> Path:
        path = self.base / "root"
        path.mkdir()
        return path

    @cached_property
    def backend(self) -> posix.PosixPlatformAdapter:
        return posix.PosixPlatformAdapter()

    @cached_property
    def root(self) -> fs.RootedDirectoryAuthority:
        root = self.backend.bind_root(self.path)
        self.addCleanup(root.close)
        return root

    @cached_property
    def lease(self) -> fs.LockLease:
        return self.acquire(self.backend, self.root)

    @cached_property
    def limits(self) -> fs.DirectoryObservationLimits:
        return fs.DirectoryObservationLimits(maximum_depth=0, maximum_directory_handles=1)

    @cached_property
    def cancel(self) -> fs.DirectoryObservationCancellation:
        return fs.DirectoryObservationCancellation()

    def acquire(self, backend: posix.PosixPlatformAdapter, root: fs.BoundDirectoryAuthority) -> fs.LockLease:
        lease = backend.acquire(root, "family.lock", b"family", fs.LockPolicy(fs.LockWait.FAIL_FAST))
        self.addCleanup(lease.close)
        return lease

    def observe(self) -> tuple[fs.DirectoryEntryMetadata, ...]:
        return self.backend.observe_root_children(self.root, self.lease, self.limits, self.cancel)

    def assertFailure(self, code: fs.PlatformFileErrorCode, operation: Callable[[], object]) -> fs.PlatformFileError:
        with self.assertRaises(fs.PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)
        return caught.exception

    def assertBorrowedLive(self) -> None:
        self.assertFalse(self.root.closed)
        self.assertFalse(self.lease.closed)
        self.root.reprove_location()
        self.lease.reprove_binding(self.root, "family.lock", b"family")

    def afterPass(self, number: int, action: Callable[[], None]):
        original_close = posix._PosixDirectoryStream.close
        count = 0

        def close(stream) -> None:
            nonlocal count
            original_close(stream)
            count += 1
            if count == number:
                action()

        return mock.patch.object(posix._PosixDirectoryStream, "close", close)

    def test_optional_runtime_capability_does_not_expand_required_backend(self) -> None:
        capability = getattr(fs, "LockedRootDirectoryObservation")
        self.assertIsInstance(self.backend, capability)
        self.assertNotIsInstance(object(), capability)
        self.assertNotIn("observe_root_children", get_protocol_members(fs.PlatformFileBackend))
        self.assertNotIsInstance(self.backend, fs.LockedDescendantNamespaceInspection)
        self.assertIsInstance(self.backend, fs.ReadOnlyDirectoryObservation)

    def test_complete_metadata_preserves_unrelated_and_unavailable_entries(self) -> None:
        (self.path / "source").write_bytes(b"private body")
        os.link(self.path / "source", self.path / "alias")
        (self.path / "nested").mkdir()
        (self.path / "nested" / "hidden").write_bytes(b"not enumerated")
        (self.path / "bad\\name").mkdir()
        (self.path / "link").symlink_to(self.base, target_is_directory=True)
        os.mkfifo(self.path / "pipe")
        entries = self.observe()
        self.assertIs(type(entries), tuple)
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"family.lock", "source", "alias", "nested", "bad\\name", "link", "pipe"})
        self.assertEqual(by_name["nested"].kind, fs.DirectoryEntryKind.DIRECTORY)
        self.assertEqual(by_name["source"].kind, fs.DirectoryEntryKind.REGULAR)
        snapshot = by_name["source"].snapshot
        assert snapshot is not None
        self.assertEqual(snapshot.byte_count, 12)
        self.assertEqual(snapshot.identity.link_count, 2)
        self.assertEqual(by_name["alias"].snapshot, snapshot)
        self.assertEqual(by_name["link"].kind, fs.DirectoryEntryKind.SYMLINK)
        self.assertEqual(by_name["link"].unavailable_reason, fs.DirectoryEntryUnavailableReason.LINK)
        self.assertEqual(by_name["pipe"].kind, fs.DirectoryEntryKind.OTHER)
        self.assertEqual(by_name["pipe"].unavailable_reason, fs.DirectoryEntryUnavailableReason.NOT_REGULAR)
        self.assertEqual(by_name["bad\\name"].unavailable_reason, fs.DirectoryEntryUnavailableReason.UNSAFE_NAME)
        self.assertBorrowedLive()
        with self.assertRaises(fs.PlatformFileError):
            self.root.observe_ledger_entries(self.lease, fs.LedgerEnumerationLimits(100, 255, 10000))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux preserves non-UTF8 names")
    def test_non_utf8_name_is_preserved_as_unavailable(self) -> None:
        name = os.fsdecode(b"unsafe-\xff")
        (self.path / name).mkdir()
        entry = next(entry for entry in self.observe() if entry.name == name)
        self.assertEqual(entry.unavailable_reason, fs.DirectoryEntryUnavailableReason.UNSAFE_NAME)

    def test_only_one_temporary_directory_handle_no_child_body_or_file_opens(self) -> None:
        (self.path / "source").write_bytes(b"private body")
        real_open, real_pread = os.open, os.pread
        reads: set[int] = set()
        temporary_fds: list[int] = []

        def pread(fd: int, size: int, offset: int) -> bytes:
            self.assertEqual(os.fstat(fd).st_ino, (self.path / "family.lock").stat().st_ino)
            reads.add(fd)
            return real_pread(fd, size, offset)

        def opened(name, flags, **kwargs):
            self.assertEqual(name, ".")
            self.assertTrue(flags & os.O_DIRECTORY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertFalse(flags & (os.O_CREAT | os.O_RDWR | os.O_WRONLY))
            descriptor = real_open(name, flags, **kwargs)
            temporary_fds.append(descriptor)
            return descriptor

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(posix.os, "open", side_effect=opened))
            stack.enter_context(mock.patch.object(posix.os, "pread", side_effect=pread))
            for name in ("read", "write", "pwrite", "dup", "mkdir", "unlink", "rename", "replace", "fsync"):
                stack.enter_context(mock.patch.object(posix.os, name, side_effect=AssertionError(name)))
            stack.enter_context(mock.patch.object(posix.fcntl, "flock", side_effect=AssertionError("flock")))
            stack.enter_context(mock.patch.object(self.backend, "bind_root", side_effect=AssertionError("fresh root")))
            entries = self.observe()
        self.assertEqual({entry.name for entry in entries}, {"source", "family.lock"})
        self.assertEqual(len(temporary_fds), 2)
        self.assertEqual(len(reads), 1)
        for descriptor in temporary_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertBorrowedLive()

    def test_foreign_issuer_root_and_lease_rejected_before_enumeration(self) -> None:
        foreign = posix.PosixPlatformAdapter()
        foreign_root = foreign.bind_root(self.path)
        self.addCleanup(foreign_root.close)
        other = self.base / "foreign"
        other.mkdir()
        foreign_parent = foreign.bind_root(other)
        self.addCleanup(foreign_parent.close)
        foreign_lease = self.acquire(foreign, foreign_parent)
        with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
            for backend, root, lease in ((foreign, self.root, self.lease), (self.backend, foreign_root, self.lease), (self.backend, self.root, foreign_lease)):
                with self.subTest(backend=backend, root=root, lease=lease):
                    self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                       lambda: backend.observe_root_children(root, lease, self.limits, self.cancel))
        self.assertBorrowedLive()

    def test_wrong_parent_and_descendant_lease_rejected(self) -> None:
        for path in (self.base / "other", self.path / "nested"):
            path.mkdir()
            root = self.backend.bind_root(path)
            self.addCleanup(root.close)
            lease = self.acquire(self.backend, root)
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                               lambda: self.backend.observe_root_children(self.root, lease, self.limits, self.cancel))
        self.assertBorrowedLive()

    def test_closed_root_or_lease_rejected_without_enumerating(self) -> None:
        self.lease.close()
        with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        self.assertFalse(self.root.closed)
        self.lease = self.acquire(self.backend, self.root)
        self.root.close()
        with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        self.assertFalse(self.lease.closed)

    def test_wrong_argument_types_rejected(self) -> None:
        parent = self.backend.bind_parent(self.root, PurePath("child"))
        self.addCleanup(parent.close)
        for values in ((self.path, self.lease, self.limits, self.cancel), (parent, self.lease, self.limits, self.cancel),
                       (self.root, object(), self.limits, self.cancel), (self.root, self.lease, object(), self.cancel),
                       (self.root, self.lease, self.limits, object())):
            with self.subTest(values=values), self.assertRaises(TypeError):
                cast(Callable[..., object], self.backend.observe_root_children)(*values)
        self.assertBorrowedLive()

    def test_cancelled_before_start_acquires_no_temporary_resource(self) -> None:
        self.cancel.cancel()
        with mock.patch.object(posix.os, "open", side_effect=AssertionError("open")):
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.observe)
        self.assertBorrowedLive()

    def test_entry_limit_counts_all_kinds_per_pass_and_allows_exact_limit(self) -> None:
        (self.path / "nested").mkdir()
        self.limits = fs.DirectoryObservationLimits(maximum_entries=2, maximum_directory_handles=1)
        self.assertEqual(len(self.observe()), 2)
        self.limits = fs.DirectoryObservationLimits(maximum_entries=1, maximum_directory_handles=1)
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, self.observe)
        self.assertBorrowedLive()

    def test_handle_budget_reserved_before_open_and_returns_to_zero(self) -> None:
        real_budget = fs.DirectoryHandleBudget
        budgets: list[fs.DirectoryHandleBudget] = []

        def budget(maximum: int) -> fs.DirectoryHandleBudget:
            value = real_budget(maximum)
            budgets.append(value)
            return value

        with mock.patch.object(posix, "DirectoryHandleBudget", side_effect=budget):
            self.observe()
        self.assertEqual([value.active_handles for value in budgets], [0])
        with mock.patch.object(posix.DirectoryHandleBudget, "reserve", side_effect=fs.PlatformFileError(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, retryable=False)), \
                mock.patch.object(posix.os, "open", side_effect=AssertionError("open")):
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, self.observe)
        self.assertBorrowedLive()

    def test_cancellation_during_or_after_either_pass_rejects_success(self) -> None:
        for number in (1, 2):
            self.cancel = fs.DirectoryObservationCancellation()
            with self.subTest(pass_number=number), self.afterPass(number, self.cancel.cancel):
                self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.observe)
            self.assertBorrowedLive()
        self.cancel = fs.DirectoryObservationCancellation()
        original_next = posix._PosixDirectoryStream.__next__

        def cancelled(stream):
            item = original_next(stream)
            self.cancel.cancel()
            return item

        with mock.patch.object(posix._PosixDirectoryStream, "__next__", cancelled):
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.observe)
        self.assertBorrowedLive()

    def test_partial_enumeration_and_duplicate_names_reject_success(self) -> None:
        original_next = posix._PosixDirectoryStream.__next__
        for mode in ("error", "duplicate", "inode"):
            seen = []

            def next_item(stream):
                if seen:
                    if mode == "error":
                        raise OSError(errno.EIO, "private body must not leak")
                    return seen[0]
                item = original_next(stream)
                seen.append(item)
                return (item[0], item[1] + 1) if mode == "inode" else item

            with self.subTest(mode=mode), mock.patch.object(posix._PosixDirectoryStream, "__next__", next_item):
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE if mode == "error" else fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
            self.assertBorrowedLive()

    def test_native_readdir_error_is_not_eof(self) -> None:
        api = posix._ObservationDirectoryAPI()

        def failed_read(_pointer):
            posix.ctypes.set_errno(errno.EIO)
            return None

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "readdir", side_effect=failed_read), \
                mock.patch.object(api, "closedir", wraps=api.closedir) as closed:
            self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, self.observe)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()

    def test_metadata_and_name_drift_between_passes_is_rejected(self) -> None:
        for kind in ("content", "mode", "names", "symlink", "special", "hardlink"):
            with self.subTest(kind=kind):
                child = self.path / kind
                if kind == "symlink":
                    child.symlink_to("missing-a")
                elif kind == "special":
                    os.mkfifo(child)
                else:
                    child.write_bytes(b"content")

                def mutate() -> None:
                    if kind == "content":
                        child.write_bytes(b"new")
                    elif kind == "mode":
                        child.chmod(0o400)
                    elif kind == "names":
                        child.rename(self.path / "renamed")
                    elif kind == "hardlink":
                        os.link(child, self.base / "outside-link")
                    else:
                        before = child.lstat()
                        retained = self.base / ("retained-" + kind)
                        # Keep the old object live outside the observed root;
                        # unlink/recreate could be an unobservable inode ABA.
                        child.rename(retained)
                        if kind == "symlink":
                            child.symlink_to("missing-b")
                        else:
                            os.mkfifo(child)
                        original = retained.lstat()
                        replacement = child.lstat()
                        self.assertEqual((original.st_dev, original.st_ino),
                                         (before.st_dev, before.st_ino))
                        self.assertNotEqual((replacement.st_dev, replacement.st_ino),
                                            (original.st_dev, original.st_ino))

                with self.afterPass(1, mutate):
                    self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
                self.assertBorrowedLive()

    def test_directory_change_during_pass_is_rejected(self) -> None:
        original_next = posix._PosixDirectoryStream.__next__
        changed = False

        def next_item(stream):
            nonlocal changed
            item = original_next(stream)
            if not changed:
                changed = True
                (self.path / "new").mkdir()
            return item

        with mock.patch.object(posix._PosixDirectoryStream, "__next__", next_item):
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
        self.assertBorrowedLive()

    def test_root_and_ancestor_replacement_before_or_at_terminal_is_rejected(self) -> None:
        for which in ("root", "ancestor"):
            for pass_number in (0, 2):
                with self.subTest(which=which, pass_number=pass_number):
                    path = self.path if which == "root" else self.base
                    moved = path.with_name(path.name + "-moved")

                    def move() -> None:
                        path.rename(moved)
                        shutil.copytree(moved, path)

                    try:
                        if pass_number == 0:
                            move()
                            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
                        else:
                            with self.afterPass(pass_number, move):
                                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
                        self.assertFalse(self.root.closed)
                        self.assertFalse(self.lease.closed)
                    finally:
                        if moved.exists():
                            shutil.rmtree(path)
                            moved.rename(path)
                    self.assertBorrowedLive()

    def test_lease_payload_change_before_or_at_terminal_is_rejected(self) -> None:
        for pass_number in (0, 2):
            def mutate() -> None:
                (self.path / "family.lock").write_bytes(b"invalid")

            with self.subTest(pass_number=pass_number):
                if pass_number == 0:
                    mutate()
                    self.assertFailure(fs.PlatformFileErrorCode.LOCK_UNAVAILABLE, self.observe)
                else:
                    with self.afterPass(pass_number, mutate):
                        self.assertFailure(fs.PlatformFileErrorCode.LOCK_UNAVAILABLE, self.observe)
                (self.path / "family.lock").write_bytes(b"family")
                self.assertBorrowedLive()

    def test_close_failure_after_either_pass_never_returns_success_or_retries(self) -> None:
        for failed_pass in (1, 2):
            api = posix._ObservationDirectoryAPI()
            real_close = api.closedir
            count = 0

            def close(pointer):
                nonlocal count
                count += 1
                result = real_close(pointer)
                return -1 if count == failed_pass else result

            with self.subTest(pass_number=failed_pass), mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                    mock.patch.object(api, "closedir", side_effect=close):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
                self.assertEqual(count, failed_pass)
            self.assertBorrowedLive()

    def test_close_failure_does_not_mask_primary_enumeration_failure(self) -> None:
        api = posix._ObservationDirectoryAPI()
        real_close = api.closedir
        primary = fs.PlatformFileError(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, retryable=False)

        def close(pointer):
            real_close(pointer)
            return -1

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "closedir", side_effect=close) as closed, \
                mock.patch.object(posix._PosixDirectoryStream, "__next__", side_effect=primary):
            self.assertIs(self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.observe), primary)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()

    def test_close_failure_still_rejects_success_inside_callers_exception_handler(self) -> None:
        api = posix._ObservationDirectoryAPI()
        real_close = api.closedir
        count = 0

        def close(pointer):
            nonlocal count
            count += 1
            result = real_close(pointer)
            return -1 if count == 2 else result

        try:
            raise RuntimeError("an unrelated caller failure")
        except RuntimeError:
            with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                    mock.patch.object(api, "closedir", side_effect=close):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        self.assertEqual(count, 2)
        self.assertBorrowedLive()

    def test_constructor_cleanup_does_not_mask_primary_cancellation(self) -> None:
        api = posix._ObservationDirectoryAPI()
        real_open, real_close = api.fdopendir, api.closedir

        def opened(descriptor):
            pointer = real_open(descriptor)
            self.cancel.cancel()
            return pointer

        def close(pointer):
            real_close(pointer)
            return -1

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "fdopendir", side_effect=opened), \
                mock.patch.object(api, "closedir", side_effect=close) as closed:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.observe)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()

    def test_native_stream_constructor_preserves_primary_when_closedir_fails(self) -> None:
        # Exercise the shared helper directly, independently of the new port.
        api = posix._ObservationDirectoryAPI()
        real_open, real_close = api.fdopendir, api.closedir
        resources = fs.DirectoryHandleBudget(1)
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)

        def opened(fd):
            pointer = real_open(fd)
            self.cancel.cancel()
            return pointer

        def close(pointer):
            real_close(pointer)
            return -1

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "fdopendir", side_effect=opened), \
                mock.patch.object(api, "closedir", side_effect=close) as closed:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                               lambda: posix._PosixDirectoryStream(descriptor, resources, self.cancel))
            self.assertEqual(closed.call_count, 1)
        self.assertEqual(resources.active_handles, 1)

    def test_native_stream_open_failure_preserves_primary_when_fd_close_fails(self) -> None:
        api = posix._ObservationDirectoryAPI()
        resources = fs.DirectoryHandleBudget(1)
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        real_close = os.close
        primary = OSError(errno.EIO, "mount observation failed")

        def close(fd: int) -> None:
            real_close(fd)
            raise OSError(errno.EBADF, "close failed")

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "check_mount", side_effect=primary), \
                mock.patch.object(posix.os, "close", side_effect=close) as closed:
            with self.assertRaises(OSError) as caught:
                posix._PosixDirectoryStream(descriptor, resources, self.cancel)
            self.assertIs(caught.exception, primary)
            self.assertEqual(closed.call_count, 1)
        self.assertEqual(resources.active_handles, 1)


if __name__ == "__main__":
    unittest.main()
