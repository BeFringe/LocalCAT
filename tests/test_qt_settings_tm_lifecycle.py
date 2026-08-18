"""Task 6.2 Qt settings journeys for canonical TM lifecycle controls."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from capability_host import CapabilityHost
from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository
from tm_application_composition import (
    TMResourceResolver,
    TMRuntimeHost,
    _TMEngineLegacyBackend,
)
from tm_migration import TMMigrationService
from tests.test_tm_initial_activation_recovery import _legacy_failure


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _write_legacy(path: Path, *records: tuple[str, str]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                {"source": source, "target": target},
                ensure_ascii=False,
            )
            + "\n"
            for source, target in records
        ),
        encoding="utf-8",
    )


def _controller(
    root: Path,
    *,
    resources: int = 1,
) -> tuple[EditorController, tuple[str, ...]]:
    repository = ResourceRepository(root / "app-data")
    resource_ids: list[str] = []
    for index in range(resources):
        resource = repository.create_resource(
            f"TM {index + 1}",
            ResourceKind.TRANSLATION_MEMORY,
        )
        _write_legacy(
            resource.path,
            ("Hello", f"你好 {index + 1}"),
        )
        resource_ids.append(resource.id)
    runtime_host = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=repository.list_resources(),
    )
    adapter = EditorTMAdapter(
        runtime_host=runtime_host,
        capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(
        EditorProject(
            name="Qt TM lifecycle",
            segments=(EditorSegment(id="segment-1", source="Hello"),),
        )
    )
    return controller, tuple(resource_ids)


class QtSettingsTMLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _status(self, dialog: QtSettingsDialog, resource_id: str) -> QLabel:
        status = dialog.findChild(QLabel, f"tmStatus_{resource_id}")
        self.assertIsNotNone(status)
        assert status is not None
        return status

    def _action(self, dialog: QtSettingsDialog, resource_id: str) -> QPushButton:
        action = dialog.findChild(QPushButton, f"tmLifecycle_{resource_id}")
        self.assertIsNotNone(action)
        assert action is not None
        return action

    def _complete_operation(
        self,
        dialog: QtSettingsDialog,
        controller: EditorController,
        *,
        timeout: float = 30.0,
    ) -> None:
        operation = controller.tm_activation_operation()
        self.assertIsNotNone(operation)
        assert operation is not None
        completed = controller.wait_tm_activation(
            operation.operation_id,
            timeout=timeout,
        )
        self.assertTrue(completed.completed)
        dialog._poll_tm_operation()
        self._events()

    def test_open_and_refresh_are_read_only_and_show_legacy_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            before_epoch = controller.query_epoch
            with (
                patch.object(
                    controller,
                    "tm_suggestion_report",
                    side_effect=AssertionError("settings must not query"),
                ) as query,
                patch.object(
                    TMMigrationService,
                    "activate_initial",
                    autospec=True,
                ) as activate,
                patch.object(
                    TMMigrationService,
                    "rebuild_from_snapshot",
                    autospec=True,
                ) as rebuild,
            ):
                dialog = QtSettingsDialog(controller)
                dialog.refresh_resources()

            self.assertEqual(query.call_count, 0)
            self.assertEqual(activate.call_count, 0)
            self.assertEqual(rebuild.call_count, 0)
            self.assertEqual(controller.query_epoch, before_epoch)
            self.assertEqual(
                self._status(dialog, resource_id).property("tm_mode"),
                "LEGACY_EXACT_ONLY",
            )
            self.assertIn("Legacy exact-only", self._status(dialog, resource_id).text())
            self.assertEqual(self._action(dialog, resource_id).text(), "激活 canonical")
            dialog.close()

    def test_cancelled_preflight_keeps_bytes_and_never_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            resource = controller.list_resources()[0]
            before = resource.path.read_bytes()
            dialog = QtSettingsDialog(controller)

            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Cancel,
                ) as question,
                patch.object(
                    TMMigrationService,
                    "activate_initial",
                    autospec=True,
                ) as activate,
            ):
                self._action(dialog, resource_id).click()

            self.assertEqual(question.call_count, 1)
            prompt = str(question.call_args.args[2])
            self.assertIn("TM 1", prompt)
            self.assertIn("有效 1", prompt)
            self.assertIn("Legacy exact-only", prompt)
            self.assertEqual(activate.call_count, 0)
            self.assertIsNone(controller.tm_activation_operation())
            self.assertEqual(resource.path.read_bytes(), before)
            self.assertIn("已取消", dialog.status_label.text())
            dialog.close()

    def test_busy_disables_duplicate_actions_then_proven_failure_keeps_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            entered = Event()
            release = Event()
            calls: list[str] = []

            def blocking_failure(
                _service: TMMigrationService,
                _source: Path,
                called_resource_id: str,
            ):
                calls.append(called_resource_id)
                entered.set()
                if not release.wait(10.0):
                    raise AssertionError("activation test worker was not released")
                return _legacy_failure()

            dialog = QtSettingsDialog(controller)
            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch.object(
                    TMMigrationService,
                    "activate_initial",
                    autospec=True,
                    side_effect=blocking_failure,
                ),
            ):
                self._action(dialog, resource_id).click()
                self.assertTrue(entered.wait(5.0))
                self._events()
                action = self._action(dialog, resource_id)
                self.assertFalse(action.isEnabled())
                self.assertIn("激活中", self._status(dialog, resource_id).text())
                action.click()
                self.assertEqual(calls, [resource_id])
                release.set()
                self._complete_operation(dialog, controller)

            self.assertEqual(calls, [resource_id])
            self.assertEqual(
                self._status(dialog, resource_id).property("tm_mode"),
                "LEGACY_EXACT_ONLY",
            )
            self.assertIn("失败", dialog.status_label.text())
            self.assertNotIn("path", dialog.status_label.text().lower())
            self.assertNotIn("proof", dialog.status_label.text().lower())
            dialog.close()

    def test_real_activation_and_explicit_rebuild_refresh_canonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            resource = controller.list_resources()[0]
            dialog = QtSettingsDialog(controller)
            prompts: list[str] = []

            def confirm(
                _parent: object,
                _title: str,
                message: str,
                *_args: object,
            ) -> QMessageBox.StandardButton:
                prompts.append(message)
                return QMessageBox.StandardButton.Yes

            with patch.object(QMessageBox, "question", side_effect=confirm):
                self._action(dialog, resource_id).click()
                self._complete_operation(dialog, controller)

                self.assertEqual(
                    self._status(dialog, resource_id).property("tm_mode"),
                    "CANONICAL_ACTIVE",
                )
                self.assertIn("Canonical active", self._status(dialog, resource_id).text())
                self.assertEqual(
                    self._action(dialog, resource_id).text(),
                    "重建 canonical",
                )

                _write_legacy(
                    resource.path,
                    ("Hello", "你好更新"),
                    ("Goodbye", "再见"),
                )
                dialog.refresh_resources()
                self.assertEqual(
                    self._status(dialog, resource_id).property("tm_mode"),
                    "SOURCE_DIVERGED",
                )

                self._action(dialog, resource_id).click()
                self._complete_operation(dialog, controller)

            self.assertEqual(len(prompts), 2)
            self.assertIn("Legacy exact-only", prompts[0])
            self.assertIn("保留 last-known-good", prompts[1])
            self.assertEqual(
                self._status(dialog, resource_id).property("tm_mode"),
                "CANONICAL_ACTIVE",
            )
            self.assertEqual(
                controller.tm_suggestion_report().suggestions[0].target,
                "你好更新",
            )
            dialog.close()

    def test_degraded_query_status_persists_without_settings_requery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            with patch.object(
                _TMEngineLegacyBackend,
                "query_exact",
                autospec=True,
                side_effect=OSError("private source body"),
            ):
                report = controller.tm_suggestion_report()
            self.assertEqual(report.resource_statuses[0].mode.value, "DEGRADED")

            with patch.object(
                controller,
                "tm_suggestion_report",
                side_effect=AssertionError("settings must not requery"),
            ) as query:
                dialog = QtSettingsDialog(controller)
                dialog.refresh_resources()

            self.assertEqual(query.call_count, 0)
            status = self._status(dialog, resource_id)
            self.assertEqual(status.property("tm_mode"), "DEGRADED")
            self.assertIn("Degraded", status.text())
            self.assertNotIn("private source body", status.text())
            dialog.close()

    def test_unavailable_resource_does_not_hide_other_resource_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, resource_ids = _controller(Path(temporary), resources=2)
            first_id, missing_id = resource_ids
            missing = next(
                resource
                for resource in controller.list_resources()
                if resource.id == missing_id
            )
            missing.path.unlink()
            controller.reload_resources()
            report = controller.tm_suggestion_report()
            self.assertEqual(
                {suggestion.resource_id for suggestion in report.suggestions},
                {first_id},
            )
            dialog = QtSettingsDialog(controller)

            first = self._status(dialog, first_id)
            unavailable = self._status(dialog, missing_id)
            self.assertEqual(first.property("tm_mode"), "LEGACY_EXACT_ONLY")
            self.assertEqual(unavailable.property("tm_mode"), "UNAVAILABLE")
            self.assertIn("Unavailable", unavailable.text())
            self.assertNotIn(str(missing.path), unavailable.text())
            self.assertFalse(self._action(dialog, missing_id).isEnabled())
            dialog.close()

    def test_unknown_preflight_exception_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            dialog = QtSettingsDialog(controller)
            secret = "/private/customer/source.jsonl proof-body secret"

            with patch.object(
                TMMigrationService,
                "preflight",
                autospec=True,
                side_effect=RuntimeError(secret),
            ):
                self._action(dialog, resource_id).click()

            feedback = dialog.status_label.text()
            self.assertIn("无法开始", feedback)
            self.assertNotIn(secret, feedback)
            self.assertNotIn("/private", feedback)
            self.assertNotIn("proof-body", feedback)
            self.assertIsNone(controller.tm_activation_operation())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
