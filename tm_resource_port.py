"""Narrow portable-snapshot adapter over the canonical TM owner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import sys
from pathlib import Path
from pathlib import PurePath

from editor_contracts import ResourceConfig, ResourceKind
from platform_fs_contracts import PlatformFileBackend, PlatformFileError
from resource_package_contracts import (
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourceOperationReceipt,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from resource_platform_io import open_rooted_regular
from tm_contracts import (
    AssetPreservationState,
    CanonicalResourceIdentity,
    ExportFailure,
    ExportReport,
    MigrationFailure,
    MigrationReport,
    snapshot_receipt_digest,
)
from tm_engine import TMEngine
from tm_migration import (
    ActivationPreparationError,
    AppliedSnapshotProof,
    MigrationPreflightError,
    PreparedJSONLDiscardReport,
    PreparedJSONLExport,
    TMMigrationService,
)
from tm_sqlite_store import (
    ResourceStoreCoordinator, SQLiteTMStore, SQLiteStoreLifecycleError,
)


_EXPORT_MANIFEST_SUFFIX = ".localcat-snapshot.json"


@dataclass(frozen=True, slots=True)
class _PreparedTMPackageSnapshot:
    """One TM port's live Core disposal authority and immutable public facts."""

    owner: TMResourceSnapshotPort
    service: TMMigrationService
    core: PreparedJSONLExport
    snapshot: PortableResourceSnapshot


