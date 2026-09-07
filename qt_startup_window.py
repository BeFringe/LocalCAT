"""Lightweight, business-free home surface while resource composition runs."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qt_theme import color_scheme_uses_dark, system_uses_dark_theme


class _StartupCard(QFrame):
    """Rounded startup surface without installing a cascading style sheet."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._surface = QColor("#ffffff")
        self._border = QColor("#d2deeb")

    def set_theme_colors(self, surface: str, border: str) -> None:
        self._surface = QColor(surface)
        self._border = QColor(border)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, self._surface)
        self.setPalette(palette)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._border, 1))
        painter.setBrush(self._surface)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(bounds, 12, 12)
        event.accept()


class _StartupButton(QPushButton):
    """Small painted button that avoids constructing QStyleSheetStyle."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(text, parent)
        self._disabled = QColor("#eef3f7")
        self._muted = QColor("#62758a")
        self._border = QColor("#d2deeb")
        button_font = _font(pixels=14)
        button_font.setWeight(QFont.Weight.DemiBold)
        self.setFont(button_font)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_theme_colors(self, disabled: str, muted: str, border: str) -> None:
        self._disabled = QColor(disabled)
        self._muted = QColor(muted)
        self._border = QColor(border)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual name
        text_size = self.fontMetrics().size(Qt.TextFlag.TextSingleLine, self.text())
        return QSize(text_size.width() + 44, max(42, text_size.height() + 20))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual name
        enabled = self.isEnabled()
        if enabled:
            background = QColor("#007a99" if self.isDown() else (
                "#0088ab" if self.underMouse() else "#079ac0"
            ))
            foreground = QColor("#ffffff")
            border = QColor("#79d8ef" if self.hasFocus() else background)
            border_width = 2 if self.hasFocus() else 1
        else:
            background = self._disabled
            foreground = self._muted
            border = self._border
            border_width = 1
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, border_width))
        painter.setBrush(background)
        inset = border_width / 2
        bounds = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawRoundedRect(bounds, 6, 6)
        painter.setPen(foreground)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())
        event.accept()


class _StartupProgress(QFrame):
    """Static indeterminate accent without the native busy-bar polish cost."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._accent = QColor("#09a1c9")
        self._track = QColor("#eef3f7")

    def maximum(self) -> int:
        return 0

    def set_theme_colors(self, accent: str, track: str) -> None:
        self._accent = QColor(accent)
        self._track = QColor(track)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        bounds = QRectF(self.rect())
        painter.setBrush(self._track)
        painter.drawRoundedRect(bounds, 2, 2)
        painter.setBrush(self._accent)
        chunk_width = max(48.0, bounds.width() * 0.28)
        chunk = QRectF(
            bounds.center().x() - chunk_width / 2,
            bounds.top(),
            chunk_width,
            bounds.height(),
        )
        painter.drawRoundedRect(chunk, 2, 2)
        event.accept()


def _set_background(widget: QWidget, color: str) -> None:
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(color))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def _set_label_color(label: QLabel, color: str) -> None:
    palette = label.palette()
    palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
    label.setPalette(palette)


