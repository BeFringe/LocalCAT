"""Professional three-column PySide6 editor window for LocalCAT."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QScrollArea,
)

from editor_contracts import SuggestionBundle, TMSuggestion, TermSuggestion
from editor_controller import EditorController, EditorControllerError
from editor_project import ProjectError
from qt_settings_dialog import QtSettingsDialog


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
        '<div style="white-space:pre-wrap; font-size:15px; color:#1c2b3a;">'
        + "".join(pieces)
        + "</div>"
    )


class QtEditorWindow(QMainWindow):
    """LocalCAT desktop shell; all domain operations go through EditorController."""

    def __init__(self, controller: EditorController) -> None:
        super().__init__()
        self.controller = controller
        self._refreshing = False
        self.settings_dialog: QtSettingsDialog | None = None
        self.current_suggestions = SuggestionBundle()
        self.setObjectName("editorWindow")
        self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
        self.setMinimumSize(1080, 700)
        self.resize(1440, 880)
        self._build_ui()
        self._wire_actions()
        self._install_shortcuts()
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

        self.open_button = QToolButton()
        self.open_button.setObjectName("openProjectButton")
        self.open_button.setText("打开")
        self.open_button.setToolTip("打开项目 (Ctrl+O)")
        self.open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        layout.addWidget(self.open_button)
        self.save_button = QToolButton()
        self.save_button.setObjectName("saveProjectButton")
        self.save_button.setText("保存")
        self.save_button.setToolTip("保存项目 (Ctrl+S)")
        self.save_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        layout.addWidget(self.save_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setText("⚙")
        self.settings_button.setToolTip("语言资源设置 (Ctrl+,)")
        self.settings_button.setFixedSize(QSize(38, 34))
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
        layout.addWidget(self.main_splitter)
        return page

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
        layout.addLayout(header)
        self.unconfirmed_filter = QCheckBox("仅显示未确认")
        self.unconfirmed_filter.setObjectName("unconfirmedFilter")
        layout.addWidget(self.unconfirmed_filter)
        self.segment_list = QListWidget()
        self.segment_list.setObjectName("segmentList")
        self.segment_list.setSpacing(3)
        layout.addWidget(self.segment_list, 1)
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
        self.previous_button.setToolTip("上一段 (Alt+Up)")
        self.next_button = QPushButton("下一段 →")
        self.next_button.setObjectName("nextSegmentButton")
        self.next_button.setToolTip("下一段 (Alt+Down)")
        self.confirm_button = QPushButton("确认译文")
        self.confirm_button.setObjectName("confirmTranslationButton")
        self.confirm_button.setToolTip("确认译文并前往下一未确认段 (Ctrl+Enter)")
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
        return panel

    def _wire_actions(self) -> None:
        self.open_button.clicked.connect(self._choose_open)
        self.empty_open_button.clicked.connect(self._choose_open)
        self.sample_button.clicked.connect(self.load_sample)
        self.save_button.clicked.connect(self._choose_save)
        self.settings_button.clicked.connect(self._open_settings)
        self.segment_list.currentRowChanged.connect(self._select_visible_row)
        self.target_editor.textChanged.connect(self._target_changed)
        self.previous_button.clicked.connect(lambda: self._navigate(-1))
        self.next_button.clicked.connect(lambda: self._navigate(1))
        self.confirm_button.clicked.connect(self.confirm_current)
        self.add_term_button.clicked.connect(self._prompt_add_term)
        self.unconfirmed_filter.toggled.connect(self._filter_changed)

    def _install_shortcuts(self) -> None:
        bindings = (
            ("open", "Ctrl+O", self._choose_open),
            ("save", "Ctrl+S", self._choose_save),
            ("confirm", "Ctrl+Enter", self.confirm_current),
            ("previous", "Alt+Up", lambda: self._navigate(-1)),
            ("next", "Alt+Down", lambda: self._navigate(1)),
            ("settings", "Ctrl+,", self._open_settings),
        )
        self.shortcuts: dict[str, QShortcut] = {}
        for name, sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setObjectName(f"{name}Shortcut")
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts[name] = shortcut

    def _show_empty_state(self) -> None:
        self.pages.setCurrentIndex(0)
        self.save_button.setEnabled(False)
        self.project_name_label.setText("未打开项目")
        self.language_label.setText("—")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
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
        except (ProjectError, EditorControllerError, OSError, ValueError) as exc:
            self._show_error("无法打开项目", str(exc))
            self.statusBar().showMessage("项目打开失败；当前会话保持不变。", 7000)
            return False
        self._render_project()
        self.statusBar().showMessage(f"已打开：{path}", 5000)
        return True

    def save_project_path(self, path: Path) -> bool:
        try:
            self.controller.save_project(path)
        except (ProjectError, EditorControllerError, OSError, ValueError) as exc:
            self._show_error("无法保存项目", str(exc))
            self.statusBar().showMessage("保存失败。", 7000)
            return False
        self._update_title()
        self.statusBar().showMessage(f"已保存：{path}", 7000)
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
        self.pages.setCurrentIndex(1)
        self.save_button.setEnabled(True)
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
        self._update_title()

    def _render_current_segment(self) -> None:
        segment = self.controller.current_segment
        project = self.controller.project
        self.source_display.setPlainText(segment.source)
        self.target_editor.setPlainText(segment.target)
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
            summary = " ".join(segment.source.split())
            if len(summary) > 72:
                summary = summary[:69] + "…"
            item.setText(
                f"{'✓' if segment.confirmed else '○'}  {project_index + 1:03d}   {summary}"
            )
            break

    def _populate_segment_list(self) -> None:
        self.segment_list.clear()
        project = self.controller.project
        unconfirmed_only = self.unconfirmed_filter.isChecked()
        selected_row = -1
        for index, segment in enumerate(project.segments):
            if unconfirmed_only and segment.confirmed:
                continue
            summary = " ".join(segment.source.split())
            if len(summary) > 72:
                summary = summary[:69] + "…"
            prefix = "✓" if segment.confirmed else "○"
            item = QListWidgetItem(f"{prefix}  {index + 1:03d}   {summary}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(segment.source)
            self.segment_list.addItem(item)
            if index == self.controller.current_index:
                selected_row = self.segment_list.count() - 1
        self.segment_list.setCurrentRow(selected_row)

    def _select_visible_row(self, row: int) -> None:
        if self._refreshing or row < 0:
            return
        item = self.segment_list.item(row)
        if item is None:
            return
        index = int(item.data(Qt.ItemDataRole.UserRole))
        try:
            self.controller.go_to(index)
        except EditorControllerError as exc:
            self._show_error("无法切换段落", str(exc))
            return
        self._refreshing = True
        try:
            self._render_current_segment()
        finally:
            self._refreshing = False

    def _navigate(self, direction: int) -> None:
        if not self.controller.has_project:
            return
        self.controller.move(direction, unconfirmed_only=self.unconfirmed_filter.isChecked())
        self._refreshing = True
        try:
            self._select_project_index(self.controller.current_index)
            self._render_current_segment()
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
            self._render_current_segment()
        finally:
            self._refreshing = False

    def confirm_current(self) -> bool:
        if not self.controller.has_project:
            return False
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
            self._render_current_segment()
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
        self.settings_dialog = dialog
        return dialog

    def _resources_changed(self) -> None:
        if self.controller.has_project:
            self.refresh_suggestions()
        self.statusBar().showMessage("语言资源已更新，当前段建议已刷新。", 6000)

    def refresh_suggestions(self) -> SuggestionBundle:
        """Render the current controller bundle as safe, actionable cards."""

        if not self.controller.has_project:
            self.current_suggestions = SuggestionBundle()
            return self.current_suggestions
        bundle = self.controller.suggestions()
        self.current_suggestions = bundle
        self.source_display.setHtml(
            render_highlighted_source(self.controller.current_segment.source, bundle.terms)
        )
        self._clear_layout(self.tm_cards_layout)
        self._clear_layout(self.term_cards_layout)

        if bundle.tm_matches:
            for index, suggestion in enumerate(bundle.tm_matches):
                self.tm_cards_layout.addWidget(self._tm_card(index, suggestion))
        else:
            self.tm_cards_layout.addWidget(self._empty_suggestion("当前段暂无翻译记忆建议。"))
        self.tm_cards_layout.addStretch()

        if bundle.terms:
            for index, suggestion in enumerate(bundle.terms):
                self.term_cards_layout.addWidget(self._term_card(index, suggestion))
        else:
            self.term_cards_layout.addWidget(self._empty_suggestion("当前段暂无术语建议。"))
        self.term_cards_layout.addStretch()
        return bundle

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
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

    def _tm_card(self, index: int, suggestion: TMSuggestion) -> QWidget:
        card = QFrame()
        card.setObjectName(f"tmCard_{index}")
        card.setProperty("suggestionCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 11, 12, 11)
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
        apply_button = QPushButton("应用译文")
        apply_button.setObjectName(f"applyTm_{index}")
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

    def _empty_suggestion(self, message: str) -> QLabel:
        return self._plain_label(message, "emptySuggestion")

    def apply_tm_suggestion(self, suggestion: TMSuggestion) -> bool:
        try:
            self.controller.apply_tm_suggestion(suggestion)
        except EditorControllerError as exc:
            self._show_error("无法应用翻译记忆", str(exc))
            return False
        self._refresh_target_from_controller()
        self.statusBar().showMessage(f"已应用来自 {suggestion.resource_name} 的译文。", 5000)
        return True

    def insert_term_suggestion(self, suggestion: TermSuggestion) -> bool:
        cursor = self.target_editor.textCursor()
        position = cursor.position()
        try:
            self.controller.insert_term_suggestion(suggestion, position)
        except EditorControllerError as exc:
            self._show_error("无法插入术语", str(exc))
            return False
        self._refresh_target_from_controller()
        cursor = self.target_editor.textCursor()
        cursor.setPosition(position + len(suggestion.target_term))
        self.target_editor.setTextCursor(cursor)
        self.statusBar().showMessage(f"已插入术语：{suggestion.target_term}", 5000)
        return True

    def _refresh_target_from_controller(self) -> None:
        self._refreshing = True
        try:
            self.target_editor.setPlainText(self.controller.current_segment.target)
        finally:
            self._refreshing = False
        self._update_segment_item(self.controller.current_index)
        self._render_progress_state()
        self._update_title()

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
        if decision is QMessageBox.StandardButton.Save:
            return self._choose_save()
        return decision is QMessageBox.StandardButton.Discard

    def _update_title(self) -> None:
        if not self.controller.has_project:
            self.setWindowTitle("LocalCAT · 本地专业翻译编辑器")
            return
        dirty = " *" if self.controller.dirty else ""
        self.setWindowTitle(f"{self.controller.project.name}{dirty} · LocalCAT")

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

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
QLabel#sectionEyebrow {
    color: #0b8eb4;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
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
    font-size: 15px;
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
QLabel#suggestionTarget, QLabel#termTarget {
    color: #182f45;
    font-size: 14px;
    font-weight: 650;
}
QLabel#suggestionProvenance, QLabel#emptySuggestion {
    color: #7a8b9c;
    font-size: 10px;
}
QLabel#matchBadge {
    color: white;
    background: #08a0c9;
    border-radius: 9px;
    padding: 3px 7px;
    font-size: 10px;
    font-weight: 750;
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
