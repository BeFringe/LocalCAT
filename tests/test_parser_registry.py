from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from parser_contracts import (
    CodecCapabilities,
    CanonicalSerializerCodec,
    CodecDescriptor,
    CodecIdentity,
    EffectivePurpose,
    FormatId,
    InputConsumptionPolicy,
    LimitProfile,
    LOCALCAT_JSON_V1,
    RawReaderCodec,
    SeekableInputPreflightCodec,
    SelectionFailure,
    SelectionHints,
    SelectionRequest,
)
from parser_registry import ParserRegistry, RegistryConfigurationError
from tests.parser_architecture_test_support import (
    SourceModule,
    build_parser_architecture_policy,
)


_ISSUES = (
    "PARSER.LIMIT.INPUT",
    "PARSER.SYNTAX.MALFORMED",
)


def _profile(name: str = "test-profile") -> LimitProfile:
    return LimitProfile(
        profile_id=name,
        profile_version=1,
        max_input_bytes=1024,
        max_decoded_field_chars=256,
        max_records=32,
        max_materialized_records=16,
        max_retained_issues=8,
        declared_issue_codes=_ISSUES,
        max_metadata_entries_per_container=8,
        max_metadata_decoded_chars_per_container=256,
        max_metadata_decoded_chars_total=1024,
        max_structure_depth=8,
    )


def _capabilities(
    *,
    readable: bool = True,
    canonical_write: bool = False,
    opaque_features: tuple[str, ...] = (),
    format_profile: str = "test-profile",
    termbase_column_preview: bool = False,
) -> CodecCapabilities:
    return CodecCapabilities(
        readable=readable,
        validatable=readable,
        canonical_write=canonical_write,
        source_round_trip_write=False,
        streaming_input=True,
        iterator_view=readable,
        materialized_view=readable,
        format_profile=format_profile,
        termbase_column_preview=termbase_column_preview,
        opaque_features=opaque_features,
    )


class _Reader:
    """Behavioral fake: deliberately does not inherit a Parser base class."""

    def __init__(self, descriptor: CodecDescriptor) -> None:
        self.descriptor = descriptor

    def iter_raw(self, source: object, request: object):
        del source, request
        return iter(())


class _Serializer:
    def __init__(self, descriptor: CodecDescriptor) -> None:
        self.descriptor = descriptor

    def serialize_canonical(self, request: object) -> object:
        return request


class _RaisingDescriptorReader:
    @property
    def descriptor(self) -> CodecDescriptor:
        raise RuntimeError("secret reader source body")

    def iter_raw(self, source: object, request: object):
        del source, request
        return iter(())


class _RaisingDescriptorSerializer:
    @property
    def descriptor(self) -> CodecDescriptor:
        raise RuntimeError("secret serializer target body")

    def serialize_canonical(self, request: object) -> object:
        return request


class _PreflightReader(_Reader):
    def __init__(self, descriptor: CodecDescriptor) -> None:
        super().__init__(descriptor)
        self.preflight_calls: list[tuple[object, object]] = []

    def preflight_input(self, source: object, request: object) -> None:
        self.preflight_calls.append((source, request))


class _RaisingPreflightPropertyReader(_Reader):
    @property
    def preflight_input(self):
        raise RuntimeError("secret XLSX member body")


