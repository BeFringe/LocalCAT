"""Feature 5 Task 5.4 Gate B: canonical physical readiness for one artifact.

Gate B freshly rehashes the staged DB, manifest, and configured source through
no-follow identity captures and compares them with the registry-owned sealed
content attestation. A grant proves only that one sealed artifact is
physically complete and ready to be considered for activation; it never
publishes a generation, touches a token, drains leases, replaces files,
publishes a manifest, writes a journal, or modifies any canonical/JSONL asset.

All public diagnostics are stable code-only values; they never contain TM
source/target text or local paths.  Path-bearing facts enter only through the
coordinator-owned registry readiness seam, never as public parameters.

Linearization semantics: a successful grant proves the exact artifact bytes
(staged DB, temporary manifest) and the configured source at Gate B's
terminal closure point, which re-runs immediately after claim closure and
before the grant is minted.  A grant cannot prevent external mutation after
evaluation returns; Task 5.5 must freshly revalidate before any replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, TypeVar

from tm_content_attestation import SealedContentAttestation
from tm_contracts import (
    SealedStage,
    stage_validation_evidence_digest,
)
from tm_stage_sealer import (
    _PhysicalReadinessSnapshot,
    _SealedArtifactReadinessView,
    _require_linearization_closure,
    StageSealError,
)


GATE_B_REPORT_VERSION = "gate-b-report-v1"
GATE_B_FACTS_VERSION = "gate-b-facts-v1"
GATE_B_GRANT_VERSION = "gate-b-grant-v1"

_GATE_B_FACTORY_KEY = object()
_GATE_B_NO_FACTORY_KEY = object()

_T = TypeVar("_T")


def _gate_b_build(cls: type[_T], **values: object) -> _T:
    """Private seam: build a factory-keyed instance without the public ctor.

    The public constructor and dataclasses.replace() always see the no-factory
    sentinel, so callers can never pass or copy the factory key into a grant
    or report; only this helper can key an instance.
    """

    instance: Any = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    instance.__post_init__()
    return instance
_DIAGNOSTIC_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _stable_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_identity(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _require_int(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _require_optional_generation(value: object) -> int | None:
    if value is None:
        return None
    return _require_int(value, "expected prior generation", minimum=0)


def _require_gram_counts(
    value: object,
) -> tuple[tuple[int, int], ...]:
    if type(value) is not tuple:
        raise TypeError("gram counts must be a tuple")
    pairs: list[tuple[int, int]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("gram counts must contain integer pairs")
        pairs.append(
            (
                _require_int(item[0], "gram size", minimum=1),
                _require_int(item[1], "gram count", minimum=0),
            )
        )
    sizes = [pair[0] for pair in pairs]
    if sizes != sorted(set(sizes)):
        raise ValueError("gram sizes must be unique and ordered")
    return tuple(pairs)


def _require_diagnostic_code(value: object) -> str:
    if type(value) is not str:
        raise TypeError("error code must be a string")
    if len(value) > 96 or _DIAGNOSTIC_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("error code must be a stable diagnostic identifier")
    return value


@dataclass(frozen=True)
class GateBPhysicalFacts:
    """Recomputed, code-only physical facts for one sealed artifact."""

    facts_version: str
    registry_namespace: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    schema_version: int
    schema_digest: str
    fts5_available: bool
    sqlite_runtime_version: str
    unicode_runtime_version: str
    journal_mode: str
    synchronous: str
    foreign_keys: bool
    busy_timeout_ms: int
    wal_enabled: bool
    extension_loading_enabled: bool
    candidate_index_kind: str
    candidate_index_version: str
    fold_version: str
    origin_batch_count: int
    migration_batch_id: str
    completed_revision: int
    source_digest: str
    record_count: int
    gram_counts: tuple[tuple[int, int], ...]
    fts_count: int
    integrity_ok: bool
    foreign_keys_ok: bool
    exact_parity_digest: str
    manifest_temp_digest: str
    stage_file_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_gate_b_facts(self)


def _facts_payload(facts: GateBPhysicalFacts) -> dict[str, object]:
    return {
        "artifact_id": facts.artifact_id,
        "artifact_seal_digest": facts.artifact_seal_digest,
        "busy_timeout_ms": facts.busy_timeout_ms,
        "candidate_index_kind": facts.candidate_index_kind,
        "candidate_index_version": facts.candidate_index_version,
        "canonical_store_id": facts.canonical_store_id,
        "completed_revision": facts.completed_revision,
        "evidence_digest": facts.evidence_digest,
        "exact_parity_digest": facts.exact_parity_digest,
        "expected_prior_generation": facts.expected_prior_generation,
        "extension_loading_enabled": facts.extension_loading_enabled,
        "facts_version": facts.facts_version,
        "fold_version": facts.fold_version,
        "foreign_keys": facts.foreign_keys,
        "foreign_keys_ok": facts.foreign_keys_ok,
        "fts5_available": facts.fts5_available,
        "fts_count": facts.fts_count,
        "gram_counts": [list(pair) for pair in facts.gram_counts],
        "integrity_ok": facts.integrity_ok,
        "journal_mode": facts.journal_mode,
        "manifest_temp_digest": facts.manifest_temp_digest,
        "migration_batch_id": facts.migration_batch_id,
        "origin_batch_count": facts.origin_batch_count,
        "record_count": facts.record_count,
        "registry_namespace": facts.registry_namespace,
        "resource_id": facts.resource_id,
        "schema_digest": facts.schema_digest,
        "schema_version": facts.schema_version,
        "sealed_stage_digest": facts.sealed_stage_digest,
        "snapshot_receipt_digest": facts.snapshot_receipt_digest,
        "source_digest": facts.source_digest,
        "stage_file_digest": facts.stage_file_digest,
        "synchronous": facts.synchronous,
        "target_identity": facts.target_identity,
        "unicode_runtime_version": facts.unicode_runtime_version,
        "sqlite_runtime_version": facts.sqlite_runtime_version,
        "wal_enabled": facts.wal_enabled,
    }


def _validate_gate_b_facts(facts: GateBPhysicalFacts) -> None:
    if type(facts) is not GateBPhysicalFacts:
        raise TypeError("facts must be exact GateBPhysicalFacts")
    if facts.facts_version != GATE_B_FACTS_VERSION:
        raise ValueError("unsupported gate B facts version")
    _require_identity(facts.registry_namespace, "registry namespace")
    _require_identity(facts.artifact_id, "artifact id")
    _require_digest(facts.artifact_seal_digest, "artifact seal digest")
    _require_digest(facts.sealed_stage_digest, "sealed stage digest")
    _require_identity(facts.resource_id, "resource id")
    _require_digest(facts.target_identity, "target identity")
    _require_identity(facts.canonical_store_id, "canonical store id")
    _require_digest(
        facts.snapshot_receipt_digest,
        "snapshot receipt digest",
    )
    _require_optional_generation(facts.expected_prior_generation)
    _require_int(facts.schema_version, "schema version", minimum=1)
    _require_digest(facts.schema_digest, "schema digest")
    _require_bool(facts.fts5_available, "FTS5 availability")
    _require_identity(
        facts.sqlite_runtime_version,
        "SQLite runtime version",
    )
    _require_identity(
        facts.unicode_runtime_version,
        "Unicode runtime version",
    )
    _require_identity(facts.journal_mode, "journal mode")
    _require_identity(facts.synchronous, "synchronous mode")
    _require_bool(facts.foreign_keys, "foreign keys")
    _require_int(facts.busy_timeout_ms, "busy timeout", minimum=0)
    _require_bool(facts.wal_enabled, "WAL enabled")
    _require_bool(
        facts.extension_loading_enabled,
        "extension loading enabled",
    )
    _require_identity(
        facts.candidate_index_kind,
        "candidate index kind",
    )
    _require_identity(
        facts.candidate_index_version,
        "candidate index version",
    )
    _require_identity(facts.fold_version, "fold version")
    _require_int(facts.origin_batch_count, "origin batch count", minimum=0)
    _require_identity(facts.migration_batch_id, "migration batch id")
    _require_int(facts.completed_revision, "completed revision", minimum=0)
    _require_digest(facts.source_digest, "source digest")
    _require_int(facts.record_count, "record count", minimum=0)
    gram_counts = _require_gram_counts(facts.gram_counts)
    _require_int(facts.fts_count, "FTS count", minimum=0)
    if facts.fts_count > facts.record_count:
        raise ValueError("FTS count cannot exceed record count")
    _require_bool(facts.integrity_ok, "integrity status")
    _require_bool(facts.foreign_keys_ok, "foreign-key status")
    if not facts.integrity_ok:
        raise ValueError("integrity validation must pass")
    if not facts.foreign_keys_ok:
        raise ValueError("foreign-key validation must pass")
    _require_digest(facts.exact_parity_digest, "exact parity digest")
    _require_digest(facts.manifest_temp_digest, "manifest temporary digest")
    _require_digest(facts.stage_file_digest, "stage file digest")
    _require_digest(facts.evidence_digest, "evidence digest")
    _ = gram_counts


def gate_b_facts_digest(facts: GateBPhysicalFacts) -> str:
    """Deterministic digest over the complete recomputed physical facts."""

    _validate_gate_b_facts(facts)
    return _stable_digest(_facts_payload(facts))


@dataclass(frozen=True)
class GateBGrant:
    """Frozen single-artifact readiness grant; only the Core factory creates it."""

    grant_version: str
    registry_namespace: str
    artifact_id: str
    artifact_seal_digest: str
    sealed_stage_digest: str
    resource_id: str
    target_identity: str
    canonical_store_id: str
    snapshot_receipt_digest: str
    expected_prior_generation: int | None
    stage_db_digest: str
    manifest_temp_digest: str
    evidence_digest: str
    grant_digest: str
    _factory_key: object = field(
        repr=False,
        compare=False,
        init=False,
        default=_GATE_B_NO_FACTORY_KEY,
    )

    def __post_init__(self) -> None:
        if self._factory_key is not _GATE_B_FACTORY_KEY:
            raise TypeError("Gate B grants require the Core factory")
        _validate_gate_b_grant(self)


def _grant_payload(grant: GateBGrant) -> dict[str, object]:
    return {
        "artifact_id": grant.artifact_id,
        "artifact_seal_digest": grant.artifact_seal_digest,
        "canonical_store_id": grant.canonical_store_id,
        "evidence_digest": grant.evidence_digest,
        "expected_prior_generation": grant.expected_prior_generation,
        "grant_version": grant.grant_version,
        "manifest_temp_digest": grant.manifest_temp_digest,
        "registry_namespace": grant.registry_namespace,
        "resource_id": grant.resource_id,
        "sealed_stage_digest": grant.sealed_stage_digest,
        "snapshot_receipt_digest": grant.snapshot_receipt_digest,
        "stage_db_digest": grant.stage_db_digest,
        "target_identity": grant.target_identity,
    }


def _validate_gate_b_grant(grant: GateBGrant) -> None:
    if type(grant) is not GateBGrant:
        raise TypeError("grant must be exact GateBGrant")
    if grant.grant_version != GATE_B_GRANT_VERSION:
        raise ValueError("unsupported gate B grant version")
    _require_identity(grant.registry_namespace, "registry namespace")
    _require_identity(grant.artifact_id, "artifact id")
    _require_digest(grant.artifact_seal_digest, "artifact seal digest")
    _require_digest(grant.sealed_stage_digest, "sealed stage digest")
    _require_identity(grant.resource_id, "resource id")
    _require_digest(grant.target_identity, "target identity")
    _require_identity(grant.canonical_store_id, "canonical store id")
    _require_digest(
        grant.snapshot_receipt_digest,
        "snapshot receipt digest",
    )
    _require_optional_generation(grant.expected_prior_generation)
    _require_digest(grant.stage_db_digest, "stage database digest")
    _require_digest(
        grant.manifest_temp_digest,
        "manifest temporary digest",
    )
    _require_digest(grant.evidence_digest, "evidence digest")
    expected_digest = _stable_digest(_grant_payload(grant))
    if grant.grant_digest != expected_digest:
        raise ValueError("gate B grant digest does not close")


def gate_b_grant_digest(grant: GateBGrant) -> str:
    """Deterministic digest over the complete readiness grant chain."""

    _validate_gate_b_grant(grant)
    return _stable_digest(_grant_payload(grant))


def _create_gate_b_grant(
    *,
    grant_version: str,
    registry_namespace: str,
    artifact_id: str,
    artifact_seal_digest: str,
    sealed_stage_digest: str,
    resource_id: str,
    target_identity: str,
    canonical_store_id: str,
    snapshot_receipt_digest: str,
    expected_prior_generation: int | None,
    stage_db_digest: str,
    manifest_temp_digest: str,
    evidence_digest: str,
) -> GateBGrant:
    """Private grant factory; callers cannot promote a denial or forge one."""

    payload: dict[str, object] = {
        "artifact_id": artifact_id,
        "artifact_seal_digest": artifact_seal_digest,
        "canonical_store_id": canonical_store_id,
        "evidence_digest": evidence_digest,
        "expected_prior_generation": expected_prior_generation,
        "grant_version": grant_version,
        "manifest_temp_digest": manifest_temp_digest,
        "registry_namespace": registry_namespace,
        "resource_id": resource_id,
        "sealed_stage_digest": sealed_stage_digest,
        "snapshot_receipt_digest": snapshot_receipt_digest,
        "stage_db_digest": stage_db_digest,
        "target_identity": target_identity,
    }
    return _gate_b_build(
        GateBGrant,
        grant_version=grant_version,
        registry_namespace=registry_namespace,
        artifact_id=artifact_id,
        artifact_seal_digest=artifact_seal_digest,
        sealed_stage_digest=sealed_stage_digest,
        resource_id=resource_id,
        target_identity=target_identity,
        canonical_store_id=canonical_store_id,
        snapshot_receipt_digest=snapshot_receipt_digest,
        expected_prior_generation=expected_prior_generation,
        stage_db_digest=stage_db_digest,
        manifest_temp_digest=manifest_temp_digest,
        evidence_digest=evidence_digest,
        grant_digest=_stable_digest(payload),
        _factory_key=_GATE_B_FACTORY_KEY,
    )


@dataclass(frozen=True)
class GateBPhysicalReadinessReport:
    """Inspectable Gate B outcome; denial never carries activation authority."""

    report_version: str
    granted: bool
    error_code: str | None
    facts: GateBPhysicalFacts | None
    grant: GateBGrant | None
    facts_digest: str | None
    grant_digest: str | None
    report_digest: str
    _factory_key: object = field(
        repr=False,
        compare=False,
        init=False,
        default=_GATE_B_NO_FACTORY_KEY,
    )

    def __post_init__(self) -> None:
        if self._factory_key is not _GATE_B_FACTORY_KEY:
            raise TypeError(
                "Gate B readiness reports require the Core factory"
            )
        _validate_gate_b_report(self)


def _report_payload_digest(
    *,
    report_version: str,
    granted: bool,
    error_code: str | None,
    facts_digest: str | None,
    grant_digest: str | None,
) -> str:
    payload: dict[str, object] = {
        "error_code": error_code,
        "facts_digest": facts_digest,
        "grant_digest": grant_digest,
        "granted": granted,
        "report_version": report_version,
    }
    return _stable_digest(payload)


def _validate_gate_b_report(report: GateBPhysicalReadinessReport) -> None:
    if type(report) is not GateBPhysicalReadinessReport:
        raise TypeError("report must be exact GateBPhysicalReadinessReport")
    if report.report_version != GATE_B_REPORT_VERSION:
        raise ValueError("unsupported gate B report version")
    _require_bool(report.granted, "granted")
    if report.granted:
        if report.error_code is not None:
            raise ValueError("granted report must not carry an error code")
        if report.facts is None or report.grant is None:
            raise ValueError("granted report requires facts and grant")
        if report.facts_digest is None or report.grant_digest is None:
            raise ValueError("granted report requires closed digests")
        _validate_gate_b_facts(report.facts)
        _validate_gate_b_grant(report.grant)
        grant = report.grant
        facts = report.facts
        if (
            grant.registry_namespace != facts.registry_namespace
            or grant.artifact_id != facts.artifact_id
            or grant.artifact_seal_digest != facts.artifact_seal_digest
            or grant.sealed_stage_digest != facts.sealed_stage_digest
            or grant.resource_id != facts.resource_id
            or grant.target_identity != facts.target_identity
            or grant.canonical_store_id != facts.canonical_store_id
            or grant.snapshot_receipt_digest
            != facts.snapshot_receipt_digest
            or grant.expected_prior_generation
            != facts.expected_prior_generation
            or grant.stage_db_digest != facts.stage_file_digest
            or grant.manifest_temp_digest != facts.manifest_temp_digest
            or grant.evidence_digest != facts.evidence_digest
        ):
            raise ValueError("gate B grant does not close over the facts")
        if report.facts_digest != gate_b_facts_digest(facts):
            raise ValueError("gate B facts digest does not close")
        if report.grant_digest != gate_b_grant_digest(grant):
            raise ValueError("gate B grant digest does not close")
    else:
        if report.grant is not None:
            raise ValueError("a denial must never carry a grant")
        if report.facts is not None:
            raise ValueError("a denial must never carry recomputed facts")
        if report.error_code is None:
            raise ValueError("a denial requires a stable error code")
        _require_diagnostic_code(report.error_code)
        if report.facts_digest is not None or report.grant_digest is not None:
            raise ValueError("a denial must not carry grant digests")
    expected_digest = _report_payload_digest(
        report_version=report.report_version,
        granted=report.granted,
        error_code=report.error_code,
        facts_digest=report.facts_digest,
        grant_digest=report.grant_digest,
    )
    if report.report_digest != expected_digest:
        raise ValueError("gate B report digest does not close")


def gate_b_report_digest(
    report: GateBPhysicalReadinessReport,
) -> str:
    """Deterministic digest over the complete readiness report."""

    _validate_gate_b_report(report)
    return _report_payload_digest(
        report_version=report.report_version,
        granted=report.granted,
        error_code=report.error_code,
        facts_digest=report.facts_digest,
        grant_digest=report.grant_digest,
    )


class _GateBFailure(Exception):
    """Internal stable-code failure; converted to an inspectable denial."""

    def __init__(self, error_code: str) -> None:
        _require_diagnostic_code(error_code)
        self.error_code = error_code
        super().__init__(error_code)


_GATE_B_CODE_MAP: dict[str, str] = {
    "SEALER.TYPE_INVALID": "GATE_B.TYPE_INVALID",
    "SEALER.REGISTRY_MISMATCH": "GATE_B.REGISTRY_MISMATCH",
    "SEALER.ALREADY_SEALED": "GATE_B.REGISTRY_MISMATCH",
    "SEALER.ARTIFACT_MUTATED": "GATE_B.ARTIFACT_MUTATED",
    "SEALER.STAGE_MUTATED_AFTER_VALIDATION": "GATE_B.ARTIFACT_MUTATED",
    "SEALER.EVIDENCE_MISMATCH": "GATE_B.EVIDENCE_MISMATCH",
    "SEALER.GENERATION_MISMATCH": "GATE_B.EVIDENCE_MISMATCH",
    "SEALER.STAGE_NOT_SEALED": "GATE_B.STAGE_NOT_SEALED",
    "SEALER.STAGE_NOT_UNPUBLISHED": "GATE_B.STAGE_NOT_SEALED",
    "SEALER.CANDIDATE_INDEX_INCOMPLETE": (
        "GATE_B.CANDIDATE_INDEX_INCOMPLETE"
    ),
    "SEALER.FTS_INDEX_INCOMPLETE": "GATE_B.FTS_INDEX_INCOMPLETE",
    "SEALER.EXACT_PARITY_MISMATCH": "GATE_B.PARITY_MISMATCH",
    "SEALER.FOLD_MISMATCH": "GATE_B.FOLD_MISMATCH",
    "SEALER.SOURCE_DIGEST_MISMATCH": "GATE_B.SOURCE_MISMATCH",
    "SEALER.RECEIPT_INVALID": "GATE_B.RECEIPT_MISMATCH",
    "SEALER.RECEIPT_LEDGER_INVALID": "GATE_B.RECEIPT_MISMATCH",
    "SEALER.MANIFEST_INVALID": "GATE_B.MANIFEST_MISMATCH",
    "SEALER.MANIFEST_MISMATCH": "GATE_B.MANIFEST_MISMATCH",
    "SEALER.BINDING_NOT_UNPUBLISHED": "GATE_B.BINDING_MISMATCH",
    "SEALER.MIGRATION_BATCH_INVALID": "GATE_B.ANCESTRY_INVALID",
    "SEALER.ORIGIN_ANCESTRY_INVALID": "GATE_B.ANCESTRY_INVALID",
    "SEALER.REVISION_ANCESTRY_INVALID": "GATE_B.ANCESTRY_INVALID",
    "SEALER.RECORD_COUNT_MISMATCH": "GATE_B.RECORD_MISMATCH",
    "SEALER.RECORD_IDENTITY_INVALID": "GATE_B.RECORD_MISMATCH",
    "SEALER.RECORD_MISMATCH": "GATE_B.RECORD_MISMATCH",
    "SEALER.RECORD_LINEAGE_INVALID": "GATE_B.RECORD_MISMATCH",
    "SEALER.PROVENANCE_MISMATCH": "GATE_B.RECORD_MISMATCH",
    "SEALER.INTEGRITY_FAILED": "GATE_B.INTEGRITY_FAILED",
    "SEALER.FOREIGN_KEY_FAILED": "GATE_B.FOREIGN_KEY_FAILED",
    "SEALER.STAGE_DATABASE_MISSING": "GATE_B.ARTIFACT_MISSING",
    "SEALER.STAGE_MANIFEST_MISSING": "GATE_B.ARTIFACT_MISSING",
    "SEALER.STAGE_DATABASE_UNSAFE": "GATE_B.ARTIFACT_UNSAFE",
    "SEALER.STAGE_MANIFEST_UNSAFE": "GATE_B.ARTIFACT_UNSAFE",
    "SEALER.DIGEST_UNREADABLE": "GATE_B.ARTIFACT_UNREADABLE",
    "SEALER.STAGE_INVALID": "GATE_B.PHYSICAL_INVALID",
    "STORE.SCHEMA_TOO_NEW": "GATE_B.SCHEMA_MISMATCH",
    "STORE.SCHEMA_UNSUPPORTED": "GATE_B.SCHEMA_MISMATCH",
    "STORE.IDENTITY_MISMATCH": "GATE_B.IDENTITY_MISMATCH",
    "STORE.SCHEMA_INCOMPLETE": "GATE_B.SCHEMA_INCOMPLETE",
    "STORE.SCHEMA_UNEXPECTED": "GATE_B.SCHEMA_UNEXPECTED",
    "STORE.TABLE_SCHEMA_MISMATCH": "GATE_B.SCHEMA_MISMATCH",
    "STORE.RUNTIME_CAPABILITY_CHANGED": "GATE_B.RUNTIME_MISMATCH",
    "STORE.SQLITE_RUNTIME_CHANGED": "GATE_B.RUNTIME_MISMATCH",
    "STORE.UNICODE_RUNTIME_MISMATCH": "GATE_B.RUNTIME_MISMATCH",
    "STORE.META_VERSION_MISMATCH": "GATE_B.VERSION_MISMATCH",
    "STORE.CANDIDATE_INDEX_MISMATCH": "GATE_B.CANDIDATE_INDEX_MISMATCH",
    "STORE.JOURNAL_MODE_UNSAFE": "GATE_B.RUNTIME_MISMATCH",
    "STORE.WAL_FORBIDDEN": "GATE_B.RUNTIME_MISMATCH",
    "STORE.SYNCHRONOUS_UNSAFE": "GATE_B.RUNTIME_MISMATCH",
    "STORE.FOREIGN_KEYS_DISABLED": "GATE_B.RUNTIME_MISMATCH",
    "STORE.BUSY_TIMEOUT_MISMATCH": "GATE_B.RUNTIME_MISMATCH",
    "STORE.STAGE_PUBLISHED": "GATE_B.STAGE_NOT_SEALED",
    "STORE.STAGE_NOT_SEALED": "GATE_B.STAGE_NOT_SEALED",
    "STORE.STAGE_DIVERGED": "GATE_B.STAGE_DIVERGED",
    "STORE.STAGE_REVISION_INVALID": "GATE_B.ANCESTRY_INVALID",
    "STORE.INDEX_SCHEMA_MISMATCH": "GATE_B.SCHEMA_MISMATCH",
    "STORE.FOREIGN_KEY_SCHEMA_MISMATCH": "GATE_B.SCHEMA_MISMATCH",
    "STORE.META_INCOMPLETE": "GATE_B.SCHEMA_MISMATCH",
    "STORE.DATABASE_MISSING": "GATE_B.ARTIFACT_MISSING",
}


def _map_gate_b_code(error: BaseException) -> str:
    code = str(error)
    if code in _GATE_B_CODE_MAP:
        return _GATE_B_CODE_MAP[code]
    return "GATE_B.READINESS_FAILED"


def _denial_report(error_code: str) -> GateBPhysicalReadinessReport:
    return _gate_b_build(
        GateBPhysicalReadinessReport,
        report_version=GATE_B_REPORT_VERSION,
        granted=False,
        error_code=error_code,
        facts=None,
        grant=None,
        facts_digest=None,
        grant_digest=None,
        report_digest=_report_payload_digest(
            report_version=GATE_B_REPORT_VERSION,
            granted=False,
            error_code=error_code,
            facts_digest=None,
            grant_digest=None,
        ),
        _factory_key=_GATE_B_FACTORY_KEY,
    )


def _require_claim_closure(
    snapshot: _PhysicalReadinessSnapshot,
    attestation: SealedContentAttestation,
) -> None:
    """Bind the registry-owned attestation to the sealed contract claim."""

    claim = snapshot.evidence
    if type(attestation) is not SealedContentAttestation:
        raise _GateBFailure("GATE_B.EVIDENCE_MISMATCH")
    semantic = attestation.semantic_facts
    if (
        claim.resource_id != snapshot.resource_id
        or claim.target_identity != snapshot.target_identity
        or claim.source_binding.receipt.canonical_store_id
        != snapshot.canonical_store_id
        or claim.snapshot_receipt_digest
        != snapshot.snapshot_receipt_digest
        or attestation.resource_id != snapshot.resource_id
        or attestation.target_identity != snapshot.target_identity
        or attestation.canonical_store_id != snapshot.canonical_store_id
        or attestation.snapshot_receipt_digest
        != snapshot.snapshot_receipt_digest
        or attestation.expected_prior_generation
        != snapshot.expected_prior_generation
        or attestation.evidence_digest
        != stage_validation_evidence_digest(claim)
        or attestation.database.sha256 != claim.stage_file_digest
        or attestation.manifest.sha256 != claim.manifest_temp_digest
        or attestation.source.sha256
        != claim.source_binding.receipt.jsonl_digest
        or semantic.schema_version != claim.schema_version
        or semantic.fold_version != claim.fold_version
        or semantic.index_version != claim.index_version
        or semantic.receipt_boundary_record_count != claim.record_count
        or semantic.origin_batch_count != claim.origin_batch_count
        or semantic.receipt_boundary_fts_count != claim.fts_count
        or semantic.gram_counts != claim.gram_counts
        or semantic.exact_parity_digest != claim.exact_parity_digest
    ):
        raise _GateBFailure("GATE_B.EVIDENCE_MISMATCH")


def _grant_report(
    snapshot: _PhysicalReadinessSnapshot,
    attestation: SealedContentAttestation,
) -> GateBPhysicalReadinessReport:
    """Build the inspectable facts and the factory-only grant for one artifact."""

    semantic = attestation.semantic_facts
    evidence_digest = attestation.evidence_digest
    facts = GateBPhysicalFacts(
        facts_version=GATE_B_FACTS_VERSION,
        registry_namespace=snapshot.registry_namespace,
        artifact_id=snapshot.artifact_id,
        artifact_seal_digest=snapshot.artifact_seal_digest,
        sealed_stage_digest=snapshot.sealed_stage_digest,
        resource_id=snapshot.resource_id,
        target_identity=snapshot.target_identity,
        canonical_store_id=snapshot.canonical_store_id,
        snapshot_receipt_digest=snapshot.snapshot_receipt_digest,
        expected_prior_generation=snapshot.expected_prior_generation,
        schema_version=semantic.schema_version,
        schema_digest=semantic.schema_digest,
        fts5_available=semantic.fts5_available,
        sqlite_runtime_version=semantic.sqlite_runtime_version,
        unicode_runtime_version=semantic.unicode_runtime_version,
        journal_mode=semantic.journal_mode,
        synchronous=semantic.synchronous,
        foreign_keys=semantic.foreign_keys,
        busy_timeout_ms=semantic.busy_timeout_ms,
        wal_enabled=semantic.wal_enabled,
        extension_loading_enabled=(
            semantic.extension_loading_enabled
        ),
        candidate_index_kind=semantic.candidate_index_kind,
        candidate_index_version=semantic.index_version,
        fold_version=semantic.fold_version,
        origin_batch_count=semantic.origin_batch_count,
        # The frozen field name is the historical migration seam; the value
        # carries the actual single origin batch id (``migration.<digest>``
        # for migration origins, ``import.<uuid>`` for explicit imports) so
        # Gate B binds the exact batch the activation will publish.
        migration_batch_id=semantic.origin_batch_id,
        completed_revision=semantic.exported_revision,
        source_digest=attestation.source.sha256,
        record_count=semantic.receipt_boundary_record_count,
        gram_counts=semantic.gram_counts,
        fts_count=semantic.receipt_boundary_fts_count,
        integrity_ok=True,
        foreign_keys_ok=True,
        exact_parity_digest=semantic.exact_parity_digest,
        manifest_temp_digest=attestation.manifest.sha256,
        stage_file_digest=attestation.database.sha256,
        evidence_digest=evidence_digest,
    )
    facts_digest = gate_b_facts_digest(facts)
    grant = _create_gate_b_grant(
        grant_version=GATE_B_GRANT_VERSION,
        registry_namespace=snapshot.registry_namespace,
        artifact_id=snapshot.artifact_id,
        artifact_seal_digest=snapshot.artifact_seal_digest,
        sealed_stage_digest=snapshot.sealed_stage_digest,
        resource_id=snapshot.resource_id,
        target_identity=snapshot.target_identity,
        canonical_store_id=snapshot.canonical_store_id,
        snapshot_receipt_digest=snapshot.snapshot_receipt_digest,
        expected_prior_generation=snapshot.expected_prior_generation,
        stage_db_digest=attestation.database.sha256,
        manifest_temp_digest=attestation.manifest.sha256,
        evidence_digest=evidence_digest,
    )
    grant_digest = gate_b_grant_digest(grant)
    return _gate_b_build(
        GateBPhysicalReadinessReport,
        report_version=GATE_B_REPORT_VERSION,
        granted=True,
        error_code=None,
        facts=facts,
        grant=grant,
        facts_digest=facts_digest,
        grant_digest=grant_digest,
        report_digest=_report_payload_digest(
            report_version=GATE_B_REPORT_VERSION,
            granted=True,
            error_code=None,
            facts_digest=facts_digest,
            grant_digest=grant_digest,
        ),
        _factory_key=_GATE_B_FACTORY_KEY,
    )


class GateBEvaluator:
    """Fresh canonical physical readiness evaluation for one sealed artifact.

    Authority comes only from the coordinator-owned exact read-only registry
    view; structurally compatible fakes and subclasses are rejected at the
    boundary, so no caller-created snapshot can reach the grant factory.
    """

    def __init__(self, *, registry: _SealedArtifactReadinessView) -> None:
        if type(registry) is not _SealedArtifactReadinessView:
            raise TypeError(
                "Gate B requires the coordinator-owned "
                "sealed artifact readiness view"
            )
        self._registry = registry

    def evaluate(
        self,
        sealed_stage: SealedStage,
    ) -> GateBPhysicalReadinessReport:
        """Re-prove exact sealed bytes from disk and the registry entry.

        Denials are returned as inspectable reports without any grant; the
        evaluation never writes, publishes, drains, or issues anything.  The
        terminal identity+digest closure runs again at the linearization
        point after claim closure and immediately before grant minting, so
        the grant always describes the exact artifact bytes at that point.
        """

        try:
            if type(sealed_stage) is not SealedStage:
                raise _GateBFailure("GATE_B.TYPE_INVALID")
            try:
                snapshot = self._registry.resolve_physical_readiness(
                    sealed_stage
                )
            except (
                StageSealError,
                TypeError,
                ValueError,
                AttributeError,
            ) as error:
                raise _GateBFailure(_map_gate_b_code(error)) from error
            if type(snapshot) is not _PhysicalReadinessSnapshot:
                raise _GateBFailure("GATE_B.REGISTRY_MISMATCH")
            try:
                attestation = snapshot.sealed_content_attestation
                _require_claim_closure(snapshot, attestation)
                _require_linearization_closure(snapshot, attestation)
            except StageSealError as error:
                raise _GateBFailure(_map_gate_b_code(error)) from error
            except (TypeError, ValueError, AttributeError) as error:
                raise _GateBFailure("GATE_B.READINESS_FAILED") from error
            return _grant_report(snapshot, attestation)
        except _GateBFailure as failure:
            return _denial_report(failure.error_code)


__all__ = [
    "GATE_B_FACTS_VERSION",
    "GATE_B_GRANT_VERSION",
    "GATE_B_REPORT_VERSION",
    "GateBEvaluator",
    "GateBGrant",
    "GateBPhysicalFacts",
    "GateBPhysicalReadinessReport",
    "gate_b_facts_digest",
    "gate_b_grant_digest",
    "gate_b_report_digest",
]
