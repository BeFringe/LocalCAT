#!/usr/bin/env python3
"""Own and strictly consume Multi-Document current-source evidence.

The emitted document is deliberately timeless: it is a deterministic function
of the checked-out Python source tree.  The Cluster 0 characterization test is
the independent live consumer; it recomputes every inventory instead of
trusting this module's scanner.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import cast


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = REPOSITORY_ROOT / "multi_document_current_source_evidence.json"
SCHEMA = "localcat.multi-document.current-source-evidence"
SCHEMA_VERSION = 1

LEGACY_SOURCE_ROOTS = (
    "editor_project",
    "editor_controller",
    "project_search",
    "workspace_state",
    "parser_source",
    "qt_editor",
    "qt_editor_window",
    "qt_browse_group_dialog",
    "qt_localized_message_box",
    "editor_contracts",
    "parser_contracts",
    "parser_composition",
)
WORKSPACE_SOURCE_ROOTS = (
    "project_workspace_identity",
    "project_workspace_contracts",
    "editor_project_workspace_adapter",
    "project_workspace_intake",
    "project_workspace",
    "project_save",
    "project_package",
)
CURRENT_SOURCE_ROOTS = (*LEGACY_SOURCE_ROOTS, *WORKSPACE_SOURCE_ROOTS)
CURRENT_SOURCE_FILES = tuple(f"{module}.py" for module in CURRENT_SOURCE_ROOTS)
CLOSED_CONSUMER_MODULES = frozenset(CURRENT_SOURCE_ROOTS)

# These categories intentionally retain the original current-source meaning:
# exact calls to selected contract constructors, authority seams and
# serialization seams.  The roots scanned by those categories are now the
# complete 19-file runtime set.
KEY_CONSTRUCTORS = frozenset(
    {
        "CanonicalDocumentWrite",
        "CanonicalSegmentWrite",
        "CanonicalSerializeRequest",
        "CodecIdentity",
        "CodecPrivateMemberRef",
        "DocumentOriginWriteState",
        "DocumentProgress",
        "DocumentSaveResult",
        "DocumentSourceWriteResult",
        "EditingOverlayEntry",
        "EditorController",
        "EditorProject",
        "EditorSegment",
        "FlatProjectSegment",
        "OpenedParserInput",
        "OpenedProjectPackage",
        "OriginBinding",
        "OriginBindingDocument",
        "ParserApplicationSurface",
        "PendingRecoveryFacts",
        "PreparedCanonicalWrite",
        "PreparedProjectPackageImport",
        "PreparedReconciliationToken",
        "ProjectDocument",
        "ProjectOrigin",
        "ProjectPackageCodecAvailability",
        "ProjectPackageDocumentEntry",
        "ProjectPackageDocumentResult",
        "ProjectPackageExportReceipt",
        "ProjectPackageExportResult",
        "ProjectPackageImportPreview",
        "ProjectPackageImportReceipt",
        "ProjectPackageImportResult",
        "ProjectPackageManifest",
        "ProjectPackageMemberDigest",
        "ProjectPackageMemberReference",
        "ProjectPackagePersistenceBinding",
        "ProjectPackageRecoveryPreview",
        "ProjectPackageValidationReport",
        "ProjectProgress",
        "ProjectRecoveryReport",
        "ProjectSaveReport",
        "ProjectSaveService",
        "ProjectSearchRequest",
        "ProjectSearchService",
        "ProjectSegment",
        "ProjectSourceSegment",
        "ProjectWorkspace",
        "ProjectWorkspaceService",
        "QtEditorWindow",
        "ReadRequest",
        "RecoveryPreview",
        "ReconciliationPreview",
        "ReconciliationReceipt",
        "SegmentIdentity",
        "SelectedProjectDocumentsRequest",
        "SelectionRequest",
        "SourceReference",
        "StagedSelectedProjectDocuments",
        "TargetReference",
        "WorkspaceEditReceipt",
        "WorkspaceStateRepository",
        "WriterCapabilitySnapshot",
    }
)
KEY_AUTHORITY_CALLS = frozenset(
    {
        "_atomic_write_bytes",
        "_create_sealed_snapshot",
        "_materialize",
        "_port_validate_artifact",
        "_validate",
        "apply_workspace_reconciliation",
        "arm_publication",
        "commit_candidate",
        "commit_prepared_import",
        "commit_reconciliation",
        "complete_pending_commit",
        "create_canonical_serializer",
        "create_parser_application_surface",
        "create_reader",
        "create_sealed_snapshot",
        "create_workspace_service",
        "derive_device_local_origin_key",
        "derive_explicit_selected_document_id",
        "derive_legacy_single_json_document_id",
        "derive_legacy_single_json_project_id",
        "editing_state_digest_v1",
        "export_copy",
        "find_project",
        "inspect_pending_recovery",
        "issue_project_id",
        "load_project",
        "normalize_portable_ref_v1",
        "open_input",
        "open_project",
        "prepare_canonical",
        "prepare_import",
        "prepare_reconciliation",
        "publish_candidate",
        "read_recovery_candidate",
        "read_recovery_last_known_good",
        "readback_candidate",
        "remember_project",
        "rollback_candidate",
        "rollback_pending",
        "save_document",
        "save_project",
        "save_project_file",
        "save_workspace",
        "search",
        "serialize_canonical",
        "source_fingerprint_v1",
        "stage_candidate",
        "stage_selected_project_documents",
        "stage_workspace_reconciliation",
        "stream",
        "update_target",
        "validate_candidate",
        "validate_document_id",
        "validate_local_segment_id",
        "validate_portable_ref_collection",
        "validate_project_id",
        "validate_sha256",
        "verified_terminal",
        "workspace_content_digest_v1",
        "workspace_manifest_digest_v1",
        "write",
    }
)
KEY_SERIALIZATION_CALLS = frozenset(
    {
        "_read_state",
        "_write_state",
        "dumps",
        "loads",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TOP_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "production_roots",
        "runtime_sources",
        "python_sources",
        "closed_consumer_imports",
        "semantic_calls",
        "patches",
        "evidence_digest",
    }
)
_SUMMARY_KEYS = frozenset({"entry_count", "call_count", "digest"})
_INVENTORY_KEYS = frozenset({"records", *_SUMMARY_KEYS})


class EvidenceValidationError(ValueError):
    """The evidence is not a closed, canonical document for this owner."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _recursive_python_sources(root: Path) -> tuple[Path, ...]:
    observed: list[Path] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if any(
            part == "__pycache__" or part.startswith(".")
            for part in relative.parts
        ):
            continue
        observed.append(path)
    return tuple(sorted(observed, key=lambda item: item.relative_to(root).as_posix()))


