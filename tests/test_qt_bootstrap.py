from __future__ import annotations

import ast
import builtins
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import qt_editor
from resource_importer import import_termbase


ROOT = Path(__file__).resolve().parents[1]


class QtBootstrapTest(unittest.TestCase):
    def test_module_top_level_uses_only_stdlib_imports(self) -> None:
        tree = ast.parse((ROOT / "qt_editor.py").read_text(encoding="utf-8"))
        top_level_imports: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.add(node.module.split(".", 1)[0])

        self.assertLessEqual(
            top_level_imports,
            {"__future__", "argparse", "os", "sys", "pathlib"},
        )
        source = (ROOT / "qt_editor.py").read_text(encoding="utf-8")
        self.assertNotIn("from qt_editor_window import", source.split("def main", 1)[0])

    def test_missing_pyside_reports_install_command_without_traceback(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
            return original_import(name, *args, **kwargs)

        stderr = io.StringIO()
        with patch("builtins.__import__", side_effect=guarded_import):
            with contextlib.redirect_stderr(stderr):
                exit_code = qt_editor.main(["--smoke-test"])

        output = stderr.getvalue()
        self.assertNotEqual(exit_code, 0)
        self.assertIn("python -m pip install -r requirements-ui.txt", output)
        self.assertNotIn("Traceback", output)

    def test_offscreen_smoke_subprocess_reaches_usable_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            completed = subprocess.run(
                [
                    sys.executable,
                    "qt_editor.py",
                    "--smoke-test",
                    "--data-dir",
                    temp_dir,
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Qt editor smoke test passed", completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_openpyxl_only_returns_actionable_xlsx_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "terms.xlsx"
            source.write_bytes(b"placeholder")
            target = root / "terms.csv"
            target.write_bytes(b"keep")
            original_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name == "openpyxl":
                    raise ImportError("openpyxl unavailable")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=guarded_import):
                report = import_termbase(source, target)

            self.assertTrue(report.errors)
            self.assertIn("openpyxl", report.errors[0])
            self.assertEqual(target.read_bytes(), b"keep")

    def test_installs_linux_desktop_launcher_without_loading_qt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = qt_editor.install_desktop_launcher(Path(temp_dir))
            rendered = launcher.read_text(encoding="utf-8")

            self.assertEqual(launcher.name, "localcat.desktop")
            self.assertIn("[Desktop Entry]", rendered)
            self.assertIn("Name=LocalCAT", rendered)
            self.assertIn(str(Path(qt_editor.__file__).resolve()), rendered)
            self.assertIn(str(Path(sys.executable).resolve()), rendered)
            self.assertFalse(rendered.startswith("Traceback"))
            self.assertTrue(launcher.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
