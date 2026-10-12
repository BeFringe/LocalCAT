from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import cached_property
import hashlib
import os
from pathlib import Path, PurePath
import shutil
import sys
import tempfile
from typing import Callable, Iterator, Literal, TYPE_CHECKING, cast, get_protocol_members, overload
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundDirectoryAuthority, CandidateContentFacts, CandidateFile, ExistingRetirementSource,
    LockLease, LockPolicy, LockWait, MutableFileReservation, OpaqueAuthority,
    PlatformFileBackend, PlatformFileError, PlatformFileErrorCode, PosixStageHandoff, PublishMode,
    RootedDirectoryAuthority,
)

if TYPE_CHECKING or sys.platform in {"darwin", "linux"}:
    from platform_fs_posix import PosixPlatformAdapter


def facts(payload: bytes) -> CandidateContentFacts:
    return CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())


@contextmanager
def authority[T: OpaqueAuthority](value: T) -> Iterator[T]:
    with value:
        yield value


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "POSIX only")
class PosixStageHandoffTests(unittest.TestCase):
    action: Callable[[str], None] | None = None
    payload: bytes = b"sealed stage payload"
    expected: CandidateContentFacts = facts(payload)

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

    @cached_property
    def lease(self) -> LockLease:
        return self.keep(self.adapter.acquire(
            self.root, "family.lock", b"family", LockPolicy(LockWait.FAIL_FAST),
        ))

    @overload
    def source(self, *, cold: Literal[False] = False) -> MutableFileReservation: ...

    @overload
    def source(self, *, cold: Literal[True]) -> ExistingRetirementSource: ...

    def source(self, *, cold: bool = False) -> MutableFileReservation | ExistingRetirementSource:
        if cold:
            (self.path / "stage").write_bytes(self.payload)
            return self.keep(self.adapter.open_existing_stage(
                self.root, "stage", owner_lease=self.lease,
                expected_content=self.expected,
            ))
        source = self.keep(self.adapter.reserve_mutable_file(self.root, "stage"))
        (self.path / "stage").write_bytes(self.payload)
        return source

    def handoff(
        self, source: MutableFileReservation | ExistingRetirementSource,
        *, lease: LockLease | None = None, expected: CandidateContentFacts | None = None,
    ) -> CandidateFile:
        return self.adapter.handoff_stage(
            source, owner_lease=self.lease if lease is None else lease,
            expected_content=self.expected if expected is None else expected,
        )

    def publish(self, candidate: CandidateFile, original_inode: int) -> None:
        with authority(self.root.begin_publish(
            candidate, "canonical", mode=PublishMode.REPLACE_UNDER_LOCK,
            lease=self.lease,
        )) as pending:
            self.assertTrue(candidate.closed)
            self.assertEqual(pending.retained_destination().read_all(), self.payload)
            self.assertEqual(pending.preliminary_facts().content_sha256, self.expected.content_sha256)
            self.assertEqual((self.path / "canonical").stat().st_ino, original_inode)
            pending.terminal_reproof()
        self.assertTrue(pending.closed)
        self.assertFalse((self.path / "stage").exists())

    def test_fresh_same_inode_publisher_and_single_consumption(self) -> None:
        self.assertIsInstance(self.adapter, PosixStageHandoff)
        self.assertNotIn("handoff_stage", get_protocol_members(PlatformFileBackend))
        self.assertNotIn("open_existing_stage", get_protocol_members(PlatformFileBackend))
        source = self.source()
        identity = source.identity()
        inode = (self.path / "stage").stat().st_ino
        candidate = self.keep(self.handoff(source))
        self.assertTrue(source.closed)
        self.assertEqual(candidate.identity(), identity)
        source.close()
        self.assertEqual(candidate.identity(), identity)
        with self.assertRaises(PlatformFileError):
            source.identity()
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        with self.assertRaises(ValueError):
            candidate.write_all(b"replacement")
        self.publish(candidate, inode)

    def test_cold_live_existing_authority_is_not_creator_and_publishes_same_inode(self) -> None:
        source = self.source(cold=True)
        self.assertIsInstance(source, ExistingRetirementSource)
        self.assertNotIsInstance(source, MutableFileReservation)
        identity = source.reprove().snapshot.identity
        inode = (self.path / "stage").stat().st_ino
        candidate = self.keep(self.handoff(source))
        self.assertTrue(source.closed)
        source.close()
        self.assertEqual(candidate.identity(), identity)
        with self.assertRaises(PlatformFileError):
            source.reprove()
        self.publish(candidate, inode)

    def test_handoff_moves_original_handles_without_reopen_dup_or_copy(self) -> None:
        source = self.source()
        self.lease.reprove()
        with mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("unexpected reopen")), mock.patch(
            "platform_fs_posix.os.dup", side_effect=AssertionError("unexpected duplicate"),
        ):
            candidate = self.keep(self.handoff(source))
        self.assertTrue(source.closed)
        self.publish(candidate, (self.path / "stage").stat().st_ino)

    def test_path_historical_identity_and_plain_reader_are_not_handoff_authority(self) -> None:
        source = self.source()
        with self.adapter.open_regular(self.root, PurePath("stage")) as reader:
            for invalid in (self.path / "stage", source.identity(), reader):
                with self.subTest(value=type(invalid).__name__), self.assertRaises(TypeError):
                    self.handoff(cast(MutableFileReservation, invalid))
        self.assertFalse(source.closed)
        with self.assertRaises(TypeError):
            self.adapter.open_existing_stage(
                cast(BoundDirectoryAuthority, cast(object, self.path)), "stage",
                owner_lease=self.lease, expected_content=self.expected,
            )

    def test_expected_size_hash_and_same_length_rewrite_rejected_before_transfer(self) -> None:
        source = self.source()
        for expected in (facts(self.payload + b"!"), facts(b"x" * len(self.payload))):
            with self.subTest(expected=expected), self.assertRaises(PlatformFileError):
                self.handoff(source, expected=expected)
            self.assertFalse(source.closed)
        (self.path / "stage").write_bytes(b"x" * len(self.payload))
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        self.assertFalse(source.closed)
        source.identity()

    def test_existing_acquisition_requires_actual_expected_content(self) -> None:
        (self.path / "stage").write_bytes(b"x" * len(self.payload))
        with self.assertRaises(PlatformFileError):
            self.adapter.open_existing_stage(
                self.root, "stage", owner_lease=self.lease, expected_content=self.expected,
            )
        self.assertEqual((self.path / "stage").read_bytes(), b"x" * len(self.payload))

    def test_closed_source_and_closed_lease_rejected(self) -> None:
        source = self.source()
        source.close()
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        self.lease.close()
        with self.assertRaises(PlatformFileError):
            self.adapter.open_existing_stage(
                self.root, "stage", owner_lease=self.lease, expected_content=self.expected,
            )

    def test_wrong_parent_and_foreign_issuer_rejected(self) -> None:
        source = self.source()
        sibling = self.base / "sibling"
        sibling.mkdir()
        with authority(self.adapter.bind_root(sibling)) as root, authority(self.adapter.acquire(
            root, "family.lock", b"family", LockPolicy(LockWait.FAIL_FAST),
        )) as lease:
            with self.assertRaises(PlatformFileError):
                self.handoff(source, lease=lease)
            with self.assertRaises(PlatformFileError):
                self.adapter.open_existing_stage(
                    self.root, "stage", owner_lease=lease, expected_content=self.expected,
                )
        foreign = PosixPlatformAdapter()
        with self.assertRaises(PlatformFileError):
            foreign.handoff_stage(source, owner_lease=self.lease, expected_content=self.expected)
        with authority(foreign.bind_root(self.path)) as root, authority(foreign.acquire(
            root, "other.lock", b"other", LockPolicy(LockWait.FAIL_FAST),
        )) as lease:
            with self.assertRaises(PlatformFileError):
                self.handoff(source, lease=lease)
            with self.assertRaises(PlatformFileError):
                self.adapter.open_existing_stage(
                    root, "stage", owner_lease=lease, expected_content=self.expected,
                )
        self.assertFalse(source.closed)
        source.identity()

    def test_same_parent_fresh_lease_still_requires_core_resource_binding(self) -> None:
        source = self.source()
        self.lease.reprove()
        with authority(self.adapter.acquire(
            self.root, "another-resource.lock", b"another resource", LockPolicy(LockWait.FAIL_FAST),
        )) as other_lease:
            # The Core borrow must make this check with its expected W1 facts.
            with self.assertRaises(PlatformFileError):
                other_lease.reprove_binding(self.root, "family.lock", b"family")
            # A creator reservation has no business resource ID. The platform
            # handoff only authenticates the same rooted parent and live lease.
            candidate = self.keep(self.handoff(source, lease=other_lease))
            self.assertTrue(source.closed)
            self.assertEqual(candidate.identity().kind, "regular")

    def test_cold_same_parent_different_live_lock_is_rejected(self) -> None:
        source = self.source(cold=True)
        with authority(self.adapter.acquire(
            self.root, "another-resource.lock", b"another resource", LockPolicy(LockWait.FAIL_FAST),
        )) as other_lease:
            with self.assertRaises(PlatformFileError):
                self.handoff(source, lease=other_lease)
        self.assertFalse(source.closed)
        source.reprove()

    def test_existing_acquisition_rejects_links_and_special_files(self) -> None:
        outside = self.base / "outside"
        outside.write_bytes(self.payload)
        stage = self.path / "stage"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    stage.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, stage)
                else:
                    os.mkfifo(stage)
                try:
                    with self.assertRaises(PlatformFileError):
                        self.adapter.open_existing_stage(
                            self.root, "stage", owner_lease=self.lease,
                            expected_content=self.expected,
                        )
                finally:
                    stage.unlink()
        self.assertEqual(outside.read_bytes(), self.payload)

    def test_fabricated_existing_authority_is_not_accepted(self) -> None:
        source = self.source(cold=True)
        actual = source.reprove()

        class FabricatedSource(ExistingRetirementSource):
            def _reprove_source(self):
                return actual

            def _close_authority(self) -> None:
                pass

        with authority(FabricatedSource(actual.snapshot.identity, self.expected)) as fake:
            with self.assertRaises(PlatformFileError):
                self.handoff(fake)
            self.assertFalse(fake.closed)
        self.assertFalse(source.closed)

    def test_cold_authority_requires_original_live_lease(self) -> None:
        source = self.source(cold=True)
        self.lease.close()
        with authority(self.adapter.acquire(
            self.root, "family.lock", b"family", LockPolicy(LockWait.FAIL_FAST),
        )) as replacement:
            with self.assertRaises(PlatformFileError):
                self.handoff(source, lease=replacement)
        self.assertFalse(source.closed)

    def test_existing_source_is_bound_to_original_expected_content(self) -> None:
        source = self.source(cold=True)
        changed = b"x" * len(self.payload)
        (self.path / "stage").write_bytes(changed)
        with self.assertRaises(PlatformFileError):
            self.handoff(source, expected=facts(changed))
        self.assertFalse(source.closed)

    def test_name_swap_link_and_symlink_reject_without_consuming_creator(self) -> None:
        source = self.source()
        original = self.path / "moved"
        (self.path / "stage").rename(original)
        (self.path / "stage").write_bytes(self.payload)
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        (self.path / "stage").unlink()
        (self.path / "stage").symlink_to(original)
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        (self.path / "stage").unlink()
        original.rename(self.path / "stage")
        os.link(self.path / "stage", self.path / "alias")
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        self.assertFalse(source.closed)

    def test_root_clone_rejected_without_consuming_creator(self) -> None:
        source = self.source()
        moved = self.base / "moved"
        self.path.rename(moved)
        shutil.copytree(moved, self.path)
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        self.assertFalse(source.closed)
        self.assertEqual((moved / "stage").read_bytes(), self.payload)

    def test_nested_parent_clone_rejected_without_consuming_creator(self) -> None:
        (self.path / "nested").mkdir()
        with authority(self.adapter.bind_parent(self.root, PurePath("nested/stage"))) as parent:
            with authority(self.adapter.acquire(parent, "family.lock", b"nested", LockPolicy(LockWait.FAIL_FAST))) as lease:
                with authority(self.adapter.reserve_mutable_file(parent, "stage")) as source:
                    (self.path / "nested/stage").write_bytes(self.payload)
                    (self.path / "nested").rename(self.path / "moved")
                    shutil.copytree(self.path / "moved", self.path / "nested")
                    with self.assertRaises(PlatformFileError):
                        self.handoff(source, lease=lease)
                    self.assertFalse(source.closed)

    def test_sync_failure_keeps_source_owner_and_successful_retry(self) -> None:
        source = self.source()
        with mock.patch("platform_fs_posix.os.fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(PlatformFileError):
                self.handoff(source)
        self.assertFalse(source.closed)
        source.identity()
        candidate = self.keep(self.handoff(source))
        self.publish(candidate, (self.path / "stage").stat().st_ino)

    def test_change_after_sync_rechecked_against_original_expected(self) -> None:
        source = self.source()

        def change(point: str) -> None:
            if point == "stage_after_sync":
                (self.path / "stage").write_bytes(b"x" * len(self.payload))

        self.action = change
        with self.assertRaises(PlatformFileError):
            self.handoff(source)
        self.assertFalse(source.closed)

    def test_pretransfer_failure_preserves_owner(self) -> None:
        source = self.source(cold=True)

        def fail(point: str) -> None:
            if point == "stage_before_transfer":
                raise RuntimeError("injected before transfer")

        self.action = fail
        with self.assertRaises(RuntimeError):
            self.handoff(source)
        self.assertFalse(source.closed)
        source.reprove()

    def test_posttransfer_failure_closes_candidate_once_and_keeps_bytes(self) -> None:
        source = self.source()
        closes: list[CandidateFile] = []
        close = CandidateFile.close

        def observe_close(candidate: CandidateFile) -> None:
            closes.append(candidate)
            close(candidate)

        def fail(point: str) -> None:
            if point == "stage_after_transfer":
                raise RuntimeError("injected after transfer")

        self.action = fail
        with mock.patch.object(CandidateFile, "close", new=observe_close):
            with self.assertRaises(RuntimeError):
                self.handoff(source)
        self.assertTrue(source.closed)
        self.assertEqual(len(closes), 1)
        self.assertTrue(closes[0].closed)
        source.close()
        self.assertEqual((self.path / "stage").read_bytes(), self.payload)
        self.lease.reprove()
        self.root.reprove()

    def test_publication_failure_consumes_candidate_and_retains_named_inode(self) -> None:
        source = self.source(cold=True)
        inode = (self.path / "stage").stat().st_ino
        candidate = self.handoff(source)

        def fail(point: str) -> None:
            if point == "candidate_after_naming":
                raise RuntimeError("injected naming failure")

        self.action = fail
        with self.assertRaises(PlatformFileError) as error:
            self.root.begin_publish(candidate, "canonical", mode=PublishMode.REPLACE_UNDER_LOCK, lease=self.lease)
        self.assertEqual(error.exception.code, PlatformFileErrorCode.RECOVERY_REQUIRED.value)
        self.assertTrue(candidate.closed)
        self.assertTrue(source.closed)
        self.assertEqual((self.path / "canonical").stat().st_ino, inode)
        self.assertEqual((self.path / "canonical").read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
