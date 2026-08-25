#!/usr/bin/env python3
"""Generate and strictly reread the TMX current-source overlay."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "tmx_context_interchange_current_source_evidence.json"
SCHEMA = "localcat.tmx-context-interchange.current-source-overlay"
SCHEMA_VERSION = 1

OWNED_SOURCES = (
    "tmx_context_contracts.py",
    "tmx_context_interchange.py",
    "tmx_artifact_save.py",
    "tmx_export_scope_contracts.py",
    "tmx_export_coordinator.py",
)
INTEGRATION_BINDINGS = (
    "tmx_application.py",
    "tmx_resource_package_handler.py",
    "parser_tmx_codec.py",
    "resource_importer.py",
    "editor_controller.py",
    "chunk_controller_adapter.py",
    "resource_payload_port.py",
    "resource_package_contracts.py",
    "resource_portability.py",
    "qt_editor.py",
    "qt_editor_window.py",
    "qt_settings_dialog.py",
    "qt_tmx_export_dialog.py",
    "language_resource_portability_current_source_evidence.json",
)
FORBIDDEN_BY_SOURCE = {
    "tmx_context_contracts.py": (
        "collaborative_",
        "editor_",
        "project_",
        "qt_",
        "resource_",
        "tm_",
    ),
    "tmx_context_interchange.py": (
        "collaborative_",
        "editor_",
        "project_",
        "qt_",
        "resource_",
        "tm_",
    ),
    "tmx_artifact_save.py": (
        "collaborative_",
        "editor_",
        "parser_",
        "project_",
        "qt_",
        "resource_",
        "tm_",
    ),
    "tmx_export_scope_contracts.py": (
        "editor_",
        "qt_",
        "resource_",
    ),
    "tmx_export_coordinator.py": (
        "editor_",
        "qt_",
        "resource_",
    ),
}


class TmxCurrentSourceEvidenceError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _imports(path: Path) -> tuple[str, ...]:
    if path.suffix != ".py":
        return ()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    observed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            observed.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            observed.add(node.module)
    return tuple(sorted(observed))


def _record(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": len(payload),
        "sha256": _sha256(payload),
        "imports": list(_imports(path)),
    }


def _boundary_violations(
    records: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    violations: list[str] = []
    for record in records:
        path = str(record["path"])
        imports = record["imports"]
        assert type(imports) is list
        for module in imports:
            assert type(module) is str
            if module.startswith(FORBIDDEN_BY_SOURCE[path]):
                violations.append(f"{path}:{module}")
            if "provider" in module.lower():
                violations.append(f"{path}:{module}")
    return tuple(sorted(violations))


def build_evidence(root: Path = ROOT) -> dict[str, object]:
    if root.resolve() != ROOT.resolve():
        raise TmxCurrentSourceEvidenceError(
            "overlay must be built from the repository root"
        )
    owned = tuple(_record(root / path) for path in OWNED_SOURCES)
    integration = tuple(_record(root / path) for path in INTEGRATION_BINDINGS)
    violations = _boundary_violations(owned)
    if violations:
        raise TmxCurrentSourceEvidenceError(
            f"forbidden imports: {violations!r}"
        )
    upstream = json.loads(
        (
            root
            / "language_resource_portability_current_source_evidence.json"
        ).read_text(encoding="utf-8")
    )
    upstream_digest = upstream.get("evidence_digest")
    if type(upstream_digest) is not str or len(upstream_digest) != 64:
        raise TmxCurrentSourceEvidenceError("upstream evidence digest is invalid")
    unsigned: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "production_roots": list(OWNED_SOURCES),
        "source_records": list(owned),
        "integration_bindings": list(integration),
        "language_resource_portability_evidence_digest": upstream_digest,
        "boundary": {
            "forbidden_imports_by_source": {
                path: list(prefixes)
                for path, prefixes in sorted(FORBIDDEN_BY_SOURCE.items())
            },
            "violations": [],
        },
    }
    return {**unsigned, "evidence_digest": _sha256(_canonical(unsigned))}


def parse_evidence(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TmxCurrentSourceEvidenceError("overlay JSON is invalid") from error
    if type(value) is not dict:
        raise TmxCurrentSourceEvidenceError("overlay must be one object")
    if _canonical(value) != payload:
        raise TmxCurrentSourceEvidenceError("overlay JSON is not canonical")
    expected_keys = {
        "schema",
        "schema_version",
        "production_roots",
        "source_records",
        "integration_bindings",
        "language_resource_portability_evidence_digest",
        "boundary",
        "evidence_digest",
    }
    if set(value) != expected_keys:
        raise TmxCurrentSourceEvidenceError("overlay fields are not closed")
    if value["schema"] != SCHEMA or value["schema_version"] != SCHEMA_VERSION:
        raise TmxCurrentSourceEvidenceError("overlay schema is unsupported")
    digest = value["evidence_digest"]
    if type(digest) is not str or len(digest) != 64:
        raise TmxCurrentSourceEvidenceError("overlay digest is invalid")
    unsigned = dict(value)
    del unsigned["evidence_digest"]
    if _sha256(_canonical(unsigned)) != digest:
        raise TmxCurrentSourceEvidenceError("overlay digest mismatch")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = build_evidence()
    if arguments.check:
        observed = parse_evidence(EVIDENCE_PATH.read_bytes())
        if observed != expected:
            raise TmxCurrentSourceEvidenceError("TMX overlay is stale")
        print(json.dumps({"evidence": EVIDENCE_PATH.name, "status": "current"}))
        return 0
    EVIDENCE_PATH.write_bytes(_canonical(expected))
    if parse_evidence(EVIDENCE_PATH.read_bytes()) != expected:
        raise TmxCurrentSourceEvidenceError("overlay readback changed")
    print(
        json.dumps(
            {
                "evidence": EVIDENCE_PATH.name,
                "evidence_digest": expected["evidence_digest"],
                "production_root_count": len(OWNED_SOURCES),
                "status": "generated",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
