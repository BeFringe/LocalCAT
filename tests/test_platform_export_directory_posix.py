"""Real no-follow POSIX export directory preparation and materialization."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from platform_export_directory import ExportDirectoryBatch, ExportDirectoryCreationError
from platform_fs_contracts import PlatformFileError

if sys.platform != 'win32':
    from platform_fs_posix import PosixPlatformAdapter


@unittest.skipIf(sys.platform == 'win32', 'POSIX directory primitives')
class PosixExportDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve()
        self.backend = PosixPlatformAdapter()
        self.root = self.backend.bind_root(self.path)
        self.addCleanup(self.root.close)
        self.batch = ExportDirectoryBatch(self.backend, self.root)
        self.addCleanup(self.batch.close)

    def test_nested_preview_zero_mutation_then_shared_materialization(self):
        first = self.batch.prepare_descendant_target('a/one.rpy')
        second = self.batch.prepare_descendant_target('a/b/two.rpy')
        self.assertEqual(list(self.path.iterdir()), [])
        self.batch.prepare_directories()
        self.assertTrue(self.batch.closed)
        self.assertEqual(self.batch.created_directories, ('a', 'a/b'))
        self.assertTrue((self.path / 'a/b').is_dir())
        self.assertFalse((self.path / 'a/one.rpy').exists())

    def test_external_directory_creation_rejected(self):
        plan = self.batch.prepare_descendant_target('a/b/x.rpy')
        (self.path / 'a').mkdir()
        with self.assertRaises(PlatformFileError):
            self.batch.prepare_directories()
        self.assertFalse((self.path / 'a/b').exists())
        self.assertEqual(self.batch.created_directories, ())

    def test_existing_ancestor_replacement_rejected(self):
        (self.path / 'a').mkdir()
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        (self.path / 'a').rename(self.path / 'old')
        (self.path / 'a').mkdir()
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)
        self.assertFalse((self.path / 'a/x.rpy').exists())
        self.assertFalse((self.path / 'old/x.rpy').exists())

    def test_symlink_parent_rejected_in_prepare_and_after_preview(self):
        (self.path / 'other').mkdir()
        (self.path / 'link').symlink_to(self.path / 'other', target_is_directory=True)
        with self.assertRaises(PlatformFileError):
            self.batch.prepare_descendant_target('link/x.rpy')
        plan = self.batch.prepare_descendant_target('later/x.rpy')
        (self.path / 'later').symlink_to(self.path / 'other', target_is_directory=True)
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)
        self.assertEqual(list((self.path / 'other').iterdir()), [])

    def test_existing_target_and_absent_target_replacement_rejected(self):
        (self.path / 'exists.rpy').write_bytes(b'original')
        old = self.batch.prepare_descendant_target('exists.rpy')
        absent = self.batch.prepare_descendant_target('absent.rpy')
        (self.path / 'exists.rpy').unlink()
        (self.path / 'exists.rpy').write_bytes(b'external')
        (self.path / 'absent.rpy').write_bytes(b'external')
        for plan in (old, absent):
            with self.assertRaises(PlatformFileError):
                self.batch.materialize_target(plan)
        self.assertEqual((self.path / 'exists.rpy').read_bytes(), b'external')

    def test_create_then_swap_is_rejected_and_creation_is_reported(self):
        def fault(point):
            if point == 'export_directory_after_create':
                (self.path / 'a').rename(self.path / 'displaced')
                (self.path / 'a').mkdir()
        self.backend._fault_injector = fault
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        with self.assertRaises(ExportDirectoryCreationError):
            self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertEqual([(f.relative_path, f.outcome) for f in self.batch.directory_failures], [('a', 'uncertain')])
        self.assertEqual(list((self.path / 'a').iterdir()), [])
        self.assertEqual(list((self.path / 'displaced').iterdir()), [])

    def test_create_failure_has_no_target_and_retains_prior_directory_fact(self):
        plan = self.batch.prepare_descendant_target('a/b/x.rpy')
        original = os.mkdir
        def mkdir(name, *args, **kwargs):
            if name == 'b':
                raise PermissionError('injected failure')
            return original(name, *args, **kwargs)
        with mock.patch('platform_fs_posix.os.mkdir', mkdir):
            with self.assertRaises(PlatformFileError):
                self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertEqual([(f.relative_path, f.outcome) for f in self.batch.directory_failures], [('a/b', 'failed')])
        self.assertEqual(list((self.path / 'a').iterdir()), [])

    def test_cancel_between_components_keeps_created_directory_and_stops(self):
        class Cancellation:
            cancelled = False
            def raise_if_cancelled(self):
                if self.cancelled:
                    raise RuntimeError('cancelled')
        cancellation = Cancellation()
        self.backend._fault_injector = lambda point: setattr(cancellation, 'cancelled', True) if point == 'export_directory_after_create' else None
        plan = self.batch.prepare_descendant_target('a/b/x.rpy')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.batch.prepare_directories(cancellation)
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertFalse((self.path / 'a/b').exists())

    def test_retirement_parent_cannot_mint_export_directory(self):
        with self.backend.bind_or_create_child_directory(self.root, 'retirement') as parent:
            with self.assertRaises((TypeError, PlatformFileError)):
                self.backend.open_export_directory(parent, 'child', create=True)
        self.assertFalse((self.path / 'retirement/child').exists())

    def test_create_race_at_mkdir_does_not_adopt_external_directory(self):
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        original = os.mkdir
        def race(name, *args, **kwargs):
            original(name, *args, **kwargs)
            return original(name, *args, **kwargs)
        with mock.patch('platform_fs_posix.os.mkdir', race):
            with self.assertRaises(PlatformFileError):
                self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ())
        self.assertEqual(list((self.path / 'a').iterdir()), [])

    def test_swap_before_first_capture_needs_fresh_preview_before_file_binding(self):
        plan = self.batch.prepare_descendant_target('a/b/x.rpy')
        original = os.mkdir
        def swap_before_return(name, *args, **kwargs):
            original(name, *args, **kwargs)
            if name == 'a':
                (self.path / 'a').rename(self.path / 'our-displaced-a')
                original(name, *args, **kwargs)
        with mock.patch('platform_fs_posix.os.mkdir', swap_before_return):
            self.batch.prepare_directories()
        self.assertTrue(self.batch.closed)
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)
        self.assertFalse((self.path / 'a/b/x.rpy').exists())
        with ExportDirectoryBatch(self.backend, self.root) as fresh:
            new_plan = fresh.prepare_descendant_target('a/b/x.rpy')
            with fresh.materialize_target(new_plan) as target:
                target.reprove()
            self.assertFalse((self.path / 'a/b/x.rpy').exists())

    def test_all_new_descriptors_closed_after_create_open_failure(self):
        opened = set()
        native_open, native_dup, native_close = os.open, os.dup, os.close
        def track_open(*args, **kwargs):
            fd = native_open(*args, **kwargs)
            opened.add(fd)
            return fd
        def track_dup(fd):
            result = native_dup(fd)
            opened.add(result)
            return result
        def track_close(fd):
            native_close(fd)
            opened.discard(fd)
        def fail(point):
            if point == 'export_directory_after_open':
                raise OSError('injected open proof failure')
        self.backend._fault_injector = fail
        with mock.patch('platform_fs_posix.os.open', track_open), \
             mock.patch('platform_fs_posix.os.dup', track_dup), \
             mock.patch('platform_fs_posix.os.close', track_close):
            plan = self.batch.prepare_descendant_target('a/x.rpy')
            with self.assertRaises(ExportDirectoryCreationError):
                self.batch.prepare_directories()
            self.batch.close()
        self.assertEqual(opened, set())
        self.assertEqual(self.batch.created_directories, ('a',))


if __name__ == '__main__':
    unittest.main()
