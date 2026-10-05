"""Read-only directory observation contracts exercised through primitive fakes."""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import pickle
import unittest

import platform_fs_contracts as fs


def _identity(name: str, kind: str = "directory") -> fs.FileObjectIdentity:
    return fs.FileObjectIdentity("posix", b"volume", name.encode(), kind, 1)


def _entry(name: str, kind: str = "regular", *, unavailable=None):
    snapshot = None
    if kind in {"regular", "directory"}:
        snapshot = fs.EntrySnapshot(_identity(name, kind), 7, b"modified", True)
    return fs.DirectoryEntryMetadata(
        name, fs.DirectoryEntryKind(kind), snapshot, unavailable,
    )


def _backend(tree=None):
    class Directory(fs.RetainedObservationDirectory):
        def __init__(self, name, reservation):
            super().__init__(reservation)
            self.name = name
            self.actual_identity = _identity(name)
            self.close_calls = 0
            self.close_failure = None
            self.native_live = True
            self.on_reprove = None

        def _reprove_identity(self):
            if self.on_reprove:
                self.on_reprove()
            return self.actual_identity

        def _close_authority(self):
            self.close_calls += 1
            if self.close_failure:
                raise self.close_failure
            self.native_live = False

    class Backend(fs.ReadOnlyDirectoryObservation):
        def __init__(self):
            self.tree = tree or {}
            self.opened = []
            self.stream_closed = 0
            self.iterated = 0
            self.on_yield = None
            self.after_open = None
            self.stream_failure = None
            self.return_child = None
            self.descend_calls = 0
            self.handle_cost = 1
            self.cursor_cost = 0
            self.resources = None

        def _bind_observation_root(self, root, resources, cancellation):
            self.resources = resources
            result = Directory("root", resources.reserve(self.handle_cost))
            self.opened.append(result)
            if self.after_open:
                self.after_open()
            return result

        def _iter_observed_children(self, retained_directory, resources, cancellation):
            reservation = resources.reserve(self.cursor_cost) if self.cursor_cost else None
            try:
                for entry in self.tree.get(retained_directory.name, ()):
                    self.iterated += 1
                    if self.on_yield:
                        self.on_yield()
                    yield entry
                if self.stream_failure:
                    raise self.stream_failure
            finally:
                self.stream_closed += 1
                if reservation:
                    reservation.close()

        def _retain_observed_directory(self, parent, entry, resources, cancellation):
            if self.return_child is not None:
                return self.return_child
            reservation = resources.reserve(self.handle_cost)
            self.descend_calls += 1
            result = Directory(entry.name, reservation)
            self.opened.append(result)
            if self.after_open:
                self.after_open()
            return result

    return Backend()