def _path_digest(paths: tuple[Path, ...], root: Path) -> str:
    return _digest(tuple(path.relative_to(root).as_posix() for path in paths))


def _import_consumers(
    paths: tuple[Path, ...],
    root: Path,
) -> Counter[tuple[str, str, str, str | None]]:
    observed: Counter[tuple[str, str, str, str | None]] = Counter()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in CLOSED_CONSUMER_MODULES:
                for item in node.names:
                    observed[(relative, cast(str, node.module), item.name, item.asname)] += 1
            elif isinstance(node, ast.Import):
                for item in node.names:
                    if item.name in CLOSED_CONSUMER_MODULES:
                        observed[(relative, item.name, "<module>", item.asname)] += 1
    return observed


def _import_digest(
    counter: Counter[tuple[str, str, str, str | None]],
) -> str:
    records = sorted(
        (path, module, name, alias or "", count)
        for (path, module, name, alias), count in counter.items()
    )
    return _digest(records)


def _selected_calls(
    root: Path,
    relative: str,
    names: frozenset[str],
) -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted.rsplit(".", 1)[-1] in names:
            observed[(relative, dotted)] += 1
    return observed


def _semantic_inventory(
    root: Path,
    names: frozenset[str],
) -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    for relative in CURRENT_SOURCE_FILES:
        observed.update(_selected_calls(root, relative, names))
    return observed


def _patch_inventory(
    paths: tuple[Path, ...],
    root: Path,
) -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative.split("/", 1)[0] != "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _dotted_name(node.func).rsplit(".", 1)[-1] != "patch":
                continue
            for argument in node.args:
                if not (
                    isinstance(argument, ast.Constant)
                    and type(argument.value) is str
                ):
                    continue
                target = cast(str, argument.value)
                if any(
                    target == module or target.startswith(f"{module}.")
                    for module in CLOSED_CONSUMER_MODULES
                ):
                    observed[(relative, target)] += 1
    return observed


def _counter_records(
    counter: Counter[tuple[str, str]],
) -> list[dict[str, object]]:
    return [
        {"path": path, "symbol": symbol, "count": count}
        for (path, symbol), count in sorted(counter.items())
    ]


def _inventory(counter: Counter[tuple[str, str]]) -> dict[str, object]:
    records = _counter_records(counter)
    return {
        "records": records,
        "entry_count": len(records),
        "call_count": sum(counter.values()),
        "digest": _digest(records),
    }


def _import_summary(
    counter: Counter[tuple[str, str, str, str | None]],
) -> dict[str, object]:
    return {
        "entry_count": len(counter),
        "call_count": sum(counter.values()),
        "digest": _import_digest(counter),
    }


