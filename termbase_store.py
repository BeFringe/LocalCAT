"""Strict local storage boundary for mixed legacy/v1 termbase CSV files."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import uuid
from pathlib import Path
from typing import Callable, TextIO

from editor_contracts import (
    LegacyTermRow,
    PreparedTermMutation,
    TermCleanupReport,
    TermCommitOutcome,
    TermCommitState,
    TermDraft,
    TermMatchPolicy,
    TermMutationReport,
    TermRecord,
    TermRecordLocator,
    TermRowKind,
)
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    LockLease,
    LockPolicy,
    LockWait,
    PendingPublication,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
)
from resource_platform_io import (
    bind_rooted_regular,
    digest_bound,
    iter_bound_chunks,
    platform_relative_path,
    read_bound_all,
)


_V1_MARKER = TermRowKind.V1.value
_BOOLEAN_VALUES = {"false": False, "true": True}
_MutationCounts = tuple[int, int, int, int, int]
_MAX_TERMBASE_BYTES = 512 * 1024 * 1024
_TERMBASE_LOCK_PREFIX = b"localcat.termbase.lock.v1\0"


@dataclass(frozen=True, slots=True)
class TermbasePortableSnapshot:
    """Owner-issued facts for one canonical mixed legacy/v1 CSV snapshot."""

    payload_digest: str
    source_baseline_digest: str
    payload_byte_count: int
    record_count: int
    legacy_record_count: int
    v1_record_count: int

    def __post_init__(self) -> None:
        for name, digest in (
            ("payload", self.payload_digest),
            ("source baseline", self.source_baseline_digest),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise TypeError(
                    f"termbase snapshot {name} digest must be lowercase SHA-256"
                )
        for name, value in (
            ("payload byte count", self.payload_byte_count),
            ("record count", self.record_count),
            ("legacy record count", self.legacy_record_count),
            ("v1 record count", self.v1_record_count),
        ):
            if type(value) is not int or value < 0:
                raise TypeError(f"termbase snapshot {name} must be nonnegative int")
        if self.legacy_record_count + self.v1_record_count != self.record_count:
            raise ValueError("termbase snapshot row counts must close")


class TermbaseValidationError(ValueError):
    """Structured, content-safe validation failure for a termbase snapshot."""

    def __init__(
        self,
        code: str,
        row_ordinal: int | None = None,
        conflicting_row_ordinal: int | None = None,
    ) -> None:
        self.code = code
        self.row_ordinal = row_ordinal
        self.conflicting_row_ordinal = conflicting_row_ordinal
        location = "" if row_ordinal is None else f" at row {row_ordinal}"
        if conflicting_row_ordinal is not None:
            location += f" (conflicts with row {conflicting_row_ordinal})"
        super().__init__(f"{code}{location}")


class _TermCommitVerificationError(RuntimeError):
    """Internal value mismatch after replace, eligible for proven rollback."""


class _CapturingLineIterator:
    """Feed ``csv.reader`` while retaining the physical text of each record."""

    def __init__(self, stream: TextIO) -> None:
        self._lines = iter(stream)
        self._captured: list[str] = []

    def __iter__(self) -> _CapturingLineIterator:
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        self._captured.append(line)
        return line

    def start_record(self) -> None:
        self._captured.clear()

    def captured_record(self) -> str:
        return "".join(self._captured)


class TermbaseStore:
    """Read and prepare atomic mutations for one mixed CSV termbase."""

    def __init__(self, backend: PlatformFileBackend | None = None) -> None:
        if backend is not None and not isinstance(backend, PlatformFileBackend):
            raise TypeError("termbase backend must satisfy PlatformFileBackend")
        self._backend = backend
        self._prepared_counts: dict[PreparedTermMutation, _MutationCounts] = {}
        self._commit_outcomes: dict[
            PreparedTermMutation,
            TermCommitOutcome,
        ] = {}
        self._prepared_artifacts: dict[
            PreparedTermMutation,
            tuple[FileObjectIdentity | None, FileObjectIdentity | None],
        ] = {}
        self._prepared_sources: dict[PreparedTermMutation, EntrySnapshot] = {}
        self._portable_artifacts: dict[
            tuple[Path, str],
            FileObjectIdentity,
        ] = {}

    def commit(self, prepared: PreparedTermMutation) -> TermCommitOutcome:
        _validate_prepared_mutation(prepared)
        previous_outcome = self._commit_outcomes.get(prepared)
        if previous_outcome is not None and previous_outcome.state in (
            TermCommitState.COMMITTED,
            TermCommitState.ROLLED_BACK,
            TermCommitState.INDETERMINATE,
        ):
            raise ValueError("terminal term commit outcome cannot be retried")

        resource_path = prepared.resource_path
        recovery_path = prepared.recovery_path
        if recovery_path is None:
            return self._remember_outcome(
                prepared,
                _failed_outcome(
                    state=TermCommitState.NOT_COMMITTED,
                    error_code="RECOVERY_UNAVAILABLE",
                    retryable=False,
                    recovery_path=None,
                    safe_detail="Prepare the termbase change again before retrying.",
                ),
            )
        try:
            recovery_bytes, recovery_snapshot = self._read_bound_snapshot(recovery_path)
        except (PlatformFileError, OSError):
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "RECOVERY_READ_FAILED",
                    "Prepare the termbase change again before retrying.",
                ),
            )
        issued_identities = self._prepared_artifacts.get(prepared)
        if (
            issued_identities is None
            or issued_identities[1] != recovery_snapshot.identity
        ):
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "RECOVERY_CHANGED",
                    "Prepare the termbase change again before retrying.",
                ),
            )
        if hashlib.sha256(recovery_bytes).hexdigest() != prepared.base_digest:
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "RECOVERY_CHANGED",
                    "Prepare the termbase change again before retrying.",
                ),
            )

        try:
            staged_bytes, staged_snapshot = self._read_bound_snapshot(
                prepared.staged_path
            )
            staged_records = self._records_from_bytes(staged_bytes)
        except (PlatformFileError, OSError, TermbaseValidationError):
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "STAGED_READ_FAILED",
                    "Prepare the termbase change again before retrying.",
                ),
            )
        if staged_records != prepared.candidate_records:
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "STAGED_CHANGED",
                    "Prepare the termbase change again before retrying.",
                ),
            )
        if issued_identities[0] != staged_snapshot.identity:
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "STAGED_CHANGED",
                    "Prepare the termbase change again before retrying.",
                ),
            )
        staged_digest = hashlib.sha256(staged_bytes).hexdigest()

        expected_source = self._prepared_sources.get(prepared)
        if expected_source is None:
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "SOURCE_CHANGED",
                    "Reload the termbase and retry.",
                ),
            )
        try:
            source_bytes, source_snapshot = self._read_bound_snapshot(resource_path)
        except (PlatformFileError, OSError):
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "SOURCE_DIGEST_FAILED",
                    "Reload the termbase and retry.",
                ),
            )
        if (
            source_snapshot != expected_source
            or hashlib.sha256(source_bytes).hexdigest() != prepared.base_digest
        ):
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "SOURCE_CHANGED",
                    "Reload the termbase and retry.",
                ),
            )

        backend = self._platform_backend(resource_path)
        owner_commit_started = False
        owner_committed = False
        committed_outcome: TermCommitOutcome | None = None

        def commit_owner(_identity: FileObjectIdentity) -> None:
            nonlocal owner_commit_started, owner_committed, committed_outcome
            owner_commit_started = True
            committed_digest = self._digest_resource(resource_path)
            if committed_digest != staged_digest:
                raise _TermCommitVerificationError("committed digest mismatch")
            committed_records = self.list_records(resource_path)
            if committed_records != prepared.candidate_records:
                raise _TermCommitVerificationError("committed records mismatch")
            old_records = self._records_from_bytes(recovery_bytes)
            counts = self._prepared_counts.get(
                prepared,
                _derive_mutation_counts(
                    prepared.action,
                    old_records,
                    committed_records,
                ),
            )
            report = TermMutationReport(
                action=prepared.action,
                resource_path=resource_path,
                committed_digest=committed_digest,
                records=committed_records,
                created=counts[0],
                updated=counts[1],
                deleted=counts[2],
                imported=counts[3],
                overwritten=counts[4],
            )
            try:
                _unlink_exact(backend, prepared.staged_path, staged_snapshot.identity)
                self._prepared_artifacts[prepared] = (None, recovery_snapshot.identity)
            except PlatformFileError:
                committed_outcome = self._remember_outcome(
                    prepared,
                    _indeterminate_outcome(prepared, "STAGED_CLEANUP_FAILED"),
                )
                owner_committed = True
                return
            committed_outcome = self._remember_outcome(
                prepared,
                TermCommitOutcome(
                    state=TermCommitState.COMMITTED,
                    report=report,
                    error_code=None,
                    retryable=False,
                    recovery_path=recovery_path,
                    quarantined=False,
                    safe_detail=None,
                ),
            )
            owner_committed = True

        try:
            _publish_bytes(
                backend,
                resource_path,
                staged_bytes,
                replace=True,
                private=True,
                expected_before_digest=prepared.base_digest,
                owner_commit=commit_owner,
            )
        except PlatformFileError as error:
            if owner_committed:
                return self._remember_outcome(
                    prepared,
                    _indeterminate_outcome(prepared, "TERMINAL_REPROOF_FAILED"),
                )
            if owner_commit_started:
                return self._rollback_after_commit_failure(
                    prepared=prepared,
                    recovery_bytes=recovery_bytes,
                    error_code="COMMIT_VERIFICATION_FAILED",
                )
            if error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
                return self._remember_outcome(
                    prepared,
                    _indeterminate_outcome(prepared, "REPLACE_INDETERMINATE"),
                )
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "REPLACE_FAILED",
                    "Retry the change or discard its prepared artifacts.",
                ),
            )
        except OSError:
            if owner_commit_started:
                return self._rollback_after_commit_failure(
                    prepared=prepared,
                    recovery_bytes=recovery_bytes,
                    error_code="COMMIT_VERIFICATION_FAILED",
                )
            return self._remember_outcome(
                prepared,
                _not_committed_outcome(
                    prepared,
                    "REPLACE_FAILED",
                    "Retry the change or discard its prepared artifacts.",
                ),
            )
        except (
            TermbaseValidationError,
            _TermCommitVerificationError,
        ):
            return self._rollback_after_commit_failure(
                prepared=prepared,
                recovery_bytes=recovery_bytes,
                error_code="COMMIT_VERIFICATION_FAILED",
            )
        except Exception:
            if owner_commit_started:
                rollback = self._rollback_after_commit_failure(
                    prepared=prepared,
                    recovery_bytes=recovery_bytes,
                    error_code="COMMIT_VERIFICATION_FAILED",
                )
                if rollback.state is TermCommitState.ROLLED_BACK:
                    raise
                return rollback
            raise

        if committed_outcome is None:
            raise AssertionError("termbase owner commit did not issue an outcome")
        return committed_outcome

    def discard(self, prepared: PreparedTermMutation) -> None:
        _validate_prepared_mutation(prepared)
        outcome = self._commit_outcomes.get(prepared)
        if outcome is not None and outcome.state in (
            TermCommitState.COMMITTED,
            TermCommitState.INDETERMINATE,
        ):
            raise ValueError(
                "committed or indeterminate term mutation cannot be discarded"
            )

        backend = self._platform_backend(prepared.resource_path)
        identities = self._prepared_artifacts.get(prepared)
        if identities is None:
            raise ValueError("prepared term mutation has no owned artifacts")
        if identities[0] is not None:
            _unlink_exact(backend, prepared.staged_path, identities[0])
        if prepared.recovery_path is not None and identities[1] is not None:
            _unlink_exact(backend, prepared.recovery_path, identities[1])
        _ = self._prepared_counts.pop(prepared, None)
        _ = self._commit_outcomes.pop(prepared, None)
        _ = self._prepared_artifacts.pop(prepared, None)
        _ = self._prepared_sources.pop(prepared, None)

    def finalize(
        self,
        prepared: PreparedTermMutation,
        outcome: TermCommitOutcome,
    ) -> TermCleanupReport:
        _validate_prepared_mutation(prepared)
        if not isinstance(outcome, TermCommitOutcome):
            raise TypeError("term commit outcome must be a TermCommitOutcome")
        recorded_outcome = self._commit_outcomes.get(prepared)
        if (
            outcome.state is not TermCommitState.COMMITTED
            or recorded_outcome is not outcome
        ):
            raise ValueError("only the recorded committed outcome can be finalized")

        recovery_path = prepared.recovery_path
        if recovery_path is None:
            _ = self._prepared_counts.pop(prepared, None)
            _ = self._commit_outcomes.pop(prepared, None)
            return TermCleanupReport(
                cleaned=True,
                recovery_path=None,
                warning_code=None,
            )

        identities = self._prepared_artifacts.get(prepared)
        if identities is None or identities[1] is None:
            return TermCleanupReport(
                cleaned=False,
                recovery_path=recovery_path,
                warning_code="RECOVERY_DELETE_FAILED",
            )
        try:
            _unlink_exact(
                self._platform_backend(recovery_path),
                recovery_path,
                identities[1],
            )
        except PlatformFileError:
            return TermCleanupReport(
                cleaned=False,
                recovery_path=recovery_path,
                warning_code="RECOVERY_DELETE_FAILED",
            )

        _ = self._prepared_counts.pop(prepared, None)
        _ = self._commit_outcomes.pop(prepared, None)
        _ = self._prepared_artifacts.pop(prepared, None)
        _ = self._prepared_sources.pop(prepared, None)
        return TermCleanupReport(
            cleaned=True,
            recovery_path=None,
            warning_code=None,
        )

    def _remember_outcome(
        self,
        prepared: PreparedTermMutation,
        outcome: TermCommitOutcome,
    ) -> TermCommitOutcome:
        self._commit_outcomes[prepared] = outcome
        return outcome

    def _rollback_after_commit_failure(
        self,
        *,
        prepared: PreparedTermMutation,
        recovery_bytes: bytes,
        error_code: str,
    ) -> TermCommitOutcome:
        rollback_outcome: TermCommitOutcome | None = None

        def commit_rollback(_identity: FileObjectIdentity) -> None:
            nonlocal rollback_outcome
            try:
                restored_digest = self._digest_resource(prepared.resource_path)
            except (PlatformFileError, OSError) as error:
                raise _TermCommitVerificationError(
                    "rollback digest unavailable"
                ) from error
            if restored_digest != prepared.base_digest:
                raise _TermCommitVerificationError("rollback digest mismatch")
            rollback_outcome = self._remember_outcome(
                prepared,
                _failed_outcome(
                    state=TermCommitState.ROLLED_BACK,
                    error_code=error_code,
                    retryable=True,
                    recovery_path=prepared.recovery_path,
                    safe_detail="The previous bytes were restored; retry the change.",
                ),
            )

        try:
            staged_bytes, _snapshot = self._read_bound_snapshot(prepared.staged_path)
            _publish_bytes(
                self._platform_backend(prepared.resource_path),
                prepared.resource_path,
                recovery_bytes,
                replace=True,
                private=True,
                expected_before_digest=hashlib.sha256(staged_bytes).hexdigest(),
                owner_commit=commit_rollback,
            )
        except _TermCommitVerificationError:
            return self._remember_outcome(
                prepared,
                _indeterminate_outcome(
                    prepared,
                    "ROLLBACK_VERIFICATION_FAILED",
                ),
            )
        except Exception:
            return self._remember_outcome(
                prepared,
                _indeterminate_outcome(prepared, "ROLLBACK_FAILED"),
            )

        if rollback_outcome is None:
            raise AssertionError("termbase rollback did not commit an outcome")
        return rollback_outcome

    def prepare_create(
        self,
        path: Path,
        draft: TermDraft,
    ) -> PreparedTermMutation:
        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        path = _absolute_resource_path(path)
        original, base_digest, records, source_snapshot = self._read_snapshot(path)
        conflicting_ordinal = _source_ordinal(records, draft.source)
        if conflicting_ordinal is not None:
            raise TermbaseValidationError(
                "DUPLICATE_SOURCE",
                len(records),
                conflicting_ordinal,
            )

        existing_ids = {
            record.record_id for record in records if record.record_id is not None
        }
        record_id = str(uuid.uuid4())
        while record_id in existing_ids:
            record_id = str(uuid.uuid4())

        candidate_rows = _rows_from_records(records)
        candidate_rows.append(
            [
                _V1_MARKER,
                record_id,
                draft.source,
                draft.target,
                _format_bool(draft.match_case),
                _format_bool(draft.whole_word),
            ]
        )
        prepared = self._prepare_artifacts(
            action="create",
            path=path,
            original=original,
            base_digest=base_digest,
            source_snapshot=source_snapshot,
            candidate_rows=candidate_rows,
        )
        self._prepared_counts[prepared] = (1, 0, 0, 0, 0)
        return prepared

    def prepare_update(
        self,
        path: Path,
        locator: TermRecordLocator,
        draft: TermDraft,
    ) -> PreparedTermMutation:
        if not isinstance(draft, TermDraft):
            raise TypeError("term draft must be a TermDraft")
        path = _absolute_resource_path(path)
        original, base_digest, records, source_snapshot = self._read_snapshot(path)
        row_ordinal, current = _locate_current_record(
            records,
            base_digest,
            locator,
        )
        conflicting_ordinal = _source_ordinal(records, draft.source)
        if (
            conflicting_ordinal is not None
            and conflicting_ordinal != row_ordinal
        ):
            raise TermbaseValidationError(
                "CONFLICTING_SOURCE",
                row_ordinal,
                conflicting_ordinal,
            )

        candidate_rows = _rows_from_records(records)
        if current.locator.row_kind is TermRowKind.LEGACY:
            candidate_rows[row_ordinal] = [draft.source, draft.target]
        else:
            record_id = current.record_id
            if record_id is None:
                raise AssertionError("validated v1 record must have an id")
            candidate_rows[row_ordinal] = [
                _V1_MARKER,
                record_id,
                draft.source,
                draft.target,
                _format_bool(draft.match_case),
                _format_bool(draft.whole_word),
            ]
        prepared = self._prepare_artifacts(
            action="update",
            path=path,
            original=original,
            base_digest=base_digest,
            source_snapshot=source_snapshot,
            candidate_rows=candidate_rows,
        )
        self._prepared_counts[prepared] = (0, 1, 0, 0, 0)
        return prepared

    def prepare_delete(
        self,
        path: Path,
        locator: TermRecordLocator,
    ) -> PreparedTermMutation:
        path = _absolute_resource_path(path)
        original, base_digest, records, source_snapshot = self._read_snapshot(path)
        row_ordinal, _ = _locate_current_record(
            records,
            base_digest,
            locator,
        )
        candidate_rows = _rows_from_records(records)
        del candidate_rows[row_ordinal]
        prepared = self._prepare_artifacts(
            action="delete",
            path=path,
            original=original,
            base_digest=base_digest,
            source_snapshot=source_snapshot,
            candidate_rows=candidate_rows,
        )
        self._prepared_counts[prepared] = (0, 0, 1, 0, 0)
        return prepared

    def prepare_merge_legacy(
        self,
        path: Path,
        rows: tuple[LegacyTermRow, ...],
    ) -> PreparedTermMutation:
        if not isinstance(rows, tuple):
            raise TypeError("legacy merge rows must be a tuple")
        if not all(isinstance(row, LegacyTermRow) for row in rows):
            raise TypeError("legacy merge rows must contain LegacyTermRow values")
        if not rows:
            raise TermbaseValidationError("EMPTY_IMPORT")

        path = _absolute_resource_path(path)
        original, base_digest, records, source_snapshot = self._read_snapshot(path)
        incoming_targets: dict[str, str] = {}
        first_input_order: list[str] = []
        for row in rows:
            if row.source not in incoming_targets:
                first_input_order.append(row.source)
            incoming_targets[row.source] = row.target

        candidate_rows = _rows_from_records(records)
        existing_sources = {record.source for record in records}
        for row_ordinal, record in enumerate(records):
            target = incoming_targets.get(record.source)
            if target is not None:
                candidate_rows[row_ordinal] = _row_with_target(record, target)

        for source in first_input_order:
            if source not in existing_sources:
                candidate_rows.append([source, incoming_targets[source]])

        prepared = self._prepare_artifacts(
            action="merge_legacy",
            path=path,
            original=original,
            base_digest=base_digest,
            source_snapshot=source_snapshot,
            candidate_rows=candidate_rows,
        )
        imported = len(incoming_targets)
        overwritten = len(rows) - imported + sum(
            source in existing_sources for source in incoming_targets
        )
        self._prepared_counts[prepared] = (
            0,
            0,
            0,
            imported,
            overwritten,
        )
        return prepared

    def validate_portable_snapshot(self, source: Path) -> TermbasePortableSnapshot:
        """Validate one exact owner-canonical CSV/v1 snapshot without mutation."""

        source = _absolute_resource_path(source)
        payload, source_snapshot = self._read_bound_snapshot(source)
        records = self._records_from_bytes(payload)
        canonical = _serialize_rows(_rows_from_records(records))
        if payload != canonical:
            raise TermbaseValidationError("NON_CANONICAL_SNAPSHOT")
        if source_snapshot.byte_count != len(payload):
            raise TermbaseValidationError("SOURCE_CHANGED")
        return _portable_snapshot_facts(
            payload,
            records,
            source_baseline_digest=hashlib.sha256(payload).hexdigest(),
        )

    def export_portable_snapshot(
        self,
        source: Path,
        destination: Path,
    ) -> TermbasePortableSnapshot:
        """Publish exact canonical snapshot bytes to a new private destination.

        The caller owns final user-destination publication.  Requiring an absent
        destination keeps this owner port from silently replacing unrelated data.
        """

        source = _absolute_resource_path(source)
        destination = _absolute_resource_path(destination)
        if source == destination:
            raise ValueError("portable snapshot destination must differ from source")
        original, source_digest, records, source_snapshot = self._read_snapshot(source)
        payload = _serialize_rows(_rows_from_records(records))
        if hashlib.sha256(original).hexdigest() != source_digest:
            raise AssertionError("termbase snapshot digest changed in memory")
        facts = _portable_snapshot_facts(
            payload,
            records,
            source_baseline_digest=source_digest,
        )
        published_identity: FileObjectIdentity | None = None
        backend = self._platform_backend(source)
        try:
            published_identity = _publish_bytes(
                backend,
                destination,
                payload,
                replace=False,
                private=True,
            )
            if (
                self._snapshot_resource(source) != source_snapshot
                or self._digest_resource(source) != source_digest
            ):
                raise TermbaseValidationError("SOURCE_CHANGED")
            validated = self.validate_portable_snapshot(destination)
            if (
                self._snapshot_resource(destination).identity != published_identity
                or
                validated.payload_digest != facts.payload_digest
                or validated.payload_byte_count != facts.payload_byte_count
                or validated.record_count != facts.record_count
                or validated.legacy_record_count != facts.legacy_record_count
                or validated.v1_record_count != facts.v1_record_count
            ):
                raise TermbaseValidationError("SNAPSHOT_VERIFY_FAILED")
        except BaseException:
            if published_identity is not None:
                _unlink_exact(backend, destination, published_identity)
            raise
        if published_identity is None:
            raise AssertionError("portable snapshot publication lost its identity")
        self._portable_artifacts[(destination, facts.payload_digest)] = published_identity
        return facts

    def discard_portable_snapshot(
        self,
        path: Path,
        snapshot: TermbasePortableSnapshot,
    ) -> None:
        """Delete only a private export artifact issued by this store instance."""

        if type(snapshot) is not TermbasePortableSnapshot:
            raise TypeError("portable termbase snapshot must be exact")
        path = _absolute_resource_path(path)
        identity = self._portable_artifacts.pop(
            (path, snapshot.payload_digest),
            None,
        )
        if identity is None:
            raise ValueError("portable snapshot artifact is not owned by this store")
        _unlink_exact(self._platform_backend(path), path, identity)

    def prepare_snapshot_replace(
        self,
        path: Path,
        source: Path,
    ) -> PreparedTermMutation:
        """Prepare a full snapshot replacement through the existing commit owner."""

        path = _absolute_resource_path(path)
        source = _absolute_resource_path(source)
        source_payload, _source_snapshot = self._read_bound_snapshot(source)
        source_records = self._records_from_bytes(source_payload)
        if source_payload != _serialize_rows(_rows_from_records(source_records)):
            raise TermbaseValidationError("NON_CANONICAL_SNAPSHOT")
        original, base_digest, _current, source_snapshot = self._read_snapshot(path)
        prepared = self._prepare_artifacts(
            action="snapshot_replace",
            path=path,
            original=original,
            base_digest=base_digest,
            source_snapshot=source_snapshot,
            candidate_rows=_rows_from_records(source_records),
        )
        self._prepared_counts[prepared] = (0, 0, 0, len(source_records), 0)
        return prepared

    def list_records(self, path: Path) -> tuple[TermRecord, ...]:
        """Return a fully validated immutable snapshot in file order."""

        payload, _snapshot = self._read_bound_snapshot(_absolute_resource_path(path))
        return self._records_from_bytes(payload)

    def _read_snapshot(
        self,
        path: Path,
    ) -> tuple[bytes, str, tuple[TermRecord, ...], EntrySnapshot]:
        original, snapshot = self._read_bound_snapshot(path)
        file_digest = hashlib.sha256(original).hexdigest()
        return original, file_digest, self._records_from_bytes(original), snapshot

    def _read_bound_snapshot(self, path: Path) -> tuple[bytes, EntrySnapshot]:
        backend = self._platform_backend(path)
        source = bind_rooted_regular(backend, path)
        try:
            snapshot = source.snapshot()
            payload = read_bound_all(
                source,
                snapshot,
                maximum_bytes=_MAX_TERMBASE_BYTES,
            )
            return payload, snapshot
        finally:
            source.close()

    def _digest_resource(self, path: Path) -> str:
        backend = self._platform_backend(path)
        source = bind_rooted_regular(backend, path)
        try:
            snapshot = source.snapshot()
            return digest_bound(source, snapshot).hex()
        finally:
            source.close()

    def _snapshot_resource(self, path: Path) -> EntrySnapshot:
        source = bind_rooted_regular(self._platform_backend(path), path)
        try:
            return source.snapshot()
        finally:
            source.close()

    def _platform_backend(self, path: Path) -> PlatformFileBackend:
        backend = self._backend
        if backend is None:
            from platform_fs import compose_platform_file_backend

            backend = compose_platform_file_backend(path.parent)
            self._backend = backend
        return backend

    def _records_from_bytes(self, original: bytes) -> tuple[TermRecord, ...]:
        file_digest = hashlib.sha256(original).hexdigest()
        try:
            text = original.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TermbaseValidationError("INVALID_UTF8") from exc

        lines = _CapturingLineIterator(io.StringIO(text, newline=""))
        reader = csv.reader(lines, strict=True)
        records: list[TermRecord] = []
        source_ordinals: dict[str, int] = {}
        id_ordinals: dict[str, int] = {}

        while True:
            row_ordinal = len(records)
            lines.start_record()
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                raise TermbaseValidationError(
                    "MALFORMED_CSV",
                    row_ordinal,
                ) from exc

            raw_record = lines.captured_record()
            _validate_strict_csv_record(raw_record, row_ordinal)
            record = self._record_from_row(
                row=row,
                raw_record=raw_record,
                file_digest=file_digest,
                row_ordinal=row_ordinal,
            )

            conflicting_source = source_ordinals.get(record.source)
            if conflicting_source is not None:
                raise TermbaseValidationError(
                    "DUPLICATE_SOURCE",
                    row_ordinal,
                    conflicting_source,
                )
            source_ordinals[record.source] = row_ordinal

            if record.record_id is not None:
                conflicting_id = id_ordinals.get(record.record_id)
                if conflicting_id is not None:
                    raise TermbaseValidationError(
                        "DUPLICATE_ID",
                        row_ordinal,
                        conflicting_id,
                    )
                id_ordinals[record.record_id] = row_ordinal

            records.append(record)

        return tuple(records)

    def _prepare_artifacts(
        self,
        *,
        action: str,
        path: Path,
        original: bytes,
        base_digest: str,
        source_snapshot: EntrySnapshot,
        candidate_rows: list[list[str]],
    ) -> PreparedTermMutation:
        candidate_bytes = _serialize_rows(candidate_rows)
        candidate_records = self._records_from_bytes(candidate_bytes)
        recovery_path: Path | None = None
        staged_path: Path | None = None
        recovery_identity: FileObjectIdentity | None = None
        staged_identity: FileObjectIdentity | None = None
        prepared: PreparedTermMutation | None = None
        backend = self._platform_backend(path)
        try:
            recovery_path = path.with_name(
                f".{path.name}.recovery-{uuid.uuid4().hex}.tmp"
            )
            recovery_identity = _publish_bytes(
                backend,
                recovery_path,
                original,
                replace=False,
                private=True,
            )
            staged_path = path.with_name(
                f".{path.name}.staged-{uuid.uuid4().hex}.tmp"
            )
            staged_identity = _publish_bytes(
                backend,
                staged_path,
                candidate_bytes,
                replace=False,
                private=True,
            )
            prepared = PreparedTermMutation(
                action=action,
                resource_path=path,
                base_digest=base_digest,
                staged_path=staged_path,
                recovery_path=recovery_path,
                candidate_records=candidate_records,
            )
            self._prepared_artifacts[prepared] = (
                staged_identity,
                recovery_identity,
            )
            if self._snapshot_resource(path) != source_snapshot:
                raise TermbaseValidationError("SOURCE_CHANGED")
            self._prepared_sources[prepared] = source_snapshot
            return prepared
        except BaseException:
            if prepared is not None:
                _ = self._prepared_artifacts.pop(prepared, None)
                _ = self._prepared_sources.pop(prepared, None)
            if staged_path is not None and staged_identity is not None:
                try:
                    _unlink_exact(backend, staged_path, staged_identity)
                except PlatformFileError:
                    pass
            if recovery_path is not None and recovery_identity is not None:
                try:
                    _unlink_exact(backend, recovery_path, recovery_identity)
                except PlatformFileError:
                    pass
            raise

    @staticmethod
    def _record_from_row(
        *,
        row: list[str],
        raw_record: str,
        file_digest: str,
        row_ordinal: int,
    ) -> TermRecord:
        if not row:
            raise TermbaseValidationError("EMPTY_ROW", row_ordinal)

        if len(row) == 2:
            record_id = None
            source, target = row
            row_kind = TermRowKind.LEGACY
            policy = TermMatchPolicy.LEGACY
            match_case = None
            whole_word = None
        elif len(row) == 6:
            marker, record_id, source, target, match_case_raw, whole_word_raw = row
            if marker != _V1_MARKER:
                raise TermbaseValidationError("UNKNOWN_MARKER", row_ordinal)
            if not record_id.strip():
                raise TermbaseValidationError("EMPTY_RECORD_ID", row_ordinal)
            match_case = _parse_bool(match_case_raw, row_ordinal)
            whole_word = _parse_bool(whole_word_raw, row_ordinal)
            row_kind = TermRowKind.V1
            policy = TermMatchPolicy.CONFIGURED
        else:
            raise TermbaseValidationError("INVALID_COLUMN_COUNT", row_ordinal)

        if not source.strip():
            raise TermbaseValidationError("EMPTY_SOURCE", row_ordinal)
        if not target.strip():
            raise TermbaseValidationError("EMPTY_TARGET", row_ordinal)

        locator = TermRecordLocator(
            row_kind=row_kind,
            file_digest=file_digest,
            row_ordinal=row_ordinal,
            row_digest=hashlib.sha256(raw_record.encode("utf-8")).hexdigest(),
            record_id=record_id,
        )
        return TermRecord(
            locator=locator,
            record_id=record_id,
            source=source,
            target=target,
            policy=policy,
            match_case=match_case,
            whole_word=whole_word,
        )


def _parse_bool(value: str, row_ordinal: int) -> bool:
    try:
        return _BOOLEAN_VALUES[value]
    except KeyError as exc:
        raise TermbaseValidationError("INVALID_BOOLEAN", row_ordinal) from exc


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _absolute_resource_path(path: Path) -> Path:
    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError("term resource path must be an absolute concrete Path")
    return path


def _validate_prepared_mutation(prepared: object) -> None:
    if not isinstance(prepared, PreparedTermMutation):
        raise TypeError("prepared term mutation must be a PreparedTermMutation")


def _failed_outcome(
    *,
    state: TermCommitState,
    error_code: str,
    retryable: bool,
    recovery_path: Path | None,
    safe_detail: str,
) -> TermCommitOutcome:
    return TermCommitOutcome(
        state=state,
        report=None,
        error_code=error_code,
        retryable=retryable,
        recovery_path=recovery_path,
        quarantined=state is TermCommitState.INDETERMINATE,
        safe_detail=safe_detail,
    )


def _not_committed_outcome(
    prepared: PreparedTermMutation,
    error_code: str,
    safe_detail: str,
) -> TermCommitOutcome:
    return _failed_outcome(
        state=TermCommitState.NOT_COMMITTED,
        error_code=error_code,
        retryable=True,
        recovery_path=prepared.recovery_path,
        safe_detail=safe_detail,
    )


def _indeterminate_outcome(
    prepared: PreparedTermMutation,
    error_code: str,
) -> TermCommitOutcome:
    return _failed_outcome(
        state=TermCommitState.INDETERMINATE,
        error_code=error_code,
        retryable=False,
        recovery_path=prepared.recovery_path,
        safe_detail=(
            "Quarantine the resource and restore it from the recovery file "
            "before retrying."
        ),
    )


def _derive_mutation_counts(
    action: str,
    old_records: tuple[TermRecord, ...],
    committed_records: tuple[TermRecord, ...],
) -> _MutationCounts:
    if action == "create":
        return (1, 0, 0, 0, 0)
    if action == "update":
        return (0, 1, 0, 0, 0)
    if action == "delete":
        return (0, 0, 1, 0, 0)
    if action == "merge_legacy":
        old_by_source = {record.source: record for record in old_records}
        imported = sum(
            record.source not in old_by_source for record in committed_records
        )
        overwritten = sum(
            record.source in old_by_source
            and record.target != old_by_source[record.source].target
            for record in committed_records
        )
        return (0, 0, 0, imported, overwritten)
    if action == "snapshot_replace":
        return (0, 0, 0, len(committed_records), 0)
    raise ValueError("unsupported prepared term mutation action")


def _source_ordinal(
    records: tuple[TermRecord, ...],
    source: str,
) -> int | None:
    for row_ordinal, record in enumerate(records):
        if record.source == source:
            return row_ordinal
    return None


def _locate_current_record(
    records: tuple[TermRecord, ...],
    base_digest: str,
    locator: TermRecordLocator,
) -> tuple[int, TermRecord]:
    if not isinstance(locator, TermRecordLocator):
        raise TypeError("term locator must be a TermRecordLocator")
    if locator.file_digest != base_digest:
        raise TermbaseValidationError("STALE_LOCATOR")
    if locator.row_ordinal >= len(records):
        raise TermbaseValidationError("STALE_LOCATOR")
    current = records[locator.row_ordinal]
    if current.locator != locator:
        raise TermbaseValidationError("STALE_LOCATOR")
    return locator.row_ordinal, current


def _row_from_record(record: TermRecord) -> list[str]:
    if record.locator.row_kind is TermRowKind.LEGACY:
        return [record.source, record.target]
    record_id = record.record_id
    match_case = record.match_case
    whole_word = record.whole_word
    if (
        record_id is None
        or match_case is None
        or whole_word is None
    ):
        raise AssertionError("validated v1 record must have identity and flags")
    return [
        _V1_MARKER,
        record_id,
        record.source,
        record.target,
        _format_bool(match_case),
        _format_bool(whole_word),
    ]


def _row_with_target(record: TermRecord, target: str) -> list[str]:
    row = _row_from_record(record)
    if record.locator.row_kind is TermRowKind.LEGACY:
        row[1] = target
    else:
        row[3] = target
    return row


def _rows_from_records(records: tuple[TermRecord, ...]) -> list[list[str]]:
    return [_row_from_record(record) for record in records]


def _serialize_rows(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _portable_snapshot_facts(
    payload: bytes,
    records: tuple[TermRecord, ...],
    *,
    source_baseline_digest: str,
) -> TermbasePortableSnapshot:
    legacy_count = sum(
        record.locator.row_kind is TermRowKind.LEGACY for record in records
    )
    return TermbasePortableSnapshot(
        payload_digest=hashlib.sha256(payload).hexdigest(),
        source_baseline_digest=source_baseline_digest,
        payload_byte_count=len(payload),
        record_count=len(records),
        legacy_record_count=legacy_count,
        v1_record_count=len(records) - legacy_count,
    )


def _publish_bytes(
    backend: PlatformFileBackend,
    destination: Path,
    payload: bytes,
    *,
    replace: bool,
    private: bool,
    expected_before_digest: str | None = None,
    owner_commit: Callable[[FileObjectIdentity], None] | None = None,
) -> FileObjectIdentity:
    if len(payload) > _MAX_TERMBASE_BYTES:
        raise TermbaseValidationError("TERMBASE_LIMIT_EXCEEDED")
    root = None
    parent = None
    lease = None
    candidate: CandidateFile | None = None
    pending: PendingPublication | None = None
    candidate_identity: FileObjectIdentity | None = None
    digest = hashlib.sha256(payload).digest()
    candidate_name = f".termbase-{uuid.uuid4().hex}.tmp"
    try:
        root = backend.bind_root(destination.parent)
        parent = backend.bind_parent(root, platform_relative_path(destination.name))
        observed = parent.inspect_entry(destination.name)
        if replace and observed is None:
            raise FileNotFoundError(destination)
        if not replace and observed is not None:
            raise FileExistsError(destination)
        if replace:
            lease = backend.acquire(
                parent,
                _termbase_lock_name(destination.name),
                _TERMBASE_LOCK_PREFIX
                + hashlib.sha256(destination.name.encode("utf-8", "strict")).digest(),
                LockPolicy(LockWait.BLOCK),
            )
        if parent.inspect_entry(destination.name) != observed:
            raise PlatformFileError(
                PlatformFileErrorCode.IDENTITY_STALE,
                retryable=True,
            )
        if expected_before_digest is not None:
            if not replace or observed is None:
                raise ValueError("expected prior digest requires replacement")
            before = backend.open_regular(
                root,
                platform_relative_path(destination.name),
            )
            try:
                before_snapshot = before.snapshot()
                if (
                    before_snapshot != observed
                    or digest_bound(before, before_snapshot).hex()
                    != expected_before_digest
                ):
                    raise PlatformFileError(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
            finally:
                before.close()
        candidate = parent.create_candidate(candidate_name, private=private)
        candidate_identity = candidate.identity()
        written = candidate.write_chunks(
            _byte_chunks(payload),
            maximum_bytes=_MAX_TERMBASE_BYTES,
        )
        candidate.flush_content()
        pending = parent.begin_publish(
            candidate,
            destination.name,
            mode=(
                PublishMode.REPLACE_UNDER_LOCK
                if replace
                else PublishMode.CREATE_IF_ABSENT
            ),
            lease=lease if replace else None,
        )
        candidate = None
        candidate_identity = None
        facts = pending.preliminary_facts()
        retained = pending.retained_destination()
        snapshot = retained.snapshot()
        if (
            written.content_sha256 != digest
            or written.byte_count != len(payload)
            or facts.content_sha256 != digest
            or facts.byte_count != len(payload)
            or snapshot.byte_count != len(payload)
            or digest_bound(retained, snapshot) != digest
        ):
            raise PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
        if replace and owner_commit is None:
            raise TypeError("termbase replacement requires owner commit callback")
        if owner_commit is not None:
            owner_commit(facts.destination_identity)
        if pending.terminal_reproof() != facts:
            raise PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
        return facts.destination_identity
    finally:
        _close_authorities(pending, candidate)
        if candidate_identity is not None and parent is not None:
            try:
                parent.unlink_owned(candidate_name, candidate_identity)
            except PlatformFileError:
                pass
        _close_authorities(lease, parent, root)


def _unlink_exact(
    backend: PlatformFileBackend,
    path: Path,
    identity: FileObjectIdentity,
) -> None:
    root = None
    parent = None
    try:
        root = backend.bind_root(path.parent)
        parent = backend.bind_parent(root, platform_relative_path(path.name))
        parent.unlink_owned(path.name, identity)
    finally:
        _close_authorities(parent, root)


def _termbase_lock_name(destination_name: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".termbase-{digest[:32]}.lock"


def _byte_chunks(payload: bytes) -> tuple[bytes, ...]:
    return tuple(
        payload[offset : offset + 64 * 1024]
        for offset in range(0, len(payload), 64 * 1024)
    )


def _close_authorities(*authorities: object) -> None:
    for authority in authorities:
        if authority is not None:
            try:
                authority.close()  # type: ignore[attr-defined]
            except PlatformFileError:
                pass


def _validate_strict_csv_record(raw_record: str, row_ordinal: int) -> None:
    """Reject quote placement that ``csv.reader(strict=True)`` still tolerates."""

    if raw_record.endswith("\r\n"):
        content = raw_record[:-2]
    elif raw_record.endswith(("\r", "\n")):
        content = raw_record[:-1]
    else:
        content = raw_record

    field_start = True
    in_quoted_field = False
    after_quoted_field = False
    index = 0

    while index < len(content):
        character = content[index]
        if in_quoted_field:
            if character != '"':
                index += 1
                continue
            if index + 1 < len(content) and content[index + 1] == '"':
                index += 2
                continue
            in_quoted_field = False
            after_quoted_field = True
            index += 1
            continue

        if after_quoted_field:
            if character != ",":
                raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
            after_quoted_field = False
            field_start = True
            index += 1
            continue

        if field_start:
            if character == '"':
                field_start = False
                in_quoted_field = True
            elif character == ",":
                field_start = True
            elif character in "\r\n":
                raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
            else:
                field_start = False
            index += 1
            continue

        if character == '"' or character in "\r\n":
            raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
        if character == ",":
            field_start = True
        index += 1

    if in_quoted_field:
        raise TermbaseValidationError("MALFORMED_CSV", row_ordinal)
