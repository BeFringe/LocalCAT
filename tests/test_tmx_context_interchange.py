from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from parser_composition import create_parser_application_surface
from parser_contracts import (
    EffectivePurpose,
    ReadRequest,
    ResourceRecord,
    SelectionRequest,
    SourceReference,
    TMX_LEVEL1_V1,
    TmxReadOptions,
)
from tmx_context_contracts import (
    TmxContextError,
    TmxEffectiveLocales,
    TmxExportUnit,
    TmxLossDisposition,
    TmxOrderedProp,
    TmxPropScope,
    TmxProvenanceEntry,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_context_interchange import (
    cold_validate_tmx_file,
    inspect_tmx_payload,
    prepare_tmx_payload,
)


class TmxContextInterchangeTests(unittest.TestCase):
    def binding(self, count: int, *, attached: int | None = None) -> TmxScopeBinding:
        return TmxScopeBinding(
            TmxScopeKind.ENTIRE_PROJECT,
            "project-1",
            hashlib.sha256(b"exact-scope-facts").hexdigest(),
            count,
            project_id="project-1",
            document_count=2,
            attached_count=count if attached is None else attached,
        )

    def test_writer_is_deterministic_level1_and_does_not_deduplicate(self) -> None:
        units = (
            TmxExportUnit("doc-a:1", "same & <source>", "same target", True),
            TmxExportUnit("doc-b:9", "same & <source>", "same target", True),
        )
        locales = TmxEffectiveLocales("en-US", "zh-CN")
        first = prepare_tmx_payload(self.binding(2), locales, units)
        second = prepare_tmx_payload(self.binding(2), locales, units)
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.proof.payload_digest, second.proof.payload_digest)
        text = first.data.decode("utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        self.assertNotIn("<!DOCTYPE", text)
        self.assertNotIn("<!ENTITY", text)
        self.assertEqual(text.count("<tu tuid="), 2)
        self.assertEqual(len(set(part.split('"', 1)[0] for part in text.split('tuid="')[1:])), 2)
        self.assertIn("same &amp; &lt;source&gt;", text)

    def test_localcat_registry_then_unknown_props_preserve_order_duplicates_and_scope(self) -> None:
        units = (
            TmxExportUnit(
                "record-1",
                "Hello",
                "你好",
                True,
                speaker="A",
                provenance=(TmxProvenanceEntry("origin", "legacy"),),
                imported_props=(
                    TmxOrderedProp("vendor-duplicate", "one"),
                    TmxOrderedProp("vendor-duplicate", "two"),
                    TmxOrderedProp("vendor-source", "three", "en-US", TmxPropScope.SOURCE_TUV),
                    TmxOrderedProp("vendor-target", "four", "zh-CN", TmxPropScope.TARGET_TUV),
                ),
            ),
        )
        payload = prepare_tmx_payload(
            self.binding(1), TmxEffectiveLocales("en-US", "zh-CN"), units
        )
        text = payload.data.decode("utf-8")
        expected_types = [
            "x-localcat-speaker",
            "x-localcat-confirmed",
            "x-localcat-provenance",
            "vendor-duplicate",
            "vendor-duplicate",
            "vendor-source",
            "vendor-target",
        ]
        observed = [piece.split('"', 1)[0] for piece in text.split('<prop type="')[1:]]
        self.assertEqual(observed, expected_types)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "props.tmx"
            path.write_bytes(payload.data)
            cold_validate_tmx_file(path, payload.proof)
            self.assertEqual(inspect_tmx_payload(path), payload.proof)

    def test_empty_detached_and_warning_policy_is_explicit(self) -> None:
        units = (
            TmxExportUnit("1", "one", "", True),
            TmxExportUnit("2", "two", "二", True, attached=False),
            TmxExportUnit("3", "same", "same", False),
        )
        payload = prepare_tmx_payload(
            self.binding(3, attached=2), TmxEffectiveLocales("en", "zh"), units
        )
        report = payload.proof.loss_report
        self.assertEqual((report.included_count, report.excluded_count), (1, 2))
        by_code = {count.code: (count.disposition, count.count) for count in report.counts}
        self.assertEqual(by_code["empty_target"], (TmxLossDisposition.EXCLUDED, 1))
        self.assertEqual(by_code["detached_member"], (TmxLossDisposition.EXCLUDED, 1))
        self.assertEqual(by_code["unconfirmed_target"], (TmxLossDisposition.WARNING, 1))
        self.assertEqual(by_code["source_equals_target"], (TmxLossDisposition.WARNING, 1))
        self.assertNotIn("same", repr(report))

    def test_inline_control_and_unroundtrippable_whitespace_are_blocking(self) -> None:
        for unit in (
            TmxExportUnit("1", "source", "target", True, has_inline_xml=True),
            TmxExportUnit("1", "source\x00", "target", True),
            TmxExportUnit("1", " source", "target", True),
            TmxExportUnit(
                "1", "source", "target", True,
                imported_props=(TmxOrderedProp("bad", "value\x00"),),
            ),
        ):
            with self.subTest(unit=unit.unit_identity), self.assertRaises(TmxContextError) as captured:
                prepare_tmx_payload(
                    self.binding(1), TmxEffectiveLocales("en", "zh"), (unit,)
                )
            self.assertEqual(captured.exception.code, "TMX.BLOCKING_LOSS")
            self.assertGreater(captured.exception.loss_report.blocking_count, 0)

    def test_cold_validation_rejects_byte_or_semantic_drift(self) -> None:
        payload = prepare_tmx_payload(
            self.binding(1),
            TmxEffectiveLocales("en", "zh"),
            (TmxExportUnit("1", "source", "target", True),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "cold.tmx"
            path.write_bytes(payload.data)
            cold_validate_tmx_file(path, payload.proof)
            path.write_bytes(payload.data.replace(b"target", b"tamper"))
            with self.assertRaises(TmxContextError) as captured:
                cold_validate_tmx_file(path, payload.proof)
            self.assertEqual(captured.exception.code, "TMX.COLD_DIGEST_MISMATCH")

    def test_stateless_inspection_discovers_exact_locales_and_rejects_unsafe_inventory(self) -> None:
        payload = prepare_tmx_payload(
            self.binding(1),
            TmxEffectiveLocales("en-US", "zh-CN"),
            (TmxExportUnit("1", "same", "same", False),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "inspect.tmx"
            path.write_bytes(payload.data)
            inspected = inspect_tmx_payload(path)
            self.assertEqual(inspected.effective_locales, TmxEffectiveLocales("en-US", "zh-CN"))
            self.assertEqual(inspected.payload_digest, payload.proof.payload_digest)
            self.assertEqual(inspected.parser_content_digest, payload.proof.parser_content_digest)
            self.assertEqual(inspected.loss_report, payload.proof.loss_report)

            unsafe = root / "unsafe.tmx"
            unsafe.write_bytes(
                b'<?xml version="1.0"?><!DOCTYPE tmx [<!ENTITY x "y">]><tmx version="1.4"/>'
            )
            with self.assertRaises(TmxContextError) as captured:
                inspect_tmx_payload(unsafe)
            self.assertEqual(captured.exception.code, "TMX.COLD_UNSAFE_XML")

    def test_writer_has_no_forbidden_owner_dependencies(self) -> None:
        text = Path("tmx_context_interchange.py").read_text(encoding="utf-8")
        for forbidden in ("workspace_", "chunk_", "resource_package", "qt_"):
            self.assertNotIn(f"import {forbidden}", text)
            self.assertNotIn(f"from {forbidden}", text)


if __name__ == "__main__":
    unittest.main()