def build_evidence(root: Path = REPOSITORY_ROOT) -> dict[str, object]:
    """Build the deterministic evidence payload for ``root``."""

    root = root.resolve()
    missing = [relative for relative in CURRENT_SOURCE_FILES if not (root / relative).is_file()]
    if missing:
        raise EvidenceValidationError(f"missing current-source roots: {missing!r}")

    all_sources = _recursive_python_sources(root)
    production_sources = tuple(
        path for path in all_sources if path.relative_to(root).parts[0] != "tests"
    )
    test_sources = tuple(
        path for path in all_sources if path.relative_to(root).parts[0] == "tests"
    )
    production_imports = _import_consumers(production_sources, root)
    test_imports = _import_consumers(test_sources, root)
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "production_roots": list(CURRENT_SOURCE_ROOTS),
        "runtime_sources": [
            {
                "path": relative,
                "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
            }
            for relative in CURRENT_SOURCE_FILES
        ],
        "python_sources": {
            "entry_count": len(all_sources),
            "path_digest": _path_digest(all_sources, root),
        },
        "closed_consumer_imports": {
            "production": _import_summary(production_imports),
            "tests": _import_summary(test_imports),
        },
        "semantic_calls": {
            "constructors": _inventory(
                _semantic_inventory(root, KEY_CONSTRUCTORS)
            ),
            "authority": _inventory(
                _semantic_inventory(root, KEY_AUTHORITY_CALLS)
            ),
            "serialization": _inventory(
                _semantic_inventory(root, KEY_SERIALIZATION_CALLS)
            ),
        },
        "patches": _inventory(_patch_inventory(all_sources, root)),
    }
    payload["evidence_digest"] = _digest(payload)
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    observed: dict[str, object] = {}
    for key, value in pairs:
        if key in observed:
            raise EvidenceValidationError(f"duplicate JSON key: {key}")
        observed[key] = value
    return observed