def _descriptor(
    name: str,
    *,
    purpose: EffectivePurpose = EffectivePurpose.PROJECT_DOCUMENT,
    format_id: FormatId | None = None,
    provider_id: str = "test.provider",
    extensions: tuple[str, ...] = (".test",),
    mime_types: tuple[str, ...] = ("application/x-test",),
    sniff_prefixes: tuple[bytes, ...] = (),
    opaque_features: tuple[str, ...] = (),
    canonical_write: bool = False,
    termbase_column_preview: bool = False,
    input_consumption_policy: InputConsumptionPolicy = (
        InputConsumptionPolicy.SEALED_BYTES_EOF
    ),
) -> CodecDescriptor:
    holder: dict[str, CodecDescriptor] = {}

    def reader_factory() -> _Reader:
        return _Reader(holder["descriptor"])

    def serializer_factory() -> _Serializer:
        return _Serializer(holder["descriptor"])

    profile = _profile(f"{name}-limits")
    effective_capabilities = _capabilities(
        format_profile=profile.profile_id,
        opaque_features=opaque_features,
        canonical_write=canonical_write,
        termbase_column_preview=termbase_column_preview,
    )
    descriptor = CodecDescriptor(
        identity=CodecIdentity(provider_id, name, "1.0"),
        purpose=purpose,
        format_id=format_id or FormatId(name),
        extensions=extensions,
        mime_types=mime_types,
        sniff_prefixes=sniff_prefixes,
        capabilities=effective_capabilities,
        limit_profile=profile,
        input_consumption_policy=input_consumption_policy,
        reader_factory=reader_factory,
        canonical_serializer_factory=(serializer_factory if canonical_write else None),
    )
    holder["descriptor"] = descriptor
    return descriptor


class ParserRegistrySelectionTests(unittest.TestCase):
    def test_explicit_key_returns_the_frozen_descriptor(self) -> None:
        descriptor = _descriptor("project-a")
        registry = ParserRegistry((descriptor,))

        selected = registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                format_id=descriptor.format_id,
            )
        )

        self.assertIs(selected, descriptor)
        with self.assertRaises(AttributeError):
            registry.descriptors.append(descriptor)  # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            registry._descriptors = ()  # type: ignore[attr-defined]

    def test_hints_narrow_only_inside_the_declared_purpose(self) -> None:
        project = _descriptor(
            "project-json",
            extensions=(".json",),
            mime_types=("application/json",),
        )
        resource = _descriptor(
            "tm-json",
            purpose=EffectivePurpose.TRANSLATION_MEMORY,
            extensions=(".json",),
            mime_types=("application/json",),
        )
        registry = ParserRegistry((resource, project))

        selected = registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                hints=SelectionHints(extensions=(".JSON",)),
            )
        )

        self.assertIs(selected, project)

    def test_multiple_hint_kinds_intersect_instead_of_widening(self) -> None:
        json_codec = _descriptor(
            "json-a",
            extensions=(".json",),
            mime_types=("application/json",),
        )
        other_mime = _descriptor(
            "json-b",
            extensions=(".json",),
            mime_types=("application/x-other",),
        )
        registry = ParserRegistry((other_mime, json_codec))

        selected = registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                hints=SelectionHints(
                    extensions=(".json",),
                    mime_types=("application/json",),
                ),
            )
        )

        self.assertIs(selected, json_codec)

    def test_prefix_hint_narrows_literal_markers_but_keeps_markerless_codec(self) -> None:
        matching = _descriptor(
            "prefix-match",
            extensions=(),
            mime_types=(),
            sniff_prefixes=(b"<?xml",),
        )
        nonmatching = _descriptor(
            "prefix-no-match",
            extensions=(),
            mime_types=(),
            sniff_prefixes=(b"{" ,),
        )
        markerless = _descriptor(
            "prefix-markerless",
            extensions=(),
            mime_types=(),
        )
        narrowing_registry = ParserRegistry((nonmatching, matching))
        markerless_registry = ParserRegistry((nonmatching, markerless, matching))

        selected = narrowing_registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                hints=SelectionHints(prefix=b"<?xml version='1.0'?>"),
            )
        )
        retained_without_marker = markerless_registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                hints=SelectionHints(prefix=b"<?xml version='1.0'?>"),
            )
        )

        self.assertIs(selected, matching)
        self.assertIsInstance(retained_without_marker, SelectionFailure)
        self.assertEqual(
            retained_without_marker.code,
            "PARSER.SELECTION.AMBIGUOUS",
        )

    def test_ambiguous_and_unsupported_failures_are_body_safe_and_deterministic(self) -> None:
        first = _descriptor("ambiguous-z", extensions=(".same",))
        second = _descriptor("ambiguous-a", extensions=(".same",))
        request = SelectionRequest(
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            hints=SelectionHints(extensions=(".same",), prefix=b"secret source"),
        )

        forward = ParserRegistry((first, second)).select(request)
        reverse = ParserRegistry((second, first)).select(request)

        self.assertIsInstance(forward, SelectionFailure)
        self.assertEqual(forward, reverse)
        self.assertEqual(forward.code, "PARSER.SELECTION.AMBIGUOUS")
        self.assertNotIn("secret source", repr(forward))
        unsupported = ParserRegistry((first,)).select(
            SelectionRequest(
                purpose=EffectivePurpose.TERMBASE,
                format_id=first.format_id,
            )
        )
        self.assertIsInstance(unsupported, SelectionFailure)
        self.assertEqual(unsupported.code, "PARSER.SELECTION.UNSUPPORTED")

    def test_supported_combinations_are_sorted_and_failure_report_is_bounded(self) -> None:
        descriptors = tuple(
            _descriptor(f"custom-{index:03d}") for index in reversed(range(70))
        )
        registry = ParserRegistry(descriptors)

        failure = registry.select(
            SelectionRequest(
                purpose=EffectivePurpose.TERMBASE,
                format_id=FormatId("missing-format"),
            )
        )

        self.assertIsInstance(failure, SelectionFailure)
        self.assertEqual(failure.supported_combination_count, 70)
        self.assertEqual(len(failure.supported_combinations), 64)
        self.assertTrue(failure.supported_combinations_truncated)
        observed = tuple(
            (item.purpose.value, item.format_id.value)
            for item in failure.supported_combinations
        )
        self.assertEqual(observed, tuple(sorted(observed)))

    def test_registration_order_does_not_change_descriptor_or_supported_order(self) -> None:
        first = _descriptor("order-z")
        second = _descriptor("order-a")

        forward = ParserRegistry((first, second))
        reverse = ParserRegistry((second, first))

        self.assertEqual(forward.descriptors, reverse.descriptors)
        self.assertEqual(
            forward.supported_combinations,
            reverse.supported_combinations,
        )


