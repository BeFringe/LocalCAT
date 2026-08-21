"""Wave 2 contracts for the normalized TM JSON single-input codec."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from parser_contracts import (
    CodecCapabilities,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    IssueSeverity,
    NORMALIZED_TM_JSON_V1,
    RawSpeaker,
    ReadRequest,
    SourceReference,
    ValidationOutcome,
)
from parser_source import (
    CancellationToken,
    GuardedParseSession,
    ParserSourceError,
    ParserSessionError,
    create_sealed_snapshot,
    materialize,
    validate,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "tm"
PAYLOAD_ROOT = FIXTURE_ROOT / "payloads"


def _fixture_bytes(name: str) -> bytes:
    return (PAYLOAD_ROOT / name).read_bytes()


class _NormalizedJsonFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _snapshot(self, payload: bytes, descriptor, name: str = "input.json"):
        path = self.root / name
        path.write_bytes(payload)
        snapshot = create_sealed_snapshot(
            SourceReference(
                safe_root=str(self.root),
                selected_path=str(path),
                display_hint=name,
            ),
            limit_profile=descriptor.limit_profile,
        )
        self.addCleanup(snapshot.close)
        return snapshot

    def _request(self) -> ReadRequest:
        return ReadRequest(
            EffectivePurpose.TRANSLATION_MEMORY,
            NORMALIZED_TM_JSON_V1,
        )

    def _materialize(self, payload: bytes, *, descriptor=None):
        from parser_tm_json_codec import (
            NORMALIZED_TM_JSON_DESCRIPTOR,
            NormalizedTmJsonReader,
        )

        active = descriptor or NORMALIZED_TM_JSON_DESCRIPTOR
        return materialize(
            NormalizedTmJsonReader(active),
            self._snapshot(payload, active),
            self._request(),
        )

    def _validate(self, payload: bytes, *, descriptor=None):
        from parser_tm_json_codec import (
            NORMALIZED_TM_JSON_DESCRIPTOR,
            NormalizedTmJsonReader,
        )

        active = descriptor or NORMALIZED_TM_JSON_DESCRIPTOR
        return validate(
            NormalizedTmJsonReader(active),
            self._snapshot(payload, active),
            self._request(),
        )


class NormalizedTmJsonDescriptorTests(_NormalizedJsonFixture):
    def test_descriptor_freezes_reader_only_non_streaming_profile(self) -> None:
        from parser_tm_json_codec import NORMALIZED_TM_JSON_DESCRIPTOR

        descriptor = NORMALIZED_TM_JSON_DESCRIPTOR
        profile = descriptor.limit_profile
        self.assertEqual(descriptor.format_id, NORMALIZED_TM_JSON_V1)
        self.assertEqual(descriptor.purpose, EffectivePurpose.TRANSLATION_MEMORY)
        self.assertEqual(descriptor.identity.codec_version, "1")
        self.assertEqual(profile.profile_id, "normalized-tm-json-v1")
        self.assertEqual(profile.profile_version, 1)
        self.assertEqual(profile.max_input_bytes, 100 * 1024 * 1024)
        self.assertEqual(profile.max_decoded_field_chars, 100 * 1024 * 1024)
        self.assertEqual(profile.max_records, 100_000)
        self.assertEqual(profile.max_materialized_records, 100_000)
        self.assertEqual(profile.max_retained_issues, 256)
        self.assertEqual(profile.max_metadata_entries_per_container, 256)
        self.assertEqual(profile.max_metadata_decoded_chars_per_container, 1024 * 1024)
        self.assertEqual(profile.max_metadata_decoded_chars_total, 16 * 1024 * 1024)
        self.assertEqual(profile.max_structure_depth, 64)
        self.assertTrue(
            set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(
                profile.declared_issue_codes
            )
        )
        self.assertEqual(
            set(profile.declared_issue_codes) - set(FOUNDATION_GUARDED_ISSUE_CODES),
            {
                "PARSER.LIMIT.DEPTH",
                "PARSER.SOURCE.ENCODING_FAILED",
                "PARSER.SYNTAX.EMPTY_INPUT",
                "PARSER.SYNTAX.INVALID_FIELD",
            },
        )
        self.assertEqual(
            descriptor.capabilities,
            CodecCapabilities(
                readable=True,
                validatable=True,
                canonical_write=False,
                source_round_trip_write=False,
                streaming_input=False,
                iterator_view=True,
                materialized_view=True,
                format_profile="normalized-tm-json-v1",
            ),
        )
        self.assertIsNotNone(descriptor.reader_factory)
        self.assertIsNone(descriptor.canonical_serializer_factory)


class NormalizedTmJsonReaderTests(_NormalizedJsonFixture):
    def test_valid_golden_preserves_order_duplicates_speaker_and_local_id(self) -> None:
        result = self._materialize(
            _fixture_bytes("normalized_valid_speaker_duplicates.json")
        )

        self.assertIsNone(result.header)
        self.assertEqual(
            [record.local_id for record in result.records],
            ["record-1", "record-2", "record-3"],
        )
        self.assertEqual(
            [record.source for record in result.records],
            ["Alpha", "Duplicate", "Duplicate"],
        )
        self.assertEqual(
            [record.target for record in result.records],
            ["甲", "一", "二"],
        )
        self.assertEqual(
            [record.speaker for record in result.records],
            [RawSpeaker("Alice Smith"), RawSpeaker(""), RawSpeaker("")],
        )
        self.assertEqual(result.issues, ())
        self.assertEqual(result.terminal.record_count, 3)

    def test_warning_golden_skips_bad_rows_and_keeps_physical_ordinal_holes(self) -> None:
        result = self._materialize(_fixture_bytes("normalized_record_warnings.json"))

        self.assertEqual(
            [record.local_id for record in result.records],
            ["record-1", "record-6", "record-7"],
        )
        self.assertEqual(
            [issue.record_number for issue in result.issues],
            [2, 3, 4, 5],
        )
        self.assertTrue(
            all(issue.severity is IssueSeverity.WARNING for issue in result.issues)
        )
        self.assertEqual(
            [(count.code, count.severity, count.count) for count in result.terminal.warning_counts],
            [("PARSER.SYNTAX.INVALID_FIELD", IssueSeverity.WARNING, 4)],
        )
        self.assertEqual(result.terminal.record_count, 3)

    def test_non_string_speaker_is_a_versioned_warning_not_empty_identity(self) -> None:
        payload = json.dumps(
            [
                {"source": "accepted", "target": "first", "speaker": None},
                {"source": "rejected-secret", "target": "second", "speaker": 42},
                {"source": "missing", "target": "third"},
            ]
        ).encode("utf-8")

        result = self._materialize(payload)

        self.assertEqual(
            [(record.local_id, record.speaker) for record in result.records],
            [("record-1", RawSpeaker("")), ("record-3", RawSpeaker(""))],
        )
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].record_number, 2)
        self.assertNotIn("rejected-secret", result.issues[0].safe_summary)

    def test_trim_is_declared_but_internal_text_case_and_unicode_are_preserved(self) -> None:
        decomposed = "Cafe\u0301"
        payload = json.dumps(
            [
                {
                    "source": f"  {decomposed}  Keep  CASE  ",
                    "target": "  <b>$HOME</b>  two  spaces  ",
                    "speaker": "  Alice  Smith  ",
                }
            ],
            ensure_ascii=False,
        ).encode("utf-8")

        result = self._materialize(payload)
        record = result.records[0]

        self.assertEqual(record.source, f"{decomposed}  Keep  CASE")
        self.assertEqual(record.target, "<b>$HOME</b>  two  spaces")
        self.assertEqual(record.speaker, RawSpeaker("Alice  Smith"))

    def test_non_array_empty_and_all_invalid_inputs_have_no_terminal(self) -> None:
        cases = (
            _fixture_bytes("normalized_non_array_root.json"),
            b"[]",
            b'[null,{"source":"","target":"x"}]',
        )

        for payload in cases:
            with self.subTest(payload=payload[:20]):
                report = self._validate(payload)
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertIsNone(report.terminal)
                self.assertEqual(report.provisional_record_count, 0)
                self.assertTrue(
                    any(issue.severity is IssueSeverity.FATAL for issue in report.issues)
                )

    def test_fatal_tail_invalid_utf8_and_bom_fail_before_any_record(self) -> None:
        cases = (
            _fixture_bytes("normalized_fatal_tail.json"),
            bytes.fromhex(
                "5b7b22736f75726365223a22ff222c22746172676574223a2274227d5d"
            ),
            bytes.fromhex(
                "efbbbf5b7b22736f75726365223a2273222c22746172676574223a2274227d5d"
            ),
        )

        for payload in cases:
            with self.subTest(payload=payload[:12]):
                report = self._validate(payload)
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertEqual(report.provisional_record_count, 0)
                self.assertIsNone(report.terminal)

    def test_input_field_depth_and_record_limits_are_fatal(self) -> None:
        from parser_tm_json_codec import NORMALIZED_TM_JSON_DESCRIPTOR

        profile = NORMALIZED_TM_JSON_DESCRIPTOR.limit_profile
        input_descriptor = replace(
            NORMALIZED_TM_JSON_DESCRIPTOR,
            limit_profile=replace(profile, max_input_bytes=20),
        )
        input_path = self.root / "oversized.json"
        input_path.write_bytes(b'[{"source":"long","target":"target"}]')
        with self.assertRaises(ParserSourceError) as input_failure:
            create_sealed_snapshot(
                SourceReference(
                    safe_root=str(self.root),
                    selected_path=str(input_path),
                    display_hint=input_path.name,
                ),
                limit_profile=input_descriptor.limit_profile,
            )
        self.assertEqual(input_failure.exception.code, "PARSER.LIMIT.INPUT")

        cases = (
            (
                replace(
                    NORMALIZED_TM_JSON_DESCRIPTOR,
                    limit_profile=replace(profile, max_decoded_field_chars=6),
                ),
                b'[{"source":"1234567","target":"ok"}]',
                "PARSER.LIMIT.FIELD",
            ),
            (
                replace(
                    NORMALIZED_TM_JSON_DESCRIPTOR,
                    limit_profile=replace(profile, max_structure_depth=2),
                ),
                b'[[[{"source":"s","target":"t"}]]]',
                "PARSER.LIMIT.DEPTH",
            ),
            (
                replace(
                    NORMALIZED_TM_JSON_DESCRIPTOR,
                    limit_profile=replace(
                        profile,
                        max_records=2,
                        max_materialized_records=2,
                    ),
                ),
                b'[{"source":"1","target":"1"},{"source":"2","target":"2"},{"source":"3","target":"3"}]',
                "PARSER.LIMIT.RECORD",
            ),
        )

        for descriptor, payload, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                report = self._validate(payload, descriptor=descriptor)
                self.assertEqual(report.outcome, ValidationOutcome.FAILED)
                self.assertIsNone(report.terminal)
                self.assertEqual(report.provisional_record_count, 0)
                self.assertEqual(report.issues[0].code, expected_code)

    def test_cancellation_after_first_record_has_no_terminal(self) -> None:
        from parser_tm_json_codec import (
            NORMALIZED_TM_JSON_DESCRIPTOR,
            NormalizedTmJsonReader,
        )

        payload = b'[{"source":"1","target":"1"},{"source":"2","target":"2"},{"source":"3","target":"3"}]'
        snapshot = self._snapshot(payload, NORMALIZED_TM_JSON_DESCRIPTOR)
        token = CancellationToken()
        session = GuardedParseSession(
            NormalizedTmJsonReader(),
            snapshot,
            self._request(),
            cancellation=token,
        )
        iterator = iter(session)

        first = next(iterator)
        self.assertEqual(first.local_id, "record-1")
        token.cancel()
        with self.assertRaises(ParserSessionError) as caught:
            next(iterator)

        self.assertEqual(caught.exception.code, "PARSER.SOURCE.CANCELLED")
        self.assertEqual(session.provisional_record_count, 1)
        self.assertIsNone(session.failed_report(cancelled=True).terminal)
        session.close()

    def test_diagnostics_are_body_safe_and_codec_has_no_writer_or_batch_policy(self) -> None:
        from parser_tm_json_codec import (
            NORMALIZED_TM_JSON_DESCRIPTOR,
            NormalizedTmJsonReader,
        )

        secret = "DO-NOT-LEAK-source-target-speaker"
        payload = json.dumps(
            [{"source": secret, "target": "", "speaker": secret}]
        ).encode("utf-8")
        report = self._validate(payload)

        self.assertEqual(report.outcome, ValidationOutcome.FAILED)
        self.assertTrue(report.issues)
        self.assertTrue(all(secret not in issue.safe_summary for issue in report.issues))
        self.assertFalse(hasattr(NormalizedTmJsonReader, "serialize_canonical"))
        self.assertIsNone(NORMALIZED_TM_JSON_DESCRIPTOR.canonical_serializer_factory)


if __name__ == "__main__":
    unittest.main()
