"""Project discovery policy; platform syscall matrices live in platform tests."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from parser_composition import create_parser_application_surface
from parser_contracts import EffectivePurpose, SelectionHints, SelectionRequest
from platform_fs import compose_platform_file_backend
import platform_fs_contracts as fs
from project_codec_settings import CodecSettings, CodecProviderSetting, CodecSettingsRepository, compose_project_codec_runtime
from project_directory_contracts import (
    DirectoryRejectionCode, DirectorySelectionRequest,
)
from project_workspace_discovery import DirectoryDiscoveryError, ProjectDirectoryDiscoveryService
from tests.test_project_directory_observation import _forbid_content_and_mutation


def _identity(name, kind="regular", links=1):
    return fs.FileObjectIdentity("posix", b"volume", name.encode(), kind, links)


def _entry(name, *, kind=fs.DirectoryEntryKind.REGULAR, identity=None):
    if kind in (fs.DirectoryEntryKind.REGULAR, fs.DirectoryEntryKind.DIRECTORY):
        return fs.DirectoryEntryMetadata(name, kind, fs.EntrySnapshot(
            identity or _identity(name, kind.value), 7, b"original", True,
        ))
    reason = {fs.DirectoryEntryKind.SYMLINK: fs.DirectoryEntryUnavailableReason.LINK,
              fs.DirectoryEntryKind.REPARSE: fs.DirectoryEntryUnavailableReason.REPARSE,
              fs.DirectoryEntryKind.OTHER: fs.DirectoryEntryUnavailableReason.NOT_REGULAR}[kind]
    return fs.DirectoryEntryMetadata(name, kind, None, reason)


class _Directory(fs.RetainedObservationDirectory):
    def __init__(self, port, path, resources):
        super().__init__(resources.reserve())
        self.port, self.path = port, path
        port.handles.append(self)

    def _reprove_identity(self):
        if self.port.stale:
            raise fs.PlatformFileError(fs.PlatformFileErrorCode.IDENTITY_STALE, retryable=True)
        return _identity(self.path[-1] if self.path else "root", "directory")

    def _close_authority(self):
        pass


class _ObservationPort(fs.ReadOnlyDirectoryObservation):
    def __init__(self, tree):
        self.tree, self.handles, self.resources = tree, [], []
        self.stale = False
        self.hook = None
        self.observations = 0

    def _bind_observation_root(self, root, resources, cancellation):
        self.resources.append(resources)
        return _Directory(self, (), resources)

    def _iter_observed_children(self, directory, resources, cancellation):
        self.observations += 1
        with resources.reserve():
            for entry in self.tree[directory.path]:
                if self.hook:
                    self.hook(directory, cancellation)
                yield entry

    def _retain_observed_directory(self, parent, entry, resources, cancellation):
        return _Directory(self, parent.path + (entry.name,), resources)


class ProjectWorkspaceDiscoveryTests(unittest.TestCase):
    def service(self, port, **kwargs):
        service = ProjectDirectoryDiscoveryService(port, kwargs.pop("parser_surface", None)
                                                  or create_parser_application_surface(), **kwargs)
        self.addCleanup(service.close)
        return service

    def assertRejected(self, code, operation):
        with self.assertRaises(DirectoryDiscoveryError) as caught:
            operation()
        self.assertIs(caught.exception.rejection.code, code)

    def request(self, preview, *refs):
        by_ref = {entry.source_ref: entry.entry_id for entry in preview.entries}
        return DirectorySelectionRequest(preview.preview_id, preview.generation,
                                         tuple(by_ref[ref] for ref in refs))

    def test_native_nested_preview_and_order_are_metadata_only_and_keep_original_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for relative in ("act2/chapter.txt", "act1/scenes/chapter.txt", "Z.txt", "a.txt",
                             "ignored.unknown"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not parsed during discovery")
            backend = compose_platform_file_backend(root)
            service = self.service(backend)
            with _forbid_content_and_mutation(backend):
                preview = service.preview(root)
                self.assertTrue(preview.complete)
                self.assertEqual(preview.selected_entry_ids, ())
                self.assertEqual([e.source_ref for e in preview.entries], sorted(e.source_ref for e in preview.entries))
                by_ref = {entry.source_ref: entry for entry in preview.entries}
                self.assertEqual(by_ref["act1/scenes/chapter.txt"].parent_entry_id,
                                 by_ref["act1/scenes"].entry_id)
                self.assertFalse(by_ref["ignored.unknown"].selectable)
                with self.assertRaises(FrozenInstanceError):
                    preview.generation = 99
                self.assertRejected(DirectoryRejectionCode.EMPTY_SELECTION,
                                    lambda: service.select(self.request(preview)))
                selected = service.select(self.request(preview, "act2/chapter.txt", "act1/scenes/chapter.txt"))
                retained = service.revalidate(selected)
                self.assertEqual(retained.root_path, root)
                self.assertEqual(tuple(item.source_ref for item in retained.files),
                                 ("act2/chapter.txt", "act1/scenes/chapter.txt"))
                one = service.select(self.request(preview, "act1/scenes/chapter.txt"))
                self.assertEqual(service.revalidate(one).root_path, root)
                self.assertEqual(service.revalidate(one).files[0].source_ref, "act1/scenes/chapter.txt")
                self.assertRejected(DirectoryRejectionCode.STALE, retained.reprove)
            service.close()

    def test_configured_registry_filters_disabled_and_nonreadable_project_codecs(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with mock.patch.object(CodecSettingsRepository, "load", return_value=CodecSettings((CodecProviderSetting("localcat.rpy", enabled),))):
                    surface = compose_project_codec_runtime(Path("/unused")).surface
                port = _ObservationPort({(): (_entry("chapter.rpy"), _entry("chapter.txt"), _entry("terms.csv"))})
                preview = self.service(port, parser_surface=surface).preview(Path.cwd() / "chosen")
                entries = {e.source_ref: e for e in preview.entries}
                self.assertEqual(entries["chapter.rpy"].selectable, enabled)
                self.assertTrue(entries["chapter.txt"].selectable)
                self.assertFalse(entries["terms.csv"].selectable)
        descriptor = create_parser_application_surface().select(SelectionRequest(
            EffectivePurpose.PROJECT_DOCUMENT, hints=SelectionHints(extensions=(".txt",))))
        for readable in (False, True):
            with self.subTest(custom_readable=readable):
                custom = replace(descriptor, extensions=(".custom",), capabilities=replace(
                    descriptor.capabilities, readable=readable, validatable=readable),
                    reader_factory=descriptor.reader_factory if readable else None)
                surface = mock.Mock()
                surface.select.return_value = custom
                preview = self.service(_ObservationPort({(): (_entry("novel.custom"),)}),
                                       parser_surface=surface).preview(Path.cwd() / "chosen")
                self.assertEqual(preview.entries[0].selectable, readable)
                self.assertEqual(surface.select.call_args.args[0].hints.extensions, (".custom",))

    def test_unavailable_entries_never_descend_and_selection_collisions_are_rejected(self):
        blocked = (_entry("bad:name.txt"), _entry("shortcut", kind=fs.DirectoryEntryKind.SYMLINK),
                   _entry("junction", kind=fs.DirectoryEntryKind.REPARSE),
                   _entry("pipe", kind=fs.DirectoryEntryKind.OTHER),
                   _entry("NUL", kind=fs.DirectoryEntryKind.DIRECTORY))
        preview = self.service(_ObservationPort({(): blocked})).preview(Path.cwd() / "chosen")
        self.assertTrue(preview.complete)
        self.assertTrue(all(not entry.selectable and entry.unavailable_reason for entry in preview.entries))
        cases = (("chapter.txt", "CHAPTER.txt", None),
                 ("é.txt", "e\u0301.txt", None),
                 ("one.txt", "two.txt", _identity("alias", links=2)))
        for first, second, identity in cases:
            with self.subTest(first=first, second=second):
                port = _ObservationPort({(): (_entry(first, identity=identity), _entry(second, identity=identity))})
                service = self.service(port)
                preview = service.preview(Path.cwd() / "chosen")
                request = DirectorySelectionRequest(preview.preview_id, preview.generation,
                                                    tuple(e.entry_id for e in preview.entries))
                self.assertRejected(DirectoryRejectionCode.ALIAS, lambda: service.select(request))
                if identity:
                    self.assertRejected(DirectoryRejectionCode.ALIAS,
                        lambda: service.select(replace(request, entry_ids=request.entry_ids[:1])))

    def test_refresh_foreign_ids_cancel_close_and_root_drift_revoke_authority(self):
        port = _ObservationPort({(): (_entry("chapter.txt"),)})
        service = self.service(port)
        preview = service.preview(Path.cwd() / "chosen")
        selection = service.select(self.request(preview, "chapter.txt"))
        binding = service.revalidate(selection)
        foreign = self.service(_ObservationPort(port.tree))
        self.assertRejected(DirectoryRejectionCode.STALE, lambda: foreign.revalidate(selection))
        self.assertRejected(DirectoryRejectionCode.STALE, lambda: service.revalidate(replace(selection)))
        self.assertRejected(DirectoryRejectionCode.INVALID_SELECTION,
            lambda: service.select(DirectorySelectionRequest(preview.preview_id, preview.generation, ("../../escape",))))
        refreshed = service.preview(Path.cwd() / "next-root")
        self.assertGreater(refreshed.generation, preview.generation)
        self.assertEqual(refreshed.selected_entry_ids, ())
        self.assertTrue(port.handles[0].closed)
        self.assertRejected(DirectoryRejectionCode.STALE, binding.reprove)
        self.assertRejected(DirectoryRejectionCode.STALE, lambda: service.select(self.request(preview, "chapter.txt")))
        port.stale = True
        self.assertRejected(DirectoryRejectionCode.STALE, lambda: service.select(self.request(refreshed, "chapter.txt")))
        port.stale = False
        service.cancel()
        self.assertTrue(all(handle.closed for handle in port.handles))
        current = service.preview(Path.cwd() / "chosen")
        chosen = service.select(self.request(current, "chapter.txt"))
        service.close()
        self.assertRejected(DirectoryRejectionCode.CLOSED, lambda: service.revalidate(chosen))
        self.assertRejected(DirectoryRejectionCode.CLOSED, lambda: service.preview(Path.cwd() / "chosen"))
        self.assertTrue(all(resource.active_handles == 0 for resource in port.resources))

    def test_incomplete_scans_block_selection_and_release_all_resources(self):
        for cause in ("depth", "entries", "handles", "unreadable"):
            with self.subTest(cause=cause):
                port = _ObservationPort({(): (_entry("sub", kind=fs.DirectoryEntryKind.DIRECTORY),),
                                         ("sub",): (_entry("chapter.txt"),)})
                options = {"depth": {"maximum_depth": 0}, "entries": {"maximum_entries": 1},
                           "handles": {"maximum_directory_handles": 1}, "unreadable": {}}[cause]
                if cause == "unreadable":
                    def fail(directory, cancellation):
                        raise fs.PlatformFileError(fs.PlatformFileErrorCode.ENTRY_UNAVAILABLE, retryable=True)
                    port.hook = fail
                service = self.service(port, limits=fs.DirectoryObservationLimits(**options))
                preview = service.preview(Path.cwd() / "chosen")
                self.assertFalse(preview.complete)
                self.assertIsNotNone(preview.rejection)
                self.assertRejected(DirectoryRejectionCode.INCOMPLETE, lambda: service.select(self.request(preview)))
                self.assertTrue(all(handle.closed for handle in port.handles))
                self.assertTrue(all(resource.active_handles == 0 for resource in port.resources))

    def test_unexpected_consumer_failure_releases_retained_root(self):
        port = _ObservationPort({(): (_entry("chapter.txt"),)})
        surface = mock.Mock()
        surface.select.side_effect = RuntimeError("consumer failure")
        service = self.service(port, parser_surface=surface)
        with self.assertRaisesRegex(RuntimeError, "consumer failure"):
            service.preview(Path.cwd() / "chosen")
        self.assertTrue(all(handle.closed for handle in port.handles))
        self.assertTrue(all(resource.active_handles == 0 for resource in port.resources))

    def test_full_entry_budget_is_not_rescanned_to_issue_metadata_selection(self):
        port = _ObservationPort({(): tuple(_entry(f"{index:05}.txt") for index in range(10000))})
        service = self.service(port)
        preview = service.preview(Path.cwd() / "chosen")
        self.assertTrue(preview.complete)
        selected = service.select(self.request(preview, "09999.txt"))
        binding = service.revalidate(selected)
        self.assertEqual(binding.files[0].snapshot.byte_count, 7)
        self.assertEqual(port.observations, 1)
        self.assertEqual(len(port.resources), 1)

    def test_cancel_during_scan_discards_late_result_and_all_handles(self):
        port = _ObservationPort({(): (_entry("sub", kind=fs.DirectoryEntryKind.DIRECTORY),),
                                 ("sub",): (_entry("chapter.txt"),)})
        entered, proceed = threading.Event(), threading.Event()
        def pause(directory, cancellation):
            if directory.path:
                entered.set()
                self.assertTrue(proceed.wait(5))
        port.hook = pause
        service = self.service(port)
        outcomes = []
        def scan():
            try:
                outcomes.append(service.preview(Path.cwd() / "chosen"))
            except DirectoryDiscoveryError as error:
                outcomes.append(error.rejection)
        worker = threading.Thread(target=scan)
        worker.start()
        self.assertTrue(entered.wait(5))
        # Cancellation must signal promptly even while native observation owns its lock.
        service.cancel()
        proceed.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIs(outcomes[0].code, DirectoryRejectionCode.CANCELLED)
        self.assertTrue(all(handle.closed for handle in port.handles))
        self.assertTrue(all(resource.active_handles == 0 for resource in port.resources))

        for action in ("cancel", "close"):
            with self.subTest(exit_window=action):
                port = _ObservationPort({(): (_entry("chapter.txt"),)})
                service = self.service(port)
                entered, proceed = threading.Event(), threading.Event()
                original_cleanup = service._cleanup_retired

                def pause_after_cleanup():
                    original_cleanup()
                    if (not entered.is_set() and service._run is not None
                            and service._run.preview is not None):
                        entered.set()
                        self.assertTrue(proceed.wait(5))

                outcomes = []
                with mock.patch.object(service, "_cleanup_retired", side_effect=pause_after_cleanup):
                    worker = threading.Thread(target=scan)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        # Revoke after the last cleanup, before the operation
                        # relinquishes ownership. No later service call repairs it.
                        getattr(service, action)()
                    finally:
                        proceed.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(all(handle.closed for handle in port.handles))
                self.assertTrue(all(resource.active_handles == 0 for resource in port.resources))


if __name__ == "__main__":
    unittest.main()
