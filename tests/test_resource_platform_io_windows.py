from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from resource_platform_io import (
    BoundRegularFileStream,
    digest_bound,
    open_rooted_regular,
    read_bound_all,
)


ROOT = Path(__file__).resolve().parents[1]


class ResourcePlatformIOTests(unittest.TestCase):
    def test_retained_rooted_read_stream_and_digest_share_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="localcat-resource-io-",
            dir=ROOT.parent,
        ) as raw:
            root = Path(raw).resolve()
            source = root / "source.localcat-resource"
            payload = (b"resource-platform-io\x00" * 7000) + b"tail"
            source.write_bytes(payload)
            backend = compose_platform_file_backend(root)

            with open_rooted_regular(backend, source) as opened:
                snapshot = opened.snapshot()
                self.assertEqual(
                    read_bound_all(opened, snapshot, maximum_bytes=len(payload)),
                    payload,
                )
                self.assertEqual(
                    digest_bound(opened, snapshot).hex(),
                    hashlib.sha256(payload).hexdigest(),
                )
                stream = BoundRegularFileStream(
                    opened,
                    snapshot,
                    start=17,
                    length=100_000,
                )
                self.assertEqual(stream.read(257), payload[17:274])
                self.assertEqual(stream.seek(-97, 2), 99_903)
                self.assertEqual(stream.read(), payload[17 + 99_903 : 17 + 100_000])

                replacement = root / "replacement.localcat-resource"
                replacement.write_bytes(b"replacement")
                if os.name == "nt":
                    with self.assertRaises(PermissionError):
                        os.replace(replacement, source)

    def test_multilink_source_is_rejected_before_body_use(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="localcat-resource-hardlink-",
            dir=ROOT.parent,
        ) as raw:
            root = Path(raw).resolve()
            source = root / "source.localcat-resource"
            alias = root / "alias.localcat-resource"
            source.write_bytes(b"must-not-authorize")
            os.link(source, alias)
            backend = compose_platform_file_backend(root)
            with self.assertRaises(PlatformFileError) as caught:
                with open_rooted_regular(backend, source):
                    self.fail("multi-link source unexpectedly received authority")
            self.assertEqual(
                caught.exception.code,
                PlatformFileErrorCode.REPARSE_REJECTED.value,
            )

    @unittest.skipUnless(os.name == "nt", "junction evidence is Windows-only")
    def test_junction_source_root_is_rejected_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="localcat-resource-junction-",
            dir=ROOT.parent,
        ) as raw:
            root = Path(raw).resolve()
            outside = root / "outside"
            outside.mkdir()
            source = outside / "package.localcat-resource"
            source.write_bytes(b"must-not-read")
            junction = root / "junction"
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            try:
                backend = compose_platform_file_backend(root)
                with self.assertRaises(PlatformFileError):
                    with open_rooted_regular(
                        backend,
                        junction / "package.localcat-resource",
                    ):
                        self.fail("junction source unexpectedly received authority")
            finally:
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