def _font(*, pixels: int, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(("Segoe UI", "Microsoft YaHei"))
    font.setPixelSize(pixels)
    font.setBold(bold)
    return font


class QtStartupWindow(QMainWindow):
    """Show honest loading/failure state; the bootstrap owns all actual work."""

    retry_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self._dark_theme: bool | None = None
        self._theme_refresh_pending = False
        self._theme_refresh_in_progress = False
        self.setObjectName("startupWindow")
        self.setWindowTitle("LocalCAT")
        self.resize(1440, 880)
        self.setMinimumSize(1080, 700)

        self.root = QWidget(self)
        self.root.setObjectName("startupRoot")
        self.root.setFont(_font(pixels=14))
        self.setCentralWidget(self.root)
        page = QVBoxLayout(self.root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        self.header = QFrame(self.root)
        self.header.setObjectName("startupHeader")
        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(28, 22, 28, 22)
        brand_column = QVBoxLayout()
        brand_column.setSpacing(2)
        self.brand_label = QLabel("LocalCAT", self.header)
        self.brand_label.setObjectName("startupBrand")
        self.brand_label.setFont(_font(pixels=25, bold=True))
        self.brand_caption = QLabel("LOCAL TRANSLATION WORKSPACE", self.header)
        self.brand_caption.setObjectName("startupBrandCaption")
        self.brand_caption.setFont(_font(pixels=11))
        brand_column.addWidget(self.brand_label)
        brand_column.addWidget(self.brand_caption)
        header_row.addLayout(brand_column)
        header_row.addStretch()
        self.privacy_label = QLabel("本地 · 私密", self.header)
        self.privacy_label.setObjectName("startupPrivacy")
        privacy_font = _font(pixels=14)
        privacy_font.setWeight(QFont.Weight.DemiBold)
        self.privacy_label.setFont(privacy_font)
        header_row.addWidget(self.privacy_label)
        page.addWidget(self.header)

        middle = QHBoxLayout()
        middle.setContentsMargins(28, 36, 28, 36)
        middle.addStretch()
        self.card = _StartupCard(self.root)
        self.card.setObjectName("startupCard")
        self.card.setMaximumWidth(680)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(40, 38, 40, 38)
        card_layout.setSpacing(18)
        self.heading_label = QLabel("开始翻译", self.card)
        self.heading_label.setObjectName("startupHeading")
        self.heading_label.setFont(_font(pixels=28, bold=True))
        card_layout.addWidget(self.heading_label)
        self.intro_label = QLabel("打开本地项目，在同一工作台完成翻译与校对。", self.card)
        self.intro_label.setObjectName("startupIntro")
        self.intro_label.setWordWrap(True)
        card_layout.addWidget(self.intro_label)

        self.open_project_button = _StartupButton("打开项目", self.card)
        self.open_project_button.setObjectName("startupOpenProject")
        self.open_project_button.setEnabled(False)
        self.open_project_button.setToolTip("语言资源加载完成后即可打开项目。")
        card_layout.addWidget(self.open_project_button, 0, Qt.AlignmentFlag.AlignLeft)
        card_layout.addSpacing(16)

        self.status_label = QLabel(self.card)
        self.status_label.setObjectName("startupStatus")
        status_font = _font(pixels=14)
        status_font.setWeight(QFont.Weight.DemiBold)
        self.status_label.setFont(status_font)
        self.status_label.setWordWrap(True)
        card_layout.addWidget(self.status_label)
        self.progress_bar = _StartupProgress(self.card)
        self.progress_bar.setObjectName("startupProgress")
        self.progress_bar.setFixedHeight(5)
        card_layout.addWidget(self.progress_bar)
        self.detail_label = QLabel(self.card)
        self.detail_label.setObjectName("startupDetail")
        self.detail_label.setWordWrap(True)
        card_layout.addWidget(self.detail_label)
        self.retry_button = _StartupButton("重试加载", self.card)
        self.retry_button.setObjectName("startupRetry")
        self.retry_button.clicked.connect(self._request_retry)
        card_layout.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignLeft)
        middle.addWidget(self.card, 2, Qt.AlignmentFlag.AlignVCenter)
        middle.addStretch()
        page.addLayout(middle, 1)
        self.set_loading()
        self._apply_system_theme()
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.styleHints().colorSchemeChanged.connect(self._apply_system_theme)

    def set_loading(self) -> None:
        """Reset display only; never start work or reopen a closed surface."""

        if self.closed:
            return
        self.status_label.setText("正在加载语言资源")
        self.detail_label.setText("正在检查本地翻译记忆与术语表。加载完成后即可打开项目。")
        self.progress_bar.show()
        self.retry_button.setEnabled(False)
        self.retry_button.hide()

    def set_loading_failed(self) -> None:
        """Render only fixed safe guidance, never raw worker exception details."""

        if self.closed:
            return
        self.status_label.setText("语言资源加载失败")
        self.detail_label.setText(
            "尚未打开项目。请重试加载语言资源，或关闭窗口。\n"
            "LOCALCAT.STARTUP.RESOURCES_FAILED"
        )
        self.progress_bar.hide()
        self.retry_button.setEnabled(True)
        self.retry_button.show()

    def _request_retry(self) -> None:
        if self.closed or not self.retry_button.isEnabled():
            return
        self.set_loading()
        self.retry_requested.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt virtual name
        super().closeEvent(event)
        if event.isAccepted():
            self.closed = True

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() in (
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.ThemeChange,
        ):
            if (
                not getattr(self, "closed", True)
                and not self._theme_refresh_in_progress
                and not self._theme_refresh_pending
            ):
                self._theme_refresh_pending = True
                QTimer.singleShot(0, self._apply_queued_system_theme)
        return result

    def _apply_queued_system_theme(self) -> None:
        self._theme_refresh_pending = False
        if not self.closed:
            self._apply_system_theme()

    def _apply_system_theme(self, scheme: object = None) -> None:
        if self.closed or self._theme_refresh_in_progress:
            return
        dark = color_scheme_uses_dark(scheme)
        if dark is None:
            dark = system_uses_dark_theme(self)
        if self._dark_theme is dark:
            return
        self._dark_theme = dark
        self._theme_refresh_in_progress = True
        try:
            self.setProperty("localcatTheme", "dark" if dark else "light")
            background = "#17191c" if dark else "#edf2f7"
            surface = "#1f2328" if dark else "#ffffff"
            text = "#e7edf3" if dark else "#17344d"
            muted = "#aab6c3" if dark else "#62758a"
            border = "#3b424a" if dark else "#d2deeb"
            disabled = "#292e34" if dark else "#eef3f7"
            _set_background(self, background)
            _set_background(self.root, background)
            _set_background(self.header, "#0c3158")
            self.card.set_theme_colors(surface, border)
            for label in (self.heading_label, self.status_label):
                _set_label_color(label, text)
            for label in (self.intro_label, self.detail_label):
                _set_label_color(label, muted)
            _set_label_color(self.brand_label, "#ffffff")
            _set_label_color(self.brand_caption, "#91b5d1")
            _set_label_color(self.privacy_label, "#99d8f0")
            self.open_project_button.set_theme_colors(disabled, muted, border)
            self.retry_button.set_theme_colors(disabled, muted, border)
            self.progress_bar.set_theme_colors("#09a1c9", disabled)
            self.update()
        finally:
            self._theme_refresh_in_progress = False
