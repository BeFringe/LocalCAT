"""Professional three-column PySide6 editor window for LocalCAT."""

from __future__ import annotations

import html
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import (
    QMimeData,
    QObject,
    QEvent,
    QItemSelectionModel,
    QPoint,
    QPointF,
    QRect,
    QSignalBlocker,
    QSize,
    Signal,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPaintEvent,
    QPainter,
    QPen,
    QPolygonF,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QScrollArea,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionToolButton,
)

from editor_contracts import (
    BatchOperationReport,
    EDITOR_FONT_SIZE_STEP,
    MAX_EDITOR_FONT_SIZE,
    MIN_EDITOR_FONT_SIZE,
    DisplayPreferences,
    EditorSegment,
    FuzzyValidationDisplay,
    FuzzyValidationState,
    LegacyExactTMSuggestion,
    ProjectSearchReport,
    ProjectSearchRequest,
    ResourceKind,
    SearchField,
    SearchScope,
    SearchOptions,
    SegmentDensity,
    SegmentTranslationStatus,
    SuggestionBundle,
    TermSuggestion,
    TMResourceDisplayMode,
    TMThresholdUpdateOutcome,
    TMSuggestion,
    TMSuggestionReport,
    TextMatcherState,
    WorkspaceSearchReport,
    WorkspaceSearchRequest,
    WorkspaceMode,
)
from editor_controller import EditorController, EditorControllerError
from chunk_controller_contracts import (
    ChunkApplicationMode,
    ChunkApplicationProjectView,
    ChunkApplicationSegmentSelectionRequest,
    CollaborativeSearchScopeV2,
    CollaborativeWorkspaceSearchHitV2,
    CollaborativeWorkspaceSearchReportV2,
    CollaborativeWorkspaceSearchRequestV2,
)
from qt_browse_group_dialog import (
    BrowseGroupPreview,
    BrowseGroupTurnBar,
    QtBrowseGroupDialog,
)
from qt_preprocess_dialog import QtPreprocessDialog
from qt_settings_dialog import QtSettingsDialog
from qt_speaker_inventory_dialog import QtSpeakerInventoryDialog
from qt_termbase_dialog import QtTermbaseDialog
from qt_control_styles import configure_combo_popup, configure_menu
from qt_localized_message_box import show_localized_critical
from qt_tm_threshold import (
    TMThresholdButton,
    configure_tm_threshold_entry,
    prompt_tm_threshold,
    tm_threshold_feedback,
)
from qt_tmx_export_dialog import (
    TmxExportDialog,
    TmxExportDialogPreview,
    TmxExportScopeChoice,
)


class _TMApplyButton(QPushButton):
    """Keep TM apply reachable through both standard activation keys."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.click()
            return
        super().keyPressEvent(event)


def _paint_top_bar_chevron(
    widget: QWidget,
    rect: QRect,
    *,
    enabled: bool,
) -> None:
    """Paint one platform-independent, high-contrast downward chevron."""

    if rect.width() <= 0 or rect.height() <= 0:
        return
    center = rect.center()
    painter = QPainter(widget)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor("#d6e7f4" if enabled else "#8facc2"))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(
            QPolygonF(
                (
                    QPointF(center.x() - 4.0, center.y() - 2.0),
                    QPointF(center.x(), center.y() + 2.0),
                    QPointF(center.x() + 4.0, center.y() - 2.0),
                )
            )
        )
    finally:
        painter.end()


def _localcat_document_icon(size: int = 18) -> QIcon:
    """Return an application-owned document icon with light-background contrast."""

    if type(size) is not int or size < 12:
        raise ValueError("document icon size must be an integer of at least 12")
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    scale = size / 18.0
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QPolygonF(
            (
                QPointF(3 * scale, 1 * scale),
                QPointF(11 * scale, 1 * scale),
                QPointF(16 * scale, 6 * scale),
                QPointF(16 * scale, 17 * scale),
                QPointF(3 * scale, 17 * scale),
            )
        )
        painter.setPen(QPen(QColor("#082f4d"), max(1.0, 1.0 * scale)))
        painter.setBrush(QColor("#176887"))
        painter.drawPolygon(body)
        fold = QPolygonF(
            (
                QPointF(11 * scale, 1 * scale),
                QPointF(11 * scale, 6 * scale),
                QPointF(16 * scale, 6 * scale),
            )
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2cc0d9"))
        painter.drawPolygon(fold)
        line_pen = QPen(QColor("#f5fbff"), max(1.0, 1.2 * scale))
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(line_pen)
        for y, end_x in ((9.0, 13.0), (12.0, 13.0), (15.0, 10.5)):
            painter.drawLine(
                QPointF(6 * scale, y * scale),
                QPointF(end_x * scale, y * scale),
            )
    finally:
        painter.end()
    return QIcon(pixmap)


class _TopBarModeCombo(QComboBox):
    """Workspace mode combo with an application-owned visible arrow."""

    _POPUP_GAP = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode_popup_menu = QMenu(self)
        self.mode_popup_menu.setObjectName("workspaceModePopupMenu")
        self.mode_popup_menu.setAccessibleName("工作区模式选项")
        configure_menu(self.mode_popup_menu)
        self.mode_popup_menu.triggered.connect(self._mode_action_triggered)

    def showPopup(self) -> None:
        """Open one app-owned menu below, never over, the current mode."""

        if not self.isEnabled() or self.count() <= 0:
            return
        self.mode_popup_menu.clear()
        for index in range(self.count()):
            action = self.mode_popup_menu.addAction(self.itemText(index))
            action.setData(index)
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
        self.mode_popup_menu.setMinimumWidth(self.width())
        self.mode_popup_menu.adjustSize()
        target = self.mapToGlobal(
            QPoint(0, self.height() + self._POPUP_GAP)
        )
        screen = self.screen().availableGeometry()
        x = min(
            max(target.x(), screen.left()),
            max(
                screen.left(),
                screen.right() - self.mode_popup_menu.width() + 1,
            ),
        )
        self.mode_popup_menu.popup(QPoint(x, target.y()))

    def hidePopup(self) -> None:
        self.mode_popup_menu.hide()
        super().hidePopup()

    def _mode_action_triggered(self, action: QAction) -> None:
        index = action.data()
        if type(index) is not int or not 0 <= index < self.count():
            return
        self.setCurrentIndex(index)

    def _arrow_rect(self) -> QRect:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        _paint_top_bar_chevron(
            self,
            self._arrow_rect(),
            enabled=self.isEnabled(),
        )


class _TopBarProjectButton(QToolButton):
    """Split project button with an application-owned visible menu arrow."""

    def _menu_rect(self) -> QRect:
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ToolButton,
            option,
            QStyle.SubControl.SC_ToolButtonMenu,
            self,
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        _paint_top_bar_chevron(
            self,
            self._menu_rect(),
            enabled=self.isEnabled(),
        )


class _TopBarSearchButton(QToolButton):
    """Top-bar search toggle with a platform-independent magnifier."""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        center = self.rect().center()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(
                QColor("#ffffff" if self.isChecked() else (
                    "#d6e7f4" if self.isEnabled() else "#7895aa"
                ))
            )
            pen.setWidthF(1.9)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            lens_center = QPointF(center.x() - 1.5, center.y() - 1.5)
            painter.drawEllipse(lens_center, 5.0, 5.0)
            painter.drawLine(
                QPointF(center.x() + 2.0, center.y() + 2.0),
                QPointF(center.x() + 6.0, center.y() + 6.0),
            )
        finally:
            painter.end()


class _InlineMenuButton(QToolButton):
    """Menu button with one app-owned chevron beside its label."""

    def inlineChevronRect(self) -> QRect:
        arrow_size = 8
        return QRect(
            max(0, self.width() - 18),
            max(0, (self.height() - arrow_size) // 2),
            arrow_size,
            arrow_size,
        )

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        label_width = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(max(hint.width(), label_width + 40), hint.height())

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        rect = self.inlineChevronRect()
        center = rect.center()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor("#244b68" if self.isEnabled() else "#8da2b2"))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(
                QPolygonF(
                    (
                        QPointF(center.x() - 3.5, center.y() - 1.5),
                        QPointF(center.x(), center.y() + 2.0),
                        QPointF(center.x() + 3.5, center.y() - 1.5),
                    )
                )
            )
        finally:
            painter.end()


class _WorkspaceDropPage(QWidget):
    """Accept only an explicit, ordered selection of local files."""

    explicitFilesDropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def explicit_local_file_paths(
        mime_data: QMimeData,
    ) -> tuple[Path, ...] | None:
        if not mime_data.hasUrls():
            return None
        paths: list[Path] = []
        identities: set[str] = set()
        for url in mime_data.urls():
            if not url.isLocalFile():
                return None
            path = Path(url.toLocalFile()).expanduser()
            if not path.is_absolute() or not path.is_file():
                return None
            identity = os.path.normcase(os.path.abspath(str(path)))
            if identity in identities:
                return None
            identities.add(identity)
            paths.append(path)
        return tuple(paths) if paths else None

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.explicit_local_file_paths(event.mimeData()) is None:
            event.ignore()
            return
        self._set_drag_active(True)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self.explicit_local_file_paths(event.mimeData()) is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self.explicit_local_file_paths(event.mimeData())
        self._set_drag_active(False)
        if paths is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.explicitFilesDropped.emit(paths)


class QtWorkspaceCreationDialog(QDialog):
    """Review and reorder one explicit file selection before C2 intake."""

    def __init__(
        self,
        selected_paths: tuple[Path, ...],
        *,
        default_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if type(selected_paths) is not tuple or len(selected_paths) < 2:
            raise ValueError("workspace creation requires at least two selected paths")
        if any(not isinstance(path, Path) for path in selected_paths):
            raise TypeError("workspace creation selection must contain Paths")
        self.setObjectName("workspaceCreationDialog")
        self.setWindowTitle("确认多文档项目")
        self.resize(680, 480)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "仅导入下列显式选择的文件。列表顺序将成为初始章节顺序；"
            "不会扫描目录或自动包含相邻文件。"
        )
        hint.setObjectName("workspaceCreationHint")
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.selected_files = QListWidget()
        self.selected_files.setObjectName("workspaceSelectedFiles")
        self.selected_files.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for path in selected_paths:
            item = QListWidgetItem(str(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path))
            self.selected_files.addItem(item)
        self.selected_files.setCurrentRow(0)
        layout.addWidget(self.selected_files, 1)

        order_row = QHBoxLayout()
        self.move_up_button = QPushButton("上移")
        self.move_up_button.setObjectName("workspaceMoveSelectedUp")
        self.move_down_button = QPushButton("下移")
        self.move_down_button.setObjectName("workspaceMoveSelectedDown")
        self.move_up_button.clicked.connect(lambda: self.move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self.move_selected(1))
        order_row.addWidget(self.move_up_button)
        order_row.addWidget(self.move_down_button)
        order_row.addStretch()
        layout.addLayout(order_row)

        metadata = QHBoxLayout()
        metadata.addWidget(QLabel("项目名"))
        self.project_name_input = QLineEdit(default_name)
        self.project_name_input.setObjectName("workspaceProjectName")
        metadata.addWidget(self.project_name_input, 1)
        metadata.addWidget(QLabel("源语言"))
        self.source_locale_input = QLineEdit()
        self.source_locale_input.setObjectName("workspaceSourceLocale")
        self.source_locale_input.setPlaceholderText("默认 en")
        metadata.addWidget(self.source_locale_input)
        metadata.addWidget(QLabel("目标语言"))
        self.target_locale_input = QLineEdit()
        self.target_locale_input.setObjectName("workspaceTargetLocale")
        self.target_locale_input.setPlaceholderText("默认 zh-CN")
        metadata.addWidget(self.target_locale_input)
        layout.addLayout(metadata)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self.buttons.setObjectName("workspaceCreationButtons")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "开始导入"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "取消"
        )
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def move_selected(self, direction: int) -> None:
        if direction not in (-1, 1):
            raise ValueError("workspace reorder direction must be -1 or 1")
        row = self.selected_files.currentRow()
        destination = row + direction
        if row < 0 or destination < 0 or destination >= self.selected_files.count():
            return
        item = self.selected_files.takeItem(row)
        self.selected_files.insertItem(destination, item)
        self.selected_files.setCurrentRow(destination)

    @property
    def ordered_paths(self) -> tuple[Path, ...]:
        return tuple(
            self.selected_files.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.selected_files.count())
        )

    def _accept_if_valid(self) -> None:
        if self.project_name_input.text().strip():
            self.accept()

    @property
    def source_locale(self) -> str:
        return self.source_locale_input.text().strip() or "en"

    @property
    def target_locale(self) -> str:
        return self.target_locale_input.text().strip() or "zh-CN"


class QtWorkspacePackageImportDialog(QDialog):
    """Show one receipt-safe package preview without wrapping its identity."""

    _MODE_COPY = {
        "new": (
            "NEW · 导入为新项目",
            "Apply 后把 incoming 包发布到新位置，并切换为该项目。",
        ),
        "replace": (
            "REPLACE · 替换当前项目",
            "project ID 不同；Apply 后 incoming 项目将替换当前项目。",
        ),
        "update_same_project": (
            "UPDATE · 更新同一项目",
            "project ID 相同；Apply 后按稳定身份调和并发布更新。",
        ),
    }

    def __init__(
        self,
        *,
        mode: str,
        current_project_name: str,
        incoming_project_name: str,
        incoming_project_id: str,
        document_count: int,
        segment_count: int,
        reconciliation_counts: tuple[int, int, int, int, int, int],
        warnings: tuple[str, ...],
        blocking_reasons: tuple[str, ...],
        required_decision_count: int,
        can_apply: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in self._MODE_COPY:
            raise ValueError("unsupported package import preview mode")
        if any(
            type(value) is not str
            for value in (
                current_project_name,
                incoming_project_name,
                incoming_project_id,
            )
        ):
            raise TypeError("package import preview text must be exact str")
        counts = (document_count, segment_count, *reconciliation_counts)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("package import preview counts must be non-negative")
        if (
            type(warnings) is not tuple
            or type(blocking_reasons) is not tuple
            or any(type(value) is not str for value in (*warnings, *blocking_reasons))
            or type(required_decision_count) is not int
            or required_decision_count < 0
            or type(can_apply) is not bool
        ):
            raise TypeError("package import preview safety facts are invalid")

        self.setObjectName("workspacePackageImportDialog")
        self.setWindowTitle("预览并导入 ProjectPackage")
        self.setModal(True)
        self.setMinimumWidth(720)
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        title = QLabel("预览并导入 ProjectPackage")
        title.setObjectName("packageImportTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(title)
        heading.addStretch()
        mode_title, mode_explanation = self._MODE_COPY[mode]
        self.mode_label = QLabel(mode_title)
        self.mode_label.setObjectName("packageImportMode")
        self.mode_label.setProperty("mode", mode)
        self.mode_label.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.mode_label)
        layout.addLayout(heading)

        self.transition_label = QLabel(
            f"{current_project_name}  →  {incoming_project_name}"
        )
        self.transition_label.setObjectName("packageImportTransition")
        self.transition_label.setTextFormat(Qt.TextFormat.PlainText)
        self.transition_label.setWordWrap(True)
        layout.addWidget(self.transition_label)

        explanation = QLabel(mode_explanation)
        explanation.setObjectName("packageImportExplanation")
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        summary = QFrame()
        summary.setObjectName("packageImportSummary")
        summary_layout = QGridLayout(summary)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setHorizontalSpacing(14)
        summary_layout.setVerticalSpacing(10)
        id_label = QLabel("导入项目 ID（可复制）")
        id_label.setObjectName("packageImportIdLabel")
        self.project_id_input = QLineEdit(incoming_project_id)
        self.project_id_input.setObjectName("packageImportProjectId")
        self.project_id_input.setReadOnly(True)
        self.project_id_input.setCursorPosition(0)
        self.project_id_input.setToolTip(incoming_project_id)
        summary_layout.addWidget(id_label, 0, 0)
        summary_layout.addWidget(self.project_id_input, 0, 1, 1, 3)
        document_label = QLabel(f"文档  {document_count}")
        document_label.setObjectName("packageImportDocumentCount")
        segment_label = QLabel(f"段落  {segment_count}")
        segment_label.setObjectName("packageImportSegmentCount")
        summary_layout.addWidget(document_label, 1, 1)
        summary_layout.addWidget(segment_label, 1, 2)
        summary_layout.setColumnStretch(3, 1)
        layout.addWidget(summary)

        unchanged, source_changed, added, removed, ambiguous, unresolved = (
            reconciliation_counts
        )
        reconciliation_text = (
            "reconciliation  ·  "
            f"未变 {unchanged}   源变化 {source_changed}   新增 {added}   "
            f"移除 {removed}   歧义 {ambiguous}   未解决 {unresolved}"
            if mode == "update_same_project"
            else "reconciliation  ·  不适用（跨项目导入不会合并文档）"
        )
        self.reconciliation_label = QLabel(reconciliation_text)
        self.reconciliation_label.setObjectName("packageImportReconciliation")
        self.reconciliation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reconciliation_label.setWordWrap(True)
        layout.addWidget(self.reconciliation_label)

        safety = QFrame()
        safety.setObjectName("packageImportSafety")
        safety.setProperty(
            "state",
            "blocked" if not can_apply else ("warning" if warnings else "ready"),
        )
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(14, 11, 14, 11)
        if not can_apply:
            safety_text = "阻断 · " + (
                "、".join(blocking_reasons) or "需要完成调和决策"
            )
            if required_decision_count:
                safety_text += f" · 待决身份 {required_decision_count}"
        elif warnings:
            safety_text = "警告 · " + "、".join(warnings)
        else:
            safety_text = "检查通过 · 没有阻断或警告"
        self.safety_label = QLabel(safety_text)
        self.safety_label.setObjectName("packageImportSafetyText")
        self.safety_label.setTextFormat(Qt.TextFormat.PlainText)
        self.safety_label.setWordWrap(True)
        safety_layout.addWidget(self.safety_label)
        layout.addWidget(safety)

        note = QLabel("只有点击“应用导入”后才会发布；取消不会修改任何项目文件。")
        note.setObjectName("packageImportNote")
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.buttons.setObjectName("packageImportButtons")
        self.cancel_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Apply
        )
        self.cancel_button.setText("取消")
        self.apply_button.setText("应用导入")
        self.cancel_button.setObjectName("packageImportCancel")
        self.apply_button.setObjectName("packageImportApply")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.apply_button.setDefault(False)
        self.apply_button.setAutoDefault(False)
        self.apply_button.setEnabled(can_apply)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.accept)
        layout.addWidget(self.buttons)


class ResponsiveSplitter(QSplitter):
    """QSplitter with inspectable stretch metadata for layout verification."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._stretch_factors: dict[int, int] = {}

    def setStretchFactor(self, index: int, stretch: int) -> None:
        super().setStretchFactor(index, stretch)
        self._stretch_factors[index] = stretch

    def stretchFactor(self, index: int) -> int:
        return self._stretch_factors.get(index, 0)


def render_highlighted_source(text: str, terms: tuple[TermSuggestion, ...]) -> str:
    """Escape project text, then add spans for longest non-overlapping term ranges."""

    occupied = [False] * len(text)
    selected: list[TermSuggestion] = []
    for term in sorted(
        terms,
        key=lambda item: (-(item.end_index - item.start_index), item.start_index),
    ):
        start = max(0, min(term.start_index, len(text)))
        end = max(start, min(term.end_index, len(text)))
        if start == end or any(occupied[start:end]):
            continue
        selected.append(term)
        for index in range(start, end):
            occupied[index] = True
    selected.sort(key=lambda item: item.start_index)

    pieces: list[str] = []
    cursor = 0
    for term in selected:
        start = max(cursor, term.start_index)
        end = min(len(text), term.end_index)
        pieces.append(html.escape(text[cursor:start]).replace("\n", "<br>"))
        highlighted = html.escape(text[start:end]).replace("\n", "<br>")
        tooltip = html.escape(f"{term.target_term} · {term.resource_name}", quote=True)
        pieces.append(
            '<span style="background-color:#fff0ad; color:#26384b; '
            f'font-weight:600;" title="{tooltip}">{highlighted}</span>'
        )
        cursor = end
    pieces.append(html.escape(text[cursor:]).replace("\n", "<br>"))
    return (
        '<div style="white-space:pre-wrap; color:#1c2b3a;">'
        + "".join(pieces)
        + "</div>"
    )


@dataclass(slots=True)
class _ChunkSegmentSelectionSession:
    manager: object
    request: ChunkApplicationSegmentSelectionRequest
    previous_mode: WorkspaceMode
    previous_identity: object
    previous_chunk_id: str | None
    previous_search_state: tuple[object, ...]
    enabled_widgets: tuple[tuple[QWidget, bool], ...]
    enabled_shortcuts: tuple[tuple[object, bool], ...]
    browse_hint_text: str
    browse_group_button_visible: bool
    browse_group_turn_bar_visible: bool
    range_start_row: int | None = None
    range_end_row: int | None = None
    selection_anchor_row: int | None = None


