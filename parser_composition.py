"""Explicit Parser composition root and sole Application-facing runtime surface.

Built-ins are imported and registered only here.  Public callers may additionally
provide explicitly configured providers; there is no discovery or purpose fallback.
Application facades can coordinate rooted source operations through this module
without importing codec, registry, or Source Boundary internals.
"""

from __future__ import annotations

from dataclasses import dataclass

from parser_contracts import (
    BUILTIN_FORMAT_IDS,
    CanonicalBytes as _CanonicalBytes,
    CanonicalSerializeRequest,
    CodecDescriptor,
    CodecProvider,
    ContractViolation,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    SourceSnapshotIdentity,
    TargetReference,
    ValidationReport,
    WriteReceipt,
    builtin_purpose_for_format,
)
from parser_gettext_codec import gettext_descriptors as _gettext_descriptors
from parser_localcat_codec import localcat_descriptors as _localcat_descriptors
from parser_registry import ParserRegistry as _ParserRegistry
from parser_source import (
    CancellationToken as _CancellationToken,
    GuardedParseSession as _GuardedParseSession,
    MaterializedParseResult as _MaterializedParseResult,
    ParserSourceError as _ParserSourceError,
    SealedSourceSnapshot as _SealedSourceSnapshot,
    atomic_write_bytes as _atomic_write_bytes,
    create_sealed_snapshot as _create_sealed_snapshot,
    materialize as _materialize,
    validate as _validate,
)
from parser_termbase_codec import termbase_descriptors as _termbase_descriptors
from parser_tm_json_codec import (
    normalized_tm_json_descriptors as _normalized_tm_json_descriptors,
)
from parser_tmx_codec import TMX_CODEC_DESCRIPTOR as _TMX_CODEC_DESCRIPTOR


_COMPOSITION_AUTHORITY = object()


class ProviderConfigurationError(ContractViolation):
    """Body-safe failure for an explicitly configured provider binding."""


class ParserApplicationError(ContractViolation):
    """Body-safe failure at the composition-owned Application surface."""


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    provider_id: str
    provider: CodecProvider | None
    enabled: bool
    compatible_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or not self.provider_id:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider identity must be a non-empty string",
            )
        if self.provider_id != self.provider_id.strip():
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider identity must not contain surrounding whitespace",
            )
        if type(self.enabled) is not bool:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider enabled state must be an exact boolean",
            )
        if type(self.compatible_versions) is not tuple:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must be an immutable tuple",
            )
        if not self.compatible_versions:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must not be empty",
            )
        for version in self.compatible_versions:
            if type(version) is not str or not version or version != version.strip():
                raise ProviderConfigurationError(
                    "PARSER.SELECTION.PROVIDER_INVALID",
                    "provider version allowlist contains an invalid version",
                )
        if len(set(self.compatible_versions)) != len(self.compatible_versions):
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must not contain duplicates",
            )
        object.__setattr__(
            self,
            "compatible_versions",
            tuple(sorted(self.compatible_versions)),
        )


def compose_registry(
    *,
    providers: tuple[ProviderBinding, ...] = (),
    descriptors: object = None,
) -> _ParserRegistry:
    """Build an empty/bound-provider registry without discovery fallback.

    ``descriptors`` is a fail-closed compatibility trap: external descriptors must
    use ``ProviderBinding``.  Built-ins are available only through
    :func:`create_builtin_registry` and this module's private trusted seam.
    """

    if descriptors is not None:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
            "external codec descriptors must be supplied by a configured provider",
        )
    return _compose_from_trusted_builtins((), providers=providers)


def create_builtin_registry(
    *,
    providers: tuple[ProviderBinding, ...] = (),
) -> _ParserRegistry:
    """Create the exact Parser v1 built-in matrix plus explicit providers."""

    descriptors = _builtin_descriptors()
    return _compose_from_trusted_builtins(descriptors, providers=providers)


def create_parser_application_surface(
    *,
    providers: tuple[ProviderBinding, ...] = (),
) -> ParserApplicationSurface:
    """Create the sole Parser surface intended for Application facades."""

    return ParserApplicationSurface(
        create_builtin_registry(providers=providers),
        _authority=_COMPOSITION_AUTHORITY,
    )


