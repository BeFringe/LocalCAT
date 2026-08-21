"""Wave 4 cross-format equivalence, content, and capability acceptance.

The tests stay at the public Parser Application surface.  They prove the
eight built-in purpose/format combinations without creating a future project,
plugin-format, speaker-profile, or synchronization authority in test code.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from parser_composition import (
    OpenedParserInput,
    ProviderBinding,
    create_parser_application_surface,
)
from parser_contracts import (
    CanonicalDocumentWrite,
    CanonicalSegmentWrite,
    CanonicalSerializeRequest,
    CodecCapabilities,
    CodecDescriptor,
    CodecIdentity,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    FormatId,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    InputConsumptionPolicy,
    IssueSeverity,
    LINE_TEXT_V1,
    LimitProfile,
    LOCALCAT_JSON_V1,
    NORMALIZED_TM_JSON_V1,
    ParseIssue,
    ParsedSegment,
    RawSpeaker,
    ReadRequest,
    ResourceRecord,
    RoundTripTokenEnvelope,
    RoundTripTokenFailureReason,
    RoundTripTokenValidationError,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
    TMX_LEVEL1_V1,
    TargetPresence,
    TargetReference,
    TermbaseColumnSelection,
    TermbaseReadOptions,
    TmxReadOptions,
    TranslationState,
    ValidationOutcome,
    validate_round_trip_token,
)


_DECOMPOSED_SOURCE = "Cafe\u0301  Keep  CASE"
_SPECIAL_TARGET = "<b>$HOME</b>  two  spaces"


class _Provider:
    """Structural plugin fake used through the real ProviderBinding seam."""

    def __init__(self, descriptors: tuple[CodecDescriptor, ...]) -> None:
        self.provider_id = "plugin.wave4"
        self.provider_version = "1"
        self._descriptors = descriptors

    def descriptors(self) -> tuple[CodecDescriptor, ...]:
        return self._descriptors


class _CountingReader:
    """Raw codec fake that makes every fresh grammar pass observable."""

    def __init__(
        self,
        descriptor: CodecDescriptor,
        observations: dict[str, object],
        mode: str,
    ) -> None:
        self.descriptor = descriptor
        self._observations = observations
        self._mode = mode
        readers = observations.setdefault("readers", [])
        assert type(readers) is list
        readers.append(self)

    def iter_raw(self, source: object, request: object):
        del request
        raw_payloads = self._observations.setdefault("raw_payloads", [])
        assert type(raw_payloads) is list
        raw_payloads.append(source.read())
        yield DocumentHeader("counting", None, None, ())
        if self._mode == "empty":
            yield ParseIssue(
                "PARSER.SYNTAX.EMPTY_INPUT",
                IssueSeverity.FATAL,
                "input contains no usable records",
            )
            return
        yield ParsedSegment(
            "record-1",
            "First",
            None,
            TargetPresence.MISSING,
            None,
            RawSpeaker(""),
            (),
        )
        if self._mode == "fatal-tail":
            yield ParseIssue(
                "PARSER.TEST.FATAL_TAIL",
                IssueSeverity.FATAL,
                "input became invalid after one provisional record",
            )
            return
        yield ParsedSegment(
            "record-2",
            "Second",
            None,
            TargetPresence.MISSING,
            None,
            RawSpeaker(""),
            (),
        )


def _counting_descriptor(
    mode: str,
    *,
    max_materialized_records: int = 4,
    source_round_trip_write: bool = False,
) -> tuple[CodecDescriptor, dict[str, object]]:
    """Build a public-provider descriptor without a Parser base-class dependency."""

    format_id = FormatId(f"wave4-{mode}")
    profile_id = f"wave4-{mode}-profile"
    observations: dict[str, object] = {}
    holder: dict[str, CodecDescriptor] = {}

    def reader_factory() -> _CountingReader:
        return _CountingReader(holder["descriptor"], observations, mode)

    descriptor = CodecDescriptor(
        identity=CodecIdentity("plugin.wave4", f"codec.{mode}", "1"),
        purpose=EffectivePurpose.PROJECT_DOCUMENT,
        format_id=format_id,
        extensions=(f".{mode}",),
        mime_types=(),
        sniff_prefixes=(),
        capabilities=CodecCapabilities(
            readable=True,
            validatable=True,
            canonical_write=False,
            source_round_trip_write=source_round_trip_write,
            streaming_input=True,
            iterator_view=True,
            materialized_view=True,
            format_profile=profile_id,
            opaque_features=("opaque-round-trip-token",)
            if source_round_trip_write
            else (),
        ),
        limit_profile=LimitProfile(
            profile_id=profile_id,
            profile_version=1,
            max_input_bytes=4096,
            max_decoded_field_chars=1024,
            max_records=4,
            max_materialized_records=max_materialized_records,
            max_retained_issues=8,
            declared_issue_codes=tuple(
                sorted(
                    {
                        *FOUNDATION_GUARDED_ISSUE_CODES,
                        "PARSER.SYNTAX.EMPTY_INPUT",
                        "PARSER.TEST.FATAL_TAIL",
                    }
                )
            ),
            max_metadata_entries_per_container=8,
            max_metadata_decoded_chars_per_container=1024,
            max_metadata_decoded_chars_total=4096,
            max_structure_depth=8,
        ),
        input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
        reader_factory=reader_factory,
        canonical_serializer_factory=None,
    )
    holder["descriptor"] = descriptor
    return descriptor, observations


class _RoundTripCaller:
    """Minimal plugin caller: token validation precedes the injected writer."""

    def __init__(
        self,
        descriptor: CodecDescriptor,
        source_fingerprint: str,
        state_fingerprint: str,
        target_writer: object,
    ) -> None:
        self._descriptor = descriptor
        self._source_fingerprint = source_fingerprint
        self._state_fingerprint = state_fingerprint
        self._target_writer = target_writer

    def write(
        self,
        token: RoundTripTokenEnvelope | None,
        target: TargetReference,
    ) -> None:
        validated = validate_round_trip_token(
            token,
            expected_codec_identity=self._descriptor.identity,
            expected_source_fingerprint=self._source_fingerprint,
            expected_format_state_fingerprint=self._state_fingerprint,
        )
        self._target_writer(target, validated.opaque_payload)


@dataclass(frozen=True, slots=True)
class _FormatCase:
    label: str
    path: Path
    purpose: EffectivePurpose
    format_id: FormatId
    request: ReadRequest


class _Wave4Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="parser-wave4-equivalence-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.surface = create_parser_application_surface()
        self.cases = self._build_cases()

    def _write(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def _build_cases(self) -> tuple[_FormatCase, ...]:
        localcat = self._write(
            "chapter.json",
            json.dumps(
                [
                    {
                        "id": "json-explicit",
                        "source": f"  {_DECOMPOSED_SOURCE}  ",
                        "target": "",
                        "speaker": "  Alice  Smith  ",
                        "confirmed": False,
                    },
                    {"source": "Missing target", "speaker": None},
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        text = self._write(
            "chapter.txt",
            f"  {_DECOMPOSED_SOURCE}  \n\nSecond source\n".encode("utf-8"),
        )
        po = self._write(
            "chapter.po",
            (
                '#, fuzzy\nmsgctxt "menu"\n'
                f'msgid "{_DECOMPOSED_SOURCE}"\n'
                f'msgstr "{_SPECIAL_TARGET}"\n'
            ).encode("utf-8"),
        )
        pot = self._write(
            "template.pot",
            (
                'msgctxt "button"\n'
                f'msgid "{_DECOMPOSED_SOURCE}"\n'
                'msgstr ""\n'
            ).encode("utf-8"),
        )
        tmx = self._write(
            "memory.tmx",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<tmx version="1.4"><header srclang="en-US"/><body>'
                '<tu><tuv xml:lang="en-US"><seg>'
                f"{_DECOMPOSED_SOURCE}"
                '</seg></tuv><tuv xml:lang="zh-CN"><seg>'
                '&lt;b&gt;$HOME&lt;/b&gt;  two  spaces'
                '</seg></tuv></tu>'
                '<tu><tuv xml:lang="en-US"><seg>missing pair</seg></tuv></tu>'
                '</body></tmx>'
            ).encode("utf-8"),
        )
        normalized = self._write(
            "normalized.json",
            json.dumps(
                [
                    {"source": "rejected", "target": "bad", "speaker": 7},
                    {
                        "source": f"  {_DECOMPOSED_SOURCE}  ",
                        "target": f"  {_SPECIAL_TARGET}  ",
                        "speaker": "  Bob  Jones  ",
                    },
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        csv_path = self._write(
            "terms.csv",
            (
                "Source,Target\n"
                f'"{_DECOMPOSED_SOURCE}","{_SPECIAL_TARGET}"\n'
                ",\n"
            ).encode("utf-8-sig"),
        )
        xlsx = self.root / "terms.xlsx"
        workbook = Workbook()
        active = workbook.active
        if active is None:  # pragma: no cover - openpyxl invariant
            self.fail("new workbook has no active worksheet")
        active.title = "Active"
        active.append(["Source", "Target"])
        active.append([_DECOMPOSED_SOURCE, _SPECIAL_TARGET])
        active.append([None, None])
        ignored = workbook.create_sheet("Ignored chapter")
        ignored.append(["Source", "Target"])
        ignored.append(["must not aggregate", "future project data"])
        workbook.save(xlsx)
        workbook.close()

        termbase_options = TermbaseReadOptions(
            columns=TermbaseColumnSelection.legacy_first_two_columns()
        )
        return (
            self._case(
                "localcat-json",
                localcat,
                EffectivePurpose.PROJECT_DOCUMENT,
                LOCALCAT_JSON_V1,
            ),
            self._case(
                "line-text",
                text,
                EffectivePurpose.PROJECT_DOCUMENT,
                LINE_TEXT_V1,
            ),
            self._case(
                "gettext-po",
                po,
                EffectivePurpose.PROJECT_DOCUMENT,
                GETTEXT_PO_V1,
            ),
            self._case(
                "gettext-pot",
                pot,
                EffectivePurpose.PROJECT_DOCUMENT,
                GETTEXT_POT_V1,
            ),
            self._case(
                "tmx",
                tmx,
                EffectivePurpose.TRANSLATION_MEMORY,
                TMX_LEVEL1_V1,
                tmx_options=TmxReadOptions("en-US", "zh-CN"),
            ),
            self._case(
                "normalized-tm-json",
                normalized,
                EffectivePurpose.TRANSLATION_MEMORY,
                NORMALIZED_TM_JSON_V1,
            ),
            self._case(
                "termbase-csv",
                csv_path,
                EffectivePurpose.TERMBASE,
                TERMBASE_CSV_V1,
                termbase_options=termbase_options,
            ),
            self._case(
                "termbase-xlsx",
                xlsx,
                EffectivePurpose.TERMBASE,
                TERMBASE_XLSX_V1,
                termbase_options=termbase_options,
            ),
        )

    @staticmethod
    def _case(
        label: str,
        path: Path,
        purpose: EffectivePurpose,
        format_id: FormatId,
        *,
        tmx_options: TmxReadOptions | None = None,
        termbase_options: TermbaseReadOptions | None = None,
    ) -> _FormatCase:
        return _FormatCase(
            label=label,
            path=path,
            purpose=purpose,
            format_id=format_id,
            request=ReadRequest(
                purpose=purpose,
                format_id=format_id,
                tmx_options=tmx_options,
                termbase_options=termbase_options,
            ),
        )

    def _open(self, case: _FormatCase) -> OpenedParserInput:
        opened = self.surface.open_input(
            SourceReference(
                safe_root=str(self.root),
                selected_path=str(case.path),
                display_hint=case.path.name,
            ),
            SelectionRequest(case.purpose, format_id=case.format_id),
            case.request,
        )
        if type(opened) is SelectionFailure:  # pragma: no cover - assertion path
            self.fail(f"{case.label}: {opened.code}: {opened.safe_summary}")
        self.assertIs(type(opened), OpenedParserInput)
        return opened

    def _materialize(self, case: _FormatCase):
        with self._open(case) as opened:
            return opened.materialize()

    @staticmethod
    def _canonical_request(format_id: FormatId) -> CanonicalSerializeRequest:
        return CanonicalSerializeRequest(
            format_id=format_id,
            document=CanonicalDocumentWrite(
                name="Demo",
                source_locale="en-US",
                target_locale="zh-CN",
                segments=(
                    CanonicalSegmentWrite(
                        local_id="one",
                        source="Source",
                        target="Target",
                        speaker=RawSpeaker("Narrator"),
                        confirmed=True,
                    ),
                ),
            ),
        )


class ParserViewEquivalenceTests(_Wave4Fixture):
    def test_all_eight_combinations_have_equivalent_guarded_views(self) -> None:
        for case in self.cases:
            with self.subTest(case=case.label), self._open(case) as opened:
                session = opened.stream()
                try:
                    events = tuple(session)
                    stream_terminal = session.verified_terminal()
                finally:
                    session.close()
                materialized = opened.materialize()
                validation = opened.validate()

                stream_headers = tuple(
                    event for event in events if type(event) is DocumentHeader
                )
                stream_records = tuple(
                    event
                    for event in events
                    if type(event) in {ParsedSegment, ResourceRecord}
                )
                stream_issues = tuple(
                    event for event in events if type(event) is ParseIssue
                )

                expected_header = (
                    stream_headers[0] if stream_headers else None
                )
                self.assertLessEqual(len(stream_headers), 1)
                self.assertEqual(materialized.header, expected_header)
                self.assertEqual(materialized.records, stream_records)
                self.assertEqual(materialized.issues, stream_issues)
                self.assertEqual(materialized.terminal, stream_terminal)
                self.assertIsNot(materialized.terminal, stream_terminal)
                self.assertIs(validation.outcome, ValidationOutcome.SUCCESS)
                self.assertEqual(validation.source, opened.source_identity)
                self.assertEqual(validation.provisional_record_count, len(stream_records))
                self.assertEqual(validation.issues, stream_issues)
                self.assertEqual(
                    validation.issue_counts,
                    stream_terminal.warning_counts,
                )
                self.assertEqual(validation.terminal, stream_terminal)
                self.assertIsNot(validation.terminal, stream_terminal)
                self.assertIsNot(validation.terminal, materialized.terminal)
                self.assertEqual(
                    validation.observed_capabilities,
                    opened.descriptor.capabilities,
                )

    def _open_counting(
        self,
        descriptor: CodecDescriptor,
    ) -> tuple[OpenedParserInput, Path]:
        source = self._write(f"{descriptor.format_id.value}.input", b"sealed fixture")
        surface = create_parser_application_surface(
            providers=(
                ProviderBinding(
                    provider_id="plugin.wave4",
                    provider=_Provider((descriptor,)),
                    enabled=True,
                    compatible_versions=("1",),
                ),
            )
        )
        opened = surface.open_input(
            SourceReference(str(self.root), str(source), source.name),
            SelectionRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                format_id=descriptor.format_id,
            ),
            ReadRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                descriptor.format_id,
            ),
        )
        self.assertIs(type(opened), OpenedParserInput)
        return opened, source

    def test_empty_and_fatal_tail_fail_equally_in_three_independent_views(
        self,
    ) -> None:
        cases = (
            ("empty", "PARSER.SYNTAX.EMPTY_INPUT", 0),
            ("fatal-tail", "PARSER.TEST.FATAL_TAIL", 1),
        )
        for mode, expected_code, expected_records in cases:
            with self.subTest(mode=mode):
                descriptor, observations = _counting_descriptor(mode)
                opened, _source = self._open_counting(descriptor)
                with opened:
                    session = opened.stream()
                    try:
                        with self.assertRaises(ContractViolation) as streamed:
                            tuple(session)
                        stream_report = session.failed_report()
                    finally:
                        session.close()

                    with self.assertRaises(ContractViolation) as materialized:
                        opened.materialize()
                    validation = opened.validate()

                    self.assertEqual(streamed.exception.code, expected_code)
                    self.assertEqual(materialized.exception.code, expected_code)
                    self.assertIs(validation.outcome, ValidationOutcome.FAILED)
                    self.assertIsNone(stream_report.terminal)
                    self.assertIsNone(validation.terminal)
                    self.assertEqual(
                        stream_report.provisional_record_count,
                        expected_records,
                    )
                    self.assertEqual(
                        validation.provisional_record_count,
                        expected_records,
                    )
                    self.assertEqual(stream_report.issues, validation.issues)
                    self.assertEqual(stream_report.issue_counts, validation.issue_counts)
                    self.assertEqual(validation.issues[-1].code, expected_code)
                    self.assertEqual(stream_report.source, opened.source_identity)
                    self.assertEqual(validation.source, opened.source_identity)

                readers = observations["readers"]
                raw_payloads = observations["raw_payloads"]
                self.assertEqual(len(readers), 3)
                self.assertEqual(len({id(reader) for reader in readers}), 3)
                self.assertEqual(raw_payloads, [b"sealed fixture"] * 3)

    def test_materialization_limit_is_view_local_and_never_reuses_a_terminal(
        self,
    ) -> None:
        descriptor, observations = _counting_descriptor(
            "materialization",
            max_materialized_records=1,
        )
        opened, _source = self._open_counting(descriptor)
        with opened:
            session = opened.stream()
            try:
                stream_events = tuple(session)
                stream_terminal = session.verified_terminal()
            finally:
                session.close()

            with self.assertRaises(ContractViolation) as materialized:
                opened.materialize()
            validation = opened.validate()

            self.assertEqual(
                tuple(
                    event.local_id
                    for event in stream_events
                    if type(event) is ParsedSegment
                ),
                ("record-1", "record-2"),
            )
            self.assertEqual(
                materialized.exception.code,
                "PARSER.LIMIT.MATERIALIZATION",
            )
            self.assertIs(validation.outcome, ValidationOutcome.SUCCESS)
            self.assertEqual(stream_terminal.record_count, 2)
            self.assertEqual(validation.terminal, stream_terminal)
            self.assertIsNot(validation.terminal, stream_terminal)

        readers = observations["readers"]
        raw_payloads = observations["raw_payloads"]
        self.assertEqual(len(readers), 3)
        self.assertEqual(len({id(reader) for reader in readers}), 3)
        self.assertEqual(raw_payloads, [b"sealed fixture"] * 3)


class ParserContentAndBoundaryTests(_Wave4Fixture):
    def test_content_presence_speaker_and_identity_stay_single_input_facts(
        self,
    ) -> None:
        results = {case.label: self._materialize(case) for case in self.cases}

        first_records = {
            label: result.records[0] for label, result in results.items()
        }
        for label, record in first_records.items():
            with self.subTest(case=label):
                self.assertEqual(record.source, _DECOMPOSED_SOURCE)
                self.assertNotEqual(record.source, "Caf\u00e9  Keep  CASE")
                self.assertNotIn("  ", record.source[:1] + record.source[-1:])

        self.assertEqual(
            first_records["localcat-json"].speaker,
            RawSpeaker("Alice  Smith"),
        )
        self.assertEqual(
            first_records["normalized-tm-json"].speaker,
            RawSpeaker("Bob  Jones"),
        )
        for label in (
            "line-text",
            "gettext-po",
            "gettext-pot",
            "tmx",
            "termbase-csv",
            "termbase-xlsx",
        ):
            self.assertEqual(first_records[label].speaker, RawSpeaker(""), label)

        project_expectations = {
            "localcat-json": (
                "",
                TargetPresence.EXPLICIT_EMPTY,
                TranslationState.UNCONFIRMED,
            ),
            "line-text": (None, TargetPresence.MISSING, None),
            "gettext-po": (
                _SPECIAL_TARGET,
                TargetPresence.PRESENT,
                TranslationState.FORMAT_DERIVED_UNCONFIRMED,
            ),
            "gettext-pot": ("", TargetPresence.EXPLICIT_EMPTY, None),
        }
        for label, expected in project_expectations.items():
            record = first_records[label]
            self.assertIs(type(record), ParsedSegment)
            self.assertEqual(
                (record.target, record.target_presence, record.translation_state),
                expected,
            )

        self.assertEqual(first_records["localcat-json"].local_id, "json-explicit")
        self.assertEqual(first_records["line-text"].local_id, "segment-1")
        self.assertTrue(first_records["gettext-po"].local_id.startswith("entry-"))
        self.assertTrue(first_records["gettext-pot"].local_id.startswith("entry-"))
        self.assertEqual(first_records["tmx"].local_id, "tu-1")
        self.assertEqual(first_records["normalized-tm-json"].local_id, "record-2")
        self.assertEqual(first_records["termbase-csv"].local_id, "row-2")
        self.assertEqual(first_records["termbase-xlsx"].local_id, "row-2")

        self.assertEqual(len(results["termbase-xlsx"].records), 1)
        self.assertNotIn(
            "must not aggregate",
            tuple(record.source for record in results["termbase-xlsx"].records),
        )

        expected_headers = {
            "localcat-json": ("chapter", "en-US", "zh-CN", ()),
            "line-text": ("chapter", None, None, ()),
            "gettext-po": ("chapter", None, None, ()),
            "gettext-pot": ("template", None, None, ()),
            "tmx": None,
            "normalized-tm-json": None,
            "termbase-csv": None,
            "termbase-xlsx": None,
        }
        expected_ids = {
            "localcat-json": ("json-explicit", "segment-2"),
            "line-text": ("segment-1", "segment-2"),
            "gettext-po": ("entry-1-1",),
            "gettext-pot": ("entry-1-1",),
            "tmx": ("tu-1",),
            "normalized-tm-json": ("record-2",),
            "termbase-csv": ("row-2",),
            "termbase-xlsx": ("row-2",),
        }
        expected_sources = {
            "localcat-json": (_DECOMPOSED_SOURCE, "Missing target"),
            "line-text": (_DECOMPOSED_SOURCE, "Second source"),
            "gettext-po": (_DECOMPOSED_SOURCE,),
            "gettext-pot": (_DECOMPOSED_SOURCE,),
            "tmx": (_DECOMPOSED_SOURCE,),
            "normalized-tm-json": (_DECOMPOSED_SOURCE,),
            "termbase-csv": (_DECOMPOSED_SOURCE,),
            "termbase-xlsx": (_DECOMPOSED_SOURCE,),
        }
        expected_targets = {
            "localcat-json": ("", None),
            "line-text": (None, None),
            "gettext-po": (_SPECIAL_TARGET,),
            "gettext-pot": ("",),
            "tmx": (_SPECIAL_TARGET,),
            "normalized-tm-json": (_SPECIAL_TARGET,),
            "termbase-csv": (_SPECIAL_TARGET,),
            "termbase-xlsx": (_SPECIAL_TARGET,),
        }
        expected_speakers = {
            "localcat-json": ("Alice  Smith", ""),
            "line-text": ("", ""),
            "gettext-po": ("",),
            "gettext-pot": ("",),
            "tmx": ("",),
            "normalized-tm-json": ("Bob  Jones",),
            "termbase-csv": ("",),
            "termbase-xlsx": ("",),
        }
        expected_metadata = {
            "localcat-json": ((), ()),
            "line-text": ((), ()),
            "gettext-po": (
                (
                    ("gettext.flags", ("#, fuzzy",)),
                    ("gettext.msgctxt", "menu"),
                ),
            ),
            "gettext-pot": ((('gettext.msgctxt', "button"),),),
            "tmx": ((),),
            "normalized-tm-json": ((),),
            "termbase-csv": ((),),
            "termbase-xlsx": ((),),
        }
        expected_issues = {
            "localcat-json": (),
            "line-text": (),
            "gettext-po": (),
            "gettext-pot": (),
            "tmx": (("PARSER.TMX.LOCALE_PAIR_MISSING", 2),),
            "normalized-tm-json": (("PARSER.SYNTAX.INVALID_FIELD", 1),),
            "termbase-csv": (
                ("PARSER.TERMBASE.HEADER_SKIPPED", 1),
                ("PARSER.TERMBASE.ROW_EMPTY", 3),
            ),
            "termbase-xlsx": (
                ("PARSER.TERMBASE.HEADER_SKIPPED", 1),
                ("PARSER.TERMBASE.ROW_EMPTY", 3),
            ),
        }
        for label, result in results.items():
            with self.subTest(full_facts=label):
                header = result.header
                header_fact = (
                    None
                    if header is None
                    else (
                        header.name,
                        header.source_locale,
                        header.target_locale,
                        tuple((entry.key, entry.value) for entry in header.metadata),
                    )
                )
                self.assertEqual(header_fact, expected_headers[label])
                self.assertEqual(
                    tuple(record.local_id for record in result.records),
                    expected_ids[label],
                )
                self.assertEqual(
                    len(set(record.local_id for record in result.records)),
                    len(result.records),
                )
                self.assertEqual(
                    tuple(record.source for record in result.records),
                    expected_sources[label],
                )
                self.assertEqual(
                    tuple(record.target for record in result.records),
                    expected_targets[label],
                )
                self.assertEqual(
                    tuple(record.speaker.value for record in result.records),
                    expected_speakers[label],
                )
                self.assertEqual(
                    tuple(
                        tuple(
                            (entry.key, entry.value)
                            for entry in record.format_metadata
                        )
                        for record in result.records
                    ),
                    expected_metadata[label],
                )
                self.assertEqual(
                    tuple((issue.code, issue.record_number) for issue in result.issues),
                    expected_issues[label],
                )
                self.assertTrue(
                    all(issue.severity is IssueSeverity.WARNING for issue in result.issues)
                )

        expected_project_state = {
            "localcat-json": (
                (TargetPresence.EXPLICIT_EMPTY, TranslationState.UNCONFIRMED),
                (TargetPresence.MISSING, TranslationState.UNCONFIRMED),
            ),
            "line-text": (
                (TargetPresence.MISSING, None),
                (TargetPresence.MISSING, None),
            ),
            "gettext-po": (
                (
                    TargetPresence.PRESENT,
                    TranslationState.FORMAT_DERIVED_UNCONFIRMED,
                ),
            ),
            "gettext-pot": ((TargetPresence.EXPLICIT_EMPTY, None),),
        }
        for label, expected in expected_project_state.items():
            self.assertTrue(
                all(type(record) is ParsedSegment for record in results[label].records)
            )
            self.assertEqual(
                tuple(
                    (record.target_presence, record.translation_state)
                    for record in results[label].records
                ),
                expected,
            )
        for label in (
            "tmx",
            "normalized-tm-json",
            "termbase-csv",
            "termbase-xlsx",
        ):
            self.assertTrue(
                all(type(record) is ResourceRecord for record in results[label].records)
            )
            self.assertEqual(
                tuple(record.target for record in results[label].records),
                (_SPECIAL_TARGET,),
            )

        self.assertEqual(
            tuple(field.name for field in fields(DocumentHeader)),
            ("name", "source_locale", "target_locale", "metadata"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(ParsedSegment)),
            (
                "local_id",
                "source",
                "target",
                "target_presence",
                "translation_state",
                "speaker",
                "format_metadata",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(ResourceRecord)),
            ("local_id", "source", "target", "speaker", "format_metadata"),
        )
        for label in (
            "tmx",
            "normalized-tm-json",
            "termbase-csv",
            "termbase-xlsx",
        ):
            for record in results[label].records:
                for project_only_field in (
                    "confirmed",
                    "target_presence",
                    "translation_state",
                ):
                    self.assertFalse(
                        hasattr(record, project_only_field),
                        f"{label}: ResourceRecord leaked {project_only_field}",
                    )

        deferred_authority_fields = (
            "project_id",
            "project_segment_id",
            "project_package",
            "projectpackage",
            "project_manifest",
            "manifest",
            "document_id",
            "current_document",
            "current_document_id",
            "display_name",
            "document_display_name",
            "project_order",
            "document_order",
            "document_filter",
            "document_separator",
            "folder_project",
            "multi_document",
            "sheet_aggregation",
            "workbook_aggregation",
            "dirty",
            "dirty_aggregation",
            "reconciliation",
            "progress",
            "progress_aggregation",
            "save_report",
            "save_reporting",
            "chunk_membership",
            "chunk_permission",
            "permissions",
            "package_sync",
            "synchronization",
            "sync_provider",
            "remote_conflict",
            "provider_behavior",
            "speaker_alias",
            "explicit_empty_profile",
            "avatar_path",
            "inferred_name",
            "device_inventory",
            "fuzzy_qualification",
        )
        for result in results.values():
            objects = tuple(result.records)
            if result.header is not None:
                objects = (result.header, *objects)
            for item in objects:
                for field_name in deferred_authority_fields:
                    self.assertFalse(hasattr(item, field_name), field_name)

                metadata = (
                    item.metadata
                    if type(item) is DocumentHeader
                    else item.format_metadata
                )
                metadata_text = repr(
                    tuple((entry.key, entry.value) for entry in metadata)
                ).lower()
                for forbidden in deferred_authority_fields:
                    self.assertNotIn(forbidden.lower(), metadata_text)


class ParserCapabilityAndWriterTests(_Wave4Fixture):
    def test_builtin_capability_matrix_and_reader_only_writers_fail_closed(
        self,
    ) -> None:
        streaming = {
            LINE_TEXT_V1,
            GETTEXT_PO_V1,
            GETTEXT_POT_V1,
            TMX_LEVEL1_V1,
            TERMBASE_CSV_V1,
            TERMBASE_XLSX_V1,
        }
        expected_opaque_features = {
            LOCALCAT_JSON_V1: (),
            LINE_TEXT_V1: (),
            GETTEXT_PO_V1: (),
            GETTEXT_POT_V1: (),
            TMX_LEVEL1_V1: (),
            NORMALIZED_TM_JSON_V1: (),
            TERMBASE_CSV_V1: (
                "explicit-column-selection",
                "legacy-header-allowlist",
            ),
            TERMBASE_XLSX_V1: (
                "conditional-dependency:openpyxl>=3.1,<4",
                "data-only-cells",
                "preflight-all-opc-xml",
            ),
        }
        for case in self.cases:
            with self.subTest(case=case.label):
                selected = self.surface.select(
                    SelectionRequest(case.purpose, format_id=case.format_id)
                )
                self.assertIs(type(selected), CodecDescriptor)
                capabilities = selected.capabilities
                self.assertTrue(capabilities.readable)
                self.assertTrue(capabilities.validatable)
                self.assertTrue(capabilities.iterator_view)
                self.assertTrue(capabilities.materialized_view)
                self.assertEqual(
                    capabilities.canonical_write,
                    case.format_id == LOCALCAT_JSON_V1,
                )
                self.assertEqual(
                    capabilities.format_profile,
                    selected.limit_profile.profile_id,
                )
                self.assertEqual(
                    selected.declared_issue_codes,
                    tuple(sorted(set(selected.declared_issue_codes))),
                )
                self.assertFalse(capabilities.source_round_trip_write)
                self.assertEqual(
                    capabilities.opaque_features,
                    expected_opaque_features[case.format_id],
                )
                self.assertEqual(
                    capabilities.streaming_input,
                    case.format_id in streaming,
                )
                self.assertEqual(
                    capabilities.active_sheet_only,
                    case.format_id == TERMBASE_XLSX_V1,
                )

                if case.format_id != LOCALCAT_JSON_V1:
                    target_parent = self.root / f"reader-only-{case.label}"
                    target = target_parent / "must-not-exist.output"
                    with self.assertRaises(ContractViolation) as caught:
                        self.surface.write_canonical(
                            case.purpose,
                            self._canonical_request(case.format_id),
                            TargetReference(
                                safe_root=str(target_parent),
                                selected_path=str(target),
                                display_hint=target.name,
                            ),
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
                    )
                    self.assertFalse(target_parent.exists())
        self.assertFalse((self.root / "unsupported-target-parent").exists())

    def test_provider_round_trip_caller_validates_before_exactly_one_write(
        self,
    ) -> None:
        descriptor, _observations = _counting_descriptor(
            "round-trip",
            source_round_trip_write=True,
        )
        surface = create_parser_application_surface(
            providers=(
                ProviderBinding(
                    provider_id="plugin.wave4",
                    provider=_Provider((descriptor,)),
                    enabled=True,
                    compatible_versions=("1",),
                ),
            )
        )
        selected = surface.select(
            SelectionRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                format_id=descriptor.format_id,
            )
        )
        self.assertIs(type(selected), CodecDescriptor)
        self.assertIs(selected, descriptor)
        self.assertTrue(selected.capabilities.source_round_trip_write)
        self.assertFalse(selected.capabilities.canonical_write)
        self.assertEqual(
            selected.capabilities.opaque_features,
            ("opaque-round-trip-token",),
        )

        expected_identity = selected.identity
        source_fingerprint = "a" * 64
        state_fingerprint = "b" * 64
        valid = RoundTripTokenEnvelope(
            codec_identity=expected_identity,
            source_fingerprint=source_fingerprint,
            format_state_fingerprint=state_fingerprint,
            opaque_payload=b"\x00private-format-state\xff",
        )
        foreign_provider = RoundTripTokenEnvelope(
            CodecIdentity(
                "plugin.other",
                expected_identity.codec_id,
                expected_identity.codec_version,
            ),
            source_fingerprint,
            state_fingerprint,
            b"foreign-provider",
        )
        foreign_codec = RoundTripTokenEnvelope(
            CodecIdentity(
                expected_identity.provider_id,
                "codec.other",
                expected_identity.codec_version,
            ),
            source_fingerprint,
            state_fingerprint,
            b"foreign-codec",
        )
        incompatible = RoundTripTokenEnvelope(
            CodecIdentity(
                expected_identity.provider_id,
                expected_identity.codec_id,
                "2",
            ),
            source_fingerprint,
            state_fingerprint,
            b"version",
        )
        stale = RoundTripTokenEnvelope(
            expected_identity,
            "c" * 64,
            state_fingerprint,
            b"stale",
        )
        wrong_state = RoundTripTokenEnvelope(
            expected_identity,
            source_fingerprint,
            "d" * 64,
            b"state",
        )
        cases = (
            ("missing", None, RoundTripTokenFailureReason.MISSING),
            (
                "foreign-provider",
                foreign_provider,
                RoundTripTokenFailureReason.FOREIGN_CODEC,
            ),
            (
                "foreign-codec",
                foreign_codec,
                RoundTripTokenFailureReason.FOREIGN_CODEC,
            ),
            (
                "incompatible-version",
                incompatible,
                RoundTripTokenFailureReason.VERSION_INCOMPATIBLE,
            ),
            ("stale-source", stale, RoundTripTokenFailureReason.STALE_SOURCE),
            (
                "wrong-format-state",
                wrong_state,
                RoundTripTokenFailureReason.FORMAT_STATE_MISMATCH,
            ),
        )
        target_parent = self.root / "round-trip-target-parent"
        target = target_parent / "opaque.output"
        target_reference = TargetReference(
            safe_root=str(target_parent),
            selected_path=str(target),
            display_hint=target.name,
        )

        def fake_target_writer(
            reference: TargetReference,
            payload: bytes,
        ) -> None:
            Path(reference.safe_root).mkdir(parents=True)
            Path(reference.selected_path).write_bytes(payload)

        target_writer = mock.Mock(side_effect=fake_target_writer)
        caller = _RoundTripCaller(
            selected,
            source_fingerprint,
            state_fingerprint,
            target_writer,
        )
        for label, token, reason in cases:
            with self.subTest(case=label, reason=reason.value):
                with self.assertRaises(RoundTripTokenValidationError) as caught:
                    caller.write(token, target_reference)
                self.assertIs(caught.exception.reason, reason)
                self.assertEqual(
                    caught.exception.code,
                    "PARSER.CAPABILITY.INVALID_TOKEN",
                )
                target_writer.assert_not_called()
                self.assertFalse(target_parent.exists())

        caller.write(valid, target_reference)
        target_writer.assert_called_once_with(
            target_reference,
            b"\x00private-format-state\xff",
        )
        self.assertEqual(target.read_bytes(), b"\x00private-format-state\xff")

    @unittest.skipUnless(os.name == "posix", "rooted atomic writer is POSIX-specific")
    def test_canonical_write_faults_preserve_target_and_leave_no_receipt(
        self,
    ) -> None:
        prepared = self.surface.prepare_canonical(
            EffectivePurpose.PROJECT_DOCUMENT,
            self._canonical_request(LOCALCAT_JSON_V1),
        )
        failures = (
            ("parser_source._write_all", OSError("write failed")),
            ("parser_source.os.fsync", OSError("fsync failed")),
            ("parser_source.os.replace", OSError("replace failed")),
        )
        for index, (qualified, failure) in enumerate(failures):
            with self.subTest(fault=qualified):
                target = self.root / f"atomic-{index}.json"
                target.write_bytes(b"last-known-good")
                before = tuple(sorted(path.name for path in self.root.iterdir()))
                with mock.patch(qualified, side_effect=failure):
                    with self.assertRaises(ContractViolation) as caught:
                        prepared.write(
                            TargetReference(
                                safe_root=str(self.root),
                                selected_path=str(target),
                                display_hint=target.name,
                            )
                        )
                self.assertEqual(
                    caught.exception.code,
                    "PARSER.SOURCE.WRITE_FAILED",
                )
                self.assertEqual(target.read_bytes(), b"last-known-good")
                self.assertEqual(
                    tuple(sorted(path.name for path in self.root.iterdir())),
                    before,
                )

        target = self.root / "atomic-temp-create.json"
        target.write_bytes(b"last-known-good")
        collision = self.root / ".parser-collision.tmp"
        collision.write_bytes(b"pre-existing private object")
        before = tuple(sorted(path.name for path in self.root.iterdir()))
        with mock.patch("parser_source.secrets.token_hex", return_value="collision"):
            with self.assertRaises(ContractViolation) as caught:
                prepared.write(
                    TargetReference(
                        safe_root=str(self.root),
                        selected_path=str(target),
                        display_hint=target.name,
                    )
                )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_FAILED")
        self.assertEqual(target.read_bytes(), b"last-known-good")
        self.assertEqual(
            tuple(sorted(path.name for path in self.root.iterdir())),
            before,
        )


if __name__ == "__main__":
    unittest.main()
