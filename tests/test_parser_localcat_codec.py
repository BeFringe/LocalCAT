"""Wave 2 contracts for the sole LocalCAT JSON/TXT grammar authority."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_contracts import (
    CanonicalDocumentWrite,
    CanonicalSegmentWrite,
    CanonicalSerializeRequest,
    CodecCapabilities,
    EffectivePurpose,
    IssueSeverity,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    RawSpeaker,
    ReadRequest,
    SourceReference,
    TargetPresence,
    TargetReference,
    TranslationState,
    ValidationOutcome,
)
from parser_source import (
    CancellationToken,
    ParserSessionError,
    atomic_write_bytes,
    create_sealed_snapshot,
    materialize,
    validate,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "project"
PAYLOAD_ROOT = FIXTURE_ROOT / "payloads"


def _fixture_bytes(name: str) -> bytes:
    path = PAYLOAD_ROOT / name
    stored = path.read_bytes()
    return bytes.fromhex(stored.decode("ascii")) if path.suffix == ".hex" else stored


class _LocalCatFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _snapshot(
        self,
        name: str,
        data: bytes,
        descriptor,
        *,
        display_hint: str | None = None,
    ):
        path = self.root / name
        path.write_bytes(data)
        snapshot = create_sealed_snapshot(
            SourceReference(
                safe_root=str(self.root),
                selected_path=str(path),
                display_hint=display_hint or name,
            ),
            limit_profile=descriptor.limit_profile,
        )
        self.addCleanup(snapshot.close)
        return snapshot


class LocalCatDescriptorTests(_LocalCatFixture):
    def test_descriptors_publish_frozen_format_specific_limits_and_capabilities(self) -> None:
        from parser_contracts import FOUNDATION_GUARDED_ISSUE_CODES
        from parser_localcat_codec import LINE_TEXT_DESCRIPTOR, LOCALCAT_JSON_DESCRIPTOR

        json_descriptor = LOCALCAT_JSON_DESCRIPTOR
        text_descriptor = LINE_TEXT_DESCRIPTOR
        self.assertEqual(json_descriptor.format_id, LOCALCAT_JSON_V1)
        self.assertEqual(text_descriptor.format_id, LINE_TEXT_V1)
        self.assertEqual(json_descriptor.purpose, EffectivePurpose.PROJECT_DOCUMENT)
        self.assertEqual(text_descriptor.purpose, EffectivePurpose.PROJECT_DOCUMENT)
        self.assertEqual(json_descriptor.identity.codec_version, "1")
        self.assertEqual(text_descriptor.identity.codec_version, "1")
        self.assertEqual(json_descriptor.limit_profile.profile_id, "localcat-json-v1")
        self.assertEqual(text_descriptor.limit_profile.profile_id, "line-text-v1")
        self.assertEqual(json_descriptor.limit_profile.max_input_bytes, 100 * 1024 * 1024)
        self.assertEqual(json_descriptor.limit_profile.max_records, 100_000)
        self.assertEqual(text_descriptor.limit_profile.max_records, 1_000_000)
        self.assertEqual(text_descriptor.limit_profile.max_materialized_records, 100_000)
        self.assertTrue(set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(
            json_descriptor.limit_profile.declared_issue_codes
        ))
        self.assertTrue(set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(
            text_descriptor.limit_profile.declared_issue_codes
        ))
        self.assertEqual(
            json_descriptor.capabilities,
            CodecCapabilities(
                readable=True,
                validatable=True,
                canonical_write=True,
                source_round_trip_write=False,
                streaming_input=False,
                iterator_view=True,
                materialized_view=True,
                format_profile="localcat-json-v1",
            ),
        )
        self.assertEqual(
            text_descriptor.capabilities,
            CodecCapabilities(
                readable=True,
                validatable=True,
                canonical_write=False,
                source_round_trip_write=False,
                streaming_input=True,
                iterator_view=True,
                materialized_view=True,
                format_profile="line-text-v1",
            ),
        )
        self.assertIsNotNone(json_descriptor.canonical_serializer_factory)
        self.assertIsNone(text_descriptor.canonical_serializer_factory)

    def test_profiles_are_codec_owned_not_aliases(self) -> None:
        from parser_localcat_codec import LINE_TEXT_DESCRIPTOR, LOCALCAT_JSON_DESCRIPTOR

        self.assertIsNot(
            LOCALCAT_JSON_DESCRIPTOR.limit_profile,
            LINE_TEXT_DESCRIPTOR.limit_profile,
        )
        self.assertNotEqual(
            LOCALCAT_JSON_DESCRIPTOR.limit_profile.profile_id,
            LINE_TEXT_DESCRIPTOR.limit_profile.profile_id,
        )


class LocalCatJsonReaderTests(_LocalCatFixture):
    def _materialize(self, name: str, payload: bytes, *, descriptor=None):
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        active_descriptor = descriptor or LOCALCAT_JSON_DESCRIPTOR
        snapshot = self._snapshot(name, payload, active_descriptor)
        return materialize(
            LocalCatJsonReader(active_descriptor),
            snapshot,
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
        )

    def test_array_golden_uses_file_stem_and_preserves_presence_speaker_and_order(self) -> None:
        result = self._materialize(
            "chapter-seven.json",
            _fixture_bytes("localcat-array-valid.json"),
        )

        self.assertEqual(result.header.name, "chapter-seven")
        self.assertEqual(result.header.source_locale, "en-US")
        self.assertEqual(result.header.target_locale, "zh-CN")
        self.assertEqual([segment.local_id for segment in result.records], ["intro", "segment-2"])
        self.assertEqual([segment.source for segment in result.records], ["Hello  world", "Second source"])
        self.assertEqual(result.records[0].target, "你好")
        self.assertEqual(result.records[0].target_presence, TargetPresence.PRESENT)
        self.assertEqual(result.records[0].translation_state, TranslationState.CONFIRMED)
        self.assertEqual(result.records[0].speaker, RawSpeaker("Narrator"))
        self.assertIsNone(result.records[1].target)
        self.assertEqual(result.records[1].target_presence, TargetPresence.MISSING)
        self.assertEqual(result.records[1].translation_state, TranslationState.UNCONFIRMED)
        self.assertEqual(result.records[1].speaker, RawSpeaker(""))
        self.assertEqual(result.terminal.record_count, 2)

    def test_object_golden_trims_header_and_keeps_explicit_empty_target(self) -> None:
        result = self._materialize(
            "ignored-fallback.json",
            _fixture_bytes("localcat-object-valid.json"),
        )

        self.assertEqual(result.header.name, "Chapter One")
        self.assertEqual(result.header.source_locale, "en-US")
        self.assertEqual(result.header.target_locale, "zh-CN")
        self.assertEqual(result.records[0].source, "First source")
        self.assertEqual(result.records[0].target, "")
        self.assertEqual(result.records[0].target_presence, TargetPresence.EXPLICIT_EMPTY)
        self.assertEqual(result.records[0].translation_state, TranslationState.UNCONFIRMED)

    def test_missing_or_empty_object_header_fields_use_compatibility_defaults(self) -> None:
        payload = json.dumps(
            {
                "name": "  ",
                "source_locale": None,
                "target_locale": "",
                "segments": [{"source": " Source "}],
            }
        ).encode()
        result = self._materialize("fallback-name.json", payload)

        self.assertEqual(result.header.name, "fallback-name")
        self.assertEqual(result.header.source_locale, "en-US")
        self.assertEqual(result.header.target_locale, "zh-CN")
        self.assertEqual(result.records[0].local_id, "segment-1")
        self.assertEqual(result.records[0].target_presence, TargetPresence.MISSING)

    def test_bom_is_accepted_and_complete_sealed_input_is_consumed(self) -> None:
        result = self._materialize(
            "bom.json",
            _fixture_bytes("localcat-json-bom-valid.hex"),
        )
        self.assertEqual(result.terminal.record_count, 1)

    def test_validation_and_materialization_share_the_same_snapshot_grammar(self) -> None:
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        snapshot = self._snapshot(
            "same-grammar.json",
            _fixture_bytes("localcat-array-valid.json"),
            LOCALCAT_JSON_DESCRIPTOR,
        )
        request = ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1)
        report = validate(LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR), snapshot, request)
        result = materialize(LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR), snapshot, request)

        self.assertEqual(report.outcome, ValidationOutcome.SUCCESS)
        self.assertIsNotNone(report.terminal)
        self.assertEqual(report.terminal.source, result.terminal.source)
        self.assertEqual(report.terminal.record_count, len(result.records))
        self.assertEqual(report.issue_counts, ())
        self.assertEqual(result.issues, ())

    def test_array_fallback_name_comes_from_rooted_source_not_display_hint(self) -> None:
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        snapshot = self._snapshot(
            "rooted-chapter.json",
            b'[{"source":"safe"}]',
            LOCALCAT_JSON_DESCRIPTOR,
            display_hint="spoofed-name.json",
        )
        result = materialize(
            LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR),
            snapshot,
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
        )
        self.assertEqual(result.header.name, "rooted-chapter")

    def test_array_fallback_name_preserves_file_stem_whitespace(self) -> None:
        result = self._materialize(
            " chapter name .json",
            b'[{"source":"safe"}]',
        )
        self.assertEqual(result.header.name, Path(" chapter name .json").stem)

    def test_all_invalid_segments_fail_the_whole_document_without_terminal(self) -> None:
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        cases = (
            (b'[{"source":"ok"},{"source":7}]', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'[{"source":"ok"},{"source":"  "}]', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'[{"source":"ok","target":false}]', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'[{"source":"ok","speaker":[]}]', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'[{"source":"ok","confirmed":1}]', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'["not-an-object"]', "PARSER.SYNTAX.INVALID_FIELD"),
            (_fixture_bytes("localcat-invalid-segment.json"), "PARSER.SYNTAX.DUPLICATE_LOCAL_ID"),
            (b'{"segments":[]}', "PARSER.SYNTAX.EMPTY_INPUT"),
            (b'{"segments":{}}', "PARSER.SYNTAX.INVALID_FIELD"),
            (b'null', "PARSER.SYNTAX.INVALID_FIELD"),
        )
        for index, (payload, code) in enumerate(cases):
            with self.subTest(index=index, code=code):
                snapshot = self._snapshot(f"bad-{index}.json", payload, LOCALCAT_JSON_DESCRIPTOR)
                report = validate(
                    LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR),
                    snapshot,
                    ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
                )
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertIsNone(report.terminal)
                self.assertEqual(report.issues[-1].code, code)
                self.assertTrue(all(issue.severity is IssueSeverity.FATAL for issue in report.issues))
                self.assertNotIn("ok", repr(report.issues))

    def test_fatal_tail_encoding_depth_field_input_and_record_limits_are_stable(self) -> None:
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        tiny_record_descriptor = replace(
            LOCALCAT_JSON_DESCRIPTOR,
            limit_profile=replace(
                LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                profile_id="localcat-json-v1-tiny-record-test",
                max_records=1,
                max_materialized_records=1,
            ),
            capabilities=replace(
                LOCALCAT_JSON_DESCRIPTOR.capabilities,
                format_profile="localcat-json-v1-tiny-record-test",
            ),
        )
        tiny_depth_descriptor = replace(
            LOCALCAT_JSON_DESCRIPTOR,
            limit_profile=replace(
                LOCALCAT_JSON_DESCRIPTOR.limit_profile,
                profile_id="localcat-json-v1-tiny-depth-test",
                max_structure_depth=2,
            ),
            capabilities=replace(
                LOCALCAT_JSON_DESCRIPTOR.capabilities,
                format_profile="localcat-json-v1-tiny-depth-test",
            ),
        )
        cases = (
            (_fixture_bytes("localcat-fatal-tail.json"), LOCALCAT_JSON_DESCRIPTOR, "PARSER.SYNTAX.MALFORMED"),
            (_fixture_bytes("localcat-json-invalid-utf8.hex"), LOCALCAT_JSON_DESCRIPTOR, "PARSER.SOURCE.ENCODING_FAILED"),
            (b'[{"source":"a"},{"source":"b"}]', tiny_record_descriptor, "PARSER.LIMIT.RECORD"),
            (b'[[[{"source":"a"}]]]', tiny_depth_descriptor, "PARSER.LIMIT.DEPTH"),
        )
        for index, (payload, descriptor, code) in enumerate(cases):
            with self.subTest(code=code):
                snapshot = self._snapshot(f"failure-{index}.json", payload, descriptor)
                report = validate(
                    LocalCatJsonReader(descriptor),
                    snapshot,
                    ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
                )
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertIsNone(report.terminal)
                self.assertEqual(report.issues[-1].code, code)

    def test_cancelled_real_snapshot_never_authorizes_terminal(self) -> None:
        from parser_localcat_codec import LocalCatJsonReader, LOCALCAT_JSON_DESCRIPTOR

        snapshot = self._snapshot("cancel.json", b'[{"source":"safe"}]', LOCALCAT_JSON_DESCRIPTOR)
        token = CancellationToken()
        token.cancel()
        report = validate(
            LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR),
            snapshot,
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
            cancellation=token,
        )
        self.assertEqual(report.outcome, ValidationOutcome.CANCELLED)
        self.assertIsNone(report.terminal)
        self.assertEqual(report.issues[-1].code, "PARSER.SOURCE.CANCELLED")


class LineTextReaderTests(_LocalCatFixture):
    def _materialize(self, name: str, payload: bytes, *, descriptor=None):
        from parser_localcat_codec import LINE_TEXT_DESCRIPTOR, LineTextReader

        active_descriptor = descriptor or LINE_TEXT_DESCRIPTOR
        snapshot = self._snapshot(name, payload, active_descriptor)
        return materialize(
            LineTextReader(active_descriptor),
            snapshot,
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LINE_TEXT_V1),
        )

    def test_golden_is_source_only_dense_and_uses_file_stem(self) -> None:
        result = self._materialize("chapter-plain.txt", _fixture_bytes("line-text-valid.hex"))

        self.assertEqual(result.header.name, "chapter-plain")
        self.assertIsNone(result.header.source_locale)
        self.assertIsNone(result.header.target_locale)
        self.assertEqual([record.local_id for record in result.records], ["segment-1", "segment-2", "segment-3"])
        self.assertEqual([record.source for record in result.records], ["First line", "Second  line", "Third line"])
        for record in result.records:
            self.assertIsNone(record.target)
            self.assertEqual(record.target_presence, TargetPresence.MISSING)
            self.assertIsNone(record.translation_state)
            self.assertEqual(record.speaker, RawSpeaker(""))

    def test_text_name_preserves_file_stem_whitespace(self) -> None:
        result = self._materialize(" chapter name .txt", b"source\n")
        self.assertEqual(result.header.name, Path(" chapter name .txt").stem)

    def test_bom_is_accepted_and_text_reader_has_no_serializer(self) -> None:
        from parser_localcat_codec import LINE_TEXT_DESCRIPTOR, LOCALCAT_JSON_DESCRIPTOR
        from parser_registry import ParserRegistry, RegistryConfigurationError

        result = self._materialize("bom.txt", _fixture_bytes("line-text-bom-valid.hex"))
        self.assertEqual([record.source for record in result.records], ["Alpha", "Beta"])
        self.assertIsNone(LINE_TEXT_DESCRIPTOR.canonical_serializer_factory)
        registry = ParserRegistry((LINE_TEXT_DESCRIPTOR, LOCALCAT_JSON_DESCRIPTOR))
        with self.assertRaises(RegistryConfigurationError) as caught:
            registry.create_canonical_serializer(LINE_TEXT_DESCRIPTOR)
        self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.WRITE_UNSUPPORTED")

    def test_all_python_splitlines_boundaries_keep_dense_source_order(self) -> None:
        payload = "a\x0bb\x0cc\x1cd\x1de\x1ef\x85g\u2028h\u2029i\r\nj\rk\nl".encode(
            "utf-8"
        )
        result = self._materialize("boundaries.txt", payload)
        self.assertEqual(
            [record.source for record in result.records],
            list("abcdefghijkl"),
        )
        self.assertEqual(
            [record.local_id for record in result.records],
            [f"segment-{index}" for index in range(1, 13)],
        )

    def test_empty_encoding_and_record_limit_are_fatal(self) -> None:
        from parser_localcat_codec import LINE_TEXT_DESCRIPTOR, LineTextReader

        tiny = replace(
            LINE_TEXT_DESCRIPTOR,
            limit_profile=replace(
                LINE_TEXT_DESCRIPTOR.limit_profile,
                profile_id="line-text-v1-tiny-test",
                max_records=1,
                max_materialized_records=1,
            ),
            capabilities=replace(
                LINE_TEXT_DESCRIPTOR.capabilities,
                format_profile="line-text-v1-tiny-test",
            ),
        )
        cases = (
            (_fixture_bytes("line-text-whitespace-only.hex"), LINE_TEXT_DESCRIPTOR, "PARSER.SYNTAX.EMPTY_INPUT"),
            (_fixture_bytes("line-text-invalid-utf8.hex"), LINE_TEXT_DESCRIPTOR, "PARSER.SOURCE.ENCODING_FAILED"),
            (b"one\ntwo\n", tiny, "PARSER.LIMIT.RECORD"),
        )
        for index, (payload, descriptor, code) in enumerate(cases):
            with self.subTest(code=code):
                snapshot = self._snapshot(f"text-{index}.txt", payload, descriptor)
                report = validate(
                    LineTextReader(descriptor),
                    snapshot,
                    ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LINE_TEXT_V1),
                )
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertIsNone(report.terminal)
                self.assertEqual(report.issues[-1].code, code)


class LocalCatCanonicalSerializerTests(_LocalCatFixture):
    def _request(self) -> CanonicalSerializeRequest:
        return CanonicalSerializeRequest(
            format_id=LOCALCAT_JSON_V1,
            document=CanonicalDocumentWrite(
                name="Demo",
                source_locale="en-US",
                target_locale="zh-CN",
                segments=(
                    CanonicalSegmentWrite(
                        local_id="one",
                        source="Hello",
                        target="你好",
                        speaker=RawSpeaker("Narrator"),
                        confirmed=True,
                    ),
                    CanonicalSegmentWrite(
                        local_id="two",
                        source="World",
                        target="",
                        speaker=RawSpeaker(""),
                        confirmed=False,
                    ),
                ),
            ),
        )

    def test_v1_bytes_are_deterministic_utf8_ordered_and_editor_compatible(self) -> None:
        from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR, LocalCatJsonCanonicalSerializer

        serializer = LocalCatJsonCanonicalSerializer(LOCALCAT_JSON_DESCRIPTOR)
        first = serializer.serialize_canonical(self._request())
        second = serializer.serialize_canonical(self._request())
        self.assertEqual(first, second)
        self.assertEqual(first.schema_version, 1)
        self.assertEqual(first.format_id, LOCALCAT_JSON_V1)
        self.assertTrue(first.payload.endswith(b"\n"))
        self.assertIn("你好".encode("utf-8"), first.payload)
        payload = json.loads(first.payload)
        self.assertEqual(
            list(payload),
            ["schema_version", "name", "source_locale", "target_locale", "segments"],
        )
        self.assertEqual(
            list(payload["segments"][0]),
            ["id", "source", "target", "speaker", "confirmed"],
        )
        self.assertEqual(payload["segments"][0]["confirmed"], True)

    def test_canonical_bytes_atomic_write_and_real_snapshot_readback(self) -> None:
        from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR, LocalCatJsonCanonicalSerializer, LocalCatJsonReader

        canonical = LocalCatJsonCanonicalSerializer(
            LOCALCAT_JSON_DESCRIPTOR
        ).serialize_canonical(self._request())
        target = self.root / "written.json"
        target.write_bytes(b"old")
        receipt = atomic_write_bytes(
            TargetReference(str(self.root), str(target), "written.json"),
            canonical.payload,
        )
        self.assertEqual(target.read_bytes(), canonical.payload)
        self.assertEqual(receipt.byte_count, len(canonical.payload))
        snapshot = create_sealed_snapshot(
            SourceReference(str(self.root), str(target), "written.json"),
            limit_profile=LOCALCAT_JSON_DESCRIPTOR.limit_profile,
        )
        self.addCleanup(snapshot.close)
        result = materialize(
            LocalCatJsonReader(LOCALCAT_JSON_DESCRIPTOR),
            snapshot,
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
        )
        self.assertEqual(result.header.name, "Demo")
        self.assertEqual([record.local_id for record in result.records], ["one", "two"])

    def test_serializer_rejects_wrong_format_before_returning_bytes(self) -> None:
        from parser_contracts import ContractViolation
        from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR, LocalCatJsonCanonicalSerializer

        serializer = LocalCatJsonCanonicalSerializer(LOCALCAT_JSON_DESCRIPTOR)
        with self.assertRaises(ContractViolation) as caught:
            serializer.serialize_canonical(
                CanonicalSerializeRequest(
                    format_id=LINE_TEXT_V1,
                    document=self._request().document,
                )
            )
        self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.WRITE_UNSUPPORTED")

    def test_canonical_bytes_keep_existing_target_on_atomic_replace_failure(self) -> None:
        from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR, LocalCatJsonCanonicalSerializer
        from parser_source import ParserSourceError

        canonical = LocalCatJsonCanonicalSerializer(
            LOCALCAT_JSON_DESCRIPTOR
        ).serialize_canonical(self._request())
        target = self.root / "protected.json"
        target.write_bytes(b"old-target")
        with mock.patch("parser_source.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_write_bytes(
                    TargetReference(str(self.root), str(target), "protected.json"),
                    canonical.payload,
                )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_FAILED")
        self.assertEqual(target.read_bytes(), b"old-target")

    def test_serializer_reports_unencodable_text_without_leaking_the_field(self) -> None:
        from parser_contracts import ContractViolation
        from parser_localcat_codec import LOCALCAT_JSON_DESCRIPTOR, LocalCatJsonCanonicalSerializer

        document = self._request().document
        unsafe = replace(
            document,
            segments=(replace(document.segments[0], source="secret\ud800body"),),
        )
        with self.assertRaises(ContractViolation) as caught:
            LocalCatJsonCanonicalSerializer(
                LOCALCAT_JSON_DESCRIPTOR
            ).serialize_canonical(
                CanonicalSerializeRequest(LOCALCAT_JSON_V1, unsafe)
            )
        self.assertEqual(caught.exception.code, "PARSER.SYNTAX.INVALID_FIELD")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
