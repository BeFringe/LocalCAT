from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
from functools import cached_property
import os
from pathlib import Path, PurePath
import shutil
import sqlite3
import sys
import tempfile
from typing import Callable, Iterator, TYPE_CHECKING, cast, get_protocol_members
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundExistingFileMutationGuard, ExistingFileMutationGuard,
    OpaqueAuthority, PlatformFileBackend, PlatformFileError, PlatformFileErrorCode,
    PublishMode, RootedDirectoryAuthority,
)

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    from platform_fs_posix import PosixPlatformAdapter


@contextmanager
def authority[T: OpaqueAuthority](value: T) -> Iterator[T]:
    with value:
        yield value


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX only")
class PosixMutationGuardTests(unittest.TestCase):
    action: Callable[[str], None] | None = None

    @cached_property
    def stack(self) -> ExitStack:
        stack = ExitStack()
        self.addCleanup(stack.close)
        return stack

    def keep[T: OpaqueAuthority](self, value: T) -> T:
        return self.stack.enter_context(authority(value))

    @cached_property
    def base(self) -> Path:
        return Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    @cached_property
    def path(self) -> Path:
        path = self.base / "family"
        path.mkdir()
        return path

    @cached_property
    def adapter(self) -> PosixPlatformAdapter:
        def fault(point: str) -> None:
            if self.action is not None:
                self.action(point)
        return PosixPlatformAdapter(_fault_injector=fault)

    @cached_property
    def root(self) -> RootedDirectoryAuthority:
        return self.keep(self.adapter.bind_root(self.path))

    def guard(self, relative: str = "database") -> BoundExistingFileMutationGuard:
        return self.keep(self.adapter.guard_existing_for_mutation(self.root, PurePath(relative)))

    def test_pending_live_identity_sql_updates_and_final_capture(self) -> None:
        seed = self.base / "seed"
        with sqlite3.connect(seed) as initial:
            initial.execute("CREATE TABLE example(value TEXT)")
            initial.execute("INSERT INTO example VALUES ('before')")
        initial.close()
        payload = seed.read_bytes()
        candidate = self.keep(self.root.create_candidate("stage", private=False))
        candidate.write_all(payload)
        candidate.flush_content()
        pending = self.keep(self.root.begin_publish(
            candidate, "database", mode=PublishMode.CREATE_IF_ABSENT, lease=None,
        ))
        reader = pending.retained_destination()
        guard = self.guard()
        self.assertFalse(reader.closed)
        self.assertEqual(guard.reprove(), reader.identity())
        preliminary = pending.preliminary_facts()
        pending.terminal_reproof()
        pending.close()
        self.assertTrue(reader.closed)
        self.assertFalse(guard.closed)
        before = (self.path / "database").stat()
        # Native SQLite calls remain real; only Python platform FD operations
        # are forbidden for the entire connection/statement/close interval.
        with mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("SQL-time open")), mock.patch(
            "platform_fs_posix.os.dup", side_effect=AssertionError("SQL-time dup"),
        ), mock.patch("platform_fs_posix.os.close", side_effect=AssertionError("SQL-time close")), mock.patch(
            "platform_fs_posix.os.pread", side_effect=AssertionError("SQL-time content read"),
        ), mock.patch("platform_fs_posix.os.fsync", side_effect=AssertionError("SQL-time fsync")):
            connection = sqlite3.connect(self.path / "database")
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE example SET value = ?", ("changed" * 5000,))
                self.assertEqual(guard.reprove(), preliminary.destination_identity)
                connection.commit()
                self.assertEqual(connection.execute("SELECT length(value) FROM example").fetchone(), (35000,))
                guard.reprove()
            finally:
                connection.close()
            guard.reprove()
        after = (self.path / "database").stat()
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertNotEqual(before.st_size, after.st_size)
        self.assertNotEqual(before.st_mtime_ns, after.st_mtime_ns)
        with authority(self.adapter.open_regular(self.root, PurePath("database"))) as final:
            capture = self.adapter.observe_bound_content(final)
            self.assertEqual(final.identity(), guard.reprove())
            self.assertNotEqual(capture.content_sha256, preliminary.content_sha256)
        guard.close()
        self.root.reprove_location()

    def test_guard_is_narrow_independent_and_close_is_one_way(self) -> None:
        (self.path / "database").write_bytes(b"before")
        guard = self.guard()
        self.assertIsInstance(self.adapter, ExistingFileMutationGuard)
        self.assertNotIn("guard_existing_for_mutation", get_protocol_members(PlatformFileBackend))
        self.root.close()
        (self.path / "database").write_bytes(b"legal changed bytes and size")
        guard.reprove()
        for operation in ("read_all", "write_all", "flush_content", "begin_publish", "detach", "rearm"):
            self.assertFalse(hasattr(guard, operation))
        guard.close()
        with mock.patch("platform_fs_posix.os.close", side_effect=AssertionError("second close")):
            guard.close()
        with self.assertRaises(PlatformFileError) as caught:
            guard.reprove()
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value)

    def test_leaf_drift_rejected_even_for_same_bytes_clone(self) -> None:
        for change in ("same", "different", "symlink", "directory", "fifo", "hardlink", "missing"):
            with self.subTest(change=change):
                parent = self.path / change
                parent.mkdir()
                target = parent / "database"
                target.write_bytes(b"original")
                guard = self.guard(f"{change}/database")
                if change == "hardlink":
                    os.link(target, parent / "alias")
                else:
                    target.rename(parent / "original")
                    if change in {"same", "different"}:
                        target.write_bytes(b"original" if change == "same" else b"replacement")
                    elif change == "symlink":
                        target.symlink_to(parent / "original")
                    elif change == "directory":
                        target.mkdir()
                    elif change == "fifo":
                        os.mkfifo(target)
                with self.assertRaises(PlatformFileError):
                    guard.reprove()
                guard.close()

    def test_root_parent_and_ancestor_drift_rejected(self) -> None:
        for scope in ("root", "parent", "ancestor", "parent_symlink"):
            with self.subTest(scope=scope):
                ancestor = self.base / scope
                parent = ancestor / "root" / "parent"
                parent.mkdir(parents=True)
                (parent / "database").write_bytes(b"original")
                root = self.keep(self.adapter.bind_root(ancestor / "root"))
                guard = self.keep(self.adapter.guard_existing_for_mutation(root, PurePath("parent/database")))
                moved = ancestor if scope == "ancestor" else ancestor / "root" if scope == "root" else parent
                old = moved.with_name(moved.name + "-old")
                moved.rename(old)
                if scope == "parent_symlink":
                    moved.symlink_to(old, target_is_directory=True)
                else:
                    shutil.copytree(old, moved)
                with self.assertRaises(PlatformFileError):
                    guard.reprove()
                guard.close()

    def test_acquire_rejects_nonregular_links_missing_and_foreign_or_closed_root(self) -> None:
        ordinary = self.path / "ordinary"
        ordinary.write_bytes(b"original")
        (self.path / "symlink").symlink_to(ordinary)
        os.link(ordinary, self.path / "hardlink")
        (self.path / "directory").mkdir()
        os.mkfifo(self.path / "fifo")
        for name in ("symlink", "hardlink", "directory", "fifo", "missing"):
            with self.subTest(name=name), self.assertRaises(PlatformFileError):
                self.guard(name)
        other = PosixPlatformAdapter()
        with mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("foreign open")), mock.patch(
            "platform_fs_posix.os.dup", side_effect=AssertionError("foreign dup"),
        ):
            with self.assertRaises(PlatformFileError):
                other.guard_existing_for_mutation(self.root, PurePath("ordinary"))
            with self.assertRaises(TypeError):
                self.adapter.guard_existing_for_mutation(cast(RootedDirectoryAuthority, cast(object, self.path)), PurePath("ordinary"))
        self.root.close()
        with self.assertRaises(PlatformFileError):
            self.guard("ordinary")

    def test_acquisition_owns_one_file_open_and_closes_all_acquired_fds(self) -> None:
        (self.path / "database").write_bytes(b"original")
        self.root.reprove_location()
        with tracked_fds() as (opened, closed):
            guard = self.adapter.guard_existing_for_mutation(self.root, PurePath("database"))
            self.assertEqual(sum(kind == "open" for kind, _ in opened), 1)
            self.assertFalse(closed)
            with mock.patch("platform_fs_posix.os.stat", wraps=os.stat) as observed:
                guard.reprove()
            self.assertTrue(observed.call_args_list)
            for call in observed.call_args_list:
                self.assertFalse(call.kwargs["follow_symlinks"])
                self.assertIsInstance(call.kwargs["dir_fd"], int)
            guard.close()
        self.assertCountEqual([fd for _, fd in opened], closed)
        self.root.reprove_location()

    def test_acquire_failure_cleans_all_owned_fds_and_preserves_programmer_error(self) -> None:
        (self.path / "database").write_bytes(b"original")
        self.root.reprove_location()
        for point in ("mutation_guard_after_open", "mutation_guard_before_return"):
            for exception in (OSError(errno.EIO, "acquire"), TypeError("programmer")):
                with self.subTest(point=point, exception=type(exception).__name__):
                    def fail(actual: str) -> None:
                        if actual == point:
                            raise exception
                    self.action = fail
                    with tracked_fds() as (opened, closed):
                        with self.assertRaises(PlatformFileError if isinstance(exception, OSError) else TypeError):
                            self.adapter.guard_existing_for_mutation(self.root, PurePath("database"))
                    self.assertCountEqual([fd for _, fd in opened], closed)
        self.action = None
        self.root.reprove_location()

    def test_partial_acquisition_failures_release_all_successful_opens(self) -> None:
        nested = self.path / "nested"
        nested.mkdir()
        (nested / "database").write_bytes(b"original")
        self.root.reprove_location()
        for operation in ("open", "dup", "set_inheritable"):
            with self.subTest(operation=operation), tracked_fds() as (opened, closed):
                original = getattr(os, operation)
                calls = 0
                def fail_second(*args: object, **kwargs: object) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError(errno.EIO, "partial acquire")
                    return original(*args, **kwargs)
                with mock.patch(f"platform_fs_posix.os.{operation}", side_effect=fail_second):
                    with self.assertRaises(PlatformFileError):
                        self.adapter.guard_existing_for_mutation(self.root, PurePath("nested/database"))
            self.assertTrue(opened)
            self.assertCountEqual([fd for _, fd in opened], closed)
        self.root.reprove_location()

    def test_contract_final_reproof_failure_closes_guard(self) -> None:
        (self.path / "database").write_bytes(b"original")
        self.root.reprove_location()
        def drift(point: str) -> None:
            if point == "mutation_guard_before_return":
                (self.path / "database").rename(self.path / "old")
                (self.path / "database").write_bytes(b"original")
        self.action = drift
        with tracked_fds() as (opened, closed), self.assertRaises(PlatformFileError):
            self.adapter.guard_existing_for_mutation(self.root, PurePath("database"))
        self.assertCountEqual([fd for _, fd in opened], closed)

    def test_acquire_and_public_reproof_keep_primary_error_when_cleanup_fails(self) -> None:
        self.root.reprove_location()
        for phase in ("acquire", "public_reproof"):
            with self.subTest(phase=phase):
                (self.path / "database").write_bytes(b"original")
                def fail(point: str) -> None:
                    if phase == "acquire" and point == "mutation_guard_after_open":
                        raise TypeError("primary programmer error")
                    if phase == "public_reproof" and point == "mutation_guard_before_return":
                        (self.path / "database").rename(self.path / "old")
                        (self.path / "database").write_bytes(b"original")
                self.action = fail
                with tracked_fds() as (opened, closed):
                    close = os.close
                    def close_then_fail(descriptor: int) -> None:
                        close(descriptor)
                        if descriptor == opened[-1][1]:
                            raise OSError(errno.EIO, "close")
                    with mock.patch("platform_fs_posix.os.close", side_effect=close_then_fail):
                        with self.assertRaises(TypeError if phase == "acquire" else PlatformFileError) as caught:
                            self.adapter.guard_existing_for_mutation(self.root, PurePath("database"))
                    cleanup = caught.exception.__cause__
                    self.assertIsInstance(cleanup, PlatformFileError)
                    self.assertEqual(cast(PlatformFileError, cleanup).code, PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value)
                    if phase == "public_reproof":
                        self.assertEqual(cast(PlatformFileError, caught.exception).code, PlatformFileErrorCode.IDENTITY_STALE.value)
                self.assertCountEqual([fd for _, fd in opened], closed)
        self.action = None

    def test_close_fault_releases_remaining_fds_and_never_retries(self) -> None:
        (self.path / "database").write_bytes(b"original")
        self.root.reprove_location()
        for failure in (OSError(errno.EIO, "close"), TypeError("programmer close")):
            with self.subTest(failure=type(failure).__name__), tracked_fds() as (opened, closed):
                guard = self.adapter.guard_existing_for_mutation(self.root, PurePath("database"))
                close = os.close
                def close_then_fail(descriptor: int) -> None:
                    close(descriptor)
                    if descriptor == opened[-1][1]:
                        raise failure
                with mock.patch("platform_fs_posix.os.close", side_effect=close_then_fail):
                    with self.assertRaises(PlatformFileError if isinstance(failure, OSError) else TypeError):
                        guard.close()
                self.assertTrue(guard.closed)
                count = len(closed)
                guard.close()
                self.assertEqual(len(closed), count)
            self.assertCountEqual([fd for _, fd in opened], closed)
        self.root.reprove_location()


@contextmanager
def tracked_fds() -> Iterator[tuple[list[tuple[str, int]], list[int]]]:
    opened: list[tuple[str, int]] = []
    closed: list[int] = []
    real_open, real_dup, real_close = os.open, os.dup, os.close
    def open_fd(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(("open", descriptor))
        return descriptor
    def dup_fd(descriptor: int) -> int:
        result = real_dup(descriptor)
        opened.append(("dup", result))
        return result
    def close_fd(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)
    with mock.patch("platform_fs_posix.os.open", side_effect=open_fd), mock.patch(
        "platform_fs_posix.os.dup", side_effect=dup_fd,
    ), mock.patch("platform_fs_posix.os.close", side_effect=close_fd):
        yield opened, closed


if __name__ == "__main__":
    unittest.main()
