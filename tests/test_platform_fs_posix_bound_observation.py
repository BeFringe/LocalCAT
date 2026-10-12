"""Task 11.2: real retained-file observations for the optional POSIX codec port."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, fields, replace
import errno
from functools import cached_property
import hashlib
import os
from pathlib import Path, PurePosixPath
import pickle
import tempfile
from typing import Callable, TYPE_CHECKING, cast, get_protocol_members
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundRegularFile,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PosixBoundContentFacts,
    PosixBoundContentObservation,
    PublishMode,
    RootedDirectoryAuthority,
)

if TYPE_CHECKING:
    from platform_fs_posix import PosixPlatformAdapter


REPOSITORY = Path(__file__).resolve().parents[1]


class PosixBoundObservationContractTests(unittest.TestCase):
    def test_facts_are_strict_live_only_data_without_authority(self) -> None:
        facts = PosixBoundContentFacts(0, 7, 0, hashlib.sha256(b"").digest())
        self.assertEqual(
            [field.name for field in fields(facts)],
            ["device", "inode", "byte_count", "content_sha256"],
        )
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(operation=operation.__name__), self.assertRaises(TypeError):
                operation(facts)
        for field in ("device", "inode", "byte_count"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                replace(facts, **{field: True})
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(facts, **{field: -1})
        with self.assertRaises(TypeError):
            replace(facts, content_sha256=bytearray(32))
        with self.assertRaises(ValueError):
            replace(facts, content_sha256=b"short")

    def test_port_is_optional_and_platform_has_no_tm_dependency(self) -> None:
        self.assertNotIn("observe_bound_content", get_protocol_members(PlatformFileBackend))
        for filename in ("platform_fs_contracts.py", "platform_fs_posix.py"):
            tree = ast.parse((REPOSITORY / filename).read_text(encoding="utf-8"))
            imports = [
                name
                for node in ast.walk(tree)
                for name in (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                )
            ]
            self.assertFalse([name for name in imports if name.startswith("tm_")])
        windows_tree = ast.parse((REPOSITORY / "platform_fs_windows.py").read_text(encoding="utf-8"))
        self.assertFalse([
            node for node in ast.walk(windows_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "observe_bound_content"
        ])


@dataclass(frozen=True)
class _Fixture:
    base: Path
    configured: Path
    path: Path
    payload: bytes
    adapter: PosixPlatformAdapter
    root: RootedDirectoryAuthority
    bound: BoundRegularFile


@unittest.skipUnless(os.name == "posix", "requires POSIX retained-file operations")
class PosixBoundObservationRuntimeTests(unittest.TestCase):
    @cached_property
    def fixture(self) -> _Fixture:
        from platform_fs_posix import PosixPlatformAdapter

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        configured = base / "family"
        configured.mkdir()
        (configured / "data").mkdir()
        path = configured / "data" / "source.bin"
        payload = bytes(range(256)) * 700
        path.write_bytes(payload)
        adapter = PosixPlatformAdapter()
        root = adapter.bind_root(configured)
        self.addCleanup(root.close)
        bound = adapter.open_regular(root, PurePosixPath("data/source.bin"))
        self.addCleanup(bound.close)
        return _Fixture(base, configured, path, payload, adapter, root, bound)

    def assert_error(self, operation: Callable[[], object], code: PlatformFileErrorCode) -> None:
        with self.assertRaises(PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(caught.exception.args, (code.value,))
        self.assertIsNone(caught.exception.__cause__)

    def test_live_original_reports_numeric_fstat_and_digest_without_reopening(self) -> None:
        original = self.fixture.path.stat()
        self.assertIsInstance(self.fixture.adapter, PosixBoundContentObservation)
        self.fixture.root.close()
        with mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("reopen")), mock.patch(
            "platform_fs_posix.os.close", side_effect=AssertionError("premature close"),
        ), mock.patch("platform_fs_posix.os.dup", side_effect=AssertionError("duplicate")):
            facts = self.fixture.adapter.observe_bound_content(self.fixture.bound)
            self.assertFalse(self.fixture.bound.closed)
            self.assertEqual(self.fixture.bound.read_all(), self.fixture.payload)
        self.assertEqual(facts, PosixBoundContentFacts(
            original.st_dev, original.st_ino, len(self.fixture.payload), hashlib.sha256(self.fixture.payload).digest(),
        ))

    def test_empty_content_is_observed(self) -> None:
        self.fixture.path.write_bytes(b"")
        facts = self.fixture.adapter.observe_bound_content(self.fixture.bound)
        self.assertEqual((facts.byte_count, facts.content_sha256), (0, hashlib.sha256(b"").digest()))

    def test_synchronized_live_file_retains_the_original_issuer(self) -> None:
        adapter = self.fixture.adapter
        bound = adapter.open_existing_for_synchronization(
            self.fixture.root, PurePosixPath("data/source.bin"),
        )
        with bound:
            self.fixture.root.close()
            facts = adapter.observe_bound_content(bound)
            self.assertEqual(facts.content_sha256, hashlib.sha256(self.fixture.payload).digest())
            self.assert_error(
                lambda: type(adapter)().observe_bound_content(bound),
                PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            )

    def test_pending_readback_inherits_the_issuing_parent(self) -> None:
        adapter = self.fixture.adapter
        foreign = type(adapter)()
        for parent_adapter in (adapter, foreign):
            with self.subTest(foreign=parent_adapter is foreign):
                name = "foreign.bin" if parent_adapter is foreign else "own.bin"
                parent = parent_adapter.bind_parent(self.fixture.root, PurePosixPath("data/" + name))
                with parent:
                    candidate = parent.create_candidate(".candidate-" + name, private=False)
                    with candidate:
                        candidate.write_all(b"published")
                        candidate.flush_content()
                        pending = parent.begin_publish(
                            candidate, name, mode=PublishMode.CREATE_IF_ABSENT, lease=None,
                        )
                    with pending:
                        retained = pending.retained_destination()
                        if parent_adapter is adapter:
                            facts = adapter.observe_bound_content(retained)
                            original = self.fixture.path.with_name(name).stat()
                            self.assertEqual((facts.device, facts.inode), (original.st_dev, original.st_ino))
                            self.assertEqual(facts.content_sha256, hashlib.sha256(b"published").digest())
                        else:
                            for observer in (adapter, foreign):
                                self.assert_error(
                                    lambda: observer.observe_bound_content(retained),
                                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                                )
                        pending.terminal_reproof()

    def test_foreign_adapter_and_foreign_root_are_rejected(self) -> None:
        foreign = type(self.fixture.adapter)()
        self.assert_error(
            lambda: foreign.observe_bound_content(self.fixture.bound),
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )
        foreign_root = foreign.bind_root(self.fixture.configured)
        with foreign_root:
            crossed = self.fixture.adapter.open_regular(foreign_root, PurePosixPath("data/source.bin"))
            with crossed:
                self.assert_error(
                    lambda: self.fixture.adapter.observe_bound_content(crossed),
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                )
                self.assert_error(
                    lambda: foreign.observe_bound_content(crossed),
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                )

    def test_paths_identities_and_closed_files_are_not_accepted(self) -> None:
        for value in (self.fixture.path, self.fixture.bound.identity(), 42):
            with self.subTest(value=type(value).__name__), self.assertRaises(TypeError):
                self.fixture.adapter.observe_bound_content(cast(BoundRegularFile, cast(object, value)))
        self.fixture.bound.close()
        self.assert_error(
            lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        )

    def test_original_root_rename_with_clone_is_rejected_without_rebind(self) -> None:
        self.fixture.configured.rename(self.fixture.base / "old-family")
        self.fixture.configured.mkdir()
        (self.fixture.configured / "data").mkdir()
        self.fixture.path.write_bytes(self.fixture.payload)
        with mock.patch.object(self.fixture.adapter, "bind_root", side_effect=AssertionError("fresh binding")):
            self.assert_error(
                lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
                PlatformFileErrorCode.IDENTITY_STALE,
            )

    def test_parent_rename_with_clone_is_rejected(self) -> None:
        (self.fixture.configured / "data").rename(self.fixture.configured / "old-data")
        (self.fixture.configured / "data").mkdir()
        self.fixture.path.write_bytes(self.fixture.payload)
        self.assert_error(
            lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
            PlatformFileErrorCode.IDENTITY_STALE,
        )

    def test_replaced_entry_with_same_bytes_is_rejected(self) -> None:
        self.fixture.path.rename(self.fixture.path.with_name("old.bin"))
        self.fixture.path.write_bytes(self.fixture.payload)
        self.assert_error(
            lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
            PlatformFileErrorCode.IDENTITY_STALE,
        )

    def test_missing_entry_is_rejected(self) -> None:
        self.fixture.path.unlink()
        self.assert_error(
            lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
            PlatformFileErrorCode.IDENTITY_STALE,
        )

    def test_hardlinked_file_is_rejected_including_link_present_at_bind(self) -> None:
        os.link(self.fixture.path, self.fixture.path.with_name("alias.bin"))
        linked = self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath("data/source.bin"))
        with linked:
            for bound in (self.fixture.bound, linked):
                self.assert_error(
                    lambda: self.fixture.adapter.observe_bound_content(bound),
                    PlatformFileErrorCode.IDENTITY_STALE,
                )

    def test_symlink_and_special_replacements_are_rejected(self) -> None:
        self.fixture.path.rename(self.fixture.path.with_name("old.bin"))
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.fixture.path.symlink_to("old.bin")
                else:
                    os.mkfifo(self.fixture.path)
                self.assert_error(
                    lambda: self.fixture.adapter.observe_bound_content(self.fixture.bound),
                    PlatformFileErrorCode.REPARSE_REJECTED,
                )
                self.fixture.path.unlink()

    def test_length_changes_during_observation_are_rejected(self) -> None:
        for point in ("bound_content_before_read", "bound_content_after_read"):
            for payload in (b"short", self.fixture.payload + b"growth"):
                with self.subTest(point=point, byte_count=len(payload)):
                    self.fixture.path.write_bytes(self.fixture.payload)

                    def change_content(hit: str) -> None:
                        if hit == point:
                            self.fixture.path.write_bytes(payload)

                    adapter = type(self.fixture.adapter)(_fault_injector=change_content)
                    root = adapter.bind_root(self.fixture.configured)
                    with root:
                        bound = adapter.open_regular(root, PurePosixPath("data/source.bin"))
                        with bound:
                            self.assert_error(
                                lambda: adapter.observe_bound_content(bound),
                                PlatformFileErrorCode.IDENTITY_STALE,
                            )
                            self.assertFalse(bound.closed)

    def test_observable_metadata_change_during_observation_is_rejected(self) -> None:
        for point in ("bound_content_before_read", "bound_content_after_read"):
            with self.subTest(point=point):
                self.fixture.path.write_bytes(self.fixture.payload)
                before = self.fixture.path.stat()
                observed_mtimes: list[int] = []

                def change_content(hit: str) -> None:
                    if hit == point:
                        self.fixture.path.write_bytes(b"x" * len(self.fixture.payload))
                        # Force an observable difference without relying on clock
                        # resolution or sleeping after the same-length rewrite.
                        os.utime(self.fixture.path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                        after = self.fixture.path.stat()
                        self.assertEqual((after.st_dev, after.st_ino, after.st_size), (
                            before.st_dev, before.st_ino, before.st_size,
                        ))
                        self.assertNotEqual(after.st_mtime_ns, before.st_mtime_ns)
                        observed_mtimes.append(after.st_mtime_ns)

                adapter = type(self.fixture.adapter)(_fault_injector=change_content)
                root = adapter.bind_root(self.fixture.configured)
                with root:
                    bound = adapter.open_regular(root, PurePosixPath("data/source.bin"))
                    with bound:
                        self.assert_error(
                            lambda: adapter.observe_bound_content(bound),
                            PlatformFileErrorCode.IDENTITY_STALE,
                        )
                        self.assertEqual(len(observed_mtimes), 1)
                        self.assertFalse(bound.closed)

    def test_controlled_timestamp_collision_reports_the_actual_finite_capture(self) -> None:
        """Fix timestamp observations only; real FS timing is not being proved."""
        original = self.fixture.payload
        replacement = b"x" * len(original)
        real_pread = os.pread
        for point, captured in (
            ("bound_content_before_read", replacement),
            ("bound_content_after_read", original),
        ):
            with self.subTest(point=point):
                self.fixture.path.write_bytes(original)
                before = self.fixture.path.stat()
                mutations: list[str] = []
                reads: list[tuple[int, int, int, bytes]] = []

                def change_content(hit: str) -> None:
                    if hit == point:
                        self.fixture.path.write_bytes(replacement)
                        after = self.fixture.path.stat()
                        self.assertEqual((after.st_dev, after.st_ino, after.st_size), (
                            before.st_dev, before.st_ino, before.st_size,
                        ))
                        mutations.append(hit)

                def read_retained(descriptor: int, size: int, offset: int) -> bytes:
                    chunk = real_pread(descriptor, size, offset)
                    reads.append((descriptor, size, offset, chunk))
                    return chunk

                adapter = type(self.fixture.adapter)(_fault_injector=change_content)
                root = adapter.bind_root(self.fixture.configured)
                with root:
                    bound = adapter.open_regular(root, PurePosixPath("data/source.bin"))
                    with bound:
                        # Identify the original live file's FD through its real
                        # read path before observing it; never replace _reprove.
                        with mock.patch("platform_fs_posix.os.pread", side_effect=read_retained):
                            self.assertEqual(bound.read_all(), original)
                        retained_descriptor = reads[0][0]
                        self.assertEqual({read[0] for read in reads}, {retained_descriptor})
                        reads.clear()
                        with mock.patch("platform_fs_posix._modified_token", return_value=b"collision") as token, mock.patch(
                            "platform_fs_posix.os.pread", side_effect=read_retained,
                        ), mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("reopen")), mock.patch(
                            "platform_fs_posix.os.dup", side_effect=AssertionError("duplicate"),
                        ), mock.patch("platform_fs_posix.os.close", side_effect=AssertionError("premature close")):
                            facts = adapter.observe_bound_content(bound)
                            self.assertFalse(bound.closed)
                            self.assertEqual(os.fstat(retained_descriptor).st_ino, before.st_ino)
                        self.assertGreaterEqual(token.call_count, 2)
                        self.assertEqual(mutations, [point])
                        self.assertEqual(facts, PosixBoundContentFacts(
                            before.st_dev, before.st_ino, len(captured), hashlib.sha256(captured).digest(),
                        ))
                        self.assertNotEqual(hashlib.sha256(original).digest(), hashlib.sha256(replacement).digest())
                        self.assertEqual({read[0] for read in reads}, {retained_descriptor})
                        self.assertEqual([(size, offset) for _, size, offset, _ in reads], [
                            (65536, 0), (65536, 65536), (48128, 131072), (1, len(captured)),
                        ])
                        self.assertEqual(b"".join(chunk for _, _, _, chunk in reads), captured)
                        self.assertEqual(reads[-1][3], b"")
                        self.assertEqual(bound.read_all(), replacement)
                    self.assertTrue(bound.closed)
                    with self.assertRaises(OSError) as closed:
                        os.fstat(retained_descriptor)
                    self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_link_and_parent_mutations_during_capture_are_rejected(self) -> None:
        for mutation in ("link", "parent"):
            with self.subTest(mutation=mutation):
                def change_namespace(point: str) -> None:
                    if point != "bound_content_after_read":
                        return
                    if mutation == "link":
                        os.link(self.fixture.path, self.fixture.path.with_name("alias.bin"))
                    else:
                        (self.fixture.configured / "data").rename(self.fixture.configured / "old-data")

                adapter = type(self.fixture.adapter)(_fault_injector=change_namespace)
                root = adapter.bind_root(self.fixture.configured)
                with root:
                    bound = adapter.open_regular(root, PurePosixPath("data/source.bin"))
                    with bound:
                        self.assert_error(
                            lambda: adapter.observe_bound_content(bound),
                            PlatformFileErrorCode.IDENTITY_STALE,
                        )
                if mutation == "link":
                    self.fixture.path.with_name("alias.bin").unlink()

    def test_read_or_fstat_failure_is_body_free_and_does_not_close_authority(self) -> None:
        fixture = self.fixture
        for call in ("pread", "fstat"):
            with self.subTest(call=call), mock.patch(
                "platform_fs_posix.os." + call,
                side_effect=OSError(errno.EBADF, "injected invalid descriptor"),
            ), mock.patch("platform_fs_posix.os.open", side_effect=AssertionError("reopen")), mock.patch(
                "platform_fs_posix.os.close", side_effect=AssertionError("premature close"),
            ):
                self.assert_error(
                    lambda: fixture.adapter.observe_bound_content(fixture.bound),
                    PlatformFileErrorCode.IDENTITY_STALE,
                )
            self.assertFalse(self.fixture.bound.closed)
        self.assertEqual(self.fixture.bound.read_all(), self.fixture.payload)


if __name__ == "__main__":
    unittest.main()
