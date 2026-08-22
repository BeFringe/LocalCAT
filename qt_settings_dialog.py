"""PySide6 language-resource settings dialog for LocalCAT."""

from __future__ import annotations

from dataclasses import replace

from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPointF,
    QPersistentModelIndex,
    QThread,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QKeyEvent,
    QPaintEvent,
    QPainter,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid as _qt_object_is_valid

from editor_contracts import (
    FuzzyValidationState,
    ImportReport,
    ImportRequest,
    ResourceConfig,
    ResourceKind,
    TermbaseImportHeaderMode,
    TermbaseImportPreview,
    TermbaseImportSelection,
    TMActivationOperationView,
    TMResourceDisplayMode,
    TMResourceStatus,
)
from editor_controller import EditorController, EditorControllerError
from qt_termbase_dialog import QtTermbaseDialog
from qt_control_styles import configure_combo_popup, configure_menu
from qt_localized_message_box import show_localized_critical
from qt_tm_threshold import (
    TMThresholdButton,
    configure_tm_threshold_entry,
    prompt_tm_threshold,
    tm_threshold_feedback,
)


TMX_FILE_FILTER = "TMX files (*.tmx)"
TERMBASE_FILE_FILTER = "Termbase files (*.csv *.xlsx)"
DEFAULT_VISIBLE_RESOURCE_ROWS = 3
_EMPTY_RESOURCE_TABLE_BODY_HEIGHT = 36
_RESOURCE_MORE_BUTTON_STYLE = """
QToolButton {
    border: none;
    background: transparent;
    color: #26435e;
    padding: 0;
    margin: 0;
}
QToolButton:hover,
QToolButton:focus {
    border: none;
    background: transparent;
    color: #0798c6;
}
QToolButton:pressed {
    border: none;
    background: transparent;
    color: #047fa8;
}
QToolButton::menu-indicator {
    image: none;
    width: 0;
    height: 0;
}
"""
_RESOURCE_KIND_COMBO_STYLE = """
QComboBox#newResourceKind {
    color: #1f3850;
    background-color: #ffffff;
}
"""
_TM_MODE_LABELS = {
    TMResourceDisplayMode.LEGACY_EXACT_ONLY: "Legacy exact-only",
    TMResourceDisplayMode.ACTIVATING: "激活中",
    TMResourceDisplayMode.CANONICAL_ACTIVE: "Canonical active",
    TMResourceDisplayMode.SOURCE_DIVERGED: "Source diverged",
    TMResourceDisplayMode.DEGRADED: "Degraded",
    TMResourceDisplayMode.UNAVAILABLE: "Unavailable",
}
_TM_SAFE_REASON_LABELS = {
    "TM.RUNTIME.PATH_UNAVAILABLE": "本地资源路径不可用",
    "TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE": "Canonical 权威无法验证",
    "TM.RUNTIME.OPEN_UNAVAILABLE": "资源无法安全打开",
    "TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE": "来源绑定无法验证",
    "TM.RUNTIME.QUERY_LEASE_UNAVAILABLE": "Canonical 查询不可用",
    "TM.RUNTIME.CANONICAL_HEALTH_UNAVAILABLE": "Canonical 健康状态不可用",
    "TM.RUNTIME.SOURCE_DIVERGED": "外部来源已变更",
    "TM.RUNTIME.REFRESH_FAILED": "运行时刷新失败",
    "TM.ACTIVATION.RUNTIME_REFRESH_FAILED": "激活后运行时验证失败",
    "TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED": "Canonical 文件身份需重新验证",
    "TM.RUNTIME.CANONICAL_REATTESTATION_NOT_APPLICABLE": "Canonical 文件身份不满足重新验证条件",
    "TM.RUNTIME.CANONICAL_REATTESTATION_IDENTITY_INVALID": "Canonical 资源身份无法验证",
    "TM.LEGACY.QUERY_FAILED": "Legacy 查询失败",
    "TM.ACTIVATION.IO_FAILED": "本地读写失败",
    "TM.ACTIVATION.PROGRAMMER_ERROR": "Canonical 操作未能安全完成",
    "MIGRATION.INITIAL_IO_FAILED": "首次激活未完成",
    "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE": "Canonical 权威无法确定",
    "MIGRATION.RESOURCE_ID_INVALID": "资源身份无法验证",
    "MIGRATION.RESOURCE_IDENTITY_MISMATCH": "资源身份无法验证",
    "MIGRATION.COORDINATOR_IDENTITY_MISMATCH": "Canonical 权威无法验证",
    "MIGRATION.COORDINATOR_UNAVAILABLE": "Canonical 权威无法验证",
    "MIGRATION.ALREADY_ACTIVE": "资源已处于 canonical active",
    "MIGRATION.ACTIVATION_NOT_READY": "Canonical 激活前置未就绪",
    "MIGRATION.SOURCE_UNREADABLE": "本地来源不可读",
    "MIGRATION.SOURCE_CHANGED": "本地来源已变更",
    "RETRIEVAL.CONTEXT_EVIDENCE_MISSING": "Context 尚未开放",
    "RETRIEVAL.CONTEXT_IDENTITY_INVALID": "Context 证据身份无法验证",
    "RETRIEVAL.CONTEXT_EVIDENCE_FAILED": "Context 正确性证据未通过",
    "RETRIEVAL.CONTEXT_EVIDENCE_EXPIRED": "Context 证据已过期",
    "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING": "Fuzzy 正确性尚未开放",
    "RETRIEVAL.FUZZY_CORRECTNESS_IDENTITY_INVALID": "Fuzzy 正确性证据身份无法验证",
    "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_FAILED": "Fuzzy 正确性证据未通过",
    "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_EXPIRED": "Fuzzy 正确性证据已过期",
    "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING": "Fuzzy 性能尚未开放",
    "RETRIEVAL.FUZZY_BENCHMARK_IDENTITY_INVALID": "Fuzzy 性能证据身份无法验证",
    "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_FAILED": "Fuzzy 性能证据未通过",
    "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_EXPIRED": "Fuzzy 性能证据已过期",
}
_TM_ACTION_EXCEPTION_SAFE_CODES = frozenset(_TM_SAFE_REASON_LABELS)
_TM_KIND_LEGACY_COLOR = "#d59a00"
_TM_KIND_CANONICAL_COLOR = "#2f9e44"
_TM_KIND_UNAVAILABLE_COLOR = "#8291a1"


