"""Read-only JSONL migration preflight and idempotency checks."""

from __future__ import annotations

from collections.abc import Iterator
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
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
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
)
from tm_stage_sealer import StageSealer
import tm_schema_upgrade as schema_upgrade_module


_NATIVE_PATH_TYPE = type(Path())
MIGRATION_STREAM_CHUNK_SIZE = 5000

_EXPORT_MANIFEST_SUFFIX = ".localcat-snapshot.json"
_EXPORT_JSONL_TEMP_SUFFIX = ".localcat-export.jsonl.tmp"
_EXPORT_MANIFEST_TEMP_SUFFIX = ".localcat-export.manifest.tmp"
_EXPORT_JSONL_RECOVERY_SUFFIX = ".localcat-export-recovery.jsonl.bak"
_EXPORT_MANIFEST_RECOVERY_SUFFIX = ".localcat-export-recovery.manifest.bak"

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


@dataclass(frozen=True)
class _CreatedFileIdentity:
    device: int
    inode: int


class _NoDestinationProof:
    """Sentinel for internal restore moves that do not publish a new file."""


_NO_DESTINATION_PROOF = _NoDestinationProof()


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


class ExportPreflightError(RuntimeError):
    """Stable export failure that never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class _ExportArtifactPaths:
    """Deterministic same-directory artifact family for one destination."""

    destination: Path
    manifest: Path
    jsonl_temp: Path
    manifest_temp: Path
    jsonl_recovery: Path
    manifest_recovery: Path

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.destination, "destination"),
            (self.manifest, "manifest"),
            (self.jsonl_temp, "jsonl_temp"),
            (self.manifest_temp, "manifest_temp"),
            (self.jsonl_recovery, "jsonl_recovery"),
            (self.manifest_recovery, "manifest_recovery"),
        ):
            if type(value) is not _NATIVE_PATH_TYPE:
                raise TypeError(f"{field_name} must be an exact native Path")
        if self.manifest != self.destination.with_name(
            f"{self.destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ):
            raise ValueError("export manifest path is not deterministic")


def _export_artifact_paths(destination: Path) -> _ExportArtifactPaths:
    if type(destination) is not _NATIVE_PATH_TYPE:
        raise TypeError("destination must be an exact native Path")
    return _ExportArtifactPaths(
        destination=destination,
        manifest=destination.with_name(
            f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ),
        jsonl_temp=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_TEMP_SUFFIX}"
        ),
        manifest_temp=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_TEMP_SUFFIX}"
        ),
        jsonl_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_RECOVERY_SUFFIX}"
        ),
        manifest_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_RECOVERY_SUFFIX}"
        ),
    )


def _export_authority_paths(
    identity: CanonicalResourceIdentity,
) -> frozenset[Path]:
    """Deterministic authority paths this export must never touch."""

    journal = _activation_journal_path(identity)
    marker = _activation_lineage_marker_path(identity)
    return frozenset(
        {
            identity.configured_jsonl_path,
            identity.snapshot_manifest_path,
            identity.canonical_sidecar_path,
            journal,
            _activation_journal_temp_path(journal),
            marker,
            _activation_lineage_marker_temp_path(marker),
        }
    )


def _export_path_in_authority_family(
    identity: CanonicalResourceIdentity,
    path: Path,
) -> bool:
    """True when ``path`` is a deterministic artifact of this resource.

    The sidecar directory holds the activation journal/lineage/recovery
    and schema-upgrade artifact families plus every deterministic stage
    file; all of them embed either the sidecar name or the canonical
    target identity fragment.  A destination that collides with any of
    those names in the same directory is an authority-path alias.
    """

    sidecar = identity.canonical_sidecar_path
    if path.parent != sidecar.parent:
        return False
    name = path.name
    if name.startswith(f".{sidecar.name}.localcat-"):
        return True
    return name.startswith(".localcat-") and (
        identity.target_identity[:16] in name
    )


def _require_export_parent_safe(destination: Path) -> None:
    """Fail closed on missing, symlinked, or unwritable parent chains."""

    parent = destination.parent
    if parent == destination:
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    chain = [parent]
    chain.extend(parent.parents)
    for candidate in reversed(chain):
        try:
            observed = os.lstat(candidate)
        except (OSError, ValueError):
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None
        if not stat.S_ISDIR(observed.st_mode):
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
    try:
        observed = os.lstat(parent)
    except (OSError, ValueError):
        raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None
    if not (
        observed.st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_mode
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise ExportPreflightError("EXPORT.PARENT_UNSAFE")


def _export_existing_digest(
    path: Path,
    *,
    unsafe_code: str,
) -> str | None:
    """Digest of an existing regular single-link file, or None when absent."""

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ExportPreflightError(unsafe_code) from None
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ExportPreflightError(unsafe_code)
    digest = _try_file_digest(path)
    if digest is None:
        raise ExportPreflightError(unsafe_code)
    return digest


def _export_existing_state(
    path: Path,
    *,
    unsafe_code: str,
) -> tuple[str, tuple[int, int]] | None:
    """Strict digest and inode proof for an existing export entry."""

    digest = _export_existing_digest(path, unsafe_code=unsafe_code)
    if digest is None:
        return None
    proof = _strict_locator_proof(path, digest)
    if proof is None:
        raise ExportPreflightError(unsafe_code)
    return (digest, proof[0])


def _validate_export_destination(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
) -> tuple[
    str | None,
    str | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    """Fail closed on any alias, unsafe parent, or conflicting path state.

    Returns the exact prior digests of the destination and its adjacent
    manifest (``None`` when the file is absent) so the caller can prove
    preservation and restore on failure.
    """

    destination = paths.destination
    if not destination.is_absolute():
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    if ".." in destination.parts or destination.name in {"", ".", ".."}:
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    authority = _export_authority_paths(identity)
    for candidate in (destination, paths.manifest):
        if candidate in authority:
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
        if _export_path_in_authority_family(identity, candidate):
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
    if destination == paths.manifest:
        raise ExportPreflightError("EXPORT.PATH_ALIASED")
    for artifact in (
        paths.jsonl_temp,
        paths.manifest_temp,
        paths.jsonl_recovery,
        paths.manifest_recovery,
    ):
        if artifact in authority:
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
    _require_export_parent_safe(destination)
    destination_state = _export_existing_state(
        paths.destination,
        unsafe_code="EXPORT.DESTINATION_UNSAFE",
    )
    manifest_state = _export_existing_state(
        paths.manifest,
        unsafe_code="EXPORT.MANIFEST_UNSAFE",
    )
    destination_before = (
        None if destination_state is None else destination_state[0]
    )
    destination_identity = (
        None if destination_state is None else destination_state[1]
    )
    manifest_before = None if manifest_state is None else manifest_state[0]
    manifest_identity = None if manifest_state is None else manifest_state[1]
    if destination_before is None and manifest_before is not None:
        raise ExportPreflightError("EXPORT.PAIR_INCONSISTENT")
    for artifact, code in (
        (paths.jsonl_temp, "EXPORT.TEMP_CONFLICT"),
        (paths.manifest_temp, "EXPORT.TEMP_CONFLICT"),
        (paths.jsonl_recovery, "EXPORT.RECOVERY_CONFLICT"),
        (paths.manifest_recovery, "EXPORT.RECOVERY_CONFLICT"),
    ):
        if _path_exists(artifact):
            raise ExportPreflightError(code)
    return (
        destination_before,
        manifest_before,
        destination_identity,
        manifest_identity,
    )


def _path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise ExportPreflightError("EXPORT.PATH_UNREADABLE") from None


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _created_export_identity(
    descriptor: int,
) -> tuple[_CreatedFileIdentity, OSError | None]:
    """Read a creation handle identity, retaining one transient failure.

    A second read through the still-open handle establishes safe unlink
    authority without trusting the pathname.  The caller then reports the
    first failure after cleaning the inode it can now prove it created.
    """

    first_error: OSError | None = None
    try:
        observed = os.fstat(descriptor)
    except OSError as error:
        first_error = error
        observed = os.fstat(descriptor)
    return (
        _CreatedFileIdentity(observed.st_dev, observed.st_ino),
        first_error,
    )


def _replace_path(
    source: Path,
    destination: Path,
    *,
    expected_destination_digest: str | None | _NoDestinationProof = (
        _NO_DESTINATION_PROOF
    ),
    expected_destination_identity: (
        tuple[int, int] | None | _NoDestinationProof
    ) = _NO_DESTINATION_PROOF,
) -> None:
    digest_unset = isinstance(
        expected_destination_digest,
        _NoDestinationProof,
    )
    identity_unset = isinstance(
        expected_destination_identity,
        _NoDestinationProof,
    )
    if digest_unset != identity_unset:
        raise ValueError("destination publication proof is incomplete")
    if not digest_unset:
        observed = _export_existing_state(
            destination,
            unsafe_code="EXPORT.PRIOR_PAIR_CHANGED",
        )
        expected = (
            None
            if expected_destination_digest is None
            else (
                expected_destination_digest,
                expected_destination_identity,
            )
        )
        if observed != expected:
            raise ExportPreflightError("EXPORT.PRIOR_PAIR_CHANGED")
    os.replace(source, destination)


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
) -> tuple[str, int, _CreatedFileIdentity]:
    """Stream one exclusive JSONL temporary file and fsync it."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.TEMP_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        count = 0
        for item in records:
            payload = (
                json.dumps(
                    _export_jsonl_row(item),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            digest.update(payload)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("export write made no progress")
                view = view[written:]
            count += 1
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.TEMP_UNSAFE")
        try:
            _fsync_file(descriptor)
        except OSError as error:
            raise ExportPreflightError(
                "EXPORT.JSONL_FSYNC_FAILED"
            ) from error
        return (
            digest.hexdigest(),
            count,
            identity,
        )
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError("EXPORT.JSONL_WRITE_FAILED") from error
    finally:
        os.close(descriptor)


def _verify_export_jsonl_temp(
    path: Path,
    *,
    expected_digest: str,
    expected_count: int,
    identity: _CreatedFileIdentity,
) -> None:
    """Re-open and re-validate one JSONL temporary before publication."""

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += chunk.count(b"\n")
        if (
            digest.hexdigest() != expected_digest
            or count != expected_count
        ):
            raise ExportPreflightError("EXPORT.JSONL_VERIFY_FAILED")
        final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
    finally:
        os.close(descriptor)


def _write_export_payload_temp(
    path: Path,
    payload: bytes,
) -> _CreatedFileIdentity:
    """Write one exclusive manifest temporary file and fsync it."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.TEMP_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("export write made no progress")
            view = view[written:]
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.TEMP_UNSAFE")
        try:
            _fsync_file(descriptor)
        except OSError as error:
            raise ExportPreflightError(
                "EXPORT.MANIFEST_FSYNC_FAILED"
            ) from error
        return identity
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError(
            "EXPORT.MANIFEST_WRITE_FAILED"
        ) from error
    finally:
        os.close(descriptor)


def _verify_export_payload_temp(
    path: Path,
    *,
    expected_bytes: bytes,
    identity: _CreatedFileIdentity,
) -> None:
    """Re-open and re-validate one manifest temporary before publication."""

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if b"".join(chunks) != expected_bytes:
            raise ExportPreflightError("EXPORT.MANIFEST_VERIFY_FAILED")
        final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
    finally:
        os.close(descriptor)


def _remove_exported_if_owned(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
    expected_digest: str | None,
) -> bool:
    """Unlink one published file only when its exact identity still holds.

    Returns True when the path was never published by us (nothing to
    restore), is already absent, or was removed by us.  A foreign swap
    (different inode or digest) fails closed without deleting anything.
    """

    if expected_identity is None or expected_digest is None:
        return not _path_exists(path)
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return True
    if (
        not stat.S_ISREG(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != expected_identity
    ):
        return False
    if _try_file_digest(path) != expected_digest:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _copy_export_prior_pair(
    paths: _ExportArtifactPaths,
    *,
    destination_before: str | None,
    manifest_before: str | None,
) -> tuple[
    _CreatedFileIdentity | None,
    _CreatedFileIdentity | None,
]:
    """Stream-copy the exact prior pair into exclusive recovery files.

    Returns the (jsonl, manifest) recovery identities.  A digest mismatch
    between validation and copy means the prior pair changed under us;
    that fails stop before any publication side effect.
    """

    jsonl_identity: _CreatedFileIdentity | None = None
    manifest_identity: _CreatedFileIdentity | None = None
    if destination_before is not None:
        jsonl_identity = _copy_export_recovery_file(
            paths.destination,
            paths.jsonl_recovery,
            expected_digest=destination_before,
            code="EXPORT.JSONL_RECOVERY_COPY_FAILED",
        )
    if manifest_before is not None:
        try:
            manifest_identity = _copy_export_recovery_file(
                paths.manifest,
                paths.manifest_recovery,
                expected_digest=manifest_before,
                code="EXPORT.MANIFEST_RECOVERY_COPY_FAILED",
            )
        except ExportPreflightError as error:
            if (
                jsonl_identity is not None
                and not _remove_failed_export_artifact(
                    paths.jsonl_recovery,
                    jsonl_identity,
                )
            ):
                raise ExportPreflightError(
                    "EXPORT.RECOVERY_CLEANUP_FAILED"
                ) from error
            raise
    return jsonl_identity, manifest_identity


def _copy_export_recovery_file(
    source: Path,
    recovery: Path,
    *,
    expected_digest: str,
    code: str,
) -> _CreatedFileIdentity:
    """Copy one prior file into an owned exclusive recovery file."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(recovery, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.RECOVERY_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    source_descriptor = -1
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.RECOVERY_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
        source_descriptor = os.open(source, os.O_RDONLY | no_follow)
        observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
        ):
            raise ExportPreflightError(code)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("export recovery copy made no progress")
                view = view[written:]
        if digest.hexdigest() != expected_digest:
            raise ExportPreflightError("EXPORT.PRIOR_PAIR_CHANGED")
        os.close(source_descriptor)
        source_descriptor = -1
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.RECOVERY_UNSAFE")
        try:
            _fsync_file(descriptor)
        except OSError as error:
            raise ExportPreflightError(code) from error
        return identity
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            recovery,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.RECOVERY_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            recovery,
            identity,
        ):
            raise ExportPreflightError(
                "EXPORT.RECOVERY_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError(code) from error
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(descriptor)


def _entry_is_owned(
    path: Path,
    *,
    identity: tuple[int, int] | None,
    digest: str | None,
) -> bool:
    """Prove one published file by both exact content and created inode."""

    if (
        identity is None
        or digest is None
        or _try_file_digest(path) != digest
    ):
        return False
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(observed.st_mode)
        and (observed.st_dev, observed.st_ino) == identity
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
) -> None:
    """Restore the exact prior pair or the original absence, then fsync.

    The destination is only replaced from the owned recovery copy when
    the current entry is still provably this export's publication; a
    foreign entry is left untouched and fails stop.
    """

    if destination_before is not None:
        if _try_file_digest(paths.destination) != destination_before:
            if not _entry_is_owned(
                paths.destination,
                identity=jsonl_published_identity,
                digest=jsonl_digest,
            ):
                raise ExportPreflightError("EXPORT.JSONL_RESTORE_FAILED")
            assert jsonl_digest is not None
            assert jsonl_published_identity is not None
            _restore_export_from_recovery(
                paths.jsonl_recovery,
                paths.destination,
                expected_digest=destination_before,
                expected_destination_digest=jsonl_digest,
                expected_destination_identity=jsonl_published_identity,
                code="EXPORT.JSONL_RESTORE_FAILED",
            )
    elif not _remove_exported_if_owned(
        paths.destination,
        expected_identity=jsonl_published_identity,
        expected_digest=jsonl_digest,
    ):
        raise ExportPreflightError("EXPORT.JSONL_RESTORE_FAILED")
    if manifest_before is not None:
        if _try_file_digest(paths.manifest) != manifest_before:
            if not _entry_is_owned(
                paths.manifest,
                identity=manifest_published_identity,
                digest=manifest_digest,
            ):
                raise ExportPreflightError("EXPORT.MANIFEST_RESTORE_FAILED")
            assert manifest_digest is not None
            assert manifest_published_identity is not None
            _restore_export_from_recovery(
                paths.manifest_recovery,
                paths.manifest,
                expected_digest=manifest_before,
                expected_destination_digest=manifest_digest,
                expected_destination_identity=manifest_published_identity,
                code="EXPORT.MANIFEST_RESTORE_FAILED",
            )
    elif not _remove_exported_if_owned(
        paths.manifest,
        expected_identity=manifest_published_identity,
        expected_digest=manifest_digest,
    ):
        raise ExportPreflightError("EXPORT.MANIFEST_RESTORE_FAILED")
    try:
        _fsync_directory(paths.destination.parent)
    except OSError as error:
        raise ExportPreflightError(
            "EXPORT.RESTORE_FSYNC_FAILED"
        ) from error


