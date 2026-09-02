from __future__ import annotations

import ast
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


import qt_editor
import windows_source_guardian as guardian
import windows_source_launcher as launcher


@unittest.skipUnless(os.name == "nt", "Windows source launcher requires Windows")
class WindowsSourceLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = dict(os.environ)
        self._cwd = Path.cwd()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._environment)
        os.chdir(self._cwd)

    def test_install_cli_is_stdlib_first_and_uses_current_venv(self) -> None:
        parsed = qt_editor.build_parser().parse_args(["--install-windows-launcher"])
        self.assertTrue(parsed.install_windows_launcher)
        report = launcher.WindowsSourceShortcutReport(
            shortcut=Path(r"C:\Users\tester\LocalCAT.lnk"),
            target_path=Path(r"C:\venv\Scripts\pythonw.exe"),
            arguments='-I "C:\\LocalCAT\\Launcher\\windows_source_guardian.py"',
            working_directory=Path(r"C:\Users\tester\AppData\Local\LocalCAT"),
            icon_location=r"C:\source\LocalCAT-logo-silver.ico,0",
            description=launcher.SHORTCUT_DESCRIPTION,
        )
        with patch(
            "windows_source_launcher.install_windows_source_launcher",
            return_value=report,
        ) as install, patch("builtins.print"):
            self.assertEqual(qt_editor.main(["--install-windows-launcher"]), 0)
        install.assert_called_once_with()

    def test_installer_and_guardian_top_level_use_only_stdlib(self) -> None:
        for module in (launcher, guardian):
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported_roots: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    imported_roots.update(
                        alias.name.partition(".")[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported_roots.add(node.module.partition(".")[0])
            self.assertLessEqual(
                imported_roots - {"__future__"},
                sys.stdlib_module_names,
            )

    def test_default_shortcut_label_marks_the_source_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT shortcut label ") as temporary:
            os.environ["APPDATA"] = temporary
            self.assertEqual(
                launcher._default_shortcut_path().name,
                "LocalCAT Source.lnk",
            )

    def test_installer_rejects_an_unavailable_runtime_before_creating_link(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT runtime diagnostic ") as temporary:
            root = Path(temporary).resolve()
            target = root / "LocalCAT.lnk"
            with patch.object(
                launcher,
                "_current_venv_pythonw",
                side_effect=RuntimeError("runtime unavailable"),
            ), self.assertRaisesRegex(RuntimeError, "runtime unavailable"):
                launcher.install_windows_source_launcher(target)
            self.assertFalse(target.exists())

    def test_installed_shortcut_binds_absolute_runtime_source_icon_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT launcher 验收 ") as temporary:
            root = Path(temporary).resolve()
            os.environ["LOCALAPPDATA"] = str(root / "用户 Local AppData")
            target = root / "开始 菜单" / "LocalCAT 源码.lnk"
            report = launcher.install_windows_source_launcher(target)
            self.assertEqual(report.shortcut, target)
            self.assertTrue(report.shortcut.is_file())
            self.assertTrue(report.target_path.is_absolute())
            self.assertEqual(report.target_path.name.casefold(), "pythonw.exe")
            installed_guardian = (
                Path(os.environ["LOCALAPPDATA"])
                / "LocalCAT"
                / "Launcher"
                / launcher.GUARDIAN_SOURCE_FILENAME
            ).resolve()
            self.assertEqual(
                report.arguments,
                subprocess.list2cmdline(
                    [
                        "-I",
                        str(installed_guardian),
                        "--source-root",
                        str(Path(launcher.__file__).resolve().parent),
                        "--",
                    ]
                ),
            )
            self.assertEqual(
                report.working_directory,
                (Path(os.environ["LOCALAPPDATA"]) / "LocalCAT").resolve(),
            )
            self.assertEqual(report.description, launcher.SHORTCUT_DESCRIPTION)
            self.assertIn("LocalCAT-logo-silver.ico", report.icon_location)
            installed_icon = Path(report.icon_location.rpartition(",")[0]).resolve()
            self.assertTrue(launcher._icon_is_extractable(installed_icon))
            self.assertEqual(
                sorted(
                    path.name
                    for path in installed_guardian.parent.iterdir()
                ),
                sorted(
                    [
                        launcher.GUARDIAN_SOURCE_FILENAME,
                        launcher.WINDOWS_ICON_FILENAME,
                    ]
                ),
            )
            self.assertEqual(
                [path.name for path in target.parent.iterdir()],
                [target.name],
            )

    def test_shortcut_reaches_visible_qwindows_window_from_nonrepo_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT visible 验收 ") as temporary:
            root = Path(temporary).resolve()
            local_app_data = root / "用户 Local AppData"
            os.environ["LOCALAPPDATA"] = str(local_app_data)
            os.environ["APPDATA"] = str(root / "用户 Roaming")
            for name in guardian.DEVELOPER_OVERRIDE_ENVIRONMENT:
                os.environ[name] = str(root / f"hostile-{name}")
            shortcut = root / "开始 菜单" / "LocalCAT 可见.lnk"
            report = launcher.install_windows_source_launcher(shortcut)
            marker = root / "source-launch.marker.json"
            data_dir = root / "业务 数据"
            arguments = subprocess.list2cmdline(
                [
                    "--smoke-test",
                    "--data-dir",
                    str(data_dir),
                    "--source-launch-smoke-marker",
                    str(marker),
                ]
            )
            returncode, guardian_pid = self._shell_execute_and_wait(
                report.shortcut,
                arguments,
            )
            self.assertEqual(returncode, 0)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["application_name"], "LocalCAT")
            self.assertEqual(payload["application_version"], launcher.APPLICATION_VERSION)
            self.assertEqual(payload["platform_name"], "windows")
            self.assertEqual(payload["developer_overrides_present"], [])
            self.assertEqual(
                Path(payload["cwd"]).resolve(),
                report.working_directory,
            )
            self.assertEqual(
                Path(payload["python_executable"]).resolve(),
                report.target_path,
            )
            self.assertTrue(payload["window_visible"])
            self.assertTrue(payload["window_icon_present"])
            self.assertIs(type(payload["window_handle"]), int)
            self.assertGreater(payload["window_handle"], 0)
            self.assertNotEqual(payload["pid"], guardian_pid)
            self.assertIn("LocalCAT", payload["window_title"])

    def test_real_shortcut_maps_missing_checkout_to_body_free_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT missing source ") as temporary:
            root = Path(temporary).resolve()
            os.environ["LOCALAPPDATA"] = str(root / "local")
            source_root = root / "movable checkout"
            source_root.mkdir()
            bootstrap = source_root / "qt_editor.py"
            bootstrap.write_text("def main(argv):\n    return 0\n", encoding="utf-8")
            shortcut = root / "menu" / "LocalCAT Source.lnk"
            report = launcher._install_windows_source_launcher(
                shortcut,
                source_root=source_root,
                working_directory=None,
                guardian_arguments=("--launcher-no-dialog",),
            )
            bootstrap.unlink()
            source_root.rmdir()
            result, _ = self._shell_execute_and_wait(
                report.shortcut,
                "",
            )
            self.assertEqual(result, guardian.EXIT_BY_CODE[guardian.SOURCE_UNAVAILABLE])
            log = (root / "local" / "LocalCAT" / "source-launcher.log").read_text(
                encoding="utf-8"
            )
            self.assertIn(guardian.SOURCE_UNAVAILABLE, log)
            self.assertNotIn(str(source_root), log)

    def test_real_shortcut_maps_child_nonzero_to_guardian_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT failed child ") as temporary:
            root = Path(temporary).resolve()
            os.environ["LOCALAPPDATA"] = str(root / "local")
            source_root = root / "checkout"
            source_root.mkdir()
            (source_root / "qt_editor.py").write_text(
                "def main(argv):\n    return 7\n",
                encoding="utf-8",
            )
            shortcut = root / "menu" / "LocalCAT Source.lnk"
            report = launcher._install_windows_source_launcher(
                shortcut,
                source_root=source_root,
                working_directory=None,
                guardian_arguments=("--launcher-no-dialog",),
            )
            result, _ = self._shell_execute_and_wait(
                report.shortcut,
                "",
            )
            self.assertEqual(result, guardian.EXIT_BY_CODE[guardian.CHILD_FAILED])
            log = (root / "local" / "LocalCAT" / "source-launcher.log").read_text(
                encoding="utf-8"
            )
            self.assertIn(guardian.CHILD_FAILED, log)

    def test_real_shortcut_maps_abnormal_child_status_to_guardian_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT aborted child ") as temporary:
            root = Path(temporary).resolve()
            os.environ["LOCALAPPDATA"] = str(root / "local")
            source_root = root / "checkout"
            source_root.mkdir()
            (source_root / "qt_editor.py").write_text(
                "def main(argv):\n    raise SystemExit(0xC0000409)\n",
                encoding="utf-8",
            )
            shortcut = root / "menu" / "LocalCAT Source.lnk"
            report = launcher._install_windows_source_launcher(
                shortcut,
                source_root=source_root,
                working_directory=None,
                guardian_arguments=("--launcher-no-dialog",),
            )
            result, _ = self._shell_execute_and_wait(
                report.shortcut,
                "",
            )
            self.assertEqual(result, guardian.EXIT_BY_CODE[guardian.CHILD_ABORTED])
            log = (root / "local" / "LocalCAT" / "source-launcher.log").read_text(
                encoding="utf-8"
            )
            self.assertIn(guardian.CHILD_ABORTED, log)

    def test_dialog_receives_only_stable_diagnostic_code(self) -> None:
        with patch.object(guardian, "_append_diagnostic") as append, patch.object(
            guardian,
            "_show_failure_dialog",
        ) as dialog:
            result = guardian._fail(guardian.CHILD_FAILED, show_dialog=True)
        self.assertEqual(result, guardian.EXIT_BY_CODE[guardian.CHILD_FAILED])
        append.assert_called_once_with(guardian.CHILD_FAILED)
        dialog.assert_called_once_with(guardian.CHILD_FAILED)

    def test_hidden_source_marker_requires_smoke_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="LocalCAT marker gate ") as temporary:
            marker = Path(temporary) / "marker.json"
            with patch("builtins.print"):
                result = qt_editor.main(
                    ["--source-launch-smoke-marker", str(marker)]
                )
            self.assertEqual(result, 1)
            self.assertFalse(marker.exists())

    @staticmethod
    def _shell_execute_and_wait(shortcut: Path, arguments: str) -> tuple[int, int]:
        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            )

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(SHELLEXECUTEINFOW),)
        shell32.ShellExecuteExW.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessId.argtypes = (wintypes.HANDLE,)
        kernel32.GetProcessId.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        invocation = SHELLEXECUTEINFOW()
        invocation.cbSize = ctypes.sizeof(invocation)
        invocation.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        invocation.lpVerb = "open"
        invocation.lpFile = str(shortcut)
        invocation.lpParameters = arguments
        invocation.nShow = 1
        if not shell32.ShellExecuteExW(ctypes.byref(invocation)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not invocation.hProcess:
                raise RuntimeError("shortcut did not return one process handle")
            process_id = int(kernel32.GetProcessId(invocation.hProcess))
            if process_id <= 0:
                raise ctypes.WinError(ctypes.get_last_error())
            wait_result = kernel32.WaitForSingleObject(invocation.hProcess, 120_000)
            if wait_result != 0:
                raise TimeoutError("Windows source launcher did not exit normally")
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(invocation.hProcess, ctypes.byref(exit_code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(exit_code.value), process_id
        finally:
            if invocation.hProcess:
                kernel32.CloseHandle(invocation.hProcess)


if __name__ == "__main__":
    unittest.main()
