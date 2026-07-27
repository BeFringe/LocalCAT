"""PySide6 language-resource settings dialog for LocalCAT."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from editor_contracts import ResourceConfig, ResourceKind
from editor_controller import EditorController, EditorControllerError


class QtSettingsDialog(QDialog):
    """Manage local TM and termbase configuration through EditorController only."""

    resources_changed = Signal()

    def __init__(self, controller: EditorController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("settingsDialog")
        self.setWindowTitle("LocalCAT · 语言资源设置")
        self.setMinimumSize(860, 560)
        self.resize(1040, 680)
        self.setModal(True)
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
        intro.addWidget(section_title)
        intro.addWidget(section_hint)
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
        self.status_label = QLabel("资源配置会自动保存。")
        self.status_label.setObjectName("settingsStatus")
        footer.addWidget(self.status_label)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("完成")
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        content_layout.addLayout(footer)
        root.addWidget(content, 1)

        self.setStyleSheet(_SETTINGS_STYLE)

    @staticmethod
    def _make_table(object_name: str) -> QTableWidget:
        table = QTableWidget(0, 6)
        table.setObjectName(object_name)
        table.setHorizontalHeaderLabels(["Active", "Lookup", "Update", "名称", "类型", "本地路径"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setMinimumHeight(128)
        header = table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
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

    def create_resource(self, name: str, kind: ResourceKind) -> ResourceConfig:
        """Create through the controller; exposed for the prompt and GUI tests."""

        resource = self.controller.create_resource(name, kind)
        self.refresh_resources()
        self.resources_changed.emit()
        self.status_label.setText(f"已创建：{resource.name}")
        return resource

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
