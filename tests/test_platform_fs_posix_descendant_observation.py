"""原 root/W1 下严格后代目录的完整闭集、漂移与关闭责任。"""

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
class PosixDescendantObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        _ = self.lease, self.descendant

    @cached_property
    def base(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    @cached_property
    def path(self) -> Path:
        path = self.base / "root"
        (path / "quarantine" / "batch").mkdir(parents=True)
        return path

    @cached_property
    def child_path(self) -> Path:
        return self.path / "quarantine" / "batch"

    @cached_property
    def backend(self) -> posix.PosixPlatformAdapter:
        return posix.PosixPlatformAdapter()

    @cached_property
    def root(self) -> fs.RootedDirectoryAuthority:
        root = self.backend.bind_root(self.path)
        self.addCleanup(root.close)
        return root

    @cached_property
    def descendant(self) -> fs.BoundDirectoryAuthority:
        return self.bind(self.backend, self.root, "quarantine/batch/item")

    @cached_property
    def lease(self) -> fs.LockLease:
        return self.acquire(self.backend, self.root)

    @cached_property
    def limits(self) -> fs.LedgerEnumerationLimits:
        return fs.LedgerEnumerationLimits(100, 255, 10000)

    def bind(self, backend: posix.PosixPlatformAdapter, root: fs.RootedDirectoryAuthority,
             relative: str) -> fs.BoundDirectoryAuthority:
        parent = backend.bind_parent(root, PurePath(relative))
        self.addCleanup(parent.close)
        return parent

    def acquire(self, backend: posix.PosixPlatformAdapter, parent: fs.BoundDirectoryAuthority) -> fs.LockLease:
        lease = backend.acquire(parent, "family.lock", b"family", fs.LockPolicy(fs.LockWait.FAIL_FAST))
        self.addCleanup(lease.close)
        return lease

    def observe(self) -> tuple[fs.LedgerEntryObservation, ...]:
        return self.backend.observe_descendant_entries(self.root, self.lease, self.descendant, self.limits)

    def assertFailure(self, code: fs.PlatformFileErrorCode, call: Callable[[], object]) -> fs.PlatformFileError:
        with self.assertRaises(fs.PlatformFileError) as caught:
            call()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)
        return caught.exception

    def assertBorrowedLive(self) -> None:
        for authority in (self.root, self.descendant, self.lease):
            self.assertFalse(authority.closed)
        self.root.reprove_location()
        self.descendant.reprove()
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

    def test_optional_capability_and_empty_multilevel_descendant(self) -> None:
        self.assertIsInstance(self.backend, fs.LockedDescendantNamespaceInspection)
        self.assertNotIsInstance(object(), fs.LockedDescendantNamespaceInspection)
        self.assertNotIn("observe_descendant_entries", get_protocol_members(fs.PlatformFileBackend))
        self.assertEqual(self.observe(), ())
        self.assertBorrowedLive()

    def test_complete_sorted_regular_snapshot_includes_unknown_file(self) -> None:
        for name, body in (("z.expected", b"last"), ("a.foreign", b"unknown"), ("零", b"")):
            (self.child_path / name).write_bytes(body)
        # Ancestor files and its sibling directories are outside this one-level closure.
        (self.path / "quarantine" / "outside").write_bytes(b"outside")
        (self.path / "quarantine" / "sibling").mkdir()
        result = self.observe()
        self.assertEqual([entry.name for entry in result], ["a.foreign", "z.expected", "零"])
        self.assertEqual([entry.snapshot.byte_count for entry in result], [7, 4, 0])
        for entry in result:
            self.assertEqual(entry.snapshot, self.descendant.inspect_entry(entry.name))
            self.assertEqual(entry.snapshot.identity.kind, "regular")
            self.assertEqual(entry.snapshot.identity.link_count, 1)
        self.assertBorrowedLive()

    def test_limits_allow_exact_entries_utf8_name_bytes_and_total_bytes(self) -> None:
        (self.child_path / "一").write_bytes(b"12")
        (self.child_path / "a").write_bytes(b"3")
        self.limits = fs.LedgerEnumerationLimits(2, 3, 3)
        self.assertEqual(len(self.observe()), 2)
        for limits in (fs.LedgerEnumerationLimits(1, 3, 3), fs.LedgerEnumerationLimits(2, 2, 3),
                       fs.LedgerEnumerationLimits(2, 3, 2)):
            self.limits = limits
            with self.subTest(limits=limits):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        self.assertBorrowedLive()

    def test_zero_byte_limit_accepts_empty_files(self) -> None:
        (self.child_path / "empty").touch()
        self.limits = fs.LedgerEnumerationLimits(1, 5, 0)
        self.assertEqual(len(self.observe()), 1)

    def test_links_directories_fifo_and_hardlinks_are_not_filtered(self) -> None:
        for kind in ("symlink", "directory", "fifo", "hardlink"):
            entry = self.child_path / kind
            if kind == "symlink":
                entry.symlink_to("missing")
            elif kind == "directory":
                entry.mkdir()
                (entry / "nested").write_bytes(b"not recursively listed")
            elif kind == "fifo":
                os.mkfifo(entry)
            else:
                entry.write_bytes(b"hard")
                os.link(entry, self.base / "outside-link")
            with self.subTest(kind=kind):
                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE if kind == "hardlink"
                                   else fs.PlatformFileErrorCode.REPARSE_REJECTED, self.observe)
            if kind == "directory":
                shutil.rmtree(entry)
            else:
                entry.unlink()
            self.assertBorrowedLive()

    def test_unsafe_and_non_utf8_names_are_stable_failure(self) -> None:
        (self.child_path / "bad\\name").touch()
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        (self.child_path / "bad\\name").unlink()
        if sys.platform == "linux":
            # Linux accepts this actual bytes name; the port must reject it.
            (self.child_path / "bad\udcff").touch()
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        else:
            # macOS refuses creation, so inject at the native stream boundary.
            with mock.patch.object(posix._PosixDirectoryStream, "__next__", return_value=("bad\udcff", 1)):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)

    def test_no_fresh_binding_second_lock_body_read_or_namespace_mutation(self) -> None:
        (self.child_path / "entry").write_bytes(b"body must stay unread")
        original_open, original_pread = os.open, os.pread
        owned: list[int] = []
        lock_inode = (self.path / "family.lock").stat().st_ino

        def opened(name, flags, **kwargs):
            self.assertEqual(name, ".")
            self.assertTrue(flags & os.O_DIRECTORY)
            self.assertTrue(flags & os.O_NOFOLLOW)
            descriptor = original_open(name, flags, **kwargs)
            owned.append(descriptor)
            return descriptor

        def read(fd: int, size: int, offset: int) -> bytes:
            self.assertEqual(os.fstat(fd).st_ino, lock_inode)
            return original_pread(fd, size, offset)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(posix.os, "open", side_effect=opened))
            stack.enter_context(mock.patch.object(posix.os, "pread", side_effect=read))
            for name in ("read", "write", "pwrite", "dup", "mkdir", "unlink", "rename", "replace", "fsync"):
                stack.enter_context(mock.patch.object(posix.os, name, side_effect=AssertionError(name)))
            stack.enter_context(mock.patch.object(posix.fcntl, "flock", side_effect=AssertionError("lock")))
            stack.enter_context(mock.patch.object(self.backend, "bind_root", side_effect=AssertionError("root")))
            self.assertEqual([entry.name for entry in self.observe()], ["entry"])
        self.assertEqual(len(owned), 2)
        for fd in owned:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertBorrowedLive()

    def test_same_root_and_retirement_authorities_do_not_gain_enumeration(self) -> None:
        same = self.bind(self.backend, self.root, "item")
        with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
            for descendant in (self.root, same):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                   lambda: self.backend.observe_descendant_entries(self.root, self.lease, descendant, self.limits))
        retirement = self.backend.bind_or_create_child_directory(self.root, "quarantine")
        self.addCleanup(retirement.close)
        with self.assertRaises(TypeError):
            cast(Callable[..., object], self.backend.observe_descendant_entries)(self.root, self.lease, retirement, self.limits)
        self.assertBorrowedLive()

    def test_foreign_issuer_and_root_origin_rejected_before_enumeration(self) -> None:
        foreign = posix.PosixPlatformAdapter()
        other_root = foreign.bind_root(self.path)
        fresh = self.backend.bind_root(self.path)
        self.addCleanup(other_root.close)
        self.addCleanup(fresh.close)
        other_child = self.bind(foreign, other_root, "quarantine/batch/item")
        fresh_child = self.bind(self.backend, fresh, "quarantine/batch/item")
        with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
            for backend, root, child in ((foreign, self.root, self.descendant), (self.backend, other_root, self.descendant),
                                         (self.backend, self.root, other_child), (self.backend, fresh, self.descendant),
                                         (self.backend, self.root, fresh_child), (self.backend, fresh, fresh_child)):
                with self.subTest(root=root, child=child):
                    self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                       lambda: backend.observe_descendant_entries(root, self.lease, child, self.limits))
        self.assertBorrowedLive()

    def test_wrong_root_descendant_and_foreign_leases_rejected(self) -> None:
        other_path = self.base / "other"
        other_path.mkdir()
        for backend, parent in ((self.backend, self.descendant), (self.backend, self.backend.bind_root(other_path)),
                                (posix.PosixPlatformAdapter(), self.root)):
            if parent is not self.root and parent is not self.descendant:
                self.addCleanup(parent.close)
            # A separate lock name avoids contending with the borrowed resource W1.
            lease = backend.acquire(parent, "other.lock", b"other", fs.LockPolicy(fs.LockWait.FAIL_FAST))
            self.addCleanup(lease.close)
            with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                   lambda: self.backend.observe_descendant_entries(self.root, lease, self.descendant, self.limits))
        self.assertBorrowedLive()

    def test_exact_parent_ledger_still_rejects_ancestor_lock(self) -> None:
        self.assertFailure(fs.PlatformFileErrorCode.LOCK_UNAVAILABLE,
                           lambda: self.descendant.observe_ledger_entries(self.lease, self.limits))
        self.assertEqual(self.observe(), ())
        self.assertBorrowedLive()

    def test_closed_borrowed_objects_and_wrong_types_rejected(self) -> None:
        call = cast(Callable[..., object], self.backend.observe_descendant_entries)
        for values in ((self.path, self.lease, self.descendant, self.limits),
                       (self.root, object(), self.descendant, self.limits),
                       (self.root, self.lease, self.child_path, self.limits),
                       (self.root, self.lease, self.descendant, object())):
            with self.subTest(values=values), self.assertRaises(TypeError):
                call(*values)
        for authority in (self.descendant, self.lease, self.root):
            authority.close()
            with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=AssertionError("enumeration")):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)

    def test_partial_duplicate_and_stale_dirent_enumerations_are_rejected(self) -> None:
        (self.child_path / "entry").touch()
        original_next = posix._PosixDirectoryStream.__next__
        for mode in ("partial", "duplicate", "inode"):
            seen: list[tuple[str, int]] = []

            def next_item(stream):
                if seen:
                    if mode == "partial":
                        raise OSError(errno.EIO, "private partial enumeration")
                    return seen[0]
                item = original_next(stream)
                seen.append(item)
                return (item[0], item[1] + 1) if mode == "inode" else item

            with self.subTest(mode=mode), mock.patch.object(posix._PosixDirectoryStream, "__next__", next_item):
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE if mode == "partial"
                                   else fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
            self.assertBorrowedLive()

    def test_native_readdir_error_not_mistaken_for_eof(self) -> None:
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

    def test_changed_name_size_or_object_between_passes_rejected(self) -> None:
        entry = self.child_path / "entry"
        for mode in ("extra", "size", "replacement"):
            entry.write_bytes(b"before")

            def mutate() -> None:
                if mode == "extra":
                    (self.child_path / "extra").touch()
                elif mode == "size":
                    entry.write_bytes(b"changed-size")
                else:
                    entry.rename(self.base / "retained-object")
                    entry.write_bytes(b"before")
                    self.assertNotEqual(entry.stat().st_ino, (self.base / "retained-object").stat().st_ino)

            with self.subTest(mode=mode), self.afterPass(1, mutate):
                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
            for file in self.child_path.iterdir():
                file.unlink()
            self.assertBorrowedLive()

    def test_mid_pass_directory_generation_change_rejected(self) -> None:
        (self.child_path / "entry").touch()
        original_next = posix._PosixDirectoryStream.__next__
        changed = False

        def next_item(stream):
            nonlocal changed
            item = original_next(stream)
            if not changed:
                changed = True
                (self.child_path / "extra").touch()
            return item

        with mock.patch.object(posix._PosixDirectoryStream, "__next__", next_item):
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
        self.assertBorrowedLive()

    def test_stat_failure_cannot_return_partial_success(self) -> None:
        (self.child_path / "entry").touch()
        original_stat = os.stat

        def failed(name, *args, **kwargs):
            if name == "entry":
                raise OSError(errno.EIO, "private metadata")
            return original_stat(name, *args, **kwargs)

        with mock.patch.object(posix.os, "stat", side_effect=failed):
            self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, self.observe)
        self.assertBorrowedLive()

    def test_original_root_and_each_descendant_chain_position_reproved(self) -> None:
        for path in (self.path, self.path / "quarantine", self.child_path):
            for pass_number in (0, 2):
                moved = path.with_name(path.name + "-moved")

                def replace() -> None:
                    path.rename(moved)
                    shutil.copytree(moved, path)

                try:
                    with self.subTest(path=path.name, pass_number=pass_number):
                        if pass_number == 0:
                            replace()
                            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
                        else:
                            with self.afterPass(pass_number, replace):
                                self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
                        self.assertFalse(self.root.closed)
                        self.assertFalse(self.lease.closed)
                        self.assertFalse(self.descendant.closed)
                finally:
                    if moved.exists():
                        shutil.rmtree(path)
                        moved.rename(path)
                self.assertBorrowedLive()

    def test_terminal_lock_payload_and_named_object_drift_rejected(self) -> None:
        lock = self.path / "family.lock"
        for mode in ("payload", "object"):
            def mutate() -> None:
                if mode == "payload":
                    lock.write_bytes(b"invalid")
                else:
                    lock.rename(self.path / "lock-moved")
                    lock.write_bytes(b"family")
                    lock.chmod(0o600)

            try:
                with self.subTest(mode=mode), self.afterPass(2, mutate):
                    self.assertFailure(fs.PlatformFileErrorCode.LOCK_UNAVAILABLE, self.observe)
            finally:
                if mode == "object" and (self.path / "lock-moved").exists():
                    lock.unlink()
                    (self.path / "lock-moved").rename(lock)
                elif mode == "payload":
                    lock.write_bytes(b"family")
            self.assertBorrowedLive()

    def test_stream_open_mount_and_fdopendir_failure_cleanup(self) -> None:
        for phase in ("open", "mount", "fdopendir"):
            api = posix._ObservationDirectoryAPI()
            target, name = (posix.os, "open") if phase == "open" else (api, "check_mount" if phase == "mount" else "fdopendir")
            failure = OSError(errno.EIO, "private acquisition")
            with self.subTest(phase=phase), mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                    mock.patch.object(target, name, side_effect=failure), \
                    mock.patch.object(posix.os, "close", wraps=os.close) as closed:
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, self.observe)
                self.assertEqual(closed.call_count, 0 if phase == "open" else 1)
            self.assertBorrowedLive()

    def test_close_failure_on_either_pass_never_succeeds_or_retries(self) -> None:
        for number in (1, 2):
            api = posix._ObservationDirectoryAPI()
            original_close = api.closedir
            count = 0

            def close(pointer):
                nonlocal count
                count += 1
                result = original_close(pointer)
                return -1 if count == number else result

            with self.subTest(number=number), mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                    mock.patch.object(api, "closedir", side_effect=close):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
            self.assertEqual(count, number)
            self.assertBorrowedLive()

    def test_primary_and_stream_close_errors_both_remain_observable(self) -> None:
        api = posix._ObservationDirectoryAPI()
        original_close = api.closedir
        primary = fs.PlatformFileError(fs.PlatformFileErrorCode.IDENTITY_STALE, retryable=True)

        def close(pointer):
            original_close(pointer)
            return -1

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "closedir", side_effect=close) as closed, \
                mock.patch.object(posix._PosixDirectoryStream, "__next__", side_effect=primary):
            error = self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
            self.assertIs(error, primary)
            self.assertIsInstance(error.__cause__, BaseExceptionGroup)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()

    def test_acquisition_and_fd_close_errors_both_remain_observable(self) -> None:
        api = posix._ObservationDirectoryAPI()
        original_close = os.close
        primary = fs.PlatformFileError(fs.PlatformFileErrorCode.IDENTITY_STALE, retryable=True)

        def close(fd: int) -> None:
            original_close(fd)
            raise OSError(errno.EIO, "close")

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "check_mount", side_effect=primary), \
                mock.patch.object(posix.os, "close", side_effect=close) as closed:
            error = self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, self.observe)
            self.assertIs(error, primary)
            self.assertIsInstance(error.__cause__, BaseExceptionGroup)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()

    def test_close_failure_inside_ambient_except_still_rejects_success(self) -> None:
        api = posix._ObservationDirectoryAPI()
        original_close = api.closedir

        def close(pointer):
            original_close(pointer)
            return -1

        try:
            raise RuntimeError("unrelated caller error")
        except RuntimeError:
            with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                    mock.patch.object(api, "closedir", side_effect=close):
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.observe)
        self.assertBorrowedLive()

    def test_programmer_error_is_not_rewritten_and_resources_close(self) -> None:
        primary = AssertionError("injected programmer failure")
        with mock.patch.object(posix._PosixDirectoryStream, "__next__", side_effect=primary), \
                mock.patch.object(posix._PosixDirectoryStream, "close", autospec=True,
                                  side_effect=posix._PosixDirectoryStream.close) as closed:
            with self.assertRaises(AssertionError) as caught:
                self.observe()
            self.assertIs(caught.exception, primary)
            self.assertEqual(closed.call_count, 1)
        self.assertBorrowedLive()


if __name__ == "__main__":
    unittest.main()
