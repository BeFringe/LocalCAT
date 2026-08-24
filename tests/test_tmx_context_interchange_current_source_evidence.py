"""Independent strict consumer for the TMX current-source overlay."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tmx_context_interchange_current_source_evidence.json"
OWNED = (
    "tmx_context_contracts.py",
    "tmx_context_interchange.py",
    "tmx_artifact_save.py",
    "tmx_export_scope_contracts.py",
    "tmx_export_coordinator.py",
)
INTEGRATION = (
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
FORBIDDEN = {
    "tmx_context_contracts.py": (
        "collaborative_", "editor_", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_context_interchange.py": (
        "collaborative_", "editor_", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_artifact_save.py": (
        "collaborative_", "editor_", "parser_", "project_", "qt_",
        "resource_", "tm_",
    ),
    "tmx_export_scope_contracts.py": ("editor_", "qt_", "resource_"),
    "tmx_export_coordinator.py": ("editor_", "qt_", "resource_"),
}
SCHEMA = "localcat.tmx-context-interchange.current-source-overlay"
TOP_LEVEL_KEYS = {
    "schema",
    "schema_version",
    "production_roots",
    "source_records",
    "integration_bindings",
    "language_resource_portability_evidence_digest",
    "boundary",
    "evidence_digest",
}
BOUNDARY_KEYS = {"forbidden_imports_by_source", "violations"}
RECORD_KEYS = {"path", "bytes", "sha256", "imports"}


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


def _imports(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return sorted(found)


def _records(paths: tuple[str, ...]) -> list[dict[str, object]]:
    records = []
    for relative in paths:
        path = ROOT / relative
        payload = path.read_bytes()
        records.append(
            {
                "path": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "imports": _imports(path),
            }
        )
    return records


def _validate_closed_shape(evidence: object) -> dict[str, object]:
    if type(evidence) is not dict or set(evidence) != TOP_LEVEL_KEYS:
        raise ValueError("overlay fields are not closed")
    if evidence["schema"] != SCHEMA or evidence["schema_version"] != 1:
        raise ValueError("overlay schema is unsupported")
    boundary = evidence["boundary"]
    if type(boundary) is not dict or set(boundary) != BOUNDARY_KEYS:
        raise ValueError("overlay boundary fields are not closed")
    expected_forbidden = {
        path: list(prefixes) for path, prefixes in sorted(FORBIDDEN.items())
    }
    if boundary["forbidden_imports_by_source"] != expected_forbidden:
        raise ValueError("overlay forbidden imports changed")
    if boundary["violations"] != []:
        raise ValueError("overlay boundary contains violations")
    for field in ("source_records", "integration_bindings"):
        records = evidence[field]
        if type(records) is not list:
            raise ValueError("overlay records must be lists")
        for record in records:
            if type(record) is not dict or set(record) != RECORD_KEYS:
                raise ValueError("overlay record fields are not closed")
    return evidence


class TmxCurrentSourceEvidenceTests(unittest.TestCase):
    def test_overlay_is_closed_canonical_and_current(self) -> None:
        payload = EVIDENCE.read_bytes()
        evidence = _validate_closed_shape(json.loads(payload.decode("utf-8")))
        self.assertEqual(payload, _canonical(evidence))
        self.assertEqual(evidence["production_roots"], list(OWNED))
        self.assertEqual(evidence["source_records"], _records(OWNED))
        self.assertEqual(evidence["integration_bindings"], _records(INTEGRATION))
        upstream = json.loads(
            (
                ROOT
                / "language_resource_portability_current_source_evidence.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            evidence["language_resource_portability_evidence_digest"],
            upstream["evidence_digest"],
        )
        unsigned = dict(evidence)
        digest = unsigned.pop("evidence_digest")
        self.assertEqual(hashlib.sha256(_canonical(unsigned)).hexdigest(), digest)

    def test_independent_consumer_rejects_shape_and_boundary_drift(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        extra = dict(evidence)
        extra["extra"] = True
        with self.assertRaises(ValueError):
            _validate_closed_shape(extra)
        changed_boundary = json.loads(json.dumps(evidence))
        changed_boundary["boundary"]["forbidden_imports_by_source"] = {}
        with self.assertRaises(ValueError):
            _validate_closed_shape(changed_boundary)
        extra_record = json.loads(json.dumps(evidence))
        extra_record["source_records"][0]["extra"] = "drift"
        with self.assertRaises(ValueError):
            _validate_closed_shape(extra_record)

    def test_owned_roots_keep_ui_resourcepackage_and_provider_authority_out(
        self,
    ) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["boundary"]["violations"], [])
        for record in evidence["source_records"]:
            forbidden = FORBIDDEN[record["path"]]
            for module in record["imports"]:
                self.assertFalse(module.startswith(forbidden), (record, module))
                self.assertNotIn("provider", module.lower())


if __name__ == "__main__":
    unittest.main()
