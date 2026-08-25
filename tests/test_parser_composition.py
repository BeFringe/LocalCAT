from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from parser_composition import (
    OpenedParserInput,
    ParserApplicationSurface,
    PreparedCanonicalWrite,
    ProviderBinding,
    ProviderConfigurationError,
    compose_registry,
    create_builtin_registry,
    create_parser_application_surface,
)
from parser_contracts import (
    BUILTIN_FORMAT_IDS,
    CanonicalDocumentWrite,
    CanonicalSegmentWrite,
    CanonicalSerializeRequest,
    CodecDescriptor,
    CodecProvider,
    ContractViolation,
    EffectivePurpose,
    FormatId,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    NORMALIZED_TM_JSON_V1,
    RawSpeaker,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TERMBASE_CSV_V1,
    TERMBASE_XLSX_V1,
    TermbaseColumnPreviewRequest,
    TMX_LEVEL1_V1,
    TargetReference,
    builtin_purpose_for_format,
)
from parser_source import ParserSourceError
from tests.parser_architecture_test_support import (
    PARSER_CODEC_PREFIXES,
    SourceModule,
    build_parser_architecture_policy,
    collect_import_references,
)
from tests.test_parser_registry import _descriptor


class _Provider:
    """Structural provider fake with no CodecProvider/BaseParser inheritance."""

    def __init__(
        self,
        provider_id: str,
        provider_version: str,
        descriptors: tuple[object, ...],
    ) -> None:
        self.provider_id = provider_id
        self.provider_version = provider_version
        self._descriptors = descriptors
        self.private_sidecar = object()

    def descriptors(self) -> tuple[object, ...]:
        return self._descriptors


class _RaisingProvider(_Provider):
    def descriptors(self) -> tuple[object, ...]:
        raise RuntimeError("source and secret must not escape")


class _ExplodingSerializer:
    def __init__(self, descriptor: CodecDescriptor) -> None:
        self.descriptor = descriptor

    def serialize_canonical(self, _request: object) -> object:
        raise RuntimeError("secret target and document body must not escape")


