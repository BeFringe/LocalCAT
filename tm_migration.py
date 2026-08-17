"""Read-only JSONL migration preflight and idempotency checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
from types import MappingProxyType
from typing import cast
import uuid

from tm_activation_journal import (
    _ActivationPreparation,
    _activation_journal_path,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_lineage_marker_temp_path,
    _activation_terminal_path,
    _activation_terminal_temp_path,
)
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    AssetKind,
    AssetPreservationEvidence,
    AssetPreservationState,
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    ExportDiagnostic,
    ExportFailure,
    ExportOutcome,
    ExportReport,
    MigrationDiagnostic,
    MigrationFailure,
    MigrationOutcome,
    MigrationPreflight,
    MigrationReport,
    MutableStageRef,
    RecoveryLocator,
    SchemaUpgradeFailure,
    SchemaUpgradeOutcome,
    SchemaUpgradeReport,
    SealedStage,
    SnapshotKind,
    SourceBindingState,
    SnapshotBinding,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    export_cleanup_pending_failure,
    export_ledger_ambiguous_failure,
    snapshot_receipt_digest,
)
from tm_snapshot_recovery import (
    IssuedReceiptRecovery,
    RecoveryError,
    RefreshRecoveryOutcome,
    RefreshRecoveryState,
)
from tm_sqlite_store import (
    ReceiptCompletionProbe,
    TM_LEGACY_SCHEMA_VERSION,
    TM_SCHEMA_VERSION,
    ActivationPreparationError,
    CanonicalExportRecord,
    CanonicalExportSnapshot,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    _APPROVED_SCHEMA_DIGESTS,
    _FTS5_STATEMENT,
    _SCHEMA_STATEMENTS,
    _SCHEMA_UPGRADE_META_KEY,
    _SCHEMA_UPGRADE_META_VALUE,
    _SchemaUpgradeSnapshotTicket,
    _legacy_completed_origin_blocks,
    _legacy_revision_ancestry,
    _open_configured_connection,
    initialize_stage_schema,
    inspect_stage_schema,
    unique_character_ngrams,
    validate_candidate_proof_index,
)
from tm_stage_sealer import StageSealer
import tm_schema_upgrade as schema_upgrade_module
import tm_snapshot_artifacts as snapshot_artifacts_module



_NATIVE_PATH_TYPE = type(Path())
MIGRATION_STREAM_CHUNK_SIZE = 20_000


def _sealed_source_binding_digest(binding: SnapshotBinding) -> str:
    """Hash one frozen source binding with the canonical contract encoding."""

    return hashlib.sha256(
        contract_to_json(binding).encode("utf-8")
    ).hexdigest()


_EXPORT_JSONL_RECOVERY_SUFFIX = snapshot_artifacts_module._EXPORT_JSONL_RECOVERY_SUFFIX
"""_EXPORT_JSONL_RECOVERY_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_JSONL_TEMP_SUFFIX = snapshot_artifacts_module._EXPORT_JSONL_TEMP_SUFFIX
"""_EXPORT_JSONL_TEMP_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_RECOVERY_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_RECOVERY_SUFFIX
"""_EXPORT_MANIFEST_RECOVERY_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_TEMP_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_TEMP_SUFFIX
"""_EXPORT_MANIFEST_TEMP_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_NO_DESTINATION_PROOF = snapshot_artifacts_module._NO_DESTINATION_PROOF
"""_NO_DESTINATION_PROOF late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_EXPORT_MANIFEST_SUFFIX = snapshot_artifacts_module._EXPORT_MANIFEST_SUFFIX
"""_EXPORT_MANIFEST_SUFFIX late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""

_REJECTION_DIAGNOSTICS = {
    "ROW.INVALID_UTF8": (
        "PREFLIGHT.DECODE",
        "ROW_SKIPPED_INVALID_UTF8",
    ),
    "ROW.INVALID_JSON": (
        "PREFLIGHT.PARSE",
        "ROW_SKIPPED_INVALID_JSON",
    ),
    "ROW.INVALID_SHAPE": (
        "PREFLIGHT.VALIDATE",
        "ROW_SKIPPED_INVALID_SHAPE",
    ),
    "ROW.INVALID_REQUIRED_FIELD": (
        "PREFLIGHT.VALIDATE",
        "ROW_SKIPPED_INVALID_REQUIRED_FIELD",
    ),
}


@dataclass(frozen=True)
class MigrationStageBuild:
    """One unpublished mutable stage; no sidecar reuse before activation."""

    preflight: MigrationPreflight
    mutable_stage: MutableStageRef | None
    reused_completed_revision: int | None

    def __post_init__(self) -> None:
        if type(self.preflight) is not MigrationPreflight:
            raise TypeError("preflight must be exact MigrationPreflight")
        if (
            self.mutable_stage is not None
            and type(self.mutable_stage) is not MutableStageRef
        ):
            raise TypeError(
                "mutable_stage must be exact MutableStageRef or None"
            )
        if self.reused_completed_revision is not None and (
            type(self.reused_completed_revision) is not int
            or self.reused_completed_revision < 1
        ):
            raise ValueError(
                "reused revision must be a positive built-in integer"
            )
        if (self.mutable_stage is None) == (
            self.reused_completed_revision is None
        ):
            raise ValueError(
                "stage build must contain exactly one reusable result"
            )


class _StreamingBuildObservation:
    """Mutable counters and digest accumulated while re-streaming the source."""

    __slots__ = (
        "digest",
        "valid_count",
        "invalid_count",
        "duplicate_source_count",
        "variant_count",
        "source_counts",
    )

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.valid_count = 0
        self.invalid_count = 0
        self.duplicate_source_count = 0
        self.variant_count = 0
        self.source_counts: dict[str, int] = {}


_CreatedFileIdentity = snapshot_artifacts_module._CreatedFileIdentity
"""_CreatedFileIdentity late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
class _ExportParentHandle(snapshot_artifacts_module._ExportParentHandle):
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts.

    ``bind`` injects the owner's late-bound
    ``_after_export_parent_chain_validated`` fault-injection seam
    into the moved implementation.
    """

    @classmethod
    def bind(
        cls,
        destination: Path,
        *,
        after_chain_validated: Callable[[Path], None] | None = None,
    ) -> _ExportParentHandle:
        """Validate the parent chain and retain one no-follow dirfd."""

        return cast(
            _ExportParentHandle,
            super().bind(
                destination,
                after_chain_validated=(
                    after_chain_validated
                    if after_chain_validated is not None
                    else _after_export_parent_chain_validated
                ),
            ),
        )


def _require_export_basename(name: str) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._require_export_basename(
        name=name,
    )

_NoDestinationProof = snapshot_artifacts_module._NoDestinationProof
"""_NoDestinationProof late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""

@dataclass(frozen=True)
class _LocatorSelection:
    """One strictly proven recovery-locator candidate and its identity.

    The identity is the ``(device, inode)`` proven by the shared strict
    proof at selection time; the return-boundary re-proof requires the
    exact same inode immediately before the locator is exposed, so a
    swap between selection and return fails stop.
    """

    path: Path
    identity: tuple[int, int]

    def __post_init__(self) -> None:
        if type(self.path) is not _NATIVE_PATH_TYPE:
            raise TypeError("locator selection path must be pathlib.Path")
        if (
            type(self.identity) is not tuple
            or len(self.identity) != 2
            or type(self.identity[0]) is not int
            or type(self.identity[1]) is not int
        ):
            raise TypeError("locator selection identity is invalid")


class MigrationPreflightError(RuntimeError):
    """Stable preflight failure that never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


ExportPreflightError = snapshot_artifacts_module.ExportPreflightError
"""ExportPreflightError late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
_ExportArtifactPaths = snapshot_artifacts_module._ExportArtifactPaths
"""_ExportArtifactPaths late-bound compatibility alias; implementation moved to tm_snapshot_artifacts."""
def _export_artifact_paths(destination: Path) -> _ExportArtifactPaths:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._export_artifact_paths(
        destination=destination,
    )

def _export_authority_paths(
    identity: CanonicalResourceIdentity,
) -> frozenset[Path]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._export_authority_paths(
        identity=identity,
    )

