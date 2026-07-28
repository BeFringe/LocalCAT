"""PySide6 language-resource settings dialog for LocalCAT."""

from __future__ import annotations

from dataclasses import replace

from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from editor_contracts import ImportReport, ImportRequest, ResourceConfig, ResourceKind
from editor_controller import EditorController, EditorControllerError


TMX_FILE_FILTER = "TMX files (*.tmx)"
TERMBASE_FILE_FILTER = "Termbase files (*.csv *.xlsx)"


class ImportWorker(QThread):
    """Run one controller import away from the GUI thread."""

    report_ready = Signal(object)

    def __init__(
        self,
        controller: EditorController,
        request: ImportRequest,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.request = request

    def run(self) -> None:
        try:
            report = self.controller.import_resource(self.request)
        except Exception as exc:
            report = ImportReport(errors=(str(exc),))
        self.report_ready.emit(report)


class QtSettingsDialog(QDialog):
    """Manage local TM and termbase configuration through EditorController only."""

    resources_changed = Signal()
    import_completed = Signal(object)

    def __init__(self, controller: EditorController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("settingsDialog")
        self.setWindowTitle("LocalCAT · 语言资源设置")
        self.setMinimumSize(860, 560)
        self.resize(1040, 680)
        self.setModal(True)
        self.import_worker: ImportWorker | None = None
        self._import_busy = False
        self._import_target_kind: ResourceKind | None = None
        self.last_import_report: ImportReport | None = None
        self._build_ui()
        self.refresh_resources()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("settingsHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 22, 28, 22)
        identity = QVBoxLayout()
        title = QLabel("语言资源设置")
        title.setObjectName("settingsTitle")
        subtitle = QLabel("管理本地翻译记忆库与术语表；所有数据保留在此设备。")
        subtitle.setObjectName("settingsSubtitle")
        identity.addWidget(title)
        identity.addWidget(subtitle)
        header_layout.addLayout(identity)
        header_layout.addStretch()
        badge = QLabel("LOCAL · PRIVATE")
        badge.setObjectName("localBadge")
        header_layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        root.addWidget(header)

        content = QWidget()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 20)
        content_layout.setSpacing(16)

        intro_row = QHBoxLayout()
        intro = QVBoxLayout()
        section_title = QLabel("Translation Memory & Termbase")
        section_title.setObjectName("resourceSectionTitle")
        section_hint = QLabel("选择资源的可见性与读写权限，或创建新的本地资源。")
        section_hint.setObjectName("resourceSectionHint")
        storage_hint = QLabel(
            "导入文件会转换并合并到列表所示的内部 JSONL/CSV；这里显示的是 "
            "LocalCAT 实际查询的本地存储路径。"
        )
        storage_hint.setObjectName("resourceStorageHint")
        storage_hint.setWordWrap(True)
        intro.addWidget(section_title)
        intro.addWidget(section_hint)
        intro.addWidget(storage_hint)
        intro_row.addLayout(intro)
        intro_row.addStretch()
        new_button = QPushButton("＋ 新建资源")
        new_button.setObjectName("newResourceButton")
        new_button.clicked.connect(self._prompt_create_resource)
        intro_row.addWidget(new_button)
        content_layout.addLayout(intro_row)

        active_group = QGroupBox("活动资源")
        active_group.setObjectName("activeResourcesGroup")
        active_layout = QVBoxLayout(active_group)
        self.active_table = self._make_table("activeResourcesTable")
        active_layout.addWidget(self.active_table)
        content_layout.addWidget(active_group, 2)

        inactive_group = QGroupBox("非活动资源")
        inactive_group.setObjectName("inactiveResourcesGroup")
        inactive_layout = QVBoxLayout(inactive_group)
        self.inactive_table = self._make_table("inactiveResourcesTable")
        inactive_layout.addWidget(self.inactive_table)
        content_layout.addWidget(inactive_group, 1)

        footer = QHBoxLayout()
        feedback = QVBoxLayout()
        self.status_label = QLabel("资源配置会自动保存。")
        self.status_label.setObjectName("settingsStatus")
        self.import_feedback = QLabel("")
        self.import_feedback.setObjectName("importFeedback")
        self.import_feedback.setWordWrap(True)
        self.import_feedback.hide()
        self.import_progress = QProgressBar()
        self.import_progress.setObjectName("importProgress")
        self.import_progress.setRange(0, 0)
        self.import_progress.setMaximumWidth(180)
        self.import_progress.hide()
        feedback.addWidget(self.status_label)
        feedback.addWidget(self.import_feedback)
        footer.addLayout(feedback, 1)
        footer.addWidget(self.import_progress)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("完成")
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        content_layout.addLayout(footer)
        root.addWidget(content, 1)

        self.setStyleSheet(_SETTINGS_STYLE)

    @staticmethod
    def _make_table(object_name: str) -> QTableWidget:
        table = QTableWidget(0, 8)
        table.setObjectName(object_name)
        table.setHorizontalHeaderLabels(
            ["Active", "Lookup", "Update", "名称", "类型", "本地路径", "导入", ""]
        )
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setMinimumHeight(128)
        header = table.horizontalHeader()
        header.setMinimumSectionSize(54)
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 128)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(6, 154)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(7, 48)
        return table

    def refresh_resources(self) -> None:
        """Render persistent controller state into active and inactive groups."""

        resources = self.controller.list_resources()
        active = tuple(resource for resource in resources if resource.active)
        inactive = tuple(resource for resource in resources if not resource.active)
        self._populate_table(self.active_table, active)
        self._populate_table(self.inactive_table, inactive)
        self.status_label.setText(
            f"{len(active)} 个活动资源 · {len(inactive)} 个非活动资源 · 配置已保存"
        )

    def _populate_table(
        self,
        table: QTableWidget,
        resources: tuple[ResourceConfig, ...],
    ) -> None:
        table.clearContents()
        table.setRowCount(len(resources))
        for row, resource in enumerate(resources):
            for column, field in enumerate(("active", "lookup", "update")):
                checkbox = QCheckBox()
                checkbox.setObjectName(f"{field}_{resource.id}")
                checkbox.setProperty("resource_id", resource.id)
                checkbox.setProperty("resource_field", field)
                checkbox.setChecked(bool(getattr(resource, field)))
                checkbox.setToolTip(f"切换 {field.title()} 状态")
                checkbox.toggled.connect(
                    lambda checked, resource_id=resource.id, state_field=field: self._set_state(
                        resource_id,
                        state_field,
                        checked,
                    )
                )
                holder = QWidget()
                holder_layout = QHBoxLayout(holder)
                holder_layout.setContentsMargins(0, 0, 0, 0)
                holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                holder_layout.addWidget(checkbox)
                table.setCellWidget(row, column, holder)
            table.setItem(row, 3, QTableWidgetItem(resource.name))
            kind_label = (
                "翻译记忆库"
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
                else "术语表"
            )
            table.setItem(row, 4, QTableWidgetItem(kind_label))
            path_item = QTableWidgetItem(str(resource.path))
            path_item.setToolTip(str(resource.path))
            table.setItem(row, 5, path_item)
            import_button = QPushButton(
                "导入 TMX"
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
                else "导入术语表"
            )
            import_button.setObjectName(f"import_{resource.id}")
            import_button.setProperty("resource_id", resource.id)
            import_button.setMinimumWidth(126)
            import_button.clicked.connect(
                lambda _checked=False, configured=resource: self._prompt_import(configured)
            )
            table.setCellWidget(row, 6, import_button)
            more_button = QToolButton()
            more_button.setObjectName(f"more_{resource.id}")
            more_button.setText("⋮")
            more_button.setToolTip(f"{resource.name} 的更多操作")
            more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(more_button)
            delete_action = menu.addAction("删除资源")
            delete_action.setObjectName(f"delete_{resource.id}")
            delete_action.triggered.connect(
                lambda _checked=False, configured=resource: self._confirm_delete_resource(
                    configured
                )
            )
            more_button.setMenu(menu)
            table.setCellWidget(row, 7, more_button)
        table.resizeRowsToContents()

    def _set_state(self, resource_id: str, field: str, checked: bool) -> None:
        try:
            resource = next(
                configured
                for configured in self.controller.list_resources()
                if configured.id == resource_id
            )
            updated = replace(resource, **{field: checked})
            self.controller.update_resource(updated)
        except (StopIteration, EditorControllerError, ValueError) as exc:
            QMessageBox.critical(self, "无法更新资源", str(exc))
            self.refresh_resources()
            return
        self.resources_changed.emit()
        QTimer.singleShot(0, self.refresh_resources)

    def create_resource(self, name: str, kind: ResourceKind | str) -> ResourceConfig:
        """Create through the controller; exposed for the prompt and GUI tests."""

        resource = self.controller.create_resource(name, kind)
        self.refresh_resources()
        self.resources_changed.emit()
        self.status_label.setText(f"已创建：{resource.name}")
        return resource

    def _confirm_delete_resource(self, resource: ResourceConfig) -> None:
        answer = QMessageBox.question(
            self,
            "删除语言资源",
            (
                f"确定删除“{resource.name}”吗？\n\n"
                "LocalCAT 托管的数据文件会一并删除；仓库默认资源或其他外部"
                "文件只会从列表取消登记。"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete_resource(resource.id)
        except EditorControllerError as exc:
            QMessageBox.critical(self, "无法删除资源", str(exc))
            return
        self.refresh_resources()
        self.resources_changed.emit()
        self.status_label.setText(f"已删除：{resource.name}")

    def _prompt_create_resource(self) -> None:
        prompt = QDialog(self)
        prompt.setWindowTitle("新建语言资源")
        layout = QVBoxLayout(prompt)
        layout.addWidget(QLabel("资源名称"))
        name_input = QLineEdit()
        name_input.setObjectName("newResourceName")
        name_input.setPlaceholderText("例如：客户 A 翻译记忆库")
        layout.addWidget(name_input)
        layout.addWidget(QLabel("资源类型"))
        kind_input = QComboBox()
        kind_input.addItem("翻译记忆库", ResourceKind.TRANSLATION_MEMORY)
        kind_input.addItem("术语表", ResourceKind.TERMBASE)
        layout.addWidget(kind_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(prompt.accept)
        buttons.rejected.connect(prompt.reject)
        layout.addWidget(buttons)
        if prompt.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.create_resource(name_input.text(), kind_input.currentData())
        except EditorControllerError as exc:
            QMessageBox.critical(self, "无法创建资源", str(exc))

    @property
    def is_importing(self) -> bool:
        return self._import_busy

    def _prompt_import(self, resource: ResourceConfig) -> None:
        file_filter = (
            TMX_FILE_FILTER
            if resource.kind is ResourceKind.TRANSLATION_MEMORY
            else TERMBASE_FILE_FILTER
        )
        title = "选择 TMX 文件" if resource.kind is ResourceKind.TRANSLATION_MEMORY else "选择术语表"
        selected, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if not selected:
            return
        source_locale = ""
        target_locale = ""
        if resource.kind is ResourceKind.TRANSLATION_MEMORY:
            try:
                default_source = self.controller.project.source_locale
                default_target = self.controller.project.target_locale
            except EditorControllerError:
                default_source = "en-US"
                default_target = "zh-CN"
            locale_dialog = QDialog(self)
            locale_dialog.setWindowTitle("选择 TMX 语言对")
            locale_layout = QVBoxLayout(locale_dialog)
            locale_layout.addWidget(QLabel("源语言 locale"))
            source_input = QLineEdit(default_source)
            source_input.setObjectName("tmxSourceLocale")
            locale_layout.addWidget(source_input)
            locale_layout.addWidget(QLabel("目标语言 locale"))
            target_input = QLineEdit(default_target)
            target_input.setObjectName("tmxTargetLocale")
            locale_layout.addWidget(target_input)
            locale_buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
            )
            locale_buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始导入")
            locale_buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            locale_buttons.accepted.connect(locale_dialog.accept)
            locale_buttons.rejected.connect(locale_dialog.reject)
            locale_layout.addWidget(locale_buttons)
            if locale_dialog.exec() != QDialog.DialogCode.Accepted:
                return
            source_locale = source_input.text()
            target_locale = target_input.text()
        self.start_import(resource.id, Path(selected), source_locale, target_locale)

    def start_import(
        self,
        resource_id: str,
        input_path: Path,
        source_locale: str = "",
        target_locale: str = "",
    ) -> bool:
        """Start a non-blocking import; return False when another import is active."""

        if self.is_importing:
            self._show_import_feedback("已有导入任务正在运行，请等待完成。", failed=True)
            return False
        try:
            resource = next(
                configured
                for configured in self.controller.list_resources()
                if configured.id == resource_id
            )
        except StopIteration:
            self._show_import_feedback(f"找不到资源：{resource_id}", failed=True)
            return False
        if resource.kind is ResourceKind.TRANSLATION_MEMORY and (
            not source_locale.strip() or not target_locale.strip()
        ):
            self._show_import_feedback("TMX 导入需要源语言和目标语言 locale。", failed=True)
            return False
        try:
            request = ImportRequest(
                resource_id=resource_id,
                input_path=input_path.expanduser().resolve(),
                source_locale=source_locale,
                target_locale=target_locale,
            )
        except (TypeError, ValueError) as exc:
            self._show_import_feedback(str(exc), failed=True)
            return False

        self.last_import_report = None
        self._import_target_kind = resource.kind
        self._import_busy = True
        self._set_import_busy(True, f"正在导入 {resource.name}…")
        worker = ImportWorker(self.controller, request, self)
        self.import_worker = worker
        worker.report_ready.connect(self._on_import_finished)
        worker.finished.connect(lambda current=worker: self._release_worker(current))
        worker.start()
        return True

    def _set_import_busy(self, busy: bool, message: str = "") -> None:
        self.active_table.setEnabled(not busy)
        self.inactive_table.setEnabled(not busy)
        self.findChild(QPushButton, "newResourceButton").setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        self.import_progress.setVisible(busy)
        if message:
            self.status_label.setText(message)

    def _on_import_finished(self, report: ImportReport) -> None:
        self.last_import_report = report
        self._import_busy = False
        self._set_import_busy(False)
        details = (
            f"已导入 {report.imported} · 已跳过 {report.skipped} · "
            f"已覆盖 {report.overwritten} · 错误 {len(report.errors)}"
        )
        if report.imported:
            internal_format = (
                "JSONL"
                if self._import_target_kind is ResourceKind.TRANSLATION_MEMORY
                else "CSV"
            )
            details += (
                f"\n已合并到列表所示的内部 {internal_format} 存储；"
                "导入文件本身不会成为运行时资源路径。"
            )
        if report.errors:
            details += "\n" + "\n".join(report.errors[:4])
        self._show_import_feedback(details, failed=bool(report.errors and not report.imported))
        self.refresh_resources()
        if report.imported:
            self.resources_changed.emit()
        self.import_completed.emit(report)

    def _show_import_feedback(self, message: str, *, failed: bool) -> None:
        self.import_feedback.setText(message)
        self.import_feedback.setProperty("failed", failed)
        self.import_feedback.style().unpolish(self.import_feedback)
        self.import_feedback.style().polish(self.import_feedback)
        self.import_feedback.show()

    def _release_worker(self, worker: ImportWorker) -> None:
        if self.import_worker is worker:
            self.import_worker = None
        worker.deleteLater()

    def reject(self) -> None:
        if self.is_importing:
            self._show_import_feedback("导入完成前无法关闭设置。", failed=True)
            return
        super().reject()


_SETTINGS_STYLE = """
QDialog#settingsDialog {
    background: #f3f6fa;
    color: #182233;
    font-family: "Inter", "Noto Sans CJK SC", sans-serif;
    font-size: 13px;
}
QFrame#settingsHeader {
    background: #082f5b;
    border: none;
}
QLabel#settingsTitle {
    color: #ffffff;
    font-size: 24px;
    font-weight: 700;
}
QLabel#settingsSubtitle {
    color: #b8d2e8;
    font-size: 13px;
}
QLabel#localBadge {
    color: #8eddf0;
    background: #124673;
    border: 1px solid #28638f;
    border-radius: 11px;
    padding: 5px 10px;
    font-size: 10px;
    font-weight: 700;
}
QWidget#settingsContent {
    background: #f3f6fa;
}
QLabel#resourceSectionTitle {
    color: #10243b;
    font-size: 19px;
    font-weight: 700;
}
QLabel#resourceSectionHint, QLabel#settingsStatus {
    color: #66758a;
}
QLabel#importFeedback {
    color: #2f6b50;
    font-size: 12px;
}
QLabel#importFeedback[failed="true"] {
    color: #b34242;
}
QProgressBar {
    border: 1px solid #bfd0df;
    border-radius: 4px;
    background: #edf3f8;
    min-height: 8px;
    max-height: 8px;
}
QProgressBar::chunk {
    background: #08a0c9;
}
QGroupBox {
    background: #ffffff;
    border: 1px solid #d7e0ea;
    border-radius: 9px;
    margin-top: 15px;
    padding-top: 12px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #2a4058;
}
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fc;
    border: none;
    color: #26384b;
}
QTableWidget::item {
    padding: 5px 8px;
}
QHeaderView::section {
    background: #eaf0f6;
    color: #53657a;
    border: none;
    border-bottom: 1px solid #d7e0ea;
    padding: 8px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton {
    min-height: 30px;
    padding: 2px 14px;
    border: 1px solid #c6d2df;
    border-radius: 5px;
    background: #ffffff;
    color: #26435e;
    font-weight: 600;
}
QPushButton:hover {
    border-color: #0798c6;
    color: #047fa8;
}
QPushButton#newResourceButton {
    background: #079fca;
    border-color: #079fca;
    color: #ffffff;
}
QPushButton#newResourceButton:hover {
    background: #078bb2;
}
QLineEdit, QComboBox {
    min-height: 32px;
    border: 1px solid #cbd6e1;
    border-radius: 5px;
    padding: 2px 8px;
    background: #ffffff;
}
"""
