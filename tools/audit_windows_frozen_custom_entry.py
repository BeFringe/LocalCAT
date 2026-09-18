#!/usr/bin/env python3
"""Produce the Task 1.6 custom-entry NO-GO evidence without building a candidate.

This producer answers one narrow question: does the materialized CPython input
obey the exact pre-authority dynamic-loader target approved in Task 1.5?  It is
not a substitute for the fourteen candidate assertions and cannot emit PASS for
any assertion that needs the customized PE.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any

pefile: Any = None


NO_GO_SCHEMA = "localcat.windows-frozen-custom-entry-no-go.v1"
MATRIX_SCHEMA = "localcat.windows-frozen-custom-entry-matrix.v1"
BLOCKING_ASSERTION = "W3.CUSTOM.DYNAMIC_ROOTS_DECLARED"
SOURCE_SCAN_SCOPE = (
    "bootloader/src/**/*.c",
    "bootloader/src/**/*.h",
    "PyInstaller/loader/**/*.py",
)
SCOPED_SOURCE_PATHS = (
    "packaging/windows/frozen-entry/README.md",
    "packaging/windows/frozen-entry/candidate-input.lock.json",
    "packaging/windows/frozen-entry/custom-entry-matrix.json",
    "packaging/windows/frozen-entry/custom-entry-no-go.schema.json",
    "tools/audit_windows_frozen_custom_entry.py",
    "tools/probe_windows_python314_preauthority.c",
    "tools/validate_windows_frozen_custom_entry_no_go.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def directory_input_fact(root: Path, role: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    digest.update(b"localcat.directory-input-sha256.v1\n")
    count = 0
    total_bytes = 0
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        item_digest = sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item_digest.encode("ascii"))
        digest.update(b"\n")
        count += 1
        total_bytes += size
    return {
        "role": role,
        "algorithm": "localcat.directory-input-sha256.v1",
        "file_count": count,
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def verified_evidence_producer(candidate_lock: dict[str, Any]) -> dict[str, Any]:
    global pefile
    expected = candidate_lock["evidence_producer"]
    python_path = Path(sys.executable).resolve(strict=True)
    base_root = Path(sys.base_prefix).resolve(strict=True)
    module_spec = importlib.util.find_spec("pefile")
    if module_spec is None or module_spec.origin is None:
        raise SystemExit("pefile source cannot be resolved before import")
    module_path = Path(module_spec.origin).resolve(strict=True)
    distribution = importlib.metadata.distribution("pefile")
    metadata_root = getattr(distribution, "_path", None)
    if metadata_root is None:
        raise SystemExit("pefile METADATA path cannot be resolved")
    metadata_path = (Path(metadata_root) / "METADATA").resolve(strict=True)
    base_fact = directory_input_fact(base_root, expected["base_runtime"]["role"])
    config_text = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    include_system = any(
        line.split("=", 1)[1].strip().casefold() == "true"
        for line in config_text.splitlines()
        if line.casefold().startswith("include-system-site-packages") and "=" in line
    )
    venv = {"is_venv": sys.prefix != sys.base_prefix, "include_system_site_packages": include_system}
    if sha256_file(python_path) != expected["python"]["sha256"]:
        raise SystemExit("executing Python differs from candidate evidence producer")
    if platform.python_version() != expected["python_version"] or base_fact != expected["base_runtime"]:
        raise SystemExit("executing Python base runtime differs from candidate evidence producer")
    if venv != expected["venv"]:
        raise SystemExit("executing Python venv profile differs from candidate evidence producer")
    if importlib.metadata.version("pefile") != expected["pefile"]["version"]:
        raise SystemExit("pefile version differs from candidate evidence producer")
    if sha256_file(module_path) != expected["pefile"]["source"]["sha256"]:
        raise SystemExit("pefile source differs from candidate evidence producer")
    if sha256_file(metadata_path) != expected["pefile"]["distribution_metadata"]["sha256"]:
        raise SystemExit("pefile METADATA differs from candidate evidence producer")
    pefile = importlib.import_module("pefile")
    if Path(pefile.__file__).resolve(strict=True) != module_path:
        raise SystemExit("loaded pefile module path changed after verification")
    return {
        "python": {"path": str(python_path), "sha256": sha256_file(python_path), "version": platform.python_version()},
        "base_runtime": {"path": str(base_root), **base_fact},
        "venv": venv,
        "pefile": {
            "version": importlib.metadata.version("pefile"),
            "module_path": str(module_path),
            "module_sha256": sha256_file(module_path),
            "metadata_path": str(metadata_path),
            "metadata_sha256": sha256_file(metadata_path),
        },
    }


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_json_object(path: Path, role: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            validated_file(path, role).read_bytes().decode("utf-8", "strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"invalid {role}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{role} must be a JSON object")
    return value


class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("link_count", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class RtlOsVersionInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.ULONG),
        ("major", wintypes.ULONG),
        ("minor", wintypes.ULONG),
        ("build", wintypes.ULONG),
        ("platform", wintypes.ULONG),
        ("service_pack", wintypes.WCHAR * 128),
    ]


def windows_build() -> dict[str, int]:
    value = RtlOsVersionInfo()
    value.size = ctypes.sizeof(value)
    if ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(value)) != 0:
        raise SystemExit("RtlGetVersion failed")
    return {"major": value.major, "minor": value.minor, "build": value.build}


def file_version(path: Path) -> str | None:
    pe = pefile.PE(str(path), fast_load=False)
    try:
        if not getattr(pe, "VS_FIXEDFILEINFO", None):
            return None
        fixed = pe.VS_FIXEDFILEINFO[0]
        return ".".join(str(part) for part in (
            fixed.FileVersionMS >> 16,
            fixed.FileVersionMS & 0xFFFF,
            fixed.FileVersionLS >> 16,
            fixed.FileVersionLS & 0xFFFF,
        ))
    finally:
        pe.close()


def file_identity(path: Path) -> dict[str, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0x02000000 | 0x80, None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise SystemExit(f"CreateFileW failed for {path}: {ctypes.get_last_error()}")
    try:
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise SystemExit(f"GetFileInformationByHandle failed for {path}: {ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(handle)
    return {
        "volume": f"{information.volume_serial:08X}",
        "file_id": f"{information.file_index_high:08X}{information.file_index_low:08X}",
        "size": f"{information.file_size_high:08X}{information.file_size_low:08X}",
    }


def binary_fact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve(strict=True)),
        "identity": file_identity(path),
        "sha256": sha256_file(path),
        "file_version": file_version(path),
    }


def scoped_source_identity(repository_root: Path) -> dict[str, Any]:
    files = []
    for relative in SCOPED_SOURCE_PATHS:
        path = validated_file(repository_root / relative, f"scoped source {relative}")
        files.append({"path": relative, "sha256": sha256_file(path)})
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", *SCOPED_SOURCE_PATHS],
        cwd=repository_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode("utf-8", "strict")
    state = {line[3:]: line[:2] for line in status.splitlines() if len(line) >= 4}
    dirty_scope = []
    for item in files:
        item["git_state"] = state.get(item["path"], "CLEAN")
        if item["git_state"] != "CLEAN":
            dirty_scope.append({"path": item["path"], "git_state": item["git_state"]})
    aggregate_payload = [{"path": item["path"], "sha256": item["sha256"]} for item in files]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, check=True, stdout=subprocess.PIPE,
    ).stdout.decode("ascii", "strict").strip()
    return {
        "base_commit": head,
        "files": files,
        "scoped_input_aggregate": hashlib.sha256(canonical_bytes(aggregate_payload)).hexdigest(),
        "dirty_scope": dirty_scope,
        "orchestrator_clean": not dirty_scope,
    }


def pe_import_fact(path: Path) -> dict[str, Any]:
    pe = pefile.PE(str(path), fast_load=False)
    try:
        imports = []
        for descriptor in pe.DIRECTORY_ENTRY_IMPORT:
            imports.append({
                "dll": descriptor.dll.decode("ascii", "strict").upper(),
                "symbols": sorted(
                    item.name.decode("ascii", "strict") if item.name else f"ordinal:{item.ordinal}"
                    for item in descriptor.imports
                ),
            })
        return {"machine": pe.FILE_HEADER.Machine, "imports": imports}
    finally:
        pe.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--candidate-lock", type=Path, required=True)
    parser.add_argument("--matrix-contract", type=Path, required=True)
    parser.add_argument("--pyinstaller-source", type=Path, required=True)
    parser.add_argument("--cpython-root", type=Path, required=True)
    parser.add_argument("--vcvarsall", type=Path, required=True)
    parser.add_argument("--msvc-version", required=True)
    parser.add_argument("--windows-sdk-version", required=True)
    parser.add_argument("--dumpbin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validated_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise SystemExit(f"{label} is not a file")
    return resolved


def validated_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise SystemExit(f"{label} is not a directory")
    return resolved


def source_scan(source_root: Path) -> dict[str, Any]:
    paths = sorted(
        {
            *(
                path
                for path in (source_root / "bootloader" / "src").rglob("*")
                if path.is_file() and path.suffix in {".c", ".h"}
            ),
            *(source_root / "PyInstaller" / "loader").rglob("*.py"),
        },
        key=lambda path: path.relative_to(source_root).as_posix(),
    )
    canonical = "".join(
        f"{path.relative_to(source_root).as_posix()}={sha256_file(path)}\n"
        for path in paths
    )
    return {
        "scope": list(SOURCE_SCAN_SCOPE),
        "count": len(paths),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def matching_source_lines(path: Path, patterns: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    matches = {name: [] for name in patterns}
    compiled = {name: re.compile(pattern) for name, pattern in patterns.items()}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for name, expression in compiled.items():
            if expression.search(line):
                matches[name].append({"line": line_number, "text": line.strip()})
    missing = sorted(name for name, values in matches.items() if not values)
    if missing:
        raise SystemExit(f"pinned PyInstaller source markers missing: {', '.join(missing)}")
    return matches


def pristine_pyinstaller_facts(source_root: Path, candidate_lock: dict[str, Any]) -> dict[str, Any]:
    main_path = validated_file(source_root / "bootloader" / "src" / "pyi_main.c", "pyi_main.c")
    archive_path = validated_file(source_root / "bootloader" / "src" / "pyi_archive.c", "pyi_archive.c")
    utils_path = validated_file(source_root / "bootloader" / "src" / "pyi_utils_win32.c", "pyi_utils_win32.c")
    actual_scan = source_scan(source_root)
    expected_scan = candidate_lock["runtime"]["pyinstaller"]["source_scan"]
    if actual_scan["count"] != expected_scan["count"] or actual_scan["sha256"] != expected_scan["sha256"]:
        raise SystemExit("PyInstaller source scan differs from candidate lock")
    expected_main = candidate_lock["runtime"]["pyinstaller"]["archive_source_scan"]["marker_file_hashes"][
        "bootloader/src/pyi_main.c"
    ]
    if sha256_file(main_path) != expected_main:
        raise SystemExit("pyi_main.c differs from candidate lock")
    return {
        "source_scan": actual_scan,
        "files": {
            "bootloader/src/pyi_main.c": sha256_file(main_path),
            "bootloader/src/pyi_archive.c": sha256_file(archive_path),
            "bootloader/src/pyi_utils_win32.c": sha256_file(utils_path),
        },
        "main_path_and_environment": matching_source_lines(
            main_path,
            {
                "resolve_pkg_before_launch": r"_pyi_main_resolve_pkg_archive\(pyi_ctx\)",
                "embedded_archive_open_by_path": r"pyi_archive_open\(pyi_ctx->executable_filename\)",
                "environment_read": r"pyi_getenv\(\"PYINSTALLER_SUPPRESS_SPLASH_SCREEN\"\)",
                "dll_directory_reset": r"SetDllDirectoryW\(NULL\)",
                "dll_directory_from_application_home": r"SetDllDirectoryW\(dllpath_w\)",
            },
        ),
        "archive_path_io": matching_source_lines(
            archive_path,
            {"archive_read_by_path": r"pyi_path_fopen\(archive->filename, \"rb\"\)"},
        ),
        "environment_api": matching_source_lines(
            utils_path,
            {"get_environment_variable": r"GetEnvironmentVariableW\(variable_w, value, PYI_PATH_MAX\)"},
        ),
        "interpretation": (
            "Pristine pyi_main resolves and opens its archive by path, reads process environment, and changes "
            "the process DLL directory before the Python launch path. A Task 1.6 entry cannot delegate E2-E10 "
            "to this largely-stock path; archive/layout bookkeeping must instead consume retained authority."
        ),
    }


def normalize_compile_arguments(arguments: list[str], repository_root: Path, output: Path) -> list[str]:
    normalized = []
    for argument in arguments:
        folded = argument.casefold()
        if folded.startswith("/fo"):
            normalized.append("/Fo<artifact>/probe.obj")
        elif folded.startswith("/fe"):
            normalized.append("/Fe<artifact>/probe.exe")
        elif Path(argument).is_absolute():
            path = Path(argument).resolve()
            try:
                normalized.append(f"<repository>/{path.relative_to(repository_root).as_posix()}")
            except ValueError:
                try:
                    normalized.append(f"<artifact>/{path.relative_to(output).as_posix()}")
                except ValueError:
                    normalized.append(path.name)
        else:
            normalized.append(argument)
    return normalized


def validate_matrix_contract(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    contract = load_json_object(path, "matrix contract")
    if contract.get("schema") != MATRIX_SCHEMA:
        raise SystemExit("unexpected matrix contract schema")
    assertion_ids = tuple(contract.get("ordered_assertion_ids", ()))
    if len(assertion_ids) != 14 or len(set(assertion_ids)) != 14:
        raise SystemExit("matrix contract must contain fourteen unique assertions")
    if BLOCKING_ASSERTION not in assertion_ids:
        raise SystemExit("matrix contract omits the blocking assertion")
    return contract, assertion_ids


def vc_environment(vcvarsall: Path, msvc_version: str, sdk_version: str) -> dict[str, str]:
    if any(character in str(vcvarsall) for character in "%\r\n"):
        raise SystemExit("vcvarsall path contains a cmd expansion/control character")
    command = (
        f'set "LOCALCAT_VCVARSALL={vcvarsall}" && '
        f'call "%LOCALCAT_VCVARSALL%" x64 {sdk_version} -vcvars_ver={msvc_version} >nul && set'
    )
    result = subprocess.run(
        f"cmd.exe /d /c {command}",
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(
            "vcvarsall failed "
            f"({result.returncode}); stdout={result.stdout.strip()!r}; stderr={result.stderr.strip()!r}"
        )
    environment: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith("="):
            name, value = line.split("=", 1)
            environment[name] = value
    for name in ("CL", "LINK", "_CL_", "_LINK_"):
        environment.pop(name, None)
    environment.pop("LOCALCAT_VCVARSALL", None)
    allowed = {
        "include",
        "lib",
        "libpath",
        "ucrtversion",
        "universalsdkdir",
        "universalcrtsdkdir",
        "vctoolsinstalldir",
        "vctoolsversion",
        "windowssdkbinpath",
        "windowssdkdir",
        "windowssdkversion",
    }
    sanitized = {name: value for name, value in environment.items() if name.casefold() in allowed}
    tool_bin = Path(environment["VCToolsInstallDir"]) / "bin" / "Hostx64" / "x64"
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    sanitized.update({
        "ComSpec": str(system32 / "cmd.exe"),
        "PATH": os.pathsep.join((str(tool_bin), str(system32))),
        "SystemRoot": os.environ["SystemRoot"],
        "WINDIR": os.environ["WINDIR"],
        "VSCMD_SKIP_SENDTELEMETRY": "1",
    })
    return sanitized


def verified_toolchain_aggregates(
    environment: dict[str, str], sdk_version: str, candidate_lock: dict[str, Any],
) -> dict[str, Any]:
    msvc_root = Path(environment["VCToolsInstallDir"]).resolve(strict=True)
    sdk_root = Path(environment["WindowsSdkDir"]).resolve(strict=True)
    locked_msvc = candidate_lock["toolchain"]["msvc"]
    locked_sdk = candidate_lock["toolchain"]["windows_sdk"]
    facts = {
        "compiler_bin": directory_input_fact(msvc_root / "bin/Hostx64/x64", locked_msvc["compiler_bin"]["role"]),
        "msvc_headers": directory_input_fact(msvc_root / "include", locked_msvc["headers"]["role"]),
        "msvc_libraries": directory_input_fact(msvc_root / "lib/x64", locked_msvc["libraries"]["role"]),
        "sdk_binary_inputs": directory_input_fact(sdk_root / "bin" / sdk_version / "x64", locked_sdk["binary_inputs"]["role"]),
        "sdk_headers": directory_input_fact(sdk_root / "Include" / sdk_version, locked_sdk["headers"]["role"]),
        "sdk_um_libraries": directory_input_fact(sdk_root / "Lib" / sdk_version / "um/x64", locked_sdk["libraries"]["role"]),
        "sdk_ucrt_libraries": directory_input_fact(sdk_root / "Lib" / sdk_version / "ucrt/x64", locked_sdk["ucrt_libraries"]["role"]),
    }
    expected = {
        "compiler_bin": locked_msvc["compiler_bin"],
        "msvc_headers": locked_msvc["headers"],
        "msvc_libraries": locked_msvc["libraries"],
        "sdk_binary_inputs": locked_sdk["binary_inputs"],
        "sdk_headers": locked_sdk["headers"],
        "sdk_um_libraries": locked_sdk["libraries"],
        "sdk_ucrt_libraries": locked_sdk["ucrt_libraries"],
    }
    if facts != expected:
        raise SystemExit("MSVC/SDK aggregate differs from candidate lock")
    return facts


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=True))) == os.path.normcase(str(right.resolve(strict=True)))


def _resolved_search_roots(value: str, role: str) -> list[Path]:
    raw_roots = value.split(os.pathsep)
    if not raw_roots or any(not item for item in raw_roots):
        raise SystemExit(f"{role} contains an empty search root")
    roots = [Path(item).resolve(strict=True) for item in raw_roots]
    normalized = [os.path.normcase(str(item)) for item in roots]
    if len(normalized) != len(set(normalized)):
        raise SystemExit(f"{role} contains a duplicate search root")
    return roots


def verify_compile_environment_projection(
    environment: dict[str, str], msvc_version: str, sdk_version: str,
) -> dict[str, Any]:
    msvc_root = Path(environment["VCToolsInstallDir"]).resolve(strict=True)
    sdk_root = Path(environment["WindowsSdkDir"]).resolve(strict=True)
    universal_root = Path(environment["UniversalCRTSdkDir"]).resolve(strict=True)
    if environment["VCToolsVersion"].rstrip("\\/") != msvc_version:
        raise SystemExit("compile environment MSVC version differs from requested version")
    if environment["WindowsSDKVersion"].rstrip("\\/") != sdk_version or environment["UCRTVersion"].rstrip("\\/") != sdk_version:
        raise SystemExit("compile environment SDK version differs from requested version")
    if not _same_path(sdk_root, universal_root):
        raise SystemExit("compile environment SDK/UCRT roots differ")
    if msvc_root.name.casefold() != msvc_version.casefold():
        raise SystemExit("compile environment MSVC root is not version-qualified")

    expected_include = [
        ("msvc_headers", msvc_root / "include"),
        *[("sdk_headers", sdk_root / "Include" / sdk_version / name) for name in ("ucrt", "um", "shared", "winrt", "cppwinrt")],
    ]
    expected_lib = [
        ("msvc_libraries_x64", msvc_root / "lib" / "x64"),
        ("sdk_ucrt_libraries_x64", sdk_root / "Lib" / sdk_version / "ucrt" / "x64"),
        ("sdk_um_libraries_x64", sdk_root / "Lib" / sdk_version / "um" / "x64"),
    ]
    actual_include = _resolved_search_roots(environment.get("INCLUDE", ""), "INCLUDE")
    actual_lib = _resolved_search_roots(environment.get("LIB", ""), "LIB")
    if len(actual_include) != len(expected_include) or any(
        not _same_path(actual, expected) for actual, (_, expected) in zip(actual_include, expected_include)
    ):
        raise SystemExit("compile environment INCLUDE roots differ from locked projection")
    if len(actual_lib) != len(expected_lib) or any(
        not _same_path(actual, expected) for actual, (_, expected) in zip(actual_lib, expected_lib)
    ):
        raise SystemExit("compile environment LIB roots differ from locked x64 projection")
    if environment.get("LIBPATH"):
        raise SystemExit("compile environment LIBPATH must be cleared")
    return {
        "architecture": "x64",
        "msvc_version": msvc_version,
        "windows_sdk_version": sdk_version,
        "include_roots": [
            {"aggregate": role, "path": str(path.resolve(strict=True))}
            for role, path in expected_include
        ],
        "lib_roots": [
            {"aggregate": role, "path": str(path.resolve(strict=True))}
            for role, path in expected_lib
        ],
        "libpath": "CLEARED",
    }


def finalize_compile_environment(
    environment: dict[str, str], msvc_version: str, sdk_version: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    finalized = dict(environment)
    msvc_root = Path(finalized["VCToolsInstallDir"]).resolve(strict=True)
    sdk_root = Path(finalized["WindowsSdkDir"]).resolve(strict=True)
    finalized["INCLUDE"] = os.pathsep.join(str(path.resolve(strict=True)) for path in (
        msvc_root / "include",
        *(sdk_root / "Include" / sdk_version / name for name in ("ucrt", "um", "shared", "winrt", "cppwinrt")),
    ))
    finalized["LIB"] = os.pathsep.join(str(path.resolve(strict=True)) for path in (
        msvc_root / "lib" / "x64",
        sdk_root / "Lib" / sdk_version / "ucrt" / "x64",
        sdk_root / "Lib" / sdk_version / "um" / "x64",
    ))
    finalized.pop("LIBPATH", None)
    return finalized, verify_compile_environment_projection(finalized, msvc_version, sdk_version)


def compile_probe(
    source: Path,
    output: Path,
    environment: dict[str, str],
    compiler: Path,
) -> tuple[list[str], subprocess.CompletedProcess[bytes]]:
    command = [
        str(compiler),
        "/nologo",
        "/W4",
        "/WX",
        "/O2",
        "/MT",
        "/guard:cf",
        "/Brepro",
        f"/Fo{output / 'probe.obj'}",
        f"/Fe{output / 'probe.exe'}",
        str(source),
        "/link",
        "/Brepro",
        "/guard:cf",
        "/dynamicbase",
        "/highentropyva",
        "/nxcompat",
        "/machine:x64",
    ]
    result = subprocess.run(
        command,
        cwd=output,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return command, result


def run_probe(executable: Path, cpython_root: Path, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(executable), str(cpython_root)],
        cwd=cwd,
        env={"SystemRoot": os.environ["SystemRoot"], "WINDIR": os.environ["WINDIR"]},
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


TRACE_PATTERN = re.compile(r"^TRACE stage=(\d+) load=([^ ]+) path=(.*)$")
MODULE_PATTERN = re.compile(
    r"^MODULE role=([^ ]+) path=(.*?) volume=([0-9A-F]{8}) "
    r"file_id=([0-9A-F]{16}) size=([0-9A-F]{16})$"
)


def parse_trace(stderr: str, system_root: Path) -> list[dict[str, Any]]:
    trace = []
    system32 = os.path.normcase(os.path.normpath(str(system_root / "System32")))
    for line in stderr.splitlines():
        match = TRACE_PATTERN.fullmatch(line)
        if match:
            loaded_path = match.group(3)
            parent = os.path.normcase(os.path.normpath(str(Path(loaded_path).parent)))
            trace.append({
                "stage": int(match.group(1)),
                "module": match.group(2).upper(),
                "path": loaded_path,
                "observed_path_role": "SYSTEM32" if parent == system32 else "OTHER",
            })
    return trace


def parse_module_records(stderr: str) -> dict[str, dict[str, Any]]:
    records = {}
    for line in stderr.splitlines():
        match = MODULE_PATTERN.fullmatch(line)
        if match:
            role = match.group(1)
            if role in records:
                raise SystemExit(f"duplicate MODULE record: {role}")
            records[role] = {
                "path": match.group(2),
                "identity": {
                    "volume": match.group(3),
                    "file_id": match.group(4),
                    "size": match.group(5),
                },
            }
    if set(records) != {"python", "vcruntime"}:
        raise SystemExit("probe did not emit exact python/vcruntime MODULE records")
    return records


def loader_iat_and_callsites(
    binary: Path, dumpbin: Path,
) -> tuple[list[dict[str, Any]], list[str], bytes]:
    pe = pefile.PE(str(binary), fast_load=False)
    loader_imports = []
    address_tokens = set()
    for descriptor in pe.DIRECTORY_ENTRY_IMPORT:
        dll = descriptor.dll.decode("ascii", "strict")
        for imported in descriptor.imports:
            if not imported.name:
                continue
            name = imported.name.decode("ascii", "strict")
            if name in {"LoadLibraryA", "LoadLibraryW", "LoadLibraryExW", "LdrLoadDll"}:
                absolute = imported.address
                token = f"{absolute:X}H"
                address_tokens.add(token)
                loader_imports.append({"dll": dll.upper(), "symbol": name, "iat_va": f"0x{absolute:016x}"})
    process = subprocess.run(
        [str(dumpbin), "/DISASM:NOBYTES", str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"dumpbin failed: {process.stderr.decode('utf-8', 'strict')}")
    dumpbin_text = process.stdout.decode("utf-8", "strict")
    matching_lines = []
    for line in dumpbin_text.splitlines():
        upper = line.upper()
        if any(token in upper for token in address_tokens):
            matching_lines.append(line.strip())
    return loader_imports, matching_lines, process.stdout


def validate_legacy_target(candidate_lock: dict[str, Any]) -> None:
    """This historical probe cannot decide a revised system-provider contract."""
    if (
        candidate_lock.get("schema") != "localcat.windows-frozen-entry-candidate-input.v1"
        or any(key in candidate_lock.get("entry_contract", {}).get("dynamic_loader", {})
               for key in ("system_provider_target", "system_service_boundary"))
    ):
        raise SystemExit("historical NO-GO replay requires the original v1 target; use the custom-entry matrix for revised targets")


def main() -> int:
    arguments = parse_arguments()
    repository_root = validated_directory(arguments.repository_root, "repository root")
    candidate_lock_path = validated_file(arguments.candidate_lock, "candidate lock")
    candidate_lock = load_json_object(candidate_lock_path, "candidate lock")
    validate_legacy_target(candidate_lock)
    pyinstaller_source = validated_directory(arguments.pyinstaller_source, "PyInstaller source")
    cpython_root = validated_directory(arguments.cpython_root, "CPython root")
    vcvarsall = validated_file(arguments.vcvarsall, "vcvarsall")
    dumpbin = validated_file(arguments.dumpbin, "dumpbin")
    output = arguments.output.resolve()
    if output.exists():
        raise SystemExit("output must not already exist")
    output.mkdir(parents=True)
    nonrepo_cwd = output / "nonrepo-cwd"
    nonrepo_cwd.mkdir()

    source = validated_file(repository_root / "tools" / "probe_windows_python314_preauthority.c", "probe source")
    python_dll = validated_file(cpython_root / "python314.dll", "python DLL")
    runtime_dll = validated_file(cpython_root / "vcruntime140.dll", "VCRUNTIME")
    matrix_contract, matrix_ids = validate_matrix_contract(arguments.matrix_contract)
    if candidate_lock.get("status") != "MATERIALIZED":
        raise SystemExit("candidate input is not materialized")
    evidence_producer = verified_evidence_producer(candidate_lock)
    if candidate_lock["runtime"]["cpython"]["dll"]["sha256"] != sha256_file(python_dll):
        raise SystemExit("python DLL digest differs from candidate lock")
    members = candidate_lock["runtime"]["cpython"]["native_closure"]["local_members"]
    runtime_member = next(item for item in members if item["name"].casefold() == "vcruntime140.dll")
    if runtime_member["sha256"] != sha256_file(runtime_dll):
        raise SystemExit("VCRUNTIME digest differs from candidate lock")

    expected_toolchain = candidate_lock["toolchain"]
    if arguments.msvc_version != expected_toolchain["msvc"]["toolset_version"]:
        raise SystemExit("MSVC version differs from candidate lock")
    if arguments.windows_sdk_version != expected_toolchain["windows_sdk"]["version"]:
        raise SystemExit("Windows SDK version differs from candidate lock")
    environment = vc_environment(vcvarsall, arguments.msvc_version, arguments.windows_sdk_version)
    environment["TEMP"] = str(output)
    environment["TMP"] = str(output)
    toolchain_aggregates = verified_toolchain_aggregates(
        environment, arguments.windows_sdk_version, candidate_lock,
    )
    environment, compile_environment_projection = finalize_compile_environment(
        environment, arguments.msvc_version, arguments.windows_sdk_version,
    )
    tool_bin = Path(environment["VCToolsInstallDir"]) / "bin" / "Hostx64" / "x64"
    compiler = validated_file(tool_bin / "cl.exe", "cl")
    linker = validated_file(tool_bin / "link.exe", "link")
    if sha256_file(compiler) != expected_toolchain["msvc"]["tools"]["cl"]["sha256"]:
        raise SystemExit("cl.exe differs from candidate lock")
    if sha256_file(linker) != expected_toolchain["msvc"]["tools"]["link"]["sha256"]:
        raise SystemExit("link.exe differs from candidate lock")
    if sha256_file(dumpbin) != expected_toolchain["msvc"]["tools"]["dumpbin"]["sha256"]:
        raise SystemExit("dumpbin.exe differs from candidate lock")
    upstream_facts = pristine_pyinstaller_facts(pyinstaller_source, candidate_lock)
    source_identity = scoped_source_identity(repository_root)
    compile_command, compile_result = compile_probe(source, output, environment, compiler)
    (output / "compile.stdout.log").write_bytes(compile_result.stdout)
    (output / "compile.stderr.log").write_bytes(compile_result.stderr)
    if compile_result.returncode != 0:
        raise SystemExit("probe compile failed")
    executable = output / "probe.exe"
    probe_result = run_probe(executable, cpython_root, nonrepo_cwd)
    (output / "probe.stdout.log").write_bytes(probe_result.stdout)
    (output / "probe.stderr.log").write_bytes(probe_result.stderr)
    if probe_result.returncode != 0:
        raise SystemExit(f"probe failed with {probe_result.returncode}")
    probe_stderr = probe_result.stderr.decode("utf-8", "strict")
    trace = parse_trace(probe_stderr, Path(os.environ["SystemRoot"]))
    module_records = parse_module_records(probe_stderr)
    diagnostic_path_reopen_modules = {}
    for role, record in module_records.items():
        fact = binary_fact(Path(record["path"]))
        if fact["identity"] != record["identity"]:
            raise SystemExit(f"diagnostic path-reopen {role} identity changed")
        diagnostic_path_reopen_modules[role] = fact
    if diagnostic_path_reopen_modules["python"]["sha256"] != sha256_file(python_dll):
        raise SystemExit("diagnostic Python path digest differs from candidate input")
    if diagnostic_path_reopen_modules["vcruntime"]["sha256"] != sha256_file(runtime_dll):
        raise SystemExit("diagnostic VCRUNTIME path digest differs from candidate input")
    stage_three = [item for item in trace if item["stage"] == 3]
    target_calls = candidate_lock["entry_contract"]["dynamic_loader"]["pre_authority_external_calls"]
    declared_external = {
        name.upper()
        for name in candidate_lock["runtime"]["cpython"]["native_closure"]["external_import_names"]
    }
    unexpected = [item for item in stage_three if item["module"] not in declared_external]
    if not unexpected:
        raise SystemExit("probe did not reproduce the Task 1.6 dynamic-root contract mismatch")
    provider_path = Path(unexpected[0]["path"])
    observed_system_provider = {
        "windows": windows_build(),
        "module": unexpected[0]["module"],
        **binary_fact(provider_path),
    }
    loader_imports, loader_callsites, dumpbin_stdout = loader_iat_and_callsites(python_dll, dumpbin)
    (output / "python314.dumpbin.log").write_bytes(dumpbin_stdout)

    matrix = []
    for assertion_id in matrix_ids:
        if assertion_id == BLOCKING_ASSERTION:
            matrix.append({
                "id": assertion_id,
                "status": "FAIL",
                "detail": "CPython initialization loaded an undeclared module before E10; exact Task 1.5 call surface cannot be realized",
            })
        else:
            matrix.append({
                "id": assertion_id,
                "status": "BLOCKED_NOT_RUN",
                "detail": "candidate construction stopped at the first exact-contract failure",
            })

    audit: dict[str, Any] = {
        "schema": NO_GO_SCHEMA,
        "status": "NO_GO",
        "repository_commit": source_identity["base_commit"],
        "candidate_input_digest": candidate_lock["candidate_input_digest"],
        "candidate_lock_sha256": sha256_file(candidate_lock_path),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "windows": windows_build(),
        },
        "evidence_producer": evidence_producer,
        "source_identity": source_identity,
        "inputs": {
            "audit_source_sha256": sha256_file(repository_root / "tools" / "audit_windows_frozen_custom_entry.py"),
            "validator_source_sha256": sha256_file(repository_root / "tools" / "validate_windows_frozen_custom_entry_no_go.py"),
            "probe_source_sha256": sha256_file(source),
            "matrix_contract_sha256": sha256_file(arguments.matrix_contract.resolve()),
            "schema_sha256": sha256_file(repository_root / "packaging" / "windows" / "frozen-entry" / "custom-entry-no-go.schema.json"),
            "python314_dll_sha256": sha256_file(python_dll),
            "vcruntime140_dll_sha256": sha256_file(runtime_dll),
            "pyinstaller_source_scan": upstream_facts["source_scan"],
            "cl_sha256": sha256_file(compiler),
            "link_sha256": sha256_file(linker),
            "dumpbin_sha256": sha256_file(dumpbin),
            "toolchain_aggregates": toolchain_aggregates,
            "compile_environment_projection": compile_environment_projection,
        },
        "probe": {
            "compile_arguments": normalize_compile_arguments(compile_command, repository_root, output),
            "compile_exit": compile_result.returncode,
            "run_exit": probe_result.returncode,
            "cwd_role": "fresh child of ignored artifact; not candidate NONREPO_CWD proof",
            "probe_exe_sha256": sha256_file(executable),
            "probe_pe": pe_import_fact(executable),
            "trace": trace,
            "diagnostic_path_reopen_modules": diagnostic_path_reopen_modules,
            "limitations": [
                "The loader notification callback records into preallocated memory and performs no I/O or allocation.",
                "Notification begins after process entry and cannot enumerate already-loaded or failed loads.",
                "Observed load notifications are diagnostic facts, not pre-authority proof or closure PASS evidence.",
            ],
        },
        "raw_artifacts": {
            name: sha256_file(output / name)
            for name in (
                "compile.stdout.log",
                "compile.stderr.log",
                "probe.stdout.log",
                "probe.stderr.log",
                "probe.obj",
                "probe.exe",
                "python314.dumpbin.log",
            )
        },
        "target_contract": {
            "pre_authority_external_calls": target_calls,
            "declared_external_import_names": sorted(declared_external),
        },
        "observed_pre_e10_dynamic_modules": unexpected,
        "observed_system_provider": observed_system_provider,
        "static_python_loader_surface": {
            "imports": loader_imports,
            "disassembly_callsites": loader_callsites,
            "interpretation": (
                "The pinned Python DLL contains real loader imports and callsites. Disassembly alone does not prove "
                "pre-E10 reachability; the stage-3 notification separately establishes the observed initialization load."
            ),
        },
        "pristine_pyinstaller_path": upstream_facts,
        "blocker": {
            "code": "FROZEN_ENTRY.CANDIDATE.DYNAMIC_ROOT_CONTRACT_MISMATCH",
            "observed_fact": (
                "BCRYPTPRIMITIVES.DLL was observed loading from System32 during "
                "Py_InitializeFromInitConfig (stage 3), before E10"
            ),
            "static_fact": "the pinned Python DLL imports loader APIs and contains real callsites to them",
            "stock_path_fact": (
                "pristine pyi_main performs path-based archive reads, environment reads, and SetDllDirectoryW "
                "before its Python launch path"
            ),
            "required_reapproval_delta": [
                "Define the exact pre-E10 System32 provider/API-triggered dynamic-root model for the observed runtime path.",
                "Define exact preauthorization and external-resolution evidence for that dynamic-root model without post-load authority escalation.",
            ],
            "preserved_implementation_boundary": (
                "The already-approved retained-authority replacement for pristine pyi_main archive/layout/environment "
                "bookkeeping remains mandatory and is not a new reapproval delta."
            ),
            "decision": "return to reapproval; this producer does not edit the candidate contract or grant authority",
        },
        "matrix_contract": {
            "schema": matrix_contract["schema"],
            "completion_rule": matrix_contract["completion_rule"],
            "no_go_rule": matrix_contract["no_go_rule"],
        },
        "matrix": matrix,
    }
    replay_projection = {
        key: value
        for key, value in audit.items()
        if key not in {"raw_artifacts", "evidence_digest", "replay_projection_digest"}
    }
    audit["replay_projection_digest"] = hashlib.sha256(canonical_bytes(replay_projection)).hexdigest()
    audit["evidence_digest"] = hashlib.sha256(canonical_bytes(audit)).hexdigest()
    (output / "custom-entry-no-go.json").write_bytes(canonical_bytes(audit))
    report = "\n".join([
        "# Task 1.6 custom-entry NO-GO",
        "",
        f"- Candidate input: `{audit['candidate_input_digest']}`",
        f"- Evidence digest: `{audit['evidence_digest']}`",
        f"- Replay projection: `{audit['replay_projection_digest']}`",
        "- Blocking assertion: `W3.CUSTOM.DYNAMIC_ROOTS_DECLARED`",
        "- Observed dynamic fact: `BCRYPTPRIMITIVES.DLL` loaded from System32 during `Py_InitializeFromInitConfig`, before E10.",
        "- Static CPython fact: the pinned DLL imports `LoadLibraryA`, `LoadLibraryW`, and `LoadLibraryExW` and contains real callsites; disassembly alone is not reachability proof.",
        "- Pristine PyInstaller fact: `pyi_main` reads the embedded archive by path, reads environment state, and calls `SetDllDirectoryW` before its Python launch path.",
        "- Contract mismatch: the approved exact pre-authority call surface contains only the release-owned dispatcher, and the declared runtime closure omits `BCRYPTPRIMITIVES.DLL`.",
        "- Reapproval boundary: define the exact pre-E10 System32 provider/API-triggered dynamic-root model and its preauthorization/external-resolution evidence.",
        "- Preserved boundary: the already-approved retained-authority replacement for stock archive/layout bookkeeping remains mandatory; it is not a new delta.",
        "- Result: candidate construction and the remaining thirteen assertions are blocked, not passed or skipped.",
        "",
    ])
    (output / "REPORT.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps({"status": "NO_GO", "evidence_digest": audit["evidence_digest"], "output": str(output)}, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
