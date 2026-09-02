"""Fresh-process Qt worker for the WA-08 source Project journey."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def _write_result(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _events() -> None:
    from PySide6.QtCore import QCoreApplication, QEventLoop

    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)


def _session(appdata: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")

    from PySide6.QtWidgets import QApplication

    from qt_editor import _compose_editor_controller
    from qt_editor_window import QtEditorWindow
    from resource_repository import ResourceRepository

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    controller, composition = _compose_editor_controller(
        ResourceRepository(appdata)
    )
    window = QtEditorWindow(controller)

    def fail_on_error(title: str, message: str) -> None:
        raise AssertionError(f"unexpected Qt error: {title}: {message}")

    window._show_error = fail_on_error
    window.show()
    _events()
    if app.platformName() != "windows":
        raise AssertionError(f"expected windows QPA, got {app.platformName()!r}")
    if not window.isVisible():
        raise AssertionError("QtEditorWindow did not become visible")
    return app, controller, composition, window


def _close(window: object) -> None:
    if not window.close_current_project():
        raise AssertionError("Qt failed to close the current project")
    window.close()
    _events()


def _legacy_save(
    source: Path,
    destination: Path,
    appdata: Path,
    result_path: Path,
) -> int:
    app, controller, composition, window = _session(appdata)
    if not window.open_project_path(source):
        raise AssertionError("Qt failed to open the legacy source project")
    window.target_editor.setPlainText("Windows Qt legacy target")
    _events()
    dirty_before_save = controller.dirty
    session_id = controller.project_session_id
    segment_id = controller.current_segment.id
    if not window.save_project_path(destination):
        raise AssertionError("Qt failed to save the legacy project")
    payload = {
        "pid": os.getpid(),
        "qpa": app.platformName(),
        "cwd": str(Path.cwd()),
        "session_id": session_id,
        "segment_id": segment_id,
        "target": controller.current_segment.target,
        "dirty_before_save": dirty_before_save,
        "dirty_after_save": controller.dirty,
        "saved_path": str(controller.project.path),
    }
    _close(window)
    payload["closed"] = not controller.has_active_project
    _write_result(result_path, payload)
    return 0


def _legacy_open(source: Path, appdata: Path, result_path: Path) -> int:
    app, controller, composition, window = _session(appdata)
    if not window.open_project_path(source):
        raise AssertionError("Qt failed to fresh-open the saved legacy project")
    payload = {
        "pid": os.getpid(),
        "qpa": app.platformName(),
        "cwd": str(Path.cwd()),
        "session_id": controller.project_session_id,
        "project_name": controller.project.name,
        "segment_id": controller.current_segment.id,
        "segment_count": len(controller.project.segments),
        "target": controller.current_segment.target,
        "dirty": controller.dirty,
        "opened_path": str(controller.project.path),
    }
    _close(window)
    payload["closed"] = not controller.has_active_project
    _write_result(result_path, payload)
    return 0


def _identity_payload(controller: object) -> dict[str, object]:
    identity = controller.current_workspace_identity
    project = identity.document.project
    return {
        "project_id": project.project_id,
        "session_id": project.session_id,
        "document_id": identity.document.document_id,
        "local_segment_id": identity.local_segment_id,
    }


def _package_save(
    source_root: Path,
    first: Path,
    second: Path,
    destination: Path,
    appdata: Path,
    result_path: Path,
) -> int:
    from project_package import ProjectPackageService
    from project_workspace_intake import SelectedProjectDocumentsRequest

    app, controller, composition, window = _session(appdata)
    created = window.create_workspace_project_from_selected_files(
        source_root,
        (first, second),
        SelectedProjectDocumentsRequest(
            name="Windows Qt ProjectPackage",
            source_locale="en",
            target_locale="zh-CN",
        ),
        destination,
    )
    if not created:
        raise AssertionError("Qt failed to create the ProjectPackage")
    window.target_editor.setPlainText("Windows Qt package target")
    _events()
    dirty_before_save = controller.workspace_save_state.project_dirty
    identity_before_save = _identity_payload(controller)

    captured: list[object] = []
    save_workspace_package = controller.save_workspace_package

    def capture_save_result():
        result = save_workspace_package()
        captured.append(result)
        return result

    controller.save_workspace_package = capture_save_result
    if not window.save_workspace_project_package():
        raise AssertionError("Qt failed to save the ProjectPackage")
    if len(captured) != 1:
        raise AssertionError("Qt did not consume exactly one Controller save result")
    result = captured[0]
    receipt = result.receipt
    if receipt is None:
        raise AssertionError("committed ProjectPackage save did not issue a receipt")
    save_state = controller.workspace_save_state
    recovery_pending = ProjectPackageService().inspect_recovery(destination) is not None
    payload = {
        "pid": os.getpid(),
        "qpa": app.platformName(),
        "cwd": str(Path.cwd()),
        "identity_before_save": identity_before_save,
        "identity_after_save": _identity_payload(controller),
        "target": controller.current_workspace_segment.target,
        "dirty_before_save": dirty_before_save,
        "dirty_after_save": save_state.project_dirty,
        "dirty_document_ids": list(save_state.dirty_document_ids),
        "artifact_digest": save_state.artifact_digest,
        "feedback": window.workspace_save_feedback.text(),
        "recovery_pending": recovery_pending,
        "receipt": {
            "project_id": receipt.project_id,
            "artifact_digest": receipt.artifact_digest,
            "workspace_content_digest": receipt.workspace_content_digest,
            "document_count": receipt.document_count,
            "segment_count": receipt.segment_count,
            "durable": receipt.durable,
            "recovery_required": receipt.recovery_required,
        },
    }
    _close(window)
    payload["closed"] = not controller.has_active_project
    _write_result(result_path, payload)
    return 0


def _package_open(source: Path, appdata: Path, result_path: Path) -> int:
    from project_package import ProjectPackageService

    app, controller, composition, window = _session(appdata)
    recovery_pending = ProjectPackageService().inspect_recovery(source) is not None
    if not window.open_project_package_path(source):
        raise AssertionError("Qt failed to fresh-open the ProjectPackage")
    opened = ProjectPackageService().open(source)
    save_state = controller.workspace_save_state
    payload = {
        "pid": os.getpid(),
        "qpa": app.platformName(),
        "cwd": str(Path.cwd()),
        "identity": _identity_payload(controller),
        "project_name": controller.workspace_view.name,
        "document_count": len(controller.workspace_view.documents),
        "segment_count": len(controller.workspace_view.segments),
        "target": controller.current_workspace_segment.target,
        "dirty": save_state.project_dirty,
        "dirty_document_ids": list(save_state.dirty_document_ids),
        "artifact_digest": save_state.artifact_digest,
        "workspace_content_digest": opened.validation.workspace_content_digest,
        "recovery_pending": recovery_pending,
        "feedback": window.workspace_save_feedback.text(),
    }
    _close(window)
    payload["closed"] = not controller.has_active_project
    _write_result(result_path, payload)
    return 0


def main(arguments: list[str]) -> int:
    mode = arguments[0]
    paths = tuple(Path(value).resolve() for value in arguments[1:])
    if mode == "legacy-save" and len(paths) == 4:
        return _legacy_save(*paths)
    if mode == "legacy-open" and len(paths) == 3:
        return _legacy_open(*paths)
    if mode == "package-save" and len(paths) == 6:
        return _package_save(*paths)
    if mode == "package-open" and len(paths) == 3:
        return _package_open(*paths)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
