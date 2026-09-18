"""Compile and run the native manifest parser against hostile fixtures on Windows."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.windows_frozen_manifest import SourceEntry, prepare_manifest


class WindowsFrozenManifestNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "the parser uses Windows ordinal filename semantics")
    def test_generated_manifest_native_roundtrip(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("run from the locked MSVC x64 build environment")
        repository = Path(__file__).resolve().parents[1]
        source = repository / "packaging/windows/frozen-entry/native"
        artifacts = repository / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        prepared = prepare_manifest([
            SourceEntry("fixture", "_internal/fixture.txt", "fixture", b"a\0b"),
            SourceEntry("bootstrap", "_internal/bootstrap.py", "bootstrap", b"x=1\n", ("fixture",)),
        ], candidate_input_digest="1" * 64, applied_sources_digest="2" * 64)
        with tempfile.TemporaryDirectory(prefix="manifest-roundtrip-", dir=artifacts) as temporary:
            work = Path(temporary)
            (work / "localcat_manifest.h").write_text(prepared.render_header(
                (source / "localcat_manifest.h").read_text(encoding="utf-8")), encoding="utf-8")
            shutil.copyfile(source / "localcat_manifest.c", work / "localcat_manifest.c")
            (work / "runtime.manifest").write_bytes(prepared.runtime_bytes)
            executable = work / "roundtrip-test.exe"
            command = [compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro",
                       "/guard:cf", "/I" + str(work), str(work / "localcat_manifest.c"),
                       str(repository / "tests/native/windows_frozen_manifest_roundtrip.c"),
                       "/Fe:" + str(executable), "/link", "/Brepro", "/GUARD:CF"]
            built = subprocess.run(command, cwd=work, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(executable)], cwd=work, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "the parser uses Windows ordinal filename semantics")
    def test_native_parser_matrix(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("run from the locked MSVC x64 build environment")
        repository = Path(__file__).resolve().parents[1]
        source = repository / "packaging/windows/frozen-entry/native"
        artifacts = repository / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="manifest-parser-", dir=artifacts) as temporary:
            work = Path(temporary)
            header = (source / "localcat_manifest.h").read_text(encoding="utf-8")
            for token in ("CANDIDATE_INPUT_DIGEST", "PRELINK_INPUT_DIGEST"):
                header = header.replace("@@" + token + "@@", "0" * 64)
            for token in ("RUNTIME_MANIFEST_DIGEST", "RUNTIME_ROOT_DIGEST"):
                header = header.replace("@@" + token + "@@", ",".join(["0"] * 32))
            self.assertNotIn("@@", header)
            (work / "localcat_manifest.h").write_text(header, encoding="utf-8")
            shutil.copyfile(source / "localcat_manifest.c", work / "localcat_manifest.c")
            executable = work / "manifest-test.exe"
            command = [compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro",
                       "/guard:cf", "/I" + str(work), str(work / "localcat_manifest.c"),
                       str(repository / "tests/native/windows_frozen_manifest_test.c"),
                       "/Fe:" + str(executable), "/link", "/Brepro", "/GUARD:CF"]
            built = subprocess.run(command, cwd=work, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            result = subprocess.run([str(executable)], cwd=work, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("0 failures", result.stdout)
            print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
