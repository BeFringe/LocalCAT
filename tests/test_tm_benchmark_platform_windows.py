from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest

from platform_fs_contracts import PlatformFileError
from platform_fs import compose_platform_file_backend
from tm_benchmark_platform_io import (
    create_new_rooted_file,
    iter_rooted_file_lines,
    rooted_file_facts,
    rooted_first_nonempty_line,
    windows_benchmark_artifact_family,
    windows_peak_working_set_bytes,
)


@unittest.skipUnless(sys.platform == "win32", "Windows platform benchmark I/O")
class WindowsBenchmarkPlatformIOTests(unittest.TestCase):
    _FIXTURE_NAME = "fixture.jsonl"
    _SIDECAR_NAME = "store.sqlite3"
    _MANIFEST_NAME = "store.manifest.json"
    _PRIVATE_NAME = ".localcat-activation-private-v1.test"
    _QUARANTINE_NAME = ".localcat-activation-quarantine-v1"

    def _populate_direct_namespace(self, root: Path) -> None:
        for name in (
            self._FIXTURE_NAME,
            self._SIDECAR_NAME,
            self._MANIFEST_NAME,
            f".{self._SIDECAR_NAME}.localcat-activated-lineage.json",
            f".{self._SIDECAR_NAME}.localcat-initial-activation.lock",
        ):
            (root / name).write_bytes(b"owner")

    def _create_private_activation_directory(self, root: Path) -> Path:
        backend = compose_platform_file_backend(root)
        root_authority = parent = private = None
        try:
            root_authority = backend.bind_root(root)
            parent = backend.bind_parent(
                root_authority,
                PureWindowsPath(self._PRIVATE_NAME),
            )
            private = backend.create_private_directory(parent, self._PRIVATE_NAME)
        finally:
            if private is not None:
                private.close()
            if parent is not None:
                parent.close()
            if root_authority is not None:
                root_authority.close()
        path = root / self._PRIVATE_NAME
        for name in (
            "activation-journal-v3.json",
            "activation-publication-db-replaced-v1.json",
            "activation-publication-generation-published-v1.json",
            "activation-publication-manifest-published-v1.json",
            "device.key",
        ):
            (path / name).write_bytes(b"owner")
        return path

    def test_create_new_and_fresh_rooted_read_preserve_exact_bytes(self) -> None:
        payload = b'\n{"source":"alpha","target":"beta"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.jsonl"
            created = create_new_rooted_file(path, (payload[:7], payload[7:]))
            facts = rooted_file_facts(path, read_content=True)

            self.assertEqual(created.byte_count, len(payload))
            self.assertEqual(
                created.content_sha256.hex(),
                hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(facts.byte_count, len(payload))
            self.assertEqual(facts.sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(facts.content, payload)
            self.assertEqual(
                rooted_first_nonempty_line(path),
                b'{"source":"alpha","target":"beta"}',
            )
            self.assertEqual(
                tuple(iter_rooted_file_lines(path)),
                (b"\n", b'{"source":"alpha","target":"beta"}\n'),
            )

    def test_create_new_never_overwrites_an_existing_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.jsonl"
            create_new_rooted_file(path, (b"original\n",))
            with self.assertRaises(FileExistsError):
                create_new_rooted_file(path, (b"replacement\n",))
            self.assertEqual(
                rooted_file_facts(path, read_content=True).content,
                b"original\n",
            )

    def test_final_reparse_point_cannot_escape_the_trusted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            trusted = base / "trusted"
            trusted.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "payload.jsonl").write_bytes(b"foreign\n")
            planted = trusted / "fixture.jsonl"
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(planted),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            with self.assertRaises(PlatformFileError):
                rooted_file_facts(planted, read_content=True)

    def test_run_root_reparse_is_rejected_before_namespace_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            marker = outside / "must-not-read.txt"
            marker.write_bytes(b"foreign-body")
            planted = base / "run-root"
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(planted),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            with self.assertRaises(PlatformFileError):
                windows_benchmark_artifact_family(
                    run_root=planted,
                    fixture_name=self._FIXTURE_NAME,
                    sidecar_name=self._SIDECAR_NAME,
                    manifest_name=self._MANIFEST_NAME,
                )
            self.assertEqual(marker.read_bytes(), b"foreign-body")

    def test_private_directory_reparse_is_rejected_before_body_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run-root"
            root.mkdir()
            self._populate_direct_namespace(root)
            (root / self._QUARANTINE_NAME).mkdir()
            outside = Path(temporary) / "outside-private"
            outside.mkdir()
            marker = outside / "must-not-read.txt"
            marker.write_bytes(b"foreign-private")
            planted = root / self._PRIVATE_NAME
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(planted), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            with self.assertRaises(PlatformFileError):
                windows_benchmark_artifact_family(
                    run_root=root,
                    fixture_name=self._FIXTURE_NAME,
                    sidecar_name=self._SIDECAR_NAME,
                    manifest_name=self._MANIFEST_NAME,
                )
            self.assertEqual(marker.read_bytes(), b"foreign-private")

    def test_quarantine_reparse_is_rejected_before_body_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run-root"
            root.mkdir()
            self._populate_direct_namespace(root)
            self._create_private_activation_directory(root)
            outside = Path(temporary) / "outside-quarantine"
            outside.mkdir()
            marker = outside / "must-not-read.txt"
            marker.write_bytes(b"foreign-quarantine")
            planted = root / self._QUARANTINE_NAME
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(planted), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            with self.assertRaises(PlatformFileError):
                windows_benchmark_artifact_family(
                    run_root=root,
                    fixture_name=self._FIXTURE_NAME,
                    sidecar_name=self._SIDECAR_NAME,
                    manifest_name=self._MANIFEST_NAME,
                )
            self.assertEqual(marker.read_bytes(), b"foreign-quarantine")

    def test_attempt_reparse_is_rejected_before_body_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run-root"
            root.mkdir()
            self._populate_direct_namespace(root)
            self._create_private_activation_directory(root)
            quarantine = root / self._QUARANTINE_NAME
            quarantine.mkdir()
            outside = Path(temporary) / "outside-attempt"
            outside.mkdir()
            marker = outside / "must-not-read.txt"
            marker.write_bytes(b"foreign-attempt")
            planted = quarantine / "initial-test"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(planted), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            with self.assertRaises(PlatformFileError):
                windows_benchmark_artifact_family(
                    run_root=root,
                    fixture_name=self._FIXTURE_NAME,
                    sidecar_name=self._SIDECAR_NAME,
                    manifest_name=self._MANIFEST_NAME,
                )
            self.assertEqual(marker.read_bytes(), b"foreign-attempt")

    def test_peak_working_set_uses_positive_byte_facts(self) -> None:
        first = windows_peak_working_set_bytes()
        allocation = bytearray(1024 * 1024)
        allocation[0] = 1
        second = windows_peak_working_set_bytes()
        self.assertGreater(first, 0)
        self.assertGreaterEqual(second, first)


if __name__ == "__main__":
    unittest.main()
