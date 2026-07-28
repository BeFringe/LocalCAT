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
    try:
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
        from editor_controller import EditorController
        from qt_editor_window import QtEditorWindow
        from resource_repository import ResourceRepository

        root = Path(__file__).resolve().parent
        data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
        repository = ResourceRepository(
            data_dir,
            default_tm_path=root / "tm.jsonl",
            default_termbase_path=root / "terms.csv",
        )
        controller = EditorController(repository)
        if args.project is not None:
            controller.open_project(args.project)
        elif args.sample or args.smoke_test:
            controller.load_sample()

        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setApplicationName("LocalCAT")
        app.setOrganizationName("LocalCAT")
        app.setDesktopFileName("localcat")
        logo_path = application_icon_path(root)
        if logo_path.is_file():
            app.setWindowIcon(QIcon(str(logo_path)))
        window = QtEditorWindow(controller)
        window.show()
        app.processEvents()

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