def _require_object(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise EvidenceValidationError(f"{label} must be an object")
    result = cast(dict[str, object], value)
    if set(result) != keys:
        raise EvidenceValidationError(f"{label} fields are not closed")
    return result


def _require_exact_int(value: object, label: str) -> int:
    if type(value) is not int or cast(int, value) < 0:
        raise EvidenceValidationError(f"{label} must be a nonnegative integer")
    return cast(int, value)


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(cast(str, value)) is None:
        raise EvidenceValidationError(f"{label} must be a canonical sha256")
    return cast(str, value)


def _validate_summary(value: object, label: str) -> dict[str, object]:
    summary = _require_object(value, _SUMMARY_KEYS, label)
    _require_exact_int(summary["entry_count"], f"{label}.entry_count")
    _require_exact_int(summary["call_count"], f"{label}.call_count")
    _require_sha256(summary["digest"], f"{label}.digest")
    return summary


def _validate_inventory(value: object, label: str) -> dict[str, object]:
    inventory = _require_object(value, _INVENTORY_KEYS, label)
    records = inventory["records"]
    if type(records) is not list:
        raise EvidenceValidationError(f"{label}.records must be an array")
    normalized: list[dict[str, object]] = []
    previous: tuple[str, str] | None = None
    for index, value_record in enumerate(cast(list[object], records)):
        record = _require_object(
            value_record,
            frozenset({"path", "symbol", "count"}),
            f"{label}.records[{index}]",
        )
        path = record["path"]
        symbol = record["symbol"]
        if type(path) is not str or not path:
            raise EvidenceValidationError(f"{label}.records[{index}].path is invalid")
        if type(symbol) is not str or not symbol:
            raise EvidenceValidationError(f"{label}.records[{index}].symbol is invalid")
        count = _require_exact_int(record["count"], f"{label}.records[{index}].count")
        if count == 0:
            raise EvidenceValidationError(f"{label}.records[{index}].count must be positive")
        identity = (cast(str, path), cast(str, symbol))
        if previous is not None and identity <= previous:
            raise EvidenceValidationError(f"{label}.records are not canonical")
        previous = identity
        normalized.append({"path": path, "symbol": symbol, "count": count})
    if _require_exact_int(inventory["entry_count"], f"{label}.entry_count") != len(normalized):
        raise EvidenceValidationError(f"{label}.entry_count is stale")
    if _require_exact_int(inventory["call_count"], f"{label}.call_count") != sum(
        cast(int, record["count"]) for record in normalized
    ):
        raise EvidenceValidationError(f"{label}.call_count is stale")
    digest = _require_sha256(inventory["digest"], f"{label}.digest")
    if digest != _digest(normalized):
        raise EvidenceValidationError(f"{label}.digest is not canonical")
    return inventory


def parse_evidence_bytes(raw: bytes) -> dict[str, object]:
    """Strictly parse one canonical evidence document.

    Duplicate keys, non-canonical JSON bytes, open schemas, wrong exact types and
    stale/non-lowercase digests all fail closed.
    """

    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except EvidenceValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("evidence is not strict UTF-8 JSON") from error

    evidence = _require_object(value, _TOP_KEYS, "evidence")
    if evidence["schema"] != SCHEMA or type(evidence["schema"]) is not str:
        raise EvidenceValidationError("evidence schema is stale")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != SCHEMA_VERSION:
        raise EvidenceValidationError("evidence schema version is stale")

    roots = evidence["production_roots"]
    if type(roots) is not list or any(type(item) is not str for item in cast(list[object], roots)):
        raise EvidenceValidationError("production_roots must be a string array")
    if tuple(cast(list[str], roots)) != CURRENT_SOURCE_ROOTS:
        raise EvidenceValidationError("production_roots are not the final closed set")

    runtime_sources = evidence["runtime_sources"]
    if type(runtime_sources) is not list:
        raise EvidenceValidationError("runtime_sources must be an array")
    expected_paths = iter(CURRENT_SOURCE_FILES)
    for index, raw_record in enumerate(cast(list[object], runtime_sources)):
        record = _require_object(
            raw_record,
            frozenset({"path", "sha256"}),
            f"runtime_sources[{index}]",
        )
        try:
            expected = next(expected_paths)
        except StopIteration as error:
            raise EvidenceValidationError("runtime_sources has extra records") from error
        if type(record["path"]) is not str or record["path"] != expected:
            raise EvidenceValidationError("runtime_sources path order is not canonical")
        _require_sha256(record["sha256"], f"runtime_sources[{index}].sha256")
    try:
        next(expected_paths)
    except StopIteration:
        pass
    else:
        raise EvidenceValidationError("runtime_sources is missing records")

    python_sources = _require_object(
        evidence["python_sources"],
        frozenset({"entry_count", "path_digest"}),
        "python_sources",
    )
    _require_exact_int(python_sources["entry_count"], "python_sources.entry_count")
    _require_sha256(python_sources["path_digest"], "python_sources.path_digest")

    imports = _require_object(
        evidence["closed_consumer_imports"],
        frozenset({"production", "tests"}),
        "closed_consumer_imports",
    )
    _validate_summary(imports["production"], "closed_consumer_imports.production")
    _validate_summary(imports["tests"], "closed_consumer_imports.tests")

    semantic = _require_object(
        evidence["semantic_calls"],
        frozenset({"constructors", "authority", "serialization"}),
        "semantic_calls",
    )
    for category in ("constructors", "authority", "serialization"):
        _validate_inventory(semantic[category], f"semantic_calls.{category}")
    _validate_inventory(evidence["patches"], "patches")

    evidence_digest = _require_sha256(evidence["evidence_digest"], "evidence_digest")
    unsigned = dict(evidence)
    del unsigned["evidence_digest"]
    if evidence_digest != _digest(unsigned):
        raise EvidenceValidationError("evidence_digest is not canonical")
    canonical = (_canonical_json(evidence) + "\n").encode("utf-8")
    if raw != canonical:
        raise EvidenceValidationError("evidence JSON serialization is not canonical")
    return evidence


def load_evidence(path: Path = EVIDENCE_PATH) -> dict[str, object]:
    return parse_evidence_bytes(path.read_bytes())


def write_evidence(
    path: Path = EVIDENCE_PATH,
    *,
    root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    if path.resolve() != EVIDENCE_PATH.resolve():
        raise EvidenceValidationError("evidence must use the canonical output path")
    evidence = build_evidence(root)
    raw = (_canonical_json(evidence) + "\n").encode("utf-8")
    path.write_bytes(raw)
    observed = load_evidence(path)
    if observed != evidence:
        raise EvidenceValidationError("evidence readback changed")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="strictly consume and live-recompute without writing",
    )
    arguments = parser.parse_args()
    if arguments.check:
        observed = load_evidence()
        expected = build_evidence()
        if observed != expected:
            raise EvidenceValidationError("current-source evidence is stale")
        print(_canonical_json({"evidence": EVIDENCE_PATH.name, "status": "current"}))
        return 0
    evidence = write_evidence()
    print(
        _canonical_json(
            {
                "evidence": EVIDENCE_PATH.name,
                "evidence_digest": evidence["evidence_digest"],
                "production_root_count": len(CURRENT_SOURCE_ROOTS),
                "status": "generated",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