def _tm_safe_reason(code: str | None) -> str:
    if code is None:
        return "内部状态无法安全确认"
    return _TM_SAFE_REASON_LABELS.get(
        code,
        f"状态信息不可用（{code}）",
    )


def _tm_status_safe_reason(status: TMResourceStatus, code: str) -> str:
    """Explain aggregate evidence without contradicting an open gate."""

    if (
        code == "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_FAILED"
        and status.fuzzy_available
    ):
        return "另一 Fuzzy 路径性能证据未通过（当前 Fuzzy 可用）"
    return _tm_safe_reason(code)


def _ask_localized_question(
    parent: QWidget,
    title: str,
    message: str,
    buttons: QMessageBox.StandardButton,
    default_button: QMessageBox.StandardButton,
    button_labels: dict[QMessageBox.StandardButton, str],
) -> QMessageBox.StandardButton:
    """Show one native question box with explicit LocalCAT button text."""

    prompt = QMessageBox(
        QMessageBox.Icon.Question,
        title,
        message,
        buttons,
        parent,
    )
    prompt.setDefaultButton(default_button)
    for standard_button, label in button_labels.items():
        button = prompt.button(standard_button)
        if button is not None:
            button.setText(label)
    return QMessageBox.StandardButton(prompt.exec())


class _ResourceMoreButton(QToolButton):
    """Keep the resource menu reachable with standard keyboard activation."""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        color = QColor("#26435e")
        if self.isDown():
            color = QColor("#047fa8")
        elif self.underMouse() or self.hasFocus():
            color = QColor("#0798c6")
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            center_x = self.width() / 2.0
            center_y = self.height() / 2.0
            for offset in (-5.0, 0.0, 5.0):
                painter.drawEllipse(
                    QPointF(center_x, center_y + offset),
                    1.5,
                    1.5,
                )
        finally:
            painter.end()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.menu() is not None:
            event.accept()
            self.showMenu()
            return
        super().keyPressEvent(event)


