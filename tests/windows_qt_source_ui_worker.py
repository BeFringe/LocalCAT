"""Native Windows Qt worker for the WA-08 source UI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def _write_result(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _events() -> None:
    from PySide6.QtCore import QCoreApplication, QEventLoop

    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)


def _icon_matches(icon: object, expected: object) -> bool:
    if icon.isNull() or expected.isNull():
        return False
    return icon.pixmap(64, 64).toImage() == expected.pixmap(64, 64).toImage()


def _run(project: Path, appdata: Path, marker: Path) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")

    from platform_fs import compose_platform_file_backend
    from platform_source_authority import compose_rooted_source_authority
    from qt_editor import (
        APPLICATION_ICON_FILENAME,
        APPLICATION_VERSION,
        _compose_chunk_controller,
        _compose_editor_controller,
    )
    from qt_source_resources import resolve_source_qt_resources

    backend = compose_platform_file_backend(WORKSPACE_ROOT)
    source_authority = compose_rooted_source_authority(
        WORKSPACE_ROOT,
        backend=backend,
    )
    window = None
    try:
        resources = resolve_source_qt_resources(
            source_authority,
            logo_filename=APPLICATION_ICON_FILENAME,
        )
        appdata.mkdir(parents=True, exist_ok=True)
        data_root = backend.bind_root(appdata)
        try:
            data_root.reprove()
        finally:
            data_root.close()

        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtGui import QIcon, QPalette
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        from qt_editor_window import QtEditorWindow
        from qt_settings_dialog import QtSettingsDialog
        from qt_speaker_avatar import SpeakerAvatarCatalog, resource_png_pixmap
        from qt_speaker_inventory_dialog import QtSpeakerInventoryDialog
        from resource_repository import ResourceRepository

        QApplication.setApplicationName("LocalCAT")
        QApplication.setApplicationDisplayName("LocalCAT")
        QApplication.setOrganizationName("LocalCAT")
        QApplication.setApplicationVersion(APPLICATION_VERSION)
        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName("LocalCAT")
        app.setApplicationDisplayName("LocalCAT")
        app.setOrganizationName("LocalCAT")
        app.setApplicationVersion(APPLICATION_VERSION)

        logo_pixmap = resource_png_pixmap(resources.logo)
        if logo_pixmap is None:
            raise AssertionError("source silver logo did not decode")
        expected_icon = QIcon(logo_pixmap)
        app.setWindowIcon(expected_icon)

        repository = ResourceRepository(
            appdata,
            default_tm_path=WORKSPACE_ROOT / "tm.jsonl",
            default_termbase_path=WORKSPACE_ROOT / "terms.csv",
            backend=backend,
        )
        controller, composition = _compose_editor_controller(
            repository,
            source_authority=source_authority,
        )
        chunk_controller = _compose_chunk_controller(controller, repository)
        window = QtEditorWindow(
            controller,
            chunk_controller=chunk_controller,
            speaker_avatar_catalog=SpeakerAvatarCatalog(
                resources.speaker_avatars
            ),
        )
        window.show()
        if not window.open_project_path(project):
            raise AssertionError("production Qt window did not open the JSON project")
        window.raise_()
        window.activateWindow()
        _events()
        exposed = bool(QTest.qWaitForWindowExposed(window, 5_000))
        if app.platformName() != "windows":
            raise AssertionError(f"expected windows QPA, got {app.platformName()!r}")
        if not exposed or not window.isVisible():
            raise AssertionError("native Qt editor window did not become visible")

        style_hints = app.styleHints()
        style_hints.setColorScheme(Qt.ColorScheme.Dark)
        _events()
        settings = QtSettingsDialog(controller, window)
        settings.show()
        _events()
        style_hints.setColorScheme(Qt.ColorScheme.Light)
        _events()
        settings_layers = (
            settings.resource_tables_scroll,
            settings.resource_tables_scroll.viewport(),
            settings.resource_tables_content,
        )
        settings_light_theme = settings.property("localcatTheme") == "light"
        settings_resource_layers_light = all(
            widget.palette().color(QPalette.ColorRole.Window).name() == "#f3f6fa"
            for widget in settings_layers
        )
        settings.close()
        settings.deleteLater()
        style_hints.unsetColorScheme()
        _events()

        search_visible_before = window.project_search_panel.isVisible()
        window.target_editor.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClick(
            window.target_editor,
            Qt.Key.Key_F,
            Qt.KeyboardModifier.ControlModifier,
        )
        _events()
        search_visible_after = window.project_search_panel.isVisible()
        search_input_focused = window.project_search_input.hasFocus()
        if search_visible_before or not search_visible_after or not search_input_focused:
            raise AssertionError("native Qt Ctrl+F did not change the search state")

        dialog_facts: dict[str, object] = {}

        def inspect_and_close_dialog() -> None:
            dialog = None
            try:
                dialogs = tuple(
                    widget
                    for widget in QApplication.topLevelWidgets()
                    if type(widget) is QtSpeakerInventoryDialog
                )
                if len(dialogs) != 1:
                    raise AssertionError("expected one production speaker dialog")
                dialog = dialogs[0]
                dialog_facts.update(
                    {
                        "dialog_accessible_name": dialog.accessibleName(),
                        "dialog_icon_matches_logo": _icon_matches(
                            dialog.windowIcon(),
                            expected_icon,
                        ),
                        "dialog_modal": QApplication.activeModalWidget() is dialog,
                        "dialog_rows": dialog.table.rowCount(),
                        "dialog_visible_in_nested_loop": dialog.isVisible(),
                    }
                )
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                _events()
                dialog_facts["dialog_closed_by_escape"] = not dialog.isVisible()
            except BaseException as error:
                dialog_facts["error"] = f"{type(error).__name__}: {error}"
            finally:
                if dialog is not None and dialog.isVisible():
                    dialog.reject()

        QTimer.singleShot(50, inspect_and_close_dialog)
        window.speaker_inventory_action.trigger()
        _events()
        if "error" in dialog_facts:
            raise AssertionError(str(dialog_facts["error"]))
        required_dialog_facts = (
            "dialog_accessible_name",
            "dialog_icon_matches_logo",
            "dialog_modal",
            "dialog_rows",
            "dialog_visible_in_nested_loop",
            "dialog_closed_by_escape",
        )
        if any(name not in dialog_facts for name in required_dialog_facts):
            raise AssertionError("speaker dialog nested-loop evidence is incomplete")

        payload = {
            "application_icon_matches_logo": _icon_matches(
                app.windowIcon(),
                expected_icon,
            ),
            "cwd": str(Path.cwd()),
            "keyboard_search_focused": search_input_focused,
            "keyboard_search_visible_after": search_visible_after,
            "keyboard_search_visible_before": search_visible_before,
            "pid": os.getpid(),
            "platform_name": app.platformName(),
            "settings_light_theme": settings_light_theme,
            "settings_resource_layers_light": settings_resource_layers_light,
            "window_exposed": exposed,
            "window_icon_matches_logo": _icon_matches(
                window.windowIcon(),
                expected_icon,
            ),
            "window_visible": window.isVisible(),
            "xlwings_imported": "xlwings" in sys.modules,
            **dialog_facts,
        }
        if not window.close_current_project():
            raise AssertionError("production Qt window did not close the clean project")
        window.close()
        _events()
        _write_result(marker, payload)
        _ = composition
        return 0
    finally:
        if window is not None:
            window.close()
            _events()
        source_authority.close()


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        return 2
    project, appdata, marker = (
        Path(value).expanduser().resolve() for value in arguments
    )
    return _run(project, appdata, marker)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
