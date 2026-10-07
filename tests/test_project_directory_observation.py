"""One Project-facing consumer exercised against the composed native backend.

This is the integration boundary before Project discovery/selection exists;
adapter syscall, reparse and resource-limit matrices stay in platform tests.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import io
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from platform_fs import compose_platform_file_backend
import platform_fs_contracts as fs


class _ProjectObservationConsumer:
    """Test consumer using only the narrow public observation contract."""

    def __init__(self, port: fs.ReadOnlyDirectoryObservation, *, limits=None):
        self.port = port
        self.limits = limits or fs.DirectoryObservationLimits()
        self.cancellation = fs.DirectoryObservationCancellation()
        self.retained = []

    def bind(self, path):
        root = self.port.bind_observation_root(path, self.limits, self.cancellation)
        self.retained.append(root)
        return root

    def observe(self, directory):
        return self.port.observe_children(directory, self.limits, self.cancellation)

    def descend(self, parent, observation, entry):
        child = self.port.retain_observed_directory(
            parent, observation, entry, self.limits, self.cancellation,
        )
        self.retained.append(child)
        return child

    def scan(self, path):
        def walk(directory, relative):
            observation = self.observe(directory)
            records = [(relative, observation)]
            for entry in sorted(observation.entries, key=lambda item: item.name):
                if entry.kind is fs.DirectoryEntryKind.DIRECTORY and entry.unavailable_reason is None:
                    with self.descend(directory, observation, entry) as child:
                        records.extend(walk(child, relative / entry.name))
            return tuple(records)

        with self.bind(path) as root:
            return walk(root, PurePosixPath())


@contextmanager
def _forbid_content_and_mutation(backend):
    """Native spies belong to the fixture, never to the shared consumer."""
    targets = [("builtins", "open"), (io, "open")]
    if os.name == "nt":
        api = backend._native_api()
        targets.extend((api, name) for name in (
            "ReadFile", "WriteFile", "CreateDirectoryW", "SetFileInformationByHandle", "LockFileEx",
        ))
    else:
        import fcntl

        targets.extend((os, name) for name in (
            "read", "pread", "write", "pwrite", "mkdir", "unlink", "rename", "replace", "fsync",
        ))
        targets.append((fcntl, "flock"))
    with ExitStack() as stack:
        spies = []
        for owner, name in targets:
            patcher = mock.patch(f"{owner}.{name}") if isinstance(owner, str) else mock.patch.object(owner, name)
            spy = stack.enter_context(patcher)
            spy.side_effect = AssertionError("metadata observation attempted content access or mutation")
            spies.append(spy)
        yield
        for spy in spies:
            spy.assert_not_called()


@contextmanager
def _cancel_in_descendant_stream(backend, cancellation):
    """Cancel after native metadata arrives, before a complete observation issues."""
    native_iterator = backend._iter_observed_children

    def interrupted(directory, resources, token):
        stream = native_iterator(directory, resources, token)
        try:
            for entry in stream:
                if directory.depth > 0:
                    cancellation.cancel()
                yield entry
        finally:
            stream.close()

    with mock.patch.object(backend, "_iter_observed_children", side_effect=interrupted):
        yield


class ProjectDirectoryObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS /var is a symlink; the selected native root must be canonical.
        self.root_path = Path(temporary.name).resolve() / "selected-root"
        self.root_path.mkdir()
        self.sources = {
            "chapter.txt": b"root source",
            "act1/chapter.txt": b"first chapter",
            "act1/scenes/chapter.txt": b"nested chapter",
            "act1/scenes/deeper/notes.unknown": b"unselected unknown format",
            "act2/chapter.txt": b"same name, different source",
        }
        for relative, body in self.sources.items():
            path = self.root_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.backend = compose_platform_file_backend(self.root_path)
        self.assertIsInstance(self.backend, fs.ReadOnlyDirectoryObservation)

    def consumer(self, *, limits=None):
        return _ProjectObservationConsumer(self.backend, limits=limits)

    def assertFailure(self, code, operation):
        with self.assertRaises(fs.PlatformFileError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code.value)
        self.assertEqual(str(caught.exception), code.value)

    def test_nested_metadata_preserves_root_and_entry_facts_without_content_or_writer(self):
        consumer = self.consumer()
        with _forbid_content_and_mutation(self.backend):
            records = consumer.scan(self.root_path)
            observations = dict(records)
            root_identity = observations[PurePosixPath()].directory_identity
            files = {}
            for relative, observation in records:
                self.assertEqual(observation.root_identity, root_identity)
                for entry in observation.entries:
                    self.assertIsNone(entry.unavailable_reason)
                    self.assertTrue(entry.snapshot.reparse_free)
                    self.assertEqual(entry.snapshot.identity.kind, entry.kind.value)
                    if entry.kind is fs.DirectoryEntryKind.DIRECTORY:
                        self.assertEqual(
                            observations[relative / entry.name].directory_identity,
                            entry.snapshot.identity,
                        )
                    else:
                        files[(relative / entry.name).as_posix()] = entry.snapshot
            self.assertEqual(set(files), set(self.sources))
            self.assertEqual(len({snapshot.identity for snapshot in files.values()}), len(files))
            for relative, snapshot in files.items():
                self.assertEqual(snapshot.byte_count, len(self.sources[relative]))

            # Even live observation handles and their issued values cannot be
            # promoted to the rooted reader/writer path by the aggregate backend.
            with consumer.bind(self.root_path) as root:
                observation = consumer.observe(root)
                entry = next(item for item in observation.entries if item.name == "act1")
                with consumer.descend(root, observation, entry) as child:
                    self.assertEqual(root.identity(), root_identity)
                    self.assertEqual(child.identity(), entry.snapshot.identity)
                    for value in (root, child, observation, entry, entry.snapshot):
                        self.assertNotIsInstance(value, fs.BoundDirectoryAuthority)
                        for operation in (self.backend.open_regular, self.backend.bind_parent):
                            with self.assertRaises(TypeError):
                                operation(value, PurePosixPath("chapter.txt"))
                        for name in ("read_all", "create_candidate", "begin_publish", "prepare"):
                            self.assertFalse(hasattr(value, name))
        self.assertTrue(all(directory.closed for directory in consumer.retained))
        for relative, body in self.sources.items():
            self.assertEqual((self.root_path / relative).read_bytes(), body)

    def test_equal_metadata_from_stale_or_foreign_observation_cannot_authorize_descent(self):
        consumer = self.consumer()
        with consumer.bind(self.root_path) as root, consumer.bind(self.root_path) as other_root:
            stale = consumer.observe(root)
            current = consumer.observe(root)
            foreign = consumer.observe(other_root)
            self.assertEqual(stale, current)
            self.assertEqual(foreign, current)
            current_entry = next(item for item in current.entries if item.name == "act1")
            for label, observation, entry in (
                ("previous observation", stale, next(item for item in stale.entries if item.name == "act1")),
                ("foreign observation", foreign, next(item for item in foreign.entries if item.name == "act1")),
                ("foreign entry", current, next(item for item in foreign.entries if item.name == "act1")),
            ):
                with self.subTest(case=label):
                    self.assertFailure(fs.PlatformFileErrorCode.IDENTITY_STALE,
                                       lambda: consumer.descend(root, observation, entry))
            with consumer.descend(root, current, current_entry) as child:
                self.assertEqual(child.identity(), current_entry.snapshot.identity)

    def test_close_and_cancel_revoke_further_observation_and_descent(self):
        for action, code in (
            ("close", fs.PlatformFileErrorCode.CAPABILITY_UNAVAILABLE),
            ("cancel", fs.PlatformFileErrorCode.OBSERVATION_CANCELLED),
        ):
            with self.subTest(action=action):
                consumer = self.consumer()
                with consumer.bind(self.root_path) as root:
                    observation = consumer.observe(root)
                    entry = next(item for item in observation.entries if item.name == "act1")
                    child = consumer.descend(root, observation, entry)
                    if action == "close":
                        root.close()
                    else:
                        consumer.cancellation.cancel()
                    for directory in (root, child):
                        self.assertFailure(code, lambda: consumer.observe(directory))
                    self.assertFailure(code, lambda: consumer.descend(root, observation, entry))
                self.assertTrue(all(directory.closed for directory in consumer.retained))

    def test_recursive_failure_never_returns_a_successful_prefix_and_closes_ancestors(self):
        for failure, code in (
            ("cancel", fs.PlatformFileErrorCode.OBSERVATION_CANCELLED),
            ("entry limit", fs.PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED),
        ):
            with self.subTest(failure=failure):
                consumer = self.consumer(limits=fs.DirectoryObservationLimits(
                    maximum_entries=3 if failure == "entry limit" else 10000,
                ))
                fault = (_cancel_in_descendant_stream(self.backend, consumer.cancellation)
                         if failure == "cancel" else nullcontext())
                result = None
                with fault, self.assertRaises(fs.PlatformFileError) as caught:
                    result = consumer.scan(self.root_path)
                self.assertEqual(caught.exception.code, code.value)
                self.assertIsNone(result)
                self.assertGreater(len(consumer.retained), 1)  # Failure follows a completed root observation.
                self.assertTrue(all(directory.closed for directory in consumer.retained))


if __name__ == "__main__":
    unittest.main()