class ParserApplicationSurface:
    """Coordinate selection, sealed reads, guarded views, and canonical writes."""

    __slots__ = ("_registry",)

    def __init__(
        self,
        registry: _ParserRegistry,
        *,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "Parser Application surfaces must be created by the composition factory",
            )
        if type(registry) is not _ParserRegistry:
            raise TypeError("registry must be exact ParserRegistry")
        self._registry = registry

    def select(
        self,
        request: SelectionRequest,
    ) -> CodecDescriptor | SelectionFailure:
        return self._registry.select(request)

    def open_input(
        self,
        reference: SourceReference,
        selection: SelectionRequest,
        request: ReadRequest,
        *,
        cancellation: _CancellationToken | None = None,
    ) -> OpenedParserInput | SelectionFailure:
        """Select and seal one rooted source before exposing any guarded view."""

        if type(reference) is not SourceReference:
            raise TypeError("reference must be exact SourceReference")
        if type(selection) is not SelectionRequest:
            raise TypeError("selection must be exact SelectionRequest")
        if type(request) is not ReadRequest:
            raise TypeError("request must be exact ReadRequest")
        descriptor = self._registry.select(selection)
        if type(descriptor) is SelectionFailure:
            return descriptor
        if (
            request.purpose is not descriptor.purpose
            or request.format_id != descriptor.format_id
        ):
            raise ParserApplicationError(
                "PARSER.SELECTION.UNSUPPORTED",
                "read request does not match the selected codec authority",
            )

        # Prove reader behavior before touching the caller-selected source.  The
        # pinned instance is consumed by the first requested view.
        primed_reader = self._registry.create_reader(descriptor)
        snapshot = _create_sealed_snapshot(
            reference,
            limit_profile=descriptor.limit_profile,
            cancellation=cancellation,
        )
        return OpenedParserInput(
            self._registry,
            descriptor,
            snapshot,
            request,
            cancellation,
            primed_reader,
            _authority=_COMPOSITION_AUTHORITY,
        )

    def write_canonical(
        self,
        purpose: EffectivePurpose,
        request: CanonicalSerializeRequest,
        target: TargetReference,
    ) -> WriteReceipt:
        """Serialize then atomically replace one rooted target and return proof."""

        if type(target) is not TargetReference:
            raise TypeError("target must be exact TargetReference")
        return self.prepare_canonical(purpose, request).write(target)

    def prepare_canonical(
        self,
        purpose: EffectivePurpose,
        request: CanonicalSerializeRequest,
    ) -> PreparedCanonicalWrite:
        """Pin proven canonical bytes without opening or modifying a target."""

        if type(purpose) is not EffectivePurpose:
            raise TypeError("purpose must be exact EffectivePurpose")
        if type(request) is not CanonicalSerializeRequest:
            raise TypeError("request must be exact CanonicalSerializeRequest")

        selected = self._registry.select(
            SelectionRequest(purpose=purpose, format_id=request.format_id)
        )
        if type(selected) is SelectionFailure:
            raise ParserApplicationError(
                selected.code,
                "canonical write purpose and format are not a supported combination",
            )
        serializer = self._registry.create_canonical_serializer(selected)
        try:
            serialized = serializer.serialize_canonical(request)
        except ContractViolation as exc:
            raise ParserApplicationError(
                exc.code,
                "canonical serializer rejected the write request before target open",
            ) from None
        except Exception:
            raise ParserApplicationError(
                "PARSER.SOURCE.WRITE_FAILED",
                "canonical serializer failed before the target was opened",
            ) from None
        if (
            type(serialized) is not _CanonicalBytes
            or serialized.codec_identity != selected.identity
            or serialized.format_id != selected.format_id
            or serialized.format_id != request.format_id
        ):
            raise ParserApplicationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "canonical serializer output does not match its selected authority",
            )
        return PreparedCanonicalWrite(
            serialized.payload,
            _authority=_COMPOSITION_AUTHORITY,
        )


class PreparedCanonicalWrite:
    """Opaque, factory-issued canonical payload authorized only for rooted writes."""

    __slots__ = ("__payload", "_frozen")

    def __init__(
        self,
        payload: bytes,
        *,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "prepared canonical writes must be created by the Application surface",
            )
        if type(payload) is not bytes:
            raise TypeError("prepared canonical payload must be exact bytes")
        object.__setattr__(self, "_PreparedCanonicalWrite__payload", payload)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        if getattr(self, "_frozen", False):
            raise AttributeError("prepared canonical write is immutable")
        raise AttributeError("prepared canonical authority is composition-owned")

    def write(self, target: TargetReference) -> WriteReceipt:
        """Atomically replace one rooted target with the already-proven bytes."""

        if type(target) is not TargetReference:
            raise TypeError("target must be exact TargetReference")
        return _atomic_write_bytes(target, self.__payload)


