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
    CandidateContentFacts, ExistingLinkPairCompletion, LockLease, LockPolicy, LockWait,
    OpaqueAuthority, PlatformFileBackend, PlatformFileError, PlatformFileErrorCode,
    RootedDirectoryAuthority,
)

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    import platform_fs_posix as posix


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX only")
class PosixExistingLinkPairTests(unittest.TestCase):
    payload = b"existing deterministic handoff"
    expected = CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())
    action: Callable[[str], None] | None = None

    def setUp(self) -> None:
        self.root.reprove_location()
        self.lease.reprove()

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
        path = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "family"
        path.mkdir()
        return path

    @cached_property
    def adapter(self) -> posix.PosixPlatformAdapter:
        return posix.PosixPlatformAdapter(_fault_injector=self.fault)

    @cached_property
    def root(self) -> RootedDirectoryAuthority:
        return self.keep(self.adapter.bind_root(self.path))

    @cached_property
    def lease(self) -> LockLease:
        return self.keep(self.adapter.acquire(
            self.root, "family.lock", b"owner", LockPolicy(LockWait.FAIL_FAST),
        ))

    def fault(self, point: str) -> None:
        if self.action is not None:
            self.action(point)

    def pair(self) -> tuple[int, int]:
        final = self.path / "final"
        final.write_bytes(self.payload)
        os.link(final, self.path / "temp")
        result = final.stat()
        return result.st_dev, result.st_ino

    def complete(self) -> None:
        self.lease.reprove_binding(self.root, "family.lock", b"owner")
        self.adapter.complete_existing_link_pair(self.root, self.lease, "final", "temp", self.expected)

    def failure(self, code: PlatformFileErrorCode, call: Callable[[], object] | None = None) -> PlatformFileError:
        with self.assertRaises(PlatformFileError) as caught:
            (call or self.complete)()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def borrowed_alive(self) -> None:
        self.assertFalse(self.root.closed)
        self.assertFalse(self.lease.closed)
        self.root.reprove_location()
        self.lease.reprove_binding(self.root, "family.lock", b"owner")

    def assert_terminal(self, identity: tuple[int, int]) -> None:
        final = self.path / "final"
        result = final.stat()
        self.assertEqual((result.st_dev, result.st_ino), identity)
        self.assertEqual(result.st_nlink, 1)
        self.assertEqual(final.read_bytes(), self.payload)
        self.assertFalse(os.path.lexists(self.path / "temp"))
        self.borrowed_alive()

    def test_optional_contract_and_exact_pair_completion_without_copy_rename_or_relock(self) -> None:
        self.assertIsInstance(self.adapter, ExistingLinkPairCompletion)
        self.assertNotIn("complete_existing_link_pair", get_protocol_members(PlatformFileBackend))
        from platform_fs_windows import WindowsPlatformAdapter
        self.assertFalse(hasattr(WindowsPlatformAdapter, "complete_existing_link_pair"))
        identity = self.pair()
        with mock.patch.object(posix.os, "rename", side_effect=AssertionError("rename")), mock.patch.object(
            posix.os, "replace", side_effect=AssertionError("replace"),
        ), mock.patch.object(self.adapter, "bind_root", side_effect=AssertionError("fresh bind")), mock.patch.object(
            self.adapter, "acquire", side_effect=AssertionError("relock"),
        ), mock.patch.object(posix.os, "write", side_effect=AssertionError("write")), mock.patch.object(
            posix.os, "pwrite", side_effect=AssertionError("pwrite"),
        ), mock.patch.object(posix.os, "unlink", wraps=os.unlink) as unlinked, mock.patch.object(
            posix.os, "fsync", wraps=os.fsync,
        ) as synced:
            self.assertIsNone(self.complete())
        self.assertEqual(unlinked.call_count, 1)
        self.assertEqual(unlinked.call_args.args, ("temp",))
        parent_fd = unlinked.call_args.kwargs["dir_fd"]
        self.assertTrue(os.path.samestat(os.fstat(parent_fd), self.path.stat()))
        synced.assert_called_once_with(parent_fd)
        self.assert_terminal(identity)

    def test_final_only_repeated_completion_still_syncs_and_proves(self) -> None:
        identity = self.pair()
        self.complete()
        with mock.patch.object(posix.os, "unlink", side_effect=AssertionError("unlink")), mock.patch.object(
            posix.os, "fsync", wraps=os.fsync,
        ) as synced:
            self.complete()
        synced.assert_called_once()
        self.assert_terminal(identity)

    def test_pair_and_final_only_resume_with_fresh_process_root_and_w1(self) -> None:
        identity = self.pair()
        self.lease.close()
        self.root.close()
        script = '''
from pathlib import Path
import hashlib, sys
from platform_fs_contracts import CandidateContentFacts, LockPolicy, LockWait
from platform_fs_posix import PosixPlatformAdapter
adapter = PosixPlatformAdapter()
with adapter.bind_root(Path(sys.argv[1])) as root:
    with adapter.acquire(root, "family.lock", b"owner", LockPolicy(LockWait.FAIL_FAST)) as lease:
        lease.reprove_binding(root, "family.lock", b"owner")
        payload = b"existing deterministic handoff"
        adapter.complete_existing_link_pair(root, lease, "final", "temp", CandidateContentFacts(len(payload), hashlib.sha256(payload).digest()))
        root.reprove_location()
        lease.reprove()
'''
        for state in ("pair", "final-only"):
            with self.subTest(state=state):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(self.path)],
                    cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True,
                    timeout=30, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                final = (self.path / "final").stat()
                self.assertEqual((final.st_dev, final.st_ino), identity)
                self.assertEqual(final.st_nlink, 1)
                self.assertEqual((self.path / "final").read_bytes(), self.payload)
                self.assertFalse((self.path / "temp").exists())

    def test_missing_final_rejected_with_and_without_temporary(self) -> None:
        for temporary in (False, True):
            with self.subTest(temporary=temporary):
                if temporary:
                    (self.path / "temp").write_bytes(self.payload)
                self.failure(PlatformFileErrorCode.IDENTITY_STALE)
                self.assertFalse((self.path / "final").exists())
                self.assertEqual((self.path / "temp").exists(), temporary)
                self.borrowed_alive()

    def test_different_objects_even_with_same_bytes_and_third_link_are_rejected(self) -> None:
        final, temporary = self.path / "final", self.path / "temp"
        for state in ("different", "third"):
            with self.subTest(state=state):
                final.write_bytes(self.payload)
                if state == "different":
                    temporary.write_bytes(self.payload)
                else:
                    os.link(final, temporary)
                    os.link(final, self.path / "third")
                before = {p.name: p.stat().st_ino for p in (final, temporary)}
                self.failure(PlatformFileErrorCode.IDENTITY_STALE)
                self.assertEqual(before, {p.name: p.stat().st_ino for p in (final, temporary)})
                final.unlink()
                temporary.unlink()
        self.borrowed_alive()

    def test_wrong_digest_or_exact_size_preserves_pair_and_final_only(self) -> None:
        self.pair()
        for paired in (True, False):
            if not paired:
                (self.path / "temp").unlink()
            for expected in (
                CandidateContentFacts(len(self.payload), bytes(32)),
                CandidateContentFacts(len(self.payload) - 1, self.expected.content_sha256),
                CandidateContentFacts(len(self.payload) + 1, self.expected.content_sha256),
            ):
                with self.subTest(paired=paired, expected=expected):
                    self.failure(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.complete_existing_link_pair(
                        self.root, self.lease, "final", "temp", expected,
                    ))
                    self.assertEqual((self.path / "temp").exists(), paired)
                    self.assertEqual((self.path / "final").read_bytes(), self.payload)

    def test_symlink_directory_fifo_and_final_only_multiple_links_rejected(self) -> None:
        for name in ("final", "temp"):
            for shape in ("symlink", "directory", "fifo"):
                with self.subTest(name=name, shape=shape):
                    final, temporary = self.path / "final", self.path / "temp"
                    final.unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                    other = temporary if name == "final" else final
                    other.write_bytes(self.payload)
                    target = self.path / name
                    if shape == "symlink":
                        target.symlink_to(other.name)
                    elif shape == "directory":
                        target.mkdir()
                    else:
                        os.mkfifo(target)
                    self.failure(PlatformFileErrorCode.REPARSE_REJECTED)
                    self.assertEqual(other.read_bytes(), self.payload)
                    self.assertTrue(os.path.lexists(target))
                    target.rmdir() if shape == "directory" else target.unlink()
        (self.path / "temp").unlink(missing_ok=True)
        os.link(self.path / "final", self.path / "elsewhere")
        self.failure(PlatformFileErrorCode.IDENTITY_STALE)

    def test_safe_distinct_names_and_exact_input_types(self) -> None:
        self.pair()
        for name in ("", ".", "..", "../temp", "/temp", "a/b", "a\\b", "bad\0name", "final"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.adapter.complete_existing_link_pair(self.root, self.lease, "final", name, self.expected)
        for arguments in (
            (self.path, self.lease, "final", "temp", self.expected),
            (self.root, object(), "final", "temp", self.expected),
            (self.root, self.lease, 1, "temp", self.expected),
            (self.root, self.lease, "final", "temp", object()),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(TypeError):
                cast(Callable[..., None], self.adapter.complete_existing_link_pair)(*arguments)
        self.assertTrue((self.path / "temp").exists())

    def test_foreign_issuer_root_lease_and_same_path_fresh_root_are_rejected(self) -> None:
        self.pair()
        foreign = posix.PosixPlatformAdapter()
        foreign_root = self.keep(foreign.bind_root(self.path))
        foreign_lease = self.keep(foreign.acquire(foreign_root, "foreign.lock", b"other", LockPolicy(LockWait.FAIL_FAST)))
        fresh_root = self.keep(self.adapter.bind_root(self.path))
        fresh_lease = self.keep(self.adapter.acquire(fresh_root, "fresh.lock", b"other", LockPolicy(LockWait.FAIL_FAST)))
        (self.path / "descendant").mkdir()
        child = self.keep(self.adapter.bind_parent(self.root, PurePath("descendant/file")))
        child_lease = self.keep(self.adapter.acquire(child, "child.lock", b"other", LockPolicy(LockWait.FAIL_FAST)))
        for root, lease in ((foreign_root, self.lease), (self.root, foreign_lease),
                            (fresh_root, self.lease), (self.root, fresh_lease), (self.root, child_lease)):
            with self.subTest(root=root, lease=lease):
                self.failure(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.complete_existing_link_pair(
                    root, lease, "final", "temp", self.expected,
                ))
        self.failure(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: foreign.complete_existing_link_pair(
            self.root, self.lease, "final", "temp", self.expected,
        ))
        self.assertTrue((self.path / "temp").exists())
        self.borrowed_alive()

    def test_resource_lock_name_payload_remain_callers_precondition(self) -> None:
        self.pair()
        other = self.keep(self.adapter.acquire(self.root, "other.lock", b"other", LockPolicy(LockWait.FAIL_FAST)))
        self.failure(PlatformFileErrorCode.LOCK_UNAVAILABLE, lambda: other.reprove_binding(self.root, "family.lock", b"owner"))
        self.assertTrue((self.path / "temp").exists())
        self.complete()

    def test_closed_root_or_lease_rejected_without_unlink(self) -> None:
        self.pair()
        self.lease.close()
        self.failure(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE)
        self.root.close()
        self.failure(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.adapter.complete_existing_link_pair(
            self.root, self.lease, "final", "temp", self.expected,
        ))
        self.assertTrue((self.path / "temp").exists())

    def test_root_location_drift_before_call_rejects_original_and_clone(self) -> None:
        self.pair()
        moved = self.path.with_name("moved")
        self.path.rename(moved)
        shutil.copytree(moved, self.path)
        self.failure(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.complete_existing_link_pair(
            self.root, self.lease, "final", "temp", self.expected,
        ))
        self.assertTrue((moved / "temp").exists())
        self.assertTrue((self.path / "temp").exists())
        self.assertFalse(self.root.closed)
        self.assertFalse(self.lease.closed)

    def test_observable_name_or_content_drift_before_unlink_never_deletes_replacement(self) -> None:
        for change in ("final", "temp", "content", "third"):
            with self.subTest(change=change):
                self.pair()
                def fault(point: str) -> None:
                    if point == "link_pair_before_unlink":
                        if change in {"final", "temp"}:
                            target = self.path / change
                            target.rename(self.path / "original")
                            target.write_bytes(self.payload)
                        elif change == "third":
                            os.link(self.path / "final", self.path / "original")
                        else:
                            (self.path / "final").write_bytes(b"x" * len(self.payload))
                self.action = fault
                self.failure(PlatformFileErrorCode.IDENTITY_STALE)
                self.assertTrue((self.path / "temp").exists())
                for name in ("final", "temp", "original"):
                    (self.path / name).unlink(missing_ok=True)
                self.borrowed_alive()

    def test_observable_root_drift_before_unlink_preserves_names(self) -> None:
        self.pair()
        moved = self.path.with_name("moved")
        def fault(point: str) -> None:
            if point == "link_pair_before_unlink":
                self.path.rename(moved)
                shutil.copytree(moved, self.path)
        self.action = fault
        self.failure(PlatformFileErrorCode.IDENTITY_STALE)
        self.assertTrue((moved / "temp").exists())
        self.assertTrue((self.path / "temp").exists())

    def test_unlink_error_before_and_after_removal_preserves_observed_facts(self) -> None:
        real_unlink = os.unlink
        for removed in (False, True):
            with self.subTest(removed=removed):
                identity = self.pair()
                def unlink(name: str, *, dir_fd: int | None = None) -> None:
                    if removed:
                        real_unlink(name, dir_fd=dir_fd)
                    raise OSError(errno.EIO, "unlink outcome")
                with mock.patch.object(posix.os, "unlink", side_effect=unlink):
                    self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
                self.assertEqual((self.path / "temp").exists(), not removed)
                self.borrowed_alive()
                self.complete()
                self.assert_terminal(identity)
                (self.path / "final").unlink()

    def test_after_unlink_and_sync_errors_allow_same_contract_retry(self) -> None:
        for point in ("link_pair_after_unlink", "link_pair_before_sync", "link_pair_before_terminal"):
            with self.subTest(point=point):
                identity = self.pair()
                def fault(actual: str) -> None:
                    if actual == point:
                        raise OSError(errno.EIO, "operation")
                self.action = fault
                self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
                self.assert_terminal(identity)
                self.action = None
                self.complete()
                (self.path / "final").unlink()

    def test_real_fsync_failure_in_pair_and_final_only_never_succeeds(self) -> None:
        identity = self.pair()
        for state in ("pair", "final-only"):
            with self.subTest(state=state), mock.patch.object(posix.os, "fsync", side_effect=OSError(errno.EIO, "sync")):
                self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
            self.assert_terminal(identity)
        self.complete()

    def test_terminal_reproof_rejects_replaced_final_reappeared_temp_content_or_third_link(self) -> None:
        for change in ("final", "temp", "content", "third"):
            with self.subTest(change=change):
                self.pair()
                def fault(point: str) -> None:
                    if point == "link_pair_before_terminal":
                        if change == "final":
                            (self.path / "final").rename(self.path / "original")
                            (self.path / "final").write_bytes(self.payload)
                        elif change == "temp":
                            (self.path / "temp").write_bytes(self.payload)
                        elif change == "third":
                            os.link(self.path / "final", self.path / "original")
                        else:
                            (self.path / "final").write_bytes(b"x" * len(self.payload))
                self.action = fault
                self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
                self.assertTrue((self.path / "final").exists())
                self.assertEqual((self.path / "temp").exists(), change == "temp")
                for name in ("final", "temp", "original"):
                    (self.path / name).unlink(missing_ok=True)
                self.borrowed_alive()

    def test_terminal_root_drift_preserves_current_facts(self) -> None:
        identity = self.pair()
        moved = self.path.with_name("moved")
        def fault(point: str) -> None:
            if point == "link_pair_before_terminal":
                self.path.rename(moved)
                shutil.copytree(moved, self.path)
        self.action = fault
        self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
        self.assertFalse((moved / "temp").exists())
        final = (moved / "final").stat()
        self.assertEqual((final.st_dev, final.st_ino), identity)
        self.assertFalse(self.root.closed)
        self.assertFalse(self.lease.closed)

    def test_changed_w1_payload_at_mutation_boundary_preserves_pair(self) -> None:
        self.pair()
        def fault(point: str) -> None:
            if point == "link_pair_before_unlink":
                (self.path / "family.lock").write_bytes(b"other")
        self.action = fault
        self.failure(PlatformFileErrorCode.LOCK_UNAVAILABLE)
        self.assertTrue((self.path / "temp").exists())
        (self.path / "family.lock").write_bytes(b"owner")
        self.borrowed_alive()

    def test_changed_w1_at_terminal_rejects_after_removal(self) -> None:
        identity = self.pair()
        def fault(point: str) -> None:
            if point == "link_pair_before_terminal":
                (self.path / "family.lock").write_bytes(b"other")
        self.action = fault
        self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
        (self.path / "family.lock").write_bytes(b"owner")
        self.assert_terminal(identity)

    def test_final_only_rejects_new_unknown_temp_without_removing_it(self) -> None:
        self.pair()
        (self.path / "temp").unlink()
        def fault(point: str) -> None:
            if point == "link_pair_before_sync":
                (self.path / "temp").write_bytes(b"unknown")
        self.action = fault
        with mock.patch.object(posix.os, "unlink", side_effect=AssertionError("unlink")):
            self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
        self.assertEqual((self.path / "temp").read_bytes(), b"unknown")
        self.assertEqual((self.path / "final").read_bytes(), self.payload)
        self.borrowed_alive()

    def test_acquisition_failures_close_every_acquired_fd_and_preserve_primary(self) -> None:
        self.pair()
        for target in ("final", "temp"):
            for error_type in (OSError, TypeError, AssertionError):
                for close_fails in (False, True):
                    with self.subTest(target=target, error=error_type, close_fails=close_fails):
                        acquired: list[int] = []
                        closed: list[int] = []
                        real_open, real_close = os.open, os.close
                        primary = error_type("primary acquisition")
                        def opened(name: str, flags: int, *, dir_fd: int | None = None) -> int:
                            if name == target:
                                raise primary
                            descriptor = real_open(name, flags, dir_fd=dir_fd)
                            acquired.append(descriptor)
                            return descriptor
                        def close(descriptor: int) -> None:
                            closed.append(descriptor)
                            real_close(descriptor)
                            if close_fails:
                                raise OSError(errno.EIO, "secondary close")
                        with mock.patch.object(posix.os, "open", side_effect=opened), mock.patch.object(
                            posix.os, "close", side_effect=close,
                        ), self.assertRaises(Exception) as caught:
                            self.complete()
                        if error_type is OSError:
                            self.assertIsInstance(caught.exception, PlatformFileError)
                            self.assertEqual(cast(PlatformFileError, caught.exception).code, PlatformFileErrorCode.IDENTITY_STALE)
                        else:
                            self.assertIs(caught.exception, primary)
                        self.assertEqual(len(acquired), 0 if target == "final" else 1)
                        self.assertEqual(acquired, closed)
                        if close_fails and acquired:
                            cause = caught.exception.__cause__
                            self.assertIsInstance(cause, BaseExceptionGroup)
                            assert isinstance(cause, BaseExceptionGroup)
                            self.assertEqual(len(cause.exceptions), 1)
                        self.assertTrue((self.path / "temp").exists())
                        self.borrowed_alive()

    def test_exact_digest_read_failure_and_same_size_change_before_unlink_preserve_pair(self) -> None:
        self.pair()
        for change in (False, True):
            with self.subTest(change=change):
                acquired: list[int] = []
                real_open, real_read = os.open, os.pread
                def opened(name: str, flags: int, *, dir_fd: int | None = None) -> int:
                    descriptor = real_open(name, flags, dir_fd=dir_fd)
                    acquired.append(descriptor)
                    return descriptor
                def read(descriptor: int, count: int, offset: int) -> bytes:
                    if acquired and descriptor == acquired[0]:
                        if change:
                            (self.path / "final").write_bytes(b"x" * len(self.payload))
                        else:
                            raise OSError(errno.EIO, "read")
                    return real_read(descriptor, count, offset)
                with mock.patch.object(posix.os, "open", side_effect=opened), mock.patch.object(posix.os, "pread", side_effect=read):
                    self.failure(PlatformFileErrorCode.IDENTITY_STALE)
                self.assertTrue((self.path / "temp").exists())
                self.borrowed_alive()
                (self.path / "final").write_bytes(self.payload)

    def test_owned_fds_close_once_after_success_and_all_failure_boundaries(self) -> None:
        for point in (None, "link_pair_after_open_final", "link_pair_after_open_temporary", "link_pair_before_unlink", "link_pair_after_unlink"):
            with self.subTest(point=point):
                self.pair()
                acquired: list[int] = []
                closed: list[int] = []
                real_open, real_close = os.open, os.close
                primary = TypeError("primary operation")
                def opened(name: str, flags: int, *, dir_fd: int | None = None) -> int:
                    descriptor = real_open(name, flags, dir_fd=dir_fd)
                    acquired.append(descriptor)
                    return descriptor
                def close(descriptor: int) -> None:
                    closed.append(descriptor)
                    real_close(descriptor)
                def fault(actual: str) -> None:
                    if actual == point:
                        raise primary
                self.action = fault
                with mock.patch.object(posix.os, "open", side_effect=opened), mock.patch.object(posix.os, "close", side_effect=close):
                    if point is None:
                        self.complete()
                    else:
                        with self.assertRaises(TypeError) as caught:
                            self.complete()
                        self.assertIs(caught.exception, primary)
                self.assertEqual(len(acquired), 1 if point == "link_pair_after_open_final" else 2)
                self.assertCountEqual(acquired, closed)
                self.assertEqual(len(closed), len(set(closed)))
                for descriptor in acquired:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.borrowed_alive()
                for name in ("final", "temp"):
                    (self.path / name).unlink(missing_ok=True)

    def test_close_failure_is_not_swallowed_by_unrelated_callers_exception(self) -> None:
        self.pair()
        real_close = os.close
        closed: list[int] = []
        def close(descriptor: int) -> None:
            closed.append(descriptor)
            real_close(descriptor)
            raise OSError(errno.EIO, "close")
        try:
            raise ValueError("unrelated caller exception")
        except ValueError:
            with mock.patch.object(posix.os, "close", side_effect=close):
                failure = self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
        self.assertEqual(len(closed), 2)
        self.assertEqual(len(set(closed)), 2)
        self.assertIsInstance(failure.__cause__, BaseExceptionGroup)
        assert isinstance(failure.__cause__, BaseExceptionGroup)
        self.assertEqual(len(failure.__cause__.exceptions), 2)
        self.assertFalse((self.path / "temp").exists())
        self.borrowed_alive()
        self.complete()

    def test_each_final_close_failure_rejects_success_and_attempts_all_owned_files(self) -> None:
        identity = self.pair()
        for paired, fail_number in ((True, 1), (True, 2), (False, 1)):
            with self.subTest(paired=paired, fail_number=fail_number):
                if paired and not (self.path / "temp").exists():
                    os.link(self.path / "final", self.path / "temp")
                real_close = os.close
                closed: list[int] = []
                def close(descriptor: int) -> None:
                    closed.append(descriptor)
                    real_close(descriptor)
                    if len(closed) == fail_number:
                        raise OSError(errno.EIO, "close")
                with mock.patch.object(posix.os, "close", side_effect=close):
                    failure = self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED)
                self.assertEqual(len(closed), 2 if paired else 1)
                self.assertEqual(len(set(closed)), len(closed))
                cause = failure.__cause__
                assert isinstance(cause, BaseExceptionGroup)
                self.assertEqual(len(cause.exceptions), 1)
                self.assert_terminal(identity)

    def test_primary_preserved_with_all_close_errors_before_and_after_unlink(self) -> None:
        for point in ("link_pair_before_unlink", "link_pair_after_unlink"):
            with self.subTest(point=point):
                self.pair()
                primary = TypeError("primary")
                closed: list[int] = []
                real_close = os.close
                def fault(actual: str) -> None:
                    if actual == point:
                        raise primary
                def close(descriptor: int) -> None:
                    closed.append(descriptor)
                    real_close(descriptor)
                    raise OSError(errno.EIO, "secondary close")
                self.action = fault
                with mock.patch.object(posix.os, "close", side_effect=close), self.assertRaises(TypeError) as caught:
                    self.complete()
                self.assertIs(caught.exception, primary)
                self.assertIsInstance(primary.__cause__, BaseExceptionGroup)
                assert isinstance(primary.__cause__, BaseExceptionGroup)
                self.assertEqual(len(primary.__cause__.exceptions), 2)
                self.assertEqual(len(closed), 2)
                self.assertEqual(len(set(closed)), 2)
                self.assertEqual((self.path / "temp").exists(), point == "link_pair_before_unlink")
                self.borrowed_alive()
                for name in ("final", "temp"):
                    (self.path / name).unlink(missing_ok=True)

    def test_ordinary_unlink_and_existing_retirement_stay_single_link(self) -> None:
        self.pair()
        snapshot = self.root.inspect_entry("temp")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.failure(PlatformFileErrorCode.RECOVERY_REQUIRED, lambda: self.root.unlink_owned("temp", snapshot.identity))
        parent = self.keep(self.adapter.bind_retirement_source_directory(self.root, PurePath("temp")))
        self.failure(PlatformFileErrorCode.IDENTITY_STALE, lambda: self.adapter.open_existing_retirement_source(parent, self.expected))
        self.assertTrue((self.path / "final").exists())
        self.assertTrue((self.path / "temp").exists())


if __name__ == "__main__":
    unittest.main()
