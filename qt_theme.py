"""Small system-theme bridge shared by LocalCAT Qt surfaces."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QWidget


def color_scheme_uses_dark(value: object) -> bool | None:
    """Decode a concrete Qt color-scheme signal without rereading stale hints."""

    if isinstance(value, Qt.ColorScheme):
        if value == Qt.ColorScheme.Dark:
            return True
        if value == Qt.ColorScheme.Light:
            return False
    return None


def system_uses_dark_theme(widget: QWidget | None = None) -> bool:
    """Return the current Qt/OS dark-theme preference with a palette fallback."""

    application = QApplication.instance()
    if isinstance(application, QApplication):
        color_scheme = application.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            return True
        if color_scheme == Qt.ColorScheme.Light:
            return False
        palette = application.palette()
    elif widget is not None:
        palette = widget.palette()
    else:
        return False
    return palette.color(QPalette.ColorRole.Window).lightnessF() < 0.5


def projected_widget_uses_dark(widget: QWidget) -> bool:
    """Let custom painters consume their owning surface's current projection."""
    current = widget
    while current is not None:
        value = current.property("localcatTheme")
        if value in ("light", "dark"):
            return value == "dark"
        current = current.parentWidget()
    return system_uses_dark_theme(widget)


class ThemeSelection:
    """Remember explicit scheme facts until an Unknown signal releases them."""

    def __init__(
        self,
        widget: QWidget,
        fallback: Callable[[QWidget | None], bool] | None = None,
    ) -> None:
        self._widget = widget
        self._fallback = fallback
        self._explicit: bool | None = None

    def resolve(self, args: tuple = ()) -> bool:
        if args:
            signaled = color_scheme_uses_dark(args[0])
            if signaled is not None:
                self._explicit = signaled
            elif args[0] == Qt.ColorScheme.Unknown:
                self._explicit = None
        if self._explicit is not None:
            return self._explicit
        parent = self._widget.parentWidget()
        while parent is not None:
            value = parent.property("localcatTheme")
            if value in ("light", "dark"):
                return value == "dark"
            parent = parent.parentWidget()
        fallback = self._fallback or system_uses_dark_theme
        return fallback(self._widget)


class ThemeBinding(QObject):
    """Parent-owned presentation binding; no domain callbacks or state rebuild."""

    def __init__(self, widget: QWidget, light: str, dark: str) -> None:
        super().__init__(widget)
        self._widget = widget
        self._light = light
        self._dark = dark
        self._selection = ThemeSelection(widget)
        self._applying = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        widget.installEventFilter(self)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.styleHints().colorSchemeChanged.connect(self.refresh)
        self.refresh()

    def eventFilter(self, watched, event):  # noqa: N802
        # Qt may deliver destruction events after Python instance attributes
        # have been cleared; no queued work is valid in that phase.
        if not getattr(self, "_applying", True) and event.type() in (
            QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange,
            QEvent.Type.ThemeChange,
        ):
            self._timer.start(0)
        return False

    def refresh(self, *args):
        dark = self._selection.resolve(args)
        target = self._light + (self._dark if dark else "")
        if self._applying or self._widget.styleSheet() == target:
            return
        # Import lazily because popup styles themselves use the theme bridge.
        from qt_control_styles import (
            LOCALCAT_COMBO_POPUP_STYLE, LOCALCAT_DARK_COMBO_POPUP_STYLE,
        )
        self._applying = True
        enabled = self._widget.updatesEnabled()
        self._widget.setUpdatesEnabled(False)
        try:
            self._widget.setProperty("localcatTheme", "dark" if dark else "light")
            self._widget.setStyleSheet("")
            self._widget.setStyleSheet(target)
            for combo in self._widget.findChildren(QComboBox):
                combo.view().setStyleSheet(
                    LOCALCAT_DARK_COMBO_POPUP_STYLE if dark else LOCALCAT_COMBO_POPUP_STYLE
                )
            for child in (self._widget, *self._widget.findChildren(QWidget)):
                child.style().unpolish(child)
                child.style().polish(child)
                child.update()
        finally:
            self._widget.setUpdatesEnabled(enabled)
            self._applying = False
