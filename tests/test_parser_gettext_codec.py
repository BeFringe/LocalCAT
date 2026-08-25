"""PO/POT singular-profile contracts for Parser Wave 2 gettext codecs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "project" / "payloads"


def _fixture_bytes(name: str) -> bytes:
    path = FIXTURE_ROOT / name
    stored = path.read_bytes()
    return bytes.fromhex(stored.decode("ascii")) if path.suffix == ".hex" else stored


class _GettextFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="parser-gettext-codec-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def request(descriptor):
        from parser_contracts import ReadRequest

        return ReadRequest(descriptor.purpose, descriptor.format_id)

    def snapshot(self, name: str, payload: bytes, descriptor):
        from parser_contracts import SourceReference
        from parser_source import create_sealed_snapshot

        path = self.root / name
        path.write_bytes(payload)
        snapshot = create_sealed_snapshot(
            SourceReference(str(self.root), str(path), name),
            limit_profile=descriptor.limit_profile,
        )
        self.addCleanup(snapshot.close)
        return snapshot

    def materialize(self, name: str, payload: bytes, *, descriptor=None):
        from parser_gettext_codec import (
            GETTEXT_PO_DESCRIPTOR,
            GettextPoCodec,
            GettextPotCodec,
        )
        from parser_source import materialize

        selected = descriptor or GETTEXT_PO_DESCRIPTOR
        codec = (
            GettextPoCodec(selected)
            if selected.format_id.value == "gettext-po-v1"
            else GettextPotCodec(selected)
        )
        return materialize(
            codec,
            self.snapshot(name, payload, selected),
            self.request(selected),
        )

    def validate(self, name: str, payload: bytes, *, descriptor=None):
        from parser_gettext_codec import (
            GETTEXT_PO_DESCRIPTOR,
            GettextPoCodec,
            GettextPotCodec,
        )
        from parser_source import validate

        selected = descriptor or GETTEXT_PO_DESCRIPTOR
        codec = (
            GettextPoCodec(selected)
            if selected.format_id.value == "gettext-po-v1"
            else GettextPotCodec(selected)
        )
        return validate(
            codec,
            self.snapshot(name, payload, selected),
            self.request(selected),
        )


class GettextDescriptorTests(_GettextFixture):
    def test_po_and_pot_publish_distinct_frozen_reader_only_profiles(self) -> None:
        from parser_contracts import (
            CodecCapabilities,
            EffectivePurpose,
            FOUNDATION_GUARDED_ISSUE_CODES,
            GETTEXT_PO_V1,
            GETTEXT_POT_V1,
            InputConsumptionPolicy,
        )
        from parser_gettext_codec import (
            GETTEXT_PO_DESCRIPTOR,
            GETTEXT_PO_LIMIT_PROFILE,
            GETTEXT_POT_DESCRIPTOR,
            GETTEXT_POT_LIMIT_PROFILE,
        )

        for descriptor, format_id, profile in (
            (GETTEXT_PO_DESCRIPTOR, GETTEXT_PO_V1, GETTEXT_PO_LIMIT_PROFILE),
            (GETTEXT_POT_DESCRIPTOR, GETTEXT_POT_V1, GETTEXT_POT_LIMIT_PROFILE),
        ):
            with self.subTest(format_id=format_id.value):
                self.assertIs(descriptor.purpose, EffectivePurpose.PROJECT_DOCUMENT)
                self.assertEqual(descriptor.format_id, format_id)
                self.assertIs(
                    descriptor.input_consumption_policy,
                    InputConsumptionPolicy.SEALED_BYTES_EOF,
                )
                self.assertEqual(profile.profile_id, format_id.value)
                self.assertEqual(profile.profile_version, 1)
                self.assertEqual(profile.max_input_bytes, 100 * 1024 * 1024)
                self.assertEqual(profile.max_decoded_field_chars, 100 * 1024 * 1024)
                self.assertEqual(profile.max_records, 1_000_000)
                self.assertEqual(profile.max_materialized_records, 100_000)
                self.assertEqual(profile.max_structure_depth, 16)
                self.assertTrue(
                    set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(
                        profile.declared_issue_codes
                    )
                )
                self.assertEqual(
                    descriptor.capabilities,
                    CodecCapabilities(
                        readable=True,
                        validatable=True,
                        canonical_write=False,
                        source_round_trip_write=False,
                        streaming_input=True,
                        iterator_view=True,
                        materialized_view=True,
                        format_profile=format_id.value,
                    ),
                )
                self.assertIsNone(descriptor.canonical_serializer_factory)
        self.assertIsNot(GETTEXT_PO_LIMIT_PROFILE, GETTEXT_POT_LIMIT_PROFILE)

    def test_each_zero_argument_reader_factory_binds_its_own_descriptor(self) -> None:
        from parser_gettext_codec import GETTEXT_PO_DESCRIPTOR, GETTEXT_POT_DESCRIPTOR

        self.assertIs(GETTEXT_PO_DESCRIPTOR.reader_factory().descriptor, GETTEXT_PO_DESCRIPTOR)
        self.assertIs(GETTEXT_POT_DESCRIPTOR.reader_factory().descriptor, GETTEXT_POT_DESCRIPTOR)


class GettextSingularDocumentTests(_GettextFixture):
    @staticmethod
    def metadata_map(entries):
        return {entry.key: entry.value for entry in entries}

    def test_po_golden_preserves_header_multiline_target_and_opaque_metadata(self) -> None:
        from parser_contracts import RawSpeaker, TargetPresence, TranslationState

        result = self.materialize(
            "chapter.po",
            _fixture_bytes("gettext-po-valid.po"),
        )

        self.assertEqual(result.header.name, "chapter")
        self.assertIsNone(result.header.source_locale)
        self.assertIsNone(result.header.target_locale)
        header = self.metadata_map(result.header.metadata)
        self.assertIn("Content-Type: text/plain; charset=UTF-8\n", header["gettext.header"])
        self.assertEqual(len(result.records), 1)
        segment = result.records[0]
        self.assertEqual(segment.local_id, "entry-6-1")
        self.assertEqual(segment.source, "Hello world")
        self.assertEqual(segment.target, "你好")
        self.assertIs(segment.target_presence, TargetPresence.PRESENT)
        self.assertIs(
            segment.translation_state,
            TranslationState.FORMAT_DERIVED_UNCONFIRMED,
        )
        self.assertEqual(segment.speaker, RawSpeaker(""))
        metadata = self.metadata_map(segment.format_metadata)
        self.assertEqual(metadata["gettext.msgctxt"], "menu")
        self.assertEqual(metadata["gettext.comments"], ("#. Synthetic translator note",))
        self.assertEqual(metadata["gettext.references"], ("#: chapter.rpy:10",))
        self.assertEqual(metadata["gettext.flags"], ("#, fuzzy",))
        self.assertEqual(
            metadata["gettext.previous_values"],
            ('#| msgid "Old menu label"',),
        )
        self.assertEqual(result.terminal.record_count, 1)

    def test_pot_golden_is_explicit_empty_and_msgctxt_never_becomes_speaker(self) -> None:
        from parser_contracts import RawSpeaker, TargetPresence
        from parser_gettext_codec import GETTEXT_POT_DESCRIPTOR

        result = self.materialize(
            "template.pot",
            _fixture_bytes("gettext-pot-valid.pot"),
            descriptor=GETTEXT_POT_DESCRIPTOR,
        )
        segment = result.records[0]
        self.assertEqual(segment.local_id, "entry-5-1")
        self.assertEqual(segment.source, "Start game")
        self.assertEqual(segment.target, "")
        self.assertIs(segment.target_presence, TargetPresence.EXPLICIT_EMPTY)
        self.assertIsNone(segment.translation_state)
        self.assertEqual(segment.speaker, RawSpeaker(""))
        self.assertEqual(
            self.metadata_map(segment.format_metadata)["gettext.msgctxt"],
            "button",
        )

    def test_valid_gettext_escapes_and_continuations_use_one_decoder(self) -> None:
        payload = (
            b'# translator\\nopaque\n'
            b'msgctxt "menu\\titem"\n'
            b'msgid ""\n'
            b'"quote: \\"; slash: \\\\; "\n'
            b'"controls: \\n\\r\\t\\b\\f\\v\\a; "\n'
            b'"octal: \\101; hex: \\x42; utf8: \\303\\251"\n'
            b'msgstr "translated\\nline"\n'
        )

        result = self.materialize("escapes.po", payload)
        segment = result.records[0]
        self.assertEqual(
            segment.source,
            'quote: "; slash: \\; controls: \n\r\t\b\f\v\a; octal: A; hex: B; utf8: é',
        )
        self.assertEqual(segment.target, "translated\nline")
        self.assertEqual(
            self.metadata_map(segment.format_metadata)["gettext.msgctxt"],
            "menu\titem",
        )

    def test_missing_charset_header_and_no_header_are_both_accepted(self) -> None:
        with_header = self.materialize(
            "missing-charset.po",
            b'msgid ""\nmsgstr "Language: zh_CN\\n"\n\nmsgid "A"\nmsgstr ""\n',
        )
        without_header = self.materialize(
            "no-header.po",
            b'msgid "A"\nmsgstr ""\n',
        )

        self.assertIn("gettext.header", self.metadata_map(with_header.header.metadata))
        self.assertEqual(without_header.header.metadata, ())
        self.assertEqual(with_header.records[0].target, "")
        self.assertEqual(without_header.records[0].target, "")

    def test_cr_only_physical_lines_keep_entry_and_location_semantics(self) -> None:
        result = self.materialize(
            "classic-mac.po",
            b'#. note\rmsgid "Source"\rmsgstr "Target"\r',
        )
        self.assertEqual(result.records[0].local_id, "entry-1-1")
        self.assertEqual(result.records[0].source, "Source")
        self.assertEqual(result.records[0].target, "Target")

    def test_unicode_separators_are_quoted_source_text_not_physical_lines(self) -> None:
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(codepoint=f"U+{ord(separator):04X}"):
                source = f"left{separator}right"
                result = self.materialize(
                    "unicode-separator.po",
                    f'msgid "{source}"\nmsgstr ""\n'.encode("utf-8"),
                )
                self.assertEqual(result.records[0].source, source)
                self.assertEqual(result.records[0].local_id, "entry-1-1")

    def test_non_fuzzy_translated_po_has_no_invented_confirmation_state(self) -> None:
        result = self.materialize(
            "translated.po",
            b'msgid "Source"\nmsgstr "Target"\n',
        )
        self.assertEqual(result.records[0].target, "Target")
        self.assertIsNone(result.records[0].translation_state)

    def test_comments_remain_exact_opaque_metadata_and_do_not_change_source(self) -> None:
        payload = (
            b'# translator note\n'
            b'#. extracted note\n'
            b'#: file.rpy:4 other.rpy:7\n'
            b'#, python-format, no-wrap\n'
            b'#| msgctxt "old"\n'
            b'#| msgid "before"\n'
            b'msgid "after"\n'
            b'msgstr "target"\n'
        )
        segment = self.materialize("comments.po", payload).records[0]
        metadata = self.metadata_map(segment.format_metadata)
        self.assertEqual(
            metadata["gettext.comments"],
            ("# translator note", "#. extracted note"),
        )
        self.assertEqual(metadata["gettext.references"], ("#: file.rpy:4 other.rpy:7",))
        self.assertEqual(metadata["gettext.flags"], ("#, python-format, no-wrap",))
        self.assertEqual(
            metadata["gettext.previous_values"],
            ('#| msgctxt "old"', '#| msgid "before"'),
        )
        self.assertEqual(segment.source, "after")


class GettextFailureAndTerminalTests(_GettextFixture):
    def assert_fatal(self, name: str, payload: bytes, code: str, *, descriptor=None) -> None:
        from parser_contracts import ValidationOutcome

        report = self.validate(name, payload, descriptor=descriptor)
        self.assertIs(report.outcome, ValidationOutcome.FAILED)
        self.assertIsNone(report.terminal)
        self.assertEqual(report.issues[-1].code, code)

    def test_utf8_bom_is_accepted_and_invalid_utf8_is_fatal_for_po_and_pot(self) -> None:
        from parser_gettext_codec import GETTEXT_POT_DESCRIPTOR

        self.assertEqual(
            self.materialize("bom.po", _fixture_bytes("gettext-po-bom-valid.hex")).records[0].source,
            "Hello",
        )
        self.assertEqual(
            self.materialize(
                "bom.pot",
                _fixture_bytes("gettext-pot-bom-valid.hex"),
                descriptor=GETTEXT_POT_DESCRIPTOR,
            ).records[0].source,
            "Hello",
        )
        self.assert_fatal(
            "invalid.po",
            _fixture_bytes("gettext-po-invalid-utf8.hex"),
            "PARSER.SOURCE.ENCODING_FAILED",
        )
        self.assert_fatal(
            "invalid.pot",
            _fixture_bytes("gettext-pot-invalid-utf8.hex"),
            "PARSER.SOURCE.ENCODING_FAILED",
            descriptor=GETTEXT_POT_DESCRIPTOR,
        )

    def test_invalid_utf8_reports_its_actual_later_physical_line(self) -> None:
        from parser_contracts import ValidationOutcome

        payload = (
            b'msgid ""\n'
            b'msgstr "Language: zh_CN\\n"\n'
            b'\n'
            b'msgid "safe"\n'
            b'msgstr "\xff"\n'
        )
        report = self.validate("later-invalid.po", payload)

        self.assertIs(report.outcome, ValidationOutcome.FAILED)
        self.assertIsNone(report.terminal)
        self.assertEqual(report.issues[-1].code, "PARSER.SOURCE.ENCODING_FAILED")
        self.assertEqual(report.issues[-1].line_number, 5)
        self.assertNotIn("safe", report.issues[-1].safe_summary)

    def test_non_utf8_header_charset_is_fatal_without_transcoding(self) -> None:
        self.assert_fatal(
            "latin.po",
            b'msgid ""\nmsgstr "Content-Type: text/plain; charset=ISO-8859-1\\n"\n\n'
            b'msgid "safe"\nmsgstr "target"\n',
            "PARSER.GETTEXT.CHARSET_UNSUPPORTED",
        )
        self.assert_fatal(
            "empty-charset.po",
            b'msgid ""\nmsgstr "Content-Type: text/plain; charset=\\n"\n\n'
            b'msgid "safe"\nmsgstr "target"\n',
            "PARSER.GETTEXT.CHARSET_UNSUPPORTED",
        )

    def test_plural_directives_are_unsupported_fatal_in_po_and_pot(self) -> None:
        from parser_gettext_codec import GETTEXT_POT_DESCRIPTOR

        self.assert_fatal(
            "plural.po",
            _fixture_bytes("gettext-po-plural.po"),
            "PARSER.GETTEXT.PLURAL_UNSUPPORTED",
        )
        self.assert_fatal(
            "plural.pot",
            _fixture_bytes("gettext-pot-plural.pot"),
            "PARSER.GETTEXT.PLURAL_UNSUPPORTED",
            descriptor=GETTEXT_POT_DESCRIPTOR,
        )

    def test_invalid_escape_order_duplicate_and_unterminated_strings_are_syntax_fatal(self) -> None:
        cases = (
            b'msgid "bad\\q"\nmsgstr ""\n',
            b'msgstr "target"\nmsgid "source"\n',
            b'msgid "one"\nmsgid "two"\nmsgstr ""\n',
            b'msgid "unterminated\nmsgstr ""\n',
            b'msgid "source" trailing\nmsgstr ""\n',
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.assert_fatal(
                    f"syntax-{index}.po",
                    payload,
                    "PARSER.GETTEXT.SYNTAX",
                )

    def test_fatal_tail_can_expose_provisional_record_but_never_terminal(self) -> None:
        from parser_contracts import ParsedSegment
        from parser_gettext_codec import GETTEXT_PO_DESCRIPTOR, GettextPoCodec
        from parser_source import GuardedParseSession, ParserSessionError

        snapshot = self.snapshot(
            "fatal-tail.po",
            _fixture_bytes("gettext-po-fatal-tail.po"),
            GETTEXT_PO_DESCRIPTOR,
        )
        session = GuardedParseSession(
            GettextPoCodec(),
            snapshot,
            self.request(GETTEXT_PO_DESCRIPTOR),
        )
        self.addCleanup(session.close)
        iterator = iter(session)
        self.assertEqual(next(iterator).name, "fatal-tail")
        first = next(iterator)
        self.assertIs(type(first), ParsedSegment)
        self.assertEqual(first.source, "First")
        with self.assertRaises(ParserSessionError) as caught:
            next(iterator)
        self.assertEqual(caught.exception.code, "PARSER.GETTEXT.SYNTAX")
        self.assertEqual(session.provisional_record_count, 1)
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()

    def test_record_and_field_limits_are_descriptor_driven(self) -> None:
        from parser_gettext_codec import GETTEXT_PO_DESCRIPTOR

        record_descriptor = replace(
            GETTEXT_PO_DESCRIPTOR,
            limit_profile=replace(
                GETTEXT_PO_DESCRIPTOR.limit_profile,
                profile_id="gettext-po-record-test",
                max_records=1,
                max_materialized_records=1,
            ),
            capabilities=replace(
                GETTEXT_PO_DESCRIPTOR.capabilities,
                format_profile="gettext-po-record-test",
            ),
        )
        field_descriptor = replace(
            GETTEXT_PO_DESCRIPTOR,
            limit_profile=replace(
                GETTEXT_PO_DESCRIPTOR.limit_profile,
                profile_id="gettext-po-field-test",
                max_decoded_field_chars=3,
            ),
            capabilities=replace(
                GETTEXT_PO_DESCRIPTOR.capabilities,
                format_profile="gettext-po-field-test",
            ),
        )
        self.assert_fatal(
            "records.po",
            b'msgid "one"\nmsgstr ""\n\nmsgid "two"\nmsgstr ""\n',
            "PARSER.LIMIT.RECORD",
            descriptor=record_descriptor,
        )
        self.assert_fatal(
            "field.po",
            b'msgid "four"\nmsgstr ""\n',
            "PARSER.LIMIT.FIELD",
            descriptor=field_descriptor,
        )

    def test_cancellation_after_first_entry_denies_terminal(self) -> None:
        from parser_gettext_codec import GETTEXT_PO_DESCRIPTOR, GettextPoCodec
        from parser_source import CancellationToken, GuardedParseSession, ParserSessionError

        payload = (
            b'msgid "one"\nmsgstr ""\n\n'
            b'msgid "two"\nmsgstr ""\n\n'
            b'msgid "three"\nmsgstr ""\n'
        )
        token = CancellationToken()
        snapshot = self.snapshot("cancel.po", payload, GETTEXT_PO_DESCRIPTOR)
        session = GuardedParseSession(
            GettextPoCodec(),
            snapshot,
            self.request(GETTEXT_PO_DESCRIPTOR),
            cancellation=token,
        )
        self.addCleanup(session.close)
        iterator = iter(session)
        next(iterator)  # document header
        self.assertEqual(next(iterator).source, "one")
        token.cancel()
        with self.assertRaises(ParserSessionError) as caught:
            next(iterator)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.CANCELLED")
        self.assertEqual(session.provisional_record_count, 1)
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()

    def test_empty_or_header_only_input_is_fatal(self) -> None:
        for name, payload in (
            ("empty.po", b""),
            ("header-only.po", b'msgid ""\nmsgstr "Language: zh\\n"\n'),
        ):
            with self.subTest(name=name):
                self.assert_fatal(name, payload, "PARSER.GETTEXT.EMPTY_INPUT")

    def test_body_safe_syntax_diagnostic_keeps_source_text_out(self) -> None:
        secret = "SECRET-SOURCE-BODY"
        report = self.validate(
            "secret.po",
            f'msgid "{secret}\\q"\nmsgstr ""\n'.encode(),
        )
        self.assertTrue(report.issues)
        self.assertTrue(all(secret not in issue.safe_summary for issue in report.issues))


if __name__ == "__main__":
    unittest.main()