def _export_path_in_authority_family(
    identity: CanonicalResourceIdentity,
    path: Path,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._export_path_in_authority_family(
        identity=identity,
        path=path,
    )

def _artifact_parent_identity(destination: Path) -> tuple[int, int]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._artifact_parent_identity(
        destination=destination,
    )

def _open_export_parent_chain_no_follow(destination: Path) -> int:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._open_export_parent_chain_no_follow(
        destination=destination,
    )

def _after_export_parent_chain_validated(destination: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._after_export_parent_chain_validated(
        destination=destination,
    )

def _after_replace_source_proved(
    source: Path,
    destination: Path,
    expected_source_identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._after_replace_source_proved(
        source=source,
        destination=destination,
        expected_source_identity=expected_source_identity,
    )

def _prove_replace_source(
    source: Path,
    *,
    expected_source_identity: tuple[int, int],
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._prove_replace_source(
        source=source,
        expected_source_identity=expected_source_identity,
        parent_handle=parent_handle,
    )

def _prove_replace_destination(
    destination: Path,
    *,
    expected_destination_digest: str | None | _NoDestinationProof,
    expected_destination_identity: (
        tuple[int, int] | None | _NoDestinationProof
    ),
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._prove_replace_destination(
        destination=destination,
        expected_destination_digest=expected_destination_digest,
        expected_destination_identity=expected_destination_identity,
        parent_handle=parent_handle,
    )

def _require_export_parent_safe(destination: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._require_export_parent_safe(
        destination=destination,
    )

def _export_existing_digest(
    path: Path,
    *,
    unsafe_code: str,
) -> str | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._export_existing_digest(
        path=path,
        unsafe_code=unsafe_code,
    )

def _export_existing_state(
    path: Path,
    *,
    unsafe_code: str,
) -> tuple[str, tuple[int, int]] | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._export_existing_state(
        path=path,
        unsafe_code=unsafe_code,
        strict_locator_proof=_strict_locator_proof,
    )

def _validate_export_destination(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
) -> tuple[
    str | None,
    str | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._validate_export_destination(
        identity=identity,
        paths=paths,
        strict_locator_proof=_strict_locator_proof,
    )

def _refresh_destination_state(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
) -> tuple[
    str | None,
    str | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._refresh_destination_state(
        identity=identity,
        paths=paths,
        strict_locator_proof=_strict_locator_proof,
    )

def _require_refresh_artifacts_absent(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._require_refresh_artifacts_absent(
        identity=identity,
        paths=paths,
    )

def _path_exists(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._path_exists(
        path=path,
        parent_handle=parent_handle,
    )

def _fsync_file(descriptor: int) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._fsync_file(
        descriptor=descriptor,
    )

def _fsync_directory(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._fsync_directory(
        path=path,
        parent_handle=parent_handle,
    )

def _created_export_identity(
    descriptor: int,
) -> tuple[_CreatedFileIdentity, OSError | None]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._created_export_identity(
        descriptor=descriptor,
    )

def _replace_path(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: tuple[int, int],
    expected_destination_digest: str | None | _NoDestinationProof = (
        _NO_DESTINATION_PROOF
    ),
    expected_destination_identity: (
        tuple[int, int] | None | _NoDestinationProof
    ) = _NO_DESTINATION_PROOF,
    parent_handle: _ExportParentHandle | None = None,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ] | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._replace_path(
        source=source,
        destination=destination,
        expected_source_identity=expected_source_identity,
        expected_destination_digest=expected_destination_digest,
        expected_destination_identity=expected_destination_identity,
        parent_handle=parent_handle,
        after_source_proved=_after_replace_source_proved,
        strict_locator_proof=(
            strict_locator_proof
            if strict_locator_proof is not None
            else _strict_locator_proof
        ),
    )

def _export_jsonl_row(item: object) -> dict[str, object]:
    """One deterministic canonical JSONL row in fixed field order."""

    if type(item) is not CanonicalExportRecord:
        raise TypeError("export row requires one CanonicalExportRecord")
    record = item.record
    return {
        "record_id": record.record_id,
        "source": record.source_raw,
        "target": record.target_raw,
        "speaker": record.speaker_raw,
        "context_prev": record.context_prev_raw,
        "context_next": record.context_next_raw,
        "file_source": record.file_source,
        "provenance": [
            [key, value] for key, value in record.provenance
        ],
        "legacy_line_no": record.legacy_line_no,
        "usage_count": item.usage_count,
        "last_used": item.last_used,
        "origin_batch_id": record.origin_batch_id,
        "origin_ordinal": record.origin_ordinal,
    }


def _stream_export_jsonl_temp(
    path: Path,
    records: tuple[object, ...],
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[str, int, _CreatedFileIdentity]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._stream_export_jsonl_temp(
        path=path,
        records=records,
        parent_handle=parent_handle,
        fsync_file=_fsync_file,
        export_jsonl_row=_export_jsonl_row,
    )

def _verify_export_jsonl_temp(
    path: Path,
    *,
    expected_digest: str,
    expected_count: int,
    identity: _CreatedFileIdentity,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._verify_export_jsonl_temp(
        path=path,
        expected_digest=expected_digest,
        expected_count=expected_count,
        identity=identity,
        parent_handle=parent_handle,
    )

def _write_export_payload_temp(
    path: Path,
    payload: bytes,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> _CreatedFileIdentity:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._write_export_payload_temp(
        path=path,
        payload=payload,
        parent_handle=parent_handle,
        fsync_file=_fsync_file,
    )

def _verify_export_payload_temp(
    path: Path,
    *,
    expected_bytes: bytes,
    identity: _CreatedFileIdentity,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._verify_export_payload_temp(
        path=path,
        expected_bytes=expected_bytes,
        identity=identity,
        parent_handle=parent_handle,
    )

def _remove_exported_if_owned(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
    expected_digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._remove_exported_if_owned(
        path=path,
        expected_identity=expected_identity,
        expected_digest=expected_digest,
        parent_handle=parent_handle,
    )

def _copy_export_prior_pair(
    paths: _ExportArtifactPaths,
    *,
    destination_before: str | None,
    manifest_before: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[
    _CreatedFileIdentity | None,
    _CreatedFileIdentity | None,
]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._copy_export_prior_pair(
        paths=paths,
        destination_before=destination_before,
        manifest_before=manifest_before,
        parent_handle=parent_handle,
        copy_recovery_file=_copy_export_recovery_file,
    )

def _copy_export_recovery_file(
    source: Path,
    recovery: Path,
    *,
    expected_digest: str,
    code: str,
    parent_handle: _ExportParentHandle | None = None,
) -> _CreatedFileIdentity:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._copy_export_recovery_file(
        source=source,
        recovery=recovery,
        expected_digest=expected_digest,
        code=code,
        parent_handle=parent_handle,
        fsync_file=_fsync_file,
    )

def _entry_is_owned(
    path: Path,
    *,
    identity: tuple[int, int] | None,
    digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._entry_is_owned(
        path=path,
        identity=identity,
        digest=digest,
        parent_handle=parent_handle,
    )

def _restore_export_pair(
    paths: _ExportArtifactPaths,
    *,
    destination_before: str | None,
    manifest_before: str | None,
    jsonl_published_identity: tuple[int, int] | None,
    manifest_published_identity: tuple[int, int] | None,
    jsonl_digest: str | None,
    manifest_digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._restore_export_pair(
        paths=paths,
        destination_before=destination_before,
        manifest_before=manifest_before,
        jsonl_published_identity=jsonl_published_identity,
        manifest_published_identity=manifest_published_identity,
        jsonl_digest=jsonl_digest,
        manifest_digest=manifest_digest,
        parent_handle=parent_handle,
        replace_path=_replace_path,
        fsync_directory=_fsync_directory,
        strict_locator_proof=_strict_locator_proof,
    )

def _restore_export_from_recovery(
    recovery: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_destination_digest: str,
    expected_destination_identity: tuple[int, int],
    code: str,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._restore_export_from_recovery(
        recovery=recovery,
        destination=destination,
        expected_digest=expected_digest,
        expected_destination_digest=expected_destination_digest,
        expected_destination_identity=expected_destination_identity,
        code=code,
        parent_handle=parent_handle,
        replace_path=_replace_path,
        strict_locator_proof=_strict_locator_proof,
    )

def _dirfd_entry_state(
    path: Path,
    *,
    parent_handle: _ExportParentHandle,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._dirfd_entry_state(
        path=path,
        parent_handle=parent_handle,
    )

def _strict_regular_file_state(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[str, tuple[int, int]] | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._strict_regular_file_state(
        path=path,
        parent_handle=parent_handle,
    )

def _published_file_identity(
    path: Path,
    expected_digest: str,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[int, int]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._published_file_identity(
        path=path,
        expected_digest=expected_digest,
        parent_handle=parent_handle,
    )

def _verify_export_pair(
    paths: _ExportArtifactPaths,
    *,
    jsonl_digest: str,
    manifest_bytes: bytes,
    jsonl_identity: tuple[int, int],
    manifest_identity: tuple[int, int],
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._verify_export_pair(
        paths=paths,
        jsonl_digest=jsonl_digest,
        manifest_bytes=manifest_bytes,
        jsonl_identity=jsonl_identity,
        manifest_identity=manifest_identity,
        parent_handle=parent_handle,
    )

def _cleanup_export_artifacts(
    paths: _ExportArtifactPaths,
    *,
    jsonl_temp_identity: _CreatedFileIdentity | None,
    manifest_temp_identity: _CreatedFileIdentity | None,
    jsonl_recovery_identity: _CreatedFileIdentity | None,
    manifest_recovery_identity: _CreatedFileIdentity | None,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[Path, ...]:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._cleanup_export_artifacts(
        paths=paths,
        jsonl_temp_identity=jsonl_temp_identity,
        manifest_temp_identity=manifest_temp_identity,
        jsonl_recovery_identity=jsonl_recovery_identity,
        manifest_recovery_identity=manifest_recovery_identity,
        parent_handle=parent_handle,
    )

def _remove_failed_export_artifact(
    path: Path,
    identity: _CreatedFileIdentity,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._remove_failed_export_artifact(
        path=path,
        identity=identity,
        parent_handle=parent_handle,
    )

def _export_diagnostic(
    code: str,
    summary: str,
) -> ExportDiagnostic:
    return ExportDiagnostic(
        code=code,
        record_id=None,
        disposition=DiagnosticDisposition.WARNING,
        safe_summary=summary,
    )


def _recovery_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    message = getattr(error, "args", None)
    if (
        type(message) is tuple
        and message
        and type(message[0]) is str
        and message[0]
    ):
        return message[0]
    return "RECOVERY.FAILED"


def _recovery_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _export_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    return "EXPORT.FAILED"


def _export_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


class TMMigrationService:
    """Preflight one configured legacy JSONL without changing any asset."""

    def __init__(
        self,
        *,
        resource_identity: CanonicalResourceIdentity,
        canonical_store_id: str,
        coordinator: ResourceStoreCoordinator | None = None,
    ) -> None:
        self._resource_identity = _snapshot_resource_identity(resource_identity)
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        if coordinator is not None and type(coordinator) is not (
            ResourceStoreCoordinator
        ):
            raise TypeError(
                "coordinator must be exact ResourceStoreCoordinator or None"
            )
        self._canonical_store_id = canonical_store_id
        self._coordinator = coordinator

    @property
    def resource_identity(self) -> CanonicalResourceIdentity:
        return self._resource_identity

    @property
    def canonical_store_id(self) -> str:
        return self._canonical_store_id

    def preflight(self, source: Path) -> MigrationPreflight:
        """Stream exact source bytes and return safe, deterministic facts."""

        self._validate_source_preconditions(source)
        preflight = _scan_jsonl(source)
        self._reject_sidecar_reuse(preflight.source_digest)
        return preflight

    def build_mutable_stage(self, source: Path) -> MigrationStageBuild:
        """Build or reuse one complete unpublished migration stage."""

        preflight = self.preflight(source)
        stage, _stage_identity, _manifest_identity = self._build_stage(
            source,
            preflight=preflight,
            canonical_store_id=self._canonical_store_id,
            batch_kind="migration",
            batch_prefix="migration",
            snapshot_prefix="snapshot.migration",
            stage_prefix="migration",
        )
        return MigrationStageBuild(
            preflight=preflight,
            mutable_stage=stage,
            reused_completed_revision=None,
        )

    def activate_initial(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        """Publish the first canonical generation for one configured resource.

        This is the sole application-facing first-activation seam.  It
        accepts only the service's exact configured JSONL/resource identity
        and the exact unactivated coordinator injected at construction.
        Mutable/sealed stages, the sealed registry, activation preparation,
        journal handle, and capability token remain inside Core.
        """

        coordinator = self._coordinator
        if type(resource_id) is not str or not resource_id.strip():
            raise MigrationPreflightError("MIGRATION.RESOURCE_ID_INVALID")
        if resource_id != self._resource_identity.resource_id:
            raise MigrationPreflightError(
                "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
            )
        if coordinator is None:
            raise MigrationPreflightError(
                "MIGRATION.COORDINATOR_UNAVAILABLE"
            )
        if (
            coordinator._resource_identity != self._resource_identity
            or coordinator.resource_id != resource_id
            or coordinator.canonical_store_id != self._canonical_store_id
        ):
            raise MigrationPreflightError(
                "MIGRATION.COORDINATOR_IDENTITY_MISMATCH"
            )
        if (
            coordinator.current_generation is not None
            or coordinator.active_store_path is not None
        ):
            raise MigrationPreflightError("MIGRATION.ALREADY_ACTIVE")
        if coordinator.state != "READY":
            raise MigrationPreflightError("MIGRATION.ACTIVATION_NOT_READY")

        build = self.build_mutable_stage(source)
        mutable_stage = build.mutable_stage
        if mutable_stage is None:
            raise MigrationPreflightError(
                "MIGRATION.INITIAL_STAGE_UNAVAILABLE"
            )
        sealed = coordinator._seal_stage(
            mutable_stage,
            canonical_store_id=self._canonical_store_id,
            expected_prior_generation=None,
        )
        prepared = coordinator.activate(sealed)
        handle = coordinator.publish_prepared_activation(prepared)
        generation = coordinator.publish_activation(prepared, handle)
        self._verify_initial_activation_runtime(
            coordinator=coordinator,
            preflight=build.preflight,
            sealed=sealed,
            generation=generation,
        )
        return self._success_report(
            preflight=build.preflight,
            sealed=sealed,
            canonical_store_id=self._canonical_store_id,
            generation=generation,
        )

    def _verify_initial_activation_runtime(
        self,
        *,
        coordinator: ResourceStoreCoordinator,
        preflight: MigrationPreflight,
        sealed: SealedStage,
        generation: int,
    ) -> None:
        """Require one complete generation-zero runtime before success.

        Publication remains coordinator-owned.  This post-publication check
        consumes only the formal reopened store/query ports and their safe
        immutable health/revision facts; it neither repairs nor rolls back a
        durable tail failure.  Task 2.4 owns that recovery classification.
        """

        receipt = sealed.evidence.source_binding.receipt
        expected_binding_digest = _sealed_source_binding_digest(
            sealed.evidence.source_binding
        )
        if (
            type(generation) is not int
            or generation != 0
            or coordinator.state != "READY"
            or coordinator.current_generation != generation
            or coordinator.canonical_store_id != self._canonical_store_id
            or coordinator.resource_id != self._resource_identity.resource_id
            or coordinator.active_store_path is None
            or type(receipt) is not SnapshotReceipt
            or receipt.resource_id != self._resource_identity.resource_id
            or receipt.canonical_store_id != self._canonical_store_id
            or receipt.jsonl_digest != preflight.source_digest
            or receipt.record_count != preflight.valid_count
        ):
            raise MigrationPreflightError(
                "MIGRATION.INITIAL_RUNTIME_INVALID"
            )
        try:
            if coordinator.durable_activation_phase != "GENERATION_PUBLISHED":
                raise MigrationPreflightError(
                    "MIGRATION.INITIAL_RUNTIME_INVALID"
                )
            store = SQLiteTMStore.from_coordinator(coordinator)
            health = store.health()
            revision = store.canonical_revision()
            with store.query_lease() as query_view:
                query_health = query_view.health()
        except MigrationPreflightError:
            raise
        except Exception as error:
            raise MigrationPreflightError(
                "MIGRATION.INITIAL_RUNTIME_REOPEN_FAILED"
            ) from error
        if (
            not health.healthy
            or not health.exact_available
            or health.context_available
            or health.fuzzy_available
            or health.generation != generation
            or health.record_count != preflight.valid_count
            or health.snapshot_binding_digest != expected_binding_digest
            or health.source_binding_state is not SourceBindingState.VERIFIED_CURRENT
            or health.diagnostic_codes
            or query_health != health
            or revision.resource_id != self._resource_identity.resource_id
            or revision.canonical_store_id != self._canonical_store_id
            or revision.generation != generation
            or revision.head_revision != receipt.exported_revision
            or revision.record_count != receipt.record_count
        ):
            raise MigrationPreflightError(
                "MIGRATION.INITIAL_RUNTIME_INVALID"
            )

    def export_jsonl(
        self,
        store: SQLiteTMStore,
        destination: Path,
    ) -> ExportOutcome:
        """Export the active canonical store to one caller-chosen path.

        Task 5.12 arbitrary-path export: the destination must be a
        non-configured absolute path that does not alias the configured
        JSONL, its manifest, the canonical sidecar, activation/schema
        artifacts, or the adjacent export family.  The export captures
        the canonical generation, store id, head revision, and every
        record from one stable coordinator lease/read snapshot, publishes
        the JSONL and adjacent manifest with same-directory exclusive
        temporaries, file/directory fsync, atomic replace and re-open
        validation, and registers an ``issued -> completed`` receipt in
        the canonical ledger against exactly the captured revision.  The
        active snapshot binding, divergence latch, head revision and
        generation are never modified, and a successful export leaves
        ``SOURCE_DIVERGED`` unchanged.  Every failure restores the exact
        prior destination pair (or original absence) and returns an
        honest ``ExportFailure``.
        """

        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore")
        return self._run_arbitrary_export(store, destination)

    def refresh_configured_snapshot(
        self,
        store: SQLiteTMStore,
    ) -> ExportOutcome:
        """Republish the active canonical store as the configured snapshot.

        Task 5.13 configured refresh: this entry point operates only on
        the service resource identity's configured JSONL and its
        deterministic adjacent manifest; callers cannot supply another
        path.  A current ``SourceBindingMonitor`` observation must be
        ``VERIFIED_CURRENT`` or ``VERIFIED_HISTORY``; ``SOURCE_DIVERGED``
        fails before any temporary file, ledger row or filesystem
        mutation and stays latched.  The monitor observes first so an
        unsafe (symlinked, hardlinked, unprovable) configured entry
        durably latches divergence before any path rejection can bypass
        it.  The whole operation runs under a resource-scoped refresh
        reservation so a second refresh never observes the first
        refresh's intentional JSONL-only publication window as
        tampering.  The capture, deterministic JSONL/manifest temporary
        pair, issued receipt, replace/fsync publication order, strict
        identity+digest pair verification and atomic complete+rebind
        reuse the Task 5.12 export protocol.  Canonical records,
        generation and head revision are never modified, and on success
        the monitor reports ``VERIFIED_CURRENT`` with the binding and
        ledger completed against the same receipt/manifest.
        """

        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore")
        identity = self._resource_identity
        paths = _export_artifact_paths(identity.configured_jsonl_path)
        destination_before: str | None = None
        manifest_before: str | None = None
        destination_identity: tuple[int, int] | None = None
        manifest_identity: tuple[int, int] | None = None
        reservation_entered = False
        try:
            reservation = store.configured_refresh_reservation()
            with reservation:
                reservation_entered = True
                try:
                    observation = store.source_binding_monitor.observe()
                except (
                    SQLiteStoreLifecycleError,
                    SQLiteStoreSchemaError,
                    sqlite3.DatabaseError,
                ) as error:
                    return self._export_failure(
                        error,
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=None,
                        destination_observed=None,
                    )
                if (
                    observation.resource_id != identity.resource_id
                    or observation.canonical_store_id
                    != self._canonical_store_id
                ):
                    return self._export_failure(
                        ExportPreflightError(
                            "REFRESH.STORE_IDENTITY_MISMATCH"
                        ),
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=None,
                        destination_observed=None,
                    )
                if (
                    observation.state
                    is SourceBindingState.SOURCE_DIVERGED
                ):
                    return self._export_failure(
                        ExportPreflightError(
                            "REFRESH.SOURCE_DIVERGED"
                        ),
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=None,
                        destination_observed=None,
                    )
                try:
                    (
                        destination_before,
                        manifest_before,
                        destination_identity,
                        manifest_identity,
                    ) = _refresh_destination_state(identity, paths)
                except ExportPreflightError as error:
                    try:
                        store.source_binding_monitor.observe()
                    except (
                        SQLiteStoreLifecycleError,
                        SQLiteStoreSchemaError,
                        sqlite3.DatabaseError,
                    ):
                        pass
                    if error.error_code in {
                        "REFRESH.CONFIGURED_UNSAFE",
                        "REFRESH.MANIFEST_UNSAFE",
                    }:
                        error = ExportPreflightError(
                            "REFRESH.SOURCE_DIVERGED"
                        )
                    return self._export_failure(
                        error,
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=None,
                        destination_observed=None,
                    )
                try:
                    _require_refresh_artifacts_absent(identity, paths)
                except ExportPreflightError as error:
                    observed = _try_file_digest(paths.destination)
                    return self._export_failure(
                        error,
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=destination_before,
                        destination_observed=observed,
                    )
                try:
                    snapshot = store.capture_export_snapshot()
                except (
                    SQLiteStoreLifecycleError,
                    SQLiteStoreSchemaError,
                    sqlite3.DatabaseError,
                ) as error:
                    observed = _try_file_digest(paths.destination)
                    return self._export_failure(
                        error,
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=destination_before,
                        destination_observed=observed,
                    )
                if (
                    snapshot.revision.resource_id
                    != identity.resource_id
                    or snapshot.revision.canonical_store_id
                    != self._canonical_store_id
                ):
                    observed = _try_file_digest(paths.destination)
                    return self._export_failure(
                        ExportPreflightError(
                            "REFRESH.STORE_IDENTITY_MISMATCH"
                        ),
                        stage_label="REFRESH.PREFLIGHT",
                        destination_before=destination_before,
                        destination_observed=observed,
                    )
                return self._publish_export_snapshot(
                    store,
                    snapshot=snapshot,
                    paths=paths,
                    destination_before=destination_before,
                    manifest_before=manifest_before,
                    destination_identity=destination_identity,
                    manifest_identity=manifest_identity,
                    receipt_id_prefix="snapshot.refresh.",
                    stage_prefix="REFRESH",
                    register=lambda receipt, jsonl_temp_identity, manifest_temp_identity, prior, parent_identity: (
                        store.register_issued_refresh_receipt(
                            receipt,
                            expected_generation=(
                                snapshot.revision.generation
                            ),
                            jsonl_temp_identity=jsonl_temp_identity,
                            manifest_temp_identity=manifest_temp_identity,
                            artifact_parent_identity=parent_identity,
                            prior_jsonl_identity=prior[0],
                            prior_jsonl_digest=prior[1],
                            prior_jsonl_absent=prior[2],
                            prior_manifest_identity=prior[3],
                            prior_manifest_digest=prior[4],
                            prior_manifest_absent=prior[5],
                        )
                    ),
                    record_recovery_handoff=(
                        lambda snapshot_id, jsonl_recovery_identity, manifest_recovery_identity: (
                            store.record_export_recovery_handoff(
                                snapshot_id,
                                expected_generation=(
                                    snapshot.revision.generation
                                ),
                                jsonl_recovery_identity=(
                                    jsonl_recovery_identity
                                ),
                                manifest_recovery_identity=(
                                    manifest_recovery_identity
                                ),
                            )
                        )
                    ),
                    complete=lambda snapshot_id, jsonl_identity, manifest_identity: (
                        store.complete_issued_refresh_receipt(
                            snapshot_id,
                            expected_generation=snapshot.revision.generation,
                            jsonl_identity=jsonl_identity,
                            manifest_identity=manifest_identity,
                        )
                    ),
                )
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            if reservation_entered:
                raise
            return self._export_failure(
                error,
                stage_label="REFRESH.PREFLIGHT",
                destination_before=None,
                destination_observed=None,
            )

    def recover_configured_refresh(
        self,
        store: SQLiteTMStore,
    ) -> RefreshRecoveryOutcome:
        """Close every issued snapshot publication crash window.

        Task 5.14 explicit service seam: delegates to the store recovery
        entry (which runs classification and effects under the
        resource-scoped reentrant refresh/observation gate) and
        normalizes the expected lifecycle/schema/database failures to a
        stable ``BLOCKED`` outcome consistent with the existing export
        failure patterns.  Unexpected programmer errors remain visible.
        Recovery is idempotent and never clears a pre-existing
        divergence latch.
        """

        if type(store) is not SQLiteTMStore:
            raise TypeError("store must be exact SQLiteTMStore")
        try:
            return store.recover_configured_refresh()
        except (
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            return RefreshRecoveryOutcome(
                state=RefreshRecoveryState.BLOCKED,
                error_code=_recovery_error_code(error),
                retryable=_recovery_retryable(error),
            )

    def _run_arbitrary_export(
        self,
        store: SQLiteTMStore,
        destination: Path,
    ) -> ExportOutcome:
        identity = self._resource_identity
        paths = _export_artifact_paths(destination)
        destination_before: str | None = None
        manifest_before: str | None = None
        destination_identity: tuple[int, int] | None = None
        manifest_identity: tuple[int, int] | None = None
        try:
            (
                destination_before,
                manifest_before,
                destination_identity,
                manifest_identity,
            ) = _validate_export_destination(identity, paths)
            snapshot = store.capture_export_snapshot()
        except (
            ExportPreflightError,
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
        ) as error:
            observed = _try_file_digest(paths.destination)
            return self._export_failure(
                error,
                stage_label="EXPORT.PREFLIGHT",
                destination_before=destination_before,
                destination_observed=observed,
            )
        if (
            snapshot.revision.resource_id != identity.resource_id
            or snapshot.revision.canonical_store_id
            != self._canonical_store_id
        ):
            observed = _try_file_digest(paths.destination)
            return self._export_failure(
                ExportPreflightError("EXPORT.STORE_IDENTITY_MISMATCH"),
                stage_label="EXPORT.PREFLIGHT",
                destination_before=destination_before,
                destination_observed=observed,
            )
        return self._publish_export_snapshot(
            store,
            snapshot=snapshot,
            paths=paths,
            destination_before=destination_before,
            manifest_before=manifest_before,
            destination_identity=destination_identity,
            manifest_identity=manifest_identity,
            receipt_id_prefix="snapshot.export.",
            stage_prefix="EXPORT",
            register=(
                lambda receipt, jsonl_temp_identity, manifest_temp_identity, prior, parent_identity: (
                    store.register_issued_export_receipt(
                        receipt,
                        destination_jsonl_path=paths.destination,
                        destination_manifest_path=paths.manifest,
                        expected_generation=snapshot.revision.generation,
                        jsonl_temp_identity=jsonl_temp_identity,
                        manifest_temp_identity=manifest_temp_identity,
                        artifact_parent_identity=parent_identity,
                        prior_jsonl_identity=prior[0],
                        prior_jsonl_digest=prior[1],
                        prior_jsonl_absent=prior[2],
                        prior_manifest_identity=prior[3],
                        prior_manifest_digest=prior[4],
                        prior_manifest_absent=prior[5],
                    )
                )
            ),
            record_recovery_handoff=(
                lambda snapshot_id, jsonl_recovery_identity, manifest_recovery_identity: (
                    store.record_export_recovery_handoff(
                        snapshot_id,
                        expected_generation=snapshot.revision.generation,
                        jsonl_recovery_identity=jsonl_recovery_identity,
                        manifest_recovery_identity=manifest_recovery_identity,
                    )
                )
            ),
            complete=lambda snapshot_id, jsonl_identity, manifest_identity: (
                store.complete_issued_export_receipt(
                    snapshot_id,
                    expected_generation=snapshot.revision.generation,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )
            ),
        )

    def _publish_export_snapshot(
        self,
        store: SQLiteTMStore,
        *,
        snapshot: CanonicalExportSnapshot,
        paths: _ExportArtifactPaths,
        destination_before: str | None,
        manifest_before: str | None,
        destination_identity: tuple[int, int] | None,
        manifest_identity: tuple[int, int] | None,
        receipt_id_prefix: str,
        stage_prefix: str,
        register: Callable[
            [
                SnapshotReceipt,
                tuple[int, int],
                tuple[int, int],
                tuple[
                    tuple[int, int] | None,
                    str | None,
                    bool | None,
                    tuple[int, int] | None,
                    str | None,
                    bool | None,
                ],
                tuple[int, int],
            ],
            None,
        ],
        record_recovery_handoff: Callable[
            [str, tuple[int, int] | None, tuple[int, int] | None],
            None,
        ],
        complete: Callable[
            [str, tuple[int, int], tuple[int, int]],
            None,
        ],
    ) -> ExportOutcome:
        """Run the shared Task 5.12 publication protocol for one pair.

        The caller has already validated the destination state and
        captured the canonical snapshot.  Before the first JSONL
        temporary creation the full parent chain is validated and the
        immediate parent is opened with ``O_DIRECTORY|O_NOFOLLOW`` and
        retained as ``_ExportParentHandle``: its exact device/inode is
        the pre-temp identity durably persisted by the issued
        registration, and every artifact create/open/verify/copy/
        replace/restore/cleanup for the six deterministic names runs
        relative to the retained descriptor with no-follow semantics and
        never re-resolves the parent pathname for destructive work.  The
        retained descriptor is fsynced for publication/cleanup
        durability and the advertised full parent pathname is re-proven
        to still resolve to that identity at the required boundaries; a
        mismatch fails closed.  The method then writes and verifies the
        deterministic JSONL/manifest temporary pair, registers exactly
        one issued receipt, publishes in the fixed JSONL replace /
        parent fsync / manifest replace / parent fsync order,
        re-verifies the published pair, and atomically completes the
        receipt.  Any caught failure restores the exact prior pair (or
        original absence), cancels the issued receipt after a complete
        restore, cleans only inode-proven artifacts, and returns the
        shared failure contract.
        """

        jsonl_temp_identity: _CreatedFileIdentity | None = None
        manifest_temp_identity: _CreatedFileIdentity | None = None
        jsonl_recovery_identity: _CreatedFileIdentity | None = None
        manifest_recovery_identity: _CreatedFileIdentity | None = None
        jsonl_published_identity: tuple[int, int] | None = None
        manifest_published_identity: tuple[int, int] | None = None
        jsonl_digest: str | None = None
        manifest_digest: str | None = None
        record_count = len(snapshot.records)
        issued = False
        complete_called = False
        receipt: SnapshotReceipt | None = None
        parent_handle: _ExportParentHandle | None = None
        try:
            parent_handle = _ExportParentHandle.bind(paths.destination)
            jsonl_digest, record_count, jsonl_temp_identity = (
                _stream_export_jsonl_temp(
                    paths.jsonl_temp,
                    snapshot.records,
                    parent_handle=parent_handle,
                )
            )
            _verify_export_jsonl_temp(
                paths.jsonl_temp,
                expected_digest=jsonl_digest,
                expected_count=record_count,
                identity=jsonl_temp_identity,
                parent_handle=parent_handle,
            )
            receipt = SnapshotReceipt(
                snapshot_id=f"{receipt_id_prefix}{uuid.uuid4().hex}",
                resource_id=snapshot.revision.resource_id,
                canonical_store_id=snapshot.revision.canonical_store_id,
                exported_revision=snapshot.revision.head_revision,
                jsonl_digest=jsonl_digest,
                record_count=record_count,
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                receipt=receipt,
                receipt_digest=snapshot_receipt_digest(receipt),
            )
            manifest_bytes = contract_to_json(manifest).encode("utf-8")
            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            manifest_temp_identity = _write_export_payload_temp(
                paths.manifest_temp,
                manifest_bytes,
                parent_handle=parent_handle,
            )
            _verify_export_payload_temp(
                paths.manifest_temp,
                expected_bytes=manifest_bytes,
                identity=manifest_temp_identity,
                parent_handle=parent_handle,
            )
            assert jsonl_temp_identity is not None
            assert manifest_temp_identity is not None
            artifact_parent_identity = parent_handle.identity
            register(
                receipt,
                (
                    jsonl_temp_identity.device,
                    jsonl_temp_identity.inode,
                ),
                (
                    manifest_temp_identity.device,
                    manifest_temp_identity.inode,
                ),
                (
                    destination_identity,
                    destination_before,
                    (
                        True
                        if destination_before is None
                        else False
                    ),
                    manifest_identity,
                    manifest_before,
                    True if manifest_before is None else False,
                ),
                artifact_parent_identity,
            )
            issued = True
            jsonl_recovery_identity, manifest_recovery_identity = (
                _copy_export_prior_pair(
                    paths,
                    destination_before=destination_before,
                    manifest_before=manifest_before,
                    parent_handle=parent_handle,
                )
            )
            record_recovery_handoff(
                receipt.snapshot_id,
                (
                    None
                    if jsonl_recovery_identity is None
                    else (
                        jsonl_recovery_identity.device,
                        jsonl_recovery_identity.inode,
                    )
                ),
                (
                    None
                    if manifest_recovery_identity is None
                    else (
                        manifest_recovery_identity.device,
                        manifest_recovery_identity.inode,
                    )
                ),
            )
            assert jsonl_temp_identity is not None
            _replace_path(
                paths.jsonl_temp,
                paths.destination,
                expected_source_identity=(
                    jsonl_temp_identity.device,
                    jsonl_temp_identity.inode,
                ),
                expected_destination_digest=destination_before,
                expected_destination_identity=destination_identity,
                parent_handle=parent_handle,
            )
            observed_jsonl_identity = _published_file_identity(
                paths.destination,
                jsonl_digest,
                parent_handle=parent_handle,
            )
            if observed_jsonl_identity != (
                jsonl_temp_identity.device,
                jsonl_temp_identity.inode,
            ):
                raise ExportPreflightError(
                    "EXPORT.PUBLISH_VERIFY_FAILED"
                )
            jsonl_published_identity = observed_jsonl_identity
            _fsync_directory(
                paths.destination.parent,
                parent_handle=parent_handle,
            )
            assert manifest_temp_identity is not None
            _replace_path(
                paths.manifest_temp,
                paths.manifest,
                expected_source_identity=(
                    manifest_temp_identity.device,
                    manifest_temp_identity.inode,
                ),
                expected_destination_digest=manifest_before,
                expected_destination_identity=manifest_identity,
                parent_handle=parent_handle,
            )
            observed_manifest_identity = _published_file_identity(
                paths.manifest,
                manifest_digest,
                parent_handle=parent_handle,
            )
            if observed_manifest_identity != (
                manifest_temp_identity.device,
                manifest_temp_identity.inode,
            ):
                raise ExportPreflightError(
                    "EXPORT.PUBLISH_VERIFY_FAILED"
                )
            manifest_published_identity = observed_manifest_identity
            _fsync_directory(
                paths.destination.parent,
                parent_handle=parent_handle,
            )
            _verify_export_pair(
                paths,
                jsonl_digest=jsonl_digest,
                manifest_bytes=manifest_bytes,
                jsonl_identity=(
                    jsonl_temp_identity.device,
                    jsonl_temp_identity.inode,
                ),
                manifest_identity=(
                    manifest_temp_identity.device,
                    manifest_temp_identity.inode,
                ),
                parent_handle=parent_handle,
            )
            complete_called = True
            complete(
                receipt.snapshot_id,
                (
                    jsonl_temp_identity.device,
                    jsonl_temp_identity.inode,
                ),
                (
                    manifest_temp_identity.device,
                    manifest_temp_identity.inode,
                ),
            )
            cleanup_pending = False
            try:
                remaining = _cleanup_export_artifacts(
                    paths,
                    jsonl_temp_identity=None,
                    manifest_temp_identity=None,
                    jsonl_recovery_identity=jsonl_recovery_identity,
                    manifest_recovery_identity=manifest_recovery_identity,
                    parent_handle=parent_handle,
                )
                if remaining:
                    cleanup_pending = True
                else:
                    _fsync_directory(
                        paths.destination.parent,
                        parent_handle=parent_handle,
                    )
                    store.clear_issued_receipt_handoff(
                        receipt.snapshot_id,
                        expected_generation=snapshot.revision.generation,
                    )
            except (
                SQLiteStoreLifecycleError,
                SQLiteStoreSchemaError,
                sqlite3.DatabaseError,
                OSError,
            ):
                cleanup_pending = True
            if cleanup_pending:
                return self._cleanup_pending_failure(
                    stage_prefix=stage_prefix,
                    paths=paths,
                    destination_before=destination_before,
                )
        except (
            ExportPreflightError,
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
            OSError,
        ) as error:
            if complete_called:
                assert receipt is not None
                window_outcome = self._post_completion_exception_window(
                    store,
                    snapshot=snapshot,
                    paths=paths,
                    stage_prefix=stage_prefix,
                    receipt=receipt,
                    jsonl_temp_identity=jsonl_temp_identity,
                    manifest_temp_identity=manifest_temp_identity,
                    jsonl_recovery_identity=jsonl_recovery_identity,
                    manifest_recovery_identity=manifest_recovery_identity,
                    jsonl_digest=jsonl_digest,
                    record_count=record_count,
                    destination_before=destination_before,
                    error=error,
                    parent_handle=parent_handle,
                )
                if window_outcome is not None:
                    return window_outcome
            restore_error: Exception | None = None
            failure_stage = f"{stage_prefix}.PUBLISH"
            if issued:
                try:
                    _restore_export_pair(
                        paths,
                        destination_before=destination_before,
                        manifest_before=manifest_before,
                        jsonl_published_identity=(
                            jsonl_published_identity
                        ),
                        manifest_published_identity=(
                            manifest_published_identity
                        ),
                        jsonl_digest=jsonl_digest,
                        manifest_digest=manifest_digest,
                        parent_handle=parent_handle,
                    )
                except Exception as restore_exception:
                    restore_error = restore_exception
                    failure_stage = f"{stage_prefix}.RESTORE"
            observed = _try_file_digest(paths.destination)
            keep_recovery = restore_error is not None
            cleanup_remaining = _cleanup_export_artifacts(
                paths,
                jsonl_temp_identity=jsonl_temp_identity,
                manifest_temp_identity=manifest_temp_identity,
                jsonl_recovery_identity=(
                    None
                    if keep_recovery
                    else jsonl_recovery_identity
                ),
                manifest_recovery_identity=(
                    None
                    if keep_recovery
                    else manifest_recovery_identity
                ),
                parent_handle=parent_handle,
            )
            ledger_error: Exception | None = None
            handoff_release_failed = False
            if issued and restore_error is None:
                assert receipt is not None
                try:
                    store.cancel_issued_export_receipt(
                        receipt.snapshot_id,
                        expected_generation=(
                            snapshot.revision.generation
                        ),
                    )
                except Exception as cancel_exception:
                    ledger_error = cancel_exception
                    failure_stage = f"{stage_prefix}.LEDGER"
                else:
                    if not cleanup_remaining:
                        try:
                            _fsync_directory(
                                paths.destination.parent,
                                parent_handle=parent_handle,
                            )
                            store.clear_issued_receipt_handoff(
                                receipt.snapshot_id,
                                expected_generation=(
                                    snapshot.revision.generation
                                ),
                            )
                        except (
                            OSError,
                            SQLiteStoreLifecycleError,
                            SQLiteStoreSchemaError,
                            sqlite3.DatabaseError,
                        ):
                            handoff_release_failed = True
                            ledger_error = ExportPreflightError(
                                "EXPORT.CLEANUP_PENDING"
                            )
                            failure_stage = f"{stage_prefix}.LEDGER"
            diagnostics: list[ExportDiagnostic] = []
            if cleanup_remaining or handoff_release_failed:
                diagnostics.append(
                    _export_diagnostic(
                        "EXPORT.CLEANUP_PENDING",
                        "EXPORT_ARTIFACTS_REMAIN",
                    )
                )
            if restore_error is not None:
                diagnostics.append(
                    _export_diagnostic(
                        "EXPORT.RESTORE_FAILED",
                        "EXPORT_RESTORE_INCOMPLETE",
                    )
                )
            diagnostics.sort(key=lambda item: item.code)
            return self._export_failure(
                (
                    restore_error
                    if restore_error is not None
                    else ledger_error
                    if ledger_error is not None
                    else error
                ),
                stage_label=failure_stage,
                destination_before=destination_before,
                destination_observed=observed,
                diagnostics=tuple(diagnostics),
                recovery_candidate=paths.jsonl_recovery,
            )
        finally:
            if parent_handle is not None:
                parent_handle.close()
        assert jsonl_digest is not None
        assert receipt is not None
        return self._export_report(
            record_count=record_count,
            jsonl_digest=jsonl_digest,
            snapshot=snapshot,
            receipt=receipt,
            diagnostics=(),
        )

    def _cleanup_pending_failure(
        self,
        *,
        stage_prefix: str,
        paths: _ExportArtifactPaths,
        destination_before: str | None,
        extra_diagnostics: tuple[ExportDiagnostic, ...] = (),
    ) -> ExportFailure:
        """Build the explicit ``EXPORT.CLEANUP_PENDING`` failure.

        Delegates to the public cleanup-pending outcome builder: the
        receipt is already terminal and the published pair is the
        durable truth, so ``publication_committed`` is True, the failure
        is fail-stop and never carries a recovery locator (the old
        destination is never restored).  The before/observed digests are
        retained whenever known; a fresh prior-absent destination is
        ``NOT_APPLICABLE``.  Only code-only diagnostics are emitted.
        """

        return export_cleanup_pending_failure(
            stage=f"{stage_prefix}.LEDGER",
            destination_before=destination_before,
            destination_observed=_try_file_digest(paths.destination),
            diagnostics=(
                _export_diagnostic(
                    "EXPORT.CLEANUP_PENDING",
                    "EXPORT_ARTIFACTS_REMAIN",
                ),
            )
            + tuple(extra_diagnostics),
        )

    def _post_completion_exception_window(
        self,
        store: SQLiteTMStore,
        *,
        snapshot: CanonicalExportSnapshot,
        paths: _ExportArtifactPaths,
        stage_prefix: str,
        receipt: SnapshotReceipt,
        jsonl_temp_identity: _CreatedFileIdentity | None,
        manifest_temp_identity: _CreatedFileIdentity | None,
        jsonl_recovery_identity: _CreatedFileIdentity | None,
        manifest_recovery_identity: _CreatedFileIdentity | None,
        jsonl_digest: str | None,
        record_count: int,
        destination_before: str | None,
        error: Exception,
        parent_handle: _ExportParentHandle | None = None,
    ) -> ExportOutcome | None:
        """Resolve the Task 5.13 ambiguous post-completion exception window.

        The completion call may have committed before raising an outward
        exception.  Before any rollback/cancel the durable ledger,
        binding and published pair are inspected: when the completion
        actually committed the refresh/export is reported as success and
        the old pair is never restored nor the completed receipt
        cancelled; a committed-but-unclean window fails closed with
        evidence preserved; only a provably not-committed window returns
        ``None`` so the ordinary known-before-commit restore/cancel
        behavior continues.  When the probe itself raises, the ledger
        truth is unknown: every probe exception is routed through the
        dedicated ``export_ledger_ambiguous_failure`` builder, which
        fails stop at ``<stage_prefix>.LEDGER`` with
        ``publication_commit_ambiguous`` True, ``publication_committed``
        False, code-only diagnostics, and never fabricates a recovery
        locator (``NOT_APPLICABLE`` only when no prior destination;
        otherwise every known before/observed digest is preserved as
        truthful VERIFIED_CHANGED/UNVERIFIED evidence).  No
        ``ExportPreflightError`` can escape the outcome.
        """

        try:
            probe = store.probe_issued_receipt_completed(
                receipt.snapshot_id,
                expected_generation=snapshot.revision.generation,
                require_bound=(stage_prefix == "REFRESH"),
            )
        except Exception as probe_error:
            return export_ledger_ambiguous_failure(
                stage=f"{stage_prefix}.LEDGER",
                error_code=_export_error_code(probe_error),
                destination_before=destination_before,
                destination_observed=_try_file_digest(paths.destination),
                diagnostics=(
                    _export_diagnostic(
                        "EXPORT.LEDGER_AMBIGUOUS",
                        "EXPORT_LEDGER_AMBIGUOUS",
                    ),
                ),
            )
        if probe is ReceiptCompletionProbe.COMMITTED:
            try:
                remaining = _cleanup_export_artifacts(
                    paths,
                    jsonl_temp_identity=jsonl_temp_identity,
                    manifest_temp_identity=manifest_temp_identity,
                    jsonl_recovery_identity=jsonl_recovery_identity,
                    manifest_recovery_identity=manifest_recovery_identity,
                    parent_handle=parent_handle,
                )
                if not remaining:
                    _fsync_directory(
                        paths.destination.parent,
                        parent_handle=parent_handle,
                    )
                    try:
                        store.clear_issued_receipt_handoff(
                            receipt.snapshot_id,
                            expected_generation=(
                                snapshot.revision.generation
                            ),
                        )
                    except SQLiteStoreSchemaError as clear_error:
                        if str(clear_error) != "STORE.HANDOFF_MISSING":
                            raise
            except (
                SQLiteStoreLifecycleError,
                SQLiteStoreSchemaError,
                sqlite3.DatabaseError,
                OSError,
            ):
                return self._cleanup_pending_failure(
                    stage_prefix=stage_prefix,
                    paths=paths,
                    destination_before=destination_before,
                )
            if remaining:
                return self._cleanup_pending_failure(
                    stage_prefix=stage_prefix,
                    paths=paths,
                    destination_before=destination_before,
                )
            assert jsonl_digest is not None
            return self._export_report(
                record_count=record_count,
                jsonl_digest=jsonl_digest,
                snapshot=snapshot,
                receipt=receipt,
                diagnostics=(),
            )
        if probe is ReceiptCompletionProbe.COMMITTED_UNCLEAN:
            return self._cleanup_pending_failure(
                stage_prefix=stage_prefix,
                paths=paths,
                destination_before=destination_before,
                extra_diagnostics=(
                    _export_diagnostic(
                        "EXPORT.LEDGER_UNCLEAN",
                        "EXPORT_LEDGER_UNCLEAN",
                    ),
                ),
            )
        return None

    def _export_report(
        self,
        *,
        record_count: int,
        jsonl_digest: str,
        snapshot: CanonicalExportSnapshot,
        receipt: SnapshotReceipt,
        diagnostics: tuple[ExportDiagnostic, ...],
    ) -> ExportReport:
        """Build the shared success report from one durable receipt."""

        return ExportReport(
            exported_count=record_count,
            skipped_count=0,
            destination_digest=jsonl_digest,
            canonical_generation=snapshot.revision.generation,
            exported_revision=snapshot.revision.head_revision,
            snapshot_id=receipt.snapshot_id,
            snapshot_receipt_digest=snapshot_receipt_digest(receipt),
            snapshot_receipt=receipt,
            diagnostics=diagnostics,
        )

    def _export_failure(
        self,
        error: Exception,
        *,
        stage_label: str,
        destination_before: str | None,
        destination_observed: str | None,
        diagnostics: tuple[ExportDiagnostic, ...] = (),
        recovery_candidate: Path | None = None,
    ) -> ExportFailure:
        """Build one digest-backed export failure without leaking paths.

        The destination preservation evidence is derived from the exact
        before/observed digests; when the prior bytes cannot be proven at
        the destination, the only honest locator is the owned recovery
        copy holding the exact prior digest.  An unprovable locator for a
        changed/unverified destination fails stop
        (``EXPORT.PRIOR_STATE_UNRECOVERABLE``) instead of fabricating one.
        """

        error_code = _export_error_code(error)
        retryable = _export_retryable(error)
        locators: list[RecoveryLocator] = []
        recovery_digest = destination_before

        def require_locator(selection: _LocatorSelection | None) -> None:
            nonlocal retryable
            if selection is None or recovery_digest is None:
                raise ExportPreflightError(
                    "EXPORT.PRIOR_STATE_UNRECOVERABLE"
                )
            _require_locator_return_proof(
                selection,
                recovery_digest,
                "EXPORT.PRIOR_STATE_UNRECOVERABLE",
            )
            locators.append(
                RecoveryLocator(
                    path=selection.path,
                    asset_kind=AssetKind.EXPORT_DESTINATION,
                    expected_digest=recovery_digest,
                )
            )
            retryable = False

        if destination_before is None:
            if destination_observed is None:
                evidence = AssetPreservationEvidence(
                    asset_kind=AssetKind.EXPORT_DESTINATION,
                    state=AssetPreservationState.NOT_APPLICABLE,
                    before_digest=None,
                    observed_digest=None,
                )
            else:
                raise ExportPreflightError(
                    "EXPORT.PRIOR_STATE_UNRECOVERABLE"
                )
        elif destination_observed == destination_before:
            evidence = _unchanged_preservation(
                AssetKind.EXPORT_DESTINATION,
                destination_before,
            )
        elif destination_observed is not None:
            evidence = _changed_preservation(
                AssetKind.EXPORT_DESTINATION,
                destination_before,
                destination_observed,
            )
            require_locator(
                None
                if recovery_candidate is None
                else _proven_live_locator(
                    recovery_candidate,
                    destination_before,
                )
            )
        else:
            evidence = _unverified_preservation(
                AssetKind.EXPORT_DESTINATION,
                destination_before,
            )
            require_locator(
                None
                if recovery_candidate is None
                else _proven_live_locator(
                    recovery_candidate,
                    destination_before,
                )
            )
        return ExportFailure(
            stage=stage_label,
            error_code=error_code,
            retryable=retryable,
            diagnostics=diagnostics,
            previous_destination_preservation=evidence,
            recovery_locators=tuple(locators),
        )

    def import_snapshot(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        """Explicitly replace the active canonical with one exact snapshot.

        Task 5.10 disambiguation entry point: the source must be the
        explicitly chosen configured JSONL.  The operation builds a fresh
        mutable stage with a brand-new canonical store id, seals it, and
        drives the live coordinator's explicit replacement activation
        pipeline.  On success the resource gets a new generation with the
        new store id and binding and any ``SOURCE_DIVERGED`` state is
        cleared; on failure the prior canonical, JSONL bytes, manifest
        bytes, and divergence state are preserved.
        """

        return self._explicit_disambiguation(source, resource_id)

    def rebuild_from_snapshot(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        """Explicitly rebuild the active canonical from one exact snapshot.

        Semantically identical to :meth:`import_snapshot`: both operations
        perform the same full-replacement safety contract and differ only
        in the caller's intent.
        """

        return self._explicit_disambiguation(source, resource_id)

    def upgrade_schema(
        self,
        store_path: Path,
    ) -> SchemaUpgradeOutcome:
        """Copy-switch one old-schema canonical to the current schema.

        Task 5.11 entry point: the coordinator proves the prior v1
        ancestry and validates the complete binding/manifest/receipt/
        source/divergence closure, then mints one single-use snapshot
        ticket backed by a ``Connection.backup()`` recovery backup of
        the active old-schema store.  A fresh mutable copy of the live
        store is migrated in place (proven completion order, records
        preserved verbatim, candidate indexes rebuilt) and sealed in the
        private schema-upgrade mode; the existing seal/activate pipeline
        then publishes the equivalent new generation under the same
        canonical store id guarded by the ticket.  The old store is never
        mutated in place; every failure stage leaves it byte-identical
        and reopenable, divergence/tampering/unprovable order fails
        closed and is never repaired, and the recovery backup is
        reported as digest-backed restoration evidence.
        """

        coordinator = self._coordinator
        if coordinator is None:
            raise MigrationPreflightError("SCHEMA.COORDINATOR_UNAVAILABLE")
        active_store_path = coordinator.active_store_path
        if (
            type(store_path) is not _NATIVE_PATH_TYPE
            or active_store_path is None
            or store_path != active_store_path
        ):
            raise MigrationPreflightError("SCHEMA.ACTIVE_STORE_REQUIRED")
        store_before = _try_file_digest(store_path)
        if store_before is None:
            raise MigrationPreflightError("SCHEMA.ACTIVE_STORE_UNREADABLE")
        prior_generation = coordinator.current_generation
        if prior_generation is None:
            raise MigrationPreflightError("SCHEMA.ACTIVE_RESOURCE_REQUIRED")

        stage_label = "PREFLIGHT"
        ticket: _SchemaUpgradeSnapshotTicket | None = None
        backup_path: Path | None = None
        backup_identity: tuple[int, int] | None = None
        backup_digest: str | None = None
        prepared: _ActivationPreparation | None = None
        sealed: SealedStage | None = None
        copy_stage: MutableStageRef | None = None
        stage_identity: _CreatedFileIdentity | None = None
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            if coordinator.canonical_store_id != self._canonical_store_id:
                raise MigrationPreflightError("SCHEMA.COORDINATOR_MISMATCH")
            if _schema_version_of_store(store_path) == TM_SCHEMA_VERSION:
                raise MigrationPreflightError(
                    "SCHEMA.SCHEMA_ALREADY_CURRENT"
                )
            activation_digest = _read_active_activation_digest(store_path)
            schema = inspect_stage_schema(
                _upgrade_source_ref(
                    self._resource_identity,
                    store_path,
                ),
                canonical_store_id=self._canonical_store_id,
                _allow_legacy_schema=True,
                _allow_active=True,
                _expected_active_generation=prior_generation,
                _expected_activation_digest=activation_digest,
            )
            if schema.schema_version != TM_LEGACY_SCHEMA_VERSION:
                raise MigrationPreflightError("SCHEMA.SCHEMA_UNSUPPORTED")
            receipt, manifest_kind = _read_legacy_snapshot_facts(
                store_path,
                canonical_store_id=self._canonical_store_id,
            )
            ticket = coordinator.prepare_schema_upgrade_ticket()
            backup_path = ticket.backup_path
            backup_identity = ticket.backup_identity
            backup_digest = ticket.backup_digest
            # The ticket seam is the stabilization point.  Build and inspect
            # the candidate only from that durable recovery snapshot; the
            # live canonical is used again solely by the post-drain ticket
            # guard, which rejects any write that happened after capture.
            activation_digest = _read_active_activation_digest(backup_path)
            stage_label = "COPY"
            copy_stage = _deterministic_stage_ref(
                self._resource_identity,
                source_digest=receipt.jsonl_digest,
                stage_prefix="schema-upgrade",
                path_salt=f"upgrade.{uuid.uuid4().hex}",
            )
            stage_identity = _copy_store_into_stage(
                backup_path,
                copy_stage.staged_db_path,
            )
            inspect_stage_schema(
                copy_stage,
                canonical_store_id=self._canonical_store_id,
                _allow_legacy_schema=True,
                _allow_active=True,
                _expected_active_generation=prior_generation,
                _expected_activation_digest=activation_digest,
            )
            with _open_configured_connection(
                copy_stage.staged_db_path,
                require_existing=True,
            ) as connection:
                _migrate_schema_copy(
                    connection,
                    fts5_available=schema.fts5_available,
                )
            inspect_stage_schema(
                copy_stage,
                canonical_store_id=self._canonical_store_id,
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=manifest_kind,
                receipt=receipt,
                receipt_digest=snapshot_receipt_digest(receipt),
            )
            manifest_identity = _write_new_file(
                copy_stage.manifest_temp_path,
                contract_to_json(manifest).encode("utf-8"),
            )
            stage_label = "ACTIVATION"
            sealed = coordinator._seal_stage(
                copy_stage,
                canonical_store_id=self._canonical_store_id,
                expected_prior_generation=prior_generation,
                schema_upgrade=True,
            )
            prepared = coordinator.activate(
                sealed,
                _schema_upgrade_ticket=ticket,
            )
            ticket = None
            handle = coordinator.publish_prepared_activation(prepared)
            generation = coordinator.publish_activation(prepared, handle)
            published_store_path = coordinator.active_store_path
            if published_store_path is None:
                raise MigrationPreflightError(
                    "SCHEMA.SUCCESS_UNVERIFIABLE"
                )
            success_digest = _try_file_digest(published_store_path)
            if success_digest is None:
                raise MigrationPreflightError(
                    "SCHEMA.SUCCESS_UNVERIFIABLE"
                )
            if (
                backup_path is None
                or backup_identity is None
                or backup_digest is None
            ):
                raise MigrationPreflightError(
                    "SCHEMA.BACKUP_UNVERIFIABLE"
                )
            return self._schema_upgrade_report(
                generation=generation,
                backup_path=backup_path,
                backup_identity=backup_identity,
                backup_digest=backup_digest,
                success_digest=success_digest,
            )
        except BaseException as error:
            if ticket is not None:
                try:
                    coordinator.retire_schema_upgrade_ticket(ticket)
                except Exception as retire_error:
                    if isinstance(error, Exception):
                        error = retire_error
            if (
                copy_stage is not None
                and stage_identity is not None
                and sealed is None
            ):
                if manifest_identity is not None:
                    _remove_created_file(
                        copy_stage.manifest_temp_path,
                        manifest_identity,
                    )
                _remove_created_file(
                    copy_stage.staged_db_path,
                    stage_identity,
                )
            if not isinstance(error, Exception):
                raise
            if prepared is not None:
                return self._reconcile_failed_upgrade_activation(
                    error,
                    prepared=prepared,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    backup_digest=backup_digest,
                )
            if coordinator.state == "ACTIVATING":
                # The preparation failed before any durable journal but
                # its cleanup reservation is still pending: retry the
                # narrow cleanup (or honestly fail stop on that cleanup).
                try:
                    coordinator.retry_failed_activation_cleanup()
                except Exception as cleanup_error:
                    return self._schema_upgrade_failure(
                        cleanup_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                    )
            return self._schema_upgrade_failure(
                error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
            )

    def _explicit_disambiguation(
        self,
        source: Path,
        resource_id: str,
    ) -> MigrationOutcome:
        coordinator = self._coordinator
        stage_label = "PREFLIGHT"
        preflight: MigrationPreflight | None = None
        source_before = _try_file_digest(
            self._resource_identity.configured_jsonl_path
        )
        if source_before is None:
            raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE")
        if coordinator is None:
            raise MigrationPreflightError("IMPORT.COORDINATOR_UNAVAILABLE")
        store_path = coordinator.active_store_path
        if store_path is None:
            raise MigrationPreflightError("IMPORT.ACTIVE_RESOURCE_REQUIRED")
        store_before = _try_file_digest(
            store_path
        )
        if store_before is None:
            raise MigrationPreflightError("IMPORT.ACTIVE_STORE_UNREADABLE")
        prepared = None
        new_store_id: str | None = None
        sealed: SealedStage | None = None
        stage: MutableStageRef | None = None
        stage_identity: _CreatedFileIdentity | None = None
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            if (
                type(resource_id) is not str
                or not resource_id.strip()
            ):
                raise MigrationPreflightError("IMPORT.RESOURCE_ID_INVALID")
            if resource_id != self._resource_identity.resource_id:
                raise MigrationPreflightError(
                    "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
                )
            if coordinator.canonical_store_id != self._canonical_store_id:
                raise MigrationPreflightError("IMPORT.COORDINATOR_MISMATCH")
            prior_generation = coordinator.current_generation
            if prior_generation is None:
                raise MigrationPreflightError(
                    "IMPORT.ACTIVE_RESOURCE_REQUIRED"
                )
            self._validate_source_preconditions(source)
            preflight = _scan_jsonl(source)
            origin_token = uuid.uuid4().hex
            new_store_id = f"store.import.{origin_token}"
            stage, stage_identity, manifest_identity = self._build_stage(
                source,
                preflight=preflight,
                canonical_store_id=new_store_id,
                batch_kind="import",
                batch_prefix="import",
                snapshot_prefix="snapshot.import",
                stage_prefix="import",
                path_salt=uuid.uuid4().hex,
                batch_id=f"import.{origin_token}",
            )
            stage_label = "ACTIVATION"
            sealed = coordinator._seal_stage(
                stage,
                canonical_store_id=new_store_id,
                expected_prior_generation=prior_generation,
            )
            prepared = coordinator.activate_replacement(sealed)
            handle = coordinator.publish_prepared_activation(prepared)
            generation = coordinator.publish_activation(prepared, handle)
            report = self._success_report(
                preflight=preflight,
                sealed=sealed,
                canonical_store_id=new_store_id,
                generation=generation,
            )
            # The publication is durably READY: the same service instance
            # adopts the fresh canonical store id so a second explicit
            # import/rebuild of the identical bytes succeeds as the next
            # generation without a caller-side service renewal.
            self._canonical_store_id = new_store_id
            return report
        except Exception as error:
            if (
                stage is not None
                and stage_identity is not None
                and sealed is None
            ):
                if manifest_identity is not None:
                    _remove_created_file(
                        stage.manifest_temp_path,
                        manifest_identity,
                    )
                _remove_created_file(
                    stage.staged_db_path,
                    stage_identity,
                )
            if prepared is not None:
                return self._reconcile_failed_activation(
                    error,
                    prepared=prepared,
                    sealed=sealed,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    new_store_id=new_store_id,
                )
            return self._disambiguation_failure(
                error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
            )

    def _success_report(
        self,
        *,
        preflight: MigrationPreflight,
        sealed: SealedStage,
        canonical_store_id: str,
        generation: int,
    ) -> MigrationReport:
        """Build one completed import report from sealed evidence."""

        receipt = sealed.evidence.source_binding.receipt
        if (
            type(receipt) is not SnapshotReceipt
            or receipt.canonical_store_id != canonical_store_id
        ):
            raise MigrationPreflightError("IMPORT.RECEIPT_MISMATCH")
        return MigrationReport(
            resource_id=self._resource_identity.resource_id,
            canonical_store_id=canonical_store_id,
            source_digest=preflight.source_digest,
            snapshot_receipt=receipt,
            migrated_count=preflight.valid_count,
            variant_count=preflight.variant_count,
            skipped_count=preflight.invalid_count,
            diagnostics=preflight.diagnostics,
            activated_generation=generation,
            canonical_exact_available=True,
            context_available=False,
            fuzzy_available=False,
        )

    def _reconcile_failed_activation(
        self,
        error: Exception,
        *,
        prepared: _ActivationPreparation,
        sealed: SealedStage | None,
        preflight: MigrationPreflight | None,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        source_before: str,
        store_before: str,
        store_path: Path,
        new_store_id: str | None,
    ) -> MigrationOutcome:
        """Auto-restore the prior READY service after one failed activation.

        Task 5.10 failure reconciliation: a failure before any durable
        journal cancels the live preparation (no journal means nothing was
        replaced), a durable pending journal (PREPARED through
        MANIFEST_PUBLISHED) is rolled back, and a durable
        ``GENERATION_PUBLISHED`` journal whose candidate active set is
        provable completes via fresh-coordinator recovery and reports
        success.  A rollback that cannot be proven fails stop with honest
        unverified preservation evidence and never claims VERIFIED_UNCHANGED.
        """

        try:
            journal_phase = coordinator.durable_activation_phase
        except Exception as phase_error:
            return self._disambiguation_failure(
                phase_error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
                force_unverified=True,
            )
        if journal_phase is None:
            try:
                coordinator.cancel_prepared_activation(prepared)
            except Exception as cancel_error:
                if _disambiguation_error_code(
                    cancel_error
                ) != "ACTIVATION.PREPARATION_NOT_ACTIVE":
                    return self._disambiguation_failure(
                        cancel_error,
                        preflight=preflight,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        source_before=source_before,
                        store_before=store_before,
                        store_path=store_path,
                        force_unverified=True,
                    )
            return self._disambiguation_failure(
                error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
            )
        if journal_phase == "GENERATION_PUBLISHED":
            try:
                coordinator.rollback_durable_activation()
            except Exception as rollback_error:
                if _disambiguation_error_code(
                    rollback_error
                ) != "ACTIVATION.ROLLBACK_COMPLETED_INVALID":
                    return self._disambiguation_failure(
                        rollback_error,
                        preflight=preflight,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        source_before=source_before,
                        store_before=store_before,
                        store_path=store_path,
                        force_unverified=True,
                    )
            else:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                )
            # The candidate active set is proven and the completed journal
            # is the cold-recovery authority: a fresh coordinator bound to
            # the unchanged prior id re-proves and completes the candidate
            # publication (crash-window recovery), then the high-level
            # operation reports the completed import.
            if preflight is None or new_store_id is None:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            recovery_coordinator = ResourceStoreCoordinator(
                canonical_store_id=coordinator.canonical_store_id,
                resource_identity=self._resource_identity,
            )
            report = recovery_coordinator.recover_durable_activation()
            if (
                report is None
                or getattr(report, "action", None) != "COMPLETED"
                or report.generation is None
                or recovery_coordinator.state != "READY"
                or recovery_coordinator.canonical_store_id != new_store_id
                or recovery_coordinator.current_generation
                != report.generation
            ):
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            if sealed is None:
                return self._disambiguation_failure(
                    error,
                    preflight=preflight,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    source_before=source_before,
                    store_before=store_before,
                    store_path=store_path,
                    force_unverified=True,
                )
            # The crash-window recovery completed the candidate
            # publication through a fresh coordinator bound to the
            # unchanged prior id.  The original coordinator adopts the
            # recovered authority (view, store id, generation) through
            # its narrow coordinator-owned transition and returns to
            # READY so the same live service keeps serving the completed
            # import without a caller-side restart.
            coordinator.adopt_recovered_authority(recovery_coordinator)
            adopted_report = self._success_report(
                preflight=preflight,
                sealed=sealed,
                canonical_store_id=new_store_id,
                generation=report.generation,
            )
            # The recovered publication is durably READY: the same service
            # instance adopts the fresh canonical store id so subsequent
            # explicit imports reuse this service without renewal.
            self._canonical_store_id = new_store_id
            return adopted_report
        try:
            coordinator.rollback_durable_activation()
        except Exception as rollback_error:
            return self._disambiguation_failure(
                rollback_error,
                preflight=preflight,
                stage_label=stage_label,
                coordinator=coordinator,
                source_before=source_before,
                store_before=store_before,
                store_path=store_path,
                force_unverified=True,
            )
        return self._disambiguation_failure(
            error,
            preflight=preflight,
            stage_label=stage_label,
            coordinator=coordinator,
            source_before=source_before,
            store_before=store_before,
            store_path=store_path,
        )

    def _disambiguation_failure(
        self,
        error: Exception,
        *,
        preflight: MigrationPreflight | None,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        source_before: str,
        store_before: str,
        store_path: Path,
        force_unverified: bool = False,
    ) -> MigrationFailure:
        """Build one preservation-backed failure without leaking paths.

        ``force_unverified`` marks the active store UNVERIFIED with a
        recovery locator and non-retryable when the rollback outcome could
        not be proven: an unprovable rollback never claims
        VERIFIED_UNCHANGED.  Every recovery locator is built through the
        shared strict prove-or-fail-stop protocol: candidates are the
        activation pipeline's byte-exact recovery backups first, then the
        live path only when its current bytes are strictly proven to
        equal the preserved ``before_digest``, and the same strict proof
        is repeated immediately before each locator is returned.  When an
        asset that requires recovery has no honest path the failure stops
        explicitly (``IMPORT.PRIOR_STATE_UNRECOVERABLE``) instead of
        exposing a fabricated live-path locator.
        """

        identity = self._resource_identity
        error_code = _disambiguation_error_code(error)
        retryable = _disambiguation_retryable(error)
        diagnostics = (
            () if preflight is None else preflight.diagnostics
        )
        active_generation = coordinator.current_generation
        source_observed = _try_file_digest(
            identity.configured_jsonl_path
        )
        store_observed = _try_file_digest(store_path)
        locators: list[RecoveryLocator] = []

        def require_locator(
            *,
            asset_kind: AssetKind,
            expected_digest: str,
            selection: _LocatorSelection | None,
        ) -> None:
            nonlocal retryable
            if selection is None:
                raise MigrationPreflightError(
                    "IMPORT.PRIOR_STATE_UNRECOVERABLE"
                )
            _require_locator_return_proof(
                selection,
                expected_digest,
                "IMPORT.PRIOR_STATE_UNRECOVERABLE",
            )
            locators.append(
                RecoveryLocator(
                    path=selection.path,
                    asset_kind=asset_kind,
                    expected_digest=expected_digest,
                )
            )
            retryable = False

        if source_observed == source_before:
            source_evidence = _unchanged_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
            )
        elif source_observed is not None:
            source_evidence = _changed_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
                source_observed,
            )
            require_locator(
                asset_kind=AssetKind.ORIGINAL_SOURCE,
                expected_digest=source_before,
                selection=_proven_live_locator(
                    identity.configured_jsonl_path,
                    source_before,
                ),
            )
        else:
            source_evidence = _unverified_preservation(
                AssetKind.ORIGINAL_SOURCE,
                source_before,
            )
            require_locator(
                asset_kind=AssetKind.ORIGINAL_SOURCE,
                expected_digest=source_before,
                selection=_proven_live_locator(
                    identity.configured_jsonl_path,
                    source_before,
                ),
            )
        if force_unverified:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            require_locator(
                asset_kind=AssetKind.ACTIVE_STORE,
                expected_digest=store_before,
                selection=_proven_store_locator(
                    store_path,
                    store_before,
                ),
            )
        elif store_observed == store_before:
            store_evidence = _unchanged_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
        elif store_observed is not None:
            store_evidence = _changed_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
                store_observed,
            )
            require_locator(
                asset_kind=AssetKind.ACTIVE_STORE,
                expected_digest=store_before,
                selection=_proven_store_locator(
                    store_path,
                    store_before,
                ),
            )
        else:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            require_locator(
                asset_kind=AssetKind.ACTIVE_STORE,
                expected_digest=store_before,
                selection=_proven_store_locator(
                    store_path,
                    store_before,
                ),
            )
        locators.sort(
            key=lambda locator: locator.asset_kind.value
        )
        return MigrationFailure(
            stage=stage_label,
            error_code=error_code,
            retryable=retryable,
            diagnostics=diagnostics,
            active_generation=active_generation,
            original_source_preservation=source_evidence,
            active_store_preservation=store_evidence,
            recovery_locators=tuple(locators),
        )

    def _schema_upgrade_report(
        self,
        *,
        generation: int,
        backup_path: Path,
        backup_identity: tuple[int, int],
        backup_digest: str,
        success_digest: str,
    ) -> SchemaUpgradeReport:
        """Build one completed schema upgrade report from durable facts.

        Immediately before the report is returned, the exact owned
        pending ``Connection.backup()`` is atomically promoted to the
        stable reported ``.bak`` suffix (idempotent when a cold recovery
        already promoted it) and its bytes are re-proven to equal the
        ticket's ``backup_digest``, so the report always references
        exactly one stable reported backup that survives later cleanup.
        """

        try:
            stable_path = _promote_schema_upgrade_artifact(
                backup_path,
                backup_identity,
            )
        except ActivationPreparationError as error:
            raise MigrationPreflightError(
                "SCHEMA.BACKUP_UNVERIFIABLE"
            ) from error
        if _strict_locator_proof(stable_path, backup_digest) is None:
            raise MigrationPreflightError(
                "SCHEMA.BACKUP_UNVERIFIABLE"
            )
        return SchemaUpgradeReport(
            canonical_store_id=self._canonical_store_id,
            from_version=TM_LEGACY_SCHEMA_VERSION,
            to_version=TM_SCHEMA_VERSION,
            backup_path=stable_path,
            backup_digest=backup_digest,
            success_digest=success_digest,
            activated_generation=generation,
        )

    def _reconcile_failed_upgrade_activation(
        self,
        error: Exception,
        *,
        prepared: _ActivationPreparation,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        store_before: str,
        store_path: Path,
        backup_path: Path | None,
        backup_identity: tuple[int, int] | None,
        backup_digest: str | None,
    ) -> SchemaUpgradeOutcome:
        """Auto-restore the prior READY service after one failed upgrade.

        Schema upgrade reuses the activation pipeline, so its failure
        reconciliation follows Task 5.10 exactly: a failure before any
        durable journal cancels the live preparation (nothing was
        replaced), a durable pending journal is rolled back, and a
        durable ``GENERATION_PUBLISHED`` journal whose candidate active
        set is provable completes via fresh-coordinator recovery and
        reports the completed upgrade.  A rollback that cannot be proven
        fails stop with honest unverified preservation evidence and
        never claims VERIFIED_UNCHANGED.
        """

        try:
            journal_phase = coordinator.durable_activation_phase
        except Exception as phase_error:
            return self._schema_upgrade_failure(
                phase_error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
                force_unverified=True,
            )
        if journal_phase is None:
            try:
                coordinator.cancel_prepared_activation(prepared)
            except Exception as cancel_error:
                if _schema_upgrade_error_code(
                    cancel_error
                ) != "ACTIVATION.PREPARATION_NOT_ACTIVE":
                    return self._schema_upgrade_failure(
                        cancel_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                        force_unverified=True,
                    )
            return self._schema_upgrade_failure(
                error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
            )
        if journal_phase == "GENERATION_PUBLISHED":
            try:
                coordinator.rollback_durable_activation()
            except Exception as rollback_error:
                if _schema_upgrade_error_code(
                    rollback_error
                ) != "ACTIVATION.ROLLBACK_COMPLETED_INVALID":
                    return self._schema_upgrade_failure(
                        rollback_error,
                        stage_label=stage_label,
                        coordinator=coordinator,
                        store_before=store_before,
                        store_path=store_path,
                        backup_path=backup_path,
                        backup_identity=backup_identity,
                        force_unverified=True,
                    )
            else:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                )
            # The candidate active set is proven and the completed
            # journal is the cold-recovery authority: a fresh coordinator
            # bound to the unchanged store id re-proves and completes the
            # candidate publication (crash-window recovery), then the
            # high-level operation reports the completed upgrade.
            recovery_coordinator = ResourceStoreCoordinator(
                canonical_store_id=coordinator.canonical_store_id,
                resource_identity=self._resource_identity,
            )
            report = recovery_coordinator.recover_durable_activation()
            if (
                report is None
                or getattr(report, "action", None) != "COMPLETED"
                or report.generation is None
                or recovery_coordinator.state != "READY"
                or recovery_coordinator.canonical_store_id
                != self._canonical_store_id
                or recovery_coordinator.current_generation
                != report.generation
            ):
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            if (
                backup_path is None
                or backup_identity is None
                or backup_digest is None
            ):
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            new_store_path = recovery_coordinator.active_store_path
            if new_store_path is None:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            success_digest = _try_file_digest(new_store_path)
            if success_digest is None:
                return self._schema_upgrade_failure(
                    error,
                    stage_label=stage_label,
                    coordinator=coordinator,
                    store_before=store_before,
                    store_path=store_path,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    force_unverified=True,
                )
            # The recovered publication is durably READY: the same
            # service instance keeps the preserved canonical store id and
            # adopts the recovered view, so a subsequent upgrade call
            # observes the current schema without a caller-side renewal.
            coordinator.adopt_recovered_authority(recovery_coordinator)
            return self._schema_upgrade_report(
                generation=report.generation,
                backup_path=backup_path,
                backup_identity=backup_identity,
                backup_digest=backup_digest,
                success_digest=success_digest,
            )
        try:
            coordinator.rollback_durable_activation()
        except Exception as rollback_error:
            return self._schema_upgrade_failure(
                rollback_error,
                stage_label=stage_label,
                coordinator=coordinator,
                store_before=store_before,
                store_path=store_path,
                backup_path=backup_path,
                backup_identity=backup_identity,
                force_unverified=True,
            )
        return self._schema_upgrade_failure(
            error,
            stage_label=stage_label,
            coordinator=coordinator,
            store_before=store_before,
            store_path=store_path,
            backup_path=backup_path,
            backup_identity=backup_identity,
        )

    def _schema_upgrade_failure(
        self,
        error: Exception,
        *,
        stage_label: str,
        coordinator: ResourceStoreCoordinator,
        store_before: str,
        store_path: Path,
        backup_path: Path | None = None,
        backup_identity: tuple[int, int] | None = None,
        force_unverified: bool = False,
    ) -> SchemaUpgradeFailure:
        """Build one preservation-backed schema upgrade failure.

        ``force_unverified`` marks the active store UNVERIFIED with a
        recovery locator and non-retryable when the rollback outcome could
        not be proven: an unprovable rollback never claims
        VERIFIED_UNCHANGED.  A recovery locator is only ever built from a
        path whose current bytes are re-proven to equal the preservation
        ``before_digest``; when no such path exists the failure stops
        explicitly instead of exposing a locator whose bytes do not match.
        The schema-upgrade backup and any unexposed byte-exact locator
        snapshot are strictly removed so failed attempts never accumulate
        hidden full DB copies.
        """

        error_code = _schema_upgrade_error_code(error)
        retryable = _schema_upgrade_retryable(error)
        active_generation = coordinator.current_generation
        if active_generation is None:
            active_generation = 0
        store_observed = _try_file_digest(store_path)
        locators: list[RecoveryLocator] = []
        locator_path: Path | None = None
        needs_recovery = False
        if force_unverified:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            needs_recovery = True
        elif store_observed == store_before:
            store_evidence = _unchanged_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
        elif store_observed is not None:
            store_evidence = _changed_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
                store_observed,
            )
            needs_recovery = True
        else:
            store_evidence = _unverified_preservation(
                AssetKind.ACTIVE_STORE,
                store_before,
            )
            needs_recovery = True
        if needs_recovery:
            selection = self._proven_schema_upgrade_locator(
                coordinator,
                store_path=store_path,
                store_before=store_before,
                backup_path=backup_path,
            )
            if selection is None:
                self._release_unexposed_schema_upgrade_artifacts(
                    coordinator,
                    backup_path=backup_path,
                    backup_identity=backup_identity,
                    locator_path=None,
                )
                raise MigrationPreflightError(
                    "SCHEMA.PRIOR_STATE_UNRECOVERABLE"
                )
            locator_path = self._promote_schema_upgrade_locator(
                coordinator,
                selection,
                backup_path=backup_path,
                backup_identity=backup_identity,
            )
            if locator_path is None:
                raise MigrationPreflightError(
                    "SCHEMA.PRIOR_STATE_UNRECOVERABLE"
                )
            # Return-boundary re-proof: the exact path about to be exposed
            # must still pass the shared strict proof with the identity
            # proven at selection; a swap (symlink, directory, multi-link,
            # or foreign inode) fails stop and is never deleted or exposed.
            _require_locator_return_proof(
                _LocatorSelection(locator_path, selection.identity),
                store_before,
                "SCHEMA.PRIOR_STATE_UNRECOVERABLE",
            )
            locators.append(
                RecoveryLocator(
                    path=locator_path,
                    asset_kind=AssetKind.ACTIVE_STORE,
                    expected_digest=store_before,
                )
            )
            retryable = False
        self._release_unexposed_schema_upgrade_artifacts(
            coordinator,
            backup_path=backup_path,
            backup_identity=backup_identity,
            locator_path=locator_path,
        )
        outcome = SchemaUpgradeFailure(
            stage=stage_label,
            error_code=error_code,
            retryable=retryable,
            active_generation=active_generation,
            active_store_preservation=store_evidence,
            recovery_locators=tuple(locators),
        )
        snapshot = coordinator.schema_upgrade_locator_snapshot
        if (
            snapshot is not None
            and locator_path is not None
            and snapshot.path == locator_path
        ):
            coordinator._detach_schema_upgrade_locator_snapshot(locator_path)
        return outcome

    def _proven_schema_upgrade_locator(
        self,
        coordinator: ResourceStoreCoordinator,
        *,
        store_path: Path,
        store_before: str,
        backup_path: Path | None,
    ) -> _LocatorSelection | None:
        """One strictly proven byte-exact recovery locator for the prior store.

        Candidates are the activation pipeline's journal-owned byte-exact
        ``.localcat-recovery.*.database.bak`` (the Task 5.10 locator
        family), the Design-required ``Connection.backup()`` schema
        backup, the coordinator-captured byte-exact locator snapshot, and
        finally the live canonical -- each accepted only when the shared
        strict proof (O_NOFOLLOW regular single-link file, bytes equal
        ``store_before``, stable terminal identity) passes at selection
        time.  No unproved fallback exists.
        """

        for candidate in _recovery_backup_candidates(
            store_path,
            label="database",
        ):
            proof = _strict_locator_proof(candidate, store_before)
            if proof is not None:
                return _LocatorSelection(candidate, proof[0])
        if backup_path is not None:
            proof = _strict_locator_proof(backup_path, store_before)
            if proof is not None:
                return _LocatorSelection(backup_path, proof[0])
        snapshot = coordinator.schema_upgrade_locator_snapshot
        if snapshot is not None:
            proof = _strict_locator_proof(snapshot.path, store_before)
            if proof is not None:
                return _LocatorSelection(snapshot.path, proof[0])
        proof = _strict_locator_proof(store_path, store_before)
        if proof is not None:
            return _LocatorSelection(store_path, proof[0])
        return None

    def _promote_schema_upgrade_locator(
        self,
        coordinator: ResourceStoreCoordinator,
        selection: _LocatorSelection,
        *,
        backup_path: Path | None,
        backup_identity: tuple[int, int] | None,
    ) -> Path | None:
        """Promote one selected pending artifact to its stable reported suffix.

        When the selected locator is the current attempt's pending
        ``Connection.backup()`` or pending byte-exact locator snapshot,
        the exact owned file is atomically renamed to its stable reported
        suffix (``.bak`` / ``.locator``) and the stable path is returned;
        any other selected candidate (journal-owned recovery backup or
        the live canonical) is already stable and is returned unchanged.
        ``None`` means the pending artifact could not be promoted safely
        (foreign inode, symlink, directory, multi-link, or missing) and
        the failure must stop without exposing anything.
        """

        if (
            backup_path is not None
            and backup_identity is not None
            and selection.path == backup_path
        ):
            try:
                return _promote_schema_upgrade_artifact(
                    backup_path,
                    backup_identity,
                )
            except ActivationPreparationError:
                return None
        snapshot = coordinator.schema_upgrade_locator_snapshot
        if snapshot is not None and selection.path == snapshot.path:
            try:
                return _promote_schema_upgrade_artifact(
                    snapshot.path,
                    snapshot.identity,
                )
            except ActivationPreparationError:
                return None
        return selection.path

    def _release_unexposed_schema_upgrade_artifacts(
        self,
        coordinator: ResourceStoreCoordinator,
        *,
        backup_path: Path | None,
        backup_identity: tuple[int, int] | None,
        locator_path: Path | None,
    ) -> None:
        """Strictly remove failure artifacts that no locator exposes."""

        snapshot = coordinator.schema_upgrade_locator_snapshot
        if snapshot is not None and snapshot.path != locator_path:
            coordinator.release_schema_upgrade_locator_snapshot()
        if (
            backup_path is not None
            and backup_identity is not None
            and backup_path != locator_path
        ):
            _remove_owned_schema_upgrade_backup(
                backup_path,
                backup_identity,
            )

    def _build_stage(
        self,
        source: Path,
        *,
        preflight: MigrationPreflight,
        canonical_store_id: str,
        batch_kind: str,
        batch_prefix: str,
        snapshot_prefix: str,
        stage_prefix: str,
        path_salt: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[
        MutableStageRef,
        _CreatedFileIdentity | None,
        _CreatedFileIdentity | None,
    ]:
        """Build or reuse one complete unpublished stage (shared builder).

        The stage is deterministic per (identity, source digest, prefix)
        and is never the canonical sidecar: reuse is only accepted after
        the full stage closure is re-proven from disk, and any build
        failure removes exactly the created stage files.  Migration
        origins use the deterministic ``migration.<source digest>`` batch
        id; explicit imports pass a fresh collision-resistant
        ``import.<uuid>`` batch id whose token also shapes the snapshot
        receipt id.  The returned identities describe exactly the files
        this builder created (``None`` for a reused stage), so a caller
        that fails before a sealed artifact owns them can remove exactly
        those paths without ever touching a foreign inode.
        """

        if batch_id is None:
            batch_id = f"{batch_prefix}.{preflight.source_digest}"
        if type(batch_id) is not str or not batch_id.strip():
            raise ValueError("batch id must be a non-empty string")
        stage = _deterministic_stage_ref(
            self._resource_identity,
            source_digest=preflight.source_digest,
            stage_prefix=stage_prefix,
            path_salt=path_salt,
        )
        if stage.staged_db_path.exists() or stage.manifest_temp_path.exists():
            _validate_reusable_stage(
                stage,
                canonical_store_id=canonical_store_id,
                preflight=preflight,
                batch_kind=batch_kind,
                batch_prefix=batch_prefix,
                snapshot_prefix=snapshot_prefix,
                batch_id=batch_id,
            )
            return stage, None, None

        initialize_stage_schema(
            stage,
            canonical_store_id=canonical_store_id,
        )
        stage_identity = _created_file_identity(stage.staged_db_path)
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            store = SQLiteTMStore(
                stage,
                canonical_store_id=canonical_store_id,
            )
            observation = _StreamingBuildObservation()
            store.append_streamed_batch(
                batch_id=batch_id,
                kind=batch_kind,
                drafts=_iter_draft_pairs(source, observation),
                source_digest=preflight.source_digest,
                source_path=source,
                invalid_count=preflight.invalid_count,
                duplicate_source_count=preflight.duplicate_source_count,
                chunk_size=MIGRATION_STREAM_CHUNK_SIZE,
                _defer_secondary_indexes=True,
            )
            if not _observation_matches(observation, preflight):
                raise MigrationPreflightError("MIGRATION.SOURCE_CHANGED")
            revision = store.canonical_revision()
            if (
                revision.head_revision != 1
                or revision.record_count != preflight.valid_count
            ):
                raise MigrationPreflightError(
                    "MIGRATION.STAGE_REVISION_INVALID"
                )
            receipt = SnapshotReceipt(
                snapshot_id=_snapshot_id_for_origin(
                    batch_id=batch_id,
                    batch_kind=batch_kind,
                    snapshot_prefix=snapshot_prefix,
                    source_digest=preflight.source_digest,
                ),
                resource_id=self._resource_identity.resource_id,
                canonical_store_id=canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=preflight.source_digest,
                record_count=preflight.valid_count,
            )
            store.register_issued_snapshot_receipt(
                receipt,
                destination_jsonl_path=(
                    self._resource_identity.configured_jsonl_path
                ),
                destination_manifest_path=(
                    self._resource_identity.snapshot_manifest_path
                ),
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=receipt,
                receipt_digest=snapshot_receipt_digest(receipt),
            )
            manifest_identity = _write_new_file(
                stage.manifest_temp_path,
                contract_to_json(manifest).encode("utf-8"),
            )
        except BaseException:
            if manifest_identity is not None:
                _remove_created_file(
                    stage.manifest_temp_path,
                    manifest_identity,
                )
            _remove_created_file(stage.staged_db_path, stage_identity)
            raise
        return stage, stage_identity, manifest_identity

    def _validate_source_preconditions(self, source: Path) -> None:
        if type(source) is not _NATIVE_PATH_TYPE:
            raise TypeError("source must be an exact native Path")
        if source != self._resource_identity.configured_jsonl_path:
            raise MigrationPreflightError(
                "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
            )
        _require_target_parent_writable(self._resource_identity)
        _require_source_readable(source)

    def _reject_sidecar_reuse(self, source_digest: str) -> None:
        """Fail closed on any sidecar claim before activation authority.

        No completed reuse is ever reported from the sidecar within
        Tasks 5.1-5.2: a naked structural sidecar is not canonical
        authority. Deterministic conflicts still surface as stable codes.
        """

        identity = self._resource_identity
        sidecar = identity.canonical_sidecar_path
        manifest = identity.snapshot_manifest_path
        if not sidecar.exists():
            if manifest.exists():
                raise MigrationPreflightError(
                    "MIGRATION.MANIFEST_WITHOUT_SIDECAR"
                )
            return
        if sidecar.is_symlink() or not sidecar.is_file():
            raise MigrationPreflightError("MIGRATION.SIDECAR_INVALID")

        try:
            connection = sqlite3.connect(
                f"{sidecar.as_uri()}?mode=ro",
                uri=True,
            )
            try:
                connection.execute("PRAGMA query_only = ON")
                meta_rows = connection.execute(
                    "SELECT key, value FROM tm_meta WHERE key IN ("
                    "'activation_status', 'canonical_store_id', "
                    "'resource_id', 'target_identity') ORDER BY key"
                ).fetchall()
                meta = {str(key): str(value) for key, value in meta_rows}
                if (
                    meta.get("resource_id") != identity.resource_id
                    or meta.get("canonical_store_id")
                    != self._canonical_store_id
                    or meta.get("target_identity")
                    != identity.target_identity
                ):
                    raise MigrationPreflightError(
                        "MIGRATION.SIDECAR_IDENTITY_MISMATCH"
                    )
                rows = connection.execute(
                    "SELECT source_digest, status, completed_revision "
                    "FROM tm_origin_batch WHERE kind = 'migration' "
                    "ORDER BY completed_revision, batch_id"
                ).fetchall()
            finally:
                connection.close()
        except MigrationPreflightError:
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            raise MigrationPreflightError(
                "MIGRATION.SIDECAR_INVALID"
            ) from error

        for digest_value, status_value, revision_value in rows:
            if (
                type(digest_value) is str
                and digest_value == source_digest
                and status_value == "completed"
                and type(revision_value) is int
                and revision_value >= 1
            ):
                raise MigrationPreflightError(
                    "MIGRATION.SIDECAR_NOT_REUSABLE"
                )
        if any(
            status_value == "completed"
            and type(revision_value) is int
            and revision_value >= 1
            for _digest_value, status_value, revision_value in rows
        ):
            raise MigrationPreflightError(
                "MIGRATION.SIDECAR_DIFFERENT_SOURCE"
            )
        raise MigrationPreflightError("MIGRATION.SIDECAR_NOT_REUSABLE")


def _scan_jsonl(source: Path) -> MigrationPreflight:
    """One bounded streaming pass producing preflight facts and diagnostics."""

    digest = hashlib.sha256()
    valid_count = 0
    invalid_count = 0
    duplicate_source_count = 0
    variant_count = 0
    row_count = 0
    source_counts: dict[str, int] = {}
    diagnostics: list[MigrationDiagnostic] = []

    try:
        with source.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                row_count += 1
                digest.update(raw_line)
                rejection_code, payload = _classify_jsonl_line(raw_line)
                if rejection_code is not None:
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code=rejection_code,
                            stage=_REJECTION_DIAGNOSTICS[rejection_code][0],
                            summary=_REJECTION_DIAGNOSTICS[rejection_code][1],
                        )
                    )
                    continue
                row = cast(dict[str, object], payload)
                source_raw = cast(str, row["source"])
                prior_count = source_counts.get(source_raw, 0)
                source_counts[source_raw] = prior_count + 1
                if prior_count == 1:
                    duplicate_source_count += 1
                if prior_count >= 1:
                    variant_count += 1
                    diagnostics.append(
                        MigrationDiagnostic(
                            code="ROW.DUPLICATE_SOURCE",
                            stage="PREFLIGHT.VALIDATE",
                            line_number=line_number,
                            record_id=None,
                            disposition=DiagnosticDisposition.WARNING,
                            safe_summary="ROW_PRESERVED_AS_VARIANT",
                        )
                    )
                valid_count += 1
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.SOURCE_UNREADABLE"
        ) from error

    if row_count == 0:
        raise MigrationPreflightError("MIGRATION.SOURCE_EMPTY")
    return MigrationPreflight(
        source_digest=digest.hexdigest(),
        valid_count=valid_count,
        invalid_count=invalid_count,
        duplicate_source_count=duplicate_source_count,
        variant_count=variant_count,
        diagnostics=tuple(diagnostics),
    )


def _classify_jsonl_line(
    raw_line: bytes,
) -> tuple[str | None, dict[str, object] | None]:
    """Return (rejection code, payload) with no retained line state."""

    try:
        decoded_line = raw_line.decode("utf-8")
    except UnicodeDecodeError:
        return "ROW.INVALID_UTF8", None
    try:
        payload = json.loads(
            decoded_line,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return "ROW.INVALID_JSON", None
    if type(payload) is not dict:
        return "ROW.INVALID_SHAPE", None
    row = cast(dict[str, object], payload)
    source_raw = row.get("source")
    target_raw = row.get("target")
    if (
        type(source_raw) is not str
        or source_raw == ""
        or type(target_raw) is not str
        or target_raw == ""
    ):
        return "ROW.INVALID_REQUIRED_FIELD", None
    return None, row


def _iter_draft_pairs(
    source: Path,
    observation: _StreamingBuildObservation,
) -> Iterator[tuple[TMRecordDraft, int | None]]:
    """Re-stream the source into a draft pair stream with live counting.

    The store seam groups the pairs into bounded chunks, so no per-record
    state is retained here beyond the running digest and counters.
    """

    try:
        with source.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                observation.digest.update(raw_line)
                rejection_code, payload = _classify_jsonl_line(raw_line)
                if rejection_code is not None:
                    observation.invalid_count += 1
                    continue
                row = cast(dict[str, object], payload)
                source_raw = cast(str, row["source"])
                prior_count = observation.source_counts.get(source_raw, 0)
                observation.source_counts[source_raw] = prior_count + 1
                if prior_count == 1:
                    observation.duplicate_source_count += 1
                if prior_count >= 1:
                    observation.variant_count += 1
                observation.valid_count += 1
                yield (_draft_from_jsonl(row), line_number)
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.SOURCE_UNREADABLE"
        ) from error


def _observation_matches(
    observation: _StreamingBuildObservation,
    preflight: MigrationPreflight,
) -> bool:
    return (
        observation.digest.hexdigest() == preflight.source_digest
        and observation.valid_count == preflight.valid_count
        and observation.invalid_count == preflight.invalid_count
        and observation.duplicate_source_count
        == preflight.duplicate_source_count
        and observation.variant_count == preflight.variant_count
    )


def _draft_from_jsonl(row: dict[str, object]) -> TMRecordDraft:
    source_raw = row["source"]
    target_raw = row["target"]
    if type(source_raw) is not str or type(target_raw) is not str:
        raise MigrationPreflightError("MIGRATION.ROW_VALIDATION_DRIFT")

    def optional_text(field_name: str) -> str | None:
        value = row.get(field_name)
        return value if type(value) is str else None

    return TMRecordDraft(
        source_raw=source_raw,
        target_raw=target_raw,
        speaker_raw=optional_text("speaker"),
        context_prev_raw=optional_text("context_prev"),
        context_next_raw=optional_text("context_next"),
        file_source=optional_text("file_source"),
        provenance=_compatible_provenance(row),
    )


def _compatible_provenance(row: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Parse the export provenance representation, or the legacy default.

    Only a well-formed list of two-item ``[key, value]`` string pairs is
    accepted; any other present value degrades to the legacy provenance
    exactly as before, so legacy row validation is never weakened.
    """

    if "provenance" not in row:
        return (("source", "legacy-jsonl"),)
    value = row["provenance"]
    if type(value) is not list:
        return (("source", "legacy-jsonl"),)
    items: list[tuple[str, str]] = []
    for entry in value:
        if (
            type(entry) is not list
            or len(entry) != 2
            or type(entry[0]) is not str
            or type(entry[1]) is not str
        ):
            return (("source", "legacy-jsonl"),)
        items.append((entry[0], entry[1]))
    return tuple(items)


def _deterministic_stage_ref(
    identity: CanonicalResourceIdentity,
    *,
    source_digest: str,
    stage_prefix: str,
    path_salt: str | None = None,
) -> MutableStageRef:
    name_key = (
        f"{identity.target_identity[:16]}.{source_digest[:16]}"
        if path_salt is None
        else (
            f"{path_salt}.{identity.target_identity[:16]}."
            f"{source_digest[:16]}"
        )
    )
    parent = identity.canonical_sidecar_path.parent
    return MutableStageRef(
        stage_id=f"stage.{stage_prefix}.{name_key}",
        resource_identity=identity,
        staged_db_path=(
            parent / f".localcat-{stage_prefix}.{name_key}.sqlite3.stage"
        ).absolute(),
        manifest_temp_path=(
            parent / f".localcat-{stage_prefix}.{name_key}.manifest.tmp"
        ).absolute(),
    )


def _snapshot_id_for_origin(
    *,
    batch_id: str,
    batch_kind: str,
    snapshot_prefix: str,
    source_digest: str,
) -> str:
    """Derive the deterministic snapshot receipt id for one origin batch.

    Migration origins keep the historical ``snapshot.migration.<digest>``
    shape; explicit imports derive the id from the fresh import batch
    token so two identical imports never collide and the receipt binds
    the exact origin batch (StageSealer and Gate B re-derive the same
    shape from the single batch row).
    """

    if batch_kind == "import":
        token = batch_id[len("import."):]
        return f"snapshot.import.{token[:24]}"
    return f"{snapshot_prefix}.{source_digest[:24]}"


def _validate_reusable_stage(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    preflight: MigrationPreflight,
    batch_kind: str = "migration",
    batch_prefix: str = "migration",
    snapshot_prefix: str = "snapshot.migration",
    batch_id: str | None = None,
) -> None:
    """Validate a completed mutable stage with bounded streaming checks."""

    try:
        if (
            stage.staged_db_path.is_symlink()
            or stage.manifest_temp_path.is_symlink()
            or not stage.staged_db_path.is_file()
            or not stage.manifest_temp_path.is_file()
        ):
            raise ValueError("stage pair is incomplete")
        try:
            schema = inspect_stage_schema(
                stage,
                canonical_store_id=canonical_store_id,
            )
        except SQLiteStoreSchemaError as error:
            if str(error) == "STORE.STAGE_PUBLISHED":
                raise MigrationPreflightError(
                    "MIGRATION.STAGE_SEALED"
                ) from error
            raise
        connection = sqlite3.connect(
            f"{stage.staged_db_path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            batch_rows = connection.execute(
                "SELECT batch_id, source_digest, source_path, status, "
                "valid_count, invalid_count, duplicate_source_count, "
                "completed_revision FROM tm_origin_batch "
                "WHERE kind = ? ORDER BY batch_id",
                (batch_kind,),
            ).fetchall()
            expected_batch_id = (
                batch_id
                if batch_id is not None
                else f"{batch_prefix}.{preflight.source_digest}"
            )
            expected_batch = (
                expected_batch_id,
                preflight.source_digest,
                str(stage.resource_identity.configured_jsonl_path),
                "completed",
                preflight.valid_count,
                preflight.invalid_count,
                preflight.duplicate_source_count,
                1,
            )
            if batch_rows != [expected_batch]:
                raise ValueError("migration batch does not close")
            if connection.execute(
                "SELECT COUNT(*) FROM tm_record"
            ).fetchone() != (preflight.valid_count,):
                raise ValueError("record count does not close")
            receipt_rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version, destination_jsonl_path, "
                "destination_manifest_path, status "
                "FROM tm_snapshot_receipt ORDER BY snapshot_id"
            ).fetchall()
            expected_snapshot_id = _snapshot_id_for_origin(
                batch_id=expected_batch_id,
                batch_kind=batch_kind,
                snapshot_prefix=snapshot_prefix,
                source_digest=preflight.source_digest,
            )
            if len(receipt_rows) != 1:
                raise ValueError("snapshot receipt does not close")
            receipt_row = receipt_rows[0]
            if (
                receipt_row[0] != expected_snapshot_id
                or receipt_row[1] != stage.resource_identity.resource_id
                or receipt_row[2] != canonical_store_id
                or receipt_row[3] != 1
                or receipt_row[4] != preflight.source_digest
                or receipt_row[5] != preflight.valid_count
                or receipt_row[6] != SNAPSHOT_FORMAT_VERSION
                or receipt_row[7]
                != str(stage.resource_identity.configured_jsonl_path)
                or receipt_row[8]
                != str(stage.resource_identity.snapshot_manifest_path)
                or receipt_row[9] != "issued"
            ):
                raise ValueError("snapshot receipt does not close")
            if connection.execute(
                "SELECT COUNT(*) FROM tm_snapshot_binding"
            ).fetchone() != (0,):
                raise ValueError("unpublished stage must not bind snapshot")

            required_sizes = (1, 2) if schema.fts5_available else (1, 2, 3)
            _validate_stage_indexes(
                connection,
                required_sizes=required_sizes,
                fts5_available=schema.fts5_available,
            )
        finally:
            connection.close()

        decoded = contract_from_json(
            stage.manifest_temp_path.read_text(encoding="utf-8")
        )
        if type(decoded) is not SnapshotManifest:
            raise ValueError("manifest temporary is invalid")
        manifest = decoded
        if (
            manifest.snapshot_kind is not SnapshotKind.MIGRATION_SOURCE
            or manifest.receipt.snapshot_id != expected_snapshot_id
            or manifest.receipt.resource_id
            != stage.resource_identity.resource_id
            or manifest.receipt.canonical_store_id != canonical_store_id
            or manifest.receipt.exported_revision != 1
            or manifest.receipt.jsonl_digest != preflight.source_digest
            or manifest.receipt.record_count != preflight.valid_count
            or manifest.receipt_digest
            != snapshot_receipt_digest(manifest.receipt)
        ):
            raise ValueError("manifest temporary does not close")
    except MigrationPreflightError:
        raise
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise MigrationPreflightError("MIGRATION.STAGE_CONFLICT") from error


def _validate_stage_indexes(
    connection: sqlite3.Connection,
    *,
    required_sizes: tuple[int, ...],
    fts5_available: bool,
) -> None:
    """Recompute every candidate-proof fact for stage reuse."""

    _ = validate_candidate_proof_index(
        connection,
        required_sizes=required_sizes,
        fts5_available=fts5_available,
    )


def _created_file_identity(path: Path) -> _CreatedFileIdentity:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise MigrationPreflightError("MIGRATION.STAGE_FILE_MISSING") from error
    if not stat.S_ISREG(observed.st_mode):
        raise MigrationPreflightError("MIGRATION.STAGE_FILE_UNSAFE")
    return _CreatedFileIdentity(observed.st_dev, observed.st_ino)


def _write_new_file(path: Path, payload: bytes) -> _CreatedFileIdentity:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.MANIFEST_TEMP_CONFLICT"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("manifest write made no progress")
            view = view[written:]
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise MigrationPreflightError(
                "MIGRATION.MANIFEST_TEMP_UNSAFE"
            )
        return _CreatedFileIdentity(observed.st_dev, observed.st_ino)
    finally:
        os.close(descriptor)


def _remove_created_file(
    path: Path,
    expected: _CreatedFileIdentity,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._remove_created_file(
        path=path,
        expected=expected,
        parent_handle=parent_handle,
    )

def _remove_owned_schema_upgrade_backup(
    path: Path,
    identity: tuple[int, int],
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._remove_owned_schema_upgrade_backup(
        path,
        identity,
        preflight_error_factory=MigrationPreflightError,
        fsync_error_factory=_copy_fsync_error_factory,
    )


def _snapshot_resource_identity(
    value: object,
) -> CanonicalResourceIdentity:
    if type(value) is not CanonicalResourceIdentity:
        raise TypeError("resource_identity must be exact CanonicalResourceIdentity")
    if (
        type(value.resource_id) is not str
        or type(value.configured_jsonl_path) is not _NATIVE_PATH_TYPE
        or type(value.canonical_sidecar_path) is not _NATIVE_PATH_TYPE
        or type(value.snapshot_manifest_path) is not _NATIVE_PATH_TYPE
        or type(value.target_identity) is not str
        or type(value.identity_version) is not str
    ):
        raise TypeError("resource_identity contains non-native values")
    private_identity = CanonicalResourceIdentity.from_configured_jsonl(
        value.resource_id,
        Path(str(value.configured_jsonl_path)),
    )
    if private_identity != value:
        raise ValueError("resource_identity is not canonical")
    return private_identity


def _require_target_parent_writable(
    identity: CanonicalResourceIdentity,
) -> None:
    parent = identity.canonical_sidecar_path.parent
    try:
        mode = parent.stat().st_mode
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.TARGET_NOT_WRITABLE"
        ) from error
    writable = mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    searchable = mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not stat.S_ISDIR(mode) or not writable or not searchable:
        raise MigrationPreflightError("MIGRATION.TARGET_NOT_WRITABLE")


def _require_source_readable(source: Path) -> None:
    try:
        mode = source.stat().st_mode
    except OSError as error:
        raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE") from error
    readable = mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    if not stat.S_ISREG(mode) or not readable:
        raise MigrationPreflightError("MIGRATION.SOURCE_UNREADABLE")


def _try_file_digest(path: Path) -> str | None:
    """Late-bound wrapper; implementation moved to tm_snapshot_artifacts."""

    return snapshot_artifacts_module._try_file_digest(
        path=path,
    )

def _unchanged_preservation(
    asset_kind: AssetKind,
    digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.VERIFIED_UNCHANGED,
        before_digest=digest,
        observed_digest=digest,
    )


def _changed_preservation(
    asset_kind: AssetKind,
    before_digest: str,
    observed_digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.VERIFIED_CHANGED,
        before_digest=before_digest,
        observed_digest=observed_digest,
    )


def _unverified_preservation(
    asset_kind: AssetKind,
    before_digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.UNVERIFIED,
        before_digest=before_digest,
        observed_digest=None,
    )


def _disambiguation_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    return "IMPORT.FAILED"


def _disambiguation_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _strict_locator_proof(
    path: Path,
    expected_digest: str,
) -> tuple[tuple[int, int], str] | None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._strict_locator_proof(
        path,
        expected_digest,
    )


def _recovery_backup_candidates(
    sidecar: Path,
    label: str,
) -> Iterator[Path]:
    """Sorted journal-owned byte-exact recovery backup candidate names."""

    yield from sorted(
        sidecar.parent.glob(
            f".{sidecar.name}.localcat-recovery.*.{label}.bak"
        ),
        key=str,
    )


def _proven_live_locator(
    path: Path,
    expected_digest: str,
) -> _LocatorSelection | None:
    """One strictly proven byte-exact live-path locator, if any."""

    proof = _strict_locator_proof(path, expected_digest)
    if proof is None:
        return None
    return _LocatorSelection(path, proof[0])


def _proven_store_locator(
    store_path: Path,
    expected_digest: str,
) -> _LocatorSelection | None:
    """One strictly proven byte-exact locator for the active store.

    Candidates are the activation pipeline's journal-owned byte-exact
    recovery backups first, then the live canonical path -- each accepted
    only when the shared strict proof (O_NOFOLLOW regular single-link
    file, bytes equal ``expected_digest``, stable terminal identity)
    passes at selection time.  No unproved live-path fallback exists.
    """

    for candidate in _recovery_backup_candidates(
        store_path,
        label="database",
    ):
        proof = _strict_locator_proof(candidate, expected_digest)
        if proof is not None:
            return _LocatorSelection(candidate, proof[0])
    proof = _strict_locator_proof(store_path, expected_digest)
    if proof is not None:
        return _LocatorSelection(store_path, proof[0])
    return None


def _require_locator_return_proof(
    selection: _LocatorSelection,
    expected_digest: str,
    fail_stop_code: str,
) -> None:
    """Fail-stop re-proof immediately before one locator is returned.

    The shared strict proof runs again on the exact path about to be
    exposed: strict file kind, single link, identity stability, and
    digest must all still hold, and the inode must equal the one proven
    at selection.  A swap between selection and return (symlink,
    directory, multi-link, or foreign inode) therefore fails stop with
    ``fail_stop_code`` and never deletes or exposes the swapped path.
    """

    proof = _strict_locator_proof(selection.path, expected_digest)
    if proof is None or proof[0] != selection.identity:
        raise MigrationPreflightError(fail_stop_code)


def _copy_fsync_error_factory(_error: OSError) -> MigrationPreflightError:
    return MigrationPreflightError("SCHEMA.COPY_FAILED")


def _schema_upgrade_copy_plan(
) -> schema_upgrade_module.SchemaUpgradeCopyPlan:
    """Snapshot the owner-provided copy boundary at each invocation.

    The moved data plane receives immutable scalar/schema values and the
    owner namespace's current ancestry/index callbacks.  Constructing the
    plan lazily preserves the pre-extraction monkeypatch/fault-injection
    behavior; a module-import-time captured callback would silently bypass
    a later patch to the original ``tm_migration`` seam.
    """

    return schema_upgrade_module.SchemaUpgradeCopyPlan(
        schema_statements=tuple(_SCHEMA_STATEMENTS),
        fts5_statement=_FTS5_STATEMENT,
        approved_schema_digests=MappingProxyType(
            {
                False: _APPROVED_SCHEMA_DIGESTS[False],
                True: _APPROVED_SCHEMA_DIGESTS[True],
            }
        ),
        schema_upgrade_meta_key=_SCHEMA_UPGRADE_META_KEY,
        schema_upgrade_meta_value=_SCHEMA_UPGRADE_META_VALUE,
        target_schema_version=TM_SCHEMA_VERSION,
        completed_origin_blocks=_legacy_completed_origin_blocks,
        revision_ancestry=_legacy_revision_ancestry,
        unique_character_ngrams=unique_character_ngrams,
        ancestry_error_type=SQLiteStoreSchemaError,
        preflight_error_factory=MigrationPreflightError,
    )


def _promote_schema_upgrade_artifact(
    path: Path,
    identity: tuple[int, int],
) -> Path:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._promote_schema_upgrade_artifact(
        path,
        identity,
    )


def _schema_upgrade_error_code(error: Exception) -> str:
    error_code = getattr(error, "error_code", None)
    if type(error_code) is str and error_code:
        return error_code
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    return "SCHEMA.FAILED"


def _schema_upgrade_retryable(error: Exception) -> bool:
    retryable = getattr(error, "retryable", None)
    return retryable if type(retryable) is bool else False


def _schema_version_of_store(store_path: Path) -> int:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._schema_version_of_store(
        store_path,
        preflight_error_factory=MigrationPreflightError,
    )


def _upgrade_source_ref(
    identity: CanonicalResourceIdentity,
    store_path: Path,
) -> MutableStageRef:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._upgrade_source_ref(
        identity,
        store_path,
    )


def _open_legacy_read_connection(
    database_path: Path,
) -> sqlite3.Connection:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._open_legacy_read_connection(
        database_path
    )


def _read_active_activation_digest(store_path: Path) -> str:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._read_active_activation_digest(
        store_path,
        preflight_error_factory=MigrationPreflightError,
    )


def _read_legacy_snapshot_facts(
    store_path: Path,
    *,
    canonical_store_id: str,
) -> tuple[SnapshotReceipt, SnapshotKind]:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._read_legacy_snapshot_facts(
        store_path,
        canonical_store_id=canonical_store_id,
        preflight_error_factory=MigrationPreflightError,
    )


def _fsync_schema_upgrade_directory(path: Path) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._fsync_schema_upgrade_directory(
        path,
        error_factory=_copy_fsync_error_factory,
    )


def _copy_store_into_stage(
    source_path: Path,
    destination_path: Path,
) -> _CreatedFileIdentity:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    created = schema_upgrade_module._copy_store_into_stage(
        source_path,
        destination_path,
        preflight_error_type=MigrationPreflightError,
        preflight_error_factory=MigrationPreflightError,
        fsync_error_factory=_copy_fsync_error_factory,
    )
    return _CreatedFileIdentity(created.device, created.inode)


def _ddl_for(
    statements: tuple[str, ...],
    prefix: str,
) -> str:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    return schema_upgrade_module._ddl_for(statements, prefix)


def _migrate_schema_copy(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
) -> None:
    """Late-bound wrapper; implementation moved to tm_schema_upgrade."""

    schema_upgrade_module._migrate_schema_copy(
        connection,
        fts5_available=fts5_available,
        plan=_schema_upgrade_copy_plan(),
    )


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _rejected_diagnostic(
    line_number: int,
    *,
    code: str,
    stage: str,
    summary: str,
) -> MigrationDiagnostic:
    return MigrationDiagnostic(
        code=code,
        stage=stage,
        line_number=line_number,
        record_id=None,
        disposition=DiagnosticDisposition.REJECTED,
        safe_summary=summary,
    )


__all__ = [
    "MIGRATION_STREAM_CHUNK_SIZE",
    "ExportPreflightError",
    "MigrationPreflightError",
    "MigrationStageBuild",
    "TMMigrationService",
]
