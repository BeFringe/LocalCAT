"""Task 7.4 capability, resource, and activation failure acceptance.

The matrix deliberately crosses the application boundaries used by the Qt
editor.  Canonical resources are built through ``activate_initial()`` and
cold-reopened by the production resolver; capability transitions use the
composition-owned public Gate C/D ports; final user-visible classification is
checked through the Qt suggestion-state mapper.  No test-side publisher,
scorer, or legacy fallback is used as acceptance authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tm_retrieval_validation as retrieval_validation_module
from capability_host import CapabilityHost, CapabilityHostComposition
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMPreferences,
    TMResourceDisplayMode,
    TMSuggestionReport,
)
from editor_tm_adapter import EditorTMAdapter
from qt_editor_window import QtEditorWindow
from tests.feature5_ui_canonical_fixture import (
    QUERY_SOURCE,
    ActivatedCanonicalFixture,
    build_activated_canonical_fixture,
)
from tests.test_capability_host_gate_c import (
    _EVALUATED_AT,
    _GENERATED_AT,
    _VALID_UNTIL,
)
from tests.test_capability_host_gate_d import (
    _FakeGateDExecution,
    _composition as _gate_d_composition,
    _gate_c,
    _gate_d_owner,
)
from tests.test_editor_controller_tm_activation_completion import (
    _fixture as _activation_fixture,
)
from tests.test_editor_tm_adapter_mixed import (
    _open_capability as _open_validated_capability,
)
from tests.test_tm_initial_activation_recovery import (
    _ambiguous_failure,
    _legacy_failure,
)
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import TMMatchType
from tm_migration import TMMigrationService


_GATE_TIME = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _canonical_configs(
    fixture: ActivatedCanonicalFixture,
) -> tuple[ResourceConfig, ...]:
    return tuple(
        ResourceConfig(
            id=resource.resource_id,
            name=resource.resource_id,
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=resource.identity.configured_jsonl_path,
            active=True,
            lookup=True,
            update=False,
        )
        for resource in fixture.resources
    )


def _legacy_config(
    path: Path,
    *,
    resource_id: str,
) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=resource_id,
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=path.resolve(),
        active=True,
        lookup=True,
        update=False,
    )


def _adapter(
    configs: tuple[ResourceConfig, ...],
    *,
    capability_host: CapabilityHost,
) -> tuple[EditorTMAdapter, TMRuntimeHost]:
    runtime = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=configs,
    )
    return (
        EditorTMAdapter(
            runtime_host=runtime,
            capability_host=capability_host,
        ),
        runtime,
    )


def _query(adapter: EditorTMAdapter, *, epoch: int = 74) -> TMSuggestionReport:
    return adapter.query_current(
        segment=EditorSegment(
            id="segment-7.4",
            source=QUERY_SOURCE,
            speaker="Narrator",
        ),
        project_session_id="task-7.4-session",
        query_epoch=epoch,
        preferences=TMPreferences(minimum_similarity=0.60),
    )


def _assert_not_no_match(
    test_case: unittest.TestCase,
    report: TMSuggestionReport,
) -> str:
    message = QtEditorWindow._tm_state_message(
        report=report,
        query_failed=False,
        has_suggestions=bool(report.suggestions),
    )
    test_case.assertIsNotNone(message)
    assert message is not None
    test_case.assertNotIn("暂无", message)
    return message


def _gate_c_owner(composition: CapabilityHostComposition) -> Any:
    owner = composition.retrieval_gate_c_validation_owner
    if owner is None:
        raise AssertionError("Task 7.4 requires the production Gate C owner")
    return cast(Any, owner)


class Feature5UICapabilityFailureMatrixTests(unittest.TestCase):
    def test_closed_expired_and_foreign_gate_c_recover_without_false_matches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            configs = _canonical_configs(fixture)

            for failure_kind in ("expired", "foreign", "operational"):
                with self.subTest(failure=failure_kind):
                    execution = _FakeGateDExecution()
                    composition = _gate_d_composition(self, execution)
                    owner = _gate_c_owner(composition)
                    closed_handoff = composition.host.retrieval_snapshot()

                    if failure_kind == "expired":
                        rejected = owner.validate_gate_c(
                            generated_at_utc=_GENERATED_AT,
                            valid_until_utc=_VALID_UNTIL,
                            evaluated_at_utc=_VALID_UNTIL,
                        )
                    elif failure_kind == "foreign":
                        with patch.object(
                            retrieval_validation_module,
                            "recompute_retrieval_validation",
                            side_effect=AssertionError(
                                "foreign evidence must not enter composition"
                            ),
                        ) as foreign:
                            rejected = owner.validate_gate_c(
                                generated_at_utc=_GENERATED_AT,
                                valid_until_utc=_VALID_UNTIL,
                                evaluated_at_utc=_EVALUATED_AT,
                            )
                        foreign.assert_not_called()
                    else:
                        binding = cast(
                            Any,
                            owner,
                        )._RetrievalGateCValidationOwner__validation_binding
                        with patch.object(
                            type(binding),
                            "recompute",
                            side_effect=OSError(
                                "/private/gate-c/body must stay hidden"
                            ),
                        ):
                            rejected = owner.validate_gate_c(
                                generated_at_utc=_GENERATED_AT,
                                valid_until_utc=_VALID_UNTIL,
                                evaluated_at_utc=_EVALUATED_AT,
                            )

                    self.assertIs(rejected, closed_handoff)
                    adapter, _runtime = _adapter(
                        configs,
                        capability_host=composition.host,
                    )
                    closed = _query(adapter)
                    self.assertEqual(
                        {item.match_type for item in closed.suggestions},
                        {TMMatchType.EXACT},
                    )
                    self.assertFalse(closed.retrieval_status.context_available)
                    self.assertFalse(closed.retrieval_status.fuzzy_available)
                    # A rejected expired/foreign/failed Gate C candidate is
                    # never published.  The consumer therefore remains on the
                    # prior formal sentinel rather than projecting diagnostics
                    # from untrusted evidence.
                    self.assertEqual(
                        closed.retrieval_status.safe_codes,
                        (
                            "RETRIEVAL.CONTEXT_EVIDENCE_MISSING",
                            "RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",
                            "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING",
                        ),
                    )
                    self.assertNotIn("private/gate-c", repr(closed))
                    self.assertIn("匹配能力", _assert_not_no_match(self, closed))

                    recovered_handoff = owner.validate_gate_c(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                    self.assertGreater(
                        recovered_handoff.generation,
                        closed_handoff.generation,
                    )
                    recovered = _query(adapter, epoch=75)
                    self.assertTrue(recovered.retrieval_status.context_available)
                    self.assertFalse(recovered.retrieval_status.fuzzy_available)
                    self.assertEqual(
                        {item.match_type for item in recovered.suggestions},
                        {TMMatchType.EXACT, TMMatchType.CONTEXT},
                    )

    def test_gate_d_failure_preserves_context_then_recovery_opens_fuzzy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            execution = _FakeGateDExecution(
                outcome="error",
                safe_code="GATE_D.CLEANUP_PENDING",
            )
            composition = _gate_d_composition(self, execution)
            gate_c = _gate_c(composition)
            adapter, _runtime = _adapter(
                _canonical_configs(fixture),
                capability_host=composition.host,
            )
            owner = _gate_d_owner(composition)

            _ = owner.start_gate_d(evaluated_at_utc=_GATE_TIME)
            failed = owner.wait(timeout=10.0)

            self.assertEqual(failed.state.value, "FAILED")
            self.assertEqual(failed.safe_code, "GATE_D.CLEANUP_PENDING")
            self.assertEqual(owner.status(), failed)
            after_failure = _query(adapter)
            self.assertEqual(
                {item.match_type for item in after_failure.suggestions},
                {TMMatchType.EXACT, TMMatchType.CONTEXT},
            )
            self.assertTrue(after_failure.retrieval_status.context_available)
            self.assertFalse(after_failure.retrieval_status.fuzzy_available)
            # Gate D's operational result stays visible on its owner status;
            # retrieval consumers keep the last formally published Gate C
            # graph and its benchmark-missing projection.
            self.assertEqual(
                after_failure.retrieval_status.safe_codes,
                ("RETRIEVAL.FUZZY_BENCHMARK_EVIDENCE_MISSING",),
            )
            self.assertIs(composition.host.retrieval_snapshot(), gate_c)
            self.assertIn(
                "匹配能力",
                _assert_not_no_match(self, after_failure),
            )

            execution.outcome = "success"
            execution.safe_code = None
            _ = owner.start_gate_d(evaluated_at_utc=_GATE_TIME)
            recovered_status = owner.wait(timeout=10.0)
            recovered = _query(adapter, epoch=75)

            self.assertEqual(recovered_status.state.value, "SUCCEEDED")
            self.assertIsNone(recovered_status.safe_code)
            self.assertTrue(recovered.retrieval_status.context_available)
            self.assertTrue(recovered.retrieval_status.fuzzy_available)
            self.assertEqual(recovered.retrieval_status.safe_codes, ())
            self.assertEqual(
                {item.match_type for item in recovered.suggestions},
                {
                    TMMatchType.EXACT,
                    TMMatchType.CONTEXT,
                    TMMatchType.FUZZY,
                },
            )


class Feature5UIResourceFailureMatrixTests(unittest.TestCase):
    def test_path_and_query_failures_remain_local_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            canonical = _canonical_configs(fixture)
            missing = _legacy_config(
                root / "missing.jsonl",
                resource_id="tm.legacy.missing",
            )
            failing_path = (root / "legacy-query.jsonl").resolve()
            failing_path.write_text(
                json.dumps({"source": QUERY_SOURCE, "target": "legacy"})
                + "\n",
                encoding="utf-8",
            )
            failing = _legacy_config(
                failing_path,
                resource_id="tm.legacy.query-failure",
            )
            capability = _open_validated_capability(self)
            adapter, runtime = _adapter(
                (missing, failing, canonical[0], canonical[1]),
                capability_host=capability.host,
            )

            primary_sidecar = fixture.resources[0].identity.canonical_sidecar_path
            primary_sidecar.write_bytes(b"corrupt after runtime capture")
            backend = runtime.snapshot().legacy_ports[0].backend
            with patch.object(
                backend,
                "query_exact",
                side_effect=OSError("/private/path/body must stay hidden"),
            ):
                first = _query(adapter)
                repeated = _query(adapter, epoch=75)

            self.assertEqual(
                tuple(
                    (status.resource_id, status.mode, status.safe_codes)
                    for status in first.resource_statuses
                ),
                (
                    (
                        "tm.legacy.missing",
                        TMResourceDisplayMode.UNAVAILABLE,
                        ("TM.RUNTIME.PATH_UNAVAILABLE",),
                    ),
                    (
                        "tm.legacy.query-failure",
                        TMResourceDisplayMode.DEGRADED,
                        ("DIRECT", "TM.LEGACY.QUERY_FAILED"),
                    ),
                    (
                        "tm.fixture.primary",
                        TMResourceDisplayMode.DEGRADED,
                        ("HEALTH", "RETRIEVAL.QUERY_FAILED"),
                    ),
                    (
                        "tm.fixture.secondary",
                        TMResourceDisplayMode.CANONICAL_ACTIVE,
                        (),
                    ),
                ),
            )
            self.assertEqual(
                first.resource_statuses,
                repeated.resource_statuses,
            )
            self.assertTrue(first.suggestions)
            self.assertEqual(
                {item.resource_id for item in first.suggestions},
                {"tm.fixture.secondary"},
            )
            self.assertEqual(
                {item.match_type for item in first.suggestions},
                {
                    TMMatchType.EXACT,
                    TMMatchType.CONTEXT,
                    TMMatchType.FUZZY,
                },
            )
            self.assertNotIn("private/path", repr(first))
            self.assertIn(
                "资源查询失败",
                _assert_not_no_match(self, first),
            )

    def test_canonical_reopen_failure_never_falls_back_to_legacy_jsonl(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            canonical = _canonical_configs(fixture)[0]
            canonical_source = canonical.path.read_bytes()
            fixture.resources[0].identity.canonical_sidecar_path.write_bytes(
                b"corrupt before cold reopen"
            )
            healthy_path = (root / "healthy-legacy.jsonl").resolve()
            healthy_path.write_text(
                json.dumps(
                    {"source": QUERY_SOURCE, "target": "healthy legacy"}
                )
                + "\n",
                encoding="utf-8",
            )
            healthy = _legacy_config(
                healthy_path,
                resource_id="tm.legacy.healthy",
            )
            capability = _open_validated_capability(self)
            adapter, runtime = _adapter(
                (canonical, healthy),
                capability_host=capability.host,
            )

            report = _query(adapter)
            snapshot = runtime.snapshot()

            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(
                tuple(port.resource_id for port in snapshot.legacy_ports),
                ("tm.legacy.healthy",),
            )
            self.assertEqual(
                tuple(
                    (status.resource_id, status.mode, status.safe_codes)
                    for status in report.resource_statuses
                ),
                (
                    (
                        canonical.id,
                        TMResourceDisplayMode.UNAVAILABLE,
                        ("TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE",),
                    ),
                    (
                        healthy.id,
                        TMResourceDisplayMode.LEGACY_EXACT_ONLY,
                        (),
                    ),
                ),
            )
            self.assertEqual(
                tuple(
                    (item.resource_id, item.target)
                    for item in report.suggestions
                ),
                ((healthy.id, "healthy legacy"),),
            )
            self.assertEqual(canonical.path.read_bytes(), canonical_source)
            self.assertIn(
                "资源当前不可用",
                _assert_not_no_match(self, report),
            )


class Feature5UIActivationFailureMatrixTests(unittest.TestCase):
    def test_proven_first_failure_preserves_legacy_and_safe_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            controller, runtime, _repository, resource_id = _activation_fixture(
                root
            )
            preflight = controller.prepare_tm_activation(resource_id)
            with patch.object(
                TMMigrationService,
                "activate_initial",
                autospec=True,
                return_value=_legacy_failure(),
            ):
                started = controller.activate_tm_resource(preflight)
                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=10.0,
                )

            report = controller.tm_suggestion_report()
            snapshot = runtime.snapshot()
            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "MIGRATION.INITIAL_IO_FAILED",
            )
            self.assertEqual(controller.tm_activation_operation(), completed)
            self.assertEqual(len(snapshot.legacy_ports), 1)
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(report.suggestions[0].target, "你好")
            self.assertEqual(
                report.suggestions[0].provenance.resource_mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )
            self.assertIn("匹配能力", _assert_not_no_match(self, report))

    def test_ambiguous_first_activation_is_unavailable_not_no_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ambiguous = [False]
            controller, runtime, _repository, resource_id = _activation_fixture(
                root,
                ambiguous_after_activation=ambiguous,
            )
            preflight = controller.prepare_tm_activation(resource_id)

            def ambiguous_outcome(*_args: object, **_kwargs: object) -> object:
                ambiguous[0] = True
                return _ambiguous_failure()

            with patch.object(
                TMMigrationService,
                "activate_initial",
                autospec=True,
                side_effect=ambiguous_outcome,
            ):
                started = controller.activate_tm_resource(preflight)
                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=10.0,
                )

            report = controller.tm_suggestion_report()
            snapshot = runtime.snapshot()
            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertEqual(controller.tm_activation_operation(), completed)
            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(report.suggestions, ())
            self.assertEqual(
                report.resource_statuses[0].mode,
                TMResourceDisplayMode.UNAVAILABLE,
            )
            self.assertEqual(
                report.resource_statuses[0].safe_codes,
                ("TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE",),
            )
            self.assertIn(
                "资源当前不可用",
                _assert_not_no_match(self, report),
            )

    def test_diverged_update_failure_keeps_canonical_lkg_not_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            controller, runtime, repository, resource_id = _activation_fixture(
                root
            )
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            activated = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )
            self.assertTrue(activated.succeeded)
            resource = repository.get(resource_id)
            external = (
                json.dumps(
                    {
                        "source": "Hello",
                        "target": "forbidden legacy fallback",
                    }
                )
                + "\n"
            ).encode()
            resource.path.write_bytes(external)

            with patch.object(
                TMMigrationService,
                "_build_stage",
                autospec=True,
                side_effect=OSError("private rebuild body"),
            ):
                rebuild = controller.rebuild_tm_resource(resource_id)
                completed = controller.wait_tm_activation(
                    rebuild.operation_id,
                    timeout=20.0,
                )

            report = controller.tm_suggestion_report()
            snapshot = runtime.snapshot()
            self.assertFalse(completed.succeeded)
            self.assertEqual(completed.safe_code, "IMPORT.FAILED")
            self.assertEqual(controller.tm_activation_operation(), completed)
            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(len(snapshot.canonical_ports), 1)
            self.assertEqual(
                report.resource_statuses[0].mode,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            )
            self.assertEqual(
                report.resource_statuses[0].safe_codes,
                (
                    "TM.RUNTIME.SOURCE_DIVERGED",
                    "RETRIEVAL.CONTEXT_EVIDENCE_MISSING",
                    "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING",
                ),
            )
            self.assertEqual(report.suggestions[0].target, "你好")
            self.assertNotEqual(
                report.suggestions[0].target,
                "forbidden legacy fallback",
            )
            self.assertEqual(resource.path.read_bytes(), external)
            self.assertNotIn("private rebuild body", repr(report))
            self.assertIn(
                "资源当前不可用",
                _assert_not_no_match(self, report),
            )


if __name__ == "__main__":
    unittest.main()
