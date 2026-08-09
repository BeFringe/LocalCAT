"""Read-only JSONL migration preflight and idempotency checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import stat
from typing import cast

from tm_contracts import (
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    MigrationDiagnostic,
    MigrationPreflight,
)


_NATIVE_PATH_TYPE = type(Path())


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

        if type(source) is not _NATIVE_PATH_TYPE:
            raise TypeError("source must be an exact native Path")
        if source != self._resource_identity.configured_jsonl_path:
            raise MigrationPreflightError(
                "MIGRATION.RESOURCE_IDENTITY_MISMATCH"
            )
        _require_target_parent_writable(self._resource_identity)
        _require_source_readable(source)

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
                                summary=(
                                    "ROW_SKIPPED_INVALID_REQUIRED_FIELD"
                                ),
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
        except OSError as error:
            raise MigrationPreflightError(
                "MIGRATION.SOURCE_UNREADABLE"
            ) from error

        if row_count == 0:
            raise MigrationPreflightError("MIGRATION.SOURCE_EMPTY")
        source_digest = digest.hexdigest()
        _ = self._completed_migration_revision(source_digest)
        return MigrationPreflight(
            source_digest=source_digest,
            valid_count=valid_count,
            invalid_count=invalid_count,
            duplicate_source_count=duplicate_source_count,
            variant_count=variant_count,
            diagnostics=tuple(diagnostics),
        )

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


__all__ = ["MigrationPreflightError", "TMMigrationService"]
