"""Professional three-column PySide6 editor window for LocalCAT."""

from __future__ import annotations

import html
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QEvent,
    QPointF,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QPaintEvent,
    QPainter,
    QPen,
    QPolygonF,
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
    QStyledItemDelegate,
)

from editor_contracts import (
    EDITOR_FONT_SIZE_STEP,
    MAX_EDITOR_FONT_SIZE,
    MIN_EDITOR_FONT_SIZE,
    DisplayPreferences,
    EditorSegment,
    LegacyExactTMSuggestion,
    ProjectSearchReport,
    ProjectSearchRequest,
    SearchField,
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
    WorkspaceMode,
)
from editor_controller import EditorController, EditorControllerError
from qt_settings_dialog import QtSettingsDialog
from qt_tm_threshold import (
    TMThresholdButton,
    configure_tm_threshold_entry,
    prompt_tm_threshold,
    tm_threshold_feedback,
)


_WORKSPACE_MODE_POPUP_STYLE = """
QAbstractItemView#workspaceModePopup {
    color: #1f3850;
    background-color: #ffffff;
    selection-color: #0b304c;
    selection-background-color: #c4e8f2;
    border: 1px solid #9fb5c8;
    outline: 0;
}
QAbstractItemView#workspaceModePopup::item {
    color: #1f3850;
    background-color: #ffffff;
    min-height: 30px;
    padding: 2px 8px;
}
QAbstractItemView#workspaceModePopup::item:hover {
    color: #16344e;
    background-color: #e7f4f8;
}
QAbstractItemView#workspaceModePopup::item:selected {
    color: #0b304c;
    background-color: #c4e8f2;
}
"""


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


class _TopBarModeCombo(QComboBox):
    """Workspace mode combo with an application-owned visible arrow."""

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


