"""Application orchestration for direct snapshots and ResourcePackage export."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import tempfile
from typing import Callable
from uuid import uuid4

from editor_contracts import ResourceConfig, ResourceKind, TermCommitState
from platform_fs_contracts import (
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    PendingPublication,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
)
from resource_artifact_save import (
    ResourceArtifactPublication,
    ResourceArtifactSaveService,
)
from resource_package import (
    SealedResourcePackage,
    open_resource_package,
    write_resource_package,
)
from resource_package_contracts import (
    RECEIPT_SCHEMA,
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourceDurableState,
    ResourceExportOutcome,
    ResourceImportMode,
    ResourceOperationKind,
    ResourceOperationReceipt,
    ResourcePackageImportPreview,
    ResourcePackageImportResult,
    ResourcePackageManifest,
    ResourcePackageSourceScope,
    ResourcePackageValidationReport,
    ResourcePayloadDescriptor,
    ResourcePayloadProfile,
    ResourcePortabilityError,
    ResourceProfileCounts,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
    ResourceRecoveryOutcome,
    ResourceRecoveryPreview,
    package_profile_triple_for_payload,
    payload_path_for_profile,
    profile_for_kind,
    resource_package_capability,
)
from resource_payload_port import ResourcePackagePayloadHandler
from resource_receipt_ledger import (
    ResourcePendingOperation,
    ResourcePendingPhase,
    ResourceReceiptLedger,
)
from resource_repository import ResourceError, ResourceRepository
from resource_platform_io import (
    bind_rooted_regular,
    digest_bound,
    iter_bound_chunks,
    platform_relative_path,
)
from termbase_store import TermbasePortableSnapshot, TermbaseStore
from tm_resource_port import TMResourceSnapshotPort


@dataclass(slots=True)
class _PreparedResourceImport:
    preview: ResourcePackageImportPreview
    sealed: SealedResourcePackage
    registry_baseline: _RepositoryBaseline
    target: ResourceConfig | None
    target_digest: str | None
    target_snapshot: EntrySnapshot | None
    target_owner_baseline: tuple[str, int, int, int] | None
    new_resource_name: str | None


@dataclass(frozen=True, slots=True)
class _RepositoryBaseline:
    resources: tuple[ResourceConfig, ...]
    registry_digest: str
    registry_snapshot: EntrySnapshot


class ResourcePortabilityService:
    """Coordinate resource-owner snapshots with artifact publication."""

    def __init__(
        self,
        repository: ResourceRepository,
        *,
        termbase_store: TermbaseStore | None = None,
        tm_port: TMResourceSnapshotPort | None = None,
        ledger: ResourceReceiptLedger | None = None,
        artifact_save: ResourceArtifactSaveService | None = None,
        tmx_payload_handler: ResourcePackagePayloadHandler | None = None,
    ) -> None:
        if type(repository) is not ResourceRepository:
            raise TypeError("resource portability repository must be exact")
        self.repository = repository
        self._termbase = termbase_store or TermbaseStore(repository.platform_backend)
        self._tm = tm_port or TMResourceSnapshotPort(repository.platform_backend)
        self._ledger = ledger or ResourceReceiptLedger(
            repository.config_dir,
            repository.platform_backend,
        )
        self._artifact_save = artifact_save or ResourceArtifactSaveService(
            repository.platform_backend
        )
        if tmx_payload_handler is not None:
            if not isinstance(tmx_payload_handler, ResourcePackagePayloadHandler):
                raise TypeError("TMX package payload handler must satisfy its port")
            if (
                tmx_payload_handler.profile
                is not ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
            ):
                raise TypeError("TMX package payload handler profile must be exact")
        self._tmx_payload_handler = tmx_payload_handler
        self._prepared_imports: dict[str, _PreparedResourceImport] = {}
        self._recovery_previews: dict[str, ResourceRecoveryPreview] = {}

    @staticmethod
    def import_supported(report: ResourcePackageValidationReport) -> bool:
        """Return whether the validated payload profile has an apply contract."""

        if type(report) is not ResourcePackageValidationReport:
            raise TypeError("resource package validation report must be exact")
        return report.payload_profile is not ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1

    def export_direct(
        self,
        resource_id: str,
        destination: Path,
    ) -> ResourceExportOutcome:
        resource = self._resource(resource_id)
        destination = _absolute_destination(destination)
        operation_id = uuid4().hex
        before_digest = _optional_digest(self.repository.platform_backend, destination)
        if resource.kind is ResourceKind.TRANSLATION_MEMORY:
            snapshot = self._tm.export_snapshot(resource, destination)
            after_digest = _digest_path(self.repository.platform_backend, destination)
            if after_digest != snapshot.payload_digest:
                raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED")
            receipt = self._receipt(
                operation_id=operation_id,
                operation_kind=ResourceOperationKind.EXPORT_DIRECT,
                resource=resource,
                snapshot=snapshot,
                package_digest=None,
                destination_resource_id=None,
                destination_before_digest=before_digest,
                destination_after_digest=after_digest,
            )
            self._arm_after_owner_publication(receipt)
        else:
            candidate = _candidate_path(destination, "csv")
            term_snapshot: TermbasePortableSnapshot | None = None
            armed = False
            owner_phase_started = False
            receipt: ResourceOperationReceipt | None = None
            try:
                term_snapshot = self._termbase.export_portable_snapshot(
                    resource.path,
                    candidate,
                )
                snapshot = _portable_term_snapshot(term_snapshot)
                self._reprove_source(resource, snapshot)
                expected = self._receipt(
                    operation_id=operation_id,
                    operation_kind=ResourceOperationKind.EXPORT_DIRECT,
                    resource=resource,
                    snapshot=snapshot,
                    package_digest=None,
                    destination_resource_id=None,
                    destination_before_digest=before_digest,
                    destination_after_digest=snapshot.payload_digest,
                )
                self._ledger.begin(_recovery_receipt(expected))
                armed = True
                def commit_direct_owner(
                    publication: object,
                    cold: object,
                ) -> None:
                    nonlocal owner_phase_started, receipt
                    owner_phase_started = True
                    if not isinstance(publication, ResourceArtifactPublication):
                        raise TypeError("resource artifact publication must be exact")
                    if type(cold) is not TermbasePortableSnapshot:
                        raise TypeError("termbase cold validation must be exact")
                    if cold.payload_digest != snapshot.payload_digest:
                        raise ResourcePortabilityError(
                            "RESOURCE.EXPORT.VALIDATION_FAILED"
                        )
                    receipt = replace(
                        expected,
                        destination_before_digest=(
                            publication.destination_before_digest
                        ),
                        destination_after_digest=(
                            publication.destination_after_digest
                        ),
                    )
                    self._mark_pending_receipt_ready(receipt)

                self._artifact_save.publish(
                    candidate,
                    destination,
                    self._termbase.validate_portable_snapshot,
                    owner_commit=commit_direct_owner,
                )
                if receipt is None:
                    raise AssertionError("direct export owner commit did not issue receipt")
                self._commit_ready_receipt(receipt)
                armed = False
            except BaseException as error:
                if armed and not owner_phase_started:
                    self._resolve_failed_pending(operation_id, error)
                raise
            finally:
                if term_snapshot is not None:
                    try:
                        self._termbase.discard_portable_snapshot(
                            candidate,
                            term_snapshot,
                        )
                    except (PlatformFileError, ValueError):
                        pass
        return ResourceExportOutcome(receipt=receipt, destination_preserved=True)

    def export_package(
        self,
        resource_id: str,
        destination: Path,
        *,
        payload_profile: ResourcePayloadProfile | None = None,
    ) -> ResourceExportOutcome:
        resource = self._resource(resource_id)
        portable_kind = (
            PortableResourceKind.TRANSLATION_MEMORY
            if resource.kind is ResourceKind.TRANSLATION_MEMORY
            else PortableResourceKind.TERMBASE
        )
        profile = (
            profile_for_kind(portable_kind)
            if payload_profile is None
            else payload_profile
        )
        if type(profile) is not ResourcePayloadProfile:
            raise TypeError("package payload profile must be exact")
        if not resource_package_capability(
            ResourcePackageSourceScope.MANAGED_RESOURCE,
            portable_kind,
            profile,
            importing=False,
        ):
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.PROFILE_UNSUPPORTED")
        destination = _absolute_destination(destination)
        operation_id = uuid4().hex
        payload = _candidate_path(
            destination,
            (
                "tmx"
                if profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
                else (
                    "jsonl"
                    if resource.kind is ResourceKind.TRANSLATION_MEMORY
                    else "csv"
                )
            ),
        )
        package_candidate = _candidate_path(destination, "resource-package")
        package_candidate_snapshot: EntrySnapshot | None = None
        companion: Path | None = None
        term_snapshot: TermbasePortableSnapshot | None = None
        armed = False
        owner_phase_started = False
        receipt: ResourceOperationReceipt | None = None
        try:
            if profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
                handler = self._require_tmx_payload_handler()
                snapshot = handler.export_snapshot(resource, payload)
                self._validate_handler_snapshot(snapshot, payload)
            elif resource.kind is ResourceKind.TRANSLATION_MEMORY:
                snapshot = self._tm.export_snapshot(resource, payload)
                companion = self._tm.companion_path(payload)
            else:
                term_snapshot = self._termbase.export_portable_snapshot(
                    resource.path,
                    payload,
                )
                snapshot = _portable_term_snapshot(term_snapshot)
            self._reprove_source(resource, snapshot)
            manifest = _manifest_for_snapshot(snapshot)
            write_resource_package(
                package_candidate,
                manifest,
                payload,
                backend=self.repository.platform_backend,
            )
            package_candidate_snapshot = _file_snapshot(
                self.repository.platform_backend,
                package_candidate,
            )
            candidate_report = self.validate_resource_package(package_candidate)
            expected = self._receipt(
                operation_id=operation_id,
                operation_kind=ResourceOperationKind.EXPORT_PACKAGE,
                resource=resource,
                snapshot=snapshot,
                package_digest=candidate_report.artifact_digest,
                destination_resource_id=None,
                destination_before_digest=_optional_digest(
                    self.repository.platform_backend,
                    destination,
                ),
                destination_after_digest=candidate_report.artifact_digest,
            )
            self._ledger.begin(_recovery_receipt(expected))
            armed = True
            def commit_package_owner(
                publication: object,
                carrier_report: object,
            ) -> None:
                nonlocal owner_phase_started, receipt
                owner_phase_started = True
                if not isinstance(publication, ResourceArtifactPublication):
                    raise TypeError("resource artifact publication must be exact")
                if type(carrier_report) is not ResourcePackageValidationReport:
                    raise TypeError("package cold validation must be exact")
                if carrier_report != candidate_report:
                    raise ResourcePortabilityError(
                        "RESOURCE.EXPORT.VALIDATION_FAILED"
                    )
                if (
                    carrier_report.artifact_digest
                    != publication.destination_after_digest
                    or carrier_report.payload_digest != snapshot.payload_digest
                    or carrier_report.record_count != snapshot.record_count
                    or carrier_report.safe_issues != snapshot.safe_issues
                ):
                    raise ResourcePortabilityError(
                        "RESOURCE.EXPORT.VALIDATION_FAILED"
                    )
                receipt = self._receipt(
                    operation_id=operation_id,
                    operation_kind=ResourceOperationKind.EXPORT_PACKAGE,
                    resource=resource,
                    snapshot=snapshot,
                    package_digest=carrier_report.artifact_digest,
                    destination_resource_id=None,
                    destination_before_digest=(
                        publication.destination_before_digest
                    ),
                    destination_after_digest=carrier_report.artifact_digest,
                )
                self._mark_pending_receipt_ready(receipt)

            self._artifact_save.publish(
                package_candidate,
                destination,
                self.validate_resource_package,
                owner_commit=commit_package_owner,
            )
            if receipt is None:
                raise AssertionError("package export owner commit did not issue receipt")
            self._commit_ready_receipt(receipt)
            armed = False
            return ResourceExportOutcome(receipt=receipt, destination_preserved=True)
        except BaseException as error:
            if armed and not owner_phase_started:
                self._resolve_failed_pending(operation_id, error)
            raise
        finally:
            if term_snapshot is not None:
                try:
                    self._termbase.discard_portable_snapshot(payload, term_snapshot)
                except (PlatformFileError, ValueError):
                    pass
            if package_candidate_snapshot is not None:
                try:
                    _unlink_snapshot(
                        self.repository.platform_backend,
                        package_candidate,
                        package_candidate_snapshot,
                    )
                except PlatformFileError:
                    pass
            if companion is not None:
                companion.unlink(missing_ok=True)

    def validate_resource_package(
        self,
        source: Path,
    ) -> ResourcePackageValidationReport:
        source = _absolute_destination(source)
        with open_resource_package(
            source,
            backend=self.repository.platform_backend,
        ) as sealed:
            report, _owner = self._validate_sealed(sealed)
            return report

    def preview_resource_package_import(
        self,
        source: Path,
        mode: ResourceImportMode,
        *,
        destination_resource_id: str | None = None,
        new_resource_name: str | None = None,
    ) -> ResourcePackageImportPreview:
        if type(mode) is not ResourceImportMode:
            raise TypeError("resource import mode must be exact")
        source = _absolute_destination(source)
        sealed = open_resource_package(
            source,
            backend=self.repository.platform_backend,
        )
        try:
            if not resource_package_capability(
                ResourcePackageSourceScope.MANAGED_RESOURCE,
                sealed.validation.resource_kind,
                sealed.validation.payload_profile,
                importing=True,
            ):
                raise ResourcePortabilityError(
                    "RESOURCE.IMPORT.PROFILE_UNSUPPORTED"
                )
            report, _owner = self._validate_sealed(sealed)
            target: ResourceConfig | None = None
            target_digest: str | None = None
            target_snapshot: EntrySnapshot | None = None
            target_owner_baseline: tuple[str, int, int, int] | None = None
            if mode is ResourceImportMode.REPLACE_SELECTED:
                if type(destination_resource_id) is not str or not destination_resource_id:
                    raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_INVALID")
                target = self._resource(destination_resource_id)
                expected_kind = _editor_kind(report.resource_kind)
                if target.kind is not expected_kind:
                    raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
                target_digest = _digest_path(
                    self.repository.platform_backend,
                    target.path,
                )
                target_snapshot = _file_snapshot(
                    self.repository.platform_backend,
                    target.path,
                )
                if target.kind is ResourceKind.TRANSLATION_MEMORY:
                    target_owner_baseline = self._tm.destination_baseline(target)
                destination_exists = True
                public_destination = target.id
                resolved_name = None
            else:
                if destination_resource_id is not None:
                    raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_INVALID")
                resolved_name = (
                    new_resource_name.strip()
                    if type(new_resource_name) is str and new_resource_name.strip()
                    else (
                        "Imported translation memory"
                        if report.resource_kind is PortableResourceKind.TRANSLATION_MEMORY
                        else "Imported termbase"
                    )
                )
                destination_exists = False
                public_destination = None
            operation_id = uuid4().hex
            preview = ResourcePackageImportPreview(
                operation_id=operation_id,
                mode=mode,
                validation=report,
                destination_exists=destination_exists,
                destination_resource_id=public_destination,
                safe_warnings=(),
                blocking_reasons=(),
            )
            self._prepared_imports[operation_id] = _PreparedResourceImport(
                preview=preview,
                sealed=sealed,
                registry_baseline=_repository_baseline(self.repository),
                target=target,
                target_digest=target_digest,
                target_snapshot=target_snapshot,
                target_owner_baseline=target_owner_baseline,
                new_resource_name=resolved_name,
            )
            return preview
        except BaseException:
            sealed.close()
            raise

    def cancel_resource_package_import(
        self,
        preview: ResourcePackageImportPreview,
    ) -> None:
        plan = self._take_import(preview)
        plan.sealed.close()

    def inspect_resource_portability_recovery(
        self,
    ) -> tuple[ResourceRecoveryPreview, ...]:
        """Project cold pending facts without mutating a resource or journal."""

        previews: list[ResourceRecoveryPreview] = []
        self._recovery_previews.clear()
        for pending in self._ledger.list_pending():
            preview = self._recovery_preview(pending)
            previews.append(preview)
            self._recovery_previews[pending.receipt.operation_id] = preview
        return tuple(previews)

    def recover_resource_portability(
        self,
        preview: ResourceRecoveryPreview,
        action: ResourceRecoveryAction,
    ) -> ResourceRecoveryOutcome:
        """Consume one exact cold-recovery projection with fresh-state proof."""

        if type(preview) is not ResourceRecoveryPreview:
            raise TypeError("resource recovery preview must be exact")
        if type(action) is not ResourceRecoveryAction:
            raise TypeError("resource recovery action must be exact")
        issued = self._recovery_previews.pop(preview.operation_id, None)
        if issued is not preview:
            if issued is not None:
                self._recovery_previews[preview.operation_id] = issued
            raise ResourcePortabilityError("RESOURCE.RECOVERY.PREVIEW_STALE")
        pending = self._ledger.get_pending(preview.operation_id)
        current = self._recovery_preview(pending)
        if current != preview:
            raise ResourcePortabilityError("RESOURCE.RECOVERY.PREVIEW_STALE")
        if action is ResourceRecoveryAction.COMPLETE:
            if preview.disposition is not ResourceRecoveryDisposition.COMPLETE_AVAILABLE:
                raise ResourcePortabilityError("RESOURCE.RECOVERY.DECISION_INVALID")
            receipt = self._complete_recovery(pending)
            return ResourceRecoveryOutcome(
                operation_id=preview.operation_id,
                action=action,
                receipt=receipt,
            )
        if preview.disposition is not ResourceRecoveryDisposition.ROLLBACK_AVAILABLE:
            raise ResourcePortabilityError("RESOURCE.RECOVERY.DECISION_INVALID")
        self._ledger.abandon(preview.operation_id)
        return ResourceRecoveryOutcome(
            operation_id=preview.operation_id,
            action=action,
            receipt=None,
        )

    def retain_runtime_recovery(
        self,
        receipt: ResourceOperationReceipt,
    ) -> None:
        """Re-arm an applied import when the Controller runtime reload fails."""

        if (
            type(receipt) is not ResourceOperationReceipt
            or receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE
            or receipt.durable_state is not ResourceDurableState.COMMITTED
        ):
            raise TypeError("runtime recovery requires a committed import receipt")
        try:
            self._ledger.begin(
                _recovery_receipt(receipt),
                import_mode=ResourceImportMode.REPLACE_SELECTED,
            )
        except ResourcePortabilityError as error:
            raise ResourcePortabilityError(
                "RESOURCE.IMPORT.RECOVERY_REQUIRED"
            ) from error

    def apply_resource_package_import(
        self,
        preview: ResourcePackageImportPreview,
    ) -> ResourcePackageImportResult:
        plan = self._take_import(preview)
        sealed = plan.sealed
        pending_started = False
        owner_published = False
        prepared_create = None
        try:
            sealed.reprove()
            if _repository_baseline(self.repository) != plan.registry_baseline:
                raise ResourcePortabilityError("RESOURCE.IMPORT.PREVIEW_STALE")
            if plan.target is not None:
                current = self.repository.get(plan.target.id)
                if (
                    current != plan.target
                    or _file_snapshot(
                        self.repository.platform_backend,
                        current.path,
                    )
                    != plan.target_snapshot
                    or _digest_path(
                        self.repository.platform_backend,
                        current.path,
                    )
                    != plan.target_digest
                ):
                    raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
                if plan.target_owner_baseline is not None:
                    self._tm.reprove_destination_baseline(
                        current,
                        plan.target_owner_baseline,
                    )
            report, owner = self._validate_sealed(sealed)
            with tempfile.TemporaryDirectory(
                dir=self.repository.config_dir,
                prefix=".resource-package-apply-",
            ) as raw:
                payload = Path(raw) / (
                    "payload.jsonl"
                    if report.resource_kind is PortableResourceKind.TRANSLATION_MEMORY
                    else "payload.csv"
                )
                sealed.copy_payload_to(payload)
                if preview.mode is ResourceImportMode.CREATE_NEW:
                    name = plan.new_resource_name
                    if name is None:
                        raise AssertionError("create import lost its resource name")
                    prepared_create = self.repository.prepare_resource_create(
                        name,
                        _editor_kind(report.resource_kind),
                    )
                    destination = prepared_create.resource
                    before_digest = None
                else:
                    destination = plan.target
                    if destination is None or plan.target_digest is None:
                        raise AssertionError("replace import lost its destination")
                    before_digest = plan.target_digest
                template = _import_receipt(
                    operation_id=preview.operation_id,
                    report=report,
                    destination=destination,
                    before_digest=before_digest,
                    snapshot=owner,
                    durable_state=ResourceDurableState.RECOVERY_REQUIRED,
                )
                if preview.mode is ResourceImportMode.CREATE_NEW:
                    relative_path = destination.path.relative_to(
                        self.repository.managed_dir
                    ).as_posix()
                    self._ledger.begin(
                        template,
                        import_mode=preview.mode,
                        destination_name=destination.name,
                        destination_relative_path=relative_path,
                    )
                else:
                    self._ledger.begin(template, import_mode=preview.mode)
                pending_started = True
                receipt: ResourceOperationReceipt | None = None
                if prepared_create is not None:
                    def commit_created_resource(
                        applied_snapshot: PortableResourceSnapshot,
                    ) -> None:
                        nonlocal owner_published, receipt
                        receipt = _import_receipt(
                            operation_id=preview.operation_id,
                            report=report,
                            destination=destination,
                            before_digest=before_digest,
                            snapshot=applied_snapshot,
                            durable_state=ResourceDurableState.COMMITTED,
                        )
                        self._mark_pending_receipt_ready(receipt)
                        owner_published = True
                        self.repository.publish_prepared_create(prepared_create)

                    applied = self._apply_owner_snapshot(
                        destination,
                        payload,
                        owner,
                        create=True,
                        owner_commit=commit_created_resource,
                    )
                    if receipt is None:
                        raise AssertionError(
                            "created resource owner commit did not issue receipt"
                        )
                    self._commit_ready_receipt(receipt)
                    pending_started = False
                else:
                    def commit_replaced_resource(
                        applied_snapshot: PortableResourceSnapshot,
                    ) -> None:
                        nonlocal owner_published, receipt
                        receipt = _import_receipt(
                            operation_id=preview.operation_id,
                            report=report,
                            destination=destination,
                            before_digest=before_digest,
                            snapshot=applied_snapshot,
                            durable_state=ResourceDurableState.COMMITTED,
                        )
                        self._mark_pending_receipt_ready(receipt)
                        owner_published = True

                    applied = self._apply_owner_snapshot(
                        destination,
                        payload,
                        owner,
                        create=False,
                        owner_commit=(
                            commit_replaced_resource
                            if destination.kind is ResourceKind.TRANSLATION_MEMORY
                            else None
                        ),
                    )
                    if receipt is None and destination.kind is ResourceKind.TRANSLATION_MEMORY:
                        raise AssertionError(
                            "replaced resource owner commit did not issue receipt"
                        )
                    if destination.kind is not ResourceKind.TRANSLATION_MEMORY:
                        owner_published = True
                        receipt = _import_receipt(
                            operation_id=preview.operation_id,
                            report=report,
                            destination=destination,
                            before_digest=before_digest,
                            snapshot=applied,
                            durable_state=ResourceDurableState.COMMITTED,
                        )
                        self._mark_pending_receipt_ready(receipt)
                    self._commit_ready_receipt(receipt)
                    pending_started = False
            return ResourcePackageImportResult(
                receipt=receipt,
                destination_resource_id=destination.id,
            )
        except BaseException as error:
            if prepared_create is not None and not owner_published:
                try:
                    self.repository.cancel_prepared_create(
                        prepared_create,
                        remove_owned_file=False,
                    )
                except ResourceError:
                    pass
            if pending_started:
                if owner_published or _is_recovery_required(error):
                    raise ResourcePortabilityError(
                        "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                    ) from error
                self._abandon_pending_or_recovery(preview.operation_id, import_operation=True)
            raise
        finally:
            sealed.close()

    def _take_import(
        self,
        preview: ResourcePackageImportPreview,
    ) -> _PreparedResourceImport:
        if type(preview) is not ResourcePackageImportPreview:
            raise TypeError("resource package preview must be exact")
        plan = self._prepared_imports.pop(preview.operation_id, None)
        if plan is None or plan.preview is not preview:
            if plan is not None:
                self._prepared_imports[preview.operation_id] = plan
            raise ResourcePortabilityError("RESOURCE.IMPORT.PREVIEW_STALE")
        return plan

    def _recovery_preview(
        self,
        pending: ResourcePendingOperation,
    ) -> ResourceRecoveryPreview:
        receipt = pending.receipt
        destination_id = receipt.destination_resource_id
        if (
            pending.phase is ResourcePendingPhase.RECEIPT_READY
            and receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE
        ):
            if self._ready_export_receipt_is_durable(receipt):
                disposition = ResourceRecoveryDisposition.COMPLETE_AVAILABLE
                reasons = ("RESOURCE.RECOVERY.RECEIPT_READY",)
            else:
                disposition = ResourceRecoveryDisposition.MANUAL_REQUIRED
                reasons = ("RESOURCE.RECOVERY.EXPORT_OWNER_REQUIRED",)
        elif pending.phase is ResourcePendingPhase.MANUAL_REQUIRED:
            disposition = ResourceRecoveryDisposition.MANUAL_REQUIRED
            reasons = ("RESOURCE.RECOVERY.MANUAL_REQUIRED",)
        elif receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE:
            disposition = ResourceRecoveryDisposition.MANUAL_REQUIRED
            reasons = ("RESOURCE.RECOVERY.EXPORT_OWNER_REQUIRED",)
        else:
            disposition, reasons = self._inspect_import_recovery(pending)
        return ResourceRecoveryPreview(
            operation_id=receipt.operation_id,
            operation_kind=receipt.operation_kind,
            disposition=disposition,
            destination_resource_id=destination_id,
            safe_reasons=reasons,
        )

    def _inspect_import_recovery(
        self,
        pending: ResourcePendingOperation,
    ) -> tuple[ResourceRecoveryDisposition, tuple[str, ...]]:
        receipt = pending.receipt
        destination_id = receipt.destination_resource_id
        if destination_id is None:
            return (
                ResourceRecoveryDisposition.MANUAL_REQUIRED,
                ("RESOURCE.RECOVERY.DESTINATION_UNKNOWN",),
            )
        if pending.import_mode is ResourceImportMode.CREATE_NEW:
            if pending.destination_relative_path is None:
                return (
                    ResourceRecoveryDisposition.MANUAL_REQUIRED,
                    ("RESOURCE.RECOVERY.DESTINATION_UNKNOWN",),
                )
            path = self.repository.managed_dir / pending.destination_relative_path
            if path.parent != self.repository.managed_dir:
                return (
                    ResourceRecoveryDisposition.MANUAL_REQUIRED,
                    ("RESOURCE.RECOVERY.DESTINATION_UNKNOWN",),
                )
            try:
                configured = self.repository.get(destination_id)
            except ResourceError:
                configured = None
            if configured is not None:
                if configured.path == path and _safe_digest(
                    self.repository.platform_backend,
                    path,
                ) == receipt.destination_after_digest:
                    return (
                        ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
                        ("RESOURCE.RECOVERY.OWNER_PUBLISHED",),
                    )
                return (
                    ResourceRecoveryDisposition.MANUAL_REQUIRED,
                    ("RESOURCE.RECOVERY.DESTINATION_CHANGED",),
                )
            try:
                observed = _entry_snapshot(
                    self.repository.platform_backend,
                    path,
                )
            except PlatformFileError:
                observed = False
            if observed is None:
                return (
                    ResourceRecoveryDisposition.ROLLBACK_AVAILABLE,
                    ("RESOURCE.RECOVERY.CREATE_NOT_PUBLISHED",),
                )
            if observed is False:
                return (
                    ResourceRecoveryDisposition.MANUAL_REQUIRED,
                    ("RESOURCE.RECOVERY.DESTINATION_CHANGED",),
                )
            if _safe_digest(
                self.repository.platform_backend,
                path,
            ) == receipt.destination_after_digest:
                return (
                    ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
                    ("RESOURCE.RECOVERY.OWNER_PUBLISHED",),
                )
            return (
                ResourceRecoveryDisposition.MANUAL_REQUIRED,
                ("RESOURCE.RECOVERY.DESTINATION_CHANGED",),
            )
        if pending.import_mode is ResourceImportMode.REPLACE_SELECTED:
            try:
                configured = self.repository.get(destination_id)
                digest = _safe_digest(
                    self.repository.platform_backend,
                    configured.path,
                )
            except ResourceError:
                configured = None
                digest = None
            if (
                configured is not None
                and configured.kind is ResourceKind.TRANSLATION_MEMORY
                and digest == receipt.destination_after_digest
            ):
                try:
                    self._tm.reopen_snapshot(configured, receipt)
                except (ResourcePortabilityError, OSError, ValueError):
                    return (
                        ResourceRecoveryDisposition.MANUAL_REQUIRED,
                        ("RESOURCE.RECOVERY.OWNER_PENDING",),
                    )
                return (
                    ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
                    ("RESOURCE.RECOVERY.OWNER_PUBLISHED",),
                )
            if digest == receipt.destination_after_digest:
                return (
                    ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
                    ("RESOURCE.RECOVERY.OWNER_PUBLISHED",),
                )
            if receipt.destination_before_digest is not None and digest == receipt.destination_before_digest:
                return (
                    ResourceRecoveryDisposition.ROLLBACK_AVAILABLE,
                    ("RESOURCE.RECOVERY.PRIOR_PRESERVED",),
                )
        return (
            ResourceRecoveryDisposition.MANUAL_REQUIRED,
            ("RESOURCE.RECOVERY.DESTINATION_CHANGED",),
        )

    def _complete_recovery(
        self,
        pending: ResourcePendingOperation,
    ) -> ResourceOperationReceipt:
        if (
            pending.phase is ResourcePendingPhase.RECEIPT_READY
            and pending.receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE
        ):
            receipt = pending.receipt
            if not self._ready_export_receipt_is_durable(receipt):
                raise ResourcePortabilityError("RESOURCE.RECOVERY.DECISION_INVALID")
            self._ledger.commit(receipt)
            return receipt
        receipt = pending.receipt
        if receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE:
            raise ResourcePortabilityError("RESOURCE.RECOVERY.DECISION_INVALID")
        destination_id = receipt.destination_resource_id
        if destination_id is None:
            raise ResourcePortabilityError("RESOURCE.RECOVERY.DESTINATION_UNKNOWN")
        if pending.import_mode is ResourceImportMode.CREATE_NEW:
            if pending.destination_name is None or pending.destination_relative_path is None:
                raise ResourcePortabilityError("RESOURCE.RECOVERY.DESTINATION_UNKNOWN")
            try:
                destination = self.repository.get(destination_id)
            except ResourceError:
                try:
                    destination = self.repository.recover_resource_create(
                        resource_id=destination_id,
                        name=pending.destination_name,
                        kind=_editor_kind(receipt.resource_kind),
                        relative_path=pending.destination_relative_path,
                        expected_digest=receipt.payload_digest,
                    )
                except ResourceError as error:
                    raise ResourcePortabilityError(
                        "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                    ) from error
        else:
            try:
                destination = self.repository.get(destination_id)
            except ResourceError as error:
                raise ResourcePortabilityError(
                    "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                ) from error
        self._cold_reopen_receipt_destination(destination, receipt)
        committed = replace(receipt, durable_state=ResourceDurableState.COMMITTED)
        self._complete_pending_receipt(committed)
        return committed

    def _cold_reopen_receipt_destination(
        self,
        destination: ResourceConfig,
        receipt: ResourceOperationReceipt,
    ) -> None:
        if destination.kind is ResourceKind.TRANSLATION_MEMORY:
            self._tm.reopen_snapshot(destination, receipt)
            return
        facts = self._termbase.validate_portable_snapshot(destination.path)
        if (
            facts.payload_digest != receipt.payload_digest
            or facts.record_count != receipt.record_count
            or facts.legacy_record_count != receipt.legacy_record_count
            or facts.v1_record_count != receipt.v1_record_count
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")

    def _validate_sealed(
        self,
        sealed: SealedResourcePackage,
    ) -> tuple[ResourcePackageValidationReport, PortableResourceSnapshot]:
        report = sealed.validation
        with tempfile.TemporaryDirectory(
            dir=self.repository.config_dir,
            prefix=".resource-package-validate-",
        ) as raw:
            suffix = {
                ResourcePayloadProfile.TM_JSONL_V1: ".jsonl",
                ResourcePayloadProfile.TERMBASE_CSV_V1: ".csv",
                ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1: ".tmx",
            }[report.payload_profile]
            payload = Path(raw) / f"payload{suffix}"
            sealed.copy_payload_to(payload)
            if report.payload_profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
                owner = self._require_tmx_payload_handler().validate_snapshot(payload)
                self._validate_handler_snapshot(owner, payload)
                counts = (0, 0)
            elif report.resource_kind is PortableResourceKind.TRANSLATION_MEMORY:
                owner = self._tm.validate_snapshot(payload)
                counts = (0, 0)
            else:
                term = self._termbase.validate_portable_snapshot(payload)
                owner = _portable_term_snapshot(term)
                counts = (term.legacy_record_count, term.v1_record_count)
            if (
                owner.payload_digest != report.payload_digest
                or owner.payload_byte_count != report.payload_byte_count
                or owner.record_count != report.record_count
                or counts != (report.legacy_record_count, report.v1_record_count)
            ):
                raise ResourcePortabilityError("RESOURCE.PACKAGE.COUNT_MISMATCH")
            sealed.reprove()
            return replace(report, safe_issues=owner.safe_issues), owner

    def _apply_owner_snapshot(
        self,
        destination: ResourceConfig,
        payload: Path,
        owner: PortableResourceSnapshot,
        *,
        create: bool,
        owner_commit: Callable[[PortableResourceSnapshot], None] | None = None,
    ) -> PortableResourceSnapshot:
        if owner.profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
            raise ResourcePortabilityError("RESOURCE.IMPORT.PROFILE_UNSUPPORTED")
        if destination.kind is ResourceKind.TRANSLATION_MEMORY:
            applied = (
                self._tm.create_snapshot(
                    destination,
                    payload,
                    owner,
                    owner_commit=owner_commit,
                )
                if create
                else self._tm.replace_snapshot(
                    destination,
                    payload,
                    owner,
                    owner_commit=owner_commit,
                )
            )
            return applied
        if create:
            applied_snapshot: PortableResourceSnapshot | None = None

            def commit_created(_identity: FileObjectIdentity) -> None:
                nonlocal applied_snapshot
                facts = self._termbase.validate_portable_snapshot(destination.path)
                applied_snapshot = _portable_term_snapshot(facts)
                if (
                    applied_snapshot.payload_digest != owner.payload_digest
                    or applied_snapshot.record_count != owner.record_count
                    or applied_snapshot.legacy_record_count
                    != owner.legacy_record_count
                    or applied_snapshot.v1_record_count != owner.v1_record_count
                ):
                    raise ResourcePortabilityError(
                        "RESOURCE.IMPORT.COLD_REOPEN_FAILED"
                    )
                if owner_commit is None:
                    raise TypeError("create owner commit callback is required")
                owner_commit(applied_snapshot)

            _copy_new_bound(
                self.repository.platform_backend,
                payload,
                destination.path,
                owner_commit=commit_created,
            )
            if applied_snapshot is None:
                raise AssertionError("created owner snapshot was not committed")
            applied = applied_snapshot
        else:
            if owner_commit is not None:
                raise ValueError("replace owner callback is unsupported")
            prepared = self._termbase.prepare_snapshot_replace(
                destination.path,
                payload,
            )
            outcome = self._termbase.commit(prepared)
            if outcome.state is not TermCommitState.COMMITTED:
                if outcome.state is TermCommitState.INDETERMINATE:
                    raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
                raise ResourcePortabilityError(
                    "RESOURCE.IMPORT.APPLY_FAILED",
                    retryable=outcome.retryable,
                )
            facts = self._termbase.validate_portable_snapshot(destination.path)
            cleanup = self._termbase.finalize(prepared, outcome)
            if not cleanup.cleaned:
                raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
            applied = _portable_term_snapshot(facts)
        if (
            applied.payload_digest != owner.payload_digest
            or applied.record_count != owner.record_count
            or applied.legacy_record_count != owner.legacy_record_count
            or applied.v1_record_count != owner.v1_record_count
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.COLD_REOPEN_FAILED")
        return applied

    def _resource(self, resource_id: str) -> ResourceConfig:
        if type(resource_id) is not str or not resource_id.strip():
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.CONTRACT_INVALID")
        try:
            return self.repository.get(resource_id)
        except ResourceError as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE") from error

    def _reprove_source(
        self,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        if snapshot.profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
            self._require_tmx_payload_handler().reprove_snapshot(resource, snapshot)
            return
        if resource.kind is ResourceKind.TRANSLATION_MEMORY:
            self._tm.reprove_snapshot(resource, snapshot)
            return
        try:
            current_digest = _digest_path(
                self.repository.platform_backend,
                resource.path,
            )
        except (PlatformFileError, ValueError):
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE") from None
        if current_digest != snapshot.source_baseline_digest:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")

    def _require_tmx_payload_handler(self) -> ResourcePackagePayloadHandler:
        handler = self._tmx_payload_handler
        if handler is None:
            raise ResourcePortabilityError(
                "RESOURCE.PORTABILITY.PAYLOAD_HANDLER_UNAVAILABLE"
            )
        return handler

    @staticmethod
    def _validate_handler_snapshot(
        snapshot: PortableResourceSnapshot,
        payload: Path,
    ) -> None:
        if type(snapshot) is not PortableResourceSnapshot:
            raise TypeError("package payload handler snapshot must be exact")
        if (
            snapshot.kind is not PortableResourceKind.TRANSLATION_MEMORY
            or snapshot.profile
            is not ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
        ):
            raise ResourcePortabilityError(
                "RESOURCE.PORTABILITY.PAYLOAD_HANDLER_INVALID"
            )
        try:
            payload_bytes = payload.read_bytes()
        except OSError as error:
            raise ResourcePortabilityError(
                "RESOURCE.EXPORT.VALIDATION_FAILED"
            ) from error
        if (
            hashlib.sha256(payload_bytes).hexdigest() != snapshot.payload_digest
            or len(payload_bytes) != snapshot.payload_byte_count
        ):
            raise ResourcePortabilityError(
                "RESOURCE.PORTABILITY.PAYLOAD_HANDLER_INVALID"
            )

    def _arm_after_owner_publication(
        self,
        receipt: ResourceOperationReceipt,
    ) -> None:
        """Durably project an owner-published success into the local ledger."""

        pending = False
        try:
            self._ledger.begin(_recovery_receipt(receipt))
            pending = True
            self._complete_pending_receipt(receipt)
        except BaseException as error:
            if pending:
                try:
                    self._ledger.mark_manual(receipt.operation_id)
                except ResourcePortabilityError:
                    pass
            raise ResourcePortabilityError("RESOURCE.RECEIPT.RECOVERY_REQUIRED") from error

    def _complete_pending_receipt(
        self,
        receipt: ResourceOperationReceipt,
    ) -> None:
        """Move one armed operation through the exact durable receipt boundary."""

        try:
            self._ledger.mark_receipt_ready(receipt)
            self._ledger.commit(receipt)
        except ResourcePortabilityError as error:
            try:
                self._ledger.mark_manual(receipt.operation_id)
            except ResourcePortabilityError:
                pass
            raise ResourcePortabilityError("RESOURCE.RECEIPT.RECOVERY_REQUIRED") from error

    def _mark_pending_receipt_ready(
        self,
        receipt: ResourceOperationReceipt,
    ) -> None:
        """Persist owner facts without minting success before outer terminal proof."""

        try:
            self._ledger.mark_receipt_ready(receipt)
        except ResourcePortabilityError as error:
            raise ResourcePortabilityError(
                "RESOURCE.RECEIPT.RECOVERY_REQUIRED"
            ) from error

    def _commit_ready_receipt(
        self,
        receipt: ResourceOperationReceipt,
    ) -> None:
        """Append success only after the artifact publisher returned terminal facts."""

        try:
            self._ledger.commit(receipt)
        except ResourcePortabilityError as error:
            raise ResourcePortabilityError(
                "RESOURCE.RECEIPT.RECOVERY_REQUIRED"
            ) from error

    def _ready_export_receipt_is_durable(
        self,
        receipt: ResourceOperationReceipt,
    ) -> bool:
        """Require an exact success entry before path-free export cleanup."""

        try:
            return self._ledger.get(receipt.operation_id) == receipt
        except ResourcePortabilityError:
            return False

    def _resolve_failed_pending(
        self,
        operation_id: str,
        error: BaseException,
    ) -> None:
        """Retain only failures whose publication outcome is not proven."""

        if _is_recovery_required(error):
            try:
                self._ledger.mark_manual(operation_id)
            except ResourcePortabilityError as ledger_error:
                raise ResourcePortabilityError(
                    "RESOURCE.RECEIPT.RECOVERY_REQUIRED"
                ) from ledger_error
            return
        try:
            self._ledger.abandon(operation_id)
        except ResourcePortabilityError as ledger_error:
            raise ResourcePortabilityError(
                "RESOURCE.RECEIPT.RECOVERY_REQUIRED"
            ) from ledger_error

    def _abandon_pending_or_recovery(
        self,
        operation_id: str,
        *,
        import_operation: bool,
    ) -> None:
        try:
            self._ledger.abandon(operation_id)
        except ResourcePortabilityError as error:
            code = (
                "RESOURCE.IMPORT.RECOVERY_REQUIRED"
                if import_operation
                else "RESOURCE.RECEIPT.RECOVERY_REQUIRED"
            )
            raise ResourcePortabilityError(code) from error

    @staticmethod
    def _receipt(
        *,
        operation_id: str,
        operation_kind: ResourceOperationKind,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
        package_digest: str | None,
        destination_resource_id: str | None,
        destination_before_digest: str | None,
        destination_after_digest: str,
    ) -> ResourceOperationReceipt:
        return ResourceOperationReceipt(
            receipt_schema=RECEIPT_SCHEMA,
            operation_id=operation_id,
            operation_kind=operation_kind,
            resource_kind=snapshot.kind,
            payload_profile=snapshot.profile,
            source_resource_id=resource.id,
            destination_resource_id=destination_resource_id,
            package_artifact_digest=package_digest,
            payload_digest=snapshot.payload_digest,
            destination_before_digest=destination_before_digest,
            destination_after_digest=destination_after_digest,
            record_count=snapshot.record_count,
            legacy_record_count=snapshot.legacy_record_count,
            v1_record_count=snapshot.v1_record_count,
            skipped_count=0,
            safe_warnings=snapshot.safe_issues,
            durable_state=ResourceDurableState.COMMITTED,
            owner_generation=snapshot.owner_generation,
            owner_revision=snapshot.owner_revision,
            owner_receipt_digest=snapshot.owner_receipt_digest,
            source_baseline_digest=snapshot.source_baseline_digest,
        )


def _portable_term_snapshot(
    facts: TermbasePortableSnapshot,
) -> PortableResourceSnapshot:
    return PortableResourceSnapshot(
        kind=PortableResourceKind.TERMBASE,
        profile=ResourcePayloadProfile.TERMBASE_CSV_V1,
        payload_digest=facts.payload_digest,
        payload_byte_count=facts.payload_byte_count,
        record_count=facts.record_count,
        legacy_record_count=facts.legacy_record_count,
        v1_record_count=facts.v1_record_count,
        source_baseline_digest=facts.source_baseline_digest,
        owner_receipt_digest=None,
    )


def _recovery_receipt(
    receipt: ResourceOperationReceipt,
) -> ResourceOperationReceipt:
    if type(receipt) is not ResourceOperationReceipt:
        raise TypeError("recovery receipt template must be exact")
    return replace(receipt, durable_state=ResourceDurableState.RECOVERY_REQUIRED)


def _import_receipt(
    *,
    operation_id: str,
    report: ResourcePackageValidationReport,
    destination: ResourceConfig,
    before_digest: str | None,
    snapshot: PortableResourceSnapshot,
    durable_state: ResourceDurableState,
) -> ResourceOperationReceipt:
    if type(report) is not ResourcePackageValidationReport:
        raise TypeError("import validation report must be exact")
    if type(destination) is not ResourceConfig:
        raise TypeError("import destination must be exact")
    if type(snapshot) is not PortableResourceSnapshot:
        raise TypeError("import snapshot must be exact")
    if type(durable_state) is not ResourceDurableState:
        raise TypeError("import durable state must be exact")
    if (
        report.resource_kind is not snapshot.kind
        or report.payload_profile is not snapshot.profile
        or report.payload_digest != snapshot.payload_digest
        or report.record_count != snapshot.record_count
        or report.legacy_record_count != snapshot.legacy_record_count
        or report.v1_record_count != snapshot.v1_record_count
    ):
        raise ResourcePortabilityError("RESOURCE.PACKAGE.COUNT_MISMATCH")
    return ResourceOperationReceipt(
        receipt_schema=RECEIPT_SCHEMA,
        operation_id=operation_id,
        operation_kind=ResourceOperationKind.IMPORT_PACKAGE,
        resource_kind=snapshot.kind,
        payload_profile=snapshot.profile,
        source_resource_id=None,
        destination_resource_id=destination.id,
        package_artifact_digest=report.artifact_digest,
        payload_digest=snapshot.payload_digest,
        destination_before_digest=before_digest,
        destination_after_digest=snapshot.payload_digest,
        record_count=snapshot.record_count,
        legacy_record_count=snapshot.legacy_record_count,
        v1_record_count=snapshot.v1_record_count,
        skipped_count=0,
        safe_warnings=report.safe_issues,
        durable_state=durable_state,
        owner_generation=snapshot.owner_generation,
        owner_revision=snapshot.owner_revision,
        owner_receipt_digest=snapshot.owner_receipt_digest,
        source_baseline_digest=snapshot.source_baseline_digest,
    )


def _is_recovery_required(error: BaseException) -> bool:
    return isinstance(error, ResourcePortabilityError) and error.code in {
        "RESOURCE.EXPORT.RECOVERY_REQUIRED",
        "RESOURCE.IMPORT.RECOVERY_REQUIRED",
        "RESOURCE.RECEIPT.RECOVERY_REQUIRED",
    }


def _manifest_for_snapshot(snapshot: PortableResourceSnapshot) -> ResourcePackageManifest:
    schema, carrier, profile_set = package_profile_triple_for_payload(
        snapshot.profile
    )
    return ResourcePackageManifest(
        schema=schema,
        carrier_profile=carrier,
        payload_profile_set=profile_set,
        resource_kind=snapshot.kind,
        payload_profile=snapshot.profile,
        payload=ResourcePayloadDescriptor(
            path=payload_path_for_profile(snapshot.profile),
            sha256=snapshot.payload_digest,
            byte_count=snapshot.payload_byte_count,
            record_count=snapshot.record_count,
        ),
        profile_counts=ResourceProfileCounts(
            snapshot.legacy_record_count,
            snapshot.v1_record_count,
        ),
    )


def _absolute_destination(path: Path) -> Path:
    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError("resource artifact path must be an absolute concrete Path")
    if path.name in ("", ".", ".."):
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return path


def _candidate_path(destination: Path, suffix: str) -> Path:
    return destination.with_name(
        f".{destination.name}.{uuid4().hex}.{suffix}.tmp"
    )


def _optional_digest(
    backend: PlatformFileBackend,
    path: Path,
) -> str | None:
    try:
        return _digest_path(backend, path)
    except PlatformFileError as error:
        if error.code == PlatformFileErrorCode.ENTRY_UNAVAILABLE.value:
            return None
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE") from None


def _digest_path(backend: PlatformFileBackend, path: Path) -> str:
    source = bind_rooted_regular(backend, path)
    try:
        snapshot = source.snapshot()
        return digest_bound(source, snapshot).hex()
    finally:
        source.close()


def _safe_digest(backend: PlatformFileBackend, path: Path) -> str | None:
    try:
        return _digest_path(backend, path)
    except (PlatformFileError, ResourcePortabilityError):
        return None


def _repository_baseline(repository: ResourceRepository) -> _RepositoryBaseline:
    try:
        source = bind_rooted_regular(
            repository.platform_backend,
            repository.registry_path,
        )
        try:
            snapshot = source.snapshot()
            digest = digest_bound(source, snapshot).hex()
        finally:
            source.close()
        return _RepositoryBaseline(
            resources=repository.list_resources(),
            registry_digest=digest,
            registry_snapshot=snapshot,
        )
    except (PlatformFileError, ResourcePortabilityError):
        raise ResourcePortabilityError("RESOURCE.IMPORT.PREVIEW_STALE") from None


def _file_snapshot(
    backend: PlatformFileBackend,
    path: Path,
) -> EntrySnapshot:
    source = bind_rooted_regular(backend, path)
    try:
        return source.snapshot()
    finally:
        source.close()


def _entry_snapshot(
    backend: PlatformFileBackend,
    path: Path,
) -> EntrySnapshot | None:
    root = None
    parent = None
    try:
        root = backend.bind_root(path.parent)
        parent = backend.bind_parent(root, platform_relative_path(path.name))
        return parent.inspect_entry(path.name)
    finally:
        for authority in (parent, root):
            if authority is not None:
                authority.close()


def _unlink_snapshot(
    backend: PlatformFileBackend,
    path: Path,
    snapshot: EntrySnapshot,
) -> None:
    root = None
    parent = None
    try:
        root = backend.bind_root(path.parent)
        parent = backend.bind_parent(root, platform_relative_path(path.name))
        parent.unlink_owned(path.name, snapshot.identity)
    finally:
        for authority in (parent, root):
            if authority is not None:
                authority.close()


def _editor_kind(kind: PortableResourceKind) -> ResourceKind:
    if type(kind) is not PortableResourceKind:
        raise TypeError("portable resource kind must be exact")
    return (
        ResourceKind.TRANSLATION_MEMORY
        if kind is PortableResourceKind.TRANSLATION_MEMORY
        else ResourceKind.TERMBASE
    )


def _copy_new_bound(
    backend: PlatformFileBackend,
    source_path: Path,
    destination: Path,
    *,
    owner_commit: Callable[[FileObjectIdentity], None],
) -> FileObjectIdentity:
    source = bind_rooted_regular(backend, source_path)
    root = None
    parent = None
    candidate: CandidateFile | None = None
    pending: PendingPublication | None = None
    candidate_identity: FileObjectIdentity | None = None
    candidate_name = f".resource-create-{uuid4().hex}.tmp"
    try:
        source_snapshot = source.snapshot()
        source_digest = digest_bound(source, source_snapshot)
        root = backend.bind_root(destination.parent)
        parent = backend.bind_parent(root, platform_relative_path(destination.name))
        if parent.inspect_entry(destination.name) is not None:
            raise ResourcePortabilityError("RESOURCE.IMPORT.DESTINATION_STALE")
        candidate = parent.create_candidate(candidate_name, private=True)
        candidate_identity = candidate.identity()
        written = candidate.write_chunks(
            iter_bound_chunks(source, source_snapshot),
            maximum_bytes=source_snapshot.byte_count,
        )
        candidate.flush_content()
        pending = parent.begin_publish(
            candidate,
            destination.name,
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        candidate = None
        candidate_identity = None
        facts = pending.preliminary_facts()
        retained = pending.retained_destination()
        retained_snapshot = retained.snapshot()
        if (
            written.content_sha256 != source_digest
            or written.byte_count != source_snapshot.byte_count
            or facts.content_sha256 != source_digest
            or facts.byte_count != source_snapshot.byte_count
            or retained_snapshot.byte_count != source_snapshot.byte_count
            or digest_bound(retained, retained_snapshot) != source_digest
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
        owner_commit(facts.destination_identity)
        if pending.terminal_reproof() != facts:
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED")
        return facts.destination_identity
    except PlatformFileError as error:
        if error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
            raise ResourcePortabilityError("RESOURCE.IMPORT.RECOVERY_REQUIRED") from None
        raise ResourcePortabilityError("RESOURCE.IMPORT.APPLY_FAILED") from None
    finally:
        for authority in (pending, candidate):
            if authority is not None:
                try:
                    authority.close()
                except PlatformFileError:
                    pass
        if candidate_identity is not None and parent is not None:
            try:
                parent.unlink_owned(candidate_name, candidate_identity)
            except PlatformFileError:
                pass
        for authority in (parent, root, source):
            if authority is not None:
                try:
                    authority.close()
                except PlatformFileError:
                    pass


__all__ = ["ResourcePortabilityService"]
