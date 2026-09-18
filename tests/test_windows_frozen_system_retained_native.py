"""Diagnostic file-proof experiment, not CNG authority or a release gate."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.windows_frozen_system_profile import collect_profile


class WindowsFrozenSystemRetainedTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Win32 retained handles")
    def test_diagnostic_system_file_proof(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("requires locked MSVC x64 environment")
        root = Path(__file__).resolve().parents[1]
        native = root / "packaging/windows/frozen-entry/native"
        # This experiment chooses files locally; the production candidate has no
        # provider profile and must not depend on this diagnostic snapshot.
        profile = collect_profile({
            "schema": "localcat.windows-system-provider-target.v1",
            "phase": "pre-E10",
            "trigger": {"dll": "bcrypt.dll", "symbol": "BCryptGenRandom",
                        "hAlgorithm": 0, "flags": 2},
            "members": [{"role": "api_host", "basename": "bcrypt.dll"},
                        {"role": "provider", "basename": "bcryptprimitives.dll"}],
        })
        self.assertEqual(profile["scope"], "explicit-system-roots")
        rows = []
        for member in profile["members"]:
            self.assertRegex(member["basename"], r"^[a-z0-9_-]+\.dll$")
            digest = ",".join(str(n) for n in bytes.fromhex(member["sha256"]))
            rows.append('{L"%s",%dULL,{%s}}' % (member["basename"], member["size"], digest))
        self.assertEqual(len(rows), 2)
        artifacts = root / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="system-retained-", dir=artifacts) as temporary:
            work = Path(temporary)
            (work / "system_candidates.h").write_text(
                "static const struct localcat_system_file_candidate system_candidates[] = {" + ",".join(rows) + "};\n",
                encoding="utf-8")
            executable = work / "system-retained.exe"
            self.run_checked([compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro", "/guard:cf",
                "/I" + str(native), "/I" + str(work), str(root / "tests/native/windows_frozen_system_retained_test.c"),
                str(native / "localcat_sha256.c"),
                "/Fe:" + str(executable), "/link", "/Brepro", "/GUARD:CF"], work)
            self.run_checked([str(executable)], work)

    def run_checked(self, command, cwd):
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if result.stdout:
            print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
