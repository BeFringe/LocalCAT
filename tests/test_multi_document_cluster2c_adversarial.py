"""Adversarial acceptance for the complete Cluster 2C business surface.

These tests intentionally enter through public ProjectPackage, workspace, and
save APIs.  Filesystem faults simulate process interruption, but no private
package plan, candidate handle, or recovery journal is constructed or edited
by the tests.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib
import os
from pathlib import Path
import tempfile
from types import ModuleType
from unittest import mock
import unittest
import zipfile

from project_save import (
    ProjectSaveService,
    RecoveryAction,
    RecoveryPhase,
    SaveJournalState,
    WorkspaceSaveBaseline,
)
from project_workspace import (
    ProjectWorkspaceService,
    ReconciliationAssociation,
    ReconciliationDecision,
    ReconciliationDisposition,
)
from project_workspace_contracts import CodecPrivateMemberRef, SegmentIdentity
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)
from tests.test_multi_document_cluster2a_aggregation import (
    _fixture_bytes,
    _write_localcat_project,
)


_MANIFEST_LIMIT = 4 * 1024 * 1024


def _cluster2c() -> ModuleType:
    try:
        return importlib.import_module("project_package")
    except ModuleNotFoundError:
        raise AssertionError(
            "Cluster 2C RED: public module project_package is missing"
        ) from None


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stream_bytes(stream: object, *, block_size: int = 13) -> bytes:
    chunks: list[bytes] = []
    while True:
        block = stream.read(block_size)  # type: ignore[attr-defined]
        if not block:
            return b"".join(chunks)
        if type(block) is not bytes or len(block) > block_size:
            raise AssertionError("bounded member reader returned an invalid block")
        chunks.append(block)


def _write_json_project(
    root: Path,
    relative: str,
    segments: tuple[tuple[str, str, str, bool], ...],
) -> Path:
    return _write_localcat_project(root, relative, segments)


def _stage_json_pair(root: Path) -> object:
    first = _write_json_project(
        root,
        "chapters/a.json",
        (("shared", "Source A", "Target A", True),),
    )
    second = _write_json_project(
        root,
        "chapters/b.json",
        (("shared", "Source B", "Target B", True),),
    )
    return stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest(
            name="Portable project",
            source_locale="en",
            target_locale="zh-CN",
        ),
    )


def _workspace_and_save(staged: object, *, session: str, revision: int):
    workspace_service = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id=session,
        revision=revision,
    )
    return workspace_service, ProjectSaveService(workspace_service, baseline=None)


def _baseline(opened: object, *, revision: int) -> WorkspaceSaveBaseline:
    return WorkspaceSaveBaseline.from_workspace(
        opened.workspace,
        workspace_revision=revision,
        saved_package_digest=opened.validation.workspace_content_digest,
    )


def _edit_first_target(workspace: object, target: str) -> object:
    first = workspace.documents[0]
    overlay = first.editing_overlay[0]
    edited_first = replace(
        first,
        editing_overlay=(
            replace(
                overlay,
                target=target,
                confirmed=False,
            ),
            *first.editing_overlay[1:],
        ),
    )
    return replace(workspace, documents=(edited_first, *workspace.documents[1:]))


def _strict_manifest_only_zip(path: Path, payload: bytes) -> None:
    info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 10
    info.flag_bits = 0
    info.internal_attr = 0
    info.external_attr = ((0o100000 | 0o644) << 16)
    info.extra = b""
    info.comment = b""
    with path.open("w+b") as output:
        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            archive.writestr(info, payload)


class Cluster2CExportCopyAndBoundSaveTests(unittest.TestCase):
    def test_export_copy_never_adopts_save_baseline_and_bound_save_is_distinct(
        self,
    ) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            staged = _stage_json_pair(root)
            workspace_service, save_service = _workspace_and_save(
                staged,
                session="export-copy-session",
                revision=6,
            )
            package = module.ProjectPackageService()
            copy_a = root / "backup-a.localcat-project"
            copy_b = root / "backup-b.localcat-project"
            bound = root / "bound.localcat-project"
            before = (
                save_service.saved_workspace_snapshot,
                save_service.dirty_document_ids,
                save_service.manifest_dirty,
                save_service.project_dirty,
                workspace_service.revision,
            )

            receipt_a = package.export_copy(workspace_service, copy_a)
            self.assertEqual(
                (
                    save_service.saved_workspace_snapshot,
                    save_service.dirty_document_ids,
                    save_service.manifest_dirty,
                    save_service.project_dirty,
                    workspace_service.revision,
                ),
                before,
            )
            receipt_b = package.export_copy(workspace_service, copy_b)

            self.assertEqual(copy_a.read_bytes(), copy_b.read_bytes())
            self.assertIs(
                receipt_a.operation_kind,
                module.ProjectPackageOperationKind.EXPORT_COPY,
            )
            self.assertIs(
                receipt_b.operation_kind,
                module.ProjectPackageOperationKind.EXPORT_COPY,
            )
            self.assertEqual(receipt_a.workspace_revision, 6)
            self.assertEqual(receipt_b.workspace_revision, 6)
            self.assertEqual(save_service.saved_workspace_snapshot, before[0])
            self.assertEqual(save_service.dirty_document_ids, before[1])
            self.assertTrue(save_service.project_dirty)

            saved = package.save_workspace(save_service, bound)

            self.assertIs(saved.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertIs(
                saved.receipt.operation_kind,
                module.ProjectPackageOperationKind.SAVE,
            )
            self.assertEqual(saved.receipt.workspace_revision, 6)
            self.assertFalse(save_service.project_dirty)
            self.assertEqual(
                save_service.saved_workspace_snapshot,
                package.open(bound).workspace,
            )


class Cluster2CManagedBlobAndStreamingTests(unittest.TestCase):
    def test_cold_open_is_unbound_and_bound_save_reuses_package_managed_blobs(
        self,
    ) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            txt = root / "reader.txt"
            po = root / "catalog.po"
            txt.write_bytes(_fixture_bytes("line-text-valid.hex"))
            po.write_bytes(_fixture_bytes("gettext-po-valid.po"))
            source_payloads = (txt.read_bytes(), po.read_bytes())
            staged = stage_selected_project_documents(
                root,
                (txt, po),
                SelectedProjectDocumentsRequest(
                    name="Reader-only portable project",
                    source_locale="en",
                    target_locale="zh-CN",
                ),
            )
            first = staged.workspace.documents[0]
            opaque = root / "codec-private.bin"
            opaque.write_bytes(bytes(range(251)) * 4096)
            opaque_payload = opaque.read_bytes()
            opaque_digest = _sha_bytes(opaque_payload)
            member_path = (
                f"codec-private/{first.document_id}/{opaque_digest}.bin"
            )
            private_ref = CodecPrivateMemberRef(
                member_path=member_path,
                sha256=opaque_digest,
                byte_count=len(opaque_payload),
                codec_identity=first.codec_identity,
                profile_version="test-private-v1",
            )
            workspace = replace(
                staged.workspace,
                documents=(
                    replace(first, codec_private_member=private_ref),
                    staged.workspace.documents[1],
                ),
            )
            initial_workspace_service = ProjectWorkspaceService(
                workspace,
                staged.origin_binding,
                session_id="initial-reader-only",
                revision=4,
            )
            initial_save = ProjectSaveService(
                initial_workspace_service,
                baseline=None,
            )
            private_source = module.ProjectPackageBlobSource.from_path(
                document_id=first.document_id,
                member_path=member_path,
                path=opaque,
                expected_sha256=opaque_digest,
                expected_byte_count=len(opaque_payload),
            )
            target = root / "reader-only.localcat-project"
            module.ProjectPackageService().save_workspace(
                initial_save,
                target,
                codec_private_sources=(private_source,),
            )

            txt.unlink()
            po.unlink()
            opaque.unlink()
            opened = module.ProjectPackageService().open(target)
            cold_service = opened.create_workspace_service(
                session_id="cold-reader-only",
                revision=0,
            )

            self.assertIs(type(cold_service), ProjectWorkspaceService)
            self.assertIsNone(cold_service.origin_binding)
            unavailable = opened.codec_availability(())
            self.assertEqual(
                tuple(item.safe_warnings for item in unavailable),
                (("PROJECT.PACKAGE.CODEC_UNAVAILABLE",),) * 2,
            )
            self.assertEqual(
                tuple(
                    (
                        document.writer_capability_snapshot.canonical_write,
                        document.writer_capability_snapshot.source_round_trip_write,
                    )
                    for document in cold_service.workspace.documents
                ),
                ((False, False), (False, False)),
            )
            for entry, expected in zip(
                opened.manifest.documents,
                source_payloads,
                strict=True,
            ):
                with opened.open_member(entry.source_member.path) as reader:
                    self.assertEqual(_stream_bytes(reader), expected)
            with opened.open_member(
                member_path,
                codec_identity=first.codec_identity,
            ) as reader:
                self.assertEqual(_stream_bytes(reader, block_size=17), opaque_payload)

            edited = _edit_first_target(cold_service.workspace, "cold package edit")
            edited_service = ProjectWorkspaceService(
                edited,
                None,
                session_id="cold-reader-only",
                revision=1,
            )
            save_service = ProjectSaveService(
                edited_service,
                baseline=_baseline(opened, revision=0),
            )
            saved = module.ProjectPackageService().save_workspace(
                save_service,
                target,
                persistence_binding=opened.persistence_binding,
            )
            reopened = module.ProjectPackageService().open(target)

            self.assertIs(
                saved.save_report.journal_state,
                SaveJournalState.COMMITTED,
                repr(saved.save_report),
            )
            self.assertEqual(
                reopened.workspace.documents[0].editing_overlay[0].target,
                "cold package edit",
            )
            self.assertFalse(
                reopened.workspace.documents[0].editing_overlay[0].confirmed
            )
            for entry, expected in zip(
                reopened.manifest.documents,
                source_payloads,
                strict=True,
            ):
                with reopened.open_member(entry.source_member.path) as reader:
                    self.assertEqual(_stream_bytes(reader), expected)
            with reopened.open_member(
                member_path,
                codec_identity=first.codec_identity,
            ) as reader:
                self.assertEqual(_stream_bytes(reader), opaque_payload)

    def test_manifest_limit_is_rejected_before_json_materialization(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            oversized = root / "oversized-manifest.localcat-project"
            _strict_manifest_only_zip(oversized, b" " * (_MANIFEST_LIMIT + 1))

            with self.assertRaises(ProjectWorkspaceError) as caught:
                module.ProjectPackageService().validate(oversized)

            self.assertEqual(caught.exception.code, "PROJECT.PACKAGE.LIMIT_EXCEEDED")
            self.assertEqual(str(caught.exception), caught.exception.code)

    def test_member_reader_is_declared_only_bounded_and_body_safe(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            staged = _stage_json_pair(root)
            workspace_service, save_service = _workspace_and_save(
                staged,
                session="stream-session",
                revision=0,
            )
            del workspace_service
            target = root / "stream.localcat-project"
            module.ProjectPackageService().save_workspace(save_service, target)
            opened = module.ProjectPackageService().open(target)
            declared = opened.manifest.documents[0].source_member

            with opened.open_member(declared.path) as reader:
                self.assertEqual(
                    _sha_bytes(_stream_bytes(reader, block_size=7)),
                    declared.sha256,
                )
            with self.assertRaises(ProjectWorkspaceError) as caught:
                with opened.open_member("sources/secret/not-declared.bin"):
                    pass
            self.assertEqual(caught.exception.code, "PROJECT.PACKAGE.MEMBER_INVALID")
            self.assertNotIn(str(root), str(caught.exception))
            self.assertNotIn("Source A", str(caught.exception))


class Cluster2CPublicColdRecoveryTests(unittest.TestCase):
    def _interrupted_save(
        self,
        root: Path,
        phase: RecoveryPhase,
    ) -> tuple[ModuleType, Path, bytes, object]:
        module = _cluster2c()
        staged = _stage_json_pair(root)
        workspace_service, first_save = _workspace_and_save(
            staged,
            session=f"recovery-initial-{phase.value}",
            revision=0,
        )
        target = root / "recover.localcat-project"
        module.ProjectPackageService().save_workspace(first_save, target)
        lkg_bytes = target.read_bytes()
        opened = module.ProjectPackageService().open(target)
        edited_workspace = replace(opened.workspace, name=f"candidate-{phase.value}")
        edited_service = ProjectWorkspaceService(
            edited_workspace,
            None,
            session_id=f"recovery-edited-{phase.value}",
            revision=1,
        )
        save_service = ProjectSaveService(
            edited_service,
            baseline=_baseline(opened, revision=0),
        )

        if phase is RecoveryPhase.STAGING:
            with mock.patch(
                "project_package.zipfile.ZipFile",
                side_effect=KeyboardInterrupt("simulated process stop"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    module.ProjectPackageService().save_workspace(
                        save_service,
                        target,
                        persistence_binding=opened.persistence_binding,
                    )
        elif phase is RecoveryPhase.PUBLISHING:
            real_replace = os.replace

            def interrupt_before_target_replace(source, destination, *args, **kwargs):
                if Path(destination) == target or (
                    destination == target.name
                    and kwargs.get("dst_dir_fd") is not None
                ):
                    raise KeyboardInterrupt("simulated process stop")
                return real_replace(source, destination, *args, **kwargs)

            with mock.patch(
                "project_package.os.replace",
                side_effect=interrupt_before_target_replace,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    module.ProjectPackageService().save_workspace(
                        save_service,
                        target,
                        persistence_binding=opened.persistence_binding,
                    )
        elif phase is RecoveryPhase.PUBLISHED:
            old_digest = _sha_bytes(lkg_bytes)
            real_open = os.open

            def interrupt_first_readback(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is None:
                    descriptor = real_open(path, flags, mode)
                else:
                    descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if (
                    path == target.name
                    and dir_fd is not None
                    and flags & os.O_ACCMODE == os.O_RDONLY
                ):
                    status = os.fstat(descriptor)
                    payload = os.pread(descriptor, status.st_size, 0)
                    if _sha_bytes(payload) != old_digest:
                        os.close(descriptor)
                        raise KeyboardInterrupt("simulated process stop")
                return descriptor

            with mock.patch(
                "project_package.os.open",
                side_effect=interrupt_first_readback,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    module.ProjectPackageService().save_workspace(
                        save_service,
                        target,
                        persistence_binding=opened.persistence_binding,
                    )
        else:  # pragma: no cover - the table below is deliberately closed.
            raise AssertionError("unsupported recovery phase")
        return module, target, lkg_bytes, edited_workspace

    def test_fresh_service_recovers_staging_publishing_and_published(self) -> None:
        expected = {
            RecoveryPhase.STAGING: (
                (RecoveryAction.ABANDON_STAGED_COPY,),
                RecoveryAction.ABANDON_STAGED_COPY,
                False,
            ),
            RecoveryPhase.PUBLISHING: (
                (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK),
                RecoveryAction.COMPLETE_COMMIT,
                True,
            ),
            RecoveryPhase.PUBLISHED: (
                (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK),
                RecoveryAction.ROLLBACK,
                False,
            ),
        }
        for phase, (actions, choice, expect_candidate) in expected.items():
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve() / "root"
                    root.mkdir()
                    module, target, lkg_bytes, candidate_workspace = (
                        self._interrupted_save(root, phase)
                    )
                    fresh = module.ProjectPackageService()

                    preview = fresh.inspect_recovery(target)

                    self.assertIs(preview.phase, phase)
                    self.assertEqual(preview.available_actions, actions)
                    self.assertEqual(preview.safe_codes, ())
                    self.assertNotIn(str(root), repr(preview))
                    self.assertNotIn("Source A", repr(preview))
                    report = fresh.recover(
                        target,
                        preview.operation_id,
                        choice,
                    )
                    self.assertFalse(report.recovery_required)
                    self.assertIsNone(fresh.inspect_recovery(target))
                    if expect_candidate:
                        self.assertEqual(
                            module.ProjectPackageService().open(target).workspace,
                            candidate_workspace,
                        )
                    else:
                        self.assertEqual(target.read_bytes(), lkg_bytes)


class Cluster2CSameProjectImportAndReceiptTests(unittest.TestCase):
    def _same_project_packages(self, root: Path):
        module = _cluster2c()
        primary = _write_json_project(
            root,
            "primary.json",
            (
                ("unchanged", "Same source", "keep-confirmed", True),
                ("changed", "Old source", "keep-draft", True),
                ("removed", "Removed source", "recover-removed", True),
                ("ambiguous-old", "Old ambiguous", "recover-ambiguous", True),
                ("unresolved-old", "Old unresolved", "recover-unresolved", True),
            ),
        )
        anchor = _write_json_project(
            root,
            "anchor.json",
            (("anchor", "Anchor source", "anchor-target", True),),
        )
        request = SelectedProjectDocumentsRequest(
            name="Same project",
            source_locale="en",
            target_locale="zh-CN",
        )
        current = stage_selected_project_documents(root, (primary, anchor), request)
        current_workspace_service, current_save = _workspace_and_save(
            current,
            session="current-export",
            revision=0,
        )
        current_package = root / "current.localcat-project"
        module.ProjectPackageService().save_workspace(current_save, current_package)

        _write_json_project(
            root,
            "primary.json",
            (
                ("unchanged", "Same source", "incoming-must-not-win", False),
                ("changed", "New source", "incoming-must-not-win", True),
                ("new", "Brand new source", "incoming-new-target", True),
                ("ambiguous-new-1", "Ambiguous one", "candidate-one", False),
                ("ambiguous-new-2", "Ambiguous two", "candidate-two", False),
            ),
        )
        incoming = stage_selected_project_documents(
            root,
            (primary, anchor),
            SelectedProjectDocumentsRequest(
                name="Same project",
                source_locale="en",
                target_locale="zh-CN",
                origin_binding=current.origin_binding,
                expected_binding_revision=current.origin_binding.revision,
            ),
        )
        incoming_workspace_service, incoming_save = _workspace_and_save(
            incoming,
            session="incoming-export",
            revision=0,
        )
        del incoming_workspace_service
        incoming_package = root / "incoming.localcat-project"
        module.ProjectPackageService().save_workspace(incoming_save, incoming_package)
        active = ProjectWorkspaceService(
            current_workspace_service.workspace,
            current.origin_binding,
            session_id="active-session",
            revision=11,
        )
        document_id = current.workspace.documents[0].document_id
        associations = (
            ReconciliationAssociation(
                current_identity=SegmentIdentity(document_id, "ambiguous-old"),
                incoming_identities=(
                    SegmentIdentity(document_id, "ambiguous-new-1"),
                    SegmentIdentity(document_id, "ambiguous-new-2"),
                ),
            ),
            ReconciliationAssociation(
                current_identity=SegmentIdentity(document_id, "unresolved-old"),
                incoming_identities=(),
            ),
        )
        decisions = (
            ReconciliationDecision(
                identity=SegmentIdentity(document_id, "removed"),
                disposition=ReconciliationDisposition.REMOVE,
            ),
            ReconciliationDecision(
                identity=SegmentIdentity(document_id, "ambiguous-old"),
                disposition=ReconciliationDisposition.ACCEPT_ASSOCIATION,
                accepted_incoming_identity=SegmentIdentity(
                    document_id,
                    "ambiguous-new-1",
                ),
            ),
            ReconciliationDecision(
                identity=SegmentIdentity(document_id, "unresolved-old"),
                disposition=ReconciliationDisposition.KEEP_DETACHED,
            ),
        )
        return (
            module,
            current_package,
            incoming_package,
            active,
            associations,
            decisions,
        )

    def test_same_source_bytes_can_take_exact_private_replacement_from_incoming_package(
        self,
    ) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            staged = _stage_json_pair(root)
            first = staged.workspace.documents[0]

            def private_facts(label: str) -> tuple[bytes, str, CodecPrivateMemberRef]:
                payload = (label.encode("ascii") + b"\0") * 257
                digest = _sha_bytes(payload)
                reference = CodecPrivateMemberRef(
                    member_path=(
                        f"codec-private/{first.document_id}/{digest}.bin"
                    ),
                    sha256=digest,
                    byte_count=len(payload),
                    codec_identity=first.codec_identity,
                    profile_version="test-private-v1",
                )
                return payload, digest, reference

            old_payload, old_digest, old_reference = private_facts("old-private")
            current_workspace = replace(
                staged.workspace,
                documents=(
                    replace(first, codec_private_member=old_reference),
                    *staged.workspace.documents[1:],
                ),
            )
            current_service = ProjectWorkspaceService(
                current_workspace,
                staged.origin_binding,
                session_id="private-current-export",
                revision=0,
            )
            old_blob = root / "old-private.bin"
            old_blob.write_bytes(old_payload)
            current_package = root / "private-current.localcat-project"
            module.ProjectPackageService().save_workspace(
                ProjectSaveService(current_service, baseline=None),
                current_package,
                codec_private_sources=(
                    module.ProjectPackageBlobSource.from_path(
                        document_id=first.document_id,
                        member_path=old_reference.member_path,
                        path=old_blob,
                        expected_sha256=old_digest,
                        expected_byte_count=len(old_payload),
                    ),
                ),
            )

            new_payload, new_digest, new_reference = private_facts("new-private")
            new_fingerprint = _sha_bytes(b"new codec source state")
            incoming_first = replace(
                first,
                source_segments=tuple(
                    replace(segment, source_fingerprint=new_fingerprint)
                    for segment in first.source_segments
                ),
                editing_overlay=tuple(
                    replace(overlay, source_fingerprint=new_fingerprint)
                    for overlay in first.editing_overlay
                ),
                codec_private_member=new_reference,
            )
            incoming_workspace = replace(
                staged.workspace,
                documents=(incoming_first, *staged.workspace.documents[1:]),
            )
            incoming_service = ProjectWorkspaceService(
                incoming_workspace,
                staged.origin_binding,
                session_id="private-incoming-export",
                revision=0,
            )
            new_blob = root / "new-private.bin"
            new_blob.write_bytes(new_payload)
            incoming_package = root / "private-incoming.localcat-project"
            module.ProjectPackageService().save_workspace(
                ProjectSaveService(incoming_service, baseline=None),
                incoming_package,
                codec_private_sources=(
                    module.ProjectPackageBlobSource.from_path(
                        document_id=first.document_id,
                        member_path=new_reference.member_path,
                        path=new_blob,
                        expected_sha256=new_digest,
                        expected_byte_count=len(new_payload),
                    ),
                ),
            )

            active = ProjectWorkspaceService(
                current_workspace,
                staged.origin_binding,
                session_id="private-active",
                revision=9,
            )
            packages = module.ProjectPackageService()
            preview = packages.preview_import(
                incoming_package,
                current_package,
                workspace_service=active,
                associations=(),
            )
            self.assertEqual(preview.source_changed_count, 1)
            self.assertEqual(preview.blocking_reasons, ())

            packages.apply_import(
                preview.operation_id,
                decisions=(),
                session_id=active.session_id,
                base_revision=active.revision,
            )
            installed = module.ProjectPackageService().open(current_package)
            self.assertEqual(
                installed.workspace.documents[0].codec_private_member,
                new_reference,
            )
            self.assertEqual(
                installed.codec_availability((first.codec_identity,))[0].available,
                True,
            )
            with installed.open_member(
                new_reference.member_path,
                codec_identity=first.codec_identity,
            ) as reader:
                self.assertEqual(_stream_bytes(reader), new_payload)

    def test_same_project_preview_uses_six_states_and_stale_or_failure_is_zero_active_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            (
                module,
                destination,
                incoming,
                active,
                associations,
                decisions,
            ) = self._same_project_packages(root)
            package = module.ProjectPackageService()

            preview = package.preview_import(
                incoming,
                destination,
                workspace_service=active,
                associations=associations,
            )
            self.assertIs(preview.mode, module.ProjectPackageImportMode.UPDATE_SAME_PROJECT)
            self.assertEqual(
                (
                    preview.unchanged_count,
                    preview.source_changed_count,
                    preview.new_count,
                    preview.removed_count,
                    preview.ambiguous_count,
                    preview.unresolved_count,
                ),
                (2, 1, 1, 1, 1, 1),
            )
            self.assertEqual(len(preview.required_decision_identities), 3)
            destination_before = destination.read_bytes()
            active_before = (active.workspace, active.origin_binding, active.revision)
            with self.assertRaises(ProjectWorkspaceError) as required:
                package.apply_import(
                    preview.operation_id,
                    decisions=(),
                    session_id=active.session_id,
                    base_revision=active.revision,
                )
            self.assertEqual(
                required.exception.code,
                "PROJECT.RECONCILE.DECISION_REQUIRED",
            )
            self.assertEqual(destination.read_bytes(), destination_before)
            self.assertEqual(
                (active.workspace, active.origin_binding, active.revision),
                active_before,
            )

            stale_preview = package.preview_import(
                incoming,
                destination,
                workspace_service=active,
                associations=associations,
            )
            active_noop = active.stage_workspace_reconciliation(
                active.workspace,
                associations=(),
                session_id=active.session_id,
                base_revision=active.revision,
            )
            active.apply_workspace_reconciliation(
                active_noop.operation_id,
                incoming=active.workspace,
                decisions=(),
                session_id=active.session_id,
                base_revision=active.revision,
            )
            after_intentional_revision = (
                active.workspace,
                active.origin_binding,
                active.revision,
            )
            with self.assertRaises(ProjectWorkspaceError) as stale:
                package.apply_import(
                    stale_preview.operation_id,
                    decisions=decisions,
                    session_id="active-session",
                    base_revision=11,
                )
            self.assertEqual(stale.exception.code, "PROJECT.PACKAGE.PREVIEW_STALE")
            self.assertEqual(destination.read_bytes(), destination_before)
            self.assertEqual(
                (active.workspace, active.origin_binding, active.revision),
                after_intentional_revision,
            )

            failure_preview = package.preview_import(
                incoming,
                destination,
                workspace_service=active,
                associations=associations,
            )
            real_replace = os.replace
            interrupted = False

            def fail_first_destination_replace(source, target, *args, **kwargs):
                nonlocal interrupted
                if (
                    Path(target) == destination
                    or (
                        target == destination.name
                        and kwargs.get("dst_dir_fd") is not None
                    )
                ) and not interrupted:
                    interrupted = True
                    raise OSError("private source body must not escape")
                return real_replace(source, target, *args, **kwargs)

            active_before_failure = (
                active.workspace,
                active.origin_binding,
                active.revision,
            )
            with mock.patch(
                "project_package.os.replace",
                side_effect=fail_first_destination_replace,
            ):
                with self.assertRaises(ProjectWorkspaceError) as failed:
                    package.apply_import(
                        failure_preview.operation_id,
                        decisions=decisions,
                        session_id=active.session_id,
                        base_revision=active.revision,
                    )
            self.assertIn(
                failed.exception.code,
                {
                    "PROJECT.PACKAGE.APPLY_FAILED",
                    "PROJECT.PACKAGE.RECOVERY_REQUIRED",
                },
            )
            self.assertEqual(str(failed.exception), failed.exception.code)
            self.assertEqual(
                (active.workspace, active.origin_binding, active.revision),
                active_before_failure,
            )
            self.assertEqual(destination.read_bytes(), destination_before)

    def test_success_receipt_freezes_operation_mode_revision_members_and_reconciliation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            (
                module,
                destination,
                incoming,
                active,
                associations,
                decisions,
            ) = self._same_project_packages(root)
            package = module.ProjectPackageService()
            preview = package.preview_import(
                incoming,
                destination,
                workspace_service=active,
                associations=associations,
            )
            active_before = (active.workspace, active.origin_binding, active.revision)

            receipt = package.apply_import(
                preview.operation_id,
                decisions=decisions,
                session_id=active.session_id,
                base_revision=active.revision,
            )
            installed = module.ProjectPackageService().open(destination)

            self.assertIs(
                receipt.operation_kind,
                module.ProjectPackageOperationKind.RECONCILE_IMPORT,
            )
            self.assertIs(receipt.mode, module.ProjectPackageImportMode.UPDATE_SAME_PROJECT)
            self.assertEqual(receipt.workspace_revision, 12)
            with installed.open_member("manifest.json") as manifest_reader:
                manifest_payload = _stream_bytes(manifest_reader)
            expected_references = tuple(
                reference
                for document in installed.manifest.documents
                for reference in (
                    document.document_member,
                    document.source_member,
                    document.codec_private_member,
                )
                if reference is not None
            )
            expected_member_facts = {
                "manifest.json": (_sha_bytes(manifest_payload), len(manifest_payload)),
                **{
                    reference.path: (reference.sha256, reference.byte_count)
                    for reference in expected_references
                },
            }
            self.assertEqual(
                {
                    item.path: (item.sha256, item.byte_count)
                    for item in receipt.member_digests
                },
                expected_member_facts,
            )
            self.assertEqual(
                tuple(result.document_id for result in receipt.document_results),
                tuple(document.document_id for document in installed.manifest.documents),
            )
            self.assertEqual(
                tuple(result.status for result in receipt.document_results),
                ("reconciled",) * len(installed.manifest.documents),
            )
            self.assertEqual(
                (
                    receipt.reconciliation.base_revision,
                    receipt.reconciliation.published_revision,
                    tuple(
                        identity.local_segment_id
                        for identity in receipt.reconciliation.removed_identities
                    ),
                    tuple(
                        identity.local_segment_id
                        for identity in receipt.reconciliation.detached_identities
                    ),
                    tuple(
                        identity.local_segment_id
                        for identity in receipt.reconciliation.accepted_association_identities
                    ),
                ),
                (11, 12, ("removed",), ("unresolved-old",), ("ambiguous-old",)),
            )
            invalid_reconciliations = (
                replace(
                    receipt.reconciliation,
                    project_id="prj-" + "0" * 64,
                ),
                replace(
                    receipt.reconciliation,
                    base_revision=20,
                    published_revision=21,
                ),
                replace(
                    receipt.reconciliation,
                    published_workspace_digest="0" * 64,
                ),
            )
            for forged in invalid_reconciliations:
                with self.subTest(forged=forged):
                    with self.assertRaises(ProjectWorkspaceError):
                        replace(receipt, reconciliation=forged)
            with self.assertRaises(FrozenInstanceError):
                receipt.operation_kind = module.ProjectPackageOperationKind.IMPORT
            with self.assertRaises(FrozenInstanceError):
                receipt.document_results[0].status = "installed"
            self.assertEqual(
                (active.workspace, active.origin_binding, active.revision),
                active_before,
            )


if __name__ == "__main__":
    unittest.main()