class ParserRegistryRegistrationTests(unittest.TestCase):
    def assert_same_registration_failure(
        self,
        forward: tuple[CodecDescriptor, ...],
        reverse: tuple[CodecDescriptor, ...],
        code: str,
    ) -> None:
        failures: list[RegistryConfigurationError] = []
        for descriptors in (forward, reverse):
            with self.assertRaises(RegistryConfigurationError) as caught:
                ParserRegistry(descriptors)
            failures.append(caught.exception)
        self.assertEqual(failures[0].code, code)
        self.assertEqual(failures[0].safe_summary, failures[1].safe_summary)

    def test_duplicate_authority_is_rejected_without_last_writer_wins(self) -> None:
        first = _descriptor("duplicate")
        second = _descriptor("duplicate", provider_id="another.provider")
        self.assert_same_registration_failure(
            (first, second),
            (second, first),
            "PARSER.SELECTION.DUPLICATE_AUTHORITY",
        )

    def test_builtin_purpose_mismatch_is_rejected(self) -> None:
        descriptor = _descriptor("wrong-localcat")
        object.__setattr__(descriptor, "format_id", LOCALCAT_JSON_V1)
        object.__setattr__(
            descriptor,
            "purpose",
            EffectivePurpose.TRANSLATION_MEMORY,
        )
        with self.assertRaises(RegistryConfigurationError) as caught:
            ParserRegistry((descriptor,))
        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.PURPOSE_INCOMPATIBLE",
        )

    def test_capability_and_factory_mismatches_fail_closed(self) -> None:
        readable_without_factory = _descriptor("no-reader")
        object.__setattr__(readable_without_factory, "reader_factory", None)
        factory_without_readable = _descriptor("false-reader")
        false_reader_capabilities = replace(
            factory_without_readable.capabilities,
            readable=False,
            validatable=False,
            iterator_view=False,
            materialized_view=False,
        )
        object.__setattr__(
            factory_without_readable,
            "capabilities",
            false_reader_capabilities,
        )
        writer_without_factory = _descriptor("no-serializer")
        object.__setattr__(
            writer_without_factory,
            "capabilities",
            replace(writer_without_factory.capabilities, canonical_write=True),
        )
        factory_without_writer = _descriptor("false-serializer")
        object.__setattr__(
            factory_without_writer,
            "canonical_serializer_factory",
            lambda: _Serializer(factory_without_writer),
        )
        for descriptor in (
            readable_without_factory,
            factory_without_readable,
            writer_without_factory,
            factory_without_writer,
        ):
            with self.subTest(codec=descriptor.identity.codec_id):
                with self.assertRaises(RegistryConfigurationError) as caught:
                    ParserRegistry((descriptor,))
                self.assertEqual(
                    caught.exception.code,
                    "PARSER.SELECTION.CAPABILITY_MISMATCH",
                )

    def test_declared_termbase_preview_requires_structural_behavior(self) -> None:
        descriptor = _descriptor(
            "preview-missing",
            purpose=EffectivePurpose.TERMBASE,
            termbase_column_preview=True,
        )
        registry = ParserRegistry((descriptor,))

        with self.assertRaises(RegistryConfigurationError) as caught:
            registry.create_reader(descriptor)

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.FACTORY_MISMATCH")
        self.assertNotIn("secret", str(caught.exception))

    def test_factory_product_must_publish_the_selected_descriptor(self) -> None:
        descriptor = _descriptor("factory-product")
        foreign = _descriptor("foreign-product")
        invalid = replace(descriptor, reader_factory=lambda: _Reader(foreign))
        registry = ParserRegistry((invalid,))

        with self.assertRaises(RegistryConfigurationError) as caught:
            registry.create_reader(invalid)

        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.FACTORY_MISMATCH",
        )

    def test_factory_exception_is_body_safe_and_suppresses_original_context(self) -> None:
        descriptor = _descriptor("factory-raises")

        def explode() -> _Reader:
            raise RuntimeError("secret source body")

        object.__setattr__(descriptor, "reader_factory", explode)
        registry = ParserRegistry((descriptor,))

        with self.assertRaises(RegistryConfigurationError) as caught:
            registry.create_reader(descriptor)

        self.assertEqual(caught.exception.code, "PARSER.SELECTION.FACTORY_FAILED")
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_reader_and_serializer_descriptor_properties_cannot_leak(self) -> None:
        reader_descriptor = _descriptor("reader-property-raises")
        object.__setattr__(
            reader_descriptor,
            "reader_factory",
            _RaisingDescriptorReader,
        )
        serializer_descriptor = _descriptor(
            "serializer-property-raises",
            canonical_write=True,
        )
        object.__setattr__(
            serializer_descriptor,
            "canonical_serializer_factory",
            _RaisingDescriptorSerializer,
        )
        registry = ParserRegistry((reader_descriptor, serializer_descriptor))

        actions = (
            lambda: registry.create_reader(reader_descriptor),
            lambda: registry.create_canonical_serializer(serializer_descriptor),
        )
        for action in actions:
            with self.assertRaises(RegistryConfigurationError) as caught:
                action()
            self.assertEqual(
                caught.exception.code,
                "PARSER.SELECTION.FACTORY_MISMATCH",
            )
            self.assertNotIn("secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_foundation_adapters_pin_descriptor_after_delegate_mutation(self) -> None:
        reader_descriptor = _descriptor("mutable-reader")
        reader_holder: dict[str, _Reader] = {}

        def reader_factory() -> _Reader:
            delegate = _Reader(reader_descriptor)
            reader_holder["delegate"] = delegate
            return delegate

        object.__setattr__(reader_descriptor, "reader_factory", reader_factory)
        serializer_descriptor = _descriptor(
            "mutable-serializer",
            canonical_write=True,
        )
        serializer_holder: dict[str, _Serializer] = {}

        def serializer_factory() -> _Serializer:
            delegate = _Serializer(serializer_descriptor)
            serializer_holder["delegate"] = delegate
            return delegate

        object.__setattr__(
            serializer_descriptor,
            "canonical_serializer_factory",
            serializer_factory,
        )
        registry = ParserRegistry((reader_descriptor, serializer_descriptor))
        reader = registry.create_reader(reader_descriptor)
        serializer = registry.create_canonical_serializer(serializer_descriptor)
        foreign = _descriptor("foreign-after-create")

        reader_holder["delegate"].descriptor = foreign
        serializer_holder["delegate"].descriptor = foreign

        self.assertIsNot(reader, reader_holder["delegate"])
        self.assertIsNot(serializer, serializer_holder["delegate"])
        self.assertIs(reader.descriptor, reader_descriptor)
        self.assertIs(serializer.descriptor, serializer_descriptor)
        with self.assertRaises(AttributeError):
            reader.descriptor = foreign  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            serializer.descriptor = foreign  # type: ignore[misc]

    def test_xlsx_policy_requires_and_pins_seekable_preflight_behavior(self) -> None:
        missing = _descriptor(
            "xlsx-preflight-missing",
            input_consumption_policy=(
                InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
            ),
        )
        with self.assertRaises(RegistryConfigurationError) as caught:
            ParserRegistry((missing,)).create_reader(missing)
        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.FACTORY_MISMATCH",
        )

        descriptor = _descriptor(
            "xlsx-preflight-valid",
            input_consumption_policy=(
                InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
            ),
        )
        holder: dict[str, _PreflightReader] = {}

        def factory() -> _PreflightReader:
            delegate = _PreflightReader(descriptor)
            holder["delegate"] = delegate
            return delegate

        object.__setattr__(descriptor, "reader_factory", factory)
        reader = ParserRegistry((descriptor,)).create_reader(descriptor)
        source = object()
        request = object()
        reader.preflight_input(source, request)  # type: ignore[attr-defined]
        foreign = _descriptor("foreign-xlsx-reader")
        holder["delegate"].descriptor = foreign

        self.assertIsInstance(reader, SeekableInputPreflightCodec)
        self.assertIs(reader.descriptor, descriptor)
        self.assertEqual(
            holder["delegate"].preflight_calls,
            [(source, request)],
        )

    def test_xlsx_preflight_property_failure_is_body_safe_but_sequential_ignores_it(self) -> None:
        xlsx = _descriptor(
            "xlsx-preflight-property-raises",
            input_consumption_policy=(
                InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
            ),
        )
        object.__setattr__(
            xlsx,
            "reader_factory",
            lambda: _RaisingPreflightPropertyReader(xlsx),
        )
        with self.assertRaises(RegistryConfigurationError) as caught:
            ParserRegistry((xlsx,)).create_reader(xlsx)
        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.FACTORY_MISMATCH",
        )
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

        sequential = _descriptor("sequential-preflight-property-raises")
        object.__setattr__(
            sequential,
            "reader_factory",
            lambda: _RaisingPreflightPropertyReader(sequential),
        )
        reader = ParserRegistry((sequential,)).create_reader(sequential)
        self.assertIsInstance(reader, RawReaderCodec)
        self.assertNotIsInstance(reader, SeekableInputPreflightCodec)

    def test_behavioral_fake_codec_requires_no_baseparser_inheritance(self) -> None:
        descriptor = _descriptor("behavior-only")
        registry = ParserRegistry((descriptor,))

        reader = registry.create_reader(descriptor)

        self.assertIsInstance(reader, RawReaderCodec)
        self.assertTrue(callable(reader.iter_raw))

    def test_canonical_serializer_is_capability_gated_and_behavioral(self) -> None:
        writable = _descriptor("canonical-writer", canonical_write=True)
        reader_only = _descriptor("reader-only")
        registry = ParserRegistry((reader_only, writable))

        serializer = registry.create_canonical_serializer(writable)

        self.assertIsInstance(serializer, CanonicalSerializerCodec)
        self.assertIs(serializer.descriptor, writable)
        with self.assertRaises(RegistryConfigurationError) as caught:
            registry.create_canonical_serializer(reader_only)
        self.assertEqual(
            caught.exception.code,
            "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
        )


class ParserRegistryArchitectureTests(unittest.TestCase):
    def test_real_registry_depends_only_on_contracts_and_stdlib(self) -> None:
        path = Path(__file__).parents[1] / "parser_registry.py"
        module = SourceModule("parser_registry", path.read_text(encoding="utf-8"))
        violations = build_parser_architecture_policy().check_module(module)
        self.assertEqual(violations, ())


if __name__ == "__main__":
    unittest.main()
