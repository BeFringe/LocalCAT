"""Reusable Task 7.2 production-built canonical integration fixture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from editor_contracts import ResourceConfig, ResourceKind
from tm_application_composition import (
    TMResourceResolver,
    TMRuntimeSnapshot,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    TMResourceHandle,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


QUERY_SOURCE = "aabba"
HIGH_FUZZY_SOURCE = "abbaa"
BOUNDARY_SOURCE = "bbaab"
BELOW_THRESHOLD_SOURCE = "bbbaa"
ONE_HUNDRED_FUZZY_SOURCE = "AABBA"

_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "feature5_ui_integration"
    / "canonical_variants.jsonl"
).resolve()
_DEFAULT_RESOURCE_IDS = (
    "tm.fixture.primary",
    "tm.fixture.secondary",
)


@dataclass(frozen=True, slots=True)
class ActivatedCanonicalResourceFixture:
    """One report and its independently reopened production handle."""

    resource_id: str
    identity: CanonicalResourceIdentity
    report: MigrationReport
    handle: TMResourceHandle


@dataclass(frozen=True, slots=True)
class ActivatedCanonicalFixture:
    """Two or more canonical resources built from the tracked input."""

    source_fixture: Path
    runtime: TMRuntimeSnapshot
    resources: tuple[ActivatedCanonicalResourceFixture, ...]

    def semantic_transcript(self) -> tuple[object, ...]:
        """Return path-independent durable semantics from formal query leases."""

        transcript: list[object] = []
        for resource in self.resources:
            store = cast(SQLiteTMStore, cast(object, resource.handle.store))
            if type(store) is not SQLiteTMStore:
                raise AssertionError("fixture must reopen an exact SQLiteTMStore")
            with store.query_lease() as view:
                health = view.health()
                records = view.records_by_id(
                    tuple(range(1, health.record_count + 1))
                )
                transcript.append(
                    (
                        resource.resource_id,
                        resource.report.source_digest,
                        resource.report.activated_generation,
                        resource.report.migrated_count,
                        resource.report.variant_count,
                        resource.report.skipped_count,
                        view.resource_id,
                        view.generation,
                        health.record_count,
                        tuple(
                            (
                                record.record_id,
                                record.source_raw,
                                record.target_raw,
                                record.speaker_raw,
                                record.context_prev_raw,
                                record.context_next_raw,
                                record.file_source,
                                record.legacy_line_no,
                                record.origin_ordinal,
                            )
                            for record in records
                        ),
                    )
                )
        return tuple(transcript)


def build_activated_canonical_fixture(
    root: Path,
    *,
    resource_ids: tuple[str, ...] = _DEFAULT_RESOURCE_IDS,
) -> ActivatedCanonicalFixture:
    """Build, publish, and cold-reopen real canonical SQLite resources.

    Every input is copied from the tracked JSONL fixture into the caller's
    disposable root.  Publication uses the application-facing production
    migration contract.  Reopening then starts at ``TMResourceResolver`` so
    no activation-time coordinator, handwritten store, or legacy importer is
    reused as acceptance evidence.
    """

    if type(root) is not type(Path()) or not root.is_absolute():
        raise TypeError("fixture root must be an absolute native Path")
    if type(resource_ids) is not tuple or len(resource_ids) < 2:
        raise TypeError("fixture requires at least two resource ids")
    if any(
        type(resource_id) is not str or not resource_id.strip()
        for resource_id in resource_ids
    ):
        raise TypeError("fixture resource ids must be non-empty exact strings")
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("fixture resource ids must be unique")

    root.mkdir(parents=True, exist_ok=True)
    source_bytes = _FIXTURE_PATH.read_bytes()
    if not source_bytes or not source_bytes.endswith(b"\n"):
        raise AssertionError("tracked canonical fixture must be non-empty JSONL")

    reports: list[MigrationReport] = []
    identities: list[CanonicalResourceIdentity] = []
    activation_coordinators: list[ResourceStoreCoordinator] = []
    configs: list[ResourceConfig] = []
    for position, resource_id in enumerate(resource_ids, start=1):
        source = (root / f"canonical-{position}.jsonl").resolve()
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource_id,
            source,
        )
        if any(
            path.exists()
            for path in (
                identity.configured_jsonl_path,
                identity.canonical_sidecar_path,
                identity.snapshot_manifest_path,
            )
        ):
            raise FileExistsError("fixture target assets must not exist")
        source.write_bytes(source_bytes)
        canonical_store_id = f"store.{resource_id}"
        coordinator = ResourceStoreCoordinator(
            canonical_store_id=canonical_store_id,
            resource_identity=identity,
        )
        outcome = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=canonical_store_id,
            coordinator=coordinator,
        ).activate_initial(source, resource_id)
        if type(outcome) is not MigrationReport:
            raise AssertionError(
                f"production canonical fixture activation failed: {outcome!r}"
            )
        reports.append(outcome)
        identities.append(identity)
        activation_coordinators.append(coordinator)
        configs.append(
            ResourceConfig(
                id=resource_id,
                name=resource_id,
                kind=ResourceKind.TRANSLATION_MEMORY,
                path=source,
                active=True,
                lookup=True,
                update=False,
            )
        )

    runtime = TMResourceResolver().resolve(tuple(configs))
    if runtime.legacy_ports or len(runtime.canonical_handles) != len(configs):
        raise AssertionError("fixture cold reopen did not classify canonical only")

    resources: list[ActivatedCanonicalResourceFixture] = []
    for identity, report, coordinator, handle in zip(
        identities,
        reports,
        activation_coordinators,
        runtime.canonical_handles,
        strict=True,
    ):
        store = cast(SQLiteTMStore, cast(object, handle.store))
        if type(store) is not SQLiteTMStore:
            raise AssertionError("fixture cold reopen did not yield SQLiteTMStore")
        if store.coordinator is coordinator:
            raise AssertionError("fixture cold reopen reused activation coordinator")
        if (
            handle.resource_id != identity.resource_id
            or store.coordinator.resource_id != identity.resource_id
            or store.coordinator.current_generation
            != report.activated_generation
        ):
            raise AssertionError("fixture cold reopen identity drift")
        resources.append(
            ActivatedCanonicalResourceFixture(
                resource_id=identity.resource_id,
                identity=identity,
                report=report,
                handle=handle,
            )
        )

    return ActivatedCanonicalFixture(
        source_fixture=_FIXTURE_PATH,
        runtime=runtime,
        resources=tuple(resources),
    )


__all__ = [
    "ActivatedCanonicalFixture",
    "ActivatedCanonicalResourceFixture",
    "BELOW_THRESHOLD_SOURCE",
    "BOUNDARY_SOURCE",
    "HIGH_FUZZY_SOURCE",
    "ONE_HUNDRED_FUZZY_SOURCE",
    "QUERY_SOURCE",
    "build_activated_canonical_fixture",
]
