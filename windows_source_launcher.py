"""Install the user-managed Windows LocalCAT source entry.

The installer copies only the stdlib guardian adapter and its icon.  It does
not copy Python, Qt, LocalCAT business source, or product resources, and it
does not grant frozen or packaged-source authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
from typing import Final, final
import uuid


APPLICATION_NAME: Final = "LocalCAT"
APPLICATION_VERSION: Final = "1.0"
SHORTCUT_DESCRIPTION: Final = "LocalCAT user-managed source editor"
SHORTCUT_FILENAME: Final = "LocalCAT Source.lnk"
SHORTCUT_SCRIPT: Final = Path("packaging/windows/source_shortcut.ps1")
GUARDIAN_SOURCE_FILENAME: Final = "windows_source_guardian.py"
SOURCE_BOOTSTRAP_FILENAME: Final = "qt_editor.py"
WINDOWS_ICON_FILENAME: Final = "LocalCAT-logo-silver.ico"

@final
@dataclass(frozen=True, slots=True)
class WindowsSourceShortcutReport:
    """Exact installed shortcut fields used by the source profile."""

    shortcut: Path
    target_path: Path
    arguments: str
    working_directory: Path
    icon_location: str
    description: str


def _source_root() -> Path:
    return Path(__file__).resolve().parent


def _local_application_directory() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    base = Path(value) if value else Path.home() / "AppData" / "Local"
    return (base.expanduser().resolve() / APPLICATION_NAME).resolve()


def _default_shortcut_path() -> Path:
    value = os.environ.get("APPDATA")
    base = Path(value) if value else Path.home() / "AppData" / "Roaming"
    return (
        base.expanduser().resolve()
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / SHORTCUT_FILENAME
    ).resolve()


def _require_absolute_regular_file(path: Path, *, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{label} is unavailable")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} is unavailable")
    return path


def _supported_runtime(*, executable: Path) -> bool:
    current = Path(sys.executable).resolve()
    return (
        os.name == "nt"
        and sys.implementation.name == "cpython"
        and sys.version_info[:2] == (3, 14)
        and struct.calcsize("P") == 8
        and sys.prefix != sys.base_prefix
        and executable.is_absolute()
        and executable.name.casefold() == "pythonw.exe"
        and executable.is_file()
        and executable.parent == current.parent
    )


def _current_venv_pythonw() -> Path:
    _require_absolute_regular_file(
        Path(sys.executable).resolve(),
        label="current CPython executable",
    )
    runtime = Path(sys.executable).resolve().with_name("pythonw.exe")
    _require_absolute_regular_file(runtime, label="CPython GUI executable")
    if not _supported_runtime(executable=runtime):
        raise RuntimeError("Windows source launcher requires a CPython 3.14 x64 venv")
    return runtime


def _runtime_dependency_paths() -> tuple[Path, Path, Path]:
    environment = Path(sys.prefix).resolve()
    discovered: list[Path] = []
    for package in ("PySide6", "openpyxl"):
        specification = importlib.util.find_spec(package)
        origin = None if specification is None else specification.origin
        if type(origin) is not str:
            raise RuntimeError("Windows source launcher dependencies are unavailable")
        path = _require_absolute_regular_file(
            Path(origin).resolve(),
            label="Windows source launcher dependency",
        )
        if not path.is_relative_to(environment):
            raise RuntimeError("Windows source launcher dependency is outside the venv")
        discovered.append(path)
    qwindows = _require_absolute_regular_file(
        (discovered[0].parent / "plugins" / "platforms" / "qwindows.dll").resolve(),
        label="Qt Windows platform plugin",
    )
    if not qwindows.is_relative_to(environment):
        raise RuntimeError("Qt Windows platform plugin is outside the venv")
    return discovered[0], discovered[1], qwindows


def _powershell_path() -> Path:
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return _require_absolute_regular_file(
        (windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve(),
        label="Windows PowerShell",
    )


def _shortcut_script() -> Path:
    return _require_absolute_regular_file(
        (_source_root() / SHORTCUT_SCRIPT).resolve(),
        label="Windows shortcut helper",
    )


def _run_shortcut_helper(mode: str, shortcut: Path, **fields: str) -> dict[str, object]:
    command = [
        str(_powershell_path()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_shortcut_script()),
        "-Mode",
        mode,
        "-ShortcutPath",
        str(shortcut),
    ]
    for name, value in fields.items():
        command.extend((f"-{name}", value))
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Windows shortcut helper failed")
    if mode != "Inspect":
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows shortcut inspection failed") from exc
    if type(payload) is not dict:
        raise RuntimeError("Windows shortcut inspection failed")
    return payload


def inspect_windows_source_shortcut(shortcut: Path) -> WindowsSourceShortcutReport:
    """Read one .lnk through the Windows Shell shortcut implementation."""

    candidate = shortcut.expanduser().resolve()
    _require_absolute_regular_file(candidate, label="Windows shortcut")
    payload = _run_shortcut_helper("Inspect", candidate)
    expected_keys = {
        "arguments",
        "description",
        "icon_location",
        "target_path",
        "working_directory",
    }
    if set(payload) != expected_keys or not all(
        type(payload[key]) is str for key in expected_keys
    ):
        raise RuntimeError("Windows shortcut metadata is invalid")
    return WindowsSourceShortcutReport(
        shortcut=candidate,
        target_path=Path(payload["target_path"]).resolve(),
        arguments=payload["arguments"],
        working_directory=Path(payload["working_directory"]).resolve(),
        icon_location=payload["icon_location"],
        description=payload["description"],
    )


def _icon_is_extractable(icon: Path) -> bool:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32.ExtractIconExW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HICON),
        ctypes.POINTER(wintypes.HICON),
        wintypes.UINT,
    )
    shell32.ExtractIconExW.restype = wintypes.UINT
    user32.DestroyIcon.argtypes = (wintypes.HICON,)
    user32.DestroyIcon.restype = wintypes.BOOL
    large = wintypes.HICON()
    small = wintypes.HICON()
    count = shell32.ExtractIconExW(
        str(icon),
        0,
        ctypes.byref(large),
        ctypes.byref(small),
        1,
    )
    try:
        return count == 1 and bool(large or small)
    finally:
        if large:
            user32.DestroyIcon(large)
        if small:
            user32.DestroyIcon(small)


def _install_guardian_assets(source_root: Path) -> tuple[Path, Path]:
    guardian_source = _require_absolute_regular_file(
        (source_root / GUARDIAN_SOURCE_FILENAME).resolve(),
        label="Windows source guardian",
    )
    icon_source = _require_absolute_regular_file(
        (source_root / WINDOWS_ICON_FILENAME).resolve(),
        label="Windows LocalCAT icon",
    )
    if not _icon_is_extractable(icon_source):
        raise ValueError("Windows LocalCAT icon is invalid")
    target_directory = (_local_application_directory() / "Launcher").resolve()
    target_directory.mkdir(parents=True, exist_ok=True)
    if not target_directory.is_dir() or target_directory.is_symlink():
        raise ValueError("Windows source guardian directory is unavailable")
    targets = (
        target_directory / GUARDIAN_SOURCE_FILENAME,
        target_directory / WINDOWS_ICON_FILENAME,
    )
    for source, target in zip((guardian_source, icon_source), targets, strict=True):
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.candidate"
        )
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    guardian = _require_absolute_regular_file(
        targets[0],
        label="installed Windows source guardian",
    )
    icon = _require_absolute_regular_file(
        targets[1],
        label="installed Windows LocalCAT icon",
    )
    if not _icon_is_extractable(icon):
        raise RuntimeError("installed Windows LocalCAT icon is invalid")
    return guardian, icon


def _install_windows_source_launcher(
    target: Path | None,
    *,
    source_root: Path,
    working_directory: Path | None,
    guardian_arguments: tuple[str, ...] = (),
) -> WindowsSourceShortcutReport:
    if os.name != "nt":
        raise RuntimeError("Windows source launcher installation requires Windows")
    runtime = _current_venv_pythonw()
    _runtime_dependency_paths()
    checkout = source_root.expanduser().resolve()
    _require_absolute_regular_file(
        (checkout / SOURCE_BOOTSTRAP_FILENAME).resolve(),
        label="LocalCAT source bootstrap",
    )
    guardian, icon = _install_guardian_assets(_source_root())
    destination = (
        _default_shortcut_path()
        if target is None
        else target.expanduser().resolve()
    )
    if destination.suffix.casefold() != ".lnk" or destination.is_symlink():
        raise ValueError("Windows launcher target must be one .lnk path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.is_symlink():
        raise ValueError("Windows launcher parent is unavailable")
    cwd = (
        _local_application_directory()
        if working_directory is None
        else working_directory.expanduser().resolve()
    )
    cwd.mkdir(parents=True, exist_ok=True)
    if not cwd.is_dir() or cwd.is_symlink():
        raise ValueError("Windows launcher working directory is unavailable")

    arguments = subprocess.list2cmdline(
        [
            "-I",
            str(guardian),
            "--source-root",
            str(checkout),
            *guardian_arguments,
            "--",
        ]
    )
    icon_location = f"{icon},0"
    temporary = destination.with_name(
        f".{destination.stem}.{os.getpid()}.{uuid.uuid4().hex}.candidate.lnk"
    )
    try:
        _run_shortcut_helper(
            "Create",
            temporary,
            TargetPath=str(runtime),
            Arguments=arguments,
            WorkingDirectory=str(cwd),
            IconLocation=icon_location,
            Description=SHORTCUT_DESCRIPTION,
        )
        report = inspect_windows_source_shortcut(temporary)
        expected = (
            report.target_path == runtime
            and report.arguments == arguments
            and report.working_directory == cwd
            and report.icon_location.casefold() == icon_location.casefold()
            and report.description == SHORTCUT_DESCRIPTION
        )
        if not expected:
            raise RuntimeError("Windows shortcut verification failed")
        os.replace(temporary, destination)
        installed = inspect_windows_source_shortcut(destination)
        if installed != WindowsSourceShortcutReport(
            shortcut=destination,
            target_path=runtime,
            arguments=arguments,
            working_directory=cwd,
            icon_location=icon_location,
            description=SHORTCUT_DESCRIPTION,
        ):
            raise RuntimeError("installed Windows shortcut verification failed")
        return installed
    finally:
        temporary.unlink(missing_ok=True)


def install_windows_source_launcher(
    target: Path | None = None,
    *,
    working_directory: Path | None = None,
) -> WindowsSourceShortcutReport:
    """Install one Start-menu source entry bound to this venv and checkout."""

    return _install_windows_source_launcher(
        target,
        source_root=_source_root(),
        working_directory=working_directory,
    )
