"""Synthetic POSIX directory observation, resource and no-write regressions."""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import platform_fs_contracts as fs

if os.name == "posix":
    import platform_fs_posix as posix


@unittest.skipUnless(os.name == "posix", "POSIX native observation")
class PosixObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.path = self.base / "root"
        self.path.mkdir()
        self.backend = posix.PosixPlatformAdapter()
        self.limits = fs.DirectoryObservationLimits()
        self.cancel = fs.DirectoryObservationCancellation()

    def bind(self):
        root = self.backend.bind_observation_root(self.path, self.limits, self.cancel)
        self.addCleanup(root.close)
        return root

    def observe(self, directory):
        return self.backend.observe_children(directory, self.limits, self.cancel)

    def descend(self, parent, result, name):
        entry = next(entry for entry in result.entries if entry.name == name)
        return self.backend.retain_observed_directory(
            parent, result, entry, self.limits, self.cancel,
        )

    def assertFailure(self, code, operation):
        with self.assertRaises(fs.PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)

    def test_metadata_only_marks_links_special_and_unsafe_names(self):
        (self.path / "chapter.txt").write_bytes(b"private body")
        (self.path / "nested").mkdir()
        (self.path / "bad\\name").mkdir()
        (self.path / "link").symlink_to(self.base, target_is_directory=True)
        os.mkfifo(self.path / "pipe")
        with ExitStack() as stack:
            forbidden = [stack.enter_context(mock.patch.object(posix.os, name))
                         for name in ("read", "pread", "write", "pwrite", "mkdir", "unlink", "rename", "replace", "fsync")]
            forbidden.append(stack.enter_context(mock.patch.object(posix.fcntl, "flock")))
            opened = stack.enter_context(mock.patch.object(posix.os, "open", wraps=os.open))
            root = self.bind()
            result = self.observe(root)
            self.assertIsInstance(self.backend, fs.ReadOnlyDirectoryObservation)
            self.assertNotIsInstance(root, fs.RootedDirectoryAuthority)
            entries = {entry.name: entry for entry in result.entries}
            self.assertEqual(entries["chapter.txt"].snapshot.byte_count, 12)
            self.assertEqual(entries["nested"].kind, fs.DirectoryEntryKind.DIRECTORY)
            self.assertEqual(entries["link"].unavailable_reason, fs.DirectoryEntryUnavailableReason.LINK)
            self.assertEqual(entries["pipe"].unavailable_reason, fs.DirectoryEntryUnavailableReason.NOT_REGULAR)
            self.assertEqual(entries["bad\\name"].unavailable_reason, fs.DirectoryEntryUnavailableReason.UNSAFE_NAME)
            for name in ("link", "pipe", "bad\\name", "chapter.txt"):
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                                   lambda name=name: self.descend(root, result, name))
            for call in opened.call_args_list:
                self.assertTrue(call.args[1] & os.O_DIRECTORY)
                self.assertTrue(call.args[1] & os.O_NOFOLLOW)
                self.assertFalse(call.args[1] & (os.O_CREAT | os.O_RDWR | os.O_WRONLY))
            for operation in forbidden:
                operation.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.path)), ["bad\\name", "chapter.txt", "link", "nested", "pipe"])

    def test_root_replacement_and_root_link_fail_closed(self):
        root = self.bind()
        self.path.rename(self.base / "old")
        self.path.mkdir()
        self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
        self.path.rmdir()
        self.path.symlink_to(self.base / "old", target_is_directory=True)
        self.assertFailure(fs.PlatformFileErrorCode.REPARSE_REJECTED, self.bind)

    def test_descendant_replacement_link_and_disconnection_fail_closed(self):
        for mutation in ("replace", "symlink", "disconnect"):
            with self.subTest(mutation=mutation):
                name = mutation
                child_path = self.path / name
                child_path.mkdir()
                root = self.bind()
                result = self.observe(root)
                child = self.descend(root, result, name)
                child_path.rename(self.base / name)
                if mutation == "replace":
                    child_path.mkdir()
                elif mutation == "symlink":
                    child_path.symlink_to(self.base / name, target_is_directory=True)
                with self.assertRaises(fs.PlatformFileError):
                    self.observe(child)
                root.close()
                self.assertTrue(child.closed)

    def test_observed_child_cannot_be_replaced_before_descent(self):
        (self.path / "child").mkdir()
        root = self.bind()
        result = self.observe(root)
        (self.path / "child").rename(self.base / "old")
        (self.path / "child").symlink_to(self.base / "old", target_is_directory=True)
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.REPARSE_REJECTED,
                               lambda: self.descend(root, result, "child"))
            opened.assert_not_called()

    def test_retained_lineage_single_fd_per_level_and_cascade_close(self):
        (self.path / "a" / "b").mkdir(parents=True)
        root = self.bind()
        child = self.descend(root, self.observe(root), "a")
        leaf = self.descend(child, self.observe(child), "b")
        resources = root._handle_reservation._budget
        self.assertEqual(resources.active_handles, 3)
        self.assertEqual(leaf.depth, 2)
        child.close()
        self.assertTrue(leaf.closed)
        self.assertEqual(resources.active_handles, 1)
        root.close()
        self.assertEqual(resources.active_handles, 0)

    def test_temporary_directory_stream_fd_is_reserved_before_native_acquisition(self):
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=1)
        self.path = Path("/")
        root = self.bind()
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                               lambda: self.observe(root))
            opened.assert_not_called()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_entry_and_depth_limits_never_issue_partial_observation(self):
        (self.path / "a").mkdir()
        (self.path / "link").symlink_to(self.base)
        self.limits = fs.DirectoryObservationLimits(maximum_entries=1)
        root = self.bind()
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                           lambda: self.observe(root))
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)
        root.close()
        self.limits = fs.DirectoryObservationLimits(maximum_depth=0)
        root = self.bind()
        result = self.observe(root)
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                               lambda: self.descend(root, result, "a"))
            opened.assert_not_called()

    def test_cancelled_operation_never_acquires_native_handles(self):
        self.cancel.cancel()
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.bind)
            opened.assert_not_called()

    def test_nested_root_requires_budget_before_second_native_open(self):
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=1)
        original_open = os.open
        acquired = []

        def opened_native(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            acquired.append(descriptor)
            return descriptor

        with mock.patch.object(posix.os, "open", side_effect=opened_native) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED, self.bind)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0], "/")
        with self.assertRaises(OSError):
            os.fstat(acquired[0])

    def test_root_binding_acquires_each_component_nofollow_and_reserves_actual_cost(self):
        original_open, original_close = os.open, os.close
        live = set()
        peak = 0

        def opened(name, flags, **kwargs):
            nonlocal peak
            descriptor = original_open(name, flags, **kwargs)
            live.add(descriptor)
            peak = max(peak, len(live))
            self.assertLessEqual(len(live), 2)
            self.assertTrue(flags & os.O_NOFOLLOW)
            if name != "/":
                self.assertIn(kwargs["dir_fd"], live)
                self.assertNotIn("/", name)
            return descriptor

        def closed(descriptor):
            original_close(descriptor)
            live.remove(descriptor)

        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=2)
        with mock.patch.object(posix.os, "open", side_effect=opened) as acquire, \
                mock.patch.object(posix.os, "close", side_effect=closed) as release:
            root = self.bind()
            self.assertEqual(peak, 2)
            self.assertEqual(live, {root._descriptor})
            self.assertEqual(root._handle_reservation._budget.active_handles, 1)
            root.close()
            self.assertEqual(live, set())
            self.assertEqual(acquire.call_count, len(self.path.parts))
            self.assertEqual(release.call_count, acquire.call_count)

    def test_root_ancestor_symlink_swap_between_stat_and_open_never_follows_link(self):
        parent = self.base / "ancestor"
        parent.mkdir()
        self.path = parent / "chosen"
        self.path.mkdir()
        original_open = os.open
        hit = []

        def opened(name, flags, **kwargs):
            if name == "ancestor":
                parent.rename(self.base / "moved")
                parent.symlink_to(self.base / "moved", target_is_directory=True)
                hit.append(name)
            return original_open(name, flags, **kwargs)

        with mock.patch.object(posix.os, "open", side_effect=opened) as acquire:
            with self.assertRaises(fs.PlatformFileError):
                self.bind()
            self.assertEqual(hit, ["ancestor"])
            self.assertFalse(any(call.args[0] == "chosen" for call in acquire.call_args_list))

    def test_root_external_ancestor_replacement_is_stale_even_if_chosen_root_moves_back(self):
        parent = self.base / "ancestor"
        parent.mkdir()
        self.path = parent / "chosen"
        self.path.mkdir()
        root = self.bind()
        parent.rename(self.base / "old-parent")
        parent.mkdir()
        (self.base / "old-parent" / "chosen").rename(self.path)
        with mock.patch.object(posix, "_PosixDirectoryStream", wraps=posix._PosixDirectoryStream) as scanned:
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
            scanned.assert_not_called()

    def test_root_symlink_ancestor_and_non_directory_are_explicit_failures(self):
        alias = self.base / "alias"
        alias.symlink_to(self.path, target_is_directory=True)
        (self.path / "chosen").mkdir()
        self.path = alias / "chosen"
        self.assertFailure(fs.PlatformFileErrorCode.REPARSE_REJECTED, self.bind)
        self.path = self.base / "plain.txt"
        self.path.write_bytes(b"body")
        self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, self.bind)

    def test_child_directory_replacement_after_observation_is_stale(self):
        (self.path / "child").mkdir()
        root = self.bind()
        result = self.observe(root)
        (self.path / "child").rename(self.base / "old")
        (self.path / "child").mkdir()
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                               lambda: self.descend(root, result, "child"))
            opened.assert_not_called()

    def test_child_replacement_after_native_open_releases_rejected_handle(self):
        (self.path / "child").mkdir()
        root = self.bind()
        result = self.observe(root)
        original_open = os.open
        acquired = []

        def opened(name, flags, **kwargs):
            descriptor = original_open(name, flags, **kwargs)
            acquired.append(descriptor)
            (self.path / "child").rename(self.base / "old")
            (self.path / "child").mkdir()
            return descriptor

        with mock.patch.object(posix.os, "open", side_effect=opened) as acquire:
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                               lambda: self.descend(root, result, "child"))
            acquire.assert_called_once()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)
        with self.assertRaises(OSError):
            os.fstat(acquired[0])

    def test_non_utf8_name_is_unavailable_metadata_without_body_access(self):
        # macOS cannot create undecodable byte names. Feed the native metadata
        # classifier the surrogateescape value that Linux directory_stream can return.
        name = os.fsdecode(b"bad-\xff.txt")
        (self.path / "file.txt").write_bytes(b"body")
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            entry = posix._observation_metadata(name, os.stat(self.path / "file.txt"))
            opened.assert_not_called()
        self.assertEqual(entry.name, name)
        self.assertEqual(entry.unavailable_reason, fs.DirectoryEntryUnavailableReason.UNSAFE_NAME)

    def test_cumulative_entries_include_unavailable_entries_and_failed_attempts(self):
        (self.path / "child").mkdir()
        (self.path / "link").symlink_to(self.base)
        (self.path / "child" / "one.txt").write_bytes(b"one")
        self.limits = fs.DirectoryObservationLimits(maximum_entries=2)
        root = self.bind()
        result = self.observe(root)
        child = self.descend(root, result, "child")
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                           lambda: self.observe(child))
        self.assertIsNone(child._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 2)
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                           lambda: self.observe(root))

    def test_directory_stream_temporary_handle_and_non_ancestor_close_allow_sibling_descent(self):
        (self.path / "a").mkdir()
        (self.path / "b").mkdir()
        self.limits = fs.DirectoryObservationLimits(maximum_directory_handles=2)
        root = self.bind()
        result = self.observe(root)
        child = self.descend(root, result, "a")
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                               lambda: self.observe(child))
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                               lambda: self.descend(root, result, "b"))
            opened.assert_not_called()
        child.close()
        sibling = self.descend(root, result, "b")
        self.assertEqual(sibling.depth, 1)
        self.assertEqual(root._handle_reservation._budget.active_handles, 2)

    def test_default_depth_and_handle_limits_apply_to_real_deep_tree(self):
        path = self.path
        for _ in range(33):
            path = path / "d"
            path.mkdir()
        root = self.bind()
        directory = root
        for depth in range(32):
            directory = self.descend(directory, self.observe(directory), "d")
            self.assertEqual(directory.depth, depth + 1)
        # Thirty-three retained fds leave no temporary cursor slot at depth 32.
        with mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                               lambda: self.observe(directory))
            opened.assert_not_called()
        self.assertEqual(root._handle_reservation._budget.active_handles, 33)
        root.close()
        self.assertTrue(directory.closed)
        self.assertEqual(root._handle_reservation._budget.active_handles, 0)

    def test_cancel_immediately_after_root_open_closes_fd_and_returns_budget(self):
        original_open = os.open
        acquired, budgets = [], []
        original_reserve = fs.DirectoryHandleBudget.reserve

        def reserve(resources, count=1):
            budgets.append(resources)
            return original_reserve(resources, count)

        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            acquired.append(descriptor)
            self.cancel.cancel()
            return descriptor

        with mock.patch.object(posix.os, "open", side_effect=opened) as acquire, \
                mock.patch.object(fs.DirectoryHandleBudget, "reserve", reserve):
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, self.bind)
            acquire.assert_called_once()
        self.assertEqual(budgets[0].active_handles, 0)
        with self.assertRaises(OSError):
            os.fstat(acquired[0])

    def test_cancel_after_child_open_closes_only_new_descriptor(self):
        (self.path / "child").mkdir()
        root = self.bind()
        result = self.observe(root)
        original_open = os.open
        acquired = []

        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            acquired.append(descriptor)
            self.cancel.cancel()
            return descriptor

        with mock.patch.object(posix.os, "open", side_effect=opened) as acquire:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                               lambda: self.descend(root, result, "child"))
            acquire.assert_called_once()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)
        self.assertFalse(root.closed)
        with self.assertRaises(OSError):
            os.fstat(acquired[0])

    def test_cancel_during_metadata_and_after_directory_stream_acquisition_cleans_iterator(self):
        (self.path / "file.txt").write_bytes(b"body")
        for phase in ("directory_stream", "metadata"):
            with self.subTest(phase=phase):
                self.cancel = fs.DirectoryObservationCancellation()
                root = self.bind()
                original_scan, original_stat = posix._PosixDirectoryStream, os.stat
                streams = []

                def scanned(*args, **kwargs):
                    stream = original_scan(*args, **kwargs)
                    streams.append(stream)
                    if phase == "directory_stream":
                        self.cancel.cancel()
                    return stream

                def stated(path, *args, **kwargs):
                    result = original_stat(path, *args, **kwargs)
                    if path == "file.txt":
                        self.cancel.cancel()
                    return result

                with mock.patch.object(posix, "_PosixDirectoryStream", side_effect=scanned) as scan, \
                        mock.patch.object(posix.os, "stat", side_effect=stated) as metadata:
                    self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                                       lambda: self.observe(root))
                    scan.assert_called_once()
                    if phase == "metadata":
                        self.assertTrue(any(call.args[0] == "file.txt" for call in metadata.call_args_list))
                self.assertEqual(list(streams[0]), [])
                self.assertEqual(root._handle_reservation._budget.active_handles, 1)
                self.assertIsNone(root._last_observation)
                root.close()

    def test_metadata_replacement_during_enumeration_fails_and_closes_iterator(self):
        (self.path / "file.txt").write_bytes(b"old")
        (self.base / "replacement.txt").write_bytes(b"new")
        root = self.bind()
        original_stat = os.stat
        hits = []

        def stated(path, *args, **kwargs):
            if path == "file.txt" and not hits:
                (self.base / "replacement.txt").replace(self.path / "file.txt")
                hits.append(path)
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(posix.os, "stat", side_effect=stated):
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
        self.assertEqual(hits, ["file.txt"])
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_native_open_directory_stream_stat_failures_are_body_free_and_cleanup(self):
        for native in ("open", "stat"):
            with self.subTest(native=native):
                root = self.bind()
                if native == "open":
                    operation = self.bind
                else:
                    operation = lambda: self.observe(root)
                with mock.patch.object(posix.os, native, side_effect=OSError(errno.EACCES, "private path")) as failed:
                    with self.assertRaises(fs.PlatformFileError) as caught:
                        operation()
                    failed.assert_called_once()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(root._handle_reservation._budget.active_handles, 1)
                root.close()

    def test_failed_native_close_keeps_reservation_charged_and_authority_terminal(self):
        root = self.bind()
        descriptor = root._descriptor
        resources = root._handle_reservation._budget
        with mock.patch.object(posix.os, "close", side_effect=OSError(errno.EIO, "private path")) as failed:
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, root.close)
            failed.assert_called_once_with(descriptor)
            root.close()
            self.assertEqual(failed.call_count, 1)
        self.assertTrue(root.closed)
        self.assertEqual(resources.active_handles, 1)
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(root))
        os.close(descriptor)

    def test_native_closedir_failure_keeps_temporary_slot_charged_without_retry(self):
        root = self.bind()
        api = posix._ObservationDirectoryAPI()
        original_close = api.closedir

        def failed_close(pointer):
            self.assertEqual(original_close(pointer), 0)
            ctypes.set_errno(errno.EIO)
            return -1

        with mock.patch.object(api, "closedir", side_effect=failed_close) as failed, \
                mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api):
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(root))
            failed.assert_called_once()
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 2)
        root.close()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_generation_change_after_create_unlink_is_stale_with_same_identity(self):
        (self.path / "first.txt").write_bytes(b"first")
        root = self.bind()
        expected_identity = root.identity()
        original_metadata = posix._observation_metadata
        hits = []

        def metadata(name, result):
            entry = original_metadata(name, result)
            hits.append(name)
            (self.path / "transient.txt").write_bytes(b"transient")
            (self.path / "transient.txt").unlink()
            return entry

        with mock.patch.object(posix, "_observation_metadata", side_effect=metadata):
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
        self.assertEqual(hits, ["first.txt"])
        self.assertEqual(root.reprove(), expected_identity)
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_repeated_observation_uses_independent_native_cursor(self):
        (self.path / "one.txt").write_bytes(b"one")
        root = self.bind()
        first, second = self.observe(root), self.observe(root)
        self.assertEqual(first.entries, second.entries)
        self.assertIsNot(first, second)

    def test_fdopendir_failure_releases_native_fd_and_budget(self):
        root = self.bind()
        api = posix._ObservationDirectoryAPI()
        original_open = os.open
        acquired = []

        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            acquired.append(descriptor)
            return descriptor

        def failed_open(descriptor):
            ctypes.set_errno(errno.EACCES)
            return None

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "fdopendir", side_effect=failed_open) as failed, \
                mock.patch.object(api, "closedir", wraps=api.closedir) as closed, \
                mock.patch.object(posix.os, "open", side_effect=opened):
            self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, lambda: self.observe(root))
            failed.assert_called_once_with(acquired[0])
            closed.assert_not_called()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)
        with self.assertRaises(OSError):
            os.fstat(acquired[0])

    def test_readdir_error_is_not_exhaustion_and_native_stream_is_closed(self):
        root = self.bind()
        api = posix._ObservationDirectoryAPI()

        def failed_read(pointer):
            ctypes.set_errno(errno.EIO)
            return None

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "readdir", side_effect=failed_read) as failed, \
                mock.patch.object(api, "closedir", wraps=api.closedir) as closed:
            self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, lambda: self.observe(root))
            failed.assert_called_once()
            closed.assert_called_once()
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_cancellation_after_fdopendir_and_during_terminal_reproof_closes_cursor(self):
        for phase in ("opened", "terminal"):
            with self.subTest(phase=phase):
                self.cancel = fs.DirectoryObservationCancellation()
                root = self.bind()
                api = posix._ObservationDirectoryAPI()
                original_open, original_close = api.fdopendir, api.closedir

                def opened(descriptor):
                    pointer = original_open(descriptor)
                    if phase == "opened":
                        self.cancel.cancel()
                    return pointer

                def closed(pointer):
                    result = original_close(pointer)
                    if phase == "terminal":
                        self.cancel.cancel()
                    return result

                with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                        mock.patch.object(api, "fdopendir", side_effect=opened) as acquired, \
                        mock.patch.object(api, "closedir", side_effect=closed) as released:
                    self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                                       lambda: self.observe(root))
                    acquired.assert_called_once()
                    released.assert_called_once()
                self.assertIsNone(root._last_observation)
                self.assertEqual(root._handle_reservation._budget.active_handles, 1)
                root.close()

    def test_unknown_abi_fails_before_native_directory_acquisition(self):
        root = self.bind()
        with mock.patch.object(posix.sys, "platform", "unsupported"), \
                mock.patch.object(posix.os, "open", wraps=os.open) as opened:
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(root))
            opened.assert_not_called()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_native_record_decoding_checks_darwin_and_linux_layouts_and_lengths(self):
        api = posix._ObservationDirectoryAPI()
        for record_type, offset in ((posix._DarwinDirent, 21), (posix._LinuxDirent64, 19)):
            with self.subTest(record_type=record_type):
                api.record_type = record_type
                self.assertEqual(record_type.name.offset, offset)
                buffer = ctypes.create_string_buffer(128)
                record = record_type.from_buffer(buffer)
                record.ino = 123
                record.reclen = offset + 10
                if record_type is posix._DarwinDirent:
                    record.namlen = 5
                ctypes.memmove(ctypes.addressof(buffer) + offset, b"a.txt\x00", 6)
                self.assertEqual(api.decode(ctypes.addressof(buffer)), ("a.txt", 123))
                record.reclen = offset
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                                   lambda: api.decode(ctypes.addressof(buffer)))
                record.reclen = offset + 5
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                                   lambda: api.decode(ctypes.addressof(buffer)))
        self.assertEqual(ctypes.sizeof(posix._DarwinStatfs), 2168)
        self.assertEqual(posix._DarwinStatfs.flags.offset, 64)

    def test_union_mount_rejection_happens_before_fdopendir_and_releases_fd(self):
        if os.uname().sysname != "Darwin":
            self.skipTest("Darwin union mount guard")
        root = self.bind()
        api = posix._ObservationDirectoryAPI()

        def union_statfs(descriptor, result):
            ctypes.cast(result, ctypes.POINTER(posix._DarwinStatfs)).contents.flags = 0x20
            return 0

        with mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "statfs", side_effect=union_statfs) as mounted, \
                mock.patch.object(api, "fdopendir", wraps=api.fdopendir) as acquired:
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, lambda: self.observe(root))
            mounted.assert_called_once()
            acquired.assert_not_called()
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)

    def test_budget_tracks_every_real_open_including_cursor_until_native_close(self):
        original_open, original_close = os.open, os.close
        original_reserve = fs.DirectoryHandleBudget.reserve
        api = posix._ObservationDirectoryAPI()
        native_stream_open, native_stream_close = api.fdopendir, api.closedir
        budgets, live, pointers = [], set(), {}
        peaks = []

        def reserve(budget, count=1):
            budgets.append(budget)
            return original_reserve(budget, count)

        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            live.add(descriptor)
            self.assertEqual(budgets[-1].active_handles, len(live))
            peaks.append(len(live))
            return descriptor

        def closed(descriptor):
            original_close(descriptor)
            live.remove(descriptor)

        def opened_stream(descriptor):
            pointer = native_stream_open(descriptor)
            pointers[pointer] = descriptor
            return pointer

        def closed_stream(pointer):
            result = native_stream_close(pointer)
            self.assertEqual(result, 0)
            live.remove(pointers.pop(pointer))
            return result

        with mock.patch.object(fs.DirectoryHandleBudget, "reserve", reserve), \
                mock.patch.object(posix.os, "open", side_effect=opened), \
                mock.patch.object(posix.os, "close", side_effect=closed), \
                mock.patch.object(posix, "_ObservationDirectoryAPI", return_value=api), \
                mock.patch.object(api, "fdopendir", side_effect=opened_stream) as acquired, \
                mock.patch.object(api, "closedir", side_effect=closed_stream) as released:
            root = self.bind()
            self.observe(root)
            acquired.assert_called_once()
            released.assert_called_once()
            self.assertEqual(live, {root._descriptor})
            root.close()
        self.assertEqual(max(peaks), 2)
        self.assertEqual(live, set())
        self.assertEqual(budgets[-1].active_handles, 0)

    def test_failed_close_during_cancelled_acquisition_keeps_quota(self):
        original_open, original_reserve = os.open, fs.DirectoryHandleBudget.reserve
        acquired, budgets = [], []

        def reserve(budget, count=1):
            budgets.append(budget)
            return original_reserve(budget, count)

        def opened(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            acquired.append(descriptor)
            self.cancel.cancel()
            return descriptor

        with mock.patch.object(fs.DirectoryHandleBudget, "reserve", reserve), \
                mock.patch.object(posix.os, "open", side_effect=opened), \
                mock.patch.object(posix.os, "close", side_effect=OSError(errno.EIO, "close")) as failed:
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, self.bind)
            failed.assert_called_once_with(acquired[0])
        self.assertEqual(budgets[0].active_handles, 1)
        os.close(acquired[0])

    def test_root_replaced_during_metadata_does_not_issue_observation(self):
        (self.path / "file.txt").write_bytes(b"body")
        root = self.bind()
        original_metadata = posix._observation_metadata

        def metadata(name, result):
            entry = original_metadata(name, result)
            self.path.rename(self.base / "old")
            self.path.mkdir()
            return entry

        with mock.patch.object(posix, "_observation_metadata", side_effect=metadata) as changed:
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE, lambda: self.observe(root))
            changed.assert_called_once()
        self.assertIsNone(root._last_observation)
        self.assertEqual(root._handle_reservation._budget.active_handles, 1)


if __name__ == "__main__":
    unittest.main()
