from __future__ import annotations

from contextlib import ExitStack
import errno
from functools import cached_property
import hashlib
import os
from pathlib import Path, PurePath
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, TYPE_CHECKING, cast, get_protocol_members
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundDirectoryAuthority, CandidateContentFacts, ExistingFileRetirement,
    ExistingRetirementSource, LockLease, LockPolicy, LockWait, MutableFileReservation,
    OpaqueAuthority, PlatformFileBackend, PlatformFileError, PlatformFileErrorCode,
    RetainedRetirement, RetirementDirectoryAuthority, RetirementSourceDirectoryAuthority,
    RootedDirectoryAuthority,
)

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    from platform_fs_posix import PosixPlatformAdapter


def content(payload: bytes) -> CandidateContentFacts:
    return CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX only")
class PosixExistingRetirementTests(unittest.TestCase):
    payload: bytes = b"existing recovery artifact"
    action: Callable[[str], None] | None = None

    @cached_property
    def stack(self) -> ExitStack:
        stack = ExitStack()
        self.addCleanup(stack.close)
        return stack

    def keep[T: OpaqueAuthority](self, value: T) -> T:
        self.stack.callback(value.close)
        return value

    @cached_property
    def path(self) -> Path:
        base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        path = base / "family"
        path.mkdir()
        (path / "source").mkdir()
        (path / "quarantine").mkdir()
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

    @cached_property
    def target(self) -> RetirementDirectoryAuthority:
        return self.keep(self.adapter.bind_or_create_child_directory(self.root, "quarantine"))

    @cached_property
    def parent(self) -> RetirementSourceDirectoryAuthority:
        return self.keep(self.adapter.bind_retirement_source_directory(self.root, PurePath("source/item")))

    @cached_property
    def ordinary(self) -> BoundDirectoryAuthority:
        return self.keep(self.adapter.bind_parent(self.root, PurePath("source/item")))

    @cached_property
    def lease(self) -> LockLease:
        return self.keep(self.adapter.acquire(self.root, "family.lock", b"owner", LockPolicy(LockWait.FAIL_FAST)))

    def source(self) -> ExistingRetirementSource:
        (self.path / "source/item").write_bytes(self.payload)
        return self.keep(self.adapter.open_existing_retirement_source(self.parent, content(self.payload)))

    def retire(self, source: ExistingRetirementSource) -> RetainedRetirement:
        return self.adapter.retire_existing_exclusive(self.parent, source, self.target, "item")

    def rebind(self) -> RetainedRetirement:
        return self.adapter.rebind_existing_retirement(
            self.ordinary, "item", self.target, "item", content(self.payload),
        )

    def assert_error(self, code: PlatformFileErrorCode, call: Callable[[], object]) -> None:
        with self.assertRaises(PlatformFileError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_optional_port_restricted_source_and_same_object_transfer(self) -> None:
        self.assertIsInstance(self.adapter, ExistingFileRetirement)
        self.assertNotIn("retire_existing_exclusive", get_protocol_members(PlatformFileBackend))
        source = self.source()
        self.assertNotIsInstance(source, MutableFileReservation)
        self.assertNotIsInstance(self.parent, BoundDirectoryAuthority)
        for name in ("inspect_entry", "begin_publish", "observe_ledger_entries"):
            self.assertFalse(hasattr(self.parent, name))
        identity = source.reprove().snapshot.identity
        self.lease.reprove()
        result = self.keep(self.retire(source))
        self.assertTrue(source.closed)
        source.close()
        self.assertEqual(result.reprove().identity, identity)
        self.assertFalse((self.path / "source/item").exists())
        self.assertEqual((self.path / "quarantine/item").read_bytes(), self.payload)
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.retire(source))
        result.close()
        self.assertFalse(self.root.closed)
        self.assertFalse(self.parent.closed)
        self.assertFalse(self.target.closed)
        self.assertFalse(self.lease.closed)
        self.root.reprove_location()
        self.lease.reprove()

    def test_root_level_source_uses_original_root(self) -> None:
        (self.path / "item").write_bytes(self.payload)
        parent = self.keep(self.adapter.bind_retirement_source_directory(self.root, PurePath("item")))
        source = self.keep(self.adapter.open_existing_retirement_source(parent, content(self.payload)))
        retained = self.keep(self.adapter.retire_existing_exclusive(parent, source, self.target, "item"))
        retained.reprove()
        self.assertFalse((self.path / "item").exists())

    def test_source_content_size_hash_shape_and_missing_rejected(self) -> None:
        for payload in (b"", self.payload + b"!", b"x" * len(self.payload)):
            with self.subTest(payload=payload):
                (self.path / "source/item").write_bytes(payload)
                self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.open_existing_retirement_source(self.parent, content(self.payload)))
        (self.path / "source/item").unlink()
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.open_existing_retirement_source(self.parent, content(self.payload)))
        for shape in ("symlink", "directory", "fifo", "hardlink"):
            with self.subTest(shape=shape):
                item = self.path / "source/item"
                if shape == "symlink":
                    item.symlink_to("../foreign")
                elif shape == "directory":
                    item.mkdir()
                elif shape == "fifo":
                    os.mkfifo(item)
                else:
                    item.write_bytes(self.payload)
                    os.link(item, self.path / "link")
                with self.assertRaises(PlatformFileError):
                    self.adapter.open_existing_retirement_source(self.parent, content(self.payload))
                if shape == "directory":
                    item.rmdir()
                else:
                    item.unlink()

    def test_same_bytes_different_live_object_and_same_inode_changed_content_rejected(self) -> None:
        source = self.source()
        item = self.path / "source/item"
        item.rename(self.path / "kept-original")
        item.write_bytes(self.payload)
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, source.reprove)
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.retire(source))
        item.unlink()
        (self.path / "kept-original").rename(item)
        item.write_bytes(b"x" * len(self.payload))
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.retire(source))
        self.assertFalse(source.closed)
        self.assertFalse((self.path / "quarantine/item").exists())

    def test_foreign_issuer_exact_root_and_source_parent_are_required(self) -> None:
        source = self.source()
        foreign = PosixPlatformAdapter()
        foreign_root = self.keep(foreign.bind_root(self.path))
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.bind_retirement_source_directory(foreign_root, PurePath("source/item")))
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: foreign.open_existing_retirement_source(self.parent, content(self.payload)))
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: foreign.retire_existing_exclusive(self.parent, source, self.target, "item"))
        for root in (foreign_root, self.keep(self.adapter.bind_root(self.path)), self.keep(self.adapter.bind_root(self.path / "source"))):
            with self.subTest(root=root):
                target_adapter = foreign if root is foreign_root else self.adapter
                # Bind an existing directory through each root; no new namespace is needed.
                target = self.keep(target_adapter.bind_or_create_child_directory(root, "quarantine"))
                self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.retire_existing_exclusive(self.parent, source, target, "item"))
                self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.rebind_existing_retirement(self.ordinary, "item", target, "item", content(self.payload)))
        wrong_parent = self.keep(self.adapter.bind_retirement_source_directory(self.root, PurePath("source/other")))
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.retire_existing_exclusive(wrong_parent, source, self.target, "item"))
        self.assertFalse(source.closed)

    def test_creator_stage_path_and_reader_cannot_be_existing_retirement_source(self) -> None:
        source = self.source()
        reservation = self.keep(self.adapter.reserve_mutable_file(self.root, "created"))
        reader = self.keep(self.adapter.open_regular(self.root, PurePath("source/item")))
        for invalid in (reservation, reader, self.path / "source/item", source.reprove().snapshot.identity):
            with self.subTest(kind=type(invalid)), self.assertRaises(TypeError):
                self.retire(cast(ExistingRetirementSource, cast(object, invalid)))
        stage = self.keep(self.adapter.open_existing_stage(self.root, "created", owner_lease=self.lease, expected_content=content(b"")))
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.retire(stage))
        with self.assertRaises(TypeError):
            self.adapter.retire_owned_exclusive(self.ordinary, "item", cast(MutableFileReservation, cast(object, source)), self.target, "item")

    def test_root_and_source_parent_location_drift_pre_arm_rejected(self) -> None:
        source = self.source()
        self.target.reprove()
        moved = self.path.with_name("moved-family")
        self.path.rename(moved)
        shutil.copytree(moved, self.path)
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.retire(source))
        self.assertEqual((moved / "source/item").read_bytes(), self.payload)
        self.assertEqual((self.path / "source/item").read_bytes(), self.payload)
        self.assertFalse((moved / "quarantine/item").exists())

    def test_source_parent_link_and_target_parent_swap_rejected(self) -> None:
        source = self.source()
        self.target.reprove()
        original = self.path / "source"
        original.rename(self.path / "kept-source")
        original.symlink_to("kept-source", target_is_directory=True)
        self.assert_error(PlatformFileErrorCode.REPARSE_REJECTED, lambda: self.retire(source))
        original.unlink()
        (self.path / "kept-source").rename(original)
        (self.path / "quarantine").rename(self.path / "kept-target")
        (self.path / "quarantine").mkdir()
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.retire(source))
        self.assertFalse((self.path / "quarantine/item").exists())

    def test_existing_target_never_overwritten(self) -> None:
        source = self.source()
        (self.path / "quarantine/item").write_bytes(b"foreign")
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
        self.assertEqual((self.path / "quarantine/item").read_bytes(), b"foreign")
        self.assertEqual((self.path / "source/item").read_bytes(), self.payload)
        self.assertFalse(source.closed)

    def test_target_appearing_at_arm_is_not_clobbered(self) -> None:
        source = self.source()
        def fault(point: str) -> None:
            if point == "existing_retirement_before_arm":
                (self.path / "quarantine/item").write_bytes(b"racer")
        self.action = fault
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
        self.assertEqual((self.path / "quarantine/item").read_bytes(), b"racer")
        self.assertEqual((self.path / "source/item").read_bytes(), self.payload)

    def test_pre_arm_and_post_arm_faults_preserve_facts_and_ownership(self) -> None:
        source = self.source()
        def before(point: str) -> None:
            if point == "existing_retirement_before_arm":
                raise OSError("before")
        self.action = before
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.retire(source))
        self.assertFalse(source.closed)
        source.reprove()
        def after(point: str) -> None:
            if point == "existing_retirement_after_arm":
                raise OSError("after")
        self.action = after
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
        self.assertFalse(source.closed)
        self.assertFalse((self.path / "source/item").exists())
        self.assertEqual((self.path / "quarantine/item").read_bytes(), self.payload)
        self.action = None
        source.close()
        self.keep(self.rebind()).reprove()

    def test_programmer_error_after_arm_is_not_normalized(self) -> None:
        source = self.source()
        def fault(point: str) -> None:
            if point == "existing_retirement_after_arm":
                raise TypeError("programmer")
        self.action = fault
        with self.assertRaisesRegex(TypeError, "programmer"):
            self.retire(source)
        self.assertFalse(source.closed)
        self.assertTrue((self.path / "quarantine/item").exists())

    def test_rename_failure_and_each_directory_fsync_failure_are_recovery_required(self) -> None:
        source = self.source()
        self.target.reprove()
        with mock.patch("platform_fs_posix._rename_owned_exclusive_at", side_effect=OSError("rename")):
            self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
        self.assertFalse(source.closed)
        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call):
                calls = 0
                real_fsync = os.fsync
                def fsync(fd: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failing_call:
                        raise OSError("sync")
                    real_fsync(fd)
                with mock.patch("platform_fs_posix.os.fsync", side_effect=fsync):
                    self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
                self.assertFalse(source.closed)
                (self.path / "quarantine/item").rename(self.path / "source/item")

    def test_terminal_content_mutation_after_move_is_rejected(self) -> None:
        source = self.source()
        def fault(point: str) -> None:
            if point == "existing_retirement_before_terminal":
                (self.path / "quarantine/item").write_bytes(b"x" * len(self.payload))
        self.action = fault
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.retire(source))
        self.assertFalse(source.closed)

    def test_retained_reproof_checks_content_identity_source_and_root(self) -> None:
        source = self.source()
        retained = self.keep(self.retire(source))
        target = self.path / "quarantine/item"
        target.write_bytes(b"x" * len(self.payload))
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.reprove)
        target.write_bytes(self.payload)
        retained.reprove()
        target.rename(self.path / "kept-original")
        target.write_bytes(self.payload)
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.reprove)
        target.unlink()
        (self.path / "kept-original").rename(target)
        (self.path / "source/item").write_bytes(self.payload)
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.reprove)
        (self.path / "source/item").unlink()
        self.path.rename(self.path.with_name("kept-root"))
        self.path.mkdir()
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.reprove)

    def test_fresh_source_target_state_matrix_and_expected_content(self) -> None:
        for has_source, has_target in ((True, True), (True, False), (False, False), (False, True)):
            with self.subTest(source=has_source, target=has_target):
                source, target = self.path / "source/item", self.path / "quarantine/item"
                for path, present in ((source, has_source), (target, has_target)):
                    path.unlink(missing_ok=True)
                    if present:
                        path.write_bytes(self.payload)
                if not has_source and has_target:
                    retained = self.keep(self.rebind())
                    retained.reprove()
                    target.write_bytes(b"x" * len(self.payload))
                    self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.reprove)
                    self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, self.rebind)
                else:
                    self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, self.rebind)
                self.assertEqual(source.exists(), has_source)
                self.assertEqual(target.exists(), has_target)

    def test_fresh_target_link_or_special_file_rejected_without_changes(self) -> None:
        for shape in ("symlink", "directory", "fifo", "hardlink"):
            with self.subTest(shape=shape):
                target = self.path / "quarantine/item"
                if shape == "symlink":
                    target.symlink_to("../foreign")
                elif shape == "directory":
                    target.mkdir()
                elif shape == "fifo":
                    os.mkfifo(target)
                else:
                    target.write_bytes(self.payload)
                    os.link(target, self.path / "alias")
                self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, self.rebind)
                if shape == "directory":
                    target.rmdir()
                else:
                    target.unlink()

    def test_fresh_root_drift_is_rejected(self) -> None:
        (self.path / "quarantine/item").write_bytes(self.payload)
        self.ordinary.reprove()
        self.target.reprove()
        self.path.rename(self.path.with_name("old-family"))
        shutil.copytree(self.path.with_name("old-family"), self.path)
        self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, self.rebind)

    def test_no_plain_rename_copy_reopen_source_or_duplicate_file_on_transfer(self) -> None:
        source = self.source()
        self.target.reprove()
        with mock.patch("platform_fs_posix.os.rename", side_effect=AssertionError("plain rename")), mock.patch(
            "platform_fs_posix.os.replace", side_effect=AssertionError("replace"),
        ), mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("file reopen")):
            retained = self.keep(self.retire(source))
        self.assertTrue(source.closed)
        retained.reprove()

    def test_close_failure_attempts_all_fds_once_and_does_not_close_borrowed_root(self) -> None:
        source = self.source()
        retained = self.keep(self.retire(source))
        self.lease.reprove()
        real_close = os.close
        closed: list[int] = []
        def close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)
            if len(closed) == 1:
                raise OSError("close")
        with mock.patch("platform_fs_posix.os.close", side_effect=close):
            self.assert_error(PlatformFileErrorCode.RECOVERY_REQUIRED, retained.close)
            retained.close()
            source.close()
        self.assertGreater(len(closed), 2)
        self.assertEqual(len(closed), len(set(closed)))
        self.assertTrue(retained.closed)
        self.root.reprove_location()
        self.lease.reprove()

    def test_primary_error_survives_retained_close_error(self) -> None:
        retained = self.keep(self.retire(self.source()))
        real_close = os.close
        def close(fd: int) -> None:
            real_close(fd)
            raise OSError("close")
        with mock.patch("platform_fs_posix.os.close", side_effect=close), self.assertRaisesRegex(ValueError, "primary"):
            with retained:
                raise ValueError("primary")
        self.assertTrue(retained.closed)
        self.root.reprove_location()

    def test_acquisition_and_fresh_terminal_failures_close_all_new_fds_preserving_primary(self) -> None:
        self.parent.reprove()
        self.ordinary.reprove()
        self.target.reprove()
        source_path = self.path / "source/item"
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                source_path.unlink(missing_ok=True)
                (self.path / "quarantine/item" if fresh else source_path).write_bytes(self.payload)
                acquired: list[int] = []
                closed: list[int] = []
                real_open, real_dup, real_close = os.open, os.dup, os.close

                def opened(path: str, flags: int, *, dir_fd: int | None = None) -> int:
                    descriptor = real_open(path, flags, dir_fd=dir_fd)
                    acquired.append(descriptor)
                    return descriptor

                def duplicated(descriptor: int) -> int:
                    result = real_dup(descriptor)
                    acquired.append(result)
                    return result

                def closed_with_error(descriptor: int) -> None:
                    closed.append(descriptor)
                    real_close(descriptor)
                    raise OSError("secondary close")

                def fault(point: str) -> None:
                    expected = "existing_retirement_rebind_before_terminal" if fresh else "existing_retirement_source_before_return"
                    if point == expected:
                        raise TypeError("primary terminal")

                self.action = fault
                with mock.patch("platform_fs_posix.os.open", side_effect=opened), mock.patch(
                    "platform_fs_posix.os.dup", side_effect=duplicated,
                ), mock.patch("platform_fs_posix.os.close", side_effect=closed_with_error):
                    with self.assertRaisesRegex(TypeError, "primary terminal"):
                        if fresh:
                            self.rebind()
                        else:
                            self.adapter.open_existing_retirement_source(self.parent, content(self.payload))
                self.assertGreater(len(acquired), 2)
                self.assertCountEqual(acquired, closed)
                self.assertEqual(len(closed), len(set(closed)))
                self.root.reprove_location()
                self.parent.reprove()
                self.target.reprove()
        self.action = None

    def test_source_parent_acquisition_failures_close_every_new_fd_and_preserve_primary(self) -> None:
        self.root.reprove_location()
        before = sorted(str(item.relative_to(self.path)) for item in self.path.rglob("*"))
        for point in ("open", "fstat", "stat"):
            for error_type in (OSError, TypeError, AssertionError, AttributeError):
                for close_error_type in (None, OSError, TypeError):
                    with self.subTest(point=point, error=error_type, close_error=close_error_type):
                        primary = OSError(errno.EIO, "primary acquisition") if error_type is OSError else error_type("primary acquisition")
                        acquired: list[int] = []
                        closed: list[int] = []
                        opened_child: int | None = None
                        real_open, real_dup = os.open, os.dup
                        real_fstat, real_stat, real_close = os.fstat, os.stat, os.close

                        def opened(name: str, flags: int, *, dir_fd: int | None = None) -> int:
                            nonlocal opened_child
                            if point == "open":
                                raise primary
                            descriptor = real_open(name, flags, dir_fd=dir_fd)
                            acquired.append(descriptor)
                            opened_child = descriptor
                            return descriptor

                        def duplicated(descriptor: int) -> int:
                            result = real_dup(descriptor)
                            acquired.append(result)
                            return result

                        def fstat(descriptor: int) -> os.stat_result:
                            if point == "fstat" and descriptor == opened_child:
                                raise primary
                            return real_fstat(descriptor)

                        def stat(name: str, *, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
                            if point == "stat" and opened_child is not None and name == "source":
                                raise primary
                            return real_stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

                        def close(descriptor: int) -> None:
                            closed.append(descriptor)
                            real_close(descriptor)
                            if close_error_type is not None:
                                raise close_error_type("secondary close")

                        try:
                            with mock.patch("platform_fs_posix.os.open", side_effect=opened), mock.patch(
                                "platform_fs_posix.os.dup", side_effect=duplicated,
                            ), mock.patch("platform_fs_posix.os.fstat", side_effect=fstat), mock.patch(
                                "platform_fs_posix.os.stat", side_effect=stat,
                            ), mock.patch("platform_fs_posix.os.close", side_effect=close):
                                with self.assertRaises(Exception) as caught:
                                    self.adapter.bind_retirement_source_directory(self.root, PurePath("source/item"))
                            if error_type is OSError:
                                self.assertIsInstance(caught.exception, PlatformFileError)
                                expected = PlatformFileErrorCode.IDENTITY_STALE if point == "stat" else PlatformFileErrorCode.OUTSIDE_ROOT
                                self.assertEqual(cast(PlatformFileError, caught.exception).code, expected)
                            else:
                                self.assertIs(caught.exception, primary)
                            self.assertGreater(len(acquired), 1)
                            self.assertCountEqual(acquired, closed)
                            self.assertEqual(len(closed), len(set(closed)))
                            for descriptor in acquired:
                                with self.assertRaises(OSError) as missing:
                                    real_fstat(descriptor)
                                self.assertEqual(missing.exception.errno, errno.EBADF)
                            self.root.reprove_location()
                            self.assertEqual(before, sorted(str(item.relative_to(self.path)) for item in self.path.rglob("*")))
                        finally:
                            # RED must not leak its deliberately exposed failures into
                            # another scenario, nor retry any close already attempted.
                            for descriptor in set(acquired) - set(closed):
                                real_close(descriptor)

    def test_source_and_parent_close_failure_preserves_context_error_and_borrowed_root(self) -> None:
        source = self.source()
        for authority in (source, self.parent):
            with self.subTest(authority=type(authority)):
                real_close = os.close
                closed: list[int] = []

                def close(descriptor: int) -> None:
                    closed.append(descriptor)
                    real_close(descriptor)
                    raise OSError("secondary close")

                with mock.patch("platform_fs_posix.os.close", side_effect=close), self.assertRaisesRegex(ValueError, "primary"):
                    with authority:
                        raise ValueError("primary")
                self.assertGreater(len(closed), 2)
                self.assertEqual(len(closed), len(set(closed)))
                self.assertTrue(authority.closed)
                authority.close()
                self.root.reprove_location()

    def test_observable_source_swap_immediately_before_arm_does_not_move_replacement(self) -> None:
        source = self.source()
        def fault(point: str) -> None:
            if point == "existing_retirement_before_arm":
                (self.path / "source/item").rename(self.path / "kept-original")
                (self.path / "source/item").write_bytes(self.payload)
        self.action = fault
        self.assert_error(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.retire(source))
        self.assertEqual((self.path / "source/item").read_bytes(), self.payload)
        self.assertFalse((self.path / "quarantine/item").exists())
        self.assertFalse(source.closed)

    def test_arm_terminal_failure_keeps_source_owner_and_closes_new_target_fds(self) -> None:
        source = self.source()
        self.target.reprove()
        real_dup, real_close = os.dup, os.close
        acquired: list[int] = []
        closed: list[int] = []

        def duplicate(descriptor: int) -> int:
            result = real_dup(descriptor)
            acquired.append(result)
            return result

        def close(descriptor: int) -> None:
            closed.append(descriptor)
            real_close(descriptor)
            raise OSError("secondary close")

        def fault(point: str) -> None:
            if point == "existing_retirement_before_terminal":
                raise TypeError("primary terminal")
        self.action = fault
        with mock.patch("platform_fs_posix.os.dup", side_effect=duplicate), mock.patch(
            "platform_fs_posix.os.close", side_effect=close,
        ), self.assertRaisesRegex(TypeError, "primary terminal"):
            self.retire(source)
        self.assertCountEqual(acquired, closed)
        self.assertGreater(len(closed), 2)
        self.assertFalse(source.closed)
        source.close()
        self.action = None
        self.keep(self.rebind()).reprove()

    def test_closed_inputs_and_unsafe_relative_names_fail_without_mutation(self) -> None:
        source = self.source()
        self.target.reprove()
        for relative in (PurePath("../item"), PurePath("/item")):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.adapter.bind_retirement_source_directory(self.root, relative)
        self.parent.close()
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.retire(source))
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.open_existing_retirement_source(self.parent, content(self.payload)))
        self.target.close()
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.rebind)
        self.root.close()
        self.assert_error(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.bind_retirement_source_directory(self.root, PurePath("item")))
        self.assertEqual((self.path / "source/item").read_bytes(), self.payload)
        self.assertFalse((self.path / "quarantine/item").exists())

    def test_partial_retirement_resumes_in_fresh_process_through_public_ports(self) -> None:
        for name in ("one", "two"):
            (self.path / "source" / name).write_bytes(self.payload)
        script = '''
from contextlib import ExitStack
import hashlib
from pathlib import Path, PurePath
import sys
from platform_fs_contracts import CandidateContentFacts, LockPolicy, LockWait
from platform_fs_posix import PosixPlatformAdapter
path, phase = Path(sys.argv[1]), sys.argv[2]
expected = CandidateContentFacts(len(b"existing recovery artifact"), hashlib.sha256(b"existing recovery artifact").digest())
adapter = PosixPlatformAdapter()
with ExitStack() as stack:
    root = stack.enter_context(adapter.bind_root(path))
    lease = stack.enter_context(adapter.acquire(root, "family.lock", b"owner", LockPolicy(LockWait.FAIL_FAST)))
    target = stack.enter_context(adapter.bind_or_create_child_directory(root, "quarantine"))
    for name in (("one",) if phase == "partial" else ("one", "two")):
        if phase == "resume" and name == "one":
            parent = stack.enter_context(adapter.bind_parent(root, PurePath("source") / name))
            retired = stack.enter_context(adapter.rebind_existing_retirement(parent, name, target, name, expected))
        else:
            parent = stack.enter_context(adapter.bind_retirement_source_directory(root, PurePath("source") / name))
            source = stack.enter_context(adapter.open_existing_retirement_source(parent, expected))
            retired = stack.enter_context(adapter.retire_existing_exclusive(parent, source, target, name))
        retired.reprove()
    lease.reprove()
    root.reprove_location()
'''
        for phase in ("partial", "resume"):
            result = subprocess.run(
                [sys.executable, "-B", "-c", script, str(self.path), phase],
                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True,
                timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("one", "two"):
            self.assertFalse((self.path / "source" / name).exists())
            self.assertEqual((self.path / "quarantine" / name).read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
