"""Task 10.1 actual application/Core resource-family handoff checks."""
from __future__ import annotations

from datetime import datetime, timezone
import gc
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import weakref

from capability_host import CapabilityHost
from editor_contracts import EditorProject, EditorSegment, ImportRequest, ResourceKind, TMResourceDisplayMode
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from resource_package_contracts import ResourceImportMode, ResourceRecoveryAction
from resource_repository import ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_migration import TMMigrationService
from tm_engine import TMEngine
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore
from tests.test_editor_controller_tm_activation_completion import _activation_test_root, _fixture


class TMSharedResourceSessionTests(unittest.TestCase):
    def runtime_owner(
        self, runtime: TMRuntimeHost, resource_id: str,
    ) -> ResourceStoreCoordinator:
        store = next(
            port.handle.store for port in runtime.snapshot().canonical_ports
            if port.resource_id == resource_id
        )
        assert type(store) is SQLiteTMStore
        return store.coordinator

    def activate(self, controller: EditorController, resource_id: str) -> None:
        preflight = controller.prepare_tm_activation(resource_id)
        operation = controller.activate_tm_resource(preflight)
        result = controller.wait_tm_activation(operation.operation_id, timeout=20.0)
        self.assertTrue(result.succeeded, result)

    def fail_initial(
        self, controller: EditorController, resource_id: str,
    ) -> weakref.ReferenceType[ResourceStoreCoordinator]:
        owners: list[weakref.ReferenceType[ResourceStoreCoordinator]] = []
        original = TMMigrationService.activate_initial
        def observe(service: TMMigrationService, source: Path, selected_id: str):
            owner = service._coordinator
            assert type(owner) is ResourceStoreCoordinator
            owners.append(weakref.ref(owner))
            return original(service, source, selected_id)
        preflight = controller.prepare_tm_activation(resource_id)
        with patch.object(TMMigrationService, "activate_initial", observe), patch.object(
            TMMigrationService, "_build_stage", side_effect=OSError("bounded build failure"),
        ):
            operation = controller.activate_tm_resource(preflight)
            completed = controller.wait_tm_activation(operation.operation_id, timeout=20.0)
        self.assertEqual(completed.safe_code, "MIGRATION.INITIAL_IO_FAILED")
        return owners[0]

    @unittest.skipUnless(os.name == "posix", "POSIX configured directory replacement")
    def test_failed_initial_refresh_keeps_original_family_after_clone(self) -> None:
        for with_adapter in (True, False):
            with self.subTest(adapter=with_adapter), _activation_test_root() as raw:
                root = raw.resolve()
                controller, runtime, repository, resource_id = _fixture(root)
                if not with_adapter:
                    controller = EditorController(repository)
                    controller.set_project(EditorProject(name="Project", segments=(EditorSegment(id="1", source="Hello"),)))
                owner_ref = self.fail_initial(controller, resource_id)
                controller.reload_resources()
                gc.collect()
                self.assertIsNotNone(owner_ref())
                directory = repository.get(resource_id).path.parent
                moved = directory.with_name("resources-before-replacement")
                directory.rename(moved)
                shutil.copytree(moved, directory)
                self.assertEqual(controller.tm_resource_statuses()[0].mode, TMResourceDisplayMode.UNAVAILABLE)
                self.assertEqual(controller.issued_tm_suggestions, ())
                if with_adapter:
                    self.assertEqual(controller.tm_suggestion_report().suggestions, ())
                else:
                    self.assertEqual(controller.suggestions().tm_matches, ())
                controller.update_target("write must fail locally")
                confirmed = controller.confirm_current()
                self.assertEqual(confirmed.write_report.written_resource_ids, ())
                self.assertTrue(confirmed.write_report.errors)
                for _ in range(3):
                    controller.reload_resources()
                    gc.collect()
                    self.assertEqual(controller.tm_resource_statuses()[0].mode, TMResourceDisplayMode.UNAVAILABLE)
                if with_adapter:
                    self.assertEqual(controller.tm_suggestion_report().suggestions, ())
                with self.assertRaises(EditorControllerError):
                    controller.prepare_tm_activation(resource_id)
                controller.update_target("independent edit")
                controller.save_project(root / "project.json")
                self.assertEqual(controller.current_segment.target, "independent edit")

    def test_failed_initial_refresh_then_retry_consumes_same_owner(self) -> None:
        with _activation_test_root() as raw:
            controller, runtime, repository, resource_id = _fixture(raw.resolve())
            owner_ref = self.fail_initial(controller, resource_id)
            controller.reload_resources()
            gc.collect()
            self.assertIsNotNone(owner_ref())
            self.activate(controller, resource_id)
            self.assertIs(self.runtime_owner(runtime, resource_id), owner_ref())
            controller.export_resource_package(resource_id, raw.resolve() / "retry.localcat-resource")
            self.assertIs(controller._resource_portability._tm.owner_for_resource(repository.get(resource_id)), owner_ref())
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].target, "你好")

    def test_create_and_replace_handoff_then_query_and_append(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            controller, runtime, repository, resource_id = _fixture(root)
            self.activate(controller, resource_id)
            package = root / "tm.localcat-resource"
            controller.export_resource_package(resource_id, package)
            result = controller.apply_resource_package_import(controller.preview_resource_package_import(
                package, ResourceImportMode.CREATE_NEW, new_resource_name="Imported",
            ))
            created = repository.get(result.destination_resource_id)
            owner = controller._resource_portability._tm.owner_for_resource(created)
            self.assertIs(self.runtime_owner(runtime, created.id), owner)
            controller.apply_resource_package_import(controller.preview_resource_package_import(
                package, ResourceImportMode.REPLACE_SELECTED, destination_resource_id=created.id,
            ))
            self.assertIs(self.runtime_owner(runtime, created.id), owner)
            self.assertEqual({s.resource_id for s in controller.tm_suggestion_report().suggestions}, {resource_id, created.id})
            controller.update_target("after replace")
            confirmed = controller.confirm_current()
            self.assertEqual(set(confirmed.write_report.written_resource_ids), {resource_id, created.id})
            self.assertEqual(confirmed.write_report.errors, ())

    def test_source_divergence_retains_lkg_and_explicit_rebuild(self) -> None:
        with _activation_test_root() as raw:
            controller, runtime, repository, resource_id = _fixture(raw.resolve())
            self.activate(controller, resource_id)
            owner = self.runtime_owner(runtime, resource_id)
            repository.get(resource_id).path.write_text('{"source":"Hello","target":"changed source"}\n')
            controller.reload_resources()
            self.assertEqual(controller.tm_resource_statuses()[0].mode, TMResourceDisplayMode.SOURCE_DIVERGED)
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].target, "你好")
            operation = controller.rebuild_tm_resource(resource_id)
            self.assertTrue(controller.wait_tm_activation(operation.operation_id, timeout=20.0).succeeded)
            self.assertIs(self.runtime_owner(runtime, resource_id), owner)
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].target, "changed source")

    def test_validated_family_handoff_does_not_require_same_coordinator_object(self) -> None:
        with _activation_test_root() as raw:
            controller, runtime, repository, resource_id = _fixture(raw.resolve())
            self.activate(controller, resource_id)
            resource = repository.get(resource_id)
            original = self.runtime_owner(runtime, resource_id)
            reopened = TMEngine(str(resource.path), expected_resource_id=resource_id).canonical_store
            assert type(reopened) is SQLiteTMStore
            self.assertIsNot(reopened.coordinator, original)
            controller._tm_resource_resolver._retain_published_owner(resource, reopened.coordinator)
            controller.reload_resources()
            self.assertEqual(controller.tm_suggestion_report().suggestions[0].target, "你好")
            controller.update_target("same family handoff")
            self.assertEqual(controller.confirm_current().write_report.errors, ())

    def test_removed_resource_releases_resolver_legacy_binding(self) -> None:
        with _activation_test_root() as raw:
            controller, runtime, repository, resource_id = _fixture(raw.resolve())
            owner_ref = self.fail_initial(controller, resource_id)
            controller.reload_resources()
            gc.collect()
            self.assertIsNotNone(owner_ref())
            controller.delete_resource(resource_id)
            gc.collect()
            self.assertIsNone(owner_ref())
            self.assertEqual(controller.suggestions().tm_matches, ())

    def test_package_owner_handoff_failure_retains_existing_recovery_route(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            controller, runtime, repository, resource_id = _fixture(root)
            self.activate(controller, resource_id)
            package = root / "recovery.localcat-resource"
            controller.export_resource_package(resource_id, package)
            preview = controller.preview_resource_package_import(
                package, ResourceImportMode.CREATE_NEW, new_resource_name="Recovered",
            )
            with patch.object(
                controller._tm_resource_resolver, "_retain_published_owner",
                side_effect=ValueError("owner handoff refused"),
            ):
                with self.assertRaisesRegex(EditorControllerError, "RESOURCE.IMPORT.RECOVERY_REQUIRED"):
                    controller.apply_resource_package_import(preview)
            recovery = controller.inspect_resource_portability_recovery()[0]
            completed = controller.recover_resource_portability(
                recovery, ResourceRecoveryAction.COMPLETE,
            )
            self.assertIsNotNone(completed.receipt)
            self.assertEqual(controller.inspect_resource_portability_recovery(), ())
            self.assertEqual(len(controller.tm_suggestion_report().suggestions), 2)

    def _check_replace_handoff_rejection(
        self, *, with_adapter: bool, during_complete: bool,
    ) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            controller, _runtime, repository, resource_id = _fixture(root)
            if not with_adapter:
                controller = EditorController(repository)
                controller.set_project(EditorProject(
                    name="Replace recovery", segments=(EditorSegment(id="1", source="Hello"),),
                ))
            self.activate(controller, resource_id)
            package = root / "before-edit.localcat-resource"
            controller.export_resource_package(resource_id, package)
            controller.update_target("new target before replace")
            self.assertEqual(controller.confirm_current().write_report.errors, ())

            def current_suggestion():
                return (
                    controller.tm_suggestion_report().suggestions[0]
                    if with_adapter else controller.suggestions().tm_matches[0]
                )

            issued = current_suggestion()
            self.assertEqual(issued.target, "new target before replace")
            epoch_before = controller.query_epoch
            preview = controller.preview_resource_package_import(
                package, ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=resource_id,
            )
            with patch.object(
                controller._tm_resource_resolver, "_retain_published_owner",
                side_effect=ValueError("bounded replace handoff failure"),
            ):
                with self.assertRaisesRegex(EditorControllerError, "RESOURCE.IMPORT.RECOVERY_REQUIRED"):
                    controller.apply_resource_package_import(preview)

            if during_complete:
                # An explicit successful refresh can issue a current result
                # while the durable receipt still awaits COMPLETE.
                controller.reload_resources()
                issued = current_suggestion()
                self.assertEqual(issued.target, "你好")
                epoch_before = controller.query_epoch
                recovery = controller.inspect_resource_portability_recovery()[0]
                with patch.object(
                    controller._tm_resource_resolver, "_retain_published_owner",
                    side_effect=ValueError("bounded COMPLETE handoff failure"),
                ):
                    with self.assertRaisesRegex(EditorControllerError, "RESOURCE.IMPORT.RECOVERY_REQUIRED"):
                        controller.recover_resource_portability(recovery, ResourceRecoveryAction.COMPLETE)

            controller.update_target("independent edit after failure")
            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(issued)
            self.assertEqual(controller.current_segment.target, "independent edit after failure")
            self.assertGreater(controller.query_epoch, epoch_before)
            self.assertEqual(controller.issued_tm_suggestions, ())
            if with_adapter:
                with self.assertRaisesRegex(EditorControllerError, "TM.RUNTIME.REFRESH_FAILED"):
                    controller.tm_suggestion_report()
            controller.save_project(root / "independent-project.json")
            recovery = controller.inspect_resource_portability_recovery()[0]
            completed = controller.recover_resource_portability(recovery, ResourceRecoveryAction.COMPLETE)
            self.assertIsNotNone(completed.receipt)
            self.assertEqual(controller.inspect_resource_portability_recovery(), ())
            self.assertEqual(current_suggestion().target, "你好")
            self.assertEqual(controller.current_segment.target, "independent edit after failure")

    def test_replace_handoff_failure_revokes_old_suggestion_before_complete(self) -> None:
        for with_adapter in (True, False):
            with self.subTest(adapter=with_adapter):
                self._check_replace_handoff_rejection(
                    with_adapter=with_adapter, during_complete=False,
                )

    def test_complete_handoff_failure_revokes_issued_suggestion(self) -> None:
        for with_adapter in (True, False):
            with self.subTest(adapter=with_adapter):
                self._check_replace_handoff_rejection(
                    with_adapter=with_adapter, during_complete=True,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX configured directory replacement")
    def test_stopped_canonical_keeps_peer_and_rejects_tmx_export(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            external = root / "external"
            external.mkdir()
            source = external / "tm.jsonl"
            source.write_text('{"source":"Hello","target":"external"}\n')
            repository = ResourceRepository(root / "app", default_tm_path=source)
            peer = repository.create_resource("Peer", ResourceKind.TRANSLATION_MEMORY)
            peer.path.write_text('{"source":"Hello","target":"peer"}\n')
            runtime = TMRuntimeHost(resolver=TMResourceResolver(), configs=repository.list_resources())
            adapter = EditorTMAdapter(runtime_host=runtime, capability_host=CapabilityHost(evaluated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc)))
            controller = EditorController(repository, tm_adapter=adapter)
            controller.set_project(EditorProject(name="Project", segments=(EditorSegment(id="1", source="Hello"),)))
            term = controller.create_resource("Terms", ResourceKind.TERMBASE)
            terms_source = root / "terms.csv"
            terms_source.write_text("Source,Target\nHello,term greeting\n", encoding="utf-8-sig")
            self.assertEqual(controller.import_resource(ImportRequest(
                resource_id=term.id, input_path=terms_source,
            )).imported, 1)
            self.activate(controller, "local-tm")
            package = root / "canonical.localcat-resource"
            controller.export_resource_package("local-tm", package)
            exported = controller.export_tmx_resource_package("local-tm", root / "healthy.localcat-resource", "en", "zh")
            self.assertEqual(exported.receipt.record_count, 1)
            old = root / "external-old"
            external.rename(old)
            shutil.copytree(old, external)
            controller.reload_resources()
            self.assertEqual([s.target for s in controller.tm_suggestion_report().suggestions], ["peer"])
            self.assertEqual([s.target_term for s in controller.term_suggestions()], ["term greeting"])
            with self.assertRaises(EditorControllerError):
                controller.prepare_tm_activation("local-tm")
            with self.assertRaises(EditorControllerError):
                controller.reattest_tm_resource("local-tm")
            with self.assertRaises(EditorControllerError):
                controller.export_tmx_resource_package("local-tm", root / "stopped.localcat-resource", "en", "zh")
            with self.assertRaises(EditorControllerError):
                controller.preview_resource_package_import(
                    package, ResourceImportMode.REPLACE_SELECTED,
                    destination_resource_id="local-tm",
                )
            controller.update_target("saved despite unavailable TM")
            controller.save_project(root / "project.json")
            self.assertTrue((root / "project.json").is_file())


if __name__ == "__main__":
    unittest.main()
