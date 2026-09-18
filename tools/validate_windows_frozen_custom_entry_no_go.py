#!/usr/bin/env python3
"""Validate Task 1.6 terminal NO-GO evidence without granting candidate authority."""

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
import shutil
import subprocess
import sys
import tempfile
from typing import Any

pefile: Any = None


EVIDENCE_SCHEMA = "localcat.windows-frozen-custom-entry-no-go.v1"
MATRIX_SCHEMA = "localcat.windows-frozen-custom-entry-matrix.v1"
BLOCKING_ASSERTION = "W3.CUSTOM.DYNAMIC_ROOTS_DECLARED"
SOURCE_PATHS = (
    "packaging/windows/frozen-entry/README.md",
    "packaging/windows/frozen-entry/candidate-input.lock.json",
    "packaging/windows/frozen-entry/custom-entry-matrix.json",
    "packaging/windows/frozen-entry/custom-entry-no-go.schema.json",
    "tools/audit_windows_frozen_custom_entry.py",
    "tools/probe_windows_python314_preauthority.c",
    "tools/validate_windows_frozen_custom_entry_no_go.py",
)
RAW_FILES = (
    "compile.stdout.log",
    "compile.stderr.log",
    "probe.stdout.log",
    "probe.stderr.log",
    "probe.obj",
    "probe.exe",
    "python314.dumpbin.log",
)
TOP_LEVEL_KEYS = {
    "schema", "status", "repository_commit", "candidate_input_digest",
    "candidate_lock_sha256", "host", "evidence_producer", "source_identity", "inputs", "probe",
    "raw_artifacts", "target_contract", "observed_pre_e10_dynamic_modules",
    "observed_system_provider", "static_python_loader_surface",
    "pristine_pyinstaller_path", "blocker", "matrix_contract", "matrix",
    "replay_projection_digest", "evidence_digest",
}
REAPPROVAL_DELTA = [
    "Define the exact pre-E10 System32 provider/API-triggered dynamic-root model for the observed runtime path.",
    "Define exact preauthorization and external-resolution evidence for that dynamic-root model without post-load authority escalation.",
]
PRESERVED_BOUNDARY = (
    "The already-approved retained-authority replacement for pristine pyi_main archive/layout/environment "
    "bookkeeping remains mandatory and is not a new reapproval delta."
)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_object(path: Path, role: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.resolve(strict=True).read_bytes().decode("utf-8", "strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"invalid {role}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{role} must be a JSON object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def validate_legacy_target(candidate_lock: dict[str, Any]) -> None:
    require(
        candidate_lock.get("schema") == "localcat.windows-frozen-entry-candidate-input.v1"
        and not any(key in candidate_lock.get("entry_contract", {}).get("dynamic_loader", {})
                    for key in ("system_provider_target", "system_service_boundary")),
        "historical NO-GO evidence cannot validate a revised system-provider target",
    )


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def aggregate_base_runtime(root: Path, role: str) -> dict[str, Any]:
    aggregate = hashlib.sha256(b"localcat.directory-input-sha256.v1\n")
    file_count = 0
    byte_count = 0
    for member in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        name = member.relative_to(root).as_posix()
        size = member.stat().st_size
        member_digest = digest_file(member)
        aggregate.update(name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(member_digest.encode("ascii"))
        aggregate.update(b"\n")
        file_count += 1
        byte_count += size
    return {
        "role": role,
        "algorithm": "localcat.directory-input-sha256.v1",
        "file_count": file_count,
        "bytes": byte_count,
        "sha256": aggregate.hexdigest(),
    }


def establish_parser_tcb(candidate_lock: dict[str, Any]) -> dict[str, Any]:
    global pefile
    expected = candidate_lock["evidence_producer"]
    executable = Path(sys.executable).resolve(strict=True)
    base_root = Path(sys.base_prefix).resolve(strict=True)
    specification = importlib.util.find_spec("pefile")
    require(specification is not None and specification.origin is not None, "cannot resolve pefile before import")
    source_path = Path(specification.origin).resolve(strict=True)
    distribution = importlib.metadata.distribution("pefile")
    distribution_root = getattr(distribution, "_path", None)
    require(distribution_root is not None, "cannot resolve pefile METADATA")
    metadata_path = (Path(distribution_root) / "METADATA").resolve(strict=True)
    runtime_fact = aggregate_base_runtime(base_root, expected["base_runtime"]["role"])
    venv_config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    include_system = any(
        line.split("=", 1)[1].strip().casefold() == "true"
        for line in venv_config.splitlines()
        if line.casefold().startswith("include-system-site-packages") and "=" in line
    )
    venv_fact = {"is_venv": sys.prefix != sys.base_prefix, "include_system_site_packages": include_system}
    require(digest_file(executable) == expected["python"]["sha256"], "validator Python differs from candidate lock")
    require(platform.python_version() == expected["python_version"], "validator Python version differs from lock")
    require(runtime_fact == expected["base_runtime"], "validator base runtime aggregate differs from lock")
    require(venv_fact == expected["venv"], "validator venv profile differs from lock")
    require(importlib.metadata.version("pefile") == expected["pefile"]["version"], "validator pefile version differs from lock")
    require(digest_file(source_path) == expected["pefile"]["source"]["sha256"], "validator pefile source differs from lock")
    require(
        digest_file(metadata_path) == expected["pefile"]["distribution_metadata"]["sha256"],
        "validator pefile METADATA differs from lock",
    )
    pefile = importlib.import_module("pefile")
    require(Path(pefile.__file__).resolve(strict=True) == source_path, "loaded pefile path changed")
    return {
        "python": {"path": str(executable), "sha256": digest_file(executable), "version": platform.python_version()},
        "base_runtime": {"path": str(base_root), **runtime_fact},
        "venv": venv_fact,
        "pefile": {
            "version": importlib.metadata.version("pefile"),
            "module_path": str(source_path),
            "module_sha256": digest_file(source_path),
            "metadata_path": str(metadata_path),
            "metadata_sha256": digest_file(metadata_path),
        },
    }


def exact_keys(value: Any, keys: set[str], role: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{role} must be an object")
    require(set(value) == keys, f"{role} keys differ: {sorted(set(value) ^ keys)}")
    return value


SUPPORTED_SCHEMA_KEYWORDS = {
    "$schema", "$id", "title", "type", "required", "properties",
    "additionalProperties", "const", "pattern", "minItems", "maxItems",
    "items", "enum",
}
SCHEMA_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate_schema_language(schema: Any, location: str = "$") -> None:
    require(isinstance(schema, dict), f"schema node {location} is not an object")
    unsupported = set(schema) - SUPPORTED_SCHEMA_KEYWORDS
    require(not unsupported, f"unsupported schema keyword at {location}: {sorted(unsupported)}")
    if "type" in schema:
        require(schema["type"] in SCHEMA_TYPES, f"unsupported schema type at {location}")
    if "required" in schema:
        required = schema["required"]
        require(
            isinstance(required, list)
            and all(isinstance(item, str) for item in required)
            and len(required) == len(set(required)),
            f"invalid required at {location}",
        )
    if "properties" in schema:
        properties = schema["properties"]
        require(isinstance(properties, dict), f"invalid properties at {location}")
        for name, child in properties.items():
            require(isinstance(name, str), f"non-string property name at {location}")
            validate_schema_language(child, f"{location}.properties[{name!r}]")
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        require(isinstance(additional, (bool, dict)), f"invalid additionalProperties at {location}")
        if isinstance(additional, dict):
            validate_schema_language(additional, f"{location}.additionalProperties")
    if "items" in schema:
        validate_schema_language(schema["items"], f"{location}.items")
    for keyword in ("minItems", "maxItems"):
        if keyword in schema:
            require(
                isinstance(schema[keyword], int) and not isinstance(schema[keyword], bool) and schema[keyword] >= 0,
                f"invalid {keyword} at {location}",
            )
    if "pattern" in schema:
        require(isinstance(schema["pattern"], str), f"invalid pattern at {location}")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise SystemExit(f"invalid pattern at {location}: {exc}") from exc
    if "enum" in schema:
        require(isinstance(schema["enum"], list) and schema["enum"], f"invalid enum at {location}")


def apply_schema(instance: Any, schema: dict[str, Any], location: str = "$") -> None:
    if "type" in schema:
        expected = SCHEMA_TYPES[schema["type"]]
        type_ok = isinstance(instance, expected)
        if schema["type"] in {"integer", "number"} and isinstance(instance, bool):
            type_ok = False
        require(type_ok, f"schema type failure at {location}: expected {schema['type']}")
    if "const" in schema:
        require(instance == schema["const"], f"schema const failure at {location}")
    if "enum" in schema:
        require(instance in schema["enum"], f"schema enum failure at {location}")
    if "pattern" in schema:
        require(isinstance(instance, str) and re.search(schema["pattern"], instance) is not None, f"schema pattern failure at {location}")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in instance]
        require(not missing, f"schema required failure at {location}: {missing}")
        properties = schema.get("properties", {})
        for name, child_schema in properties.items():
            if name in instance:
                apply_schema(instance[name], child_schema, f"{location}.{name}")
        unknown = set(instance) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            require(not unknown, f"schema additionalProperties failure at {location}: {sorted(unknown)}")
        elif isinstance(additional, dict):
            for name in unknown:
                apply_schema(instance[name], additional, f"{location}.{name}")
    if isinstance(instance, list):
        if "minItems" in schema:
            require(len(instance) >= schema["minItems"], f"schema minItems failure at {location}")
        if "maxItems" in schema:
            require(len(instance) <= schema["maxItems"], f"schema maxItems failure at {location}")
        if "items" in schema:
            for index, item in enumerate(instance):
                apply_schema(item, schema["items"], f"{location}[{index}]")


class FileInformation(ctypes.Structure):
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


class OsVersion(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.ULONG),
        ("major", wintypes.ULONG),
        ("minor", wintypes.ULONG),
        ("build", wintypes.ULONG),
        ("platform", wintypes.ULONG),
        ("service_pack", wintypes.WCHAR * 128),
    ]


def current_windows_build() -> dict[str, int]:
    version = OsVersion()
    version.size = ctypes.sizeof(version)
    require(ctypes.windll.ntdll.RtlGetVersion(ctypes.byref(version)) == 0, "RtlGetVersion failed")
    return {"major": version.major, "minor": version.minor, "build": version.build}


def current_identity(path: Path) -> dict[str, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0x02000000 | 0x80, None,
    )
    require(handle != wintypes.HANDLE(-1).value, f"cannot open identity handle: {path}")
    try:
        information = FileInformation()
        require(
            bool(kernel32.GetFileInformationByHandle(handle, ctypes.byref(information))),
            f"cannot read identity: {path}",
        )
    finally:
        kernel32.CloseHandle(handle)
    return {
        "volume": f"{information.volume_serial:08X}",
        "file_id": f"{information.file_index_high:08X}{information.file_index_low:08X}",
        "size": f"{information.file_size_high:08X}{information.file_size_low:08X}",
    }


def current_file_version(path: Path) -> str | None:
    pe = pefile.PE(str(path), fast_load=False)
    try:
        fixed_items = getattr(pe, "VS_FIXEDFILEINFO", None)
        if not fixed_items:
            return None
        fixed = fixed_items[0]
        return ".".join(str(part) for part in (
            fixed.FileVersionMS >> 16,
            fixed.FileVersionMS & 0xFFFF,
            fixed.FileVersionLS >> 16,
            fixed.FileVersionLS & 0xFFFF,
        ))
    finally:
        pe.close()


def current_binary_fact(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "identity": current_identity(resolved),
        "sha256": digest_file(resolved),
        "file_version": current_file_version(resolved),
    }


def derive_source_scan(source_root: Path) -> dict[str, Any]:
    paths = sorted(
        {
            *(path for path in (source_root / "bootloader" / "src").rglob("*") if path.is_file() and path.suffix in {".c", ".h"}),
            *(source_root / "PyInstaller" / "loader").rglob("*.py"),
        },
        key=lambda path: path.relative_to(source_root).as_posix(),
    )
    canonical = "".join(
        f"{path.relative_to(source_root).as_posix()}={digest_file(path)}\n" for path in paths
    )
    return {
        "scope": ["bootloader/src/**/*.c", "bootloader/src/**/*.h", "PyInstaller/loader/**/*.py"],
        "count": len(paths),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def source_matches(path: Path, patterns: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    result = {name: [] for name in patterns}
    expressions = {name: re.compile(pattern) for name, pattern in patterns.items()}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for name, expression in expressions.items():
            if expression.search(line):
                result[name].append({"line": number, "text": line.strip()})
    require(all(result.values()), f"source markers missing in {path}")
    return result


def derive_pristine_facts(source_root: Path, candidate_lock: dict[str, Any]) -> dict[str, Any]:
    main_path = (source_root / "bootloader/src/pyi_main.c").resolve(strict=True)
    archive_path = (source_root / "bootloader/src/pyi_archive.c").resolve(strict=True)
    utils_path = (source_root / "bootloader/src/pyi_utils_win32.c").resolve(strict=True)
    scan = derive_source_scan(source_root)
    locked_scan = candidate_lock["runtime"]["pyinstaller"]["source_scan"]
    require(
        scan["count"] == locked_scan["count"] and scan["sha256"] == locked_scan["sha256"],
        "PyInstaller source scan mismatch",
    )
    return {
        "source_scan": scan,
        "files": {
            "bootloader/src/pyi_main.c": digest_file(main_path),
            "bootloader/src/pyi_archive.c": digest_file(archive_path),
            "bootloader/src/pyi_utils_win32.c": digest_file(utils_path),
        },
        "main_path_and_environment": source_matches(main_path, {
            "resolve_pkg_before_launch": r"_pyi_main_resolve_pkg_archive\(pyi_ctx\)",
            "embedded_archive_open_by_path": r"pyi_archive_open\(pyi_ctx->executable_filename\)",
            "environment_read": r"pyi_getenv\(\"PYINSTALLER_SUPPRESS_SPLASH_SCREEN\"\)",
            "dll_directory_reset": r"SetDllDirectoryW\(NULL\)",
            "dll_directory_from_application_home": r"SetDllDirectoryW\(dllpath_w\)",
        }),
        "archive_path_io": source_matches(
            archive_path, {"archive_read_by_path": r"pyi_path_fopen\(archive->filename, \"rb\"\)"}
        ),
        "environment_api": source_matches(
            utils_path, {"get_environment_variable": r"GetEnvironmentVariableW\(variable_w, value, PYI_PATH_MAX\)"}
        ),
        "interpretation": (
            "Pristine pyi_main resolves and opens its archive by path, reads process environment, and changes "
            "the process DLL directory before the Python launch path. A Task 1.6 entry cannot delegate E2-E10 "
            "to this largely-stock path; archive/layout bookkeeping must instead consume retained authority."
        ),
    }


def derive_scoped_identity(repository_root: Path) -> dict[str, Any]:
    files = []
    for relative in SOURCE_PATHS:
        path = (repository_root / relative).resolve(strict=True)
        require(path.is_file(), f"scoped source is not a file: {relative}")
        files.append({"path": relative, "sha256": digest_file(path)})
    status_raw = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", *SOURCE_PATHS],
        cwd=repository_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode("utf-8", "strict")
    states = {line[3:]: line[:2] for line in status_raw.splitlines() if len(line) >= 4}
    dirty = []
    for fact in files:
        fact["git_state"] = states.get(fact["path"], "CLEAN")
        if fact["git_state"] != "CLEAN":
            dirty.append({"path": fact["path"], "git_state": fact["git_state"]})
    aggregate = [{"path": fact["path"], "sha256": fact["sha256"]} for fact in files]
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, check=True, stdout=subprocess.PIPE,
    ).stdout.decode("ascii", "strict").strip()
    return {
        "base_commit": base,
        "files": files,
        "scoped_input_aggregate": hashlib.sha256(canonical_bytes(aggregate)).hexdigest(),
        "dirty_scope": dirty,
        "orchestrator_clean": not dirty,
    }


TRACE_RE = re.compile(r"^TRACE stage=(\d+) load=([^ ]+) path=(.*)$")
MODULE_RE = re.compile(
    r"^MODULE role=([^ ]+) path=(.*?) volume=([0-9A-F]{8}) "
    r"file_id=([0-9A-F]{16}) size=([0-9A-F]{16})$"
)


def derive_probe_records(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    text = raw.decode("utf-8", "strict")
    system32 = os.path.normcase(os.path.normpath(str(Path(os.environ["SystemRoot"]) / "System32")))
    trace = []
    modules = {}
    for line in text.splitlines():
        trace_match = TRACE_RE.fullmatch(line)
        if trace_match:
            loaded_path = trace_match.group(3)
            parent = os.path.normcase(os.path.normpath(str(Path(loaded_path).parent)))
            trace.append({
                "stage": int(trace_match.group(1)),
                "module": trace_match.group(2).upper(),
                "path": loaded_path,
                "observed_path_role": "SYSTEM32" if parent == system32 else "OTHER",
            })
        module_match = MODULE_RE.fullmatch(line)
        if module_match:
            role = module_match.group(1)
            require(role not in modules, f"duplicate raw module record: {role}")
            modules[role] = {
                "path": module_match.group(2),
                "identity": {
                    "volume": module_match.group(3),
                    "file_id": module_match.group(4),
                    "size": module_match.group(5),
                },
            }
    require(set(modules) == {"python", "vcruntime"}, "raw module records incomplete")
    return trace, modules


def derive_pe_imports(path: Path) -> dict[str, Any]:
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


def derive_loader_surface(python_dll: Path, dumpbin: Path) -> tuple[dict[str, Any], bytes]:
    pe = pefile.PE(str(python_dll), fast_load=False)
    try:
        imports = []
        tokens = set()
        for descriptor in pe.DIRECTORY_ENTRY_IMPORT:
            dll = descriptor.dll.decode("ascii", "strict")
            for imported in descriptor.imports:
                if not imported.name:
                    continue
                name = imported.name.decode("ascii", "strict")
                if name in {"LoadLibraryA", "LoadLibraryW", "LoadLibraryExW", "LdrLoadDll"}:
                    imports.append({"dll": dll.upper(), "symbol": name, "iat_va": f"0x{imported.address:016x}"})
                    tokens.add(f"{imported.address:X}H")
    finally:
        pe.close()
    result = subprocess.run(
        [str(dumpbin), "/DISASM:NOBYTES", str(python_dll)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    require(result.returncode == 0, f"independent dumpbin failed: {result.stderr!r}")
    text = result.stdout.decode("utf-8", "strict")
    callsites = [line.strip() for line in text.splitlines() if any(token in line.upper() for token in tokens)]
    return {
        "imports": imports,
        "disassembly_callsites": callsites,
        "interpretation": (
            "The pinned Python DLL contains real loader imports and callsites. Disassembly alone does not prove "
            "pre-E10 reachability; the stage-3 notification separately establishes the observed initialization load."
        ),
    }, result.stdout


def tool_environment(vcvarsall: Path, msvc_version: str, sdk_version: str, temp_root: Path) -> dict[str, str]:
    require(not any(character in str(vcvarsall) for character in "%\r\n"), "invalid vcvarsall path")
    command = (
        f'set "LOCALCAT_VALIDATE_VCVARS={vcvarsall}" && '
        f'call "%LOCALCAT_VALIDATE_VCVARS%" x64 {sdk_version} -vcvars_ver={msvc_version} >nul && set'
    )
    result = subprocess.run(
        f"cmd.exe /d /c {command}", check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    require(result.returncode == 0, f"validator vcvarsall failed: {result.stderr!r}")
    expanded = {}
    for line in result.stdout.decode("utf-8", "strict").splitlines():
        if "=" in line and not line.startswith("="):
            name, value = line.split("=", 1)
            expanded[name] = value
    allowed = {
        "include", "lib", "libpath", "ucrtversion", "universalsdkdir",
        "universalcrtsdkdir", "vctoolsinstalldir", "vctoolsversion",
        "windowssdkbinpath", "windowssdkdir", "windowssdkversion",
    }
    environment = {name: value for name, value in expanded.items() if name.casefold() in allowed}
    tool_bin = Path(expanded["VCToolsInstallDir"]) / "bin" / "Hostx64" / "x64"
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    environment.update({
        "ComSpec": str(system32 / "cmd.exe"),
        "PATH": os.pathsep.join((str(tool_bin), str(system32))),
        "SystemRoot": os.environ["SystemRoot"],
        "WINDIR": os.environ["WINDIR"],
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
        "VSCMD_SKIP_SENDTELEMETRY": "1",
    })
    return environment


def same_resolved_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=True))) == os.path.normcase(str(right.resolve(strict=True)))


def resolved_search_roots(value: str, role: str) -> list[Path]:
    raw_roots = value.split(os.pathsep)
    require(bool(raw_roots) and all(raw_roots), f"{role} contains an empty search root")
    roots = [Path(item).resolve(strict=True) for item in raw_roots]
    normalized = [os.path.normcase(str(item)) for item in roots]
    require(len(normalized) == len(set(normalized)), f"{role} contains duplicate search roots")
    return roots


def verify_compile_environment_projection(
    environment: dict[str, str], msvc_version: str, sdk_version: str,
) -> dict[str, Any]:
    msvc_root = Path(environment["VCToolsInstallDir"]).resolve(strict=True)
    sdk_root = Path(environment["WindowsSdkDir"]).resolve(strict=True)
    universal_root = Path(environment["UniversalCRTSdkDir"]).resolve(strict=True)
    require(environment["VCToolsVersion"].rstrip("\\/") == msvc_version, "compile environment MSVC version mismatch")
    require(
        environment["WindowsSDKVersion"].rstrip("\\/") == sdk_version
        and environment["UCRTVersion"].rstrip("\\/") == sdk_version,
        "compile environment SDK version mismatch",
    )
    require(same_resolved_path(sdk_root, universal_root), "compile environment SDK/UCRT roots differ")
    require(msvc_root.name.casefold() == msvc_version.casefold(), "compile environment MSVC root is not version-qualified")

    expected_include = [
        ("msvc_headers", msvc_root / "include"),
        *[("sdk_headers", sdk_root / "Include" / sdk_version / name) for name in ("ucrt", "um", "shared", "winrt", "cppwinrt")],
    ]
    expected_lib = [
        ("msvc_libraries_x64", msvc_root / "lib" / "x64"),
        ("sdk_ucrt_libraries_x64", sdk_root / "Lib" / sdk_version / "ucrt" / "x64"),
        ("sdk_um_libraries_x64", sdk_root / "Lib" / sdk_version / "um" / "x64"),
    ]
    include_roots = resolved_search_roots(environment.get("INCLUDE", ""), "INCLUDE")
    lib_roots = resolved_search_roots(environment.get("LIB", ""), "LIB")
    require(
        len(include_roots) == len(expected_include)
        and all(same_resolved_path(actual, expected) for actual, (_, expected) in zip(include_roots, expected_include)),
        "compile environment INCLUDE roots differ from locked projection",
    )
    require(
        len(lib_roots) == len(expected_lib)
        and all(same_resolved_path(actual, expected) for actual, (_, expected) in zip(lib_roots, expected_lib)),
        "compile environment LIB roots differ from locked x64 projection",
    )
    require(not environment.get("LIBPATH"), "compile environment LIBPATH must be cleared")
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


def independently_rebuild_probe(
    source: Path, vcvarsall: Path, msvc_version: str, sdk_version: str, candidate_lock: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="localcat-entry-validator-") as temporary:
        root = Path(temporary)
        environment = tool_environment(vcvarsall, msvc_version, sdk_version, root)
        tool_bin = Path(environment["VCToolsInstallDir"]) / "bin" / "Hostx64" / "x64"
        compiler = (tool_bin / "cl.exe").resolve(strict=True)
        linker = (tool_bin / "link.exe").resolve(strict=True)
        expected_tools = candidate_lock["toolchain"]["msvc"]["tools"]
        msvc_root = Path(environment["VCToolsInstallDir"]).resolve(strict=True)
        sdk_root = Path(environment["WindowsSdkDir"]).resolve(strict=True)
        locked_msvc = candidate_lock["toolchain"]["msvc"]
        locked_sdk = candidate_lock["toolchain"]["windows_sdk"]
        aggregates = {
            "compiler_bin": aggregate_base_runtime(msvc_root / "bin/Hostx64/x64", locked_msvc["compiler_bin"]["role"]),
            "msvc_headers": aggregate_base_runtime(msvc_root / "include", locked_msvc["headers"]["role"]),
            "msvc_libraries": aggregate_base_runtime(msvc_root / "lib/x64", locked_msvc["libraries"]["role"]),
            "sdk_binary_inputs": aggregate_base_runtime(sdk_root / "bin" / sdk_version / "x64", locked_sdk["binary_inputs"]["role"]),
            "sdk_headers": aggregate_base_runtime(sdk_root / "Include" / sdk_version, locked_sdk["headers"]["role"]),
            "sdk_um_libraries": aggregate_base_runtime(sdk_root / "Lib" / sdk_version / "um/x64", locked_sdk["libraries"]["role"]),
            "sdk_ucrt_libraries": aggregate_base_runtime(sdk_root / "Lib" / sdk_version / "ucrt/x64", locked_sdk["ucrt_libraries"]["role"]),
        }
        expected_aggregates = {
            "compiler_bin": locked_msvc["compiler_bin"],
            "msvc_headers": locked_msvc["headers"],
            "msvc_libraries": locked_msvc["libraries"],
            "sdk_binary_inputs": locked_sdk["binary_inputs"],
            "sdk_headers": locked_sdk["headers"],
            "sdk_um_libraries": locked_sdk["libraries"],
            "sdk_ucrt_libraries": locked_sdk["ucrt_libraries"],
        }
        require(aggregates == expected_aggregates, "validator MSVC/SDK aggregates differ from lock")
        environment, compile_environment_projection = finalize_compile_environment(
            environment, msvc_version, sdk_version,
        )
        require(digest_file(compiler) == expected_tools["cl"]["sha256"], "validator cl differs from lock")
        require(digest_file(linker) == expected_tools["link"]["sha256"], "validator link differs from lock")
        command = [
            str(compiler), "/nologo", "/W4", "/WX", "/O2", "/MT", "/guard:cf", "/Brepro",
            f"/Fo{root / 'probe.obj'}", f"/Fe{root / 'probe.exe'}", str(source), "/link", "/Brepro",
            "/guard:cf", "/dynamicbase", "/highentropyva", "/nxcompat", "/machine:x64",
        ]
        result = subprocess.run(command, cwd=root, env=environment, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        require(result.returncode == 0, f"independent probe build failed: {result.stderr!r}")
        return (
            digest_file(root / "probe.exe"), digest_file(compiler), digest_file(linker),
            aggregates, compile_environment_projection,
        )


def refresh_self_digests(evidence: dict[str, Any]) -> None:
    projection = {
        key: value
        for key, value in evidence.items()
        if key not in {"raw_artifacts", "evidence_digest", "replay_projection_digest"}
    }
    evidence["replay_projection_digest"] = hashlib.sha256(canonical_bytes(projection)).hexdigest()
    payload = dict(evidence)
    payload.pop("evidence_digest", None)
    evidence["evidence_digest"] = hashlib.sha256(canonical_bytes(payload)).hexdigest()


def recursive_validator_command(arguments: argparse.Namespace, fixture_root: Path) -> list[str]:
    return [
        sys.executable, "-B", str(Path(__file__).resolve(strict=True)),
        "--repository-root", str(arguments.repository_root.resolve(strict=True)),
        "--evidence", str(fixture_root / "custom-entry-no-go.json"),
        "--raw-artifact-root", str(fixture_root),
        "--candidate-lock", str(arguments.candidate_lock.resolve(strict=True)),
        "--matrix-contract", str(arguments.matrix_contract.resolve(strict=True)),
        "--schema", str(arguments.schema.resolve(strict=True)),
        "--cpython-root", str(arguments.cpython_root.resolve(strict=True)),
        "--pyinstaller-source", str(arguments.pyinstaller_source.resolve(strict=True)),
        "--vcvarsall", str(arguments.vcvarsall.resolve(strict=True)),
        "--msvc-version", arguments.msvc_version,
        "--windows-sdk-version", arguments.windows_sdk_version,
        "--dumpbin", str(arguments.dumpbin.resolve(strict=True)),
    ]


def recursive_producer_command(
    arguments: argparse.Namespace, candidate_lock: Path, matrix_contract: Path, output: Path,
) -> list[str]:
    return [
        sys.executable, "-B", str((arguments.repository_root / "tools/audit_windows_frozen_custom_entry.py").resolve(strict=True)),
        "--repository-root", str(arguments.repository_root.resolve(strict=True)),
        "--candidate-lock", str(candidate_lock.resolve(strict=True)),
        "--matrix-contract", str(matrix_contract.resolve(strict=True)),
        "--pyinstaller-source", str(arguments.pyinstaller_source.resolve(strict=True)),
        "--cpython-root", str(arguments.cpython_root.resolve(strict=True)),
        "--vcvarsall", str(arguments.vcvarsall.resolve(strict=True)),
        "--msvc-version", arguments.msvc_version,
        "--windows-sdk-version", arguments.windows_sdk_version,
        "--dumpbin", str(arguments.dumpbin.resolve(strict=True)),
        "--output", str(output),
    ]


def run_negative_self_tests(arguments: argparse.Namespace, raw_root: Path) -> list[dict[str, Any]]:
    results = []
    with tempfile.TemporaryDirectory(prefix="localcat-entry-negative-") as temporary:
        parent = Path(temporary)

        producer_lock_path = parent / "duplicate-candidate-lock.json"
        original_lock = arguments.candidate_lock.resolve(strict=True).read_bytes()
        require(original_lock.startswith(b"{"), "candidate lock is not an object byte stream")
        producer_lock_path.write_bytes(b'{"status":"MATERIALIZED",' + original_lock[1:])
        producer_lock_run = subprocess.run(
            recursive_producer_command(
                arguments, producer_lock_path, arguments.matrix_contract, parent / "producer-lock-output",
            ),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        producer_lock_output = (producer_lock_run.stdout + producer_lock_run.stderr).decode("utf-8", "strict")
        require(
            producer_lock_run.returncode != 0 and "duplicate JSON key" in producer_lock_output,
            "producer duplicate-key candidate-lock fixture was accepted",
        )
        results.append({
            "fixture": "producer-candidate-lock-duplicate-key", "status": "REJECTED",
            "diagnostic": "duplicate JSON key",
        })

        producer_matrix_path = parent / "duplicate-matrix.json"
        original_matrix = arguments.matrix_contract.resolve(strict=True).read_bytes()
        require(original_matrix.startswith(b"{"), "matrix contract is not an object byte stream")
        producer_matrix_path.write_bytes(b'{"schema":"localcat.windows-frozen-custom-entry-matrix.v1",' + original_matrix[1:])
        producer_matrix_run = subprocess.run(
            recursive_producer_command(
                arguments, arguments.candidate_lock, producer_matrix_path, parent / "producer-matrix-output",
            ),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        producer_matrix_output = (producer_matrix_run.stdout + producer_matrix_run.stderr).decode("utf-8", "strict")
        require(
            producer_matrix_run.returncode != 0 and "duplicate JSON key" in producer_matrix_output,
            "producer duplicate-key matrix fixture was accepted",
        )
        results.append({
            "fixture": "producer-matrix-duplicate-key", "status": "REJECTED",
            "diagnostic": "duplicate JSON key",
        })

        environment = tool_environment(
            arguments.vcvarsall.resolve(strict=True), arguments.msvc_version,
            arguments.windows_sdk_version, parent,
        )
        environment, _ = finalize_compile_environment(
            environment, arguments.msvc_version, arguments.windows_sdk_version,
        )
        rogue_include = parent / "rogue-include"
        rogue_include.mkdir()
        environment["INCLUDE"] += os.pathsep + str(rogue_include)
        try:
            verify_compile_environment_projection(
                environment, arguments.msvc_version, arguments.windows_sdk_version,
            )
        except SystemExit as exc:
            require(
                "INCLUDE roots differ from locked projection" in str(exc),
                "redirected compile-environment fixture failed for an unexpected reason",
            )
        else:
            raise SystemExit("redirected compile-environment fixture was accepted")
        results.append({
            "fixture": "compile-environment-out-of-scope-include", "status": "REJECTED",
            "diagnostic": "compile search-root mismatch",
        })

        duplicate_root = parent / "duplicate-key"
        shutil.copytree(raw_root, duplicate_root)
        duplicate_path = duplicate_root / "custom-entry-no-go.json"
        original_bytes = duplicate_path.read_bytes()
        require(original_bytes.startswith(b"{"), "evidence is not an object byte stream")
        duplicate_path.write_bytes(b'{"status":"GO",' + original_bytes[1:])
        duplicate_run = subprocess.run(
            recursive_validator_command(arguments, duplicate_root),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        duplicate_output = (duplicate_run.stdout + duplicate_run.stderr).decode("utf-8", "strict")
        require(duplicate_run.returncode != 0 and "duplicate JSON key" in duplicate_output, "duplicate-key negative fixture was accepted")
        results.append({"fixture": "duplicate-key-last-wins", "status": "REJECTED", "diagnostic": "duplicate JSON key"})

        schema_root = parent / "schema-invalid"
        shutil.copytree(raw_root, schema_root)
        schema_path = schema_root / "custom-entry-no-go.json"
        schema_evidence = load_object(schema_path, "schema-invalid fixture source")
        schema_evidence["repository_commit"] = "not-a-commit"
        refresh_self_digests(schema_evidence)
        schema_path.write_bytes(canonical_bytes(schema_evidence))
        schema_run = subprocess.run(
            recursive_validator_command(arguments, schema_root),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        schema_output = (schema_run.stdout + schema_run.stderr).decode("utf-8", "strict")
        require(schema_run.returncode != 0 and "schema pattern failure" in schema_output, "schema-invalid negative fixture was accepted")
        results.append({"fixture": "schema-invalid-self-consistent", "status": "REJECTED", "diagnostic": "schema pattern failure"})

        tcb_root = parent / "producer-tcb-tamper"
        shutil.copytree(raw_root, tcb_root)
        tcb_path = tcb_root / "custom-entry-no-go.json"
        tcb_evidence = load_object(tcb_path, "producer-TCB fixture source")
        tcb_evidence["evidence_producer"]["pefile"]["module_sha256"] = "0" * 64
        refresh_self_digests(tcb_evidence)
        tcb_path.write_bytes(canonical_bytes(tcb_evidence))
        tcb_run = subprocess.run(
            recursive_validator_command(arguments, tcb_root),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tcb_output = (tcb_run.stdout + tcb_run.stderr).decode("utf-8", "strict")
        require(
            tcb_run.returncode != 0 and "evidence producer Python/base runtime/pefile differs" in tcb_output,
            "producer-TCB negative fixture was accepted",
        )
        results.append({"fixture": "producer-tcb-self-consistent", "status": "REJECTED", "diagnostic": "producer identity mismatch"})

        toolchain_root = parent / "toolchain-aggregate-tamper"
        shutil.copytree(raw_root, toolchain_root)
        toolchain_path = toolchain_root / "custom-entry-no-go.json"
        toolchain_evidence = load_object(toolchain_path, "toolchain fixture source")
        toolchain_evidence["inputs"]["toolchain_aggregates"]["compiler_bin"]["sha256"] = "1" * 64
        refresh_self_digests(toolchain_evidence)
        toolchain_path.write_bytes(canonical_bytes(toolchain_evidence))
        toolchain_run = subprocess.run(
            recursive_validator_command(arguments, toolchain_root),
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        toolchain_output = (toolchain_run.stdout + toolchain_run.stderr).decode("utf-8", "strict")
        require(
            toolchain_run.returncode != 0 and "current authority input facts differ" in toolchain_output,
            "toolchain-aggregate negative fixture was accepted",
        )
        results.append({"fixture": "toolchain-aggregate-self-consistent", "status": "REJECTED", "diagnostic": "authority input mismatch"})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--raw-artifact-root", type=Path, required=True)
    parser.add_argument("--candidate-lock", type=Path, required=True)
    parser.add_argument("--matrix-contract", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--cpython-root", type=Path, required=True)
    parser.add_argument("--pyinstaller-source", type=Path, required=True)
    parser.add_argument("--vcvarsall", type=Path, required=True)
    parser.add_argument("--msvc-version", required=True)
    parser.add_argument("--windows-sdk-version", required=True)
    parser.add_argument("--dumpbin", type=Path, required=True)
    parser.add_argument("--run-negative-self-tests", action="store_true")
    arguments = parser.parse_args()

    repository_root = arguments.repository_root.resolve(strict=True)
    raw_root = arguments.raw_artifact_root.resolve(strict=True)
    evidence_path = arguments.evidence.resolve(strict=True)
    candidate_lock_path = arguments.candidate_lock.resolve(strict=True)
    matrix_path = arguments.matrix_contract.resolve(strict=True)
    schema_path = arguments.schema.resolve(strict=True)
    cpython_root = arguments.cpython_root.resolve(strict=True)
    pyinstaller_source = arguments.pyinstaller_source.resolve(strict=True)
    vcvarsall = arguments.vcvarsall.resolve(strict=True)
    dumpbin = arguments.dumpbin.resolve(strict=True)
    require(repository_root.is_dir() and raw_root.is_dir(), "repository/raw root must be directories")
    require(evidence_path.parent == raw_root, "evidence must be an immediate raw-artifact sibling")
    require(evidence_path.name == "custom-entry-no-go.json", "unexpected evidence filename")

    evidence = load_object(evidence_path, "evidence")
    candidate_lock = load_object(candidate_lock_path, "candidate lock")
    validate_legacy_target(candidate_lock)
    matrix_contract = load_object(matrix_path, "matrix contract")
    schema = load_object(schema_path, "schema")
    validate_schema_language(schema)
    apply_schema(evidence, schema)
    exact_keys(evidence, TOP_LEVEL_KEYS, "evidence")
    require(schema.get("$id") == EVIDENCE_SCHEMA, "unexpected evidence schema document")
    require(schema.get("additionalProperties") is False, "schema is not closed")
    require(set(schema.get("required", ())) == TOP_LEVEL_KEYS, "schema required fields differ")
    require(set(schema.get("properties", {})) == TOP_LEVEL_KEYS, "schema top-level properties differ")
    require(evidence.get("schema") == EVIDENCE_SCHEMA, "unexpected evidence schema")
    require(evidence.get("status") == "NO_GO", "evidence is not terminal NO_GO")
    require(matrix_contract.get("schema") == MATRIX_SCHEMA, "unexpected matrix contract schema")
    require(candidate_lock.get("status") == "MATERIALIZED", "candidate input is not materialized")

    parser_tcb = establish_parser_tcb(candidate_lock)
    require(evidence["evidence_producer"] == parser_tcb, "evidence producer Python/base runtime/pefile differs")

    exact_keys(evidence["host"], {"system", "machine", "python", "windows"}, "host")
    exact_keys(
        evidence["source_identity"],
        {"base_commit", "files", "scoped_input_aggregate", "dirty_scope", "orchestrator_clean"},
        "source_identity",
    )
    exact_keys(evidence["inputs"], {
        "audit_source_sha256", "validator_source_sha256", "probe_source_sha256",
        "matrix_contract_sha256", "schema_sha256", "python314_dll_sha256",
        "vcruntime140_dll_sha256", "pyinstaller_source_scan", "cl_sha256",
        "link_sha256", "dumpbin_sha256", "toolchain_aggregates", "compile_environment_projection",
    }, "inputs")
    exact_keys(evidence["probe"], {
        "compile_arguments", "compile_exit", "run_exit", "cwd_role", "probe_exe_sha256",
        "probe_pe", "trace", "diagnostic_path_reopen_modules", "limitations",
    }, "probe")
    exact_keys(evidence["raw_artifacts"], set(RAW_FILES), "raw_artifacts")
    exact_keys(evidence["target_contract"], {"pre_authority_external_calls", "declared_external_import_names"}, "target_contract")
    exact_keys(evidence["observed_system_provider"], {"windows", "module", "path", "identity", "sha256", "file_version"}, "provider")
    exact_keys(evidence["static_python_loader_surface"], {"imports", "disassembly_callsites", "interpretation"}, "loader surface")
    exact_keys(evidence["blocker"], {
        "code", "observed_fact", "static_fact", "stock_path_fact", "required_reapproval_delta",
        "preserved_implementation_boundary", "decision",
    }, "blocker")
    exact_keys(evidence["matrix_contract"], {"schema", "completion_rule", "no_go_rule"}, "matrix summary")

    recorded_digest = evidence.get("evidence_digest")
    payload = dict(evidence)
    payload.pop("evidence_digest", None)
    actual_digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    require(recorded_digest == actual_digest, "evidence digest mismatch")
    replay_projection = {
        key: value
        for key, value in evidence.items()
        if key not in {"raw_artifacts", "evidence_digest", "replay_projection_digest"}
    }
    replay_digest = hashlib.sha256(canonical_bytes(replay_projection)).hexdigest()
    require(evidence.get("replay_projection_digest") == replay_digest, "replay projection digest mismatch")

    source_identity = derive_scoped_identity(repository_root)
    require(evidence["source_identity"] == source_identity, "scoped source/working-diff identity mismatch")
    require(evidence["repository_commit"] == source_identity["base_commit"], "base commit mismatch")
    require(evidence["candidate_lock_sha256"] == digest_file(candidate_lock_path), "candidate lock digest mismatch")
    require(evidence["candidate_input_digest"] == candidate_lock["candidate_input_digest"], "candidate input digest mismatch")
    require(evidence["host"] == {
        "system": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
        "windows": current_windows_build(),
    }, "host identity mismatch")

    python_dll = (cpython_root / "python314.dll").resolve(strict=True)
    runtime_dll = (cpython_root / "vcruntime140.dll").resolve(strict=True)
    runtime_members = candidate_lock["runtime"]["cpython"]["native_closure"]["local_members"]
    runtime_member = next(item for item in runtime_members if item["name"].casefold() == "vcruntime140.dll")
    require(digest_file(python_dll) == candidate_lock["runtime"]["cpython"]["dll"]["sha256"], "current Python DLL differs from lock")
    require(digest_file(runtime_dll) == runtime_member["sha256"], "current VCRUNTIME differs from lock")
    expected_toolchain = candidate_lock["toolchain"]
    require(arguments.msvc_version == expected_toolchain["msvc"]["toolset_version"], "MSVC version differs from lock")
    require(arguments.windows_sdk_version == expected_toolchain["windows_sdk"]["version"], "SDK version differs from lock")
    source_path = (repository_root / "tools/probe_windows_python314_preauthority.c").resolve(strict=True)
    audit_path = (repository_root / "tools/audit_windows_frozen_custom_entry.py").resolve(strict=True)
    validator_path = (repository_root / "tools/validate_windows_frozen_custom_entry_no_go.py").resolve(strict=True)
    rebuilt_exe, compiler_digest, linker_digest, toolchain_aggregates, compile_environment_projection = independently_rebuild_probe(
        source_path, vcvarsall, arguments.msvc_version, arguments.windows_sdk_version, candidate_lock,
    )
    current_inputs = {
        "audit_source_sha256": digest_file(audit_path),
        "validator_source_sha256": digest_file(validator_path),
        "probe_source_sha256": digest_file(source_path),
        "matrix_contract_sha256": digest_file(matrix_path),
        "schema_sha256": digest_file(schema_path),
        "python314_dll_sha256": digest_file(python_dll),
        "vcruntime140_dll_sha256": digest_file(runtime_dll),
        "pyinstaller_source_scan": derive_source_scan(pyinstaller_source),
        "cl_sha256": compiler_digest,
        "link_sha256": linker_digest,
        "dumpbin_sha256": digest_file(dumpbin),
        "toolchain_aggregates": toolchain_aggregates,
        "compile_environment_projection": compile_environment_projection,
    }
    require(evidence["inputs"] == current_inputs, "current authority input facts differ from evidence")
    require(current_inputs["dumpbin_sha256"] == expected_toolchain["msvc"]["tools"]["dumpbin"]["sha256"], "dumpbin differs from lock")

    for name in RAW_FILES:
        path = (raw_root / name).resolve(strict=True)
        require(path.parent == raw_root and path.is_file(), f"raw sibling invalid: {name}")
        require(evidence["raw_artifacts"][name] == digest_file(path), f"raw sibling digest mismatch: {name}")
    raw_probe = (raw_root / "probe.exe").resolve(strict=True)
    require(digest_file(raw_probe) == rebuilt_exe, "raw probe PE does not reproduce from current pinned source/toolchain")
    require(evidence["probe"]["probe_exe_sha256"] == rebuilt_exe, "probe PE evidence digest mismatch")
    require(evidence["probe"]["probe_pe"] == derive_pe_imports(raw_probe), "probe PE/IAT facts mismatch")
    expected_compile_arguments = [
        "cl.exe", "/nologo", "/W4", "/WX", "/O2", "/MT", "/guard:cf", "/Brepro",
        "/Fo<artifact>/probe.obj", "/Fe<artifact>/probe.exe",
        "<repository>/tools/probe_windows_python314_preauthority.c", "/link", "/Brepro",
        "/guard:cf", "/dynamicbase", "/highentropyva", "/nxcompat", "/machine:x64",
    ]
    require(evidence["probe"]["compile_arguments"] == expected_compile_arguments, "probe compile arguments mismatch")
    require(evidence["probe"]["compile_exit"] == 0 and evidence["probe"]["run_exit"] == 0, "probe did not succeed")
    require(
        evidence["probe"]["cwd_role"] == "fresh child of ignored artifact; not candidate NONREPO_CWD proof",
        "probe cwd role overclaims candidate evidence",
    )
    require(evidence["probe"]["limitations"] == [
        "The loader notification callback records into preallocated memory and performs no I/O or allocation.",
        "Notification begins after process entry and cannot enumerate already-loaded or failed loads.",
        "Observed load notifications are diagnostic facts, not pre-authority proof or closure PASS evidence.",
    ], "probe limitations differ")

    raw_stdout = (raw_root / "probe.stdout.log").read_bytes()
    raw_stderr = (raw_root / "probe.stderr.log").read_bytes()
    replay = subprocess.run(
        [str(raw_probe), str(cpython_root)], cwd=raw_root / "nonrepo-cwd",
        env={"SystemRoot": os.environ["SystemRoot"], "WINDIR": os.environ["WINDIR"]},
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    require(replay.returncode == 0, "independent probe replay failed")
    require(replay.stdout == raw_stdout and replay.stderr == raw_stderr, "raw probe trace is not replayable")
    trace, module_records = derive_probe_records(raw_stderr)
    require(evidence["probe"]["trace"] == trace, "probe trace evidence differs from raw trace")

    diagnostic_path_reopen = {}
    expected_module_paths = {"python": python_dll, "vcruntime": runtime_dll}
    for role, record in module_records.items():
        recorded_path = Path(record["path"]).resolve(strict=True)
        require(os.path.normcase(str(recorded_path)) == os.path.normcase(str(expected_module_paths[role])), f"actual {role} path differs")
        fact = current_binary_fact(recorded_path)
        require(fact["identity"] == record["identity"], f"actual {role} handle identity differs from raw record")
        diagnostic_path_reopen[role] = fact
    require(
        evidence["probe"]["diagnostic_path_reopen_modules"] == diagnostic_path_reopen,
        "diagnostic module path-reopen facts differ",
    )

    target_contract = {
        "pre_authority_external_calls": candidate_lock["entry_contract"]["dynamic_loader"]["pre_authority_external_calls"],
        "declared_external_import_names": sorted(
            name.upper() for name in candidate_lock["runtime"]["cpython"]["native_closure"]["external_import_names"]
        ),
    }
    require(evidence["target_contract"] == target_contract, "target contract projection differs from candidate lock")
    declared = set(target_contract["declared_external_import_names"])
    unexpected = [item for item in trace if item["stage"] == 3 and item["module"] not in declared]
    require(evidence["observed_pre_e10_dynamic_modules"] == unexpected, "observed dynamic modules differ from raw trace")
    require(any(item["module"] == "BCRYPTPRIMITIVES.DLL" and item["observed_path_role"] == "SYSTEM32" for item in unexpected), "expected System32 provider observation missing")
    provider_item = next(item for item in unexpected if item["module"] == "BCRYPTPRIMITIVES.DLL")
    provider = {"windows": current_windows_build(), "module": provider_item["module"], **current_binary_fact(Path(provider_item["path"]))}
    require(evidence["observed_system_provider"] == provider, "observed System32 provider identity/version/hash mismatch")

    loader_surface, fresh_dumpbin = derive_loader_surface(python_dll, dumpbin)
    require(fresh_dumpbin == (raw_root / "python314.dumpbin.log").read_bytes(), "raw dumpbin output is not independently reproducible")
    require(evidence["static_python_loader_surface"] == loader_surface, "static Python loader surface differs")
    require(evidence["pristine_pyinstaller_path"] == derive_pristine_facts(pyinstaller_source, candidate_lock), "pristine PyInstaller facts differ")

    expected_ids = matrix_contract.get("ordered_assertion_ids")
    matrix = evidence.get("matrix")
    require(isinstance(expected_ids, list) and len(expected_ids) == 14, "matrix contract is not fourteen assertions")
    require(isinstance(matrix, list) and len(matrix) == 14, "matrix is not fourteen rows")
    expected_matrix = []
    for assertion_id in expected_ids:
        expected_matrix.append({
            "id": assertion_id,
            "status": "FAIL" if assertion_id == BLOCKING_ASSERTION else "BLOCKED_NOT_RUN",
            "detail": (
                "CPython initialization loaded an undeclared module before E10; exact Task 1.5 call surface cannot be realized"
                if assertion_id == BLOCKING_ASSERTION
                else "candidate construction stopped at the first exact-contract failure"
            ),
        })
    require(matrix == expected_matrix, "machine matrix differs from terminal stop rule")
    require(evidence["matrix_contract"] == {
        "schema": matrix_contract["schema"],
        "completion_rule": matrix_contract["completion_rule"],
        "no_go_rule": matrix_contract["no_go_rule"],
    }, "matrix summary differs from current contract")
    require(evidence["blocker"] == {
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
        "required_reapproval_delta": REAPPROVAL_DELTA,
        "preserved_implementation_boundary": PRESERVED_BOUNDARY,
        "decision": "return to reapproval; this producer does not edit the candidate contract or grant authority",
    }, "blocker/reapproval boundary differs")
    negative_tests = run_negative_self_tests(arguments, raw_root) if arguments.run_negative_self_tests else []
    print(json.dumps({
        "status": "VALID_NO_GO",
        "evidence_digest": recorded_digest,
        "replay_projection_digest": replay_digest,
        "probe_exe_sha256": rebuilt_exe,
        "negative_tests": negative_tests,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
