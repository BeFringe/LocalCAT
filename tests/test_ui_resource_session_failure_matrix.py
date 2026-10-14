"""Task 10.2 bounded late completion and independent project outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import EditorProject, EditorSegment, ResourceKind, TMResourceDisplayMode
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from editor_project import load_project
from resource_repository import ResourceRepository
from tests.test_editor_controller_tm_activation_completion import _activation_test_root
from tm_application_composition import TMResourceResolver, TMRuntimeHost, _binding_owner
from tm_migration import MigrationReport, TMMigrationService


def _fixture(root: Path):
    """Keep the healthy peer outside the one externally replaced family."""
    external = root / "external"
    external.mkdir()
    source = external / "tm.jsonl"
    source.write_text('{"source":"Hello","target":"original"}\n', encoding="utf-8")
    repository = ResourceRepository(root / "app", default_tm_path=source)
    peer = repository.create_resource("Healthy peer", ResourceKind.TRANSLATION_MEMORY)
    peer.path.write_text('{"source":"Hello","target":"peer"}\n', encoding="utf-8")
    runtime = TMRuntimeHost(resolver=TMResourceResolver(), configs=repository.list_resources())
    adapter = EditorTMAdapter(
        runtime_host=runtime,
        capability_host=CapabilityHost(evaluated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc)),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(EditorProject(name="Independent project", segments=(
        EditorSegment(id="first", source="Hello"),
        EditorSegment(id="second", source="Next segment"),
    )))
    return controller, adapter, runtime, repository, peer.id


def _replace_directory(directory: Path) -> None:
    """One observable replacement after a real operation, not an I/O race claim."""
    previous = directory.with_name(directory.name + "-original")
    directory.rename(previous)
    shutil.copytree(previous, directory)


@unittest.skipUnless(os.name == "posix", "POSIX observable configured-directory replacement")
class UIResourceSessionFailureMatrixTests(unittest.TestCase):
    def _activate(self, controller: EditorController) -> None:
        operation = controller.activate_tm_resource(controller.prepare_tm_activation("local-tm"))
        self.assertTrue(controller.wait_tm_activation(operation.operation_id, timeout=20.0).succeeded)

    def test_late_query_after_resource_stop_retries_and_keeps_healthy_peer(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            controller, adapter, runtime, repository, peer_id = _fixture(root)
            self._activate(controller)
            issued = next(item for item in controller.tm_suggestion_report().suggestions if item.resource_id == "local-tm")
            original = EditorTMAdapter._query_current_operation
            calls = 0

            def query_then_stop(current, **kwargs):
                nonlocal calls
                operation = original(current, **kwargs)
                if current is adapter:
                    calls += 1
                    if calls == 1:
                        self.assertIn("local-tm", {item.resource_id for item in operation.report.suggestions})
                        _replace_directory(repository.get("local-tm").path.parent)
                        stopped = runtime.refresh(repository.list_resources())
                        self.assertEqual(stopped.statuses[0].mode, TMResourceDisplayMode.UNAVAILABLE)
                return operation

            with patch.object(EditorTMAdapter, "_query_current_operation", query_then_stop):
                report = controller.tm_suggestion_report()

            self.assertEqual(calls, 2)
            self.assertEqual({item.resource_id for item in report.suggestions}, {peer_id})
            self.assertEqual(controller.issued_tm_suggestions, report.suggestions)
            self.assertEqual(report.resource_statuses[0].mode, TMResourceDisplayMode.UNAVAILABLE)
            before = controller.current_segment
            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(issued)
            self.assertEqual(controller.current_segment, before)
            self.assertNotIn(str(root), repr(report))

    def test_late_successful_activation_after_location_loss_stays_resource_local(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            controller, _adapter, runtime, repository, peer_id = _fixture(root)
            controller.create_resource("Healthy terms", ResourceKind.TERMBASE)
            controller.add_term("Hello", "term target")
            original = TMMigrationService.activate_initial
            completed_core = []

            def activate_then_replace(service, source, resource_id):
                outcome = original(service, source, resource_id)
                self.assertIs(type(outcome), MigrationReport)
                completed_core.append(outcome)
                _replace_directory(repository.get(resource_id).path.parent)
                return outcome

            preflight = controller.prepare_tm_activation("local-tm")
            before_runtime = runtime.snapshot()
            before_report = controller.tm_suggestion_report()
            issued = next(item for item in before_report.suggestions if item.resource_id == "local-tm")
            resolver = controller._tm_resource_resolver
            original_owner = _binding_owner(resolver._bindings["local-tm"][1])
            self.assertEqual(
                {item.resource_id for item in controller.tm_suggestion_report().suggestions},
                {"local-tm", peer_id},
            )
            with patch.object(TMMigrationService, "activate_initial", activate_then_replace):
                operation = controller.activate_tm_resource(preflight)
                completed = controller.wait_tm_activation(operation.operation_id, timeout=20.0)

            self.assertEqual(len(completed_core), 1)
            self.assertFalse(completed.succeeded)
            self.assertNotIn(str(root), repr(completed))
            statuses = {status.resource_id: status for status in controller.tm_resource_statuses()}
            self.assertEqual(statuses["local-tm"].mode, TMResourceDisplayMode.UNAVAILABLE)
            self.assertNotEqual(statuses[peer_id].mode, TMResourceDisplayMode.UNAVAILABLE)
            self.assertEqual({item.resource_id for item in controller.tm_suggestion_report().suggestions}, {peer_id})
            self.assertEqual(completed.safe_code, "TM.RUNTIME.SOURCE_BINDING_UNAVAILABLE")
            self.assertEqual(completed.retryable, statuses["local-tm"].retryable)
            self.assertIsNone(controller._tm_runtime_blocked_safe_code)
            self.assertIs(_binding_owner(resolver._bindings["local-tm"][1]), original_owner)
            self.assertEqual(runtime.snapshot().generation, before_runtime.generation + 1)
            for snapshot in (runtime.snapshot(), runtime.capture_operation_snapshot()):
                self.assertNotIn("local-tm", {port.resource_id for port in snapshot.legacy_ports})
                self.assertNotIn("local-tm", {port.resource_id for port in snapshot.canonical_ports})
                self.assertNotIn("local-tm", {handle.resource_id for handle in snapshot.canonical_handles})
            self.assertNotIn("local-tm", controller._tm_engines)
            self.assertIn(peer_id, controller._tm_engines)
            self.assertEqual({item.resource_id for item in controller.issued_tm_suggestions}, {peer_id})
            self.assertGreater(controller.query_epoch, before_report.query_identity.query_epoch)
            before_segment = controller.current_segment
            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(issued)
            self.assertEqual(controller.current_segment, before_segment)
            self.assertEqual(len(controller.term_suggestions()), 1)
            controller.update_target("independent target")
            saved_path = root / "independent-project.json"
            saved = controller.save_project(saved_path)
            self.assertEqual(saved.segments[0].target, "independent target")
            self.assertEqual(load_project(saved_path).segments[0].target, "independent target")

    def test_late_stopped_candidate_still_requires_report_resource_and_store(self) -> None:
        original = TMMigrationService.activate_initial
        for field in ("resource_id", "canonical_store_id"):
            with self.subTest(field=field), _activation_test_root() as raw:
                controller, _adapter, runtime, repository, _peer_id = _fixture(raw.resolve())
                preflight = controller.prepare_tm_activation("local-tm")
                before = runtime.snapshot()

                def activate_then_forge(service, source, resource_id):
                    outcome = original(service, source, resource_id)
                    self.assertIs(type(outcome), MigrationReport)
                    assert isinstance(outcome, MigrationReport)
                    _replace_directory(repository.get(resource_id).path.parent)
                    return replace(
                        outcome,
                        snapshot_receipt=replace(outcome.snapshot_receipt, **{field: "foreign-identity"}),
                        **{field: "foreign-identity"},
                    )

                with patch.object(TMMigrationService, "activate_initial", activate_then_forge):
                    operation = controller.activate_tm_resource(preflight)
                    completed = controller.wait_tm_activation(operation.operation_id, timeout=20.0)

                self.assertFalse(completed.succeeded)
                self.assertEqual(completed.safe_code, "TM.ACTIVATION.RUNTIME_REFRESH_FAILED")
                self.assertEqual(controller._tm_runtime_blocked_safe_code, completed.safe_code)
                self.assertIs(runtime.snapshot(), before)
                with self.assertRaises(EditorControllerError):
                    controller.tm_suggestion_report()

    def test_late_stopped_candidate_precommit_failure_keeps_global_latch(self) -> None:
        original = TMMigrationService.activate_initial
        for fault in ("engine-build", "target-engine", "candidate-drift"):
            with self.subTest(fault=fault), _activation_test_root() as raw:
                controller, _adapter, runtime, repository, _peer_id = _fixture(raw.resolve())
                preflight = controller.prepare_tm_activation("local-tm")
                before = runtime.snapshot()
                old_engines = controller._tm_engines
                build = controller._build_tm_engine_set_for_runtime_snapshot
                reached_precommit = []

                def activate_then_replace(service, source, resource_id):
                    outcome = original(service, source, resource_id)
                    self.assertIs(type(outcome), MigrationReport)
                    _replace_directory(repository.get(resource_id).path.parent)
                    return outcome

                def fail_precommit(configs, candidate):
                    status = next(item for item in candidate.statuses if item.resource_id == "local-tm")
                    self.assertEqual(status.mode, TMResourceDisplayMode.UNAVAILABLE)
                    self.assertNotIn("local-tm", {port.resource_id for port in candidate.legacy_ports})
                    self.assertNotIn("local-tm", {port.resource_id for port in candidate.canonical_ports})
                    reached_precommit.append(fault)
                    if fault == "engine-build":
                        raise ValueError("injected compatibility build failure")
                    engines = build(configs, candidate)
                    if fault == "target-engine":
                        engines["local-tm"] = old_engines["local-tm"]
                    else:
                        object.__setattr__(candidate, "generation", candidate.generation + 1)
                    return engines

                with (
                    patch.object(TMMigrationService, "activate_initial", activate_then_replace),
                    patch.object(controller, "_build_tm_engine_set_for_runtime_snapshot", fail_precommit),
                ):
                    operation = controller.activate_tm_resource(preflight)
                    completed = controller.wait_tm_activation(operation.operation_id, timeout=20.0)

                self.assertEqual(reached_precommit, [fault])
                self.assertFalse(completed.succeeded)
                self.assertEqual(completed.safe_code, "TM.ACTIVATION.RUNTIME_REFRESH_FAILED")
                self.assertEqual(controller._tm_runtime_blocked_safe_code, completed.safe_code)
                self.assertIs(runtime.snapshot(), before)
                self.assertIs(controller._tm_engines, old_engines)
                self.assertEqual(controller.issued_tm_suggestions, ())
                with self.assertRaises(EditorControllerError):
                    controller.tm_suggestion_report()


if __name__ == "__main__":
    unittest.main()
