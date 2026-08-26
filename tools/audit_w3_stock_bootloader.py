"""Audit stock PyInstaller against the LocalCAT W3 native bootstrap gate.

This tool is intentionally a negative feasibility audit.  It records static PE
and pinned-source facts without claiming that they prove runtime DLL identity,
rooted containment, or executed Python source.  A stock bootloader that lacks
those proofs produces ``NO_GO`` and a non-zero exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tarfile
from typing import Any


SCHEMA_ID = "localcat.windows-w3-stock-bootloader-audit.v1"
EXPECTED_PYINSTALLER_VERSION = "6.22.2"
EXPECTED_PYTHON_SERIES = "3.14."
EXPECTED_SDIST_SHA256 = "89b65a3ad07d9dd5832253e37bc45f31872d10d7f9d5c9fd0fdd6088a83829dd"
EXPECTED_STOCK_RUNW_SHA256 = "87b0c589906a5d690c26c602bf2ee7e43b3eab55574bf72887a8ca07fbb2dba0"
EXPECTED_PYTHON_DLL_SHA256 = "0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700"
EXPECTED_BASELINE_EXE_SHA256 = "20fccf54845e8b171928d5ed5f85efd716c5dfeb49508c0dcaf2ca0f163a5ac7"
EXPECTED_BASELINE_COMMIT = "b925b803d81001f55dea46ace8b26159ca82db19"
EXPECTED_BASELINE_RUN_ID = "20260826-task1-1-fresh-01"
EXPECTED_BASELINE_MATRIX_SHA256 = "1b31a9ce178fb0a7061417ddc433d1185d7a4f5af35331f52f9c719946ce5e08"
EXPECTED_BASELINE_INVENTORY_SHA256 = "1678bb7c05502f28e97345fdac6b942a0f52e12edb73d6d67699ee25f8fedfeb"
EXPECTED_BASELINE_ANALYSIS_SHA256 = "32865a86d1e1265af5be279253de6e326e9edc9e930954d91825b4048eb97e79"
EXPECTED_BASELINE_CHECKSUMS_SHA256 = "6498ae6e471dd7e8316558385487698f915576a0ee568f16599975cb323c887b"
EXPECTED_STOCK_TEXT_SHA256 = "6fcaee6735d961a7ded1c1d8fd789ef6d4a8ac7a5c551b04afbad8e32c1da4e3"
EXPECTED_SOURCE_SCAN_SHA256 = "e9815b1301b5aaa706b44b55ee49c2348511b8d8c92f1dcc86a2b41602cb4826"
EXPECTED_SOURCE_SCAN_COUNT = 51

_CHECKSUM_LINE_RE = re.compile(r"([0-9A-Fa-f]{64}) [ *](.+)\Z")

SOURCE_FILES = (
    "bootloader/src/pyi_main.c",
    "bootloader/src/pyi_launch.c",
    "bootloader/src/pyi_dylib_python.c",
    "bootloader/src/pyi_utils_win32_low_level.c",
    "PyInstaller/loader/pyiboot01_bootstrap.py",
    "PyInstaller/loader/pyimod03_ctypes.py",
)

EXPECTED_SOURCE_SHA256 = {
    "bootloader/src/pyi_main.c": "19616e0a5982da96a27861bb9baca84c1b05d63625aea098962bfea85e6db43b",
    "bootloader/src/pyi_launch.c": "50416af7e74b12a56f67cd360bfa46ed71770ac43629001eaa6d73c4e196880e",
    "bootloader/src/pyi_dylib_python.c": "900996eac30595b5a474bd2ec3601ae0c5d1389f394979bbfca7f7a9313e67e7",
    "bootloader/src/pyi_utils_win32_low_level.c": "ee6a6a0fef89d8c81af93185d97b116ad15618f2ef35c1c1cf78e30fdda5ff12",
    "PyInstaller/loader/pyiboot01_bootstrap.py": "6773bb364344a01f176133f8624ddda9e8f992052eaddad32609b1e90399ef1f",
    "PyInstaller/loader/pyimod03_ctypes.py": "a93c2ec1677cc5d77fa4c000136b9da36f61388f9536f2b1a66193b329d1f312",
}

SOURCE_MARKERS = {
    "application_home_from_executable_path": (
        "bootloader/src/pyi_main.c",
        "pyi_path_dirname(executable_dir, pyi_ctx->executable_filename);",
    ),
    "set_dll_directory_from_application_home": (
        "bootloader/src/pyi_main.c",
        "SetDllDirectoryW(dllpath_w);",
    ),
    "launch_execute": (
        "bootloader/src/pyi_main.c",
        "ret = pyi_launch_execute(pyi_ctx);",
    ),
    "python_dll_load": (
        "bootloader/src/pyi_launch.c",
        "pyi_ctx->dylib_python = pyi_dylib_python_load(",
    ),
    "python_interpreter_start": (
        "bootloader/src/pyi_launch.c",
        "if (pyi_python_start_interpreter(pyi_ctx))",
    ),
    "python_bootstrap_import": (
        "bootloader/src/pyi_launch.c",
        "if (pyi_python_import_modules(pyi_ctx))",
    ),
    "python_dll_load_altered_search_path": (
        "bootloader/src/pyi_dylib_python.c",
        "LoadLibraryExW(dll_fullpath, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);",
    ),
    "meipass_bootstrap": (
        "PyInstaller/loader/pyiboot01_bootstrap.py",
        "sys._MEIPASS",
    ),
    "meipass_path_dynamic_search": (
        "PyInstaller/loader/pyimod03_ctypes.py",
        "search_dirs = [sys._MEIPASS] + os.environ['PATH'].split(os.pathsep)",
    ),
}

W3_NATIVE_TOKENS = (
    "SetDefaultDllDirectories",
    "AddDllDirectory",
    "GetFinalPathNameByHandleW",
    "GetFileInformationByHandleEx",
    "FILE_ID_INFO",
    "FILE_FLAG_OPEN_REPARSE_POINT",
    "PyCapsule_New",
    "TrustedSourceAuthority",
    "TrustedSourceLoader",
)

# This is deliberately only a conservative name screen.  Passing it would not
# prove actual loader resolution; W3 requires pre-load and post-load identity.
ENTRY_NAME_ALLOWLIST = frozenset(
    {"ADVAPI32.DLL", "GDI32.DLL", "KERNEL32.DLL", "USER32.DLL"}
)


class AuditInputError(ValueError):
    """The pinned audit inputs are absent or malformed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fact(role: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AuditInputError(f"missing {role}")
    return {
        "role": role,
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _line_number(text: str, marker: str) -> int | None:
    index = text.find(marker)
    if index < 0:
        return None
    return text.count("\n", 0, index) + 1


def audit_sources(source_root: Path) -> dict[str, Any]:
    texts: dict[str, str] = {}
    source_facts: list[dict[str, Any]] = []
    for relative_path in SOURCE_FILES:
        path = source_root / Path(relative_path)
        if not path.is_file():
            raise AuditInputError(f"missing pinned source file: {relative_path}")
        text = path.read_text(encoding="utf-8")
        texts[relative_path] = text
        actual_sha256 = sha256_file(path)
        source_facts.append(
            {
                "path": relative_path,
                "bytes": path.stat().st_size,
                "sha256": actual_sha256,
                "expected_sha256": EXPECTED_SOURCE_SHA256[relative_path],
                "pinned_hash_match": actual_sha256 == EXPECTED_SOURCE_SHA256[relative_path],
            }
        )

    markers: dict[str, dict[str, Any]] = {}
    for name, (relative_path, marker) in SOURCE_MARKERS.items():
        markers[name] = {
            "path": relative_path,
            "line": _line_number(texts[relative_path], marker),
        }

    scan_paths = sorted(
        (
            path
            for path in (source_root / "bootloader/src").rglob("*")
            if path.is_file() and path.suffix in {".c", ".h"}
        ),
        key=lambda path: path.relative_to(source_root).as_posix(),
    )
    scan_paths.extend(
        sorted(
            (source_root / "PyInstaller/loader").rglob("*.py"),
            key=lambda path: path.relative_to(source_root).as_posix(),
        )
    )
    scan_paths = sorted(
        set(scan_paths), key=lambda path: path.relative_to(source_root).as_posix()
    )
    scan_lines: list[str] = []
    scan_texts: list[str] = []
    for path in scan_paths:
        relative_path = path.relative_to(source_root).as_posix()
        scan_lines.append(f"{relative_path}={sha256_file(path)}\n")
        scan_texts.append(path.read_text(encoding="utf-8"))
    scan_digest = hashlib.sha256("".join(scan_lines).encode("utf-8")).hexdigest()
    searchable = "\n".join(scan_texts)
    token_counts = {token: searchable.count(token) for token in W3_NATIVE_TOKENS}

    main_markers = (
        markers["application_home_from_executable_path"]["line"],
        markers["set_dll_directory_from_application_home"]["line"],
        markers["launch_execute"]["line"],
    )
    launch_markers = (
        markers["python_dll_load"]["line"],
        markers["python_interpreter_start"]["line"],
        markers["python_bootstrap_import"]["line"],
    )
    marker_set_complete = all(item["line"] is not None for item in markers.values())
    stock_order_confirmed = (
        marker_set_complete
        and all(isinstance(line, int) for line in main_markers + launch_markers)
        and main_markers[0] < main_markers[1] < main_markers[2]
        and launch_markers[0] < launch_markers[1] < launch_markers[2]
    )

    return {
        "files": source_facts,
        "pinned_hashes_match": (
            all(item["pinned_hash_match"] for item in source_facts)
            and len(scan_paths) == EXPECTED_SOURCE_SCAN_COUNT
            and scan_digest == EXPECTED_SOURCE_SCAN_SHA256
        ),
        "source_scan": {
            "scope": ["bootloader/src/**/*.c", "bootloader/src/**/*.h", "PyInstaller/loader/**/*.py"],
            "count": len(scan_paths),
            "sha256": scan_digest,
            "expected_count": EXPECTED_SOURCE_SCAN_COUNT,
            "expected_sha256": EXPECTED_SOURCE_SCAN_SHA256,
        },
        "markers": markers,
        "token_counts": token_counts,
        "stock_order_confirmed": stock_order_confirmed,
        "interpretation": (
            "application-home path and SetDllDirectoryW precede the native call "
            "that loads Python; Python bootstrap/import hooks run only after the "
            "Python DLL is loaded and the interpreter starts"
        ),
    }


def audit_sdist_sources(source_archive: Path) -> dict[str, Any]:
    prefix = f"pyinstaller-{EXPECTED_PYINSTALLER_VERSION}/"
    selected: dict[str, tuple[int, str]] = {}
    try:
        archive = tarfile.open(source_archive, mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise AuditInputError("invalid PyInstaller sdist archive") from exc

    with archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if not name.startswith(prefix):
                continue
            relative = PurePosixPath(name[len(prefix) :])
            is_bootloader_source = (
                len(relative.parts) >= 3
                and relative.parts[:2] == ("bootloader", "src")
                and relative.suffix in {".c", ".h"}
            )
            is_loader_source = (
                len(relative.parts) >= 3
                and relative.parts[:2] == ("PyInstaller", "loader")
                and relative.suffix == ".py"
            )
            if not (is_bootloader_source or is_loader_source):
                continue
            relative_key = relative.as_posix()
            if relative_key in selected or not member.isfile():
                raise AuditInputError("duplicate or non-file member in pinned sdist source scan")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AuditInputError("unreadable member in pinned sdist source scan")
            data = extracted.read()
            selected[relative_key] = (len(data), hashlib.sha256(data).hexdigest())

    canonical = "".join(
        f"{relative_path}={selected[relative_path][1]}\n"
        for relative_path in sorted(selected)
    )
    return {
        "scope": ["bootloader/src/**/*.c", "bootloader/src/**/*.h", "PyInstaller/loader/**/*.py"],
        "count": len(selected),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "expected_count": EXPECTED_SOURCE_SCAN_COUNT,
        "expected_sha256": EXPECTED_SOURCE_SCAN_SHA256,
        "marker_file_hashes": {
            relative_path: selected.get(relative_path, (None, None))[1]
            for relative_path in SOURCE_FILES
        },
    }


def audit_pe(path: Path, role: str) -> dict[str, Any]:
    try:
        import pefile
    except ImportError as exc:  # pragma: no cover - PyInstaller depends on pefile
        raise AuditInputError("pefile is required for the PE audit") from exc

    if not path.is_file():
        raise AuditInputError(f"missing {role}")
    try:
        pe = pefile.PE(str(path), fast_load=False)
    except Exception as exc:  # pefile exposes several parse-error subclasses
        raise AuditInputError(f"invalid PE input: {role}") from exc

    def entries(attribute: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for entry in getattr(pe, attribute, []):
            dll = entry.dll.decode("ascii", errors="replace")
            symbols: list[str] = []
            for imported in entry.imports:
                if imported.name is None:
                    symbols.append(f"ordinal:{imported.ordinal}")
                else:
                    symbols.append(imported.name.decode("ascii", errors="replace"))
            output.append({"dll": dll, "symbols": sorted(symbols, key=str.casefold)})
        return sorted(output, key=lambda item: item["dll"].casefold())

    imports = entries("DIRECTORY_ENTRY_IMPORT")
    delay_imports = entries("DIRECTORY_ENTRY_DELAY_IMPORT")
    sections: list[dict[str, Any]] = []
    for section in pe.sections:
        name = section.Name.rstrip(b"\0").decode("ascii", errors="replace")
        data = section.get_data()
        sections.append(
            {
                "name": name,
                "raw_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    imported_names = {entry["dll"].upper() for entry in imports}
    non_allowlisted = sorted(imported_names - ENTRY_NAME_ALLOWLIST)
    fact = file_fact(role, path)
    fact.update(
        {
            "machine": f"0x{pe.FILE_HEADER.Machine:04x}",
            "subsystem": int(pe.OPTIONAL_HEADER.Subsystem),
            "imports": imports,
            "delay_imports": delay_imports,
            "sections": sections,
            "text_sha256": next(
                (section["sha256"] for section in sections if section["name"] == ".text"),
                None,
            ),
            "entry_name_allowlist": sorted(ENTRY_NAME_ALLOWLIST),
            "non_allowlisted_static_import_names": non_allowlisted,
        }
    )
    pe.close()
    return fact


def _load_json_object(path: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"invalid {role}") from exc
    if not isinstance(value, dict):
        raise AuditInputError(f"invalid {role}")
    return value


def _load_checksum_map(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AuditInputError("invalid baseline checksums") from exc
    checksums: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise AuditInputError("invalid baseline checksums")
        relative_path = PurePosixPath(match.group(2).replace("\\", "/"))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise AuditInputError("invalid baseline checksum key")
        key = relative_path.as_posix()
        if key in checksums:
            raise AuditInputError("duplicate baseline checksum key")
        checksums[key] = match.group(1).lower()
    return checksums


def audit_baseline_provenance(
    matrix_path: Path,
    inventory_path: Path,
    checksums_path: Path,
    analysis_path: Path,
    baseline_pe: dict[str, Any],
    stock_pe: dict[str, Any],
) -> dict[str, Any]:
    matrix_fact = file_fact("baseline_matrix", matrix_path)
    inventory_fact = file_fact("baseline_dist_inventory", inventory_path)
    checksums_fact = file_fact("baseline_checksums", checksums_path)
    analysis_fact = file_fact("baseline_analysis_toc", analysis_path)
    matrix = _load_json_object(matrix_path, "baseline matrix")
    inventory = _load_json_object(inventory_path, "baseline dist inventory")
    checksum_map = _load_checksum_map(checksums_path)

    files = inventory.get("files")
    if not isinstance(files, list):
        raise AuditInputError("invalid baseline dist inventory")
    inventory_entries = [item for item in files if isinstance(item, dict)]
    baseline_entries = [
        item for item in inventory_entries if item.get("path") == "LocalCAT-ui-mvp.exe"
    ]
    ctypes_binary_entries = [
        item for item in inventory_entries if item.get("path") == "_internal/_ctypes.pyd"
    ]
    try:
        analysis_text = analysis_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuditInputError("invalid baseline analysis toc") from exc
    ctypes_modules = [
        module_name
        for module_name in ("ctypes", "ctypes._endian", "ctypes._layout")
        if f"('{module_name}'," in analysis_text
    ]

    environment = matrix.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    relationships = {
        "matrix_digest_listed": checksum_map.get("matrix.json") == matrix_fact["sha256"],
        "inventory_digest_listed": (
            checksum_map.get("dist-clean-sha256.json") == inventory_fact["sha256"]
        ),
        "analysis_digest_listed": (
            checksum_map.get("build-clean/LocalCAT-ui-mvp/Analysis-00.toc")
            == analysis_fact["sha256"]
        ),
        "inventory_file_count_matches": (
            inventory.get("file_count") == len(inventory_entries) == len(files)
        ),
        "baseline_exe_inventory_match": (
            len(baseline_entries) == 1
            and baseline_entries[0].get("bytes") == baseline_pe["bytes"]
            and baseline_entries[0].get("sha256") == baseline_pe["sha256"]
        ),
        "stock_baseline_text_equal": (
            stock_pe["text_sha256"] is not None
            and stock_pe["text_sha256"] == baseline_pe["text_sha256"]
        ),
    }
    return {
        "matrix": matrix_fact,
        "dist_inventory": inventory_fact,
        "checksums": checksums_fact,
        "analysis_toc": analysis_fact,
        "repository_commit": matrix.get("repository_commit"),
        "run_id": matrix.get("run_id"),
        "schema": matrix.get("schema"),
        "environment": {
            "python": environment.get("python"),
            "pyinstaller": environment.get("pyinstaller"),
        },
        "inventory_file_count": inventory.get("file_count"),
        "ctypes_runtime_present": len(ctypes_binary_entries) == 1 and len(ctypes_modules) == 3,
        "ctypes_modules_in_analysis": ctypes_modules,
        "stock_text_sha256": stock_pe["text_sha256"],
        "baseline_text_sha256": baseline_pe["text_sha256"],
        "relationships": relationships,
    }


def validate_pinned_inputs(
    *,
    python_version: str,
    pyinstaller_version: str,
    source_archive_fact: dict[str, Any],
    python_dll_fact: dict[str, Any],
    source: dict[str, Any],
    sdist_source: dict[str, Any],
    stock_pe: dict[str, Any],
    baseline_pe: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    mismatches: list[str] = []

    def require(condition: bool, code: str) -> None:
        if not condition:
            mismatches.append(code)

    require(python_version.startswith(EXPECTED_PYTHON_SERIES), "PYTHON_VERSION")
    require(pyinstaller_version == EXPECTED_PYINSTALLER_VERSION, "PYINSTALLER_VERSION")
    require(source_archive_fact["sha256"] == EXPECTED_SDIST_SHA256, "SDIST_DIGEST")
    require(stock_pe["sha256"] == EXPECTED_STOCK_RUNW_SHA256, "STOCK_RUNW_DIGEST")
    require(python_dll_fact["sha256"] == EXPECTED_PYTHON_DLL_SHA256, "PYTHON_DLL_DIGEST")
    require(baseline_pe["sha256"] == EXPECTED_BASELINE_EXE_SHA256, "BASELINE_EXE_DIGEST")
    require(source["pinned_hashes_match"], "EXTRACTED_SOURCE_PIN")
    require(
        sdist_source["count"] == EXPECTED_SOURCE_SCAN_COUNT
        and sdist_source["sha256"] == EXPECTED_SOURCE_SCAN_SHA256,
        "SDIST_SOURCE_PIN",
    )
    require(
        sdist_source["count"] == source["source_scan"]["count"]
        and sdist_source["sha256"] == source["source_scan"]["sha256"],
        "SDIST_EXTRACTED_SOURCE_RELATION",
    )
    require(
        all(
            sdist_source["marker_file_hashes"].get(item["path"]) == item["sha256"]
            for item in source["files"]
        ),
        "SDIST_MARKER_SOURCE_RELATION",
    )
    require(
        baseline["matrix"]["sha256"] == EXPECTED_BASELINE_MATRIX_SHA256,
        "BASELINE_MATRIX_DIGEST",
    )
    require(
        baseline["dist_inventory"]["sha256"] == EXPECTED_BASELINE_INVENTORY_SHA256,
        "BASELINE_INVENTORY_DIGEST",
    )
    require(
        baseline["analysis_toc"]["sha256"] == EXPECTED_BASELINE_ANALYSIS_SHA256,
        "BASELINE_ANALYSIS_DIGEST",
    )
    require(
        baseline["checksums"]["sha256"] == EXPECTED_BASELINE_CHECKSUMS_SHA256,
        "BASELINE_CHECKSUMS_DIGEST",
    )
    require(baseline["schema"] == "localcat.windows.baseline-matrix.v1", "BASELINE_SCHEMA")
    require(baseline["repository_commit"] == EXPECTED_BASELINE_COMMIT, "BASELINE_COMMIT")
    require(baseline["run_id"] == EXPECTED_BASELINE_RUN_ID, "BASELINE_RUN_ID")
    require(baseline["environment"]["python"] == "CPython 3.14.7 x64", "BASELINE_PYTHON")
    require(baseline["environment"]["pyinstaller"] == EXPECTED_PYINSTALLER_VERSION, "BASELINE_PYINSTALLER")
    require(baseline["inventory_file_count"] == 171, "BASELINE_INVENTORY_COUNT")
    require(all(baseline["relationships"].values()), "BASELINE_RELATIONSHIP")
    require(stock_pe["text_sha256"] == EXPECTED_STOCK_TEXT_SHA256, "STOCK_TEXT_DIGEST")
    require(baseline_pe["text_sha256"] == EXPECTED_STOCK_TEXT_SHA256, "BASELINE_TEXT_DIGEST")

    if mismatches:
        raise AuditInputError("pinned input mismatch: " + ",".join(sorted(mismatches)))
    return {
        "status": "PASS",
        "python": python_version,
        "python_series_expected": EXPECTED_PYTHON_SERIES,
        "pyinstaller": pyinstaller_version,
        "pyinstaller_expected": EXPECTED_PYINSTALLER_VERSION,
        "sdist_extracted_source_relation": "MATCH",
        "baseline_inventory_executable_relation": "MATCH",
        "stock_baseline_text_relation": "MATCH",
    }


def audit_toolchain() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("cl.exe", "link.exe", "msbuild.exe", "cmake.exe", "gcc.exe", "clang.exe", "ninja.exe", "waf.exe"):
        resolved = shutil.which(name)
        result[name] = "FOUND" if resolved else "NOT_FOUND"
    program_files_x86_value = os.environ.get("ProgramFiles(x86)")
    program_files_x86 = Path(program_files_x86_value) if program_files_x86_value else None
    result["vswhere.exe"] = (
        "FOUND"
        if program_files_x86 is not None
        and (program_files_x86 / "Microsoft Visual Studio/Installer/vswhere.exe").is_file()
        else "NOT_FOUND"
    )
    result["windows_sdk_include"] = (
        "FOUND"
        if program_files_x86 is not None
        and (program_files_x86 / "Windows Kits/10/Include").is_dir()
        else "NOT_FOUND"
    )
    return result


def evaluate_assertions(
    source: dict[str, Any],
    stock_pe: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    token_counts = source["token_counts"]
    has_identity_primitives = (
        token_counts["GetFileInformationByHandleEx"] > 0
        and token_counts["FILE_ID_INFO"] > 0
        and token_counts["FILE_FLAG_OPEN_REPARSE_POINT"] > 0
    )
    has_restricted_search = (
        token_counts["SetDefaultDllDirectories"] > 0
        and token_counts["AddDllDirectory"] > 0
    )
    has_attestation_handoff = (
        token_counts["PyCapsule_New"] > 0
        or token_counts["TrustedSourceAuthority"] > 0
    )
    meipass_path_line = source["markers"]["meipass_path_dynamic_search"]["line"]
    ctypes_runtime_present = bool(baseline and baseline["ctypes_runtime_present"])

    def observed_failure(condition: bool) -> str:
        return "FAIL" if condition else "INDETERMINATE"

    return [
        {
            "id": "W3.STOCK.DLL_POLICY_BEFORE_PYTHON",
            "status": observed_failure(
                source["stock_order_confirmed"] and not has_restricted_search
            ),
            "reason": (
                "stock source derives a pathname-based application_home_dir and calls "
                "SetDllDirectoryW before loading Python, but has no W3 restricted-search "
                "and retained-handle policy"
                if source["stock_order_confirmed"] and not has_restricted_search
                else "static token/order facts do not establish either PASS or the stock failure"
            ),
        },
        {
            "id": "W3.STOCK.NATIVE_CLOSURE_PRELOAD",
            "status": observed_failure(not has_identity_primitives),
            "reason": (
                "no recursive manifest-bound native closure and retained-handle "
                "root/reparse/live-identity/digest preproof is implemented"
                if not has_identity_primitives
                else "identity-related API tokens alone do not establish PASS or a conclusive failure"
            ),
        },
        {
            "id": "W3.STOCK.ACTUAL_MODULE_REPROOF",
            "status": observed_failure(not has_identity_primitives and not has_attestation_handoff),
            "reason": (
                "stock load path has neither retained live-identity primitives nor an attestation handoff for actual-module reproof"
                if not has_identity_primitives and not has_attestation_handoff
                else "static token presence alone does not establish PASS or a conclusive failure"
            ),
        },
        {
            "id": "W3.STOCK.ATTESTATION_HANDOFF",
            "status": observed_failure(not has_attestation_handoff),
            "reason": (
                "no native-to-Python unforgeable bundle/DLL attestation handoff is present"
                if not has_attestation_handoff
                else "attestation-related token presence alone does not establish PASS or a conclusive failure"
            ),
        },
        {
            "id": "W3.STOCK.PREAUTHORITY_HOOK",
            "status": observed_failure(source["stock_order_confirmed"]),
            "reason": (
                "Python bootstrap executes only after Python is loaded; the baseline includes ctypes, so its later stock ctypes hook can search sys._MEIPASS plus PATH"
                if source["stock_order_confirmed"]
                and meipass_path_line is not None
                and ctypes_runtime_present
                else (
                    "Python bootstrap executes only after Python is loaded; the stock ctypes PATH-search branch exists but activation was not established"
                    if source["stock_order_confirmed"] and meipass_path_line is not None
                    else "source order does not establish either PASS or the stock failure"
                )
            ),
        },
        {
            "id": "W3.STOCK.PE_SYSTEM_ONLY",
            "status": observed_failure(
                bool(stock_pe["non_allowlisted_static_import_names"])
            ),
            "reason": (
                "static import names do not prove runtime resolution and include names "
                "outside the conservative entry allowlist: "
                + ", ".join(stock_pe["non_allowlisted_static_import_names"])
                if stock_pe["non_allowlisted_static_import_names"]
                else "static names alone cannot establish PASS or a conclusive failure"
            ),
        },
    ]


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    stock_bootloader = args.stock_bootloader.resolve(strict=True)
    baseline_exe = args.baseline_exe.resolve(strict=True)
    python_dll = args.python_dll.resolve(strict=True)
    source_archive = args.source_archive.resolve(strict=True)
    source_root = args.source_root.resolve(strict=True)
    baseline_matrix = args.baseline_matrix.resolve(strict=True)
    baseline_inventory = args.baseline_inventory.resolve(strict=True)
    baseline_checksums = args.baseline_checksums.resolve(strict=True)
    baseline_analysis = args.baseline_analysis.resolve(strict=True)

    pyinstaller_version = importlib.metadata.version("PyInstaller")
    python_version = platform.python_version()
    source_archive_fact = file_fact("pyinstaller_sdist", source_archive)
    python_dll_fact = file_fact("python_dll", python_dll)
    source = audit_sources(source_root)
    sdist_source = audit_sdist_sources(source_archive)
    stock_pe = audit_pe(stock_bootloader, "stock_runw_bootloader")
    baseline_pe = audit_pe(baseline_exe, "baseline_windowed_executable")
    baseline = audit_baseline_provenance(
        baseline_matrix,
        baseline_inventory,
        baseline_checksums,
        baseline_analysis,
        baseline_pe,
        stock_pe,
    )
    input_gate = validate_pinned_inputs(
        python_version=python_version,
        pyinstaller_version=pyinstaller_version,
        source_archive_fact=source_archive_fact,
        python_dll_fact=python_dll_fact,
        source=source,
        sdist_source=sdist_source,
        stock_pe=stock_pe,
        baseline_pe=baseline_pe,
        baseline=baseline,
    )
    assertions = evaluate_assertions(source, stock_pe, baseline)
    all_required_pass = all(item["status"] == "PASS" for item in assertions)

    return {
        "schema": SCHEMA_ID,
        "result": "PASS" if all_required_pass else "NO_GO",
        "task": "windows-platform-enablement/1.4",
        "gate": "W3",
        "input_gate": input_gate,
        "inputs": {
            "source_archive": source_archive_fact,
            "python_dll": python_dll_fact,
        },
        "stock_bootloader_pe": stock_pe,
        "baseline_executable_pe": baseline_pe,
        "baseline_provenance": baseline,
        "pinned_source": source,
        "pinned_sdist_source": sdist_source,
        "toolchain_availability": audit_toolchain(),
        "required_assertions": assertions,
        "source_collection_fact": {
            "status": "SUPPORTED_BUT_NOT_SUFFICIENT",
            "mechanism": "module_collection_mode=py",
            "reason": (
                "external .py collection cannot establish native pre-Python DLL closure, "
                "attestation handoff, or retained-handle exact-byte execution"
            ),
        },
        "gate_effect": {
            "stock_task_1_4_complete": True,
            "w3_feasibility_task_1_6_complete": False,
            "source_lane_allowed": True,
            "task_7_allowed": False,
            "w3_reapproval_required": True,
            "reason": (
                "the stock-route audit is complete as NO_GO; custom in-process entry "
                "reapproval and the Task 1.6 minimal feasibility spike remain mandatory"
            ),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-bootloader", type=Path, required=True)
    parser.add_argument("--baseline-exe", type=Path, required=True)
    parser.add_argument("--python-dll", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--baseline-matrix", type=Path, required=True)
    parser.add_argument("--baseline-inventory", type=Path, required=True)
    parser.add_argument("--baseline-checksums", type=Path, required=True)
    parser.add_argument("--baseline-analysis", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--log-output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.log_output is not None and args.output is None:
        print("W3_AUDIT_INPUT_ERROR: --log-output requires --output", file=sys.stderr)
        return 2
    try:
        audit = build_audit(args)
    except (AuditInputError, FileNotFoundError, OSError) as exc:
        print(f"W3_AUDIT_INPUT_ERROR: {exc}", file=sys.stderr)
        return 2

    encoded = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    process_exit_code = 0 if audit["result"] == "PASS" else 1
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
        messages = (
            f"W3_STOCK_AUDIT={audit['result']}",
            f"OUTPUT_SHA256={sha256_file(args.output)}",
            f"PROCESS_EXIT_CODE={process_exit_code}",
        )
        if args.log_output is not None:
            args.log_output.parent.mkdir(parents=True, exist_ok=True)
            args.log_output.write_text("\n".join(messages) + "\n", encoding="utf-8", newline="\n")
        for message in messages[:-1]:
            print(message)
    return process_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
