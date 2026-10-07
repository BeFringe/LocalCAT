"""Windows read-only directory observations: pure records and native handles."""

from __future__ import annotations

from pathlib import Path
from contextlib import ExitStack
from dataclasses import replace
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platform_fs_contracts as fs
import platform_fs_windows as win


def page(name="a.txt", *, attributes=0x20, tag=0, next_offset=0):
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    return win._FILE_ID_EXTD_DIR_HEADER.pack(
        next_offset, 0, 0, 0, 11, 12, 4, 0, attributes,
        len(encoded), 0, tag, b"a" * 16,
    ) + encoded


class WindowsObservationRecordTests(unittest.TestCase):
    def test_capability_is_lazy_and_cancel_precedes_native_initialization(self):
        backend = win.WindowsPlatformAdapter()
        self.assertIsInstance(backend, fs.ReadOnlyDirectoryObservation)
        self.assertIsNone(backend._api)
        cancel = fs.DirectoryObservationCancellation()
        cancel.cancel()
        with mock.patch.object(backend, "_native_api") as native:
            with self.assertRaises(fs.PlatformFileError) as caught:
                backend.bind_observation_root(Path.cwd(), fs.DirectoryObservationLimits(), cancel)
            self.assertEqual(caught.exception.code, fs.PlatformFileErrorCode.OBSERVATION_CANCELLED.value)
            native.assert_not_called()

    def test_record_retains_native_facts_without_inventing_link_count(self):
        record, = win._decode_observation_buffer(page())
        self.assertEqual(record.name, "a.txt")
        self.assertEqual(record.file_id, b"a" * 16)
        self.assertEqual(record.byte_count, 4)
        self.assertEqual(record.modified_token, struct.pack("<qqI", 11, 12, 0x20))
        self.assertFalse(hasattr(record, "snapshot"))

    def test_reparse_and_unsafe_utf16_are_preserved_for_unavailable_metadata(self):
        for name, attrs, tag in (("junction", 0x410, 0xA0000003), ("bad\ud800", 0x20, 0)):
            with self.subTest(name=repr(name)):
                record, = win._decode_observation_buffer(page(name, attributes=attrs, tag=tag))
                self.assertEqual(record.name, name)
                self.assertEqual(record.attributes, attrs)
                self.assertEqual(record.reparse_tag, tag)

    def test_malformed_pages_fail_without_partial_success(self):
        for payload in (b"", page()[:-1], page(next_offset=89)):
            with self.subTest(payload_length=len(payload)), self.assertRaises(fs.PlatformFileError) as caught:
                tuple(win._decode_observation_buffer(payload))
            self.assertEqual(caught.exception.code, fs.PlatformFileErrorCode.IDENTITY_STALE.value)


class WindowsObservationAdapterTests(unittest.TestCase):
    def test_cached_directory_times_do_not_hide_identity_or_content_drift(self):
        root_identity = fs.FileObjectIdentity("windows", b"v" * 8, b"r" * 16, "directory", 1)
        root_snapshot = fs.EntrySnapshot(root_identity, 0, b"root generation", True)
        for scenario, kind, file_id, root_changed, accepted in (
            ("cached directory times", "directory", b"a" * 16, False, True),
            ("replaced directory", "directory", b"b" * 16, False, False),
            ("modified regular file", "regular", b"a" * 16, False, False),
            ("changed root generation", "directory", b"a" * 16, True, False),
        ):
            with self.subTest(scenario=scenario):
                attributes = 0x10 if kind == "directory" else 0x20
                payload = page("child", attributes=attributes)
                identity = fs.FileObjectIdentity("windows", root_identity.volume_id, file_id, kind, 1)
                # Both times lag in the enumeration page, while the opened
                # handle reports the current, stable metadata for this object.
                snapshot = fs.EntrySnapshot(identity, 4, struct.pack("<qqI", 21, 22, attributes), True)
                path = "C:\\root\\child"
                proof = win._WindowsHandleProof(identity, snapshot, path)
                listing_handle = win.Win32Handle(101, _close=lambda raw: 1)
                self.addCleanup(listing_handle.close)
                metadata_handle = win.Win32Handle(102, _close=lambda raw: 1)
                self.addCleanup(metadata_handle.close)
                api = mock.Mock()
                api.open_handle.return_value = metadata_handle
                api.last_error.return_value = win.ERROR_NO_MORE_FILES

                def query(raw, info, buffer, size):
                    if info == win._FILE_ID_EXTD_DIR_RESTART_INFO_CLASS:
                        buffer.raw = payload
                        return True
                    return False

                api.GetFileInformationByHandleEx.side_effect = query
                directory = mock.Mock(spec=win._WindowsObservationDirectory)
                directory._api = api
                directory._leaf_path = "C:\\root"
                directory._maximum_component_units = 255
                directory._records = (win._WindowsDirectoryRecord(listing_handle, "C:\\root", root_identity),)
                directory._snapshot.side_effect = (
                    root_snapshot,
                    replace(root_snapshot, modified_token=b"changed") if root_changed else root_snapshot,
                )
                budget = fs.DirectoryHandleBudget(1)
                backend = win.WindowsPlatformAdapter()
                with mock.patch.object(win, "_capture_handle_proof", return_value=proof) as captured:
                    stream = backend._iter_observed_children(directory, budget, fs.DirectoryObservationCancellation())
                    if accepted:
                        entry, = tuple(stream)
                        self.assertEqual(entry.kind, fs.DirectoryEntryKind.DIRECTORY)
                        self.assertIs(entry.snapshot, snapshot)
                    else:
                        with self.assertRaises(fs.PlatformFileError) as caught:
                            tuple(stream)
                        self.assertEqual(caught.exception.code, fs.PlatformFileErrorCode.IDENTITY_STALE.value)
                    captured.assert_called_once_with(
                        api, 102, expected_final_path=path, expected_kind=kind, stale=True,
                    )
                self.assertEqual(directory._snapshot.call_count, 2 if accepted or root_changed else 1)
                self.assertTrue(metadata_handle.closed)
                self.assertEqual(budget.active_handles, 0)