class TMResourceSnapshotPort:
    """Call the existing Core export transaction without re-encoding JSONL."""

    def __init__(
        self,
        platform_backend: PlatformFileBackend,
        *,
        owner_resolver: Callable[[ResourceConfig], ResourceStoreCoordinator] | None = None,
    ) -> None:
        if not isinstance(platform_backend, PlatformFileBackend):
            raise TypeError("TM resource backend must satisfy PlatformFileBackend")
        if owner_resolver is not None and not callable(owner_resolver):
            raise TypeError("TM owner resolver must be callable")
        self._backend = platform_backend
        self._windows_runtime = sys.platform == "win32"
        self._owner_resolver = owner_resolver
        self._owners: dict[str, tuple[Path, ResourceStoreCoordinator]] = {}
        self._open_failures: dict[str, Exception] = {}

    def owner_for_resource(self, resource: ResourceConfig) -> ResourceStoreCoordinator:
        """Retain the original owner, including its failure state, for this session.

        Application composition can supply its existing coordinator. Standalone
        tools retain each canonical owner once acquired; a failed open is not
        retried by rebinding the same path. Legacy resources without a canonical
        owner can be tried again after activation. New imports issue a different
        local identity.
        """
        if type(resource) is not ResourceConfig:
            raise TypeError("TM resource must be exact ResourceConfig")
        if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        cached = self._owners.get(resource.id)
        if cached is not None:
            path, coordinator = cached
            if path != resource.path:
                raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
            return coordinator
        failed = self._open_failures.get(resource.id)
        if failed is not None:
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED") from failed
        try:
            if self._owner_resolver is None:
                store = TMEngine(str(resource.path), expected_resource_id=resource.id).canonical_store
                if store is None:
                    # Core reported ordinary legacy state, not a failed
                    # canonical open. No owner exists to latch or rebind.
                    raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
                coordinator = store.coordinator
            else:
                coordinator = self._owner_resolver(resource)
                if type(coordinator) is not ResourceStoreCoordinator:
                    raise TypeError("TM owner resolver must return exact coordinator")
                # This public facade binding checks configured identity without
                # opening a second owner or changing lifecycle state.
                TMEngine.from_open_canonical_store(
                    SQLiteTMStore.from_coordinator(coordinator),
                    configured_jsonl=resource.path, expected_resource_id=resource.id,
                )
            self._owners[resource.id] = (resource.path, coordinator)
            return coordinator
        except (OSError, ValueError, SQLiteStoreLifecycleError) as error:
            self._open_failures[resource.id] = error
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED") from error

    def _store(self, resource: ResourceConfig) -> SQLiteTMStore:
        return SQLiteTMStore.from_coordinator(self.owner_for_resource(resource))

    def export_snapshot(
        self,
        resource: ResourceConfig,
        destination: Path,
    ) -> PortableResourceSnapshot:
        service, store = self._export_owner(resource)
        return self._snapshot_from_export(
            service.export_jsonl(store, destination), destination,
        )

    def prepare_package_snapshot(
        self, resource: ResourceConfig, destination: Path,
    ) -> _PreparedTMPackageSnapshot:
        service, store = self._export_owner(resource)
        prepared = service.prepare_jsonl_export(store, destination)
        if type(prepared) is ExportFailure:
            raise ResourcePortabilityError(
                "RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE", retryable=prepared.retryable,
            )
        if type(prepared) is not PreparedJSONLExport:
            raise TypeError("TM package export returned an unknown outcome")
        # Retain the exact service/token before any facts projection can fail.
        try:
            snapshot = self._snapshot_from_export(prepared.report, destination)
            return _PreparedTMPackageSnapshot(self, service, prepared, snapshot)
        except BaseException as primary:
            try:
                self._discard_export(service, prepared)
            except BaseException as cleanup:
                primary.add_note("RESOURCE.EXPORT.STAGE_FAILED")
                if (
                    type(cleanup) is ResourcePortabilityError
                    and cleanup.code == "RESOURCE.EXPORT.STAGE_FAILED"
                ):
                    for note in getattr(cleanup, "__notes__", ()):
                        primary.add_note(note)
            raise

    def discard_package_snapshot(
        self, prepared: _PreparedTMPackageSnapshot,
    ) -> PreparedJSONLDiscardReport:
        if type(prepared) is not _PreparedTMPackageSnapshot:
            raise TypeError("TM package snapshot must be exact prepared snapshot")
        if prepared.owner is not self:
            raise ValueError("TM package snapshot belongs to another port")
        return self._discard_export(prepared.service, prepared.core)

    @staticmethod
    def _discard_export(
        service: TMMigrationService, prepared: PreparedJSONLExport,
    ) -> PreparedJSONLDiscardReport:
        result = service.discard_prepared_jsonl_export(prepared)
        if type(result) is not PreparedJSONLDiscardReport:
            raise TypeError("TM discard returned an unknown outcome")
        if not result.complete:
            error = ResourcePortabilityError("RESOURCE.EXPORT.STAGE_FAILED")
            error.add_note(
                "RESOURCE.EXPORT.STAGE_FAILED "
                f"removed_count={len(result.removed_names)} "
                f"retained_count={len(result.retained_names)} "
                f"diagnostics={','.join(result.diagnostic_codes)}"
            )
            raise error
        return result

    def _export_owner(
        self, resource: ResourceConfig,
    ) -> tuple[TMMigrationService, SQLiteTMStore]:
        if type(resource) is not ResourceConfig:
            raise TypeError("TM portable resource must be exact ResourceConfig")
        resource.__post_init__()
        if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        try:
            store = self._store(resource)
        except (ResourcePortabilityError, SQLiteStoreLifecycleError, OSError, ValueError) as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE") from error
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource.id,
            resource.path,
        )
        coordinator = store.coordinator
        if coordinator.resource_id != resource.id:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE")
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=coordinator.canonical_store_id,
            coordinator=coordinator,
            platform_backend=self._backend,
        )
        return service, store

    @staticmethod
    def _snapshot_from_export(
        outcome: ExportReport | ExportFailure, destination: Path,
    ) -> PortableResourceSnapshot:
        if type(outcome) is ExportFailure:
            raise ResourcePortabilityError(
                "RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE",
                retryable=outcome.retryable,
            )
        if type(outcome) is not ExportReport:
            raise TypeError("TM export returned an unknown outcome")
        if outcome.skipped_count or outcome.diagnostics:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_INCOMPLETE")
        try:
            payload = destination.read_bytes()
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED") from error
        digest = hashlib.sha256(payload).hexdigest()
        if (
            digest != outcome.destination_digest
            or outcome.exported_count != outcome.snapshot_receipt.record_count
            or outcome.snapshot_receipt_digest
            != outcome.snapshot_receipt_digest.lower()
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED")
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TM_JSONL_V1,
            payload_digest=digest,
            payload_byte_count=len(payload),
            record_count=outcome.exported_count,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=outcome.snapshot_receipt_digest,
            owner_receipt_digest=outcome.snapshot_receipt_digest,
            owner_generation=outcome.canonical_generation,
            owner_revision=outcome.exported_revision,
        )

    @staticmethod
    def companion_path(destination: Path) -> Path:
        return destination.with_name(f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}")

    def validate_snapshot(self, source: Path) -> PortableResourceSnapshot:
        """Run the Core-owned JSONL grammar preflight on a private payload."""

        if not isinstance(source, Path) or not source.is_absolute():
            raise TypeError("TM snapshot source must be an absolute Path")
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            "portable-validation",
            source,
        )
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id="store.portable-validation",
            platform_backend=self._backend,
        )
        try:
            preflight = service.preflight(source)
        except (MigrationPreflightError, OSError, UnicodeError, ValueError) as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED") from error
        if preflight.invalid_count:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_INCOMPLETE")
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TM_JSONL_V1,
            payload_digest=preflight.source_digest,
            payload_byte_count=source.stat().st_size,
            record_count=preflight.valid_count,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=preflight.source_digest,
            owner_receipt_digest=None,
        )

    def reprove_snapshot(
        self,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        """Reject publication if the canonical TM advanced after capture."""

        if type(resource) is not ResourceConfig:
            raise TypeError("TM portable resource must be exact ResourceConfig")
        if type(snapshot) is not PortableResourceSnapshot:
            raise TypeError("TM portable snapshot must be exact")
        if (
            resource.kind is not ResourceKind.TRANSLATION_MEMORY
            or snapshot.kind is not PortableResourceKind.TRANSLATION_MEMORY
            or snapshot.profile is not ResourcePayloadProfile.TM_JSONL_V1
            or snapshot.owner_generation is None
            or snapshot.owner_revision is None
        ):
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        try:
            store = self._store(resource)
            if store is None or store.coordinator.resource_id != resource.id:
                raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")
            revision = store.canonical_revision()
        except ResourcePortabilityError:
            raise
        except (OSError, ValueError, SQLiteStoreLifecycleError) as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE") from error
        if (
            revision.generation != snapshot.owner_generation
            or revision.head_revision != snapshot.owner_revision
            or revision.record_count != snapshot.record_count
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")

    def destination_baseline(
        self,
        resource: ResourceConfig,
    ) -> tuple[str, int, int, int]:
        """Capture the canonical authority facts bound by an import preview."""

        if type(resource) is not ResourceConfig:
            raise TypeError("TM destination must be exact ResourceConfig")
        if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        try:
            store = self._store(resource)
            if store is None or store.coordinator.resource_id != resource.id:
                raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
            revision = store.canonical_revision()
            return (
                store.coordinator.canonical_store_id,
                revision.generation,
                revision.head_revision,
                revision.record_count,
            )
        except ResourcePortabilityError:
            raise
        except (OSError, ValueError, SQLiteStoreLifecycleError) as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE") from error

    def reprove_destination_baseline(
        self,
        resource: ResourceConfig,
        baseline: tuple[str, int, int, int],
    ) -> None:
        if (
            type(baseline) is not tuple
            or len(baseline) != 4
            or type(baseline[0]) is not str
            or any(type(value) is not int for value in baseline[1:])
        ):
            raise TypeError("TM destination baseline must be exact")
        if self.destination_baseline(resource) != baseline:
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")

    def reopen_snapshot(
        self,
        resource: ResourceConfig,
        receipt: ResourceOperationReceipt,
    ) -> PortableResourceSnapshot:
        """Cold-reopen one published TM authority and reprove receipt facts."""

        if type(resource) is not ResourceConfig:
            raise TypeError("TM recovery resource must be exact ResourceConfig")
        if type(receipt) is not ResourceOperationReceipt:
            raise TypeError("TM recovery receipt must be exact")
        if (
            resource.kind is not ResourceKind.TRANSLATION_MEMORY
            or receipt.destination_resource_id != resource.id
            or receipt.resource_kind is not PortableResourceKind.TRANSLATION_MEMORY
            or receipt.payload_profile is not ResourcePayloadProfile.TM_JSONL_V1
        ):
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        # An ARMED template binds incoming bytes, not a published canonical
        # authority. Equal row counts cannot upgrade it into owner evidence.
        if (receipt.owner_generation is None or receipt.owner_revision is None
                or receipt.owner_receipt_digest is None):
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
        payload_root = None
        payload_authority = None
        try:
            payload_root = self._backend.bind_root(resource.path.parent)
            payload_authority = self._backend.open_regular(
                payload_root,
                PurePath(resource.path.name),
            )
            payload_facts = payload_authority.content_facts()
            coordinator = self.owner_for_resource(resource)
            if self._windows_runtime:
                TMMigrationService(
                    resource_identity=CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path),
                    canonical_store_id=coordinator.canonical_store_id,
                    coordinator=coordinator, platform_backend=self._backend,
                ).rehydrate_completed_portable_activation()
            else:
                coordinator.rehydrate_runtime_authority()
            store = SQLiteTMStore.from_coordinator(coordinator)
            if store is None or store.coordinator.resource_id != resource.id:
                raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
            revision = store.canonical_revision()
        except ResourcePortabilityError:
            raise
        except (PlatformFileError, OSError, ValueError, SQLiteStoreLifecycleError,
                ActivationPreparationError, MigrationPreflightError) as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED") from error
        finally:
            close_error: PlatformFileError | OSError | None = None
            for authority in (payload_authority, payload_root):
                if authority is None:
                    continue
                try:
                    authority.close()
                except (PlatformFileError, OSError) as error:
                    if close_error is None:
                        close_error = error
            if close_error is not None:
                raise ResourcePortabilityError(
                    "RESOURCE.IMPORT.COLD_REOPEN_FAILED"
                ) from close_error
        if (
            payload_facts.content_sha256.hex() != receipt.payload_digest
            or revision.record_count != receipt.record_count
            or (
                receipt.owner_generation is not None
                and revision.generation != receipt.owner_generation
            )
            or (
                receipt.owner_revision is not None
                and revision.head_revision != receipt.owner_revision
            )
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TM_JSONL_V1,
            payload_digest=receipt.payload_digest,
            payload_byte_count=payload_facts.snapshot.byte_count,
            record_count=receipt.record_count,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=receipt.source_baseline_digest,
            owner_receipt_digest=receipt.owner_receipt_digest,
            owner_generation=revision.generation,
            owner_revision=revision.head_revision,
        )

    def replace_snapshot(
        self,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
        *,
        expected_destination_baseline: tuple[str, int, int, int] | None = None,
        expected_destination_digest: str | None = None,
        owner_prepare: Callable[[], None] | None = None,
        owner_commit: Callable[[PortableResourceSnapshot], None] | None = None,
        owner_finalize: Callable[[PortableResourceSnapshot], None] | None = None,
    ) -> PortableResourceSnapshot:
        """Replace through the original owner, checking preview facts under W1."""
        _validate_apply_inputs(resource, payload, validated)
        coordinator = self.owner_for_resource(resource)
        # Standalone port callers have no sealed Resource preview. Bind
        # their before facts here; Core rechecks the canonical baseline
        # under W1, and a normal failure must return this source digest.
        if expected_destination_baseline is None:
            expected_destination_baseline = self.destination_baseline(resource)
        if expected_destination_digest is None:
            try:
                with open_rooted_regular(self._backend, resource.path) as source:
                    expected_destination_digest = source.content_facts().content_sha256.hex()
            except (PlatformFileError, OSError) as error:
                raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE") from error
        return self._apply_snapshot(
            resource, payload, validated, coordinator, initial=False,
            expected_destination_baseline=expected_destination_baseline,
            expected_destination_digest=expected_destination_digest,
            owner_prepare=owner_prepare, owner_commit=owner_commit,
            owner_finalize=owner_finalize,
        )

    def create_snapshot(
        self,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
        *,
        owner_prepare: Callable[[], None] | None = None,
        owner_commit: Callable[[PortableResourceSnapshot], None] | None = None,
        owner_finalize: Callable[[PortableResourceSnapshot], None] | None = None,
    ) -> PortableResourceSnapshot:
        """Issue a first owner only for a newly allocated local identity."""
        _validate_apply_inputs(resource, payload, validated)
        if (resource.path.exists() or resource.id in self._owners
                or resource.id in self._open_failures):
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        coordinator = ResourceStoreCoordinator(
            canonical_store_id=f"store.{resource.id}", resource_identity=identity,
        )
        self._owners[resource.id] = (resource.path, coordinator)
        return self._apply_snapshot(
            resource, payload, validated, coordinator, initial=True,
            owner_prepare=owner_prepare, owner_commit=owner_commit,
            owner_finalize=owner_finalize,
        )

    def _apply_snapshot(
        self,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
        coordinator: ResourceStoreCoordinator,
        *,
        initial: bool,
        expected_destination_baseline: tuple[str, int, int, int] | None = None,
        expected_destination_digest: str | None = None,
        owner_prepare: Callable[[], None] | None,
        owner_commit: Callable[[PortableResourceSnapshot], None] | None,
        owner_finalize: Callable[[PortableResourceSnapshot], None] | None,
    ) -> PortableResourceSnapshot:
        service = TMMigrationService(
            resource_identity=CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path),
            canonical_store_id=coordinator.canonical_store_id,
            coordinator=coordinator, platform_backend=self._backend,
        )
        committed: PortableResourceSnapshot | None = None
        finalized: PortableResourceSnapshot | None = None

        def commit_before_terminal(report: MigrationReport) -> None:
            nonlocal committed
            try:
                facts = service.project_applied_owner(
                    report, expected_digest=validated.payload_digest,
                    expected_record_count=validated.record_count,
                )
            except ActivationPreparationError as error:
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED") from error
            committed = _project_apply_outcome(resource, validated, report, owner_facts=facts)
            if owner_commit is not None:
                owner_commit(committed)

        def finalize_under_owner(report: MigrationReport, proof: AppliedSnapshotProof) -> None:
            nonlocal finalized
            # Core has independently compared real canonical rows and configured
            # payload draft semantics. The semantic digest is not the byte digest
            # carried by the Resource receipt, and must never replace it.
            if (type(proof) is not AppliedSnapshotProof
                    or proof.resource_id != resource.id
                    or proof.canonical_store_id != coordinator.canonical_store_id
                    or proof.canonical_store_id != report.canonical_store_id
                    or proof.payload_digest != validated.payload_digest
                    or proof.payload_byte_count != validated.payload_byte_count
                    or proof.record_count != validated.record_count
                    or type(proof.semantic_digest) is not str
                    or len(proof.semantic_digest) != 64
                    or any(c not in "0123456789abcdef" for c in proof.semantic_digest)):
                raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
            finalized = _project_apply_outcome(
                resource, validated, report,
                owner_facts=(proof.generation, proof.head_revision,
                             proof.record_count, proof.payload_byte_count),
            )
            if finalized != committed:
                raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
            if owner_finalize is not None:
                owner_finalize(finalized)

        try:
            outcome = service.apply_snapshot_payload(
                payload, resource.id, expected_digest=validated.payload_digest,
                expected_record_count=validated.record_count, initial=initial,
                expected_destination_baseline=expected_destination_baseline,
                owner_prepare=owner_prepare,
                owner_commit=commit_before_terminal,
                owner_finalize=finalize_under_owner,
            )
        except ResourcePortabilityError as error:
            if error.code == "RESOURCE.IMPORT.APPLY_FAILED":
                # A callback exception cannot stand in for a returned,
                # preservation-checked MigrationFailure below.
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED") from error
            raise
        except MigrationPreflightError as error:
            code = ("RESOURCE.IMPORT.DESTINATION_STALE"
                    if error.error_code == "IMPORT.DESTINATION_STALE"
                    else "RESOURCE.IMPORT.RECOVERY_REQUIRED")
            raise ResourcePortabilityError(code, retryable=True) from error
        except (PlatformFileError, OSError, SQLiteStoreLifecycleError, ActivationPreparationError) as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED", retryable=True) from error
        if type(outcome) is MigrationReport:
            if finalized is None:
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
            return finalized
        return _project_apply_outcome(
            resource, validated, outcome, initial=initial,
            expected_destination_digest=expected_destination_digest,
            expected_destination_baseline=expected_destination_baseline,
        )


