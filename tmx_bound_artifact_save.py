"""Direct TMX publication over rooted authority, lock, journal, and LKG ports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import secrets
from typing import Callable, Iterable

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    LockLease,
    LockPolicy,
    LockWait,
    OutputArtifactLockRetirement,
    PendingPublication,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishFacts,
    PublishMode,
)
from tmx_context_contracts import (
    TmxContextError,
    TmxDestinationBeforeKind,
    TmxDirectPlan,
    TmxDirectReceipt,
    TmxEffectiveLocales,
    TmxExportPreview,
    TmxLossCount,
    TmxLossDisposition,
    TmxLossReport,
    TmxPayloadProof,
    TmxPreparedPayload,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_platform_io import (
    digest_bound,
    iter_bound_chunks,
    platform_relative_path,
    read_bound_all,
)


_MAX_TMX_BYTES = 100 * 1024 * 1024
_MAX_JOURNAL_BYTES = 1024 * 1024
_LOCK_PREFIX = b"localcat.tmx-direct.lock.v1\0"
_JOURNAL_SCHEMA = "localcat-tmx-direct-journal-v1"


class _JournalPhase(str, Enum):
    ARMED = "armed"
    STAGED = "staged"
    RECEIPT_READY = "receipt_ready"


@dataclass(frozen=True, slots=True)
class _DestinationFact:
    backend: PlatformFileBackend
    parent: BoundDirectoryAuthority
    prior: BoundRegularFile | None
    before: EntrySnapshot | None
    before_digest: str | None
    name: str

    def close(self) -> None:
        try:
            if self.prior is not None:
                self.prior.close()
        finally:
            self.parent.close()


@dataclass(frozen=True, slots=True)
class _RecoveryRecord:
    phase: _JournalPhase
    operation_id: str
    destination_name: str
    before_kind: TmxDestinationBeforeKind
    before_digest: str | None
    after_digest: str
    stage_name: str
    lkg_name: str | None
    scope_kind: TmxScopeKind
    scope_id: str
    profile_id: str
    source_locale: str
    target_locale: str
    parser_content_digest: str
    included_count: int
    excluded_count: int
    warning_count: int
    prop_count: int
    loss_counts: tuple[TmxLossCount, ...]

    def proof(self) -> TmxPayloadProof:
        return TmxPayloadProof(
            profile_id=self.profile_id,
            effective_locales=TmxEffectiveLocales(self.source_locale, self.target_locale),
            payload_digest=self.after_digest,
            parser_content_digest=self.parser_content_digest,
            included_count=self.included_count,
            prop_count=self.prop_count,
            loss_report=TmxLossReport(
                included_count=self.included_count,
                excluded_count=self.excluded_count,
                warning_count=self.warning_count,
                blocking_count=0,
                counts=self.loss_counts,
                issues=(),
            ),
        )

    def receipt(self, destination: Path) -> TmxDirectReceipt:
        return TmxDirectReceipt(
            operation_id=self.operation_id,
            scope_kind=self.scope_kind,
            scope_id=self.scope_id,
            profile_id=self.profile_id,
            effective_locales=TmxEffectiveLocales(self.source_locale, self.target_locale),
            destination=destination,
            destination_before=self.before_kind,
            before_digest=self.before_digest,
            after_digest=self.after_digest,
            included_count=self.included_count,
            excluded_count=self.excluded_count,
            warning_count=self.warning_count,
            loss_counts=self.loss_counts,
            durable=True,
        )


class TmxDirectArtifactSaver:
    """Issue private plans and publish under one TMX-owned recovery state machine."""

    __slots__ = (
        "_backend",
        "_cold_validator",
        "_fault_hook",
        "_issued",
        "_scope_revalidator",
    )

    def __init__(
        self,
        cold_validator: Callable[[Path, TmxPayloadProof], None],
        scope_revalidator: Callable[[TmxScopeBinding], None],
        *,
        backend: PlatformFileBackend | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not callable(cold_validator) or not callable(scope_revalidator):
            raise TypeError("TMX validators must be callable")
        if backend is not None and not isinstance(backend, PlatformFileBackend):
            raise TypeError("TMX backend must satisfy PlatformFileBackend")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("TMX fault hook must be callable")
        self._cold_validator = cold_validator
        self._scope_revalidator = scope_revalidator
        self._backend = backend
        self._fault_hook = fault_hook
        self._issued: dict[str, TmxDirectPlan] = {}

    def preview(
        self,
        binding: TmxScopeBinding,
        payload: TmxPreparedPayload,
        destination: Path,
    ) -> tuple[TmxExportPreview, TmxDirectPlan]:
        if type(binding) is not TmxScopeBinding:
            raise TypeError("binding must be exact TmxScopeBinding")
        if type(payload) is not TmxPreparedPayload:
            raise TypeError("payload must be exact TmxPreparedPayload")
        if (
            payload.scope_kind is not binding.scope_kind
            or payload.scope_id != binding.scope_id
            or payload.binding_digest != binding.binding_digest
        ):
            raise TmxContextError(
                "TMX.SCOPE_PAYLOAD_MISMATCH",
                "TMX payload was prepared for a different scope",
            )
        _validate_destination(destination)
        backend = self._backend
        if backend is None:
            from platform_fs import compose_platform_file_backend

            try:
                backend = compose_platform_file_backend(destination.parent)
            except PlatformFileError as error:
                raise _map_platform_error(error, published=False) from None

        parent = None
        prior = None
        try:
            parent = backend.bind_root(destination.parent)
            if parent.inspect_entry(_journal_name(destination.name)) is not None:
                raise _recovery_required()
            before = parent.inspect_entry(destination.name)
            _require_safe_snapshot(before)
            before_digest = None
            if before is not None:
                prior = backend.open_regular(parent, platform_relative_path(destination.name))
                if prior.snapshot() != before:
                    raise TmxContextError(
                        "TMX.DESTINATION_STALE",
                        "destination changed during preview",
                    )
                before_digest = digest_bound(prior, before).hex()
            before_kind = (
                TmxDestinationBeforeKind.ABSENT
                if before is None
                else TmxDestinationBeforeKind.REGULAR
            )
            operation_id = secrets.token_hex(16)
            loss = payload.proof.loss_report
            preview = TmxExportPreview(
                operation_id=operation_id,
                scope_kind=binding.scope_kind,
                scope_id=binding.scope_id,
                project_id=binding.project_id,
                chunk_plan_id=binding.chunk_plan_id,
                chunk_plan_revision=binding.chunk_plan_revision,
                chunk_id=binding.chunk_id,
                document_count=binding.document_count,
                attached_count=binding.attached_count,
                included_count=loss.included_count,
                excluded_count=loss.excluded_count,
                warning_count=loss.warning_count,
                loss_counts=loss.counts,
                safe_issues=loss.issues,
                effective_locales=payload.proof.effective_locales,
                profile_id=payload.proof.profile_id,
                destination=destination,
                destination_before=before_kind,
                destination_before_digest=before_digest,
            )
            fact = _DestinationFact(
                backend=backend,
                parent=parent,
                prior=prior,
                before=before,
                before_digest=before_digest,
                name=destination.name,
            )
            parent = None
            prior = None
            plan = TmxDirectPlan(
                operation_id=operation_id,
                payload=payload,
                binding=binding,
                preview=preview,
                destination_fact=fact,
            )
            self._issued[operation_id] = plan
            return preview, plan
        except PlatformFileError as error:
            raise _map_platform_error(error, published=False) from None
        finally:
            _close_authorities(prior, parent)

    def cancel(self, plan: TmxDirectPlan) -> None:
        self._claim(plan)
        _payload, _binding, _preview, raw_fact = plan._consume()
        if type(raw_fact) is _DestinationFact:
            raw_fact.close()

    def apply(self, plan: TmxDirectPlan) -> TmxDirectReceipt:
        self._claim(plan)
        payload, binding, preview, raw_fact = plan._consume()
        if type(raw_fact) is not _DestinationFact:
            raise TmxContextError("TMX.PLAN_INVALID", "direct export plan has invalid facts")
        fact = raw_fact
        lease: LockLease | None = None
        journal: PendingPublication | None = None
        stage: PendingPublication | None = None
        lkg: PendingPublication | None = None
        published: PendingPublication | None = None
        target_named = False
        operation_uncertain = False
        recovery: _RecoveryRecord | None = None
        try:
            self._revalidate_scope(binding)
            parent = fact.parent
            backend = fact.backend
            lease = backend.acquire(
                parent,
                _lock_name(fact.name),
                _lock_payload(fact.name),
                LockPolicy(LockWait.BLOCK),
            )
            parent.reprove()
            if parent.inspect_entry(_journal_name(fact.name)) is not None:
                raise _recovery_required()
            if parent.inspect_entry(fact.name) != fact.before:
                raise TmxContextError(
                    "TMX.DESTINATION_STALE",
                    "destination changed after preview",
                )
            if fact.prior is not None:
                if fact.prior.snapshot() != fact.before:
                    raise TmxContextError("TMX.DESTINATION_STALE", "destination changed")
                assert fact.before is not None
                if digest_bound(fact.prior, fact.before).hex() != fact.before_digest:
                    raise TmxContextError("TMX.DESTINATION_STALE", "destination changed")

            recovery = _recovery_record(preview, payload)
            try:
                journal = _publish_bytes(
                    parent,
                    _journal_name(fact.name),
                    _recovery_to_bytes(recovery),
                    maximum_bytes=_MAX_JOURNAL_BYTES,
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            except BaseException as error:
                operation_uncertain = _is_recovery_required(error)
                raise
            self._fault("journal_armed")

            try:
                stage = _publish_bytes(
                    parent,
                    recovery.stage_name,
                    payload.data,
                    maximum_bytes=_MAX_TMX_BYTES,
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            except BaseException as error:
                operation_uncertain = _is_recovery_required(error)
                raise
            _require_pending_readback(stage, payload.proof.payload_digest)
            self._cold_validator(preview.destination.parent / recovery.stage_name, payload.proof)
            _require_pending_readback(stage, payload.proof.payload_digest)
            self._fault("stage_validated")

            if fact.before is not None:
                assert fact.prior is not None and recovery.lkg_name is not None
                try:
                    lkg = _publish_chunks(
                        parent,
                        recovery.lkg_name,
                        iter_bound_chunks(fact.prior, fact.before),
                        maximum_bytes=fact.before.byte_count,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                    lkg_facts = _require_pending_readback(lkg, fact.before_digest)
                    _require_pending_terminal(lkg, lkg_facts)
                except BaseException as error:
                    operation_uncertain = _is_recovery_required(error)
                    raise
            self._fault("lkg_ready")

            recovery = replace(recovery, phase=_JournalPhase.STAGED)
            try:
                journal = _replace_journal(parent, lease, fact.name, journal, recovery)
            except BaseException as error:
                operation_uncertain = _is_recovery_required(error)
                raise

            staged = stage.retained_destination()
            staged_snapshot = staged.snapshot()
            output_name = _candidate_name("target")
            output = _candidate_from_chunks(
                parent,
                output_name,
                iter_bound_chunks(staged, staged_snapshot),
                maximum_bytes=_MAX_TMX_BYTES,
            )
            if fact.prior is not None:
                # Windows replacement requires releasing the retained preview
                # handle's no-delete-share profile.  Reprove the destination name
                # immediately afterwards, before the candidate is published.
                fact.prior.close()
                self._fault("prior_released")
                if parent.inspect_entry(fact.name) != fact.before:
                    try:
                        _discard_candidate(parent, output_name, output)
                    except BaseException as error:
                        operation_uncertain = _is_recovery_required(error)
                        raise
                    raise TmxContextError(
                        "TMX.DESTINATION_STALE",
                        "destination changed after retained preview release",
                    )
            self._fault("before_target_publish")
            if parent.inspect_entry(fact.name) != fact.before:
                try:
                    _discard_candidate(parent, output_name, output)
                except BaseException as error:
                    operation_uncertain = _is_recovery_required(error)
                    raise
                raise TmxContextError(
                    "TMX.DESTINATION_STALE",
                    "destination changed immediately before publication",
                )
            try:
                published = parent.begin_publish(
                    output,
                    fact.name,
                    mode=(
                        PublishMode.CREATE_IF_ABSENT
                        if fact.before is None
                        else PublishMode.REPLACE_UNDER_LOCK
                    ),
                    lease=lease if fact.before is not None else None,
                )
            except BaseException as error:
                if _is_recovery_required(error):
                    operation_uncertain = True
                    target_named = True
                raise
            target_named = True
            published_facts = _require_pending_readback(
                published,
                payload.proof.payload_digest,
            )
            self._fault("after_target_publish")
            self._cold_validator(preview.destination, payload.proof)
            _require_pending_readback(published, payload.proof.payload_digest)

            recovery = replace(recovery, phase=_JournalPhase.RECEIPT_READY)
            try:
                journal = _replace_journal(parent, lease, fact.name, journal, recovery)
            except BaseException as error:
                operation_uncertain = _is_recovery_required(error)
                raise
            self._fault("owner_commit")
            _require_pending_terminal(published, published_facts)
            self._fault("terminal_reproof")

            _close_and_unlink(parent, recovery.lkg_name, lkg)
            lkg = None
            _close_and_unlink(parent, recovery.stage_name, stage)
            stage = None
            _close_and_unlink(parent, _journal_name(fact.name), journal)
            journal = None
            published.close()
            published = None
            receipt = recovery.receipt(preview.destination)
            _finish_output_lock(backend, parent, lease)
            if lease.closed:
                lease = None
            return receipt
        except BaseException as primary:
            if operation_uncertain:
                if isinstance(primary, TmxContextError) and primary.code == "TMX.RECOVERY_REQUIRED":
                    raise
                raise _recovery_required() from primary
            if target_named:
                if isinstance(primary, TmxContextError) and primary.code == "TMX.RECOVERY_REQUIRED":
                    raise
                if isinstance(primary, PlatformFileError):
                    raise _recovery_required() from None
                raise _recovery_required() from primary
            if recovery is not None:
                try:
                    _close_and_unlink(fact.parent, recovery.lkg_name, lkg)
                    lkg = None
                    _close_and_unlink(fact.parent, recovery.stage_name, stage)
                    stage = None
                    _close_and_unlink(
                        fact.parent,
                        _journal_name(fact.name),
                        journal,
                    )
                    journal = None
                except BaseException as cleanup_error:
                    raise _recovery_required() from cleanup_error
            if isinstance(primary, TmxContextError):
                raise
            if isinstance(primary, PlatformFileError):
                raise _map_platform_error(primary, published=False) from None
            raise TmxContextError(
                "TMX.PUBLICATION_FAILED",
                "direct TMX destination was not published",
            ) from primary
        finally:
            _close_authorities(published, lkg, stage, journal, lease)
            fact.close()

    def recover(self, destination: Path) -> TmxDirectReceipt | None:
        """Cold-reopen one destination family and settle only exact journal facts."""

        _validate_destination(destination)
        backend = self._backend
        if backend is None:
            from platform_fs import compose_platform_file_backend

            backend = compose_platform_file_backend(destination.parent)
        parent = lease = journal_source = None
        try:
            parent = backend.bind_root(destination.parent)
            lease = backend.acquire(
                parent,
                _lock_name(destination.name),
                _lock_payload(destination.name),
                LockPolicy(LockWait.BLOCK),
            )
            journal_observed = parent.inspect_entry(_journal_name(destination.name))
            if journal_observed is None:
                _finish_output_lock(backend, parent, lease)
                if lease.closed:
                    lease = None
                return None
            journal_source = backend.open_regular(
                parent,
                platform_relative_path(_journal_name(destination.name)),
            )
            if journal_source.snapshot() != journal_observed:
                raise _recovery_required()
            recovery = _recovery_from_bytes(
                read_bound_all(
                    journal_source,
                    journal_observed,
                    maximum_bytes=_MAX_JOURNAL_BYTES,
                )
            )
            if recovery.destination_name != destination.name:
                raise _recovery_required()

            current = parent.inspect_entry(destination.name)
            _require_safe_snapshot(current)
            current_digest = _entry_digest(backend, parent, destination.name, current)
            if current_digest == recovery.after_digest:
                self._cold_validator(destination, recovery.proof())
                if parent.inspect_entry(destination.name) != current:
                    raise _recovery_required()
                receipt = recovery.receipt(destination)
            elif (
                recovery.phase is not _JournalPhase.RECEIPT_READY
                and (
                    (recovery.before_kind is TmxDestinationBeforeKind.ABSENT and current is None)
                    or (
                        recovery.before_kind is TmxDestinationBeforeKind.REGULAR
                        and current_digest == recovery.before_digest
                    )
                )
            ):
                receipt = None
            else:
                raise _recovery_required()

            _verify_optional_sidecar(
                backend, parent, recovery.stage_name, recovery.after_digest
            )
            _verify_optional_sidecar(
                backend, parent, recovery.lkg_name, recovery.before_digest
            )
            journal_source.close()
            journal_source = None
            _unlink_sidecar(parent, recovery.lkg_name)
            _unlink_sidecar(parent, recovery.stage_name)
            parent.unlink_owned(_journal_name(destination.name), journal_observed.identity)
            if parent.inspect_entry(_journal_name(destination.name)) is not None:
                raise _recovery_required()
            _finish_output_lock(backend, parent, lease)
            if lease.closed:
                lease = None
            return receipt
        except TmxContextError:
            raise
        except PlatformFileError as error:
            raise _map_platform_error(error, published=True) from None
        except Exception as error:
            raise _recovery_required() from error
        finally:
            _close_authorities(journal_source, lease, parent)

    def _claim(self, plan: TmxDirectPlan) -> None:
        if type(plan) is not TmxDirectPlan:
            raise TypeError("plan must be exact TmxDirectPlan")
        if self._issued.pop(plan._operation_id, None) is not plan:
            raise TmxContextError("TMX.PLAN_INVALID", "direct export plan is not active")

    def _revalidate_scope(self, binding: TmxScopeBinding) -> None:
        try:
            revalidated = self._scope_revalidator(binding)
            if revalidated is False or (
                type(revalidated) is TmxScopeBinding and revalidated != binding
            ):
                raise TmxContextError("TMX.SCOPE_STALE", "source scope no longer matches preview")
            if revalidated not in (None, True) and type(revalidated) is not TmxScopeBinding:
                raise TypeError("scope revalidator returned an unsupported value")
        except TmxContextError:
            raise
        except Exception as error:
            raise TmxContextError(
                "TMX.SCOPE_REVALIDATION_FAILED",
                "source scope no longer matches preview",
            ) from error

    def _fault(self, phase: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)


def _validate_destination(destination: Path) -> None:
    if type(destination) is not type(Path()) or not destination.is_absolute():
        raise TypeError("destination must be an absolute concrete Path")
    if destination.suffix.casefold() != ".tmx" or destination.name in ("", ".", ".."):
        raise TmxContextError(
            "TMX.DESTINATION_EXTENSION",
            "direct TMX destination must end in .tmx",
        )


def _require_safe_snapshot(snapshot: EntrySnapshot | None) -> None:
    if snapshot is not None and (
        snapshot.identity.kind != "regular"
        or snapshot.identity.link_count != 1
        or not snapshot.reparse_free
    ):
        raise TmxContextError(
            "TMX.DESTINATION_UNSAFE",
            "destination must be absent or a single-link regular file",
        )


def _candidate_from_chunks(
    parent: BoundDirectoryAuthority,
    name: str,
    chunks: Iterable[bytes],
    *,
    maximum_bytes: int,
) -> CandidateFile:
    candidate = None
    identity = None
    try:
        candidate = parent.create_candidate(name, private=False)
        identity = candidate.identity()
        candidate.write_chunks(chunks, maximum_bytes=maximum_bytes)
        candidate.flush_content()
        return candidate
    except BaseException:
        if candidate is not None:
            candidate.close()
        if identity is not None:
            try:
                parent.unlink_owned(name, identity)
            except PlatformFileError:
                pass
        raise


def _discard_candidate(
    parent: BoundDirectoryAuthority,
    name: str,
    candidate: CandidateFile,
) -> None:
    identity = candidate.identity()
    candidate.close()
    parent.unlink_owned(name, identity)
    if parent.inspect_entry(name) is not None:
        raise _recovery_required()


def _payload_chunks(payload: bytes) -> Iterable[bytes]:
    return (
        payload[offset : offset + 64 * 1024]
        for offset in range(0, len(payload), 64 * 1024)
    )


def _publish_chunks(
    parent: BoundDirectoryAuthority,
    destination: str,
    chunks: Iterable[bytes],
    *,
    maximum_bytes: int,
    mode: PublishMode,
    lease: LockLease | None,
) -> PendingPublication:
    candidate = _candidate_from_chunks(
        parent,
        _candidate_name(destination),
        chunks,
        maximum_bytes=maximum_bytes,
    )
    return parent.begin_publish(candidate, destination, mode=mode, lease=lease)


def _publish_bytes(
    parent: BoundDirectoryAuthority,
    destination: str,
    payload: bytes,
    *,
    maximum_bytes: int,
    mode: PublishMode,
    lease: LockLease | None,
) -> PendingPublication:
    pending = _publish_chunks(
        parent,
        destination,
        _payload_chunks(payload),
        maximum_bytes=maximum_bytes,
        mode=mode,
        lease=lease,
    )
    try:
        facts = _require_pending_readback(
            pending,
            hashlib.sha256(payload).hexdigest(),
        )
        _require_pending_terminal(pending, facts)
    except BaseException:
        pending.close()
        raise
    return pending


def _replace_journal(
    parent: BoundDirectoryAuthority,
    lease: LockLease,
    destination_name: str,
    current: PendingPublication,
    recovery: _RecoveryRecord,
) -> PendingPublication:
    current.close()
    return _publish_bytes(
        parent,
        _journal_name(destination_name),
        _recovery_to_bytes(recovery),
        maximum_bytes=_MAX_JOURNAL_BYTES,
        mode=PublishMode.REPLACE_UNDER_LOCK,
        lease=lease,
    )


def _require_pending_readback(
    pending: PendingPublication,
    expected_digest: str | None,
) -> PublishFacts:
    if expected_digest is None:
        raise _recovery_required()
    facts = pending.preliminary_facts()
    retained = pending.retained_destination()
    snapshot = retained.snapshot()
    if (
        facts.content_sha256.hex() != expected_digest
        or facts.byte_count != snapshot.byte_count
        or digest_bound(retained, snapshot) != facts.content_sha256
    ):
        raise _recovery_required()
    return facts


def _require_pending_terminal(
    pending: PendingPublication,
    expected_facts: PublishFacts,
) -> None:
    if pending.terminal_reproof() != expected_facts:
        raise _recovery_required()


def _entry_digest(
    backend: PlatformFileBackend,
    parent: BoundDirectoryAuthority,
    name: str,
    observed: EntrySnapshot | None,
) -> str | None:
    if observed is None:
        return None
    source = backend.open_regular(parent, platform_relative_path(name))
    try:
        if source.snapshot() != observed:
            raise _recovery_required()
        return digest_bound(source, observed).hex()
    finally:
        source.close()


def _verify_optional_sidecar(
    backend: PlatformFileBackend,
    parent: BoundDirectoryAuthority,
    name: str | None,
    expected_digest: str | None,
) -> None:
    if name is None:
        return
    observed = parent.inspect_entry(name)
    if observed is None:
        return
    _require_safe_snapshot(observed)
    if expected_digest is None or _entry_digest(backend, parent, name, observed) != expected_digest:
        raise _recovery_required()


def _unlink_sidecar(parent: BoundDirectoryAuthority, name: str | None) -> None:
    if name is None:
        return
    observed = parent.inspect_entry(name)
    if observed is None:
        return
    _require_safe_snapshot(observed)
    parent.unlink_owned(name, observed.identity)
    if parent.inspect_entry(name) is not None:
        raise _recovery_required()


def _close_and_unlink(
    parent: BoundDirectoryAuthority,
    name: str | None,
    pending: PendingPublication | None,
) -> None:
    if name is None or pending is None:
        return
    identity = pending.preliminary_facts().destination_identity
    pending.close()
    parent.unlink_owned(name, identity)
    if parent.inspect_entry(name) is not None:
        raise _recovery_required()


def _recovery_record(
    preview: TmxExportPreview,
    payload: TmxPreparedPayload,
) -> _RecoveryRecord:
    proof = payload.proof
    operation = preview.operation_id
    return _RecoveryRecord(
        phase=_JournalPhase.ARMED,
        operation_id=operation,
        destination_name=preview.destination.name,
        before_kind=preview.destination_before,
        before_digest=preview.destination_before_digest,
        after_digest=proof.payload_digest,
        stage_name=f".localcat-tmx-{operation}.stage.tmx",
        lkg_name=(
            None
            if preview.destination_before is TmxDestinationBeforeKind.ABSENT
            else f".localcat-tmx-{operation}.lkg.tmx"
        ),
        scope_kind=preview.scope_kind,
        scope_id=preview.scope_id,
        profile_id=preview.profile_id,
        source_locale=proof.effective_locales.source_locale,
        target_locale=proof.effective_locales.target_locale,
        parser_content_digest=proof.parser_content_digest,
        included_count=proof.included_count,
        excluded_count=proof.loss_report.excluded_count,
        warning_count=proof.loss_report.warning_count,
        prop_count=proof.prop_count,
        loss_counts=proof.loss_report.counts,
    )


def _recovery_to_bytes(record: _RecoveryRecord) -> bytes:
    payload = {
        "schema": _JOURNAL_SCHEMA,
        "phase": record.phase.value,
        "operation_id": record.operation_id,
        "destination_name": record.destination_name,
        "before_kind": record.before_kind.value,
        "before_digest": record.before_digest,
        "after_digest": record.after_digest,
        "stage_name": record.stage_name,
        "lkg_name": record.lkg_name,
        "scope_kind": record.scope_kind.value,
        "scope_id": record.scope_id,
        "profile_id": record.profile_id,
        "source_locale": record.source_locale,
        "target_locale": record.target_locale,
        "parser_content_digest": record.parser_content_digest,
        "included_count": record.included_count,
        "excluded_count": record.excluded_count,
        "warning_count": record.warning_count,
        "prop_count": record.prop_count,
        "loss_counts": [
            {
                "code": item.code,
                "disposition": item.disposition.value,
                "count": item.count,
            }
            for item in record.loss_counts
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _recovery_from_bytes(payload: bytes) -> _RecoveryRecord:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate journal key")
            result[key] = value
        return result

    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
        if type(raw) is not dict or raw.get("schema") != _JOURNAL_SCHEMA:
            raise ValueError
        loss_raw = raw["loss_counts"]
        if type(loss_raw) is not list:
            raise TypeError
        loss_counts = tuple(
            TmxLossCount(
                code=item["code"],
                disposition=TmxLossDisposition(item["disposition"]),
                count=item["count"],
            )
            for item in loss_raw
            if type(item) is dict and set(item) == {"code", "disposition", "count"}
        )
        if len(loss_counts) != len(loss_raw):
            raise ValueError
        record = _RecoveryRecord(
            phase=_JournalPhase(raw["phase"]),
            operation_id=raw["operation_id"],
            destination_name=raw["destination_name"],
            before_kind=TmxDestinationBeforeKind(raw["before_kind"]),
            before_digest=raw["before_digest"],
            after_digest=raw["after_digest"],
            stage_name=raw["stage_name"],
            lkg_name=raw["lkg_name"],
            scope_kind=TmxScopeKind(raw["scope_kind"]),
            scope_id=raw["scope_id"],
            profile_id=raw["profile_id"],
            source_locale=raw["source_locale"],
            target_locale=raw["target_locale"],
            parser_content_digest=raw["parser_content_digest"],
            included_count=raw["included_count"],
            excluded_count=raw["excluded_count"],
            warning_count=raw["warning_count"],
            prop_count=raw["prop_count"],
            loss_counts=loss_counts,
        )
        record.proof()
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise _recovery_required() from error
    if _recovery_to_bytes(record) != payload:
        raise _recovery_required()
    return record


def _lock_name(destination_name: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".localcat-tmx-{digest[:32]}.lock"


def _lock_payload(destination_name: str) -> bytes:
    return _LOCK_PREFIX + hashlib.sha256(destination_name.encode("utf-8", "strict")).digest()


def _journal_name(destination_name: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".localcat-tmx-{digest[:32]}.journal"


def _candidate_name(role: str) -> str:
    digest = hashlib.sha256(role.encode("utf-8", "strict")).hexdigest()
    return f".localcat-tmx-{digest[:16]}-{secrets.token_hex(12)}.tmp"


def _recovery_required() -> TmxContextError:
    return TmxContextError(
        "TMX.RECOVERY_REQUIRED",
        "direct TMX publication requires cold recovery",
    )


def _is_recovery_required(error: BaseException) -> bool:
    return (
        isinstance(error, TmxContextError)
        and error.code == "TMX.RECOVERY_REQUIRED"
    ) or (
        isinstance(error, PlatformFileError)
        and error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value
    )


def _map_platform_error(error: PlatformFileError, *, published: bool) -> TmxContextError:
    if published or error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
        return _recovery_required()
    if error.code == PlatformFileErrorCode.LOCK_CONTENDED.value:
        return TmxContextError(
            "TMX.DESTINATION_LOCKED",
            "another direct TMX publication owns the destination family",
        )
    if error.code in {
        PlatformFileErrorCode.IDENTITY_STALE.value,
        PlatformFileErrorCode.REPARSE_REJECTED.value,
        PlatformFileErrorCode.OUTSIDE_ROOT.value,
        PlatformFileErrorCode.ENTRY_UNAVAILABLE.value,
    }:
        return TmxContextError(
            "TMX.DESTINATION_STALE",
            "destination authority changed during direct publication",
        )
    return TmxContextError(
        "TMX.PUBLICATION_FAILED",
        "direct TMX destination was not published",
    )


def _close_authorities(*authorities: object) -> None:
    for authority in authorities:
        if authority is not None:
            try:
                authority.close()  # type: ignore[attr-defined]
            except PlatformFileError:
                pass


def _finish_output_lock(
    backend: PlatformFileBackend,
    parent: BoundDirectoryAuthority,
    lease: LockLease,
) -> None:
    if not isinstance(backend, OutputArtifactLockRetirement):
        return
    try:
        backend.finish_output_lock(parent, lease)
    except Exception:
        pass


__all__ = ["TmxDirectArtifactSaver"]
