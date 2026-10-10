from __future__ import annotations

import ast
import builtins
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import qt_editor
from resource_importer import import_termbase


ROOT = Path(__file__).resolve().parents[1]


class QtBootstrapTest(unittest.TestCase):
    def test_fuzzy_immediate_rejection_is_diagnosed_once_per_request(self) -> None:
        from editor_contracts import FuzzyValidationState
        from resource_repository import ResourceRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _composition = qt_editor._compose_editor_controller(
                ResourceRepository(Path(temp_dir)),
            )
            with patch.object(qt_editor, "_startup_diagnostic") as diagnostic:
                self.assertIs(
                    controller.tm_fuzzy_validation_status().state,
                    FuzzyValidationState.IDLE,
                )
                diagnostic.assert_not_called()
                for request in range(2):
                    result = controller.revalidate_tm_fuzzy()
                    self.assertIs(result.state, FuzzyValidationState.FAILED)
                    self.assertEqual(result.safe_code, "GATE_D.GATE_C_REQUIRED")
                    for _ in range(3):
                        self.assertEqual(controller.tm_fuzzy_validation_status(), result)
                    self.assertEqual(diagnostic.call_count, request + 1)
                    diagnostic.assert_called_with({
                        "stage": "fuzzy_validation",
                        "status": "failed",
                        "code": "GATE_D.GATE_C_REQUIRED",
                    })
                self.assertFalse(controller.tm_retrieval_status().fuzzy_available)

    def test_fuzzy_polled_failure_diagnostics_are_safe_and_non_authoritative(self) -> None:
        from capability_host import GateDRunState, GateDRunStatus
        from editor_contracts import FuzzyValidationState
        from resource_repository import ResourceRepository

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, composition = qt_editor._compose_editor_controller(
                ResourceRepository(Path(temp_dir)),
            )
            owner = composition.retrieval_gate_d_owner
            assert owner is not None
            with (
                patch.object(type(owner), "status") as status,
                patch.object(qt_editor, "_startup_diagnostic") as diagnostic,
            ):
                for epoch in (1, 2):
                    status.return_value = GateDRunStatus(
                        epoch=epoch, state=GateDRunState.RUNNING, safe_code=None,
                    )
                    self.assertIs(
                        controller.tm_fuzzy_validation_status().state,
                        FuzzyValidationState.RUNNING,
                    )
                    self.assertEqual(diagnostic.call_count, epoch - 1)
                    status.return_value = GateDRunStatus(
                        epoch=epoch, state=GateDRunState.FAILED,
                        safe_code="GATE_D.INPUT_AUTHORITY_UNAVAILABLE",
                    )
                    for _ in range(3):
                        result = controller.tm_fuzzy_validation_status()
                        self.assertEqual(result.safe_code, "GATE_D.INPUT_AUTHORITY_UNAVAILABLE")
                    self.assertEqual(diagnostic.call_count, epoch)
                    diagnostic.assert_called_with({
                        "stage": "fuzzy_validation", "status": "failed",
                        "code": "GATE_D.INPUT_AUTHORITY_UNAVAILABLE",
                    })

                status.return_value = GateDRunStatus(
                    epoch=3, state=GateDRunState.FAILED,
                    safe_code="GATE_D.WORK_ROOT_UNAVAILABLE",
                )
                diagnostic.side_effect = OSError("/private/customer/key: secret body")
                self.assertEqual(
                    controller.tm_fuzzy_validation_status().safe_code,
                    "GATE_D.WORK_ROOT_UNAVAILABLE",
                )
                controller.tm_fuzzy_validation_status()
                self.assertEqual(diagnostic.call_count, 3)
                self.assertFalse(controller.tm_retrieval_status().fuzzy_available)

                status.return_value = GateDRunStatus(
                    epoch=3, state=GateDRunState.SUCCEEDED, safe_code=None,
                )
                self.assertIs(
                    controller.tm_fuzzy_validation_status().state,
                    FuzzyValidationState.SUCCEEDED,
                )
                self.assertFalse(controller.tm_retrieval_status().fuzzy_available)
                self.assertEqual(diagnostic.call_count, 3)

                status.return_value = GateDRunStatus(
                    epoch=4, state=GateDRunState.FAILED,
                    safe_code="/private/customer/key: secret body",
                )
                with self.assertRaises(ValueError):
                    controller.tm_fuzzy_validation_status()
                self.assertEqual(diagnostic.call_count, 3)

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
            {
                "__future__",
                "argparse",
                "os",
                "pathlib",
                "plistlib",
                "shutil",
                "stat",
                "subprocess",
                "sys",
                "time",
            },
        )
        # A deferred helper may appear before main in the source. Verify the
        # import boundary itself, not the textual position of its definition.
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; import qt_editor; "
             "assert 'qt_editor_window' not in sys.modules; "
             "assert 'PySide6' not in sys.modules"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

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

    def test_offscreen_smoke_from_non_repository_cwd_reaches_usable_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            launch_cwd = temporary_root / "launch-cwd"
            data_dir = temporary_root / "app-data"
            launch_cwd.mkdir()
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "qt_editor.py"),
                    "--smoke-test",
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=launch_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Qt editor smoke test passed", completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)

    def test_ordinary_smoke_checks_editor_before_close_and_preserves_failures(self) -> None:
        from platform_fs import compose_platform_file_backend
        from platform_source_authority import compose_rooted_source_authority
        from qt_source_resources import resolve_source_qt_resources
        from PySide6.QtWidgets import QApplication

        # Exercise the real bootstrap/window/Controller lifecycle. The ordinary
        # authority and expensive Core checks are isolated; this is not frozen evidence.
        app = QApplication.instance() or QApplication([])
        authority = compose_rooted_source_authority(
            ROOT, backend=compose_platform_file_backend(ROOT))
        self.addCleanup(authority.close)
        resources = resolve_source_qt_resources(
            authority, logo_filename=qt_editor.APPLICATION_ICON_FILENAME)
        compose = qt_editor._compose_editor_controller
        for scenario in ('close', 'unusable', 'core_failure'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                observed = {}
                def source_composition(repository, **kwargs):
                    controller, composition = compose(repository, source_authority=authority)
                    observed['controller'] = controller
                    return controller, composition
                def start_validation(composition, window):
                    observed['window'] = window
                    if scenario == 'unusable':
                        window.controller.close_project()
                def ordinary_finish(app, composition, worker, marker, *, window):
                    self.assertTrue(window.controller.has_active_project)
                    if scenario == 'core_failure':
                        raise RuntimeError('ordinary smoke failure')
                    self.assertTrue(window.close())
                    self.assertFalse(window.controller.has_active_project)
                candidate = SimpleNamespace(read_data=lambda name: (ROOT / name).read_bytes())
                with (patch('frozen_candidate.running_candidate', return_value=candidate),
                      patch('frozen_product_entry.ordinary_resources', return_value=resources),
                      patch('frozen_product_entry.finish_ordinary_smoke', side_effect=ordinary_finish) as finish,
                      patch.object(qt_editor, '_compose_editor_controller', side_effect=source_composition),
                      patch.object(qt_editor, '_start_capability_validation', side_effect=start_validation),
                      contextlib.redirect_stderr(io.StringIO()),
                      contextlib.redirect_stdout(io.StringIO())):
                    code = qt_editor.main(['--smoke-test', '--data-dir', temp_dir,
                                           '--bundle-smoke-marker', str(Path(temp_dir) / 'marker.json')],
                                          _ordinary=True)
                self.assertEqual(code, 0 if scenario == 'close' else 1)
                self.assertEqual(finish.call_count, 0 if scenario == 'unusable' else 1)
                observed['window'].close()
                self.assertFalse(observed['controller'].has_active_project)
                observed['window'].deleteLater()
                app.processEvents()

    def test_source_qt_does_not_require_xlwings(self) -> None:
        requirements = tuple(
            line.strip().lower()
            for line in (ROOT / "requirements-ui.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertTrue(any(line.startswith("pyside6") for line in requirements))

        self.assertTrue(any(line.startswith("openpyxl") for line in requirements))
        self.assertFalse(any(line.startswith("xlwings") for line in requirements))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launch_cwd = root / "launch-cwd"
            data_dir = root / "app-data"
            launch_cwd.mkdir()
            probe = (
                "import builtins, sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "original_import = builtins.__import__\n"
                "def guarded_import(name, *args, **kwargs):\n"
                "    if name == 'xlwings' or name.startswith('xlwings.'):\n"
                "        raise AssertionError('Qt source imported xlwings')\n"
                "    return original_import(name, *args, **kwargs)\n"
                "builtins.__import__ = guarded_import\n"
                "import qt_editor\n"
                f"raise SystemExit(qt_editor.main(['--smoke-test', '--data-dir', {str(data_dir)!r}]))\n"
            )
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=launch_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Qt editor smoke test passed", completed.stdout)
        self.assertNotIn("xlwings", completed.stderr.lower())
        self.assertNotIn("Traceback", completed.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows tooltip presentation only")
    def test_windows_tooltips_disable_animated_first_show(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance() or QApplication([])
        effects = (
            Qt.UIEffect.UI_AnimateTooltip,
            Qt.UIEffect.UI_FadeTooltip,
        )
        previous = tuple(
            application.isEffectEnabled(effect) for effect in effects
        )
        try:
            for effect in effects:
                application.setEffectEnabled(effect, True)

            qt_editor._stabilize_windows_tooltips(application)

            self.assertTrue(
                all(
                    not application.isEffectEnabled(effect)
                    for effect in effects
                )
            )
        finally:
            for effect, enabled in zip(effects, previous, strict=True):
                application.setEffectEnabled(effect, enabled)

    def test_source_authority_failure_is_body_safe_before_business_imports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            launch_cwd = root / "launch-cwd"
            data_dir = root / "app-data"
            secret_path = root / "private" / "source-proof.json"
            secret_body = "proof=private-source-authority-body"
            launch_cwd.mkdir()
            probe = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import platform_source_authority as source_authority\n"
                "def fail(*args, **kwargs):\n"
                f"    raise RuntimeError({f'{secret_path}: {secret_body}'!r})\n"
                "source_authority.compose_rooted_source_authority = fail\n"
                "import qt_editor\n"
                f"code = qt_editor.main(['--smoke-test', '--data-dir', {str(data_dir)!r}])\n"
                "for name in ('capability_host', 'editor_controller', "
                "'resource_repository', 'qt_editor_window'):\n"
                "    assert name not in sys.modules, name\n"
                "raise SystemExit(code)\n"
            )
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=launch_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr.strip(),
            "LocalCAT Qt editor could not start [LOCALCAT.STARTUP.FAILED].",
        )
        self.assertNotIn(str(secret_path), completed.stderr)
        self.assertNotIn(secret_body, completed.stderr)
        self.assertNotIn("proof", completed.stderr.lower())
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_openpyxl_only_returns_actionable_xlsx_error(self) -> None:
        from tests.test_parser_xlsx_support import _archive_bytes

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "terms.xlsx"
            # A malformed placeholder now fails in the Parser's structural
            # preflight before the optional dependency is consulted.  Use a
            # valid, synthetic workbook so this test continues to isolate the
            # missing-openpyxl branch it names.
            source.write_bytes(_archive_bytes())
            target = root / "terms.csv"
            prior = b"keep,value\n"
            target.write_bytes(prior)
            import parser_termbase_codec

            original_import_module = (
                parser_termbase_codec.importlib.import_module
            )

            def guarded_import_module(name, *args, **kwargs):
                if name == "openpyxl":
                    raise ImportError("openpyxl unavailable")
                return original_import_module(name, *args, **kwargs)

            with patch(
                "parser_termbase_codec.importlib.import_module",
                side_effect=guarded_import_module,
            ):
                report = import_termbase(source, target)

            self.assertTrue(report.errors)
            self.assertIn("openpyxl", report.errors[0])
            self.assertEqual(target.read_bytes(), prior)

    def test_installs_linux_desktop_launcher_without_loading_qt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("qt_editor.sys.platform", "linux"),
                patch(
                    "qt_editor._install_linux_icon_resource",
                    return_value="localcat",
                ) as install_icon,
                patch("qt_editor._refresh_desktop_database") as refresh,
            ):
                launcher = qt_editor.install_desktop_launcher(Path(temp_dir))
            rendered = launcher.read_text(encoding="utf-8")
            icon = ROOT / "LocalCAT-logo-silver.png"

            self.assertEqual(launcher.name, "localcat.desktop")
            self.assertIn("[Desktop Entry]", rendered)
            self.assertIn("Name=LocalCAT", rendered)
            self.assertIn(
                qt_editor._desktop_exec_argument(
                    Path(qt_editor.__file__).resolve()
                ),
                rendered,
            )
            self.assertIn(
                qt_editor._desktop_exec_argument(Path(sys.executable).resolve()),
                rendered,
            )
            self.assertIn("Icon=localcat", rendered)
            self.assertIn(f"Path={ROOT.resolve()}", rendered)
            self.assertIn("StartupWMClass=LocalCAT", rendered)
            self.assertFalse(rendered.startswith("Traceback"))
            if os.name != "nt":
                self.assertTrue(launcher.stat().st_mode & 0o111)
            install_icon.assert_called_once_with(icon.resolve())
            refresh.assert_called_once_with(Path(temp_dir).resolve())

    def test_linux_menu_icon_uses_freedesktop_user_icon_resource(self) -> None:
        icon = (ROOT / "LocalCAT-logo-silver.png").resolve()
        completed = subprocess.CompletedProcess(
            args=["xdg-icon-resource"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with (
            patch("qt_editor.shutil.which", return_value="/usr/bin/xdg-icon-resource"),
            patch("qt_editor.subprocess.run", return_value=completed) as run,
        ):
            icon_name = qt_editor._install_linux_icon_resource(icon)

        self.assertEqual(icon_name, "localcat")
        run.assert_called_once_with(
            [
                "/usr/bin/xdg-icon-resource",
                "install",
                "--novendor",
                "--mode",
                "user",
                "--context",
                "apps",
                "--size",
                "512",
                str(icon),
                "localcat",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_silver_logo_is_the_single_application_icon_asset(self) -> None:
        icon = qt_editor.application_icon_path(ROOT)

        self.assertEqual(icon, (ROOT / "LocalCAT-logo-silver.png").resolve())
        self.assertTrue(icon.is_file())

    def test_real_bootstrap_applies_silver_icon_to_application_window_and_dialog(
        self,
    ) -> None:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication, QDialog, QWidget
        from qt_speaker_avatar import SpeakerAvatarCatalog

        existing_app = QApplication.instance()
        app = (
            QApplication(["localcat-icon-test"])
            if existing_app is None
            else cast(QApplication, existing_app)
        )
        app.setWindowIcon(QIcon())
        captured: dict[str, QIcon] = {}
        captured_identity: dict[str, str] = {}
        captured_catalog: list[object | None] = []

        class CapturingWindow(QWidget):
            def __init__(
                self,
                _controller: object,
                *,
                chunk_controller: object | None = None,
                speaker_avatar_catalog: object | None = None,
            ) -> None:
                super().__init__()
                self.chunk_controller = chunk_controller
                self.file_operation_running = False
                self.speaker_avatar_catalog = speaker_avatar_catalog
                captured_catalog.append(speaker_avatar_catalog)
                self.pages = SimpleNamespace(
                    currentWidget=lambda: SimpleNamespace(
                        objectName=lambda: "editorPage"
                    )
                )
                self.segment_list = SimpleNamespace(count=lambda: 1)

            def show(self) -> None:
                super().show()
                dialog = QDialog(self)
                current_app = QApplication.instance()
                assert current_app is not None
                captured["application"] = cast(
                    QApplication,
                    current_app,
                ).windowIcon()
                captured_identity["application_name"] = (
                    current_app.applicationName()
                )
                captured_identity["application_display_name"] = (
                    current_app.applicationDisplayName()
                )
                captured["window"] = self.windowIcon()
                captured["dialog"] = dialog.windowIcon()

        fake_window_module = types.ModuleType("qt_editor_window")
        setattr(fake_window_module, "QtEditorWindow", CapturingWindow)
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    sys.modules,
                    {"qt_editor_window": fake_window_module},
                ),
                patch.object(
                    qt_editor,
                    "_start_capability_validation",
                ) as start_validation,
            ):
                exit_code = qt_editor.main(
                    ["--smoke-test", "--data-dir", temp_dir]
                )

        expected = QIcon(str(ROOT / "LocalCAT-logo-silver.png")).pixmap(
            64,
            64,
        ).toImage()
        self.assertEqual(exit_code, 0)
        start_validation.assert_called_once()
        self.assertEqual(
            captured_identity,
            {
                "application_name": "LocalCAT",
                "application_display_name": "LocalCAT",
            },
        )
        self.assertEqual(set(captured), {"application", "window", "dialog"})
        self.assertEqual(len(captured_catalog), 1)
        self.assertIs(type(captured_catalog[0]), SpeakerAvatarCatalog)
        for name, icon in captured.items():
            with self.subTest(name=name):
                self.assertFalse(icon.isNull())
                self.assertEqual(icon.pixmap(64, 64).toImage(), expected)


if __name__ == "__main__":
    unittest.main()