__all__ = ["TMResourceSnapshotPort"]


def _validate_apply_inputs(
    resource: ResourceConfig,
    payload: Path,
    validated: PortableResourceSnapshot,
) -> None:
    if type(resource) is not ResourceConfig:
        raise TypeError("TM apply resource must be exact ResourceConfig")
    if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
    if not isinstance(payload, Path) or not payload.is_absolute():
        raise TypeError("TM apply payload must be an absolute Path")
    if type(validated) is not PortableResourceSnapshot:
        raise TypeError("TM apply validation must be exact snapshot")
    if (
        validated.kind is not PortableResourceKind.TRANSLATION_MEMORY
        or validated.profile is not ResourcePayloadProfile.TM_JSONL_V1
    ):
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")


def _project_apply_outcome(
    resource: ResourceConfig,
    validated: PortableResourceSnapshot,
    outcome: object,
    *,
    owner_facts: tuple[int, int, int, int] | None = None,
    initial: bool = False,
    expected_destination_digest: str | None = None,
    expected_destination_baseline: tuple[str, int, int, int] | None = None,
) -> PortableResourceSnapshot:
    if type(outcome) is MigrationFailure:
        if (
            outcome.canonical_authority_published
            or outcome.canonical_authority_ambiguous
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
        source = outcome.original_source_preservation
        active = outcome.active_store_preservation
        # This projection is reached only after the synchronous public
        # apply returned, including all owned-input/root/W1 closes. A
        # private outcome attached to an exception is never consumed.
        source_before = validated.payload_digest if initial else expected_destination_digest
        source_preserved = (
            source.state is AssetPreservationState.VERIFIED_UNCHANGED
            and source_before is not None
            and source.before_digest == source_before
            and source.observed_digest == source_before
        )
        if initial:
            # First activation has no prior canonical. Core's source
            # evidence refers to the incoming payload; its normal return
            # also closes removal of the newly allocated source.
            active_preserved = (
                outcome.active_generation is None
                and active.state is AssetPreservationState.NOT_APPLICABLE
                and active.before_digest is None
                and active.observed_digest is None
            )
        else:
            active_preserved = (
                expected_destination_baseline is not None
                and outcome.active_generation == expected_destination_baseline[1]
                and active.state is AssetPreservationState.VERIFIED_UNCHANGED
                and active.before_digest is not None
                and active.observed_digest == active.before_digest
            )
        if not source_preserved or not active_preserved or outcome.recovery_locators:
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
        raise ResourcePortabilityError(
            "RESOURCE.IMPORT.APPLY_FAILED",
            retryable=outcome.retryable,
        )
    if type(outcome) is not MigrationReport:
        raise TypeError("TM import returned an unknown outcome")
    if (
        outcome.resource_id != resource.id
        or outcome.source_digest != validated.payload_digest
        or outcome.migrated_count != validated.record_count
        or outcome.skipped_count
    ):
        raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
    if owner_facts is not None:
        if (
            type(owner_facts) is not tuple
            or len(owner_facts) != 4
            or any(type(value) is not int for value in owner_facts)
        ):
            raise TypeError("owner projection facts are invalid")
        generation, head_revision, record_count, payload_byte_count = owner_facts
    else:
        raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
    if (
        generation != outcome.activated_generation
        or head_revision != outcome.snapshot_receipt.exported_revision
        or record_count != outcome.migrated_count
    ):
        raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
    return PortableResourceSnapshot(
        kind=PortableResourceKind.TRANSLATION_MEMORY,
        profile=ResourcePayloadProfile.TM_JSONL_V1,
        payload_digest=outcome.source_digest,
        payload_byte_count=payload_byte_count,
        record_count=outcome.migrated_count,
        legacy_record_count=0,
        v1_record_count=0,
        source_baseline_digest=outcome.source_digest,
        owner_receipt_digest=snapshot_receipt_digest(outcome.snapshot_receipt),
        owner_generation=outcome.activated_generation,
        owner_revision=outcome.snapshot_receipt.exported_revision,
    )
