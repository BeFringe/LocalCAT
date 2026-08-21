from __future__ import annotations

import os
import json
import plistlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import qt_editor
from macos_app_launcher import (
    LOCALCAT_BUNDLE_IDENTIFIER,
    MacOSAppLauncher,
)


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "macOS bundle contract")
class MacOSAppLauncherTests(unittest.TestCase):
    def _launcher(self) -> MacOSAppLauncher:
        return MacOSAppLauncher(
            icon_path=(ROOT / "LocalCAT-logo-silver.icns").resolve(),
        )

    def test_builds_and_cold_launches_from_an_unrelated_working_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT bundle ") as temporary:
            root = Path(temporary)
            target = (root / "Applications" / "LocalCAT.app").resolve()
            target.parent.mkdir()
            launcher = self._launcher()

            bundle = launcher.build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            report = launcher.validate_bundle(bundle)

            self.assertEqual(bundle, target)
            self.assertEqual(report.bundle, target)
            self.assertEqual(report.display_name, "LocalCAT")
            self.assertEqual(
                report.bundle_identifier,
                LOCALCAT_BUNDLE_IDENTIFIER,
            )
            self.assertEqual(report.executable_name, "LocalCAT")
            self.assertTrue(report.icon_present)
            self.assertTrue(report.cold_launch_passed)
            executable = target / "Contents" / "MacOS" / "LocalCAT"
            bundled_icon = target / "Contents" / "Resources" / "LocalCAT.icns"
            self.assertEqual(
                bundled_icon.read_bytes(),
                (ROOT / "LocalCAT-logo-silver.icns").read_bytes(),
            )
            self.assertTrue(executable.stat().st_mode & 0o111)
            self.assertEqual(executable.read_bytes()[:4], b"\xca\xfe\xba\xbe")
            signature = target / "Contents" / "_CodeSignature" / "CodeResources"
            self.assertTrue(signature.is_file())
            verified = subprocess.run(
                [
                    "/usr/bin/codesign",
                    "--verify",
                    "--deep",
                    "--strict",
                    "--verbose=4",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            identity = subprocess.run(
                ["/usr/bin/codesign", "-d", "--verbose=4", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(identity.returncode, 0, identity.stderr)
            self.assertIn(
                f"Identifier={LOCALCAT_BUNDLE_IDENTIFIER}",
                identity.stderr,
            )
            self.assertIn("Info.plist entries=", identity.stderr)
            self.assertIn("Sealed Resources version=", identity.stderr)
            architectures = subprocess.run(
                ["/usr/bin/lipo", "-archs", str(executable)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(architectures.returncode, 0, architectures.stderr)
            self.assertEqual(
                set(architectures.stdout.split()),
                {"arm64", "x86_64"},
            )
            build_versions = subprocess.run(
                ["/usr/bin/otool", "-arch", "all", "-l", str(executable)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build_versions.returncode, 0, build_versions.stderr)
            self.assertEqual(build_versions.stdout.count("minos 13.0"), 2)
            launcher_source = (
                ROOT / "macos" / "LocalCATLauncher.c"
            ).read_text(encoding="utf-8")
            self.assertIn("execv(python, arguments)", launcher_source)
            self.assertNotIn("system(", launcher_source)
            with (target / "Contents" / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            self.assertEqual(info["CFBundleName"], "LocalCAT")
            self.assertEqual(info["CFBundleDisplayName"], "LocalCAT")
            self.assertEqual(info["CFBundleExecutable"], "LocalCAT")
            self.assertEqual(
                info["CFBundleIdentifier"],
                LOCALCAT_BUNDLE_IDENTIFIER,
            )
            self.assertEqual(
                info["LocalCATPythonExecutable"],
                str(Path(sys.executable).resolve()),
            )
            self.assertEqual(
                info["LocalCATBootstrapPath"],
                str((ROOT / "qt_editor.py").resolve()),
            )

    def test_invalid_rebuild_preserves_the_previous_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            launcher = self._launcher()
            _ = launcher.build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            marker = target / "Contents" / "Resources" / "old.marker"
            marker.write_text("keep-old", encoding="utf-8")

            with (
                patch(
                    "macos_app_launcher._swap_directories",
                    side_effect=OSError("injected atomic publication failure"),
                ),
                self.assertRaises(OSError),
            ):
                launcher.build_bundle(
                    target,
                    Path(sys.executable).resolve(),
                    (ROOT / "qt_editor.py").resolve(),
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep-old")

    def test_invalid_candidate_preserves_an_absent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            launcher = self._launcher()

            with (
                patch.object(
                    launcher,
                    "validate_bundle",
                    side_effect=ValueError("injected candidate failure"),
                ),
                self.assertRaises(ValueError),
            ):
                launcher.build_bundle(
                    target,
                    Path(sys.executable).resolve(),
                    (ROOT / "qt_editor.py").resolve(),
                )

            self.assertFalse(target.exists())
            self.assertEqual(tuple(root.glob(".LocalCAT-candidate-*.app")), ())

    def test_successful_rebuild_atomically_replaces_the_previous_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            launcher = self._launcher()
            _ = launcher.build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            old_marker = target / "Contents" / "Resources" / "old.marker"
            old_marker.write_text("old bundle", encoding="utf-8")

            self.assertEqual(
                launcher.build_bundle(
                    target,
                    Path(sys.executable).resolve(),
                    (ROOT / "qt_editor.py").resolve(),
                ),
                target,
            )
            self.assertFalse(old_marker.exists())
            self.assertTrue(launcher.validate_bundle(target).cold_launch_passed)

    def test_launchservices_reports_localcat_identity_after_python_exec(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT identity ") as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            _ = self._launcher().build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            marker = root / "live.marker"
            data_dir = root / "live data"
            opened = subprocess.run(
                [
                    "/usr/bin/open",
                    "-n",
                    str(target),
                    "--args",
                    "--data-dir",
                    str(data_dir),
                    "--bundle-smoke-marker",
                    str(marker),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(opened.returncode, 0, opened.stderr)
            deadline = time.monotonic() + 10.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            process_id = payload["pid"]
            try:
                script = (
                    "ObjC.import('AppKit');"
                    "const app=$.NSRunningApplication."
                    f"runningApplicationWithProcessIdentifier({process_id});"
                    "console.log(ObjC.unwrap(app.localizedName));"
                    "console.log(ObjC.unwrap(app.bundleIdentifier));"
                    "console.log(ObjC.unwrap(app.bundleURL.path));"
                )
                identity = subprocess.run(
                    ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(identity.returncode, 0, identity.stderr)
                facts = tuple(
                    line.strip()
                    for line in identity.stderr.splitlines()
                    if line.strip()
                )
                self.assertEqual(
                    facts[:2],
                    ("LocalCAT", LOCALCAT_BUNDLE_IDENTIFIER),
                )
                self.assertEqual(Path(facts[2]).resolve(), target)
            finally:
                os.kill(process_id, signal.SIGTERM)

    def test_native_launcher_accepts_one_process_scoped_checkout_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT direct handoff ") as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            _ = self._launcher().build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            marker = root / "handoff.json"
            override = root / "override bootstrap.py"
            override.write_text(
                "import json, os, pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({\n"
                "    'argv': sys.argv[2:],\n"
                "    'native': os.environ.get('LOCALCAT_NATIVE_LAUNCH'),\n"
                "    'python_override': os.environ.get('LOCALCAT_DIRECT_PYTHON'),\n"
                "    'bootstrap_override': os.environ.get('LOCALCAT_DIRECT_BOOTSTRAP'),\n"
                "}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    str(target / "Contents" / "MacOS" / "LocalCAT"),
                    "--localcat-direct-python",
                    str(Path(sys.executable).resolve()),
                    "--localcat-direct-bootstrap",
                    str(override.resolve()),
                    str(marker),
                    "--from-current-worktree",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")),
                {
                    "argv": ["--from-current-worktree"],
                    "bootstrap_override": None,
                    "native": "1",
                    "python_override": None,
                },
            )

    def test_direct_python_launch_hands_off_to_compatible_native_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT handoff bundle ") as temporary:
            root = Path(temporary)
            bundle = root / "LocalCAT.app"
            executable = bundle / "Contents" / "MacOS" / "LocalCAT"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"native launcher")
            executable.chmod(0o755)
            with (bundle / "Contents" / "Info.plist").open("wb") as stream:
                plistlib.dump(
                    {
                        "CFBundleDisplayName": "LocalCAT",
                        "CFBundleExecutable": "LocalCAT",
                        "CFBundleIdentifier": LOCALCAT_BUNDLE_IDENTIFIER,
                        "LocalCATDirectHandoffVersion": 1,
                    },
                    stream,
                )

            with (
                patch.object(
                    qt_editor,
                    "_macos_bundle_candidates",
                    return_value=(bundle,),
                ),
                patch("qt_editor.os.execve") as execve,
            ):
                qt_editor._handoff_to_macos_native_launcher(
                    ("--project", "chapter.json")
                )

            execve.assert_called_once()
            called_executable, called_argv, called_environment = execve.call_args.args
            self.assertEqual(called_executable, "/usr/bin/open")
            self.assertEqual(
                called_argv,
                [
                    "/usr/bin/open",
                    "-W",
                    str(bundle),
                    "--args",
                    "--localcat-direct-python",
                    str(Path(sys.executable).resolve()),
                    "--localcat-direct-bootstrap",
                    str(Path(qt_editor.__file__).resolve()),
                    "--project",
                    "chapter.json",
                ],
            )
            self.assertNotIn("LOCALCAT_DIRECT_BOOTSTRAP", called_environment)
            self.assertNotIn("LOCALCAT_DIRECT_PYTHON", called_environment)

    def test_rejects_non_app_targets_and_tampered_bundle_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self._launcher()
            with self.assertRaises(ValueError):
                launcher.build_bundle(
                    (root / "LocalCAT").resolve(),
                    Path(sys.executable).resolve(),
                    (ROOT / "qt_editor.py").resolve(),
                )
            target = (root / "LocalCAT.app").resolve()
            _ = launcher.build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            executable = target / "Contents" / "MacOS" / "LocalCAT"
            executable_bytes = executable.read_bytes()
            tamper_offset = min(4096, len(executable_bytes) // 3)
            tampered = bytearray(executable_bytes)
            tampered[tamper_offset] ^= 0xFF
            executable.write_bytes(tampered)
            executable.chmod(0o755)
            with self.assertRaises(ValueError):
                launcher.validate_bundle(target)
            _ = launcher.build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            plist_path = target / "Contents" / "Info.plist"
            with plist_path.open("rb") as stream:
                info = plistlib.load(stream)
            info["CFBundleIdentifier"] = "invalid.foreign.bundle"
            with plist_path.open("wb") as stream:
                plistlib.dump(info, stream)
            with self.assertRaises(ValueError):
                launcher.validate_bundle(target)

    def test_install_refuses_to_replace_a_running_app_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            applications = (Path(temporary) / "Applications").resolve()
            applications.mkdir()
            old_bundle = applications / "LocalCAT.app"
            old_bundle.mkdir()
            marker = old_bundle / "old.marker"
            marker.write_text("keep running bundle", encoding="utf-8")

            with (
                patch.object(qt_editor, "_localcat_app_is_running", return_value=True),
                patch("macos_app_launcher.MacOSAppLauncher.build_bundle") as build,
                self.assertRaisesRegex(RuntimeError, "quit LocalCAT"),
            ):
                qt_editor.install_macos_app(applications)

            build.assert_not_called()
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "keep running bundle",
            )

    def test_install_cli_is_stdlib_first_and_uses_the_user_applications_target(
        self,
    ) -> None:
        parsed = qt_editor.build_parser().parse_args(["--install-macos-app"])
        self.assertTrue(parsed.install_macos_app)
        with tempfile.TemporaryDirectory() as temporary:
            applications = (Path(temporary) / "Applications").resolve()
            with patch.object(
                qt_editor,
                "_localcat_app_is_running",
                return_value=False,
            ):
                installed = qt_editor.install_macos_app(applications)
            self.assertEqual(installed, applications / "LocalCAT.app")
            self.assertTrue(installed.is_dir())
            with patch.object(
                qt_editor,
                "install_macos_app",
                return_value=installed,
            ) as install, patch("builtins.print"):
                self.assertEqual(qt_editor.main(["--install-macos-app"]), 0)
            install.assert_called_once_with()

    def test_missing_configured_runtime_exits_nonzero_without_python_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = (root / "LocalCAT.app").resolve()
            _ = self._launcher().build_bundle(
                target,
                Path(sys.executable).resolve(),
                (ROOT / "qt_editor.py").resolve(),
            )
            plist_path = target / "Contents" / "Info.plist"
            with plist_path.open("rb") as stream:
                info = plistlib.load(stream)
            info["LocalCATPythonExecutable"] = str(root / "missing-python")
            with plist_path.open("wb") as stream:
                plistlib.dump(info, stream)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            completed = subprocess.run(
                [str(target / "Contents" / "MacOS" / "LocalCAT")],
                cwd=unrelated,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 72)
            self.assertIn("configured runtime is unavailable", completed.stderr)
            self.assertNotIn(str(root / "missing-python"), completed.stderr)


if __name__ == "__main__":
    unittest.main()
