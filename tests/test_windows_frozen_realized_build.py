"""Build reproduction is required evidence, not a W3 acceptance shortcut."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import tools.windows_frozen_realized_build as realized

from tools.windows_frozen_realized_build import (
    RealizedBuildError, compare_dist_bytes, check_native_projection,
    CLASSIFICATION,
)


class RealizedBuildTests(unittest.TestCase):
    def test_claim_is_build_only(self):
        self.assertIn('NOT_W3_ACCEPTANCE', CLASSIFICATION)

    def test_byte_comparison_requires_two_distinct_trees(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            with self.assertRaises(RealizedBuildError):
                compare_dist_bytes(root, root, {'a': {}})

    def test_each_dist_member_compared_not_only_exe(self):
        with tempfile.TemporaryDirectory() as work:
            roots = [Path(work)/name for name in ('first', 'second')]
            for root in roots:
                root.mkdir(); (root/'app.exe').write_bytes(b'pe'); (root/'fixture').write_bytes(b'same')
            members = {'app.exe': {}, 'fixture': {}}
            compare_dist_bytes(*roots, members)
            (roots[1]/'fixture').write_bytes(b'diff')
            with self.assertRaisesRegex(RealizedBuildError, 'fixture'):
                compare_dist_bytes(*roots, members)

    def test_native_projection_compares_full_expected_closure(self):
        expected = {'root':'python314.dll','local_members':[{'name':'python314.dll'}],
                    'external_import_names':['KERNEL32.DLL']}
        check_native_projection(expected, expected)
        for bad in ({**expected, 'external_import_names':['EVIL.DLL']},
                    {**expected, 'local_members':[]}, {**expected, 'extra':True}):
            with self.assertRaises(RealizedBuildError):
                check_native_projection(bad, expected)

    def test_releases_must_match_before_inventory_or_observation(self):
        with tempfile.TemporaryDirectory() as work:
            a, b = Path(work)/'a', Path(work)/'b'; a.mkdir(); b.mkdir()
            releases = (a/'release.json', b/'release.json')
            releases[0].write_bytes(b'first'); releases[1].write_bytes(b'second')
            args = SimpleNamespace(first_build=a, second_build=b,
                first_release=releases[0], second_release=releases[1])
            with mock.patch.object(realized, 'verify_release_binding') as verifier:
                with self.assertRaisesRegex(RealizedBuildError, 'releases differ'):
                    realized.realize(args)
                verifier.assert_not_called()

    def test_external_anchor_is_not_taken_from_child_release(self):
        with tempfile.TemporaryDirectory() as work:
            a, b = Path(work)/'a', Path(work)/'b'; a.mkdir(); b.mkdir()
            releases = (a/'release.json', b/'release.json')
            for p in releases:
                p.write_bytes(b'{}')
            args = SimpleNamespace(first_build=a, second_build=b,
                first_release=releases[0], second_release=releases[1],
                expected_release_sha256='0'*64, expected_commit='1'*40,
                expected_candidate_digest='2'*64)
            with self.assertRaisesRegex(ValueError, 'external release anchor mismatch'):
                realized.realize(args)

    def test_hardlinked_member_is_not_independent(self):
        import os
        with tempfile.TemporaryDirectory() as work:
            a, b = Path(work)/'a', Path(work)/'b'; a.mkdir(); b.mkdir()
            (a/'file').write_bytes(b'same'); os.link(a/'file', b/'file')
            with self.assertRaisesRegex(RealizedBuildError, 'share a member identity'):
                compare_dist_bytes(a, b, {'file': {}})


if __name__ == '__main__':
    unittest.main()
