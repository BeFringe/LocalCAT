"""Independent strict consumer for the Collaborative Chunks source overlay."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "collaborative_chunks_current_source_evidence.json"
OWNED = (
    "chunk_controller_contracts.py",
    "collaborative_chunk_contracts.py",
    "collaborative_chunk_store.py",
    "collaborative_chunk_workspace_adapter.py",
    "collaborative_chunks.py",
    "collaborative_chunk_conflict.py",
    "chunk_controller_adapter.py",
    "qt_chunk_manager_dialog.py",
)
INTEGRATION = (
    "editor_controller.py",
    "qt_editor.py",
    "qt_editor_window.py",
    "multi_document_current_source_evidence.json",
)
FORBIDDEN = (
    "parser_",
    "project_package",
    "resource_",
    "tm_engine",
    "tm_store",
    "tm_candidate",
    "tm_retrieval",
    "tm_fuzzy",
    "tmx_",
)
SCHEMA = "localcat.collaborative-chunks.current-source-overlay"
TOP_LEVEL_KEYS = {
    "schema",
    "schema_version",
    "production_roots",
    "source_records",
    "integration_bindings",
    "multi_document_evidence_digest",
    "boundary",
    "evidence_digest",
}
BOUNDARY_KEYS = {"forbidden_import_prefixes", "violations"}
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
    if boundary["forbidden_import_prefixes"] != list(FORBIDDEN):
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
            if (
                type(record["path"]) is not str
                or type(record["bytes"]) is not int
                or type(record["sha256"]) is not str
                or type(record["imports"]) is not list
                or any(type(module) is not str for module in record["imports"])
            ):
                raise ValueError("overlay record types are invalid")
    return evidence


class CollaborativeChunksCurrentSourceEvidenceTests(unittest.TestCase):
    def test_overlay_is_closed_canonical_and_current(self) -> None:
        payload = EVIDENCE.read_bytes()
        evidence = _validate_closed_shape(json.loads(payload.decode("utf-8")))
        self.assertEqual(payload, _canonical(evidence))
        self.assertEqual(evidence["production_roots"], list(OWNED))
        self.assertEqual(evidence["source_records"], _records(OWNED))
        self.assertEqual(evidence["integration_bindings"], _records(INTEGRATION))
        upstream = json.loads(
            (ROOT / "multi_document_current_source_evidence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            evidence["multi_document_evidence_digest"],
            upstream["evidence_digest"],
        )
        unsigned = dict(evidence)
        digest = unsigned.pop("evidence_digest")
        self.assertEqual(hashlib.sha256(_canonical(unsigned)).hexdigest(), digest)

    def test_independent_consumer_rejects_schema_boundary_and_field_drift(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        extra = dict(evidence)
        extra["extra"] = True
        with self.assertRaises(ValueError):
            _validate_closed_shape(extra)
        missing_schema = dict(evidence)
        del missing_schema["schema"]
        with self.assertRaises(ValueError):
            _validate_closed_shape(missing_schema)
        changed_boundary = json.loads(json.dumps(evidence))
        changed_boundary["boundary"]["forbidden_import_prefixes"] = []
        with self.assertRaises(ValueError):
            _validate_closed_shape(changed_boundary)
        extra_record = json.loads(json.dumps(evidence))
        extra_record["source_records"][0]["extra"] = "drift"
        with self.assertRaises(ValueError):
            _validate_closed_shape(extra_record)

    def test_owned_roots_keep_payload_carrier_and_provider_authority_out(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["boundary"]["violations"], [])
        for record in evidence["source_records"]:
            for module in record["imports"]:
                self.assertFalse(module.startswith(FORBIDDEN), (record, module))
                self.assertNotIn("provider", module.lower())
                self.assertNotIn("sync", module.lower())


if __name__ == "__main__":
    unittest.main()
