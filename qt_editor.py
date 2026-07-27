#!/usr/bin/env python3
"""Stdlib bootstrap for the optional LocalCAT PySide6 desktop editor."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


INSTALL_HINT = "python -m pip install -r requirements-ui.txt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the LocalCAT desktop editor.")
    project = parser.add_mutually_exclusive_group()
    project.add_argument("--project", type=Path, help="Open a JSON or TXT translation project.")
    project.add_argument("--sample", action="store_true", help="Open the bundled sample project.")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
