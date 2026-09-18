"""Check the compiled-in hash against an independent hashlib oracle."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class WindowsFrozenSHA256NativeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires the MSVC candidate compiler")
    def test_hash_padding_streaming_and_comparison(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("run from the locked MSVC x64 build environment")
        repository = Path(__file__).resolve().parents[1]
        source = repository / "packaging/windows/frozen-entry/native"
        artifacts = repository / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sha256-", dir=artifacts) as temporary:
            work = Path(temporary)
            executable = work / "sha256-test.exe"
            command = [compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro",
                       "/guard:cf", "/I" + str(source), str(source / "localcat_sha256.c"),
                       str(repository / "tests/native/windows_frozen_sha256_test.c"),
                       "/Fe:" + str(executable), "/link", "/Brepro", "/GUARD:CF"]
            built = subprocess.run(command, cwd=work, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            for length in (0, 1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 4097, 1000000):
                data = bytes((index * 73 + 19) & 255 for index in range(length))
                expected = hashlib.sha256(data).hexdigest()
                for chunk in (1, 7, 64, 4096):
                    with self.subTest(length=length, chunk=chunk):
                        result = subprocess.run([str(executable), str(length), str(chunk)],
                                                cwd=work, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