class QtEditorWindow(QMainWindow):
    """LocalCAT desktop shell; all domain operations go through EditorController."""

    def __init__(
        self,
        controller: EditorController,
        *,
        chunk_controller: object | None = None,
        tmx_export_coordinator: object | None = None,
    ) -> None:
        super().__init__()
        # The UI is assembled by small builder methods.  Keep the resulting
        # widget contract explicit here so static analysis sees the same
        # always-initialized surface that callers receive after construction.
        self.shortcuts: dict[str, QShortcut]
        self.target_editor_shortcuts: dict[str, QShortcut]
        self.project_search_shortcut: QShortcut
        self.top_bar: QFrame
        self.top_bar_layout: QHBoxLayout
        self.brand_name_label: QLabel
        self.brand_tagline_label: QLabel
        self.top_separator: QFrame
        self.project_name_label: QLabel
        self.language_label: QLabel
        self.progress_bar: QProgressBar
        self.workspace_mode_combo: _TopBarModeCombo
        self.open_button: QToolButton
        self.project_menu: QMenu
        self.open_project_action: QAction
        self.open_workspace_package_action: QAction
        self.import_workspace_package_action: QAction
        self.save_workspace_document_action: QAction
        self.recent_projects_menu: QMenu
        self.speaker_inventory_action: QAction
        self.preprocess_action: QAction
        self.close_project_action: QAction
        self.quit_action: QAction
        self.save_button: QToolButton
        self.settings_button: QToolButton
        self.project_search_toggle: QToolButton
        self.workspace_documents_button: QToolButton
        self.workspace_documents_menu: QMenu
        self.editor_page: QWidget
        self.empty_page: _WorkspaceDropPage
        self.empty_open_button: QPushButton
        self.sample_button: QPushButton
        self.main_splitter: ResponsiveSplitter
        self.workspace_pages: QStackedWidget
        self.segment_count_label: QLabel
        self.workspace_chapter_title: QLabel
        self.workspace_browse_chapter_title: QLabel
        self.chapter_progress_label: QLabel
        self.workspace_save_feedback: QLabel
        self.workspace_browse_save_feedback: QLabel
        self.browse_hint: QLabel
        self.segment_density_combo: QComboBox
        self.unconfirmed_filter: QCheckBox
        self.segment_list: QListWidget
        self.project_search_input: QLineEdit
        self.project_search_panel: QFrame
        self.project_search_status: QComboBox
        self.workspace_search_scope: QComboBox
        self.project_search_scope: QComboBox
        self.project_search_clear: QPushButton
        self.project_search_source: QCheckBox
        self.project_search_target: QCheckBox
        self.project_search_speaker: QCheckBox
        self.project_search_match_case: QCheckBox
        self.project_search_whole_word: QCheckBox
        self.project_search_button: QPushButton
        self.project_search_previous: QPushButton
        self.project_search_next: QPushButton
        self.project_search_capability: QLabel
        self.project_search_result: QLabel
        self.project_search_preview: QLabel
        self.chunk_scope_menu: QMenu
        self.chunk_manage_action: QAction
        self.browse_table: QTableWidget
        self.browse_group_button: QPushButton
        self.browse_group_turn_bar: BrowseGroupTurnBar
        self.chunk_segment_selection_bar: QFrame
        self.chunk_segment_selection_title: QLabel
        self.chunk_segment_selection_status: QLabel
        self.chunk_segment_range_start: QPushButton
        self.chunk_segment_range_end: QPushButton
        self.chunk_segment_bulk_select: QPushButton
        self.chunk_segment_clear: QPushButton
        self.chunk_segment_cancel: QPushButton
        self.chunk_segment_done: QPushButton
        self.segment_position_label: QLabel
        self.speaker_display: QLabel
        self.source_display: QTextBrowser
        self.confirmation_label: QLabel
        self.target_editor: QTextEdit
        self.previous_button: QPushButton
        self.next_button: QPushButton
        self.confirm_button: QPushButton
        self.suggestion_tabs: QTabWidget
        self.translation_matches_page: QWidget
        self.tm_scroll: QScrollArea
        self.tm_container: QWidget
        self.tm_cards_layout: QVBoxLayout
        self.tm_threshold_chip: QPushButton
        self.tm_threshold_state: QLabel
        self.termbase_page: QWidget
        self.manage_terms_button: QToolButton
        self.manage_terms_menu: QMenu
        self.add_term_button: QPushButton
        self.term_scroll: QScrollArea
        self.term_container: QWidget
        self.term_cards_layout: QVBoxLayout
        self.controller = controller
        self.chunk_controller = chunk_controller
        self.tmx_export_coordinator = tmx_export_coordinator
        self._chunk_view: ChunkApplicationProjectView | None = None
        self._chunk_view_error_code: str | None = None
        self._chunk_scope_cache_key: tuple[str, int, str] | None = None
        self._chunk_identity_keys_cache: set[tuple[str, str]] | None = None
        self._chunk_manager_dialog: object | None = None
        self._chunk_segment_selection_session: (
            _ChunkSegmentSelectionSession | None
        ) = None
        self._refreshing = False
        self._display_preferences: DisplayPreferences = controller.display_preferences()
        self.segment_density = self._display_preferences.segment_density
        self.workspace_mode = self._display_preferences.workspace_mode
        self.editor_font_size = self._display_preferences.editor_font_size
        self.settings_dialog: QtSettingsDialog | None = None
        self._fuzzy_validation_timer = QTimer(self)
        self._fuzzy_validation_timer.setInterval(250)
        self._fuzzy_validation_timer.timeout.connect(
            self._poll_fuzzy_validation
        )
        self.current_suggestions = SuggestionBundle()
        self.current_tm_report: TMSuggestionReport | None = None
        self.current_project_search_report: (
            ProjectSearchReport
            | WorkspaceSearchReport
            | CollaborativeWorkspaceSearchReportV2
            | None
        ) = None
        self.current_workspace_search_report: (
            WorkspaceSearchReport | CollaborativeWorkspaceSearchReportV2 | None
        ) = None
        self._workspace_package_import_preview: object | None = None
        self._workspace_package_import_source: tuple[Path, Path | None] | None = None
        self._workspace_package_preview_text = "尚未预览导入包。"
        self._workspace_package_import_can_apply = False
        self._project_search_ordinal: int | None = None
        self._project_search_expanded = False
        self.setObjectName("editorWindow")
        self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
        self.setMinimumSize(1080, 700)
        self.resize(1440, 880)
        self._build_ui()
        self._apply_top_bar_responsiveness(self.width())
        self.setTabOrder(self.settings_button, self.project_search_toggle)
        self.setTabOrder(self.project_search_toggle, self.project_search_input)
        self.setTabOrder(self.project_search_input, self.project_search_source)
        self.setTabOrder(self.project_search_source, self.project_search_target)
        self.setTabOrder(self.project_search_target, self.project_search_speaker)
        self.setTabOrder(
            self.project_search_match_case,
            self.project_search_whole_word,
        )
        self.setTabOrder(self.project_search_speaker, self.project_search_status)
        self.setTabOrder(
            self.project_search_status,
            self.project_search_match_case,
        )
        self.setTabOrder(
            self.project_search_whole_word,
            self.project_search_clear,
        )
        self.setTabOrder(self.project_search_clear, self.project_search_button)
        self.setTabOrder(self.project_search_button, self.project_search_previous)
        self.setTabOrder(self.project_search_previous, self.project_search_next)
        self.setTabOrder(self.project_search_next, self.segment_list)
        self.setTabOrder(self.confirm_button, self.tm_threshold_chip)
        self.apply_editor_font_size(self.editor_font_size)
        self.source_display.viewport().installEventFilter(self)
        self.target_editor.viewport().installEventFilter(self)
        self._wire_actions()
        self._install_shortcuts()
        self.set_segment_density(self.segment_density, persist=False)
        self.refresh_recent_projects()
        if controller.has_active_project:
            self._render_project()
        else:
            self._show_empty_state()

    def install_chunk_controller(self, chunk_controller: object) -> None:
        """Install the optional collaboration façade after shell construction."""

        if chunk_controller is None:
            raise TypeError("chunk controller must not be None")
        if self.chunk_controller is not None and self.chunk_controller is not chunk_controller:
            raise RuntimeError("chunk controller is already installed")
        if self.chunk_controller is chunk_controller:
            return
        self.chunk_controller = chunk_controller
        if self.controller.has_active_project:
            self._refreshing = True
            try:
                self._refresh_chunk_view()
                self._render_current_segment(reset_target_history=False)
            finally:
                self._refreshing = False

    def _build_ui(self) -> None:
        shell = QWidget()
        shell.setObjectName("windowShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._build_top_bar())

        self.pages = QStackedWidget()
        self.pages.setObjectName("workspacePages")
        self.pages.addWidget(self._build_empty_page())
        self.pages.addWidget(self._build_editor_page())
        shell_layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)

        status = QStatusBar()
        status.setObjectName("editorStatusBar")
        status.setSizeGripEnabled(False)
        self.setStatusBar(status)
        self.setStyleSheet(_EDITOR_STYLE)

    def _build_top_bar(self) -> QWidget:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        self.top_bar = top_bar
        layout = QHBoxLayout(top_bar)
        self.top_bar_layout = layout
        layout.setContentsMargins(22, 12, 20, 12)
        layout.setSpacing(12)

        mark = QLabel("L")
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(QSize(34, 34))
        layout.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        self.brand_name_label = QLabel("LocalCAT")
        self.brand_name_label.setObjectName("brandName")
        self.brand_tagline_label = QLabel("LOCAL TRANSLATION WORKSPACE")
        self.brand_tagline_label.setObjectName("brandTagline")
        brand.addWidget(self.brand_name_label)
        brand.addWidget(self.brand_tagline_label)
        layout.addLayout(brand)

        self.top_separator = QFrame()
        self.top_separator.setObjectName("topSeparator")
        self.top_separator.setFrameShape(QFrame.Shape.VLine)
        layout.addWidget(self.top_separator)

        project_info = QVBoxLayout()
        project_info.setSpacing(1)
        self.project_name_label = QLabel("未打开项目")
        self.project_name_label.setObjectName("projectName")
        self.language_label = QLabel("—")
        self.language_label.setObjectName("languageDirection")
        project_info.addWidget(self.project_name_label)
        project_info.addWidget(self.language_label)
        layout.addLayout(project_info)
        layout.addStretch()

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("projectProgress")
        self.progress_bar.setFormat("%v / %m · %p%")
        self.progress_bar.setFixedWidth(180)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.project_search_toggle = _TopBarSearchButton()
        self.project_search_toggle.setObjectName("projectSearchToggle")
        self.project_search_toggle.setCheckable(True)
        self.project_search_toggle.setEnabled(False)
        self.project_search_toggle.setFixedSize(QSize(34, 34))
        self.project_search_toggle.setAccessibleName(
            "展开或收起项目搜索"
        )
        self.project_search_toggle.setToolTip("项目搜索 (Ctrl+F)")
        layout.addWidget(self.project_search_toggle)

        self.workspace_documents_button = QToolButton()
        self.workspace_documents_button.setObjectName("workspaceDocumentsButton")
        self.workspace_documents_button.setEnabled(False)
        self.workspace_documents_button.setFixedSize(QSize(34, 34))
        self.workspace_documents_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self.workspace_documents_button.setAccessibleName(
            "显示项目内文档"
        )
        self.workspace_documents_button.setToolTip("项目内文档")
        self.workspace_documents_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.workspace_documents_menu = QMenu(self.workspace_documents_button)
        self.workspace_documents_menu.setObjectName("workspaceDocumentsMenu")
        self.workspace_documents_menu.setAccessibleName("项目内文档列表")
        configure_menu(self.workspace_documents_menu)
        self.workspace_documents_button.setMenu(self.workspace_documents_menu)
        layout.addWidget(self.workspace_documents_button)

        self.workspace_mode_combo = _TopBarModeCombo()
        self.workspace_mode_combo.setObjectName("workspaceModeCombo")
        self.workspace_mode_combo.setAccessibleName("工作区模式")
        self.workspace_mode_combo.setToolTip("切换编辑或双语浏览校对模式")
        self.workspace_mode_combo.addItem("编辑", WorkspaceMode.EDIT.value)
        self.workspace_mode_combo.addItem("浏览 / 校对", WorkspaceMode.BROWSE.value)
        configure_combo_popup(
            self.workspace_mode_combo,
            object_name="workspaceModePopup",
            accessible_name="工作区模式选项",
        )
        self.workspace_mode_combo.setCurrentIndex(
            0 if self.workspace_mode is WorkspaceMode.EDIT else 1
        )
        layout.addWidget(self.workspace_mode_combo)

        self.open_button = _TopBarProjectButton()
        self.open_button.setObjectName("openProjectButton")
        self.open_button.setText("项目")
        self.open_button.setAccessibleName(
            "项目：主按钮打开项目，箭头显示更多项目操作"
        )
        self.open_button.setToolTip("打开或切换项目")
        self.open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.open_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.project_menu = QMenu(self)
        self.project_menu.setObjectName("projectMenu")
        configure_menu(self.project_menu)
        self.open_project_action = self.project_menu.addAction("打开本地项目")
        self.open_project_action.setObjectName("openLocalProjectAction")
        self.open_project_action.setToolTip(
            "选择一个文件直接打开；按住 Shift 多选时创建多文档项目"
        )
        self.open_workspace_package_action = self.project_menu.addAction(
            "打开 ProjectPackage"
        )
        self.open_workspace_package_action.setObjectName(
            "openWorkspacePackageAction"
        )
        self.open_workspace_package_action.setToolTip(
            "直接打开一个 ProjectPackage 作为当前项目"
        )
        self.import_workspace_package_action = self.project_menu.addAction(
            "预览并导入 ProjectPackage"
        )
        self.import_workspace_package_action.setObjectName(
            "importWorkspacePackageAction"
        )
        self.import_workspace_package_action.setToolTip(
            "预览并显式应用项目包；Legacy 会先选择导入目标位置，原文件不变"
        )
        self.save_workspace_document_action = self.project_menu.addAction(
            "保存当前章节"
        )
        self.save_workspace_document_action.setObjectName(
            "saveWorkspaceDocumentAction"
        )
        self.save_workspace_document_action.setEnabled(False)
        self.recent_projects_menu = self.project_menu.addMenu("最近项目")
        self.recent_projects_menu.setObjectName("recentProjectsMenu")
        configure_menu(self.recent_projects_menu)
        self.project_menu.addSeparator()
        self.chunk_scope_menu = self.project_menu.addMenu("当前分工")
        self.chunk_scope_menu.setObjectName("chunkScopeMenu")
        configure_menu(self.chunk_scope_menu)
        self.chunk_scope_menu.aboutToShow.connect(
            self._populate_chunk_scope_menu
        )
        self.chunk_manage_action = self.project_menu.addAction("协作分工管理")
        self.chunk_manage_action.setObjectName("chunkManageProjectAction")
        self.chunk_manage_action.setEnabled(False)
        self.chunk_manage_action.setToolTip(
            "创建、拆分、合并或调整当前项目的协作分工"
        )
        self.export_project_action = self.project_menu.addAction("导出项目")
        self.export_project_action.setObjectName("exportProjectTmxAction")
        self.export_project_action.setToolTip(
            "将整个项目或一个明选分工导出为 TMX"
        )
        self.export_project_action.setEnabled(False)
        self.project_menu.addSeparator()
        self.speaker_inventory_action = self.project_menu.addAction(
            "Raw speaker 盘点"
        )
        self.speaker_inventory_action.setObjectName(
            "speakerInventoryProjectAction"
        )
        self.speaker_inventory_action.setToolTip(
            "只读盘点当前 JSON 项目的 raw speaker 与出现次数"
        )
        self.preprocess_action = self.project_menu.addAction("Target 文字预处理")
        self.preprocess_action.setObjectName("preprocessProjectAction")
        self.preprocess_action.setToolTip(
            "预览并显式应用有序的 target literal 替换规则"
        )
        self.project_menu.addSeparator()
        self.close_project_action = self.project_menu.addAction("退出当前项目")
        self.project_menu.addSeparator()
        self.quit_action = self.project_menu.addAction("退出 LocalCAT")
        self.open_button.setMenu(self.project_menu)
        layout.addWidget(self.open_button)
        self.save_button = QToolButton()
        self.save_button.setObjectName("saveProjectButton")
        self.save_button.setText("保存")
        self.save_button.setToolTip("保存项目")
        self.save_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        layout.addWidget(self.save_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setText("⚙ 设置")
        self.settings_button.setToolTip("语言资源设置")
        self.settings_button.setFixedSize(QSize(72, 34))
        layout.addWidget(self.settings_button)
        return top_bar

    def _build_empty_page(self) -> QWidget:
        page = _WorkspaceDropPage()
        page.setObjectName("emptyPage")
        page.setAccessibleName("多文档项目文件拖放区")
        page.explicitFilesDropped.connect(self._queue_workspace_drop_selection)
        self.empty_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(80, 60, 80, 80)
        layout.addStretch()
        card = QFrame()
        card.setObjectName("emptyCard")
        card.setMinimumWidth(620)
        card.setMaximumWidth(680)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(48, 42, 48, 42)
        card_layout.setSpacing(14)
        icon = QLabel("文")
        icon.setObjectName("emptyIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(58, 58)
        title = QLabel("开始本地翻译")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel(
            "打开或拖入本地文件；按住 Shift 可多选创建项目。\n"
            "只导入所选文件，不扫描文件夹或相邻文件。"
        )
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setMinimumHeight(48)
        actions = QHBoxLayout()
        actions.addStretch()
        self.empty_open_button = QPushButton("打开本地项目")
        self.empty_open_button.setObjectName("emptyOpenButton")
        self.sample_button = QPushButton("载入示例")
        self.sample_button.setObjectName("loadSampleButton")
        actions.addWidget(self.empty_open_button)
        actions.addWidget(self.sample_button)
        actions.addStretch()
        privacy = QLabel("离线优先 · 不发送项目或语言资源到网络")
        privacy.setObjectName("privacyHint")
        privacy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(title)
        card_layout.addWidget(hint)
        card_layout.addSpacing(8)
        card_layout.addLayout(actions)
        card_layout.addWidget(privacy)
        layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return page

    def _build_editor_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("editorPage")
        self.editor_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        search_panel = self._build_project_search_bar()
        layout.addWidget(search_panel)

        self.main_splitter = ResponsiveSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainWorkspaceSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(7)
        self.main_splitter.addWidget(self._build_segment_panel())
        self.main_splitter.addWidget(self._build_edit_panel())
        self.main_splitter.addWidget(self._build_suggestion_panel())
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 5)
        self.main_splitter.setStretchFactor(2, 3)
        self.main_splitter.setSizes([240, 600, 360])
        self.main_splitter.splitterMoved.connect(
            lambda _position, _index: self._schedule_layout_refresh()
        )

        self.workspace_pages = QStackedWidget()
        self.workspace_pages.setObjectName("editorWorkspacePages")
        self.workspace_pages.addWidget(self.main_splitter)
        self.workspace_pages.addWidget(self._build_browse_panel())
        layout.addWidget(self.workspace_pages, 1)
        return page

    def _build_project_search_bar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("projectSearchPanel")
        self.project_search_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(6)

        query_row = QHBoxLayout()
        query_row.setSpacing(8)
        title = QLabel("项目搜索")
        title.setObjectName("projectSearchTitle")
        self.project_search_input = QLineEdit()
        self.project_search_input.setObjectName("projectSearchQuery")
        self.project_search_input.setPlaceholderText("在当前 JSON 项目中搜索…")
        self.project_search_input.setClearButtonEnabled(True)
        self.project_search_input.setAccessibleName("项目搜索关键词")
        self.project_search_input.setToolTip(
            "输入非空关键词并按 Enter 搜索当前 JSON 项目"
        )
        self.project_search_button = QPushButton("搜索")
        self.project_search_button.setObjectName("projectSearchSubmit")
        self.project_search_button.setAccessibleName("执行项目搜索")
        self.project_search_button.setToolTip("执行当前项目搜索")
        self.project_search_clear = QPushButton("清除")
        self.project_search_clear.setObjectName("projectSearchClear")
        self.project_search_clear.setAccessibleName(
            "清除项目搜索关键词与结果"
        )
        self.project_search_clear.setToolTip(
            "清除关键词、可见结果和已签发结果；保留搜索条件"
        )
        query_row.addWidget(title)
        query_row.addWidget(self.project_search_input, 1)
        query_row.addWidget(self.project_search_clear)
        query_row.addWidget(self.project_search_button)
        layout.addLayout(query_row)

        options_row = QHBoxLayout()
        options_row.setSpacing(10)
        scope = QLabel("范围")
        scope.setObjectName("projectSearchScopeLabel")
        self.project_search_source = self._project_search_checkbox(
            "Source",
            "projectSearchSource",
            "搜索 source 字段",
            checked=True,
        )
        self.project_search_target = self._project_search_checkbox(
            "Target",
            "projectSearchTarget",
            "搜索 target 字段",
            checked=True,
        )
        self.project_search_speaker = self._project_search_checkbox(
            "Speaker",
            "projectSearchSpeaker",
            "搜索 raw speaker 字段",
            checked=True,
        )
        status_label = QLabel("状态")
        status_label.setObjectName("projectSearchStatusLabel")
        self.project_search_status = QComboBox()
        self.project_search_status.setObjectName("projectSearchStatus")
        self.project_search_status.setAccessibleName("段翻译状态筛选")
        self.project_search_status.setToolTip(
            "按未填写、草稿或已翻译筛选段；仍需输入关键词"
        )
        self.project_search_status.addItem("全部状态", None)
        self.project_search_status.addItem(
            "未填写",
            SegmentTranslationStatus.UNFILLED.value,
        )
        self.project_search_status.addItem(
            "草稿",
            SegmentTranslationStatus.DRAFT.value,
        )
        self.project_search_status.addItem(
            "已翻译",
            SegmentTranslationStatus.TRANSLATED.value,
        )
        configure_combo_popup(
            self.project_search_status,
            object_name="projectSearchStatusPopup",
            accessible_name="段翻译状态选项",
        )
        self.workspace_search_scope = QComboBox()
        self.workspace_search_scope.setObjectName("workspaceSearchScope")
        self.workspace_search_scope.setAccessibleName("多文档搜索范围")
        self.workspace_search_scope.addItem(
            "当前章节",
            SearchScope.CURRENT_DOCUMENT,
        )
        self.workspace_search_scope.addItem(
            "搜索全部章节",
            SearchScope.ENTIRE_PROJECT,
        )
        self.workspace_search_scope.setVisible(False)
        self.project_search_scope = self.workspace_search_scope
        configure_combo_popup(
            self.workspace_search_scope,
            object_name="workspaceSearchScopePopup",
            accessible_name="多文档搜索范围选项",
        )
        self.project_search_match_case = self._project_search_checkbox(
            "Match Case",
            "projectSearchMatchCase",
            "区分大小写；仅在 TEXT_V1 已验证时参与搜索",
        )
        self.project_search_whole_word = self._project_search_checkbox(
            "Whole Word",
            "projectSearchWholeWord",
            "全词匹配；纯 CJK 语义由 Core 决定",
        )
        self.project_search_previous = QPushButton("←")
        self.project_search_previous.setObjectName("projectSearchPrevious")
        self.project_search_previous.setAccessibleName("上一个搜索结果")
        self.project_search_previous.setToolTip(
            "上一个搜索结果；位于第一个结果时停用"
        )
        self.project_search_next = QPushButton("→")
        self.project_search_next.setObjectName("projectSearchNext")
        self.project_search_next.setAccessibleName("下一个搜索结果")
        self.project_search_next.setToolTip(
            "下一个搜索结果；位于最后一个结果时停用"
        )
        options_row.addWidget(scope)
        options_row.addWidget(self.project_search_source)
        options_row.addWidget(self.project_search_target)
        options_row.addWidget(self.project_search_speaker)
        options_row.addSpacing(8)
        options_row.addWidget(status_label)
        options_row.addWidget(self.project_search_status)
        options_row.addWidget(self.project_search_scope)
        options_row.addSpacing(8)
        options_row.addWidget(self.project_search_match_case)
        options_row.addWidget(self.project_search_whole_word)
        options_row.addStretch()
        options_row.addWidget(self.project_search_previous)
        options_row.addWidget(self.project_search_next)
        layout.addLayout(options_row)

        feedback_row = QHBoxLayout()
        feedback_row.setSpacing(12)
        self.project_search_capability = QLabel("搜索能力尚未读取。")
        self.project_search_capability.setObjectName("projectSearchCapability")
        self.project_search_capability.setTextFormat(Qt.TextFormat.PlainText)
        self.project_search_capability.setAccessibleName("项目搜索能力状态")
        self.project_search_capability.setToolTip(
            "显示当前项目与 Core TextMatcher 的真实可用状态"
        )
        self.project_search_result = QLabel("尚未搜索。")
        self.project_search_result.setObjectName("projectSearchResult")
        self.project_search_result.setTextFormat(Qt.TextFormat.PlainText)
        self.project_search_result.setAccessibleName("项目搜索结果状态：尚未搜索")
        self.project_search_result.setToolTip(
            "显示结果总数、当前序号、命中字段和段落"
        )
        feedback_row.addWidget(self.project_search_capability, 1)
        feedback_row.addWidget(self.project_search_result)
        layout.addLayout(feedback_row)

        self.project_search_preview = QLabel("预览：—")
        self.project_search_preview.setObjectName("projectSearchPreview")
        self.project_search_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.project_search_preview.setWordWrap(True)
        self.project_search_preview.setMaximumHeight(42)
        self.project_search_preview.setAccessibleName("当前搜索结果预览：无")
        self.project_search_preview.setToolTip(
            "当前命中字段的原始纯文本预览"
        )
        layout.addWidget(self.project_search_preview)
        self.project_search_previous.setEnabled(False)
        self.project_search_next.setEnabled(False)
        panel.setVisible(False)
        return panel

    @staticmethod
    def _project_search_checkbox(
        text: str,
        object_name: str,
        description: str,
        *,
        checked: bool = False,
    ) -> QCheckBox:
        checkbox = QCheckBox(text)
        checkbox.setObjectName(object_name)
        checkbox.setChecked(checked)
        checkbox.setAccessibleName(description)
        checkbox.setToolTip(description)
        return checkbox

    def _build_segment_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("segmentPanel")
        panel.setMinimumWidth(210)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        title = QLabel("段落")
        title.setObjectName("panelTitle")
        self.segment_count_label = QLabel("0")
        self.segment_count_label.setObjectName("countBadge")
        header.addWidget(title)
        header.addWidget(self.segment_count_label)
        header.addStretch()
        self.segment_density_combo = QComboBox()
        self.segment_density_combo.setObjectName("segmentDensityCombo")
        self.segment_density_combo.setToolTip("段落列表：紧凑等高或完整自动换行")
        self.segment_density_combo.addItem("紧凑", SegmentDensity.COMPACT.value)
        self.segment_density_combo.addItem("自动换行", SegmentDensity.WRAPPED.value)
        configure_combo_popup(
            self.segment_density_combo,
            object_name="segmentDensityPopup",
            accessible_name="段落显示密度选项",
        )
        self.segment_density_combo.setCurrentIndex(
            0 if self.segment_density is SegmentDensity.COMPACT else 1
        )
        header.addWidget(self.segment_density_combo)
        layout.addLayout(header)

        self.workspace_chapter_title = QLabel("—")
        self.workspace_chapter_title.setObjectName("workspaceChapterTitle")
        self.workspace_chapter_title.setTextFormat(Qt.TextFormat.PlainText)
        self.workspace_chapter_title.setVisible(False)
        self.chapter_progress_label = self.workspace_chapter_title
        layout.addWidget(self.workspace_chapter_title)
        self.unconfirmed_filter = QCheckBox("仅显示未确认")
        self.unconfirmed_filter.setObjectName("unconfirmedFilter")
        layout.addWidget(self.unconfirmed_filter)
        self.workspace_save_feedback = QLabel("项目包尚未保存。")
        self.workspace_save_feedback.setObjectName("workspaceSaveFeedback")
        self.workspace_save_feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.workspace_save_feedback.setWordWrap(True)
        self.workspace_save_feedback.setVisible(False)
        layout.addWidget(self.workspace_save_feedback)
        self.segment_list = QListWidget()
        self.segment_list.setObjectName("segmentList")
        self.segment_list.setSpacing(3)
        layout.addWidget(self.segment_list, 1)
        return panel

    def _build_browse_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("browsePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("浏览 / 校对")
        title.setObjectName("panelTitle")
        self.browse_hint = QLabel("双语全文只读浏览 · 双击任一行返回同段编辑")
        self.browse_hint.setObjectName("browseHint")
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(self.browse_hint)
        header.addStretch()
        self.browse_group_button = QPushButton("分组轮次")
        self.browse_group_button.setObjectName("browseGroupNavigatorButton")
        self.browse_group_button.setAccessibleName("浏览分组轮次")
        self.browse_group_button.setEnabled(False)
        header.addWidget(self.browse_group_button)
        self.workspace_browse_chapter_title = QLabel("—")
        self.workspace_browse_chapter_title.setObjectName(
            "workspaceBrowseChapterTitle"
        )
        self.workspace_browse_chapter_title.setTextFormat(Qt.TextFormat.PlainText)
        self.workspace_browse_chapter_title.setVisible(False)
        header.addWidget(self.workspace_browse_chapter_title)
        layout.addLayout(header)

        self.workspace_browse_save_feedback = QLabel("项目包尚未保存。")
        self.workspace_browse_save_feedback.setObjectName(
            "workspaceBrowseSaveFeedback"
        )
        self.workspace_browse_save_feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.workspace_browse_save_feedback.setWordWrap(True)
        self.workspace_browse_save_feedback.setVisible(False)
        layout.addWidget(self.workspace_browse_save_feedback)

        self.chunk_segment_selection_bar = QFrame()
        self.chunk_segment_selection_bar.setObjectName(
            "chunkSegmentSelectionBar"
        )
        self.chunk_segment_selection_bar.setAccessibleName(
            "高级分工段落选择"
        )
        selection_layout = QVBoxLayout(self.chunk_segment_selection_bar)
        selection_layout.setContentsMargins(14, 10, 14, 10)
        selection_layout.setSpacing(8)
        selection_heading = QHBoxLayout()
        self.chunk_segment_selection_title = QLabel("选择分工段落")
        self.chunk_segment_selection_title.setObjectName(
            "chunkSegmentSelectionTitle"
        )
        self.chunk_segment_selection_status = QLabel("未选择段落")
        self.chunk_segment_selection_status.setObjectName(
            "chunkSegmentSelectionStatus"
        )
        self.chunk_segment_selection_status.setWordWrap(True)
        selection_heading.addWidget(self.chunk_segment_selection_title)
        selection_heading.addSpacing(12)
        selection_heading.addWidget(self.chunk_segment_selection_status, 1)
        selection_layout.addLayout(selection_heading)
        selection_actions = QHBoxLayout()
        self.chunk_segment_range_start = QPushButton("设为起点")
        self.chunk_segment_range_start.setObjectName("chunkBrowseRangeStart")
        self.chunk_segment_range_start.setAccessibleName(
            "将当前浏览段落设为选择起点"
        )
        self.chunk_segment_range_end = QPushButton("设为终点")
        self.chunk_segment_range_end.setObjectName("chunkBrowseRangeEnd")
        self.chunk_segment_range_end.setAccessibleName(
            "将当前浏览段落设为选择终点"
        )
        self.chunk_segment_bulk_select = QPushButton("选择全部尚未分工")
        self.chunk_segment_bulk_select.setObjectName("chunkBrowseBulkSelect")
        self.chunk_segment_bulk_select.setAccessibleName(
            "选择全部尚未归入分工的段落"
        )
        self.chunk_segment_bulk_select.setToolTip(
            "选择尚未归入任何分工、且仍存在于当前项目的段落；"
            "与未翻译或未确认状态无关"
        )
        self.chunk_segment_clear = QPushButton("清除")
        self.chunk_segment_clear.setObjectName("chunkBrowseSelectionClear")
        self.chunk_segment_clear.setAccessibleName("清除高级分工段落选择")
        self.chunk_segment_cancel = QPushButton("取消")
        self.chunk_segment_cancel.setObjectName("chunkBrowseSelectionCancel")
        self.chunk_segment_cancel.setAccessibleName("取消高级分工段落选择")
        self.chunk_segment_done = QPushButton("使用所选段落")
        self.chunk_segment_done.setObjectName("chunkBrowseSelectionDone")
        self.chunk_segment_done.setAccessibleName("返回所选高级分工段落")
        selection_actions.addWidget(self.chunk_segment_range_start)
        selection_actions.addWidget(self.chunk_segment_range_end)
        selection_actions.addWidget(self.chunk_segment_bulk_select)
        selection_actions.addWidget(self.chunk_segment_clear)
        selection_actions.addStretch(1)
        selection_actions.addWidget(self.chunk_segment_cancel)
        selection_actions.addWidget(self.chunk_segment_done)
        selection_layout.addLayout(selection_actions)
        self.chunk_segment_selection_bar.hide()
        layout.addWidget(self.chunk_segment_selection_bar)

        self.browse_table = QTableWidget(0, 5)
        self.browse_table.setObjectName("browseTable")
        self.browse_table.setHorizontalHeaderLabels(
            ["段落", "SOURCE", "TARGET", "SPEAKER", "状态"]
        )
        self.browse_table.setToolTip("只读浏览段落 source、target 与 raw speaker")
        self.browse_table.setAccessibleName("段落双语与 raw speaker 浏览表")
        self.browse_table.setWordWrap(True)
        self.browse_table.setShowGrid(False)
        self.browse_table.setAlternatingRowColors(True)
        self.browse_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.browse_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.browse_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.browse_table.viewport().installEventFilter(self)
        self.browse_table.verticalHeader().setVisible(False)
        browse_header = self.browse_table.horizontalHeader()
        browse_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        browse_header.resizeSection(0, 72)
        browse_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        browse_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        browse_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        browse_header.resizeSection(3, 140)
        browse_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        browse_body = QHBoxLayout()
        browse_body.setContentsMargins(0, 0, 0, 0)
        browse_body.setSpacing(8)
        self.browse_group_turn_bar = BrowseGroupTurnBar()
        browse_body.addWidget(self.browse_group_turn_bar)
        browse_body.addWidget(self.browse_table, 1)
        layout.addLayout(browse_body, 1)
        return panel

    def _build_edit_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("editPanel")
        panel.setMinimumWidth(380)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        source_header = QHBoxLayout()
        source_title = QLabel("SOURCE")
        source_title.setObjectName("sectionEyebrow")
        self.segment_position_label = QLabel("—")
        self.segment_position_label.setObjectName("segmentPosition")
        source_header.addWidget(source_title)
        source_header.addStretch()
        source_header.addWidget(self.segment_position_label)
        layout.addLayout(source_header)
        speaker_row = QHBoxLayout()
        speaker_title = QLabel("SPEAKER")
        speaker_title.setObjectName("speakerEyebrow")
        speaker_title.setToolTip("当前段的规范化 raw speaker")
        speaker_title.setAccessibleName("Speaker 标签")
        self.speaker_display = QLabel("无 speaker")
        self.speaker_display.setObjectName("speakerDisplay")
        self.speaker_display.setTextFormat(Qt.TextFormat.PlainText)
        self.speaker_display.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.speaker_display.setWordWrap(True)
        self.speaker_display.setToolTip(
            "当前段规范化后的 raw speaker；空值显示“无 speaker”"
        )
        self.speaker_display.setAccessibleName("当前段 raw speaker：无 speaker")
        speaker_row.addWidget(speaker_title)
        speaker_row.addSpacing(8)
        speaker_row.addWidget(self.speaker_display, 1)
        layout.addLayout(speaker_row)
        self.source_display = QTextBrowser()
        self.source_display.setObjectName("sourceDisplay")
        self.source_display.setOpenExternalLinks(False)
        self.source_display.setMinimumHeight(155)
        layout.addWidget(self.source_display, 2)

        target_header = QHBoxLayout()
        target_title = QLabel("TARGET")
        target_title.setObjectName("sectionEyebrow")
        self.confirmation_label = QLabel("待确认")
        self.confirmation_label.setObjectName("confirmationState")
        target_header.addWidget(target_title)
        target_header.addStretch()
        target_header.addWidget(self.confirmation_label)
        layout.addLayout(target_header)
        self.target_editor = QTextEdit()
        self.target_editor.setObjectName("targetEditor")
        self.target_editor.setPlaceholderText("在此输入译文…")
        self.target_editor.setMinimumHeight(190)
        layout.addWidget(self.target_editor, 3)

        actions = QHBoxLayout()
        self.previous_button = QPushButton("← 上一段")
        self.previous_button.setObjectName("previousSegmentButton")
        self.previous_button.setToolTip("上一段")
        self.next_button = QPushButton("下一段 →")
        self.next_button.setObjectName("nextSegmentButton")
        self.next_button.setToolTip("下一段")
        self.confirm_button = QPushButton("确认译文")
        self.confirm_button.setObjectName("confirmTranslationButton")
        self.confirm_button.setToolTip("确认译文并前往下一未确认段")
        actions.addWidget(self.previous_button)
        actions.addWidget(self.next_button)
        actions.addStretch()
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)
        return panel

    def _build_suggestion_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("suggestionPanel")
        panel.setMinimumWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("语言资源")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.suggestion_tabs = QTabWidget()
        self.suggestion_tabs.setObjectName("suggestionTabs")
        self.translation_matches_page = QWidget()
        self.translation_matches_page.setObjectName("translationMatchesPage")
        matches_layout = QVBoxLayout(self.translation_matches_page)
        matches_layout.setContentsMargins(0, 0, 0, 0)
        threshold_row = QHBoxLayout()
        threshold_row.setContentsMargins(10, 8, 10, 4)
        threshold_row.setSpacing(8)
        self.tm_threshold_state = QLabel()
        self.tm_threshold_state.setObjectName("tmThresholdState")
        self.tm_threshold_state.setWordWrap(True)
        threshold_row.addWidget(self.tm_threshold_state, 1)
        self.tm_threshold_chip = TMThresholdButton()
        self.tm_threshold_chip.setObjectName("tmThresholdChip")
        self.tm_threshold_chip.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tm_threshold_chip.clicked.connect(self._request_tm_threshold_update)
        threshold_row.addWidget(self.tm_threshold_chip)
        matches_layout.addLayout(threshold_row)
        self.tm_scroll = QScrollArea()
        self.tm_scroll.setObjectName("tmSuggestionsScroll")
        self.tm_scroll.setWidgetResizable(True)
        self.tm_container = QWidget()
        self.tm_cards_layout = QVBoxLayout(self.tm_container)
        self.tm_cards_layout.setContentsMargins(10, 10, 10, 10)
        self.tm_cards_layout.setSpacing(9)
        self.tm_scroll.setWidget(self.tm_container)
        matches_layout.addWidget(self.tm_scroll)

        self.termbase_page = QWidget()
        self.termbase_page.setObjectName("termbasePage")
        terms_layout = QVBoxLayout(self.termbase_page)
        terms_layout.setContentsMargins(0, 0, 0, 0)
        term_toolbar = QHBoxLayout()
        term_toolbar.addStretch()
        self.manage_terms_button = _InlineMenuButton()
        self.manage_terms_button.setObjectName("manageTermsButton")
        self.manage_terms_button.setText("管理术语")
        self.manage_terms_button.setAccessibleName("管理术语")
        self.manage_terms_button.setToolTip(
            "选择一个 Active+Update 术语表并打开集中式术语管理"
        )
        self.manage_terms_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.manage_terms_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.manage_terms_menu = QMenu(self.manage_terms_button)
        self.manage_terms_menu.setObjectName("manageTermsMenu")
        self.manage_terms_menu.setAccessibleName("可管理术语表")
        configure_menu(self.manage_terms_menu)
        self.manage_terms_button.setMenu(self.manage_terms_menu)
        term_toolbar.addWidget(self.manage_terms_button)
        self.add_term_button = QPushButton("＋ 添加术语")
        self.add_term_button.setObjectName("addTermButton")
        term_toolbar.addWidget(self.add_term_button)
        terms_layout.addLayout(term_toolbar)
        self.term_scroll = QScrollArea()
        self.term_scroll.setObjectName("termSuggestionsScroll")
        self.term_scroll.setWidgetResizable(True)
        self.term_container = QWidget()
        self.term_cards_layout = QVBoxLayout(self.term_container)
        self.term_cards_layout.setContentsMargins(10, 10, 10, 10)
        self.term_cards_layout.setSpacing(9)
        self.term_scroll.setWidget(self.term_container)
        terms_layout.addWidget(self.term_scroll, 1)
        self.suggestion_tabs.addTab(self.translation_matches_page, "Translation Matches")
        self.suggestion_tabs.addTab(self.termbase_page, "Termbase")
        layout.addWidget(self.suggestion_tabs, 1)
        self._refresh_manage_terms_menu()
        self._refresh_tm_threshold_entry()
        return panel

    def _wire_actions(self) -> None:
        self.open_button.clicked.connect(self._choose_open_home)
        self.open_project_action.triggered.connect(self._choose_open_home)
        self.open_workspace_package_action.triggered.connect(
            self._choose_open_workspace_package
        )
        self.import_workspace_package_action.triggered.connect(
            self._choose_import_workspace_package
        )
        self.save_workspace_document_action.triggered.connect(
            self.save_workspace_current_document
        )
        self.workspace_documents_menu.triggered.connect(
            self._workspace_document_action_triggered
        )
        self.chunk_scope_menu.triggered.connect(
            self._chunk_scope_action_triggered
        )
        self.chunk_manage_action.triggered.connect(
            self._open_chunk_manager
        )
        self.export_project_action.triggered.connect(
            self._open_tmx_project_export_dialog
        )
        self.project_menu.aboutToShow.connect(
            self._refresh_project_chunk_menu
        )
        self.speaker_inventory_action.triggered.connect(
            self._open_speaker_inventory_dialog
        )
        self.preprocess_action.triggered.connect(self._open_preprocess_dialog)
        self.close_project_action.triggered.connect(self.close_current_project)
        self.quit_action.triggered.connect(self.close)
        self.empty_open_button.clicked.connect(self._choose_open_home)
        self.sample_button.clicked.connect(self.load_sample)
        self.save_button.clicked.connect(self._choose_save)
        self.settings_button.clicked.connect(self._open_settings)
        self.project_search_toggle.toggled.connect(
            self._project_search_toggled
        )
        self.segment_list.currentRowChanged.connect(self._select_visible_row)
        self.target_editor.textChanged.connect(self._target_changed)
        self.previous_button.clicked.connect(lambda: self._navigate(-1))
        self.next_button.clicked.connect(lambda: self._navigate(1))
        self.confirm_button.clicked.connect(self.confirm_current)
        self.add_term_button.clicked.connect(self._prompt_add_term)
        self.unconfirmed_filter.toggled.connect(self._filter_changed)
        self.segment_density_combo.currentIndexChanged.connect(
            self._segment_density_changed
        )
        self.workspace_mode_combo.currentIndexChanged.connect(
            self._workspace_mode_changed
        )
        self.browse_group_button.clicked.connect(
            self._open_browse_group_dialog
        )
        self.browse_group_turn_bar.groupSelected.connect(
            self._navigate_browse_group
        )
        self.browse_table.cellDoubleClicked.connect(self._activate_browse_row)
        self.browse_table.currentCellChanged.connect(
            self._browse_current_cell_changed
        )
        self.browse_table.itemSelectionChanged.connect(
            self._chunk_segment_browse_selection_changed
        )
        self.chunk_segment_range_start.clicked.connect(
            lambda: self._set_chunk_segment_range_endpoint("start")
        )
        self.chunk_segment_range_end.clicked.connect(
            lambda: self._set_chunk_segment_range_endpoint("end")
        )
        self.chunk_segment_bulk_select.clicked.connect(
            self._select_chunk_segment_bulk_scope
        )
        self.chunk_segment_clear.clicked.connect(
            self._clear_chunk_segment_browse_selection
        )
        self.chunk_segment_cancel.clicked.connect(
            lambda: self._finish_chunk_segment_selection(False)
        )
        self.chunk_segment_done.clicked.connect(
            lambda: self._finish_chunk_segment_selection(True)
        )
        self.project_search_input.returnPressed.connect(
            self._submit_project_search
        )
        self.project_search_button.clicked.connect(
            self._submit_project_search
        )
        self.project_search_clear.clicked.connect(
            self._clear_project_search
        )
        self.project_search_input.textEdited.connect(
            self._project_search_criteria_changed
        )
        for checkbox in (
            self.project_search_source,
            self.project_search_target,
            self.project_search_speaker,
            self.project_search_match_case,
            self.project_search_whole_word,
        ):
            checkbox.toggled.connect(self._project_search_criteria_changed)
        self.project_search_status.currentIndexChanged.connect(
            self._project_search_criteria_changed
        )
        self.project_search_scope.currentIndexChanged.connect(
            self._project_search_criteria_changed
        )
        self.project_search_previous.clicked.connect(
            lambda: self._navigate_project_search(-1)
        )
        self.project_search_next.clicked.connect(
            lambda: self._navigate_project_search(1)
        )

    def _install_shortcuts(self) -> None:
        # On macOS Qt's portable ``Ctrl`` maps to the Command key.  The
        # suggestion-tab contract deliberately uses the physical Control key,
        # so spell that modifier as ``Meta`` there.  Workspace-mode shortcuts
        # remain platform-primary Command+1/2 through portable ``Ctrl``.
        physical_control = "Meta" if sys.platform == "darwin" else "Ctrl"
        bindings = (
            ("open", ("Ctrl+O",), self._choose_open_home),
            ("save", ("Ctrl+S",), self._choose_save),
            (
                "confirm",
                ("Ctrl+Return", "Ctrl+Enter"),
                self.confirm_current,
            ),
            ("previous", ("Alt+Up",), lambda: self._navigate(-1)),
            ("next", ("Alt+Down",), lambda: self._navigate(1)),
            ("settings", ("Ctrl+,",), self._open_settings),
            ("close_project", ("Ctrl+Shift+W",), self.close_current_project),
            ("quit", ("Ctrl+Q",), self.close),
            (
                "suggestion_tab_next",
                (f"{physical_control}+Tab",),
                lambda: self._cycle_suggestion_tab(1),
            ),
            (
                "suggestion_tab_previous",
                (f"{physical_control}+Shift+Tab",),
                lambda: self._cycle_suggestion_tab(-1),
            ),
            (
                "workspace_edit",
                ("Ctrl+1",),
                lambda: self._set_workspace_mode_from_shortcut(WorkspaceMode.EDIT),
            ),
            (
                "workspace_browse",
                ("Ctrl+2",),
                lambda: self._set_workspace_mode_from_shortcut(WorkspaceMode.BROWSE),
            ),
            (
                "segment_density_toggle",
                ("Ctrl+Shift+L",),
                self._toggle_segment_density_from_shortcut,
            ),
        )
        self.shortcuts: dict[str, QShortcut] = {}
        for name, sequences, callback in bindings:
            keys = [QKeySequence(sequence) for sequence in sequences]
            shortcut = QShortcut(keys[0], self)
            shortcut.setKeys(keys)
            shortcut.setObjectName(f"{name}Shortcut")
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts[name] = shortcut
        self.project_search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.project_search_shortcut.setObjectName("projectSearchToggleShortcut")
        self.project_search_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self.project_search_shortcut.activated.connect(
            self._toggle_project_search_shortcut
        )
        self._install_target_editor_shortcuts()
        self._update_shortcut_tooltips()

    def _cycle_suggestion_tab(self, direction: int) -> None:
        if not self._has_active_project() or not self.suggestion_tabs.isEnabled():
            return
        count = self.suggestion_tabs.count()
        if count > 1:
            self.suggestion_tabs.setCurrentIndex(
                (self.suggestion_tabs.currentIndex() + direction) % count
            )

    def _set_workspace_mode_from_shortcut(self, mode: WorkspaceMode) -> None:
        if not self._has_active_project() or not self.workspace_mode_combo.isEnabled():
            return
        self.set_workspace_mode(mode)

    def _toggle_segment_density_from_shortcut(self) -> None:
        if not self._has_active_project() or not self.segment_density_combo.isEnabled():
            return
        target = (
            SegmentDensity.WRAPPED
            if self.segment_density is SegmentDensity.COMPACT
            else SegmentDensity.COMPACT
        )
        self.set_segment_density(target)

    @staticmethod
    def _native_shortcut_text(shortcut: QShortcut) -> str:
        return " / ".join(
            key.toString(QKeySequence.SequenceFormat.NativeText)
            for key in shortcut.keys()
        )

    def _update_shortcut_tooltips(self) -> None:
        self.open_button.setToolTip(
            f"打开或切换项目 ({self._native_shortcut_text(self.shortcuts['open'])})"
        )
        self.save_button.setToolTip(
            f"保存项目 ({self._native_shortcut_text(self.shortcuts['save'])})"
        )
        self.settings_button.setToolTip(
            f"语言资源设置 ({self._native_shortcut_text(self.shortcuts['settings'])})"
        )
        self.confirm_button.setToolTip(
            "确认译文并前往下一未确认段 "
            f"({self._native_shortcut_text(self.shortcuts['confirm'])})"
        )
        self.previous_button.setToolTip(
            f"上一段 ({self._native_shortcut_text(self.shortcuts['previous'])})"
        )
        self.next_button.setToolTip(
            f"下一段 ({self._native_shortcut_text(self.shortcuts['next'])})"
        )
        next_tab = self._native_shortcut_text(
            self.shortcuts["suggestion_tab_next"]
        )
        previous_tab = self._native_shortcut_text(
            self.shortcuts["suggestion_tab_previous"]
        )
        self.suggestion_tabs.setAccessibleName(
            f"语言资源：{next_tab} / {previous_tab} 切换 Translation Matches 与 Termbase"
        )
        for index in range(self.suggestion_tabs.count()):
            self.suggestion_tabs.setTabToolTip(
                index,
                f"{self.suggestion_tabs.tabText(index)} ({next_tab} / {previous_tab})",
            )
        self.workspace_mode_combo.setToolTip(
            "切换编辑或双语浏览校对模式 "
            f"(编辑 {self._native_shortcut_text(self.shortcuts['workspace_edit'])} / "
            f"校对 {self._native_shortcut_text(self.shortcuts['workspace_browse'])})"
        )
        self.segment_density_combo.setToolTip(
            "段落列表：紧凑等高或完整自动换行 "
            f"({self._native_shortcut_text(self.shortcuts['segment_density_toggle'])})"
        )
        project_search_shortcut = self._native_shortcut_text(
            self.project_search_shortcut
        )
        self.project_search_toggle.setToolTip(
            f"展开或收起项目搜索 ({project_search_shortcut})"
        )
        self.workspace_documents_button.setToolTip("显示项目内文档")
        self.project_search_input.setToolTip(
            f"输入非空关键词并按 Enter 搜索当前 JSON 项目 ({project_search_shortcut})"
        )
        self.project_search_button.setToolTip(
            f"执行当前项目搜索；{project_search_shortcut} 聚焦关键词"
        )

    def _toggle_project_search_shortcut(self) -> None:
        if not self._has_active_project():
            return
        if self.project_search_panel.isVisible():
            self._set_project_search_expanded(False)
            if self.workspace_mode is WorkspaceMode.EDIT:
                self.target_editor.setFocus(
                    Qt.FocusReason.ShortcutFocusReason
                )
            else:
                self.browse_table.setFocus(
                    Qt.FocusReason.ShortcutFocusReason
                )
            return
        self._set_project_search_expanded(True, focus=True)

    def _project_search_toggled(self, expanded: bool) -> None:
        self._set_project_search_expanded(expanded, focus=expanded)

    def _set_project_search_expanded(
        self,
        expanded: bool,
        *,
        focus: bool = False,
    ) -> None:
        visible = bool(expanded and self._has_active_project())
        self._project_search_expanded = visible
        self.source_display.setMinimumHeight(96 if visible else 155)
        self.target_editor.setMinimumHeight(100 if visible else 190)
        self.project_search_panel.setVisible(visible)
        blocker = QSignalBlocker(self.project_search_toggle)
        try:
            self.project_search_toggle.setChecked(visible)
        finally:
            del blocker
        if visible and focus:
            self.project_search_input.setFocus(
                Qt.FocusReason.ShortcutFocusReason
            )
            self.project_search_input.selectAll()

    def _project_search_criteria_changed(self, _value: object) -> None:
        if self._chunk_segment_selection_session is not None:
            return
        if self._refreshing:
            return
        if self.current_project_search_report is None:
            if self.controller.has_workspace:
                self.controller.clear_workspace_search()
            else:
                self.controller.clear_project_search()
            return
        self._clear_project_search_results(
            "搜索条件已变化；请重新搜索。"
        )

    def _clear_project_search(self) -> None:
        if self._chunk_segment_selection_session is not None:
            return
        if self.controller.has_workspace:
            self.controller.clear_workspace_search()
        else:
            self.controller.clear_project_search()
        blocker = QSignalBlocker(self.project_search_input)
        try:
            self.project_search_input.clear()
        finally:
            del blocker
        self._clear_project_search_results(clear_controller=False)
        self.project_search_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _install_target_editor_shortcuts(self) -> None:
        bindings = (
            (
                "targetUndoShortcut",
                "Ctrl+Z",
                "撤销译文框最近一次文本编辑",
                self.target_editor.undo,
            ),
            (
                "targetRedoShortcut",
                "Ctrl+Y",
                "重做译文框最近一次文本编辑",
                self.target_editor.redo,
            ),
            (
                "targetAlternateRedoShortcut",
                "Ctrl+Shift+Z",
                "重做译文框最近一次文本编辑",
                self.target_editor.redo,
            ),
        )
        self.target_editor_shortcuts: dict[str, QShortcut] = {}
        for object_name, sequence, description, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self.target_editor)
            shortcut.setObjectName(object_name)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.setWhatsThis(description)
            shortcut.activated.connect(callback)
            self.target_editor_shortcuts[object_name] = shortcut
        if sys.platform == "darwin":
            for key_name in ("Return", "Enter"):
                object_name = f"targetPhysicalControl{key_name}Shortcut"
                shortcut = QShortcut(
                    QKeySequence(f"Meta+{key_name}"),
                    self.target_editor,
                )
                shortcut.setObjectName(object_name)
                shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                shortcut.setWhatsThis("在译文框中插入换行")
                shortcut.activated.connect(
                    lambda: self.target_editor.insertPlainText("\n")
                )
                self.target_editor_shortcuts[object_name] = shortcut

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Handle editor zoom and deterministic Browse/Review range selection."""

        session = self._chunk_segment_selection_session
        if (
            session is not None
            and watched is self.browse_table.viewport()
            and event.type() is QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            index = self.browse_table.indexAt(event.position().toPoint())
            if index.isValid() and self._chunk_segment_identity_for_row(
                index.row()
            ) is not None:
                row = index.row()
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    anchor = session.selection_anchor_row
                    if anchor is None:
                        current = self.browse_table.currentRow()
                        anchor = row if current < 0 else current
                    first, last = sorted((anchor, row))
                    selection = self.browse_table.selectionModel()
                    selection.clearSelection()
                    flags = (
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows
                    )
                    for candidate in range(first, last + 1):
                        if self._chunk_segment_identity_for_row(candidate) is None:
                            continue
                        selection.select(
                            self.browse_table.model().index(candidate, 0),
                            flags,
                        )
                    selection.setCurrentIndex(
                        self.browse_table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
                    return True
                session.selection_anchor_row = row

        editor_viewports = (
            self.source_display.viewport(),
            self.target_editor.viewport(),
        )
        if (
            watched in editor_viewports
            and self.workspace_mode is WorkspaceMode.EDIT
            and event.type() is QEvent.Type.Wheel
        ):
            wheel_event = event
            if (
                isinstance(wheel_event, QWheelEvent)
                and wheel_event.modifiers()
                == Qt.KeyboardModifier.ControlModifier
            ):
                vertical_delta = wheel_event.angleDelta().y()
                if vertical_delta:
                    direction = 1 if vertical_delta > 0 else -1
                    self.set_editor_font_size(
                        self.editor_font_size
                        + direction * EDITOR_FONT_SIZE_STEP
                    )
                    wheel_event.accept()
                    return True
        return super().eventFilter(watched, event)

    def apply_editor_font_size(self, size: int) -> None:
        """Apply one validated pixel size to both editor documents."""

        for widget in (self.source_display, self.target_editor):
            widget_font = widget.font()
            widget_font.setPixelSize(size)
            widget.setFont(widget_font)
            document_font = widget.document().defaultFont()
            document_font.setPixelSize(size)
            widget.document().setDefaultFont(document_font)
        self.editor_font_size = size

    def set_editor_font_size(
        self,
        size: int,
        *,
        persist: bool = True,
    ) -> bool:
        """Clamp, apply and optionally persist the local editor font size."""

        if not isinstance(size, int) or isinstance(size, bool):
            self._show_error("无法调整编辑字号", "编辑字号必须是整数。")
            return False
        normalized = max(MIN_EDITOR_FONT_SIZE, min(MAX_EDITOR_FONT_SIZE, size))
        if normalized == self.editor_font_size:
            return True

        self.apply_editor_font_size(normalized)
        preferences = replace(
            self._display_preferences,
            editor_font_size=normalized,
        )
        if not persist:
            self._display_preferences = preferences
            return True
        try:
            saved_preferences = self.controller.update_display_preferences(preferences)
        except EditorControllerError as exc:
            self._show_error("字号偏好未保存", str(exc))
            return False
        self._display_preferences = saved_preferences
        return True

    def _has_active_project(self) -> bool:
        return self.controller.has_active_project

    def _active_index(self) -> int:
        return (
            self.controller.workspace_global_index
            if self.controller.has_workspace
            else self.controller.current_index
        )

    def _active_segments(self) -> tuple[EditorSegment, ...]:
        if not self.controller.has_workspace:
            return self.controller.project.segments
        return tuple(
            EditorSegment(
                id=item.identity.local_segment_id,
                source=item.source,
                target=item.target,
                speaker=item.raw_speaker,
                confirmed=item.confirmed,
            )
            for item in self.controller.workspace_view.segments
        )

    def _active_project_name(self) -> str:
        return (
            self.controller.workspace_view.name
            if self.controller.has_workspace
            else self.controller.project.name
        )

    def _active_locales(self) -> tuple[str, str]:
        if self.controller.has_workspace:
            view = self.controller.workspace_view
            return view.source_locale, view.target_locale
        project = self.controller.project
        return project.source_locale, project.target_locale

    def _active_dirty(self) -> bool:
        return self.controller.active_project_dirty

    def _active_confirmed_count(self) -> int:
        if self.controller.has_workspace:
            return self.controller.workspace_project_progress.confirmed_segments
        return self.controller.confirmed_count

    def _workspace_current_document(self):
        view = self.controller.workspace_view
        current = view.current_segment.document
        return next(item for item in view.documents if item.identity is current)

    def _set_workspace_save_feedback(self, text: str) -> None:
        """Keep the edit and browse feedback projections identical."""

        self.workspace_save_feedback.setText(text)
        self.workspace_browse_save_feedback.setText(text)

    @staticmethod
    def _workspace_error_text(error: EditorControllerError | OSError) -> str:
        """Render only frozen Controller codes or a body-free I/O fallback."""

        if isinstance(error, EditorControllerError):
            candidate = str(error)
            parts = candidate.split(".")
            if (
                1 < len(parts) <= 8
                and parts[0] == "PROJECT"
                and len(candidate) <= 128
                and all(
                    part
                    and part.isascii()
                    and part == part.upper()
                    and part.replace("_", "").isalnum()
                    for part in parts
                )
            ):
                return candidate
        return "本地 I/O 操作失败；为保护项目正文，未显示底层错误内容"

    def _show_empty_state(self) -> None:
        self._set_project_search_expanded(False)
        self.pages.setCurrentIndex(0)
        self.save_button.setEnabled(False)
        self.project_name_label.setText("未打开项目")
        self.language_label.setText("—")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.close_project_action.setEnabled(False)
        self.speaker_inventory_action.setEnabled(False)
        self.preprocess_action.setEnabled(False)
        self.workspace_mode_combo.setEnabled(False)
        self.segment_density_combo.setEnabled(False)
        self.chapter_progress_label.setVisible(False)
        self.workspace_browse_chapter_title.setVisible(False)
        self.workspace_save_feedback.setVisible(False)
        self.workspace_browse_save_feedback.setVisible(False)
        self.project_search_scope.setVisible(False)
        self._chunk_view = None
        self._chunk_view_error_code = None
        self._chunk_scope_cache_key = None
        self._chunk_identity_keys_cache = None
        self.chunk_scope_menu.clear()
        self.chunk_scope_menu.setEnabled(False)
        self.chunk_manage_action.setEnabled(False)
        self.workspace_documents_button.setEnabled(False)
        self.workspace_documents_menu.clear()
        self._refresh_browse_group_button()
        self.import_workspace_package_action.setEnabled(False)
        self.save_workspace_document_action.setEnabled(False)
        self._workspace_package_import_preview = None
        self._workspace_package_import_source = None
        self._workspace_package_import_can_apply = False
        self._workspace_package_preview_text = "尚未预览导入包。"
        self._clear_project_search_results("打开 JSON 项目后可搜索。")
        self._refresh_project_search_controls()
        self.statusBar().showMessage("打开本地项目或载入示例以开始。")

    def load_sample(self) -> bool:
        if not self._confirm_unsaved():
            return False
        self.controller.load_sample()
        self._render_project()
        self.statusBar().showMessage("已载入 LocalCAT 示例项目。", 5000)
        return True

    def open_project_path(self, path: Path) -> bool:
        if not self._confirm_unsaved():
            return False
        try:
            self.controller.open_project(path)
        except (EditorControllerError, OSError, ValueError) as exc:
            self._show_error("无法打开项目", str(exc))
            self.statusBar().showMessage("项目打开失败；当前会话保持不变。", 7000)
            return False
        self._render_project()
        self.refresh_recent_projects()
        self.statusBar().showMessage(f"已打开：{path}", 5000)
        return True

    def open_workspace_package_path(self, path: Path) -> bool:
        """Cold-open a real ProjectPackage through the Controller surface."""

        if not self._confirm_unsaved():
            return False
        try:
            self.controller.open_project_package(path)
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            self._show_error("无法打开项目包", safe_error)
            self.statusBar().showMessage(
                "ProjectPackage 打开失败；当前会话保持不变。",
                7000,
            )
            return False
        self._render_project()
        self.refresh_recent_projects()
        self._set_workspace_save_feedback(
            "LocalCAT项目包已打开 · 源文件只读。"
        )
        self.statusBar().showMessage(f"已打开项目包：{path}", 5000)
        return True

    def open_project_package_path(self, path: Path) -> bool:
        """Stable C4 command name for opening a ProjectPackage."""

        return self.open_workspace_package_path(path)

    def create_workspace_from_selected_files(
        self,
        root: Path,
        selected_paths: tuple[Path, ...],
        destination: Path,
        *,
        name: str,
        source_locale: str,
        target_locale: str,
    ) -> bool:
        """Create one package from an explicit ordered selection, never a folder scan."""

        if not self._confirm_unsaved():
            return False
        try:
            result = self.controller.create_workspace_package(
                root,
                selected_paths,
                destination,
                name=name,
                source_locale=source_locale,
                target_locale=target_locale,
            )
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            self._show_error("无法新建多文档项目", safe_error)
            self.statusBar().showMessage(
                "多文档项目未创建；当前会话和所选源文件保持不变。",
                7000,
            )
            return False
        self._render_project()
        self.refresh_recent_projects()
        self._set_workspace_save_feedback(
            f"LocalCAT项目包已保存 · {result.receipt.document_count} 个章节 · "
            "源文件只读。"
        )
        self.statusBar().showMessage(
            f"已创建 ProjectPackage · {result.receipt.document_count} 个章节 · "
            f"{result.receipt.segment_count} 个段落。",
            7000,
        )
        return True

    def create_workspace_project_from_selected_files(
        self,
        root: Path,
        selected_paths: tuple[Path, ...],
        request: object,
        destination: Path,
    ) -> bool:
        """Forward a C2 intake request through Controller without scanning root."""

        if not self._confirm_unsaved():
            return False
        try:
            result = self.controller.create_workspace_project_from_selected_files(
                root,
                selected_paths,
                request,
                destination,
            )
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            self._show_error("无法新建多文档项目", safe_error)
            self.statusBar().showMessage(
                "多文档项目未创建；当前会话和所选源文件保持不变。",
                7000,
            )
            return False
        self._render_project()
        self.refresh_recent_projects()
        self._set_workspace_save_feedback(
            f"LocalCAT项目包已保存 · {result.receipt.document_count} 个章节 · "
            "源文件只读。"
        )
        self.statusBar().showMessage(
            f"已创建 ProjectPackage · {result.receipt.document_count} 个章节。",
            7000,
        )
        return True

    def import_workspace_package_path(
        self,
        source: Path,
        *,
        destination: Path | None = None,
    ) -> bool:
        """Preview through the Project menu; apply remains explicit."""

        if not self.controller.has_active_project:
            self._set_workspace_save_feedback(
                "PROJECT.PACKAGE.NO_ACTIVE_WORKSPACE · 未导入，可重试。"
            )
            return False
        return self.preview_workspace_package_import_path(
            source,
            destination=destination,
        )

    def preview_workspace_package_import_path(
        self,
        source: Path,
        *,
        destination: Path | None = None,
    ) -> bool:
        """Render only receipt-safe package metadata before explicit apply."""

        if not self.controller.has_active_project:
            self._set_workspace_save_feedback(
                "PROJECT.PACKAGE.NO_ACTIVE_WORKSPACE · 未导入，可重试。"
            )
            return False
        if not self.controller.has_workspace and destination is None:
            self._set_workspace_save_feedback(
                "PROJECT.PACKAGE.DESTINATION_REQUIRED · Legacy 原文件未修改。"
            )
            return False
        try:
            preview = self.controller.preview_workspace_package_import(
                source,
                destination=destination,
            )
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            self._workspace_package_import_preview = None
            self._workspace_package_import_can_apply = False
            self._workspace_package_preview_text = "预览失败；未应用任何更改。"
            self._set_workspace_save_feedback(
                f"{safe_error} · 项目包未导入，可重试。"
            )
            self._show_error("无法预览项目包", safe_error)
            return False
        self._workspace_package_import_preview = preview
        self._workspace_package_import_source = (source, destination)
        blockers = len(preview.blocking_reasons) + len(
            preview.required_decision_identities
        )
        warnings = "、".join(preview.safe_warnings) or "无"
        blocking_reasons = "、".join(preview.blocking_reasons) or "无"
        self._workspace_package_preview_text = (
            f"项目：{preview.project_name} · ID：{preview.project_id} · "
            f"文档：{preview.document_count} · 段落：{preview.segment_count}\n"
            f"reconciliation：未变 {preview.unchanged_count} · "
            f"源变化 {preview.source_changed_count} · 新增 {preview.new_count} · "
            f"移除 {preview.removed_count} · 歧义 {preview.ambiguous_count} · "
            f"未解决 {preview.unresolved_count}\n"
            f"警告：{len(preview.safe_warnings)}（{warnings}） · "
            f"阻断：{blockers}（{blocking_reasons}）。"
            "只有显式应用后才会发布。"
        )
        self._workspace_package_import_can_apply = blockers == 0
        self.statusBar().showMessage("ProjectPackage preview 已就绪。", 5000)
        return True

    def apply_workspace_package_import(self) -> bool:
        """Apply only the currently issued preview and keep it retryable on failure."""

        preview = self._workspace_package_import_preview
        if preview is None:
            retry = self._workspace_package_import_source
            if retry is None:
                self._set_workspace_save_feedback(
                    "请先选择并预览 ProjectPackage，再显式应用。"
                )
                return False
            if not self.preview_workspace_package_import_path(
                retry[0],
                destination=retry[1],
            ):
                return False
            preview = self._workspace_package_import_preview
        if preview is None or not self._workspace_package_import_can_apply:
            return False
        try:
            result = self.controller.apply_workspace_package_import(preview)
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            self._workspace_package_import_preview = None
            self._set_workspace_save_feedback(
                f"{safe_error} · 项目包未导入，未保存更改仍在；"
                "下次应用将先重新预览，可重试。"
            )
            self._workspace_package_import_can_apply = (
                self._workspace_package_import_source is not None
            )
            self._show_error("项目包应用失败", safe_error)
            return False
        self._workspace_package_import_preview = None
        self._workspace_package_import_source = None
        self._workspace_package_import_can_apply = False
        if result.active_session_changed:
            self._render_project()
        self._workspace_package_preview_text = "导入已应用；如需继续请重新预览。"
        self._set_workspace_save_feedback(
            f"ProjectPackage 导入 receipt 已持久化 · "
            f"{result.receipt.document_count} 个文档 · 源文件只读。"
        )
        self.statusBar().showMessage(
            "ProjectPackage 已验证、发布并冷重开。",
            7000,
        )
        return True

    def save_project_path(self, path: Path) -> bool:
        try:
            self.controller.save_project(path)
        except (EditorControllerError, OSError, ValueError) as exc:
            self._show_error("无法保存项目", str(exc))
            self.statusBar().showMessage("保存失败。", 7000)
            return False
        self._update_title()
        self._refresh_project_tool_actions()
        self.refresh_recent_projects()
        self.statusBar().showMessage(f"已保存：{path}", 7000)
        return True

    def save_workspace_package(self) -> bool:
        """Persist the current package and project its structured outcome."""

        try:
            result = self.controller.save_workspace_package()
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            affected = self._dirty_workspace_display_names()
            self._set_workspace_save_feedback(
                f"{safe_error} · 受影响章节：{affected} · 项目包未保存，"
                "当前会话保留，可重试。"
            )
            self._show_error("无法保存项目包", safe_error)
            self.statusBar().showMessage(
                "项目包保存失败；未证明持久化的章节仍保持未保存状态。",
                7000,
            )
            return False
        return self._finish_workspace_save(result)

    def save_workspace_project_package(self) -> bool:
        """Stable C4 command for a full ProjectPackage save."""

        return self.save_workspace_package()

    def save_workspace_current_document(self) -> bool:
        """Persist only the active issued document into the package overlay."""

        if not self.controller.has_workspace:
            return False
        document = self.controller.current_workspace_identity.document
        try:
            result = self.controller.save_workspace_document(document)
        except (EditorControllerError, OSError) as exc:
            safe_error = self._workspace_error_text(exc)
            current_name = self._workspace_current_document().display_name
            self._set_workspace_save_feedback(
                f"{safe_error} · 受影响章节：{current_name} · 当前章节未保存，"
                "当前会话保留，可重试。"
            )
            self._show_error("无法保存当前章节", safe_error)
            return False
        return self._finish_workspace_save(result)

    def _dirty_workspace_display_names(self) -> str:
        dirty_ids = set(self.controller.workspace_save_state.dirty_document_ids)
        names = tuple(
            document.display_name
            for document in self.controller.workspace_view.documents
            if document.identity.document_id in dirty_ids
        )
        return "、".join(names) or "项目级元数据"

    def _finish_workspace_save(self, result: object) -> bool:
        report = result.save_report
        document_names = {
            document.identity.document_id: document.display_name
            for document in self.controller.workspace_view.documents
        }
        dirty_ids = set(self.controller.workspace_save_state.dirty_document_ids)
        document_summary = "、".join(
            f"{document_names.get(item.document_id, item.document_id)}:"
            f"{item.status.value}"
            + (f"({item.safe_code})" if item.safe_code is not None else "")
            + ("·仍未保存" if item.document_id in dirty_ids else "")
            for item in report.document_results
        )
        if report.recovery_required:
            code = report.safe_code or "PROJECT.SAVE.RECOVERY_REQUIRED"
            self._set_workspace_save_feedback(
                f"{code} · {document_summary} · 项目包未证明已保存，"
                "请执行恢复后重试。"
            )
            self._show_error("项目包需要恢复", code)
            self.statusBar().showMessage(
                f"项目包发布状态需恢复：{code}。",
                7000,
            )
            self._render_project()
            return False
        noncommitted = tuple(
            item
            for item in report.document_results
            if item.status.value in {"failed", "rolled_back"}
        )
        if noncommitted or report.retryable or report.safe_code is not None:
            code = report.safe_code or "PROJECT.SAVE.COMMIT_FAILED"
            self._set_workspace_save_feedback(
                f"{code} · {document_summary} · "
                "项目包未完全保存，可重试。"
            )
            self._show_error("部分章节未保存", code)
            self._render_project()
            return False
        self._render_project()
        self.refresh_recent_projects()
        self._set_workspace_save_feedback(
            f"LocalCAT项目包已保存 · {report.requested_count} 个章节 · "
            f"源文件只读。{document_summary}"
        )
        self.statusBar().showMessage(
            f"ProjectPackage 已保存 · {report.requested_count} 个请求章节。",
            7000,
        )
        return True

    def refresh_recent_projects(self) -> None:
        """Rebuild the project menu from controller-owned local workspace state."""

        self.recent_projects_menu.clear()
        legacy = self.controller.recent_projects()
        workspaces = self.controller.recent_workspace_projects()
        if not legacy and not workspaces:
            empty = self.recent_projects_menu.addAction("暂无最近项目")
            empty.setEnabled(False)
            return
        for project, is_workspace in (
            *((project, True) for project in workspaces),
            *((project, False) for project in legacy),
        ):
            action = self.recent_projects_menu.addAction(
                f"{project.path.name}  —  {project.path.parent}"
            )
            action.setData(str(project.path))
            action.setToolTip(str(project.path))
            action.triggered.connect(
                lambda _checked=False, path=project.path, workspace=is_workspace: (
                    self._open_recent_project_by_kind(path, workspace=workspace)
                )
            )

    def open_recent_project(self, path: Path) -> bool:
        """Open a remembered project by its persisted kind, never by suffix."""

        normalized = path.expanduser().resolve()
        workspace_paths = {
            project.path for project in self.controller.recent_workspace_projects()
        }
        return self._open_recent_project_by_kind(
            normalized,
            workspace=normalized in workspace_paths,
        )

    def _open_recent_project_by_kind(
        self,
        path: Path,
        *,
        workspace: bool,
    ) -> bool:
        """Dispatch one typed recent entry and prune it when its file vanished."""

        normalized = path.expanduser().resolve()
        if not normalized.is_file():
            try:
                self.controller.remove_recent_project(normalized)
            except EditorControllerError:
                pass
            self.refresh_recent_projects()
            self._show_error("最近项目不可用", f"项目文件不存在：{normalized}")
            return False
        if workspace:
            return self.open_workspace_package_path(normalized)
        return self.open_project_path(normalized)

    def close_current_project(self) -> bool:
        """Return to the empty workspace after applying the unsaved guard."""

        if not self._has_active_project():
            return True
        if not self._confirm_unsaved():
            return False
        self.controller.close_project()
        self._refreshing = True
        try:
            self.segment_list.clear()
            self.source_display.clear()
            self._replace_target_text("")
            self.browse_table.clearContents()
            self.browse_table.setRowCount(0)
        finally:
            self._refreshing = False
        self.current_suggestions = SuggestionBundle()
        self._show_empty_state()
        self.refresh_recent_projects()
        self._update_title()
        return True

    def _choose_open(self) -> bool:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "打开 LocalCAT 项目",
            "",
            "LocalCAT projects (*.json *.txt)",
        )
        return bool(selected) and self.open_project_path(Path(selected))

    def _choose_open_home(self) -> bool:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "打开本地项目（Shift 可多选）",
            "",
            "Local project documents (*)",
        )
        selected_paths = tuple(Path(path).expanduser() for path in selected)
        if len(selected_paths) == 1:
            return self.open_project_path(selected_paths[0])
        return self._review_and_create_workspace_from_selected_paths(
            selected_paths
        )

    def _choose_open_workspace_package(self) -> bool:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "打开 LocalCAT ProjectPackage",
            "",
            "LocalCAT ProjectPackage (*.localcat-project *.zip)",
        )
        return bool(selected) and self.open_workspace_package_path(Path(selected))

    def _choose_create_workspace(self) -> bool:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "Shift 多选项目文档",
            "",
            "Project documents (*)",
        )
        return self._review_and_create_workspace_from_selected_paths(
            tuple(Path(path).expanduser() for path in selected)
        )

    def _queue_workspace_drop_selection(self, selected: object) -> None:
        """Leave the native drop event before opening or reviewing files."""

        if self._has_active_project() or type(selected) is not tuple:
            return
        selected_paths = tuple(
            path for path in selected if isinstance(path, Path)
        )
        if len(selected_paths) != len(selected):
            return
        QTimer.singleShot(
            0,
            lambda paths=selected_paths: self._open_or_review_home_selection(
                paths
            ),
        )

    def _open_or_review_home_selection(
        self,
        selected_paths: tuple[Path, ...],
    ) -> bool:
        if self._has_active_project() or not selected_paths:
            return False
        if len(selected_paths) == 1:
            return self.open_project_path(selected_paths[0])
        return self._review_and_create_workspace_from_selected_paths(
            selected_paths
        )

    def _review_and_create_workspace_from_selected_paths(
        self,
        selected_paths: tuple[Path, ...],
    ) -> bool:
        """Run the one review/save workflow shared by picker and drop."""

        if len(selected_paths) < 2:
            self.statusBar().showMessage("多文档项目至少需要显式选择两个文件。", 6000)
            return False
        if type(selected_paths) is not tuple or any(
            not isinstance(path, Path) for path in selected_paths
        ):
            self.statusBar().showMessage("多文档选择无效。", 6000)
            return False
        path_identities = tuple(
            os.path.normcase(os.path.abspath(str(path)))
            for path in selected_paths
        )
        if len(set(path_identities)) != len(path_identities):
            self.statusBar().showMessage("多文档选择不能包含重复文件。", 6000)
            return False
        root = Path(
            os.path.commonpath(
                tuple(str(path.parent) for path in selected_paths)
            )
        )
        review = QtWorkspaceCreationDialog(
            selected_paths,
            default_name="project",
            parent=self,
        )
        if review.exec() != QDialog.DialogCode.Accepted:
            return False
        project_name = review.project_name_input.text().strip()
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存 LocalCAT ProjectPackage",
            str(root / f"{project_name}.localcat-project"),
            "LocalCAT ProjectPackage (*.localcat-project)",
        )
        if not destination:
            return False
        target = Path(destination)
        return self.create_workspace_from_selected_files(
            root,
            review.ordered_paths,
            target,
            name=project_name,
            source_locale=review.source_locale,
            target_locale=review.target_locale,
        )

    def _choose_import_workspace_package(self) -> bool:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "导入 LocalCAT ProjectPackage",
            "",
            "LocalCAT ProjectPackage (*.localcat-project *.zip)",
        )
        if not selected:
            return False
        destination = None
        if not self.controller.has_workspace:
            if not self._confirm_unsaved():
                return False
            legacy_path = self.controller.project.path
            suggested = (
                str(legacy_path.with_suffix(".localcat-project"))
                if legacy_path is not None
                else f"{self.controller.project.name}.localcat-project"
            )
            destination_text, _ = QFileDialog.getSaveFileName(
                self,
                "导入 ProjectPackage 保存为",
                suggested,
                "LocalCAT ProjectPackage (*.localcat-project)",
            )
            if not destination_text:
                return False
            destination = Path(destination_text)
        if not self.import_workspace_package_path(
            Path(selected),
            destination=destination,
        ):
            return False
        preview = self._workspace_package_import_preview
        if preview is None:
            return False
        dialog = QtWorkspacePackageImportDialog(
            mode=preview.mode.value,
            current_project_name=self._active_project_name(),
            incoming_project_name=preview.project_name,
            incoming_project_id=preview.project_id,
            document_count=preview.document_count,
            segment_count=preview.segment_count,
            reconciliation_counts=(
                preview.unchanged_count,
                preview.source_changed_count,
                preview.new_count,
                preview.removed_count,
                preview.ambiguous_count,
                preview.unresolved_count,
            ),
            warnings=preview.safe_warnings,
            blocking_reasons=preview.blocking_reasons,
            required_decision_count=len(preview.required_decision_identities),
            can_apply=self._workspace_package_import_can_apply,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        return self.apply_workspace_package_import()

    def _choose_save(self) -> bool:
        if self.controller.has_workspace:
            return self.save_workspace_package()
        if not self.controller.has_project:
            return False
        current_path = self.controller.project.path
        if current_path is not None and current_path.suffix.lower() == ".json":
            return self.save_project_path(current_path)
        suggested = (
            str(current_path.with_suffix(".json"))
            if current_path is not None
            else f"{self.controller.project.name}.json"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存 LocalCAT 项目",
            suggested,
            "LocalCAT JSON project (*.json)",
        )
        return bool(selected) and self.save_project_path(Path(selected))

    def _render_project(self) -> None:
        self._set_project_search_expanded(False)
        self.pages.setCurrentIndex(1)
        self.save_button.setEnabled(True)
        self.close_project_action.setEnabled(True)
        self.workspace_mode_combo.setEnabled(True)
        self.segment_density_combo.setEnabled(True)
        self.workspace_documents_button.setEnabled(self.controller.has_workspace)
        self.import_workspace_package_action.setEnabled(
            self.controller.has_active_project
        )
        self.save_workspace_document_action.setEnabled(
            self.controller.has_workspace
        )
        self._clear_project_search_results()
        self._refresh_project_tool_actions()
        self._refreshing = True
        try:
            source_locale, target_locale = self._active_locales()
            self.project_name_label.setText(self._active_project_name())
            self.language_label.setText(f"{source_locale}  →  {target_locale}")
            self.segment_count_label.setText(str(len(self._active_segments())))
            self._refresh_chunk_view()
            self.project_search_scope.setVisible(self.controller.has_workspace)
            self._populate_segment_list()
            self._render_current_segment(refresh_chunk_view=False)
        finally:
            self._refreshing = False
        self.set_workspace_mode(self.workspace_mode, persist=False)
        self._update_title()

    def _refresh_workspace_documents_menu(self) -> None:
        self.workspace_documents_menu.clear()
        if not self.controller.has_workspace:
            self.workspace_documents_button.setEnabled(False)
            self.chapter_progress_label.setVisible(False)
            self.workspace_browse_chapter_title.setVisible(False)
            self.workspace_save_feedback.setVisible(False)
            self.workspace_browse_save_feedback.setVisible(False)
            return
        view = self.controller.workspace_view
        current = self.controller.current_workspace_identity.document
        dirty_ids = set(self.controller.workspace_save_state.dirty_document_ids)
        chunk_keys = self._current_chunk_identity_keys()
        chunk_document_ids = (
            None
            if chunk_keys is None
            else {document_id for document_id, _segment_id in chunk_keys}
        )
        file_icon = _localcat_document_icon()
        display_name_counts = {
            document.display_name: sum(
                candidate.display_name == document.display_name
                for candidate in view.documents
            )
            for document in view.documents
        }
        for document in view.documents:
            if (
                chunk_document_ids is not None
                and document.identity.document_id not in chunk_document_ids
            ):
                continue
            dirty = (
                " · 未保存"
                if document.identity.document_id in dirty_ids
                else ""
            )
            is_current = document.identity is current
            visible_name = document.display_name
            if display_name_counts[document.display_name] > 1:
                visible_name = (
                    f"{document.display_name} — {document.source_ref}"
                )
            action = self.workspace_documents_menu.addAction(
                file_icon,
                f"{visible_name}{dirty}"
                f"{'    ✓' if is_current else ''}",
            )
            action.setData(document.identity)
            action.setCheckable(True)
            action.setChecked(is_current)
            action.setToolTip(
                f"{document.source_ref} · "
                f"{document.progress.confirmed_segments} / "
                f"{document.progress.total_segments} 已确认"
            )
        self.workspace_documents_button.setEnabled(True)
        self.chapter_progress_label.setVisible(True)
        self.workspace_browse_chapter_title.setVisible(True)
        self.workspace_save_feedback.setVisible(True)
        self.workspace_browse_save_feedback.setVisible(True)

    @staticmethod
    def _chunk_access_text(access: str, safe_code: str | None = None) -> str:
        labels = {
            "legacy_editable_no_plan": "个人编辑模式",
            "workspace_editable_no_chunk": "全部章节可编辑",
            "editable_assigned_current": "当前分工可编辑",
            "read_only_no_current_chunk": "请选择当前分工",
            "read_only_unallocated": "未分配 · 只读",
            "read_only_outside_current": "当前分工外 · 只读",
            "read_only_not_assignee": "分配给其他身份 · 只读",
            "read_only_detached": "已分离 · 只读",
            "read_only_stale": "计划已过期 · 只读",
        }
        return labels.get(access, f"协作状态不可用 · {safe_code or access}")

    def _refresh_chunk_view(self) -> None:
        """Refresh the frozen Chunk product view without reading its owners."""

        available = (
            self.controller.has_workspace and self.chunk_controller is not None
        )
        self.chunk_scope_menu.setEnabled(available)
        if not available:
            self._chunk_view = None
            self._chunk_view_error_code = None
            self._chunk_scope_cache_key = None
            self._chunk_identity_keys_cache = None
            if self._has_active_project():
                self.segment_count_label.setText(str(len(self._active_segments())))
            self.chunk_manage_action.setEnabled(False)
            self.segment_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
            self._apply_chunk_access_to_editor()
            return
        try:
            view = self.chunk_controller.project_view()
        except Exception as exc:
            self._chunk_view = None
            self.segment_count_label.setText(str(len(self._active_segments())))
            code = str(getattr(exc, "code", "CHUNK.RECOVERY_REQUIRED"))
            self._chunk_view_error_code = code
            self._chunk_scope_cache_key = None
            self._chunk_identity_keys_cache = None
            self.chunk_manage_action.setEnabled(False)
            self.segment_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
            self._apply_chunk_access_to_editor()
            return
        if type(view) is not ChunkApplicationProjectView:
            raise TypeError("chunk façade returned an invalid project view")
        view.__post_init__()
        self._chunk_view = view
        self._chunk_view_error_code = None
        if view.mode is ChunkApplicationMode.ACTIVE:
            self.chunk_manage_action.setEnabled(True)
            self.segment_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
        elif view.mode is ChunkApplicationMode.NO_PLAN:
            self.chunk_manage_action.setEnabled(True)
            self.segment_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
        else:
            self.chunk_manage_action.setEnabled(
                view.safe_code == "CHUNK.REBASE_REQUIRED"
            )
            self.segment_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection
            )
        self._refresh_chunk_identity_cache(view)
        chunk_keys = self._current_chunk_identity_keys()
        self.segment_count_label.setText(
            str(len(self._active_segments()) if chunk_keys is None else len(chunk_keys))
        )
        self._refresh_chunk_search_scopes(view)
        self._apply_chunk_access_to_editor()

    def _populate_chunk_scope_menu(self) -> None:
        """Build the project submenu only when the user opens it."""

        self.chunk_scope_menu.clear()
        if not (
            self.controller.has_workspace and self.chunk_controller is not None
        ):
            placeholder = self.chunk_scope_menu.addAction("仅多文档项目可用")
            placeholder.setEnabled(False)
            return
        view = self._chunk_view
        if view is None:
            code = self._chunk_view_error_code or "CHUNK.RECOVERY_REQUIRED"
            placeholder = self.chunk_scope_menu.addAction(
                f"分工状态不可用 · {code}"
            )
            placeholder.setEnabled(False)
            return
        if view.mode is ChunkApplicationMode.ACTIVE:
            whole = self.chunk_scope_menu.addAction("全部章节（未选择分工）")
            whole.setData(None)
            whole.setCheckable(True)
            whole.setChecked(view.current_chunk_id is None)
            for chunk in view.chunks:
                progress = chunk.progress
                action = self.chunk_scope_menu.addAction(
                    f"{chunk.name} · {progress.confirmed}/"
                    f"{progress.attached_total} 已确认 · {chunk.member_count} 段"
                )
                action.setData(chunk.chunk_id)
                action.setCheckable(True)
                action.setChecked(chunk.chunk_id == view.current_chunk_id)
                action.setToolTip(
                    f"跨文档显示“{chunk.name}”的全部 exact members"
                )
            return
        if view.mode is ChunkApplicationMode.NO_PLAN:
            placeholder = self.chunk_scope_menu.addAction(
                f"尚未建立分工 · {view.unallocated_count} 段"
            )
        else:
            placeholder = self.chunk_scope_menu.addAction(
                f"分工需处理 · {view.safe_code or 'CHUNK.PERMISSION_STALE'}"
            )
        placeholder.setEnabled(False)

    def _refresh_project_chunk_menu(self) -> None:
        """Refresh only after a cheap Chunk session-version comparison."""

        self.export_project_action.setEnabled(
            self.controller.has_workspace
            and self.tmx_export_coordinator is not None
        )

        if self.chunk_controller is not None and self.controller.has_workspace:
            try:
                session = self.chunk_controller.session_view
            except Exception:
                session = None
            view = self._chunk_view
            if session is None or view is None or (
                view.project_id != session.project_id
                or view.plan_revision != session.plan_revision
                or view.current_chunk_id != session.current_chunk_id
                or view.safe_code != session.safe_code
            ):
                self._refresh_chunk_view()
        self._populate_chunk_scope_menu()

    def _refresh_chunk_identity_cache(
        self,
        view: ChunkApplicationProjectView,
    ) -> None:
        if (
            view.mode is not ChunkApplicationMode.ACTIVE
            or view.chunk_plan_id is None
            or view.plan_revision is None
            or view.current_chunk_id is None
            or self.chunk_controller is None
        ):
            self._chunk_scope_cache_key = None
            self._chunk_identity_keys_cache = None
            return
        cache_key = (
            view.chunk_plan_id,
            view.plan_revision,
            view.current_chunk_id,
        )
        if self._chunk_scope_cache_key == cache_key:
            return
        try:
            choices = self.chunk_controller.segment_choices()
        except Exception:
            self._chunk_scope_cache_key = cache_key
            self._chunk_identity_keys_cache = set()
            return
        self._chunk_scope_cache_key = cache_key
        self._chunk_identity_keys_cache = {
            (choice.identity.document_id, choice.identity.local_segment_id)
            for choice in choices
            if choice.chunk_id == view.current_chunk_id and choice.attached
        }

    def _current_chunk_identity_keys(self) -> set[tuple[str, str]] | None:
        view = self._chunk_view
        if (
            view is None
            or view.mode is not ChunkApplicationMode.ACTIVE
            or view.current_chunk_id is None
            or self.chunk_controller is None
        ):
            return None
        return self._chunk_identity_keys_cache or set()

    def _apply_chunk_access_to_editor(self) -> None:
        view = self._chunk_view
        if view is None:
            editable = not (
                self.controller.has_workspace and self.chunk_controller is not None
            )
            confirmable = editable
            reason = "计划已过期 · 只读"
        else:
            editable = view.current_segment_access.may_edit_target
            confirmable = view.current_segment_access.may_change_confirmed
            reason = self._chunk_access_text(
                view.current_segment_access.access,
                (
                    view.current_segment_access.safe_codes[0]
                    if view.current_segment_access.safe_codes
                    else view.safe_code
                ),
            )
        self.target_editor.setReadOnly(not editable)
        self.confirm_button.setEnabled(confirmable)
        self.target_editor.setToolTip("" if editable else reason)
        self.confirm_button.setToolTip(
            (
                "确认译文并前往下一未确认段 "
                f"({self._native_shortcut_text(self.shortcuts['confirm'])})"
            )
            if confirmable
            else reason
        )

    def _refresh_chunk_search_scopes(
        self,
        view: ChunkApplicationProjectView,
    ) -> None:
        previous = self.workspace_search_scope.currentData()
        blocker = QSignalBlocker(self.workspace_search_scope)
        self.workspace_search_scope.clear()
        if (
            view.mode is ChunkApplicationMode.ACTIVE
            and view.current_chunk_id is not None
        ):
            entries = (
                ("当前章节", CollaborativeSearchScopeV2.CURRENT_DOCUMENT),
                ("当前分工", CollaborativeSearchScopeV2.CURRENT_CHUNK),
                ("搜索全部章节", CollaborativeSearchScopeV2.ENTIRE_PROJECT),
            )
        else:
            entries = (
                ("当前章节", SearchScope.CURRENT_DOCUMENT),
                ("搜索全部章节", SearchScope.ENTIRE_PROJECT),
            )
        for label, value in entries:
            self.workspace_search_scope.addItem(label, value)
        selected = next(
            (
                index
                for index in range(self.workspace_search_scope.count())
                if self.workspace_search_scope.itemData(index) == previous
            ),
            0,
        )
        self.workspace_search_scope.setCurrentIndex(selected)
        del blocker

    def _chunk_scope_action_triggered(self, action: QAction) -> None:
        if (
            self._refreshing
            or self._chunk_view is None
            or self._chunk_segment_selection_session is not None
        ):
            return
        if self._chunk_view.mode is not ChunkApplicationMode.ACTIVE:
            return
        chunk_id = action.data()
        try:
            if chunk_id is None:
                self.chunk_controller.clear_current_chunk()
            else:
                self.chunk_controller.select_current_chunk(str(chunk_id))
                choices = self.chunk_controller.segment_choices()
                selected = next(
                    (
                        choice.identity
                        for choice in choices
                        if choice.chunk_id == str(chunk_id)
                    ),
                    None,
                )
                if selected is not None:
                    issued = next(
                        (
                            item.identity
                            for item in self.controller.workspace_view.segments
                            if item.identity.document.document_id
                            == selected.document_id
                            and item.identity.local_segment_id
                            == selected.local_segment_id
                        ),
                        None,
                    )
                    if issued is not None:
                        self.controller.go_to_workspace_segment(issued)
        except Exception as exc:
            self.statusBar().showMessage(
                f"无法切换分工：{getattr(exc, 'code', exc)}",
                7000,
            )
        self._refreshing = True
        try:
            self._refresh_chunk_view()
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=False,
                refresh_chunk_view=False,
            )
            if self.workspace_mode is WorkspaceMode.BROWSE:
                self._refresh_browse_table()
        finally:
            self._refreshing = False

    def _open_tmx_project_export_dialog(self) -> None:
        """Open the exact-scope project/chunk TMX export surface."""

        service = self.tmx_export_coordinator
        if not self.controller.has_workspace or service is None:
            self.statusBar().showMessage("TMX 项目导出当前不可用。", 5000)
            return
        try:
            available = service.available_project_scopes()
            scopes = tuple(
                TmxExportScopeChoice(
                    token,
                    "整个项目" if token == "project" else f"分工 · {label}",
                )
                for token, label in available
            )
        except Exception as error:
            self.statusBar().showMessage(
                f"TMX 导出范围不可用：{getattr(error, 'code', 'TMX.SCOPE.UNAVAILABLE')}",
                7000,
            )
            return
        if not scopes:
            self.statusBar().showMessage("TMX 项目导出当前不可用。", 5000)
            return
        source_locale, target_locale = self._active_locales()

        def prepare(
            token: str,
            source: str,
            target: str,
            destination: Path,
        ) -> TmxExportDialogPreview:
            application_preparation = service.prepare_project_export(
                token,
                source,
                target,
                destination,
            )
            preview = application_preparation.preview
            chunk_label = next(
                (choice.label for choice in scopes if choice.token == token),
                "分工",
            )
            if token == "project":
                badge = "PROJECT · 整个项目"
                title = self._active_project_name()
            else:
                badge = "CHUNK · 明选分工"
                title = chunk_label
            return TmxExportDialogPreview(
                domain_preparation=application_preparation,
                badge=badge,
                title=title,
                binding=(
                    f"{preview.scope_kind.value} · {preview.scope_id} · "
                    f"{application_preparation.preview.operation_id[:12]}"
                ),
                document_count=preview.document_count,
                attached_count=preview.attached_count,
                included_count=preview.included_count,
                excluded_count=preview.excluded_count,
                warning_count=preview.warning_count,
                profile_id=preview.profile_id,
            )

        dialog = TmxExportDialog(
            title="导出项目 TMX",
            scopes=scopes,
            source_locale=source_locale,
            target_locale=target_locale,
            prepare=prepare,
            publish=service.publish,
            parent=self,
        )
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                receipt = dialog.receipt
                included = getattr(receipt, "included_count", 0)
                self.statusBar().showMessage(
                    f"TMX 已导出 · {included} 个翻译单元。",
                    7000,
                )
        finally:
            dialog.deleteLater()

    def _open_chunk_manager(self) -> None:
        if (
            self.chunk_controller is None
            or self._chunk_view is None
            or self._chunk_segment_selection_session is not None
        ):
            return
        existing = self._chunk_manager_dialog
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                self._chunk_manager_dialog = None
        try:
            from qt_chunk_manager_dialog import QtChunkManagerDialog

            dialog = QtChunkManagerDialog(
                self.chunk_controller,
                self._chunk_view,
                parent=self,
            )
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self._chunk_manager_dialog = dialog
            dialog.segmentSelectionRequested.connect(
                self._begin_chunk_segment_selection
            )
            dialog.viewRefreshRequested.connect(
                self._refresh_after_chunk_manager_change
            )
            dialog.finished.connect(
                lambda _result, issued=dialog: self._chunk_manager_finished(
                    issued
                )
            )
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception as exc:
            self._chunk_manager_dialog = None
            self._show_error(
                "无法管理分工",
                str(getattr(exc, "code", exc)),
            )

    def _refresh_after_chunk_manager_change(self) -> None:
        if not self._has_active_project():
            return
        self._refreshing = True
        try:
            self._refresh_chunk_view()
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=False,
                refresh_chunk_view=False,
            )
            if self.workspace_mode is WorkspaceMode.BROWSE:
                self._refresh_browse_table()
        finally:
            self._refreshing = False

    def _chunk_manager_finished(self, dialog: object) -> None:
        session = self._chunk_segment_selection_session
        if session is not None and session.manager is dialog:
            self._finish_chunk_segment_selection(False, restore_manager=False)
        if self._chunk_manager_dialog is dialog:
            self._chunk_manager_dialog = None
        self._refresh_after_chunk_manager_change()
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass

    def _capture_chunk_selection_search_state(self) -> tuple[object, ...]:
        return (
            self.project_search_input.text(),
            self.project_search_status.currentData(),
            self.workspace_search_scope.currentData(),
            self.project_search_scope.currentData(),
            self.project_search_source.isChecked(),
            self.project_search_target.isChecked(),
            self.project_search_speaker.isChecked(),
            self.project_search_match_case.isChecked(),
            self.project_search_whole_word.isChecked(),
            self._project_search_expanded,
        )

    def _restore_chunk_selection_search_state(
        self,
        state: tuple[object, ...],
    ) -> None:
        if type(state) is not tuple or len(state) != 10:
            raise ValueError("CHUNK.SEGMENT_SELECTION_SEARCH_STATE_INVALID")
        controls = (
            self.project_search_input,
            self.project_search_status,
            self.workspace_search_scope,
            self.project_search_scope,
            self.project_search_source,
            self.project_search_target,
            self.project_search_speaker,
            self.project_search_match_case,
            self.project_search_whole_word,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        try:
            self.project_search_input.setText(str(state[0]))
            for combo, value in (
                (self.project_search_status, state[1]),
                (self.workspace_search_scope, state[2]),
                (self.project_search_scope, state[3]),
            ):
                index = combo.findData(value)
                if index >= 0:
                    combo.setCurrentIndex(index)
            for checkbox, checked in (
                (self.project_search_source, state[4]),
                (self.project_search_target, state[5]),
                (self.project_search_speaker, state[6]),
                (self.project_search_match_case, state[7]),
                (self.project_search_whole_word, state[8]),
            ):
                checkbox.setChecked(bool(checked))
        finally:
            del blockers
        self._set_project_search_expanded(bool(state[9]))

    @staticmethod
    def _chunk_segment_identity_key(value: object) -> tuple[str, str] | None:
        nested = getattr(value, "segment_identity", None)
        if nested is not None:
            value = nested
        document_id = getattr(value, "document_id", None)
        local_segment_id = getattr(value, "local_segment_id", None)
        if (
            type(document_id) is not str
            or not document_id
            or type(local_segment_id) is not str
            or not local_segment_id
        ):
            return None
        return document_id, local_segment_id

    def _chunk_segment_allowed_map(self) -> dict[tuple[str, str], object]:
        session = self._chunk_segment_selection_session
        if session is None:
            return {}
        return {
            self._chunk_segment_identity_key(identity): identity
            for identity in session.request.allowed_identities
        }

    def _chunk_segment_identity_for_row(self, row: int) -> object | None:
        session = self._chunk_segment_selection_session
        if session is None or row < 0:
            return None
        item = self.browse_table.item(row, 0)
        if item is None:
            return None
        key = self._chunk_segment_identity_key(
            item.data(Qt.ItemDataRole.UserRole)
        )
        if key is None:
            return None
        return self._chunk_segment_allowed_map().get(key)

    def _chunk_segment_row_label(self, row: int) -> str:
        position = self.browse_table.item(row, 0)
        document_label = ""
        for candidate in range(row - 1, -1, -1):
            item = self.browse_table.item(candidate, 0)
            if item is None:
                continue
            if item.data(Qt.ItemDataRole.UserRole) is None and item.text():
                document_label = item.text()
                break
        return (
            f"{document_label} · {position.text()}"
            if document_label and position is not None
            else (position.text() if position is not None else "—")
        )

    def _selected_chunk_segment_identities(self) -> tuple[object, ...]:
        session = self._chunk_segment_selection_session
        model = self.browse_table.selectionModel()
        if session is None or model is None:
            return ()
        return tuple(
            identity
            for index in sorted(model.selectedRows(), key=lambda item: item.row())
            if (identity := self._chunk_segment_identity_for_row(index.row()))
            is not None
        )

    def _select_chunk_segment_identities(self, identities: tuple[object, ...]) -> None:
        if self._chunk_segment_selection_session is None:
            return
        wanted = {
            key
            for identity in identities
            if (key := self._chunk_segment_identity_key(identity)) is not None
        }
        selection = self.browse_table.selectionModel()
        if selection is None:
            return
        blocker = QSignalBlocker(self.browse_table)
        first_row = None
        try:
            selection.clearSelection()
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
            for row in range(self.browse_table.rowCount()):
                identity = self._chunk_segment_identity_for_row(row)
                key = self._chunk_segment_identity_key(identity)
                if key not in wanted:
                    continue
                selection.select(
                    self.browse_table.model().index(row, 0),
                    flags,
                )
                if first_row is None:
                    first_row = row
            if first_row is not None:
                selection.setCurrentIndex(
                    self.browse_table.model().index(first_row, 0),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            del blocker
        self._chunk_segment_browse_selection_changed()

    def _begin_chunk_segment_selection(self, request: object) -> None:
        if type(request) is not ChunkApplicationSegmentSelectionRequest:
            self._show_error(
                "无法选择分工段落",
                "CHUNK.SEGMENT_SELECTION_REQUEST_INVALID",
            )
            return
        request.__post_init__()
        manager = self.sender() or self._chunk_manager_dialog
        if (
            manager is None
            or not callable(getattr(manager, "accept_segment_selection", None))
            or not callable(getattr(manager, "cancel_segment_selection", None))
            or not self.controller.has_workspace
            or self._chunk_segment_selection_session is not None
        ):
            try:
                manager.cancel_segment_selection(request)
            except (AttributeError, RuntimeError):
                pass
            self._show_error(
                "无法选择分工段落",
                "CHUNK.SEGMENT_SELECTION_SESSION_UNAVAILABLE",
            )
            return
        workspace_keys = {
            self._chunk_segment_identity_key(item.identity)
            for item in self.controller.workspace_view.segments
        }
        requested_keys = {
            self._chunk_segment_identity_key(identity)
            for identity in request.allowed_identities
        }
        if None in requested_keys or not requested_keys.issubset(workspace_keys):
            manager.cancel_segment_selection(request)
            self._show_error(
                "无法选择分工段落",
                "CHUNK.SEGMENT_SELECTION_SCOPE_STALE",
            )
            return
        widgets = (
            self.workspace_mode_combo,
            self.open_button,
            self.save_button,
            self.settings_button,
            self.workspace_documents_button,
            self.project_search_toggle,
            self.project_search_input,
            self.project_search_status,
            self.workspace_search_scope,
            self.project_search_scope,
            self.project_search_clear,
            self.project_search_source,
            self.project_search_target,
            self.project_search_speaker,
            self.project_search_match_case,
            self.project_search_whole_word,
            self.project_search_button,
            self.project_search_previous,
            self.project_search_next,
            self.browse_group_button,
        )
        shortcuts = tuple(self.shortcuts.values()) + (self.project_search_shortcut,)
        session = _ChunkSegmentSelectionSession(
            manager=manager,
            request=request,
            previous_mode=self.workspace_mode,
            previous_identity=self.controller.current_workspace_identity,
            previous_chunk_id=(
                self._chunk_view.current_chunk_id
                if self._chunk_view is not None
                else None
            ),
            previous_search_state=self._capture_chunk_selection_search_state(),
            enabled_widgets=tuple(
                (widget, widget.isEnabled()) for widget in widgets
            ),
            enabled_shortcuts=tuple(
                (shortcut, shortcut.isEnabled()) for shortcut in shortcuts
            ),
            browse_hint_text=self.browse_hint.text(),
            browse_group_button_visible=self.browse_group_button.isVisible(),
            browse_group_turn_bar_visible=self.browse_group_turn_bar.isVisible(),
        )
        self._chunk_segment_selection_session = session
        manager.hide()
        for widget, _enabled in session.enabled_widgets:
            widget.setEnabled(False)
        for shortcut, _enabled in session.enabled_shortcuts:
            shortcut.setEnabled(False)
        self.browse_hint.setText(
            "临时选择分工段落 · source / target 仅用于核对，不会导航或发布"
        )
        self.browse_group_button.hide()
        self.browse_group_turn_bar.setVisible(
            session.browse_group_turn_bar_visible
        )
        self.chunk_segment_selection_title.setText(request.action_label)
        self.chunk_segment_bulk_select.setVisible(
            request.bulk_select_label is not None
        )
        if request.bulk_select_label is not None:
            self.chunk_segment_bulk_select.setText(request.bulk_select_label)
        self.chunk_segment_range_start.setText("设为起点")
        self.chunk_segment_range_end.setText("设为终点")
        self.chunk_segment_selection_bar.show()
        self.browse_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        if not self.set_workspace_mode(WorkspaceMode.BROWSE, persist=False):
            self._finish_chunk_segment_selection(False)
            return
        self._select_chunk_segment_identities(request.selected_identities)
        self.browse_table.setFocus(Qt.FocusReason.OtherFocusReason)

    def _chunk_segment_browse_selection_changed(self) -> None:
        session = self._chunk_segment_selection_session
        if session is None:
            return
        count = len(self._selected_chunk_segment_identities())
        minimum = session.request.minimum_selection
        if count == 0:
            status = (
                f"未选择段落 · 可清空返回；此操作通常至少需要 {minimum} 段"
            )
        elif count < minimum:
            status = f"已选择 {count} 段 · 此操作至少需要 {minimum} 段"
        else:
            status = (
                f"已选择 {count} / {len(session.request.allowed_identities)} 段 · "
                "按项目与文档顺序返回"
            )
        self.chunk_segment_selection_status.setText(status)
        self.chunk_segment_done.setEnabled(count == 0 or count >= minimum)

    def _clear_chunk_segment_browse_selection(self) -> None:
        if self._chunk_segment_selection_session is None:
            return
        self.browse_table.clearSelection()
        session = self._chunk_segment_selection_session
        session.range_start_row = None
        session.range_end_row = None
        session.selection_anchor_row = None
        self.chunk_segment_range_start.setText("设为起点")
        self.chunk_segment_range_end.setText("设为终点")
        self._chunk_segment_browse_selection_changed()

    def _select_chunk_segment_bulk_scope(self) -> None:
        session = self._chunk_segment_selection_session
        if session is None or not session.request.bulk_select_identities:
            return
        self._select_chunk_segment_identities(
            session.request.bulk_select_identities
        )

    def _set_chunk_segment_range_endpoint(self, endpoint: str) -> None:
        session = self._chunk_segment_selection_session
        if session is None:
            return
        row = self.browse_table.currentRow()
        identity = self._chunk_segment_identity_for_row(row)
        if identity is None:
            self.chunk_segment_selection_status.setText(
                "请先选择一个可用于当前操作的段落。"
            )
            return
        label = self._chunk_segment_row_label(row)
        if endpoint == "start":
            session.range_start_row = row
            self.chunk_segment_range_start.setText(f"起点：{label}")
        elif endpoint == "end":
            session.range_end_row = row
            self.chunk_segment_range_end.setText(f"终点：{label}")
        else:
            raise ValueError("CHUNK.RANGE_ENDPOINT_INVALID")
        if session.range_start_row is None or session.range_end_row is None:
            return
        start, end = sorted(
            (session.range_start_row, session.range_end_row)
        )
        identities = tuple(
            identity
            for candidate in range(start, end + 1)
            if (
                identity := self._chunk_segment_identity_for_row(candidate)
            )
            is not None
        )
        self._select_chunk_segment_identities(identities)

    def _restore_chunk_selection_current_scope(
        self,
        session: _ChunkSegmentSelectionSession,
    ) -> None:
        if self.chunk_controller is not None:
            try:
                current = self.chunk_controller.project_view().current_chunk_id
                if current != session.previous_chunk_id:
                    if session.previous_chunk_id is None:
                        self.chunk_controller.clear_current_chunk()
                    else:
                        self.chunk_controller.select_current_chunk(
                            session.previous_chunk_id
                        )
            except Exception as exc:
                self.statusBar().showMessage(
                    f"当前分工恢复失败：{getattr(exc, 'code', exc)}",
                    7000,
                )
        try:
            self.controller.go_to_workspace_segment(session.previous_identity)
        except (TypeError, ValueError, EditorControllerError) as exc:
            self.statusBar().showMessage(
                f"原段落位置恢复失败：{exc}",
                7000,
            )

    def _finish_chunk_segment_selection(
        self,
        accepted: bool,
        *,
        restore_manager: bool = True,
    ) -> None:
        session = self._chunk_segment_selection_session
        if session is None:
            return
        manager = session.manager
        if accepted:
            identities = self._selected_chunk_segment_identities()
            try:
                manager.accept_segment_selection(session.request, identities)
            except Exception as exc:
                self.chunk_segment_selection_status.setText(
                    f"无法返回所选段落：{getattr(exc, 'code', exc)}"
                )
                return
        else:
            try:
                manager.cancel_segment_selection(session.request)
            except RuntimeError:
                restore_manager = False
        self._chunk_segment_selection_session = None
        self.chunk_segment_selection_bar.hide()
        self.chunk_segment_bulk_select.hide()
        self.browse_hint.setText(session.browse_hint_text)
        self.browse_group_button.setVisible(
            session.browse_group_button_visible
        )
        self.browse_group_turn_bar.setVisible(
            session.browse_group_turn_bar_visible
        )
        self.browse_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for widget, enabled in session.enabled_widgets:
            widget.setEnabled(enabled)
        for shortcut, enabled in session.enabled_shortcuts:
            shortcut.setEnabled(enabled)
        self._restore_chunk_selection_search_state(
            session.previous_search_state
        )
        self._restore_chunk_selection_current_scope(session)
        self._refreshing = True
        try:
            self._refresh_chunk_view()
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=False,
                refresh_chunk_view=False,
            )
        finally:
            self._refreshing = False
        self.set_workspace_mode(session.previous_mode, persist=False)
        if restore_manager:
            try:
                manager.show()
                manager.raise_()
                manager.activateWindow()
            except RuntimeError:
                pass

    def _workspace_document_action_triggered(self, action: QAction) -> None:
        if (
            self._refreshing
            or not self.controller.has_workspace
            or self._chunk_segment_selection_session is not None
        ):
            return
        identity = action.data()
        try:
            self.controller.select_workspace_document(identity)
        except (TypeError, EditorControllerError) as exc:
            self._show_error("无法切换文档", str(exc))
            self._refresh_workspace_documents_menu()
            return
        self._refreshing = True
        try:
            self._select_project_index(self.controller.workspace_global_index)
            self._render_current_segment()
            if self.workspace_mode is WorkspaceMode.BROWSE:
                self._refresh_browse_table()
        finally:
            self._refreshing = False

    def _render_current_segment(
        self,
        *,
        reset_target_history: bool = True,
        refresh_chunk_view: bool = True,
    ) -> None:
        segment = self.controller.current_segment
        segments = self._active_segments()
        speaker_text = segment.speaker or "无 speaker"
        self.speaker_display.setText(speaker_text)
        self.speaker_display.setAccessibleName(f"当前段 raw speaker：{speaker_text}")
        self.speaker_display.setProperty("empty", not bool(segment.speaker))
        self.speaker_display.style().unpolish(self.speaker_display)
        self.speaker_display.style().polish(self.speaker_display)
        self.source_display.setPlainText(segment.source)
        if reset_target_history:
            self._replace_target_text(segment.target)
        if self.controller.has_workspace:
            current_document = self._workspace_current_document()
            current_view = self.controller.workspace_view.segments[
                self.controller.workspace_global_index
            ]
            self.segment_position_label.setText(
                f"文档内 {current_view.document_local_index + 1} / "
                f"{current_document.progress.total_segments} · 全项目 "
                f"{current_view.project_global_index + 1} / {len(segments)}"
            )
            self.chapter_progress_label.setText(
                f"{current_document.display_name} · "
                f"{current_document.progress.confirmed_segments} / "
                f"{current_document.progress.total_segments} 已确认"
            )
            self.workspace_browse_chapter_title.setText(
                f"当前文档 · {current_document.display_name}"
            )
            self._refresh_workspace_documents_menu()
        else:
            self.segment_position_label.setText(
                f"{self.controller.current_index + 1} / {len(segments)}"
            )
        self.confirmation_label.setText("已确认" if segment.confirmed else "待确认")
        self.confirmation_label.setProperty("confirmed", segment.confirmed)
        self.confirmation_label.style().unpolish(self.confirmation_label)
        self.confirmation_label.style().polish(self.confirmation_label)
        self.progress_bar.setRange(0, len(segments))
        self.progress_bar.setValue(self._active_confirmed_count())
        if refresh_chunk_view:
            self._refresh_chunk_view()
        self._refresh_browse_group_button()
        self.refresh_suggestions()

    def _target_changed(self) -> None:
        if self._refreshing or not self._has_active_project():
            return
        try:
            if self.controller.has_workspace:
                self.controller.update_workspace_target(
                    self.target_editor.toPlainText()
                )
            else:
                self.controller.update_target(self.target_editor.toPlainText())
        except EditorControllerError as exc:
            self._refresh_target_from_controller()
            self._refresh_chunk_view()
            self.statusBar().showMessage(
                f"当前段只读，未保存输入：{exc}",
                7000,
            )
            return
        if self.current_project_search_report is not None:
            self._clear_project_search_results(
                "搜索结果已过期；请按当前项目内容重新搜索。"
            )
        if self.unconfirmed_filter.isChecked():
            self._refreshing = True
            try:
                self._populate_segment_list()
            finally:
                self._refreshing = False
        else:
            self._update_segment_item(self._active_index())
        self._render_progress_state()
        self._refresh_chunk_view()
        self._update_title()

    def _render_progress_state(self) -> None:
        segment = self.controller.current_segment
        self.confirmation_label.setText("已确认" if segment.confirmed else "待确认")
        self.confirmation_label.setProperty("confirmed", segment.confirmed)
        self.confirmation_label.style().unpolish(self.confirmation_label)
        self.confirmation_label.style().polish(self.confirmation_label)
        self.progress_bar.setValue(self._active_confirmed_count())
        if self.controller.has_workspace:
            self._refresh_workspace_documents_menu()

    def _update_segment_item(self, project_index: int) -> None:
        issued_identity = (
            self.controller.workspace_view.segments[project_index].identity
            if self.controller.has_workspace
            else project_index
        )
        for row in range(self.segment_list.count()):
            item = self.segment_list.item(row)
            item_identity = item.data(Qt.ItemDataRole.UserRole)
            if (
                item_identity is not issued_identity
                if self.controller.has_workspace
                else item_identity != issued_identity
            ):
                continue
            segment = self._active_segments()[project_index]
            item.setText(self._segment_item_text(project_index, segment))
            item.setSizeHint(self._segment_item_size_hint(item.text()))
            break

    def _populate_segment_list(self) -> None:
        self.segment_list.clear()
        segments = self._active_segments()
        unconfirmed_only = self.unconfirmed_filter.isChecked()
        selected_row = -1
        workspace_view = (
            self.controller.workspace_view if self.controller.has_workspace else None
        )
        if workspace_view is not None:
            chunk_keys = self._current_chunk_identity_keys()
            file_icon = _localcat_document_icon()
            for document in workspace_view.documents:
                document_segments = tuple(
                    item
                    for item in workspace_view.segments
                    if item.identity.document is document.identity
                    and (
                        chunk_keys is None
                        or (
                            item.identity.document.document_id,
                            item.identity.local_segment_id,
                        )
                        in chunk_keys
                    )
                    and (not unconfirmed_only or not item.confirmed)
                )
                if not document_segments:
                    continue
                divider = QListWidgetItem(file_icon, document.display_name)
                divider.setFlags(
                    divider.flags() & ~Qt.ItemFlag.ItemIsSelectable
                )
                divider.setData(Qt.ItemDataRole.UserRole, None)
                divider.setBackground(QColor("#dcecf5"))
                divider.setForeground(QColor("#0b5e80"))
                divider_font = divider.font()
                divider_font.setBold(True)
                divider.setFont(divider_font)
                divider.setToolTip(document.display_name)
                divider.setSizeHint(QSize(0, 44))
                self.segment_list.addItem(divider)
                for item_view in document_segments:
                    index = item_view.project_global_index
                    segment = segments[index]
                    item = QListWidgetItem(self._segment_item_text(index, segment))
                    item.setData(Qt.ItemDataRole.UserRole, item_view.identity)
                    item.setToolTip(segment.source)
                    item.setSizeHint(self._segment_item_size_hint(item.text()))
                    self.segment_list.addItem(item)
                    if index == self._active_index():
                        selected_row = self.segment_list.count() - 1
            self.segment_list.setCurrentRow(selected_row)
            return
        for index, segment in enumerate(segments):
            if unconfirmed_only and segment.confirmed:
                continue
            text = self._segment_item_text(index, segment)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(segment.source)
            item.setSizeHint(self._segment_item_size_hint(item.text()))
            self.segment_list.addItem(item)
            if index == self._active_index():
                selected_row = self.segment_list.count() - 1
        self.segment_list.setCurrentRow(selected_row)

    def _segment_item_text(self, index: int, segment: EditorSegment) -> str:
        prefix = "✓" if segment.confirmed else "○"
        if self.segment_density is SegmentDensity.WRAPPED:
            source = segment.source.strip()
        else:
            source = " ".join(segment.source.split())
            if len(source) > 72:
                source = source[:69] + "…"
        return f"{prefix}  {index + 1:03d}   {source}"

    def _segment_item_size_hint(self, text: str) -> QSize:
        if self.segment_density is SegmentDensity.COMPACT:
            return QSize(0, 66 if "\n" in text else 44)
        available_width = max(150, self.segment_list.viewport().width() - 28)
        metrics = QFontMetrics(self.segment_list.font())
        bounds = metrics.boundingRect(
            QRect(0, 0, available_width, 10000),
            Qt.TextFlag.TextWordWrap,
            text,
        )
        return QSize(available_width, max(44, bounds.height() + 22))

    def _refresh_segment_item_sizes(self) -> None:
        if (
            not self._has_active_project()
            or self.segment_density is SegmentDensity.COMPACT
        ):
            return
        for row in range(self.segment_list.count()):
            item = self.segment_list.item(row)
            if item is not None:
                item.setSizeHint(self._segment_item_size_hint(item.text()))
        self.segment_list.doItemsLayout()

    def _segment_density_changed(self, _index: int) -> None:
        if self._refreshing:
            return
        try:
            density = SegmentDensity(str(self.segment_density_combo.currentData()))
        except ValueError:
            return
        self.set_segment_density(density)

    def set_segment_density(
        self,
        density: SegmentDensity,
        *,
        persist: bool = True,
    ) -> bool:
        """Switch between equal-height summaries and full wrapped source rows."""

        try:
            normalized = (
                density
                if isinstance(density, SegmentDensity)
                else SegmentDensity(density)
            )
            preferences = replace(
                self._display_preferences,
                segment_density=normalized,
            )
            if persist:
                preferences = self.controller.update_display_preferences(preferences)
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法切换段落显示", str(exc))
            return False
        self._display_preferences = preferences
        self.segment_density = normalized
        self._refreshing = True
        try:
            self.segment_density_combo.setCurrentIndex(
                0 if normalized is SegmentDensity.COMPACT else 1
            )
            self.segment_list.setWordWrap(normalized is SegmentDensity.WRAPPED)
            self.segment_list.setUniformItemSizes(
                normalized is SegmentDensity.COMPACT
            )
            self.segment_list.setTextElideMode(
                Qt.TextElideMode.ElideRight
                if normalized is SegmentDensity.COMPACT
                else Qt.TextElideMode.ElideNone
            )
            if self._has_active_project():
                self._populate_segment_list()
        finally:
            self._refreshing = False
        return True

    def _workspace_mode_changed(self, _index: int) -> None:
        if self._refreshing:
            return
        try:
            mode = WorkspaceMode(str(self.workspace_mode_combo.currentData()))
        except ValueError:
            return
        self.set_workspace_mode(mode)

    def set_workspace_mode(
        self,
        mode: WorkspaceMode,
        *,
        persist: bool = True,
    ) -> bool:
        """Switch between the focused editor and the bilingual browse page."""

        try:
            normalized = (
                mode if isinstance(mode, WorkspaceMode) else WorkspaceMode(mode)
            )
            if (
                self._chunk_segment_selection_session is not None
                and normalized is not WorkspaceMode.BROWSE
            ):
                return False
            preferences = replace(
                self._display_preferences,
                workspace_mode=normalized,
            )
            if persist:
                preferences = self.controller.update_display_preferences(preferences)
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法切换工作区", str(exc))
            return False
        self._display_preferences = preferences
        self.workspace_mode = normalized
        self._refreshing = True
        try:
            self.workspace_mode_combo.setCurrentIndex(
                0 if normalized is WorkspaceMode.EDIT else 1
            )
            if self._has_active_project():
                if normalized is WorkspaceMode.BROWSE:
                    self._refresh_browse_table()
                    self.workspace_pages.setCurrentIndex(1)
                else:
                    self.workspace_pages.setCurrentIndex(0)
                    self._select_project_index(self._active_index())
        finally:
            self._refreshing = False
        return True

    def _refresh_browse_table(self) -> None:
        if not self._has_active_project():
            self.browse_table.clearContents()
            self.browse_table.setRowCount(0)
            self._refresh_browse_group_button()
            return
        segments = self._active_segments()
        selection_session = self._chunk_segment_selection_session
        selected_before = (
            self._selected_chunk_segment_identities()
            if selection_session is not None
            else ()
        )
        allowed_keys = set(self._chunk_segment_allowed_map())
        workspace_view = (
            self.controller.workspace_view if self.controller.has_workspace else None
        )
        self.browse_table.setUpdatesEnabled(False)
        try:
            self.browse_table.clearSpans()
            self.browse_table.clearContents()
            row_specs: list[tuple[str, int | None]] = []
            if workspace_view is not None:
                chunk_keys = (
                    None
                    if selection_session is not None
                    else self._current_chunk_identity_keys()
                )
                for document in workspace_view.documents:
                    document_rows = tuple(
                        item.project_global_index
                        for item in workspace_view.segments
                        if item.identity.document is document.identity
                        and (
                            chunk_keys is None
                            or (
                                item.identity.document.document_id,
                                item.identity.local_segment_id,
                            )
                            in chunk_keys
                        )
                    )
                    if document_rows:
                        row_specs.append((document.display_name, None))
                        row_specs.extend(("", index) for index in document_rows)
            else:
                row_specs.extend(("", index) for index in range(len(segments)))
            maximum_position = len(segments)
            if workspace_view is not None:
                maximum_position = max(
                    (
                        item.document_local_index + 1
                        for item in workspace_view.segments
                    ),
                    default=0,
                )
            self._resize_browse_position_column(maximum_position)
            self.browse_table.setRowCount(len(row_specs))
            current_row = 0
            for row, (document_title, index) in enumerate(row_specs):
                if index is None:
                    for column, value in enumerate(
                        (document_title, "", "", "", "")
                    ):
                        item = QTableWidgetItem(value)
                        item.setFlags(
                            item.flags() & ~Qt.ItemFlag.ItemIsSelectable
                        )
                        item.setData(Qt.ItemDataRole.UserRole, None)
                        item.setBackground(QColor("#dcecf5"))
                        item.setForeground(QColor("#0b5e80"))
                        divider_font = item.font()
                        divider_font.setBold(True)
                        item.setFont(divider_font)
                        if column == 0:
                            item.setIcon(_localcat_document_icon())
                        self.browse_table.setItem(row, column, item)
                    self.browse_table.setSpan(row, 0, 1, 5)
                    continue
                segment = segments[index]
                position = f"{index + 1:03d}"
                identity: object = index
                if workspace_view is not None:
                    view_segment = workspace_view.segments[index]
                    position = f"{view_segment.document_local_index + 1:03d}"
                    identity = view_segment.identity
                values = (
                    position,
                    segment.source,
                    segment.target or "—",
                    segment.speaker or "无 speaker",
                    "已确认" if segment.confirmed else "待确认",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, identity)
                    item.setToolTip(value)
                    if (
                        selection_session is not None
                        and self._chunk_segment_identity_key(identity)
                        not in allowed_keys
                    ):
                        item.setFlags(
                            item.flags() & ~Qt.ItemFlag.ItemIsSelectable
                        )
                        item.setForeground(QColor("#8b9cab"))
                        item.setToolTip(
                            "当前高级操作不可选择此段落 · " + value
                        )
                    self.browse_table.setItem(row, column, item)
                if index == self._active_index():
                    current_row = row
        finally:
            self.browse_table.setUpdatesEnabled(True)
        self.browse_table.resizeRowsToContents()
        if selection_session is None:
            self.browse_table.setCurrentCell(current_row, 1)
        else:
            selection = self.browse_table.selectionModel()
            if selection is not None and self.browse_table.rowCount():
                selection.setCurrentIndex(
                    self.browse_table.model().index(current_row, 1),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
            self._select_chunk_segment_identities(selected_before)
        current = self.browse_table.item(current_row, 1)
        if current is not None:
            self.browse_table.scrollToItem(
                current,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
        self._refresh_browse_group_button()

    def _resize_browse_position_column(self, maximum_position: int) -> None:
        """Keep the compact paragraph identifier visible without elision."""

        if type(maximum_position) is not int or maximum_position < 0:
            raise ValueError("browse maximum position must be non-negative")
        widest_position = f"{max(1, maximum_position):03d}"
        body_width = (
            self.browse_table.fontMetrics().horizontalAdvance(widest_position)
            + 40
        )
        header_width = (
            self.browse_table.horizontalHeader()
            .fontMetrics()
            .horizontalAdvance("段落")
            + 32
        )
        self.browse_table.horizontalHeader().resizeSection(
            0,
            max(72, body_width, header_width),
        )

    def _browse_document_projection(
        self,
    ) -> tuple[str, tuple[tuple[int, str, str, object], ...]]:
        """Project the current document without creating a second authority."""

        if not self._has_active_project():
            return "", ()
        if not self.controller.has_workspace:
            return (
                self.controller.project.name,
                tuple(
                    (index, segment.source, segment.target, index)
                    for index, segment in enumerate(self.controller.project.segments)
                ),
            )
        view = self.controller.workspace_view
        document_identity = self.controller.current_workspace_identity.document
        document = next(
            item for item in view.documents if item.identity is document_identity
        )
        selection_session = self._chunk_segment_selection_session
        chunk_keys = (
            set(self._chunk_segment_allowed_map())
            if selection_session is not None
            else self._current_chunk_identity_keys()
        )
        return (
            document.display_name,
            tuple(
                (
                    item.document_local_index,
                    item.source,
                    item.target,
                    item.identity,
                )
                for item in view.segments
                if item.identity.document is document_identity
                and (
                    chunk_keys is None
                    or (
                        item.identity.document.document_id,
                        item.identity.local_segment_id,
                    )
                    in chunk_keys
                )
            ),
        )

    def _browse_group_previews(self) -> tuple[BrowseGroupPreview, ...]:
        """Build per-document turn previews bound to first segment identities."""

        _document_name, entries = self._browse_document_projection()
        if not entries:
            return ()
        preferences = self._display_preferences.browse_grouping
        group_size = preferences.segments_per_group
        total_groups = preferences.group_count(len(entries))
        current_identity: object = (
            self.controller.current_workspace_identity
            if self.controller.has_workspace
            else self.controller.current_index
        )
        previews: list[BrowseGroupPreview] = []
        for start in range(0, len(entries), group_size):
            chunk = entries[start : start + group_size]
            first = chunk[0]
            previews.append(
                BrowseGroupPreview(
                    ordinal=len(previews) + 1,
                    total_groups=total_groups,
                    start_index=first[0],
                    end_index=chunk[-1][0] + 1,
                    source=first[1],
                    target=first[2],
                    issued_identity=first[3],
                    selected=any(
                        issued is current_identity
                        if self.controller.has_workspace
                        else issued == current_identity
                        for _local, _source, _target, issued in chunk
                    ),
                )
            )
        return tuple(previews)

    def _refresh_browse_group_button(self) -> None:
        """Refresh the single browse-header entry from current document state."""

        if not self._has_active_project():
            self.browse_group_button.setText("分组轮次")
            self.browse_group_button.setEnabled(False)
            self.browse_group_button.setProperty("groupActive", False)
            self.browse_group_button.setToolTip("打开项目后可配置浏览分组。")
            self.browse_group_turn_bar.set_previews((), document_name="")
            self.browse_group_button.style().unpolish(self.browse_group_button)
            self.browse_group_button.style().polish(self.browse_group_button)
            return
        document_name, entries = self._browse_document_projection()
        preferences = self._display_preferences.browse_grouping
        group_count = preferences.group_count(len(entries))
        active = preferences.should_show(len(entries))
        previews = self._browse_group_previews() if active else ()
        self.browse_group_turn_bar.set_display_mode(
            preferences.display_mode
        )
        self.browse_group_turn_bar.set_previews(
            previews,
            document_name=document_name,
        )
        self.browse_group_button.setEnabled(True)
        self.browse_group_button.setProperty("groupActive", active)
        if not preferences.enabled:
            self.browse_group_button.setText("分组轮次 · 不显示")
            tooltip = "分组轮次当前不显示；点击修改设置。"
        elif active:
            selected = next(
                (preview.ordinal for preview in previews if preview.selected),
                1,
            )
            self.browse_group_button.setText(
                f"轮次 {selected} / {group_count}"
            )
            tooltip = "当前文档轮次导航已显示；点击修改分组设置。"
        else:
            self.browse_group_button.setText("分组轮次")
            tooltip = (
                f"当前 {len(entries)} 段、{group_count} 组；超过 "
                f"{preferences.activation_group_threshold} 组或 "
                f"{preferences.activation_segment_threshold} 段时显示。"
            )
        self.browse_group_button.setToolTip(tooltip)
        self.browse_group_button.style().unpolish(self.browse_group_button)
        self.browse_group_button.style().polish(self.browse_group_button)

    def _open_browse_group_dialog(self) -> None:
        """Open the settings-only dialog for the embedded turn bar."""

        if not self._has_active_project():
            return
        document_name, entries = self._browse_document_projection()
        preferences = self._display_preferences.browse_grouping
        dialog = QtBrowseGroupDialog(
            preferences=preferences,
            document_name=document_name,
            segment_count=len(entries),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.saved_preferences is not None:
            try:
                saved = self.controller.update_display_preferences(
                    replace(
                        self._display_preferences,
                        browse_grouping=dialog.saved_preferences,
                    )
                )
            except (TypeError, ValueError, EditorControllerError) as exc:
                self._show_error("分组设置未保存", str(exc))
                return
            self._display_preferences = saved
            self._refresh_browse_group_button()
            self.statusBar().showMessage("已保存浏览分组设置。", 5000)
        return

    def _navigate_browse_group(self, issued_identity: object) -> None:
        """Jump from the embedded turn bar to its group-first segment."""

        if not self._has_active_project():
            return
        if self._chunk_segment_selection_session is not None:
            wanted = self._chunk_segment_identity_key(issued_identity)
            if wanted is None:
                return
            for row in range(self.browse_table.rowCount()):
                item = self.browse_table.item(row, 0)
                if item is None:
                    continue
                if self._chunk_segment_identity_key(
                    item.data(Qt.ItemDataRole.UserRole)
                ) != wanted:
                    continue
                selection = self.browse_table.selectionModel()
                if selection is not None:
                    selection.setCurrentIndex(
                        self.browse_table.model().index(row, 1),
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
                self.browse_table.scrollToItem(
                    item,
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                return
            return
        try:
            if self.controller.has_workspace:
                self.controller.go_to_workspace_segment(issued_identity)
            else:
                self.controller.go_to(int(issued_identity))
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法打开分组首段", str(exc))
            return
        self._refreshing = True
        try:
            self._select_project_index(self._active_index())
            self._render_current_segment()
            self._refresh_browse_table()
        finally:
            self._refreshing = False

    def _browse_current_cell_changed(
        self,
        current_row: int,
        _current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        """Make the selected browse row the current document projection."""

        if self._refreshing or current_row < 0 or not self._has_active_project():
            return
        if self._chunk_segment_selection_session is not None:
            self._chunk_segment_browse_selection_changed()
            return
        item = self.browse_table.item(current_row, 0)
        if item is None:
            return
        issued = item.data(Qt.ItemDataRole.UserRole)
        if issued is None:
            return
        previous_index = self._active_index()
        try:
            if self.controller.has_workspace:
                self.controller.go_to_workspace_segment(issued)
            else:
                self.controller.go_to(int(issued))
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法选择浏览段落", str(exc))
            return
        self._refreshing = True
        try:
            self._select_project_index(self._active_index())
            self._render_current_segment(
                reset_target_history=(self._active_index() != previous_index)
            )
        finally:
            self._refreshing = False

    def _activate_browse_row(self, row: int, _column: int) -> None:
        if self._chunk_segment_selection_session is not None:
            self._chunk_segment_browse_selection_changed()
            return
        item = self.browse_table.item(row, 0)
        if item is None:
            return
        previous_index = self._active_index()
        try:
            issued = item.data(Qt.ItemDataRole.UserRole)
            if issued is None:
                return
            if self.controller.has_workspace:
                self.controller.go_to_workspace_segment(issued)
            else:
                self.controller.go_to(int(issued))
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法打开浏览段落", str(exc))
            return
        if not self.set_workspace_mode(WorkspaceMode.EDIT):
            return
        self._refreshing = True
        try:
            self._select_project_index(self._active_index())
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _schedule_layout_refresh(self) -> None:
        if self.segment_density is SegmentDensity.WRAPPED:
            QTimer.singleShot(0, self._refresh_segment_item_sizes)
        if (
            self._has_active_project()
            and self.workspace_mode is WorkspaceMode.BROWSE
        ):
            QTimer.singleShot(0, self.browse_table.resizeRowsToContents)

    def _select_visible_row(self, row: int) -> None:
        if self._refreshing or row < 0:
            return
        item = self.segment_list.item(row)
        if item is None:
            return
        issued = item.data(Qt.ItemDataRole.UserRole)
        if issued is None:
            return
        previous_index = self._active_index()
        try:
            if self.controller.has_workspace:
                self.controller.go_to_workspace_segment(issued)
            else:
                self.controller.go_to(int(issued))
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法切换段落", str(exc))
            return
        self._refreshing = True
        try:
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _navigate(self, direction: int) -> None:
        if not self._has_active_project():
            return
        previous_index = self._active_index()
        if self.controller.has_workspace:
            self.controller.move_workspace(
                direction,
                unconfirmed_only=self.unconfirmed_filter.isChecked(),
            )
        else:
            self.controller.move(
                direction,
                unconfirmed_only=self.unconfirmed_filter.isChecked(),
            )
        self._refreshing = True
        try:
            self._select_project_index(self._active_index())
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _select_project_index(self, project_index: int) -> None:
        issued_identity = (
            self.controller.workspace_view.segments[project_index].identity
            if self.controller.has_workspace
            else project_index
        )
        for row in range(self.segment_list.count()):
            item = self.segment_list.item(row)
            item_identity = item.data(Qt.ItemDataRole.UserRole)
            matches = (
                item_identity is issued_identity
                if self.controller.has_workspace
                else item_identity == issued_identity
            )
            if matches:
                self.segment_list.setCurrentRow(row)
                return
        self.segment_list.setCurrentRow(-1)

    def _filter_changed(self, enabled: bool) -> None:
        if self._refreshing or not self._has_active_project():
            return
        previous_index = self._active_index()
        if enabled and self.controller.current_segment.confirmed:
            next_unconfirmed = next(
                (
                    index
                    for index, segment in enumerate(self._active_segments())
                    if not segment.confirmed
                ),
                None,
            )
            if next_unconfirmed is not None:
                if self.controller.has_workspace:
                    self.controller.go_to_workspace_index(
                        next_unconfirmed,
                        project=self.controller.workspace_view.project,
                    )
                else:
                    self.controller.go_to(next_unconfirmed)
        self._refreshing = True
        try:
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _refresh_project_search_controls(self) -> None:
        """Project the current JSON and matcher gates without executing search."""

        self.project_search_toggle.setEnabled(self._has_active_project())
        self.project_search_clear.setEnabled(self._has_active_project())
        self.project_search_scope.setVisible(self.controller.has_workspace)
        display = None
        search_available = False
        advanced_available = False
        project_capability = self.controller.project_tool_capability()
        if (
            not self.controller.has_workspace
            and not project_capability.single_json_tools_available
        ):
            code = project_capability.unavailable_reason or (
                "PROJECT_SEARCH.PROJECT_GATE_INVALID"
            )
            detail = (
                "请打开一个本地 JSON 项目"
                if code == "PROJECT_TOOLS.NO_PROJECT"
                else "当前项目不是本规格支持的单 JSON 项目"
            )
            message = f"搜索不可用：{code}（{detail}）。"
        else:
            display = self.controller.project_search_matcher_display()
            if display.state is TextMatcherState.BASIC_VALIDATED:
                search_available = True
                message = (
                    "BASIC 搜索可用；Match Case / Whole Word 属于第二阶段，"
                    "当前不参与搜索。"
                )
            elif display.state is TextMatcherState.TEXT_V1_VALIDATED:
                search_available = True
                advanced_available = True
                message = (
                    "TEXT_V1 搜索可用；Match Case / Whole Word 已使用 Core 语义。"
                )
            else:
                code = display.safe_reason or "MATCHER.UNAVAILABLE"
                message = f"搜索不可用：{code}。"

        self.project_search_button.setEnabled(search_available)
        for checkbox in (
            self.project_search_source,
            self.project_search_target,
            self.project_search_speaker,
        ):
            checkbox.setEnabled(search_available)
        self.project_search_status.setEnabled(search_available)
        self.project_search_scope.setEnabled(
            search_available and self.controller.has_workspace
        )
        self.project_search_match_case.setEnabled(advanced_available)
        self.project_search_whole_word.setEnabled(advanced_available)
        self.project_search_capability.setText(message)
        self.project_search_capability.setAccessibleName(
            f"项目搜索能力状态：{message}"
        )
        self.project_search_capability.setToolTip(message)

        report = self.current_project_search_report
        if report is not None and (
            not search_available
            or display is None
            or report.capability != display
        ):
            self._clear_project_search_results(
                "搜索能力已变化；请重新搜索。"
            )

    def _selected_project_search_fields(self) -> tuple[SearchField, ...]:
        return tuple(
            field
            for field, checkbox in (
                (SearchField.SOURCE, self.project_search_source),
                (SearchField.TARGET, self.project_search_target),
                (SearchField.SPEAKER, self.project_search_speaker),
            )
            if checkbox.isChecked()
        )

    def _project_search_options(self) -> SearchOptions:
        """Ignore disabled visual state and preserve BASIC false/false."""

        return SearchOptions(
            match_case=(
                self.project_search_match_case.isEnabled()
                and self.project_search_match_case.isChecked()
            ),
            whole_word=(
                self.project_search_whole_word.isEnabled()
                and self.project_search_whole_word.isChecked()
            ),
        )

    def _project_search_status_filter(
        self,
    ) -> SegmentTranslationStatus | None:
        value = self.project_search_status.currentData()
        if value is None:
            return None
        return SegmentTranslationStatus(str(value))

    def _submit_project_search(self) -> None:
        if self._chunk_segment_selection_session is not None:
            return
        """Issue one Controller search and activate its first issued hit."""

        if not self.project_search_button.isEnabled():
            self._clear_project_search_results(
                "搜索当前不可用；请查看能力说明。"
            )
            return
        query = self.project_search_input.text()
        if not query.strip():
            self._clear_project_search_results("请输入有效关键词。")
            self.statusBar().showMessage("请输入有效搜索关键词。", 5000)
            return
        fields = self._selected_project_search_fields()
        if not fields:
            self._clear_project_search_results("请至少选择一个搜索字段。")
            self.statusBar().showMessage("请至少选择一个搜索字段。", 5000)
            return
        try:
            if self.controller.has_workspace:
                scope = self.workspace_search_scope.currentData()
                if (
                    self._chunk_view is not None
                    and self._chunk_view.mode is ChunkApplicationMode.ACTIVE
                    and self._chunk_view.current_chunk_id is not None
                    and self.chunk_controller is not None
                ):
                    if not isinstance(scope, CollaborativeSearchScopeV2):
                        scope = CollaborativeSearchScopeV2(str(scope))
                    request = CollaborativeWorkspaceSearchRequestV2(
                        query=query,
                        fields=fields,
                        options=self._project_search_options(),
                        status=self._project_search_status_filter(),
                        scope=scope,
                    )
                    report = self.chunk_controller.search_workspace(request)
                else:
                    if not isinstance(scope, SearchScope):
                        scope = SearchScope(str(scope))
                    request = WorkspaceSearchRequest(
                        query=query,
                        fields=fields,
                        options=self._project_search_options(),
                        status=self._project_search_status_filter(),
                        scope=scope,
                    )
                    report = self.controller.search_workspace(request)
                self.current_workspace_search_report = report
            else:
                request = ProjectSearchRequest(
                    query=query,
                    fields=fields,
                    options=self._project_search_options(),
                    status=self._project_search_status_filter(),
                )
                report = self.controller.search_project(request)
        except Exception as exc:
            self._clear_project_search_results(f"搜索失败：{exc}。")
            self.statusBar().showMessage(f"项目搜索失败：{exc}", 7000)
            return

        self.current_project_search_report = report
        if not report.hits:
            self._project_search_ordinal = None
            self.project_search_result.setText("没有找到匹配结果。")
            self.project_search_result.setAccessibleName(
                "项目搜索结果状态：没有找到匹配结果"
            )
            self.project_search_preview.setText("预览：—")
            self.project_search_preview.setAccessibleName(
                "当前搜索结果预览：无"
            )
            self.project_search_previous.setEnabled(False)
            self.project_search_next.setEnabled(False)
            self.statusBar().showMessage("项目搜索没有找到匹配结果。", 5000)
            return

        if self._activate_project_search_ordinal(0):
            self.statusBar().showMessage(
                f"项目搜索找到 {report.total} 个结果。",
                5000,
            )

    def _navigate_project_search(self, direction: int) -> None:
        if self._chunk_segment_selection_session is not None:
            return
        report = self.current_project_search_report
        ordinal = self._project_search_ordinal
        if report is None or ordinal is None or direction not in (-1, 1):
            return
        candidate = ordinal + direction
        if 0 <= candidate < report.total:
            _ = self._activate_project_search_ordinal(candidate)

    def _activate_project_search_ordinal(self, ordinal: int) -> bool:
        report = self.current_project_search_report
        if report is None or not 0 <= ordinal < report.total:
            return False
        previous_index = self._active_index()
        try:
            if self.controller.has_workspace:
                if type(report) is CollaborativeWorkspaceSearchReportV2:
                    if self.chunk_controller is None:
                        raise EditorControllerError("CHUNK.PERMISSION_STALE")
                    _ = self.chunk_controller.go_to_search_hit(
                        report.hits[ordinal]
                    )
                else:
                    _ = self.controller.go_to_workspace_search_hit(
                        report.hits[ordinal]
                    )
            else:
                _ = self.controller.go_to_search_hit(report.hits[ordinal])
        except Exception as exc:
            self._clear_project_search_results(
                f"搜索结果已过期：{exc}；请重新搜索。"
            )
            self.statusBar().showMessage(
                "搜索结果已过期，请重新搜索。",
                7000,
            )
            return False

        self._project_search_ordinal = ordinal
        self._refreshing = True
        try:
            self._select_project_index(self._active_index())
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
            if self.workspace_mode is WorkspaceMode.BROWSE:
                self._refresh_browse_table()
        finally:
            self._refreshing = False
        self._render_project_search_hit()
        return True

    def _render_project_search_hit(self) -> None:
        report = self.current_project_search_report
        ordinal = self._project_search_ordinal
        if report is None or ordinal is None or not 0 <= ordinal < report.total:
            return
        issued_hit = report.hits[ordinal]
        access_text = ""
        if type(issued_hit) is CollaborativeWorkspaceSearchHitV2:
            hit = issued_hit.workspace_hit
            access_text = (
                " · 可编辑"
                if issued_hit.access.may_edit_target
                else " · 只读"
            )
        else:
            hit = issued_hit
        field = hit.field.value.upper()
        position = (
            f"章节段落 {hit.local_segment_id}"
            if self.controller.has_workspace
            else f"段落 {hit.segment_index + 1}"
        )
        result = (
            f"共 {report.total} 个结果 · 第 {ordinal + 1} 个 · "
            f"{field} · {position}{access_text}"
        )
        preview = f"预览（{field}）：{hit.preview}"
        self.project_search_result.setText(result)
        self.project_search_result.setAccessibleName(
            f"项目搜索结果状态：{result}"
        )
        self.project_search_result.setToolTip(result)
        self.project_search_preview.setText(preview)
        self.project_search_preview.setAccessibleName(
            f"当前搜索结果预览：{preview}"
        )
        self.project_search_preview.setToolTip(hit.preview)
        self.project_search_previous.setEnabled(ordinal > 0)
        self.project_search_next.setEnabled(ordinal + 1 < report.total)

    def _clear_project_search_results(
        self,
        message: str = "尚未搜索。",
        *,
        clear_controller: bool = True,
    ) -> None:
        if clear_controller:
            if self.controller.has_workspace:
                self.controller.clear_workspace_search()
            else:
                self.controller.clear_project_search()
        self.current_project_search_report = None
        self.current_workspace_search_report = None
        self._project_search_ordinal = None
        self.project_search_result.setText(message)
        self.project_search_result.setAccessibleName(
            f"项目搜索结果状态：{message}"
        )
        self.project_search_preview.setText("预览：—")
        self.project_search_preview.setAccessibleName("当前搜索结果预览：无")
        self.project_search_preview.setToolTip("当前命中字段的原始纯文本预览")
        self.project_search_previous.setEnabled(False)
        self.project_search_next.setEnabled(False)

    def confirm_current(self) -> bool:
        if not self._has_active_project():
            return False
        previous_index = self._active_index()
        try:
            result = self.controller.confirm_current()
        except EditorControllerError as exc:
            self._show_error("无法确认译文", str(exc))
            self.statusBar().showMessage("确认失败；当前段保持待确认。", 7000)
            return False
        if result.write_report.errors:
            self._show_error("记忆库写入失败", "\n".join(result.write_report.errors))
            self.statusBar().showMessage("记忆库写入失败；当前段未确认。", 7000)
            return False
        self._refreshing = True
        try:
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=(
                    self._active_index() != previous_index
                )
            )
        finally:
            self._refreshing = False
        self._refresh_chunk_view()
        self._update_title()
        self.statusBar().showMessage(
            f"译文已确认 · 已写入 {len(result.write_report.written_resource_ids)} 个记忆库",
            6000,
        )
        return True

    def _refresh_project_tool_actions(self) -> None:
        """Project the Controller's single-JSON gate onto project tools."""

        try:
            capability = self.controller.project_tool_capability()
            capability.__post_init__()
        except (TypeError, ValueError, EditorControllerError):
            self.speaker_inventory_action.setEnabled(False)
            self.preprocess_action.setEnabled(False)
            self.speaker_inventory_action.setToolTip(
                "当前项目工具能力无法验证"
            )
            self.preprocess_action.setToolTip("当前项目工具能力无法验证")
            return
        available = capability.single_json_tools_available
        self.speaker_inventory_action.setEnabled(available)
        self.preprocess_action.setEnabled(available)
        if available:
            self.speaker_inventory_action.setToolTip(
                "只读盘点当前 JSON 项目的 raw speaker 与出现次数"
            )
            self.preprocess_action.setToolTip(
                "预览并显式应用有序的 target literal 替换规则"
            )
        else:
            self.speaker_inventory_action.setToolTip(
                "Raw speaker 盘点仅适用于当前单个 JSON 项目"
            )
            self.preprocess_action.setToolTip(
                "文字预处理仅适用于当前单个 JSON 项目"
            )

    def _open_speaker_inventory_dialog(self) -> None:
        """Open the Controller-owned, read-only raw speaker inventory."""

        if not self.speaker_inventory_action.isEnabled():
            self.statusBar().showMessage(
                "Raw speaker 盘点仅适用于当前单个 JSON 项目。",
                6000,
            )
            return
        try:
            dialog = QtSpeakerInventoryDialog(self.controller, self)
        except (EditorControllerError, TypeError, ValueError) as error:
            self._show_error("无法盘点 raw speaker", str(error))
            self.statusBar().showMessage(
                "Speaker 盘点失败；项目保持不变。",
                6000,
            )
            return
        dialog.exec()

    def _open_preprocess_dialog(self) -> None:
        """Open the dedicated Controller-only preprocessing surface."""

        if not self.preprocess_action.isEnabled():
            self.statusBar().showMessage(
                "文字预处理仅适用于当前单个 JSON 项目。",
                6000,
            )
            return
        dialog = QtPreprocessDialog(self.controller, self)
        dialog.mutation_committed.connect(self._preprocessing_changed)
        dialog.exec()

    def _preprocessing_changed(self, report: object) -> None:
        """Refresh all four project projections after one committed mutation."""

        if type(report) is not BatchOperationReport:
            raise TypeError("preprocessing refresh requires BatchOperationReport")
        report.__post_init__()
        self._refresh_from_controller()
        action = "应用" if report.operation == "apply" else "撤销"
        self.statusBar().showMessage(
            f"已{action}批量预处理 · {len(report.changed_segment_ids)} 个段落。",
            6000,
        )

    def _refresh_from_controller(self) -> None:
        """Re-render edit, browse, progress/dirty and suggestions from one snapshot."""

        if not self._has_active_project():
            self._show_empty_state()
            self._update_title()
            return
        if self.current_project_search_report is not None:
            self._clear_project_search_results(
                "项目内容已变化；请重新搜索。"
            )
        self._refreshing = True
        try:
            source_locale, target_locale = self._active_locales()
            self.project_name_label.setText(self._active_project_name())
            self.language_label.setText(f"{source_locale}  →  {target_locale}")
            self.segment_count_label.setText(str(len(self._active_segments())))
            self._refresh_chunk_view()
            self._refresh_workspace_documents_menu()
            self._populate_segment_list()
            self._render_current_segment()
            self._refresh_browse_table()
        finally:
            self._refreshing = False
        self._refresh_project_tool_actions()
        self._update_title()

    def _open_settings(self) -> None:
        dialog = self.create_settings_dialog()
        dialog.exec()

    def create_settings_dialog(self) -> QtSettingsDialog:
        """Create the controller-only settings seam and connect resource refresh."""

        dialog = QtSettingsDialog(
            self.controller,
            self,
            tmx_export_service=self.tmx_export_coordinator,
        )
        dialog.resources_changed.connect(self._resources_changed)
        dialog.tm_threshold_changed.connect(self._settings_tm_threshold_changed)
        dialog.fuzzy_validation_changed.connect(
            self._settings_fuzzy_validation_changed
        )
        dialog.destroyed.connect(
            lambda _object=None: self._forget_settings_dialog(dialog)
        )
        dialog.term_suggestions_changed.connect(
            self._term_suggestions_changed
        )
        self.settings_dialog = dialog
        return dialog

    def _forget_settings_dialog(self, dialog: QtSettingsDialog) -> None:
        if self.settings_dialog is dialog:
            self.settings_dialog = None

    def _resources_changed(self) -> None:
        if self._has_active_project():
            self.refresh_suggestions()
        self.statusBar().showMessage("语言资源已更新，当前段建议已刷新。", 6000)

    def _term_suggestions_changed(self) -> None:
        if self._has_active_project():
            self.refresh_suggestions()
        self.statusBar().showMessage("术语已更新，当前段建议已刷新。", 6000)

    def _refresh_manage_terms_menu(self) -> None:
        """Project current writable termbases into the main Termbase entry."""

        self.manage_terms_menu.clear()
        writable = tuple(
            resource
            for resource in self.controller.list_resources()
            if resource.kind is ResourceKind.TERMBASE
            and resource.active
            and resource.update
        )
        if not writable:
            unavailable = self.manage_terms_menu.addAction(
                "无 Active+Update 术语表"
            )
            unavailable.setObjectName("manageTermsMainUnavailable")
            unavailable.setEnabled(False)
            return
        for resource in writable:
            action = self.manage_terms_menu.addAction(resource.name)
            action.setObjectName(f"manageTermsMain_{resource.id}")
            action.setToolTip(f"打开 {resource.name} 的集中式术语管理")
            action.setStatusTip(action.toolTip())
            action.triggered.connect(
                lambda _checked=False, resource_id=resource.id: self._open_main_termbase_dialog(
                    resource_id
                )
            )

    def _open_main_termbase_dialog(self, resource_id: str) -> None:
        """Open the same Controller-only dialog as the settings-row entry."""

        resource = next(
            (
                configured
                for configured in self.controller.list_resources()
                if configured.id == resource_id
                and configured.kind is ResourceKind.TERMBASE
                and configured.active
                and configured.update
            ),
            None,
        )
        if resource is None:
            self._refresh_manage_terms_menu()
            self.statusBar().showMessage(
                "当前术语表已不可写；请在语言资源设置中刷新。",
                6000,
            )
            return
        dialog = QtTermbaseDialog(
            self.controller,
            resource.id,
            resource.name,
            self,
        )
        dialog.terms_committed.connect(self._term_suggestions_changed)
        dialog.exec()

    def refresh_suggestions(self) -> SuggestionBundle:
        """Render the current controller bundle as safe, actionable cards."""

        self._refresh_project_search_controls()
        self._refresh_manage_terms_menu()
        if not self._has_active_project():
            self.current_suggestions = SuggestionBundle()
            self.current_tm_report = None
            self._refresh_tm_threshold_entry()
            return self.current_suggestions
        report: TMSuggestionReport | None = None
        tm_query_failed = False
        if self.controller.tm_suggestion_reports_enabled:
            try:
                report = self.controller.tm_suggestion_report()
            except EditorControllerError:
                tm_query_failed = True
                bundle = SuggestionBundle(
                    terms=self.controller.term_suggestions(),
                )
            else:
                bundle = SuggestionBundle(
                    terms=self.controller.term_suggestions(),
                )
        else:
            bundle = self.controller.suggestions()
        self.current_suggestions = bundle
        self.current_tm_report = report
        self._refresh_tm_threshold_entry()
        self.source_display.setHtml(
            render_highlighted_source(self.controller.current_segment.source, bundle.terms)
        )
        self._clear_layout(self.tm_cards_layout)
        self._clear_layout(self.term_cards_layout)

        tm_matches: tuple[TMSuggestion | LegacyExactTMSuggestion, ...]
        if report is not None:
            tm_matches = report.suggestions
        else:
            tm_matches = bundle.tm_matches
        state_message = self._tm_state_message(
            report=report,
            query_failed=tm_query_failed,
            has_suggestions=bool(tm_matches),
        )
        if state_message is not None:
            self.tm_cards_layout.addWidget(
                self._empty_suggestion(
                    state_message,
                    object_name="tmSuggestionState",
                )
            )
        if tm_matches:
            for index, suggestion in enumerate(tm_matches):
                self.tm_cards_layout.addWidget(self._tm_card(index, suggestion))
        self.tm_cards_layout.addStretch()

        if bundle.terms:
            for index, suggestion in enumerate(bundle.terms):
                self.term_cards_layout.addWidget(self._term_card(index, suggestion))
        else:
            self.term_cards_layout.addWidget(self._empty_suggestion("当前段暂无术语建议。"))
        self.term_cards_layout.addStretch()
        return bundle

    def _refresh_tm_threshold_entry(self) -> None:
        """Render the compact entry from fresh defensive Controller values."""

        configure_tm_threshold_entry(
            self.tm_threshold_chip,
            self.tm_threshold_state,
            preferences=self.controller.tm_preferences(),
            retrieval_status=self.controller.tm_retrieval_status(),
            fuzzy_validation=self.controller.tm_fuzzy_validation_status(),
        )

    def _request_tm_threshold_update(self) -> None:
        """Submit one chip edit through the Controller and refresh visible cards."""

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
            self.statusBar().showMessage(
                "Fuzzy 阈值未更新；当前值保持不变。",
                7000,
            )
            return
        if outcome.succeeded and self._has_active_project():
            self.refresh_suggestions()
        else:
            self._refresh_tm_threshold_entry()
        if (
            outcome.preferences != previous
            and self.settings_dialog is not None
        ):
            self.settings_dialog.refresh_resources()
        self.statusBar().showMessage(tm_threshold_feedback(outcome), 7000)

    def _settings_tm_threshold_changed(self, outcome: object) -> None:
        """Refresh cards when the settings entry updates the shared preference."""

        if type(outcome) is not TMThresholdUpdateOutcome:
            raise TypeError("settings TM threshold outcome is invalid")
        outcome.__post_init__()
        if self._has_active_project():
            self.refresh_suggestions()
        else:
            self._refresh_tm_threshold_entry()
        self.statusBar().showMessage(
            tm_threshold_feedback(outcome),
            7000,
        )

    def _settings_fuzzy_validation_changed(self, status: object) -> None:
        if type(status) is not FuzzyValidationDisplay:
            raise TypeError("Fuzzy validation display is invalid")
        status.__post_init__()
        if status.state is FuzzyValidationState.RUNNING:
            self._fuzzy_validation_timer.start()
            self._refresh_tm_threshold_entry()
            self.statusBar().showMessage("Fuzzy 性能验证中。", 5000)
            return
        self._fuzzy_validation_timer.stop()
        if self._has_active_project():
            self.refresh_suggestions()
        else:
            self._refresh_tm_threshold_entry()
        dialog = self.settings_dialog
        if dialog is not None and dialog.isVisible():
            dialog.refresh_resources()
            dialog.status_label.setText(
                "Fuzzy 性能资格已验证并保存到本机。"
                if status.state is FuzzyValidationState.SUCCEEDED
                else "Fuzzy 性能资格验证未通过。"
            )

    def _poll_fuzzy_validation(self) -> None:
        status = self.controller.tm_fuzzy_validation_status()
        if status.state is FuzzyValidationState.RUNNING:
            self._refresh_tm_threshold_entry()
            return
        self._settings_fuzzy_validation_changed(status)

    @staticmethod
    def _tm_state_message(
        *,
        report: TMSuggestionReport | None,
        query_failed: bool,
        has_suggestions: bool,
    ) -> str | None:
        if query_failed:
            return "TM 查询失败，请稍后重试。"
        if report is None:
            return None if has_suggestions else "当前段暂无翻译记忆建议。"

        resource_problems = tuple(
            status
            for status in report.resource_statuses
            if status.mode in (
                TMResourceDisplayMode.DEGRADED,
                TMResourceDisplayMode.SOURCE_DIVERGED,
                TMResourceDisplayMode.UNAVAILABLE,
            )
        )
        if resource_problems:
            names = "、".join(status.resource_name for status in resource_problems)
            query_failure = any(
                "QUERY" in code
                for status in resource_problems
                for code in status.safe_codes
            )
            if query_failure:
                return f"TM 资源查询失败：{names}。"
            return f"TM 资源当前不可用：{names}。"

        capability_closed = bool(report.retrieval_status.safe_codes) or any(
            status.safe_codes for status in report.resource_statuses
        )
        if capability_closed:
            return "部分 TM 匹配能力当前不可用。"
        if not has_suggestions:
            return "当前段暂无翻译记忆建议。"
        return None

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _plain_label(text: str, object_name: str = "") -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if object_name:
            label.setObjectName(object_name)
        return label

    def _chunk_target_editable(self) -> bool:
        return (
            self._chunk_view is None
            and not (
                self.controller.has_workspace and self.chunk_controller is not None
            )
        ) or (
            self._chunk_view is not None
            and self._chunk_view.current_segment_access.may_edit_target
        )

    def _tm_card(
        self,
        index: int,
        suggestion: TMSuggestion | LegacyExactTMSuggestion,
    ) -> QWidget:
        card = QFrame()
        card.setObjectName(f"tmCard_{index}")
        card.setProperty("suggestionCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 11, 12, 11)

        if type(suggestion) is TMSuggestion:
            heading = QHBoxLayout()
            match_type = QLabel(suggestion.match_type.value)
            match_type.setObjectName(f"tmMatchType_{index}")
            match_type.setProperty("tmMatchType", True)
            similarity = QLabel(
                f"{round(suggestion.final_similarity * 100)}%"
            )
            similarity.setObjectName(f"tmSimilarity_{index}")
            similarity.setProperty("matchBadge", True)
            heading.addWidget(match_type)
            heading.addStretch()
            heading.addWidget(similarity, alignment=Qt.AlignmentFlag.AlignTop)
            layout.addLayout(heading)

            if suggestion.matched_source != suggestion.query_source:
                matched_source = self._plain_label(
                    f"实际命中原文：{suggestion.matched_source}",
                    f"tmMatchedSource_{index}",
                )
                matched_source.setProperty("tmMatchedSource", True)
                layout.addWidget(matched_source)

            target = self._plain_label(
                suggestion.target,
                f"tmTarget_{index}",
            )
            target.setProperty("suggestionTarget", True)
            layout.addWidget(target)

            footer = QHBoxLayout()
            resource = self._plain_label(
                suggestion.provenance.resource_name,
                f"tmResource_{index}",
            )
            resource.setProperty("suggestionProvenance", True)
            apply_button = _TMApplyButton("应用译文")
            apply_button.setObjectName(f"applyTm_{index}")
            apply_accessible_name = (
                f"应用来自 {suggestion.provenance.resource_name} 的 "
                f"{suggestion.match_type.value} 译文"
            )
            apply_button.setAccessibleName(apply_accessible_name)
            apply_button.setToolTip(
                f"{apply_accessible_name}；不会自动确认或跳转段落"
            )
            apply_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            apply_button.setEnabled(self._chunk_target_editable())
            apply_button.clicked.connect(
                lambda _checked=False, current=suggestion: self.apply_tm_suggestion(
                    current
                )
            )
            footer.addWidget(resource, 1)
            footer.addWidget(apply_button)
            layout.addLayout(footer)
            return card

        if type(suggestion) is not LegacyExactTMSuggestion:
            raise TypeError("unsupported TM suggestion contract")

        heading = QHBoxLayout()
        source = self._plain_label(suggestion.source, "suggestionSource")
        badge = QLabel(f"{round(suggestion.similarity * 100)}%")
        badge.setObjectName("matchBadge")
        heading.addWidget(source, 1)
        heading.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        target = self._plain_label(suggestion.target, "suggestionTarget")
        footer = QHBoxLayout()
        provenance = self._plain_label(
            f"{suggestion.resource_name} · {suggestion.match_type}",
            "suggestionProvenance",
        )
        apply_button = _TMApplyButton("应用译文")
        apply_button.setObjectName(f"applyTm_{index}")
        apply_accessible_name = (
            f"应用来自 {suggestion.resource_name} 的 {suggestion.match_type} 译文"
        )
        apply_button.setAccessibleName(apply_accessible_name)
        apply_button.setToolTip(
            f"{apply_accessible_name}；不会自动确认或跳转段落"
        )
        apply_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        apply_button.setEnabled(self._chunk_target_editable())
        apply_button.clicked.connect(
            lambda _checked=False, current=suggestion: self.apply_tm_suggestion(current)
        )
        footer.addWidget(provenance, 1)
        footer.addWidget(apply_button)
        layout.addLayout(heading)
        layout.addWidget(target)
        layout.addLayout(footer)
        return card

    def _term_card(self, index: int, suggestion: TermSuggestion) -> QWidget:
        card = QFrame()
        card.setObjectName(f"termCard_{index}")
        card.setProperty("suggestionCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 11, 12, 11)
        pair = QHBoxLayout()
        source = self._plain_label(suggestion.source_term, "termSource")
        arrow = QLabel("→")
        arrow.setObjectName("termArrow")
        target = self._plain_label(suggestion.target_term, "termTarget")
        pair.addWidget(source, 1)
        pair.addWidget(arrow)
        pair.addWidget(target, 1)
        footer = QHBoxLayout()
        provenance = self._plain_label(suggestion.resource_name, "suggestionProvenance")
        insert_button = QPushButton("插入译文")
        insert_button.setObjectName(f"insertTerm_{index}")
        insert_button.setEnabled(self._chunk_target_editable())
        insert_button.clicked.connect(
            lambda _checked=False, current=suggestion: self.insert_term_suggestion(current)
        )
        footer.addWidget(provenance, 1)
        footer.addWidget(insert_button)
        layout.addLayout(pair)
        layout.addLayout(footer)
        return card

    def _empty_suggestion(
        self,
        message: str,
        *,
        object_name: str = "emptySuggestion",
    ) -> QLabel:
        label = self._plain_label(message, object_name)
        label.setProperty("emptySuggestion", True)
        label.setAccessibleName(message)
        label.setToolTip(message)
        return label

    def apply_tm_suggestion(
        self,
        suggestion: TMSuggestion | LegacyExactTMSuggestion,
    ) -> bool:
        try:
            self.controller.apply_tm_suggestion(suggestion)
        except EditorControllerError:
            self.statusBar().showMessage(
                "未应用 TM 建议：建议已过期或当前不可用。",
                7000,
            )
            return False
        cursor = self.target_editor.textCursor()
        cursor.beginEditBlock()
        try:
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(self.controller.current_segment.target)
        finally:
            cursor.endEditBlock()
        self.target_editor.setTextCursor(cursor)
        self._refresh_chunk_view()
        if type(suggestion) is TMSuggestion:
            resource_name = suggestion.provenance.resource_name
        elif type(suggestion) is LegacyExactTMSuggestion:
            resource_name = suggestion.resource_name
        else:
            raise TypeError("unsupported TM suggestion contract")
        self.statusBar().showMessage(f"已应用来自 {resource_name} 的译文。", 5000)
        return True

    def insert_term_suggestion(self, suggestion: TermSuggestion) -> bool:
        cursor = self.target_editor.textCursor()
        position = cursor.position()
        try:
            self.controller.insert_term_suggestion(suggestion, position)
        except EditorControllerError as exc:
            self._show_error("无法插入术语", str(exc))
            return False
        cursor.clearSelection()
        cursor.beginEditBlock()
        try:
            cursor.insertText(suggestion.target_term)
        finally:
            cursor.endEditBlock()
        self.target_editor.setTextCursor(cursor)
        self._refresh_chunk_view()
        self.statusBar().showMessage(f"已插入术语：{suggestion.target_term}", 5000)
        return True

    def _refresh_target_from_controller(self) -> None:
        self._refreshing = True
        try:
            self._replace_target_text(self.controller.current_segment.target)
        finally:
            self._refreshing = False
        self._update_segment_item(self._active_index())
        self._render_progress_state()
        self._update_title()

    def _replace_target_text(self, text: str) -> None:
        blocker = QSignalBlocker(self.target_editor)
        try:
            self.target_editor.setPlainText(text)
            self.target_editor.document().clearUndoRedoStacks()
        finally:
            del blocker

    def add_term(self, source: str, target: str) -> bool:
        try:
            resource = self.controller.add_term(source, target)
        except EditorControllerError as exc:
            self._show_error("无法添加术语", str(exc))
            return False
        self.refresh_suggestions()
        self.statusBar().showMessage(f"已添加到 {resource.name}：{source} → {target}", 6000)
        return True

    def _prompt_add_term(self) -> None:
        prompt = QDialog(self)
        prompt.setWindowTitle("添加术语")
        layout = QVBoxLayout(prompt)
        layout.addWidget(QLabel("源术语"))
        source_input = QLineEdit()
        source_input.setObjectName("addTermSource")
        layout.addWidget(source_input)
        layout.addWidget(QLabel("目标术语"))
        target_input = QLineEdit()
        target_input.setObjectName("addTermTarget")
        layout.addWidget(target_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(prompt.accept)
        buttons.rejected.connect(prompt.reject)
        layout.addWidget(buttons)
        if prompt.exec() == QDialog.DialogCode.Accepted:
            self.add_term(source_input.text(), target_input.text())

    def _confirm_unsaved(self) -> bool:
        if not self._has_active_project() or not self._active_dirty():
            return True
        prompt = QMessageBox(
            QMessageBox.Icon.Warning,
            "存在未保存修改",
            "当前项目有未保存修改。保存后继续吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            self,
        )
        prompt.setDefaultButton(QMessageBox.StandardButton.Save)
        prompt.button(QMessageBox.StandardButton.Save).setText("保存并继续")
        prompt.button(QMessageBox.StandardButton.Discard).setText("放弃修改")
        prompt.button(QMessageBox.StandardButton.Cancel).setText("取消")
        decision = QMessageBox.StandardButton(prompt.exec())
        if decision == QMessageBox.StandardButton.Save:
            return self._choose_save()
        return decision == QMessageBox.StandardButton.Discard

    def _update_title(self) -> None:
        if not self._has_active_project():
            self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
            return
        dirty = " *" if self._active_dirty() else ""
        self.setWindowTitle(f"{self._active_project_name()}{dirty} · LocalCAT")

    def _show_error(self, title: str, message: str) -> None:
        show_localized_critical(self, title=title, text=message)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "top_bar_layout"):
            self._apply_top_bar_responsiveness(event.size().width())
        self._schedule_layout_refresh()

    def _apply_top_bar_responsiveness(self, width: int) -> None:
        """Keep every project command inspectable at the 1080px minimum."""

        compact = width < 1220
        self.brand_name_label.setVisible(not compact)
        self.brand_tagline_label.setVisible(not compact)
        self.top_separator.setVisible(not compact)
        self.language_label.setVisible(not compact)
        if compact:
            self.top_bar_layout.setContentsMargins(12, 10, 12, 10)
            self.top_bar_layout.setSpacing(6)
            self.project_name_label.setMaximumWidth(135)
            self.progress_bar.setFixedWidth(125)
        else:
            self.top_bar_layout.setContentsMargins(22, 12, 20, 12)
            self.top_bar_layout.setSpacing(12)
            self.project_name_label.setMaximumWidth(16777215)
            self.progress_bar.setFixedWidth(180)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._confirm_unsaved():
            if self._chunk_segment_selection_session is not None:
                self._finish_chunk_segment_selection(
                    False,
                    restore_manager=False,
                )
            manager = self._chunk_manager_dialog
            if manager is not None:
                try:
                    manager.close()
                except RuntimeError:
                    pass
            event.accept()
        else:
            event.ignore()


_EDITOR_STYLE = """
QMainWindow#editorWindow, QWidget#windowShell {
    background: #eef2f7;
    color: #1a2a3c;
    font-family: "Inter", "Noto Sans CJK SC", sans-serif;
    font-size: 13px;
}
QFrame#topBar {
    background: #062f5c;
    border: none;
}
QLabel#brandMark {
    background: #0aa0c8;
    color: white;
    border-radius: 17px;
    font-size: 18px;
    font-weight: 800;
}
QLabel#brandName {
    color: white;
    font-size: 18px;
    font-weight: 750;
}
QLabel#brandTagline {
    color: #8eb5d2;
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 1px;
}
QFrame#topSeparator {
    color: #31577d;
    margin: 2px 8px;
}
QLabel#projectName {
    color: #f7fbff;
    font-weight: 650;
}
QLabel#languageDirection {
    color: #93b8d4;
    font-size: 11px;
}
QToolButton {
    min-width: 52px;
    min-height: 32px;
    padding: 0 8px;
    color: #d6e7f4;
    background: transparent;
    border: 1px solid #31577d;
    border-radius: 5px;
    font-weight: 650;
    font-size: 13px;
}
QToolButton:hover {
    color: white;
    background: #124875;
    border-color: #4a789e;
}
QToolButton:disabled {
    color: #6686a1;
    border-color: #244c70;
}
QToolButton#openProjectButton {
    padding-left: 8px;
    padding-right: 25px;
}
QToolButton#openProjectButton::menu-button {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    border-left: 1px solid #31577d;
}
QToolButton#openProjectButton::menu-arrow {
    image: none;
}
QComboBox#workspaceModeCombo {
    min-height: 30px;
    min-width: 104px;
    padding: 0 9px;
    color: #d6e7f4;
    background: #0b3e6a;
    border: 1px solid #315f86;
    border-radius: 5px;
    font-size: 13px;
    font-weight: 650;
}
QComboBox#workspaceModeCombo:hover,
QComboBox#workspaceModeCombo:focus {
    color: #ffffff;
    background: #124875;
    border-color: #4a789e;
}
QComboBox#workspaceModeCombo::drop-down {
    border: none;
    width: 26px;
}
QComboBox#workspaceModeCombo::down-arrow {
    image: none;
}
QProgressBar#projectProgress {
    min-height: 17px;
    max-height: 17px;
    color: #dff5fb;
    background: #0b3e6a;
    border: 1px solid #315f86;
    border-radius: 8px;
    text-align: center;
    font-size: 9px;
    font-weight: 700;
}
QProgressBar#projectProgress::chunk {
    border-radius: 7px;
    background: #0aa4cd;
}
QDialog#workspacePackageImportDialog {
    background: #f4f7fb;
    color: #18314a;
}
QLabel#packageImportTitle {
    color: #102943;
    font-size: 21px;
    font-weight: 750;
}
QLabel#packageImportMode {
    color: #176344;
    background: #e4f4eb;
    border: 1px solid #b9ddc9;
    border-radius: 10px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 750;
}
QLabel#packageImportMode[mode="replace"] {
    color: #8a4d12;
    background: #fff1d9;
    border-color: #edcd98;
}
QLabel#packageImportTransition {
    color: #0a6f91;
    font-size: 18px;
    font-weight: 750;
}
QLabel#packageImportExplanation,
QLabel#packageImportNote {
    color: #60768a;
}
QFrame#packageImportSummary {
    background: #ffffff;
    border: 1px solid #d4dfe9;
    border-radius: 9px;
}
QLabel#packageImportIdLabel {
    color: #5e7489;
    font-size: 11px;
    font-weight: 650;
}
QLineEdit#packageImportProjectId {
    min-height: 30px;
    padding: 2px 9px;
    color: #29445d;
    background: #f7f9fc;
    border: 1px solid #d5dfe8;
    border-radius: 5px;
    font-family: "SFMono-Regular", "Menlo", monospace;
    font-size: 11px;
}
QLabel#packageImportDocumentCount,
QLabel#packageImportSegmentCount {
    color: #163d5b;
    background: #eaf4f8;
    border-radius: 8px;
    padding: 7px 11px;
    font-weight: 700;
}
QLabel#packageImportReconciliation {
    color: #405c73;
    background: #edf2f7;
    border-radius: 7px;
    padding: 9px 11px;
}
QFrame#packageImportSafety {
    background: #e8f5ed;
    border: 1px solid #c2e1ce;
    border-radius: 8px;
}
QFrame#packageImportSafety[state="warning"] {
    background: #fff4df;
    border-color: #efd39e;
}
QFrame#packageImportSafety[state="blocked"] {
    background: #fdebec;
    border-color: #efbdc1;
}
QLabel#packageImportSafetyText {
    color: #285d42;
    font-weight: 650;
}
QPushButton#packageImportApply {
    color: #ffffff;
    background: #079fc9;
    border-color: #079fc9;
    min-width: 98px;
}
QPushButton#packageImportApply:hover {
    background: #0787ad;
    border-color: #0787ad;
}
QPushButton#packageImportApply:disabled {
    color: #91a7b8;
    background: #e3e9ef;
    border-color: #d2dce5;
}
QPushButton#packageImportCancel {
    min-width: 82px;
}
QWidget#emptyPage {
    background: #eef2f7;
}
QWidget#emptyPage[dragActive="true"] {
    background: #dff3f9;
}
QFrame#emptyCard {
    background: white;
    border: 1px solid #d3dde8;
    border-radius: 13px;
}
QLabel#emptyIcon {
    color: white;
    background: #0a99c2;
    border-radius: 12px;
    font-size: 25px;
    font-weight: 750;
}
QLabel#emptyTitle {
    color: #102943;
    font-size: 25px;
    font-weight: 750;
}
QLabel#emptyHint {
    color: #64778b;
    font-size: 14px;
}
QLabel#privacyHint {
    color: #77908a;
    font-size: 11px;
}
QFrame#segmentPanel, QFrame#editPanel, QFrame#suggestionPanel {
    background: #ffffff;
    border: 1px solid #d6e0ea;
    border-radius: 8px;
    padding: 12px;
}
QFrame#projectSearchPanel {
    background: #ffffff;
    border: 1px solid #d6e0ea;
    border-radius: 8px;
}
QToolButton#projectSearchToggle,
QToolButton#workspaceDocumentsButton {
    color: #d6e7f4;
    background: #0b3e6a;
    border: 1px solid #1e5c87;
    border-radius: 5px;
    font-size: 20px;
    font-weight: 700;
}
QToolButton#workspaceDocumentsButton {
    min-width: 34px;
    max-width: 34px;
    padding: 0;
}
QToolButton#projectSearchToggle:hover,
QToolButton#projectSearchToggle:checked,
QToolButton#workspaceDocumentsButton:hover {
    color: #ffffff;
    background: #087da2;
    border-color: #20a9ce;
}
QToolButton#projectSearchToggle:disabled,
QToolButton#workspaceDocumentsButton:disabled {
    color: #7895aa;
    background: #154568;
    border-color: #315b79;
}
QToolButton#workspaceDocumentsButton::menu-indicator {
    image: none;
    width: 0px;
}
QLabel#projectSearchTitle {
    color: #17314b;
    font-size: 13px;
    font-weight: 750;
}
QLabel#workspaceChapterTitle,
QLabel#workspaceBrowseChapterTitle {
    color: #0b5e80;
    font-size: 11px;
    font-weight: 700;
}
QLabel#workspaceSaveFeedback,
QLabel#workspaceBrowseSaveFeedback {
    color: #506a7d;
    background: #eef5f8;
    border-radius: 4px;
    padding: 5px 7px;
    font-size: 10px;
}
QLabel#projectSearchScopeLabel {
    color: #64778b;
    font-size: 10px;
    font-weight: 700;
}
QLabel#projectSearchStatusLabel {
    color: #64778b;
    font-size: 10px;
    font-weight: 700;
}
QComboBox#projectSearchStatus {
    min-height: 28px;
    min-width: 92px;
    color: #405367;
    background: #ffffff;
    border: 1px solid #cbd7e2;
    border-radius: 4px;
    padding: 0 22px 0 7px;
}
QLineEdit#projectSearchQuery {
    min-height: 30px;
    color: #1c2b3a;
    background: #fbfcfe;
    border: 1px solid #cbd7e2;
    border-radius: 5px;
    padding: 0 9px;
    selection-background-color: #87d8eb;
}
QLineEdit#projectSearchQuery:focus {
    border: 2px solid #0a9ec7;
    background: #ffffff;
}
QCheckBox#projectSearchSource,
QCheckBox#projectSearchTarget,
QCheckBox#projectSearchSpeaker,
QCheckBox#projectSearchMatchCase,
QCheckBox#projectSearchWholeWord {
    color: #405367;
    font-size: 11px;
}
QCheckBox#projectSearchMatchCase:disabled,
QCheckBox#projectSearchWholeWord:disabled {
    color: #8897a5;
}
QPushButton#projectSearchSubmit {
    color: white;
    background: #079fc9;
    border-color: #079fc9;
}
QPushButton#projectSearchSubmit:hover {
    background: #078bb2;
}
QPushButton#projectSearchClear {
    color: #24455f;
    background: #f5f8fb;
    border-color: #cbd7e2;
}
QPushButton#projectSearchPrevious,
QPushButton#projectSearchNext {
    min-width: 32px;
    max-width: 36px;
    padding: 0;
}
QLabel#projectSearchCapability {
    color: #66798d;
    font-size: 10px;
}
QLabel#projectSearchResult {
    color: #24455f;
    font-size: 10px;
    font-weight: 650;
}
QLabel#projectSearchPreview {
    color: #405367;
    background: #f5f8fb;
    border-radius: 4px;
    padding: 4px 7px;
    font-size: 10px;
}
QFrame#browsePanel {
    background: #ffffff;
    border: 1px solid #d6e0ea;
    border-radius: 8px;
}
QLabel#browseHint {
    color: #74869a;
    font-size: 11px;
}
QFrame#chunkSegmentSelectionBar {
    background: #eef8fb;
    border: 1px solid #9ed7e5;
    border-radius: 8px;
}
QLabel#chunkSegmentSelectionTitle {
    color: #075f7b;
    font-size: 13px;
    font-weight: 800;
}
QLabel#chunkSegmentSelectionStatus {
    color: #4c7083;
    font-size: 11px;
}
QPushButton#chunkBrowseSelectionDone {
    color: #ffffff;
    background: #079fc9;
    border-color: #079fc9;
}
QPushButton#browseGroupNavigatorButton {
    min-height: 28px;
    min-width: 92px;
    padding: 0 11px;
    color: #315269;
    background: #f4f8fb;
    border: 1px solid #c5d4df;
    border-radius: 14px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton#browseGroupNavigatorButton:hover,
QPushButton#browseGroupNavigatorButton:focus {
    color: #075f7b;
    background: #e9f7fb;
    border-color: #58b8cf;
}
QPushButton#browseGroupNavigatorButton[groupActive="true"] {
    color: #ffffff;
    background: #078caf;
    border-color: #078caf;
}
QPushButton#browseGroupNavigatorButton:disabled {
    color: #95a5b2;
    background: #edf1f4;
    border-color: #dce3e9;
}
QComboBox#segmentDensityCombo {
    min-height: 26px;
    min-width: 88px;
    color: #405367;
    background: #f7f9fc;
    border: 1px solid #ced9e4;
    border-radius: 4px;
    padding: 0 7px;
}
QLabel#panelTitle {
    color: #17314b;
    font-size: 15px;
    font-weight: 750;
}
QLabel#countBadge {
    color: #087d9f;
    background: #e4f5fa;
    border-radius: 9px;
    padding: 2px 7px;
    font-weight: 700;
}
QCheckBox#unconfirmedFilter {
    color: #607387;
    padding: 4px 0 7px;
}
QListWidget#segmentList {
    border: none;
    background: #f7f9fc;
    outline: none;
}
QListWidget#segmentList::item {
    color: #405367;
    min-height: 38px;
    padding: 5px 8px;
    border-left: 3px solid transparent;
    border-bottom: 1px solid #e9eef4;
}
QListWidget#segmentList::item:selected {
    color: #123b58;
    background: #e1f3f8;
    border-left-color: #069fc8;
    font-weight: 650;
}
QTableWidget#browseTable {
    color: #26384b;
    background: #fbfcfe;
    alternate-background-color: #f1f5f9;
    border: 1px solid #d5dfe9;
    border-radius: 6px;
    outline: none;
}
QTableWidget#browseTable::item {
    padding: 10px 12px;
    border-bottom: 1px solid #dce5ed;
}
QTableWidget#browseTable::item:selected {
    color: #17314b;
    background: #e1f3f8;
}
QTableWidget#browseTable QHeaderView::section {
    color: #53677b;
    background: #e9eff5;
    border: none;
    border-right: 1px solid #d5dfe9;
    padding: 8px;
    font-weight: 700;
}
QLabel#sectionEyebrow {
    color: #0b8eb4;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
}
QLabel#speakerEyebrow {
    color: #66798d;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
}
QLabel#speakerDisplay {
    color: #24455f;
    background: #edf6f9;
    border: 1px solid #d3e5eb;
    border-radius: 5px;
    padding: 4px 8px;
    font-size: 11px;
    font-weight: 650;
}
QLabel#speakerDisplay[empty="true"] {
    color: #7c8b99;
    background: #f4f6f8;
    border-color: #e1e6eb;
    font-style: italic;
    font-weight: 500;
}
QLabel#segmentPosition {
    color: #78899a;
    font-size: 11px;
}
QLabel#confirmationState {
    color: #a56c19;
    background: #fff2d9;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 10px;
    font-weight: 700;
}
QLabel#confirmationState[confirmed="true"] {
    color: #27744e;
    background: #e1f3e9;
}
QTextBrowser#sourceDisplay, QTextEdit#targetEditor {
    color: #1c2b3a;
    background: #fbfcfe;
    border: 1px solid #ced9e4;
    border-radius: 7px;
    padding: 12px;
    selection-background-color: #87d8eb;
    line-height: 1.45;
}
QTextEdit#targetEditor:focus {
    border: 2px solid #0a9ec7;
    background: #ffffff;
}
QPushButton {
    min-height: 32px;
    padding: 2px 14px;
    border: 1px solid #c5d1dd;
    border-radius: 5px;
    background: #ffffff;
    color: #29445d;
    font-weight: 650;
}
QPushButton:hover {
    color: #087fa3;
    border-color: #079bc4;
}
QToolButton#manageTermsButton,
QPushButton#addTermButton {
    min-height: 32px;
    padding: 2px 14px;
    color: #15283a;
    background: #eef3f7;
    border: 1px solid #aebdca;
    border-radius: 5px;
    font-size: 13px;
    font-weight: 650;
}
QToolButton#manageTermsButton {
    padding: 2px 24px 2px 12px;
}
QToolButton#manageTermsButton:hover,
QToolButton#manageTermsButton:focus,
QPushButton#addTermButton:hover,
QPushButton#addTermButton:focus {
    color: #102637;
    background: #e8f8fc;
    border: 2px solid #20a9ce;
    border-color: #20a9ce;
}
QToolButton#manageTermsButton:pressed,
QPushButton#addTermButton:pressed {
    color: #102637;
    background: #d8f1f7;
    border-color: #078bb2;
}
QToolButton#manageTermsButton::menu-indicator {
    image: none;
    width: 0px;
}
QPushButton#confirmTranslationButton, QPushButton#emptyOpenButton {
    color: white;
    background: #079fc9;
    border-color: #079fc9;
}
QPushButton#confirmTranslationButton:hover, QPushButton#emptyOpenButton:hover {
    background: #078bb2;
}
QTabWidget#suggestionTabs::pane {
    background: #fbfcfe;
    border: 1px solid #d2dce6;
    border-radius: 6px;
}
QLabel#tmThresholdState {
    color: #52677b;
    font-size: 10px;
}
QLabel#tmThresholdState[fuzzyAvailable="false"] {
    color: #9b5a24;
}
QPushButton#tmThresholdChip {
    min-height: 24px;
    max-height: 26px;
    padding: 0 9px;
    color: #087f9f;
    background: #e6f5f9;
    border: 1px solid #99cfdb;
    border-radius: 12px;
    font-size: 10px;
    font-weight: 750;
}
QPushButton#tmThresholdChip:focus {
    border: 2px solid #087f9f;
}
QPushButton#tmThresholdChip[fuzzyAvailable="false"] {
    color: #6f7d8a;
    background: #edf0f3;
    border-color: #cbd3da;
}
QScrollArea#tmSuggestionsScroll, QScrollArea#termSuggestionsScroll {
    border: none;
    background: #fbfcfe;
}
QScrollArea#tmSuggestionsScroll > QWidget > QWidget,
QScrollArea#termSuggestionsScroll > QWidget > QWidget {
    background: #fbfcfe;
}
QFrame[suggestionCard="true"] {
    background: #ffffff;
    border: 1px solid #d9e2eb;
    border-radius: 7px;
}
QLabel#suggestionSource, QLabel#termSource {
    color: #45596c;
    font-size: 12px;
}
QLabel#suggestionTarget, QLabel#termTarget,
QLabel[suggestionTarget="true"] {
    color: #182f45;
    font-size: 14px;
    font-weight: 650;
}
QLabel#suggestionProvenance, QLabel#emptySuggestion,
QLabel[suggestionProvenance="true"], QLabel[emptySuggestion="true"] {
    color: #7a8b9c;
    font-size: 10px;
}
QLabel#matchBadge, QLabel[matchBadge="true"] {
    color: white;
    background: #08a0c9;
    border-radius: 9px;
    padding: 3px 7px;
    font-size: 10px;
    font-weight: 750;
}
QLabel[tmMatchType="true"] {
    color: #0b6f8d;
    font-size: 10px;
    font-weight: 750;
}
QLabel[tmMatchedSource="true"] {
    color: #45596c;
    font-size: 12px;
}
QLabel#termArrow {
    color: #0a97bd;
    font-weight: 800;
}
QTabBar::tab {
    color: #65798d;
    background: #eaf0f6;
    border: 1px solid #d2dce6;
    padding: 9px 12px;
}
QTabBar::tab:selected {
    color: #087f9f;
    background: #ffffff;
    border-bottom-color: #ffffff;
    font-weight: 700;
}
QSplitter::handle {
    background: #eef2f7;
}
QStatusBar#editorStatusBar {
    color: #607287;
    background: #f7f9fc;
    border-top: 1px solid #d8e1ea;
    font-size: 11px;
}
"""
