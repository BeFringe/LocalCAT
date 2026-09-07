#!/usr/bin/env python3
"""Stdlib bootstrap for the optional LocalCAT PySide6 desktop editor."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


INSTALL_HINT = "python -m pip install -r requirements-ui.txt"
APPLICATION_ICON_FILENAME = "LocalCAT-logo-silver.png"
APPLICATION_ICNS_FILENAME = "LocalCAT-logo-silver.icns"
APPLICATION_VERSION = "1.0"
APPLICATION_ICON_NAME = "localcat"
# hicolor's freedesktop theme index declares apps directories only through
# 512x512; resources installed into an undeclared 1024x1024/apps directory are
# ignored by GTK menu lookup.
APPLICATION_ICON_SIZE = 512
LOCALCAT_NATIVE_LAUNCH_ENV = "LOCALCAT_NATIVE_LAUNCH"
LOCALCAT_DIRECT_HANDOFF_VERSION = 1
STARTUP_FAILURE_CODE = "LOCALCAT.STARTUP.FAILED"


def _startup_diagnostic(event: dict[str, object]) -> None:
    """Optional stdlib guardian sink; direct launches do not persist diagnostics."""


class _StartupTrace:
    """Record fixed phase names and numeric timing, never application content."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.started = time.perf_counter()

    def finish(self, next_stage: str | None = None, error: Exception | None = None) -> None:
        event: dict[str, object] = {
            "stage": self.stage,
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000),
            "status": "failed" if error is not None else "ok",
        }
        if error is not None:
            event["exception_type"] = type(error).__name__
            traceback = error.__traceback__
            while traceback is not None:
                module = traceback.tb_frame.f_globals.get("__name__")
                if module in {
                    "qt_editor", "editor_controller", "resource_repository",
                    "tm_application_composition", "tm_engine", "tm_migration",
                    "tm_sqlite_store", "capability_host", "platform_fs",
                    "platform_fs_windows", "platform_source_authority",
                    "qt_source_resources", "qt_editor_window",
                }:
                    event["failure_module"] = module
                    event["failure_line"] = traceback.tb_lineno
                traceback = traceback.tb_next
            for name in ("errno", "winerror"):
                value = getattr(error, name, None)
                if type(value) is int:
                    event[name] = value
        try:
            _startup_diagnostic(event)
        except Exception:
            # Diagnostics must not change the startup result.
            pass
        if next_stage is not None:
            self.stage = next_stage
            self.started = time.perf_counter()


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
    project.add_argument(
        "--install-windows-launcher",
        action="store_true",
        help="Install the user-managed Windows source launcher, then exit.",
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
    parser.add_argument(
        "--source-launch-smoke-marker",
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


def _stabilize_windows_tooltips(application: object) -> None:
    """Show Windows tooltips without the native first-frame animation flash."""

    if os.name != "nt":
        return
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    if not isinstance(application, QApplication):
        raise TypeError("tooltip presentation requires QApplication")
    application.setEffectEnabled(Qt.UIEffect.UI_AnimateTooltip, False)
    application.setEffectEnabled(Qt.UIEffect.UI_FadeTooltip, False)


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
    if _localcat_app_is_running():
        raise RuntimeError("quit LocalCAT before installing an updated LocalCAT.app")
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


def _macos_bundle_candidates() -> tuple[Path, ...]:
    """Return deterministic installed-bundle candidates without following links."""

    return (
        Path.home() / "Applications" / "LocalCAT.app",
        Path("/Applications/LocalCAT.app"),
    )


def _compatible_macos_native_launcher(bundle: Path) -> Path | None:
    if not isinstance(bundle, Path) or bundle.is_symlink() or not bundle.is_dir():
        return None
    contents = bundle / "Contents"
    plist_path = contents / "Info.plist"
    executable = contents / "MacOS" / "LocalCAT"
    if plist_path.is_symlink() or executable.is_symlink():
        return None
    try:
        plist_metadata = plist_path.stat()
        executable_metadata = executable.stat()
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        return None
    if (
        not stat.S_ISREG(plist_metadata.st_mode)
        or not stat.S_ISREG(executable_metadata.st_mode)
        or not executable_metadata.st_mode & 0o111
        or type(info) is not dict
        or info.get("CFBundleDisplayName") != "LocalCAT"
        or info.get("CFBundleExecutable") != "LocalCAT"
        or info.get("CFBundleIdentifier") != "app.localcat.desktop"
        or info.get("LocalCATDirectHandoffVersion")
        != LOCALCAT_DIRECT_HANDOFF_VERSION
    ):
        return None
    return executable


def _handoff_to_macos_native_launcher(argv: tuple[str, ...]) -> None:
    """Let LaunchServices start the signed bundle against this checkout."""

    if sys.platform != "darwin" or os.environ.get(LOCALCAT_NATIVE_LAUNCH_ENV) == "1":
        return
    executable = next(
        (
            candidate
            for bundle in _macos_bundle_candidates()
            if (candidate := _compatible_macos_native_launcher(bundle)) is not None
        ),
        None,
    )
    if executable is None:
        return
    bundle = executable.parents[2]
    open_command = Path("/usr/bin/open")
    try:
        open_metadata = open_command.stat()
    except OSError:
        return
    if not stat.S_ISREG(open_metadata.st_mode) or not open_metadata.st_mode & 0o111:
        return
    os.execve(
        str(open_command),
        [
            str(open_command),
            "-W",
            str(bundle),
            "--args",
            "--localcat-direct-python",
            str(Path(sys.executable).resolve()),
            "--localcat-direct-bootstrap",
            str(Path(__file__).resolve()),
            *argv,
        ],
        os.environ.copy(),
    )


def _localcat_app_is_running() -> bool:
    """Return whether LaunchServices currently owns the LocalCAT bundle id."""

    osascript = Path("/usr/bin/osascript")
    if not osascript.is_file():
        raise RuntimeError("macOS application-state inspection is unavailable")
    script = (
        "ObjC.import('AppKit'); "
        "$.NSRunningApplication."
        "runningApplicationsWithBundleIdentifier('app.localcat.desktop')."
        "count > 0"
    )
    completed = subprocess.run(
        [str(osascript), "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to inspect running LocalCAT applications")
    answer = completed.stdout.strip()
    if answer == "true":
        return True
    if answer == "false":
        return False
    raise RuntimeError("running LocalCAT application state is invalid")


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


def _loaded_windows_module_path(module_name: str) -> Path:
    """Return the live Windows loader path for one already-loaded DLL."""

    if os.name != "nt":
        raise RuntimeError("Windows module inspection is unavailable")
    if type(module_name) is not str or not module_name:
        raise TypeError("Windows module name must be a non-empty exact string")

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleFileNameW.argtypes = (
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    kernel32.GetModuleFileNameW.restype = wintypes.DWORD

    module = kernel32.GetModuleHandleW(module_name)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    capacity = 32_768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(kernel32.GetModuleFileNameW(module, buffer, capacity))
    if length == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= capacity:
        raise RuntimeError("loaded Windows module path is too long")
    return Path(buffer.value).resolve()


def _write_source_launch_smoke_marker(
    path: Path,
    *,
    app: object,
    window: object,
) -> None:
    """Publish observable source-entry facts after one visible native window."""

    import json

    from PySide6.QtCore import QLibraryInfo

    from windows_source_guardian import DEVELOPER_OVERRIDE_ENVIRONMENT

    marker = path.expanduser()
    if not marker.is_absolute() or marker.exists() or not marker.parent.is_dir():
        raise ValueError("source launch smoke marker path is invalid")
    qt_plugins_path = Path(
        QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    ).resolve()
    qwindows_module_path = _loaded_windows_module_path("qwindows.dll")
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "application_name": app.applicationName(),
                    "application_version": app.applicationVersion(),
                    "application_icon_present": not app.windowIcon().isNull(),
                    "cwd": str(Path.cwd()),
                    "developer_overrides_present": sorted(
                        name
                        for name in DEVELOPER_OVERRIDE_ENVIRONMENT
                        if name in os.environ
                    ),
                    "pid": os.getpid(),
                    "platform_name": app.platformName(),
                    "python_executable": str(Path(sys.executable).resolve()),
                    "qt_plugins_path": str(qt_plugins_path),
                    "qwindows_module_path": str(qwindows_module_path),
                    "version": 2,
                    "window_handle": int(window.winId()),
                    "window_icon_present": not window.windowIcon().isNull(),
                    "window_title": window.windowTitle(),
                    "window_visible": window.isVisible(),
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


def _request_fuzzy_revalidation(composition: object):
    """Start the explicit real Gate D run and return its safe lifecycle."""

    from datetime import datetime, timezone

    from capability_host import CapabilityHostComposition

    if type(composition) is not CapabilityHostComposition:
        raise TypeError("Fuzzy revalidation requires one host composition")
    owner = composition.retrieval_gate_d_owner
    if owner is None:
        raise RuntimeError("Fuzzy revalidation owner is unavailable")
    _ = owner.start_gate_d(
        evaluated_at_utc=datetime.now(timezone.utc).replace(microsecond=0)
    )
    return _fuzzy_validation_display(composition)


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
            _ = gate_d.restore_gate_d(
                evaluated_at_utc=generated_at_utc
            )
        except BaseException:
            if gate_c_changed:
                notify_capability_change()
            raise
        notify_capability_change()

    worker = Thread(
        target=validate,
        name="LocalCAT-capability-validation",
        daemon=True,
    )
    setattr(worker, "_localcat_capability_completion_bridge", bridge)
    worker.start()
    return worker


def _compose_editor_controller(
    repository: object,
    *,
    source_authority: object | None = None,
):
    """Build the one formal TM composition graph owned by this app run."""

    if source_authority is None:
        # Legacy in-process callers still use the production platform factory;
        # only main() owns the stricter bootstrap-before-business-import route.
        from platform_source_authority import compose_rooted_source_authority

        source_authority = compose_rooted_source_authority(
            Path(__file__).absolute().parent
        )

    from datetime import datetime, timezone

    from capability_host import compose_capability_host
    from editor_controller import compose_project_enabled_editor_controller
    from editor_tm_adapter import EditorTMAdapter
    from platform_source_authority import RootedSourceAuthority
    from resource_repository import ResourceRepository
    from tm_application_composition import TMResourceResolver, TMRuntimeHost

    if type(repository) is not ResourceRepository:
        raise TypeError("editor composition requires ResourceRepository")
    if type(source_authority) is not RootedSourceAuthority:
        raise TypeError(
            "editor composition requires one rooted source authority"
        )
    startup_trace = _StartupTrace("capability_composition")
    try:
        capability_composition = compose_capability_host(
            source_authority=source_authority,
            evaluated_at_utc=datetime.now(timezone.utc),
            gate_d_attestation_root=(
                repository.config_dir / "gate-d-qualification"
            ),
        )
        startup_trace.finish("tm_runtime_resolution")
        runtime_host = TMRuntimeHost(
            resolver=TMResourceResolver(),
            configs=repository.list_resources(),
        )
        snapshot = runtime_host.snapshot()
        try:
            _startup_diagnostic({
                "stage": "tm_resource_availability",
                "status": (
                    "complete"
                    if len(snapshot.legacy_ports) + len(snapshot.canonical_ports)
                    == len(snapshot.statuses)
                    else "partial"
                ),
            })
        except Exception:
            pass
        startup_trace.finish("controller_resource_composition")
        controller = compose_project_enabled_editor_controller(
            repository,
            tm_adapter=EditorTMAdapter(
                runtime_host=runtime_host,
                capability_host=capability_composition.host,
                fuzzy_validation_status=lambda: _fuzzy_validation_display(
                    capability_composition
                ),
                fuzzy_validation_start=lambda: _request_fuzzy_revalidation(
                    capability_composition
                ),
            ),
        )
    except Exception as exc:
        startup_trace.finish(error=exc)
        raise
    startup_trace.finish()
    return controller, capability_composition


def _compose_chunk_controller(controller: object, repository: object):
    """Build the device-local Chunk application boundary for one app run."""

    from chunk_controller_adapter import (
        ChunkControllerAdapter,
        create_chunk_metadata_binding_resolver,
    )
    from collaborative_chunks import LocalReferenceActorPort
    from editor_controller import EditorController
    from platform_fs import compose_platform_file_backend
    from resource_repository import ResourceRepository

    if type(controller) is not EditorController:
        raise TypeError("chunk composition requires one EditorController")
    if type(repository) is not ResourceRepository:
        raise TypeError("chunk composition requires one ResourceRepository")
    metadata_root = (repository.config_dir / "collaborative-chunks").resolve()
    metadata_root.mkdir(parents=True, exist_ok=True)
    platform_backend = compose_platform_file_backend(metadata_root)
    actor_port = LocalReferenceActorPort(
        "localcat-local-reference",
        "device-workflow",
    )

    return ChunkControllerAdapter(
        controller,
        actor_port,
        actor_port.current_actor(),
        metadata_binding_resolver=create_chunk_metadata_binding_resolver(
            metadata_root
        ),
        platform_backend=platform_backend,
    )


def _compose_tmx_export_service(
    controller: object,
    repository: object,
    chunk_controller: object,
):
    """Connect exact owner projections to the TMX application boundary."""

    from chunk_controller_adapter import ChunkControllerAdapter
    from editor_controller import EditorController
    from resource_repository import ResourceRepository
    from tmx_application import TmxExportApplicationService

    if type(controller) is not EditorController:
        raise TypeError("TMX composition requires one EditorController")
    if type(repository) is not ResourceRepository:
        raise TypeError("TMX composition requires one ResourceRepository")
    if type(chunk_controller) is not ChunkControllerAdapter:
        raise TypeError("TMX composition requires one ChunkControllerAdapter")
    return TmxExportApplicationService(
        controller,
        repository,
        chunk_controller=chunk_controller,
    )


def _run_empty_home_startup(
    repository: object,
    source_authority: object,
    qt_resources: object,
    startup_trace: _StartupTrace,
) -> int:
    """Show the empty home before loading the global language resources."""

    from typing import cast

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from capability_host import CapabilityHostComposition
    from chunk_controller_adapter import ChunkControllerAdapter
    from editor_controller import EditorController
    from qt_editor_window import QtEditorWindow
    from qt_resource_contracts import SourceQtResources
    from qt_speaker_avatar import SpeakerAvatarCatalog, resource_png_pixmap
    from qt_startup_loader import QtStartupLoader
    from qt_startup_window import QtStartupWindow
    from tmx_application import TmxExportApplicationService

    resources = cast(SourceQtResources, qt_resources)

    QApplication.setApplicationName("LocalCAT")
    QApplication.setApplicationDisplayName("LocalCAT")
    QApplication.setOrganizationName("LocalCAT")
    QApplication.setApplicationVersion(APPLICATION_VERSION)
    app = cast(QApplication, QApplication.instance() or QApplication([sys.argv[0]]))
    app.setDesktopFileName("localcat")
    _stabilize_windows_tooltips(app)
    logo_pixmap = resource_png_pixmap(resources.logo)
    if logo_pixmap is None:
        raise ValueError("source Qt logo is not a valid PNG")
    app.setWindowIcon(QIcon(logo_pixmap))
    home = QtStartupWindow()
    home.show()
    app.processEvents()
    startup_trace.finish("home_first_events")
    startup_trace.finish()
    if home.closed:
        return 0
    # Keep the complete published graph and Qt bridges alive for app.exec().
    owners: list[object] = []

    def build() -> object:
        trace = _StartupTrace("background_resource_preload")
        try:
            controller, capabilities = _compose_editor_controller(
                repository, source_authority=source_authority
            )
            chunks = _compose_chunk_controller(controller, repository)
            exports = _compose_tmx_export_service(controller, repository, chunks)
        except Exception as error:
            trace.finish(error=error)
            raise
        trace.finish()
        return controller, capabilities, chunks, exports

    def publish(result: object) -> None:
        if home.closed:
            return
        controller, capabilities, chunks, exports = cast(
            tuple[EditorController, CapabilityHostComposition,
                  ChunkControllerAdapter, TmxExportApplicationService], result,
        )
        trace = _StartupTrace("ready_editor_window")
        window = None
        try:
            window = QtEditorWindow(
                controller,
                chunk_controller=chunks,
                speaker_avatar_catalog=SpeakerAvatarCatalog(resources.speaker_avatars),
            )
            window.tmx_export_coordinator = exports
            window.setGeometry(home.geometry())
            window.setWindowState(home.windowState())
            validation_worker = _start_capability_validation(capabilities, window)
            window.show()
        except Exception as error:
            if window is not None:
                window.close()
            trace.finish(error=error)
            raise
        owners.extend((controller, capabilities, chunks, exports, window, validation_worker))
        # Show the ready editor before closing the home: otherwise Qt's
        # lastWindowClosed could exit the event loop during this handoff.
        home.close()
        trace.finish("resources_ready")
        app.processEvents()
        trace.finish()

    def failed(error: Exception) -> None:
        if not home.closed:
            _StartupTrace("background_preload_failed").finish(error=error)
            home.set_loading_failed()

    loader = QtStartupLoader(build, publish, failed, parent=home)

    def retry() -> None:
        if not loader.running:
            home.set_loading()
            loader.start()

    home.retry_requested.connect(retry)
    app.aboutToQuit.connect(loader.stop_publication)
    loader.start()
    try:
        return app.exec()
    finally:
        loader.stop_publication()


def main(argv: list[str] | None = None) -> int:
    launch_argv = tuple(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(launch_argv)
    if args.source_launch_smoke_marker is not None and not args.smoke_test:
        print(
            f"LocalCAT Qt editor could not start [{STARTUP_FAILURE_CODE}].",
            file=sys.stderr,
        )
        return 1
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
    if args.install_windows_launcher:
        try:
            from windows_source_launcher import install_windows_source_launcher

            report = install_windows_source_launcher()
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Unable to install LocalCAT Windows launcher: {exc}", file=sys.stderr)
            return 1
        print(f"Installed LocalCAT Windows launcher: {report.shortcut}")
        return 0
    if not args.smoke_test and args.bundle_smoke_marker is None:
        _handoff_to_macos_native_launcher(launch_argv)
    startup_trace = _StartupTrace("qt_imports")
    try:
        from typing import cast

        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ModuleNotFoundError as exc:
        startup_trace.finish(error=exc)
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            print(
                "LocalCAT Qt editor requires PySide6.\n"
                f"Install the desktop dependencies with:\n  {INSTALL_HINT}",
                file=sys.stderr,
            )
            return 2
        raise

    startup_trace.finish("source_authority")
    try:
        # The source route establishes one live rooted authority before any
        # LocalCAT business module is imported.  Paths below remain locators;
        # CapabilityHost receives only the retained platform proof.
        from platform_fs import compose_platform_file_backend
        from platform_source_authority import compose_rooted_source_authority

        root = Path(__file__).absolute().parent
        platform_backend = compose_platform_file_backend(root)
        source_authority = compose_rooted_source_authority(
            root,
            backend=platform_backend,
        )
        startup_trace.finish("qt_source_resources")
        from qt_source_resources import resolve_source_qt_resources

        qt_resources = resolve_source_qt_resources(
            source_authority,
            logo_filename=APPLICATION_ICON_FILENAME,
        )
        startup_trace.finish("data_directory")
        data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        data_root = platform_backend.bind_root(data_dir)
        try:
            data_root.reprove()
        finally:
            data_root.close()

        startup_trace.finish("business_imports")
        from qt_editor_window import QtEditorWindow
        from qt_speaker_avatar import SpeakerAvatarCatalog, resource_png_pixmap
        from resource_repository import ResourceRepository

        startup_trace.finish("resource_repository")
        repository = ResourceRepository(
            data_dir,
            default_tm_path=root / "tm.jsonl",
            default_termbase_path=root / "terms.csv",
            backend=platform_backend,
        )
        if (
            sys.platform == "win32"
            and args.project is None
            and not args.sample
            and not args.smoke_test
            and args.bundle_smoke_marker is None
        ):
            startup_trace.finish("startup_home")
            return _run_empty_home_startup(
                repository, source_authority, qt_resources, startup_trace
            )
        startup_trace.finish("editor_composition")
        controller, capability_composition = _compose_editor_controller(
            repository,
            source_authority=source_authority,
        )
        startup_trace.finish("chunk_and_export_composition")
        chunk_controller = _compose_chunk_controller(controller, repository)
        tmx_export_service = _compose_tmx_export_service(
            controller,
            repository,
            chunk_controller,
        )
        # Retain the owner-only validation ports for the complete QApplication
        # lifetime; the Controller receives only the host read boundary.
        _ = capability_composition
        startup_trace.finish("initial_project")
        if args.project is not None:
            if args.project.suffix.lower() == ".localcat-project":
                controller.open_project_package(args.project)
            else:
                controller.open_project(args.project)
        elif args.sample or args.smoke_test:
            controller.load_sample()

        # Set the Qt process identity before constructing QApplication.  On
        # macOS a script launched through the Python interpreter otherwise
        # lets the application menu adopt the interpreter's display name
        # (for example, "Python 3.14") before LocalCAT can replace it.
        startup_trace.finish("application_and_window")
        QApplication.setApplicationName("LocalCAT")
        QApplication.setApplicationDisplayName("LocalCAT")
        QApplication.setOrganizationName("LocalCAT")
        QApplication.setApplicationVersion(APPLICATION_VERSION)
        existing_app = QApplication.instance()
        app = (
            QApplication([sys.argv[0]])
            if existing_app is None
            else cast(QApplication, existing_app)
        )
        app.setApplicationName("LocalCAT")
        app.setApplicationDisplayName("LocalCAT")
        app.setOrganizationName("LocalCAT")
        app.setApplicationVersion(APPLICATION_VERSION)
        app.setDesktopFileName("localcat")
        _stabilize_windows_tooltips(app)
        logo_pixmap = resource_png_pixmap(qt_resources.logo)
        if logo_pixmap is None:
            raise ValueError("source Qt logo is not a valid PNG")
        app.setWindowIcon(QIcon(logo_pixmap))
        window = QtEditorWindow(
            controller,
            chunk_controller=chunk_controller,
            speaker_avatar_catalog=SpeakerAvatarCatalog(
                qt_resources.speaker_avatars
            ),
        )
        # Keep the long-standing window construction seam compatible with
        # bootstrap probes while still installing the run-owned TMX service
        # before the window is shown or any project menu can open.
        window.tmx_export_coordinator = tmx_export_service
        window.show()
        startup_trace.finish("first_events")
        validation_worker = _start_capability_validation(
            capability_composition,
            window,
        )
        # Retain both the daemon and its Qt signal bridge for the application
        # lifetime. The worker only emits; Qt invokes the window on its thread.
        _ = validation_worker
        app.processEvents()
        startup_trace.finish()
        if args.bundle_smoke_marker is not None:
            _write_bundle_smoke_marker(
                args.bundle_smoke_marker.expanduser().resolve(),
                application_name=app.applicationName(),
                window_title=window.windowTitle(),
            )
        if args.source_launch_smoke_marker is not None:
            _write_source_launch_smoke_marker(
                args.source_launch_smoke_marker.expanduser().resolve(),
                app=app,
                window=window,
            )

        if args.smoke_test:
            if (
                not controller.has_active_project
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
        startup_trace.finish(error=exc)
        print(
            f"LocalCAT Qt editor could not start [{STARTUP_FAILURE_CODE}].",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
