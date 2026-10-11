"""Canonical activation preserves resource flags in the production composition."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMActivationOperationView,
    TMResourceDisplayMode,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from tm_contracts import CanonicalResourceIdentity, MigrationFailure, MigrationReport
from tm_migration import TMMigrationService
from tm_sqlite_store import SQLiteTMStore


_SOURCE = b'{"source":"Hello","target":"canonical target"}\n'


def _resource(
    root: Path, *, active: bool, lookup: bool, update: bool,
) -> tuple[ResourceRepository, ResourceConfig]:
    repository = ResourceRepository(root.resolve() / "app-data")
    resource = repository.create_resource("Memory", ResourceKind.TRANSLATION_MEMORY)
    resource.path.write_bytes(_SOURCE)
    resource = repository.update_resource(
        replace(resource, active=active, lookup=lookup, update=update)
    )
    return repository, resource


def _set_project(controller: EditorController) -> None:
    controller.set_project(EditorProject(
        name="Activation flags",
        segments=(EditorSegment(id="segment-1", source="Hello", target="draft"),),
    ))


@contextmanager
def _composed(
    repository: ResourceRepository, *, use_adapter: bool = True,
) -> Iterator[EditorController]:
    if use_adapter:
        controller, composition = _compose_editor_controller(repository)
    else:
        controller, composition = EditorController(repository), None
    try:
        _set_project(controller)
        yield controller
    finally:
        controller.close_project()
        controller.abandon_file_jobs()
        if composition is not None:
            composition._close_frozen_inputs()


def _activate(controller: EditorController, resource_id: str) -> TMActivationOperationView:
    preflight = controller.prepare_tm_activation(resource_id)
    started = controller.activate_tm_resource(preflight)
    return controller.wait_tm_activation(started.operation_id, timeout=20.0)


class InactiveTMActivationTests(unittest.TestCase):
    def test_composed_activation_preserves_all_flag_combinations(self) -> None:
        for active, lookup, update in product((False, True), repeat=3):
            with self.subTest(active=active, lookup=lookup, update=update):
                with tempfile.TemporaryDirectory() as temporary:
                    repository, resource = _resource(
                        Path(temporary), active=active, lookup=lookup, update=update,
                    )
                    config_bytes = (repository.config_dir / "resources.json").read_bytes()
                    with _composed(repository) as controller:
                        completed = _activate(controller, resource.id)

                        self.assertTrue(completed.succeeded, completed)
                        self.assertTrue(completed.completed)
                        self.assertIsNone(completed.safe_code)
                        self.assertFalse(completed.retryable)
                        self.assertEqual(repository.get(resource.id), resource)
                        self.assertEqual(
                            (repository.config_dir / "resources.json").read_bytes(),
                            config_bytes,
                        )
                        self.assertEqual(resource.path.read_bytes(), _SOURCE)
                        self.assertEqual(resource.id in controller._tm_engines, active)
                        status, = controller.tm_resource_statuses()
                        self.assertIs(status.mode, TMResourceDisplayMode.CANONICAL_ACTIVE)
                        self.assertTrue(status.exact_available)
                        self.assertFalse(status.context_available)
                        self.assertFalse(status.fuzzy_available)
                        suggestions = controller.tm_suggestion_report().suggestions
                        self.assertEqual(bool(suggestions), active and lookup)
                        self.assertEqual(bool(controller.suggestions().tm_matches), active and lookup)
                        if suggestions:
                            self.assertEqual(suggestions[0].target, "canonical target")
                            self.assertIs(
                                suggestions[0].provenance.resource_mode,
                                TMResourceDisplayMode.CANONICAL_ACTIVE,
                            )
                        identity = CanonicalResourceIdentity.from_configured_jsonl(
                            resource.id, resource.path,
                        )
                        canonical_before = identity.canonical_sidecar_path.read_bytes()
                        controller.update_target("confirmed target")
                        confirmed = controller.confirm_current()
                        self.assertEqual(
                            confirmed.write_report.written_resource_ids,
                            (resource.id,) if active and update else (),
                        )
                        self.assertEqual(confirmed.write_report.errors, ())
                        if not (active and update):
                            self.assertEqual(
                                identity.canonical_sidecar_path.read_bytes(), canonical_before,
                            )
                            self.assertEqual(resource.path.read_bytes(), _SOURCE)

    def test_inactive_activation_keeps_peer_queryable_and_cold_enable_uses_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, resource = _resource(
                Path(temporary), active=False, lookup=False, update=False,
            )
            peer = repository.create_resource("Peer", ResourceKind.TRANSLATION_MEMORY)
            peer.path.write_bytes(b'{"source":"Hello","target":"peer target"}\n')
            with _composed(repository) as controller:
                completed = _activate(controller, resource.id)
                self.assertTrue(completed.succeeded, completed)
                self.assertEqual(
                    tuple(item.resource_id for item in controller.tm_suggestion_report().suggestions),
                    (peer.id,),
                )
            with _composed(ResourceRepository(repository.config_dir)) as cold:
                self.assertEqual(cold.repository.get(resource.id), resource)
                self.assertEqual(
                    tuple(item.resource_id for item in cold.tm_suggestion_report().suggestions),
                    (peer.id,),
                )
                cold.update_resource(replace(resource, active=True, lookup=True))
                self.assertFalse(cold.repository.get(resource.id).update)
                suggestions = cold.tm_suggestion_report().suggestions
                activated = next(item for item in suggestions if item.resource_id == resource.id)
                self.assertEqual(activated.target, "canonical target")
                self.assertIs(activated.provenance.resource_mode, TMResourceDisplayMode.CANONICAL_ACTIVE)
                self.assertEqual(resource.path.read_bytes(), _SOURCE)

    def test_no_adapter_inactive_activation_preserves_flags_and_no_query_or_write(self) -> None:
        for lookup, update in product((False, True), repeat=2):
            with self.subTest(lookup=lookup, update=update), tempfile.TemporaryDirectory() as temporary:
                repository, resource = _resource(
                    Path(temporary), active=False, lookup=lookup, update=update,
                )
                controller = EditorController(repository)
                _set_project(controller)
                try:
                    completed = _activate(controller, resource.id)
                    self.assertTrue(completed.succeeded, completed)
                    self.assertEqual(repository.get(resource.id), resource)
                    self.assertNotIn(resource.id, controller._tm_engines)
                    self.assertEqual(controller.suggestions().tm_matches, ())
                    self.assertEqual(controller.confirm_current().write_report.written_resource_ids, ())
                    self.assertEqual(resource.path.read_bytes(), _SOURCE)
                    controller.update_resource(replace(resource, active=True, lookup=True))
                    self.assertEqual(controller.suggestions().tm_matches[0].target, "canonical target")
                    self.assertIsNotNone(controller._tm_engines[resource.id].canonical_store)
                finally:
                    controller.close_project()

    def test_real_core_build_failure_remains_failure_for_inactive_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, resource = _resource(
                Path(temporary), active=False, lookup=False, update=False,
            )
            with _composed(repository) as controller:
                with patch.object(
                    SQLiteTMStore, "_append_streamed_batch_for_immediate_seal",
                    side_effect=OSError("private build failure"),
                ):
                    completed = _activate(controller, resource.id)
                self.assertFalse(completed.succeeded)
                self.assertEqual(completed.safe_code, "MIGRATION.INITIAL_IO_FAILED")
                self.assertTrue(completed.retryable)
                self.assertIs(type(controller._tm_activation_outcome), MigrationFailure)
                self.assertEqual(repository.get(resource.id), resource)
                self.assertEqual(resource.path.read_bytes(), _SOURCE)
                self.assertEqual(controller.tm_suggestion_report().suggestions, ())
                status, = controller.tm_resource_statuses()
                self.assertIs(status.mode, TMResourceDisplayMode.LEGACY_EXACT_ONLY)

    def test_runtime_reproof_failure_is_not_accepted_as_success(self) -> None:
        original_activate = TMMigrationService.activate_initial
        for use_adapter, active in product((False, True), repeat=2):
            with self.subTest(use_adapter=use_adapter, active=active), tempfile.TemporaryDirectory() as temporary:
                repository, resource = _resource(
                    Path(temporary), active=active, lookup=active, update=active,
                )

                def activate_then_diverge(
                    service: TMMigrationService, source: Path, resource_id: str,
                ) -> MigrationReport | MigrationFailure:
                    outcome = original_activate(service, source, resource_id)
                    self.assertIs(type(outcome), MigrationReport)
                    source.write_bytes(_SOURCE + b'{"source":"Other","target":"changed"}\n')
                    return outcome

                with _composed(repository, use_adapter=use_adapter) as controller:
                    with patch.object(
                        TMMigrationService, "activate_initial", autospec=True,
                        side_effect=activate_then_diverge,
                    ):
                        completed = _activate(controller, resource.id)
                    self.assertFalse(completed.succeeded, completed)
                    self.assertEqual(completed.safe_code, "TM.ACTIVATION.RUNTIME_REFRESH_FAILED")
                    self.assertIs(type(controller._tm_activation_outcome), MigrationReport)
                    self.assertEqual(repository.get(resource.id), resource)
                    with self.assertRaises(EditorControllerError):
                        controller.suggestions()
                    if use_adapter:
                        with self.assertRaises(EditorControllerError):
                            controller.tm_suggestion_report()

    def test_inactive_resource_still_requires_matching_store_and_generation(self) -> None:
        for mismatch in ("store", "generation"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                repository, resource = _resource(
                    Path(temporary), active=False, lookup=False, update=False,
                )
                with _composed(repository) as controller:
                    self.assertTrue(_activate(controller, resource.id).succeeded)
                    outcome = controller._tm_activation_outcome
                    assert isinstance(outcome, MigrationReport)
                    adapter = controller._tm_adapter
                    assert adapter is not None
                    runtime = adapter._runtime_host
                    before = runtime.snapshot()
                    candidate = (
                        replace(outcome, activated_generation=outcome.activated_generation + 1)
                        if mismatch == "generation" else outcome
                    )
                    with self.assertRaisesRegex(ValueError, "generation changed|service authority changed"):
                        controller._refresh_runtime_for_activation_outcome(
                            resource_id=resource.id,
                            outcome=candidate,
                            service_canonical_store_id=(
                                "foreign.store" if mismatch == "store" else outcome.canonical_store_id
                            ),
                        )
                    self.assertIs(runtime.snapshot(), before)
                    self.assertNotIn(resource.id, controller._tm_engines)
                    with self.assertRaises(EditorControllerError):
                        controller.tm_suggestion_report()

    def test_active_resource_still_requires_compatibility_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository, resource = _resource(
                Path(temporary), active=True, lookup=True, update=True,
            )
            with _composed(repository) as controller:
                with patch.object(controller, "_build_tm_engine_set_for_runtime_snapshot", return_value={}):
                    completed = _activate(controller, resource.id)
                self.assertFalse(completed.succeeded)
                self.assertEqual(completed.safe_code, "TM.ACTIVATION.RUNTIME_REFRESH_FAILED")
                with self.assertRaises(EditorControllerError):
                    controller.tm_suggestion_report()


if __name__ == "__main__":
    unittest.main()
