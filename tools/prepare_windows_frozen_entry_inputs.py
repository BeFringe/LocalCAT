"""Materialize reproducible inputs for the Windows frozen-entry review.

The generated record is a candidate-input inventory, not a release approval.
It binds the locally selected MSVC/SDK, pinned CPython and PyInstaller inputs,
and the Python/native facts that are already knowable before the custom entry
is implemented.  Task 1.6 later binds the applied patch and resulting PE in a
separate realized-build lock.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Sequence

from tools.audit_w3_stock_bootloader import (
    EXPECTED_PYINSTALLER_VERSION,
    EXPECTED_PYTHON_DLL_SHA256,
    EXPECTED_SDIST_SHA256,
    EXPECTED_SOURCE_SCAN_COUNT,
    EXPECTED_SOURCE_SCAN_SHA256,
    EXPECTED_STOCK_RUNW_SHA256,
    AuditInputError,
    audit_sdist_sources,
    audit_sources,
    sha256_file,
)
from tools.windows_frozen_packaging import PackagingInputError, candidate_packaging_fact
SCHEMA_ID = "localcat.windows-frozen-entry-candidate-input.v3"
DIRECTORY_DIGEST_ALGORITHM = "localcat.directory-input-sha256.v1"
EXPECTED_PYTHON_VERSION = "3.14.7"
EXPECTED_MACHINE = 0x8664
SUPPORT_REFERENCE = (
    "https://learn.microsoft.com/en-us/visualstudio/releases/2022/"
    "release-history"
)
ENTRY_CONTRACT_SCHEMA = "localcat.windows-frozen-entry-target-contract.v3"
# Pin the interpreter's system API call; collect host observations separately.
SYSTEM_SERVICE_BOUNDARY = {
    "authority": "Windows OS",
    "rng": {"dll": "bcrypt.dll", "symbol": "BCryptGenRandom",
            "hAlgorithm": 0, "flags": 2},
}
PROBE_EVIDENCE_SCHEMA = "localcat.windows-frozen-entry-toolchain-probe-evidence.v1"
PROBE_PREBUILD_EVIDENCE_SCHEMA = (
    "localcat.windows-frozen-entry-toolchain-probe-prebuild-evidence.v1"
)
PROBE_BUILD_ARGUMENTS = ["bootloader/waf", "all", "--target-arch=64bit", "-j1"]
PROBE_CLEARED_ENVIRONMENT = ["CL", "INCLUDE", "LIB", "LIBPATH", "LINK", "_CL_", "_LINK_"]
INTERPRETER_SOURCES = (
    ("encoding-package", "encodings", "Lib/encodings/__init__.py"),
    ("encoding-aliases", "encodings.aliases", "Lib/encodings/aliases.py"),
    ("encoding-utf8", "encodings.utf_8", "Lib/encodings/utf_8.py"),
    ("encoding-win", "encodings._win_cp_codecs", "Lib/encodings/_win_cp_codecs.py"),
    ("importlib-external", "_frozen_importlib_external", "Lib/importlib/_bootstrap_external.py"),
)

_IMPORT_FUNCTION_RE = re.compile(r"(?m)^\s+_IMPORT_FUNCTION\((Py[A-Za-z0-9_]+)\)\s*(?:/\*.*\*/)?$")


class CandidateInputError(ValueError):
    """A candidate input is absent, ambiguous, or outside the contract."""


def _require_file(path: Path, role: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise CandidateInputError(f"missing {role}")
    return resolved


def _require_directory(path: Path, role: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise CandidateInputError(f"missing {role}")
    return resolved


def _file_fact(path: Path, role: str) -> dict[str, Any]:
    checked = _require_file(path, role)
    return {
        "role": role,
        "name": checked.name,
        "bytes": checked.stat().st_size,
        "sha256": sha256_file(checked),
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _verified_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO calendar date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected an ISO calendar date (YYYY-MM-DD)")
    return value


def _target_platform_fact() -> dict[str, str]:
    system = platform.system()
    machine = platform.machine()
    if system != "Windows" or machine.upper() != "AMD64":
        raise CandidateInputError("candidate preparation requires Windows AMD64")
    return {"system": "Windows", "machine": "AMD64"}


def _directory_input_fact(
    root: Path,
    role: str,
    *,
    include: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    checked = _require_directory(root, role)
    members: list[tuple[str, int, str]] = []
    total_bytes = 0
    digest = hashlib.sha256()
    digest.update((DIRECTORY_DIGEST_ALGORITHM + "\n").encode("ascii"))
    for path in sorted(
        (item for item in checked.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(checked).as_posix(),
    ):
        relative = path.relative_to(checked)
        if include is not None and not include(relative):
            continue
        name = relative.as_posix()
        size = path.stat().st_size
        item_sha256 = sha256_file(path)
        members.append((name, size, item_sha256))
        total_bytes += size
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item_sha256.encode("ascii"))
        digest.update(b"\n")
    if not members:
        raise CandidateInputError(f"empty {role}")
    return {
        "role": role,
        "algorithm": DIRECTORY_DIGEST_ALGORITHM,
        "file_count": len(members),
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def _version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise CandidateInputError(f"invalid numeric version directory: {value}") from exc


def _select_version_directory(parent: Path, requested: str | None, role: str) -> Path:
    checked = _require_directory(parent, role)
    if requested:
        return _require_directory(checked / requested, f"{role} {requested}")
    candidates = [item for item in checked.iterdir() if item.is_dir()]
    if not candidates:
        raise CandidateInputError(f"no {role} version directories")
    return max(candidates, key=lambda item: _version_key(item.name))


def _pe_fact(path: Path, role: str, *, exports: bool = False) -> dict[str, Any]:
    try:
        import pefile
    except ImportError as exc:  # pragma: no cover - exercised by the real runner
        raise CandidateInputError("pefile is required for frozen-entry input preparation") from exc

    checked = _require_file(path, role)
    pe = pefile.PE(str(checked), fast_load=True)
    directories = [
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
    ]
    if exports:
        directories.append(pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"])
    pe.parse_data_directories(directories=directories)

    def imported(entry: Any) -> dict[str, Any]:
        symbols: list[str] = []
        for item in entry.imports:
            if item.name is not None:
                symbols.append(item.name.decode("ascii"))
            else:
                symbols.append(f"ordinal:{item.ordinal}")
        return {
            "dll": entry.dll.decode("ascii"),
            "symbols": sorted(symbols),
        }

    imports = sorted(
        (imported(entry) for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])),
        key=lambda item: item["dll"].upper(),
    )
    delay_imports = sorted(
        (imported(entry) for entry in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])),
        key=lambda item: item["dll"].upper(),
    )
    exported_symbols: list[str] = []
    export_directory = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
    if export_directory is not None:
        for item in export_directory.symbols:
            if item.name is not None:
                exported_symbols.append(item.name.decode("ascii"))
            else:
                exported_symbols.append(f"ordinal:{item.ordinal}")

    fact = _file_fact(checked, role)
    fact.update(
        {
            "machine": pe.FILE_HEADER.Machine,
            "imports": imports,
            "delay_imports": delay_imports,
        }
    )
    if exports:
        fact["exports"] = sorted(exported_symbols)
    pe.close()
    return fact


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def python_api_table_for_314(source_path: Path) -> dict[str, Any]:
    """Extract the exact CPython 3.14 binding path from pinned PyInstaller."""

    checked = _require_file(source_path, "PyInstaller Python binding source")
    text = checked.read_text(encoding="utf-8")
    branch_marker = "    if (dylib->has_pep741) {"
    else_marker = "    } else {"
    post_marker = "    _IMPORT_FUNCTION(PyErr_Clear)"
    try:
        branch_index = text.index(branch_marker)
        else_index = text.index(else_marker, branch_index)
        post_index = text.index(post_marker, else_index)
    except ValueError as exc:
        raise CandidateInputError("unrecognized PyInstaller Python binding control flow") from exc

    common_before = _IMPORT_FUNCTION_RE.findall(text[:branch_index])
    pep741 = _IMPORT_FUNCTION_RE.findall(text[branch_index:else_index])
    common_after = _IMPORT_FUNCTION_RE.findall(text[post_index:])
    symbols = _ordered_unique(
        [*common_before, "PyInitConfig_Create", *pep741, *common_after]
    )
    if not symbols or "Py_InitializeFromInitConfig" not in symbols:
        raise CandidateInputError("empty or incomplete CPython 3.14 API table")
    binding_header = _require_file(
        checked.with_name("pyi_dylib_python.h"),
        "PyInstaller Python binding header",
    )
    return {
        "source_name": checked.name,
        "source_sha256": sha256_file(checked),
        "binding_header_name": binding_header.name,
        "binding_header_sha256": sha256_file(binding_header),
        "selection": "PEP-741 path required by CPython 3.14.7",
        "symbols": symbols,
        "symbol_count": len(symbols),
    }


def _runtime_native_closure(python_root: Path) -> dict[str, Any]:
    checked = _require_directory(python_root, "CPython root")
    local_dlls = {
        item.name.upper(): item
        for item in checked.glob("*.dll")
        if item.is_file()
    }
    root_name = "PYTHON314.DLL"
    if root_name not in local_dlls:
        raise CandidateInputError("missing python314.dll")

    queue = [root_name]
    visited: set[str] = set()
    members: list[dict[str, Any]] = []
    external: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        fact = _pe_fact(local_dlls[name], f"runtime native member {name.lower()}")
        members.append(fact)
        for table in (fact["imports"], fact["delay_imports"]):
            for imported in table:
                dependency = imported["dll"].upper()
                if dependency in local_dlls:
                    queue.append(dependency)
                else:
                    external.add(dependency)
    return {
        "root": root_name.lower(),
        "local_members": sorted(members, key=lambda item: item["name"].upper()),
        "external_import_names": sorted(external),
    }


def _vs_instance(vswhere: Path, vs_install: Path) -> dict[str, Any]:
    checked = _require_file(vswhere, "vswhere.exe")
    result = subprocess.run(
        [
            str(checked),
            "-products",
            "Microsoft.VisualStudio.Product.BuildTools",
            "-format",
            "json",
            "-utf8",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
    )
    instances = json.loads(result.stdout)
    expected = os.path.normcase(str(vs_install.resolve()))
    matches = [
        item
        for item in instances
        if os.path.normcase(str(Path(item["installationPath"]).resolve())) == expected
    ]
    if len(matches) != 1:
        raise CandidateInputError("selected Visual Studio instance is absent or ambiguous")
    item = matches[0]
    catalog = item.get("catalog", {})
    return {
        "installation_version": item.get("installationVersion"),
        "product_id": item.get("productId"),
        "product_line_version": catalog.get("productLineVersion"),
        "product_display_version": catalog.get("productDisplayVersion"),
        "is_complete": bool(item.get("isComplete")),
        "is_launchable": bool(item.get("isLaunchable")),
    }


def _validated_vs_instance(vswhere: Path, vs_install: Path) -> dict[str, Any]:
    instance = _vs_instance(vswhere, vs_install)
    valid = (
        instance["product_id"] == "Microsoft.VisualStudio.Product.BuildTools"
        and instance["product_line_version"] == "2022"
        and str(instance["installation_version"] or "").startswith("17.")
        and instance["is_complete"]
        and instance["is_launchable"]
    )
    if not valid:
        raise CandidateInputError("selected Visual Studio instance is not complete Build Tools 2022")
    return instance


def _assertion(identifier: str, passed: bool, detail: str) -> dict[str, str]:
    return {
        "id": identifier,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
    }


def _venv_semantics(configuration: Path) -> dict[str, Any]:
    checked = _require_file(configuration, "candidate producer venv configuration")
    values: dict[str, str] = {}
    for raw_line in checked.read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    include_system = values.get("include-system-site-packages", "").lower()
    if include_system != "false":
        raise CandidateInputError("candidate producer must disable system site-packages")
    return {
        "is_venv": Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve(),
        "include_system_site_packages": False,
    }


def _evidence_producer_fact(
    *, python_root: Path, vswhere: Path, packaging_dependencies: Path
) -> dict[str, Any]:
    try:
        import pefile
    except ImportError as exc:  # pragma: no cover - exercised by the real runner
        raise CandidateInputError("pefile is required for frozen-entry input preparation") from exc
    pefile_source = Path(pefile.__file__).resolve()
    producer_base = Path(sys.base_prefix).resolve()
    producer_prefix = Path(sys.prefix).resolve()
    if producer_base != python_root.resolve():
        raise CandidateInputError("candidate producer does not use the pinned CPython base")
    venv_semantics = _venv_semantics(producer_prefix / "pyvenv.cfg")
    if not venv_semantics["is_venv"]:
        raise CandidateInputError("candidate producer must run from a dedicated venv")
    pefile_distribution = importlib.metadata.distribution("pefile")
    metadata_candidates = [
        Path(pefile_distribution.locate_file(item)).resolve()
        for item in (pefile_distribution.files or [])
        if item.name == "METADATA" and ".dist-info" in item.as_posix()
    ]
    if len(metadata_candidates) != 1:
        raise CandidateInputError("pefile distribution metadata is absent or ambiguous")
    return {
        "custom_packaging": candidate_packaging_fact(Path(__file__).resolve().parents[1], packaging_dependencies),
        "python": _file_fact(Path(sys.executable), "candidate input producer Python"),
        "python_version": platform.python_version(),
        "base_runtime": _directory_input_fact(
            producer_base,
            "candidate producer CPython base runtime",
            include=lambda relative: (
                "__pycache__" not in relative.parts
                and relative.suffix.lower() not in {".pyc", ".pyo"}
            ),
        ),
        "venv": venv_semantics,
        "preparer": _file_fact(Path(__file__), "candidate input producer"),
        "replay_wrapper": _file_fact(
            Path(__file__).with_name("replay_windows_frozen_entry_inputs.ps1"),
            "candidate input replay wrapper",
        ),
        "stock_audit_helper": _file_fact(
            Path(__file__).with_name("audit_w3_stock_bootloader.py"),
            "stock audit helper",
        ),
        "probe_evidence_recorder": _file_fact(
            Path(__file__).with_name("record_windows_frozen_entry_probe.py"),
            "probe build evidence recorder",
        ),
        "probe_prebuild_evidence_recorder": _file_fact(
            Path(__file__).with_name("record_windows_frozen_entry_probe_prebuild.py"),
            "probe pre-build evidence recorder",
        ),
        "vswhere": _file_fact(vswhere, "Visual Studio instance resolver"),
        "pefile": {
            "version": importlib.metadata.version("pefile"),
            "source": _file_fact(pefile_source, "pefile parser source"),
            "distribution_metadata": _file_fact(
                metadata_candidates[0], "pefile distribution metadata"
            ),
        },
    }


def _reject_duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateInputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(value: Any, expected: set[str], role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateInputError(f"{role} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise CandidateInputError(
            f"{role} keys mismatch (missing={missing}, unknown={unknown})"
        )
    return value


def _validate_interpreter_sources(members: Any) -> None:
    expected = [dict(zip(("id", "module", "path"), row)) for row in INTERPRETER_SOURCES]
    if members != expected:
        raise CandidateInputError("interpreter sources differ from the approved exact startup set")


def _interpreter_source_facts(python_root: Path, members: Any) -> list[dict[str, Any]]:
    """Materialize exact bytes, not runtime retained proof or a frozen fallback."""
    _validate_interpreter_sources(members)
    root = _require_directory(python_root, "CPython source root")
    facts = []
    for member in members:
        source = _require_file(root / member["path"], "interpreter source " + member["id"])
        if not source.is_relative_to(root):
            raise CandidateInputError("interpreter source escapes the CPython input root")
        data = source.read_bytes()
        if not data:
            raise CandidateInputError("empty interpreter source " + member["id"])
        facts.append({**member, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return facts


def _validate_entry_contract_shape(contract: dict[str, Any]) -> None:
    sections = {
        "runtime_abi",
        "patch",
        "build",
        "python_c_api",
        "pe_allowlist",
        "dynamic_loader",
        "boot_tcb",
        "interpreter_sources",
    }
    _require_exact_keys(contract, {"schema", *sections}, "entry contract")
    _validate_interpreter_sources(contract["interpreter_sources"])
    _require_exact_keys(
        contract["runtime_abi"],
        {"module", "protocol", "authority_type", "state_machine"},
        "runtime_abi",
    )
    patch = _require_exact_keys(
        contract["patch"],
        {"base", "owned_existing_files", "owned_new_files", "series", "apply"},
        "patch",
    )
    _require_exact_keys(
        patch["base"], {"pyinstaller_version", "source_scan_sha256"}, "patch.base"
    )
    if not isinstance(patch["series"], list) or not patch["series"]:
        raise CandidateInputError("patch.series must be a non-empty list")
    for index, item in enumerate(patch["series"]):
        _require_exact_keys(item, {"id", "scope"}, f"patch.series[{index}]")
    _require_exact_keys(
        patch["apply"],
        {"format", "order", "precondition", "conflict_result", "result"},
        "patch.apply",
    )
    _require_exact_keys(
        contract["build"],
        {
            "architecture",
            "configuration",
            "compiler_required_flags",
            "linker_required_flags",
            "debug_directory",
            "security_directory",
        },
        "build",
    )
    _require_exact_keys(
        contract["python_c_api"],
        {
            "base",
            "base_signature_authority",
            "removed_base_functions",
            "additional_functions",
            "function_signatures",
            "data_exports",
            "data_export_types",
        },
        "python_c_api",
    )
    pe_allowlist = _require_exact_keys(
        contract["pe_allowlist"],
        {
            "base_stock_runw_sha256",
            "compiler_probe",
            "expected_dll_names",
            "remove_dlls",
            "remove_symbols",
            "add_symbols",
        },
        "pe_allowlist",
    )
    _require_exact_keys(
        pe_allowlist["compiler_probe"],
        {"purpose", "import_table_sha256", "added_symbols", "removed_symbols"},
        "pe_allowlist.compiler_probe",
    )
    _validate_dynamic_loader(contract["dynamic_loader"])
    _require_exact_keys(
        contract["boot_tcb"],
        {"native_roles", "python_roles", "external_roles"},
        "boot_tcb",
    )


def _approved_system_loader_calls() -> list[dict[str, Any]]:
    """Exact approved application call targets; not runtime resolution evidence."""
    calls = []
    for name, api_set, host, prefixes in (
        ("crt-vcruntime-critical-section", "api-ms-win-core-synch-l1-2-0", "kernel32", ["api-ms-"]),
        ("crt-vcruntime-fls", "api-ms-win-core-fibers-l1-1-1", "kernel32", ["api-ms-"]),
        ("crt-ucrt-fls2", "api-ms-win-core-fibers-l1-1-2", "kernelbase", ["api-ms-", "ext-ms-"]),
    ):
        calls.append({
            "id": name, "owner": "custom-entry-static-crt", "phase": "E0.5",
            "loader": "LoadLibraryExW", "targets": [api_set, host], "flags": 0x800,
            "condition": "crt-initialization-fixed-name-probe",
            "fallback": {"error": 87, "targets": [host], "flags": 0, "excluded_prefixes": prefixes},
            "requires": "PRE_ENTRY_SYSTEM_RESOLUTION_PROOF",
        })
    calls.append({
        "id": "crt-ucrt-process-termination", "owner": "custom-entry-static-crt",
        "phase": "CRT_EXIT_INCLUDING_E1_FAILURE", "loader": "LoadLibraryExW",
        "targets": ["api-ms-win-appmodel-runtime-l1-1-2"], "flags": 0x800,
        "condition": "crt-termination-non-secure-and-empty-function-and-module-cache",
        "fallback": None, "requires": "POLICY_INDEPENDENT_SYSTEM_RESOLUTION_PROOF",
    })
    calls.append({
        "id": "crt-ucrt-multibyte-casing", "owner": "custom-entry-static-crt",
        "phase": "E0.5", "loader": "LoadLibraryExW",
        "targets": ["api-ms-win-core-localization-l1-2-1", "kernel32"], "flags": 0x800,
        "condition": "crt-multibyte-initialization-non-utf8-acp-and-getcpinfo-success-and-empty-function-and-module-cache",
        "module_selection": "first-available-module-next-candidate-on-load-failure-only",
        "symbols": ["LCMapStringEx", "LocaleNameToLCID"],
        "symbol_fallback": {
            "LCMapStringEx": "static-LCMapStringW-with-LocaleNameToLCID-conversion",
            "LocaleNameToLCID": "downlevel-conversion-no-loader",
            "resolution": "same-candidate-list-and-module-cache-no-next-candidate-on-missing-symbol",
        },
        "fallback": {"error": 87, "targets": ["kernel32"], "flags": 0,
                     "excluded_prefixes": ["api-ms-", "ext-ms-"]},
        "requires": "PRE_ENTRY_SYSTEM_RESOLUTION_PROOF",
    })
    for name, target, phase, condition in (
        ("python-mimalloc-memory-kernelbase", "kernelbase.dll", "E6_DLL_INITIALIZATION", "process-memory-init"),
        ("python-mimalloc-memory-ntdll", "ntdll.dll", "E6_DLL_INITIALIZATION", "process-memory-init"),
        ("python-mimalloc-memory-kernel32", "kernel32.dll", "E6_DLL_INITIALIZATION", "process-memory-init"),
        ("python-mimalloc-random", "bcrypt.dll", "AFTER_E1", "os-random-request-and-empty-function-cache"),
        ("python-mimalloc-stats", "psapi.dll", "AFTER_E1_INCLUDING_EARLY_EXIT", "stats-or-verbose-and-empty-function-cache"),
    ):
        calls.append({
            "id": name, "owner": "python314.dll", "phase": phase, "loader": "LoadLibraryA",
            "targets": [target], "flags": None, "condition": condition, "fallback": None,
            "requires": "E1_SYSTEM32_AND_SYSTEM_RESOLUTION_PROOF",
        })
    return calls


def _validate_dynamic_loader(loader: Any) -> None:
    calls = _approved_system_loader_calls()
    expected = {
        "non_system_dispatcher": "localcat_native_closure_load_verified",
        "allowed_flags": ["LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR", "LOAD_LIBRARY_SEARCH_SYSTEM32"],
        "pre_authority_external_calls": [
            "LoadLibraryExW through localcat_native_closure_load_verified", *[item["id"] for item in calls]],
        "application_system_calls": calls,
        "system_service_boundary": SYSTEM_SERVICE_BOUNDARY,
    }
    if _canonical_json(loader) != _canonical_json(expected):
        raise CandidateInputError("dynamic loader differs from the approved exact application system-call boundary")


def _load_entry_contract(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checked = _require_file(path, "frozen-entry target contract")
    try:
        contract = json.loads(
            checked.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
        )
    except json.JSONDecodeError as exc:
        raise CandidateInputError("invalid frozen-entry target contract JSON") from exc
    if contract.get("schema") != ENTRY_CONTRACT_SCHEMA:
        raise CandidateInputError("unexpected frozen-entry target contract schema")
    _validate_entry_contract_shape(contract)
    return contract, _file_fact(checked, "frozen-entry target contract")


def _load_probe_build_evidence(path: Path) -> dict[str, Any]:
    checked = _require_file(path, "toolchain probe build evidence")
    try:
        evidence = json.loads(
            checked.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
        )
    except json.JSONDecodeError as exc:
        raise CandidateInputError("invalid toolchain probe build evidence JSON") from exc
    _require_exact_keys(
        evidence,
        {
            "schema",
            "source",
            "toolchain",
            "build",
            "result",
            "evidence_digest",
            "status",
        },
        "toolchain probe build evidence",
    )
    if evidence["schema"] != PROBE_EVIDENCE_SCHEMA or evidence["status"] != "RECORDED":
        raise CandidateInputError("unexpected toolchain probe build evidence schema/status")
    _require_exact_keys(
        evidence["source"],
        {
            "sdist_sha256",
            "actual_build_copy_source_scan",
            "actual_build_copy_build_driver",
            "archive_source_scan",
            "prebuild_evidence_digest",
        },
        "probe evidence source",
    )
    toolchain = _require_exact_keys(
        evidence["toolchain"],
        {"msvc_version", "windows_sdk_version", "resolved_tools"},
        "probe evidence toolchain",
    )
    _require_exact_keys(
        toolchain["resolved_tools"], {"cl", "link", "rc", "mt"}, "probe resolved tools"
    )
    for name, fact in toolchain["resolved_tools"].items():
        _require_exact_keys(
            fact, {"role", "name", "bytes", "sha256"}, f"probe resolved tool {name}"
        )
    build = _require_exact_keys(
        evidence["build"], {"arguments", "environment_projection", "stdout", "stderr"},
        "probe evidence build",
    )
    environment = _require_exact_keys(
        build["environment_projection"],
        {"pre_vcvars_path_roles", "cleared", "vscmd_skip_sendtelemetry", "vcvars"},
        "probe environment projection",
    )
    _require_exact_keys(
        environment["vcvars"],
        {"script", "architecture", "msvc_version", "windows_sdk_version"},
        "probe vcvars projection",
    )
    for stream in ("stdout", "stderr"):
        _require_exact_keys(
            build[stream], {"role", "normalization", "bytes", "sha256"},
            f"probe build {stream}",
        )
    _require_exact_keys(
        evidence["result"], {"machine", "import_table_sha256", "delay_imports"},
        "probe evidence result",
    )
    prebuild_payload = {
        "schema": PROBE_PREBUILD_EVIDENCE_SCHEMA,
        "source": {
            key: value
            for key, value in evidence["source"].items()
            if key != "prebuild_evidence_digest"
        },
    }
    if evidence["source"]["prebuild_evidence_digest"] != _json_digest(prebuild_payload):
        raise CandidateInputError("probe pre-build evidence digest mismatch")
    digest_payload = {
        key: value
        for key, value in evidence.items()
        if key not in {"evidence_digest", "status"}
    }
    if evidence["evidence_digest"] != _json_digest(digest_payload):
        raise CandidateInputError("toolchain probe build evidence digest mismatch")
    return evidence


def _file_identity(fact: dict[str, Any]) -> tuple[str, int, str]:
    return fact["name"], fact["bytes"], fact["sha256"]


def _validate_probe_build_evidence(
    evidence: dict[str, Any],
    *,
    source_audit: dict[str, Any],
    archive_audit: dict[str, Any],
    build_driver: dict[str, Any],
    tools: dict[str, Any],
    msvc_version: str,
    sdk_version: str,
    probe_fact: dict[str, Any],
) -> None:
    source = evidence["source"]
    toolchain = evidence["toolchain"]
    build = evidence["build"]
    result = evidence["result"]
    if (
        source["sdist_sha256"] != EXPECTED_SDIST_SHA256
        or source["actual_build_copy_source_scan"] != source_audit["source_scan"]
        or source["actual_build_copy_build_driver"] != build_driver
        or source["archive_source_scan"] != archive_audit
    ):
        raise CandidateInputError("probe build evidence source is not the pinned input")
    if (
        toolchain["msvc_version"] != msvc_version
        or toolchain["windows_sdk_version"] != sdk_version
    ):
        raise CandidateInputError("probe build evidence toolchain version mismatch")
    for name in ("cl", "link", "rc", "mt"):
        if _file_identity(toolchain["resolved_tools"][name]) != _file_identity(tools[name]):
            raise CandidateInputError(f"probe build evidence resolved {name} mismatch")
    expected_environment = {
        "pre_vcvars_path_roles": ["System32", "Windows"],
        "cleared": PROBE_CLEARED_ENVIRONMENT,
        "vscmd_skip_sendtelemetry": "1",
        "vcvars": {
            "script": "vcvarsall.bat",
            "architecture": "x64",
            "msvc_version": msvc_version,
            "windows_sdk_version": sdk_version,
        },
    }
    if build["arguments"] != PROBE_BUILD_ARGUMENTS or build["environment_projection"] != expected_environment:
        raise CandidateInputError("probe build evidence command/environment mismatch")
    if build["stdout"]["bytes"] <= 0:
        raise CandidateInputError("probe build evidence stdout is empty")
    if (
        result["machine"] != probe_fact["machine"]
        or result["import_table_sha256"] != probe_fact["import_table_sha256"]
        or result["delay_imports"] != probe_fact["delay_imports"]
    ):
        raise CandidateInputError("probe build evidence result mismatch")


def _validate_patch_owner_inventory(policy: dict[str, Any], pristine_names: set[str]) -> None:
    existing, new = policy["owned_existing_files"], policy["owned_new_files"]
    all_names = [*existing, *new]
    if (not existing or not new or len({name.casefold() for name in all_names}) != len(all_names)
            or not set(existing) <= pristine_names or set(new) & pristine_names):
        raise CandidateInputError("patch owner inventory differs from the actual pristine source")


def _target_python_functions(base_symbols: list[str], policy: dict[str, Any]) -> list[str]:
    removed = policy["removed_base_functions"]
    additional = policy["additional_functions"]
    if len(removed) != len(set(removed)) or not set(removed) <= set(base_symbols):
        raise CandidateInputError("custom Python API removes duplicate or absent base function")
    if len(additional) != len(set(additional)) or set(removed) & set(additional):
        raise CandidateInputError("custom Python API adds duplicate or removed function")
    if set(policy["function_signatures"]) != set(additional):
        raise CandidateInputError("custom Python function signature table is not exact")
    return _ordered_unique([*(name for name in base_symbols if name not in removed), *additional])


def _expected_entry_imports(
    stock_pe: dict[str, Any], contract: dict[str, Any]
) -> list[dict[str, Any]]:
    policy = contract["pe_allowlist"]
    if stock_pe["sha256"] != policy["base_stock_runw_sha256"]:
        raise CandidateInputError("stock runw digest does not match entry contract")
    if stock_pe["delay_imports"]:
        raise CandidateInputError("stock runw unexpectedly contains delay imports")

    imports = {
        item["dll"].upper(): set(item["symbols"])
        for item in stock_pe["imports"]
    }
    for dll in policy["remove_dlls"]:
        key = dll.upper()
        if key not in imports:
            raise CandidateInputError(f"entry contract removes absent DLL: {dll}")
        del imports[key]
    for dll, symbols in policy["remove_symbols"].items():
        key = dll.upper()
        if key not in imports:
            raise CandidateInputError(f"entry contract removes symbols from absent DLL: {dll}")
        for symbol in symbols:
            if symbol not in imports[key]:
                raise CandidateInputError(
                    f"entry contract removes absent symbol: {dll}!{symbol}"
                )
            imports[key].remove(symbol)
    for dll, symbols in policy["add_symbols"].items():
        imports.setdefault(dll.upper(), set()).update(symbols)

    expected_names = sorted(name.upper() for name in policy["expected_dll_names"])
    if sorted(imports) != expected_names:
        raise CandidateInputError("derived entry DLL names do not match exact target allowlist")
    return [
        {"dll": name, "symbols": sorted(imports[name])}
        for name in sorted(imports)
    ]


def _import_pairs(pe_fact: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (item["dll"].upper(), symbol)
        for table_name in ("imports", "delay_imports")
        for item in pe_fact[table_name]
        for symbol in item["symbols"]
    }


def _toolchain_probe_fact(
    stock_pe: dict[str, Any], probe_pe: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    policy = contract["pe_allowlist"]["compiler_probe"]
    if probe_pe["machine"] != EXPECTED_MACHINE:
        raise CandidateInputError("toolchain stock-rebuild probe is not AMD64")
    if probe_pe["delay_imports"]:
        raise CandidateInputError("toolchain stock-rebuild probe contains delay imports")
    import_table_sha256 = _json_digest(
        {
            "imports": probe_pe["imports"],
            "delay_imports": probe_pe["delay_imports"],
        }
    )
    if import_table_sha256 != policy["import_table_sha256"]:
        raise CandidateInputError("toolchain stock-rebuild probe import table mismatch")
    actual_added = _import_pairs(probe_pe) - _import_pairs(stock_pe)
    actual_removed = _import_pairs(stock_pe) - _import_pairs(probe_pe)
    expected_added = {
        (dll.upper(), symbol)
        for dll, symbols in policy["added_symbols"].items()
        for symbol in symbols
    }
    expected_removed = {
        (dll.upper(), symbol)
        for dll, symbols in policy["removed_symbols"].items()
        for symbol in symbols
    }
    if actual_added != expected_added or actual_removed != expected_removed:
        raise CandidateInputError("toolchain stock-rebuild import delta mismatch")
    return {
        "scope": policy["purpose"],
        "machine": probe_pe["machine"],
        "import_table_sha256": import_table_sha256,
        "delay_imports": [],
        "added_symbols": [
            {"dll": dll, "symbol": symbol}
            for dll, symbol in sorted(actual_added)
        ],
        "removed_symbols": [
            {"dll": dll, "symbol": symbol}
            for dll, symbol in sorted(actual_removed)
        ],
    }


def build_candidate_input(args: argparse.Namespace) -> dict[str, Any]:
    target_platform = _target_platform_fact()
    vs_install = _require_directory(args.vs_install, "Visual Studio installation")
    vs_layout = _require_directory(args.vs_layout, "Visual Studio offline layout")
    sdk_root = _require_directory(args.sdk_root, "Windows SDK root")
    python_root = _require_directory(args.python_root, "CPython root")
    pyinstaller_source = _require_directory(args.pyinstaller_source, "PyInstaller source")
    sdist = _require_file(args.pyinstaller_sdist, "PyInstaller sdist")
    entry_contract, entry_contract_fact = _load_entry_contract(args.entry_contract)
    probe_build_evidence = _load_probe_build_evidence(args.probe_build_evidence)

    msvc_root = _select_version_directory(
        vs_install / "VC/Tools/MSVC", args.msvc_version, "MSVC toolset"
    )
    sdk_include = _select_version_directory(
        sdk_root / "Include", args.sdk_version, "Windows SDK include"
    )
    sdk_version = sdk_include.name
    sdk_lib = _require_directory(sdk_root / "Lib" / sdk_version, "Windows SDK lib")
    sdk_bin = _require_directory(sdk_root / "bin" / sdk_version / "x64", "Windows SDK x64 bin")
    compiler_bin = _require_directory(
        msvc_root / "bin/Hostx64/x64", "MSVC Hostx64/x64 bin"
    )

    vs_instance = _validated_vs_instance(args.vswhere, vs_install)
    tools = {
        name: _file_fact(compiler_bin / filename, name)
        for name, filename in (
            ("cl", "cl.exe"),
            ("link", "link.exe"),
            ("dumpbin", "dumpbin.exe"),
        )
    }
    tools.update(
        {
            "rc": _file_fact(sdk_bin / "rc.exe", "rc"),
            "mt": _file_fact(sdk_bin / "mt.exe", "mt"),
        }
    )

    layout_files = [
        _file_fact(vs_layout / name, f"Visual Studio layout {name}")
        for name in ("Catalog.json", "ChannelManifest.json", "Layout.json", "Response.json")
    ]
    bootstrapper = vs_layout / "vs_buildtools_2022_current.exe"
    if not bootstrapper.is_file():
        bootstrapper = vs_layout / "vs_setup.exe"
    layout_files.append(_file_fact(bootstrapper, "Visual Studio layout bootstrapper"))

    python_executable = _file_fact(python_root / "python.exe", "CPython executable")
    python_dll = _pe_fact(python_root / "python314.dll", "CPython DLL", exports=True)
    python_dll_summary = {
        key: value
        for key, value in python_dll.items()
        if key not in {"imports", "delay_imports", "exports"}
    }
    python_dll_summary.update(
        {
            "import_table_sha256": _json_digest(
                {
                    "imports": python_dll["imports"],
                    "delay_imports": python_dll["delay_imports"],
                }
            ),
            "export_count": len(python_dll["exports"]),
            "export_table_sha256": _json_digest(python_dll["exports"]),
        }
    )
    python_import_library = _file_fact(
        python_root / "libs/python314.lib", "CPython import library"
    )
    python_headers = _directory_input_fact(python_root / "include", "CPython headers")
    python_version = subprocess.run(
        [str(python_root / "python.exe"), "-c", "import platform; print(platform.python_version())"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()

    try:
        source_audit = audit_sources(pyinstaller_source)
        archive_audit = audit_sdist_sources(sdist)
    except AuditInputError as exc:
        raise CandidateInputError(str(exc)) from exc
    _validate_patch_owner_inventory(entry_contract["patch"], {
        path.relative_to(pyinstaller_source).as_posix()
        for path in (pyinstaller_source / "bootloader").rglob("*") if path.is_file()
    })
    api_table = python_api_table_for_314(
        pyinstaller_source / "bootloader/src/pyi_dylib_python.c"
    )
    build_driver = _directory_input_fact(
        pyinstaller_source / "bootloader",
        "PyInstaller bootloader build driver",
    )
    exported = set(python_dll["exports"])
    missing_api = sorted(set(api_table["symbols"]) - exported)
    runtime_closure = _runtime_native_closure(python_root)
    interpreter_sources = _interpreter_source_facts(python_root, entry_contract["interpreter_sources"])
    api_policy = entry_contract["python_c_api"]
    function_signatures = api_policy["function_signatures"]
    data_export_types = api_policy["data_export_types"]
    if set(data_export_types) != set(api_policy["data_exports"]):
        raise CandidateInputError("custom Python data export type table is not exact")
    target_function_symbols = _target_python_functions(api_table["symbols"], api_policy)
    target_data_symbols = list(api_policy["data_exports"])
    if len(target_data_symbols) != len(set(target_data_symbols)):
        raise CandidateInputError("duplicate data symbol in Python C API target")
    missing_target_api = sorted(
        (set(target_function_symbols) | set(target_data_symbols)) - exported
    )
    stock_runw = _pe_fact(
        pyinstaller_source / "PyInstaller/bootloader/Windows-64bit-intel/runw.exe",
        "pinned stock runw",
    )
    toolchain_probe_runw = _pe_fact(
        args.toolchain_probe_runw,
        "selected-toolchain stock runw rebuild probe",
    )
    toolchain_probe = _toolchain_probe_fact(
        stock_runw, toolchain_probe_runw, entry_contract
    )
    _validate_probe_build_evidence(
        probe_build_evidence,
        source_audit=source_audit,
        archive_audit=archive_audit,
        build_driver=build_driver,
        tools=tools,
        msvc_version=msvc_root.name,
        sdk_version=sdk_version,
        probe_fact=toolchain_probe,
    )
    toolchain_probe["build_evidence"] = probe_build_evidence
    expected_entry_imports = _expected_entry_imports(stock_runw, entry_contract)
    stock_runw_summary = {
        key: value
        for key, value in stock_runw.items()
        if key not in {"imports", "delay_imports"}
    }
    stock_runw_summary["import_table_sha256"] = _json_digest(
        {"imports": stock_runw["imports"], "delay_imports": stock_runw["delay_imports"]}
    )
    runtime_closure_digest = _json_digest(runtime_closure)

    upstream_pinned = (
        sha256_file(sdist) == EXPECTED_SDIST_SHA256
        and source_audit["pinned_hashes_match"]
        and archive_audit["count"] == EXPECTED_SOURCE_SCAN_COUNT
        and archive_audit["sha256"] == EXPECTED_SOURCE_SCAN_SHA256
    )
    python_pinned = (
        python_version == EXPECTED_PYTHON_VERSION
        and python_dll["sha256"] == EXPECTED_PYTHON_DLL_SHA256
        and python_dll["machine"] == EXPECTED_MACHINE
    )
    toolchain_materialized = (
        target_platform == {"system": "Windows", "machine": "AMD64"}
        and vs_instance["product_id"] == "Microsoft.VisualStudio.Product.BuildTools"
        and vs_instance["product_line_version"] == "2022"
        and vs_instance["is_complete"]
        and vs_instance["is_launchable"]
        and msvc_root.name == args.msvc_version
        and sdk_version == args.sdk_version
    )

    assertions = [
        _assertion(
            "FROZEN_ENTRY.INPUT.TOOLCHAIN_MATERIALIZED",
            toolchain_materialized,
            "Windows AMD64, complete Build Tools 2022, exact MSVC and SDK inputs are content-addressed",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.UPSTREAM_PINNED",
            upstream_pinned,
            "PyInstaller sdist and audited source aggregate match the approved pin",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.PYTHON_RUNTIME_PINNED",
            python_pinned,
            "CPython version, DLL identity, and AMD64 machine match the approved pin",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.STOCK_API_EXPORTS",
            not missing_api,
            "all CPython symbols selected by the pinned PyInstaller 3.14 path are exported",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.TARGET_API_EXPORTS",
            not missing_target_api,
            "the exact custom-entry Python function/data target is exported by the pinned DLL",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.ENTRY_IMPORT_TARGET",
            stock_runw["sha256"] == EXPECTED_STOCK_RUNW_SHA256,
            "the exact custom-entry import target is derived from the pinned stock runw and approved deltas",
        ),
        _assertion(
            "FROZEN_ENTRY.INPUT.TOOLCHAIN_IMPORT_PROBE",
            True,
            "the selected MSVC stock rebuild has the approved compiler-generated import delta",
        ),
    ]

    payload: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "profile": "windows-x64-frozen-custom-entry-candidate",
        "support": {
            "verified_on": args.verified_on,
            "reference": SUPPORT_REFERENCE,
        },
        "target_platform": target_platform,
        "evidence_producer": _evidence_producer_fact(
            python_root=python_root, vswhere=args.vswhere, packaging_dependencies=args.packaging_dependencies
        ),
        "toolchain": {
            "visual_studio": {
                "instance": vs_instance,
                "layout_files": layout_files,
            },
            "msvc": {
                "toolset_version": msvc_root.name,
                "tools": tools,
                "compiler_bin": _directory_input_fact(
                    compiler_bin, "MSVC Hostx64/x64 binary inputs"
                ),
                "headers": _directory_input_fact(msvc_root / "include", "MSVC headers"),
                "libraries": _directory_input_fact(msvc_root / "lib/x64", "MSVC x64 libraries"),
                "stock_rebuild_probe": toolchain_probe,
            },
            "windows_sdk": {
                "version": sdk_version,
                "tools": {"rc": tools["rc"], "mt": tools["mt"]},
                "binary_inputs": _directory_input_fact(sdk_bin, "Windows SDK x64 binary inputs"),
                "headers": _directory_input_fact(sdk_include, "Windows SDK headers"),
                "libraries": _directory_input_fact(sdk_lib / "um/x64", "Windows SDK UM x64 libraries"),
                "ucrt_libraries": _directory_input_fact(
                    sdk_lib / "ucrt/x64", "Windows SDK UCRT x64 libraries"
                ),
            },
        },
        "runtime": {
            "cpython": {
                "version": python_version,
                "executable": python_executable,
                "dll": python_dll_summary,
                "import_library": python_import_library,
                "headers": python_headers,
                "native_closure": runtime_closure,
                "interpreter_sources": interpreter_sources,
            },
            "pyinstaller": {
                "version": EXPECTED_PYINSTALLER_VERSION,
                "sdist": _file_fact(sdist, "PyInstaller official sdist"),
                "source_scan": source_audit["source_scan"],
                "archive_source_scan": archive_audit,
                "python_314_api": {
                    **api_table,
                    "missing_exports": missing_api,
                },
                "build_driver": build_driver,
            },
        },
        "entry_contract": {
            "contract_file": entry_contract_fact,
            "runtime_abi": entry_contract["runtime_abi"],
            "patch": entry_contract["patch"],
            "build": entry_contract["build"],
            "python_c_api": {
                "base": api_policy["base"],
                "base_signature_authority": api_policy["base_signature_authority"],
                "removed_base_functions": api_policy["removed_base_functions"],
                "function_symbols": target_function_symbols,
                "additional_function_signatures": function_signatures,
                "data_symbols": target_data_symbols,
                "data_export_types": data_export_types,
                "symbol_count": len(target_function_symbols) + len(target_data_symbols),
                "missing_exports": missing_target_api,
            },
            "expected_pe": {
                "base_stock_runw": stock_runw_summary,
                "imports": expected_entry_imports,
                "delay_imports": [],
            },
            "expected_runtime_native_closure": {
                "source": "runtime.cpython.native_closure",
                "sha256": runtime_closure_digest,
            },
            "boot_tcb": entry_contract["boot_tcb"],
            "dynamic_loader": entry_contract["dynamic_loader"],
            "interpreter_sources": entry_contract["interpreter_sources"],
        },
        "assertions": assertions,
    }
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key != "assertions"
    }
    payload["candidate_input_digest"] = hashlib.sha256(
        _canonical_json(digest_payload)
    ).hexdigest()
    payload["status"] = (
        "MATERIALIZED" if all(item["status"] == "PASS" for item in assertions) else "INVALID_INPUT"
    )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vs-install", type=Path, required=True)
    parser.add_argument("--vs-layout", type=Path, required=True)
    parser.add_argument("--vswhere", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--msvc-version", required=True)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--pyinstaller-sdist", type=Path, required=True)
    parser.add_argument("--pyinstaller-source", type=Path, required=True)
    parser.add_argument("--packaging-dependencies", type=Path, required=True)
    parser.add_argument("--toolchain-probe-runw", type=Path, required=True)
    parser.add_argument("--probe-build-evidence", type=Path, required=True)
    parser.add_argument("--entry-contract", type=Path, required=True)
    parser.add_argument("--verified-on", type=_verified_date, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        record = build_candidate_input(args)
    except (
        CandidateInputError,
        PackagingInputError,
        KeyError,
        TypeError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"INVALID_INPUT: {exc}", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"{record['status']}: {record['candidate_input_digest']}")
    return 0 if record["status"] == "MATERIALIZED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
