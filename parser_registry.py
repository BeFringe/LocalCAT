"""Purpose-aware immutable registry for neutral Parser codec descriptors."""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Iterator

from parser_contracts import (
    MAX_RETAINED_SUPPORTED_COMBINATIONS,
    CanonicalBytes,
    CanonicalSerializeRequest,
    CanonicalSerializerCodec,
    CodecDescriptor,
    ContractViolation,
    EffectivePurpose,
    FormatId,
    InputConsumptionPolicy,
    RawParseEvent,
    RawReaderCodec,
    ReadRequest,
    SelectionFailure,
    SelectionHintSummary,
    SelectionRequest,
    SeekableInputPreflightCodec,
    SnapshotCursorLease,
    SupportedCombination,
    TermbaseColumnPreview,
    TermbaseColumnPreviewCodec,
    TermbaseColumnPreviewRequest,
    builtin_purpose_for_format,
)


class RegistryConfigurationError(ContractViolation):
    """Deterministic, body-safe descriptor or factory configuration failure."""


class _PinnedRawReader:
    """Foundation adapter that never re-reads a delegate's mutable descriptor."""

    __slots__ = ("_descriptor", "_iter_raw")

    def __init__(
        self,
        descriptor: CodecDescriptor,
        iter_raw: Callable[
            [SnapshotCursorLease, ReadRequest],
            Iterator[RawParseEvent],
        ],
    ) -> None:
        object.__setattr__(self, "_descriptor", descriptor)
        object.__setattr__(self, "_iter_raw", iter_raw)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Foundation reader adapter is immutable")

    @property
    def descriptor(self) -> CodecDescriptor:
        return self._descriptor

    def iter_raw(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> Iterator[RawParseEvent]:
        return self._iter_raw(source, request)


class _PinnedCanonicalSerializer:
    """Foundation adapter pinning serializer authority at factory validation time."""

    __slots__ = ("_descriptor", "_serialize_canonical")

    def __init__(
        self,
        descriptor: CodecDescriptor,
        serialize_canonical: Callable[[CanonicalSerializeRequest], CanonicalBytes],
    ) -> None:
        object.__setattr__(self, "_descriptor", descriptor)
        object.__setattr__(self, "_serialize_canonical", serialize_canonical)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Foundation serializer adapter is immutable")

    @property
    def descriptor(self) -> CodecDescriptor:
        return self._descriptor

    def serialize_canonical(
        self,
        request: CanonicalSerializeRequest,
    ) -> CanonicalBytes:
        return self._serialize_canonical(request)


class _PinnedSeekablePreflightReader(_PinnedRawReader):
    """Pinned reader carrying the XLSX policy-specific preflight behavior."""

    __slots__ = ("_preflight_input",)

    def __init__(
        self,
        descriptor: CodecDescriptor,
        iter_raw: Callable[
            [SnapshotCursorLease, ReadRequest],
            Iterator[RawParseEvent],
        ],
        preflight_input: Callable[[SnapshotCursorLease, ReadRequest], None],
    ) -> None:
        super().__init__(descriptor, iter_raw)
        object.__setattr__(self, "_preflight_input", preflight_input)

    def preflight_input(
        self,
        source: SnapshotCursorLease,
        request: ReadRequest,
    ) -> None:
        self._preflight_input(source, request)


class _PinnedTermbasePreviewReader(_PinnedRawReader):
    """Pinned reader carrying the selected codec's bounded preview behavior."""

    __slots__ = ("_preview_columns",)

    def __init__(
        self,
        descriptor: CodecDescriptor,
        iter_raw: Callable[[SnapshotCursorLease, ReadRequest], Iterator[RawParseEvent]],
        preview_columns: Callable[
            [SnapshotCursorLease, TermbaseColumnPreviewRequest],
            TermbaseColumnPreview,
        ],
    ) -> None:
        super().__init__(descriptor, iter_raw)
        object.__setattr__(self, "_preview_columns", preview_columns)

    def preview_columns(
        self,
        source: SnapshotCursorLease,
        request: TermbaseColumnPreviewRequest,
    ) -> TermbaseColumnPreview:
        return self._preview_columns(source, request)


class _PinnedSeekableTermbasePreviewReader(_PinnedSeekablePreflightReader):
    """Pinned XLSX reader carrying structural preflight and bounded preview."""

    __slots__ = ("_preview_columns",)

    def __init__(
        self,
        descriptor: CodecDescriptor,
        iter_raw: Callable[[SnapshotCursorLease, ReadRequest], Iterator[RawParseEvent]],
        preflight_input: Callable[[SnapshotCursorLease, ReadRequest], None],
        preview_columns: Callable[
            [SnapshotCursorLease, TermbaseColumnPreviewRequest],
            TermbaseColumnPreview,
        ],
    ) -> None:
        super().__init__(descriptor, iter_raw, preflight_input)
        object.__setattr__(self, "_preview_columns", preview_columns)

    def preview_columns(
        self,
        source: SnapshotCursorLease,
        request: TermbaseColumnPreviewRequest,
    ) -> TermbaseColumnPreview:
        return self._preview_columns(source, request)


class ParserRegistry:
    """A construction-time frozen `(purpose, format)` authority map."""

    __slots__ = (
        "_by_key",
        "_descriptors",
        "_supported_combinations",
        "_frozen",
    )

    def __init__(self, descriptors: tuple[CodecDescriptor, ...] = ()) -> None:
        if type(descriptors) is not tuple:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.DESCRIPTOR_INVALID",
                "registry descriptors must be supplied as an immutable tuple",
            )

        typed: list[CodecDescriptor] = []
        for descriptor in descriptors:
            if type(descriptor) is not CodecDescriptor:
                raise RegistryConfigurationError(
                    "PARSER.SELECTION.DESCRIPTOR_INVALID",
                    "registry entries must use the neutral codec descriptor contract",
                )
            typed.append(descriptor)

        ordered = tuple(sorted(typed, key=_descriptor_sort_key))
        failures: list[tuple[tuple[str, str], str, str]] = []
        by_key: dict[tuple[EffectivePurpose, FormatId], CodecDescriptor] = {}
        duplicate_keys: set[tuple[EffectivePurpose, FormatId]] = set()

        for descriptor in ordered:
            key = (descriptor.purpose, descriptor.format_id)
            text_key = (descriptor.purpose.value, descriptor.format_id.value)
            if key in by_key:
                duplicate_keys.add(key)
            else:
                by_key[key] = descriptor

            expected_purpose = builtin_purpose_for_format(descriptor.format_id)
            if expected_purpose is not None and expected_purpose is not descriptor.purpose:
                failures.append(
                    (
                        text_key,
                        "PARSER.SELECTION.PURPOSE_INCOMPATIBLE",
                        "descriptor purpose is incompatible with the stable format contract",
                    )
                )
            capability_problem = _capability_factory_problem(descriptor)
            if capability_problem is not None:
                failures.append(
                    (
                        text_key,
                        "PARSER.SELECTION.CAPABILITY_MISMATCH",
                        capability_problem,
                    )
                )

        for purpose, format_id in duplicate_keys:
            failures.append(
                (
                    (purpose.value, format_id.value),
                    "PARSER.SELECTION.DUPLICATE_AUTHORITY",
                    "multiple descriptors claim the same purpose and format authority",
                )
            )

        if failures:
            _key, code, summary = min(
                failures,
                key=lambda item: (item[0], item[1], item[2]),
            )
            raise RegistryConfigurationError(code, summary)

        object.__setattr__(self, "_descriptors", ordered)
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(
            self,
            "_supported_combinations",
            tuple(
                SupportedCombination(
                    purpose=descriptor.purpose,
                    format_id=descriptor.format_id,
                )
                for descriptor in ordered
            )
        )
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        if getattr(self, "_frozen", False):
            raise AttributeError("ParserRegistry is frozen after construction")
        raise AttributeError("ParserRegistry state is Foundation-owned")

    @property
    def descriptors(self) -> tuple[CodecDescriptor, ...]:
        return self._descriptors

    @property
    def supported_combinations(self) -> tuple[SupportedCombination, ...]:
        return self._supported_combinations

    def select(self, request: SelectionRequest) -> CodecDescriptor | SelectionFailure:
        if type(request) is not SelectionRequest:
            raise TypeError("request must be exact SelectionRequest")

        if request.format_id is not None:
            descriptor = self._by_key.get((request.purpose, request.format_id))
            if descriptor is not None:
                return descriptor
            return self._selection_failure(
                request,
                code="PARSER.SELECTION.UNSUPPORTED",
            )

        candidates = tuple(
            descriptor
            for descriptor in self._descriptors
            if descriptor.purpose is request.purpose
        )
        hints = request.hints
        assert hints is not None
        if hints.extensions:
            extensions = frozenset(hints.extensions)
            candidates = tuple(
                descriptor
                for descriptor in candidates
                if extensions.intersection(descriptor.extensions)
            )
        if hints.mime_types:
            mime_types = frozenset(hints.mime_types)
            candidates = tuple(
                descriptor
                for descriptor in candidates
                if mime_types.intersection(descriptor.mime_types)
            )
        if hints.prefix:
            candidates = tuple(
                descriptor
                for descriptor in candidates
                if not descriptor.sniff_prefixes
                or any(
                    hints.prefix.startswith(marker)
                    for marker in descriptor.sniff_prefixes
                )
            )

        if len(candidates) == 1:
            return candidates[0]
        return self._selection_failure(
            request,
            code=(
                "PARSER.SELECTION.UNSUPPORTED"
                if not candidates
                else "PARSER.SELECTION.AMBIGUOUS"
            ),
        )

    def create_reader(self, descriptor: CodecDescriptor) -> RawReaderCodec:
        self._require_registered_descriptor(descriptor)
        factory = descriptor.reader_factory
        if factory is None:
            raise RegistryConfigurationError(
                "PARSER.CAPABILITY.READ_UNSUPPORTED",
                "the selected codec does not publish a reader factory",
            )
        try:
            reader = factory()
        except Exception:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_FAILED",
                "the selected reader factory failed",
            ) from None
        try:
            published_descriptor = reader.descriptor
            iter_raw = reader.iter_raw
            behavior_matches = isinstance(reader, RawReaderCodec)
        except Exception:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "reader factory product does not match its registered descriptor contract",
            ) from None
        if (
            published_descriptor is not descriptor
            or not behavior_matches
            or not callable(iter_raw)
        ):
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "reader factory product does not match its registered descriptor contract",
            )
        preview_columns = None
        if descriptor.capabilities.termbase_column_preview:
            try:
                preview_columns = reader.preview_columns
                preview_matches = isinstance(reader, TermbaseColumnPreviewCodec)
            except Exception:
                raise RegistryConfigurationError(
                    "PARSER.SELECTION.FACTORY_MISMATCH",
                    "reader factory product lacks its declared column preview behavior",
                ) from None
            if not preview_matches or not callable(preview_columns):
                raise RegistryConfigurationError(
                    "PARSER.SELECTION.FACTORY_MISMATCH",
                    "reader factory product lacks its declared column preview behavior",
                )
        if (
            descriptor.input_consumption_policy
            is InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
        ):
            try:
                preflight_input = reader.preflight_input
                preflight_matches = isinstance(reader, SeekableInputPreflightCodec)
            except Exception:
                raise RegistryConfigurationError(
                    "PARSER.SELECTION.FACTORY_MISMATCH",
                    "reader factory product lacks its required input preflight behavior",
                ) from None
            if not preflight_matches or not callable(preflight_input):
                raise RegistryConfigurationError(
                    "PARSER.SELECTION.FACTORY_MISMATCH",
                    "reader factory product lacks its required input preflight behavior",
                )
            if preview_columns is not None:
                return _PinnedSeekableTermbasePreviewReader(
                    descriptor,
                    iter_raw,
                    preflight_input,
                    preview_columns,
                )
            return _PinnedSeekablePreflightReader(descriptor, iter_raw, preflight_input)
        if preview_columns is not None:
            return _PinnedTermbasePreviewReader(descriptor, iter_raw, preview_columns)
        return _PinnedRawReader(descriptor, iter_raw)

    def create_canonical_serializer(
        self,
        descriptor: CodecDescriptor,
    ) -> CanonicalSerializerCodec:
        self._require_registered_descriptor(descriptor)
        factory = descriptor.canonical_serializer_factory
        if factory is None:
            raise RegistryConfigurationError(
                "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
                "the selected codec does not publish a canonical serializer factory",
            )
        try:
            serializer = factory()
        except Exception:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_FAILED",
                "the selected canonical serializer factory failed",
            ) from None
        try:
            published_descriptor = serializer.descriptor
            serialize_canonical = serializer.serialize_canonical
            behavior_matches = isinstance(serializer, CanonicalSerializerCodec)
        except Exception:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "serializer factory product does not match its registered descriptor contract",
            ) from None
        if (
            published_descriptor is not descriptor
            or not behavior_matches
            or not callable(serialize_canonical)
        ):
            raise RegistryConfigurationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "serializer factory product does not match its registered descriptor contract",
            )
        return _PinnedCanonicalSerializer(descriptor, serialize_canonical)

    def _require_registered_descriptor(self, descriptor: CodecDescriptor) -> None:
        if type(descriptor) is not CodecDescriptor:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.DESCRIPTOR_INVALID",
                "factory lookup requires the neutral codec descriptor contract",
            )
        registered = self._by_key.get((descriptor.purpose, descriptor.format_id))
        if registered is not descriptor:
            raise RegistryConfigurationError(
                "PARSER.SELECTION.DESCRIPTOR_UNREGISTERED",
                "factory lookup requires the descriptor selected from this registry",
            )

    def _selection_failure(
        self,
        request: SelectionRequest,
        *,
        code: str,
    ) -> SelectionFailure:
        retained = self._supported_combinations[
            :MAX_RETAINED_SUPPORTED_COMBINATIONS
        ]
        return SelectionFailure(
            code=code,
            requested_purpose=request.purpose,
            requested_format_id=request.format_id,
            observed_hints=(
                None
                if request.hints is None
                else SelectionHintSummary.from_hints(request.hints)
            ),
            supported_combinations=retained,
            supported_combination_count=len(self._supported_combinations),
            supported_combinations_truncated=(
                len(self._supported_combinations) > len(retained)
            ),
        )


def _descriptor_sort_key(descriptor: CodecDescriptor) -> tuple[str, str, str, str, str]:
    return (
        descriptor.purpose.value,
        descriptor.format_id.value,
        descriptor.identity.provider_id,
        descriptor.identity.codec_id,
        descriptor.identity.codec_version,
    )


def _capability_factory_problem(descriptor: CodecDescriptor) -> str | None:
    capabilities = descriptor.capabilities
    reader_present = descriptor.reader_factory is not None
    serializer_present = descriptor.canonical_serializer_factory is not None
    if capabilities.readable != reader_present:
        return "reader capability and reader factory availability disagree"
    if capabilities.canonical_write != serializer_present:
        return "canonical-write capability and serializer factory availability disagree"
    if capabilities.validatable and not capabilities.readable:
        return "validation capability requires readable input"
    if (
        capabilities.iterator_view or capabilities.materialized_view
    ) and not capabilities.readable:
        return "record view capability requires readable input"
    return None
