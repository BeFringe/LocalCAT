"""Record portable provenance for the selected-toolchain stock bootloader probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence

from tools.audit_w3_stock_bootloader import (
    EXPECTED_SDIST_SHA256,
    EXPECTED_SOURCE_SCAN_COUNT,
    EXPECTED_SOURCE_SCAN_SHA256,
    AuditInputError,
    audit_sdist_sources,
    audit_sources,
    sha256_file,
)
from tools.prepare_windows_frozen_entry_inputs import (
    CandidateInputError,
    PROBE_PREBUILD_EVIDENCE_SCHEMA,
    _canonical_json,
    _file_fact,
    _json_digest,
    _pe_fact,
    _reject_duplicate_json_object,
    _require_exact_keys,
)


SCHEMA_ID = "localcat.windows-frozen-entry-toolchain-probe-evidence.v1"
BUILD_ARGUMENTS = ["bootloader/waf", "all", "--target-arch=64bit", "-j1"]
CLEARED_ENVIRONMENT = ["CL", "INCLUDE", "LIB", "LIBPATH", "LINK", "_CL_", "_LINK_"]
_ELAPSED_RE = re.compile(r"\(\d+(?:\.\d+)?s\)")


def _load_prebuild_evidence(path: Path) -> dict[str, Any]:
    checked = path.resolve()
    if not checked.is_file():
        raise CandidateInputError("missing probe pre-build evidence")
    try:
        evidence = json.loads(
            checked.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
        )
    except json.JSONDecodeError as exc:
        raise CandidateInputError("invalid probe pre-build evidence JSON") from exc
    _require_exact_keys(
        evidence,
        {"schema", "source", "evidence_digest", "status"},
        "probe pre-build evidence",
    )
    if (
        evidence["schema"] != PROBE_PREBUILD_EVIDENCE_SCHEMA
        or evidence["status"] != "RECORDED"
    ):
        raise CandidateInputError("unexpected probe pre-build evidence schema/status")
    _require_exact_keys(
        evidence["source"],
        {
            "sdist_sha256",
            "actual_build_copy_source_scan",
            "actual_build_copy_build_driver",
            "archive_source_scan",
        },
        "probe pre-build evidence source",
    )
    digest_payload = {
        key: value
        for key, value in evidence.items()
        if key not in {"evidence_digest", "status"}
    }
    if evidence["evidence_digest"] != _json_digest(digest_payload):
        raise CandidateInputError("probe pre-build evidence digest mismatch")
    return evidence


def _canonical_log_fact(
    path: Path,
    role: str,
    *,
    replacements: list[tuple[str, str]],
) -> dict[str, Any]:
    checked = path.resolve()
    if not checked.is_file():
        raise CandidateInputError(f"missing {role}")
    text = checked.read_text(encoding="utf-8", errors="strict")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw, token in replacements:
        for spelling in {raw, raw.replace("\\", "/"), raw.replace("/", "\\")}:
            text = re.sub(re.escape(spelling), lambda _match: token, text, flags=re.IGNORECASE)
    text = _ELAPSED_RE.sub("(<elapsed>)", text)
    canonical = text.encode("utf-8")
    return {
        "role": role,
        "normalization": "LF; approved path tokens; elapsed durations replaced",
        "bytes": len(canonical),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def build_probe_evidence(args: argparse.Namespace) -> dict[str, Any]:
    source_copy = args.source_copy.resolve()
    expected_probe = source_copy / "bootloader/build/releasew/runw.exe"
    if args.probe_runw.resolve() != expected_probe.resolve():
        raise CandidateInputError("probe runw is not the actual build-copy releasew result")

    try:
        source_audit = audit_sources(source_copy)
        archive_audit = audit_sdist_sources(args.sdist.resolve())
    except AuditInputError as exc:
        raise CandidateInputError(str(exc)) from exc
    if (
        sha256_file(args.sdist.resolve()) != EXPECTED_SDIST_SHA256
        or not source_audit["pinned_hashes_match"]
        or source_audit["source_scan"]["count"] != EXPECTED_SOURCE_SCAN_COUNT
        or source_audit["source_scan"]["sha256"] != EXPECTED_SOURCE_SCAN_SHA256
        or archive_audit["count"] != EXPECTED_SOURCE_SCAN_COUNT
        or archive_audit["sha256"] != EXPECTED_SOURCE_SCAN_SHA256
    ):
        raise CandidateInputError("actual probe build source is not the pinned sdist source")

    prebuild_evidence = _load_prebuild_evidence(args.prebuild_evidence)
    expected_prebuild_source = {
        "sdist_sha256": EXPECTED_SDIST_SHA256,
        "actual_build_copy_source_scan": source_audit["source_scan"],
        "archive_source_scan": archive_audit,
    }
    for key, value in expected_prebuild_source.items():
        if prebuild_evidence["source"][key] != value:
            raise CandidateInputError("probe pre-build evidence source mismatch")

    tool_paths = {
        "cl": args.cl.resolve(),
        "link": args.link.resolve(),
        "rc": args.rc.resolve(),
        "mt": args.mt.resolve(),
    }
    tools = {
        name: _file_fact(path, f"probe build {name}")
        for name, path in tool_paths.items()
    }
    probe = _pe_fact(expected_probe, "selected-toolchain stock runw rebuild probe")
    if probe["machine"] != 0x8664 or probe["delay_imports"]:
        raise CandidateInputError("actual probe build result is not AMD64/no-delay")

    replacements = [
        (str(source_copy), "<PROBE_SOURCE>"),
        (str(tool_paths["cl"].parent), "<MSVC_BIN>"),
        (str(tool_paths["rc"].parent), "<SDK_BIN>"),
    ]
    payload: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "source": {
            "sdist_sha256": EXPECTED_SDIST_SHA256,
            "actual_build_copy_source_scan": source_audit["source_scan"],
            "actual_build_copy_build_driver": prebuild_evidence["source"][
                "actual_build_copy_build_driver"
            ],
            "archive_source_scan": archive_audit,
            "prebuild_evidence_digest": prebuild_evidence["evidence_digest"],
        },
        "toolchain": {
            "msvc_version": args.msvc_version,
            "windows_sdk_version": args.sdk_version,
            "resolved_tools": tools,
        },
        "build": {
            "arguments": BUILD_ARGUMENTS,
            "environment_projection": {
                "pre_vcvars_path_roles": ["System32", "Windows"],
                "cleared": CLEARED_ENVIRONMENT,
                "vscmd_skip_sendtelemetry": "1",
                "vcvars": {
                    "script": "vcvarsall.bat",
                    "architecture": "x64",
                    "msvc_version": args.msvc_version,
                    "windows_sdk_version": args.sdk_version,
                },
            },
            "stdout": _canonical_log_fact(
                args.stdout_log,
                "probe build canonical stdout",
                replacements=replacements,
            ),
            "stderr": _canonical_log_fact(
                args.stderr_log,
                "probe build canonical stderr",
                replacements=replacements,
            ),
        },
        "result": {
            "machine": probe["machine"],
            "import_table_sha256": _json_digest(
                {"imports": probe["imports"], "delay_imports": probe["delay_imports"]}
            ),
            "delay_imports": [],
        },
    }
    payload["evidence_digest"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    payload["status"] = "RECORDED"
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-copy", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--prebuild-evidence", type=Path, required=True)
    parser.add_argument("--probe-runw", type=Path, required=True)
    parser.add_argument("--cl", type=Path, required=True)
    parser.add_argument("--link", type=Path, required=True)
    parser.add_argument("--rc", type=Path, required=True)
    parser.add_argument("--mt", type=Path, required=True)
    parser.add_argument("--msvc-version", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--stdout-log", type=Path, required=True)
    parser.add_argument("--stderr-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        record = build_probe_evidence(args)
    except (CandidateInputError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"INVALID_INPUT: {exc}")
        return 2
    output = args.output.resolve()
    output.write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"RECORDED: {record['evidence_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
