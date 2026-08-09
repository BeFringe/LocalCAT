"""Read-only JSONL migration preflight and idempotency checks."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
from typing import cast

from tm_contracts import (
    SNAPSHOT_MANIFEST_VERSION,
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    MigrationDiagnostic,
    MigrationPreflight,
    MutableStageRef,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    initialize_stage_schema,
    inspect_stage_schema,
    unique_character_ngrams,
)


_NATIVE_PATH_TYPE = type(Path())


@dataclass(frozen=True)
class MigrationStageBuild:
    """Either one unpublished mutable stage or an active idempotent hit."""

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


@dataclass(frozen=True)
class _JSONLScan:
    preflight: MigrationPreflight
    drafts: tuple[TMRecordDraft, ...]
    legacy_line_nos: tuple[int, ...]


@dataclass(frozen=True)
class _CreatedFileIdentity:
    device: int
    inode: int


class MigrationPreflightError(RuntimeError):
    """Stable preflight failure that never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


class TMMigrationService:
    """Preflight one configured legacy JSONL without changing any asset."""

    def __init__(
        self,
        *,
        resource_identity: CanonicalResourceIdentity,
        canonical_store_id: str,
    ) -> None:
        self._resource_identity = _snapshot_resource_identity(resource_identity)
        if type(canonical_store_id) is not str:
            raise TypeError("canonical_store_id must be a built-in string")
        if not canonical_store_id.strip():
            raise ValueError("canonical_store_id must not be empty")
        self._canonical_store_id = canonical_store_id

    @property
    def resource_identity(self) -> CanonicalResourceIdentity:
        return self._resource_identity

    @property
    def canonical_store_id(self) -> str:
        return self._canonical_store_id

    def preflight(self, source: Path) -> MigrationPreflight:
        """Stream exact source bytes and return safe, deterministic facts."""

        self._validate_source_preconditions(source)
        scan = _scan_jsonl(source, collect_drafts=False)
        _ = self._completed_migration_revision(scan.preflight.source_digest)
        return scan.preflight

    def build_mutable_stage(self, source: Path) -> MigrationStageBuild:
        """Build or reuse one complete unpublished migration stage."""

        preflight = self.preflight(source)
        completed_revision = self._completed_migration_revision(
            preflight.source_digest
        )
        if completed_revision is not None:
            return MigrationStageBuild(
                preflight=preflight,
                mutable_stage=None,
                reused_completed_revision=completed_revision,
            )

        scan = _scan_jsonl(source, collect_drafts=True)
        if scan.preflight != preflight:
            raise MigrationPreflightError("MIGRATION.SOURCE_CHANGED")
        stage = _migration_stage_ref(
            self._resource_identity,
            source_digest=preflight.source_digest,
        )
        if stage.staged_db_path.exists() or stage.manifest_temp_path.exists():
            _validate_reusable_stage(
                stage,
                canonical_store_id=self._canonical_store_id,
                preflight=preflight,
            )
            return MigrationStageBuild(
                preflight=preflight,
                mutable_stage=stage,
                reused_completed_revision=None,
            )

        initialize_stage_schema(
            stage,
            canonical_store_id=self._canonical_store_id,
        )
        stage_identity = _created_file_identity(stage.staged_db_path)
        manifest_identity: _CreatedFileIdentity | None = None
        try:
            store = SQLiteTMStore(
                stage,
                canonical_store_id=self._canonical_store_id,
            )
            _ = store.append_batch(
                batch_id=f"migration.{preflight.source_digest}",
                kind="migration",
                drafts=scan.drafts,
                source_digest=preflight.source_digest,
                source_path=source,
                legacy_line_nos=scan.legacy_line_nos,
                invalid_count=preflight.invalid_count,
                duplicate_source_count=preflight.duplicate_source_count,
            )
            revision = store.canonical_revision()
            if (
                revision.head_revision != 1
                or revision.record_count != preflight.valid_count
            ):
                raise MigrationPreflightError(
                    "MIGRATION.STAGE_REVISION_INVALID"
                )
            receipt = SnapshotReceipt(
                snapshot_id=(
                    f"snapshot.migration.{preflight.source_digest[:24]}"
                ),
                resource_id=self._resource_identity.resource_id,
                canonical_store_id=self._canonical_store_id,
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
                canonical_store_id=self._canonical_store_id,
                preflight=preflight,
            )
        except BaseException:
            if manifest_identity is not None:
                _remove_created_file(
                    stage.manifest_temp_path,
                    manifest_identity,
                )
            _remove_created_file(stage.staged_db_path, stage_identity)
            raise
        return MigrationStageBuild(
            preflight=preflight,
            mutable_stage=stage,
            reused_completed_revision=None,
        )

    def _validate_source_preconditions(self, source: Path) -> None:
        if type(source) is not _NATIVE_PATH_TYPE:
            raise TypeError("source must be an exact native Path")
        if source != self._resource_identity.configured_jsonl_path:
            raise MigrationPreflightError(
                "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
            )
        _require_target_parent_writable(self._resource_identity)
        _require_source_readable(source)

    def _completed_migration_revision(
        self,
        source_digest: str,
    ) -> int | None:
        """Return the reusable completed revision, if no sidecar conflict."""

        identity = self._resource_identity
        sidecar = identity.canonical_sidecar_path
        manifest = identity.snapshot_manifest_path
        if not sidecar.exists():
            if manifest.exists():
                raise MigrationPreflightError(
                    "MIGRATION.MANIFEST_WITHOUT_SIDECAR"
                )
            return None
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
                    or meta.get("target_identity") != identity.target_identity
                ):
                    raise MigrationPreflightError(
                        "MIGRATION.SIDECAR_IDENTITY_MISMATCH"
                    )
                if meta.get("activation_status") != "ACTIVE":
                    raise MigrationPreflightError(
                        "MIGRATION.SIDECAR_NOT_REUSABLE"
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
                return revision_value
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


def _scan_jsonl(source: Path, *, collect_drafts: bool) -> _JSONLScan:
    digest = hashlib.sha256()
    valid_count = 0
    invalid_count = 0
    duplicate_source_count = 0
    variant_count = 0
    row_count = 0
    source_counts: dict[str, int] = {}
    diagnostics: list[MigrationDiagnostic] = []
    drafts: list[TMRecordDraft] = []
    legacy_line_nos: list[int] = []

    try:
        with source.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                row_count += 1
                digest.update(raw_line)
                try:
                    decoded_line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code="ROW.INVALID_UTF8",
                            stage="PREFLIGHT.DECODE",
                            summary="ROW_SKIPPED_INVALID_UTF8",
                        )
                    )
                    continue
                try:
                    payload = json.loads(
                        decoded_line,
                        parse_constant=_reject_json_constant,
                    )
                except (json.JSONDecodeError, ValueError):
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code="ROW.INVALID_JSON",
                            stage="PREFLIGHT.PARSE",
                            summary="ROW_SKIPPED_INVALID_JSON",
                        )
                    )
                    continue
                if type(payload) is not dict:
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code="ROW.INVALID_SHAPE",
                            stage="PREFLIGHT.VALIDATE",
                            summary="ROW_SKIPPED_INVALID_SHAPE",
                        )
                    )
                    continue
                row = cast(dict[str, object], payload)
                source_raw = row.get("source")
                target_raw = row.get("target")
                if (
                    type(source_raw) is not str
                    or source_raw == ""
                    or type(target_raw) is not str
                    or target_raw == ""
                ):
                    invalid_count += 1
                    diagnostics.append(
                        _rejected_diagnostic(
                            line_number,
                            code="ROW.INVALID_REQUIRED_FIELD",
                            stage="PREFLIGHT.VALIDATE",
                            summary="ROW_SKIPPED_INVALID_REQUIRED_FIELD",
                        )
                    )
                    continue

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
                if collect_drafts:
                    drafts.append(_draft_from_jsonl(row))
                    legacy_line_nos.append(line_number)
    except OSError as error:
        raise MigrationPreflightError(
            "MIGRATION.SOURCE_UNREADABLE"
        ) from error

    if row_count == 0:
        raise MigrationPreflightError("MIGRATION.SOURCE_EMPTY")
    preflight = MigrationPreflight(
        source_digest=digest.hexdigest(),
        valid_count=valid_count,
        invalid_count=invalid_count,
        duplicate_source_count=duplicate_source_count,
        variant_count=variant_count,
        diagnostics=tuple(diagnostics),
    )
    return _JSONLScan(
        preflight=preflight,
        drafts=tuple(drafts),
        legacy_line_nos=tuple(legacy_line_nos),
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
        provenance=(("source", "legacy-jsonl"),),
    )


