"""TMX Level 1 codec contract tests for Parser Wave 2c."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "tm"
PAYLOAD_ROOT = FIXTURE_ROOT / "payloads"


def _tmx(*units: str, prefix: bytes = b"") -> bytes:
    body = "".join(units)
    return prefix + (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<tmx version="1.4"><header srclang="en-US"/><body>'
        f"{body}</body></tmx>"
    ).encode("utf-8")


def _unit(*variants: tuple[str, str], attributes: str = "") -> str:
    rendered = "".join(
        f'<tuv xml:lang="{locale}"><seg>{text}</seg></tuv>'
        for locale, text in variants
    )
    return f"<tu{attributes}>{rendered}</tu>"


class _CodecFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="parser-tmx-codec-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def request(source_locale: str = "en-US", target_locale: str = "zh-CN"):
        from parser_contracts import (
            EffectivePurpose,
            ReadRequest,
            TMX_LEVEL1_V1,
            TmxReadOptions,
        )

        return ReadRequest(
            purpose=EffectivePurpose.TRANSLATION_MEMORY,
            format_id=TMX_LEVEL1_V1,
            tmx_options=TmxReadOptions(
                source_locale=source_locale,
                target_locale=target_locale,
            ),
        )

    def snapshot(self, payload: bytes, *, descriptor=None):
        from parser_contracts import SourceReference
        from parser_source import create_sealed_snapshot
        from parser_tmx_codec import TMX_CODEC_DESCRIPTOR

        descriptor = descriptor or TMX_CODEC_DESCRIPTOR
        path = self.root / "sample.tmx"
        path.write_bytes(payload)
        return create_sealed_snapshot(
            SourceReference(
                safe_root=str(self.root),
                selected_path=str(path),
                display_hint="sample.tmx",
            ),
            limit_profile=descriptor.limit_profile,
        )

    def session(self, payload: bytes, *, request=None, cancellation=None):
        from parser_source import GuardedParseSession
        from parser_tmx_codec import TmxLevel1Codec

        codec = TmxLevel1Codec()
        snapshot = self.snapshot(payload, descriptor=codec.descriptor)
        session = GuardedParseSession(
            codec,
            snapshot,
            request or self.request(),
            cancellation=cancellation,
        )
        self.addCleanup(session.close)
        self.addCleanup(snapshot.close)
        return session

    @staticmethod
    def consume(session):
        from parser_contracts import ParseIssue, ResourceRecord

        records = []
        issues = []
        for event in session:
            if type(event) is ResourceRecord:
                records.append(event)
            elif type(event) is ParseIssue:
                issues.append(event)
            else:  # pragma: no cover - closed event set is asserted by Foundation
                raise AssertionError(f"unexpected raw event: {type(event)!r}")
        return tuple(records), tuple(issues), session.verified_terminal()


class TmxDescriptorTests(_CodecFixture):
    def test_descriptor_freezes_tmx_v1_limits_and_reader_only_streaming_capability(self) -> None:
        from parser_contracts import (
            FOUNDATION_GUARDED_ISSUE_CODES,
            EffectivePurpose,
            InputConsumptionPolicy,
            TMX_LEVEL1_V1,
        )
        from parser_tmx_codec import TMX_CODEC_DESCRIPTOR, TMX_LIMIT_PROFILE

        descriptor = TMX_CODEC_DESCRIPTOR
        self.assertEqual(descriptor.purpose, EffectivePurpose.TRANSLATION_MEMORY)
        self.assertEqual(descriptor.format_id, TMX_LEVEL1_V1)
        self.assertEqual(descriptor.input_consumption_policy, InputConsumptionPolicy.SEALED_BYTES_EOF)
        self.assertEqual(descriptor.limit_profile, TMX_LIMIT_PROFILE)
        self.assertEqual(TMX_LIMIT_PROFILE.profile_id, "tmx-level1-v1")
        self.assertEqual(TMX_LIMIT_PROFILE.profile_version, 1)
        self.assertEqual(TMX_LIMIT_PROFILE.max_input_bytes, 100 * 1024 * 1024)
        self.assertEqual(TMX_LIMIT_PROFILE.max_decoded_field_chars, 1_000_000)
        self.assertEqual(TMX_LIMIT_PROFILE.max_records, 1_000_000)
        self.assertEqual(TMX_LIMIT_PROFILE.max_materialized_records, 100_000)
        self.assertEqual(TMX_LIMIT_PROFILE.max_structure_depth, 64)
        self.assertTrue(set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(TMX_LIMIT_PROFILE.declared_issue_codes))
        self.assertLessEqual(len(TMX_LIMIT_PROFILE.declared_issue_codes), 64)
        self.assertTrue(descriptor.capabilities.readable)
        self.assertTrue(descriptor.capabilities.validatable)
        self.assertTrue(descriptor.capabilities.streaming_input)
        self.assertTrue(descriptor.capabilities.iterator_view)
        self.assertTrue(descriptor.capabilities.materialized_view)
        self.assertFalse(descriptor.capabilities.canonical_write)
        self.assertFalse(descriptor.capabilities.source_round_trip_write)
        self.assertFalse(descriptor.capabilities.active_sheet_only)
        self.assertIsNone(descriptor.canonical_serializer_factory)

    def test_tmx_request_requires_explicit_locale_options_without_stealing_codec_normalization(self) -> None:
        from parser_contracts import (
            ContractViolation,
            EffectivePurpose,
            ReadRequest,
            TMX_LEVEL1_V1,
            TmxReadOptions,
        )

        with self.assertRaises(ContractViolation):
            ReadRequest(
                purpose=EffectivePurpose.TRANSLATION_MEMORY,
                format_id=TMX_LEVEL1_V1,
            )
        self.assertEqual(
            TmxReadOptions(source_locale=" en_US ", target_locale="zh-cn").source_locale,
            " en_US ",
        )

    def test_codec_rejects_invalid_or_same_normalized_locale_selection(self) -> None:
        self.assert_fatal_locale("bad locale", "zh-CN")
        self.assert_fatal_locale("en-US", "en_us")

    def assert_fatal_locale(self, source_locale: str, target_locale: str) -> None:
        from parser_source import ParserSessionError

        session = self.session(
            _tmx(_unit(("en-US", "A"), ("zh-CN", "甲"))),
            request=self.request(source_locale, target_locale),
        )
        with self.assertRaises(ParserSessionError) as raised:
            list(session)
        self.assertEqual(raised.exception.code, "PARSER.TMX.LOCALE_SELECTION_INVALID")
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()


class TmxLocaleAndMappingTests(_CodecFixture):
    def test_golden_exact_then_unambiguous_base_fallback_preserves_variants_and_order(self) -> None:
        payload = (PAYLOAD_ROOT / "tmx_valid_locale_fallback_variants.tmx").read_bytes()
        records, issues, terminal = self.consume(self.session(payload))

        self.assertEqual(issues, ())
        self.assertEqual(
            [(item.local_id, item.source, item.target) for item in records],
            [
                ("tu-1", "Exact", "精确译文"),
                ("tu-2", "Fallback", "回退译文"),
                ("tu-3", "Variant", "变体一"),
                ("tu-4", "Variant", "变体二"),
            ],
        )
        self.assertEqual([item.speaker.value for item in records], ["", "", "", ""])
        self.assertTrue(all(item.format_metadata == () for item in records))
        self.assertEqual(terminal.record_count, 4)
        self.assertEqual(terminal.warning_counts, ())

    def test_record_warnings_skip_units_and_keep_physical_ordinal_holes(self) -> None:
        payload = (PAYLOAD_ROOT / "tmx_record_warnings.tmx").read_bytes()
        records, issues, terminal = self.consume(self.session(payload))

        self.assertEqual([(item.local_id, item.source) for item in records], [("tu-1", "Accepted")])
        self.assertEqual([item.record_number for item in issues], [2, 3, 4])
        self.assertEqual(
            [item.code for item in issues],
            [
                "PARSER.TMX.INLINE_XML_UNSUPPORTED",
                "PARSER.TMX.LOCALE_PAIR_MISSING",
                "PARSER.TMX.LOCALE_FALLBACK_AMBIGUOUS",
            ],
        )
        self.assertTrue(all("Accepted" not in item.safe_summary for item in issues))
        self.assertEqual(terminal.record_count, 1)
        self.assertEqual(sum(count.count for count in terminal.warning_counts), 3)

        hole_payload = _tmx(
            _unit(("en-US", "Before"), ("zh-CN", "前")),
            '<tu><tuv xml:lang="en-US"><seg>Inline <ph/></seg></tuv>'
            '<tuv xml:lang="zh-CN"><seg>跳过</seg></tuv></tu>',
            _unit(("en-US", "After"), ("zh-CN", "后")),
        )
        hole_records, hole_issues, _hole_terminal = self.consume(
            self.session(hole_payload)
        )
        self.assertEqual(
            [(record.local_id, record.source) for record in hole_records],
            [("tu-1", "Before"), ("tu-3", "After")],
        )
        self.assertEqual(
            [(issue.code, issue.record_number) for issue in hole_issues],
            [("PARSER.TMX.INLINE_XML_UNSUPPORTED", 2)],
        )

    def test_exact_locale_wins_and_same_normalized_locale_uses_last_physical_variant(self) -> None:
        payload = _tmx(
            _unit(
                ("en-GB", "fallback"),
                ("en_US", "first exact"),
                ("en-us", "last exact"),
                ("zh-Hans", "fallback target"),
                ("zh_CN", "first target"),
                ("zh-cn", "last target"),
            )
        )
        records, issues, _terminal = self.consume(self.session(payload))
        self.assertEqual(issues, ())
        self.assertEqual([(item.source, item.target) for item in records], [("last exact", "last target")])

    def test_trim_is_only_at_seg_edges_and_no_context_or_prop_is_inferred(self) -> None:
        payload = _tmx(
            '<tu><prop type="x-context">secret-context</prop>'
            '<tuv xml:lang="en-US"><seg>  A  B\nC  </seg></tuv>'
            '<tuv xml:lang="zh-CN"><seg>  甲  乙\n丙  </seg></tuv></tu>'
        )
        records, issues, _terminal = self.consume(self.session(payload))
        self.assertEqual(issues, ())
        self.assertEqual(records[0].source, "A  B\nC")
        self.assertEqual(records[0].target, "甲  乙\n丙")
        self.assertEqual(records[0].format_metadata, ())

    def test_empty_selected_seg_is_a_missing_pair_warning(self) -> None:
        payload = _tmx(_unit(("en-US", "   "), ("zh-CN", "译文")))
        records, issues, terminal = self.consume(self.session(payload))
        self.assertEqual(records, ())
        self.assertEqual([item.code for item in issues], ["PARSER.TMX.LOCALE_PAIR_MISSING"])
        self.assertEqual(terminal.record_count, 0)


class TmxSafetyAndLimitTests(_CodecFixture):
    def assert_fatal(self, payload: bytes, code: str):
        from parser_source import ParserSessionError

        session = self.session(payload)
        with self.assertRaises(ParserSessionError) as raised:
            list(session)
        self.assertEqual(raised.exception.code, code)
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()
        return session

    def test_utf8_bom_is_accepted_but_invalid_utf8_is_fatal(self) -> None:
        records, issues, terminal = self.consume(
            self.session(_tmx(_unit(("en-US", "A"), ("zh-CN", "甲")), prefix=b"\xef\xbb\xbf"))
        )
        self.assertEqual((records[0].source, records[0].target), ("A", "甲"))
        self.assertEqual(issues, ())
        self.assertEqual(terminal.record_count, 1)

        invalid = bytes.fromhex(
            "3c746d783e3c626f64793e3c74753e3c74757620786d6c3a6c616e673d22656e2d5553223e"
            "3c7365673eff3c2f7365673e3c2f7475763e3c2f74753e3c2f626f64793e3c2f746d783e"
        )
        self.assert_fatal(invalid, "PARSER.SOURCE.ENCODING_FAILED")

        declared_latin1 = (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>'
            b'<tmx><body></body></tmx>'
        )
        self.assert_fatal(declared_latin1, "PARSER.SOURCE.ENCODING_FAILED")

    def test_dtd_entity_and_external_resolution_are_fatal_without_network_access(self) -> None:
        boundary = (PAYLOAD_ROOT / "tmx_dtd_entity.tmx").read_bytes()
        with mock.patch("socket.socket", side_effect=AssertionError("network access attempted")):
            self.assert_fatal(boundary, "PARSER.TMX.UNSAFE_XML")

        external = (
            b'<?xml version="1.0"?><!DOCTYPE tmx SYSTEM "https://example.invalid/evil.dtd">'
            b'<tmx><body></body></tmx>'
        )
        with mock.patch("socket.socket", side_effect=AssertionError("network access attempted")):
            self.assert_fatal(external, "PARSER.TMX.UNSAFE_XML")

    def test_malformed_no_tu_and_structure_depth_are_fatal(self) -> None:
        self.assert_fatal(b"<tmx><body><tu>", "PARSER.SYNTAX.MALFORMED")
        self.assert_fatal(b"<tmx><body></body></tmx>", "PARSER.TMX.NO_TRANSLATION_UNITS")
        deep = ("<x>" * 65 + "</x>" * 65).encode("utf-8")
        self.assert_fatal(deep, "PARSER.LIMIT.DEPTH")

    def test_oversize_segment_is_one_warning_and_does_not_materialize_full_text(self) -> None:
        oversized = "x" * 1_000_001
        payload = _tmx(_unit(("en-US", oversized), ("zh-CN", "译文")))
        records, issues, terminal = self.consume(self.session(payload))
        self.assertEqual(records, ())
        self.assertEqual([item.code for item in issues], ["PARSER.TMX.SEGMENT_LIMIT"])
        self.assertEqual(issues[0].record_number, 1)
        self.assertNotIn("x" * 64, issues[0].safe_summary)
        self.assertEqual(terminal.record_count, 0)

    def test_snapshot_input_limit_is_rejected_before_codec_reads(self) -> None:
        from parser_contracts import SourceReference
        from parser_source import ParserSourceError, create_sealed_snapshot
        from parser_tmx_codec import TMX_CODEC_DESCRIPTOR

        path = self.root / "too-large.tmx"
        with path.open("wb") as handle:
            handle.truncate(TMX_CODEC_DESCRIPTOR.limit_profile.max_input_bytes + 1)
        with self.assertRaises(ParserSourceError) as raised:
            create_sealed_snapshot(
                SourceReference(str(self.root), str(path), "too-large.tmx"),
                limit_profile=TMX_CODEC_DESCRIPTOR.limit_profile,
            )
        self.assertEqual(raised.exception.code, "PARSER.LIMIT.INPUT")


class TmxTerminalAndCancellationTests(_CodecFixture):
    def test_fatal_tail_exposes_provisional_record_but_never_terminal(self) -> None:
        from parser_contracts import ResourceRecord
        from parser_source import ParserSessionError

        payload = (PAYLOAD_ROOT / "tmx_fatal_tail.tmx").read_bytes()
        session = self.session(payload)
        iterator = iter(session)
        first = next(iterator)
        self.assertIs(type(first), ResourceRecord)
        self.assertEqual(first.local_id, "tu-1")
        with self.assertRaises(ParserSessionError) as raised:
            next(iterator)
        self.assertEqual(raised.exception.code, "PARSER.SYNTAX.MALFORMED")
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()

    def test_cancellation_after_first_tu_denies_terminal(self) -> None:
        from parser_contracts import ResourceRecord
        from parser_source import CancellationToken, ParserSessionError

        payload = _tmx(
            _unit(("en-US", "One"), ("zh-CN", "一")),
            _unit(("en-US", "Two"), ("zh-CN", "二")),
            _unit(("en-US", "Three"), ("zh-CN", "三")),
        )
        cancellation = CancellationToken()
        session = self.session(payload, cancellation=cancellation)
        iterator = iter(session)
        first = next(iterator)
        self.assertIs(type(first), ResourceRecord)
        cancellation.cancel()
        with self.assertRaises(ParserSessionError) as raised:
            next(iterator)
        self.assertEqual(raised.exception.code, "PARSER.SOURCE.CANCELLED")
        self.assertEqual(session.provisional_record_count, 1)
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()

    def test_natural_eof_is_required_and_terminal_binds_descriptor_profile(self) -> None:
        from parser_tmx_codec import TMX_CODEC_DESCRIPTOR

        session = self.session(_tmx(_unit(("en-US", "A"), ("zh-CN", "甲"))))
        records, issues, terminal = self.consume(session)
        self.assertEqual(len(records), 1)
        self.assertEqual(issues, ())
        self.assertEqual(terminal.codec_identity, TMX_CODEC_DESCRIPTOR.identity)
        self.assertEqual(terminal.limit_profile, TMX_CODEC_DESCRIPTOR.limit_profile)
        self.assertEqual(terminal.source.byte_count, session.source.byte_count)


if __name__ == "__main__":
    unittest.main()