@unittest.skipUnless(sys.platform == "win32", "Windows native observation")
class WindowsObservationNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "root"
        self.path.mkdir()
        self.backend = win.WindowsPlatformAdapter()
        self.limits = fs.DirectoryObservationLimits()
        self.cancel = fs.DirectoryObservationCancellation()

    def bind(self):
        root = self.backend.bind_observation_root(self.path, self.limits, self.cancel)
        self.addCleanup(root.close)
        return root

    def observe(self, root):
        return self.backend.observe_children(root, self.limits, self.cancel)

    def descend(self, parent, observation, name):
        entry = next(entry for entry in observation.entries if entry.name == name)
        return self.backend.retain_observed_directory(
            parent, observation, entry, self.limits, self.cancel,
        )

    def failure(self, code, operation):
        with self.assertRaises(fs.PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)

    def test_real_root_and_nested_metadata_are_read_only(self):
        (self.path / "a").mkdir()
        (self.path / "a" / "chapter.txt").write_bytes(b"body")
        root = self.bind()
        self.assertIsInstance(self.backend, fs.ReadOnlyDirectoryObservation)
        self.assertNotIsInstance(root, fs.RootedDirectoryAuthority)
        observation = self.observe(root)
        child = self.descend(root, observation, "a")
        entry, = self.observe(child).entries
        self.assertEqual((entry.name, entry.kind, entry.snapshot.byte_count),
                         ("chapter.txt", fs.DirectoryEntryKind.REGULAR, 4))
        root.close()
        self.assertTrue(child.closed)

    def test_no_body_write_lock_or_writer_authority_is_used(self):
        (self.path / "a").mkdir()
        (self.path / "a" / "file.txt").write_bytes(b"body")
        api = self.backend._native_api()
        with ExitStack() as stack:
            forbidden = [stack.enter_context(mock.patch.object(api, name)) for name in
                         ("ReadFile", "WriteFile", "CreateDirectoryW", "SetFileInformationByHandle", "LockFileEx")]
            opened = stack.enter_context(mock.patch.object(api, "open_handle", wraps=api.open_handle))
            root = self.bind()
            child = self.descend(root, self.observe(root), "a")
            self.assertEqual(self.observe(child).entries[0].snapshot.byte_count, 4)
            for call in opened.call_args_list:
                self.assertEqual(call.kwargs["creation_disposition"], win.OPEN_EXISTING)
                self.assertFalse(call.kwargs["desired_access"] & (win.GENERIC_WRITE | win.DELETE))
                self.assertTrue(call.kwargs["flags"] & win.FILE_FLAG_OPEN_REPARSE_POINT)
            for operation in forbidden:
                operation.assert_not_called()
            root.close()
        self.assertEqual((self.path / "a" / "file.txt").read_bytes(), b"body")

    def test_junction_is_unavailable_and_never_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"unselected body")
        junction = self.path / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        root = None
        try:
            root = self.bind()
            observation = self.observe(root)
            entry, = observation.entries
            self.assertEqual(entry.kind, fs.DirectoryEntryKind.REPARSE)
            self.assertEqual(entry.unavailable_reason, fs.DirectoryEntryUnavailableReason.REPARSE)
            self.assertIsNone(entry.snapshot)
            with mock.patch.object(root._api, "open_handle", wraps=root._api.open_handle) as opened:
                self.failure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                             lambda: self.descend(root, observation, "junction"))
                opened.assert_not_called()
            root.close()
            self.path = junction
            self.failure(fs.PlatformFileErrorCode.REPARSE_REJECTED, self.bind)
        finally:
            if root is not None:
                root.close()
            os.rmdir(junction)

    def test_retained_root_and_child_deny_rename_then_release(self):
        child_path = self.path / "child"
        child_path.mkdir()
        root = self.bind()
        child = self.descend(root, self.observe(root), "child")
        for original, destination in ((self.path, self.base / "moved-root"),
                                      (child_path, self.path / "moved-child")):
            with self.assertRaises(PermissionError) as caught:
                original.rename(destination)
            self.assertEqual(caught.exception.winerror, 32)
        self.assertEqual(self.observe(child).entries, ())
        budget = root._handle_reservation._budget
        root.close()
        self.assertTrue(child.closed)
        self.assertEqual(budget.active_handles, 0)
        child_path.rename(self.path / "moved-child")
        self.path.rename(self.base / "moved-root")
        self.failure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(child))

    def test_observed_unretained_directory_replacement_is_denied_until_root_close(self):
        child_path = self.path / "child"
        child_path.mkdir()
        root = self.bind()
        observation = self.observe(root)
        before = root._handle_reservation._budget.active_handles
        # On Windows the retained listing ancestor also denies replacing a
        # child which has only been observed; its metadata handle is closed.
        with self.assertRaises(PermissionError) as caught:
            child_path.rename(self.path / "old-child")
        self.assertEqual(caught.exception.winerror, 32)
        self.assertEqual(root._handle_reservation._budget.active_handles, before)
        child = self.descend(root, observation, "child")
        self.assertEqual(child.identity(), observation.entries[0].snapshot.identity)
        root.close()
        self.assertTrue(child.closed)
        child_path.rename(self.path / "old-child")
        child_path.mkdir()
        self.failure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                     lambda: self.descend(root, observation, "child"))

    def test_actual_root_chain_and_metadata_handle_budgets(self):
        (self.path / "child").mkdir()
        root_cost = len(self.path.parts)
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=root_cost)
        root = self.bind()
        self.assertEqual(root._handle_reservation._budget.active_handles, root_cost)
        with mock.patch.object(root._api, "open_handle", wraps=root._api.open_handle) as opened:
            self.failure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, lambda: self.observe(root))
            opened.assert_not_called()
        self.assertIsNone(root._last_observation)
        root.close()
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=root_cost + 1)
        root = self.bind()
        observation = self.observe(root)
        child = self.descend(root, observation, "child")
        self.assertEqual(root._handle_reservation._budget.active_handles, root_cost + 1)
        # Enumeration reuses the private retained query cursor, without a copy.
        self.assertEqual(self.observe(child).entries, ())
        child.close()
        self.assertEqual(root._handle_reservation._budget.active_handles, root_cost)

    def test_root_budget_exhaustion_closes_every_acquired_handle(self):
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=1)
        api = self.backend._native_api()
        original = api.open_handle
        handles = []
        def opened(*args, **kwargs):
            value = original(*args, **kwargs)
            handles.append(value)
            return value
        with mock.patch.object(api, "open_handle", side_effect=opened):
            self.failure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, self.bind)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_depth_and_cumulative_entry_limits_do_not_issue_partial_results(self):
        (self.path / "child").mkdir()
        (self.path / "child" / "file.txt").write_bytes(b"body")
        self.limits = fs.DirectoryObservationLimits(maximum_depth=0)
        root = self.bind()
        observation = self.observe(root)
        with mock.patch.object(root._api, "open_handle", wraps=root._api.open_handle) as opened:
            self.failure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                         lambda: self.descend(root, observation, "child"))
            opened.assert_not_called()
        root.close()
        self.limits = fs.DirectoryObservationLimits(maximum_entries=1)
        root = self.bind()
        child = self.descend(root, self.observe(root), "child")
        self.failure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, lambda: self.observe(child))
        self.assertIsNone(child._last_observation)
        self.failure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, lambda: self.observe(root))

    def test_cancel_before_root_never_initializes_native_api(self):
        self.cancel.cancel()
        with mock.patch.object(self.backend, "_native_api") as native:
            self.failure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.bind)
            native.assert_not_called()

    def test_cancel_after_native_root_open_releases_all_handles(self):
        api = self.backend._native_api()
        handles = []
        original = api.open_handle
        def opened(*args, **kwargs):
            handle = original(*args, **kwargs)
            handles.append(handle)
            self.cancel.cancel()
            return handle
        with mock.patch.object(api, "open_handle", side_effect=opened):
            self.failure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.bind)
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_cancel_during_metadata_or_after_descent_does_not_leak(self):
        (self.path / "child").mkdir()
        for phase in ("observation_after_entry_proof", "observation_after_child_open"):
            with self.subTest(phase=phase):
                self.cancel = fs.DirectoryObservationCancellation()
                self.backend._fault_injector = None
                root = self.bind()
                observation = self.observe(root)
                before = root._handle_reservation._budget.active_handles
                hits = []
                def fault(point):
                    if point == phase:
                        hits.append(point)
                        self.cancel.cancel()
                self.backend._fault_injector = fault
                operation = (lambda: self.observe(root)) if phase.endswith("proof") else (
                    lambda: self.descend(root, observation, "child"))
                self.failure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, operation)
                self.assertEqual(hits, [phase])
                self.assertEqual(root._handle_reservation._budget.active_handles, before)
                root.close()

    def test_repeated_queries_restart_retained_cursor(self):
        (self.path / "a.txt").write_bytes(b"a")
        root = self.bind()
        first = self.observe(root)
        second = self.observe(root)
        self.assertEqual(first.entries, second.entries)
        self.assertIsNot(first, second)

    def test_generation_change_is_stale_without_identity_change(self):
        (self.path / "a.txt").write_bytes(b"a")
        root = self.bind()
        identity = root.identity()
        hits = []
        def fault(point):
            if point == "observation_after_enumeration":
                (self.path / "transient.txt").write_bytes(b"temporary")
                (self.path / "transient.txt").unlink()
                hits.append(point)
        self.backend._fault_injector = fault
        self.failure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
        self.assertEqual(len(hits), 1)
        self.assertEqual(root.reprove(), identity)
        self.assertIsNone(root._last_observation)

    def test_regular_mutation_after_query_is_stale(self):
        target = self.path / "a.txt"
        target.write_bytes(b"a")
        root = self.bind()
        hits = []
        def fault(point):
            if point == "observation_before_entry_proof":
                # Namespace replacement is denied by retained ancestors, but
                # a non-cooperating writer can still modify the regular file.
                target.write_bytes(b"changed after directory query")
                hits.append(point)
        self.backend._fault_injector = fault
        self.failure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
        self.assertEqual(len(hits), 1)
        self.assertEqual(target.read_bytes(), b"changed after directory query")
        self.assertIsNone(root._last_observation)

    def test_query_failure_is_not_end_of_directory(self):
        root = self.bind()
        api = root._api
        original = api.GetFileInformationByHandleEx
        def query(raw, info, buffer, size):
            if info in (19, 20):
                return 0
            return original(raw, info, buffer, size)
        with mock.patch.object(api, "GetFileInformationByHandleEx", side_effect=query) as queried, \
                mock.patch.object(api, "last_error", return_value=5):
            self.failure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, lambda: self.observe(root))
            self.assertTrue(any(call.args[1] in (19, 20) for call in queried.call_args_list))
        self.assertIsNone(root._last_observation)
        self.assertEqual(self.observe(root).entries, ())

    def test_failed_retained_close_keeps_quota_and_closes_other_ancestors(self):
        root = self.bind()
        budget = root._handle_reservation._budget
        selected = root._records[-1].handle
        owner = type(selected)
        original = owner.close
        failures = []
        def close(handle):
            original(handle)
            if handle is selected:
                failures.append(handle)
                raise win.Win32CallError("CloseHandle", 5)
        with mock.patch.object(owner, "close", side_effect=close, autospec=True):
            self.failure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, root.close)
            root.close()
        self.assertEqual(failures, [selected])
        self.assertTrue(root.closed)
        self.assertTrue(all(record.handle.closed for record in root._records))
        self.assertEqual(budget.active_handles, 1)

    def test_failed_directory_metadata_close_keeps_temporary_quota(self):
        (self.path / "child").mkdir()
        root = self.bind()
        budget = root._handle_reservation._budget
        initial = budget.active_handles
        api = root._api
        original = api.CloseHandle
        failures = []
        def close(raw):
            original(raw)
            failures.append(raw)
            return 0
        # Only newly opened metadata handles capture this failing callback;
        # retained handles and their eventual finalizers keep the original.
        with mock.patch.object(api, "CloseHandle", side_effect=close), \
                mock.patch.object(api, "last_error", return_value=5):
            self.failure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(root))
        self.assertEqual(len(failures), 1)
        self.assertIsNone(root._last_observation)
        self.assertEqual(budget.active_handles, initial + 1)
        root.close()
        self.assertEqual(budget.active_handles, 1)


if __name__ == "__main__":
    unittest.main()