class ParserCompositionTests(unittest.TestCase):
    def test_empty_seam_rejects_direct_external_descriptor_injection(self) -> None:
        empty = compose_registry()
        self.assertEqual(empty.descriptors, ())

        descriptor = _descriptor("direct")
        with self.assertRaises(ProviderConfigurationError) as caught:
            compose_registry(descriptors=(descriptor,))
        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
        )

    def test_version_rejected_plugin_cannot_bypass_binding_with_direct_descriptor(self) -> None:
        descriptor = _descriptor(
            "gated-plugin",
            provider_id="plugin.gated",
        )
        provider = _Provider("plugin.gated", "2", (descriptor,))
        binding = ProviderBinding(
            provider_id="plugin.gated",
            provider=provider,
            enabled=True,
            compatible_versions=("1",),
        )

        with self.assertRaises(ProviderConfigurationError) as gated:
            compose_registry(providers=(binding,))
        with self.assertRaises(ProviderConfigurationError) as bypass:
            compose_registry(
                providers=(binding,),
                descriptors=(descriptor,),
            )

        self.assertEqual(
            gated.exception.code,
            "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
        )
        self.assertEqual(
            bypass.exception.code,
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
        )

    def test_structural_provider_registers_opaque_capability_without_interpretation(self) -> None:
        descriptor = _descriptor(
            "opaque-plugin",
            provider_id="plugin.rpy",
            opaque_features=("opaque-round-trip-token",),
        )
        provider = _Provider("plugin.rpy", "7", (descriptor,))
        binding = ProviderBinding(
            provider_id="plugin.rpy",
            provider=provider,
            enabled=True,
            compatible_versions=("7",),
        )

        registry = compose_registry(providers=(binding,))
        reader = registry.create_reader(descriptor)

        self.assertNotIn(CodecProvider, type(provider).__mro__)
        self.assertIsInstance(provider, CodecProvider)
        self.assertIs(reader.descriptor, descriptor)
        self.assertEqual(
            reader.descriptor.capabilities.opaque_features,
            ("opaque-round-trip-token",),
        )
        self.assertIsNotNone(provider.private_sidecar)

    def test_missing_disabled_and_version_incompatible_are_structured(self) -> None:
        provider = _Provider("plugin.test", "2", ())
        cases = (
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=None,
                    enabled=True,
                    compatible_versions=("2",),
                ),
                "PARSER.SELECTION.PROVIDER_MISSING",
            ),
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=provider,
                    enabled=False,
                    compatible_versions=("2",),
                ),
                "PARSER.SELECTION.PROVIDER_DISABLED",
            ),
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=provider,
                    enabled=True,
                    compatible_versions=("1",),
                ),
                "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
            ),
        )
        for binding, expected_code in cases:
            with self.subTest(code=expected_code):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    compose_registry(providers=(binding,))
                self.assertEqual(caught.exception.code, expected_code)

    def test_provider_identity_and_descriptor_authority_must_agree(self) -> None:
        wrong_provider = _Provider("actual.provider", "1", ())
        wrong_descriptor = _descriptor(
            "wrong-provider-descriptor",
            provider_id="another.provider",
        )
        cases = (
            ProviderBinding(
                provider_id="configured.provider",
                provider=wrong_provider,
                enabled=True,
                compatible_versions=("1",),
            ),
            ProviderBinding(
                provider_id="actual.provider",
                provider=_Provider("actual.provider", "1", (wrong_descriptor,)),
                enabled=True,
                compatible_versions=("1",),
            ),
        )
        for binding in cases:
            with self.subTest(binding=binding.provider_id):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    compose_registry(providers=(binding,))
                self.assertEqual(
                    caught.exception.code,
                    "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
                )

    def test_provider_exception_is_wrapped_without_leaking_message(self) -> None:
        provider = _RaisingProvider("plugin.raise", "1", ())
        binding = ProviderBinding(
            provider_id="plugin.raise",
            provider=provider,
            enabled=True,
            compatible_versions=("1",),
        )

        with self.assertRaises(ProviderConfigurationError) as caught:
            compose_registry(providers=(binding,))

        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.PROVIDER_FAILED",
        )
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("source", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_provider_order_does_not_change_failure(self) -> None:
        first = ProviderBinding(
            provider_id="plugin.same",
            provider=_Provider("plugin.same", "1", ()),
            enabled=True,
            compatible_versions=("1",),
        )
        second = ProviderBinding(
            provider_id="plugin.same",
            provider=_Provider("plugin.same", "1", ()),
            enabled=True,
            compatible_versions=("1",),
        )
        failures: list[ProviderConfigurationError] = []
        for bindings in ((first, second), (second, first)):
            with self.assertRaises(ProviderConfigurationError) as caught:
                compose_registry(providers=bindings)
            failures.append(caught.exception)
        self.assertEqual(
            failures[0].code,
            "PARSER.SELECTION.PROVIDER_DUPLICATE",
        )
        self.assertEqual(failures[0].safe_summary, failures[1].safe_summary)


class BuiltinCompositionTests(unittest.TestCase):
    def test_builtin_registry_has_the_exact_eight_purpose_format_authorities(self) -> None:
        registry = create_builtin_registry()

        self.assertEqual(len(registry.descriptors), 8)
        self.assertEqual(
            {
                (descriptor.purpose, descriptor.format_id)
                for descriptor in registry.descriptors
            },
            {
                (builtin_purpose_for_format(format_id), format_id)
                for format_id in BUILTIN_FORMAT_IDS
            },
        )
        for descriptor in registry.descriptors:
            with self.subTest(format_id=descriptor.format_id.value):
                self.assertIs(type(descriptor), CodecDescriptor)
                self.assertEqual(
                    descriptor.capabilities.format_profile,
                    descriptor.limit_profile.profile_id,
                )
                self.assertTrue(descriptor.limit_profile.declared_issue_codes)

        expected_capabilities = {
            LOCALCAT_JSON_V1: (False, True),
            LINE_TEXT_V1: (True, False),
            GETTEXT_PO_V1: (True, False),
            GETTEXT_POT_V1: (True, False),
            TMX_LEVEL1_V1: (True, False),
            NORMALIZED_TM_JSON_V1: (False, False),
            TERMBASE_CSV_V1: (True, False),
            TERMBASE_XLSX_V1: (True, False),
        }
        self.assertEqual(
            {
                descriptor.format_id: (
                    descriptor.capabilities.streaming_input,
                    descriptor.capabilities.canonical_write,
                )
                for descriptor in registry.descriptors
            },
            expected_capabilities,
        )
        self.assertTrue(
            all(
                descriptor.capabilities.readable
                and descriptor.capabilities.validatable
                and descriptor.capabilities.iterator_view
                and descriptor.capabilities.materialized_view
                and not descriptor.capabilities.source_round_trip_write
                for descriptor in registry.descriptors
            )
        )

    def test_builtin_registry_is_order_independent_and_rejects_wrong_purpose(self) -> None:
        registry = create_builtin_registry()
        wrong = registry.select(
            SelectionRequest(
                EffectivePurpose.TRANSLATION_MEMORY,
                format_id=LOCALCAT_JSON_V1,
            )
        )

        self.assertIs(type(wrong), SelectionFailure)
        self.assertEqual(wrong.code, "PARSER.SELECTION.UNSUPPORTED")
        self.assertEqual(
            tuple(
                (item.purpose.value, item.format_id.value)
                for item in registry.supported_combinations
            ),
            tuple(
                sorted(
                    (
                        descriptor.purpose.value,
                        descriptor.format_id.value,
                    )
                    for descriptor in registry.descriptors
                )
            ),
        )

    def test_provider_cannot_duplicate_a_builtin_authority(self) -> None:
        duplicate = _descriptor(
            "duplicate-localcat",
            format_id=LOCALCAT_JSON_V1,
            provider_id="plugin.duplicate",
        )
        binding = ProviderBinding(
            provider_id="plugin.duplicate",
            provider=_Provider("plugin.duplicate", "1", (duplicate,)),
            enabled=True,
            compatible_versions=("1",),
        )

        with self.assertRaises(ContractViolation) as caught:
            create_builtin_registry(providers=(binding,))

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.DUPLICATE_AUTHORITY")


class ParserApplicationSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="parser-composition-test-")
        self.root = Path(self._temporary.name)
        self.surface = create_parser_application_surface()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _source_reference(self, name: str, payload: bytes) -> SourceReference:
        source = self.root / name
        source.write_bytes(payload)
        return SourceReference(str(self.root), str(source), name)

    def _target_reference(self, name: str) -> TargetReference:
        return TargetReference(str(self.root), str(self.root / name), name)

    @staticmethod
    def _surface_with_provider_descriptor(
        descriptor: CodecDescriptor,
    ) -> ParserApplicationSurface:
        provider_id = descriptor.identity.provider_id
        binding = ProviderBinding(
            provider_id=provider_id,
            provider=_Provider(provider_id, "1", (descriptor,)),
            enabled=True,
            compatible_versions=("1",),
        )
        return create_parser_application_surface(providers=(binding,))

    @staticmethod
    def _canonical_request(format_id: FormatId = LOCALCAT_JSON_V1) -> CanonicalSerializeRequest:
        return CanonicalSerializeRequest(
            format_id=format_id,
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
                ),
            ),
        )

    def _open_text(self, payload: bytes = b"one\ntwo\n") -> OpenedParserInput:
        opened = self.surface.open_input(
            self._source_reference("chapter.txt", payload),
            SelectionRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                format_id=LINE_TEXT_V1,
            ),
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LINE_TEXT_V1),
        )
        self.assertIs(type(opened), OpenedParserInput)
        return opened

    def test_factory_returns_the_single_application_facing_surface(self) -> None:
        self.assertIs(type(self.surface), ParserApplicationSurface)
        selected = self.surface.select(
            SelectionRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                format_id=LOCALCAT_JSON_V1,
            )
        )
        self.assertIs(type(selected), CodecDescriptor)
        self.assertEqual(selected.format_id, LOCALCAT_JSON_V1)
        self.assertFalse(hasattr(self.surface, "registry"))

    def test_public_surface_and_opened_input_constructors_cannot_bypass_composition(self) -> None:
        registry = create_builtin_registry()
        with self.assertRaises(ContractViolation) as surface_failure:
            ParserApplicationSurface(registry)
        self.assertEqual(
            surface_failure.exception.code,
            "PARSER.SELECTION.COMPOSITION_REQUIRED",
        )

        opened = self._open_text(b"only\n")
        try:
            with self.assertRaises(ContractViolation) as input_failure:
                OpenedParserInput(
                    opened._registry,
                    opened.descriptor,
                    opened._snapshot,
                    opened._request,
                    None,
                    None,
                )
            self.assertEqual(
                input_failure.exception.code,
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
            )
        finally:
            opened.close()

        with self.assertRaises(ContractViolation) as prepared_failure:
            PreparedCanonicalWrite(b"not-authorized")
        self.assertEqual(
            prepared_failure.exception.code,
            "PARSER.SELECTION.COMPOSITION_REQUIRED",
        )

    def test_composition_does_not_reexport_registry_or_source_constructors(self) -> None:
        import parser_composition

        for name in (
            "ParserRegistry",
            "CanonicalBytes",
            "GuardedParseSession",
            "SealedSourceSnapshot",
            "create_sealed_snapshot",
            "atomic_write_bytes",
            "materialize",
            "validate",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(parser_composition, name))

    def test_opened_input_preserves_identity_name_and_foundation_terminal_stream(self) -> None:
        payload = b"one\ntwo\n"
        opened = self._open_text(payload)

        with mock.patch("parser_composition._materialize") as forbidden_materialize:
            session = opened.stream()
            events = tuple(session)
            terminal = session.verified_terminal()
            session.close()

        self.assertFalse(forbidden_materialize.called)
        self.assertEqual(opened.source_name_hint, "chapter.txt")
        self.assertEqual(opened.source_identity.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(terminal.source, opened.source_identity)
        self.assertEqual(terminal.record_count, 2)
        self.assertEqual(len(events), 3)
        opened.close()

        with self.assertRaises(ParserSourceError) as caught:
            opened.stream()
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.SNAPSHOT_RELEASED")

    def test_validate_and_materialize_reuse_one_snapshot_without_reimplementing_grammar(self) -> None:
        with self._open_text() as opened:
            report = opened.validate()
            materialized = opened.materialize()

        self.assertIsNotNone(report.terminal)
        self.assertEqual(report.source, opened.source_identity)
        self.assertEqual(materialized.terminal.source, opened.source_identity)
        self.assertEqual(len(materialized.records), 2)

    def test_termbase_preview_is_codec_owned_and_snapshot_bound(self) -> None:
        payload = b"Source,Target,Notes\nAlpha,Beta,ignored\n"
        preview = self.surface.preview_termbase_columns(
            self._source_reference("terms.csv", payload),
            SelectionRequest(
                EffectivePurpose.TERMBASE,
                format_id=TERMBASE_CSV_V1,
            ),
            TermbaseColumnPreviewRequest(
                EffectivePurpose.TERMBASE,
                TERMBASE_CSV_V1,
            ),
        )

        self.assertEqual(preview.source.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(preview.format_id, TERMBASE_CSV_V1)
        self.assertEqual(
            tuple(column.header_candidate for column in preview.columns),
            ("Source", "Target", "Notes"),
        )
        self.assertTrue(preview.legacy_header_detected)

    def test_preview_capability_and_request_mismatch_fail_before_source_open(self) -> None:
        missing = SourceReference(
            str(self.root),
            str(self.root / "missing.json"),
            "missing.json",
        )
        with self.assertRaises(ContractViolation) as unsupported:
            self.surface.preview_termbase_columns(
                missing,
                SelectionRequest(
                    EffectivePurpose.PROJECT_DOCUMENT,
                    format_id=LOCALCAT_JSON_V1,
                ),
                TermbaseColumnPreviewRequest(
                    EffectivePurpose.TERMBASE,
                    TERMBASE_CSV_V1,
                ),
            )
        self.assertEqual(unsupported.exception.code, "PARSER.SELECTION.UNSUPPORTED")
        self.assertFalse((self.root / "missing.json").exists())

        plugin_format = FormatId("plugin-termbase")
        plugin_descriptor = _descriptor(
            "plugin-termbase",
            purpose=EffectivePurpose.TERMBASE,
            format_id=plugin_format,
            provider_id="plugin.termbase",
            extensions=(".terms",),
        )
        plugin_surface = create_parser_application_surface(
            providers=(
                ProviderBinding(
                    provider_id="plugin.termbase",
                    provider=_Provider(
                        "plugin.termbase",
                        "1",
                        (plugin_descriptor,),
                    ),
                    enabled=True,
                    compatible_versions=("1",),
                ),
            )
        )
        missing_plugin = SourceReference(
            str(self.root),
            str(self.root / "missing.terms"),
            "missing.terms",
        )
        with self.assertRaises(ContractViolation) as no_preview:
            plugin_surface.preview_termbase_columns(
                missing_plugin,
                SelectionRequest(
                    EffectivePurpose.TERMBASE,
                    format_id=plugin_format,
                ),
                TermbaseColumnPreviewRequest(
                    EffectivePurpose.TERMBASE,
                    plugin_format,
                ),
            )
        self.assertEqual(
            no_preview.exception.code,
            "PARSER.CAPABILITY.PREVIEW_UNSUPPORTED",
        )
        self.assertFalse((self.root / "missing.terms").exists())

    def test_selection_failure_occurs_before_source_snapshot_open(self) -> None:
        reference = SourceReference(
            str(self.root),
            str(self.root / "missing.json"),
            "missing.json",
        )

        result = self.surface.open_input(
            reference,
            SelectionRequest(
                EffectivePurpose.TRANSLATION_MEMORY,
                format_id=LOCALCAT_JSON_V1,
            ),
            ReadRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                LOCALCAT_JSON_V1,
            ),
        )

        self.assertIs(type(result), SelectionFailure)
        self.assertEqual(result.code, "PARSER.SELECTION.UNSUPPORTED")

    def test_read_request_mismatch_occurs_before_source_snapshot_open(self) -> None:
        reference = SourceReference(
            str(self.root),
            str(self.root / "missing.json"),
            "missing.json",
        )

        with self.assertRaises(ContractViolation) as caught:
            self.surface.open_input(
                reference,
                SelectionRequest(
                    EffectivePurpose.PROJECT_DOCUMENT,
                    format_id=LOCALCAT_JSON_V1,
                ),
                ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LINE_TEXT_V1),
            )

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.UNSUPPORTED")
        self.assertNotIn("missing", str(caught.exception))

    def test_opened_close_defers_snapshot_release_until_active_session_closes(self) -> None:
        opened = self._open_text(b"only\n")
        session = opened.stream()

        opened.close()
        events = tuple(session)
        terminal = session.verified_terminal()
        self.assertEqual(terminal.record_count, 1)
        self.assertEqual(len(events), 2)
        self.assertFalse(session.source.closed)
        session.close()
        self.assertTrue(session.source.closed)

        with self.assertRaises(ContractViolation) as caught:
            opened.stream()
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.SNAPSHOT_RELEASED")

    def test_canonical_write_returns_foundation_receipt_and_preserves_bytes(self) -> None:
        target = self._target_reference("project.json")

        receipt = self.surface.write_canonical(
            EffectivePurpose.PROJECT_DOCUMENT,
            self._canonical_request(),
            target,
        )
        payload = (self.root / "project.json").read_bytes()

        self.assertEqual(receipt.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(receipt.byte_count, len(payload))
        self.assertIn("你好".encode(), payload)

    def test_prepare_canonical_is_opaque_and_does_not_touch_missing_parent(self) -> None:
        parent = self.root / "not-created-by-prepare"
        target = parent / "project.json"

        prepared = self.surface.prepare_canonical(
            EffectivePurpose.PROJECT_DOCUMENT,
            self._canonical_request(),
        )

        self.assertIs(type(prepared), PreparedCanonicalWrite)
        self.assertFalse(parent.exists())
        for name in ("payload", "canonical_bytes", "descriptor", "registry"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(prepared, name))
        with self.assertRaises(AttributeError):
            prepared._payload = b"mutated"  # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            prepared._PreparedCanonicalWrite__payload = b"mutated"

        parent.mkdir()
        receipt = prepared.write(
            TargetReference(str(parent), str(target), target.name)
        )
        payload = target.read_bytes()
        self.assertEqual(receipt.content_sha256, hashlib.sha256(payload).hexdigest())

    def test_unsupported_writer_fails_before_target_is_opened(self) -> None:
        parent = self.root / "unsupported-parent"

        with self.assertRaises(ContractViolation) as caught:
            self.surface.prepare_canonical(
                EffectivePurpose.PROJECT_DOCUMENT,
                self._canonical_request(LINE_TEXT_V1),
            )

        self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.WRITE_UNSUPPORTED")
        self.assertFalse(parent.exists())

    def test_serializer_output_mismatch_fails_before_target_is_opened(self) -> None:
        descriptor = _descriptor(
            "bad-output",
            canonical_write=True,
            provider_id="plugin.bad-output",
        )
        surface = self._surface_with_provider_descriptor(descriptor)
        parent = self.root / "bad-output-parent"

        with self.assertRaises(ContractViolation) as caught:
            surface.prepare_canonical(
                EffectivePurpose.PROJECT_DOCUMENT,
                self._canonical_request(descriptor.format_id),
            )

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.FACTORY_MISMATCH")
        self.assertFalse(parent.exists())

    def test_serializer_exception_is_body_safe_and_does_not_touch_target(self) -> None:
        original = _descriptor(
            "exploding",
            canonical_write=True,
            provider_id="plugin.exploding",
        )
        holder: dict[str, CodecDescriptor] = {}

        def serializer_factory() -> _ExplodingSerializer:
            return _ExplodingSerializer(holder["descriptor"])

        descriptor = replace(
            original,
            canonical_serializer_factory=serializer_factory,
        )
        holder["descriptor"] = descriptor
        surface = self._surface_with_provider_descriptor(descriptor)
        parent = self.root / "secret-parent"

        with self.assertRaises(ContractViolation) as caught:
            surface.prepare_canonical(
                EffectivePurpose.PROJECT_DOCUMENT,
                self._canonical_request(descriptor.format_id),
            )

        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_FAILED")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("document", str(caught.exception))
        self.assertFalse(parent.exists())

    def test_serializer_factory_failure_via_provider_occurs_during_prepare(self) -> None:
        original = _descriptor(
            "factory-failure",
            canonical_write=True,
            provider_id="plugin.factory-failure",
        )

        def serializer_factory():
            raise RuntimeError("secret target body must not escape")

        descriptor = replace(
            original,
            canonical_serializer_factory=serializer_factory,
        )
        surface = self._surface_with_provider_descriptor(descriptor)
        parent = self.root / "factory-failure-parent"

        with self.assertRaises(ContractViolation) as caught:
            surface.prepare_canonical(
                EffectivePurpose.PROJECT_DOCUMENT,
                self._canonical_request(descriptor.format_id),
            )

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.FACTORY_FAILED")
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(parent.exists())

    def test_reader_factory_exception_via_provider_is_body_safe_before_source_open(self) -> None:
        original = _descriptor(
            "exploding-reader",
            provider_id="plugin.exploding-reader",
        )

        def reader_factory():
            raise RuntimeError("secret source body must not escape")

        descriptor = replace(original, reader_factory=reader_factory)
        surface = self._surface_with_provider_descriptor(descriptor)
        missing = self.root / "secret-source.test"

        with self.assertRaises(ContractViolation) as caught:
            surface.open_input(
                SourceReference(str(self.root), str(missing), missing.name),
                SelectionRequest(
                    EffectivePurpose.PROJECT_DOCUMENT,
                    format_id=descriptor.format_id,
                ),
                ReadRequest(
                    EffectivePurpose.PROJECT_DOCUMENT,
                    descriptor.format_id,
                ),
            )

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.FACTORY_FAILED")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("source body", str(caught.exception))


class ParserCompositionArchitectureTests(unittest.TestCase):
    def test_real_composition_is_the_only_codec_capable_seam(self) -> None:
        root = Path(__file__).parents[1]
        composition = SourceModule(
            "parser_composition",
            (root / "parser_composition.py").read_text(encoding="utf-8"),
        )
        registry = SourceModule(
            "parser_registry",
            (root / "parser_registry.py").read_text(encoding="utf-8"),
        )
        policy = build_parser_architecture_policy()

        self.assertEqual(policy.check_module(composition), ())
        self.assertEqual(policy.check_module(registry), ())
        self.assertNotIn("importlib", composition.source)
        self.assertNotIn("entry_points", composition.source)
        imported = {
            reference.target
            for reference in collect_import_references(
                composition.source,
                module_name="parser_composition",
            )
        }
        self.assertEqual(
            {
                prefix
                for prefix in PARSER_CODEC_PREFIXES
                if any(
                    target == prefix or target.startswith(prefix + ".")
                    for target in imported
                )
            },
            set(PARSER_CODEC_PREFIXES),
        )
        self.assertTrue(
            any(
                target == "parser_source" or target.startswith("parser_source.")
                for target in imported
            )
        )


if __name__ == "__main__":
    unittest.main()