class _FullCellWidgetDelegate(QStyledItemDelegate):
    """Keep action widgets flush instead of applying text-item padding."""

    def updateEditorGeometry(
        self,
        editor: QWidget,
        option: QStyleOptionViewItem,
        _index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        editor.setGeometry(option.rect)


class _ResourceTable(QTableWidget):
    """Hand wheel input to the outer resource scroller at inner boundaries."""

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        self._wheel_handoff_target: QScrollArea | None = None

    def set_wheel_handoff_target(self, target: QScrollArea) -> None:
        self._wheel_handoff_target = target

    def wheelEvent(self, event: QWheelEvent) -> None:
        pixel_delta = event.pixelDelta().y()
        scrollbar = self.verticalScrollBar()
        target = self._wheel_handoff_target
        if pixel_delta:
            requested = -pixel_delta
            inner_before = scrollbar.value()
            inner_after = min(
                scrollbar.maximum(),
                max(scrollbar.minimum(), inner_before + requested),
            )
            scrollbar.setValue(inner_after)
            remaining = requested - (inner_after - inner_before)
            if remaining and target is not None:
                target_scrollbar = target.verticalScrollBar()
                target_scrollbar.setValue(target_scrollbar.value() + remaining)
            event.accept()
            return

        vertical_delta = event.angleDelta().y()
        at_boundary = (
            vertical_delta < 0 and scrollbar.value() >= scrollbar.maximum()
        ) or (
            vertical_delta > 0 and scrollbar.value() <= scrollbar.minimum()
        )
        if vertical_delta == 0 or not at_boundary or target is None:
            super().wheelEvent(event)
            return

        target_position = target.viewport().mapFromGlobal(
            event.globalPosition().toPoint()
        )
        forwarded = QWheelEvent(
            QPointF(target_position),
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
        )
        QApplication.sendEvent(target.viewport(), forwarded)
        event.accept()


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


class PreviewWorker(QThread):
    """Build one codec-owned termbase preview away from the GUI thread."""

    def __init__(
        self,
        controller: EditorController,
        input_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.input_path = input_path
        self.preview: TermbaseImportPreview | None = None
        self.error_message: str | None = None

    def run(self) -> None:
        try:
            preview = self.controller.preview_termbase_import(self.input_path)
            if type(preview) is not TermbaseImportPreview:
                raise TypeError("术语表列预览返回了无效结果。")
            self.preview = preview
        except EditorControllerError as exc:
            self.error_message = str(exc)
        except Exception:
            self.error_message = "术语表列预览未能安全完成。"


class TermbaseColumnSelectionDialog(QDialog):
    """Select two physical columns from one Controller-issued preview."""

    def __init__(
        self,
        preview: TermbaseImportPreview,
        parent: QWidget | None = None,
    ) -> None:
        if type(preview) is not TermbaseImportPreview:
            raise TypeError("termbase column prompt requires TermbaseImportPreview")
        super().__init__(parent)
        self.preview = preview
        self.setObjectName("termbaseColumnSelectionDialog")
        self.setWindowTitle("选择术语表列")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        scope_parts = [f"格式：{preview.format_name}"]
        if preview.active_sheet_name:
            scope_parts.append(f"活动工作表：{preview.active_sheet_name}")
        scope = QLabel(" · ".join(scope_parts))
        scope.setObjectName("termbasePreviewScope")
        scope.setWordWrap(True)
        layout.addWidget(scope)

        if preview.columns_truncated:
            truncation = QLabel(
                f"文件共有 {preview.total_column_count} 列；"
                f"当前安全预览显示前 {len(preview.columns)} 列。"
            )
            truncation.setObjectName("termbasePreviewTruncation")
            truncation.setWordWrap(True)
            layout.addWidget(truncation)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("原文列"))
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("termbaseSourceColumn")
        self.source_combo.setAccessibleName("术语表原文列")
        source_row.addWidget(self.source_combo, 1)
        layout.addLayout(source_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("译文列"))
        self.target_combo = QComboBox()
        self.target_combo.setObjectName("termbaseTargetColumn")
        self.target_combo.setAccessibleName("术语表译文列")
        target_row.addWidget(self.target_combo, 1)
        layout.addLayout(target_row)

        for column in preview.columns:
            label = self._column_label(column)
            self.source_combo.addItem(label, column.zero_based_index)
            self.target_combo.addItem(label, column.zero_based_index)
        if len(preview.columns) > 1:
            self.target_combo.setCurrentIndex(1)

        configure_combo_popup(
            self.source_combo,
            object_name="termbaseSourceColumnPopup",
            accessible_name="术语表原文列选项",
        )
        configure_combo_popup(
            self.target_combo,
            object_name="termbaseTargetColumnPopup",
            accessible_name="术语表译文列选项",
        )

        self.header_checkbox = QCheckBox("首行是表头")
        self.header_checkbox.setObjectName("termbaseFirstRowHeader")
        self.header_checkbox.setAccessibleName("首行是表头")
        self.header_checkbox.setChecked(preview.legacy_header_detected)
        layout.addWidget(self.header_checkbox)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.setObjectName("termbaseColumnButtons")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始导入")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.source_combo.currentIndexChanged.connect(self._update_accept_enabled)
        self.target_combo.currentIndexChanged.connect(self._update_accept_enabled)
        self._update_accept_enabled()

    @staticmethod
    def _column_label(column) -> str:
        candidate = column.header_candidate
        if candidate is None or not candidate:
            display = "（空）"
        else:
            display = candidate.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            if column.header_truncated:
                display += "…"
        return f"第 {column.zero_based_index + 1} 列 · {display}"

    def _update_accept_enabled(self, _index: int = -1) -> None:
        source_index = self.source_combo.currentData()
        target_index = self.target_combo.currentData()
        enabled = (
            type(source_index) is int
            and type(target_index) is int
            and source_index != target_index
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)

    def selection(self) -> TermbaseImportSelection:
        """Build the immutable selection consumed by the existing import command."""

        return TermbaseImportSelection(
            source_zero_based_index=self.source_combo.currentData(),
            target_zero_based_index=self.target_combo.currentData(),
            header_mode=(
                TermbaseImportHeaderMode.FIRST_ROW
                if self.header_checkbox.isChecked()
                else TermbaseImportHeaderMode.NO_HEADER
            ),
            preview_column_count=len(self.preview.columns),
            preview_source_identity=self.preview.source_identity,
        )


class QtSettingsDialog(QDialog):
    """Manage local TM and termbase configuration through EditorController only."""

    resources_changed = Signal()
    import_completed = Signal(object)
    tm_threshold_changed = Signal(object)
    fuzzy_validation_changed = Signal(object)
    term_suggestions_changed = Signal()

    def __init__(self, controller: EditorController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.new_resource_button: QPushButton
        self.tm_threshold_chip: QPushButton
        self.tm_threshold_state: QLabel
        self._resource_row_resize_pending = False
        self._resource_row_layout_signature: tuple[object, ...] | None = None
        self.controller = controller
        self.setObjectName("settingsDialog")
        self.setWindowTitle("LocalCAT · 语言资源设置")
        self.setMinimumSize(860, 560)
        self.resize(1040, 680)
        self.setModal(True)
        self.import_worker: ImportWorker | None = None
        self.preview_worker: PreviewWorker | None = None
        self._import_busy = False
        self._import_target_kind: ResourceKind | None = None
        self.last_import_report: ImportReport | None = None
        self._tm_operation_id: str | None = None
        self._tm_operation_action: str | None = None
        self._tm_operation_timer = QTimer(self)
        self._tm_operation_timer.setInterval(75)
        self._tm_operation_timer.timeout.connect(self._poll_tm_operation)
        self._build_ui()
        self.setTabOrder(self.new_resource_button, self.tm_threshold_chip)
        self.refresh_resources()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_resource_row_resize()

    def _schedule_resource_row_resize(self) -> None:
        if (
            self._resource_row_resize_pending
            or not hasattr(self, "active_table")
        ):
            return
        self._resource_row_resize_pending = True
        QTimer.singleShot(0, self._resize_resource_rows)

    def _resize_resource_rows(self) -> None:
        self._resource_row_resize_pending = False
        if (
            not _qt_object_is_valid(self)
            or not _qt_object_is_valid(self.active_table)
            or not _qt_object_is_valid(self.inactive_table)
        ):
            return
        signature_parts: list[object] = []
        for table in (self.active_table, self.inactive_table):
            row_count = table.rowCount()
            table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            table.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                if row_count <= DEFAULT_VISIBLE_RESOURCE_ROWS
                else Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            table.resizeRowsToContents()
            for row in range(row_count):
                name_cell = table.cellWidget(row, 3)
                if (
                    name_cell is None
                    or not name_cell.objectName().startswith("tmResource_")
                ):
                    continue
                required_height = table.verticalHeader().minimumSectionSize()
                for column in range(table.columnCount()):
                    cell = table.cellWidget(row, column)
                    if cell is None:
                        continue
                    layout = cell.layout()
                    if layout is not None and layout.hasHeightForWidth():
                        cell_height = layout.totalHeightForWidth(max(1, cell.width()))
                    else:
                        cell_height = max(
                            cell.minimumSizeHint().height(),
                            cell.sizeHint().height(),
                        )
                    required_height = max(required_height, cell_height)
                table.setRowHeight(row, required_height)
            visible_count = min(row_count, DEFAULT_VISIBLE_RESOURCE_ROWS)
            if visible_count:
                body_height = sum(
                    sorted(
                        (table.rowHeight(row) for row in range(row_count)),
                        reverse=True,
                    )[:visible_count]
                )
            else:
                body_height = _EMPTY_RESOURCE_TABLE_BODY_HEIGHT
            table_height = (
                table.horizontalHeader().height()
                + body_height
                + table.frameWidth() * 2
            )
            table.setFixedHeight(table_height)
            group = table.parentWidget()
            if group is not None:
                group.setMinimumHeight(0)
                group.setMaximumHeight(16_777_215)
                group.setFixedHeight(group.sizeHint().height())
            signature_parts.extend(
                (
                    table.width(),
                    table.viewport().width(),
                    table_height,
                    tuple(table.rowHeight(row) for row in range(row_count)),
                )
            )
        resource_tables_layout = self.resource_tables_content.layout()
        if resource_tables_layout is None:
            raise AssertionError("resource tables content layout is required")
        self.resource_tables_content.setMinimumHeight(0)
        resource_tables_layout.invalidate()
        self.resource_tables_content.setMinimumHeight(
            self.resource_tables_content.sizeHint().height()
        )
        resource_tables_layout.activate()
        self.resource_tables_content.updateGeometry()
        signature = tuple(signature_parts)
        if signature != self._resource_row_layout_signature:
            self._resource_row_layout_signature = signature
            self._schedule_resource_row_resize()

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
            "导入文件会转换并合并至内部 JSONL/CSV。"
            "列表中的路径就是 LocalCAT 实际查询的本地存储位置。"
        )
        storage_hint.setObjectName("resourceStorageHint")
        storage_hint.setWordWrap(True)
        intro.addWidget(section_title)
        intro.addWidget(section_hint)
        intro.addWidget(storage_hint)
        intro_row.addLayout(intro, 1)
        intro_row.addSpacing(24)
        new_button = QPushButton("＋ 新建资源")
        new_button.setObjectName("newResourceButton")
        new_button.clicked.connect(self._prompt_create_resource)
        self.new_resource_button = new_button
        intro_row.addWidget(new_button)
        content_layout.addLayout(intro_row)

        threshold_panel = QFrame()
        threshold_panel.setObjectName("settingsTmThresholdPanel")
        threshold_layout = QHBoxLayout(threshold_panel)
        threshold_layout.setContentsMargins(12, 8, 12, 8)
        threshold_layout.setSpacing(10)
        threshold_title = QLabel("Fuzzy 建议阈值")
        threshold_title.setObjectName("settingsTmThresholdTitle")
        threshold_layout.addWidget(threshold_title)
        self.tm_threshold_state = QLabel()
        self.tm_threshold_state.setObjectName("settingsTmThresholdState")
        self.tm_threshold_state.setWordWrap(True)
        threshold_layout.addWidget(self.tm_threshold_state, 1)
        self.tm_threshold_chip = TMThresholdButton()
        self.tm_threshold_chip.setObjectName("settingsTmThresholdChip")
        self.tm_threshold_chip.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tm_threshold_chip.clicked.connect(self._request_tm_threshold_update)
        threshold_layout.addWidget(self.tm_threshold_chip)
        self.fuzzy_revalidate_button = QPushButton("重新验证 Fuzzy")
        self.fuzzy_revalidate_button.setObjectName("fuzzyRevalidateButton")
        self.fuzzy_revalidate_button.setAccessibleName(
            "重新运行本机 Fuzzy 性能资格验证"
        )
        self.fuzzy_revalidate_button.setToolTip(
            "显式运行完整性能验证；验证期间 Exact 与 Context 保持可用"
        )
        self.fuzzy_revalidate_button.clicked.connect(
            self._request_fuzzy_revalidation
        )
        threshold_layout.addWidget(self.fuzzy_revalidate_button)
        content_layout.addWidget(threshold_panel)

        self.resource_tables_scroll = QScrollArea()
        self.resource_tables_scroll.setObjectName("resourceTablesScroll")
        self.resource_tables_scroll.setWidgetResizable(True)
        self.resource_tables_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.resource_tables_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.resource_tables_content = QWidget()
        self.resource_tables_content.setObjectName("resourceTablesContent")
        resource_tables_layout = QVBoxLayout(self.resource_tables_content)
        resource_tables_layout.setContentsMargins(0, 0, 0, 0)
        resource_tables_layout.setSpacing(16)

        active_group = QGroupBox("活动资源")
        active_group.setObjectName("activeResourcesGroup")
        active_group_policy = active_group.sizePolicy()
        active_group_policy.setVerticalPolicy(QSizePolicy.Policy.Fixed)
        active_group.setSizePolicy(active_group_policy)
        active_layout = QVBoxLayout(active_group)
        self.active_table = self._make_table("activeResourcesTable")
        active_layout.addWidget(self.active_table)
        resource_tables_layout.addWidget(active_group)

        inactive_group = QGroupBox("非活动资源")
        inactive_group.setObjectName("inactiveResourcesGroup")
        inactive_group_policy = inactive_group.sizePolicy()
        inactive_group_policy.setVerticalPolicy(QSizePolicy.Policy.Fixed)
        inactive_group.setSizePolicy(inactive_group_policy)
        inactive_layout = QVBoxLayout(inactive_group)
        self.inactive_table = self._make_table("inactiveResourcesTable")
        inactive_layout.addWidget(self.inactive_table)
        self.active_table.set_wheel_handoff_target(self.resource_tables_scroll)
        self.inactive_table.set_wheel_handoff_target(self.resource_tables_scroll)
        resource_tables_layout.addWidget(inactive_group)
        resource_tables_layout.addStretch()
        self.resource_tables_scroll.setWidget(self.resource_tables_content)
        content_layout.addWidget(self.resource_tables_scroll, 1)

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
    def _make_table(object_name: str) -> _ResourceTable:
        table = _ResourceTable(0, 8)
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
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = table.horizontalHeader()
        header.setMinimumSectionSize(32)
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 234)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(6, 128)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(7, 32)
        full_cell_delegate = _FullCellWidgetDelegate(table)
        table.setItemDelegateForColumn(3, full_cell_delegate)
        table.setItemDelegateForColumn(4, full_cell_delegate)
        table.setItemDelegateForColumn(6, full_cell_delegate)
        table.setItemDelegateForColumn(7, full_cell_delegate)
        return table

    def refresh_resources(self) -> None:
        """Render persistent controller state into active and inactive groups."""

        resources = self.controller.list_resources()
        try:
            statuses = self.controller.tm_resource_statuses()
        except Exception:
            statuses = ()
        status_by_resource_id = {
            status.resource_id: status
            for status in statuses
            if type(status) is TMResourceStatus
        }
        self._refresh_tm_threshold_entry()
        try:
            operation = self.controller.tm_activation_operation()
        except Exception:
            operation = None
        if operation is not None and not operation.completed:
            self._tm_operation_id = operation.operation_id
            if self._tm_operation_action is None:
                self._tm_operation_action = "Canonical 操作"
            if not self._tm_operation_timer.isActive():
                self._tm_operation_timer.start()
        active = tuple(resource for resource in resources if resource.active)
        inactive = tuple(resource for resource in resources if not resource.active)
        self._populate_table(
            self.active_table,
            active,
            status_by_resource_id=status_by_resource_id,
            operation=operation,
        )
        self._populate_table(
            self.inactive_table,
            inactive,
            status_by_resource_id=status_by_resource_id,
            operation=operation,
        )
        self._refresh_resource_menu_tab_order(resources)
        self._schedule_resource_row_resize()
        if operation is not None and not operation.completed:
            self.status_label.setText("Canonical 操作正在进行；重复操作已禁用。")
        else:
            self.status_label.setText(
                f"{len(active)} 个活动资源 · {len(inactive)} 个非活动资源 · 配置已保存"
            )

    def _refresh_resource_menu_tab_order(
        self,
        resources: tuple[ResourceConfig, ...],
    ) -> None:
        """Keep each resource menu reachable outside table cell navigation."""

        menus: list[QToolButton] = []
        for resource in resources:
            more = self.findChild(QToolButton, f"more_{resource.id}")
            if more is not None and more.isEnabled():
                menus.append(more)
        if not menus:
            return
        previous: QWidget = self.tm_threshold_chip
        for more in menus:
            self.setTabOrder(previous, more)
            previous = more
        self.setTabOrder(previous, self.active_table)

    def _refresh_tm_threshold_entry(self) -> None:
        """Render the settings entry from fresh defensive Controller values."""

        retrieval_status = self.controller.tm_retrieval_status()
        fuzzy_validation = self.controller.tm_fuzzy_validation_status()
        configure_tm_threshold_entry(
            self.tm_threshold_chip,
            self.tm_threshold_state,
            preferences=self.controller.tm_preferences(),
            retrieval_status=retrieval_status,
            fuzzy_validation=fuzzy_validation,
        )
        needs_revalidation = not retrieval_status.fuzzy_available
        self.fuzzy_revalidate_button.setVisible(needs_revalidation)
        running = fuzzy_validation.state is FuzzyValidationState.RUNNING
        self.fuzzy_revalidate_button.setEnabled(
            needs_revalidation and not running
        )
        self.fuzzy_revalidate_button.setText(
            "Fuzzy 验证中…" if running else "重新验证 Fuzzy"
        )

    def _request_fuzzy_revalidation(self) -> None:
        try:
            status = self.controller.revalidate_tm_fuzzy()
        except EditorControllerError:
            self.status_label.setText("Fuzzy 性能验证未能启动。")
            self._refresh_tm_threshold_entry()
            return
        self._refresh_tm_threshold_entry()
        self.fuzzy_validation_changed.emit(status)

    def _request_tm_threshold_update(self) -> None:
        """Submit one constrained value through the sole Controller update seam."""

        if self.tm_threshold_chip.property("fuzzyAvailable") is not True:
            return
        previous = self.controller.tm_preferences()
        try:
            requested = prompt_tm_threshold(self, previous)
            if requested is None:
                return
            outcome = self.controller.update_tm_minimum_similarity(requested)
        except EditorControllerError:
            self._refresh_tm_threshold_entry()
            self.status_label.setText(
                "Fuzzy 阈值未更新；当前值保持不变。"
            )
            return
        self.refresh_resources()
        self.status_label.setText(tm_threshold_feedback(outcome))
        if outcome.preferences != previous:
            self.tm_threshold_changed.emit(outcome)

    def _populate_table(
        self,
        table: QTableWidget,
        resources: tuple[ResourceConfig, ...],
        *,
        status_by_resource_id: dict[str, TMResourceStatus],
        operation: TMActivationOperationView | None,
    ) -> None:
        for widget in table.findChildren(QWidget):
            if widget.objectName().startswith(
                (
                    "tmResource_",
                    "resourceName_",
                    "tmStatus_",
                    "tmKindCell_",
                    "tmKindState_",
                    "tmCapabilities_",
                    "resourceKind_",
                    "more_",
                )
            ):
                if isinstance(widget, (QPushButton, QToolButton)):
                    widget.setEnabled(False)
                widget.setObjectName("")
        for action in table.findChildren(QAction):
            if action.objectName().startswith(
                ("tmLifecycleAction_", "manageTerms_")
            ):
                action.setEnabled(False)
                action.setObjectName("")
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
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                table.setCellWidget(
                    row,
                    3,
                    self._make_tm_resource_cell(
                        resource,
                        status_by_resource_id.get(resource.id),
                        operation=operation,
                    ),
                )
            else:
                table.setItem(row, 3, QTableWidgetItem(resource.name))
            kind_label = (
                "翻译记忆库"
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
                else "术语表"
            )
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                table.setCellWidget(
                    row,
                    4,
                    self._make_tm_kind_cell(
                        resource,
                        status_by_resource_id.get(resource.id),
                    ),
                )
            else:
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
            import_button.setMinimumWidth(112)
            import_button.clicked.connect(
                lambda _checked=False, configured=resource: self._prompt_import(configured)
            )
            table.setCellWidget(row, 6, import_button)
            more_button = _ResourceMoreButton()
            more_button.setObjectName(f"more_{resource.id}")
            more_button.setText("")
            more_button.setToolTip(f"{resource.name} 的更多操作")
            more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(more_button)
            menu.setObjectName(f"resourceMenu_{resource.id}")
            menu.setAccessibleName(f"{resource.name} 的更多操作菜单")
            configure_menu(menu)
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                action_text, action_tooltip, can_start = self._tm_lifecycle_action_spec(
                    status_by_resource_id.get(resource.id),
                )
                lifecycle_action = menu.addAction(action_text)
                lifecycle_action.setObjectName(f"tmLifecycleAction_{resource.id}")
                lifecycle_action.setToolTip(action_tooltip)
                lifecycle_action.setStatusTip(action_tooltip)
                lifecycle_action.setEnabled(
                    can_start
                    and not (operation is not None and not operation.completed)
                )
                lifecycle_action.triggered.connect(
                    lambda _checked=False, resource_id=resource.id: self._request_tm_lifecycle(
                        resource_id
                    )
                )
                menu.addSeparator()
            else:
                manage_action = menu.addAction("管理术语")
                manage_action.setObjectName(f"manageTerms_{resource.id}")
                manage_action.setToolTip(
                    f"打开 {resource.name} 的术语管理"
                )
                manage_action.setStatusTip(manage_action.toolTip())
                manage_action.setEnabled(resource.active and resource.update)
                manage_action.triggered.connect(
                    lambda _checked=False, configured=resource: self._open_termbase_dialog(
                        configured
                    )
                )
                menu.addSeparator()
            delete_action = menu.addAction("删除资源")
            delete_action.setObjectName(f"delete_{resource.id}")
            delete_action.triggered.connect(
                lambda _checked=False, configured=resource: self._confirm_delete_resource(
                    configured
                )
            )
            more_button.setMenu(menu)
            more_button.setAutoRaise(True)
            more_button.setStyleSheet(_RESOURCE_MORE_BUTTON_STYLE)
            size_policy = more_button.sizePolicy()
            size_policy.setHorizontalPolicy(QSizePolicy.Policy.Fixed)
            more_button.setSizePolicy(size_policy)
            more_button.setAccessibleName(f"{resource.name} 的更多操作")
            more_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            compact_width = min(
                40,
                max(32, more_button.sizeHint().width() + 8),
            )
            more_button.setFixedWidth(compact_width)
            table.setCellWidget(row, 7, more_button)
        table.resizeRowsToContents()

    def _open_termbase_dialog(self, resource: ResourceConfig) -> None:
        """Open one Controller-only termbase management surface."""

        if type(resource) is not ResourceConfig:
            raise TypeError("term management resource must be ResourceConfig")
        if resource.kind is not ResourceKind.TERMBASE:
            raise ValueError("term management requires a termbase resource")
        dialog = QtTermbaseDialog(
            self.controller,
            resource.id,
            resource.name,
            self,
        )
        dialog.terms_committed.connect(self._term_suggestions_committed)
        dialog.exec()

    def _term_suggestions_committed(self) -> None:
        """Refresh settings and forward one committed term publication."""

        self.refresh_resources()
        self.term_suggestions_changed.emit()

    @staticmethod
    def _tm_kind_projection(
        status: TMResourceStatus | None,
    ) -> tuple[str, str, str]:
        """Return one safe, non-authoritative lifecycle marker projection."""

        if status is None:
            return (
                "unavailable",
                _TM_KIND_UNAVAILABLE_COLOR,
                "状态暂不可用",
            )
        if status.mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
            return (
                "legacy",
                _TM_KIND_LEGACY_COLOR,
                "Legacy exact-only",
            )
        if status.mode in (
            TMResourceDisplayMode.CANONICAL_ACTIVE,
            TMResourceDisplayMode.SOURCE_DIVERGED,
        ) or (
            status.mode is TMResourceDisplayMode.DEGRADED
            and status.exact_available
        ):
            description = (
                "Canonical active"
                if status.mode is TMResourceDisplayMode.CANONICAL_ACTIVE
                else "Canonical last-known-good"
            )
            return (
                "canonical",
                _TM_KIND_CANONICAL_COLOR,
                description,
            )
        return (
            "unavailable",
            _TM_KIND_UNAVAILABLE_COLOR,
            _TM_MODE_LABELS[status.mode],
        )

    def _make_tm_kind_cell(
        self,
        resource: ResourceConfig,
        status: TMResourceStatus | None,
    ) -> QWidget:
        semantics, color, description = self._tm_kind_projection(status)
        capabilities_text = self._tm_capability_text(status)
        accessible_text = (
            f"翻译记忆库：{description}；"
            f"{capabilities_text.replace(' · ', '；')}"
        )
        holder = QWidget()
        holder.setObjectName(f"tmKindCell_{resource.id}")
        holder.setAccessibleName(accessible_text)
        holder.setToolTip(accessible_text)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(8, 3, 6, 3)
        layout.setSpacing(1)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(7)

        state = QLabel()
        state.setObjectName(f"tmKindState_{resource.id}")
        state.setProperty("tm_semantics", semantics)
        state.setFixedSize(10, 10)
        state.setStyleSheet(
            f"background-color: {color}; border: none; border-radius: 5px;"
        )
        state.setAccessibleName(f"{accessible_text}；状态点")
        state.setToolTip(accessible_text)
        heading.addWidget(state)

        kind = QLabel("翻译记忆库")
        kind.setObjectName(f"resourceKind_{resource.id}")
        kind.setAccessibleName(accessible_text)
        kind.setToolTip(accessible_text)
        heading.addWidget(kind, 1)
        layout.addLayout(heading)

        capabilities = QLabel(capabilities_text)
        capabilities.setObjectName(f"tmCapabilities_{resource.id}")
        capabilities.setAccessibleName(accessible_text)
        capabilities.setToolTip(accessible_text)
        capabilities.setWordWrap(False)
        capabilities.setStyleSheet("color: #52677b; font-size: 10px;")
        capabilities.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(capabilities)
        return holder

    @staticmethod
    def _tm_capability_text(status: TMResourceStatus | None) -> str:
        return (
            f"Exact {'可用' if status is not None and status.exact_available else '不可用'} · "
            f"Context {'可用' if status is not None and status.context_available else '不可用'} · "
            f"Fuzzy {'可用' if status is not None and status.fuzzy_available else '不可用'}"
        )

    @staticmethod
    def _tm_lifecycle_action_spec(
        status: TMResourceStatus | None,
    ) -> tuple[str, str, bool]:
        if (
            status is not None
            and status.mode is TMResourceDisplayMode.UNAVAILABLE
            and status.safe_codes
            == ("TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED",)
        ):
            return (
                "重新验证 canonical",
                "重新证明同一 canonical 文件身份，不重建 TM 或运行 Fuzzy 验证",
                True,
            )
        if status is not None and status.mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
            return (
                "激活 canonical",
                "显式检查并激活 canonical TM",
                True,
            )
        if status is not None and (
            status.mode
            in (
                TMResourceDisplayMode.CANONICAL_ACTIVE,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            )
            or (
                status.mode is TMResourceDisplayMode.DEGRADED
                and status.exact_available
            )
        ):
            return (
                "重建 canonical",
                "显式重建 canonical TM，失败时保留 last-known-good",
                True,
            )
        return (
            "Canonical 不可用",
            "当前状态不允许启动 canonical 操作",
            False,
        )

    def _make_tm_resource_cell(
        self,
        resource: ResourceConfig,
        status: TMResourceStatus | None,
        *,
        operation: TMActivationOperationView | None,
    ) -> QWidget:
        holder = QWidget()
        holder.setObjectName(f"tmResource_{resource.id}")
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(3)

        name = QLabel(resource.name)
        name.setObjectName(f"resourceName_{resource.id}")
        layout.addWidget(name)

        status_label = QLabel()
        status_label.setObjectName(f"tmStatus_{resource.id}")
        status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        status_label.setWordWrap(True)
        layout.addWidget(status_label)

        target_running = (
            operation is not None
            and not operation.completed
            and operation.resource_id == resource.id
        )
        if target_running:
            mode = TMResourceDisplayMode.ACTIVATING
            text = "激活中 · 原状态保持可见"
            safe_codes: tuple[str, ...] = ()
        elif status is None:
            mode = TMResourceDisplayMode.UNAVAILABLE
            text = "Unavailable · 状态暂不可用"
            safe_codes = ()
        else:
            status.__post_init__()
            mode = status.mode
            safe_codes = status.safe_codes
            text = _TM_MODE_LABELS[mode]
            if safe_codes:
                text += f" · {_tm_status_safe_reason(status, safe_codes[0])}"
        status_label.setText(text)
        status_label.setProperty("tm_mode", mode.value)
        status_label.setProperty("tmMode", mode.value)
        capabilities_text = self._tm_capability_text(status)
        status_accessible_name = (
            f"{resource.name}：{text}；"
            f"{capabilities_text.replace(' · ', '；')}"
        )
        holder.setAccessibleName(status_accessible_name)
        holder.setToolTip(status_accessible_name)
        name.setAccessibleName(status_accessible_name)
        name.setToolTip(status_accessible_name)
        status_label.setAccessibleName(status_accessible_name)
        status_label.setToolTip(status_accessible_name)

        return holder

    def _request_tm_lifecycle(self, resource_id: str) -> None:
        """Run one explicit Controller-owned activation or rebuild request."""

        try:
            operation = self.controller.tm_activation_operation()
            if operation is not None and not operation.completed:
                self.status_label.setText("Canonical 操作已在进行。")
                self.refresh_resources()
                return
            resource = next(
                configured
                for configured in self.controller.list_resources()
                if configured.id == resource_id
                and configured.kind is ResourceKind.TRANSLATION_MEMORY
            )
            status = next(
                item
                for item in self.controller.tm_resource_statuses()
                if item.resource_id == resource_id
            )
            if (
                status.mode is TMResourceDisplayMode.UNAVAILABLE
                and status.safe_codes
                == ("TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED",)
            ):
                answer = _ask_localized_question(
                    self,
                    "重新验证 canonical TM",
                    (
                        f"资源：{resource.name}\n\n"
                        "将重新证明同一 canonical generation 的本地文件身份；"
                        "不会重建或修改 TM 内容，也不会启动 Fuzzy 性能验证。"
                    ),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                    {
                        QMessageBox.StandardButton.Yes: "重新验证",
                        QMessageBox.StandardButton.Cancel: "取消",
                    },
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.status_label.setText(
                        f"已取消 {resource.name} 的 canonical 重新验证。"
                    )
                    return
                started = self.controller.reattest_tm_resource(resource_id)
                action_name = "Canonical 重新验证"
            elif status.mode is TMResourceDisplayMode.LEGACY_EXACT_ONLY:
                preflight = self.controller.prepare_tm_activation(resource_id)
                prompt = (
                    f"资源：{preflight.resource_name}\n"
                    f"有效 {preflight.valid_count} · 无效 {preflight.invalid_count} "
                    f"· 变体 {preflight.variant_count}\n\n"
                    "预期变化：Legacy exact-only → Canonical active。\n"
                    "Context / fuzzy 仍只按当前已验证能力开放。"
                )
                answer = _ask_localized_question(
                    self,
                    "激活 canonical TM",
                    prompt,
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                    {
                        QMessageBox.StandardButton.Yes: "激活",
                        QMessageBox.StandardButton.Cancel: "取消",
                    },
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.controller.cancel_tm_activation(preflight)
                    self.status_label.setText(
                        f"已取消 {preflight.resource_name} 的 canonical 激活。"
                    )
                    return
                started = self.controller.activate_tm_resource(preflight)
                action_name = "Canonical 激活"
            elif status.mode in (
                TMResourceDisplayMode.CANONICAL_ACTIVE,
                TMResourceDisplayMode.SOURCE_DIVERGED,
                TMResourceDisplayMode.DEGRADED,
            ) and status.exact_available:
                answer = _ask_localized_question(
                    self,
                    "重建 canonical TM",
                    (
                        f"资源：{resource.name}\n\n"
                        "将从当前本地来源显式重建 canonical TM；"
                        "失败时保留 last-known-good canonical 状态。"
                    ),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                    {
                        QMessageBox.StandardButton.Yes: "重建",
                        QMessageBox.StandardButton.Cancel: "取消",
                    },
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.status_label.setText(
                        f"已取消 {resource.name} 的 canonical 重建。"
                    )
                    return
                started = self.controller.rebuild_tm_resource(resource_id)
                action_name = "Canonical 重建"
            else:
                self.status_label.setText("当前资源状态不允许启动 canonical 操作。")
                return
        except Exception as error:
            self._show_tm_action_error(error)
            return

        self._tm_operation_id = started.operation_id
        self._tm_operation_action = action_name
        self.refresh_resources()
        self.status_label.setText(f"{action_name}已开始；重复操作已禁用。")
        if not self._tm_operation_timer.isActive():
            self._tm_operation_timer.start()

    def _poll_tm_operation(self) -> None:
        """Refresh one body-free Controller operation without blocking Qt."""

        operation_id = self._tm_operation_id
        if operation_id is None:
            self._tm_operation_timer.stop()
            return
        try:
            operation = self.controller.tm_activation_operation()
        except Exception as error:
            self._tm_operation_timer.stop()
            self._show_tm_action_error(error)
            return
        if operation is None or operation.operation_id != operation_id:
            self._tm_operation_timer.stop()
            self._tm_operation_id = None
            self._tm_operation_action = None
            self.status_label.setText("Canonical 操作状态无法确认。")
            return
        if not operation.completed:
            return

        self._tm_operation_timer.stop()
        action_name = self._tm_operation_action or "Canonical 操作"
        self._tm_operation_id = None
        self._tm_operation_action = None
        self.refresh_resources()
        if operation.succeeded:
            self.status_label.setText(f"{action_name}已完成。")
        else:
            reason = _tm_safe_reason(operation.safe_code)
            self.status_label.setText(f"{action_name}失败：{reason}。")
        self.resources_changed.emit()

    def _show_tm_action_error(self, error: Exception) -> None:
        raw = str(error) if type(error) is EditorControllerError else None
        code = (
            raw
            if raw is not None
            and raw in _TM_ACTION_EXCEPTION_SAFE_CODES
            else None
        )
        reason = (
            _tm_safe_reason(code)
            if code is not None
            else "内部状态无法安全确认"
        )
        self.status_label.setText(f"Canonical 操作无法开始：{reason}。")

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
            show_localized_critical(self, title="无法更新资源", text=str(exc))
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
        answer = _ask_localized_question(
            self,
            "删除语言资源",
            (
                f"确定删除“{resource.name}”吗？\n\n"
                "LocalCAT 托管的数据文件会一并删除；仓库默认资源或其他外部"
                "文件只会从列表取消登记。"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
            {
                QMessageBox.StandardButton.Yes: "删除",
                QMessageBox.StandardButton.Cancel: "取消",
            },
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.delete_resource(resource.id)
        except EditorControllerError as exc:
            show_localized_critical(self, title="无法删除资源", text=str(exc))
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
        kind_input.setObjectName("newResourceKind")
        kind_input.setAccessibleName("资源类型")
        kind_input.addItem("翻译记忆库", ResourceKind.TRANSLATION_MEMORY)
        kind_input.addItem("术语表", ResourceKind.TERMBASE)
        kind_input.setStyleSheet(_RESOURCE_KIND_COMBO_STYLE)
        configure_combo_popup(
            kind_input,
            object_name="newResourceKindPopup",
            accessible_name="资源类型选项",
        )
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
            show_localized_critical(self, title="无法创建资源", text=str(exc))

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
        if resource.kind is ResourceKind.TERMBASE:
            self.start_termbase_preview(resource.id, Path(selected))
            return
        source_locale = ""
        target_locale = ""
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

    @staticmethod
    def _lexical_absolute(path: Path) -> Path:
        expanded = path.expanduser()
        return expanded if expanded.is_absolute() else expanded.absolute()

    def start_termbase_preview(
        self,
        resource_id: str,
        input_path: Path,
    ) -> bool:
        """Start the codec-owned preview while holding the shared import gate."""

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
        if resource.kind is not ResourceKind.TERMBASE:
            self._show_import_feedback("只有术语表导入需要列预览。", failed=True)
            return False
        if not isinstance(input_path, Path):
            self._show_import_feedback("术语表路径无效。", failed=True)
            return False

        lexical_input = self._lexical_absolute(input_path)
        self.last_import_report = None
        self._import_target_kind = ResourceKind.TERMBASE
        self._import_busy = True
        self._set_import_busy(True, f"正在预览 {resource.name} 的列…")
        worker = PreviewWorker(self.controller, lexical_input, self)
        self.preview_worker = worker
        worker.finished.connect(
            lambda current=worker, configured=resource: self._on_preview_finished(
                current,
                configured,
            )
        )
        worker.start()
        return True

    def _on_preview_finished(
        self,
        worker: PreviewWorker,
        resource: ResourceConfig,
    ) -> None:
        if self.preview_worker is not worker:
            worker.deleteLater()
            return
        self.preview_worker = None
        preview = worker.preview
        error_message = worker.error_message
        input_path = worker.input_path
        worker.deleteLater()

        if error_message is not None:
            self._finish_preview_without_import(
                f"无法预览术语表：{error_message}",
                failed=True,
            )
            return
        if preview is None:
            self._finish_preview_without_import(
                "无法预览术语表：预览结果不可用。",
                failed=True,
            )
            return
        if len(preview.columns) < 2:
            self._finish_preview_without_import(
                "术语表预览至少 2 列才能导入。",
                failed=True,
            )
            return

        prompt = TermbaseColumnSelectionDialog(preview, self)
        try:
            if prompt.exec() != QDialog.DialogCode.Accepted:
                self._finish_preview_without_import("已取消术语表导入。", failed=False)
                return
            try:
                selection = prompt.selection()
                request = ImportRequest(
                    resource_id=resource.id,
                    input_path=input_path,
                    termbase_selection=selection,
                )
            except (TypeError, ValueError) as exc:
                self._finish_preview_without_import(str(exc), failed=True)
                return
        finally:
            prompt.deleteLater()

        self._launch_import(resource, request, busy_already=True)

    def _finish_preview_without_import(self, message: str, *, failed: bool) -> None:
        self._import_busy = False
        self._set_import_busy(False)
        self._show_import_feedback(message, failed=failed)

    def start_import(
        self,
        resource_id: str,
        input_path: Path,
        source_locale: str = "",
        target_locale: str = "",
        termbase_selection: TermbaseImportSelection | None = None,
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
                input_path=self._lexical_absolute(input_path),
                source_locale=source_locale,
                target_locale=target_locale,
                termbase_selection=termbase_selection,
            )
        except (TypeError, ValueError) as exc:
            self._show_import_feedback(str(exc), failed=True)
            return False

        self._launch_import(resource, request, busy_already=False)
        return True

    def _launch_import(
        self,
        resource: ResourceConfig,
        request: ImportRequest,
        *,
        busy_already: bool,
    ) -> None:
        if not busy_already:
            self._import_busy = True
        self.last_import_report = None
        self._import_target_kind = resource.kind
        self._set_import_busy(True, f"正在导入 {resource.name}…")
        worker = ImportWorker(self.controller, request, self)
        self.import_worker = worker
        worker.report_ready.connect(self._on_import_finished)
        worker.finished.connect(lambda current=worker: self._release_worker(current))
        worker.start()

    def _set_import_busy(self, busy: bool, message: str = "") -> None:
        self.active_table.setEnabled(not busy)
        self.inactive_table.setEnabled(not busy)
        self.new_resource_button.setEnabled(not busy)
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
            self._show_import_feedback("导入预览或导入完成前无法关闭设置。", failed=True)
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
QFrame#settingsTmThresholdPanel {
    background: #ffffff;
    border: 1px solid #d7e0ea;
    border-radius: 7px;
}
QLabel#settingsTmThresholdTitle {
    color: #2a4058;
    font-weight: 700;
}
QLabel#settingsTmThresholdState {
    color: #52677b;
    font-size: 11px;
}
QLabel#settingsTmThresholdState[fuzzyAvailable="false"] {
    color: #9b5a24;
}
QPushButton#settingsTmThresholdChip {
    min-height: 26px;
    padding: 1px 10px;
    color: #087f9f;
    background: #e6f5f9;
    border: 1px solid #99cfdb;
    border-radius: 13px;
    font-weight: 750;
}
QPushButton#settingsTmThresholdChip:focus {
    border: 2px solid #087f9f;
}
QPushButton#settingsTmThresholdChip[fuzzyAvailable="false"] {
    color: #6f7d8a;
    background: #edf0f3;
    border-color: #cbd3da;
}
QPushButton#fuzzyRevalidateButton {
    min-height: 26px;
    padding: 1px 10px;
}
QLabel[tmMode="LEGACY_EXACT_ONLY"] {
    color: #40566d;
}
QLabel[tmMode="CANONICAL_ACTIVE"] {
    color: #216746;
}
QLabel[tmMode="SOURCE_DIVERGED"],
QLabel[tmMode="DEGRADED"],
QLabel[tmMode="ACTIVATING"] {
    color: #8a5414;
}
QLabel[tmMode="UNAVAILABLE"] {
    color: #a13434;
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
