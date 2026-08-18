"""Task 3.8 TM resource lifecycle, local failure, and snapshot tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from typing import Iterator, NoReturn, Protocol, cast

from editor_contracts import (
    ResourceConfig,
    ResourceKind,
    TMResourceDisplayMode,
    TMResourceStatus,
)
from tm_application_composition import (
    CanonicalOpenBinding,
    LegacyOpenBinding,
    LegacyPortBackend,
    RuntimeOpenBinding,
    TMResourceResolver,
    TMRuntimeHost,
    TMRuntimeSnapshot,
)
from tm_contracts import (
    SourceBindingState,
    StoreHealth,
    TMRecord,
    TMRecordDraft,
    TMStore,
)
from tm_engine import TMMatch
from tm_sqlite_store import SourceBindingObservation
from tests.test_tm_engine_compat import _activate_resource


def _config(root: Path, resource_id: str) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=resource_id,
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=(root / f"{resource_id}.jsonl").resolve(),
        active=True,
        lookup=True,
        update=True,
    )


class _LegacyBackend:
    def __init__(self, target: str) -> None:
        self._target = target

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None:
        del speaker_raw
        return TMMatch(
            source=source,
            target=self._target,
            similarity=1.0,
            match_type="EXACT",
            tm_source="test.jsonl",
        )

    def append(self, draft: TMRecordDraft) -> None:
        del draft


class _StoreShape:
    """Canonical-shaped operation surface shared by lifecycle doubles."""

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        del source_raw
        return ()

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        del record_ids
        return ()

    def append(self, draft: TMRecordDraft) -> TMRecord:
        del draft
        raise AssertionError("append is outside Task 3.8")

    def export_records(self) -> Iterator[TMRecord]:
        return iter(())

    def health(self) -> StoreHealth:
        raise AssertionError("resolver must use the leased health view")


class _BrokenLeaseStore(_StoreShape):
    """Canonical-shaped port whose query-lease precondition fails locally."""

    @property
    def source_binding_monitor(self) -> _SourceMonitor:
        return _SourceMonitor()

    @contextmanager
    def query_lease(self) -> Iterator[object]:
        raise OSError("private body must not escape")
        yield object()


class _SourceMonitor:
    def observe(self) -> SourceBindingObservation:
        return SourceBindingObservation(
            resource_id="canonical.broken",
            canonical_store_id="store.broken",
            generation=0,
            head_revision=0,
            state=SourceBindingState.VERIFIED_CURRENT,
            binding_digest=None,
            diagnostic_codes=(),
        )


class _ObservationPort(Protocol):
    def observe(self) -> SourceBindingObservation: ...


class _SequenceMonitor:
    def __init__(
        self,
        observations: tuple[SourceBindingObservation, ...],
    ) -> None:
        self._observations = iter(observations)

    def observe(self) -> SourceBindingObservation:
        return next(self._observations)


class _ProbeView:
    def __init__(
        self,
        *,
        resource_id: str,
        binding_digest: str | None,
    ) -> None:
        self.resource_id = resource_id
        self.generation = 0
        self._binding_digest = binding_digest

    def health(self) -> StoreHealth:
        return StoreHealth(
            healthy=True,
            schema_version=1,
            generation=0,
            record_count=0,
            index_kind="GRAM_FALLBACK",
            snapshot_binding_digest=self._binding_digest,
            source_binding_state=SourceBindingState.VERIFIED_CURRENT,
            exact_available=True,
            context_available=False,
            fuzzy_available=False,
            diagnostic_codes=(),
        )


class _LifecycleProbeStore(_StoreShape):
    def __init__(
        self,
        *,
        monitor: _ObservationPort,
        health_binding_digest: str | None,
        resource_id: str = "canonical.broken",
    ) -> None:
        self._monitor = monitor
        self._health_binding_digest = health_binding_digest
        self._resource_id = resource_id

    @property
    def source_binding_monitor(self) -> _ObservationPort:
        return self._monitor

    @contextmanager
    def query_lease(self) -> Iterator[_ProbeView]:
        yield _ProbeView(
            resource_id=self._resource_id,
            binding_digest=self._health_binding_digest,
        )


class _SourceMonitorPropertyFailureStore(_StoreShape):
    @property
    def source_binding_monitor(self) -> NoReturn:
        raise OSError("/secret/private/source-monitor")


class _ObservePropertyFailureMonitor:
    @property
    def observe(self) -> NoReturn:
        raise OSError("/secret/private/observe")


class _ObservePropertyFailureStore(_StoreShape):
    @property
    def source_binding_monitor(self) -> _ObservePropertyFailureMonitor:
        return _ObservePropertyFailureMonitor()


class _QueryLeasePropertyFailureStore(_StoreShape):
    @property
    def source_binding_monitor(self) -> _SourceMonitor:
        return _SourceMonitor()

    @property
    def query_lease(self) -> NoReturn:
        raise OSError("/secret/private/query-lease")


class _ProgrammerFailureStore(_StoreShape):
    @property
    def source_binding_monitor(self) -> NoReturn:
        raise TypeError("programmer contract violation")


class _StaticResolver:
    def __init__(self, snapshot: TMRuntimeSnapshot) -> None:
        self._snapshot = snapshot

    def resolve(
        self,
        configs: tuple[ResourceConfig, ...],
    ) -> TMRuntimeSnapshot:
        del configs
        return self._snapshot


def _observation(
    *,
    canonical_store_id: str,
    binding_digest: str | None,
    head_revision: int = 0,
    resource_id: str = "canonical.broken",
    state: SourceBindingState = SourceBindingState.VERIFIED_CURRENT,
) -> SourceBindingObservation:
    return SourceBindingObservation(
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        generation=0,
        head_revision=head_revision,
        state=state,
        binding_digest=binding_digest,
        diagnostic_codes=(),
    )


def _unavailable_status(config: ResourceConfig) -> TMResourceStatus:
    return TMResourceStatus(
        resource_id=config.id,
        resource_name=config.name,
        mode=TMResourceDisplayMode.UNAVAILABLE,
        exact_available=False,
        context_available=False,
        fuzzy_available=False,
        safe_codes=("TM.RUNTIME.TEST_UNAVAILABLE",),
        retryable=False,
    )


def _legacy_binding(target: str) -> LegacyOpenBinding:
    return LegacyOpenBinding(
        backend=cast(LegacyPortBackend, _LegacyBackend(target)),
    )


class TMResourceLifecycleTests(unittest.TestCase):
    def test_dynamic_probe_property_failures_are_resource_local_and_body_safe(
        self,
    ) -> None:
        cases: tuple[tuple[type[_StoreShape], str], ...] = (
            (
                _SourceMonitorPropertyFailureStore,
                "TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE",
            ),
            (
                _ObservePropertyFailureStore,
                "TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE",
            ),
            (
                _QueryLeasePropertyFailureStore,
                "TM.RUNTIME.QUERY_LEASE_UNAVAILABLE",
            ),
        )
        for store_type, expected_code in cases:
            with self.subTest(store=store_type.__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    canonical = _config(root, "canonical.broken")
                    legacy = _config(root, "legacy.good")

                    def open_runtime(path: Path) -> RuntimeOpenBinding:
                        if path == canonical.path:
                            return CanonicalOpenBinding(
                                resource_id=canonical.id,
                                store=cast(TMStore, store_type()),
                            )
                        return _legacy_binding("Legacy survives")

                    snapshot = TMResourceResolver(
                        runtime_open=open_runtime
                    ).resolve((canonical, legacy))

                    self.assertEqual(snapshot.canonical_ports, ())
                    self.assertEqual(
                        tuple(
                            port.resource_id for port in snapshot.legacy_ports
                        ),
                        (legacy.id,),
                    )
                    self.assertEqual(
                        snapshot.statuses[0].safe_codes,
                        (expected_code,),
                    )
                    self.assertNotIn("/secret/private", repr(snapshot.statuses))

    def test_snapshot_constructor_rejects_port_status_contradictions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = _config(root, "legacy.valid")
            second = _config(root, "legacy.second")
            valid = TMResourceResolver(
                runtime_open=lambda _path: _legacy_binding("valid")
            ).resolve((legacy, second))
            unavailable = _unavailable_status(legacy)

            with self.assertRaisesRegex(ValueError, "runtime port status"):
                replace(
                    valid,
                    statuses=(unavailable, valid.statuses[1]),
                )
            with self.assertRaisesRegex(ValueError, "runtime port status"):
                replace(valid, statuses=())
            with self.assertRaisesRegex(ValueError, "resource name"):
                replace(
                    valid,
                    statuses=(
                        replace(valid.statuses[0], resource_name="foreign"),
                        valid.statuses[1],
                    ),
                )
            with self.assertRaisesRegex(ValueError, "legacy exact-only"):
                replace(
                    valid,
                    statuses=(
                        replace(
                            valid.statuses[0],
                            mode=TMResourceDisplayMode.CANONICAL_ACTIVE,
                        ),
                        valid.statuses[1],
                    ),
                )
            with self.assertRaisesRegex(ValueError, "declarative order"):
                replace(valid, statuses=tuple(reversed(valid.statuses)))

            canonical = _config(root, "canonical.valid")
            digest = "a" * 64
            observations = (
                _observation(
                    canonical_store_id="store-1",
                    binding_digest=digest,
                    resource_id=canonical.id,
                ),
                _observation(
                    canonical_store_id="store-1",
                    binding_digest=digest,
                    resource_id=canonical.id,
                ),
            )
            canonical_snapshot = TMResourceResolver(
                runtime_open=lambda _path: CanonicalOpenBinding(
                    resource_id=canonical.id,
                    store=cast(
                        TMStore,
                        _LifecycleProbeStore(
                            monitor=_SequenceMonitor(observations),
                            health_binding_digest=digest,
                            resource_id=canonical.id,
                        ),
                    ),
                )
            ).resolve((canonical,))
            self.assertTrue(canonical_snapshot.statuses[0].exact_available)
            self.assertFalse(canonical_snapshot.statuses[0].context_available)
            self.assertFalse(canonical_snapshot.statuses[0].fuzzy_available)
            with self.assertRaisesRegex(ValueError, "runtime port status"):
                replace(
                    canonical_snapshot,
                    statuses=(_unavailable_status(canonical),),
                )
            canonical_status = canonical_snapshot.statuses[0]
            invalid_canonical_statuses = (
                replace(
                    canonical_status,
                    mode=TMResourceDisplayMode.DEGRADED,
                    safe_codes=("TM.RUNTIME.TEST_DEGRADED",),
                ),
                replace(canonical_status, context_available=True),
                replace(canonical_status, fuzzy_available=True),
                replace(
                    canonical_status,
                    mode=TMResourceDisplayMode.SOURCE_DIVERGED,
                    context_available=True,
                    safe_codes=("TM.RUNTIME.TEST_DIVERGED",),
                ),
                replace(
                    canonical_status,
                    mode=TMResourceDisplayMode.SOURCE_DIVERGED,
                    fuzzy_available=True,
                    safe_codes=("TM.RUNTIME.TEST_DIVERGED",),
                ),
            )
            for invalid_status in invalid_canonical_statuses:
                with self.subTest(
                    mode=invalid_status.mode,
                    context=invalid_status.context_available,
                    fuzzy=invalid_status.fuzzy_available,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "canonical lifecycle",
                    ):
                        replace(
                            canonical_snapshot,
                            statuses=(invalid_status,),
                        )

            host_status = canonical_snapshot.statuses[0]
            object.__setattr__(
                host_status,
                "mode",
                TMResourceDisplayMode.DEGRADED,
            )
            object.__setattr__(
                host_status,
                "safe_codes",
                ("TM.RUNTIME.TEST_DEGRADED",),
            )
            with self.assertRaisesRegex(ValueError, "canonical lifecycle"):
                TMRuntimeHost(
                    resolver=_StaticResolver(canonical_snapshot),
                    configs=(canonical,),
                )

    def test_dynamic_probe_programmer_error_is_not_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = _config(root, "canonical.programmer-error")

            with self.assertRaisesRegex(
                TypeError,
                "programmer contract violation",
            ):
                TMResourceResolver(
                    runtime_open=lambda _path: CanonicalOpenBinding(
                        resource_id=canonical.id,
                        store=cast(TMStore, _ProgrammerFailureStore()),
                    )
                ).resolve((canonical,))

    def test_runtime_host_revalidates_all_nested_frozen_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            legacy = _config(root, "legacy.tampered")
            legacy_snapshot = TMResourceResolver(
                runtime_open=lambda _path: _legacy_binding("legacy")
            ).resolve((legacy,))
            object.__setattr__(
                legacy_snapshot.statuses[0],
                "context_available",
                True,
            )
            with self.subTest(case="legacy-status"):
                with self.assertRaisesRegex(ValueError, "legacy exact-only"):
                    TMRuntimeHost(
                        resolver=_StaticResolver(legacy_snapshot),
                        configs=(legacy,),
                    )

            unavailable = _config(root, "unavailable.tampered")
            unavailable_snapshot = TMResourceResolver().resolve((unavailable,))
            object.__setattr__(
                unavailable_snapshot.statuses[0],
                "exact_available",
                True,
            )
            with self.subTest(case="unavailable-status"):
                with self.assertRaisesRegex(
                    ValueError,
                    "unavailable TM resource",
                ):
                    TMRuntimeHost(
                        resolver=_StaticResolver(unavailable_snapshot),
                        configs=(unavailable,),
                    )

            diverged = _config(root, "canonical.diverged-tampered")
            digest = "d" * 64
            diverged_snapshot = TMResourceResolver(
                runtime_open=lambda _path: CanonicalOpenBinding(
                    resource_id=diverged.id,
                    store=cast(
                        TMStore,
                        _LifecycleProbeStore(
                            monitor=_SequenceMonitor(
                                (
                                    SourceBindingObservation(
                                        resource_id=diverged.id,
                                        canonical_store_id="store-diverged",
                                        generation=0,
                                        head_revision=0,
                                        state=(
                                            SourceBindingState.VERIFIED_CURRENT
                                        ),
                                        binding_digest=digest,
                                        diagnostic_codes=(),
                                    ),
                                    SourceBindingObservation(
                                        resource_id=diverged.id,
                                        canonical_store_id="store-diverged",
                                        generation=0,
                                        head_revision=0,
                                        state=SourceBindingState.SOURCE_DIVERGED,
                                        binding_digest=digest,
                                        diagnostic_codes=(
                                            "SOURCE_BINDING.TEST_DIVERGED",
                                        ),
                                    ),
                                )
                            ),
                            health_binding_digest=digest,
                            resource_id=diverged.id,
                        ),
                    ),
                )
            ).resolve((diverged,))
            self.assertIs(
                diverged_snapshot.statuses[0].mode,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            )
            object.__setattr__(diverged_snapshot.statuses[0], "safe_codes", ())
            with self.subTest(case="diverged-status"):
                with self.assertRaisesRegex(ValueError, "safe code"):
                    TMRuntimeHost(
                        resolver=_StaticResolver(diverged_snapshot),
                        configs=(diverged,),
                    )

            port_config = _config(root, "legacy.port-tampered")
            port_snapshot = TMResourceResolver(
                runtime_open=lambda _path: _legacy_binding("legacy")
            ).resolve((port_config,))
            object.__setattr__(port_snapshot.legacy_ports[0], "backend", object())
            with self.subTest(case="legacy-port"):
                with self.assertRaisesRegex(TypeError, "legacy backend"):
                    TMRuntimeHost(
                        resolver=_StaticResolver(port_snapshot),
                        configs=(port_config,),
                    )

            canonical = _config(root, "canonical.handle-tampered")
            observations = (
                _observation(
                    canonical_store_id="store-handle",
                    binding_digest=digest,
                    resource_id=canonical.id,
                ),
                _observation(
                    canonical_store_id="store-handle",
                    binding_digest=digest,
                    resource_id=canonical.id,
                ),
            )
            canonical_snapshot = TMResourceResolver(
                runtime_open=lambda _path: CanonicalOpenBinding(
                    resource_id=canonical.id,
                    store=cast(
                        TMStore,
                        _LifecycleProbeStore(
                            monitor=_SequenceMonitor(observations),
                            health_binding_digest=digest,
                            resource_id=canonical.id,
                        ),
                    ),
                )
            ).resolve((canonical,))
            object.__setattr__(canonical_snapshot.canonical_handles[0], "store", None)
            with self.subTest(case="canonical-handle"):
                with self.assertRaisesRegex(ValueError, "store binding"):
                    TMRuntimeHost(
                        resolver=_StaticResolver(canonical_snapshot),
                        configs=(canonical,),
                    )

    def test_runtime_host_rejects_snapshot_missing_configured_tm_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = _config(root, "missing.status")
            incomplete = TMRuntimeSnapshot(
                generation=0,
                legacy_ports=(),
                canonical_ports=(),
                canonical_handles=(),
                global_order_by_resource_id=((missing.id, 0),),
                statuses=(),
            )

            with self.assertRaisesRegex(ValueError, "configured TM statuses"):
                TMRuntimeHost(
                    resolver=_StaticResolver(incomplete),
                    configs=(missing,),
                )

            foreign_name = replace(
                incomplete,
                statuses=(
                    replace(
                        _unavailable_status(missing),
                        resource_name="foreign-name",
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, "preserve names"):
                TMRuntimeHost(
                    resolver=_StaticResolver(foreign_name),
                    configs=(missing,),
                )

    def test_source_binding_store_identity_and_digest_drift_fail_closed(
        self,
    ) -> None:
        digest_a = "a" * 64
        digest_b = "b" * 64
        cases = (
            (
                (
                    _observation(
                        canonical_store_id="store-1",
                        binding_digest=digest_a,
                    ),
                    _observation(
                        canonical_store_id="store-2",
                        binding_digest=digest_a,
                    ),
                ),
                digest_a,
            ),
            (
                (
                    _observation(
                        canonical_store_id="store-1",
                        binding_digest=digest_a,
                    ),
                    _observation(
                        canonical_store_id="store-1",
                        binding_digest=digest_b,
                    ),
                ),
                digest_a,
            ),
            (
                (
                    _observation(
                        canonical_store_id="store-1",
                        binding_digest=digest_a,
                    ),
                    _observation(
                        canonical_store_id="store-1",
                        binding_digest=digest_a,
                    ),
                ),
                digest_b,
            ),
        )
        for observations, health_digest in cases:
            with self.subTest(
                stores=tuple(item.canonical_store_id for item in observations),
                health_digest=health_digest,
            ):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    canonical = _config(root, "canonical.broken")
                    legacy = _config(root, "legacy.good")
                    store = _LifecycleProbeStore(
                        monitor=_SequenceMonitor(observations),
                        health_binding_digest=health_digest,
                    )

                    snapshot = TMResourceResolver(
                        runtime_open=lambda path: (
                            CanonicalOpenBinding(
                                resource_id=canonical.id,
                                store=cast(TMStore, store),
                            )
                            if path == canonical.path
                            else _legacy_binding("Legacy survives")
                        )
                    ).resolve((canonical, legacy))

                    self.assertEqual(snapshot.canonical_ports, ())
                    self.assertEqual(
                        tuple(
                            port.resource_id for port in snapshot.legacy_ports
                        ),
                        (legacy.id,),
                    )
                    self.assertEqual(
                        snapshot.statuses[0].safe_codes,
                        ("TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE",),
                    )

    def test_verified_current_history_transitions_remain_canonical_active(
        self,
    ) -> None:
        digest = "c" * 64
        transitions = (
            (
                SourceBindingState.VERIFIED_CURRENT,
                SourceBindingState.VERIFIED_HISTORY,
                0,
                1,
            ),
            (
                SourceBindingState.VERIFIED_HISTORY,
                SourceBindingState.VERIFIED_CURRENT,
                1,
                2,
            ),
        )
        for before, after, before_revision, after_revision in transitions:
            with self.subTest(before=before, after=after):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    canonical = _config(root, "canonical.concurrent")
                    observations = (
                        _observation(
                            canonical_store_id="store-concurrent",
                            binding_digest=digest,
                            head_revision=before_revision,
                            resource_id=canonical.id,
                            state=before,
                        ),
                        _observation(
                            canonical_store_id="store-concurrent",
                            binding_digest=digest,
                            head_revision=after_revision,
                            resource_id=canonical.id,
                            state=after,
                        ),
                    )
                    snapshot = TMResourceResolver(
                        runtime_open=lambda _path: CanonicalOpenBinding(
                            resource_id=canonical.id,
                            store=cast(
                                TMStore,
                                _LifecycleProbeStore(
                                    monitor=_SequenceMonitor(observations),
                                    health_binding_digest=digest,
                                    resource_id=canonical.id,
                                ),
                            ),
                        )
                    ).resolve((canonical,))

                    self.assertEqual(len(snapshot.canonical_ports), 1)
                    self.assertIs(
                        snapshot.statuses[0].mode,
                        TMResourceDisplayMode.CANONICAL_ACTIVE,
                    )

    def test_source_diverged_to_verified_transition_remains_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = _config(root, "canonical.diverged-reversal")
            digest = "d" * 64
            observations = (
                _observation(
                    canonical_store_id="store-diverged",
                    binding_digest=digest,
                    resource_id=canonical.id,
                    state=SourceBindingState.SOURCE_DIVERGED,
                ),
                _observation(
                    canonical_store_id="store-diverged",
                    binding_digest=digest,
                    head_revision=1,
                    resource_id=canonical.id,
                    state=SourceBindingState.VERIFIED_CURRENT,
                ),
            )
            snapshot = TMResourceResolver(
                runtime_open=lambda _path: CanonicalOpenBinding(
                    resource_id=canonical.id,
                    store=cast(
                        TMStore,
                        _LifecycleProbeStore(
                            monitor=_SequenceMonitor(observations),
                            health_binding_digest=digest,
                            resource_id=canonical.id,
                        ),
                    ),
                )
            ).resolve((canonical,))

            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(
                snapshot.statuses[0].safe_codes,
                ("TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE",),
            )

    def test_missing_path_is_local_unavailable_and_other_legacy_survives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = _config(root, "legacy.missing")
            healthy = _config(root, "legacy.healthy")
            healthy.path.write_text(
                '{"source":"Source","target":"Healthy"}\n',
                encoding="utf-8",
            )

            snapshot = TMResourceResolver().resolve((missing, healthy))

            self.assertEqual(
                tuple(port.resource_id for port in snapshot.legacy_ports),
                ("legacy.healthy",),
            )
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(
                tuple(status.mode for status in snapshot.statuses),
                (
                    TMResourceDisplayMode.UNAVAILABLE,
                    TMResourceDisplayMode.LEGACY_EXACT_ONLY,
                ),
            )
            self.assertEqual(
                snapshot.statuses[0].safe_codes,
                ("TM.RUNTIME.PATH_UNAVAILABLE",),
            )
            self.assertTrue(snapshot.statuses[0].retryable)
            result = snapshot.legacy_ports[0].query_exact("Source", None)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.target, "Healthy")

    def test_present_but_ambiguous_activation_facts_never_fall_back_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "ambiguous")
            original = b'{"source":"Source","target":"Legacy"}\n'
            config.path.write_bytes(original)
            config.path.with_name(
                f"{config.path.name}.localcat-snapshot.json"
            ).write_text("{}", encoding="utf-8")

            snapshot = TMResourceResolver().resolve((config,))

            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(len(snapshot.statuses), 1)
            status = snapshot.statuses[0]
            self.assertIs(status.mode, TMResourceDisplayMode.UNAVAILABLE)
            self.assertEqual(
                status.safe_codes,
                ("TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE",),
            )
            self.assertFalse(status.retryable)
            self.assertEqual(config.path.read_bytes(), original)

    def test_corrupt_activated_sidecar_is_unavailable_never_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _activate_resource(root)
            config = ResourceConfig(
                id="tm.primary",
                name="Primary TM",
                kind=ResourceKind.TRANSLATION_MEMORY,
                path=path,
                active=True,
                lookup=True,
                update=True,
            )
            original = path.read_bytes()
            path.with_name(f"{path.name}.sqlite3").write_bytes(b"corrupt")

            snapshot = TMResourceResolver().resolve((config,))

            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertIs(
                snapshot.statuses[0].mode,
                TMResourceDisplayMode.UNAVAILABLE,
            )
            self.assertEqual(
                snapshot.statuses[0].safe_codes,
                ("TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE",),
            )
            self.assertEqual(path.read_bytes(), original)

    def test_real_activated_source_divergence_keeps_canonical_lkg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _activate_resource(root)
            config = ResourceConfig(
                id="tm.primary",
                name="Primary TM",
                kind=ResourceKind.TRANSLATION_MEMORY,
                path=path,
                active=True,
                lookup=True,
                update=True,
            )
            external = b'{"source":"external","target":"changed"}\n'
            path.write_bytes(external)

            snapshot = TMResourceResolver().resolve((config,))

            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(len(snapshot.canonical_ports), 1)
            status = snapshot.statuses[0]
            self.assertIs(status.mode, TMResourceDisplayMode.SOURCE_DIVERGED)
            self.assertTrue(status.exact_available)
            self.assertFalse(status.context_available)
            self.assertFalse(status.fuzzy_available)
            self.assertEqual(
                status.safe_codes,
                ("TM.RUNTIME.SOURCE_DIVERGED",),
            )
            self.assertEqual(path.read_bytes(), external)

    def test_query_lease_failure_is_local_and_does_not_hide_legacy_port(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = _config(root, "canonical.broken")
            legacy = _config(root, "legacy.good")

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                if path == canonical.path:
                    return CanonicalOpenBinding(
                        resource_id=canonical.id,
                        store=cast(TMStore, _BrokenLeaseStore()),
                    )
                return _legacy_binding("Legacy survives")

            snapshot = TMResourceResolver(runtime_open=open_runtime).resolve(
                (canonical, legacy)
            )

            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(
                tuple(port.resource_id for port in snapshot.legacy_ports),
                (legacy.id,),
            )
            self.assertEqual(
                snapshot.statuses[0].safe_codes,
                ("TM.RUNTIME.QUERY_LEASE_UNAVAILABLE",),
            )
            self.assertNotIn("private body", repr(snapshot.statuses))

    def test_refresh_publishes_only_complete_snapshot_and_old_capture_lives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_config = _config(root, "legacy.old")
            new_first = _config(root, "legacy.new-first")
            new_second = _config(root, "legacy.new-second")
            release = threading.Event()
            entered = threading.Event()

            def open_runtime(path: Path) -> RuntimeOpenBinding:
                if path == new_second.path:
                    entered.set()
                    self.assertTrue(release.wait(timeout=5.0))
                return _legacy_binding(path.stem)

            resolver = TMResourceResolver(runtime_open=open_runtime)
            host = TMRuntimeHost(resolver=resolver, configs=(old_config,))
            old_capture = host.snapshot()
            result: list[object] = []

            worker = threading.Thread(
                target=lambda: result.append(
                    host.refresh((new_first, new_second))
                ),
                daemon=True,
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=5.0))

            during = host.snapshot()
            self.assertIs(during, old_capture)
            self.assertEqual(during.generation, 0)
            self.assertEqual(
                tuple(port.resource_id for port in during.legacy_ports),
                (old_config.id,),
            )

            release.set()
            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            published = host.snapshot()
            self.assertIs(result[0], published)
            self.assertEqual(published.generation, 1)
            self.assertEqual(
                tuple(port.resource_id for port in published.legacy_ports),
                (new_first.id, new_second.id),
            )
            old_result = old_capture.legacy_ports[0].query_exact(
                "still-old",
                None,
            )
            self.assertIsNotNone(old_result)
            assert old_result is not None
            self.assertEqual(old_result.target, old_config.path.stem)

    def test_failed_refresh_preserves_old_snapshot_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.stable")
            host = TMRuntimeHost(
                resolver=TMResourceResolver(
                    runtime_open=lambda _path: _legacy_binding("stable")
                ),
                configs=(config,),
            )
            old = host.snapshot()

            with self.assertRaisesRegex(ValueError, "resource ids must be unique"):
                host.refresh((config, config))

            self.assertIs(host.snapshot(), old)
            self.assertEqual(host.snapshot().generation, 0)

    def test_refresh_recovers_local_missing_path_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.restored")
            host = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=(config,),
            )
            self.assertIs(
                host.snapshot().statuses[0].mode,
                TMResourceDisplayMode.UNAVAILABLE,
            )

            config.path.write_text(
                '{"source":"Source","target":"Restored"}\n',
                encoding="utf-8",
            )
            refreshed = host.refresh((config,))

            self.assertEqual(refreshed.generation, 1)
            self.assertEqual(len(refreshed.legacy_ports), 1)
            self.assertIs(
                refreshed.statuses[0].mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )


if __name__ == "__main__":
    unittest.main()
