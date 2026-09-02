from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

import tm_snapshot_artifacts


@unittest.skipUnless(os.name == "nt", "Windows rooted artifact proof")
class WindowsSnapshotArtifactProofTests(unittest.TestCase):
    def test_strict_pair_digest_uses_exact_crlf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.jsonl"
            content = b'{"source":"a","target":"b"}\r\n'
            path.write_bytes(content)

            state, digest, identity = (
                tm_snapshot_artifacts._strict_pair_file_state(path)
            )

            self.assertEqual(state, "present")
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())
            self.assertIsNotNone(identity)


if __name__ == "__main__":
    unittest.main()
