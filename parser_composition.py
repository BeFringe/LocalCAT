"""Explicit Parser composition root and sole Application-facing runtime surface.

Built-ins are imported and registered only here.  Public callers may additionally
provide explicitly configured providers; there is no discovery or purpose fallback.
Application facades can coordinate rooted source operations through this module
without importing codec, registry, or Source Boundary internals.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import threading
from typing import Callable
from weakref import WeakKeyDictionary, WeakSet

from platform_fs import compose_platform_file_backend as _compose_platform_file_backend
from platform_fs_contracts import PlatformFileBackend, PlatformFileError
from platform_export_directory import MaterializedExportTarget as _MaterializedExportTarget


from parser_contracts import (
    BUILTIN_FORMAT_IDS,
    CanonicalBytes as _CanonicalBytes,
    CanonicalSerializeRequest,
    CodecDescriptor,
    CodecIdentity,
    CodecProvider,
    ContractViolation,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    FormatId,
    IssueSeverity,
    ParseIssue,
    PreparedFormatBytes,
    PreparedWriteOutcome,
    PreparedWriteResult,
    ReadRequest,
    RoundTripRequest,
    RoundTripSegmentEdit,
    RoundTripTokenEnvelope,
    OpaqueSourceState,
    SourceStateRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    SourceSnapshotIdentity,
    TargetReference,
    TermbaseColumnPreview,
    TermbaseColumnPreviewCodec,
    TermbaseColumnPreviewRequest,
    TerminalSuccess,
    ValidationReport,
    WriteReceipt,
    builtin_purpose_for_format,
    validate_round_trip_token,
)
from parser_gettext_codec import gettext_descriptors as _gettext_descriptors
from parser_localcat_codec import localcat_descriptors as _localcat_descriptors
from parser_registry import ParserRegistry as _ParserRegistry
from parser_source import (
    BoundWriteTarget as _BoundWriteTarget,
    CancellationToken as _CancellationToken,
    GuardedParseSession as _GuardedParseSession,
    MaterializedParseResult as _MaterializedParseResult,
    ParserSourceError as _ParserSourceError,
    ParserSessionError as _ParserSessionError,
    SealedSourceSnapshot as _SealedSourceSnapshot,
    atomic_write_bytes as _atomic_write_bytes,
    atomic_write_bound_bytes as _atomic_write_bound_bytes,
    bind_write_target as _bind_write_target,
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
_PlatformBackendFactory = Callable[[Path], PlatformFileBackend]


class ProviderConfigurationError(ContractViolation):
    """Body-safe failure for an explicitly configured provider binding."""


class ParserApplicationError(ContractViolation):
    """Body-safe failure at the composition-owned Application surface."""


class RoundTripPreparationError(ParserApplicationError):
    """Rejected preparation with bounded, body-safe positional diagnostics."""

    def __init__(
        self, code: str, safe_summary: str, diagnostics: tuple[ParseIssue, ...] = (),
    ) -> None:
        self.diagnostics = diagnostics
        super().__init__(code, safe_summary)


class SourceHandoffError(ParserApplicationError):
    """No source handoff was issued; diagnostics contain safe positions only."""

    def __init__(self, code: str, diagnostics: tuple[ParseIssue, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(code, "source verification did not complete")


def _rooted_backend(
    factory: _PlatformBackendFactory,
    root_text: str,
) -> PlatformFileBackend:
    """Compose one live rooted backend while retaining Parser failure semantics."""

    try:
        backend = factory(Path(root_text))
    except PlatformFileError:
        raise _ParserSourceError(
            "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE",
            "this platform cannot establish the required rooted file authority",
        ) from None
    if not isinstance(backend, PlatformFileBackend):
        raise TypeError("platform backend factory must return PlatformFileBackend")
    return backend


def _preview_termbase_columns_on_snapshot(
    reader,
    snapshot: _SealedSourceSnapshot,
    descriptor: CodecDescriptor,
    request: TermbaseColumnPreviewRequest,
) -> TermbaseColumnPreview:
    """Run pinned preview behavior on one already sealed immutable source."""

    if not descriptor.capabilities.termbase_column_preview:
        raise ParserApplicationError(
            "PARSER.CAPABILITY.PREVIEW_UNSUPPORTED",
            "the selected codec does not publish termbase column preview",
        )
    if not isinstance(reader, TermbaseColumnPreviewCodec):
        raise ParserApplicationError(
            "PARSER.SELECTION.FACTORY_MISMATCH",
            "the selected reader lacks its pinned column preview behavior",
        )
    lease = snapshot.lease(descriptor)
    try:
        try:
            preview = reader.preview_columns(lease, request)
        except ContractViolation as exc:
            if exc.code not in descriptor.declared_issue_codes and exc.code not in {
                "PARSER.TERMBASE.PREVIEW_EMPTY",
                "PARSER.SELECTION.UNSUPPORTED",
            }:
                raise ParserApplicationError(
                    "PARSER.PLUGIN.ISSUE_UNDECLARED",
                    "the selected preview codec raised an undeclared failure code",
                ) from None
            raise
        if (
            type(preview) is not TermbaseColumnPreview
            or preview.source != snapshot.identity
            or preview.codec_identity != descriptor.identity
            or preview.format_id != descriptor.format_id
        ):
            raise ParserApplicationError(
                "PARSER.SELECTION.FACTORY_MISMATCH",
                "column preview output does not match its selected authority",
            )
        return preview
    finally:
        lease.close()


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


def new_cancellation_token() -> _CancellationToken:
    """Create an independent cancellation flag through the Application seam.

    The flag can stop an operation at Foundation checkpoints. It grants no
    source, codec, or publication authority and does not revoke proved writes.
    """

    return _CancellationToken()


def create_parser_application_surface(
    *,
    providers: tuple[ProviderBinding, ...] = (),
) -> ParserApplicationSurface:
    """Create the sole Parser surface intended for Application facades."""

    return ParserApplicationSurface(
        create_builtin_registry(providers=providers),
        _platform_backend_factory=_compose_platform_file_backend,
        _authority=_COMPOSITION_AUTHORITY,
    )


class ParserApplicationSurface:
    """Coordinate selection, sealed reads, guarded views, and canonical writes."""

    __slots__ = (
        "_registry", "_platform_backend_factory", "_opened_inputs",
        "_prepared_round_trips",
        "_preparation_lock",
    )

    def __init__(
        self,
        registry: _ParserRegistry,
        *,
        _platform_backend_factory: _PlatformBackendFactory | None = None,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "Parser Application surfaces must be created by the composition factory",
            )
        if type(registry) is not _ParserRegistry:
            raise TypeError("registry must be exact ParserRegistry")
        if not callable(_platform_backend_factory):
            raise TypeError("platform backend factory must be callable")
        self._registry = registry
        self._platform_backend_factory = _platform_backend_factory
        self._opened_inputs: WeakSet[OpenedParserInput] = WeakSet()
        self._prepared_round_trips: WeakKeyDictionary[
            PreparedRoundTripWrite, _RoundTripPreparation,
        ] = WeakKeyDictionary()
        self._preparation_lock = threading.RLock()

    def select(
        self,
        request: SelectionRequest,
    ) -> CodecDescriptor | SelectionFailure:
        return self._registry.select(request)

    def materialize_handoff(self, opened: OpenedParserInput) -> VerifiedSourceHandoff:
        """Issue complete source/records/private data once from one live sealed input."""
        if type(opened) is not OpenedParserInput or opened not in self._opened_inputs:
            raise ParserApplicationError("PARSER.SOURCE.UNVERIFIED", "input belongs to another surface")
        with opened._handoff_lock:
            _round_trip_checkpoint(opened)
            if opened._handoff_claimed:
                raise ParserApplicationError("PARSER.SOURCE.UNVERIFIED", "source handoff already consumed")
            opened._handoff_claimed = True
        descriptor = opened.descriptor
        self._registry._require_registered_descriptor(descriptor)
        try:
            materialized = opened.materialize()
        except _ParserSessionError as error:
            raise SourceHandoffError(error.code, tuple(
                replace(issue, safe_summary="the selected codec rejected source verification")
                for issue in error.diagnostics
            )) from None
        materialized = replace(materialized, issues=tuple(
            replace(issue, safe_summary="the selected codec reported a source diagnostic")
            for issue in materialized.issues
        ))
        terminal = materialized.terminal
        _round_trip_checkpoint(opened, terminal)
        opened._snapshot.reprove_content(terminal.source, cancellation=opened._cancellation)
        private = None
        if descriptor.source_state_factory is not None:
            codec = self._registry.create_source_state_codec(descriptor)
            _round_trip_checkpoint(opened, terminal)
            lease = opened._snapshot.lease(descriptor, cancellation=opened._cancellation)
            try:
                request = SourceStateRequest(
                    descriptor.identity, descriptor.format_id, lease, terminal,
                    descriptor.source_state_limits, descriptor.limit_profile,
                )
                try:
                    private = codec.prepare_source_state(request)
                except ContractViolation as error:
                    code = error.code if error.code in descriptor.declared_issue_codes else "PARSER.PLUGIN.ISSUE_UNDECLARED"
                    raise ParserApplicationError(code, "source state preparation rejected") from None
                except Exception:
                    raise ParserApplicationError("PARSER.SOURCE.READ_FAILED", "source state preparation failed") from None
                _round_trip_checkpoint(opened, terminal)
                if (lease.closed or not lease.consumption_proved
                        or type(private) is not OpaqueSourceState
                        or private.codec_identity != descriptor.identity
                        or private.format_id != descriptor.format_id):
                    raise ParserApplicationError("PARSER.SOURCE.UNVERIFIED", "source state binding is invalid")
                if len(private.payload) > descriptor.source_state_limits.max_opaque_payload_bytes:
                    raise ParserApplicationError("PARSER.LIMIT.OPAQUE_PAYLOAD", "source state exceeds its byte limit")
            finally:
                lease.close()
        lease = opened._snapshot.lease(descriptor, cancellation=opened._cancellation)
        try:
            payload = lease.read(descriptor.limit_profile.max_input_bytes + 1)
            _round_trip_checkpoint(opened, terminal)
            if (lease.closed or not lease.consumption_proved
                    or len(payload) != terminal.source.byte_count
                    or hashlib.sha256(payload).hexdigest() != terminal.source.content_sha256):
                raise ParserApplicationError("PARSER.SOURCE.UNVERIFIED", "source bytes do not match verified terminal")
            with opened._handoff_lock:
                _round_trip_checkpoint(opened, terminal)
                return VerifiedSourceHandoff(materialized, payload, private)
        finally:
            lease.close()

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
            file_system=_rooted_backend(
                self._platform_backend_factory,
                reference.safe_root,
            ),
        )
        opened = OpenedParserInput(
            self._registry,
            descriptor,
            snapshot,
            request,
            cancellation,
            primed_reader,
            _authority=_COMPOSITION_AUTHORITY,
        )
        self._opened_inputs.add(opened)
        return opened

    def preview_termbase_columns(
        self,
        reference: SourceReference,
        selection: SelectionRequest,
        request: TermbaseColumnPreviewRequest,
        *,
        cancellation: _CancellationToken | None = None,
    ) -> TermbaseColumnPreview | SelectionFailure:
        """Return one bounded codec-owned first-record column preview."""

        if type(reference) is not SourceReference:
            raise TypeError("reference must be exact SourceReference")
        if type(selection) is not SelectionRequest:
            raise TypeError("selection must be exact SelectionRequest")
        if type(request) is not TermbaseColumnPreviewRequest:
            raise TypeError("request must be exact TermbaseColumnPreviewRequest")
        descriptor = self._registry.select(selection)
        if type(descriptor) is SelectionFailure:
            return descriptor
        if (
            request.purpose is not descriptor.purpose
            or request.format_id != descriptor.format_id
        ):
            raise ParserApplicationError(
                "PARSER.SELECTION.UNSUPPORTED",
                "preview request does not match the selected codec authority",
            )
        if not descriptor.capabilities.termbase_column_preview:
            raise ParserApplicationError(
                "PARSER.CAPABILITY.PREVIEW_UNSUPPORTED",
                "the selected codec does not publish termbase column preview",
            )
        reader = self._registry.create_reader(descriptor)
        snapshot = _create_sealed_snapshot(
            reference,
            limit_profile=descriptor.limit_profile,
            cancellation=cancellation,
            file_system=_rooted_backend(
                self._platform_backend_factory,
                reference.safe_root,
            ),
        )
        try:
            return _preview_termbase_columns_on_snapshot(
                reader,
                snapshot,
                descriptor,
                request,
            )
        finally:
            snapshot.close()

    def prepare_round_trip(
        self,
        opened: OpenedParserInput,
        token: RoundTripTokenEnvelope | None,
        edits: tuple[RoundTripSegmentEdit, ...],
    ) -> PreparedRoundTripWrite:
        """Verify one live sealed input, then prepare bounded format bytes.

        No caller-provided source bytes, source digest, terminal, or ordinary
        PreparedFormatBytes can confer this authority. The input must remain
        open until the preparation is consumed or discarded. Token hashing
        binds opaque state; only the codec can verify that state's grammar.
        """

        if type(opened) is not OpenedParserInput or opened not in self._opened_inputs:
            raise ParserApplicationError(
                "PARSER.SOURCE.UNVERIFIED",
                "round-trip preparation requires a live input issued by this surface",
            )
        opened._require_open()
        descriptor = opened.descriptor
        self._registry._require_registered_descriptor(descriptor)
        if (
            descriptor.round_trip_serializer_factory is None
            or not descriptor.capabilities.source_round_trip_write
        ):
            raise ParserApplicationError(
                "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
                "the selected codec does not publish a round-trip serializer factory",
            )
        limits = descriptor.round_trip_limits
        assert limits is not None
        _round_trip_checkpoint(opened)
        if token is not None and type(token) is not RoundTripTokenEnvelope:
            raise TypeError("token must be exact RoundTripTokenEnvelope or None")
        if token is not None and len(token.opaque_payload) > limits.max_opaque_payload_bytes:
            raise ParserApplicationError(
                "PARSER.LIMIT.OPAQUE_PAYLOAD",
                "round-trip opaque payload exceeds its declared byte limit",
            )
        validated_token = validate_round_trip_token(
            token,
            expected_codec_identity=descriptor.identity,
            expected_source_fingerprint=opened.source_identity.content_sha256,
            expected_format_state_fingerprint=hashlib.sha256(
                token.opaque_payload if token is not None else b"",
            ).hexdigest(),
        )
        _validate_round_trip_edits(edits, descriptor)
        # Run the full guarded grammar; a caller-supplied terminal is not proof.
        report = opened.validate()
        if report.terminal is None:
            diagnostics = _safe_round_trip_diagnostics(report.issues, descriptor)
            code = next(
                (item.code for item in diagnostics if item.severity is IssueSeverity.FATAL),
                "PARSER.SOURCE.UNVERIFIED",
            )
            raise RoundTripPreparationError(
                code, "round-trip source verification did not reach a successful terminal",
                diagnostics,
            )
        terminal = report.terminal
        _round_trip_checkpoint(opened, terminal)
        serializer = self._registry.create_round_trip_serializer(descriptor)
        _round_trip_checkpoint(opened, terminal)
        lease = opened._snapshot.lease(descriptor, cancellation=opened._cancellation)
        try:
            request = RoundTripRequest(
                descriptor.identity, descriptor.format_id, lease, terminal,
                validated_token, edits, limits, descriptor.limit_profile,
            )
            try:
                serialized = serializer.prepare(request)
            except ContractViolation as exc:
                code = exc.code
                if code not in descriptor.declared_issue_codes:
                    code = "PARSER.PLUGIN.ISSUE_UNDECLARED"
                raise RoundTripPreparationError(
                    code, "round-trip serializer rejected preparation before target open",
                ) from None
            except Exception:
                raise RoundTripPreparationError(
                    "PARSER.SOURCE.WRITE_FAILED",
                    "round-trip serializer failed before target open",
                ) from None
            _round_trip_checkpoint(opened, terminal)
            if lease.closed or not lease.consumption_proved:
                raise ParserApplicationError(
                    "PARSER.SOURCE.UNVERIFIED",
                    "round-trip serializer did not consume its complete live source lease",
                )
            serialized = _validate_round_trip_output(serialized, descriptor, terminal)
            _round_trip_checkpoint(opened, terminal)
            prepared = PreparedRoundTripWrite(
                descriptor, terminal.source, serialized,
                _discard=self._discard_round_trip_preparation,
                _authority=_COMPOSITION_AUTHORITY,
            )
            with self._preparation_lock:
                self._prepared_round_trips[prepared] = _RoundTripPreparation(
                    opened, descriptor, terminal, validated_token, serialized,
                )
                opened._round_trip_preparations.add(prepared)
                # Closing an input concurrently cannot leave a newly issued
                # preparation alive after the close traversal.
                if opened._closed:
                    prepared.close()
                    opened._require_open()
            return prepared
        finally:
            lease.close()

    def _require_round_trip_preparation(
        self, prepared: PreparedRoundTripWrite,
    ) -> _RoundTripPreparation:
        """Owner-private reproof of an unconsumed round-trip preparation."""

        if type(prepared) is not PreparedRoundTripWrite:
            binding = None
        else:
            binding = self._prepared_round_trips.get(prepared)
        if binding is None or prepared.closed:
            raise ParserApplicationError(
                "PARSER.CAPABILITY.INVALID_PREPARATION",
                "preparation is not a live result issued by this surface",
            )
        self._verify_round_trip_binding(prepared, binding)
        return binding

    def _verify_round_trip_binding(
        self, prepared: PreparedRoundTripWrite, binding: _RoundTripPreparation,
    ) -> None:
        self._registry._require_registered_descriptor(binding.descriptor)
        _round_trip_checkpoint(binding.opened, binding.terminal)
        if (
            binding.opened.descriptor is not binding.descriptor
            or prepared.codec_identity != binding.descriptor.identity
            or prepared.source_identity != binding.terminal.source
            or prepared.format_id != binding.descriptor.format_id
            or prepared.output_fingerprint != binding.serialized.output_fingerprint
            or prepared.byte_count != len(binding.serialized.payload)
        ):
            raise ParserApplicationError(
                "PARSER.CAPABILITY.INVALID_PREPARATION", "prepared codec authority has changed",
            )
        validate_round_trip_token(
            binding.token,
            expected_codec_identity=binding.descriptor.identity,
            expected_source_fingerprint=binding.terminal.source.content_sha256,
            expected_format_state_fingerprint=hashlib.sha256(binding.token.opaque_payload).hexdigest(),
        )
        _validate_round_trip_output(binding.serialized, binding.descriptor, binding.terminal)

    def _discard_round_trip_preparation(self, prepared: PreparedRoundTripWrite) -> None:
        with self._preparation_lock:
            binding = self._prepared_round_trips.pop(prepared, None)
            if binding is not None and binding.target is not None:
                binding.target.close()

    def bind_round_trip_target(
        self, prepared: PreparedRoundTripWrite, target: TargetReference,
    ) -> None:
        """Bind a preparation once to an existing parent and expected target.

        This is read-only, retaining the parent and any existing target handle.
        Publication rechecks this exact existing/absent condition under the
        platform protocol; it does not promise CAS against noncooperating writers.
        Closing the preparation or its source releases these retained handles.
        """

        with self._preparation_lock:
            try:
                binding = self._require_round_trip_preparation(prepared)
            except BaseException:
                if type(prepared) is PreparedRoundTripWrite and prepared in self._prepared_round_trips:
                    prepared.close()
                raise
            if binding.target is not None:
                raise ParserApplicationError(
                    "PARSER.CAPABILITY.INVALID_PREPARATION",
                    "a preparation cannot be rebound to another target condition",
                )
            if type(target) is not TargetReference:
                raise TypeError("target must be exact TargetReference")
            try:
                binding.target = _bind_write_target(
                    target, backend=_rooted_backend(
                        self._platform_backend_factory, target.safe_root,
                    ),
                )
                self._require_round_trip_preparation(prepared)
            except BaseException:
                prepared.close()
                raise

    def bind_materialized_round_trip_target(self, prepared, target):
        """Consume a platform target without resampling its preview condition."""
        if type(target) is not _MaterializedExportTarget:
            raise TypeError('target must be a platform-issued materialized target')
        with self._preparation_lock:
            transferred = None
            try:
                binding = self._require_round_trip_preparation(prepared)
                if binding.target is not None:
                    raise ParserApplicationError(
                        'PARSER.CAPABILITY.INVALID_PREPARATION',
                        'a preparation cannot be rebound to another target condition',
                    )
                transferred = _BoundWriteTarget(*target.take_binding())
                if not isinstance(transferred.backend, PlatformFileBackend):
                    raise TypeError('materialized target backend must implement PlatformFileBackend')
                transferred.reprove()
                binding.target, transferred = transferred, None
                self._require_round_trip_preparation(prepared)
            except BaseException:
                if type(prepared) is PreparedRoundTripWrite and prepared in self._prepared_round_trips:
                    prepared.close()
                raise
            finally:
                try:
                    if transferred is not None:
                        transferred.close()
                finally:
                    target.close()

    def write_prepared(self, prepared: PreparedRoundTripWrite) -> PreparedWriteResult:
        """Consume once, publish exact prepared bytes, and report physical proof.

        Every attempt on an issued result consumes it, including stale, failed
        and uncertain attempts. Concurrent reuse cannot start another writer.
        Retry requires a fresh preparation and target preview. An already issued
        system call is allowed to finish proof after cancellation; no new write
        stage starts after a cancellation checkpoint rejects the operation.
        """

        with self._preparation_lock:
            binding = (
                self._prepared_round_trips.pop(prepared, None)
                if type(prepared) is PreparedRoundTripWrite else None
            )
            if binding is None:
                return PreparedWriteResult(
                    PreparedWriteOutcome.FAILED, code="PARSER.CAPABILITY.INVALID_PREPARATION",
                    safe_summary="preparation is not an unconsumed result of this surface",
                )
            already_closed = prepared.closed
            object.__setattr__(prepared, "_PreparedRoundTripWrite__closed", True)

        receipt = None
        result = None
        writer_started = False
        try:
            if already_closed or binding.target is None:
                raise ParserApplicationError(
                    "PARSER.CAPABILITY.INVALID_PREPARATION",
                    "publication requires an unconsumed preparation bound to a target",
                )

            def checkpoint() -> None:
                self._verify_round_trip_binding(prepared, binding)
                binding.opened._snapshot.reprove_content(
                    binding.terminal.source, cancellation=binding.opened._cancellation,
                )

            checkpoint()
            writer_started = True
            receipt = _atomic_write_bound_bytes(
                binding.target, binding.serialized.payload, checkpoint=checkpoint,
            )
            result = PreparedWriteResult(PreparedWriteOutcome.PUBLISHED, receipt=receipt)
        except ContractViolation as error:
            uncertain = error.code == "PARSER.SOURCE.WRITE_RECOVERY_REQUIRED"
            result = PreparedWriteResult(
                PreparedWriteOutcome.UNCERTAIN if uncertain else PreparedWriteOutcome.FAILED,
                code=error.code,
                safe_summary="publication requires inspection; no success was proved"
                if uncertain else "prepared publication was rejected before publishing",
            )
        except Exception:
            result = PreparedWriteResult(
                PreparedWriteOutcome.UNCERTAIN if writer_started else PreparedWriteOutcome.FAILED,
                code="PARSER.SOURCE.WRITE_RECOVERY_REQUIRED" if writer_started
                else "PARSER.SOURCE.WRITE_FAILED",
                safe_summary="prepared publication failed before a receipt was issued",
            )
        finally:
            if binding.target is not None:
                try:
                    binding.target.close()
                except Exception:
                    # Cleanup must not escape and turn an already published or
                    # uncertain result into an apparent prepublication failure.
                    if receipt is not None:
                        result = PreparedWriteResult(
                            PreparedWriteOutcome.UNCERTAIN,
                            code="PARSER.SOURCE.WRITE_RECOVERY_REQUIRED",
                            safe_summary="publication cleanup could not be proved",
                        )
        assert result is not None
        return result

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
            _platform_backend_factory=self._platform_backend_factory,
            _authority=_COMPOSITION_AUTHORITY,
        )


def _round_trip_checkpoint(
    opened: OpenedParserInput, terminal: TerminalSuccess | None = None,
) -> None:
    opened._require_open()
    if opened._snapshot.release_requested or opened._snapshot.released:
        raise _ParserSourceError(
            "PARSER.SOURCE.SNAPSHOT_RELEASED",
            "round-trip preparation requires a live sealed source",
        )
    if opened._cancellation is not None:
        opened._cancellation.raise_if_cancelled()
    if terminal is not None and opened.source_identity != terminal.source:
        raise ParserApplicationError(
            "PARSER.SOURCE.STALE", "prepared source identity has changed",
        )


def _validate_round_trip_edits(
    edits: tuple[RoundTripSegmentEdit, ...], descriptor: CodecDescriptor,
) -> None:
    if type(edits) is not tuple:
        raise TypeError("round-trip edits must be an exact tuple")
    profile = descriptor.limit_profile
    if len(edits) > profile.max_records:
        raise ParserApplicationError(
            "PARSER.LIMIT.RECORD", "round-trip edits exceed the record limit",
        )
    local_ids: set[str] = set()
    for edit in edits:
        if type(edit) is not RoundTripSegmentEdit:
            raise TypeError("round-trip edits must contain exact RoundTripSegmentEdit values")
        if max(len(edit.local_id), len(edit.target)) > profile.max_decoded_field_chars:
            raise ParserApplicationError(
                "PARSER.LIMIT.FIELD", "round-trip edit exceeds the decoded field limit",
            )
        if edit.local_id in local_ids:
            raise ParserApplicationError(
                "PARSER.SYNTAX.DUPLICATE_LOCAL_ID", "round-trip edits contain duplicate local identities",
            )
        local_ids.add(edit.local_id)


def _safe_round_trip_diagnostics(
    diagnostics: tuple[ParseIssue, ...], descriptor: CodecDescriptor,
) -> tuple[ParseIssue, ...]:
    if len(diagnostics) > descriptor.limit_profile.max_retained_issues:
        raise ParserApplicationError(
            "PARSER.LIMIT.DIAGNOSTICS", "round-trip diagnostics exceed the retained issue limit",
        )
    if any(issue.code not in descriptor.declared_issue_codes for issue in diagnostics):
        raise ParserApplicationError(
            "PARSER.PLUGIN.ISSUE_UNDECLARED", "round-trip diagnostics contain an undeclared issue code",
        )
    # A plugin's printable summary is not proof it is body-safe. Preserve the
    # machine-readable reason and location, never arbitrary provider text.
    return tuple(replace(
        issue, safe_summary="the selected codec reported a round-trip diagnostic",
    ) for issue in diagnostics)


def _validate_round_trip_output(
    serialized: PreparedFormatBytes,
    descriptor: CodecDescriptor,
    terminal: TerminalSuccess,
) -> PreparedFormatBytes:
    if type(serialized) is not PreparedFormatBytes:
        raise ParserApplicationError(
            "PARSER.SELECTION.FACTORY_MISMATCH", "round-trip serializer returned a non-contract result",
        )
    limits = descriptor.round_trip_limits
    assert limits is not None
    if len(serialized.payload) > limits.max_output_bytes:
        raise ParserApplicationError(
            "PARSER.LIMIT.OUTPUT", "round-trip output exceeds its declared byte limit",
        )
    if (
        serialized.codec_identity != descriptor.identity
        or serialized.format_id != descriptor.format_id
        or serialized.source_fingerprint != terminal.source.content_sha256
        or serialized.output_fingerprint != hashlib.sha256(serialized.payload).hexdigest()
    ):
        raise ParserApplicationError(
            "PARSER.SELECTION.FACTORY_MISMATCH",
            "round-trip output does not match its selected codec, source, or output fingerprint",
        )
    diagnostics = _safe_round_trip_diagnostics(serialized.diagnostics, descriptor)
    fatal = next((issue for issue in diagnostics if issue.severity is IssueSeverity.FATAL), None)
    if fatal is not None:
        raise RoundTripPreparationError(
            fatal.code, "round-trip output contains blocking diagnostics", diagnostics,
        )
    return replace(serialized, diagnostics=diagnostics)


@dataclass(slots=True)
class _RoundTripPreparation:
    """Surface-private bytes and live source binding; not a public DTO."""

    opened: OpenedParserInput
    descriptor: CodecDescriptor
    terminal: TerminalSuccess
    token: RoundTripTokenEnvelope
    serialized: PreparedFormatBytes
    target: _BoundWriteTarget | None = None


class PreparedRoundTripWrite:
    """Read-only preparation facts; publication is a separate surface operation.

    Closing discards the surface's private payload immediately. Keeping this
    object does not keep a closed input valid; its issuing input must stay open.
    """

    __slots__ = (
        "__codec_identity", "__format_id", "__source_identity", "__output_fingerprint",
        "__byte_count", "__diagnostics", "__closed", "__discard", "__weakref__",
    )

    def __init__(
        self,
        descriptor: CodecDescriptor | None = None,
        source: SourceSnapshotIdentity | None = None,
        serialized: PreparedFormatBytes | None = None,
        *,
        _discard: Callable[[PreparedRoundTripWrite], None] | None = None,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "round-trip preparations must be issued by the Application surface",
            )
        assert descriptor is not None and source is not None and serialized is not None
        assert _discard is not None
        for name, value in (
            ("codec_identity", descriptor.identity), ("format_id", descriptor.format_id),
            ("source_identity", source), ("output_fingerprint", serialized.output_fingerprint),
            ("byte_count", len(serialized.payload)), ("diagnostics", serialized.diagnostics),
            ("closed", False), ("discard", _discard),
        ):
            object.__setattr__(self, f"_PreparedRoundTripWrite__{name}", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("prepared round-trip facts are immutable")

    @property
    def codec_identity(self) -> CodecIdentity:
        return self.__codec_identity

    @property
    def format_id(self) -> FormatId:
        return self.__format_id

    @property
    def source_identity(self) -> SourceSnapshotIdentity:
        return self.__source_identity

    @property
    def output_fingerprint(self) -> str:
        return self.__output_fingerprint

    @property
    def byte_count(self) -> int:
        return self.__byte_count

    @property
    def diagnostics(self) -> tuple[ParseIssue, ...]:
        return self.__diagnostics

    @property
    def closed(self) -> bool:
        return self.__closed

    def close(self) -> None:
        if not self.__closed:
            object.__setattr__(self, "_PreparedRoundTripWrite__closed", True)
            self.__discard(self)

    def __enter__(self) -> PreparedRoundTripWrite:
        if self.closed:
            raise ParserApplicationError(
                "PARSER.CAPABILITY.INVALID_PREPARATION", "preparation was already discarded",
            )
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            if hasattr(self, "_PreparedRoundTripWrite__closed"):
                self.close()
        except Exception:
            # Explicit close reports cleanup errors; finalization cannot report
            # or retry a physical operation during garbage collection.
            pass


class PreparedCanonicalWrite:
    """Opaque, factory-issued canonical payload authorized only for rooted writes."""

    __slots__ = ("__payload", "__platform_backend_factory", "_frozen")

    def __init__(
        self,
        payload: bytes,
        *,
        _platform_backend_factory: _PlatformBackendFactory | None = None,
        _authority: object = None,
    ) -> None:
        if _authority is not _COMPOSITION_AUTHORITY:
            raise ParserApplicationError(
                "PARSER.SELECTION.COMPOSITION_REQUIRED",
                "prepared canonical writes must be created by the Application surface",
            )
        if type(payload) is not bytes:
            raise TypeError("prepared canonical payload must be exact bytes")
        if not callable(_platform_backend_factory):
            raise TypeError("platform backend factory must be callable")
        object.__setattr__(self, "_PreparedCanonicalWrite__payload", payload)
        object.__setattr__(
            self,
            "_PreparedCanonicalWrite__platform_backend_factory",
            _platform_backend_factory,
        )
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
        return _atomic_write_bytes(
            target,
            self.__payload,
            backend=_rooted_backend(
                self.__platform_backend_factory,
                target.safe_root,
            ),
        )


@dataclass(frozen=True, slots=True)
class VerifiedSourceHandoff:
    """Data-only copy of one fully verified source; grants no writer authority."""

    materialized: _MaterializedParseResult
    source_bytes: bytes
    private_state: OpaqueSourceState | None


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
        "_round_trip_preparations",
        "_handoff_claimed",
        "_handoff_lock",
        "__weakref__",
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
        self._handoff_claimed = False
        self._handoff_lock = threading.RLock()
        self._round_trip_preparations: WeakSet[PreparedRoundTripWrite] = WeakSet()

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

    def preview_termbase_columns(
        self,
        request: TermbaseColumnPreviewRequest,
    ) -> TermbaseColumnPreview:
        """Preview columns on this exact snapshot before formal parsing."""

        self._require_open()
        if type(request) is not TermbaseColumnPreviewRequest:
            raise TypeError("request must be exact TermbaseColumnPreviewRequest")
        if (
            request.purpose is not self._descriptor.purpose
            or request.format_id != self._descriptor.format_id
        ):
            raise ParserApplicationError(
                "PARSER.SELECTION.UNSUPPORTED",
                "preview request does not match the opened codec authority",
            )
        return _preview_termbase_columns_on_snapshot(
            self._reader(),
            self._snapshot,
            self._descriptor,
            request,
        )

    def close(self) -> None:
        with self._handoff_lock:
            if self._closed:
                return
            self._closed = True
        self._primed_reader = None
        close_error = None
        try:
            for prepared in tuple(self._round_trip_preparations):
                try:
                    prepared.close()
                except Exception as error:
                    close_error = error
        finally:
            self._snapshot.close()
        if close_error is not None:
            raise close_error

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