class QtEditorWindow(QMainWindow):
    """LocalCAT desktop shell; all domain operations go through EditorController."""

    def __init__(self, controller: EditorController) -> None:
        super().__init__()
        # The UI is assembled by small builder methods.  Keep the resulting
        # widget contract explicit here so static analysis sees the same
        # always-initialized surface that callers receive after construction.
        self.shortcuts: dict[str, QShortcut]
        self.target_editor_shortcuts: dict[str, QShortcut]
        self.project_search_shortcut: QShortcut
        self.project_name_label: QLabel
        self.language_label: QLabel
        self.progress_bar: QProgressBar
        self.workspace_mode_combo: QComboBox
        self.open_button: QToolButton
        self.project_menu: QMenu
        self.open_project_action: QAction
        self.recent_projects_menu: QMenu
        self.close_project_action: QAction
        self.quit_action: QAction
        self.save_button: QToolButton
        self.settings_button: QToolButton
        self.project_search_toggle: QToolButton
        self.empty_open_button: QPushButton
        self.sample_button: QPushButton
        self.main_splitter: ResponsiveSplitter
        self.workspace_pages: QStackedWidget
        self.segment_count_label: QLabel
        self.segment_density_combo: QComboBox
        self.unconfirmed_filter: QCheckBox
        self.segment_list: QListWidget
        self.project_search_input: QLineEdit
        self.project_search_panel: QFrame
        self.project_search_status: QComboBox
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
        self.browse_table: QTableWidget
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
        self.add_term_button: QPushButton
        self.term_scroll: QScrollArea
        self.term_container: QWidget
        self.term_cards_layout: QVBoxLayout
        self.controller = controller
        self._refreshing = False
        self._display_preferences: DisplayPreferences = controller.display_preferences()
        self.segment_density = self._display_preferences.segment_density
        self.workspace_mode = self._display_preferences.workspace_mode
        self.editor_font_size = self._display_preferences.editor_font_size
        self.settings_dialog: QtSettingsDialog | None = None
        self.current_suggestions = SuggestionBundle()
        self.current_tm_report: TMSuggestionReport | None = None
        self.current_project_search_report: ProjectSearchReport | None = None
        self._project_search_ordinal: int | None = None
        self._project_search_expanded = False
        self.setObjectName("editorWindow")
        self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
        self.setMinimumSize(1080, 700)
        self.resize(1440, 880)
        self._build_ui()
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
        if controller.has_project:
            self._render_project()
        else:
            self._show_empty_state()

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
        layout = QHBoxLayout(top_bar)
        layout.setContentsMargins(22, 12, 20, 12)
        layout.setSpacing(12)

        mark = QLabel("L")
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(QSize(34, 34))
        layout.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel("LocalCAT")
        name.setObjectName("brandName")
        tagline = QLabel("LOCAL TRANSLATION WORKSPACE")
        tagline.setObjectName("brandTagline")
        brand.addWidget(name)
        brand.addWidget(tagline)
        layout.addLayout(brand)

        separator = QFrame()
        separator.setObjectName("topSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        layout.addWidget(separator)

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

        self.workspace_mode_combo = _TopBarModeCombo()
        self.workspace_mode_combo.setObjectName("workspaceModeCombo")
        self.workspace_mode_combo.setAccessibleName("工作区模式")
        self.workspace_mode_combo.setToolTip("切换编辑或双语浏览校对模式")
        self.workspace_mode_combo.addItem("编辑", WorkspaceMode.EDIT.value)
        self.workspace_mode_combo.addItem("浏览 / 校对", WorkspaceMode.BROWSE.value)
        workspace_mode_popup = self.workspace_mode_combo.view()
        workspace_mode_popup.setObjectName("workspaceModePopup")
        workspace_mode_popup.setAccessibleName("工作区模式选项")
        workspace_mode_popup.setStyleSheet(_WORKSPACE_MODE_POPUP_STYLE)
        workspace_mode_popup.setItemDelegate(
            QStyledItemDelegate(workspace_mode_popup)
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
        self.open_project_action = self.project_menu.addAction("打开项目…")
        self.recent_projects_menu = self.project_menu.addMenu("最近项目")
        self.recent_projects_menu.setObjectName("recentProjectsMenu")
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
        page = QWidget()
        page.setObjectName("emptyPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(80, 60, 80, 80)
        layout.addStretch()
        card = QFrame()
        card.setObjectName("emptyCard")
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
            "打开 JSON/TXT 项目，或载入内置示例体验双栏编辑、翻译记忆与术语建议。"
        )
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
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
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._build_project_search_bar())

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
        layout.addWidget(self.workspace_pages)
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
        self.segment_density_combo.setCurrentIndex(
            0 if self.segment_density is SegmentDensity.COMPACT else 1
        )
        header.addWidget(self.segment_density_combo)
        layout.addLayout(header)
        self.unconfirmed_filter = QCheckBox("仅显示未确认")
        self.unconfirmed_filter.setObjectName("unconfirmedFilter")
        layout.addWidget(self.unconfirmed_filter)
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
        hint = QLabel("双语全文只读浏览 · 双击任一行返回同段编辑")
        hint.setObjectName("browseHint")
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(hint)
        header.addStretch()
        layout.addLayout(header)

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
        self.browse_table.verticalHeader().setVisible(False)
        browse_header = self.browse_table.horizontalHeader()
        browse_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        browse_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        browse_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        browse_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        browse_header.resizeSection(3, 140)
        browse_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.browse_table, 1)
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
        self._refresh_tm_threshold_entry()
        return panel

    def _wire_actions(self) -> None:
        self.open_button.clicked.connect(self._choose_open)
        self.open_project_action.triggered.connect(self._choose_open)
        self.close_project_action.triggered.connect(self.close_current_project)
        self.quit_action.triggered.connect(self.close)
        self.empty_open_button.clicked.connect(self._choose_open)
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
        self.browse_table.cellDoubleClicked.connect(self._activate_browse_row)
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
        self.project_search_previous.clicked.connect(
            lambda: self._navigate_project_search(-1)
        )
        self.project_search_next.clicked.connect(
            lambda: self._navigate_project_search(1)
        )

    def _install_shortcuts(self) -> None:
        bindings = (
            ("open", ("Ctrl+O",), self._choose_open),
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
                ("Ctrl+Tab",),
                lambda: self._cycle_suggestion_tab(1),
            ),
            (
                "suggestion_tab_previous",
                ("Ctrl+Shift+Tab",),
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
        self.project_search_shortcut.setObjectName("projectSearchFocusShortcut")
        self.project_search_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self.project_search_shortcut.activated.connect(
            self._focus_project_search
        )
        self._install_target_editor_shortcuts()
        self._update_shortcut_tooltips()

    def _cycle_suggestion_tab(self, direction: int) -> None:
        if not self.controller.has_project or not self.suggestion_tabs.isEnabled():
            return
        count = self.suggestion_tabs.count()
        if count > 1:
            self.suggestion_tabs.setCurrentIndex(
                (self.suggestion_tabs.currentIndex() + direction) % count
            )

    def _set_workspace_mode_from_shortcut(self, mode: WorkspaceMode) -> None:
        if not self.controller.has_project or not self.workspace_mode_combo.isEnabled():
            return
        self.set_workspace_mode(mode)

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
        project_search_shortcut = self._native_shortcut_text(
            self.project_search_shortcut
        )
        self.project_search_toggle.setToolTip(
            f"展开或收起项目搜索 ({project_search_shortcut})"
        )
        self.project_search_input.setToolTip(
            f"输入非空关键词并按 Enter 搜索当前 JSON 项目 ({project_search_shortcut})"
        )
        self.project_search_button.setToolTip(
            f"执行当前项目搜索；{project_search_shortcut} 聚焦关键词"
        )

    def _focus_project_search(self) -> None:
        if not self.controller.has_project:
            return
        self._set_project_search_expanded(True)
        self.project_search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.project_search_input.selectAll()

    def _project_search_toggled(self, expanded: bool) -> None:
        self._set_project_search_expanded(expanded, focus=expanded)

    def _set_project_search_expanded(
        self,
        expanded: bool,
        *,
        focus: bool = False,
    ) -> None:
        visible = bool(expanded and self.controller.has_project)
        self._project_search_expanded = visible
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
        if self._refreshing:
            return
        if self.current_project_search_report is None:
            self.controller.clear_project_search()
            return
        self._clear_project_search_results(
            "搜索条件已变化；请重新搜索。"
        )

    def _clear_project_search(self) -> None:
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
        """Handle Ctrl+wheel only for the two editor viewports."""

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

    def _show_empty_state(self) -> None:
        self._set_project_search_expanded(False)
        self.pages.setCurrentIndex(0)
        self.save_button.setEnabled(False)
        self.project_name_label.setText("未打开项目")
        self.language_label.setText("—")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.close_project_action.setEnabled(False)
        self.workspace_mode_combo.setEnabled(False)
        self.segment_density_combo.setEnabled(False)
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

    def save_project_path(self, path: Path) -> bool:
        try:
            self.controller.save_project(path)
        except (EditorControllerError, OSError, ValueError) as exc:
            self._show_error("无法保存项目", str(exc))
            self.statusBar().showMessage("保存失败。", 7000)
            return False
        self._update_title()
        self.refresh_recent_projects()
        self.statusBar().showMessage(f"已保存：{path}", 7000)
        return True

    def refresh_recent_projects(self) -> None:
        """Rebuild the project menu from controller-owned local workspace state."""

        self.recent_projects_menu.clear()
        recent = self.controller.recent_projects()
        if not recent:
            empty = self.recent_projects_menu.addAction("暂无最近项目")
            empty.setEnabled(False)
            return
        for project in recent:
            action = self.recent_projects_menu.addAction(
                f"{project.path.name}  —  {project.path.parent}"
            )
            action.setData(str(project.path))
            action.setToolTip(str(project.path))
            action.triggered.connect(
                lambda _checked=False, path=project.path: self.open_recent_project(path)
            )

    def open_recent_project(self, path: Path) -> bool:
        """Open a remembered project, pruning entries that no longer exist."""

        normalized = path.expanduser().resolve()
        if not normalized.is_file():
            try:
                self.controller.remove_recent_project(normalized)
            except EditorControllerError:
                pass
            self.refresh_recent_projects()
            self._show_error("最近项目不可用", f"项目文件不存在：{normalized}")
            return False
        return self.open_project_path(normalized)

    def close_current_project(self) -> bool:
        """Return to the empty workspace after applying the unsaved guard."""

        if not self.controller.has_project:
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

    def _choose_save(self) -> bool:
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
        self._clear_project_search_results()
        self._refreshing = True
        try:
            project = self.controller.project
            self.project_name_label.setText(project.name)
            self.language_label.setText(
                f"{project.source_locale}  →  {project.target_locale}"
            )
            self.segment_count_label.setText(str(len(project.segments)))
            self._populate_segment_list()
            self._render_current_segment()
        finally:
            self._refreshing = False
        self.set_workspace_mode(self.workspace_mode, persist=False)
        self._update_title()

    def _render_current_segment(self, *, reset_target_history: bool = True) -> None:
        segment = self.controller.current_segment
        project = self.controller.project
        speaker_text = segment.speaker or "无 speaker"
        self.speaker_display.setText(speaker_text)
        self.speaker_display.setAccessibleName(f"当前段 raw speaker：{speaker_text}")
        self.speaker_display.setProperty("empty", not bool(segment.speaker))
        self.speaker_display.style().unpolish(self.speaker_display)
        self.speaker_display.style().polish(self.speaker_display)
        self.source_display.setPlainText(segment.source)
        if reset_target_history:
            self._replace_target_text(segment.target)
        self.segment_position_label.setText(
            f"{self.controller.current_index + 1} / {len(project.segments)}"
        )
        self.confirmation_label.setText("已确认" if segment.confirmed else "待确认")
        self.confirmation_label.setProperty("confirmed", segment.confirmed)
        self.confirmation_label.style().unpolish(self.confirmation_label)
        self.confirmation_label.style().polish(self.confirmation_label)
        self.progress_bar.setRange(0, len(project.segments))
        self.progress_bar.setValue(self.controller.confirmed_count)
        self.refresh_suggestions()

    def _target_changed(self) -> None:
        if self._refreshing or not self.controller.has_project:
            return
        self.controller.update_target(self.target_editor.toPlainText())
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
            self._update_segment_item(self.controller.current_index)
        self._render_progress_state()
        self._update_title()

    def _render_progress_state(self) -> None:
        segment = self.controller.current_segment
        self.confirmation_label.setText("已确认" if segment.confirmed else "待确认")
        self.confirmation_label.setProperty("confirmed", segment.confirmed)
        self.confirmation_label.style().unpolish(self.confirmation_label)
        self.confirmation_label.style().polish(self.confirmation_label)
        self.progress_bar.setValue(self.controller.confirmed_count)

    def _update_segment_item(self, project_index: int) -> None:
        for row in range(self.segment_list.count()):
            item = self.segment_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) != project_index:
                continue
            segment = self.controller.project.segments[project_index]
            item.setText(self._segment_item_text(project_index, segment))
            item.setSizeHint(self._segment_item_size_hint(item.text()))
            break

    def _populate_segment_list(self) -> None:
        self.segment_list.clear()
        project = self.controller.project
        unconfirmed_only = self.unconfirmed_filter.isChecked()
        selected_row = -1
        for index, segment in enumerate(project.segments):
            if unconfirmed_only and segment.confirmed:
                continue
            item = QListWidgetItem(self._segment_item_text(index, segment))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(segment.source)
            item.setSizeHint(self._segment_item_size_hint(item.text()))
            self.segment_list.addItem(item)
            if index == self.controller.current_index:
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
            return QSize(0, 44)
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
            not self.controller.has_project
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
            if self.controller.has_project:
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
            if self.controller.has_project:
                if normalized is WorkspaceMode.BROWSE:
                    self._refresh_browse_table()
                    self.workspace_pages.setCurrentIndex(1)
                else:
                    self.workspace_pages.setCurrentIndex(0)
                    self._select_project_index(self.controller.current_index)
        finally:
            self._refreshing = False
        return True

    def _refresh_browse_table(self) -> None:
        if not self.controller.has_project:
            self.browse_table.clearContents()
            self.browse_table.setRowCount(0)
            return
        project = self.controller.project
        self.browse_table.setUpdatesEnabled(False)
        try:
            self.browse_table.clearContents()
            self.browse_table.setRowCount(len(project.segments))
            for index, segment in enumerate(project.segments):
                values = (
                    f"{index + 1:03d}",
                    segment.source,
                    segment.target or "—",
                    segment.speaker or "无 speaker",
                    "已确认" if segment.confirmed else "待确认",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, index)
                    item.setToolTip(value)
                    self.browse_table.setItem(index, column, item)
        finally:
            self.browse_table.setUpdatesEnabled(True)
        self.browse_table.resizeRowsToContents()
        self.browse_table.setCurrentCell(self.controller.current_index, 1)
        current = self.browse_table.item(self.controller.current_index, 1)
        if current is not None:
            self.browse_table.scrollToItem(
                current,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )

    def _activate_browse_row(self, row: int, _column: int) -> None:
        item = self.browse_table.item(row, 0)
        if item is None:
            return
        previous_index = self.controller.current_index
        try:
            self.controller.go_to(int(item.data(Qt.ItemDataRole.UserRole)))
        except (TypeError, ValueError, EditorControllerError) as exc:
            self._show_error("无法打开浏览段落", str(exc))
            return
        if not self.set_workspace_mode(WorkspaceMode.EDIT):
            return
        self._refreshing = True
        try:
            self._select_project_index(self.controller.current_index)
            self._render_current_segment(
                reset_target_history=(
                    self.controller.current_index != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _schedule_layout_refresh(self) -> None:
        if self.segment_density is SegmentDensity.WRAPPED:
            QTimer.singleShot(0, self._refresh_segment_item_sizes)
        if (
            self.controller.has_project
            and self.workspace_mode is WorkspaceMode.BROWSE
        ):
            QTimer.singleShot(0, self.browse_table.resizeRowsToContents)

    def _select_visible_row(self, row: int) -> None:
        if self._refreshing or row < 0:
            return
        item = self.segment_list.item(row)
        if item is None:
            return
        index = int(item.data(Qt.ItemDataRole.UserRole))
        previous_index = self.controller.current_index
        try:
            self.controller.go_to(index)
        except EditorControllerError as exc:
            self._show_error("无法切换段落", str(exc))
            return
        self._refreshing = True
        try:
            self._render_current_segment(
                reset_target_history=(
                    self.controller.current_index != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _navigate(self, direction: int) -> None:
        if not self.controller.has_project:
            return
        previous_index = self.controller.current_index
        self.controller.move(direction, unconfirmed_only=self.unconfirmed_filter.isChecked())
        self._refreshing = True
        try:
            self._select_project_index(self.controller.current_index)
            self._render_current_segment(
                reset_target_history=(
                    self.controller.current_index != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _select_project_index(self, project_index: int) -> None:
        for row in range(self.segment_list.count()):
            item = self.segment_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == project_index:
                self.segment_list.setCurrentRow(row)
                return
        self.segment_list.setCurrentRow(-1)

    def _filter_changed(self, enabled: bool) -> None:
        if self._refreshing or not self.controller.has_project:
            return
        previous_index = self.controller.current_index
        if enabled and self.controller.current_segment.confirmed:
            next_unconfirmed = next(
                (
                    index
                    for index, segment in enumerate(self.controller.project.segments)
                    if not segment.confirmed
                ),
                None,
            )
            if next_unconfirmed is not None:
                self.controller.go_to(next_unconfirmed)
        self._refreshing = True
        try:
            self._populate_segment_list()
            self._render_current_segment(
                reset_target_history=(
                    self.controller.current_index != previous_index
                )
            )
        finally:
            self._refreshing = False

    def _refresh_project_search_controls(self) -> None:
        """Project the current JSON and matcher gates without executing search."""

        project_capability = self.controller.project_tool_capability()
        self.project_search_toggle.setEnabled(self.controller.has_project)
        self.project_search_clear.setEnabled(self.controller.has_project)
        display = None
        search_available = False
        advanced_available = False
        if not project_capability.single_json_tools_available:
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
        request = ProjectSearchRequest(
            query=query,
            fields=fields,
            options=self._project_search_options(),
            status=self._project_search_status_filter(),
        )
        try:
            report = self.controller.search_project(request)
        except EditorControllerError as exc:
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
        previous_index = self.controller.current_index
        try:
            _ = self.controller.go_to_search_hit(report.hits[ordinal])
        except EditorControllerError as exc:
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
            self._select_project_index(self.controller.current_index)
            self._render_current_segment(
                reset_target_history=(
                    self.controller.current_index != previous_index
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
        hit = report.hits[ordinal]
        field = hit.field.value.upper()
        result = (
            f"共 {report.total} 个结果 · 第 {ordinal + 1} 个 · "
            f"{field} · 段落 {hit.segment_index + 1}"
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
            self.controller.clear_project_search()
        self.current_project_search_report = None
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
        if not self.controller.has_project:
            return False
        previous_index = self.controller.current_index
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
                    self.controller.current_index != previous_index
                )
            )
        finally:
            self._refreshing = False
        self._update_title()
        self.statusBar().showMessage(
            f"译文已确认 · 已写入 {len(result.write_report.written_resource_ids)} 个记忆库",
            6000,
        )
        return True

    def _open_settings(self) -> None:
        dialog = self.create_settings_dialog()
        dialog.exec()

    def create_settings_dialog(self) -> QtSettingsDialog:
        """Create the controller-only settings seam and connect resource refresh."""

        dialog = QtSettingsDialog(self.controller, self)
        dialog.resources_changed.connect(self._resources_changed)
        dialog.tm_threshold_changed.connect(self._settings_tm_threshold_changed)
        dialog.term_suggestions_changed.connect(
            self._term_suggestions_changed
        )
        self.settings_dialog = dialog
        return dialog

    def _resources_changed(self) -> None:
        if self.controller.has_project:
            self.refresh_suggestions()
        self.statusBar().showMessage("语言资源已更新，当前段建议已刷新。", 6000)

    def _term_suggestions_changed(self) -> None:
        if self.controller.has_project:
            self.refresh_suggestions()
        self.statusBar().showMessage("术语已更新，当前段建议已刷新。", 6000)

    def refresh_suggestions(self) -> SuggestionBundle:
        """Render the current controller bundle as safe, actionable cards."""

        self._refresh_project_search_controls()
        if not self.controller.has_project:
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
        if outcome.succeeded and self.controller.has_project:
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
        if self.controller.has_project:
            self.refresh_suggestions()
        else:
            self._refresh_tm_threshold_entry()
        self.statusBar().showMessage(
            tm_threshold_feedback(outcome),
            7000,
        )

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
        self.statusBar().showMessage(f"已插入术语：{suggestion.target_term}", 5000)
        return True

    def _refresh_target_from_controller(self) -> None:
        self._refreshing = True
        try:
            self._replace_target_text(self.controller.current_segment.target)
        finally:
            self._refreshing = False
        self._update_segment_item(self.controller.current_index)
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
        if not self.controller.has_project or not self.controller.dirty:
            return True
        decision = QMessageBox.question(
            self,
            "存在未保存修改",
            "当前项目有未保存修改。保存后继续吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if decision == QMessageBox.StandardButton.Save:
            return self._choose_save()
        return decision == QMessageBox.StandardButton.Discard

    def _update_title(self) -> None:
        if not self.controller.has_project:
            self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
            return
        dirty = " *" if self.controller.dirty else ""
        self.setWindowTitle(f"{self.controller.project.name}{dirty} · LocalCAT")

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_layout_refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._confirm_unsaved():
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
QWidget#emptyPage {
    background: #eef2f7;
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
QToolButton#projectSearchToggle {
    color: #d6e7f4;
    background: #0b3e6a;
    border: 1px solid #1e5c87;
    border-radius: 5px;
    font-size: 20px;
    font-weight: 700;
}
QToolButton#projectSearchToggle:hover,
QToolButton#projectSearchToggle:checked {
    color: #ffffff;
    background: #087da2;
    border-color: #20a9ce;
}
QToolButton#projectSearchToggle:disabled {
    color: #7895aa;
    background: #154568;
    border-color: #315b79;
}
QLabel#projectSearchTitle {
    color: #17314b;
    font-size: 13px;
    font-weight: 750;
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
