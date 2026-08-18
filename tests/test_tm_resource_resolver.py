"""Task 3.7 declarative TM resource to ordered runtime-port tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect
from pathlib import Path
import tempfile
import unittest
from typing import NoReturn, cast
from unittest.mock import patch

from editor_contracts import ResourceConfig, ResourceKind
from tm_application_composition import (
    CanonicalOpenBinding,
    CanonicalResourcePort,
    LegacyExactPort,
    LegacyOpenBinding,
    LegacyPortBackend,
    RuntimeOpenBinding,
    TMResourceResolver,
    TMRuntimeSnapshot,
)
from tm_contracts import (
    StoreHealth,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    TMStore,
)
from tm_engine import TMEngine, TMMatch


class _FakeCoordinator:
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id


class _NoOperationStore:
    """Structural Core store whose data ports must stay untouched by resolve."""

    def __init__(self, resource_id: str) -> None:
        self.coordinator = _FakeCoordinator(resource_id)

    @staticmethod
    def _unexpected() -> NoReturn:
        raise AssertionError("resolver must not invoke a store operation")

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        del source_raw
        return self._unexpected()

    def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[TMRecord, ...]:
        del record_ids
        return self._unexpected()

    def append(self, draft: TMRecordDraft) -> TMRecord:
        del draft
        return self._unexpected()

    def export_records(self):  # type: ignore[no-untyped-def]
        return self._unexpected()

    def health(self) -> StoreHealth:
        return self._unexpected()


class _RecordingStore(_NoOperationStore):
    def __init__(self, resource_id: str) -> None:
        super().__init__(resource_id)
        self.appended: list[TMRecordDraft] = []

    def append(self, draft: TMRecordDraft) -> TMRecord:
        self.appended.append(draft)
        return cast(TMRecord, object())


class _RecordingLegacyBackend:
    def __init__(self, result: TMMatch | None = None) -> None:
        self.result = result
        self.queries: list[tuple[str, str | None]] = []
        self.appended: list[TMRecordDraft] = []

    def query_exact(self, source: str, speaker_raw: str | None) -> TMMatch | None:
        self.queries.append((source, speaker_raw))
        return self.result

    def append(self, draft: TMRecordDraft) -> None:
        self.appended.append(draft)


def _draft() -> TMRecordDraft:
    return TMRecordDraft(
        source_raw="Source",
        target_raw="Target",
        speaker_raw="Speaker",
        context_prev_raw=None,
        context_next_raw=None,
        file_source="project.json",
        provenance=(("source", "test"),),
    )


def _legacy_binding(backend: _RecordingLegacyBackend) -> LegacyOpenBinding:
    return LegacyOpenBinding(backend=cast(LegacyPortBackend, backend))


def _canonical_binding(store: _NoOperationStore) -> CanonicalOpenBinding:
    return CanonicalOpenBinding(
        resource_id=store.coordinator.resource_id,
        store=cast(TMStore, store),
    )


def _routing_projection(snapshot: TMRuntimeSnapshot) -> tuple[object, ...]:
    return (
        snapshot.generation,
        tuple(
            (
                port.resource_id,
                port.global_order,
                port.active,
                port.lookup,
                port.update,
            )
            for port in snapshot.legacy_ports
        ),
        tuple(
            (
                port.resource_id,
                port.global_order,
                port.active,
                port.lookup,
                port.update,
                port.handle.order,
            )
            for port in snapshot.canonical_ports
        ),
        snapshot.global_order_by_resource_id,
    )


def _config(
    root: Path,
    resource_id: str,
    *,
    kind: ResourceKind = ResourceKind.TRANSLATION_MEMORY,
    active: bool = True,
    lookup: bool = True,
    update: bool = True,
) -> ResourceConfig:
    suffix = ".jsonl" if kind is ResourceKind.TRANSLATION_MEMORY else ".csv"
    return ResourceConfig(
        id=resource_id,
        name=resource_id,
        kind=kind,
        path=(root / f"{resource_id}{suffix}").resolve(),
        active=active,
        lookup=lookup,
        update=update,
    )


class TMResourceResolverTests(unittest.TestCase):
    def test_default_open_classifies_each_resource_once_and_freezes_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.single-classification")
            config.path.write_text(
                '{"source":"Source","target":"Legacy"}\n',
                encoding="utf-8",
            )
            classification_calls: list[str] = []

            def classify_once_then_activate(
                path: Path,
                **_kwargs: object,
            ) -> TMStore | None:
                self.assertEqual(path, config.path)
                classification_calls.append("open")
                if len(classification_calls) == 1:
                    return None
                return cast(TMStore, _NoOperationStore(config.id))

            with patch(
                "tm_engine.open_canonical_tm_store",
                side_effect=classify_once_then_activate,
            ):
                snapshot = TMResourceResolver().resolve((config,))

            self.assertEqual(classification_calls, ["open"])
            result = snapshot.legacy_ports[0].query_exact("Source", None)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.target, "Legacy")

    def test_tm_engine_has_no_caller_forced_legacy_constructor(self) -> None:
        parameters = inspect.signature(TMEngine).parameters
        self.assertEqual(
            tuple(parameters),
            (
                "tm_path",
                "active",
                "lookup",
                "update",
                "drain_timeout_seconds",
            ),
        )
        self.assertFalse(hasattr(TMEngine, "from_proven_legacy"))
        self.assertIsNone(TMEngine.canonical_store.fset)

    def test_default_legacy_binding_delegates_to_existing_tm_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.default")
            config.path.write_text(
                '{"source":"Source","target":"old"}\n'
                '{"source":"Source","target":"winner"}\n',
                encoding="utf-8",
            )

            port = TMResourceResolver().resolve((config,)).legacy_ports[0]
            result = port.query_exact("Source", "Speaker")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.target, "winner")

            draft = _draft()
            port.append(draft)
            self.assertIn(
                '"target": "Target"',
                config.path.read_text(encoding="utf-8").splitlines()[-1],
            )

    def test_formal_canonical_binding_needs_no_ad_hoc_store_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "canonical.formal")
            store = _NoOperationStore("unused")
            del store.coordinator

            snapshot = TMResourceResolver(
                runtime_open=lambda _path: CanonicalOpenBinding(
                    resource_id="canonical.formal",
                    store=cast(TMStore, store),
                ),
            ).resolve((config,))

            self.assertEqual(snapshot.canonical_ports[0].resource_id, config.id)
            self.assertIs(snapshot.canonical_handles[0].store, store)

    def test_runtime_ports_delegate_query_and_append_to_existing_owners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_config = _config(root, "legacy.delegate")
            canonical_config = _config(root, "canonical.delegate")
            legacy_result = TMMatch(
                source="Source",
                target="Legacy target",
                similarity=1.0,
                match_type="EXACT",
                tm_source="legacy.jsonl",
            )
            legacy_backend = _RecordingLegacyBackend(legacy_result)
            canonical_store = _RecordingStore("canonical.delegate")

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                if path == canonical_config.path:
                    return CanonicalOpenBinding(
                        resource_id="canonical.delegate",
                        store=cast(TMStore, canonical_store),
                    )
                return _legacy_binding(legacy_backend)

            snapshot = TMResourceResolver(
                runtime_open=open_runtime,
            ).resolve((legacy_config, canonical_config))

            legacy_port = snapshot.legacy_ports[0]
            canonical_port = snapshot.canonical_ports[0]
            self.assertEqual(legacy_backend.queries, [])
            self.assertEqual(legacy_backend.appended, [])
            self.assertEqual(canonical_store.appended, [])

            self.assertIs(
                legacy_port.query_exact("Source", "Speaker"),
                legacy_result,
            )
            draft = _draft()
            legacy_port.append(draft)
            canonical_port.append(draft)

            self.assertEqual(legacy_backend.queries, [("Source", "Speaker")])
            self.assertEqual(legacy_backend.appended, [draft])
            self.assertEqual(canonical_store.appended, [draft])

    def test_interleaved_resources_keep_global_order_and_contiguous_core_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.first", active=True, lookup=False),
                _config(root, "terms.middle", kind=ResourceKind.TERMBASE),
                _config(root, "canonical.first", update=False),
                _config(
                    root,
                    "canonical.second",
                    active=False,
                    lookup=True,
                    update=True,
                ),
                _config(root, "legacy.last", active=False, update=False),
            )
            stores = {
                configs[2].path: _NoOperationStore("canonical.first"),
                configs[3].path: _NoOperationStore("canonical.second"),
            }
            legacy_backends = {
                configs[0].path: _RecordingLegacyBackend(),
                configs[4].path: _RecordingLegacyBackend(),
            }
            opened: list[Path] = []

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                opened.append(path)
                store = stores.get(path)
                if store is None:
                    return _legacy_binding(legacy_backends[path])
                return _canonical_binding(store)

            snapshot = TMResourceResolver(
                runtime_open=open_runtime,
            ).resolve(configs)

            self.assertIs(type(snapshot), TMRuntimeSnapshot)
            self.assertEqual(snapshot.generation, 0)
            self.assertEqual(
                snapshot.global_order_by_resource_id,
                (
                    ("legacy.first", 0),
                    ("terms.middle", 1),
                    ("canonical.first", 2),
                    ("canonical.second", 3),
                    ("legacy.last", 4),
                ),
            )
            self.assertEqual(
                opened,
                [configs[0].path, configs[2].path, configs[3].path, configs[4].path],
            )

            self.assertTrue(
                all(type(port) is LegacyExactPort for port in snapshot.legacy_ports)
            )
            self.assertEqual(
                tuple(port.resource_id for port in snapshot.legacy_ports),
                ("legacy.first", "legacy.last"),
            )
            self.assertEqual(
                tuple(port.global_order for port in snapshot.legacy_ports),
                (0, 4),
            )
            self.assertEqual(
                tuple(
                    (port.active, port.lookup, port.update)
                    for port in snapshot.legacy_ports
                ),
                ((True, False, True), (False, True, False)),
            )

            self.assertTrue(
                all(
                    type(port) is CanonicalResourcePort
                    for port in snapshot.canonical_ports
                )
            )
            self.assertEqual(
                tuple(port.resource_id for port in snapshot.canonical_ports),
                ("canonical.first", "canonical.second"),
            )
            self.assertEqual(
                tuple(port.global_order for port in snapshot.canonical_ports),
                (2, 3),
            )
            self.assertEqual(
                tuple(handle.order for handle in snapshot.canonical_handles),
                (0, 1),
            )
            self.assertEqual(
                tuple(handle.resource_id for handle in snapshot.canonical_handles),
                ("canonical.first", "canonical.second"),
            )
            self.assertEqual(
                tuple(
                    (handle.active, handle.lookup, handle.update)
                    for handle in snapshot.canonical_handles
                ),
                ((True, True, False), (False, True, True)),
            )
            self.assertEqual(
                tuple(port.handle for port in snapshot.canonical_ports),
                snapshot.canonical_handles,
            )
            self.assertEqual(snapshot.statuses, ())

    def test_repeated_resolution_is_deterministic_and_returns_frozen_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.before", update=False),
                _config(root, "canonical.middle", lookup=False),
                _config(root, "legacy.after", active=False),
            )
            store = _NoOperationStore("canonical.middle")
            legacy_backends = {
                configs[0].path: _RecordingLegacyBackend(),
                configs[2].path: _RecordingLegacyBackend(),
            }

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                if path != configs[1].path:
                    return _legacy_binding(legacy_backends[path])
                return _canonical_binding(store)

            resolver = TMResourceResolver(
                runtime_open=open_runtime,
            )
            first = resolver.resolve(configs)
            second = resolver.resolve(configs)

            self.assertEqual(first, second)
            self.assertIs(type(first.legacy_ports), tuple)
            self.assertIs(type(first.canonical_ports), tuple)
            self.assertIs(type(first.canonical_handles), tuple)
            self.assertIs(type(first.global_order_by_resource_id), tuple)
            with self.assertRaises(FrozenInstanceError):
                setattr(first, "generation", 1)
            with self.assertRaises(FrozenInstanceError):
                setattr(first.legacy_ports[0], "active", False)
            with self.assertRaises(FrozenInstanceError):
                setattr(first.canonical_ports[0], "global_order", 99)

    def test_fresh_store_instances_preserve_observable_routing_determinism(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.first", lookup=False),
                _config(root, "canonical.middle", update=False),
                _config(root, "legacy.last", active=False),
            )
            opened_stores: list[_NoOperationStore] = []

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                if path != configs[1].path:
                    return _legacy_binding(_RecordingLegacyBackend())
                store = _NoOperationStore("canonical.middle")
                opened_stores.append(store)
                return _canonical_binding(store)

            resolver = TMResourceResolver(
                runtime_open=open_runtime,
            )
            first = resolver.resolve(configs)
            second = resolver.resolve(configs)

            self.assertIsNot(opened_stores[0], opened_stores[1])
            self.assertEqual(_routing_projection(first), _routing_projection(second))

    def test_port_and_snapshot_constructors_reject_contradictory_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_config = _config(root, "legacy.valid")
            canonical_config = _config(root, "canonical.valid")
            store = _NoOperationStore("canonical.valid")
            snapshot = TMResourceResolver(
                runtime_open=lambda path: (
                    _canonical_binding(store)
                    if path == canonical_config.path
                    else _legacy_binding(_RecordingLegacyBackend())
                )
            ).resolve((legacy_config, canonical_config))

            with self.assertRaisesRegex(ValueError, "global order.*complete"):
                TMRuntimeSnapshot(
                    generation=0,
                    legacy_ports=snapshot.legacy_ports,
                    canonical_ports=snapshot.canonical_ports,
                    canonical_handles=snapshot.canonical_handles,
                    global_order_by_resource_id=(("canonical.valid", 0),),
                    statuses=(),
                )

            non_contiguous_handle = TMResourceHandle(
                resource_id="canonical.valid",
                store=cast(TMStore, store),
                active=True,
                lookup=True,
                update=True,
                order=2,
            )
            non_contiguous_port = CanonicalResourcePort(
                resource_name="canonical.valid",
                path=canonical_config.path,
                global_order=1,
                handle=non_contiguous_handle,
            )
            with self.assertRaisesRegex(ValueError, "canonical handle order"):
                TMRuntimeSnapshot(
                    generation=0,
                    legacy_ports=snapshot.legacy_ports,
                    canonical_ports=(non_contiguous_port,),
                    canonical_handles=(non_contiguous_handle,),
                    global_order_by_resource_id=(
                        ("legacy.valid", 0),
                        ("canonical.valid", 1),
                    ),
                    statuses=(),
                )

            foreign_handle = TMResourceHandle(
                resource_id="canonical.valid",
                store=cast(TMStore, _NoOperationStore("canonical.valid")),
                active=True,
                lookup=True,
                update=True,
                order=0,
            )
            with self.assertRaisesRegex(ValueError, "canonical ports and handles"):
                TMRuntimeSnapshot(
                    generation=0,
                    legacy_ports=snapshot.legacy_ports,
                    canonical_ports=snapshot.canonical_ports,
                    canonical_handles=(foreign_handle,),
                    global_order_by_resource_id=snapshot.global_order_by_resource_id,
                    statuses=(),
                )

            with self.assertRaisesRegex(TypeError, "legacy backend"):
                LegacyExactPort(
                    resource_id="legacy.invalid",
                    resource_name="legacy.invalid",
                    path=(root / "legacy.invalid.jsonl").resolve(),
                    global_order=0,
                    active=True,
                    lookup=True,
                    update=True,
                    backend=cast(LegacyPortBackend, object()),
                )

    def test_duplicate_or_foreign_identity_fails_before_publishing_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = _config(root, "duplicate")
            opened: list[Path] = []

            def record_open(path: Path) -> RuntimeOpenBinding:
                opened.append(path)
                return _legacy_binding(_RecordingLegacyBackend())

            resolver = TMResourceResolver(
                runtime_open=record_open,
            )
            with self.assertRaisesRegex(ValueError, "resource ids must be unique"):
                resolver.resolve((duplicate, duplicate))
            self.assertEqual(opened, [])

            canonical = _config(root, "canonical.expected")
            foreign_store = _NoOperationStore("canonical.foreign")
            with self.assertRaisesRegex(ValueError, "canonical resource identity"):
                TMResourceResolver(
                    runtime_open=lambda _path: CanonicalOpenBinding(
                        resource_id="canonical.foreign",
                        store=cast(TMStore, foreign_store),
                    ),
                ).resolve((canonical,))


if __name__ == "__main__":
    unittest.main()
