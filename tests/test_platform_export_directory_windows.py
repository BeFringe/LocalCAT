"""Windows export directory stage and current-identity checks."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from platform_export_directory import ExportDirectoryBatch, ExportDirectoryCreationError
from platform_fs_contracts import PlatformFileError
from platform_fs_windows import WindowsPlatformAdapter
import platform_fs_windows as native


class WindowsExportSurfaceTests(unittest.TestCase):
    def test_only_ordinary_authority_can_enter_export_primitives(self):
        adapter = WindowsPlatformAdapter()
        for call in (
            lambda: adapter.duplicate_export_directory(object()),
            lambda: adapter.open_export_directory(object(), 'child', create=True),
        ):
            with self.assertRaises(PlatformFileError):
                call()


@unittest.skipUnless(sys.platform == 'win32', 'native Windows directory handles')
class WindowsExportDirectoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve()
        self.backend = WindowsPlatformAdapter()
        self.root = self.backend.bind_root(self.path)
        self.addCleanup(self.root.close)
        self.batch = ExportDirectoryBatch(self.backend, self.root)
        self.addCleanup(self.batch.close)

    def test_prepare_then_fresh_preview_binds_nested_targets(self):
        old = self.batch.prepare_descendant_target('a/one.rpy')
        self.batch.prepare_descendant_target('a/b/two.rpy')
        self.assertEqual(list(self.path.iterdir()), [])
        self.assertEqual(self.batch.prepare_directories(), ('a', 'a/b'))
        self.assertTrue(self.batch.closed)
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(old)
        with ExportDirectoryBatch(self.backend, self.root) as fresh:
            new = fresh.prepare_descendant_target('a/b/two.rpy')
            with fresh.materialize_target(new) as target:
                target.reprove()
        self.assertFalse((self.path / 'a/b/two.rpy').exists())

    def test_external_creator_rejected_before_mutation(self):
        self.batch.prepare_descendant_target('a/b/x.rpy')
        (self.path / 'a').mkdir()
        with self.assertRaises(PlatformFileError):
            self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ())
        self.assertFalse((self.path / 'a/b').exists())

    def test_absent_target_creation_is_stale(self):
        plan = self.batch.prepare_descendant_target('x.rpy')
        (self.path / 'x.rpy').write_bytes(b'external')
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)
        self.assertEqual((self.path / 'x.rpy').read_bytes(), b'external')

    def test_existing_parent_and_target_are_retained_until_close(self):
        (self.path / 'a').mkdir()
        (self.path / 'a/x.rpy').write_bytes(b'old')
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        with self.assertRaises(PermissionError):
            (self.path / 'a').rename(self.path / 'moved')
        with self.assertRaises(PermissionError):
            (self.path / 'a/x.rpy').unlink()
        target = self.batch.materialize_target(plan)
        target.reprove()
        target.close()
        self.batch.close()
        self.root.close()  # The batch borrows its caller-owned root.
        (self.path / 'a/x.rpy').unlink()
        (self.path / 'a').rename(self.path / 'moved')

    def test_creation_failure_reports_exact_path(self):
        self.batch.prepare_descendant_target('a/b/x.rpy')
        original = self.backend.open_export_directory
        def fail(parent, name, *, create=False):
            if name == 'b' and create:
                raise native._capability_unavailable()
            return original(parent, name, create=create)
        with mock.patch.object(self.backend, 'open_export_directory', fail):
            with self.assertRaises(PlatformFileError):
                self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertEqual([(f.relative_path, f.outcome) for f in self.batch.directory_failures], [('a/b', 'failed')])

    def test_post_creation_failure_records_uncertainty_and_releases_handles(self):
        def fail(point):
            if point == 'export_directory_after_open':
                raise OSError('injected proof failure')
        self.backend._fault_injector = fail
        self.batch.prepare_descendant_target('a/x.rpy')
        with self.assertRaises(ExportDirectoryCreationError):
            self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertEqual(self.batch.directory_failures[0].outcome, 'uncertain')
        (self.path / 'a').rmdir()

    def test_cancel_between_components_leaves_facts_and_no_file(self):
        class Cancellation:
            cancelled = False
            def raise_if_cancelled(self):
                if self.cancelled:
                    raise RuntimeError('cancelled')
        cancellation = Cancellation()
        self.backend._fault_injector = lambda point: setattr(cancellation, 'cancelled', True) if point == 'export_directory_after_open' else None
        self.batch.prepare_descendant_target('a/b/x.rpy')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.batch.prepare_directories(cancellation)
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertFalse((self.path / 'a/b').exists())

    def test_swap_after_first_capture_is_rejected(self):
        def swap(point):
            if point == 'export_directory_after_create':
                (self.path / 'a').rename(self.path / 'displaced')
                (self.path / 'a').mkdir()
        self.backend._fault_injector = swap
        self.batch.prepare_descendant_target('a/x.rpy')
        with self.assertRaises(ExportDirectoryCreationError):
            self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertEqual(list((self.path / 'a').iterdir()), [])

    def test_create_race_rejects_existing_directory(self):
        self.batch.prepare_descendant_target('a/x.rpy')
        original = self.backend.open_export_directory
        def race(parent, name, *, create=False):
            if create:
                (self.path / name).mkdir()
            return original(parent, name, create=create)
        with mock.patch.object(self.backend, 'open_export_directory', race):
            with self.assertRaises(PlatformFileError):
                self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ())
        self.assertEqual(self.batch.directory_failures[0].outcome, 'failed')

    def test_retirement_authority_is_not_export_authority(self):
        with self.backend.bind_or_create_child_directory(self.root, 'retirement') as parent:
            with self.assertRaises(PlatformFileError):
                self.backend.open_export_directory(parent, 'child', create=True)
        self.assertFalse((self.path / 'retirement/child').exists())


if __name__ == '__main__':
    unittest.main()
