"""Narrow portable-snapshot adapter over the canonical TM owner."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from uuid import uuid4

from editor_contracts import ResourceConfig, ResourceKind
from resource_package_contracts import (
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourceOperationReceipt,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    ExportFailure,
    ExportReport,
    MigrationFailure,
    MigrationReport,
    snapshot_receipt_digest,
)
from tm_engine import TMEngine
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


_EXPORT_MANIFEST_SUFFIX = ".localcat-snapshot.json"


class TMResourceSnapshotPort:
    """Call the existing Core export transaction without re-encoding JSONL."""

    def export_snapshot(
        self,
        resource: ResourceConfig,
        destination: Path,
    ) -> PortableResourceSnapshot:
        if type(resource) is not ResourceConfig:
            raise TypeError("TM portable resource must be exact ResourceConfig")
        resource.__post_init__()
        if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        engine = TMEngine(str(resource.path))
        store = engine.canonical_store
        if store is None:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE")
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
        )
        outcome = service.export_jsonl(store, destination)
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
            store = TMEngine(str(resource.path)).canonical_store
            if store is None or store.coordinator.resource_id != resource.id:
                raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")
            revision = store.canonical_revision()
        except ResourcePortabilityError:
            raise
        except (OSError, ValueError) as error:
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
            store = TMEngine(str(resource.path)).canonical_store
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
        except (OSError, ValueError) as error:
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
        validated = self.validate_snapshot(resource.path)
        try:
            store = TMEngine(str(resource.path)).canonical_store
            if store is None or store.coordinator.resource_id != resource.id:
                raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
            revision = store.canonical_revision()
        except ResourcePortabilityError:
            raise
        except (OSError, ValueError) as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED") from error
        if (
            validated.payload_digest != receipt.payload_digest
            or validated.record_count != receipt.record_count
            or (
                receipt.owner_generation is not None
                and revision.generation != receipt.owner_generation
            )
            or (
                receipt.owner_revision is not None
                and revision.head_revision != receipt.owner_revision
            )
            or revision.record_count != receipt.record_count
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
        return PortableResourceSnapshot(
            kind=validated.kind,
            profile=validated.profile,
            payload_digest=validated.payload_digest,
            payload_byte_count=validated.payload_byte_count,
            record_count=validated.record_count,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=validated.source_baseline_digest,
            owner_receipt_digest=receipt.owner_receipt_digest,
            owner_generation=revision.generation,
            owner_revision=revision.head_revision,
        )

    def replace_snapshot(
        self,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
    ) -> PortableResourceSnapshot:
        """Replace one configured JSONL then drive Core's full generation swap."""

        _validate_apply_inputs(resource, payload, validated)
        engine = TMEngine(str(resource.path))
        store = engine.canonical_store
        if store is None:
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
        coordinator = store.coordinator
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource.id,
            resource.path,
        )
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=coordinator.canonical_store_id,
            coordinator=coordinator,
        )
        return self._publish_and_activate(
            resource=resource,
            payload=payload,
            validated=validated,
            service=service,
            initial=False,
        )

    def create_snapshot(
        self,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
    ) -> PortableResourceSnapshot:
        """Publish one new configured JSONL and its first canonical generation."""

        _validate_apply_inputs(resource, payload, validated)
        if resource.path.exists():
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource.id,
            resource.path,
        )
        canonical_store_id = f"store.{resource.id}"
        coordinator = ResourceStoreCoordinator(
            canonical_store_id=canonical_store_id,
            resource_identity=identity,
        )
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=canonical_store_id,
            coordinator=coordinator,
        )
        return self._publish_and_activate(
            resource=resource,
            payload=payload,
            validated=validated,
            service=service,
            initial=True,
        )

    def _publish_and_activate(
        self,
        *,
        resource: ResourceConfig,
        payload: Path,
        validated: PortableResourceSnapshot,
        service: TMMigrationService,
        initial: bool,
    ) -> PortableResourceSnapshot:
        original = None if initial else _backup_file(resource.path)
        manifest = resource.path.with_name(
            f"{resource.path.name}.localcat-snapshot.json"
        )
        original_manifest = _backup_file(manifest) if manifest.exists() else None
        candidate = resource.path.with_name(
            f".{resource.path.name}.{uuid4().hex}.portable.tmp"
        )
        retain_recovery = False
        try:
            _copy_new_file(payload, candidate)
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != validated.payload_digest:
                raise ResourcePortabilityError("RESOURCE.IMPORT.SOURCE_STALE")
            os.replace(candidate, resource.path)
            _fsync_directory(resource.path.parent)
            outcome = (
                service.activate_initial(resource.path, resource.id)
                if initial
                else service.import_snapshot(resource.path, resource.id)
            )
            if type(outcome) is MigrationFailure:
                if outcome.canonical_authority_published or outcome.canonical_authority_ambiguous:
                    retain_recovery = True
                    raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
                _restore_file(original, resource.path)
                _restore_file(original_manifest, manifest)
                raise ResourcePortabilityError(
                    "RESOURCE.IMPORT.APPLY_FAILED",
                    retryable=outcome.retryable,
                )
            if type(outcome) is not MigrationReport:
                raise TypeError("TM import returned an unknown outcome")
            if (
                outcome.source_digest != validated.payload_digest
                or outcome.migrated_count != validated.record_count
                or outcome.skipped_count
            ):
                retain_recovery = True
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
            reopened = TMEngine(str(resource.path)).canonical_store
            if (
                reopened is None
                or reopened.coordinator.resource_id != resource.id
                or reopened.coordinator.current_generation != outcome.activated_generation
            ):
                retain_recovery = True
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
            return PortableResourceSnapshot(
                kind=PortableResourceKind.TRANSLATION_MEMORY,
                profile=ResourcePayloadProfile.TM_JSONL_V1,
                payload_digest=outcome.source_digest,
                payload_byte_count=resource.path.stat().st_size,
                record_count=outcome.migrated_count,
                legacy_record_count=0,
                v1_record_count=0,
                source_baseline_digest=outcome.source_digest,
                owner_receipt_digest=snapshot_receipt_digest(outcome.snapshot_receipt),
                owner_generation=outcome.activated_generation,
                owner_revision=outcome.snapshot_receipt.exported_revision,
            )
        except ResourcePortabilityError:
            raise
        except BaseException:
            retain_recovery = True
            raise
        finally:
            candidate.unlink(missing_ok=True)
            if original is not None and not retain_recovery:
                original.unlink(missing_ok=True)
            if original_manifest is not None and not retain_recovery:
                original_manifest.unlink(missing_ok=True)


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


def _copy_new_file(source: Path, destination: Path) -> None:
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    _fsync_directory(destination.parent)


def _backup_file(source: Path) -> Path:
    destination = source.with_name(f".{source.name}.{uuid4().hex}.portable.lkg")
    _copy_new_file(source, destination)
    return destination


def _restore_file(backup: Path | None, destination: Path) -> None:
    if backup is None:
        destination.unlink(missing_ok=True)
    else:
        os.replace(backup, destination)
    _fsync_directory(destination.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
