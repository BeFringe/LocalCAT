"""Browse/review group navigator and its device-local settings dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from editor_contracts import (
    BROWSE_GROUP_SIZE_STEP,
    MAX_BROWSE_GROUP_SIZE,
    MIN_BROWSE_GROUP_SIZE,
    BrowseGroupDisplayMode,
    BrowseGroupPreferences,
)


_CARD_HEIGHT = 92
_CARD_SPACING = 7


@dataclass(frozen=True, slots=True)
class BrowseGroupPreview:
    """One presentation-only group bound to its first stable segment identity."""

    ordinal: int
    total_groups: int
    start_index: int
    end_index: int
    source: str
    target: str
    issued_identity: object
    selected: bool = False

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("browse group ordinal must be positive")
        if type(self.total_groups) is not int or self.total_groups < self.ordinal:
            raise ValueError("browse group total must cover its ordinal")
        if (
            type(self.start_index) is not int
            or type(self.end_index) is not int
            or self.start_index < 0
            or self.end_index <= self.start_index
        ):
            raise ValueError("browse group range must be non-empty and ordered")
        if type(self.source) is not str or type(self.target) is not str:
            raise TypeError("browse group preview text must be exact strings")
        if self.issued_identity is None:
            raise ValueError("browse group preview requires one issued identity")
        if type(self.selected) is not bool:
            raise TypeError("browse group selection must be an exact bool")

    @property
    def range_text(self) -> str:
        return f"{self.start_index + 1}–{self.end_index}"


def _collapsed_preview(text: str) -> str:
    return " ".join(text.split())


def _elided_lines(
    text: str,
    *,
    metrics: object,
    width: int,
    maximum_lines: int,
) -> tuple[str, ...]:
    """Wrap by measured characters and elide only the final visible line."""

    if maximum_lines <= 0 or width <= 0:
        return ()
    remaining = _collapsed_preview(text)
    if not remaining:
        return ()
    if maximum_lines == 1:
        return (
            metrics.elidedText(
                remaining,
                Qt.TextElideMode.ElideRight,
                width,
            ),
        )

    lines: list[str] = []
    while remaining and len(lines) < maximum_lines:
        if len(lines) == maximum_lines - 1:
            lines.append(
                metrics.elidedText(
                    remaining,
                    Qt.TextElideMode.ElideRight,
                    width,
                )
            )
            break
        if metrics.horizontalAdvance(remaining) <= width:
            lines.append(remaining)
            break
        low = 1
        high = len(remaining)
        best = 1
        while low <= high:
            middle = (low + high) // 2
            if metrics.horizontalAdvance(remaining[:middle]) <= width:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        split_at = best
        whitespace = remaining.rfind(" ", 0, best + 1)
        if whitespace >= max(1, best // 2):
            split_at = whitespace
        line = remaining[:split_at].rstrip()
        if not line:
            line = remaining[:best]
            split_at = best
        lines.append(line)
        remaining = remaining[split_at:].lstrip()
    return tuple(lines)


class BrowseGroupCard(QAbstractButton):
    """Four-line, keyboard-selectable turn card for one browse group."""

    def __init__(
        self,
        preview: BrowseGroupPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        preview.__post_init__()
        self.preview = preview
        self.setObjectName(f"browseGroupCard_{preview.ordinal}")
        self.setCheckable(True)
        self.setChecked(preview.selected)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(_CARD_HEIGHT)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        source_lines, target_lines = self.preview_line_limits
        self.setProperty("sourceLineLimit", source_lines)
        self.setProperty("targetLineLimit", target_lines)
        target_summary = (
            f"；译文 {preview.target}"
            if preview.target.strip()
            else "；未填写译文"
        )
        self.setAccessibleName(
            f"第 {preview.ordinal} 组，段落 {preview.range_text}；"
            f"原文 {preview.source}{target_summary}"
        )
        self.setToolTip(
            f"第 {preview.ordinal} 组 · 段落 {preview.range_text}；"
            "点击跳到该组首段"
        )

    @property
    def preview_line_limits(self) -> tuple[int, int]:
        return (1, 3) if self.preview.target.strip() else (4, 0)

    def sizeHint(self) -> QSize:
        return QSize(540, _CARD_HEIGHT)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            outer = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            selected = self.isChecked()
            hovered = self.underMouse() or self.hasFocus()
            background = QColor("#e9f6fa" if selected else "#ffffff")
            if hovered and not selected:
                background = QColor("#f2f8fb")
            border = QColor("#079fc9" if selected else "#cfdbe5")
            painter.setPen(QPen(border, 1.4 if selected else 1.0))
            painter.setBrush(background)
            painter.drawRoundedRect(outer, 8.0, 8.0)

            rail_width = 58.0
            rail = QRectF(
                outer.left(),
                outer.top(),
                rail_width,
                outer.height(),
            )
            painter.save()
            painter.setClipPath(self._rounded_clip(outer))
            painter.fillRect(
                rail,
                QColor("#087fa3" if selected else "#214f70"),
            )
            painter.restore()

            ordinal_font = self.font()
            ordinal_font.setBold(True)
            ordinal_font.setPointSize(max(10, ordinal_font.pointSize() + 1))
            painter.setFont(ordinal_font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                rail.adjusted(0.0, 14.0, 0.0, -28.0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                str(self.preview.ordinal),
            )
            range_font = self.font()
            range_font.setPointSize(max(8, range_font.pointSize() - 2))
            painter.setFont(range_font)
            painter.setPen(QColor("#d3edf5"))
            painter.drawText(
                rail.adjusted(3.0, 0.0, -3.0, -11.0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                self.preview.range_text,
            )

            content = outer.adjusted(rail_width + 12.0, 8.0, -10.0, -7.0)
            preview_font = self.font()
            preview_font.setPointSize(max(9, preview_font.pointSize() - 1))
            painter.setFont(preview_font)
            metrics = painter.fontMetrics()
            line_height = max(15, metrics.height() + 1)
            source_limit, target_limit = self.preview_line_limits
            source_text = self.preview.source or "（空原文）"
            source_lines = _elided_lines(
                source_text,
                metrics=metrics,
                width=max(1, int(content.width())),
                maximum_lines=source_limit,
            )
            y = content.top()
            painter.setPen(QColor("#17364e"))
            for line in source_lines:
                painter.drawText(
                    QRectF(content.left(), y, content.width(), line_height),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    line,
                )
                y += line_height
            if target_limit:
                target_lines = _elided_lines(
                    self.preview.target,
                    metrics=metrics,
                    width=max(1, int(content.width())),
                    maximum_lines=target_limit,
                )
                painter.setPen(QColor("#5b7083"))
                for line in target_lines:
                    painter.drawText(
                        QRectF(content.left(), y, content.width(), line_height),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        line,
                    )
                    y += line_height
        finally:
            painter.end()

    @staticmethod
    def _rounded_clip(rect: QRectF):
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.addRoundedRect(rect, 8.0, 8.0)
        return path


class BrowseGroupIndicatorTick(QAbstractButton):
    """One compact Codex-style turn mark with hover preview signals."""

    previewRequested = Signal()
    previewDismissed = Signal()

    def __init__(
        self,
        preview: BrowseGroupPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setObjectName(f"browseGroupTick_{preview.ordinal}")
        self.setCheckable(True)
        self.setChecked(preview.selected)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(28, 18)
        self.setAccessibleName(
            f"第 {preview.ordinal} 组，段落 {preview.range_text}；"
            "点击跳到该组首段"
        )

    def enterEvent(self, event) -> None:
        self.previewRequested.emit()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.previewDismissed.emit()
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:
        self.previewRequested.emit()
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.previewDismissed.emit()
        self.update()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.click()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            selected = self.isChecked()
            hovered = self.underMouse() or self.hasFocus()
            width = 22 if selected else (15 if hovered else 9)
            color = QColor(
                "#173d56" if selected else ("#5c8198" if hovered else "#bdc7ce")
            )
            pen = QPen(color, 3.0 if selected else 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            center = self.rect().center()
            painter.drawLine(
                QPoint(center.x() - width // 2, center.y()),
                QPoint(center.x() + width // 2, center.y()),
            )
        finally:
            painter.end()


class _BrowseGroupPreviewPopup(QFrame):
    """Non-activating preview floated beside one indicator tick."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip)
        self.setObjectName("browseGroupPreviewPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._card: BrowseGroupCard | None = None

    def show_preview(
        self,
        preview: BrowseGroupPreview,
        anchor: QWidget,
    ) -> None:
        if self._card is not None:
            self._layout.removeWidget(self._card)
            self._card.deleteLater()
        self._card = BrowseGroupCard(preview)
        self._card.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self._card.setFixedWidth(370)
        self._layout.addWidget(self._card)
        self.adjustSize()
        target = anchor.mapToGlobal(QPoint(anchor.width() + 8, -37))
        available = anchor.screen().availableGeometry()
        x = min(target.x(), available.right() - self.width())
        y = min(
            max(target.y(), available.top()),
            available.bottom() - self.height(),
        )
        self.move(max(available.left(), x), y)
        self.show()
        self.raise_()


class BrowseGroupTurnBar(QFrame):
    """Current-document navigator with collapsible indicator or fixed cards."""

    groupSelected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("browseGroupTurnBar")
        self.setAccessibleName("当前文档分组轮次")
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.stack.setObjectName("browseGroupTurnBarStack")
        root_layout.addWidget(self.stack)

        indicator_page = QFrame()
        indicator_page.setObjectName("browseGroupIndicatorPage")
        indicator_layout = QVBoxLayout(indicator_page)
        indicator_layout.setContentsMargins(3, 2, 3, 2)
        self.indicator_scroll = QScrollArea()
        self.indicator_scroll.setObjectName("browseGroupIndicatorScroll")
        self.indicator_scroll.setWidgetResizable(True)
        self.indicator_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.indicator_container = QWidget()
        self.indicator_container.setObjectName("browseGroupIndicatorContainer")
        self.indicator_layout = QVBoxLayout(self.indicator_container)
        self.indicator_layout.setContentsMargins(1, 2, 1, 2)
        self.indicator_layout.setSpacing(1)
        self.indicator_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.indicator_scroll.setWidget(self.indicator_container)
        indicator_layout.addWidget(self.indicator_scroll, 1)
        self.stack.addWidget(indicator_page)

        fixed_page = QFrame()
        fixed_page.setObjectName("browseGroupFixedPage")
        fixed_layout = QVBoxLayout(fixed_page)
        fixed_layout.setContentsMargins(10, 10, 10, 10)
        fixed_layout.setSpacing(7)
        heading = QHBoxLayout()
        title = QLabel("分组轮次")
        title.setObjectName("browseGroupTurnBarTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.document_label = QLabel("—")
        self.document_label.setObjectName("browseGroupTurnBarDocument")
        self.document_label.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.document_label)
        fixed_layout.addLayout(heading)
        self.group_scroll = QScrollArea()
        self.group_scroll.setObjectName("browseGroupTurnBarScroll")
        self.group_scroll.setWidgetResizable(True)
        self.group_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.group_container = QWidget()
        self.group_container.setObjectName("browseGroupTurnBarContainer")
        self.group_layout = QVBoxLayout(self.group_container)
        self.group_layout.setContentsMargins(1, 1, 1, 1)
        self.group_layout.setSpacing(_CARD_SPACING)
        self.group_scroll.setWidget(self.group_container)
        fixed_layout.addWidget(self.group_scroll, 1)
        self.stack.addWidget(fixed_page)

        self.cards: tuple[BrowseGroupCard, ...] = ()
        self.ticks: tuple[BrowseGroupIndicatorTick, ...] = ()
        self._previews: tuple[BrowseGroupPreview, ...] = ()
        self._preview_popup = _BrowseGroupPreviewPopup()
        self._preview_hide_timer = QTimer(self)
        self._preview_hide_timer.setSingleShot(True)
        self._preview_hide_timer.setInterval(140)
        self._preview_hide_timer.timeout.connect(self._preview_popup.hide)
        self.setStyleSheet(_TURN_BAR_STYLE)
        self.set_display_mode(BrowseGroupDisplayMode.AUTO_COLLAPSE)
        self.setVisible(False)

    def set_display_mode(self, mode: BrowseGroupDisplayMode) -> None:
        if type(mode) is not BrowseGroupDisplayMode:
            raise TypeError("browse group display mode must be exact")
        self._preview_hide_timer.stop()
        self._preview_popup.hide()
        self.setProperty("displayMode", mode.value)
        if mode is BrowseGroupDisplayMode.AUTO_COLLAPSE:
            self.stack.setCurrentIndex(0)
            self.setMinimumWidth(42)
            self.setMaximumWidth(42)
        else:
            self.stack.setCurrentIndex(1)
            self.setMinimumWidth(280)
            self.setMaximumWidth(360)
        self.style().unpolish(self)
        self.style().polish(self)
        QTimer.singleShot(0, self._reveal_selected_group)

    def set_previews(
        self,
        previews: tuple[BrowseGroupPreview, ...],
        *,
        document_name: str,
    ) -> None:
        if type(previews) is not tuple or any(
            type(preview) is not BrowseGroupPreview for preview in previews
        ):
            raise TypeError("browse turn bar previews must be an exact tuple")
        if type(document_name) is not str:
            raise TypeError("browse turn bar document name must be an exact string")
        visible_name = self.document_label.fontMetrics().elidedText(
            document_name or "当前文档",
            Qt.TextElideMode.ElideMiddle,
            130,
        )
        self.document_label.setText(visible_name)
        self.document_label.setToolTip(document_name)
        if self._same_projection(previews):
            self._previews = previews
            for card, tick, preview in zip(
                self.cards,
                self.ticks,
                previews,
                strict=True,
            ):
                card.preview = preview
                card.setChecked(preview.selected)
                tick.preview = preview
                tick.setChecked(preview.selected)
            self.setVisible(bool(previews))
            if not previews:
                self._preview_popup.hide()
            QTimer.singleShot(0, self._reveal_selected_group)
            return
        self._preview_popup.hide()
        self._clear_layout(self.group_layout)
        self._clear_layout(self.indicator_layout)
        cards: list[BrowseGroupCard] = []
        ticks: list[BrowseGroupIndicatorTick] = []
        for preview in previews:
            card = BrowseGroupCard(preview)
            card.clicked.connect(
                lambda _checked=False, issued=preview.issued_identity: (
                    self.groupSelected.emit(issued)
                )
            )
            cards.append(card)
            self.group_layout.addWidget(card)

            tick = BrowseGroupIndicatorTick(preview)
            tick.clicked.connect(
                lambda _checked=False, issued=preview.issued_identity: (
                    self.groupSelected.emit(issued)
                )
            )
            tick.previewRequested.connect(
                lambda tick=tick: self._show_preview(
                    tick.preview,
                    tick,
                )
            )
            tick.previewDismissed.connect(self._schedule_preview_hide)
            ticks.append(tick)
            self.indicator_layout.addWidget(tick)
        self.group_layout.addStretch()
        self.indicator_layout.addStretch()
        self.cards = tuple(cards)
        self.ticks = tuple(ticks)
        self._previews = previews
        self.setVisible(bool(previews))
        QTimer.singleShot(0, self._reveal_selected_group)

    def _same_projection(
        self,
        previews: tuple[BrowseGroupPreview, ...],
    ) -> bool:
        return len(self._previews) == len(previews) and all(
            old.ordinal == new.ordinal
            and old.total_groups == new.total_groups
            and old.start_index == new.start_index
            and old.end_index == new.end_index
            and old.source == new.source
            and old.target == new.target
            and (
                old.issued_identity is new.issued_identity
                or (
                    type(old.issued_identity) is int
                    and old.issued_identity == new.issued_identity
                )
            )
            for old, new in zip(self._previews, previews, strict=True)
        )

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_preview(
        self,
        preview: BrowseGroupPreview,
        tick: BrowseGroupIndicatorTick,
    ) -> None:
        self._preview_hide_timer.stop()
        self._preview_popup.show_preview(preview, tick)

    def _schedule_preview_hide(self) -> None:
        self._preview_hide_timer.start()

    def hideEvent(self, event) -> None:
        self._preview_hide_timer.stop()
        self._preview_popup.hide()
        super().hideEvent(event)

    def _reveal_selected_group(self) -> None:
        card = next((item for item in self.cards if item.isChecked()), None)
        tick = next((item for item in self.ticks if item.isChecked()), None)
        if card is not None:
            self.group_scroll.ensureWidgetVisible(card, 8, 8)
        if tick is not None:
            self.indicator_scroll.ensureWidgetVisible(tick, 3, 3)


class QtBrowseGroupDialog(QDialog):
    """Settings-only dialog for the Browse/Review turn bar."""

    def __init__(
        self,
        *,
        preferences: BrowseGroupPreferences,
        document_name: str,
        segment_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        preferences.__post_init__()
        if type(document_name) is not str:
            raise TypeError("browse group document name must be an exact string")
        if type(segment_count) is not int or segment_count < 0:
            raise ValueError("browse group segment count must be non-negative")
        self.saved_preferences: BrowseGroupPreferences | None = None
        self.setObjectName("browseGroupDialog")
        self.setWindowTitle("浏览 / 校对 · 分组设置")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.resize(560, 310)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("分组设置")
        title.setObjectName("browseGroupDialogTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.document_label = QLabel(document_name or "当前文档")
        self.document_label.setObjectName("browseGroupDocument")
        self.document_label.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.document_label)
        layout.addLayout(heading)

        group_count = preferences.group_count(segment_count)
        groups_active = preferences.should_show(segment_count)
        if not preferences.enabled:
            status_text = "分组轮次当前不显示；可在下方启用。"
        elif not groups_active:
            status_text = (
                f"当前文档 {segment_count} 段 · {group_count} 组，"
                "尚未超过任一显示阈值。"
            )
        else:
            status_text = (
                f"当前文档 {segment_count} 段 · {group_count} 组；"
                "已超过显示阈值，轮次导航在浏览主页显示。"
            )
        self.status_label = QLabel(status_text)
        self.status_label.setObjectName("browseGroupStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        settings = QFrame()
        settings.setObjectName("browseGroupSettings")
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(14, 12, 14, 12)
        settings_layout.setSpacing(9)
        settings_title = QLabel("显示设置")
        settings_title.setObjectName("browseGroupSettingsTitle")
        settings_layout.addWidget(settings_title)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("显示方式"))
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.setObjectName("browseGroupDisplayMode")
        self.display_mode_combo.addItem(
            "自动收起式（轮次指示条）",
            BrowseGroupDisplayMode.AUTO_COLLAPSE.value,
        )
        self.display_mode_combo.addItem(
            "固定式（预览列表）",
            BrowseGroupDisplayMode.FIXED.value,
        )
        self.display_mode_combo.setCurrentIndex(
            0
            if preferences.display_mode is BrowseGroupDisplayMode.AUTO_COLLAPSE
            else 1
        )
        mode_row.addWidget(self.display_mode_combo, 1)
        settings_layout.addLayout(mode_row)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("每组"))
        self.group_size_spin = QSpinBox()
        self.group_size_spin.setObjectName("browseGroupSize")
        self.group_size_spin.setRange(
            MIN_BROWSE_GROUP_SIZE,
            MAX_BROWSE_GROUP_SIZE,
        )
        self.group_size_spin.setSingleStep(BROWSE_GROUP_SIZE_STEP)
        self.group_size_spin.setValue(preferences.segments_per_group)
        self.group_size_spin.setSuffix(" 段")
        self.group_size_spin.editingFinished.connect(self._snap_group_size)
        size_row.addWidget(self.group_size_spin)
        size_row.addStretch()
        settings_layout.addLayout(size_row)

        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("超过"))
        self.group_threshold_spin = QSpinBox()
        self.group_threshold_spin.setObjectName("browseGroupThreshold")
        self.group_threshold_spin.setRange(1, 999)
        self.group_threshold_spin.setValue(
            preferences.activation_group_threshold
        )
        self.group_threshold_spin.setSuffix(" 组")
        threshold_row.addWidget(self.group_threshold_spin)
        threshold_row.addWidget(QLabel("或"))
        self.segment_threshold_spin = QSpinBox()
        self.segment_threshold_spin.setObjectName("browseSegmentThreshold")
        self.segment_threshold_spin.setRange(MIN_BROWSE_GROUP_SIZE, 100_000)
        self.segment_threshold_spin.setSingleStep(BROWSE_GROUP_SIZE_STEP)
        self.segment_threshold_spin.setValue(
            preferences.activation_segment_threshold
        )
        self.segment_threshold_spin.setSuffix(" 段")
        threshold_row.addWidget(self.segment_threshold_spin)
        threshold_row.addWidget(QLabel("时显示"))
        threshold_row.addStretch()
        self.enabled_checkbox = QCheckBox()
        self.enabled_checkbox.setObjectName("browseGroupEnabled")
        self.enabled_checkbox.setChecked(preferences.enabled)
        self.enabled_checkbox.toggled.connect(self._sync_enabled_copy)
        self._sync_enabled_copy(preferences.enabled)
        threshold_row.addWidget(self.enabled_checkbox)
        settings_layout.addLayout(threshold_row)
        layout.addWidget(settings)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("browseGroupButtons")
        self.save_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Save
        )
        self.cancel_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button.setText("保存设置")
        self.cancel_button.setText("取消")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.save_button.setDefault(False)
        self.save_button.setAutoDefault(False)
        self.buttons.accepted.connect(self._save_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.setStyleSheet(_DIALOG_STYLE)

    def _sync_enabled_copy(self, checked: bool) -> None:
        self.enabled_checkbox.setText("启用中" if checked else "不显示")
        self.enabled_checkbox.setAccessibleName(
            "分组轮次已启用" if checked else "分组轮次不显示"
        )

    def _save_and_accept(self) -> None:
        self._snap_group_size()
        self.saved_preferences = BrowseGroupPreferences(
            enabled=self.enabled_checkbox.isChecked(),
            segments_per_group=self.group_size_spin.value(),
            activation_group_threshold=self.group_threshold_spin.value(),
            activation_segment_threshold=self.segment_threshold_spin.value(),
            display_mode=BrowseGroupDisplayMode(
                self.display_mode_combo.currentData()
            ),
        )
        self.accept()

    def _snap_group_size(self) -> None:
        """Keep typed values on the same ten-segment grid as arrow changes."""

        value = self.group_size_spin.value()
        normalized = (
            (value + BROWSE_GROUP_SIZE_STEP // 2)
            // BROWSE_GROUP_SIZE_STEP
            * BROWSE_GROUP_SIZE_STEP
        )
        normalized = max(
            MIN_BROWSE_GROUP_SIZE,
            min(MAX_BROWSE_GROUP_SIZE, normalized),
        )
        if normalized != value:
            self.group_size_spin.setValue(normalized)


_DIALOG_STYLE = """
QDialog#browseGroupDialog {
    color: #18314a;
    background: #f3f7fa;
}
QLabel#browseGroupDialogTitle {
    color: #143650;
    font-size: 19px;
    font-weight: 750;
}
QLabel#browseGroupDocument {
    color: #0b7194;
    background: #e3f3f8;
    border-radius: 8px;
    padding: 5px 9px;
    font-weight: 700;
}
QLabel#browseGroupStatus {
    color: #5a7084;
}
QFrame#browseGroupSettings {
    background: #ffffff;
    border: 1px solid #d3dfe8;
    border-radius: 8px;
}
QLabel#browseGroupSettingsTitle {
    color: #244b68;
    font-weight: 750;
}
QSpinBox, QComboBox#browseGroupDisplayMode {
    min-height: 28px;
    color: #24445d;
    background: #f9fbfd;
    border: 1px solid #cbd7e2;
    border-radius: 4px;
    padding: 0 5px;
}
QCheckBox#browseGroupEnabled {
    color: #176344;
    font-weight: 700;
}
"""


_TURN_BAR_STYLE = """
QFrame#browseGroupTurnBar {
    color: #18314a;
}
QFrame#browseGroupTurnBar[displayMode="auto_collapse"] {
    background: transparent;
    border: none;
}
QFrame#browseGroupTurnBar[displayMode="fixed"] {
    background: #f3f7fa;
    border: 1px solid #d5e0e8;
    border-radius: 8px;
}
QStackedWidget#browseGroupTurnBarStack,
QFrame#browseGroupIndicatorPage,
QScrollArea#browseGroupIndicatorScroll,
QWidget#browseGroupIndicatorContainer {
    background: transparent;
    border: none;
}
QScrollArea#browseGroupIndicatorScroll QScrollBar:vertical {
    width: 4px;
    margin: 0;
    background: transparent;
}
QScrollArea#browseGroupIndicatorScroll QScrollBar::handle:vertical {
    min-height: 18px;
    border-radius: 2px;
    background: #a9bcc8;
}
QScrollArea#browseGroupIndicatorScroll QScrollBar::add-line:vertical,
QScrollArea#browseGroupIndicatorScroll QScrollBar::sub-line:vertical,
QScrollArea#browseGroupIndicatorScroll QScrollBar::add-page:vertical,
QScrollArea#browseGroupIndicatorScroll QScrollBar::sub-page:vertical {
    height: 0;
    background: transparent;
}
QLabel#browseGroupTurnBarTitle {
    color: #173b55;
    font-weight: 750;
}
QLabel#browseGroupTurnBarDocument {
    color: #0b7194;
    font-size: 10px;
    font-weight: 700;
}
QScrollArea#browseGroupTurnBarScroll {
    border: none;
    background: transparent;
}
QWidget#browseGroupTurnBarContainer {
    background: transparent;
}
"""


__all__ = [
    "BrowseGroupCard",
    "BrowseGroupIndicatorTick",
    "BrowseGroupPreview",
    "BrowseGroupTurnBar",
    "QtBrowseGroupDialog",
]
