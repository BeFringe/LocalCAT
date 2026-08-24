"""Qt-only presentation for one body-safe TMX export preview.

The dialog owns no TMX, Workspace, Chunk, Resource, or carrier semantics.  It
receives two callables that return/finalize an opaque domain preparation and
keeps all slow work off the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qt_control_styles import configure_combo_popup


_DIALOG_STYLE = """
QDialog { background: #eef6fc; color: #123b5a; }
QLabel#tmxExportTitle { font-size: 25px; font-weight: 800; color: #062f4f; }
QLabel#tmxScopeBadge {
    color: #006f8f; background: #d9f3fb; border: 1px solid #69cfe4;
    border-radius: 13px; padding: 7px 14px; font-weight: 700;
}
QFrame#tmxExportCard {
    background: white; border: 1px solid #bfd8e8; border-radius: 12px;
}
QLabel#tmxBinding { color: #58778d; }
QLabel#tmxStatus { color: #5b778b; padding: 8px; }
QLabel#tmxStatus[failed="true"] { color: #9c4b00; background: #fff2df; }
QLineEdit, QComboBox {
    background: white; border: 1px solid #b6cfdf; border-radius: 7px;
    min-height: 32px; padding: 3px 9px;
}
QPushButton {
    border: 1px solid #aec8da; border-radius: 8px; min-height: 34px;
    padding: 4px 18px; background: white; color: #123b5a; font-weight: 650;
}
QPushButton#tmxPrimary { background: #00a6c8; color: white; border-color: #00a6c8; }
QPushButton:disabled { color: #9aafbd; background: #edf2f5; }
"""

_TMX_ERROR_MESSAGES = {
    "TMX.NO_INCLUDED_UNITS": (
        "当前范围没有可导出的双语段落；空译文不会写入 TMX。"
    ),
    "TMX.DESTINATION_EXTENSION": "目标文件名必须以 .tmx 结尾。",
    "TMX.LOCALE.INVALID": "请填写有效的源语言和目标语言。",
}


@dataclass(frozen=True, slots=True)
class TmxExportScopeChoice:
    token: str
    label: str

    def __post_init__(self) -> None:
        if type(self.token) is not str or not self.token:
            raise ValueError("TMX scope token must be non-empty")
        if type(self.label) is not str or not self.label:
            raise ValueError("TMX scope label must be non-empty")


@dataclass(frozen=True, slots=True)
class TmxExportDialogPreview:
    domain_preparation: object
    badge: str
    title: str
    binding: str
    document_count: int
    attached_count: int
    included_count: int
    excluded_count: int
    warning_count: int
    profile_id: str


class _TmxExportWorker(QThread):
    def __init__(self, operation: Callable[[], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.operation = operation
        self.result: object | None = None
        self.error_message: str | None = None

    def run(self) -> None:
        try:
            self.result = self.operation()
        except Exception as error:
            code = getattr(error, "code", None)
            self.error_message = (
                str(code)
                if type(code) is str and code
                else "TMX.EXPORT.OPERATION_FAILED"
            )


class TmxExportDialog(QDialog):
    """Choose exact scope/locales/destination, preview, then publish once."""

    def __init__(
        self,
        *,
        title: str,
        scopes: tuple[TmxExportScopeChoice, ...],
        source_locale: str,
        target_locale: str,
        prepare: Callable[[str, str, str, Path], TmxExportDialogPreview],
        publish: Callable[[object], object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not scopes or not callable(prepare) or not callable(publish):
            raise ValueError("TMX export dialog requires scopes and operations")
        self._prepare = prepare
        self._publish = publish
        self._preview: TmxExportDialogPreview | None = None
        self._worker: _TmxExportWorker | None = None
        self.receipt: object | None = None
        self.setObjectName("tmxExportDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(850, 620)
        self.setStyleSheet(_DIALOG_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(14)
        header = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("tmxExportTitle")
        header.addWidget(heading)
        header.addStretch()
        self.badge = QLabel("TMX · 待预览")
        self.badge.setObjectName("tmxScopeBadge")
        header.addWidget(self.badge)
        layout.addLayout(header)

        controls = QGridLayout()
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(10)
        controls.addWidget(QLabel("导出范围"), 0, 0)
        self.scope_combo = QComboBox()
        self.scope_combo.setObjectName("tmxExportScope")
        for scope in scopes:
            self.scope_combo.addItem(scope.label, scope.token)
        configure_combo_popup(
            self.scope_combo,
            object_name="tmxExportScopePopup",
            accessible_name="TMX 导出范围",
        )
        controls.addWidget(self.scope_combo, 0, 1, 1, 3)

        controls.addWidget(QLabel("源语言"), 1, 0)
        self.source_locale = QLineEdit()
        self.source_locale.setObjectName("tmxSourceLocale")
        self.source_locale.setText("" if source_locale.casefold() == "und" else source_locale)
        self.source_locale.setPlaceholderText("默认 en")
        controls.addWidget(self.source_locale, 1, 1)
        controls.addWidget(QLabel("目标语言"), 1, 2)
        self.target_locale = QLineEdit()
        self.target_locale.setObjectName("tmxTargetLocale")
        self.target_locale.setText("" if target_locale.casefold() == "und" else target_locale)
        self.target_locale.setPlaceholderText("默认 zh-CN")
        controls.addWidget(self.target_locale, 1, 3)

        controls.addWidget(QLabel("目标文件"), 2, 0)
        self.destination = QLineEdit()
        self.destination.setObjectName("tmxDestination")
        controls.addWidget(self.destination, 2, 1, 1, 2)
        browse = QPushButton("选择")
        browse.setObjectName("tmxChooseDestination")
        browse.clicked.connect(self._choose_destination)
        controls.addWidget(browse, 2, 3)
        self.preview_button = QPushButton("生成预览")
        self.preview_button.setObjectName("tmxPreview")
        self.preview_button.clicked.connect(self._start_preview)
        controls.addWidget(self.preview_button, 3, 3)
        layout.addLayout(controls)

        self.card = QFrame()
        self.card.setObjectName("tmxExportCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(22, 18, 22, 18)
        card_layout.setSpacing(10)
        self.scope_title = QLabel("选择范围并生成预览")
        self.scope_title.setStyleSheet("font-size: 19px; font-weight: 750;")
        card_layout.addWidget(self.scope_title)
        self.binding = QLabel("预览不会修改项目、分工、资源或目标文件。")
        self.binding.setObjectName("tmxBinding")
        self.binding.setWordWrap(True)
        card_layout.addWidget(self.binding)
        self.counts = QLabel("文档 —  ·  Attached —  ·  导出 —  ·  排除 —  ·  警告 —")
        self.counts.setWordWrap(True)
        card_layout.addWidget(self.counts)
        self.profile = QLabel("Profile：localcat-tmx-level1-context-v1")
        card_layout.addWidget(self.profile)
        layout.addWidget(self.card, 1)

        self.status = QLabel("先生成预览，再导出。")
        self.status.setObjectName("tmxStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.setObjectName("tmxCancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.export_button = QPushButton("导出")
        self.export_button.setObjectName("tmxPrimary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._start_export)
        buttons.addWidget(self.export_button)
        layout.addLayout(buttons)

        self.scope_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.source_locale.textChanged.connect(self._invalidate_preview)
        self.target_locale.textChanged.connect(self._invalidate_preview)
        self.destination.textChanged.connect(self._invalidate_preview)

    def _effective_locales(self) -> tuple[str, str]:
        return (
            self.source_locale.text().strip() or "en",
            self.target_locale.text().strip() or "zh-CN",
        )

    def _choose_destination(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "导出 TMX",
            self.destination.text(),
            "TMX (*.tmx)",
        )
        if selected:
            path = Path(selected)
            if path.suffix.casefold() != ".tmx":
                path = path.with_name(f"{path.name}.tmx")
            self.destination.setText(str(path))

    def _invalidate_preview(self, *_args: object) -> None:
        self._preview = None
        self.export_button.setEnabled(False)
        if self._worker is None:
            self.status.setText("范围、语言或目标已变化，请重新生成预览。")

    def _set_busy(self, busy: bool) -> None:
        self.scope_combo.setEnabled(not busy)
        self.source_locale.setEnabled(not busy)
        self.target_locale.setEnabled(not busy)
        self.destination.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and self._preview is not None)

    def _set_error(self, message: str) -> None:
        self.status.setProperty("failed", True)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(
            f"未完成：{_TMX_ERROR_MESSAGES.get(message, message)}"
        )

    def _start_preview(self) -> None:
        raw_destination = self.destination.text().strip()
        if not raw_destination:
            self._set_error("请选择目标 TMX 文件")
            return
        destination = Path(raw_destination).expanduser()
        if destination.suffix.casefold() != ".tmx":
            destination = destination.with_name(f"{destination.name}.tmx")
            self.destination.setText(str(destination))
        if not destination.is_absolute():
            destination = destination.absolute()
        source, target = self._effective_locales()
        token = str(self.scope_combo.currentData())
        self._set_busy(True)
        self.status.setProperty("failed", False)
        self.status.setText("正在生成绑定范围与目标的预览…")
        worker = _TmxExportWorker(
            lambda: self._prepare(token, source, target, destination),
            self,
        )
        self._worker = worker
        worker.finished.connect(self._preview_finished)
        worker.start()

    def _preview_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is None or worker.error_message is not None:
            self._set_error(
                "TMX.PREVIEW.FAILED" if worker is None else worker.error_message or "TMX.PREVIEW.FAILED"
            )
            return
        preview = worker.result
        if type(preview) is not TmxExportDialogPreview:
            self._set_error("TMX.PREVIEW.RESULT_INVALID")
            return
        self._preview = preview
        self.badge.setText(preview.badge)
        self.scope_title.setText(preview.title)
        self.binding.setText(preview.binding)
        self.counts.setText(
            f"文档 {preview.document_count}  ·  Attached {preview.attached_count}  ·  "
            f"导出 {preview.included_count}  ·  排除 {preview.excluded_count}  ·  "
            f"警告 {preview.warning_count}"
        )
        self.profile.setText(f"Profile：{preview.profile_id}")
        self.status.setProperty("failed", False)
        self.status.setText("预览已绑定；导出前会再次验证范围与目标。")
        self.export_button.setEnabled(True)

    def _start_export(self) -> None:
        preview = self._preview
        if preview is None:
            return
        self._set_busy(True)
        self.status.setText("正在写入、冷验证并发布 TMX…")
        worker = _TmxExportWorker(
            lambda: self._publish(preview.domain_preparation),
            self,
        )
        self._worker = worker
        worker.finished.connect(self._export_finished)
        worker.start()

    def _export_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is None or worker.error_message is not None:
            self._set_error(
                "TMX.EXPORT.FAILED" if worker is None else worker.error_message or "TMX.EXPORT.FAILED"
            )
            self._preview = None
            self.export_button.setEnabled(False)
            return
        self.receipt = worker.result
        self.accept()


__all__ = [
    "TmxExportDialog",
    "TmxExportDialogPreview",
    "TmxExportScopeChoice",
]
