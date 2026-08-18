"""Qt-free application composition for translation-memory runtime resources.

Task 3.7 owns the deterministic projection from declarative
``ResourceConfig`` values to frozen, ordered runtime ports and Core
``TMResourceHandle`` values. Task 3.8 adds fail-closed lifecycle projection
and single-pointer runtime publication. Query and append rules stay with
the injected legacy/Core owners.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Protocol, TypeGuard, TypeVar, cast, final

from editor_contracts import (
    ResourceConfig,
    ResourceKind,
    TMResourceDisplayMode,
    TMResourceStatus,
)
from tm_activation_journal import ActivationPreparationError
from tm_contracts import (
    SourceBindingState,
    StoreHealth,
    TMRecordDraft,
    TMResourceHandle,
    TMStore,
)
from tm_engine import SourceUnit, TMEngine, TMMatch
from tm_sqlite_store import (
    SQLiteStoreSchemaError,
    SourceBindingObservation,
)


_PATH_UNAVAILABLE_CODE = "TM.RUNTIME.PATH_UNAVAILABLE"


_OperationResultT = TypeVar("_OperationResultT")
_CANONICAL_AUTHORITY_UNAVAILABLE_CODE = (
    "TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE"
)
_OPEN_UNAVAILABLE_CODE = "TM.RUNTIME.OPEN_UNAVAILABLE"


@final
class _RuntimeGenerationChanged(RuntimeError):
    """Application-private signal for a stale runtime commit reservation."""
_SOURCE_BINDING_UNAVAILABLE_CODE = "TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE"
_QUERY_LEASE_UNAVAILABLE_CODE = "TM.RUNTIME.QUERY_LEASE_UNAVAILABLE"
_CANONICAL_HEALTH_UNAVAILABLE_CODE = "TM.RUNTIME.CANONICAL_HEALTH_UNAVAILABLE"
_SOURCE_DIVERGED_CODE = "TM.RUNTIME.SOURCE_DIVERGED"


class LegacyPortBackend(Protocol):
    """Existing-owner seam delegated to by one frozen legacy port."""

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None: ...

    def append(self, draft: TMRecordDraft) -> None: ...


@final
class LegacyAppendOperationError(RuntimeError):
    """Body-free formal failure emitted by the existing legacy owner."""

    __slots__ = ("error_code", "retryable")

    def __init__(self) -> None:
        super().__init__()
        self.error_code = "TM.WRITE.LEGACY_APPEND_FAILED"
        self.retryable = True


@dataclass(frozen=True, slots=True)
class CanonicalOpenBinding:
    """Formal Core-open result with an explicit resource identity claim."""

    resource_id: str
    store: TMStore

    def __post_init__(self) -> None:
        _require_identity(self.resource_id, "canonical binding resource id")
        _require_store_port(self.store)


@dataclass(frozen=True, slots=True)
class LegacyOpenBinding:
    """Formal open result retaining the engine from the same classification."""

    backend: LegacyPortBackend = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.backend, "query_exact", None)) or not callable(
            getattr(self.backend, "append", None)
        ):
            raise TypeError("legacy open binding must provide a legacy backend")


type RuntimeOpenBinding = CanonicalOpenBinding | LegacyOpenBinding


class RuntimeOpenPort(Protocol):
    def __call__(self, path: Path, /) -> RuntimeOpenBinding: ...


class _CanonicalQueryViewPort(Protocol):
    resource_id: str
    generation: int

    def health(self) -> StoreHealth: ...


@dataclass(frozen=True, slots=True)
class LegacyExactPort:
    """Frozen routing facts that delegate behavior to the legacy owner."""

    resource_id: str
    resource_name: str
    path: Path
    global_order: int
    active: bool
    lookup: bool
    update: bool
    backend: LegacyPortBackend = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_port_metadata(
            resource_id=self.resource_id,
            resource_name=self.resource_name,
            path=self.path,
            global_order=self.global_order,
            active=self.active,
            lookup=self.lookup,
            update=self.update,
        )
        if not callable(getattr(self.backend, "query_exact", None)) or not callable(
            getattr(self.backend, "append", None)
        ):
            raise TypeError("legacy backend must provide query_exact and append")

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None:
        return self.backend.query_exact(source, speaker_raw)

    def append(self, draft: TMRecordDraft) -> None:
        self.backend.append(draft)


@dataclass(frozen=True, slots=True)
class CanonicalResourcePort:
    """Frozen global routing facts around one Core-owned resource handle."""

    resource_name: str
    path: Path
    global_order: int
    handle: TMResourceHandle = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.handle) is not TMResourceHandle:
            raise TypeError("canonical port handle must be TMResourceHandle")
        _require_port_metadata(
            resource_id=self.handle.resource_id,
            resource_name=self.resource_name,
            path=self.path,
            global_order=self.global_order,
            active=self.handle.active,
            lookup=self.handle.lookup,
            update=self.handle.update,
        )

    @property
    def resource_id(self) -> str:
        return self.handle.resource_id

    @property
    def active(self) -> bool:
        return self.handle.active

    @property
    def lookup(self) -> bool:
        return self.handle.lookup

    @property
    def update(self) -> bool:
        return self.handle.update

    def append(self, draft: TMRecordDraft) -> None:
        _ = self.handle.store.append(draft)


@dataclass(frozen=True, slots=True)
class TMRuntimeSnapshot:
    """One immutable, internally coherent ordered runtime-port set."""

    generation: int
    legacy_ports: tuple[LegacyExactPort, ...]
    canonical_ports: tuple[CanonicalResourcePort, ...]
    canonical_handles: tuple[TMResourceHandle, ...]
    global_order_by_resource_id: tuple[tuple[str, int], ...]
    statuses: tuple[TMResourceStatus, ...]

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise TypeError("runtime snapshot generation must be non-negative int")
        if type(self.legacy_ports) is not tuple or any(
            type(port) is not LegacyExactPort for port in self.legacy_ports
        ):
            raise TypeError("legacy ports must be a tuple of LegacyExactPort")
        if type(self.canonical_ports) is not tuple or any(
            type(port) is not CanonicalResourcePort
            for port in self.canonical_ports
        ):
            raise TypeError(
                "canonical ports must be a tuple of CanonicalResourcePort"
            )
        if type(self.canonical_handles) is not tuple or any(
            type(handle) is not TMResourceHandle
            for handle in self.canonical_handles
        ):
            raise TypeError(
                "canonical handles must be a tuple of TMResourceHandle"
            )
        if type(self.statuses) is not tuple or any(
            type(status) is not TMResourceStatus for status in self.statuses
        ):
            raise TypeError("resource statuses must be a tuple of TMResourceStatus")

        for status in self.statuses:
            status.__post_init__()
        for port in self.legacy_ports:
            port.__post_init__()
        for handle in self.canonical_handles:
            handle.__post_init__()
        for port in self.canonical_ports:
            port.__post_init__()

        _validate_global_order(self.global_order_by_resource_id)
        if len(self.canonical_ports) != len(self.canonical_handles) or any(
            port.handle is not handle
            for port, handle in zip(
                self.canonical_ports,
                self.canonical_handles,
                strict=True,
            )
        ):
            raise ValueError(
                "canonical ports and handles must have one-to-one identity"
            )
        if tuple(handle.order for handle in self.canonical_handles) != tuple(
            range(len(self.canonical_handles))
        ):
            raise ValueError("canonical handle order must be continuous")

        all_ports = (*self.legacy_ports, *self.canonical_ports)
        port_ids = tuple(port.resource_id for port in all_ports)
        if len(port_ids) != len(set(port_ids)):
            raise ValueError("runtime port resource ids must be unique")
        global_order = dict(self.global_order_by_resource_id)
        if any(resource_id not in global_order for resource_id in port_ids):
            raise ValueError(
                "global order must be complete for every runtime port"
            )
        if any(
            port.global_order != global_order[port.resource_id]
            for port in all_ports
        ):
            raise ValueError("runtime port global order must match snapshot mapping")
        for ports in (self.legacy_ports, self.canonical_ports):
            observed = tuple(port.global_order for port in ports)
            if any(left >= right for left, right in zip(observed, observed[1:])):
                raise ValueError("runtime ports must preserve declarative order")

        status_ids = tuple(status.resource_id for status in self.statuses)
        if len(status_ids) != len(set(status_ids)) or any(
            resource_id not in global_order for resource_id in status_ids
        ):
            raise ValueError("resource statuses must bind unique configured resources")
        status_by_resource_id = {
            status.resource_id: status for status in self.statuses
        }
        port_by_resource_id = {port.resource_id: port for port in all_ports}
        if set(port_by_resource_id) != {
            resource_id
            for resource_id, status in status_by_resource_id.items()
            if status.mode is not TMResourceDisplayMode.UNAVAILABLE
        }:
            raise ValueError(
                "runtime port status must have one-to-one availability"
            )
        status_order = tuple(global_order[resource_id] for resource_id in status_ids)
        if any(
            left >= right
            for left, right in zip(status_order, status_order[1:])
        ):
            raise ValueError("resource statuses must preserve declarative order")

        legacy_ids = {port.resource_id for port in self.legacy_ports}
        canonical_ids = {port.resource_id for port in self.canonical_ports}
        for resource_id, port in port_by_resource_id.items():
            status = status_by_resource_id[resource_id]
            if status.resource_name != port.resource_name:
                raise ValueError("runtime port status resource name must match")
            if resource_id in legacy_ids:
                if status.mode is not TMResourceDisplayMode.LEGACY_EXACT_ONLY:
                    raise ValueError(
                        "runtime port status must classify legacy exact-only"
                    )
                continue
            if resource_id not in canonical_ids or status.mode not in (
                TMResourceDisplayMode.CANONICAL_ACTIVE,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            ):
                raise ValueError(
                    "canonical lifecycle status must be active or source-diverged"
                )
            if (
                not status.exact_available
                or status.context_available
                or status.fuzzy_available
            ):
                raise ValueError(
                    "canonical lifecycle status must expose exact only"
                )


class RuntimeResolverPort(Protocol):
    """Narrow resolver port consumed by the atomic runtime host."""

    def resolve(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot: ...


def _clone_resource_configs(
    configs: tuple[ResourceConfig, ...],
) -> tuple[ResourceConfig, ...]:
    return tuple(
        ResourceConfig(
            id=config.id,
            name=config.name,
            kind=config.kind,
            path=config.path,
            active=config.active,
            lookup=config.lookup,
            update=config.update,
        )
        for config in configs
    )


def _clone_runtime_snapshot(
    snapshot: TMRuntimeSnapshot,
    *,
    generation: int,
) -> TMRuntimeSnapshot:
    """Build a defensive port graph while retaining Core-owned backends."""

    legacy_ports = tuple(
        LegacyExactPort(
            resource_id=port.resource_id,
            resource_name=port.resource_name,
            path=port.path,
            global_order=port.global_order,
            active=port.active,
            lookup=port.lookup,
            update=port.update,
            backend=port.backend,
        )
        for port in snapshot.legacy_ports
    )
    canonical_handles: list[TMResourceHandle] = []
    canonical_ports: list[CanonicalResourcePort] = []
    for port, handle in zip(
        snapshot.canonical_ports,
        snapshot.canonical_handles,
        strict=True,
    ):
        cloned_handle = TMResourceHandle(
            resource_id=handle.resource_id,
            store=handle.store,
            active=handle.active,
            lookup=handle.lookup,
            update=handle.update,
            order=handle.order,
        )
        canonical_handles.append(cloned_handle)
        canonical_ports.append(
            CanonicalResourcePort(
                resource_name=port.resource_name,
                path=port.path,
                global_order=port.global_order,
                handle=cloned_handle,
            )
        )
    return TMRuntimeSnapshot(
        generation=generation,
        legacy_ports=legacy_ports,
        canonical_ports=tuple(canonical_ports),
        canonical_handles=tuple(canonical_handles),
        global_order_by_resource_id=tuple(
            (resource_id, order)
            for resource_id, order in snapshot.global_order_by_resource_id
        ),
        statuses=tuple(replace(status) for status in snapshot.statuses),
    )


def _runtime_snapshot_matches_private_binding(
    published: TMRuntimeSnapshot,
    private: TMRuntimeSnapshot,
    configs: tuple[ResourceConfig, ...],
) -> bool:
    """Check values plus backend/store identities without trusting equality."""

    try:
        if type(published) is not TMRuntimeSnapshot:
            return False
        _validate_snapshot_against_configs(published, configs)
        if (
            published.generation != private.generation
            or published.global_order_by_resource_id
            != private.global_order_by_resource_id
            or published.statuses != private.statuses
            or len(published.legacy_ports) != len(private.legacy_ports)
            or len(published.canonical_ports) != len(private.canonical_ports)
            or len(published.canonical_handles)
            != len(private.canonical_handles)
        ):
            return False
        for observed, expected in zip(
            published.legacy_ports,
            private.legacy_ports,
            strict=True,
        ):
            if (
                observed.resource_id != expected.resource_id
                or observed.resource_name != expected.resource_name
                or observed.path != expected.path
                or observed.global_order != expected.global_order
                or observed.active is not expected.active
                or observed.lookup is not expected.lookup
                or observed.update is not expected.update
                or observed.backend is not expected.backend
            ):
                return False
        for observed_port, observed_handle, expected_port, expected_handle in zip(
            published.canonical_ports,
            published.canonical_handles,
            private.canonical_ports,
            private.canonical_handles,
            strict=True,
        ):
            if (
                observed_port.handle is not observed_handle
                or expected_port.handle is not expected_handle
                or observed_port.resource_name != expected_port.resource_name
                or observed_port.path != expected_port.path
                or observed_port.global_order != expected_port.global_order
                or observed_handle.resource_id != expected_handle.resource_id
                or observed_handle.store is not expected_handle.store
                or observed_handle.active is not expected_handle.active
                or observed_handle.lookup is not expected_handle.lookup
                or observed_handle.update is not expected_handle.update
                or observed_handle.order != expected_handle.order
            ):
                return False
    except ValueError:
        return False
    return True


class TMRuntimeHost:
    """Own one atomically replaceable immutable resource snapshot.

    Resolution and every Core precondition probe complete before the narrow
    publication lock is acquired. A caller captures the current frozen value
    once; that ordinary strong reference keeps the old ports and stores alive
    for the operation even after a later refresh publishes a new value.
    """

    def __init__(
        self,
        *,
        resolver: RuntimeResolverPort,
        configs: tuple[ResourceConfig, ...],
    ) -> None:
        resolve = getattr(resolver, "resolve", None)
        if not callable(resolve):
            raise TypeError("runtime host resolver must provide resolve")
        _validate_configs(configs)
        private_configs = _clone_resource_configs(configs)
        initial = resolve(private_configs)
        if type(initial) is not TMRuntimeSnapshot:
            raise TypeError("runtime resolver must return TMRuntimeSnapshot")
        private = _clone_runtime_snapshot(initial, generation=0)
        _validate_snapshot_against_configs(private, private_configs)
        published = _clone_runtime_snapshot(private, generation=0)
        if not _runtime_snapshot_matches_private_binding(
            published,
            private,
            private_configs,
        ):
            raise ValueError("runtime initial candidate drift")
        self._resolver = resolver
        self._lock = threading.Lock()
        self._configs = private_configs
        self._snapshot = published
        self._operation_template = private

    def snapshot(self) -> TMRuntimeSnapshot:
        """Capture one complete snapshot for an operation."""

        with self._lock:
            return self._snapshot

    def capture_operation_snapshot(self) -> TMRuntimeSnapshot:
        """Return a defensive snapshot bound to the current published graph."""

        with self._lock:
            if not _runtime_snapshot_matches_private_binding(
                self._snapshot,
                self._operation_template,
                self._configs,
            ):
                raise ValueError("runtime snapshot drift")
            return _clone_runtime_snapshot(
                self._operation_template,
                generation=self._operation_template.generation,
            )

    def _inspect_resource_statuses(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> tuple[TMResourceStatus, ...]:
        """Re-observe lifecycle facts without publishing a runtime generation."""

        _validate_configs(configs)
        private_configs = _clone_resource_configs(configs)
        candidate = self._resolver.resolve(private_configs)
        if type(candidate) is not TMRuntimeSnapshot:
            raise TypeError("runtime resolver must return TMRuntimeSnapshot")
        private_candidate = _clone_runtime_snapshot(candidate, generation=0)
        _validate_snapshot_against_configs(private_candidate, private_configs)
        return tuple(replace(status) for status in private_candidate.statuses)

    def _capture_operation_snapshot_for_configs(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot:
        """Capture startup state only when it matches current repository facts."""

        _validate_configs(configs)
        private_configs = _clone_resource_configs(configs)
        with self._lock:
            if self._configs != private_configs:
                raise ValueError("runtime startup configs do not match repository")
            if not _runtime_snapshot_matches_private_binding(
                self._snapshot,
                self._operation_template,
                private_configs,
            ):
                raise ValueError("runtime snapshot drift")
            return _clone_runtime_snapshot(
                self._operation_template,
                generation=self._operation_template.generation,
            )

    def _current_generation(self) -> int:
        """Return the validated current generation without cloning a snapshot."""

        with self._lock:
            if not _runtime_snapshot_matches_private_binding(
                self._snapshot,
                self._operation_template,
                self._configs,
            ):
                raise ValueError("runtime snapshot drift")
            generation = self._operation_template.generation
            if type(generation) is not int or generation < 0:
                raise ValueError("runtime generation drift")
            return generation

    def _run_if_generation_current(
        self,
        generation: int,
        operation: Callable[[], _OperationResultT],
    ) -> _OperationResultT:
        """Run one short application commit against an exact generation."""

        if type(generation) is not int or generation < 0:
            raise TypeError("runtime generation must be non-negative int")
        if not callable(operation):
            raise TypeError("runtime generation operation must be callable")
        with self._lock:
            if not _runtime_snapshot_matches_private_binding(
                self._snapshot,
                self._operation_template,
                self._configs,
            ):
                raise _RuntimeGenerationChanged
            if self._operation_template.generation != generation:
                raise _RuntimeGenerationChanged
            return operation()

    def refresh(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot:
        """Resolve completely, then publish exactly one new generation."""

        return self._refresh_validated(configs, lambda _candidate: None)

    def _refresh_validated(
        self,
        configs: tuple[ResourceConfig, ...],
        validate_candidate: Callable[[TMRuntimeSnapshot], None],
    ) -> TMRuntimeSnapshot:
        """Resolve, run one application precommit validator, then publish."""

        _validate_configs(configs)
        if not callable(validate_candidate):
            raise TypeError("runtime candidate validator must be callable")
        private_configs = _clone_resource_configs(configs)
        candidate = self._resolver.resolve(private_configs)
        if type(candidate) is not TMRuntimeSnapshot:
            raise TypeError("runtime resolver must return TMRuntimeSnapshot")
        private_candidate = _clone_runtime_snapshot(candidate, generation=0)
        _validate_snapshot_against_configs(private_candidate, private_configs)
        published_candidate = _clone_runtime_snapshot(
            private_candidate,
            generation=0,
        )
        validation_result = validate_candidate(private_candidate)
        if validation_result is not None:
            raise TypeError("runtime candidate validator must return None")
        try:
            _validate_snapshot_against_configs(private_candidate, private_configs)
        except ValueError as error:
            raise ValueError("runtime refresh candidate drift") from error
        if not _runtime_snapshot_matches_private_binding(
            published_candidate,
            private_candidate,
            private_configs,
        ):
            raise ValueError("runtime refresh candidate drift")
        with self._lock:
            if not _runtime_snapshot_matches_private_binding(
                self._snapshot,
                self._operation_template,
                self._configs,
            ):
                raise ValueError("runtime snapshot drift")
            if not _runtime_snapshot_matches_private_binding(
                published_candidate,
                private_candidate,
                private_configs,
            ):
                raise ValueError("runtime refresh candidate drift")
            private = replace(
                private_candidate,
                generation=self._snapshot.generation + 1,
            )
            published = replace(
                published_candidate,
                generation=private.generation,
            )
            if not _runtime_snapshot_matches_private_binding(
                published,
                private,
                private_configs,
            ):
                raise ValueError("runtime refresh candidate drift")
            self._configs = private_configs
            self._operation_template = private
            self._snapshot = published
            return published


class TMResourceResolver:
    """Resolve declarative resources without owning their operations."""

    def __init__(
        self,
        *,
        runtime_open: RuntimeOpenPort | None = None,
    ) -> None:
        if runtime_open is None:
            runtime_open = _open_runtime_binding
        if not callable(runtime_open):
            raise TypeError("runtime_open must be callable")
        self._runtime_open = runtime_open

    def resolve(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot:
        """Build a frozen snapshot in repository order.

        The complete declarative order is retained independently from the
        canonical cohort. Canonical handles are numbered continuously so a
        later ``TMQuery.resource_order`` can satisfy Core's one-to-one mapping.
        """

        _validate_configs(configs)
        global_order = tuple(
            (config.id, position)
            for position, config in enumerate(configs)
        )
        legacy_ports: list[LegacyExactPort] = []
        canonical_ports: list[CanonicalResourcePort] = []
        canonical_handles: list[TMResourceHandle] = []
        statuses: list[TMResourceStatus] = []

        for position, config in enumerate(configs):
            if config.kind is not ResourceKind.TRANSLATION_MEMORY:
                continue
            try:
                binding = self._runtime_open(config.path)
            except (
                FileNotFoundError,
                PermissionError,
                IsADirectoryError,
            ):
                statuses.append(
                    _unavailable_status(
                        config,
                        code=_PATH_UNAVAILABLE_CODE,
                        retryable=True,
                    )
                )
                continue
            except ValueError as error:
                statuses.append(
                    _unavailable_status(
                        config,
                        code=(
                            _CANONICAL_AUTHORITY_UNAVAILABLE_CODE
                            if str(error).startswith("TM.CANONICAL_")
                            else _OPEN_UNAVAILABLE_CODE
                        ),
                        retryable=False,
                    )
                )
                continue
            except ActivationPreparationError as error:
                statuses.append(
                    _unavailable_status(
                        config,
                        code=_CANONICAL_AUTHORITY_UNAVAILABLE_CODE,
                        retryable=error.retryable,
                    )
                )
                continue
            except SQLiteStoreSchemaError:
                statuses.append(
                    _unavailable_status(
                        config,
                        code=_OPEN_UNAVAILABLE_CODE,
                        retryable=False,
                    )
                )
                continue
            except (OSError, sqlite3.DatabaseError):
                statuses.append(
                    _unavailable_status(
                        config,
                        code=_OPEN_UNAVAILABLE_CODE,
                        retryable=True,
                    )
                )
                continue
            if type(binding) is LegacyOpenBinding:
                legacy_ports.append(
                    LegacyExactPort(
                        resource_id=config.id,
                        resource_name=config.name,
                        path=config.path,
                        global_order=position,
                        active=config.active,
                        lookup=config.lookup,
                        update=config.update,
                        backend=binding.backend,
                    )
                )
                statuses.append(_legacy_status(config))
                continue
            if type(binding) is not CanonicalOpenBinding:
                raise TypeError(
                    "runtime_open must return a canonical or legacy binding"
                )
            if binding.resource_id != config.id:
                statuses.append(
                    _unavailable_status(
                        config,
                        code=_CANONICAL_AUTHORITY_UNAVAILABLE_CODE,
                        retryable=False,
                    )
                )
                continue
            try:
                canonical_status = _probe_canonical_status(config, binding)
            except _ResourcePreconditionFailure as failure:
                statuses.append(
                    _unavailable_status(
                        config,
                        code=failure.code,
                        retryable=failure.retryable,
                    )
                )
                continue
            handle = TMResourceHandle(
                resource_id=config.id,
                store=binding.store,
                active=config.active,
                lookup=config.lookup,
                update=config.update,
                order=len(canonical_handles),
            )
            canonical_handles.append(handle)
            canonical_ports.append(
                CanonicalResourcePort(
                    resource_name=config.name,
                    path=config.path,
                    global_order=position,
                    handle=handle,
                )
            )
            statuses.append(canonical_status)

        return TMRuntimeSnapshot(
            generation=0,
            legacy_ports=tuple(legacy_ports),
            canonical_ports=tuple(canonical_ports),
            canonical_handles=tuple(canonical_handles),
            global_order_by_resource_id=global_order,
            statuses=tuple(statuses),
        )


class _TMEngineLegacyBackend:
    """Mechanical contract adapter around the existing legacy TM owner."""

    def __init__(self, engine: TMEngine) -> None:
        if type(engine) is not TMEngine or engine.canonical_store is not None:
            raise ValueError("legacy backend requires the classified legacy engine")
        self._engine = engine

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None:
        del speaker_raw
        return self._engine.query_exact(source)

    def append(self, draft: TMRecordDraft) -> None:
        unit = SourceUnit(
            id="localcat-runtime-append",
            text=draft.source_raw,
            context_prev=draft.context_prev_raw,
            context_next=draft.context_next_raw,
            speaker=draft.speaker_raw,
            file_source=draft.file_source or "",
        )
        if not self._engine.save_record(unit, draft.target_raw):
            raise LegacyAppendOperationError()


def _open_runtime_binding(path: Path) -> RuntimeOpenBinding:
    engine = TMEngine(str(path))
    store = engine.canonical_store
    if store is None:
        if not path.is_file():
            raise FileNotFoundError(_PATH_UNAVAILABLE_CODE)
        try:
            with path.open("rb") as stream:
                _ = stream.read(1)
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            raise
        except OSError as error:
            raise OSError(_PATH_UNAVAILABLE_CODE) from error
        return LegacyOpenBinding(backend=_TMEngineLegacyBackend(engine))
    return CanonicalOpenBinding(
        resource_id=store.coordinator.resource_id,
        store=store,
    )


def _validate_configs(configs: tuple[ResourceConfig, ...]) -> None:
    if type(configs) is not tuple:
        raise TypeError("resource configs must be a tuple")
    resource_ids: list[str] = []
    for config in configs:
        if type(config) is not ResourceConfig:
            raise TypeError("resource configs must contain ResourceConfig values")
        if type(config.kind) is not ResourceKind:
            raise TypeError("resource kind must be ResourceKind")
        if not all(
            type(flag) is bool
            for flag in (config.active, config.lookup, config.update)
        ):
            raise TypeError("resource state flags must be booleans")
        resource_ids.append(config.id)
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("resource ids must be unique")


def _validate_global_order(mapping: tuple[tuple[str, int], ...]) -> None:
    if type(mapping) is not tuple:
        raise TypeError("global resource order must be a tuple")
    resource_ids: list[str] = []
    orders: list[int] = []
    for item in mapping:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("global resource order entries must be pairs")
        resource_id, order = item
        _require_identity(resource_id, "global resource order id")
        if type(order) is not int or order < 0:
            raise TypeError("global resource order values must be non-negative int")
        resource_ids.append(resource_id)
        orders.append(order)
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("global resource order ids must be unique")
    if tuple(orders) != tuple(range(len(mapping))):
        raise ValueError("global resource order must be complete and continuous")


def _validate_snapshot_against_configs(
    snapshot: TMRuntimeSnapshot,
    configs: tuple[ResourceConfig, ...],
) -> None:
    snapshot.__post_init__()
    expected_global_order = tuple(
        (config.id, position) for position, config in enumerate(configs)
    )
    if snapshot.global_order_by_resource_id != expected_global_order:
        raise ValueError("runtime snapshot configured global order must match")

    tm_configs = tuple(
        config
        for config in configs
        if config.kind is ResourceKind.TRANSLATION_MEMORY
    )
    if tuple(status.resource_id for status in snapshot.statuses) != tuple(
        config.id for config in tm_configs
    ):
        raise ValueError(
            "runtime snapshot configured TM statuses must be complete and ordered"
        )
    config_by_resource_id = {config.id: config for config in tm_configs}
    for status in snapshot.statuses:
        if status.resource_name != config_by_resource_id[status.resource_id].name:
            raise ValueError(
                "runtime snapshot configured TM statuses must preserve names"
            )

    for port in (*snapshot.legacy_ports, *snapshot.canonical_ports):
        config = config_by_resource_id[port.resource_id]
        expected_order = expected_global_order[port.global_order]
        if (
            expected_order != (config.id, port.global_order)
            or port.resource_name != config.name
            or port.path != config.path
            or port.active is not config.active
            or port.lookup is not config.lookup
            or port.update is not config.update
        ):
            raise ValueError(
                "runtime snapshot configured TM port lineage must match"
            )


def _require_port_metadata(
    *,
    resource_id: str,
    resource_name: str,
    path: Path,
    global_order: int,
    active: bool,
    lookup: bool,
    update: bool,
) -> None:
    _require_identity(resource_id, "runtime port resource id")
    _require_identity(resource_name, "runtime port resource name")
    if type(path) is not type(Path()) or not path.is_absolute():
        raise TypeError("runtime port path must be an absolute native Path")
    if type(global_order) is not int or global_order < 0:
        raise TypeError("runtime port global order must be non-negative int")
    if not all(type(flag) is bool for flag in (active, lookup, update)):
        raise TypeError("runtime port state flags must be booleans")


def _require_identity(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")


def _require_store_port(store: object) -> None:
    if store is None:
        raise TypeError("canonical binding store must not be None")
    for method_name in (
        "exact_records",
        "records_by_id",
        "append",
        "export_records",
        "health",
    ):
        if not callable(getattr(store, method_name, None)):
            raise TypeError("canonical binding store must implement TMStore")


class _ResourcePreconditionFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _legacy_status(config: ResourceConfig) -> TMResourceStatus:
    return TMResourceStatus(
        resource_id=config.id,
        resource_name=config.name,
        mode=TMResourceDisplayMode.LEGACY_EXACT_ONLY,
        exact_available=True,
        context_available=False,
        fuzzy_available=False,
        safe_codes=(),
        retryable=False,
    )


def _unavailable_status(
    config: ResourceConfig,
    *,
    code: str,
    retryable: bool,
) -> TMResourceStatus:
    return TMResourceStatus(
        resource_id=config.id,
        resource_name=config.name,
        mode=TMResourceDisplayMode.UNAVAILABLE,
        exact_available=False,
        context_available=False,
        fuzzy_available=False,
        safe_codes=(code,),
        retryable=retryable,
    )


def _probe_canonical_status(
    config: ResourceConfig,
    binding: CanonicalOpenBinding,
) -> TMResourceStatus:
    """Project one coherent Core source-binding/query-lease observation."""

    try:
        monitor = getattr(binding.store, "source_binding_monitor", None)
        observe = getattr(monitor, "observe", None)
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        sqlite3.DatabaseError,
        OSError,
        ValueError,
    ) as error:
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=_safe_retryable(error),
        ) from error
    if not callable(observe):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=False,
        )
    try:
        observation = observe()
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        sqlite3.DatabaseError,
        OSError,
        ValueError,
    ) as error:
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=_safe_retryable(error),
        ) from error
    if not _valid_source_observation(observation):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=False,
        )
    if observation.resource_id != binding.resource_id:
        raise _ResourcePreconditionFailure(
            _CANONICAL_AUTHORITY_UNAVAILABLE_CODE,
            retryable=False,
        )

    try:
        query_lease = getattr(binding.store, "query_lease", None)
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        sqlite3.DatabaseError,
        OSError,
        ValueError,
    ) as error:
        raise _ResourcePreconditionFailure(
            _QUERY_LEASE_UNAVAILABLE_CODE,
            retryable=_safe_retryable(error),
        ) from error
    if not callable(query_lease):
        raise _ResourcePreconditionFailure(
            _QUERY_LEASE_UNAVAILABLE_CODE,
            retryable=False,
        )
    query_lease_port = cast(
        Callable[[], AbstractContextManager[_CanonicalQueryViewPort]],
        query_lease,
    )
    try:
        with query_lease_port() as view:
            health_port = getattr(view, "health", None)
            view_resource_id = getattr(view, "resource_id", None)
            view_generation = getattr(view, "generation", None)
            if (
                not callable(health_port)
                or type(view_resource_id) is not str
                or type(view_generation) is not int
                or isinstance(view_generation, bool)
                or view_generation < 0
            ):
                raise _ResourcePreconditionFailure(
                    _QUERY_LEASE_UNAVAILABLE_CODE,
                    retryable=False,
                )
            if view_resource_id != binding.resource_id:
                raise _ResourcePreconditionFailure(
                    _CANONICAL_AUTHORITY_UNAVAILABLE_CODE,
                    retryable=False,
                )
            health = health_port()
    except _ResourcePreconditionFailure:
        raise
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        sqlite3.DatabaseError,
        OSError,
        ValueError,
    ) as error:
        raise _ResourcePreconditionFailure(
            _QUERY_LEASE_UNAVAILABLE_CODE,
            retryable=_safe_retryable(error),
        ) from error

    if not _valid_store_health(health):
        raise _ResourcePreconditionFailure(
            _CANONICAL_HEALTH_UNAVAILABLE_CODE,
            retryable=False,
        )
    if (
        not health.healthy
        or not health.exact_available
        or health.generation != view_generation
    ):
        raise _ResourcePreconditionFailure(
            _CANONICAL_HEALTH_UNAVAILABLE_CODE,
            retryable=True,
        )
    try:
        final_observation = observe()
    except (
        ActivationPreparationError,
        SQLiteStoreSchemaError,
        sqlite3.DatabaseError,
        OSError,
        ValueError,
    ) as error:
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=_safe_retryable(error),
        ) from error
    if (
        not _valid_source_observation(final_observation)
        or final_observation.resource_id != binding.resource_id
        or observation.generation != view_generation
        or final_observation.generation != view_generation
    ):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=True,
        )
    if (
        observation.canonical_store_id
        != final_observation.canonical_store_id
        or observation.binding_digest != final_observation.binding_digest
        or health.snapshot_binding_digest != final_observation.binding_digest
    ):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=True,
        )

    allowed_health_states = {observation.state, final_observation.state}
    if health.source_binding_state not in allowed_health_states:
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=True,
        )
    verified_states = (
        SourceBindingState.VERIFIED_CURRENT,
        SourceBindingState.VERIFIED_HISTORY,
    )
    if observation.state is not final_observation.state and not (
        observation.state in verified_states
        and final_observation.state
        in (*verified_states, SourceBindingState.SOURCE_DIVERGED)
    ):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=True,
        )

    if final_observation.state is SourceBindingState.SOURCE_DIVERGED:
        return TMResourceStatus(
            resource_id=config.id,
            resource_name=config.name,
            mode=TMResourceDisplayMode.SOURCE_DIVERGED,
            exact_available=True,
            context_available=False,
            fuzzy_available=False,
            safe_codes=(_SOURCE_DIVERGED_CODE,),
            retryable=True,
        )
    if final_observation.state not in (
        SourceBindingState.VERIFIED_CURRENT,
        SourceBindingState.VERIFIED_HISTORY,
    ):
        raise _ResourcePreconditionFailure(
            _SOURCE_BINDING_UNAVAILABLE_CODE,
            retryable=False,
        )
    return TMResourceStatus(
        resource_id=config.id,
        resource_name=config.name,
        mode=TMResourceDisplayMode.CANONICAL_ACTIVE,
        exact_available=True,
        context_available=False,
        fuzzy_available=False,
        safe_codes=(),
        retryable=False,
    )


def _safe_retryable(error: BaseException) -> bool:
    retryable = getattr(error, "retryable", None)
    if type(retryable) is bool:
        return retryable
    return isinstance(error, OSError)


def _valid_source_observation(
    value: object,
) -> TypeGuard[SourceBindingObservation]:
    if type(value) is not SourceBindingObservation:
        return False
    observation = cast(SourceBindingObservation, value)
    if (
        type(observation.resource_id) is not str
        or not observation.resource_id.strip()
        or type(observation.canonical_store_id) is not str
        or not observation.canonical_store_id.strip()
        or type(observation.generation) is not int
        or observation.generation < 0
        or type(observation.head_revision) is not int
        or observation.head_revision < 0
        or type(observation.state) is not SourceBindingState
    ):
        return False
    digest = observation.binding_digest
    if digest is not None and (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return False
    codes = observation.diagnostic_codes
    return (
        type(codes) is tuple
        and all(type(code) is str and bool(code.strip()) for code in codes)
        and len(codes) == len(set(codes))
        and codes == tuple(sorted(codes))
    )


def _valid_store_health(value: object) -> TypeGuard[StoreHealth]:
    if type(value) is not StoreHealth:
        return False
    health = cast(StoreHealth, value)
    return (
        type(health.healthy) is bool
        and type(health.exact_available) is bool
        and type(health.generation) is int
        and health.generation >= 0
        and type(health.source_binding_state) is SourceBindingState
    )


__all__ = [
    "CanonicalOpenBinding",
    "CanonicalResourcePort",
    "LegacyExactPort",
    "LegacyOpenBinding",
    "LegacyPortBackend",
    "RuntimeOpenBinding",
    "RuntimeOpenPort",
    "RuntimeResolverPort",
    "TMResourceResolver",
    "TMRuntimeHost",
    "TMRuntimeSnapshot",
]