class DirectoryObservationContractsTests(unittest.TestCase):
    def setUp(self):
        self.limits = fs.DirectoryObservationLimits()
        self.cancel = fs.DirectoryObservationCancellation()

    def bind(self, backend, limits=None):
        return backend.bind_observation_root(Path.cwd(), limits or self.limits, self.cancel)

    def observe(self, backend, directory, limits=None):
        return backend.observe_children(directory, limits or self.limits, self.cancel)

    def descend(self, backend, parent, observation, entry, limits=None):
        return backend.retain_observed_directory(
            parent, observation, entry, limits or self.limits, self.cancel,
        )

    def assertFailure(self, code, operation):
        with self.assertRaises(fs.PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)

    def test_metadata_is_bounded_immutable_live_only_and_does_not_claim_content(self):
        entry = _entry("chapter.rpy")
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises(TypeError):
                operation(entry)
        with self.assertRaises(FrozenInstanceError):
            entry.name = "changed"
        with self.assertRaises(ValueError):
            _entry("x" * 4097)
        with self.assertRaises(ValueError):
            replace(entry, snapshot=None)
        with self.assertRaises(ValueError):
            replace(entry, kind=fs.DirectoryEntryKind.DIRECTORY)
        self.assertFalse(hasattr(entry, "content"))
        self.assertFalse(hasattr(entry, "content_sha256"))

    def test_root_and_descendant_offer_no_writer_reader_lock_or_retirement_authority(self):
        backend = _backend({"root": (_entry("nested", "directory"),)})
        with self.bind(backend) as root:
            observation = self.observe(backend, root)
            with self.descend(backend, root, observation, observation.entries[0]) as child:
                self.assertEqual(root.depth, 0)
                self.assertEqual(child.depth, 1)
                self.assertEqual(observation.root_identity, _identity("root"))
                self.assertEqual(observation.directory_identity, root.identity())
                for authority in (root, child):
                    self.assertNotIsInstance(authority, fs.BoundDirectoryAuthority)
                    self.assertNotIsInstance(authority, fs.BoundRegularFile)
                    self.assertNotIsInstance(authority, fs.RetirementDirectoryAuthority)
                    for name in ("read_all", "create_candidate", "observe_ledger_entries", "unlink_owned"):
                        self.assertFalse(hasattr(authority, name))
        self.assertEqual([item.close_calls for item in backend.opened], [1, 1])

    def test_special_and_unsafe_entries_are_visible_but_cannot_be_descended(self):
        unavailable = fs.DirectoryEntryUnavailableReason
        entries = (
            _entry("link", "symlink", unavailable=unavailable.LINK),
            _entry("junction", "reparse", unavailable=unavailable.REPARSE),
            _entry("pipe", "other", unavailable=unavailable.NOT_REGULAR),
            _entry("bad\\name", "directory", unavailable=unavailable.UNSAFE_NAME),
            _entry("regular.txt"),
        )
        backend = _backend({"root": entries})
        with self.bind(backend) as root:
            observed = self.observe(backend, root)
            self.assertEqual(set(item.name for item in observed.entries), set(item.name for item in entries))
            for entry in observed.entries:
                self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                    lambda: self.descend(backend, root, observed, entry))
            self.assertEqual(backend.descend_calls, 0)

    def test_cumulative_entry_limit_counts_directories_and_unavailable_entries(self):
        limits = fs.DirectoryObservationLimits(maximum_entries=2)
        backend = _backend({"root": (_entry("dir", "directory"),
            _entry("link", "symlink", unavailable=fs.DirectoryEntryUnavailableReason.LINK)),
            "dir": (_entry("too-many"),)})
        with self.bind(backend, limits) as root:
            result = self.observe(backend, root, limits)
            entry = next(item for item in result.entries if item.name == "dir")
            with self.descend(backend, root, result, entry, limits) as child:
                self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                    lambda: self.observe(backend, child, limits))
            self.assertEqual(backend.stream_closed, 2)

    def test_stream_limit_is_checked_before_unbounded_materialization_and_no_prefix_returns(self):
        limits = fs.DirectoryObservationLimits(maximum_entries=2)
        backend = _backend({"root": tuple(_entry(str(i)) for i in range(50))})
        with self.bind(backend, limits) as root:
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.observe(backend, root, limits))
            self.assertEqual(backend.iterated, 3)
            self.assertEqual(backend.stream_closed, 1)

    def test_depth_limit_and_handle_limit_are_shared_and_release_is_reusable(self):
        limits = fs.DirectoryObservationLimits(maximum_depth=1, maximum_directory_handles=2)
        backend = _backend({"root": (_entry("a", "directory"), _entry("b", "directory")),
                           "a": (_entry("deep", "directory"),)})
        with self.bind(backend, limits) as root:
            result = self.observe(backend, root, limits)
            first = self.descend(backend, root, result, result.entries[0], limits)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.descend(backend, root, result, result.entries[1], limits))
            child_result = self.observe(backend, first, limits)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.descend(backend, first, child_result, child_result.entries[0], limits))
            self.assertEqual(backend.descend_calls, 1)
            first.close()
            second = self.descend(backend, root, result, result.entries[1], limits)
            root.close()
            self.assertTrue(second.closed)
            self.assertEqual(second.close_calls, 1)

    def test_limits_cannot_be_expanded_or_reset_on_an_existing_root(self):
        limits = fs.DirectoryObservationLimits(maximum_entries=1)
        backend = _backend({"root": (_entry("one"),)})
        with self.bind(backend, limits) as root:
            self.observe(backend, root, limits)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.observe(backend, root, limits))
            with self.assertRaises(ValueError):
                self.observe(backend, root, self.limits)
        for kwargs in ({"maximum_depth": 33}, {"maximum_entries": 10001},
                       {"maximum_directory_handles": 34}, {"maximum_entries": True}):
            with self.assertRaises((TypeError, ValueError)):
                fs.DirectoryObservationLimits(**kwargs)

    def test_cancel_before_call_mid_stream_and_after_new_handle_always_cleans_up(self):
        backend = _backend({"root": (_entry("child", "directory"),)})
        self.cancel.cancel()
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, lambda: self.bind(backend))
        self.assertEqual(backend.opened, [])
        self.cancel = fs.DirectoryObservationCancellation()
        backend.after_open = self.cancel.cancel
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED, lambda: self.bind(backend))
        self.assertTrue(backend.opened[0].closed)
        self.cancel = fs.DirectoryObservationCancellation()
        backend.after_open = None
        with self.bind(backend) as root:
            backend.on_yield = self.cancel.cancel
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                lambda: self.observe(backend, root))
            self.assertEqual(backend.stream_closed, 1)

    def test_cancel_during_descend_releases_new_handle_and_budget_slot(self):
        backend = _backend({"root": (_entry("child", "directory"),)})
        with self.bind(backend) as root:
            observed = self.observe(backend, root)
            backend.after_open = self.cancel.cancel
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                lambda: self.descend(backend, root, observed, observed.entries[0]))
            self.assertTrue(backend.opened[-1].closed)
            self.assertFalse(root.closed)
            self.assertEqual(backend.resources.active_handles, 1)
            self.cancel = fs.DirectoryObservationCancellation()
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                lambda: self.observe(backend, root))

    def test_cancellation_during_terminal_identity_reproof_prevents_result_issuance(self):
        backend = _backend({"root": (_entry("one"),)})
        with self.bind(backend) as root:
            checks = []

            def cancel_on_terminal_check():
                checks.append(None)
                if len(checks) == 2:
                    self.cancel.cancel()

            root.on_reprove = cancel_on_terminal_check
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_CANCELLED,
                lambda: self.observe(backend, root))
            self.assertIsNone(root._last_observation)
            self.assertEqual(backend.stream_closed, 1)

    def test_foreign_closed_forged_and_previous_observations_do_not_authorize_downward_open(self):
        backend = _backend({"root": (_entry("child", "directory"),)})
        other = _backend()
        with self.bind(backend) as root:
            observed = self.observe(backend, root)
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                lambda: self.observe(other, root))
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.descend(backend, root, replace(observed), observed.entries[0]))
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.descend(backend, root, observed, replace(observed.entries[0])))
            self.observe(backend, root)
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.descend(backend, root, observed, observed.entries[0]))
            self.assertEqual(backend.descend_calls, 0)
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            lambda: self.observe(backend, root))

    def test_identity_drift_during_enumeration_or_descend_fails_without_leaking_handles(self):
        backend = _backend({"root": (_entry("child", "directory"),)})
        with self.bind(backend) as root:
            backend.on_yield = lambda: setattr(root, "actual_identity", _identity("replacement"))
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.observe(backend, root))
            root.actual_identity = _identity("root")
            backend.on_yield = None
            observed = self.observe(backend, root)
            backend.after_open = lambda: setattr(backend.opened[-1], "actual_identity", _identity("wrong-child"))
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.descend(backend, root, observed, observed.entries[0]))
            self.assertTrue(backend.opened[-1].closed)

    def test_directory_failure_and_duplicate_metadata_never_return_a_partial_result(self):
        backend = _backend({"root": (_entry("one"),)})
        with self.bind(backend) as root:
            backend.stream_failure = fs.PlatformFileError(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, retryable=False)
            self.assertFailure(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                lambda: self.observe(backend, root))
            self.assertEqual(backend.stream_closed, 1)
            backend.stream_failure = None
            backend.tree["root"] = (_entry("one"), _entry("one"))
            self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                lambda: self.observe(backend, root))

    def test_backend_cannot_return_an_existing_or_foreign_handle_as_a_new_descendant(self):
        backend = _backend({"root": (_entry("child", "directory"),)})
        other = _backend()
        with self.bind(backend) as root, self.bind(other) as foreign:
            observed = self.observe(backend, root)
            for returned in (root, foreign):
                backend.return_child = returned
                self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    lambda: self.descend(backend, root, observed, observed.entries[0]))
                self.assertFalse(returned.closed)

    def test_budget_counts_copied_ancestor_and_temporary_native_handles(self):
        limits = fs.DirectoryObservationLimits(maximum_directory_handles=4)
        backend = _backend({"root": (_entry("child", "directory"),)})
        backend.handle_cost = 2
        with self.bind(backend, limits) as root:
            observed = self.observe(backend, root, limits)
            child = self.descend(backend, root, observed, observed.entries[0], limits)
            self.assertEqual(backend.resources.active_handles, 4)
            backend.cursor_cost = 1
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.observe(backend, child, limits))
            child.close()
            self.observe(backend, root, limits)
            self.assertEqual(backend.resources.active_handles, 2)
        self.assertEqual(backend.resources.active_handles, 0)

    def test_resource_reservations_cannot_be_forged_reused_or_released_before_owner(self):
        budget = fs.DirectoryHandleBudget(1)
        with self.assertRaises(TypeError):
            fs.DirectoryHandleReservation(budget, 1, object())
        with budget.reserve() as reservation:
            self.assertEqual(budget.active_handles, 1)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: budget.reserve())
        reservation.close()
        self.assertEqual(budget.active_handles, 0)
        backend = _backend()
        with self.bind(backend) as root:
            reservation = root._handle_reservation
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, reservation.close)
            with self.assertRaises(ValueError):
                type(root)("forged", reservation)
            self.assertEqual(backend.resources.active_handles, 1)

    def test_default_depth_32_allows_33_single_handles_and_closing_root_releases_all(self):
        tree = {"root": (_entry("d1", "directory"),)}
        tree.update({f"d{i}": (_entry(f"d{i+1}", "directory"),) for i in range(1, 33)})
        backend = _backend(tree)
        with self.bind(backend) as root:
            directory = root
            for depth in range(1, 33):
                observed = self.observe(backend, directory)
                directory = self.descend(backend, directory, observed, observed.entries[0])
                self.assertEqual(directory.depth, depth)
            self.assertEqual(backend.resources.active_handles, 33)
            observed = self.observe(backend, directory)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.descend(backend, directory, observed, observed.entries[0]))
        self.assertEqual(backend.resources.active_handles, 0)
        self.assertTrue(all(item.closed and item.close_calls == 1 for item in backend.opened))

    def test_failed_native_close_keeps_budget_charged_and_prevents_extra_descendant(self):
        limits = fs.DirectoryObservationLimits(maximum_directory_handles=2)
        backend = _backend({"root": (_entry("a", "directory"), _entry("b", "directory"))})
        with self.bind(backend, limits) as root:
            observed = self.observe(backend, root, limits)
            child = self.descend(backend, root, observed, observed.entries[0], limits)
            child.close_failure = fs.PlatformFileError(
                fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
            )
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, child.close)
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.descend(backend, root, observed, observed.entries[1], limits))
            self.assertEqual(backend.resources.active_handles, 2)
            self.assertEqual(sum(item.native_live for item in backend.opened), 2)
            child.close()
            self.assertEqual(child.close_calls, 1)
        self.assertEqual(backend.resources.active_handles, 1)
        self.assertEqual(sum(item.native_live for item in backend.opened), 1)

    def test_failed_unbound_descendant_cleanup_keeps_budget_charged(self):
        limits = fs.DirectoryObservationLimits(maximum_directory_handles=2)
        backend = _backend({"root": (_entry("a", "directory"), _entry("b", "directory"))})
        with self.bind(backend, limits) as root:
            observed = self.observe(backend, root, limits)

            def invalidate_new_child_and_fail_close():
                child = backend.opened[-1]
                child.actual_identity = _identity("replacement")
                child.close_failure = fs.PlatformFileError(
                    fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
                )

            backend.after_open = invalidate_new_child_and_fail_close
            self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                lambda: self.descend(backend, root, observed, observed.entries[0], limits))
            self.assertIsNone(backend.opened[-1]._observation_scope)
            backend.after_open = None
            self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                lambda: self.descend(backend, root, observed, observed.entries[1], limits))
            self.assertEqual(backend.resources.active_handles, 2)
            self.assertEqual(sum(item.native_live for item in backend.opened), 2)
        self.assertEqual(backend.resources.active_handles, 1)

    def test_failed_unbound_root_cleanup_does_not_release_its_native_handle_charge(self):
        limits = fs.DirectoryObservationLimits(maximum_directory_handles=1)
        backend = _backend()

        def invalidate_root_and_fail_close():
            root = backend.opened[-1]
            root.actual_identity = _identity("not-a-directory", "regular")
            root.close_failure = fs.PlatformFileError(
                fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
            )

        backend.after_open = invalidate_root_and_fail_close
        self.assertFailure(fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
            lambda: self.bind(backend, limits))
        self.assertFailure(fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED,
            backend.resources.reserve)
        self.assertIsNone(backend.opened[-1]._observation_scope)
        self.assertEqual(backend.resources.active_handles, 1)
        self.assertTrue(backend.opened[-1].native_live)


if __name__ == "__main__":
    unittest.main()
