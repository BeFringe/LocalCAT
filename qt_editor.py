#!/usr/bin/env python3
"""Stdlib bootstrap for the optional LocalCAT PySide6 desktop editor."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


INSTALL_HINT = "python -m pip install -r requirements-ui.txt"
APPLICATION_ICON_FILENAME = "LocalCAT-logo-silver.png"
APPLICATION_ICNS_FILENAME = "LocalCAT-logo-silver.icns"
APPLICATION_ICON_NAME = "localcat"
# hicolor's freedesktop theme index declares apps directories only through
# 512x512; resources installed into an undeclared 1024x1024/apps directory are
# ignored by GTK menu lookup.
APPLICATION_ICON_SIZE = 512


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the LocalCAT desktop editor.")
    project = parser.add_mutually_exclusive_group()
    project.add_argument("--project", type=Path, help="Open a JSON or TXT translation project.")
    project.add_argument("--sample", action="store_true", help="Open the bundled sample project.")
    project.add_argument(
        "--install-desktop-launcher",
        action="store_true",
        help="Install a Linux application-menu launcher, then exit.",
    )
    project.add_argument(
        "--install-macos-app",
        action="store_true",
        help="Install the lightweight macOS LocalCAT.app, then exit.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Override the local application-data directory.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Build one usable editor window, process events, then exit.",
    )
    parser.add_argument(
        "--bundle-smoke-marker",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def default_data_dir() -> Path:
    """Return an OS-appropriate, entirely local application-data directory."""

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "LocalCAT"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LocalCAT"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "LocalCAT"


def application_icon_path(project_root: Path | None = None) -> Path:
    """Return the one icon asset shared by the launcher and Qt windows."""

    root = (
        project_root
        if project_root is not None
        else Path(__file__).resolve().parent
    )
    return (root.expanduser().resolve() / APPLICATION_ICON_FILENAME).resolve()


def _desktop_exec_argument(value: Path) -> str:
    rendered = str(value)
    for source, replacement in (
        ("\\", "\\\\"),
        ('"', '\\"'),
        ("`", "\\`"),
        ("$", "\\$"),
    ):
        rendered = rendered.replace(source, replacement)
    return f'"{rendered}"'


def _refresh_desktop_database(applications_dir: Path) -> None:
    command = shutil.which("update-desktop-database")
    if command is None:
        return
    completed = subprocess.run(
        [command, str(applications_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"unable to refresh desktop database: {detail}")


def _install_linux_icon_resource(icon_path: Path) -> str:
    """Install the application icon in the user's freedesktop icon theme."""

    command = shutil.which("xdg-icon-resource")
    if command is None:
        return str(icon_path)
    completed = subprocess.run(
        [
            command,
            "install",
            "--novendor",
            "--mode",
            "user",
            "--context",
            "apps",
            "--size",
            str(APPLICATION_ICON_SIZE),
            str(icon_path),
            APPLICATION_ICON_NAME,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"unable to install application icon: {detail}")
    return APPLICATION_ICON_NAME


def install_desktop_launcher(
    target_dir: Path | None = None,
    icon_path: Path | None = None,
) -> Path:
    """Install an application-menu entry pointing at this checkout."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError("desktop launcher installation is currently supported on Linux")
    applications_dir = (
        target_dir
        if target_dir is not None
        else Path.home() / ".local" / "share" / "applications"
    ).expanduser().resolve()
    applications_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = applications_dir / "localcat.desktop"
    temporary_path = applications_dir / ".localcat.desktop.tmp"
    python_path = Path(sys.executable).resolve()
    script_path = Path(__file__).resolve()
    project_root = script_path.parent
    candidate_icon = (
        icon_path.expanduser().resolve()
        if icon_path is not None
        else application_icon_path(project_root)
    )
    icon_value = (
        _install_linux_icon_resource(candidate_icon)
        if candidate_icon.is_file()
        else "accessories-text-editor"
    )
    rendered = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=LocalCAT\n"
        "Comment=Local-first translation editor\n"
        f"TryExec={python_path}\n"
        f"Exec={_desktop_exec_argument(python_path)} "
        f"{_desktop_exec_argument(script_path)}\n"
        f"Path={project_root}\n"
        f"Icon={icon_value}\n"
        "Terminal=false\n"
        "Categories=Office;\n"
        "StartupWMClass=LocalCAT\n"
        "StartupNotify=true\n"
    )
    temporary_path.write_text(rendered, encoding="utf-8")
    os.replace(temporary_path, launcher_path)
    launcher_path.chmod(0o755)
    _refresh_desktop_database(applications_dir)
    return launcher_path


def install_macos_app(target_dir: Path | None = None) -> Path:
    """Build and atomically install the user-local lightweight LocalCAT.app."""

    if sys.platform != "darwin":
        raise RuntimeError("LocalCAT.app installation is only supported on macOS")
    from macos_app_launcher import MacOSAppLauncher

    root = Path(__file__).resolve().parent
    applications_dir = (
        target_dir
        if target_dir is not None
        else Path.home() / "Applications"
    ).expanduser().resolve()
    applications_dir.mkdir(parents=True, exist_ok=True)
    launcher = MacOSAppLauncher(
        icon_path=(root / APPLICATION_ICNS_FILENAME).resolve(),
    )
    return launcher.build_bundle(
        applications_dir / "LocalCAT.app",
        Path(sys.executable).resolve(),
        Path(__file__).resolve(),
    )


def _write_bundle_smoke_marker(
    path: Path,
    *,
    application_name: str,
    window_title: str,
) -> None:
    """Publish the private cold-launch marker only after a usable Qt window."""

    import json

    from macos_app_launcher import LOCALCAT_SMOKE_MARKER_VERSION

    marker = path.expanduser()
    if not marker.is_absolute() or marker.exists() or not marker.parent.is_dir():
        raise ValueError("bundle smoke marker path is invalid")
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "application_name": application_name,
                    "pid": os.getpid(),
                    "version": LOCALCAT_SMOKE_MARKER_VERSION,
                    "window_title": window_title,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _fuzzy_validation_display(composition: object):
    """Project the composition-private Gate D lifecycle into a safe DTO."""

    from capability_host import (
        CapabilityHostComposition,
        GateDRunState,
        GateDRunStatus,
    )
    from editor_contracts import (
        FuzzyValidationDisplay,
        FuzzyValidationState,
    )

    if type(composition) is not CapabilityHostComposition:
        raise TypeError(
            "fuzzy validation display requires one host composition"
        )
    owner = composition.retrieval_gate_d_owner
    if owner is None:
        return FuzzyValidationDisplay(
            state=FuzzyValidationState.IDLE,
            safe_code=None,
        )
    status = owner.status()
    if type(status) is not GateDRunStatus:
        raise TypeError("Gate D status contract is invalid")
    status.__post_init__()
    state = {
        GateDRunState.IDLE: FuzzyValidationState.IDLE,
        GateDRunState.RUNNING: FuzzyValidationState.RUNNING,
        GateDRunState.SUCCEEDED: FuzzyValidationState.SUCCEEDED,
        GateDRunState.FAILED: FuzzyValidationState.FAILED,
    }[status.state]
    return FuzzyValidationDisplay(
        state=state,
        safe_code=status.safe_code,
    )


def _start_capability_validation(
    composition: object,
    on_capability_changed: object | None = None,
) -> object:
    """Start validation and queue generation changes to one Qt receiver."""

    from datetime import datetime, timedelta, timezone
    from threading import Thread

    from capability_host import CapabilityHostComposition
    from PySide6.QtCore import QObject, Qt, Signal

    if type(composition) is not CapabilityHostComposition:
        raise TypeError(
            "editor capability validation requires one host composition"
        )

    class CapabilityCompletionBridge(QObject):
        changed = Signal()

    bridge: CapabilityCompletionBridge | None = None
    if on_capability_changed is not None:
        if isinstance(on_capability_changed, QObject):
            receiver = on_capability_changed
            callback = getattr(receiver, "refresh_suggestions", None)
        else:
            callback = on_capability_changed
            receiver = getattr(callback, "__self__", None)
        if not callable(callback) or not isinstance(
            receiver,
            QObject,
        ):
            raise TypeError(
                "capability completion requires one bound Qt receiver"
            )
        bridge = CapabilityCompletionBridge()
        _ = bridge.changed.connect(
            callback,
            Qt.ConnectionType.QueuedConnection,
        )

    generated_at_utc = datetime.now(timezone.utc).replace(microsecond=0)
    valid_until_utc = generated_at_utc + timedelta(days=1)

    def notify_capability_change() -> None:
        if bridge is not None:
            bridge.changed.emit()

    def validate() -> None:
        _ = composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
            evaluated_at_utc=generated_at_utc,
        )
        gate_c = composition.retrieval_gate_c_validation_owner
        if gate_c is None:
            return
        gate_c_generation = (
            composition.host.retrieval_operation_snapshot().generation
        )
        _ = gate_c.validate_gate_c(
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
            evaluated_at_utc=generated_at_utc,
        )
        current_generation = (
            composition.host.retrieval_operation_snapshot().generation
        )
        gate_c_changed = current_generation != gate_c_generation
        gate_d = composition.retrieval_gate_d_owner
        if gate_d is None:
            if gate_c_changed:
                notify_capability_change()
            return
        try:
            started = gate_d.start_gate_d(
                evaluated_at_utc=generated_at_utc
            )
        except BaseException:
            if gate_c_changed:
                notify_capability_change()
            raise
        notify_capability_change()
        completed = gate_d.wait()
        if completed != started:
            notify_capability_change()

    worker = Thread(
        target=validate,
        name="LocalCAT-capability-validation",
        daemon=True,
    )
    setattr(worker, "_localcat_capability_completion_bridge", bridge)
    worker.start()
    return worker


def _compose_editor_controller(repository: object):
    """Build the one formal TM composition graph owned by this app run."""

    from datetime import datetime, timezone

    from capability_host import compose_capability_host
    from editor_controller import EditorController
    from editor_tm_adapter import EditorTMAdapter
    from resource_repository import ResourceRepository
    from tm_application_composition import TMResourceResolver, TMRuntimeHost

    if type(repository) is not ResourceRepository:
        raise TypeError("editor composition requires ResourceRepository")
    capability_composition = compose_capability_host(
        evaluated_at_utc=datetime.now(timezone.utc),
    )
    runtime_host = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=repository.list_resources(),
    )
    controller = EditorController(
        repository,
        tm_adapter=EditorTMAdapter(
            runtime_host=runtime_host,
            capability_host=capability_composition.host,
            fuzzy_validation_status=lambda: _fuzzy_validation_display(
                capability_composition
            ),
        ),
    )
    return controller, capability_composition


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.install_desktop_launcher:
        try:
            launcher = install_desktop_launcher()
        except (OSError, RuntimeError) as exc:
            print(f"Unable to install LocalCAT desktop launcher: {exc}", file=sys.stderr)
            return 1
        print(f"Installed LocalCAT desktop launcher: {launcher}")
        return 0
    if args.install_macos_app:
        try:
            bundle = install_macos_app()
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Unable to install LocalCAT.app: {exc}", file=sys.stderr)
            return 1
        print(f"Installed LocalCAT.app: {bundle}")
        return 0
    try:
        from typing import cast

        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            print(
                "LocalCAT Qt editor requires PySide6.\n"
                f"Install the desktop dependencies with:\n  {INSTALL_HINT}",
                file=sys.stderr,
            )
            return 2
        raise

    try:
        from qt_editor_window import QtEditorWindow
        from resource_repository import ResourceRepository

        root = Path(__file__).resolve().parent
        data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
        repository = ResourceRepository(
            data_dir,
            default_tm_path=root / "tm.jsonl",
            default_termbase_path=root / "terms.csv",
        )
        controller, capability_composition = _compose_editor_controller(
            repository
        )
        # Retain the owner-only validation ports for the complete QApplication
        # lifetime; the Controller receives only the host read boundary.
        _ = capability_composition
        if args.project is not None:
            controller.open_project(args.project)
        elif args.sample or args.smoke_test:
            controller.load_sample()

        existing_app = QApplication.instance()
        app = (
            QApplication([sys.argv[0]])
            if existing_app is None
            else cast(QApplication, existing_app)
        )
        app.setApplicationName("LocalCAT")
        app.setOrganizationName("LocalCAT")
        app.setDesktopFileName("localcat")
        logo_path = application_icon_path(root)
        if logo_path.is_file():
            app.setWindowIcon(QIcon(str(logo_path)))
        window = QtEditorWindow(controller)
        window.show()
        validation_worker = _start_capability_validation(
            capability_composition,
            window,
        )
        # Retain both the daemon and its Qt signal bridge for the application
        # lifetime. The worker only emits; Qt invokes the window on its thread.
        _ = validation_worker
        app.processEvents()
        if args.bundle_smoke_marker is not None:
            _write_bundle_smoke_marker(
                args.bundle_smoke_marker.expanduser().resolve(),
                application_name=app.applicationName(),
                window_title=window.windowTitle(),
            )

        if args.smoke_test:
            if (
                not controller.has_project
                or window.pages.currentWidget().objectName() != "editorPage"
                or window.segment_list.count() == 0
            ):
                print("Qt editor smoke test did not reach a usable editor state.", file=sys.stderr)
                window.close()
                return 1
            window.close()
            app.processEvents()
            print("Qt editor smoke test passed.")
            return 0
        return app.exec()
    except Exception as exc:
        print(f"LocalCAT Qt editor could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
