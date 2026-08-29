"""WA-03 real-NTFS selected-files and ProjectPackage acceptance."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from project_package import ProjectPackageService
from project_save import RecoveryAction, RecoveryPhase, SaveJournalState
from project_workspace import ProjectWorkspaceService
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
    stage_selected_project_documents_with_file_system,
)
from platform_fs import compose_platform_file_backend
from platform_fs_windows import WindowsPlatformAdapter
from pathlib import PureWindowsPath


WORKER = Path(__file__).with_name("windows_project_package_worker.py")


def _write_document(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "shared",
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _workspace(root: Path):
    first = _write_document(root, "chapters/a.json", "A")
    second = _write_document(root, "chapters/b.json", "B")
    staged = stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest("WA-03", "en", "zh-CN"),
    )
    workspace = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id="wa03",
        revision=2,
    )
    from project_save import ProjectSaveService

    return staged, workspace, ProjectSaveService(workspace, baseline=None)


@unittest.skipUnless(sys.platform == "win32", "WA-03 requires real Windows NTFS")
class WindowsSelectedFilesIntakeTests(unittest.TestCase):
    def test_live_file_ids_are_not_persisted_and_hardlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-intake-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            staged, _workspace_service, _save_service = _workspace(root)
            self.assertEqual(len(staged.workspace.documents), 2)
            self.assertTrue(
                all(
                    item.regular_file_identity.startswith("windows-observation:")
                    for item in staged.source_identities
                )
            )
            self.assertTrue(
                all(
                    not item.source_identity.regular_file_identity.startswith("windows:")
                    for item in staged.origin_binding.documents
                )
            )

            first = root / "chapters/a.json"
            alias = root / "chapters/a-hardlink.json"
            os.link(first, alias)
            with self.assertRaises(ProjectWorkspaceError) as duplicate:
                stage_selected_project_documents(
                    root,
                    (first, alias),
                    SelectedProjectDocumentsRequest("duplicate", "en", "zh-CN"),
                )
            self.assertIn(
                duplicate.exception.code,
                {
                    "PROJECT.INTAKE.SOURCE_UNSAFE",
                    "PROJECT.WORKSPACE.IDENTITY_DUPLICATE",
                },
            )

    def test_junction_selected_ancestor_is_rejected_without_skip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-junction-") as directory:
            container = Path(directory)
            root = container / "root"
            outside = container / "outside"
            root.mkdir()
            outside.mkdir()
            foreign = _write_document(outside, "foreign.json", "foreign")
            local = _write_document(root, "local.json", "local")
            junction = root / "linked"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            with self.assertRaises(ProjectWorkspaceError) as unsafe:
                stage_selected_project_documents(
                    root,
                    (local, junction / foreign.name),
                    SelectedProjectDocumentsRequest("junction", "en", "zh-CN"),
                )
            self.assertEqual(unsafe.exception.code, "PROJECT.INTAKE.SOURCE_UNSAFE")

    def test_final_name_swap_between_probe_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-final-swap-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            first = _write_document(root, "a.json", "A")
            second = _write_document(root, "b.json", "B")
            attempted = False
            replaced = False

            def fault(phase: str) -> None:
                nonlocal attempted, replaced
                if phase == "windows_after_entry_probe" and not replaced:
                    attempted = True
                    original = first.read_bytes()
                    status = first.stat()
                    replacement = root / "replacement.tmp"
                    replacement.write_bytes(original)
                    os.utime(
                        replacement,
                        ns=(status.st_atime_ns, status.st_mtime_ns),
                    )
                    os.replace(replacement, first)
                    replaced = True

            with self.assertRaises(ProjectWorkspaceError) as stale:
                stage_selected_project_documents_with_file_system(
                    root,
                    (first, second),
                    SelectedProjectDocumentsRequest("swap", "en", "zh-CN"),
                    file_system=WindowsPlatformAdapter(_fault_injector=fault),
                )
            self.assertTrue(attempted)
            self.assertIn(
                stale.exception.code,
                {"PROJECT.INTAKE.SOURCE_UNSAFE", "PROJECT.INTAKE.SOURCE_STALE"},
            )


@unittest.skipUnless(sys.platform == "win32", "WA-03 requires real Windows NTFS")
class WindowsProjectPackageTests(unittest.TestCase):
    def _initial_package(self, root: Path, name: str = "project.localcat-project"):
        _staged, workspace_service, save_service = _workspace(root)
        target = root / name
        service = ProjectPackageService()
        exported = service.save_workspace(save_service, target)
        self.assertEqual(exported.save_report.journal_state, SaveJournalState.COMMITTED)
        self.assertIsNotNone(exported.receipt)
        return service, workspace_service, save_service, target, exported

    def test_two_document_save_is_deterministic_and_cold_reopens(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-package-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            service, workspace_service, _save_service, first, exported = (
                self._initial_package(root, "first.localcat-project")
            )
            from project_save import ProjectSaveService

            second = root / "second.localcat-project"
            exported_second = ProjectPackageService().save_workspace(
                ProjectSaveService(workspace_service, baseline=None),
                second,
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertTrue(exported.receipt.durable)
            self.assertTrue(exported_second.receipt.durable)
            reopened = ProjectPackageService().open(first)
            self.assertEqual(len(reopened.workspace.documents), 2)
            self.assertEqual(reopened.workspace, workspace_service.workspace)

    def test_import_preview_rejects_same_bytes_new_invalid_destination_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-import-stale-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _export_service, _workspace_service, _save_service, source, _exported = (
                self._initial_package(root, "source.localcat-project")
            )
            destination = root / "installed.localcat-project"
            destination.write_bytes(b"opaque-existing-target")
            service = ProjectPackageService()
            preview = service.preview_import(source, destination)
            before_bytes = destination.read_bytes()

            backend = compose_platform_file_backend(root)
            rooted = backend.bind_root(root)
            try:
                before = rooted.inspect_entry(destination.name)
                self.assertIsNotNone(before)
            finally:
                rooted.close()

            replacement = root / "replacement.tmp"
            replacement.write_bytes(before_bytes)
            os.replace(replacement, destination)
            rooted = backend.bind_root(root)
            try:
                after = rooted.inspect_entry(destination.name)
                self.assertIsNotNone(after)
                self.assertNotEqual(after.identity, before.identity)
            finally:
                rooted.close()

            with self.assertRaises(ProjectWorkspaceError) as stale:
                service.apply_import(preview.operation_id)
            self.assertEqual(stale.exception.code, "PROJECT.PACKAGE.DESTINATION_STALE")
            self.assertEqual(destination.read_bytes(), before_bytes)

    def test_controller_creation_cold_reopens_in_a_clean_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-controller-") as directory:
            root = Path(directory) / "root"
            source_root = root / "sources"
            source_root.mkdir(parents=True)
            first = _write_document(source_root, "chapters/a.json", "A")
            second = _write_document(source_root, "chapters/b.json", "B")
            target = root / "controller.localcat-project"
            appdata = root / "appdata"
            marker = root / "cold-open.json"
            from qt_editor import _compose_editor_controller
            from resource_repository import ResourceRepository

            controller, _composition = _compose_editor_controller(
                ResourceRepository(appdata)
            )
            created = controller.create_workspace_project_from_selected_files(
                source_root,
                (first, second),
                SelectedProjectDocumentsRequest("controller", "en", "zh-CN"),
                target,
            )
            self.assertEqual(created.receipt.document_count, 2)
            self.assertEqual(len(created.session.documents), 2)
            reopened = subprocess.run(
                [
                    sys.executable,
                    str(WORKER),
                    "controller-cold-open",
                    str(target),
                    str(appdata),
                    str(marker),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reopened.returncode, 0, reopened.stderr or reopened.stdout)
            facts = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(facts["document_count"], 2)
            self.assertEqual(facts["project_id"], created.session.project.project_id)

    def test_long_target_name_does_not_expand_owner_residue_components(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-long-name-") as directory:
            root = Path(directory) / "r"
            root.mkdir()
            long_name = "p" * 175 + ".localcat-project"
            target = root / long_name
            try:
                service, _workspace_service, _save_service, target, exported = (
                    self._initial_package(root, long_name)
                )
                self.assertEqual(
                    exported.save_report.journal_state,
                    SaveJournalState.COMMITTED,
                )
                self.assertEqual(len(service.open(target).workspace.documents), 2)
                owner_names = [
                    path.name
                    for path in root.iterdir()
                    if path.name.startswith(".localcat-project-")
                ]
                self.assertTrue(owner_names)
                self.assertTrue(all(len(name) <= 255 for name in owner_names))
            finally:
                # TemporaryDirectory cleanup may lack an extended path prefix.
                # Remove the deliberately long target through the rooted port.
                backend = compose_platform_file_backend(root)
                rooted = backend.bind_root(root)
                parent = backend.bind_parent(rooted, PureWindowsPath(target.name))
                try:
                    observed = parent.inspect_entry(target.name)
                    if observed is not None:
                        parent.unlink_owned(target.name, observed.identity)
                finally:
                    parent.close()
                    rooted.close()

    def test_fresh_member_open_rehashes_same_size_same_mtime_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-rehash-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            service, _workspace_service, _save_service, target, _exported = (
                self._initial_package(root)
            )
            opened = service.open(target)
            original_status = target.stat()
            corrupted = bytearray(target.read_bytes())
            marker = b'"schema":"localcat-project-package-manifest-v1"'
            index = corrupted.find(marker)
            self.assertGreaterEqual(index, 0)
            corrupted[index] ^= 1
            replacement = root / "replacement.tmp"
            replacement.write_bytes(corrupted)
            os.utime(
                replacement,
                ns=(original_status.st_atime_ns, original_status.st_mtime_ns),
            )
            os.replace(replacement, target)
            self.assertEqual(target.stat().st_size, original_status.st_size)
            self.assertEqual(target.stat().st_mtime_ns, original_status.st_mtime_ns)
            with self.assertRaises(ProjectWorkspaceError) as stale:
                with opened.open_member("manifest.json") as stream:
                    stream.read()
            self.assertEqual(stale.exception.code, "PROJECT.PACKAGE.SOURCE_STALE")

    def test_second_process_lock_contention_is_clean_prepublication_rejection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-lock-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            service, _workspace_service, _save_service, target, _exported = (
                self._initial_package(root)
            )
            opened = service.open(target)
            save_service = opened.create_save_service(session_id="contended", revision=4)
            save_service.workspace_service._workspace = replace(
                save_service.workspace_service.workspace,
                name="contended edit",
            )
            ready = root / "holder.ready"
            release = root / "holder.release"
            worker = subprocess.Popen(
                [sys.executable, str(WORKER), "hold-lock", str(root), str(target), str(ready), str(release)]
            )
            try:
                deadline = time.monotonic() + 20.0
                while not ready.exists() and worker.poll() is None:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                before = target.read_bytes()
                result = ProjectPackageService().save_workspace(
                    save_service,
                    target,
                    persistence_binding=opened.persistence_binding,
                )
                self.assertEqual(result.save_report.journal_state, SaveJournalState.CLEAN)
                self.assertEqual(target.read_bytes(), before)
            finally:
                release.write_text("release", encoding="ascii")
                self.assertEqual(worker.wait(timeout=20), 0)

    def test_open_destination_handle_never_clears_dirty_or_baseline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa03-share-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            service, _workspace_service, _save_service, target, _exported = (
                self._initial_package(root)
            )
            opened = service.open(target)
            save_service = opened.create_save_service(session_id="share", revision=5)
            save_service.workspace_service._workspace = replace(
                save_service.workspace_service.workspace,
                name="blocked edit",
            )
            before = target.read_bytes()
            backend = compose_platform_file_backend(root)
            rooted = backend.bind_root(root)
            retained = backend.open_regular(rooted, PureWindowsPath(target.name))
            try:
                result = ProjectPackageService().save_workspace(
                    save_service,
                    target,
                    persistence_binding=opened.persistence_binding,
                )
            finally:
                retained.close()
                rooted.close()
            self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(
                save_service.saved_workspace_snapshot,
                opened.workspace,
            )
            self.assertTrue(save_service.project_dirty)

    def test_prepublication_owner_faults_preserve_old_target_and_baseline(self) -> None:
        fault_sites = (
            "_build_package_snapshot",
            "_write_candidate_snapshot",
            "validate_candidate",
            "_copy_artifact",
        )
        for fault_site in fault_sites:
            with self.subTest(fault_site=fault_site), tempfile.TemporaryDirectory(
                prefix="localcat-wa03-prepublish-"
            ) as directory:
                root = Path(directory) / "root"
                root.mkdir()
                _service, _workspace_service, save_service, target, exported = (
                    self._initial_package(root)
                )
                before = target.read_bytes()
                baseline = save_service.saved_workspace_snapshot
                save_service.workspace_service._workspace = replace(
                    save_service.workspace_service.workspace,
                    name=f"fault-{fault_site}",
                )
                patch_target = (
                    f"project_package.{fault_site}"
                    if fault_site == "_build_package_snapshot"
                    else (
                        "project_package._WindowsProjectPackagePersistencePort."
                        + fault_site
                    )
                )
                with mock.patch(patch_target, side_effect=OSError("injected fault")):
                    result = ProjectPackageService().save_workspace(
                        save_service,
                        target,
                        persistence_binding=exported.persistence_binding,
                    )
                self.assertIsNone(result.receipt)
                self.assertNotEqual(
                    result.save_report.journal_state,
                    SaveJournalState.COMMITTED,
                )
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(save_service.saved_workspace_snapshot, baseline)
                self.assertTrue(save_service.project_dirty)
                recovery = ProjectPackageService()
                preview = recovery.inspect_recovery(target)
                if result.save_report.recovery_required:
                    self.assertIsNotNone(preview)
                    self.assertEqual(
                        preview.available_actions,
                        (RecoveryAction.ABANDON_STAGED_COPY,),
                    )
                    recovered = recovery.recover(
                        target,
                        preview.operation_id,
                        RecoveryAction.ABANDON_STAGED_COPY,
                    )
                    self.assertFalse(recovered.recovery_required)
                else:
                    self.assertIsNone(preview)
                self.assertIsNone(ProjectPackageService().inspect_recovery(target))

    def test_each_windows_publish_fault_converges_to_old_target(self) -> None:
        phases = (
            "publish_before_rename",
            "publish_after_rename",
            "publish_after_final_facts",
            "publish_before_destination_reopen",
            "publish_after_destination_reopen",
        )
        import project_package

        port_type = project_package._WindowsProjectPackagePersistencePort
        for fault_phase in phases:
            with self.subTest(fault_phase=fault_phase), tempfile.TemporaryDirectory(
                prefix="localcat-wa03-publish-fault-"
            ) as directory:
                root = Path(directory) / "root"
                root.mkdir()
                _service, _workspace_service, _save_service, target, _exported = (
                    self._initial_package(root)
                )
                before = target.read_bytes()
                state = {"armed": False, "fired": False}

                def fault(phase: str) -> None:
                    if (
                        state["armed"]
                        and not state["fired"]
                        and phase == fault_phase
                    ):
                        state["fired"] = True
                        raise OSError("injected platform publish fault")

                backend = WindowsPlatformAdapter(_fault_injector=fault)
                service = ProjectPackageService(backend=backend)
                opened = service.open(target)
                save_service = opened.create_save_service(
                    session_id="publish-fault",
                    revision=7,
                )
                save_service.workspace_service._workspace = replace(
                    save_service.workspace_service.workspace,
                    name=f"fault-{fault_phase}",
                )
                original_write_journal = port_type._write_journal

                def arm_after_publishing_journal(port, handle) -> None:
                    original_write_journal(port, handle)
                    if handle.phase is RecoveryPhase.PUBLISHING:
                        state["armed"] = True

                with mock.patch.object(
                    port_type,
                    "_write_journal",
                    new=arm_after_publishing_journal,
                ):
                    result = service.save_workspace(
                        save_service,
                        target,
                        persistence_binding=opened.persistence_binding,
                    )
                self.assertTrue(state["fired"])
                self.assertIsNone(result.receipt)
                self.assertNotEqual(
                    result.save_report.journal_state,
                    SaveJournalState.COMMITTED,
                )
                self.assertEqual(target.read_bytes(), before)
                self.assertTrue(save_service.project_dirty)
                self.assertIsNone(ProjectPackageService().inspect_recovery(target))

    def test_terminal_journal_cleanup_fault_is_cold_recoverable(self) -> None:
        import project_package

        with tempfile.TemporaryDirectory(prefix="localcat-wa03-cleanup-") as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _service, _workspace_service, _save_service, target, _exported = (
                self._initial_package(root)
            )
            opened = ProjectPackageService().open(target)
            save_service = opened.create_save_service(
                session_id="cleanup",
                revision=8,
            )
            save_service.workspace_service._workspace = replace(
                save_service.workspace_service.workspace,
                name="cleanup-new",
            )
            port_type = project_package._WindowsProjectPackagePersistencePort
            original_unlink = port_type._unlink
            failed = False

            def fail_journal_cleanup(port, path: Path) -> None:
                nonlocal failed
                if path == port._journal and not failed:
                    failed = True
                    raise OSError("injected journal cleanup fault")
                original_unlink(port, path)

            with mock.patch.object(port_type, "_unlink", new=fail_journal_cleanup):
                result = ProjectPackageService().save_workspace(
                    save_service,
                    target,
                    persistence_binding=opened.persistence_binding,
                )
            self.assertTrue(failed)
            self.assertIsNone(result.receipt)
            self.assertTrue(result.save_report.recovery_required)
            # C2B freezes this exact distinction: the first durable readback may
            # adopt the proved candidate baseline, while incomplete finalize
            # still forbids a success receipt and requires cold recovery.
            self.assertFalse(save_service.project_dirty)
            self.assertEqual(save_service.saved_workspace_snapshot.name, "cleanup-new")
            fresh = ProjectPackageService()
            preview = fresh.inspect_recovery(target)
            self.assertIsNotNone(preview)
            self.assertIs(preview.phase, RecoveryPhase.COMMIT_UNCERTAIN)
            report = fresh.recover(
                target,
                preview.operation_id,
                RecoveryAction.COMPLETE_COMMIT,
            )
            self.assertFalse(report.recovery_required)
            self.assertEqual(
                ProjectPackageService().open(target).workspace.name,
                "cleanup-new",
            )
            self.assertIsNone(ProjectPackageService().inspect_recovery(target))

    def test_process_termination_converges_to_old_or_new_via_exact_recovery(self) -> None:
        for phase in (
            RecoveryPhase.PUBLISHING,
            RecoveryPhase.PUBLISHED,
            RecoveryPhase.COMMIT_UNCERTAIN,
        ):
            with self.subTest(phase=phase.value), tempfile.TemporaryDirectory(
                prefix="localcat-wa03-restart-"
            ) as directory:
                root = Path(directory) / "root"
                root.mkdir()
                _service, _workspace_service, _save_service, target, _exported = (
                    self._initial_package(root)
                )
                old_name = ProjectPackageService().open(target).workspace.name
                killed = subprocess.run(
                    [sys.executable, str(WORKER), "kill-save", str(target), phase.value],
                    check=False,
                )
                self.assertEqual(killed.returncode, 73)
                recovery = ProjectPackageService()
                preview = recovery.inspect_recovery(target)
                self.assertIsNotNone(preview)
                self.assertEqual(preview.phase, phase)
                choice = (
                    RecoveryAction.ROLLBACK
                    if phase is RecoveryPhase.PUBLISHING
                    else RecoveryAction.COMPLETE_COMMIT
                )
                report = recovery.recover(target, preview.operation_id, choice)
                self.assertFalse(report.recovery_required)
                reopened = ProjectPackageService().open(target)
                if choice is RecoveryAction.ROLLBACK:
                    self.assertEqual(reopened.workspace.name, old_name)
                else:
                    self.assertEqual(reopened.workspace.name, f"edited-{phase.value}")
                self.assertIsNone(ProjectPackageService().inspect_recovery(target))


if __name__ == "__main__":
    unittest.main()