def _restore_export_from_recovery(
    recovery: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_destination_digest: str,
    expected_destination_identity: tuple[int, int],
    code: str,
) -> None:
    """Replace one owned published file with the prior-bytes recovery copy.

    The recovery copy must still pass the strict no-follow, regular,
    single-link, digest and stable-identity proof; an unprovable or
    swapped recovery file fails stop and is never used to overwrite.
    """

    proof = _strict_locator_proof(recovery, expected_digest)
    if proof is None:
        raise ExportPreflightError(code)
    try:
        _replace_path(
            recovery,
            destination,
            expected_destination_digest=expected_destination_digest,
            expected_destination_identity=expected_destination_identity,
        )
    except OSError as error:
        raise ExportPreflightError(code) from error


def _published_file_identity(
    path: Path,
    expected_digest: str,
) -> tuple[int, int]:
    """Prove the just-published file still holds our exact bytes."""

    try:
        observed = os.lstat(path)
    except OSError as error:
        raise ExportPreflightError(
            "EXPORT.PUBLISH_VERIFY_FAILED"
        ) from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
    ):
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")
    if _try_file_digest(path) != expected_digest:
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")
    return (observed.st_dev, observed.st_ino)


def _verify_export_pair(
    paths: _ExportArtifactPaths,
    *,
    jsonl_digest: str,
    manifest_bytes: bytes,
) -> None:
    """Re-open and re-validate the published destination pair."""

    observed_jsonl = _export_existing_digest(
        paths.destination,
        unsafe_code="EXPORT.PUBLISH_VERIFY_FAILED",
    )
    if observed_jsonl != jsonl_digest:
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")
    observed_manifest = _export_existing_digest(
        paths.manifest,
        unsafe_code="EXPORT.PUBLISH_VERIFY_FAILED",
    )
    if observed_manifest != hashlib.sha256(manifest_bytes).hexdigest():
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")


