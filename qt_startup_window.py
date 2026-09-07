"""Lightweight, business-free home surface while resource composition runs."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qt_theme import color_scheme_uses_dark, system_uses_dark_theme


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
        self.setCentralWidget(self.root)
        page = QVBoxLayout(self.root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        header = QFrame(self.root)
        header.setObjectName("startupHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(28, 22, 28, 22)
        brand_column = QVBoxLayout()
        brand_column.setSpacing(2)
        brand = QLabel("LocalCAT", header)
        brand.setObjectName("startupBrand")
        caption = QLabel("LOCAL TRANSLATION WORKSPACE", header)
        caption.setObjectName("startupBrandCaption")
        brand_column.addWidget(brand)
        brand_column.addWidget(caption)
        header_row.addLayout(brand_column)
        header_row.addStretch()
        privacy = QLabel("本地 · 私密", header)
        privacy.setObjectName("startupPrivacy")
        header_row.addWidget(privacy)
        page.addWidget(header)

        middle = QHBoxLayout()
        middle.setContentsMargins(28, 36, 28, 36)
        middle.addStretch()
        self.card = QFrame(self.root)
        self.card.setObjectName("startupCard")
        self.card.setMaximumWidth(680)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(40, 38, 40, 38)
        card_layout.setSpacing(18)
        heading = QLabel("开始翻译", self.card)
        heading.setObjectName("startupHeading")
        card_layout.addWidget(heading)
        intro = QLabel("打开本地项目，在同一工作台完成翻译与校对。", self.card)
        intro.setObjectName("startupIntro")
        intro.setWordWrap(True)
        card_layout.addWidget(intro)

        self.open_project_button = QPushButton("打开项目", self.card)
        self.open_project_button.setObjectName("startupOpenProject")
        self.open_project_button.setEnabled(False)
        self.open_project_button.setToolTip("语言资源加载完成后即可打开项目。")
        card_layout.addWidget(self.open_project_button, 0, Qt.AlignmentFlag.AlignLeft)
        card_layout.addSpacing(16)

        self.status_label = QLabel(self.card)
        self.status_label.setObjectName("startupStatus")
        self.status_label.setWordWrap(True)
        card_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar(self.card)
        self.progress_bar.setObjectName("startupProgress")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        card_layout.addWidget(self.progress_bar)
        self.detail_label = QLabel(self.card)
        self.detail_label.setObjectName("startupDetail")
        self.detail_label.setWordWrap(True)
        card_layout.addWidget(self.detail_label)
        self.retry_button = QPushButton("重试加载", self.card)
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
            self.setStyleSheet(f"""
                QWidget {{ font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 14px; }}
                QMainWindow#startupWindow, QWidget#startupRoot {{ background: {background}; }}
                QFrame#startupHeader {{ background: #0c3158; }}
                QLabel {{ color: {text}; background: transparent; }}
                QLabel#startupBrand {{ color: #ffffff; font-size: 25px; font-weight: 700; }}
                QLabel#startupBrandCaption {{ color: #91b5d1; font-size: 11px; letter-spacing: 1px; }}
                QLabel#startupPrivacy {{ color: #99d8f0; font-weight: 600; }}
                QFrame#startupCard {{ background: {surface}; border: 1px solid {border}; border-radius: 12px; }}
                QLabel#startupHeading {{ font-size: 28px; font-weight: 700; }}
                QLabel#startupIntro, QLabel#startupDetail {{ color: {muted}; }}
                QLabel#startupStatus {{ font-weight: 600; }}
                QPushButton {{ padding: 10px 22px; border-radius: 6px; border: 1px solid #079ac0; background: #079ac0; color: #ffffff; font-weight: 600; }}
                QPushButton:hover {{ background: #0088ab; border-color: #0088ab; }}
                QPushButton:focus {{ border: 2px solid #79d8ef; padding: 9px 21px; }}
                QPushButton:disabled {{ background: {disabled}; color: {muted}; border: 1px solid {border}; }}
                QProgressBar#startupProgress {{ border: none; border-radius: 2px; background: {disabled}; }}
                QProgressBar#startupProgress::chunk {{ background: #09a1c9; border-radius: 2px; }}
            """)
            self.update()
        finally:
            self._theme_refresh_in_progress = False
