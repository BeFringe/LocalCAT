"""TMX payload handler injected into the ResourcePackage transaction owner."""

from __future__ import annotations

from pathlib import Path

from editor_contracts import ResourceConfig, ResourceKind
from resource_package_contracts import (
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from tm_engine import TMEngine
from tmx_context_contracts import TmxEffectiveLocales, TmxPayloadProof
from tmx_context_interchange import (
    cold_validate_tmx_file,
    inspect_tmx_payload,
    prepare_tmx_payload,
)
from tmx_export_coordinator import TmxExportCoordinator
from tmx_export_scope_contracts import ManagedResourceScopeMaterialization


class TmxResourcePackagePayloadHandler:
    """Produce one complete managed-resource TMX payload, never a package."""

    __slots__ = ("_locales", "_issued", "_validated")

    def __init__(self, effective_locales: TmxEffectiveLocales | None = None) -> None:
        if effective_locales is not None and type(effective_locales) is not TmxEffectiveLocales:
            raise TypeError("TMX package handler locales must be exact")
        self._locales = effective_locales
        self._issued: dict[str, ManagedResourceScopeMaterialization] = {}
        self._validated: dict[str, tuple[PortableResourceSnapshot, TmxPayloadProof]] = {}

    @property
    def profile(self) -> ResourcePayloadProfile:
        return ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1

    def prepare_export_payload(
        self,
        resource: ResourceConfig,
    ) -> tuple[PortableResourceSnapshot, bytes]:
        if self._locales is None:
            raise ResourcePortabilityError(
                "RESOURCE.PORTABILITY.PAYLOAD_HANDLER_UNAVAILABLE"
            )
        store = _canonical_owner(resource)
        coordinator = TmxExportCoordinator(resource_owner=store)
        materialized = coordinator.capture_managed_resource()
        payload = prepare_tmx_payload(
            materialized.tmx_binding,
            self._locales,
            materialized.units,
        )
        safe_issues = tuple(item.code for item in payload.proof.loss_report.counts)
        snapshot = PortableResourceSnapshot(
                kind=PortableResourceKind.TRANSLATION_MEMORY,
                profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                payload_digest=payload.proof.payload_digest,
                payload_byte_count=len(payload.data),
                record_count=payload.proof.included_count,
                legacy_record_count=0,
                v1_record_count=0,
                source_baseline_digest=materialized.tmx_binding.binding_digest,
                owner_receipt_digest=None,
                owner_generation=materialized.binding.generation,
                owner_revision=materialized.binding.head_revision,
                safe_issues=safe_issues,
            )
        self._issued[snapshot.source_baseline_digest] = materialized
        self._validated = {snapshot.payload_digest: (snapshot, payload.proof)}
        return snapshot, payload.data

    def validate_snapshot(self, source: Path) -> PortableResourceSnapshot:
        try:
            proof = inspect_tmx_payload(source)
            byte_count = source.stat().st_size
        except Exception as error:
            raise ResourcePortabilityError(
                "RESOURCE.EXPORT.VALIDATION_FAILED"
            ) from error
        prepared = self._validated.get(proof.payload_digest)
        if prepared is not None:
            issued, expected_proof = prepared
            # Stateless inspection proves grammar; exporting also has to prove
            # that Parser observes the original prepared bodies and ordered props.
            try:
                cold_validate_tmx_file(source, expected_proof)
            except Exception as error:
                raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED") from error
            if (
                issued.payload_byte_count != byte_count
                or issued.record_count != proof.included_count
            ):
                raise ResourcePortabilityError(
                    "RESOURCE.EXPORT.VALIDATION_FAILED"
                )
            return issued
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            payload_digest=proof.payload_digest,
            payload_byte_count=byte_count,
            record_count=proof.included_count,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=proof.payload_digest,
            owner_receipt_digest=None,
            safe_issues=tuple(item.code for item in proof.loss_report.counts),
        )

    def reprove_snapshot(
        self,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        if type(snapshot) is not PortableResourceSnapshot:
            raise TypeError("TMX package snapshot must be exact")
        materialized = self._issued.pop(snapshot.source_baseline_digest, None)
        if materialized is None:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")
        try:
            coordinator = TmxExportCoordinator(resource_owner=_canonical_owner(resource))
            coordinator.revalidate_managed_resource(materialized)
        except Exception as error:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE") from error
        if (
            snapshot.profile is not self.profile
            or snapshot.owner_generation != materialized.binding.generation
            or snapshot.owner_revision != materialized.binding.head_revision
            or snapshot.source_baseline_digest
            != materialized.tmx_binding.binding_digest
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")


def _canonical_owner(resource: ResourceConfig):
    if type(resource) is not ResourceConfig:
        raise TypeError("TMX package resource must be exact ResourceConfig")
    if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.KIND_MISMATCH")
    try:
        store = TMEngine(
            str(resource.path),
            expected_resource_id=resource.id,
        ).canonical_store
    except Exception as error:
        raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE") from error
    if store is None or store.coordinator.resource_id != resource.id:
        raise ResourcePortabilityError("RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE")
    return store


__all__ = ["TmxResourcePackagePayloadHandler"]
