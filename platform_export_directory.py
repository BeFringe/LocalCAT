"""Platform-owned preparation and materialization of nested export targets.

Plans preserve the preview's existing/absent conditions. A batch may reuse only
its own created directory authorities; it never adopts a concurrent creator.
The primitives belong to the platform adapters, not to retirement or Parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol
import unicodedata

from platform_fs_contracts import (
    BoundDirectoryAuthority, BoundRegularFile, EntrySnapshot, OpaqueAuthority,
    PlatformFileError, PlatformFileErrorCode, RootedDirectoryAuthority,
    validate_relative_name,
)

_TARGET_ISSUER = object()


def _stale() -> PlatformFileError:
    return PlatformFileError(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)


def _checkpoint(cancellation) -> None:
    if cancellation is not None:
        cancellation.raise_if_cancelled()


class ExportDirectoryCreationError(PlatformFileError):
    """Creation syscall succeeded, but retaining/proving its result failed.

    This records a mutation fact, not proof that the directory still exists.
    The batch reports the attempted path without claiming rollback or ownership.
    """

    def __init__(self):
        super().__init__(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)


def _parts(value: str) -> tuple[str, ...]:
    if type(value) is not str:
        raise TypeError('portable_ref must be exact str')
    parts = tuple(value.split('/'))
    if len(parts) > 33 or len(value.encode('utf-8')) > 4096:
        raise ValueError('export path exceeds platform bounds')
    for part in parts:
        validate_relative_name(part)
        stem = part.split('.')[0].upper()
        if (unicodedata.normalize('NFC', part) != part or part[-1:] in {' ', '.'}
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or stem in {'CON', 'PRN', 'AUX', 'NUL'}
                or stem in {f'{p}{n}' for p in ('COM', 'LPT') for n in range(1, 10)}):
            raise ValueError('export path is not portable')
    return parts


class ExportDirectoryBackend(Protocol):
    """Ordinary rooted directory primitives; creation must be exclusive.

    Implementations return independently owned handles and prove both lineage
    and child identity before returning. create=True must reject already-exists.
    """

    def duplicate_export_directory(self, parent: BoundDirectoryAuthority) -> BoundDirectoryAuthority: ...

    def open_export_directory(
        self, parent: BoundDirectoryAuthority, name: str, *, create: bool = False,
    ) -> BoundDirectoryAuthority: ...

    def open_regular(self, root: RootedDirectoryAuthority, relative: PurePath) -> BoundRegularFile: ...


@dataclass(frozen=True, slots=True, eq=False)
class ExportDirectoryPlan:
    """Read-only facts; only the exact issuing batch can materialize this plan."""

    relative_path: str
    missing_directories: tuple[str, ...]
    expected: EntrySnapshot | None


@dataclass(slots=True)
class _PlanState:
    plan: ExportDirectoryPlan
    parent: BoundDirectoryAuthority
    existing: BoundRegularFile | None
    prefix: tuple[str, ...]
    consumed: bool = False


class MaterializedExportTarget(OpaqueAuthority):
    """One-use transfer of ordinary rooted publication authority to its owner."""

    def __init__(self, issuer, backend, parent, relative_path, expected, existing):
        if issuer is not _TARGET_ISSUER:
            raise TypeError('materialized targets are platform-issued')
        super().__init__()
        self._backend, self._parent = backend, parent
        self._relative_path, self._expected = relative_path, expected
        self._existing = existing

    @property
    def relative_path(self) -> str:
        return self._relative_path

    @property
    def expected(self) -> EntrySnapshot | None:
        return self._expected

    def reprove(self) -> None:
        self._require_open()
        self._parent.reprove()
        if self._parent.inspect_entry(self.relative_path.rsplit('/', 1)[-1]) != self.expected:
            raise _stale()
        if self._existing is not None and self._existing.snapshot() != self.expected:
            raise _stale()

    def take_binding(self):
        """Transfer ownership exactly once without rebinding the pathname."""
        self.reprove()
        result = self._backend, self._parent, self.relative_path, self.expected, self._existing
        self._mark_authority_transferred()
        self._parent = self._existing = None
        return result

    def _close_authority(self) -> None:
        _close_all((self._existing, self._parent))
        self._existing = self._parent = None


def _close_all(authorities) -> None:
    failure = None
    for authority in authorities:
        if authority is not None:
            try:
                authority.close()
            except Exception as error:
                failure = failure or error
    if failure is not None:
        raise failure


class ExportDirectoryBatch(OpaqueAuthority):
    """Owns plans and shared ancestor handles, borrowing one live export root.

    All plans must be prepared before the first materialization. The caller
    keeps the root alive through batch completion and closes it separately.
    Operations are serialized by the Application worker; cancellation is polled
    at component boundaries, never used to erase already-completed mutations.
    """

    def __init__(self, backend: ExportDirectoryBackend, root: RootedDirectoryAuthority):
        if not isinstance(root, RootedDirectoryAuthority):
            raise TypeError('export root must be a rooted directory authority')
        root.reprove()
        super().__init__()
        self._backend, self._root = backend, root
        self._states: dict[ExportDirectoryPlan, _PlanState] = {}
        self._created: dict[tuple[str, ...], BoundDirectoryAuthority] = {}
        self._created_paths: list[str] = []
        self._started = False

    @property
    def created_directories(self) -> tuple[str, ...]:
        return tuple(self._created_paths)

    def prepare_descendant_target(self, portable_ref: str) -> ExportDirectoryPlan:
        self._require_open()
        if self._started:
            raise _stale()
        parts = _parts(portable_ref)
        if len(self._states) >= 256:
            raise ValueError('export batch exceeds 256 targets')
        folded = tuple(p.casefold() for p in parts)
        for plan in self._states:
            other_parts = tuple(plan.relative_path.split('/'))
            other = tuple(p.casefold() for p in other_parts)
            if folded[:len(other)] == other or other[:len(folded)] == folded:
                raise ValueError('export paths collide or cross a file ancestor')
            for left, right in zip(parts[:-1], other_parts[:-1]):
                if left.casefold() != right.casefold():
                    break
                if left != right:
                    raise ValueError('export directory spellings collide')
        parent = existing = None
        try:
            parent = self._backend.duplicate_export_directory(self._root)
            prefix = ()
            for part in parts[:-1]:
                observed = parent.inspect_entry(part)
                if observed is None:
                    break
                if observed.identity.kind != 'directory' or not observed.reparse_free:
                    raise _stale()
                child = self._backend.open_export_directory(parent, part)
                try:
                    if parent.inspect_entry(part) != observed:
                        raise _stale()
                except BaseException:
                    child.close()
                    raise
                previous, parent = parent, child
                previous.close()
                prefix += (part,)
            missing = parts[len(prefix):-1]
            expected = None if missing else parent.inspect_entry(parts[-1])
            if expected is not None:
                if (expected.identity.kind != 'regular' or expected.identity.link_count != 1
                        or not expected.reparse_free):
                    raise _stale()
                existing = self._backend.open_regular(self._root, PurePath(*parts))
                if existing.snapshot() != expected or parent.inspect_entry(parts[-1]) != expected:
                    raise _stale()
            plan = ExportDirectoryPlan(portable_ref, missing, expected)
            self._states[plan] = _PlanState(plan, parent, existing, prefix)
            parent = existing = None
            return plan
        finally:
            _close_all((existing, parent))

    def reprove(self, plan: ExportDirectoryPlan) -> None:
        state = self._require_plan(plan)
        self._root.reprove()
        state.parent.reprove()
        if not plan.missing_directories:
            if state.parent.inspect_entry(plan.relative_path.rsplit('/', 1)[-1]) != plan.expected:
                raise _stale()
        elif state.parent.inspect_entry(plan.missing_directories[0]) is not None:
            prefix = state.prefix + (plan.missing_directories[0],)
            created = self._created.get(prefix)
            if created is None:
                raise _stale()
            created.reprove()
        if state.existing is not None and state.existing.snapshot() != plan.expected:
            raise _stale()

    def _require_plan(self, plan: ExportDirectoryPlan) -> _PlanState:
        self._require_open()
        if type(plan) is not ExportDirectoryPlan:
            raise TypeError('plan must be an issued ExportDirectoryPlan')
        state = self._states.get(plan)
        if state is None or state.consumed:
            raise _stale()
        return state

    def materialize_target(self, plan: ExportDirectoryPlan, cancellation=None) -> MaterializedExportTarget:
        state = self._require_plan(plan)
        self._started = True
        parent = existing = None
        try:
            _checkpoint(cancellation)
            self.reprove(plan)
            parent = self._backend.duplicate_export_directory(state.parent)
            prefix = state.prefix
            for part in plan.missing_directories:
                _checkpoint(cancellation)
                prefix += (part,)
                shared = self._created.get(prefix)
                if shared is not None:
                    shared.reprove()
                    child = self._backend.duplicate_export_directory(shared)
                else:
                    if parent.inspect_entry(part) is not None:
                        raise _stale()
                    try:
                        shared = self._backend.open_export_directory(parent, part, create=True)
                    except ExportDirectoryCreationError:
                        self._created_paths.append('/'.join(prefix))
                        raise
                    self._created[prefix] = shared
                    self._created_paths.append('/'.join(prefix))
                    child = self._backend.duplicate_export_directory(shared)
                previous, parent = parent, child
                previous.close()
            _checkpoint(cancellation)
            if parent.inspect_entry(plan.relative_path.rsplit('/', 1)[-1]) != plan.expected:
                raise _stale()
            existing, state.existing = state.existing, None
            # Release the plan before transferring to the next owner. A close
            # failure must not orphan an otherwise successfully issued target.
            state.parent.close()
            target = MaterializedExportTarget(
                _TARGET_ISSUER, self._backend, parent, plan.relative_path, plan.expected, existing,
            )
            parent = existing = None
            try:
                target.reprove()
            except BaseException:
                target.close()
                raise
            return target
        finally:
            state.consumed = True
            _close_all((existing, parent, state.existing, state.parent))
            state.existing = None

    def _close_authority(self) -> None:
        authorities = [a for state in self._states.values() for a in (state.existing, state.parent)]
        authorities.extend(reversed(tuple(self._created.values())))
        self._states.clear()
        self._created.clear()
        _close_all(authorities)
