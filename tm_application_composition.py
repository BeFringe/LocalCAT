"""Qt-free application composition for translation-memory runtime resources.

Task 3.7 owns only the deterministic projection from declarative
``ResourceConfig`` values to frozen, ordered runtime ports and Core
``TMResourceHandle`` values. Query and append rules stay with injected
legacy/Core owners; lifecycle classification, resource-local status, and
snapshot replacement remain owned by Task 3.8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from editor_contracts import ResourceConfig, ResourceKind, TMResourceStatus
from tm_contracts import TMRecordDraft, TMResourceHandle, TMStore
from tm_engine import SourceUnit, TMEngine, TMMatch


class LegacyPortBackend(Protocol):
    """Existing-owner seam delegated to by one frozen legacy port."""

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None: ...

    def append(self, draft: TMRecordDraft) -> None: ...


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

        for position, config in enumerate(configs):
            if config.kind is not ResourceKind.TRANSLATION_MEMORY:
                continue
            binding = self._runtime_open(config.path)
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
                continue
            if type(binding) is not CanonicalOpenBinding:
                raise TypeError(
                    "runtime_open must return a canonical or legacy binding"
                )
            if binding.resource_id != config.id:
                raise ValueError(
                    "canonical resource identity must match configured resource id"
                )
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

        return TMRuntimeSnapshot(
            generation=0,
            legacy_ports=tuple(legacy_ports),
            canonical_ports=tuple(canonical_ports),
            canonical_handles=tuple(canonical_handles),
            global_order_by_resource_id=global_order,
            statuses=(),
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
            raise RuntimeError("legacy TM append failed")


def _open_runtime_binding(path: Path) -> RuntimeOpenBinding:
    engine = TMEngine(str(path))
    store = engine.canonical_store
    if store is None:
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


__all__ = [
    "CanonicalOpenBinding",
    "CanonicalResourcePort",
    "LegacyExactPort",
    "LegacyOpenBinding",
    "LegacyPortBackend",
    "RuntimeOpenBinding",
    "RuntimeOpenPort",
    "TMResourceResolver",
    "TMRuntimeSnapshot",
]