class OpenedParserInput:
    """Own one sealed snapshot while delegating all verification to Foundation."""

    __slots__ = (
        "_registry",
        "_descriptor",
        "_snapshot",
        "_request",
        "_cancellation",
        "_primed_reader",
        "_closed",
    )

    def __init__(
        self,
        registry: _ParserRegistry,
        descriptor: CodecDescriptor,
        snapshot: _SealedSourceSnapshot,
        request: ReadRequest,
        cancellation: _CancellationToken | None,
        primed_reader,
        *,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "opened Parser inputs must be created by the Application surface",
            )
        self._registry = registry
        self._descriptor = descriptor
        self._snapshot = snapshot
        self._request = request
        self._cancellation = cancellation
        self._primed_reader = primed_reader
        self._closed = False

    @property
    def descriptor(self) -> CodecDescriptor:
        return self._descriptor

    @property
    def source_identity(self) -> SourceSnapshotIdentity:
        return self._snapshot.identity

    @property
    def source_name_hint(self) -> str:
        return self._snapshot.source_name_hint

    def _reader(self):
        self._require_open()
        reader = self._primed_reader
        if reader is not None:
            self._primed_reader = None
            return reader
        return self._registry.create_reader(self._descriptor)

    def stream(self) -> _GuardedParseSession:
        """Return Foundation's terminal-aware iterator without materializing it."""

        return _GuardedParseSession(
            self._reader(),
            self._snapshot,
            self._request,
            cancellation=self._cancellation,
        )

    def validate(self) -> ValidationReport:
        return _validate(
            self._reader(),
            self._snapshot,
            self._request,
            cancellation=self._cancellation,
        )

    def materialize(self) -> _MaterializedParseResult:
        return _materialize(
            self._reader(),
            self._snapshot,
            self._request,
            cancellation=self._cancellation,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._primed_reader = None
        self._snapshot.close()

    def _require_open(self) -> None:
        if self._closed:
            raise _ParserSourceError(
                "PARSER.SOURCE.SNAPSHOT_RELEASED",
                "the opened parser input no longer accepts guarded views",
            )

    def __enter__(self) -> OpenedParserInput:
        self._require_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def _builtin_descriptors() -> tuple[CodecDescriptor, ...]:
    descriptors = (
        *_localcat_descriptors(),
        *_gettext_descriptors(),
        _TMX_CODEC_DESCRIPTOR,
        *_normalized_tm_json_descriptors(),
        *_termbase_descriptors(),
    )
    expected = {
        (builtin_purpose_for_format(format_id), format_id)
        for format_id in BUILTIN_FORMAT_IDS
    }
    observed = {
        (descriptor.purpose, descriptor.format_id)
        for descriptor in descriptors
    }
    if len(descriptors) != len(BUILTIN_FORMAT_IDS) or observed != expected:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.BUILTIN_MATRIX_INVALID",
            "built-in codec exports do not match the closed Parser v1 support matrix",
        )
    for descriptor in descriptors:
        if not set(FOUNDATION_GUARDED_ISSUE_CODES).issubset(
            descriptor.declared_issue_codes
        ):
            raise ProviderConfigurationError(
                "PARSER.SELECTION.BUILTIN_MATRIX_INVALID",
                "a built-in codec profile omits mandatory Foundation issue codes",
            )
    return descriptors


def _compose_from_trusted_builtins(
    builtin_descriptors: tuple[CodecDescriptor, ...],
    *,
    providers: tuple[ProviderBinding, ...],
) -> _ParserRegistry:
    """Internal-only seam for the explicitly imported built-in descriptors."""

    if type(builtin_descriptors) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.DESCRIPTOR_INVALID",
            "trusted built-in descriptors must be supplied as an immutable tuple",
        )
    if type(providers) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "provider bindings must be supplied as an immutable tuple",
        )
    for binding in providers:
        if type(binding) is not ProviderBinding:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider bindings must use the explicit neutral composition contract",
            )

    provider_ids = tuple(sorted(binding.provider_id for binding in providers))
    if len(set(provider_ids)) != len(provider_ids):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_DUPLICATE",
            "the same provider identity was configured more than once",
        )

    collected = list(builtin_descriptors)
    for binding in sorted(providers, key=lambda item: item.provider_id):
        collected.extend(_load_provider(binding))
    return _ParserRegistry(tuple(collected))


def _load_provider(binding: ProviderBinding) -> tuple[CodecDescriptor, ...]:
    if not binding.enabled:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_DISABLED",
            "the configured codec provider is disabled",
        )
    provider = binding.provider
    if provider is None:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_MISSING",
            "the configured codec provider is not available",
        )

    try:
        provider_id = provider.provider_id
        provider_version = provider.provider_version
        descriptors_method = provider.descriptors
    except Exception:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the configured codec provider does not satisfy the neutral provider contract",
        ) from None

    if not isinstance(provider, CodecProvider):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the configured codec provider does not satisfy the neutral provider contract",
        )

    if (
        type(provider_id) is not str
        or provider_id != binding.provider_id
        or not provider_id
    ):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
            "configured and published provider identities do not match",
        )
    if (
        type(provider_version) is not str
        or not provider_version
        or provider_version != provider_version.strip()
    ):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider publishes an invalid version",
        )
    if provider_version not in binding.compatible_versions:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
            "the codec provider version is outside the configured compatibility allowlist",
        )
    if not callable(descriptors_method):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider does not publish a descriptor factory",
        )

    try:
        descriptors = descriptors_method()
    except Exception:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_FAILED",
            "the codec provider failed while publishing descriptors",
        ) from None
    if type(descriptors) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider must publish an immutable descriptor tuple",
        )
    for descriptor in descriptors:
        if type(descriptor) is not CodecDescriptor:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "the codec provider published a non-contract descriptor",
            )
        if descriptor.identity.provider_id != provider_id:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
                "provider and descriptor identities do not match",
            )
    return descriptors
