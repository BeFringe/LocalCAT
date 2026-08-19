"""Build and validate the lightweight user-local LocalCAT macOS bundle."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Final, final


LOCALCAT_BUNDLE_IDENTIFIER: Final = "app.localcat.desktop"
LOCALCAT_BUNDLE_NAME: Final = "LocalCAT.app"
LOCALCAT_EXECUTABLE_NAME: Final = "LocalCAT"
LOCALCAT_ICON_FILENAME: Final = "LocalCAT.icns"
LOCALCAT_LAUNCHER_TEMPLATE_FILENAME: Final = "LocalCAT-launcher"
LOCALCAT_SMOKE_MARKER_VERSION: Final = 1

_RENAME_SWAP: Final = 0x00000002


@final
@dataclass(frozen=True, slots=True)
class MacOSBundleReport:
    """Validated identity and launch facts for one bundle."""

    bundle: Path
    display_name: str
    bundle_identifier: str
    executable_name: str
    icon_present: bool
    cold_launch_passed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, Path) or not self.bundle.is_absolute():
            raise ValueError("bundle report requires one absolute path")
        for value in (
            self.display_name,
            self.bundle_identifier,
            self.executable_name,
        ):
            if type(value) is not str or not value:
                raise ValueError("bundle report identity must be non-empty text")
        if type(self.icon_present) is not bool:
            raise ValueError("bundle icon state must be exact bool")
        if type(self.cold_launch_passed) is not bool:
            raise ValueError("bundle launch state must be exact bool")


def _require_absolute_regular_file(
    path: Path,
    *,
    label: str,
    executable: bool = False,
) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if executable and not metadata.st_mode & 0o111:
        raise ValueError(f"{label} must be executable")
    return path


def _require_bundle_directory(bundle: Path) -> Path:
    if not isinstance(bundle, Path) or not bundle.is_absolute():
        raise ValueError("bundle must be an absolute path")
    if bundle.suffix != ".app" or bundle.is_symlink():
        raise ValueError("bundle must be one regular .app directory")
    if not bundle.is_dir():
        raise ValueError("bundle directory is unavailable")
    return bundle


def _swap_directories(candidate: Path, target: Path) -> None:
    """Atomically exchange two Darwin directories without a missing-target gap."""

    if sys.platform != "darwin":
        raise RuntimeError("LocalCAT.app atomic exchange requires macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise OSError("Darwin atomic directory exchange is unavailable")
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(candidate),
        os.fsencode(target),
        _RENAME_SWAP,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


@final
class MacOSAppLauncher:
    """Create one cwd-independent bundle around the approved Python checkout."""

    def __init__(self, *, icon_path: Path) -> None:
        self.__icon_path = _require_absolute_regular_file(
            icon_path,
            label="LocalCAT icon",
        )
        if self.__icon_path.suffix.lower() != ".icns":
            raise ValueError("LocalCAT icon must use the .icns format")
        self.__launcher_template = _require_absolute_regular_file(
            (Path(__file__).resolve().parent / LOCALCAT_LAUNCHER_TEMPLATE_FILENAME),
            label="LocalCAT native launcher",
            executable=True,
        )
        if self.__launcher_template.read_bytes()[:4] != b"\xca\xfe\xba\xbe":
            raise ValueError("LocalCAT native launcher must be one universal Mach-O")

    def build_bundle(
        self,
        target: Path,
        python: Path,
        bootstrap: Path,
    ) -> Path:
        """Build, cold-launch, then atomically publish one LocalCAT.app."""

        if sys.platform != "darwin":
            raise RuntimeError("LocalCAT.app installation requires macOS")
        if not isinstance(target, Path) or not target.is_absolute():
            raise ValueError("LocalCAT.app target must be absolute")
        if target.name != LOCALCAT_BUNDLE_NAME or target.is_symlink():
            raise ValueError("target must be named LocalCAT.app")
        python_path = _require_absolute_regular_file(
            python,
            label="Python executable",
            executable=True,
        )
        bootstrap_path = _require_absolute_regular_file(
            bootstrap,
            label="LocalCAT bootstrap",
        )
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError("LocalCAT.app parent directory is unavailable")
        if target.exists() and not target.is_dir():
            raise ValueError("existing LocalCAT.app target is not a directory")

        candidate = Path(
            tempfile.mkdtemp(
                prefix=".LocalCAT-candidate-",
                suffix=".app",
                dir=parent,
            )
        ).resolve()
        try:
            self.__populate_candidate(
                candidate,
                python=python_path,
                bootstrap=bootstrap_path,
            )
            report = self.validate_bundle(candidate)
            if not report.cold_launch_passed:
                raise ValueError("LocalCAT.app cold launch did not complete")
            if target.exists():
                _swap_directories(candidate, target)
                try:
                    shutil.rmtree(candidate)
                except OSError:
                    # The new target is already atomically installed. A stale
                    # hidden old bundle is safer than reversing publication.
                    pass
            else:
                os.replace(candidate, target)
            return target
        finally:
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)

    def validate_bundle(self, bundle: Path) -> MacOSBundleReport:
        """Validate identity, static bootstrap binding, and one real cold launch."""

        candidate = _require_bundle_directory(bundle)
        contents = candidate / "Contents"
        plist_path = _require_absolute_regular_file(
            contents / "Info.plist",
            label="bundle Info.plist",
        )
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)
        if type(info) is not dict:
            raise ValueError("bundle Info.plist must be one dictionary")
        expected = {
            "CFBundleName": "LocalCAT",
            "CFBundleDisplayName": "LocalCAT",
            "CFBundleExecutable": LOCALCAT_EXECUTABLE_NAME,
            "CFBundleIdentifier": LOCALCAT_BUNDLE_IDENTIFIER,
            "CFBundlePackageType": "APPL",
            "CFBundleIconFile": LOCALCAT_ICON_FILENAME,
        }
        for key, value in expected.items():
            if info.get(key) != value:
                raise ValueError(f"bundle metadata is invalid: {key}")

        executable = _require_absolute_regular_file(
            contents / "MacOS" / LOCALCAT_EXECUTABLE_NAME,
            label="bundle executable",
            executable=True,
        )
        icon = _require_absolute_regular_file(
            contents / "Resources" / LOCALCAT_ICON_FILENAME,
            label="bundle icon",
        )
        if icon.stat().st_size == 0 or icon.read_bytes()[:4] != b"icns":
            raise ValueError("bundle icon is not a valid icns container")

        python_value = info.get("LocalCATPythonExecutable")
        bootstrap_value = info.get("LocalCATBootstrapPath")
        if type(python_value) is not str or type(bootstrap_value) is not str:
            raise ValueError("bundle bootstrap binding is missing")
        python_path = _require_absolute_regular_file(
            Path(python_value),
            label="configured Python executable",
            executable=True,
        )
        bootstrap_path = _require_absolute_regular_file(
            Path(bootstrap_value),
            label="configured LocalCAT bootstrap",
        )
        if executable.read_bytes() != self.__launcher_template.read_bytes():
            raise ValueError("bundle executable is not the approved native launcher")

        self.__cold_launch(candidate)
        return MacOSBundleReport(
            bundle=candidate,
            display_name="LocalCAT",
            bundle_identifier=LOCALCAT_BUNDLE_IDENTIFIER,
            executable_name=LOCALCAT_EXECUTABLE_NAME,
            icon_present=True,
            cold_launch_passed=True,
        )

    def __populate_candidate(
        self,
        candidate: Path,
        *,
        python: Path,
        bootstrap: Path,
    ) -> None:
        contents = candidate / "Contents"
        macos_dir = contents / "MacOS"
        resources_dir = contents / "Resources"
        macos_dir.mkdir(parents=True)
        resources_dir.mkdir()
        executable = macos_dir / LOCALCAT_EXECUTABLE_NAME
        shutil.copyfile(self.__launcher_template, executable)
        executable.chmod(0o755)
        shutil.copyfile(self.__icon_path, resources_dir / LOCALCAT_ICON_FILENAME)
        info = {
            "CFBundleDevelopmentRegion": "zh_CN",
            "CFBundleDisplayName": "LocalCAT",
            "CFBundleExecutable": LOCALCAT_EXECUTABLE_NAME,
            "CFBundleIconFile": LOCALCAT_ICON_FILENAME,
            "CFBundleIdentifier": LOCALCAT_BUNDLE_IDENTIFIER,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": "LocalCAT",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMultipleInstancesProhibited": True,
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "LocalCATBootstrapPath": str(bootstrap),
            "LocalCATPythonExecutable": str(python),
        }
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(info, stream, fmt=plistlib.FMT_XML, sort_keys=True)

    @staticmethod
    def __cold_launch(bundle: Path) -> None:
        open_command = Path("/usr/bin/open")
        _require_absolute_regular_file(
            open_command,
            label="macOS open command",
            executable=True,
        )
        with tempfile.TemporaryDirectory(
            prefix="localcat-bundle-cold-launch-"
        ) as temporary:
            root = Path(temporary).resolve()
            unrelated = root / "unrelated working directory"
            unrelated.mkdir()
            data_dir = root / "application data"
            marker = root / "cold-launch.marker"
            completed = subprocess.run(
                [
                    str(open_command),
                    "-n",
                    "-W",
                    str(bundle),
                    "--args",
                    "--smoke-test",
                    "--data-dir",
                    str(data_dir),
                    "--bundle-smoke-marker",
                    str(marker),
                ],
                cwd=unrelated,
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
            if completed.returncode != 0:
                raise ValueError("LocalCAT.app could not be opened by LaunchServices")
            try:
                marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("LocalCAT.app did not reach a usable Qt window") from exc
            if type(marker_payload) is not dict or marker_payload != {
                "application_name": "LocalCAT",
                "pid": marker_payload.get("pid"),
                "version": LOCALCAT_SMOKE_MARKER_VERSION,
                "window_title": marker_payload.get("window_title"),
            }:
                raise ValueError("LocalCAT.app cold-launch marker is invalid")
            if (
                type(marker_payload["pid"]) is not int
                or marker_payload["pid"] <= 0
                or type(marker_payload["window_title"]) is not str
                or "LocalCAT" not in marker_payload["window_title"]
            ):
                raise ValueError("LocalCAT.app cold-launch identity is invalid")
