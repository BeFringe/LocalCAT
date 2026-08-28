"""WA-01 real-NTFS Parser rooted source and canonical publication matrix."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from parser_composition import create_parser_application_surface
from parser_contracts import (
    EffectivePurpose,
    LOCALCAT_JSON_V1,
    ReadRequest,
    SelectionRequest,
    SourceReference,
    TargetReference,
)
from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR
from parser_source import ParserSourceError, atomic_write_bytes, create_sealed_snapshot
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
)
import platform_fs_windows
from platform_fs_windows import WindowsPlatformAdapter
from tests.parser_io_test_support import (
    atomic_test_write_bytes,
    create_test_sealed_snapshot,
)


WORKER = Path(__file__).with_name("windows_parser_source_worker.py")
OLD = b'[{"id":"old","source":"old","target":""}]\n'
NEW = b'[{"id":"new","source":"new","target":"done"}]\n'


@unittest.skipUnless(sys.platform == "win32", "WA-01 requires real Windows NTFS")
class WindowsParserSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="localcat-wa01-")
        self.container = Path(self.temporary.name)
        self.root = self.container / "Root"
        self.input = self.root / "Input"
        self.output = self.root / "Output"
        self.input.mkdir(parents=True)
        self.output.mkdir()
        self.source = self.input / "chapter.json"
        self.source.write_bytes(NEW)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _reference(self, path: Path | None = None) -> SourceReference:
        selected = path or self.source
        return SourceReference(str(self.root), str(selected), selected.name)

    def _target(self, path: Path) -> TargetReference:
        return TargetReference(str(self.root), str(path), path.name)

    def _reopen_id(self, target: Path) -> str:
        surface = create_parser_application_surface()
        opened = surface.open_input(
            self._reference(target),
            SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
        )
        self.assertTrue(hasattr(opened, "materialize"))
        with opened:
            result = opened.materialize()
        return result.records[0].local_id

    def test_live_rooted_source_seals_exact_bytes_and_runs_real_codec(self) -> None:
        original_read_at = BoundRegularFile.read_at
        reads: list[tuple[int, int]] = []

        def tracked_read_at(
            authority: BoundRegularFile,
            offset: int,
            maximum_bytes: int,
            expected: object,
        ) -> bytes:
            reads.append((offset, maximum_bytes))
            return original_read_at(authority, offset, maximum_bytes, expected)

        with mock.patch.object(
            BoundRegularFile,
            "read_all",
            side_effect=AssertionError("Parser source must use bounded read_at"),
        ), mock.patch.object(
            BoundRegularFile,
            "read_at",
            autospec=True,
            side_effect=tracked_read_at,
        ):
            snapshot = create_test_sealed_snapshot(
                self._reference(),
                limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
            )
        try:
            self.assertEqual(snapshot.identity.content_sha256, hashlib.sha256(NEW).hexdigest())
            self.assertTrue(snapshot.identity.regular_file_identity.startswith("windows:"))
        finally:
            snapshot.close()
        self.assertEqual(reads, [(0, len(NEW)), (len(NEW), 1)])
        self.assertEqual(self._reopen_id(self.source), "new")

    def test_bounded_source_copy_observes_cancellation_after_each_native_chunk(self) -> None:
        from parser_source import CancellationToken

        self.source.write_bytes(b"x" * (64 * 1024 * 2 + 17))
        cancellation = CancellationToken()
        completed_chunks = 0
        roots: list[object] = []
        sources: list[object] = []
        temporary_handles: list[object] = []
        read_offsets: list[int] = []
        actual_temporary_file = tempfile.TemporaryFile
        original_read_at = BoundRegularFile.read_at

        def cancel_after_first_chunk(phase: str) -> None:
            nonlocal completed_chunks
            if phase == "windows_after_body_read":
                completed_chunks += 1
                if completed_chunks == 1:
                    cancellation.cancel()

        class TrackingAdapter(WindowsPlatformAdapter):
            def _bind_root(self, root: Path) -> object:
                authority = super()._bind_root(root)
                roots.append(authority)
                return authority

            def _open_regular(self, root: object, relative: object) -> object:
                authority = super()._open_regular(root, relative)
                sources.append(authority)
                return authority

        def capture_temporary(*args: object, **kwargs: object) -> object:
            handle = actual_temporary_file(*args, **kwargs)
            temporary_handles.append(handle)
            return handle

        def tracked_read_at(
            authority: BoundRegularFile,
            offset: int,
            maximum_bytes: int,
            expected: object,
        ) -> bytes:
            read_offsets.append(offset)
            return original_read_at(authority, offset, maximum_bytes, expected)

        with mock.patch(
            "parser_source.tempfile.TemporaryFile",
            side_effect=capture_temporary,
        ), mock.patch.object(
            BoundRegularFile,
            "read_at",
            autospec=True,
            side_effect=tracked_read_at,
        ), self.assertRaises(ParserSourceError) as caught:
            create_test_sealed_snapshot(
                self._reference(),
                limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                cancellation=cancellation,
                file_system=TrackingAdapter(
                    _fault_injector=cancel_after_first_chunk,
                ),
            )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.CANCELLED")
        self.assertEqual(completed_chunks, 1)
        self.assertEqual(read_offsets, [0])
        self.assertEqual(len(temporary_handles), 1)
        self.assertTrue(all(handle.closed for handle in temporary_handles))
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(sources), 1)
        self.assertTrue(all(authority.closed for authority in (*roots, *sources)))

    def test_missing_entry_is_read_failure_and_writer_entry_error_is_write_failure(self) -> None:
        missing = self.input / "missing.json"
        with self.assertRaises(ParserSourceError) as source_error:
            create_test_sealed_snapshot(
                self._reference(missing),
                limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
            )
        self.assertEqual(source_error.exception.code, "PARSER.SOURCE.READ_FAILED")

        adapter = WindowsPlatformAdapter()
        api = adapter._native_api()
        with mock.patch.object(
            api,
            "ReadFile",
            wraps=api.ReadFile,
        ) as read_file, self.assertRaises(ParserSourceError) as directory_error:
            create_test_sealed_snapshot(
                self._reference(self.input),
                limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                file_system=adapter,
            )
        self.assertEqual(directory_error.exception.code, "PARSER.SOURCE.NOT_REGULAR")
        read_file.assert_not_called()

        class EntryUnavailableWriterAdapter(WindowsPlatformAdapter):
            def _bind_parent(self, root: object, relative: object) -> object:
                del root, relative
                raise PlatformFileError(
                    PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                    retryable=False,
                )

        with self.assertRaises(ParserSourceError) as writer_error:
            atomic_test_write_bytes(
                self._target(self.output / "missing-parent.json"),
                NEW,
                backend=EntryUnavailableWriterAdapter(),
            )
        self.assertEqual(writer_error.exception.code, "PARSER.SOURCE.WRITE_FAILED")

    def test_hardlink_is_rejected_before_first_body_byte(self) -> None:
        hardlink = self.input / "alias.json"
        os.link(self.source, hardlink)
        adapter = WindowsPlatformAdapter()
        api = adapter._native_api()
        with mock.patch.object(api, "ReadFile", wraps=api.ReadFile) as read_file:
            with self.assertRaises(ParserSourceError) as caught:
                create_test_sealed_snapshot(
                    self._reference(hardlink),
                    limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                    file_system=adapter,
                )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.NOT_REGULAR")
        read_file.assert_not_called()
        self.assertNotIn(NEW.decode("ascii").strip(), str(caught.exception))

    def test_junction_is_body_safe_and_never_reads_outside_bytes(self) -> None:
        outside = self.container / "Outside"
        outside.mkdir()
        secret = b"outside-body-must-not-appear"
        (outside / "secret.json").write_bytes(secret)
        junction = self.root / "Junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        try:
            with self.assertRaises(ParserSourceError) as caught:
                create_test_sealed_snapshot(
                    self._reference(junction / "secret.json"),
                    limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                )
            self.assertEqual(caught.exception.code, "PARSER.SOURCE.NOT_REGULAR")
            self.assertNotIn(secret.decode("ascii"), str(caught.exception))
        finally:
            os.rmdir(junction)

    def test_retained_ancestor_and_source_handles_block_swaps(self) -> None:
        replacement = self.input / "replacement.json"
        replacement.write_bytes(NEW)
        swap_errors: list[OSError] = []
        attempted = False

        def swap(phase: str) -> None:
            nonlocal attempted
            if phase == "windows_before_body_read" and not attempted:
                attempted = True
                for operation in (
                    lambda: os.replace(replacement, self.source),
                    lambda: self.source.write_bytes(OLD),
                    lambda: os.rename(self.input, self.root / "MovedInput"),
                ):
                    try:
                        operation()
                    except OSError as error:
                        swap_errors.append(error)

        snapshot = create_test_sealed_snapshot(
            self._reference(),
            limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
            file_system=WindowsPlatformAdapter(_fault_injector=swap),
        )
        try:
            self.assertEqual(snapshot.identity.content_sha256, hashlib.sha256(NEW).hexdigest())
        finally:
            snapshot.close()
        self.assertEqual(len(swap_errors), 3)
        self.assertTrue(all(isinstance(error, PermissionError) for error in swap_errors))
        os.replace(replacement, self.source)
        moved = self.root / "MovedInput"
        os.rename(self.input, moved)
        os.rename(moved, self.input)

    def test_entry_probe_concurrent_rewrite_is_stale_before_body_read(self) -> None:
        mutated = False

        def rewrite(phase: str) -> None:
            nonlocal mutated
            if phase == "windows_after_entry_probe":
                self.source.write_bytes(OLD)
                mutated = True

        adapter = WindowsPlatformAdapter(_fault_injector=rewrite)
        api = adapter._native_api()
        with mock.patch.object(api, "ReadFile", wraps=api.ReadFile) as read_file:
            with self.assertRaises(ParserSourceError) as caught:
                create_test_sealed_snapshot(
                    self._reference(),
                    limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                    file_system=adapter,
                )
        self.assertTrue(mutated)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")
        read_file.assert_not_called()
        self.assertEqual(self.source.read_bytes(), OLD)

    def test_canonical_writer_rejects_hardlink_and_junction_without_change(self) -> None:
        primary = self.output / "primary.json"
        hardlink = self.output / "hardlink.json"
        primary.write_bytes(OLD)
        os.link(primary, hardlink)
        with self.assertRaises(ParserSourceError) as hardlink_error:
            atomic_test_write_bytes(self._target(hardlink), NEW)
        self.assertEqual(hardlink_error.exception.code, "PARSER.SOURCE.NOT_REGULAR")
        self.assertEqual(primary.read_bytes(), OLD)
        self.assertEqual(hardlink.read_bytes(), OLD)

        outside = self.container / "OutsideOutput"
        outside.mkdir()
        outside_target = outside / "project.json"
        outside_target.write_bytes(OLD)
        junction = self.root / "JunctionOutput"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        try:
            with self.assertRaises(ParserSourceError) as junction_error:
                atomic_test_write_bytes(
                    self._target(junction / "project.json"),
                    NEW,
                )
            self.assertEqual(junction_error.exception.code, "PARSER.SOURCE.NOT_REGULAR")
            self.assertEqual(outside_target.read_bytes(), OLD)
        finally:
            os.rmdir(junction)

    def test_proof_drift_after_body_returns_stale_without_snapshot(self) -> None:
        drifted = False

        def mark(phase: str) -> None:
            nonlocal drifted
            if phase == "windows_after_body_read":
                drifted = True

        adapter = WindowsPlatformAdapter(_fault_injector=mark)
        real_capture = platform_fs_windows._capture_handle_proof

        def drifting_capture(*args: object, **kwargs: object) -> object:
            proof = real_capture(*args, **kwargs)
            if drifted and kwargs.get("expected_kind") == "regular":
                changed = platform_fs_windows.EntrySnapshot(
                    identity=proof.identity,
                    byte_count=proof.snapshot.byte_count + 1,
                    modified_token=proof.snapshot.modified_token,
                    reparse_free=True,
                )
                return platform_fs_windows._WindowsHandleProof(
                    proof.identity,
                    changed,
                    proof.final_path,
                )
            return proof

        with mock.patch.object(
            platform_fs_windows,
            "_capture_handle_proof",
            side_effect=drifting_capture,
        ):
            with self.assertRaises(ParserSourceError) as caught:
                create_test_sealed_snapshot(
                    self._reference(),
                    limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                    file_system=adapter,
                )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")

    def test_unavailable_rooted_capability_keeps_stable_parser_code(self) -> None:
        unavailable = PlatformFileError(
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            retryable=False,
        )
        with mock.patch(
            "parser_composition._compose_platform_file_backend",
            side_effect=unavailable,
        ):
            surface = create_parser_application_surface()
            with self.assertRaises(ParserSourceError) as caught:
                surface.open_input(
                    self._reference(),
                    SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
                    ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
                )
        self.assertEqual(
            caught.exception.code,
            "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE",
        )

    def test_canonical_create_replace_readback_and_target_open_failure(self) -> None:
        target = self.output / "project.json"
        adapter = WindowsPlatformAdapter()
        created = atomic_test_write_bytes(self._target(target), OLD, backend=adapter)
        self.assertEqual(created.content_sha256, hashlib.sha256(OLD).hexdigest())
        self.assertEqual(self._reopen_id(target), "old")

        replaced = atomic_test_write_bytes(self._target(target), NEW, backend=adapter)
        self.assertEqual(replaced.content_sha256, hashlib.sha256(NEW).hexdigest())
        self.assertEqual(self._reopen_id(target), "new")

        root = adapter.bind_root(self.root)
        reader = adapter.open_regular(root, PureWindowsPath("Output", "project.json"))
        root.close()
        try:
            with self.assertRaises(ParserSourceError) as caught:
                atomic_test_write_bytes(self._target(target), OLD, backend=adapter)
            self.assertEqual(
                caught.exception.code,
                "PARSER.SOURCE.WRITE_RECOVERY_REQUIRED",
            )
            self.assertEqual(target.read_bytes(), NEW)
        finally:
            reader.close()
        self.assertEqual(
            atomic_test_write_bytes(self._target(target), OLD, backend=adapter).content_sha256,
            hashlib.sha256(OLD).hexdigest(),
        )

    def test_canonical_writer_orders_modes_close_readback_terminal_and_receipt(self) -> None:
        target = self.output / "ordered.json"
        adapter = WindowsPlatformAdapter()
        events: list[object] = []
        state = {"begin": False, "terminal": False}
        original_write = CandidateFile.write_all
        original_flush = CandidateFile.flush_content
        original_begin = BoundDirectoryAuthority.begin_publish
        original_preliminary = PendingPublication.preliminary_facts
        original_retained = PendingPublication.retained_destination
        original_read_all = BoundRegularFile.read_all
        original_terminal = PendingPublication.terminal_reproof

        def tracked_write(authority: CandidateFile, payload: bytes) -> None:
            events.append("candidate.write")
            original_write(authority, payload)

        def tracked_flush(authority: CandidateFile) -> None:
            events.append("candidate.flush")
            original_flush(authority)

        def tracked_begin(
            authority: BoundDirectoryAuthority,
            candidate: CandidateFile,
            destination: str,
            *,
            mode: PublishMode,
            lease: object,
        ) -> PendingPublication:
            events.append(("publish.begin", mode, lease is None, candidate.closed))
            state["begin"] = True
            try:
                pending = original_begin(
                    authority,
                    candidate,
                    destination,
                    mode=mode,
                    lease=lease,
                )
            finally:
                state["begin"] = False
            events.append(("publish.return", candidate.closed))
            return pending

        def tracked_preliminary(authority: PendingPublication) -> object:
            events.append("preliminary")
            return original_preliminary(authority)

        def tracked_retained(authority: PendingPublication) -> BoundRegularFile:
            events.append("retained")
            return original_retained(authority)

        def tracked_read_all(authority: BoundRegularFile) -> bytes:
            if state["begin"]:
                events.append("publisher.readback")
            elif state["terminal"]:
                events.append("terminal.readback")
            else:
                events.append("parser.readback")
            return original_read_all(authority)

        def tracked_terminal(authority: PendingPublication) -> object:
            events.append("terminal.begin")
            state["terminal"] = True
            try:
                return original_terminal(authority)
            finally:
                state["terminal"] = False
                events.append("terminal.return")

        def run_once(payload: bytes) -> tuple[object, ...]:
            events.clear()
            receipt = atomic_test_write_bytes(self._target(target), payload, backend=adapter)
            events.append("receipt.return")
            self.assertEqual(receipt.content_sha256, hashlib.sha256(payload).hexdigest())
            return tuple(events)

        with mock.patch.object(CandidateFile, "write_all", new=tracked_write), mock.patch.object(
            CandidateFile,
            "flush_content",
            new=tracked_flush,
        ), mock.patch.object(
            BoundDirectoryAuthority,
            "begin_publish",
            new=tracked_begin,
        ), mock.patch.object(
            PendingPublication,
            "preliminary_facts",
            new=tracked_preliminary,
        ), mock.patch.object(
            PendingPublication,
            "retained_destination",
            new=tracked_retained,
        ), mock.patch.object(
            BoundRegularFile,
            "read_all",
            new=tracked_read_all,
        ), mock.patch.object(
            PendingPublication,
            "terminal_reproof",
            new=tracked_terminal,
        ):
            created = run_once(OLD)
            replaced = run_once(NEW)

        for observed, expected_mode, lease_is_none in (
            (created, PublishMode.CREATE_IF_ABSENT, True),
            (replaced, PublishMode.REPLACE_UNDER_LOCK, False),
        ):
            begin = next(item for item in observed if isinstance(item, tuple) and item[0] == "publish.begin")
            returned = next(item for item in observed if isinstance(item, tuple) and item[0] == "publish.return")
            self.assertEqual(begin, ("publish.begin", expected_mode, lease_is_none, False))
            self.assertEqual(returned, ("publish.return", True))
            self.assertLess(observed.index("candidate.write"), observed.index("candidate.flush"))
            self.assertLess(observed.index("candidate.flush"), observed.index(begin))
            self.assertLess(observed.index(returned), observed.index("preliminary"))
            self.assertLess(observed.index("preliminary"), observed.index("retained"))
            self.assertLess(observed.index("retained"), observed.index("parser.readback"))
            self.assertLess(observed.index("parser.readback"), observed.index("terminal.begin"))
            self.assertLess(observed.index("terminal.return"), observed.index("receipt.return"))

    def test_prearm_failure_preserves_old_target_and_cleans_candidate(self) -> None:
        target = self.output / "project.json"
        target.write_bytes(OLD)
        adapter = WindowsPlatformAdapter()
        api = adapter._native_api()
        with mock.patch.object(
            api,
            "self_probe_nt_set_information_file",
            side_effect=RuntimeError("unavailable"),
        ):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_test_write_bytes(self._target(target), NEW, backend=adapter)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_FAILED")
        self.assertEqual(target.read_bytes(), OLD)
        self.assertEqual(tuple(self.output.glob(".parser-*.tmp")), ())

    def test_postpublication_failure_returns_stable_error_and_no_false_receipt(self) -> None:
        target = self.output / "project.json"
        target.write_bytes(OLD)

        def fail(phase: str) -> None:
            if phase == "publish_after_rename":
                raise RuntimeError("fault")

        with self.assertRaises(ParserSourceError) as caught:
            atomic_test_write_bytes(
                self._target(target),
                NEW,
                backend=WindowsPlatformAdapter(_fault_injector=fail),
            )
        self.assertEqual(
            caught.exception.code,
            "PARSER.SOURCE.WRITE_RECOVERY_REQUIRED",
        )
        self.assertEqual(target.read_bytes(), NEW)
        self.assertEqual(self._reopen_id(target), "new")

    def test_terminal_reproof_failure_issues_no_receipt_and_closes_authorities(self) -> None:
        target = self.output / "terminal.json"
        target.write_bytes(OLD)
        observed: dict[str, object] = {}

        class TrackingAdapter(WindowsPlatformAdapter):
            def _bind_root(self, root: Path):
                authority = super()._bind_root(root)
                observed["root"] = authority
                return authority

            def _bind_parent(self, root: object, relative: object):
                authority = super()._bind_parent(root, relative)
                observed["parent"] = authority
                return authority

            def _acquire(
                self,
                parent: object,
                name: str,
                payload: bytes,
                policy: object,
            ):
                authority = super()._acquire(parent, name, payload, policy)
                observed["lease"] = authority
                return authority

        original_begin = BoundDirectoryAuthority.begin_publish

        def tracked_begin(
            authority: BoundDirectoryAuthority,
            candidate: CandidateFile,
            destination: str,
            *,
            mode: PublishMode,
            lease: object,
        ) -> PendingPublication:
            pending = original_begin(
                authority,
                candidate,
                destination,
                mode=mode,
                lease=lease,
            )
            observed["pending"] = pending
            return pending

        def fail_terminal(_authority: PendingPublication) -> object:
            raise PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )

        with mock.patch.object(
            BoundDirectoryAuthority,
            "begin_publish",
            new=tracked_begin,
        ), mock.patch.object(
            PendingPublication,
            "terminal_reproof",
            new=fail_terminal,
        ):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_test_write_bytes(
                    self._target(target),
                    NEW,
                    backend=TrackingAdapter(),
                )

        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_RECOVERY_REQUIRED")
        self.assertEqual(target.read_bytes(), NEW)
        self.assertEqual(self._reopen_id(target), "new")
        self.assertEqual(set(observed), {"root", "parent", "lease", "pending"})
        self.assertTrue(all(getattr(authority, "closed") for authority in observed.values()))

    def test_process_termination_allows_only_complete_old_or_new_and_clean_reopen(self) -> None:
        expected = {
            "publish_before_rename": "old",
            "publish_after_rename": "new",
            "publish_after_final_facts": "new",
            "publish_before_destination_reopen": "new",
            "publish_after_destination_reopen": "new",
        }
        for phase, expected_id in expected.items():
            with self.subTest(phase=phase):
                phase_root = self.root / f"Termination-{phase}"
                phase_root.mkdir()
                target = phase_root / "project.json"
                target.write_bytes(OLD)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(WORKER),
                        "publish",
                        str(phase_root),
                        str(target),
                        "--fault-phase",
                        phase,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 87, completed.stderr.decode(errors="replace"))
                expected_payload = OLD if expected_id == "old" else NEW
                self.assertEqual(target.read_bytes(), expected_payload)
                residue = phase_root / ".parser-untrusted-residue.tmp"
                residue.write_bytes(NEW)
                residues_before_reopen = {
                    path.name: path.read_bytes()
                    for path in phase_root.glob(".parser-*.tmp")
                }
                reopened = subprocess.run(
                    [sys.executable, "-B", str(WORKER), "reopen", str(phase_root), str(target)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(reopened.returncode, 0, reopened.stderr)
                self.assertEqual(json.loads(reopened.stdout)["local_id"], expected_id)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in phase_root.glob(".parser-*.tmp")
                    },
                    residues_before_reopen,
                )


if __name__ == "__main__":
    unittest.main()
