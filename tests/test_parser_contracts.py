from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError


class SelectionContractTests(unittest.TestCase):
    def test_effective_purposes_and_builtin_format_ids_are_closed_and_stable(self) -> None:
        from parser_contracts import BUILTIN_FORMAT_IDS, EffectivePurpose, FormatId

        self.assertEqual(
            {purpose.value for purpose in EffectivePurpose},
            {
                "project_document",
                "language_resource.translation_memory",
                "language_resource.termbase",
            },
        )
        self.assertEqual(
            tuple(format_id.value for format_id in BUILTIN_FORMAT_IDS),
            (
                "localcat-json-v1",
                "line-text-v1",
                "gettext-po-v1",
                "gettext-pot-v1",
                "tmx-level1-v1",
                "normalized-tm-json-v1",
                "termbase-csv-v1",
                "termbase-xlsx-v1",
            ),
        )
        with self.assertRaises(ValueError):
            FormatId("")
        with self.assertRaises(ValueError):
            FormatId("raw source body / not an identifier")

    def test_selection_request_requires_format_or_nonempty_bounded_hints(self) -> None:
        from parser_contracts import (
            MAX_EXTENSION_HINT_CHARS,
            MAX_HINT_VALUES_PER_KIND,
            MAX_MIME_HINT_CHARS,
            MAX_SNIFF_PREFIX_BYTES,
            EffectivePurpose,
            FormatId,
            SelectionHints,
            SelectionRequest,
        )

        explicit = SelectionRequest(
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            format_id=FormatId("localcat-json-v1"),
        )
        self.assertIsNone(explicit.hints)
        discovered = SelectionRequest(
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            hints=SelectionHints(
                extensions=(".json",),
                mime_types=("application/json",),
                prefix=b"{}",
            ),
        )
        self.assertIsNone(discovered.format_id)

        invalid_requests = (
            {},
            {
                "format_id": FormatId("localcat-json-v1"),
                "hints": SelectionHints(extensions=(".json",)),
            },
        )
        for extra in invalid_requests:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                SelectionRequest(purpose=EffectivePurpose.PROJECT_DOCUMENT, **extra)
        with self.assertRaises(ValueError):
            SelectionHints()
        with self.assertRaises(ValueError):
            SelectionHints(prefix=b"x" * (MAX_SNIFF_PREFIX_BYTES + 1))
        with self.assertRaises(ValueError):
            SelectionHints(extensions=(".x",) * (MAX_HINT_VALUES_PER_KIND + 1))
        invalid_extensions = (
            "json",
            ".bad/path",
            ".bad hint",
            "." + "x" * MAX_EXTENSION_HINT_CHARS,
        )
        for extension in invalid_extensions:
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                SelectionHints(extensions=(extension,))
        invalid_mime_types = (
            "application-json",
            "application/json; charset=utf-8",
            "application/bad mime",
            "a/" + "x" * MAX_MIME_HINT_CHARS,
        )
        for mime_type in invalid_mime_types:
            with self.subTest(mime_type=mime_type), self.assertRaises(ValueError):
                SelectionHints(mime_types=(mime_type,))
        normalized = SelectionHints(
            extensions=(".JSON",),
            mime_types=("Application/JSON",),
        )
        self.assertEqual(normalized.extensions, (".json",))
        self.assertEqual(normalized.mime_types, ("application/json",))
        with self.assertRaises(TypeError):
            SelectionHints(prefix="not bytes")  # type: ignore[arg-type]

    def test_termbase_selectors_reject_ambiguous_or_implicit_columns(self) -> None:
        from parser_contracts import (
            ColumnSelectorKind,
            TermbaseColumnSelection,
            TermbaseColumnSelector,
            TermbaseHeaderPolicy,
        )

        header_source = TermbaseColumnSelector(
            kind=ColumnSelectorKind.HEADER_NAME,
            header_name=" Source ",
        )
        self.assertEqual(header_source.header_name, "Source")
        index_target = TermbaseColumnSelector(
            kind=ColumnSelectorKind.ZERO_BASED_INDEX,
            zero_based_index=1,
        )
        self.assertEqual(index_target.zero_based_index, 1)

        invalid_selectors = (
            {"kind": ColumnSelectorKind.HEADER_NAME},
            {"kind": ColumnSelectorKind.HEADER_NAME, "header_name": "   "},
            {
                "kind": ColumnSelectorKind.HEADER_NAME,
                "header_name": "Source",
                "zero_based_index": 0,
            },
            {"kind": ColumnSelectorKind.ZERO_BASED_INDEX},
            {"kind": ColumnSelectorKind.ZERO_BASED_INDEX, "zero_based_index": -1},
            {"kind": ColumnSelectorKind.ZERO_BASED_INDEX, "zero_based_index": True},
            {
                "kind": ColumnSelectorKind.ZERO_BASED_INDEX,
                "header_name": "Source",
                "zero_based_index": 0,
            },
        )
        for kwargs in invalid_selectors:
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                TermbaseColumnSelector(**kwargs)

        header_target = TermbaseColumnSelector(
            kind=ColumnSelectorKind.HEADER_NAME,
            header_name="Target",
        )
        with self.assertRaises(ValueError):
            TermbaseColumnSelection(
                source=header_source,
                target=header_target,
                header_policy=TermbaseHeaderPolicy.NO_HEADER,
            )
        with self.assertRaises(ValueError):
            TermbaseColumnSelection(
                source=index_target,
                target=TermbaseColumnSelector(
                    kind=ColumnSelectorKind.ZERO_BASED_INDEX,
                    zero_based_index=1,
                ),
                header_policy=TermbaseHeaderPolicy.NO_HEADER,
            )
        mixed = TermbaseColumnSelection(
            source=header_source,
            target=TermbaseColumnSelector(
                kind=ColumnSelectorKind.ZERO_BASED_INDEX,
                zero_based_index=2,
            ),
            header_policy=TermbaseHeaderPolicy.FIRST_ROW,
        )
        self.assertEqual(mixed.source.header_name, "Source")
        self.assertEqual(mixed.target.zero_based_index, 2)

    def test_legacy_column_preset_is_explicit_and_exact(self) -> None:
        from parser_contracts import (
            ColumnSelectorKind,
            TermbaseColumnSelection,
            TermbaseHeaderPolicy,
        )

        preset = TermbaseColumnSelection.legacy_first_two_columns()
        self.assertEqual(preset.source.kind, ColumnSelectorKind.ZERO_BASED_INDEX)
        self.assertEqual(preset.source.zero_based_index, 0)
        self.assertEqual(preset.target.zero_based_index, 1)
        self.assertEqual(preset.header_policy, TermbaseHeaderPolicy.LEGACY_ALLOWLIST)

    def test_read_request_validates_purpose_format_and_options_before_input(self) -> None:
        from parser_contracts import (
            ContractViolation,
            EffectivePurpose,
            FormatId,
            ReadRequest,
            TermbaseColumnSelection,
            TermbaseReadOptions,
            TmxReadOptions,
        )

        project = ReadRequest(
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            format_id=FormatId("localcat-json-v1"),
        )
        self.assertIsNone(project.termbase_options)
        termbase_options = TermbaseReadOptions(
            columns=TermbaseColumnSelection.legacy_first_two_columns()
        )
        termbase = ReadRequest(
            purpose=EffectivePurpose.TERMBASE,
            format_id=FormatId("termbase-csv-v1"),
            termbase_options=termbase_options,
        )
        self.assertIs(termbase.termbase_options, termbase_options)
        tmx_options = TmxReadOptions(
            source_locale="en_US",
            target_locale="zh-CN",
        )
        tmx = ReadRequest(
            purpose=EffectivePurpose.TRANSLATION_MEMORY,
            format_id=FormatId("tmx-level1-v1"),
            tmx_options=tmx_options,
        )
        self.assertIs(tmx.tmx_options, tmx_options)

        cases = (
            (
                {
                    "purpose": EffectivePurpose.TERMBASE,
                    "format_id": FormatId("termbase-csv-v1"),
                },
                "PARSER.TERMBASE.COLUMN_SELECTION_REQUIRED",
            ),
            (
                {
                    "purpose": EffectivePurpose.PROJECT_DOCUMENT,
                    "format_id": FormatId("localcat-json-v1"),
                    "termbase_options": termbase_options,
                },
                "PARSER.TERMBASE.COLUMN_SELECTION_NOT_APPLICABLE",
            ),
            (
                {
                    "purpose": EffectivePurpose.PROJECT_DOCUMENT,
                    "format_id": FormatId("tmx-level1-v1"),
                },
                "PARSER.SELECTION.UNSUPPORTED",
            ),
            (
                {
                    "purpose": EffectivePurpose.TRANSLATION_MEMORY,
                    "format_id": FormatId("tmx-level1-v1"),
                },
                "PARSER.TMX.LOCALE_SELECTION_REQUIRED",
            ),
            (
                {
                    "purpose": EffectivePurpose.TRANSLATION_MEMORY,
                    "format_id": FormatId("normalized-tm-json-v1"),
                    "tmx_options": tmx_options,
                },
                "PARSER.TMX.LOCALE_SELECTION_NOT_APPLICABLE",
            ),
        )
        for kwargs, expected_code in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractViolation) as caught:
                ReadRequest(**kwargs)
            self.assertEqual(caught.exception.code, expected_code)

    def test_selection_failure_is_structured_safe_and_immutable(self) -> None:
        from parser_contracts import (
            EffectivePurpose,
            FormatId,
            SelectionFailure,
            SelectionHintSummary,
            SelectionHints,
            SupportedCombination,
        )

        failure = SelectionFailure(
            code="PARSER.SELECTION.UNSUPPORTED",
            requested_purpose=EffectivePurpose.PROJECT_DOCUMENT,
            requested_format_id=FormatId("tmx-level1-v1"),
            observed_hints=SelectionHintSummary.from_hints(
                SelectionHints(extensions=(".tmx",), prefix=b"raw source text")
            ),
            supported_combinations=(
                SupportedCombination(
                    purpose=EffectivePurpose.TRANSLATION_MEMORY,
                    format_id=FormatId("tmx-level1-v1"),
                ),
            ),
            supported_combination_count=1,
            supported_combinations_truncated=False,
        )
        self.assertEqual(failure.supported_combinations[0].format_id.value, "tmx-level1-v1")
        self.assertEqual(failure.observed_hints.extensions, (".tmx",))
        self.assertTrue(failure.observed_hints.sniff_prefix_present)
        self.assertEqual(failure.observed_hints.sniff_prefix_byte_count, 15)
        self.assertFalse(hasattr(failure.observed_hints, "prefix"))
        self.assertFalse(hasattr(failure, "safe_summary"))
        self.assertNotIn(b"raw source text", repr(failure).encode("utf-8"))
        with self.assertRaises(TypeError):
            SelectionFailure(
                code="PARSER.SELECTION.UNSUPPORTED",
                requested_purpose=EffectivePurpose.PROJECT_DOCUMENT,
                requested_format_id=None,
                observed_hints=SelectionHints(prefix=b"body"),  # type: ignore[arg-type]
                supported_combinations=(),
                supported_combination_count=0,
                supported_combinations_truncated=False,
            )
        with self.assertRaises(ValueError):
            SelectionFailure(
                code="PARSER.SELECTION.UNSUPPORTED",
                requested_purpose=EffectivePurpose.PROJECT_DOCUMENT,
                requested_format_id=None,
                observed_hints=None,
                supported_combinations=tuple(
                    SupportedCombination(
                        purpose=EffectivePurpose.PROJECT_DOCUMENT,
                        format_id=FormatId(f"custom-{index:02d}"),
                    )
                    for index in range(65)
                ),
                supported_combination_count=65,
                supported_combinations_truncated=False,
            )
        with self.assertRaises(FrozenInstanceError):
            failure.code = "changed"  # type: ignore[misc]

    def test_selection_failure_supported_combination_truncation_truth_table(self) -> None:
        from parser_contracts import (
            EffectivePurpose,
            FormatId,
            SelectionFailure,
            SupportedCombination,
        )

        retained = (
            SupportedCombination(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                format_id=FormatId("localcat-json-v1"),
            ),
        )
        truncated = SelectionFailure(
            code="PARSER.SELECTION.UNSUPPORTED",
            requested_purpose=EffectivePurpose.PROJECT_DOCUMENT,
            requested_format_id=None,
            observed_hints=None,
            supported_combinations=retained,
            supported_combination_count=2,
            supported_combinations_truncated=True,
        )
        self.assertEqual(truncated.supported_combination_count, 2)
        self.assertTrue(truncated.supported_combinations_truncated)

        contradictions = (
            (0, False),  # total cannot be smaller than retained
            (0, True),  # truncation cannot repair an impossible total
            (1, True),  # equal total means nothing was truncated
            (2, False),  # larger total means retained entries were truncated
        )
        for total_count, truncated_flag in contradictions:
            with self.subTest(
                total_count=total_count,
                truncated=truncated_flag,
            ), self.assertRaises(ValueError):
                SelectionFailure(
                    code="PARSER.SELECTION.UNSUPPORTED",
                    requested_purpose=EffectivePurpose.PROJECT_DOCUMENT,
                    requested_format_id=None,
                    observed_hints=None,
                    supported_combinations=retained,
                    supported_combination_count=total_count,
                    supported_combinations_truncated=truncated_flag,
                )


