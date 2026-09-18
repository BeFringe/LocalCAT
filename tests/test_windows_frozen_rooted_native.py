"""Real Win32 rooted I/O tests in a disposable, isolated bundle directory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


class WindowsFrozenRootedNativeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires real Win32 handles")
    def test_native_rooted_matrix(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("run from the locked MSVC x64 build environment")
        repository = Path(__file__).resolve().parents[1]
        source = repository / "packaging/windows/frozen-entry/native"
        artifacts = repository / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rooted-io-", dir=artifacts) as temporary:
            work = Path(temporary)
            bundle = work / "bundle"
            (bundle / "_internal/lib").mkdir(parents=True)
            (bundle / "_internal/code").mkdir()
            (work / "probe.c").write_text(
                '#include <windows.h>\nBOOL WINAPI DllMain(HINSTANCE m,DWORD r,LPVOID p)'
                '{(void)m;(void)r;(void)p;return TRUE;}\n', encoding="utf-8")
            self._run([compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/LD", str(work / "probe.c"),
                       "/Fe:" + str(work / "probe.dll"), "/link", "/IMPLIB:" + str(work / "probe.lib")], work)
            paths = ["_internal/lib/probe.dll", "_internal/code/bootstrap.py", "_internal/code/critical.py", "_internal/fixture.txt", "_internal/lib/other.dll"]
            ids = ["native-probe", "bootstrap", "critical-source", "fixture", "other-native"]
            dll_bytes = (work / "probe.dll").read_bytes()
            payloads = [dll_bytes, b"x=1\n", b"y=2\n", b"binary\0fixture\n", dll_bytes]
            roles = [1, 3, 4, 5, 1]
            records, strings = bytearray(), bytearray()
            for path, name, data, role in zip(paths, ids, payloads, roles):
                (bundle / path).write_bytes(data)
                path_bytes, name_bytes = path.replace("/", "\\").encode(), name.encode()
                path_offset = len(strings); strings.extend(path_bytes)
                name_offset = len(strings); strings.extend(name_bytes)
                records.extend(struct.pack("<8IQ32s", path_offset, len(path_bytes), name_offset, len(name_bytes),
                                           role, 0, 0, 0, len(data), hashlib.sha256(data).digest()))
            manifest = struct.pack("<8s10I32s", b"LCFMV001", 1, 80, len(paths), 72, 80, 80 + len(records), 0,
                                   80 + len(records), len(strings), 0, bytes(32)) + records + strings
            (bundle / "localcat-runtime.manifest").write_bytes(manifest)
            header = (source / "localcat_manifest.h").read_text(encoding="utf-8")
            substitutions = {"CANDIDATE_INPUT_DIGEST": "0" * 64, "PRELINK_INPUT_DIGEST": "0" * 64,
                             "RUNTIME_MANIFEST_DIGEST": ",".join(map(str, hashlib.sha256(manifest).digest())),
                             "RUNTIME_ROOT_DIGEST": ",".join(["0"] * 32)}
            for name, replacement in substitutions.items():
                header = header.replace("@@" + name + "@@", replacement)
            self.assertNotIn("@@", header)
            (work / "localcat_manifest.h").write_text(header, encoding="utf-8")
            for name in ("localcat_rooted_io.c", "localcat_rooted_io.h", "localcat_manifest.c", "localcat_sha256.c", "localcat_sha256.h"):
                shutil.copyfile(source / name, work / name)
            executable = bundle / "rooted-test.exe"
            self._run([compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro", "/guard:cf", "/I" + str(work),
                       str(repository / "tests/native/windows_frozen_rooted_test.c"), str(work / "localcat_manifest.c"),
                       str(work / "localcat_sha256.c"), "/Fe:" + str(executable), "/link", "/Brepro", "/GUARD:CF", "user32.lib"], work)
            self._run([str(executable), "preexisting"], work)
            self._run([str(executable), "preexisting-other"], work)
            self._run([str(executable), "preexisting-swap"], work)
            self._run([str(executable), "positive"], work)
            for path, data in zip(paths, payloads):
                (bundle / path).write_bytes(data)
            for extra_name in ("extra.dll", "unexpected.txt", "second.exe", "_internal/code/extra.py",
                               "_internal/lib/probe.pyc", "_internal/code/critical.PYC", "_internal/code/hidden.pyz"):
                with self.subTest(extra=extra_name):
                    extra = bundle / extra_name
                    extra.write_bytes(b"not declared")
                    try:
                        self._run([str(executable), "negative"], work)
                    finally:
                        extra.unlink()
            for extra_name in ("empty-extra", "_internal/code/nested", "_internal/code/__pycache__"):
                with self.subTest(extra_directory=extra_name):
                    extra = bundle / extra_name
                    extra.mkdir()
                    try:
                        self._run([str(executable), "negative"], work)
                    finally:
                        extra.rmdir()
            external = work / "external-directory"
            external.mkdir()
            junction = bundle / "_internal/code/unknown-junction"
            self._run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)], work)
            try:
                self._run([str(executable), "negative"], work)
            finally:
                junction.rmdir()
            symlink = bundle / "_internal/code/unknown-symlink.py"
            try:
                os.symlink(bundle / paths[2], symlink)
            except OSError as error:
                if error.winerror != 1314:
                    raise
                print("rooted fixture: BLOCKED_NOT_RUN file-symlink (host lacks SeCreateSymbolicLinkPrivilege)")
            else:
                try:
                    self._run([str(executable), "negative"], work)
                finally:
                    symlink.unlink()
            (bundle / paths[3]).write_bytes(b"tamper\0fixture\n")
            self._run([str(executable), "negative"], work)
            (bundle / paths[3]).write_bytes(payloads[3])
            (bundle / paths[2]).unlink()
            self._run([str(executable), "negative"], work)
            (bundle / paths[2]).write_bytes(payloads[2])
            code = bundle / "_internal/code"
            target = bundle / "_internal/real-code"
            code.rename(target)
            self._run(["cmd.exe", "/d", "/c", "mklink", "/J", str(code), str(target)], work)
            self._run([str(executable), "negative"], work)

    def _run(self, command, cwd):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "rooted I/O:" in result.stdout:
            print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
