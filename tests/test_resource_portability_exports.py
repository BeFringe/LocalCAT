from __future__ import annotations

import json
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from editor_controller import _initial_tm_activation_service
from resource_package_contracts import (
    PortableResourceKind,
    ResourceImportMode,
    ResourceOperationKind,
    ResourcePayloadProfile,
    ResourcePortabilityError,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
)
from resource_portability import ResourcePortabilityService
from resource_receipt_ledger import ResourcePendingPhase
from resource_repository import ResourceRepository
from tm_contracts import MigrationReport, TMRecordDraft
from tm_engine import TMEngine


_MIXED_TERMS = (
    b"\xef\xbb\xbfsource,target\n"
    b"localcat-term-v1,id-1,Case,Target,true,false\n"
)


def _remove_long_quarantine(root: Path) -> None:
    """Remove authenticated Core quarantine residue through Win32 long paths."""

    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


class ResourcePortabilityExportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows terminal reproof behavior")
    def test_outer_terminal_failure_retains_ready_export_for_fresh_manual_recovery(
        self,
    ) -> None:
        from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
        from platform_fs_windows import _WindowsPendingPublication

        original_terminal = _WindowsPendingPublication._terminal_reproof
        for operation in ("direct", "package"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                repository = ResourceRepository(root / "app")
                resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
                resource.path.write_bytes(_MIXED_TERMS)
                service = ResourcePortabilityService(repository)
                destination = root / (
                    "terms.csv"
                    if operation == "direct"
                    else "terms.localcat-resource"
                )
                ready_marked = False
                real_mark_ready = service._mark_pending_receipt_ready

                def observe_ready(receipt: object) -> None:
                    nonlocal ready_marked
                    real_mark_ready(receipt)
                    ready_marked = True

                def fail_outer_terminal(
                    pending: object,
                    retained: object,
                    preliminary: object,
                ) -> object:
                    if ready_marked:
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )
                    return original_terminal(pending, retained, preliminary)

                with (
                    patch.object(
                        service,
                        "_mark_pending_receipt_ready",
                        side_effect=observe_ready,
                    ),
                    patch.object(
                        _WindowsPendingPublication,
                        "_terminal_reproof",
                        new=fail_outer_terminal,
                    ),
                ):
                    with self.assertRaises(ResourcePortabilityError):
                        if operation == "direct":
                            service.export_direct(resource.id, destination)
                        else:
                            service.export_package(resource.id, destination)

                self.assertTrue(ready_marked)
                self.assertTrue(destination.exists())
                self.assertEqual(service._ledger.list_receipts(), ())
                pending = service._ledger.list_pending()
                self.assertEqual(len(pending), 1)
                self.assertIs(pending[0].phase, ResourcePendingPhase.RECEIPT_READY)

                cold = ResourcePortabilityService(
                    ResourceRepository(root / "app")
                )
                preview = cold.inspect_resource_portability_recovery()[0]
                self.assertIs(
                    preview.disposition,
                    ResourceRecoveryDisposition.MANUAL_REQUIRED,
                )
                with self.assertRaises(ResourcePortabilityError) as caught:
                    cold.recover_resource_portability(
                        preview,
                        ResourceRecoveryAction.COMPLETE,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "RESOURCE.RECOVERY.DECISION_INVALID",
                )
                self.assertEqual(len(cold._ledger.list_pending()), 1)
                self.assertEqual(cold._ledger.list_receipts(), ())

    def test_termbase_direct_and_package_use_identical_profile_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_MIXED_TERMS)
            service = ResourcePortabilityService(repository)
            direct = root / "terms.csv"
            package = root / "terms.localcat-resource"

            direct_outcome = service.export_direct(resource.id, direct)
            package_outcome = service.export_package(resource.id, package)
            report = service.validate_resource_package(package)

            self.assertEqual(direct.read_bytes(), _MIXED_TERMS)
            self.assertEqual(
                direct_outcome.receipt.payload_digest,
                package_outcome.receipt.payload_digest,
            )
            self.assertEqual(report.payload_digest, direct_outcome.receipt.payload_digest)
            self.assertEqual((report.record_count, report.legacy_record_count, report.v1_record_count), (2, 1, 1))
            self.assertIs(
                package_outcome.receipt.operation_kind,
                ResourceOperationKind.EXPORT_PACKAGE,
            )
            self.assertEqual(len(service._ledger.list_receipts()), 2)

    def test_real_active_canonical_tm_exports_direct_and_package(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            activation = _initial_tm_activation_service(resource)
            outcome = activation.activate_initial(resource.path, resource.id)
            self.assertIs(type(outcome), MigrationReport)

            service = ResourcePortabilityService(repository)
            direct = root / "tm.jsonl"
            package = root / "tm.localcat-resource"
            direct_outcome = service.export_direct(resource.id, direct)
            package_outcome = service.export_package(resource.id, package)
            report = service.validate_resource_package(package)

            self.assertEqual(
                direct_outcome.receipt.payload_digest,
                package_outcome.receipt.payload_digest,
            )
            self.assertEqual(report.payload_digest, direct_outcome.receipt.payload_digest)
            self.assertEqual(report.record_count, 1)
            self.assertIs(report.resource_kind, PortableResourceKind.TRANSLATION_MEMORY)
            self.assertIs(report.payload_profile, ResourcePayloadProfile.TM_JSONL_V1)
            self.assertTrue(service._tm.companion_path(direct).exists())

    def test_existing_direct_destination_is_atomically_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_MIXED_TERMS)
            destination = root / "terms.csv"
            destination.write_bytes(b"prior")
            outcome = ResourcePortabilityService(repository).export_direct(
                resource.id,
                destination,
            )
            self.assertEqual(destination.read_bytes(), _MIXED_TERMS)
            self.assertIsNotNone(outcome.receipt.destination_before_digest)

    def test_termbase_package_create_replace_and_replay_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Source terms", ResourceKind.TERMBASE)
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            old = destination_repo.create_resource("Old terms", ResourceKind.TERMBASE)
            old.path.write_bytes(b"\xef\xbb\xbfold,value\n")
            service = ResourcePortabilityService(destination_repo)

            create_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported terms",
            )
            created = service.apply_resource_package_import(create_preview)
            created_resource = destination_repo.get(created.destination_resource_id)
            self.assertEqual(created_resource.path.read_bytes(), _MIXED_TERMS)
            with self.assertRaises(ResourcePortabilityError) as replay:
                service.apply_resource_package_import(create_preview)
            self.assertEqual(replay.exception.code, "RESOURCE.IMPORT.PREVIEW_STALE")

            replace_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=old.id,
            )
            replaced = service.apply_resource_package_import(replace_preview)
            self.assertEqual(replaced.destination_resource_id, old.id)
            self.assertEqual(old.path.read_bytes(), _MIXED_TERMS)

    def test_tm_package_create_and_replace_use_core_generation_transactions(self) -> None:
        callback_observations: list[str] = []

        def assert_pending_path_locked(path: Path) -> None:
            """The Core pending publication must retain exclusive handles."""

            self.assertTrue(path.exists())
            with self.assertRaises(PermissionError):
                path.write_bytes(path.read_bytes())
            with self.assertRaises(PermissionError):
                path.unlink()
            replacement = path.with_name(f"{path.name}.callback-replacement")
            replacement.write_bytes(path.read_bytes())
            try:
                with self.assertRaises(PermissionError):
                    replacement.replace(path)
            finally:
                if replacement.exists():
                    replacement.unlink()

        with ExitStack() as stack:
            raw = stack.enter_context(tempfile.TemporaryDirectory())
            root = Path(raw).resolve()
            stack.callback(
                _remove_long_quarantine,
                root / "source-app" / "resources",
            )
            stack.callback(
                _remove_long_quarantine,
                root / "destination-app" / "resources",
            )
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Source TM", ResourceKind.TRANSLATION_MEMORY)
            source.path.write_text(
                json.dumps({"source": "new", "target": "新"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertIs(
                type(_initial_tm_activation_service(source).activate_initial(source.path, source.id)),
                MigrationReport,
            )
            package = root / "tm.localcat-resource"
            source_package = ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            existing = destination_repo.create_resource("Existing TM", ResourceKind.TRANSLATION_MEMORY)
            existing.path.write_text(
                json.dumps({"source": "old", "target": "旧"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertIs(
                type(_initial_tm_activation_service(existing).activate_initial(existing.path, existing.id)),
                MigrationReport,
            )
            service = ResourcePortabilityService(destination_repo)
            real_mark_ready = service._mark_pending_receipt_ready

            def observe_replace_ready(receipt: object) -> None:
                real_mark_ready(receipt)
                pending = service._ledger.list_pending()
                self.assertEqual(len(pending), 1)
                self.assertIs(pending[0].phase, ResourcePendingPhase.RECEIPT_READY)
                self.assertEqual(service._ledger.list_receipts(), ())
                assert_pending_path_locked(existing.path)
                callback_observations.append("replace")

            replace_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=existing.id,
            )
            with patch.object(
                service,
                "_mark_pending_receipt_ready",
                side_effect=observe_replace_ready,
            ):
                replaced = service.apply_resource_package_import(replace_preview)
            self.assertEqual(
                replaced.receipt.payload_digest,
                source_package.receipt.payload_digest,
            )
            self.assertEqual(replaced.receipt.owner_generation, 1)

            create_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported TM",
            )
            receipts_before_create = service._ledger.list_receipts()
            real_publish = destination_repo.publish_prepared_create

            def observe_create_publish(prepared: object) -> object:
                pending = service._ledger.list_pending()
                self.assertEqual(len(pending), 1)
                self.assertIs(pending[0].phase, ResourcePendingPhase.RECEIPT_READY)
                self.assertEqual(service._ledger.list_receipts(), receipts_before_create)
                assert_pending_path_locked(prepared.resource.path)  # type: ignore[attr-defined]
                result = real_publish(prepared)
                self.assertIsNotNone(
                    destination_repo.get(prepared.resource.id)  # type: ignore[attr-defined]
                )
                self.assertEqual(service._ledger.list_receipts(), receipts_before_create)
                callback_observations.append("create")
                return result

            with patch.object(
                destination_repo,
                "publish_prepared_create",
                side_effect=observe_create_publish,
            ):
                created = service.apply_resource_package_import(create_preview)
            created_resource = destination_repo.get(created.destination_resource_id)
            self.assertEqual(
                created.receipt.payload_digest,
                source_package.receipt.payload_digest,
            )
            self.assertEqual(
                hashlib.sha256(created_resource.path.read_bytes()).hexdigest(),
                created.receipt.payload_digest,
            )
            exported = root / "created-export.jsonl"
            post = service.export_direct(created_resource.id, exported)
            self.assertEqual(post.receipt.record_count, created.receipt.record_count)

            # Programming errors from the owner callback must remain visible
            # to the caller, while Core still closes its configured-source
            # publication and removes the private replace recovery copy.
            receipts_before_faults = service._ledger.list_receipts()
            real_replace_snapshot = service._tm.replace_snapshot

            def raise_type_error(
                resource: object,
                payload: Path,
                validated: object,
                *,
                owner_commit: object = None,
            ) -> object:
                del owner_commit

                def callback(_snapshot: object) -> None:
                    raise TypeError("owner callback programming error")

                return real_replace_snapshot(
                    resource,  # type: ignore[arg-type]
                    payload,
                    validated,  # type: ignore[arg-type]
                    owner_commit=callback,
                )

            fault_replace_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=created_resource.id,
            )
            with patch.object(
                service._tm,
                "replace_snapshot",
                side_effect=raise_type_error,
            ):
                with self.assertRaises(TypeError):
                    service.apply_resource_package_import(fault_replace_preview)
            self.assertEqual(service._ledger.list_receipts(), receipts_before_faults)
            self.assertEqual(service._ledger.list_pending(), ())
            self.assertEqual(
                list(
                    created_resource.path.parent.glob(
                        f".{created_resource.path.name}.localcat-import-recovery-*.jsonl"
                    )
                ),
                [],
            )
            created_resource.path.write_bytes(created_resource.path.read_bytes())

            real_create_snapshot = service._tm.create_snapshot

            def raise_assertion_error(
                resource: object,
                payload: Path,
                validated: object,
                *,
                owner_commit: object = None,
            ) -> object:
                del owner_commit

                def callback(_snapshot: object) -> None:
                    raise AssertionError("owner callback assertion")

                return real_create_snapshot(
                    resource,  # type: ignore[arg-type]
                    payload,
                    validated,  # type: ignore[arg-type]
                    owner_commit=callback,
                )

            fault_create_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Faulted TM",
            )
            with patch.object(
                service._tm,
                "create_snapshot",
                side_effect=raise_assertion_error,
            ):
                with self.assertRaises(AssertionError):
                    service.apply_resource_package_import(fault_create_preview)
            self.assertEqual(service._ledger.list_receipts(), receipts_before_faults)
            self.assertEqual(service._ledger.list_pending(), ())
            self.assertEqual(callback_observations, ["replace", "create"])
            self.assertEqual(len(service._ledger.list_receipts()), 3)

    @unittest.skipUnless(os.name == "nt", "Windows retained-handle behavior")
    def test_create_registry_commit_precedes_destination_terminal_close(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            service = ResourcePortabilityService(destination_repo)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported terms",
            )
            real_publish = destination_repo.publish_prepared_create
            owner_called = False

            def observe_registry_commit(prepared: object) -> object:
                nonlocal owner_called
                owner_called = True
                resource = prepared.resource
                replacement = resource.path.with_name("replacement.csv")
                replacement.write_bytes(resource.path.read_bytes())
                with self.assertRaises(PermissionError):
                    replacement.replace(resource.path)
                return real_publish(prepared)

            with patch.object(
                destination_repo,
                "publish_prepared_create",
                side_effect=observe_registry_commit,
            ):
                result = service.apply_resource_package_import(preview)

            self.assertTrue(owner_called)
            self.assertEqual(
                destination_repo.get(result.destination_resource_id).path.read_bytes(),
                _MIXED_TERMS,
            )

    @unittest.skipUnless(os.name == "nt", "Windows terminal reproof behavior")
    def test_create_terminal_failure_does_not_commit_success_receipt(self) -> None:
        from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
        from platform_fs_windows import _WindowsPendingPublication

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            service = ResourcePortabilityService(destination_repo)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported terms",
            )
            original_terminal = _WindowsPendingPublication._terminal_reproof

            def fail_created_destination_terminal(
                pending: object,
                retained: object,
                preliminary: object,
            ) -> object:
                entry_path = Path(retained._entry_path)  # type: ignore[attr-defined]
                if entry_path.suffix.lower() == ".csv":
                    ready = service._ledger.list_pending()
                    if ready and ready[0].phase is ResourcePendingPhase.RECEIPT_READY:
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )
                return original_terminal(pending, retained, preliminary)

            with patch.object(
                _WindowsPendingPublication,
                "_terminal_reproof",
                new=fail_created_destination_terminal,
            ):
                with self.assertRaises(ResourcePortabilityError) as caught:
                    service.apply_resource_package_import(preview)

            self.assertEqual(
                caught.exception.code,
                "RESOURCE.IMPORT.RECOVERY_REQUIRED",
            )
            self.assertEqual(service._ledger.list_receipts(), ())
            pending = service._ledger.list_pending()
            self.assertEqual(len(pending), 1)
            self.assertIs(pending[0].phase, ResourcePendingPhase.RECEIPT_READY)
            resources = destination_repo.list_resources()
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].path.read_bytes(), _MIXED_TERMS)

    def test_preview_rejects_source_or_destination_inode_replacement_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            destination = destination_repo.create_resource("Target", ResourceKind.TERMBASE)
            destination.path.write_bytes(b"\xef\xbb\xbfold,value\n")
            service = ResourcePortabilityService(destination_repo)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=destination.id,
            )
            original = destination.path.read_bytes()
            replacement = destination.path.with_name("replacement.csv")
            replacement.write_bytes(original)
            replacement.replace(destination.path)
            with self.assertRaises(ResourcePortabilityError) as stale_destination:
                service.apply_resource_package_import(preview)
            self.assertEqual(
                stale_destination.exception.code,
                "RESOURCE.IMPORT.DESTINATION_STALE",
            )
            self.assertEqual(destination.path.read_bytes(), original)

            create_preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
            )
            package_bytes = package.read_bytes()
            package_replacement = root / "replacement.localcat-resource"
            package_replacement.write_bytes(package_bytes)
            if os.name == "nt":
                with self.assertRaises(PermissionError):
                    package_replacement.replace(package)
                service.cancel_resource_package_import(create_preview)
                self.assertEqual(package.read_bytes(), package_bytes)
            else:
                package_replacement.replace(package)
                with self.assertRaises(ResourcePortabilityError) as stale_source:
                    service.apply_resource_package_import(create_preview)
                self.assertEqual(
                    stale_source.exception.code,
                    "RESOURCE.IMPORT.SOURCE_STALE",
                )

    def test_create_preview_rejects_same_bytes_new_registry_inode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Terms", ResourceKind.TERMBASE)
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            service = ResourcePortabilityService(destination_repo)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
            )
            registry_bytes = destination_repo.registry_path.read_bytes()
            replacement = destination_repo.registry_path.with_name("resources-new.json")
            replacement.write_bytes(registry_bytes)
            replacement.replace(destination_repo.registry_path)
            with self.assertRaises(ResourcePortabilityError) as caught:
                service.apply_resource_package_import(preview)
            self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.PREVIEW_STALE")
            self.assertEqual(destination_repo.list_resources(), ())

    def test_tm_replace_preview_rejects_canonical_revision_advance(self) -> None:
        with ExitStack() as stack:
            raw = stack.enter_context(tempfile.TemporaryDirectory())
            root = Path(raw).resolve()
            stack.callback(
                _remove_long_quarantine,
                root / "source-app" / "resources",
            )
            stack.callback(
                _remove_long_quarantine,
                root / "destination-app" / "resources",
            )
            source_repo = ResourceRepository(root / "source-app")
            source = source_repo.create_resource("Source TM", ResourceKind.TRANSLATION_MEMORY)
            source.path.write_text(
                json.dumps({"source": "new", "target": "新"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertIs(
                type(_initial_tm_activation_service(source).activate_initial(source.path, source.id)),
                MigrationReport,
            )
            package = root / "tm.localcat-resource"
            ResourcePortabilityService(source_repo).export_package(source.id, package)

            destination_repo = ResourceRepository(root / "destination-app")
            destination = destination_repo.create_resource(
                "Destination TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            destination.path.write_text(
                json.dumps({"source": "old", "target": "旧"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertIs(
                type(
                    _initial_tm_activation_service(destination).activate_initial(
                        destination.path,
                        destination.id,
                    )
                ),
                MigrationReport,
            )
            service = ResourcePortabilityService(destination_repo)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=destination.id,
            )
            store = TMEngine(
                str(destination.path),
                expected_resource_id=destination.id,
            ).canonical_store
            self.assertIsNotNone(store)
            store.append(
                TMRecordDraft(
                    source_raw="local advance",
                    target_raw="本地推进",
                    speaker_raw=None,
                    context_prev_raw=None,
                    context_next_raw=None,
                    file_source=None,
                    provenance=(),
                )
            )
            with self.assertRaises(ResourcePortabilityError) as caught:
                service.apply_resource_package_import(preview)
            self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.DESTINATION_STALE")


if __name__ == "__main__":
    unittest.main()
