"""Task 5.5 activation completion and atomic runtime replacement tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    ResourceConfig,
    TMResourceDisplayMode,
    TMSuggestionReport,
)
import editor_controller as controller_module
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
import tm_application_composition as composition_module
from tm_application_composition import (
    TMResourceResolver,
    TMRuntimeHost,
    TMRuntimeSnapshot,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
)
import tm_migration
from tm_migration import TMMigrationService
from tests.test_tm_initial_activation_recovery import (
    _ambiguous_failure,
    _legacy_failure,
    _published_failure,
)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _fixture(
    root: Path,
    *,
    ambiguous_after_activation: list[bool] | None = None,
) -> tuple[
    EditorController,
    TMRuntimeHost,
    ResourceRepository,
    str,
]:
    repository = ResourceRepository(root / "app-data")
    resource = repository.create_resource(
        "Legacy TM",
        ResourceKind.TRANSLATION_MEMORY,
    )
    resource.path.write_text(
        json.dumps({"source": "Hello", "target": "你好"}) + "\n",
        encoding="utf-8",
    )

    if ambiguous_after_activation is None:
        resolver = TMResourceResolver()
    else:
        def runtime_open(path: Path):  # type: ignore[no-untyped-def]
            if ambiguous_after_activation[0]:
                raise ValueError("TM.CANONICAL_ACTIVATION_AMBIGUOUS")
            return composition_module._open_runtime_binding(path)

        resolver = TMResourceResolver(runtime_open=runtime_open)
    runtime = TMRuntimeHost(
        resolver=resolver,
        configs=repository.list_resources(),
    )
    adapter = EditorTMAdapter(
        runtime_host=runtime,
        capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(
        EditorProject(
            name="Activation completion",
            segments=(
                EditorSegment(
                    id="segment-1",
                    source="Hello",
                    target="draft",
                ),
            ),
        )
    )
    return controller, runtime, repository, resource.id


class EditorControllerTMActivationCompletionTests(unittest.TestCase):
    def test_query_waits_until_runtime_and_epoch_publish_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, _repository, resource_id = _fixture(root)
            before_epoch = controller.tm_suggestion_report().query_identity.query_epoch
            preflight = controller.prepare_tm_activation(resource_id)
            refresh_entered = Event()
            refresh_release = Event()
            query_done = Event()
            query_reports: list[TMSuggestionReport] = []
            query_errors: list[BaseException] = []
            original_refresh = EditorTMAdapter._refresh_runtime_after_activation

            def block_runtime_refresh(
                adapter: EditorTMAdapter,
                configs: tuple[ResourceConfig, ...],
                validate_candidate: Callable[[TMRuntimeSnapshot], None],
            ) -> TMRuntimeSnapshot:
                refresh_entered.set()
                if not refresh_release.wait(5.0):
                    raise AssertionError("activation runtime refresh was not released")
                return original_refresh(
                    adapter,
                    configs,
                    validate_candidate,
                )

            def query_current() -> None:
                try:
                    query_reports.append(controller.tm_suggestion_report())
                except BaseException as error:
                    query_errors.append(error)
                finally:
                    query_done.set()

            with patch.object(
                EditorTMAdapter,
                "_refresh_runtime_after_activation",
                autospec=True,
                side_effect=block_runtime_refresh,
            ):
                started = controller.activate_tm_resource(preflight)
                self.assertTrue(refresh_entered.wait(10.0))
                query_thread = Thread(target=query_current, daemon=True)
                query_thread.start()
                try:
                    self.assertFalse(query_done.wait(0.1))
                finally:
                    refresh_release.set()
                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                )
                query_thread.join(10.0)

            self.assertTrue(completed.succeeded)
            self.assertTrue(query_done.is_set())
            self.assertEqual(query_errors, [])
            self.assertEqual(len(query_reports), 1)
            report = query_reports[0]
            report.__post_init__()
            self.assertGreater(
                report.query_identity.query_epoch,
                before_epoch,
            )
            self.assertEqual(
                report.suggestions[0].provenance.resource_mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertEqual(runtime.snapshot().generation, 1)

    def test_real_success_replaces_runtime_then_next_query_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, _repository, resource_id = _fixture(root)
            before_report = controller.tm_suggestion_report()
            before_epoch = before_report.query_identity.query_epoch
            self.assertEqual(
                before_report.suggestions[0].provenance.resource_mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )
            preflight = controller.prepare_tm_activation(resource_id)

            started = controller.activate_tm_resource(preflight)
            completed = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )

            snapshot = runtime.capture_operation_snapshot()
            self.assertTrue(completed.succeeded)
            self.assertEqual(snapshot.generation, 1)
            self.assertEqual(len(snapshot.legacy_ports), 0)
            self.assertEqual(len(snapshot.canonical_ports), 1)
            status = snapshot.statuses[0]
            self.assertEqual(status.mode, TMResourceDisplayMode.CANONICAL_ACTIVE)
            self.assertTrue(status.exact_available)
            self.assertFalse(status.context_available)
            self.assertFalse(status.fuzzy_available)

            issued_after_completion = controller.issued_tm_suggestions
            self.assertEqual(len(issued_after_completion), 1)
            self.assertEqual(
                issued_after_completion[0].provenance.resource_mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertIsNotNone(
                controller._tm_engines[resource_id].canonical_store
            )

            after_report = controller.tm_suggestion_report()
            self.assertGreater(
                after_report.query_identity.query_epoch,
                before_epoch,
            )
            self.assertEqual(
                after_report.suggestions[0].provenance.resource_mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertEqual(after_report.suggestions[0].target, "你好")

    def test_orphan_stage_activation_keeps_peer_legacy_and_cohort_queryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            orphaned = repository.create_resource(
                "Orphaned legacy TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            peer = repository.create_resource(
                "Healthy legacy TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            orphaned.path.write_text(
                json.dumps({"source": "Hello", "target": "orphan target"})
                + "\n",
                encoding="utf-8",
            )
            peer.path.write_text(
                json.dumps({"source": "Hello", "target": "peer target"})
                + "\n",
                encoding="utf-8",
            )
            identity = CanonicalResourceIdentity.from_configured_jsonl(
                orphaned.id,
                orphaned.path,
            )
            residue = tm_migration._deterministic_stage_ref(
                identity,
                source_digest=hashlib.sha256(orphaned.path.read_bytes()).hexdigest(),
                stage_prefix="migration",
                path_salt=f"initial-{'b' * 32}",
            ).staged_db_path
            residue.write_bytes(b"unpublished sqlite stage")
            residue_journal = Path(f"{residue}-journal")
            residue_journal.write_bytes(b"unpublished sqlite hot journal")
            residue_before = residue.read_bytes()
            journal_before = residue_journal.read_bytes()

            runtime = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=repository.list_resources(),
            )
            adapter = EditorTMAdapter(
                runtime_host=runtime,
                capability_host=CapabilityHost(
                    evaluated_at_utc=_EVALUATED_AT
                ),
            )
            controller = EditorController(repository, tm_adapter=adapter)
            controller.set_project(
                EditorProject(
                    name="Orphan recovery",
                    segments=(
                        EditorSegment(
                            id="segment-1",
                            source="Hello",
                            target="draft",
                        ),
                    ),
                )
            )
            before = runtime.capture_operation_snapshot()
            self.assertEqual(len(before.legacy_ports), 2)

            preflight = controller.prepare_tm_activation(orphaned.id)
            started = controller.activate_tm_resource(preflight)
            completed = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )

            self.assertTrue(completed.succeeded)
            self.assertIsNone(completed.safe_code)
            after = runtime.capture_operation_snapshot()
            modes = {
                status.resource_id: status.mode for status in after.statuses
            }
            self.assertEqual(
                modes,
                {
                    orphaned.id: TMResourceDisplayMode.CANONICAL_ACTIVE,
                    peer.id: TMResourceDisplayMode.LEGACY_EXACT_ONLY,
                },
            )
            self.assertEqual(len(after.canonical_ports), 1)
            self.assertEqual(len(after.legacy_ports), 1)
            self.assertIsNone(controller._tm_runtime_blocked_safe_code)
            self.assertGreaterEqual(
                len(controller.tm_suggestion_report().suggestions),
                1,
            )
            self.assertEqual(residue.read_bytes(), residue_before)
            self.assertEqual(residue_journal.read_bytes(), journal_before)

            cold_runtime = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=repository.list_resources(),
            )
            cold = cold_runtime.capture_operation_snapshot()
            cold_modes = {
                status.resource_id: status.mode for status in cold.statuses
            }
            self.assertEqual(cold_modes, modes)
            self.assertEqual(residue.read_bytes(), residue_before)
            self.assertEqual(residue_journal.read_bytes(), journal_before)

    def test_proven_first_failure_rebuilds_and_preserves_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, _repository, resource_id = _fixture(root)
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

            snapshot = runtime.capture_operation_snapshot()
            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "MIGRATION.INITIAL_IO_FAILED",
            )
            self.assertEqual(snapshot.generation, 1)
            self.assertEqual(len(snapshot.legacy_ports), 1)
            self.assertEqual(len(snapshot.canonical_ports), 0)
            self.assertEqual(
                snapshot.statuses[0].mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )
            self.assertEqual(
                controller.tm_suggestion_report().suggestions[0].target,
                "你好",
            )

    def test_ambiguous_failure_replaces_resource_with_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambiguous = [False]
            controller, runtime, _repository, resource_id = _fixture(
                root,
                ambiguous_after_activation=ambiguous,
            )
            preflight = controller.prepare_tm_activation(resource_id)

            def ambiguous_outcome(*_args: object, **_kwargs: object):
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

            snapshot = runtime.capture_operation_snapshot()
            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertEqual(snapshot.generation, 1)
            self.assertEqual(snapshot.legacy_ports, ())
            self.assertEqual(snapshot.canonical_ports, ())
            self.assertEqual(
                snapshot.statuses[0].mode,
                TMResourceDisplayMode.UNAVAILABLE,
            )
            report = controller.tm_suggestion_report()
            self.assertEqual(report.suggestions, ())
            self.assertEqual(
                report.resource_statuses[0].mode,
                TMResourceDisplayMode.UNAVAILABLE,
            )

    def test_success_candidate_validation_failure_is_atomic_and_blocks_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, _repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            before = runtime.snapshot()

            with patch.object(
                controller_module,
                "_validate_activation_runtime_candidate",
                side_effect=ValueError("candidate mismatch"),
            ):
                started = controller.activate_tm_resource(preflight)
                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                )

            after = runtime.snapshot()
            self.assertIs(after, before)
            self.assertEqual(after.generation, 0)
            self.assertEqual(len(after.legacy_ports), 1)
            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            )
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            ):
                controller.tm_suggestion_report()

    def test_success_outcome_generation_mismatch_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, _repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            outcome = controller._tm_activation_outcome
            self.assertIs(type(outcome), MigrationReport)
            assert isinstance(outcome, MigrationReport)
            before = runtime.snapshot()

            with self.assertRaisesRegex(
                ValueError,
                "generation changed",
            ):
                controller._refresh_runtime_for_activation_outcome(
                    resource_id=resource_id,
                    outcome=replace(
                        outcome,
                        activated_generation=outcome.activated_generation + 1,
                    ),
                    service_canonical_store_id=outcome.canonical_store_id,
                )

            self.assertIs(runtime.snapshot(), before)
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            ):
                controller.tm_suggestion_report()

    def test_success_rejects_same_generation_foreign_service_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            outcome = controller._tm_activation_outcome
            self.assertIs(type(outcome), MigrationReport)
            assert isinstance(outcome, MigrationReport)
            before = runtime.snapshot()
            foreign_service = controller_module._tm_rebuild_service(
                repository.get(resource_id)
            )
            object.__setattr__(
                foreign_service,
                "_canonical_store_id",
                "foreign.same-generation.store",
            )

            with (
                patch.object(
                    controller_module,
                    "_tm_rebuild_service",
                    return_value=foreign_service,
                ),
                patch.object(
                    TMMigrationService,
                    "rebuild_from_snapshot",
                    autospec=True,
                    return_value=outcome,
                ),
            ):
                rebuild = controller.rebuild_tm_resource(resource_id)
                completed = controller.wait_tm_activation(
                    rebuild.operation_id,
                    timeout=20.0,
                )

            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            )
            self.assertIs(runtime.snapshot(), before)

    def test_published_failure_rejects_same_generation_foreign_service_store(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            before = runtime.snapshot()
            foreign_service = controller_module._tm_rebuild_service(
                repository.get(resource_id)
            )
            object.__setattr__(
                foreign_service,
                "_canonical_store_id",
                "foreign.same-generation.store",
            )

            with (
                patch.object(
                    controller_module,
                    "_tm_rebuild_service",
                    return_value=foreign_service,
                ),
                patch.object(
                    TMMigrationService,
                    "rebuild_from_snapshot",
                    autospec=True,
                    return_value=_published_failure(),
                ),
            ):
                rebuild = controller.rebuild_tm_resource(resource_id)
                completed = controller.wait_tm_activation(
                    rebuild.operation_id,
                    timeout=20.0,
                )

            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            )
            self.assertIs(runtime.snapshot(), before)
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            ):
                controller.tm_suggestion_report()

    def test_lkg_failure_rejects_same_generation_foreign_service_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            config = repository.get(resource_id)
            config.path.write_text(
                json.dumps({"source": "Hello", "target": "diverged"}) + "\n",
                encoding="utf-8",
            )
            real_service = controller_module._tm_rebuild_service(config)
            with patch.object(
                TMMigrationService,
                "_build_stage",
                autospec=True,
                side_effect=OSError("forced LKG outcome"),
            ):
                lkg_outcome = real_service.rebuild_from_snapshot(
                    config.path,
                    resource_id,
                )
            self.assertIs(type(lkg_outcome), MigrationFailure)
            assert isinstance(lkg_outcome, MigrationFailure)
            self.assertEqual(lkg_outcome.active_generation, 0)
            before = runtime.snapshot()
            foreign_service = controller_module._tm_rebuild_service(config)
            object.__setattr__(
                foreign_service,
                "_canonical_store_id",
                "foreign.same-generation.store",
            )

            with (
                patch.object(
                    controller_module,
                    "_tm_rebuild_service",
                    return_value=foreign_service,
                ),
                patch.object(
                    TMMigrationService,
                    "rebuild_from_snapshot",
                    autospec=True,
                    return_value=lkg_outcome,
                ),
            ):
                rebuild = controller.rebuild_tm_resource(resource_id)
                completed = controller.wait_tm_activation(
                    rebuild.operation_id,
                    timeout=20.0,
                )

            self.assertFalse(completed.succeeded)
            self.assertEqual(
                completed.safe_code,
                "TM.ACTIVATION.RUNTIME_REFRESH_FAILED",
            )
            self.assertIs(runtime.snapshot(), before)

    def test_public_canonical_rebuild_failure_keeps_last_known_good_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            active = runtime.capture_operation_snapshot()
            config = repository.get(resource_id)
            config.path.write_text(
                json.dumps(
                    {"source": "Hello", "target": "已变更的 legacy 译文"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(
                TMMigrationService,
                "_build_stage",
                autospec=True,
                side_effect=OSError("forced rebuild failure"),
            ):
                started = controller.rebuild_tm_resource(resource_id)
                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                )

            self.assertFalse(completed.succeeded)
            self.assertEqual(completed.safe_code, "IMPORT.FAILED")
            refreshed = runtime.capture_operation_snapshot()
            self.assertEqual(refreshed.generation, active.generation + 1)
            self.assertEqual(refreshed.legacy_ports, ())
            self.assertEqual(len(refreshed.canonical_ports), 1)
            self.assertEqual(
                refreshed.statuses[0].mode,
                TMResourceDisplayMode.SOURCE_DIVERGED,
            )
            self.assertEqual(
                controller.tm_suggestion_report().suggestions[0].target,
                "你好",
            )

    def test_public_canonical_rebuild_success_refreshes_current_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, runtime, repository, resource_id = _fixture(root)
            preflight = controller.prepare_tm_activation(resource_id)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            before = runtime.capture_operation_snapshot()
            config = repository.get(resource_id)
            config.path.write_text(
                json.dumps(
                    {"source": "Hello", "target": "重建后译文"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            rebuild = controller.rebuild_tm_resource(resource_id)
            completed = controller.wait_tm_activation(
                rebuild.operation_id,
                timeout=20.0,
            )

            self.assertTrue(completed.succeeded)
            self.assertIsNone(completed.safe_code)
            after = runtime.capture_operation_snapshot()
            self.assertEqual(after.generation, before.generation + 1)
            self.assertEqual(
                after.statuses[0].mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertEqual(
                controller.issued_tm_suggestions[0].target,
                "重建后译文",
            )


if __name__ == "__main__":
    unittest.main()
