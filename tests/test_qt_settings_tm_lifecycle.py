"""Task 6.2 Qt settings journeys for canonical TM lifecycle controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
)
from PySide6.QtGui import QAction, QColor, QFontMetrics, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
)

from capability_host import CapabilityHost
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMResourceDisplayMode,
)
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from qt_settings_dialog import DEFAULT_VISIBLE_RESOURCE_ROWS, QtSettingsDialog
from resource_repository import ResourceRepository
from tm_application_composition import (
    TMResourceResolver,
    TMRuntimeHost,
    _TMEngineLegacyBackend,
)
from tm_contracts import CanonicalResourceIdentity
from tm_migration import TMMigrationService
from tm_sqlite_store import _activation_journal_path
from tests.test_tm_canonical_reattestation import _rewrite_persisted_devices
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

    def _action(self, dialog: QtSettingsDialog, resource_id: str) -> QAction:
        action = dialog.findChild(QAction, f"tmLifecycleAction_{resource_id}")
        self.assertIsNotNone(action)
        assert action is not None
        return action

    def _more(self, dialog: QtSettingsDialog, resource_id: str) -> QToolButton:
        more = dialog.findChild(QToolButton, f"more_{resource_id}")
        self.assertIsNotNone(more)
        assert more is not None
        return more

    def _kind_state(self, dialog: QtSettingsDialog, resource_id: str) -> QLabel:
        state = dialog.findChild(QLabel, f"tmKindState_{resource_id}")
        self.assertIsNotNone(state)
        assert state is not None
        return state

    def _capabilities(self, dialog: QtSettingsDialog, resource_id: str) -> QLabel:
        capabilities = dialog.findChild(QLabel, f"tmCapabilities_{resource_id}")
        self.assertIsNotNone(capabilities)
        assert capabilities is not None
        return capabilities

    def _assert_app_owned_more_indicator(self, button: QToolButton) -> None:
        image = button.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertEqual(button.text(), "")
        opaque = {
            (x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        }
        components: list[set[tuple[int, int]]] = []
        while opaque:
            pending = [opaque.pop()]
            component = set(pending)
            while pending:
                x, y = pending.pop()
                for adjacent_x in range(x - 1, x + 2):
                    for adjacent_y in range(y - 1, y + 2):
                        adjacent = (adjacent_x, adjacent_y)
                        if adjacent in opaque:
                            opaque.remove(adjacent)
                            component.add(adjacent)
                            pending.append(adjacent)
            components.append(component)
        self.assertEqual(len(components), 3)
        boxes = sorted(
            (
                min(x for x, _y in component),
                min(y for _x, y in component),
                max(x for x, _y in component),
                max(y for _x, y in component),
            )
            for component in components
        )
        component_sizes = {
            (right - left + 1, bottom - top + 1)
            for left, top, right, bottom in boxes
        }
        self.assertEqual(len(component_sizes), 1)
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
        ratio = image.devicePixelRatio()
        x_delta = abs((left + right + 1) / 2 - image.width() / 2) / ratio
        y_delta = abs((top + bottom + 1) / 2 - image.height() / 2) / ratio
        self.assertLessEqual(x_delta, 1.0)
        self.assertLessEqual(y_delta, 1.0)

    def _assert_wrapped_label_is_fully_visible(self, label: QLabel) -> None:
        contents = label.contentsRect()
        required = QFontMetrics(label.font()).boundingRect(
            QRect(0, 0, max(1, contents.width()), 10_000),
            int(
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignTop
                | Qt.TextFlag.TextWordWrap
            ),
            label.text(),
        ).height()
        self.assertGreaterEqual(
            contents.height(),
            required,
            f"{label.objectName()} clips {label.text()!r} at {contents.size().toTuple()}",
        )
        self.assertTrue(label.wordWrap())

    def _wheel(
        self,
        receiver: QObject,
        *,
        angle_y: int = 0,
        pixel_y: int = 0,
        phase: Qt.ScrollPhase = Qt.ScrollPhase.ScrollUpdate,
    ) -> QWheelEvent:
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, pixel_y),
            QPoint(0, angle_y),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            phase,
            False,
        )
        QApplication.sendEvent(receiver, event)
        self._events()
        return event

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

    def test_device_drift_offers_canonical_revalidation_not_fuzzy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            completed = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )
            self.assertTrue(completed.succeeded)
            config = controller.repository.get(resource_id)
            identity = CanonicalResourceIdentity.from_configured_jsonl(
                resource_id,
                config.path,
            )
            _rewrite_persisted_devices(
                _activation_journal_path(identity),
                persisted_device=config.path.stat().st_dev + 17,
            )
            dialog = QtSettingsDialog(controller)
            dialog.show()
            self._events()

            status = next(
                item
                for item in controller.tm_resource_statuses()
                if item.resource_id == resource_id
            )
            self.assertEqual(
                status.safe_codes,
                ("TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED",),
            )
            action = self._action(dialog, resource_id)
            self.assertEqual(action.text(), "重新验证 canonical")
            self.assertTrue(action.isEnabled())
            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch.object(
                    controller,
                    "revalidate_tm_fuzzy",
                    side_effect=AssertionError(
                        "canonical re-attestation must not run Gate D"
                    ),
                ) as fuzzy_revalidation,
            ):
                action.trigger()
                self._events()
                self._complete_operation(dialog, controller)
                fuzzy_revalidation.assert_not_called()

            refreshed = next(
                item
                for item in controller.tm_resource_statuses()
                if item.resource_id == resource_id
            )
            self.assertEqual(
                refreshed.mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertIn("已完成", dialog.status_label.text())
            dialog.close()

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
            self.assertIsNone(dialog.findChild(QPushButton, f"tmLifecycle_{resource_id}"))
            kind_state = self._kind_state(dialog, resource_id)
            self.assertEqual(kind_state.property("tm_semantics"), "legacy")
            self.assertIn("Legacy exact-only", kind_state.accessibleName())
            self.assertEqual(
                self._capabilities(dialog, resource_id).text(),
                "Exact 可用 · Context 不可用 · Fuzzy 不可用",
            )
            dialog.close()

    def test_pixel_only_trackpad_partially_consumes_inner_then_outer_scroll(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, resource_ids = _controller(Path(temporary), resources=6)
            for resource_id in resource_ids[4:]:
                resource = next(
                    configured
                    for configured in controller.list_resources()
                    if configured.id == resource_id
                )
                controller.update_resource(replace(resource, active=False))

            dialog = QtSettingsDialog(controller)
            dialog.resize(860, 560)
            dialog.show()
            self._events()
            inner = dialog.active_table.verticalScrollBar()
            outer = dialog.resource_tables_scroll.verticalScrollBar()
            self.assertEqual(inner.maximum(), 56)
            self.assertEqual(outer.maximum(), 308)

            phases = (
                Qt.ScrollPhase.NoScrollPhase,
                Qt.ScrollPhase.ScrollBegin,
                Qt.ScrollPhase.ScrollUpdate,
                Qt.ScrollPhase.ScrollEnd,
            )
            for phase in phases:
                with self.subTest(phase=phase):
                    inner.setValue(inner.minimum())
                    outer.setValue(outer.minimum())
                    first = self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=-17,
                        phase=phase,
                    )
                    self.assertTrue(first.isAccepted())
                    self.assertEqual(inner.value(), 17)
                    self.assertEqual(outer.value(), 0)

                    second = self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=-31,
                        phase=phase,
                    )
                    self.assertTrue(second.isAccepted())
                    self.assertEqual(inner.value(), 48)
                    self.assertEqual(outer.value(), 0)

                    third = self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=-31,
                        phase=phase,
                    )
                    self.assertTrue(third.isAccepted())
                    self.assertEqual(inner.value(), inner.maximum())
                    self.assertEqual(outer.value(), 23)

                    self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=31,
                        phase=phase,
                    )
                    self.assertEqual(inner.value(), 25)
                    self.assertEqual(outer.value(), 23)
                    self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=64,
                        phase=phase,
                    )
                    self.assertEqual(inner.value(), inner.minimum())
                    self.assertEqual(outer.value(), outer.minimum())

                    inner.setValue(inner.maximum())
                    outer.setValue(outer.maximum())
                    self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=-31,
                        phase=phase,
                    )
                    self.assertEqual(inner.value(), inner.maximum())
                    self.assertEqual(outer.value(), outer.maximum())
                    inner.setValue(inner.minimum())
                    outer.setValue(outer.minimum())
                    self._wheel(
                        dialog.active_table.viewport(),
                        pixel_y=31,
                        phase=phase,
                    )
                    self.assertEqual(inner.value(), inner.minimum())
                    self.assertEqual(outer.value(), outer.minimum())

            self.assertEqual(
                dialog.close_button.visibleRegion().boundingRect(),
                dialog.close_button.rect(),
            )
            dialog.close()

    def test_1180_render_has_one_tm_name_nonoverlapping_status_and_yellow_legacy_dot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, (resource_id,) = _controller(Path(temporary))
            resource = controller.list_resources()[0]
            dialog = QtSettingsDialog(controller)
            dialog.resize(1180, 680)
            dialog.show()
            self._events()

            table = dialog.active_table
            row = next(
                row
                for row in range(table.rowCount())
                if table.cellWidget(row, 3) is not None
                and table.cellWidget(row, 3).objectName() == f"tmResource_{resource_id}"
            )
            name = dialog.findChild(QLabel, f"resourceName_{resource_id}")
            status = self._status(dialog, resource_id)
            state = self._kind_state(dialog, resource_id)
            assert name is not None

            self.assertIsNone(table.item(row, 3))
            self.assertEqual(name.text(), resource.name)
            self.assertFalse(name.geometry().intersects(status.geometry()))
            self.assertGreaterEqual(status.height(), status.minimumSizeHint().height())
            self.assertNotIn("\n", status.text())
            capabilities = self._capabilities(dialog, resource_id)
            self.assertFalse(table.cellWidget(row, 3).grab().toImage().isNull())
            kind_cell = table.cellWidget(row, 4)
            self.assertIsNotNone(kind_cell)
            assert kind_cell is not None
            self.assertEqual(kind_cell.findChildren(QCheckBox), [])
            self.assertGreaterEqual(
                capabilities.width(),
                capabilities.sizeHint().width(),
            )
            self.assertGreaterEqual(
                capabilities.height(),
                capabilities.minimumSizeHint().height(),
            )
            for phrase in ("Exact 可用", "Context 不可用", "Fuzzy 不可用"):
                self.assertIn(phrase, name.toolTip())
                self.assertIn(phrase, name.accessibleName())
                self.assertIn(phrase, capabilities.toolTip())
                self.assertIn(phrase, kind_cell.accessibleName())
            state_image = state.grab().toImage()
            self.assertFalse(state_image.isNull())
            self.assertEqual(
                state_image.pixelColor(state_image.rect().center()),
                QColor("#d59a00"),
            )
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
                self._action(dialog, resource_id).trigger()

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
                self._action(dialog, resource_id).trigger()
                self.assertTrue(entered.wait(5.0))
                self._events()
                action = self._action(dialog, resource_id)
                self.assertFalse(action.isEnabled())
                self.assertIn("激活中", self._status(dialog, resource_id).text())
                action.trigger()
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
                self._action(dialog, resource_id).trigger()
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
                canonical_state = self._kind_state(dialog, resource_id)
                self.assertEqual(canonical_state.property("tm_semantics"), "canonical")
                canonical_capabilities = self._capabilities(dialog, resource_id)
                self.assertEqual(
                    canonical_capabilities.text(),
                    "Exact 可用 · Context 不可用 · Fuzzy 不可用",
                )
                for phrase in ("Exact 可用", "Context 不可用", "Fuzzy 不可用"):
                    self.assertIn(phrase, canonical_capabilities.accessibleName())
                    self.assertIn(phrase, canonical_capabilities.toolTip())
                canonical_image = canonical_state.grab().toImage()
                self.assertEqual(
                    canonical_image.pixelColor(canonical_image.rect().center()),
                    QColor("#2f9e44"),
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
                diverged_state = self._kind_state(dialog, resource_id)
                self.assertEqual(diverged_state.property("tm_semantics"), "canonical")
                self.assertIn("last-known-good", diverged_state.accessibleName())

                self._action(dialog, resource_id).trigger()
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
            dialog.resize(1180, 680)
            dialog.show()
            self._events()

            first = self._status(dialog, first_id)
            unavailable = self._status(dialog, missing_id)
            self.assertEqual(first.property("tm_mode"), "LEGACY_EXACT_ONLY")
            self.assertEqual(unavailable.property("tm_mode"), "UNAVAILABLE")
            self.assertIn("Unavailable", unavailable.text())
            self.assertNotIn(str(missing.path), unavailable.text())
            self.assertFalse(self._action(dialog, missing_id).isEnabled())
            unavailable_state = self._kind_state(dialog, missing_id)
            self.assertEqual(unavailable_state.property("tm_semantics"), "unavailable")
            unavailable_capabilities = self._capabilities(dialog, missing_id)
            self.assertEqual(
                unavailable_capabilities.text(),
                "Exact 不可用 · Context 不可用 · Fuzzy 不可用",
            )
            self.assertGreaterEqual(
                unavailable_capabilities.width(),
                unavailable_capabilities.sizeHint().width(),
            )
            for phrase in ("Exact 不可用", "Context 不可用", "Fuzzy 不可用"):
                self.assertIn(phrase, unavailable_capabilities.accessibleName())
                self.assertIn(phrase, unavailable_capabilities.toolTip())
            dialog.close()

    def test_more_indicator_is_centered_for_canonical_legacy_and_unavailable_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, resource_ids = _controller(Path(temporary), resources=3)
            canonical_id, legacy_id, missing_id = resource_ids
            activation_dialog = QtSettingsDialog(controller)
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                self._action(activation_dialog, canonical_id).trigger()
                self._complete_operation(activation_dialog, controller)
            activation_dialog.close()

            missing = next(
                resource
                for resource in controller.list_resources()
                if resource.id == missing_id
            )
            missing.path.unlink()
            controller.reload_resources()
            controller.tm_suggestion_report()

            dialog = QtSettingsDialog(controller)
            dialog.show()
            self._events()
            expected_modes = {
                canonical_id: "CANONICAL_ACTIVE",
                legacy_id: "LEGACY_EXACT_ONLY",
                missing_id: "UNAVAILABLE",
            }
            observed_row_heights: set[int] = set()
            for width in (860, 1180, 1320):
                dialog.resize(width, 680)
                self._events()
                for resource_id, expected_mode in expected_modes.items():
                    status = self._status(dialog, resource_id)
                    self.assertEqual(status.property("tm_mode"), expected_mode)
                    button = self._more(dialog, resource_id)
                    row = next(
                        row
                        for row in range(dialog.active_table.rowCount())
                        if dialog.active_table.cellWidget(row, 7) is button
                    )
                    observed_row_heights.add(dialog.active_table.rowHeight(row))
                    holder = dialog.active_table.cellWidget(row, 3)
                    self.assertIsNotNone(holder)
                    assert holder is not None
                    name = dialog.findChild(QLabel, f"resourceName_{resource_id}")
                    self.assertIsNotNone(name)
                    assert name is not None
                    self.assertFalse(name.geometry().intersects(status.geometry()))
                    self._assert_wrapped_label_is_fully_visible(status)
                    self.assertLessEqual(
                        status.geometry().bottom(),
                        holder.contentsRect().bottom(),
                    )
                    self.assertGreaterEqual(
                        dialog.active_table.rowHeight(row),
                        holder.minimumSizeHint().height(),
                    )
                    capabilities = self._capabilities(dialog, resource_id)
                    self.assertGreaterEqual(
                        capabilities.contentsRect().width(),
                        capabilities.sizeHint().width(),
                    )
                    self.assertEqual(button.width(), 32)
                    self.assertEqual(button.toolTip(), button.accessibleName())
                    self.assertTrue(button.menu())
                    self._assert_app_owned_more_indicator(button)
                self.assertEqual(
                    dialog.active_table.verticalScrollBarPolicy(),
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )
                last_row = dialog.active_table.rowCount() - 1
                self.assertLessEqual(
                    dialog.active_table.rowViewportPosition(last_row)
                    + dialog.active_table.rowHeight(last_row),
                    dialog.active_table.viewport().height(),
                )
                self.assertEqual(
                    dialog.inactive_table.verticalScrollBarPolicy(),
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                )
                self.assertGreater(
                    dialog.inactive_table.height(),
                    dialog.inactive_table.horizontalHeader().height(),
                )
                active_group = dialog.active_table.parentWidget()
                inactive_group = dialog.inactive_table.parentWidget()
                assert active_group is not None
                assert inactive_group is not None
                self.assertLess(
                    active_group.geometry().bottom(),
                    inactive_group.geometry().top(),
                )
            self.assertGreaterEqual(len(observed_row_heights), 2)
            dialog.close()

    def test_tables_show_three_rows_then_scroll_to_a_fully_clickable_last_row(
        self,
    ) -> None:
        self.assertEqual(DEFAULT_VISIBLE_RESOURCE_ROWS, 3)
        with tempfile.TemporaryDirectory() as temporary:
            controller, resource_ids = _controller(Path(temporary), resources=8)
            for resource_id in resource_ids[4:]:
                resource = next(
                    configured
                    for configured in controller.list_resources()
                    if configured.id == resource_id
                )
                controller.update_resource(replace(resource, active=False))

            dialog = QtSettingsDialog(controller)
            dialog.show()
            self._events()
            for width, height in ((860, 560), (860, 734), (1180, 680)):
                dialog.resize(width, height)
                self._events()
                self.assertEqual(
                    dialog.close_button.visibleRegion().boundingRect(),
                    dialog.close_button.rect(),
                )
                for table, ids in (
                    (dialog.active_table, resource_ids[:4]),
                    (dialog.inactive_table, resource_ids[4:]),
                ):
                    self.assertEqual(table.rowCount(), 4)
                    self.assertEqual(
                        table.verticalScrollMode(),
                        QAbstractItemView.ScrollMode.ScrollPerPixel,
                    )
                    self.assertNotEqual(
                        table.verticalScrollBarPolicy(),
                        Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
                    )
                    visible_rows_height = sum(
                        sorted(
                            (table.rowHeight(row) for row in range(table.rowCount())),
                            reverse=True,
                        )[:DEFAULT_VISIBLE_RESOURCE_ROWS]
                    )
                    self.assertEqual(
                        table.height(),
                        table.horizontalHeader().height()
                        + visible_rows_height
                        + table.frameWidth() * 2,
                    )
                    scrollbar = table.verticalScrollBar()
                    self.assertGreater(scrollbar.maximum(), 0)
                    scrollbar.setValue(scrollbar.maximum())
                    self._events()
                    last_row = table.rowCount() - 1
                    self.assertGreaterEqual(table.rowViewportPosition(last_row), 0)
                    self.assertLessEqual(
                        table.rowViewportPosition(last_row)
                        + table.rowHeight(last_row),
                        table.viewport().height(),
                    )
                    last_more = self._more(dialog, ids[-1])
                    tables_scroll = dialog.findChild(QScrollArea, "resourceTablesScroll")
                    self.assertIsNotNone(tables_scroll)
                    assert tables_scroll is not None
                    tables_scroll.ensureWidgetVisible(last_more)
                    self._events()
                    self._assert_app_owned_more_indicator(last_more)
                    opened: list[bool] = []

                    def close_menu() -> None:
                        opened.append(True)
                        QTimer.singleShot(0, last_more.menu().close)

                    last_more.menu().aboutToShow.connect(close_menu)
                    QTest.mouseClick(last_more, Qt.MouseButton.LeftButton)
                    self._events()
                    self.assertEqual(opened, [True])
                    last_more.menu().aboutToShow.disconnect(close_menu)
            dialog.close()

    def test_wheel_hands_off_between_inner_tables_and_outer_resource_scroll(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, resource_ids = _controller(Path(temporary), resources=6)
            for resource_id in resource_ids[4:]:
                resource = next(
                    configured
                    for configured in controller.list_resources()
                    if configured.id == resource_id
                )
                controller.update_resource(replace(resource, active=False))

            dialog = QtSettingsDialog(controller)
            dialog.resize(860, 560)
            dialog.show()
            self._events()
            active_inner = dialog.active_table.verticalScrollBar()
            inactive_inner = dialog.inactive_table.verticalScrollBar()
            outer = dialog.resource_tables_scroll.verticalScrollBar()
            self.assertGreater(active_inner.maximum(), 0)
            self.assertEqual(inactive_inner.maximum(), 0)
            self.assertGreater(outer.maximum(), 0)
            active_inner.setValue(active_inner.minimum())
            outer.setValue(outer.minimum())

            self._wheel(dialog.active_table.viewport(), angle_y=-120)
            self.assertGreater(active_inner.value(), active_inner.minimum())
            self.assertEqual(outer.value(), outer.minimum())
            while active_inner.value() < active_inner.maximum():
                self._wheel(dialog.active_table.viewport(), angle_y=-120)
            self.assertEqual(active_inner.value(), active_inner.maximum())

            self._wheel(dialog.active_table.viewport(), angle_y=-120)
            self.assertGreater(outer.value(), outer.minimum())
            for _ in range(12):
                self._wheel(dialog.active_table.viewport(), angle_y=-120)
            self.assertEqual(outer.value(), outer.maximum())
            last_inactive_more = self._more(dialog, resource_ids[-1])
            self.assertEqual(
                last_inactive_more.visibleRegion().boundingRect(),
                last_inactive_more.rect(),
            )

            inner_at_boundary = active_inner.value()
            outer_at_boundary = outer.value()
            self._wheel(dialog.active_table.viewport(), angle_y=-120)
            self.assertEqual(active_inner.value(), inner_at_boundary)
            self.assertEqual(outer.value(), outer_at_boundary)

            self._wheel(dialog.inactive_table.viewport(), angle_y=120)
            self.assertLess(outer.value(), outer.maximum())
            outer.setValue(outer.maximum())
            self._wheel(dialog.inactive_table.viewport(), pixel_y=48)
            self.assertLess(outer.value(), outer.maximum())
            self.assertEqual(inactive_inner.value(), inactive_inner.minimum())
            for _ in range(20):
                self._wheel(dialog.inactive_table.viewport(), pixel_y=48)
            self.assertEqual(outer.value(), outer.minimum())
            active_group = dialog.active_table.parentWidget()
            self.assertIsNotNone(active_group)
            assert active_group is not None
            self.assertEqual(active_group.geometry().top(), 0)
            self._wheel(dialog.active_table.viewport(), angle_y=120)
            self.assertLess(active_inner.value(), active_inner.maximum())
            self.assertEqual(outer.value(), outer.minimum())
            while active_inner.value() > active_inner.minimum():
                self._wheel(dialog.active_table.viewport(), angle_y=120)
            first_active_more = self._more(dialog, resource_ids[0])
            self.assertEqual(
                first_active_more.visibleRegion().boundingRect(),
                first_active_more.rect(),
            )

            self._wheel(dialog.inactive_table.viewport(), pixel_y=48)
            self.assertEqual(outer.value(), outer.minimum())
            self._wheel(dialog.inactive_table.viewport(), pixel_y=-48)
            self.assertGreater(outer.value(), outer.minimum())
            outer.setValue(outer.minimum())
            self.assertEqual(
                dialog.close_button.visibleRegion().boundingRect(),
                dialog.close_button.rect(),
            )
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
                self._action(dialog, resource_id).trigger()

            feedback = dialog.status_label.text()
            self.assertIn("无法开始", feedback)
            self.assertNotIn(secret, feedback)
            self.assertNotIn("/private", feedback)
            self.assertNotIn("proof-body", feedback)
            self.assertIsNone(controller.tm_activation_operation())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
