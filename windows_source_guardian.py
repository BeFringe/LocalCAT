"""User-local stdlib guardian for the Windows LocalCAT source profile.

The installed copy validates one user-managed venv and checkout, starts a
separate child with the same absolute pythonw.exe, waits for it, and projects
only stable failure codes.  It never copies or vouches for business source.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import time
from typing import Final


APPLICATION_NAME: Final = "LocalCAT"
SOURCE_BOOTSTRAP_FILENAME: Final = "qt_editor.py"

CONFIG_INVALID: Final = "LOCALCAT.WINDOWS.SOURCE.CONFIG_INVALID"
RUNTIME_UNSUPPORTED: Final = "LOCALCAT.WINDOWS.SOURCE.RUNTIME_UNSUPPORTED"
SOURCE_UNAVAILABLE: Final = "LOCALCAT.WINDOWS.SOURCE.SOURCE_UNAVAILABLE"
DEPENDENCIES_UNAVAILABLE: Final = "LOCALCAT.WINDOWS.SOURCE.DEPENDENCIES_UNAVAILABLE"
WORKDIR_UNAVAILABLE: Final = "LOCALCAT.WINDOWS.SOURCE.WORKDIR_UNAVAILABLE"
CHILD_START_FAILED: Final = "LOCALCAT.WINDOWS.SOURCE.CHILD_START_FAILED"
CHILD_FAILED: Final = "LOCALCAT.WINDOWS.SOURCE.CHILD_FAILED"
CHILD_ABORTED: Final = "LOCALCAT.WINDOWS.SOURCE.CHILD_ABORTED"

EXIT_BY_CODE: Final = {
    CONFIG_INVALID: 10,
    RUNTIME_UNSUPPORTED: 11,
    SOURCE_UNAVAILABLE: 12,
    DEPENDENCIES_UNAVAILABLE: 13,
    WORKDIR_UNAVAILABLE: 14,
    CHILD_START_FAILED: 15,
    CHILD_FAILED: 16,
    CHILD_ABORTED: 17,
}

DEVELOPER_OVERRIDE_ENVIRONMENT: Final = (
    "PYTHONBREAKPOINT",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONWARNINGS",
    "PYSIDE_DESIGNER_PLUGINS",
    "QML2_IMPORT_PATH",
    "QML_IMPORT_PATH",
    "QT_DEBUG_PLUGINS",
    "QT_LOGGING_RULES",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)


def _local_application_directory() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    base = Path(value) if value else Path.home() / "AppData" / "Local"
    return (base.expanduser().resolve() / APPLICATION_NAME).resolve()


def _require_regular_file(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("file is unavailable")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("file is unavailable")
    return path


def _current_venv_pythonw() -> Path:
    current = _require_regular_file(Path(sys.executable).resolve())
    supported = (
        os.name == "nt"
        and sys.implementation.name == "cpython"
        and sys.version_info[:2] == (3, 14)
        and struct.calcsize("P") == 8
        and sys.prefix != sys.base_prefix
        and current.name.casefold() == "pythonw.exe"
    )
    if not supported:
        raise RuntimeError("runtime is unsupported")
    return current


def _validate_dependencies() -> None:
    environment = Path(sys.prefix).resolve()
    origins: list[Path] = []
    for package in ("PySide6", "openpyxl"):
        specification = importlib.util.find_spec(package)
        origin = None if specification is None else specification.origin
        if type(origin) is not str:
            raise RuntimeError("dependency is unavailable")
        path = _require_regular_file(Path(origin).resolve())
        if not path.is_relative_to(environment):
            raise RuntimeError("dependency is outside the venv")
        origins.append(path)
    qwindows = _require_regular_file(
        (origins[0].parent / "plugins" / "platforms" / "qwindows.dll").resolve()
    )
    if not qwindows.is_relative_to(environment):
        raise RuntimeError("qwindows is outside the venv")


def _append_diagnostic(code: str) -> None:
    try:
        directory = _local_application_directory()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "source-launcher.log").open(
            "a",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            stream.write(f"{timestamp} {code}\n")
    except OSError:
        pass


def _append_startup_diagnostic(event: dict[str, object]) -> None:
    """Persist only bounded metadata; never exception text, arguments or paths."""

    safe: dict[str, object] = {}
    for key in ("stage", "status", "exception_type", "failure_module"):
        value = event.get(key)
        if type(value) is str and value and len(value) <= 80:
            if all(character.isascii() and (character.isalnum() or character == "_") for character in value):
                safe[key] = value
    for key in ("elapsed_ms", "returncode", "errno", "winerror", "failure_line"):
        value = event.get(key)
        if type(value) is int:
            safe[key] = value
    safe["pid"] = os.getpid()
    safe["timestamp"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        directory = _local_application_directory()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "source-startup.log"
        if path.exists() and path.stat().st_size >= 256 * 1024:
            os.replace(path, directory / "source-startup.previous.log")
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(safe, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _show_failure_dialog(code: str) -> None:
    text = (
        f"LocalCAT could not start [{code}].\n\n"
        "Reinstall LocalCAT Source from an available checkout."
    )
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    )
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(None, text, APPLICATION_NAME, 0x00000010)


def _fail(code: str, *, show_dialog: bool) -> int:
    _append_diagnostic(code)
    if show_dialog:
        _show_failure_dialog(code)
    return EXIT_BY_CODE[code]


def _parse(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--launcher-no-dialog", action="store_true")
    parser.add_argument("application_arguments", nargs=argparse.REMAINDER)
    try:
        known = parser.parse_args(argv)
    except SystemExit:
        return None
    application_arguments = list(known.application_arguments)
    if application_arguments[:1] == ["--"]:
        application_arguments = application_arguments[1:]
    return (
        known.source_root.expanduser().resolve(),
        known.child,
        not known.launcher_no_dialog,
        tuple(application_arguments),
    )


def _clear_developer_overrides() -> None:
    for name in DEVELOPER_OVERRIDE_ENVIRONMENT:
        os.environ.pop(name, None)


def _run_child(source_root: Path, application_arguments: tuple[str, ...]) -> int:
    started = time.perf_counter()
    try:
        bootstrap = _require_regular_file(
            (source_root / SOURCE_BOOTSTRAP_FILENAME).resolve()
        )
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        specification = importlib.util.spec_from_file_location("qt_editor", bootstrap)
        if specification is None or specification.loader is None:
            raise ImportError("source bootstrap loader is unavailable")
        module = importlib.util.module_from_spec(specification)
        sys.modules["qt_editor"] = module
        specification.loader.exec_module(module)
        module._startup_diagnostic = _append_startup_diagnostic
        _append_startup_diagnostic({
            "stage": "bootstrap_import", "status": "ok",
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        })
        result = module.main(list(application_arguments))
    except Exception as exc:
        _append_startup_diagnostic({
            "stage": "child_execution", "status": "failed",
            "exception_type": type(exc).__name__,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        })
        return 120
    return result if type(result) is int else 120


def main(argv: list[str] | None = None) -> int:
    parsed = _parse(list(sys.argv[1:] if argv is None else argv))
    if parsed is None:
        return _fail(CONFIG_INVALID, show_dialog=True)
    source_root, child, show_dialog, application_arguments = parsed
    try:
        runtime = _current_venv_pythonw()
    except (OSError, RuntimeError, ValueError):
        return _fail(RUNTIME_UNSUPPORTED, show_dialog=show_dialog)
    _clear_developer_overrides()
    try:
        _validate_dependencies()
    except (OSError, RuntimeError, ValueError):
        return _fail(DEPENDENCIES_UNAVAILABLE, show_dialog=show_dialog)
    try:
        _require_regular_file((source_root / SOURCE_BOOTSTRAP_FILENAME).resolve())
    except (OSError, ValueError):
        return _fail(SOURCE_UNAVAILABLE, show_dialog=show_dialog)
    try:
        working_directory = _local_application_directory()
        working_directory.mkdir(parents=True, exist_ok=True)
        if not working_directory.is_dir() or working_directory.is_symlink():
            raise OSError("working directory is unavailable")
        os.chdir(working_directory)
    except OSError:
        return _fail(WORKDIR_UNAVAILABLE, show_dialog=show_dialog)
    if child:
        return _run_child(source_root, application_arguments)

    command = [
        str(runtime),
        "-I",
        str(Path(__file__).resolve()),
        "--child",
        "--launcher-no-dialog",
        "--source-root",
        str(source_root),
        "--",
        *application_arguments,
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return _fail(CHILD_START_FAILED, show_dialog=show_dialog)
    _append_startup_diagnostic({
        "stage": "child_exit",
        "status": "ok" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    })
    if completed.returncode == 0:
        return 0
    if completed.returncode < 0 or completed.returncode >= 0xC0000000:
        return _fail(CHILD_ABORTED, show_dialog=show_dialog)
    return _fail(CHILD_FAILED, show_dialog=show_dialog)


if __name__ == "__main__":
    raise SystemExit(main())
