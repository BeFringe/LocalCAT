from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from qt_tmx_export_dialog import (
    TmxExportDialog,
    TmxExportDialogPreview,
    TmxExportScopeChoice,
)


class TmxExportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _finish_worker(self, dialog: TmxExportDialog) -> None:
        worker = dialog._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertTrue(worker.wait(3000))
        for _ in range(5):
            self.app.processEvents()

    def test_project_preview_has_explicit_scope_defaults_and_single_publish(self) -> None:
        calls: list[tuple[object, ...]] = []
        preparation = object()
        receipt = object()

        def prepare(token: str, source: str, target: str, destination: Path):
            calls.append(("prepare", token, source, target, destination))
            return TmxExportDialogPreview(
                domain_preparation=preparation,
                badge="CHUNK · 当前分工",
                title="Project · Chunk 7",
                binding="project prj-1 · plan plan-1 · revision 4 · chunk c-7",
                document_count=2,
                attached_count=31,
                included_count=29,
                excluded_count=2,
                warning_count=3,
                profile_id="localcat-tmx-level1-context-v1",
            )

        def publish(value: object):
            calls.append(("publish", value))
            return receipt

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "chunk.tmx"
            dialog = TmxExportDialog(
                title="导出项目 TMX",
                scopes=(
                    TmxExportScopeChoice("project", "整个项目"),
                    TmxExportScopeChoice("chunk:c-7", "分工 7"),
                ),
                source_locale="und",
                target_locale="und",
                prepare=prepare,
                publish=publish,
            )
            self.assertEqual(dialog.source_locale.placeholderText(), "默认 en")
            self.assertEqual(dialog.target_locale.placeholderText(), "默认 zh-CN")
            dialog.scope_combo.setCurrentIndex(1)
            dialog.destination.setText(str(destination))
            dialog._start_preview()
            self._finish_worker(dialog)

            self.assertTrue(dialog.export_button.isEnabled())
            self.assertEqual(dialog.badge.text(), "CHUNK · 当前分工")
            self.assertIn("导出 29", dialog.counts.text())
            self.assertEqual(
                calls[0],
                ("prepare", "chunk:c-7", "en", "zh-CN", destination),
            )

            dialog._start_export()
            self._finish_worker(dialog)
            self.assertIs(dialog.receipt, receipt)
            self.assertEqual(calls[-1], ("publish", preparation))

    def test_failure_is_inline_and_never_uses_message_box(self) -> None:
        def fail(*_args: object):
            error = RuntimeError("private body")
            error.code = "TMX.SCOPE.STALE"
            raise error

        with tempfile.TemporaryDirectory() as temporary:
            dialog = TmxExportDialog(
                title="导出 TMX",
                scopes=(TmxExportScopeChoice("project", "整个项目"),),
                source_locale="en",
                target_locale="zh-CN",
                prepare=fail,
                publish=lambda _value: None,
            )
            dialog.destination.setText(str(Path(temporary) / "out.tmx"))
            dialog._start_preview()
            self._finish_worker(dialog)

        self.assertIn("TMX.SCOPE.STALE", dialog.status.text())
        self.assertNotIn("private body", dialog.status.text())
        self.assertFalse(dialog.export_button.isEnabled())

    def test_new_destination_name_gets_tmx_extension_before_preview(self) -> None:
        observed: list[Path] = []

        def prepare(
            _token: str,
            _source: str,
            _target: str,
            destination: Path,
        ) -> TmxExportDialogPreview:
            observed.append(destination)
            return TmxExportDialogPreview(
                domain_preparation=object(),
                badge="PROJECT",
                title="Project",
                binding="project prj-1",
                document_count=1,
                attached_count=1,
                included_count=1,
                excluded_count=0,
                warning_count=0,
                profile_id="localcat-tmx-level1-context-v1",
            )

        with tempfile.TemporaryDirectory() as temporary:
            dialog = TmxExportDialog(
                title="导出项目 TMX",
                scopes=(TmxExportScopeChoice("project", "整个项目"),),
                source_locale="en",
                target_locale="zh-CN",
                prepare=prepare,
                publish=lambda _value: object(),
            )
            destination = Path(temporary) / "新建的项目翻译记忆"
            dialog.destination.setText(str(destination))
            dialog._start_preview()
            self._finish_worker(dialog)

        self.assertEqual(observed, [destination.with_suffix(".tmx")])
        self.assertEqual(dialog.destination.text(), str(destination.with_suffix(".tmx")))

    def test_no_bilingual_units_has_localized_explanation(self) -> None:
        def fail(*_args: object):
            error = RuntimeError("private body")
            error.code = "TMX.NO_INCLUDED_UNITS"
            raise error

        with tempfile.TemporaryDirectory() as temporary:
            dialog = TmxExportDialog(
                title="导出项目 TMX",
                scopes=(TmxExportScopeChoice("project", "整个项目"),),
                source_locale="en",
                target_locale="zh-CN",
                prepare=fail,
                publish=lambda _value: None,
            )
            dialog.destination.setText(str(Path(temporary) / "project"))
            dialog._start_preview()
            self._finish_worker(dialog)

        self.assertIn("没有可导出的双语段落", dialog.status.text())
        self.assertNotIn("TMX.NO_INCLUDED_UNITS", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
