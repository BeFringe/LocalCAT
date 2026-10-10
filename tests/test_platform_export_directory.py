"""Nested export plans preserve preview conditions, using a primitive fake."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import PurePath
import unittest
from unittest import mock

import platform_export_directory as export
from platform_fs_contracts import EntrySnapshot, FileObjectIdentity, PlatformFileError
from tests.test_platform_fs_contracts import _Directory, _Regular, _RetirementDirectory


def snapshot(key, kind='directory'):
    return EntrySnapshot(FileObjectIdentity('posix', b'v', key.encode(), kind, 1), 0, b'v1', True)


class Directory(_Directory):
    def __init__(self, backend, path):
        super().__init__()
        self.backend, self.path = backend, path
        self.original = backend.tree[path]

    def _reprove(self):
        if self.backend.tree.get(self.path) != self.original:
            raise export._stale()

    def _inspect_entry(self, name):
        self._reprove()
        return self.backend.tree.get(self.path + '/' + name)


class Backend:
    def __init__(self):
        self.tree = {'': snapshot('root')}
        self.opened = []
        self.created = []
        self.root = self.retain('')

    def retain(self, path):
        result = Directory(self, path)
        self.opened.append(result)
        return result

    def duplicate_export_directory(self, parent):
        parent.reprove()
        return self.retain(parent.path)

    def open_export_directory(self, parent, name, *, create=False):
        parent.reprove()
        path = parent.path + '/' + name
        if create:
            if path in self.tree:
                raise export._stale()
            self.tree[path] = snapshot(path)
            self.created.append(path)
        return self.retain(path)

    def open_regular(self, root, relative):
        backend = self
        path = '/' + relative.as_posix()

        class Regular(_Regular):
            def _snapshot(self):
                return backend.tree[path]

        result = Regular()
        self.opened.append(result)
        return result


class ExportDirectoryContractTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.batch = export.ExportDirectoryBatch(self.backend, self.backend.root)
        self.addCleanup(self.batch.close)
        self.addCleanup(self.backend.root.close)

    def test_preview_is_read_only_and_plan_is_immutable(self):
        plan = self.batch.prepare_descendant_target('a/b/chapter.rpy')
        self.assertEqual(plan.missing_directories, ('a', 'b'))
        self.assertIsNone(plan.expected)
        self.assertEqual(self.backend.created, [])
        with self.assertRaises(FrozenInstanceError):
            plan.relative_path = 'other.rpy'

    def test_directory_preparation_consumes_batch_without_target_authority(self):
        first = self.batch.prepare_descendant_target('a/one.rpy')
        self.batch.prepare_descendant_target('a/b/two.rpy')
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(first)
        self.assertEqual(self.backend.created, [])
        created = self.batch.prepare_directories()
        self.assertEqual(created, ('a', 'a/b'))
        self.assertTrue(self.batch.closed)
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(first)
        with export.ExportDirectoryBatch(self.backend, self.backend.root) as fresh:
            plan = fresh.prepare_descendant_target('a/b/two.rpy')
            self.assertEqual(plan.missing_directories, ())
            with fresh.materialize_target(plan) as target:
                target.reprove()
        self.assertNotIn('/a/b/two.rpy', self.backend.tree)

    def test_external_directory_or_target_creation_is_stale(self):
        for changed, kind in (('/a', 'directory'), ('/target.rpy', 'regular')):
            with self.subTest(changed=changed):
                backend = Backend()
                with export.ExportDirectoryBatch(backend, backend.root) as batch:
                    plan = batch.prepare_descendant_target('a/x.rpy' if kind == 'directory' else 'target.rpy')
                    backend.tree[changed] = snapshot('external', kind)
                    with self.assertRaises(PlatformFileError):
                        batch.materialize_target(plan)
                    self.assertEqual(batch.created_directories, ())
                backend.root.close()

    def test_preparation_rechecks_all_plans_before_creating_any_directory(self):
        self.batch.prepare_descendant_target('a/new.rpy')
        self.batch.prepare_descendant_target('b/new.rpy')
        self.backend.tree['/b'] = snapshot('external')
        with self.assertRaises(PlatformFileError):
            self.batch.prepare_directories()
        self.assertEqual(self.backend.created, [])
        self.assertTrue(self.batch.closed)

    def test_missing_directory_blocks_binding_even_for_existing_parent_plan(self):
        existing = self.batch.prepare_descendant_target('existing.rpy')
        self.batch.prepare_descendant_target('a/new.rpy')
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(existing)
        self.assertFalse(self.batch.closed)
        self.assertEqual(self.batch.prepare_directories(), ('a',))

    def test_existing_target_and_ancestor_replacement_rejected(self):
        self.backend.tree['/a'] = snapshot('a')
        self.backend.tree['/a/x.rpy'] = snapshot('x', 'regular')
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        plan2 = self.batch.prepare_descendant_target('a/y.rpy')
        self.backend.tree['/a/x.rpy'] = snapshot('replacement', 'regular')
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)
        self.backend.tree['/a'] = snapshot('replacement-directory')
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan2)

    def test_foreign_cloned_and_consumed_plans_rejected(self):
        plan = self.batch.prepare_descendant_target('a.rpy')
        with export.ExportDirectoryBatch(self.backend, self.backend.root) as other:
            for forged in (plan, _RetirementDirectory()):
                with self.assertRaises((TypeError, PlatformFileError)):
                    other.materialize_target(forged)
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(replace(plan))
        self.batch.materialize_target(plan).close()
        with self.assertRaises(PlatformFileError):
            self.batch.materialize_target(plan)

    def test_cancel_closes_resources_and_does_not_create(self):
        plan = self.batch.prepare_descendant_target('a/b/x.rpy')

        class Cancelled:
            def raise_if_cancelled(self):
                raise RuntimeError('cancelled')

        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.batch.prepare_directories(Cancelled())
        self.assertEqual(self.backend.created, [])
        self.batch.close()
        self.assertTrue(all(item.closed for item in self.backend.opened if item is not self.backend.root))

    def test_duplicate_case_and_file_ancestor_conflicts_are_rejected(self):
        self.batch.prepare_descendant_target('a/X.rpy')
        for value in ('a/X.rpy', 'A/x.rpy', 'A/other.rpy', 'a/X.rpy/child.rpy', 'a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.batch.prepare_descendant_target(value)

    def test_failure_after_directory_creation_reports_mutation_without_reuse(self):
        plan = self.batch.prepare_descendant_target('a/x.rpy')
        original = self.backend.open_export_directory

        def fail(parent, name, *, create=False):
            child = original(parent, name, create=create)
            child.close()
            raise export.ExportDirectoryCreationError()

        self.backend.open_export_directory = fail
        with self.assertRaises(export.ExportDirectoryCreationError):
            self.batch.prepare_directories()
        self.assertEqual(self.batch.created_directories, ('a',))
        self.assertNotIn('/a/x.rpy', self.backend.tree)
        self.batch.close()
        self.assertTrue(all(item.closed for item in self.backend.opened if item is not self.backend.root))

    def test_parent_close_failure_releases_child_during_prepare_and_materialize(self):
        for operation in ('prepare', 'materialize'):
            with self.subTest(operation=operation):
                backend = Backend()
                with export.ExportDirectoryBatch(backend, backend.root) as batch:
                    if operation == 'prepare':
                        backend.tree['/a'] = snapshot('a')
                    else:
                        plan = batch.prepare_descendant_target('a/x.rpy')
                    failed = False

                    def close_failure(directory):
                        nonlocal failed
                        if directory.path == '' and directory is not backend.root and not failed:
                            failed = True
                            raise RuntimeError('close failure')

                    with mock.patch.object(Directory, '_close_authority', close_failure):
                        with self.assertRaisesRegex(RuntimeError, 'close failure'):
                            if operation == 'prepare':
                                batch.prepare_descendant_target('a/x.rpy')
                            else:
                                batch.prepare_directories()
                self.assertTrue(all(item.closed for item in backend.opened if item is not backend.root))
                backend.root.close()

    def test_unsafe_and_nonportable_paths_are_rejected(self):
        for value in ('../x', '/x', 'a//x', 'a/./x', 'a\\x', 'CON.rpy', 'x:ads', 'x. ', 'a/' * 33 + 'x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.batch.prepare_descendant_target(value)


if __name__ == '__main__':
    unittest.main()
