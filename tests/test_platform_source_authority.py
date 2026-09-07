from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from platform_fs_contracts import PlatformFileError
from platform_source_authority import compose_rooted_source_authority


class RootedSourceAuthorityTests(unittest.TestCase):
    def test_real_backend_binds_exact_bytes_and_reproves_current_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            content = b"VALUE = 1\n"
            source_path.write_bytes(content)

            authority = compose_rooted_source_authority(root)
            try:
                source = authority.bind_path(source_path)

                self.assertEqual(source.path, source_path)
                self.assertEqual(source.content, content)
                self.assertEqual(
                    source.content_sha256,
                    hashlib.sha256(content).digest(),
                )
                self.assertTrue(source.is_current())
            finally:
                authority.close()

    def test_outside_locator_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            source_path.write_bytes(b"VALUE = 1\n")
            authority = compose_rooted_source_authority(root)
            try:
                _ = authority.bind_path(source_path)

                with self.assertRaises(ValueError):
                    authority.bind_path(root.parent / "foreign.py")
            finally:
                authority.close()

    def test_replaced_or_mutated_name_cannot_remain_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            source_path.write_bytes(b"VALUE = 1\n")
            authority = compose_rooted_source_authority(root)
            try:
                source = authority.bind_path(source_path)
                replacement = root / "replacement.py"
                replacement.write_bytes(b"VALUE = 2\n")
                try:
                    os.replace(replacement, source_path)
                except PermissionError:
                    # Windows may deny replacement while the retained source
                    # handle is open.  If in-place mutation is also denied, the
                    # original binding remains current because both attacks
                    # were blocked by the platform handle.
                    try:
                        source_path.write_bytes(b"VALUE = 2\n")
                    except PermissionError:
                        self.assertTrue(source.is_current())
                        return

                self.assertFalse(source.is_current())
                with self.assertRaises(PlatformFileError):
                    authority.bind_path(source_path)
            finally:
                authority.close()

    def test_proof_window_rejects_a_source_changed_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            source_path.write_bytes(b"VALUE = 1\n")
            authority = compose_rooted_source_authority(root)
            try:
                with self.assertRaises(PlatformFileError):
                    with authority.proof_window():
                        _ = authority.bind_path(source_path)
                        try:
                            source_path.write_bytes(b"VALUE = 2\n")
                        except PermissionError:
                            # Windows can deny the attack while the retained
                            # source handle is open.  In that case there is no
                            # changed source for the terminal proof to reject.
                            raise unittest.SkipTest(
                                "platform retained handle denied source mutation"
                            )
            finally:
                authority.close()

    def test_proof_window_cache_does_not_cross_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            source_path.write_bytes(b"VALUE = 1\n")
            authority = compose_rooted_source_authority(root)
            try:
                with authority.proof_window():
                    source = authority.bind_path(source_path)
                    self.assertTrue(source.is_current())

                replacement = root / "replacement.py"
                replacement.write_bytes(b"VALUE = 2\n")
                try:
                    os.replace(replacement, source_path)
                except PermissionError:
                    try:
                        source_path.write_bytes(b"VALUE = 2\n")
                    except PermissionError:
                        self.assertTrue(source.is_current())
                        return

                with self.assertRaises(PlatformFileError):
                    with authority.proof_window():
                        self.assertFalse(source.is_current())
            finally:
                authority.close()

    def test_root_reproof_is_reused_only_inside_one_proof_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority = compose_rooted_source_authority(root)
            root_authority = authority._RootedSourceAuthority__root
            root_type = type(root_authority)
            original_reprove = root_type._reprove
            reproof_count = 0

            def counted_reprove(current: object) -> None:
                nonlocal reproof_count
                reproof_count += 1
                original_reprove(current)

            try:
                with patch.object(root_type, "_reprove", new=counted_reprove):
                    with authority.proof_window():
                        after_entry = reproof_count
                        authority.reprove()
                        authority.reprove()
                        self.assertEqual(reproof_count, after_entry)
                    self.assertEqual(reproof_count, after_entry + 1)

                    authority.reprove()
                    self.assertEqual(reproof_count, after_entry + 2)
            finally:
                authority.close()

    def test_close_revokes_source_reproof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "module.py"
            source_path.write_bytes(b"VALUE = 1\n")
            authority = compose_rooted_source_authority(root)
            source = authority.bind_path(source_path)

            authority.close()

            self.assertTrue(authority.closed)
            self.assertFalse(source.is_current())
            with self.assertRaises(PlatformFileError):
                authority.reprove()

    @unittest.skipUnless(os.name == "nt", "Windows import boundary")
    def test_windows_import_does_not_load_posix_adapter(self) -> None:
        source_root = Path(__file__).absolute().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    f"import sys; sys.path.insert(0, {str(source_root)!r}); "
                    "import platform_source_authority; "
                    "assert 'fcntl' not in sys.modules; "
                    "assert 'platform_fs_posix' not in sys.modules"
                ),
            ],
            cwd=Path(tempfile.gettempdir()),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