def _cleanup_export_artifacts(
    paths: _ExportArtifactPaths,
    *,
    jsonl_temp_identity: _CreatedFileIdentity | None,
    manifest_temp_identity: _CreatedFileIdentity | None,
    jsonl_recovery_identity: _CreatedFileIdentity | None,
    manifest_recovery_identity: _CreatedFileIdentity | None,
) -> tuple[Path, ...]:
    """Remove only artifacts whose creation identity we still own.

    Returns the paths that could not be removed (hostile swap or I/O
    failure); those are never deleted and fail the next export closed.
    """

    remaining: list[Path] = []
    for path, identity in (
        (paths.jsonl_temp, jsonl_temp_identity),
        (paths.manifest_temp, manifest_temp_identity),
        (paths.jsonl_recovery, jsonl_recovery_identity),
        (paths.manifest_recovery, manifest_recovery_identity),
    ):
        if identity is None:
            continue
        try:
            _remove_created_file(path, identity)
            if _path_exists(path):
                remaining.append(path)
        except (OSError, ExportPreflightError):
            remaining.append(path)
    return tuple(remaining)


def _remove_failed_export_artifact(
    path: Path,
    identity: _CreatedFileIdentity,
) -> bool:
    """Remove an artifact created by this call after an ordinary exception.

    Process termination can still leave the deterministic temp for the
    cross-call recovery protocol, but a caught write/fsync failure must not
    turn an otherwise retryable export into a permanent TEMP_CONFLICT.
    """

    try:
        _remove_created_file(path, identity)
        return not _path_exists(path)
    except (OSError, ExportPreflightError):
        return False


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
        receipt: SnapshotReceipt | None = None
        try:
            jsonl_digest, record_count, jsonl_temp_identity = (
                _stream_export_jsonl_temp(
                    paths.jsonl_temp,
                    snapshot.records,
                )
            )
            _verify_export_jsonl_temp(
                paths.jsonl_temp,
                expected_digest=jsonl_digest,
                expected_count=record_count,
                identity=jsonl_temp_identity,
            )
            receipt = SnapshotReceipt(
                snapshot_id=f"snapshot.export.{uuid.uuid4().hex}",
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
            )
            _verify_export_payload_temp(
                paths.manifest_temp,
                expected_bytes=manifest_bytes,
                identity=manifest_temp_identity,
            )
            store.register_issued_export_receipt(
                receipt,
                destination_jsonl_path=paths.destination,
                destination_manifest_path=paths.manifest,
                expected_generation=snapshot.revision.generation,
            )
            issued = True
            jsonl_recovery_identity, manifest_recovery_identity = (
                _copy_export_prior_pair(
                    paths,
                    destination_before=destination_before,
                    manifest_before=manifest_before,
                )
            )
            _replace_path(
                paths.jsonl_temp,
                paths.destination,
                expected_destination_digest=destination_before,
                expected_destination_identity=destination_identity,
            )
            jsonl_published_identity = _published_file_identity(
                paths.destination,
                jsonl_digest,
            )
            _fsync_directory(paths.destination.parent)
            _replace_path(
                paths.manifest_temp,
                paths.manifest,
                expected_destination_digest=manifest_before,
                expected_destination_identity=manifest_identity,
            )
            manifest_published_identity = _published_file_identity(
                paths.manifest,
                manifest_digest,
            )
            _fsync_directory(paths.destination.parent)
            _verify_export_pair(
                paths,
                jsonl_digest=jsonl_digest,
                manifest_bytes=manifest_bytes,
            )
            store.complete_issued_export_receipt(
                receipt.snapshot_id,
                expected_generation=snapshot.revision.generation,
            )
        except (
            ExportPreflightError,
            SQLiteStoreLifecycleError,
            SQLiteStoreSchemaError,
            sqlite3.DatabaseError,
            OSError,
        ) as error:
            restore_error: Exception | None = None
            failure_stage = "EXPORT.PUBLISH"
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
                    )
                except Exception as restore_exception:
                    restore_error = restore_exception
                    failure_stage = "EXPORT.RESTORE"
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
            )
            ledger_error: Exception | None = None
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
                    failure_stage = "EXPORT.LEDGER"
            diagnostics: list[ExportDiagnostic] = []
            if cleanup_remaining:
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
        remaining = _cleanup_export_artifacts(
            paths,
            jsonl_temp_identity=None,
            manifest_temp_identity=None,
            jsonl_recovery_identity=jsonl_recovery_identity,
            manifest_recovery_identity=manifest_recovery_identity,
        )
        success_diagnostics = (
            ()
            if not remaining
            else (
                _export_diagnostic(
                    "EXPORT.CLEANUP_PENDING",
                    "EXPORT_ARTIFACTS_REMAIN",
                ),
            )
        )
        assert jsonl_digest is not None
        assert receipt is not None
        return ExportReport(
            exported_count=record_count,
            skipped_count=0,
            destination_digest=jsonl_digest,
            canonical_generation=snapshot.revision.generation,
            exported_revision=snapshot.revision.head_revision,
            snapshot_id=receipt.snapshot_id,
            snapshot_receipt_digest=snapshot_receipt_digest(receipt),
            snapshot_receipt=receipt,
            diagnostics=success_diagnostics,
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
            sealed = StageSealer(
                registry=coordinator.sealed_registry,
                canonical_store_id=self._canonical_store_id,
            ).seal(
                copy_stage,
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
            sealed = StageSealer(
                registry=coordinator.sealed_registry,
                canonical_store_id=new_store_id,
            ).seal(
                stage,
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
            _validate_reusable_stage(
                stage,
                canonical_store_id=canonical_store_id,
                preflight=preflight,
                batch_kind=batch_kind,
                batch_prefix=batch_prefix,
                snapshot_prefix=snapshot_prefix,
                batch_id=batch_id,
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
    """Stream-compare record, gram, and FTS rows with bounded per-record sets."""

    folded_cursor = connection.execute(
        "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
    )
    gram_cursor = connection.execute(
        "SELECT record_id, gram_size, gram FROM tm_gram "
        "ORDER BY record_id, gram_size, gram"
    )
    fts_cursor = (
        connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_fts "
            "ORDER BY record_id"
        )
        if fts5_available
        else None
    )
    current_gram = gram_cursor.fetchone()
    current_fts = fts_cursor.fetchone() if fts_cursor is not None else None
    expected_record_id = 1
    for folded_row in folded_cursor:
        record_id = int(folded_row[0])
        folded_source = str(folded_row[1])
        if record_id != expected_record_id:
            raise ValueError("record ids are not contiguous")
        expected_record_id += 1
        actual_grams: set[tuple[int, str]] = set()
        while current_gram is not None and int(current_gram[0]) == record_id:
            actual_grams.add((int(current_gram[1]), str(current_gram[2])))
            current_gram = gram_cursor.fetchone()
        if current_gram is not None and int(current_gram[0]) < record_id:
            raise ValueError("candidate gram index is out of order")
        expected_grams: set[tuple[int, str]] = set()
        for gram_size in required_sizes:
            expected_grams.update(
                (gram_size, gram)
                for gram in unique_character_ngrams(
                    folded_source,
                    gram_size,
                )
            )
        if actual_grams != expected_grams:
            raise ValueError("candidate gram index is incomplete")
        if fts5_available:
            assert fts_cursor is not None
            actual_fts: set[tuple[int, str]] = set()
            while current_fts is not None and int(current_fts[0]) == record_id:
                actual_fts.add((int(current_fts[0]), str(current_fts[1])))
                current_fts = fts_cursor.fetchone()
            if current_fts is not None and int(current_fts[0]) < record_id:
                raise ValueError("candidate FTS index is out of order")
            if actual_fts != {(record_id, folded_source)}:
                raise ValueError("candidate FTS index is incomplete")
    if current_gram is not None:
        raise ValueError("candidate gram index has extra rows")
    if current_fts is not None:
        raise ValueError("candidate FTS index has extra rows")


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
) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(observed.st_mode)
        and observed.st_dev == expected.device
        and observed.st_ino == expected.inode
    ):
        path.unlink()


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
    """Hash one file's exact bytes, or return None when unreadable."""

    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


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