class NeutralRecordContractTests(unittest.TestCase):
    @staticmethod
    def _snapshot():
        from parser_contracts import SourceSnapshotIdentity

        return SourceSnapshotIdentity(
            relative_reference_sha256="1" * 64,
            regular_file_identity="1048576:42",
            original_size=12,
            original_mtime_ns=123456789,
            content_sha256="2" * 64,
            byte_count=12,
            schema_version=1,
        )

    @staticmethod
    def _capabilities():
        from parser_contracts import CodecCapabilities

        return CodecCapabilities(
            readable=True,
            validatable=True,
            canonical_write=False,
            source_round_trip_write=False,
            streaming_input=True,
            iterator_view=True,
            materialized_view=True,
            format_profile="line-text-v1",
        )

    def test_snapshot_identity_is_strongly_typed_and_self_consistent(self) -> None:
        from parser_contracts import SourceSnapshotIdentity

        snapshot = self._snapshot()
        self.assertEqual(snapshot.byte_count, snapshot.original_size)
        invalid = (
            {"content_sha256": "not-a-digest"},
            {"relative_reference_sha256": "f" * 63},
            {"original_size": -1},
            {"original_mtime_ns": -1},
            {"byte_count": 13},
            {"schema_version": True},
        )
        base = {
            "relative_reference_sha256": "1" * 64,
            "regular_file_identity": "1048576:42",
            "original_size": 12,
            "original_mtime_ns": 123456789,
            "content_sha256": "2" * 64,
            "byte_count": 12,
            "schema_version": 1,
        }
        for update in invalid:
            with self.subTest(update=update), self.assertRaises((TypeError, ValueError)):
                SourceSnapshotIdentity(**(base | update))

    def test_metadata_is_immutable_json_compatible_scalar_or_tuple_only(self) -> None:
        from parser_contracts import MetadataEntry

        metadata = MetadataEntry(
            key="gettext.comments",
            value=("translator note", 3, True, None, ("nested", 2.5)),
        )
        self.assertEqual(metadata.value[-1], ("nested", 2.5))
        invalid_values = (["list"], {"dict": "value"}, float("nan"), object())
        for value in invalid_values:
            with self.subTest(value=type(value).__name__), self.assertRaises(
                (TypeError, ValueError)
            ):
                MetadataEntry(key="format.value", value=value)
        with self.assertRaises(ValueError):
            MetadataEntry(key="", value=None)

    def test_parsed_segment_preserves_target_presence_and_state(self) -> None:
        from parser_contracts import (
            ParsedSegment,
            RawSpeaker,
            TargetPresence,
            TranslationState,
        )

        missing = ParsedSegment(
            local_id="line-1",
            source="source",
            target=None,
            target_presence=TargetPresence.MISSING,
            translation_state=None,
            speaker=RawSpeaker(""),
            format_metadata=(),
        )
        explicit_empty = ParsedSegment(
            local_id="entry-2",
            source="source",
            target="",
            target_presence=TargetPresence.EXPLICIT_EMPTY,
            translation_state=TranslationState.FORMAT_DERIVED_UNCONFIRMED,
            speaker=RawSpeaker("Alice"),
            format_metadata=(),
        )
        present = ParsedSegment(
            local_id="segment-3",
            source="source",
            target="target",
            target_presence=TargetPresence.PRESENT,
            translation_state=TranslationState.CONFIRMED,
            speaker=RawSpeaker("Alice"),
            format_metadata=(),
        )
        self.assertIsNone(missing.target)
        self.assertEqual(explicit_empty.target, "")
        self.assertEqual(present.target, "target")

        bad_presence = (
            (None, TargetPresence.EXPLICIT_EMPTY),
            ("", TargetPresence.MISSING),
            ("nonempty", TargetPresence.EXPLICIT_EMPTY),
            ("", TargetPresence.PRESENT),
        )
        for target, presence in bad_presence:
            with self.subTest(target=target, presence=presence), self.assertRaises(ValueError):
                ParsedSegment(
                    local_id="segment",
                    source="source",
                    target=target,
                    target_presence=presence,
                    translation_state=None,
                    speaker=RawSpeaker(""),
                    format_metadata=(),
                )

    def test_parsed_document_is_one_input_and_requires_unique_local_ids(self) -> None:
        from parser_contracts import (
            FormatId,
            ParsedDocument,
            ParsedSegment,
            RawSpeaker,
            TargetPresence,
        )

        segment = ParsedSegment(
            local_id="segment-1",
            source="source",
            target=None,
            target_presence=TargetPresence.MISSING,
            translation_state=None,
            speaker=RawSpeaker(""),
            format_metadata=(),
        )
        document = ParsedDocument(
            source=self._snapshot(),
            format_id=FormatId("line-text-v1"),
            name="chapter",
            source_locale=None,
            target_locale=None,
            segments=(segment,),
            document_metadata=(),
            issues=(),
            capabilities=self._capabilities(),
        )
        self.assertEqual(document.segments, (segment,))
        self.assertFalse(hasattr(document, "document_id"))
        with self.assertRaises(ValueError):
            ParsedDocument(
                source=self._snapshot(),
                format_id=FormatId("line-text-v1"),
                name="chapter",
                source_locale=None,
                target_locale=None,
                segments=(segment, segment),
                document_metadata=(),
                issues=(),
                capabilities=self._capabilities(),
            )

    def test_resource_record_is_not_a_project_document_or_store_record(self) -> None:
        from parser_contracts import RawSpeaker, ResourceRecord

        record = ResourceRecord(
            local_id="record-1",
            source="source",
            target="target",
            speaker=RawSpeaker("Speaker"),
            format_metadata=(),
        )
        self.assertFalse(hasattr(record, "document_id"))
        self.assertFalse(hasattr(record, "canonical_id"))
        with self.assertRaises(ValueError):
            ResourceRecord(
                local_id="record-1",
                source="",
                target="target",
                speaker=RawSpeaker(""),
                format_metadata=(),
            )

    def test_canonical_write_dto_is_editor_independent_and_keeps_legacy_states(self) -> None:
        from parser_contracts import (
            CanonicalBytes,
            CanonicalDocumentWrite,
            CanonicalSegmentWrite,
            CanonicalSerializeRequest,
            CodecIdentity,
            FormatId,
            RawSpeaker,
        )

        segment = CanonicalSegmentWrite(
            local_id="segment-1",
            source="source",
            target="",
            speaker=RawSpeaker(""),
            confirmed=True,
        )
        document = CanonicalDocumentWrite(
            name="chapter",
            source_locale="en-US",
            target_locale="zh-CN",
            segments=(segment,),
        )
        request = CanonicalSerializeRequest(
            format_id=FormatId("localcat-json-v1"),
            document=document,
        )
        serialized = CanonicalBytes(
            codec_identity=CodecIdentity("localcat", "localcat-json", "1"),
            format_id=request.format_id,
            schema_version=1,
            payload=b"{}",
        )
        self.assertTrue(document.segments[0].confirmed)
        self.assertEqual(serialized.payload, b"{}")
        with self.assertRaises(TypeError):
            CanonicalSegmentWrite(
                local_id="segment-2",
                source="source",
                target="target",
                speaker=RawSpeaker(""),
                confirmed=1,  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            CanonicalDocumentWrite(
                name="chapter",
                source_locale="en-US",
                target_locale="zh-CN",
                segments=(segment, segment),
            )


class CapabilityContractTests(unittest.TestCase):
    def test_capability_snapshot_is_immutable_and_reader_does_not_imply_writer(self) -> None:
        from parser_contracts import CodecCapabilities

        capabilities = CodecCapabilities(
            readable=True,
            validatable=True,
            canonical_write=False,
            source_round_trip_write=False,
            streaming_input=True,
            iterator_view=True,
            materialized_view=False,
            format_profile="tmx-level1-v1",
            opaque_features=("inline-xml-rejected",),
        )
        self.assertTrue(capabilities.readable)
        self.assertFalse(capabilities.canonical_write)
        self.assertFalse(capabilities.source_round_trip_write)
        with self.assertRaises(FrozenInstanceError):
            capabilities.readable = False  # type: ignore[misc]
        with self.assertRaises(TypeError):
            CodecCapabilities(
                readable=1,  # type: ignore[arg-type]
                validatable=True,
                canonical_write=False,
                source_round_trip_write=False,
                streaming_input=False,
                iterator_view=True,
                materialized_view=True,
                format_profile="profile",
            )

    def test_termbase_preview_contract_is_bounded_and_identity_bound(self) -> None:
        from parser_contracts import (
            EffectivePurpose,
            TERMBASE_CSV_V1,
            TermbaseColumnPreview,
            TermbaseColumnPreviewRequest,
            TermbasePreviewColumn,
        )

        request = TermbaseColumnPreviewRequest(
            purpose=EffectivePurpose.TERMBASE,
            format_id=TERMBASE_CSV_V1,
        )
        preview = TermbaseColumnPreview(
            source=NeutralRecordContractTests._snapshot(),
            codec_identity=self._codec_identity(),
            format_id=request.format_id,
            columns=(
                TermbasePreviewColumn(0, "Source", 6, False),
                TermbasePreviewColumn(1, None),
            ),
            total_column_count=2,
            columns_truncated=False,
            legacy_header_detected=False,
            active_sheet_name=None,
        )
        self.assertEqual(preview.columns[0].header_candidate, "Source")
        with self.assertRaises(ValueError):
            TermbaseColumnPreview(
                source=preview.source,
                codec_identity=preview.codec_identity,
                format_id=preview.format_id,
                columns=preview.columns,
                total_column_count=3,
                columns_truncated=False,
                legacy_header_detected=False,
                active_sheet_name=None,
            )
        with self.assertRaises(ValueError):
            TermbasePreviewColumn(0, "x" * 257)

    @staticmethod
    def _codec_identity():
        from parser_contracts import CodecIdentity

        return CodecIdentity("localcat", "termbase-csv", "1")

    def test_round_trip_token_payload_remains_opaque_bytes(self) -> None:
        from parser_contracts import CodecIdentity, RoundTripTokenEnvelope

        token = RoundTripTokenEnvelope(
            codec_identity=CodecIdentity("rpy-provider", "rpy", "2.1"),
            source_fingerprint="a" * 64,
            format_state_fingerprint="b" * 64,
            opaque_payload=b"\x00\xffprivate-format-state",
        )
        self.assertEqual(token.opaque_payload, b"\x00\xffprivate-format-state")
        self.assertFalse(hasattr(token, "decoded_payload"))
        with self.assertRaises(TypeError):
            RoundTripTokenEnvelope(
                codec_identity=token.codec_identity,
                source_fingerprint="a" * 64,
                format_state_fingerprint="b" * 64,
                opaque_payload="private",  # type: ignore[arg-type]
            )

    def test_token_preflight_structures_missing_foreign_version_and_stale_failures(self) -> None:
        from parser_contracts import (
            CodecIdentity,
            RoundTripTokenEnvelope,
            RoundTripTokenFailureReason,
            RoundTripTokenValidationError,
            validate_round_trip_token,
        )

        expected = CodecIdentity("rpy-provider", "rpy", "2.1")
        good = RoundTripTokenEnvelope(
            codec_identity=expected,
            source_fingerprint="a" * 64,
            format_state_fingerprint="b" * 64,
            opaque_payload=b"opaque",
        )
        self.assertIs(
            validate_round_trip_token(
                good,
                expected_codec_identity=expected,
                expected_source_fingerprint="a" * 64,
                expected_format_state_fingerprint="b" * 64,
            ),
            good,
        )

        cases = (
            (None, expected, "a" * 64, "b" * 64, RoundTripTokenFailureReason.MISSING),
            (
                good,
                CodecIdentity("foreign", "rpy", "2.1"),
                "a" * 64,
                "b" * 64,
                RoundTripTokenFailureReason.FOREIGN_CODEC,
            ),
            (
                good,
                CodecIdentity("rpy-provider", "rpy", "3.0"),
                "a" * 64,
                "b" * 64,
                RoundTripTokenFailureReason.VERSION_INCOMPATIBLE,
            ),
            (
                good,
                expected,
                "c" * 64,
                "b" * 64,
                RoundTripTokenFailureReason.STALE_SOURCE,
            ),
            (
                good,
                expected,
                "a" * 64,
                "c" * 64,
                RoundTripTokenFailureReason.FORMAT_STATE_MISMATCH,
            ),
        )
        for token, identity, source, state, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(
                RoundTripTokenValidationError
            ) as caught:
                validate_round_trip_token(
                    token,
                    expected_codec_identity=identity,
                    expected_source_fingerprint=source,
                    expected_format_state_fingerprint=state,
                )
            self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.INVALID_TOKEN")
            self.assertEqual(caught.exception.reason, reason)


class DiagnosticAndTerminalContractTests(unittest.TestCase):
    @staticmethod
    def _snapshot():
        from parser_contracts import SourceSnapshotIdentity

        return SourceSnapshotIdentity(
            relative_reference_sha256="1" * 64,
            regular_file_identity="1048576:42",
            original_size=12,
            original_mtime_ns=123456789,
            content_sha256="2" * 64,
            byte_count=12,
            schema_version=1,
        )

    @staticmethod
    def _identity():
        from parser_contracts import CodecIdentity

        return CodecIdentity("localcat", "line-text", "1.0.0")

    @staticmethod
    def _capabilities():
        from parser_contracts import CodecCapabilities

        return CodecCapabilities(
            readable=True,
            validatable=True,
            canonical_write=False,
            source_round_trip_write=False,
            streaming_input=True,
            iterator_view=True,
            materialized_view=True,
            format_profile="line-text-v1",
        )

    @staticmethod
    def _profile(**updates):
        from parser_contracts import LimitProfile

        values = {
            "profile_id": "line-text-v1",
            "profile_version": 1,
            "max_input_bytes": 100 * 1024 * 1024,
            "max_decoded_field_chars": 100 * 1024 * 1024,
            "max_records": 1_000_000,
            "max_materialized_records": 100_000,
            "max_retained_issues": 256,
            "declared_issue_codes": (
                "PARSER.LIMIT.FIELD",
                "PARSER.LIMIT.METADATA",
                "PARSER.SOURCE.CANCELLED",
                "PARSER.SYNTAX.INVALID_FIELD",
            ),
            "max_metadata_entries_per_container": 256,
            "max_metadata_decoded_chars_per_container": 1024 * 1024,
            "max_metadata_decoded_chars_total": 16 * 1024 * 1024,
            "max_structure_depth": 8,
        }
        values.update(updates)
        return LimitProfile(**values)

    def test_limit_profile_freezes_design_dimensions_and_finite_allowlist(self) -> None:
        from parser_contracts import LimitProfile

        profile = self._profile()
        self.assertEqual(profile.max_input_bytes, 100 * 1024 * 1024)
        self.assertEqual(profile.max_records, 1_000_000)
        self.assertEqual(profile.max_materialized_records, 100_000)
        self.assertIsNone(profile.max_expanded_bytes)
        with self.assertRaises(FrozenInstanceError):
            profile.max_records = 1  # type: ignore[misc]

        invalid = (
            {"max_input_bytes": 0},
            {"max_materialized_records": 1_000_001},
            {"max_retained_issues": 257},
            {"declared_issue_codes": ("not.stable",)},
            {
                "declared_issue_codes": tuple(
                    f"PARSER.PLUGIN.CODE_{index:02d}" for index in range(65)
                )
            },
            {"max_metadata_decoded_chars_total": 1024},
        )
        for update in invalid:
            with self.subTest(update=next(iter(update))), self.assertRaises(
                (TypeError, ValueError)
            ):
                self._profile(**update)

    def test_metadata_limit_helper_rejects_entries_chars_total_and_depth(self) -> None:
        from parser_contracts import (
            ContractViolation,
            MetadataEntry,
            validate_metadata_containers,
        )

        profile = self._profile(
            max_metadata_entries_per_container=1,
            max_metadata_decoded_chars_per_container=8,
            max_metadata_decoded_chars_total=8,
            max_structure_depth=2,
        )
        validate_metadata_containers(
            ((MetadataEntry("a", ("bc",)),),),
            limit_profile=profile,
        )
        for scalar in (1.25, False, None):
            with self.subTest(scalar=scalar):
                validate_metadata_containers(
                    ((MetadataEntry("a", scalar),),),
                    limit_profile=profile,
                )
        invalid = (
            ((MetadataEntry("a", None), MetadataEntry("b", None)),),
            ((MetadataEntry("long-key", "xx"),),),
            ((MetadataEntry("a", ((("too-deep",),),)),),),
            ((MetadataEntry("a", "1234"),), (MetadataEntry("b", "4567"),)),
            ((MetadataEntry("a", 10**10_000),),),
            ((MetadataEntry("abcd", False),),),
            ((MetadataEntry("abcde", 1.25),),),
            ((MetadataEntry("abcde", None),),),
        )
        for containers in invalid:
            with self.subTest(containers=containers), self.assertRaises(
                ContractViolation
            ) as caught:
                validate_metadata_containers(containers, limit_profile=profile)
            self.assertEqual(caught.exception.code, "PARSER.LIMIT.METADATA")

    def test_parse_issue_has_bounded_safe_location_without_content_fields(self) -> None:
        from parser_contracts import IssueSeverity, ParseIssue

        issue = ParseIssue(
            code="PARSER.SYNTAX.INVALID_FIELD",
            severity=IssueSeverity.FATAL,
            safe_summary="record 7 has an invalid source field type",
            byte_offset=18,
            line_number=3,
            record_number=7,
        )
        self.assertEqual(issue.record_number, 7)
        for forbidden in ("source", "target", "speaker", "body"):
            self.assertFalse(hasattr(issue, forbidden))
        with self.assertRaises(ValueError):
            ParseIssue(
                code="PARSER.SYNTAX.INVALID_FIELD",
                severity=IssueSeverity.FATAL,
                safe_summary="line one\nsecret body",
            )
        with self.assertRaises(ValueError):
            ParseIssue(
                code="PARSER.SYNTAX.INVALID_FIELD",
                severity=IssueSeverity.FATAL,
                safe_summary="invalid",
                line_number=0,
            )
        for separator in ("\x7f", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=ascii(separator)), self.assertRaises(ValueError):
                ParseIssue(
                    code="PARSER.SYNTAX.INVALID_FIELD",
                    severity=IssueSeverity.FATAL,
                    safe_summary=f"safe prefix{separator}unsafe continuation",
                )

    def test_issue_counts_are_unique_sorted_allowed_and_match_retained_issues(self) -> None:
        from parser_contracts import (
            FormatId,
            IssueCount,
            IssueSeverity,
            ParseIssue,
            ValidationOutcome,
            ValidationReport,
        )

        warning = ParseIssue(
            code="PARSER.SYNTAX.INVALID_FIELD",
            severity=IssueSeverity.WARNING,
            safe_summary="record 3 was rejected by the selected format rule",
            record_number=3,
        )
        report = ValidationReport(
            outcome=ValidationOutcome.FAILED,
            source=self._snapshot(),
            format_id=FormatId("line-text-v1"),
            codec_identity=self._identity(),
            observed_capabilities=self._capabilities(),
            limit_profile=self._profile(),
            provisional_record_count=2,
            issue_counts=(
                IssueCount(
                    code="PARSER.LIMIT.FIELD",
                    severity=IssueSeverity.FATAL,
                    count=1,
                ),
                IssueCount(
                    code="PARSER.SYNTAX.INVALID_FIELD",
                    severity=IssueSeverity.WARNING,
                    count=2,
                ),
            ),
            issues=(
                ParseIssue(
                    code="PARSER.LIMIT.FIELD",
                    severity=IssueSeverity.FATAL,
                    safe_summary="decoded field exceeds the active profile",
                    record_number=4,
                ),
                warning,
                warning,
            ),
            issues_truncated=False,
            terminal=None,
        )
        self.assertEqual(sum(item.count for item in report.issue_counts), 3)

        with self.assertRaises(ValueError):
            ValidationReport(
                outcome=ValidationOutcome.FAILED,
                source=self._snapshot(),
                format_id=FormatId("line-text-v1"),
                codec_identity=self._identity(),
                observed_capabilities=self._capabilities(),
                limit_profile=self._profile(),
                provisional_record_count=0,
                issue_counts=(
                    IssueCount(
                        code="PARSER.UNKNOWN.CODE",
                        severity=IssueSeverity.FATAL,
                        count=1,
                    ),
                ),
                issues=(),
                issues_truncated=True,
                terminal=None,
            )

    def test_terminal_is_foundation_issued_once_shape_with_zero_fatals(self) -> None:
        from parser_contracts import (
            IssueCount,
            IssueSeverity,
            TerminalSuccess,
            _issue_terminal_success,
        )

        with self.assertRaises(TypeError):
            TerminalSuccess(  # type: ignore[call-arg]
                source=self._snapshot(),
                codec_identity=self._identity(),
                limit_profile=self._profile(),
                record_count=2,
                warning_counts=(),
                issues_truncated=False,
                fatal_count=0,
            )
        terminal = _issue_terminal_success(
            source=self._snapshot(),
            codec_identity=self._identity(),
            limit_profile=self._profile(),
            record_count=2,
            warning_counts=(
                IssueCount(
                    code="PARSER.SYNTAX.INVALID_FIELD",
                    severity=IssueSeverity.WARNING,
                    count=1,
                ),
            ),
            issues_truncated=False,
        )
        self.assertEqual(terminal.fatal_count, 0)
        self.assertEqual(terminal.record_count, 2)
        with self.assertRaises(ValueError):
            _issue_terminal_success(
                source=self._snapshot(),
                codec_identity=self._identity(),
                limit_profile=self._profile(),
                record_count=2,
                warning_counts=(
                    IssueCount(
                        code="PARSER.LIMIT.FIELD",
                        severity=IssueSeverity.FATAL,
                        count=1,
                    ),
                ),
                issues_truncated=False,
            )
        with self.assertRaises(ValueError):
            _issue_terminal_success(
                source=self._snapshot(),
                codec_identity=self._identity(),
                limit_profile=self._profile(max_records=2, max_materialized_records=2),
                record_count=3,
                warning_counts=(),
                issues_truncated=False,
            )

    def test_validation_success_requires_exact_terminal_bindings(self) -> None:
        from parser_contracts import (
            FormatId,
            ValidationOutcome,
            ValidationReport,
            _issue_terminal_success,
        )

        profile = self._profile()
        terminal = _issue_terminal_success(
            source=self._snapshot(),
            codec_identity=self._identity(),
            limit_profile=profile,
            record_count=2,
            warning_counts=(),
            issues_truncated=False,
        )
        report = ValidationReport(
            outcome=ValidationOutcome.SUCCESS,
            source=self._snapshot(),
            format_id=FormatId("line-text-v1"),
            codec_identity=self._identity(),
            observed_capabilities=self._capabilities(),
            limit_profile=profile,
            provisional_record_count=2,
            issue_counts=(),
            issues=(),
            issues_truncated=False,
            terminal=terminal,
        )
        self.assertIs(report.terminal, terminal)

        for outcome, supplied_terminal in (
            (ValidationOutcome.SUCCESS, None),
            (ValidationOutcome.FAILED, terminal),
            (ValidationOutcome.CANCELLED, terminal),
        ):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                ValidationReport(
                    outcome=outcome,
                    source=self._snapshot(),
                    format_id=FormatId("line-text-v1"),
                    codec_identity=self._identity(),
                    observed_capabilities=self._capabilities(),
                    limit_profile=profile,
                    provisional_record_count=2,
                    issue_counts=(),
                    issues=(),
                    issues_truncated=False,
                    terminal=supplied_terminal,
                )

    def test_validation_report_requires_validation_capability_and_bounded_count(self) -> None:
        from parser_contracts import (
            CodecCapabilities,
            FormatId,
            IssueCount,
            IssueSeverity,
            ParseIssue,
            ValidationOutcome,
            ValidationReport,
        )

        profile = self._profile(max_records=2, max_materialized_records=2)
        non_validatable = CodecCapabilities(
            readable=True,
            validatable=False,
            canonical_write=False,
            source_round_trip_write=False,
            streaming_input=True,
            iterator_view=True,
            materialized_view=True,
            format_profile="line-text-v1",
        )
        fatal = ParseIssue(
            code="PARSER.LIMIT.FIELD",
            severity=IssueSeverity.FATAL,
            safe_summary="decoded field exceeds the active profile",
        )
        common = {
            "source": self._snapshot(),
            "format_id": FormatId("line-text-v1"),
            "codec_identity": self._identity(),
            "limit_profile": profile,
            "issue_counts": (
                IssueCount(
                    code=fatal.code,
                    severity=fatal.severity,
                    count=1,
                ),
            ),
            "issues": (fatal,),
            "issues_truncated": False,
            "terminal": None,
        }
        from parser_contracts import _issue_terminal_success

        terminal = _issue_terminal_success(
            source=self._snapshot(),
            codec_identity=self._identity(),
            limit_profile=profile,
            record_count=1,
            warning_counts=(),
            issues_truncated=False,
        )
        with self.assertRaises(ValueError):
            ValidationReport(
                outcome=ValidationOutcome.SUCCESS,
                source=self._snapshot(),
                format_id=FormatId("line-text-v1"),
                codec_identity=self._identity(),
                observed_capabilities=non_validatable,
                limit_profile=profile,
                provisional_record_count=1,
                issue_counts=(),
                issues=(),
                issues_truncated=False,
                terminal=terminal,
            )
        with self.assertRaises(ValueError):
            ValidationReport(
                outcome=ValidationOutcome.FAILED,
                observed_capabilities=non_validatable,
                provisional_record_count=1,
                **common,
            )
        with self.assertRaises(ValueError):
            ValidationReport(
                outcome=ValidationOutcome.FAILED,
                observed_capabilities=self._capabilities(),
                provisional_record_count=3,
                **common,
            )


if __name__ == "__main__":
    unittest.main()