def _migration_stage_ref(
    identity: CanonicalResourceIdentity,
    *,
    source_digest: str,
) -> MutableStageRef:
    name_key = f"{identity.target_identity[:16]}.{source_digest[:16]}"
    parent = identity.canonical_sidecar_path.parent
    return MutableStageRef(
        stage_id=f"stage.migration.{name_key}",
        resource_identity=identity,
        staged_db_path=(
            parent / f".localcat-migration.{name_key}.sqlite3.stage"
        ).resolve(),
        manifest_temp_path=(
            parent / f".localcat-migration.{name_key}.manifest.tmp"
        ).resolve(),
    )


def _validate_reusable_stage(
    stage: MutableStageRef,
    *,
    canonical_store_id: str,
    preflight: MigrationPreflight,
) -> None:
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
                "WHERE kind = 'migration' ORDER BY batch_id"
            ).fetchall()
            expected_batch = (
                f"migration.{preflight.source_digest}",
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
            record_rows = connection.execute(
                "SELECT record_id, source_fold_v1 FROM tm_record "
                "ORDER BY record_id"
            ).fetchall()
            if len(record_rows) != preflight.valid_count:
                raise ValueError("record count does not close")
            receipt_rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version, destination_jsonl_path, "
                "destination_manifest_path, status "
                "FROM tm_snapshot_receipt ORDER BY snapshot_id"
            ).fetchall()
            expected_snapshot_id = (
                f"snapshot.migration.{preflight.source_digest[:24]}"
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
            expected_grams = {
                (int(record_id), gram_size, gram)
                for record_id, folded_source in record_rows
                for gram_size in required_sizes
                for gram in unique_character_ngrams(
                    str(folded_source),
                    gram_size,
                )
            }
            actual_grams = {
                (int(record_id), int(gram_size), str(gram))
                for record_id, gram_size, gram in connection.execute(
                    "SELECT record_id, gram_size, gram FROM tm_gram"
                ).fetchall()
            }
            if actual_grams != expected_grams:
                raise ValueError("candidate gram index is incomplete")
            if schema.fts5_available:
                expected_fts = {
                    (int(record_id), str(folded_source))
                    for record_id, folded_source in record_rows
                }
                actual_fts = {
                    (int(record_id), str(folded_source))
                    for record_id, folded_source in connection.execute(
                        "SELECT record_id, source_fold_v1 FROM tm_fts"
                    ).fetchall()
                }
                if actual_fts != expected_fts:
                    raise ValueError("candidate FTS index is incomplete")
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
            or manifest.receipt.snapshot_id
            != f"snapshot.migration.{preflight.source_digest[:24]}"
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
    "MigrationPreflightError",
    "MigrationStageBuild",
    "TMMigrationService",
]
