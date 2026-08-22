"""Cluster 2B RED contracts for carrier-neutral save and cold recovery.

The fake persistence port in this module is deliberately an in-memory contract
model.  It does not choose the Cluster 2C package carrier and it does not
enable a directory, workbook, TXT, PO, or POT source writer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib
from pathlib import Path
import tempfile
from types import ModuleType
from typing import get_args, get_type_hints
import unittest
from unittest.mock import PropertyMock, patch

from parser_contracts import CodecIdentity, SourceSnapshotIdentity
from project_workspace_contracts import (
    CodecPrivateMemberRef,
    EditingOverlayEntry,
    OriginBinding,
    OriginBindingDocument,
    ProjectDocument,
    ProjectOrigin,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectSourceSegment,
    ProjectWorkspace,
    SourcePresence,
    StagedSelectedProjectDocuments,
    WriterCapabilitySnapshot,
)
from project_workspace import ProjectWorkspaceService
from project_workspace_identity import (
    ProjectWorkspaceError,
    editing_state_digest_v1,
)


_PROJECT_ID = "prj-" + "7" * 64
_DOCUMENT_A = "doc-" + "a" * 64
_DOCUMENT_B = "doc-" + "b" * 64
_DOCUMENT_C = "doc-" + "c" * 64
_DOCUMENT_D = "doc-" + "d" * 64
_SOURCE_A = hashlib.sha256(b"source-a").hexdigest()
_SOURCE_B = hashlib.sha256(b"source-b").hexdigest()
_SOURCE_C = hashlib.sha256(b"source-c").hexdigest()


def _cluster2b() -> ModuleType:
    try:
        module = importlib.import_module("project_save")
    except ModuleNotFoundError:
        raise AssertionError(
            "Cluster 2B RED: public module project_save is missing"
        ) from None
    required = (
        "DocumentSaveResult",
        "DocumentSaveStatus",
        "PendingRecoveryFacts",
        "ProjectDocumentWriterPort",
        "ProjectRecoveryReport",
        "ProjectSaveReport",
        "ProjectSaveService",
        "ProjectWorkspacePersistencePort",
        "RecoveryAction",
        "RecoveryPhase",
        "SaveJournalState",
        "WorkspaceSaveBaseline",
    )
    missing = tuple(name for name in required if not hasattr(module, name))
    if missing:
        raise AssertionError(
            f"Cluster 2B RED: save/recovery public contract is missing {missing!r}"
        )
    return module


def _saved_digest(
    document_id: str,
    local_segment_id: str,
    source_fingerprint: str,
    target: str,
    confirmed: bool,
) -> str:
    return editing_state_digest_v1(
        document_id,
        local_segment_id,
        source_fingerprint,
        target,
        confirmed,
    )


def _document(
    *,
    document_id: str,
    source_ref: str,
    order: int,
    format_id: str,
    codec_identity: CodecIdentity,
    source_fingerprint: str,
    target: str,
    confirmed: bool,
) -> ProjectDocument:
    local_id = "shared"
    return ProjectDocument(
        document_id=document_id,
        source_ref=source_ref,
        display_name=source_ref,
        order=order,
        format_id=format_id,
        codec_identity=codec_identity,
        writer_capability_snapshot=WriterCapabilitySnapshot(
            canonical_write=False,
            source_round_trip_write=False,
            format_profile=format_id,
        ),
        source_snapshot_digest=source_fingerprint,
        source_segments=(
            ProjectSourceSegment(
                local_segment_id=local_id,
                source=f"Source for {source_ref}",
                raw_speaker="",
                source_fingerprint=source_fingerprint,
            ),
        ),
        editing_overlay=(
            EditingOverlayEntry(
                document_id=document_id,
                local_segment_id=local_id,
                source_fingerprint=source_fingerprint,
                target=target,
                confirmed=confirmed,
                saved_state_digest=_saved_digest(
                    document_id,
                    local_id,
                    source_fingerprint,
                    target,
                    confirmed,
                ),
            ),
        ),
        codec_private_member=None,
    )


def _baseline_workspace() -> ProjectWorkspace:
    return ProjectWorkspace(
        schema_version=1,
        project_id=_PROJECT_ID,
        name="Reader-only multi-document project",
        source_locale="en",
        target_locale="zh-CN",
        origin=ProjectOrigin(
            kind=ProjectOriginKind.DIRECTORY,
            profile_version="explicit-selected-files-v1",
            portable_root_ref="selected-sources",
        ),
        persistence_kind=ProjectPersistenceKind.PROJECT_PACKAGE,
        documents=(
            _document(
                document_id=_DOCUMENT_A,
                source_ref="chapter-a.txt",
                order=0,
                format_id="line-text-v1",
                codec_identity=CodecIdentity("localcat", "line-text", "1"),
                source_fingerprint=_SOURCE_A,
                target="old-a",
                confirmed=True,
            ),
            _document(
                document_id=_DOCUMENT_B,
                source_ref="chapter-b.po",
                order=1,
                format_id="gettext-po-v1",
                codec_identity=CodecIdentity("localcat", "gettext-po", "1"),
                source_fingerprint=_SOURCE_B,
                target="old-b",
                confirmed=False,
            ),
        ),
    )


def _edit_document(
    workspace: ProjectWorkspace,
    document_id: str,
    *,
    target: str,
    confirmed: bool,
) -> ProjectWorkspace:
    documents: list[ProjectDocument] = []
    for document in workspace.documents:
        if document.document_id != document_id:
            documents.append(document)
            continue
        overlay = document.editing_overlay[0]
        documents.append(
            replace(
                document,
                editing_overlay=(
                    replace(
                        overlay,
                        target=target,
                        confirmed=confirmed,
                        # The saved digest is intentionally the old baseline.
                    ),
                ),
            )
        )
    return replace(workspace, documents=tuple(documents))


def _targets(workspace: ProjectWorkspace) -> tuple[str, ...]:
    return tuple(
        document.editing_overlay[0].target for document in workspace.documents
    )


def _binding(workspace: ProjectWorkspace) -> OriginBinding:
    attached_documents = tuple(
        document
        for document in workspace.documents
        if any(
            source.source_presence is SourcePresence.ATTACHED
            for source in document.source_segments
        )
    )
    return OriginBinding(
        schema_version=1,
        project_id=workspace.project_id,
        profile_version="explicit-selected-files-v1",
        absolute_root="/virtual/localcat-selected-sources",
        root_device=41,
        root_inode=43,
        revision=1,
        documents=tuple(
            OriginBindingDocument(
                source_ref=document.source_ref,
                document_id=document.document_id,
                format_id=document.format_id,
                codec_identity=document.codec_identity,
                source_identity=SourceSnapshotIdentity(
                    relative_reference_sha256=hashlib.sha256(
                        document.source_ref.encode("utf-8")
                    ).hexdigest(),
                    regular_file_identity=f"regular-{index}",
                    original_size=1,
                    original_mtime_ns=1,
                    content_sha256=document.source_snapshot_digest,
                    byte_count=1,
                    schema_version=1,
                ),
            )
            for index, document in enumerate(attached_documents)
        ),
    )


@dataclass(slots=True)
class _Candidate:
    operation_id: str
    candidate_workspace: ProjectWorkspace
    last_known_good_workspace: ProjectWorkspace | None
    requested_document_ids: tuple[str, ...]


@dataclass(slots=True)
class _RecoveryRecord:
    candidate: _Candidate
    phase: str


class _InMemoryPersistencePort:
    """Neutral phase/fault model consumed by the production coordinator."""

    def __init__(
        self,
        last_known_good_workspace: ProjectWorkspace | None,
        *,
        faults: tuple[str, ...] = (),
        after_validation: object | None = None,
    ) -> None:
        self.installed_workspace = last_known_good_workspace
        self.last_known_good_workspace = last_known_good_workspace
        self.faults = frozenset(faults)
        self.calls: list[str] = []
        self.staged: list[_Candidate] = []
        self.pending: _RecoveryRecord | None = None
        self.after_validation = after_validation

    def _fault(self, phase: str) -> None:
        if phase in self.faults:
            raise OSError(f"/private/carrier/{phase}/source-body")

    def stage_candidate(
        self,
        *,
        operation_id: str,
        candidate_workspace: ProjectWorkspace,
        last_known_good_workspace: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
    ) -> object:
        self.calls.append("stage")
        candidate = _Candidate(
            operation_id=operation_id,
            candidate_workspace=candidate_workspace,
            last_known_good_workspace=last_known_good_workspace,
            requested_document_ids=requested_document_ids,
        )
        if "stage_residue" in self.faults:
            self.staged.append(candidate)
            self.pending = _RecoveryRecord(candidate, "staged")
            raise OSError("/private/carrier/stage-residue/source-body")
        self._fault("stage")
        self.staged.append(candidate)
        return candidate

    def validate_candidate(self, candidate_handle: object) -> None:
        self.calls.append("validation")
        candidate = self._require_candidate(candidate_handle)
        if "validation_residue" in self.faults:
            self.pending = _RecoveryRecord(candidate, "staged")
            raise OSError("/private/carrier/validation-residue/source-body")
        self._fault("validation")
        if self.after_validation is not None:
            callback = self.after_validation
            if not callable(callback):
                raise AssertionError("test validation callback is not callable")
            callback()

    def arm_publication(self, candidate_handle: object) -> None:
        self.calls.append("arm")
        candidate = self._require_candidate(candidate_handle)
        if "arm_residue" in self.faults:
            self.pending = _RecoveryRecord(candidate, "armed")
            raise OSError("/private/carrier/arm-residue/source-body")
        self._fault("arm")
        self.pending = _RecoveryRecord(candidate, "armed")

    def publish_candidate(self, candidate_handle: object) -> None:
        self.calls.append("publication")
        candidate = self._require_candidate(candidate_handle)
        self._fault("publication_before")
        self.installed_workspace = candidate.candidate_workspace
        assert self.pending is not None
        self.pending.phase = "published"
        self._fault("publication_after")

    def readback_candidate(self, candidate_handle: object) -> ProjectWorkspace:
        self.calls.append("readback")
        candidate = self._require_candidate(candidate_handle)
        self._fault("readback")
        if "readback_mismatch" in self.faults:
            return candidate.last_known_good_workspace
        return self.installed_workspace

    def commit_candidate(self, candidate_handle: object) -> None:
        self.calls.append("commit")
        candidate = self._require_candidate(candidate_handle)
        assert self.pending is not None
        self.pending.phase = "commit-uncertain"
        self._fault("commit")
        self.last_known_good_workspace = candidate.candidate_workspace
        self.installed_workspace = candidate.candidate_workspace
        self.pending = None

    def rollback_candidate(
        self, candidate_handle: object
    ) -> ProjectWorkspace | None:
        self.calls.append("rollback")
        candidate = self._require_candidate(candidate_handle)
        self._fault("rollback")
        self.installed_workspace = candidate.last_known_good_workspace
        self.last_known_good_workspace = candidate.last_known_good_workspace
        if "rollback_readback_mismatch" in self.faults:
            return candidate.candidate_workspace
        self.pending = None
        return self.installed_workspace

    def inspect_pending_recovery(self) -> object | None:
        self.calls.append("recovery-inspect")
        self._fault("recovery_inspect")
        return self.pending

    def describe_pending_recovery(self, recovery_handle: object) -> object:
        self.calls.append("recovery-describe")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_describe")
        module = _cluster2b()
        phase = next(
            item
            for item in module.RecoveryPhase
            if item.value == record.phase
        )
        lkg = record.candidate.last_known_good_workspace
        return module.PendingRecoveryFacts(
            operation_id=record.candidate.operation_id,
            project_id=record.candidate.candidate_workspace.project_id,
            phase=phase,
            candidate_digest=_workspace_content_digest(
                record.candidate.candidate_workspace
            ),
            last_known_good_digest=(
                None if lkg is None else _workspace_content_digest(lkg)
            ),
        )

    def read_recovery_last_known_good(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        self.calls.append("recovery-read-lkg")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_read_lkg")
        return record.candidate.last_known_good_workspace

    def read_recovery_candidate(self, recovery_handle: object) -> ProjectWorkspace:
        self.calls.append("recovery-read-candidate")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_read_candidate")
        if "recovery_candidate_mismatch" in self.faults:
            return record.candidate.last_known_good_workspace
        return record.candidate.candidate_workspace

    def complete_pending_commit(self, recovery_handle: object) -> ProjectWorkspace:
        self.calls.append("recovery-complete")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_complete")
        self.installed_workspace = record.candidate.candidate_workspace
        self.last_known_good_workspace = record.candidate.candidate_workspace
        if "recovery_keep_pending" not in self.faults:
            self.pending = None
        return self.installed_workspace

    def rollback_pending(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        self.calls.append("recovery-rollback")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_rollback")
        self.installed_workspace = record.candidate.last_known_good_workspace
        self.last_known_good_workspace = record.candidate.last_known_good_workspace
        if "recovery_keep_pending" not in self.faults:
            self.pending = None
        return self.installed_workspace

    def abandon_staged_copy(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        self.calls.append("recovery-abandon")
        record = self._require_recovery(recovery_handle)
        self._fault("recovery_abandon")
        if record.phase not in {"staging", "staged", "armed"}:
            raise OSError("/private/carrier/cannot-abandon-published")
        self.installed_workspace = record.candidate.last_known_good_workspace
        self.last_known_good_workspace = record.candidate.last_known_good_workspace
        if "recovery_keep_pending" not in self.faults:
            self.pending = None
        return self.installed_workspace

    def prime_recovery(
        self,
        candidate_workspace: ProjectWorkspace,
        *,
        phase: str,
        requested_document_ids: tuple[str, ...] = (_DOCUMENT_A, _DOCUMENT_B),
    ) -> str:
        operation_id = "save-" + "c" * 64
        candidate = _Candidate(
            operation_id,
            candidate_workspace,
            self.last_known_good_workspace,
            requested_document_ids,
        )
        self.staged.append(candidate)
        self.pending = _RecoveryRecord(candidate, phase)
        if phase not in {"staging", "staged", "armed"}:
            self.installed_workspace = candidate_workspace
        return operation_id

    @staticmethod
    def _require_candidate(value: object) -> _Candidate:
        if type(value) is not _Candidate:
            raise AssertionError("coordinator forged candidate handle")
        return value

    @staticmethod
    def _require_recovery(value: object) -> _RecoveryRecord:
        if type(value) is not _RecoveryRecord:
            raise AssertionError("coordinator forged recovery handle")
        return value


def _workspace_content_digest(workspace: ProjectWorkspace) -> str:
    workspace_module = importlib.import_module("project_workspace")
    if not hasattr(workspace_module, "workspace_content_digest_v1"):
        raise AssertionError(
            "Cluster 2B RED: project_workspace must export the canonical "
            "workspace_content_digest_v1 helper"
        )
    return workspace_module.workspace_content_digest_v1(workspace)


def _baseline_contract(module: ModuleType, workspace: ProjectWorkspace) -> object:
    baseline = module.WorkspaceSaveBaseline.from_workspace(
        workspace,
        workspace_revision=40,
        saved_package_digest=None,
    )
    self_digest = _workspace_content_digest(workspace)
    if baseline.workspace_content_digest != self_digest:
        raise AssertionError("baseline did not use the canonical workspace digest")
    return baseline


def _service(
    module: ModuleType,
    current: ProjectWorkspace,
    baseline: ProjectWorkspace | None,
) -> object:
    workspace_service = ProjectWorkspaceService(
        current,
        _binding(current),
        session_id="save-session",
        revision=41,
    )
    save_service = module.ProjectSaveService(
        workspace_service,
        baseline=(
            None
            if baseline is None
            else _baseline_contract(module, baseline)
        ),
    )
    if save_service.workspace_service is not workspace_service:
        raise AssertionError("save service must retain the C2A authority owner")
    return save_service


def _apply_public_display_name_reconciliation(
    workspace_service: ProjectWorkspaceService,
) -> None:
    """Advance the C2A authority without touching any production private field."""

    current = workspace_service.workspace
    changed_first = replace(
        current.documents[0],
        display_name="publicly reconciled display name",
    )
    incoming_workspace = replace(
        current,
        documents=(changed_first, *current.documents[1:]),
    )
    binding = workspace_service.origin_binding
    staged = StagedSelectedProjectDocuments(
        workspace=incoming_workspace,
        origin_binding=binding,
        source_identities=tuple(
            document.source_identity for document in binding.documents
        ),
        source_write_back_authorized=False,
        durable=False,
    )
    revision = workspace_service.revision
    preview = workspace_service.stage_reconciliation(
        staged,
        associations=(),
        session_id=workspace_service.session_id,
        base_revision=revision,
    )
    if preview.required_decision_identities:
        raise AssertionError("metadata-only reconciliation unexpectedly needs decisions")
    workspace_service.apply_reconciliation(
        preview.operation_id,
        decisions=(),
        session_id=workspace_service.session_id,
        base_revision=revision,
        incoming_source_identities=staged.source_identities,
    )


class Cluster2BSaveBaselineTests(unittest.TestCase):
    def test_minimal_public_surface_and_reader_only_writer_boundary_are_closed(
        self,
    ) -> None:
        module = _cluster2b()

        self.assertEqual(
            tuple(status.value for status in module.DocumentSaveStatus),
            ("saved", "rolled_back", "unchanged", "failed"),
        )
        self.assertEqual(
            tuple(action.value for action in module.RecoveryAction),
            ("complete_commit", "rollback", "abandon_staged_copy"),
        )
        self.assertEqual(
            tuple(phase.value for phase in module.RecoveryPhase),
            (
                "staging",
                "staged",
                "armed",
                "publishing",
                "published",
                "commit-uncertain",
            ),
        )
        self.assertTrue(
            hasattr(
                module.ProjectWorkspacePersistencePort,
                "describe_pending_recovery",
            )
        )
        facts = module.PendingRecoveryFacts(
            operation_id="save-" + "4" * 64,
            project_id=_PROJECT_ID,
            phase=module.RecoveryPhase.STAGED,
            candidate_digest=hashlib.sha256(b"candidate-facts").hexdigest(),
            last_known_good_digest=None,
        )
        self.assertNotIn("Source for", repr(facts))
        self.assertTrue(hasattr(module.ProjectDocumentWriterPort, "prepare"))
        writer_hints = get_type_hints(module.ProjectDocumentWriterPort)
        self.assertIs(writer_hints.get("codec_identity"), CodecIdentity)
        self.assertIs(writer_hints.get("format_id"), str)
        self.assertEqual(
            get_args(writer_hints.get("write_mode")),
            ("canonical", "source_round_trip"),
        )
        forbidden_exports = (
            "DirectoryProjectWriter",
            "WorkbookProjectWriter",
            "TxtProjectDocumentWriter",
            "PoProjectDocumentWriter",
            "PotProjectDocumentWriter",
        )
        self.assertEqual(
            tuple(name for name in forbidden_exports if hasattr(module, name)),
            (),
        )

    def test_dirty_is_derived_from_exact_saved_overlay_baseline(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        port = _InMemoryPersistencePort(baseline)
        service = _service(module, current, baseline)

        self.assertEqual(service.workspace_service.workspace, current)
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
        self.assertTrue(service.project_dirty)

    def test_dirty_domains_are_exact_and_saved_digest_is_only_a_receipt(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        first = baseline.documents[0]
        overlay = first.editing_overlay[0]
        changed_source_fingerprint = hashlib.sha256(
            b"changed-source-owned-facts"
        ).hexdigest()
        private_digest = hashlib.sha256(b"opaque-private-state").hexdigest()
        document_mutations = (
            (
                "target",
                replace(
                    first,
                    editing_overlay=(replace(overlay, target="changed target"),),
                ),
            ),
            (
                "confirmed",
                replace(
                    first,
                    editing_overlay=(replace(overlay, confirmed=False),),
                ),
            ),
            (
                "source",
                replace(
                    first,
                    source_snapshot_digest=changed_source_fingerprint,
                    source_segments=(
                        replace(
                            first.source_segments[0],
                            source="Changed source",
                            source_fingerprint=changed_source_fingerprint,
                        ),
                    ),
                    editing_overlay=(
                        replace(
                            overlay,
                            source_fingerprint=changed_source_fingerprint,
                        ),
                    ),
                ),
            ),
            (
                "codec_private_member",
                replace(
                    first,
                    codec_private_member=CodecPrivateMemberRef(
                        member_path="codec-private/document-a.bin",
                        sha256=private_digest,
                        byte_count=20,
                        codec_identity=first.codec_identity,
                        profile_version="opaque-v1",
                    ),
                ),
            ),
            ("display_name", replace(first, display_name="Chapter A renamed")),
        )
        for label, changed_document in document_mutations:
            with self.subTest(document_field=label):
                current = replace(
                    baseline,
                    documents=(changed_document, baseline.documents[1]),
                )
                service = _service(module, current, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
                self.assertFalse(service.manifest_dirty)

        reordered = replace(
            baseline,
            documents=(
                replace(baseline.documents[1], order=0),
                replace(baseline.documents[0], order=1),
            ),
        )
        manifest_mutations = (
            ("order", reordered),
            ("name", replace(baseline, name="Renamed project")),
            ("source_locale", replace(baseline, source_locale="en-GB")),
            ("target_locale", replace(baseline, target_locale="zh-Hant")),
            (
                "origin",
                replace(
                    baseline,
                    origin=replace(
                        baseline.origin,
                        portable_root_ref="renamed-selected-sources",
                    ),
                ),
            ),
        )
        for label, current in manifest_mutations:
            with self.subTest(manifest_field=label):
                service = _service(module, current, baseline)
                self.assertEqual(service.dirty_document_ids, ())
                self.assertTrue(service.manifest_dirty)

        receipt_only_overlay = replace(
            overlay,
            saved_state_digest=hashlib.sha256(b"receipt-only").hexdigest(),
        )
        receipt_only = replace(
            baseline,
            documents=(
                replace(first, editing_overlay=(receipt_only_overlay,)),
                baseline.documents[1],
            ),
        )
        service = _service(module, receipt_only, baseline)
        self.assertEqual(service.dirty_document_ids, ())
        self.assertFalse(service.manifest_dirty)
        self.assertFalse(service.project_dirty)

    def test_first_full_save_establishes_baseline_but_partial_save_requires_one(
        self,
    ) -> None:
        module = _cluster2b()
        current = _baseline_workspace()
        first_port = _InMemoryPersistencePort(None)
        first_service = _service(module, current, None)

        first_report = first_service.save_workspace(first_port)

        self.assertIsNone(first_port.staged[0].last_known_good_workspace)
        self.assertEqual(first_report.requested_count, 2)
        self.assertEqual(first_report.saved_count, 2)
        self.assertEqual(
            tuple(result.before_digest for result in first_report.document_results),
            (None, None),
        )
        self.assertEqual(first_service.saved_workspace_snapshot, current)
        self.assertEqual(first_service.dirty_document_ids, ())

        missing_baseline_port = _InMemoryPersistencePort(current)
        missing_baseline_service = _service(module, current, None)
        with self.assertRaises(ProjectWorkspaceError) as caught:
            missing_baseline_service.save_document(_DOCUMENT_A, missing_baseline_port)
        self.assertEqual(
            caught.exception.code,
            "PROJECT.SAVE.VALIDATION_FAILED",
        )
        self.assertEqual(missing_baseline_port.calls, [])

    def test_partial_save_rejects_document_membership_drift_before_staging(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        old_second = baseline.documents[1]
        replacement_id = "doc-" + "c" * 64
        replacement_overlay = replace(
            old_second.editing_overlay[0],
            document_id=replacement_id,
        )
        replacement_document = replace(
            old_second,
            document_id=replacement_id,
            source_ref="chapter-c.po",
            display_name="chapter-c.po",
            editing_overlay=(replacement_overlay,),
        )
        current = replace(
            baseline,
            documents=(baseline.documents[0], replacement_document),
        )
        port = _InMemoryPersistencePort(baseline)
        service = _service(module, current, baseline)

        with self.assertRaises(ProjectWorkspaceError) as caught:
            service.save_document(_DOCUMENT_A, port)

        self.assertEqual(
            caught.exception.code,
            "PROJECT.SAVE.VALIDATION_FAILED",
        )
        self.assertEqual(port.calls, [])
        self.assertEqual(service.saved_workspace_snapshot, baseline)

    def test_project_save_publishes_one_complete_candidate_and_reports_unchanged(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        port = _InMemoryPersistencePort(baseline)
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        self.assertIs(type(report), module.ProjectSaveReport)
        self.assertFalse(hasattr(report, "success"))
        self.assertEqual(port.calls, [
            "recovery-inspect",
            "stage",
            "validation",
            "arm",
            "publication",
            "readback",
            "commit",
            "readback",
            "recovery-inspect",
        ])
        self.assertEqual(len(port.staged), 1)
        self.assertEqual(
            _targets(port.staged[0].candidate_workspace),
            ("draft-a", "old-b"),
        )
        self.assertEqual(
            tuple(result.status for result in report.document_results),
            (
                module.DocumentSaveStatus.SAVED,
                module.DocumentSaveStatus.UNCHANGED,
            ),
        )
        self.assertEqual(
            (
                report.requested_count,
                report.saved_count,
                report.rolled_back_count,
                report.unchanged_count,
                report.failed_count,
            ),
            (2, 1, 0, 1, 0),
        )
        self.assertIs(report.journal_state, module.SaveJournalState.COMMITTED)
        self.assertIsNone(report.safe_code)
        self.assertEqual(service.dirty_document_ids, ())
        self.assertFalse(service.project_dirty)
        saved_snapshot = service.saved_workspace_snapshot
        current_workspace = service.workspace_service.workspace
        self.assertEqual(
            _workspace_content_digest(saved_snapshot),
            _workspace_content_digest(current_workspace),
        )
        self.assertEqual(
            _targets(saved_snapshot),
            _targets(current_workspace),
        )
        changed_overlay = saved_snapshot.documents[0].editing_overlay[0]
        self.assertEqual(
            changed_overlay.saved_state_digest,
            _saved_digest(
                changed_overlay.document_id,
                changed_overlay.local_segment_id,
                changed_overlay.source_fingerprint,
                changed_overlay.target,
                changed_overlay.confirmed,
            ),
        )
        self.assertNotEqual(
            changed_overlay.saved_state_digest,
            current.documents[0].editing_overlay[0].saved_state_digest,
        )
        self.assertEqual(port.last_known_good_workspace, saved_snapshot)

    def test_single_document_save_uses_current_selected_and_saved_unselected(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        current = _edit_document(
            current,
            _DOCUMENT_B,
            target="draft-b",
            confirmed=True,
        )
        port = _InMemoryPersistencePort(baseline)
        service = _service(module, current, baseline)

        report = service.save_document(_DOCUMENT_A, port)

        candidate = port.staged[0].candidate_workspace
        self.assertEqual(_targets(candidate), ("draft-a", "old-b"))
        self.assertEqual(report.requested_count, 1)
        self.assertEqual(
            tuple(result.status for result in report.document_results),
            (
                module.DocumentSaveStatus.SAVED,
                module.DocumentSaveStatus.UNCHANGED,
            ),
        )
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_B,))
        self.assertTrue(service.project_dirty)
        self.assertEqual(
            _targets(service.workspace_service.workspace),
            ("draft-a", "draft-b"),
        )
        self.assertEqual(
            _targets(service.saved_workspace_snapshot),
            ("draft-a", "old-b"),
        )
        self.assertEqual(service.workspace_service.workspace, current)
        self.assertNotEqual(
            service.saved_workspace_snapshot.documents[
                0
            ].editing_overlay[0].saved_state_digest,
            current.documents[0].editing_overlay[0].saved_state_digest,
        )
        self.assertEqual(
            service.workspace_service.workspace.documents[
                1
            ].editing_overlay[0].saved_state_digest,
            current.documents[1].editing_overlay[0].saved_state_digest,
        )

    def test_single_save_keeps_baseline_manifest_order_and_only_cleans_selected(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        changed_a = replace(
            _edit_document(
                baseline,
                _DOCUMENT_A,
                target="draft-a",
                confirmed=False,
            ).documents[0],
            display_name="Current Chapter A",
            order=1,
        )
        changed_b = replace(
            _edit_document(
                baseline,
                _DOCUMENT_B,
                target="draft-b",
                confirmed=True,
            ).documents[1],
            order=0,
        )
        current = replace(
            baseline,
            name="Current project name",
            source_locale="en-GB",
            target_locale="zh-Hant",
            origin=replace(
                baseline.origin,
                portable_root_ref="current-selected-sources",
            ),
            documents=(changed_b, changed_a),
        )
        port = _InMemoryPersistencePort(baseline)
        service = _service(module, current, baseline)

        report = service.save_document(_DOCUMENT_A, port)

        candidate = port.staged[0].candidate_workspace
        self.assertEqual(candidate.name, baseline.name)
        self.assertEqual(candidate.source_locale, baseline.source_locale)
        self.assertEqual(candidate.target_locale, baseline.target_locale)
        self.assertEqual(candidate.origin, baseline.origin)
        self.assertEqual(
            tuple((item.document_id, item.order) for item in candidate.documents),
            ((_DOCUMENT_A, 0), (_DOCUMENT_B, 1)),
        )
        self.assertEqual(candidate.documents[0].display_name, "Current Chapter A")
        self.assertEqual(_targets(candidate), ("draft-a", "old-b"))
        self.assertEqual(report.requested_count, 1)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_B,))
        self.assertTrue(service.manifest_dirty)
        self.assertTrue(service.project_dirty)
        self.assertEqual(service.workspace_service.workspace, current)
        self.assertEqual(service.saved_workspace_snapshot, candidate)


class Cluster2BDtoClosureTests(unittest.TestCase):
    @staticmethod
    def _digest(label: str) -> str:
        return hashlib.sha256(label.encode("ascii")).hexdigest()

    def _result(
        self,
        module: ModuleType,
        document_id: str,
        status: object,
        *,
        safe_code: str | None = None,
    ) -> object:
        return module.DocumentSaveResult(
            document_id=document_id,
            status=status,
            before_digest=self._digest("before-" + document_id),
            after_digest=self._digest("after-" + document_id),
            safe_code=safe_code,
        )

    def _report(
        self,
        module: ModuleType,
        *,
        results: tuple[object, ...],
        scope: object | None = None,
        requested_count: int | None = None,
        journal_state: object | None = None,
        recovery_required: bool = False,
        retryable: bool = False,
        safe_code: str | None = None,
    ) -> object:
        status = module.DocumentSaveStatus
        return module.ProjectSaveReport(
            operation_id="save-" + "1" * 64,
            scope=module.SaveScope.WORKSPACE if scope is None else scope,
            origin_kind=ProjectOriginKind.DIRECTORY,
            workspace_revision=42,
            workspace_content_digest=self._digest("workspace"),
            requested_count=(
                len(results) if requested_count is None else requested_count
            ),
            saved_count=sum(item.status is status.SAVED for item in results),
            rolled_back_count=sum(
                item.status is status.ROLLED_BACK for item in results
            ),
            unchanged_count=sum(
                item.status is status.UNCHANGED for item in results
            ),
            failed_count=sum(item.status is status.FAILED for item in results),
            document_results=results,
            journal_state=(
                module.SaveJournalState.COMMITTED
                if journal_state is None
                else journal_state
            ),
            recovery_required=recovery_required,
            retryable=retryable,
            safe_code=safe_code,
        )

    def test_document_results_and_report_membership_fail_closed(self) -> None:
        module = _cluster2b()
        status = module.DocumentSaveStatus
        digest = self._digest("document")
        invalid_results = (
            (status.FAILED, None),
            (status.ROLLED_BACK, None),
            (status.UNCHANGED, "PROJECT.SAVE.RECOVERY_REQUIRED"),
        )
        for invalid_status, safe_code in invalid_results:
            with self.subTest(status=invalid_status, safe_code=safe_code):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    module.DocumentSaveResult(
                        document_id=_DOCUMENT_A,
                        status=invalid_status,
                        before_digest=digest,
                        after_digest=digest,
                        safe_code=safe_code,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )

        saved_a = self._result(module, _DOCUMENT_A, status.SAVED)
        saved_b = self._result(module, _DOCUMENT_B, status.SAVED)
        duplicate_b = self._result(module, _DOCUMENT_A, status.SAVED)
        invalid_reports = (
            {
                "results": (saved_a, duplicate_b),
            },
            {
                "results": (saved_a,),
                "scope": module.SaveScope.DOCUMENT,
                "requested_count": 2,
            },
            {
                "results": (saved_a, saved_b),
                "scope": module.SaveScope.WORKSPACE,
                "requested_count": 1,
            },
        )
        for index, kwargs in enumerate(invalid_reports):
            with self.subTest(report_case=index):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    self._report(module, **kwargs)
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )

    def test_project_save_report_state_flags_and_codes_are_closed(self) -> None:
        module = _cluster2b()
        status = module.DocumentSaveStatus
        saved = self._result(module, _DOCUMENT_A, status.SAVED)
        failed = self._result(
            module,
            _DOCUMENT_A,
            status.FAILED,
            safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
        )
        rolled_back = self._result(
            module,
            _DOCUMENT_A,
            status.ROLLED_BACK,
            safe_code="PROJECT.SAVE.COMMIT_FAILED",
        )
        invalid_reports = (
            {"results": (failed,)},
            {"results": (rolled_back,)},
            {
                "results": (saved,),
                "recovery_required": True,
                "retryable": True,
                "safe_code": "PROJECT.SAVE.RECOVERY_REQUIRED",
            },
            {
                "results": (saved,),
                "safe_code": "PROJECT.SAVE.COMMIT_FAILED",
            },
            {
                "results": (saved,),
                "journal_state": module.SaveJournalState.RECOVERY_REQUIRED,
                "recovery_required": False,
                "retryable": True,
                "safe_code": "PROJECT.SAVE.RECOVERY_REQUIRED",
            },
            {
                "results": (saved,),
                "journal_state": module.SaveJournalState.RECOVERY_REQUIRED,
                "recovery_required": True,
                "retryable": True,
                "safe_code": None,
            },
            {
                "results": (saved,),
                "journal_state": module.SaveJournalState.RECOVERY_REQUIRED,
                "recovery_required": True,
                "retryable": False,
                "safe_code": "PROJECT.SAVE.RECOVERY_REQUIRED",
            },
        )
        for index, kwargs in enumerate(invalid_reports):
            with self.subTest(report_case=index):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    self._report(module, **kwargs)
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )

    def test_recovery_preview_actions_and_safe_codes_are_closed(self) -> None:
        module = _cluster2b()
        action = module.RecoveryAction
        invalid = (
            ((action.COMPLETE_COMMIT, action.COMPLETE_COMMIT), ()),
            ((action.ROLLBACK,), ()),
            ((action.ROLLBACK, action.COMPLETE_COMMIT), ()),
            (
                (action.COMPLETE_COMMIT, action.ROLLBACK),
                ("PROJECT.SAVE.RECOVERY_REQUIRED",),
            ),
            ((), ()),
        )
        for actions, safe_codes in invalid:
            with self.subTest(actions=actions, safe_codes=safe_codes):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    module.RecoveryPreview(
                        operation_id="save-" + "2" * 64,
                        project_id=_PROJECT_ID,
                        last_known_good_digest=self._digest("lkg"),
                        candidate_digest=self._digest("candidate"),
                        available_actions=actions,
                        safe_codes=safe_codes,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )

    def test_recovery_report_action_journal_flags_and_codes_are_closed(self) -> None:
        module = _cluster2b()
        action = module.RecoveryAction
        state = module.SaveJournalState
        invalid = (
            (action.COMPLETE_COMMIT, state.ROLLED_BACK, False, False, None),
            (action.ROLLBACK, state.COMMITTED, False, False, None),
            (action.ABANDON_STAGED_COPY, state.ROLLED_BACK, False, False, None),
            (
                action.COMPLETE_COMMIT,
                state.RECOVERY_REQUIRED,
                False,
                True,
                "PROJECT.SAVE.RECOVERY_REQUIRED",
            ),
            (
                action.ROLLBACK,
                state.RECOVERY_REQUIRED,
                True,
                True,
                None,
            ),
            (
                action.ABANDON_STAGED_COPY,
                state.RECOVERY_REQUIRED,
                True,
                False,
                "PROJECT.SAVE.RECOVERY_REQUIRED",
            ),
        )
        for values in invalid:
            (
                recovery_action,
                journal_state,
                recovery_required,
                retryable,
                safe_code,
            ) = values
            with self.subTest(action=recovery_action, state=journal_state):
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    module.ProjectRecoveryReport(
                        operation_id="save-" + "3" * 64,
                        action=recovery_action,
                        journal_state=journal_state,
                        workspace_content_digest=self._digest("recovered"),
                        recovery_required=recovery_required,
                        retryable=retryable,
                        safe_code=safe_code,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )


class Cluster2BSaveFaultTests(unittest.TestCase):
    def test_existing_recovery_or_inspect_fault_blocks_new_save_before_stage(
        self,
    ) -> None:
        module = _cluster2b()
        scenarios = (
            ("staged", ()),
            ("published", ()),
            ("staged", ("recovery_inspect",)),
        )
        for phase, faults in scenarios:
            with self.subTest(phase=phase, faults=faults):
                baseline = _baseline_workspace()
                old_candidate = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="older-recovery-candidate",
                    confirmed=False,
                )
                current = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="new-save-must-not-stage",
                    confirmed=False,
                )
                port = _InMemoryPersistencePort(baseline, faults=faults)
                old_operation_id = port.prime_recovery(
                    old_candidate,
                    phase=phase,
                )
                old_pending = port.pending
                old_staged = tuple(port.staged)
                old_installed = port.installed_workspace
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertEqual(port.calls, ["recovery-inspect"])
                self.assertNotEqual(report.operation_id, old_operation_id)
                self.assertTrue(report.recovery_required)
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.RECOVERY_REQUIRED,
                )
                self.assertEqual(
                    report.safe_code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIs(port.pending, old_pending)
                self.assertEqual(tuple(port.staged), old_staged)
                self.assertEqual(port.installed_workspace, old_installed)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
                for forbidden in (
                    "stage",
                    "validation",
                    "arm",
                    "publication",
                ):
                    self.assertNotIn(forbidden, port.calls)

    def test_prepublication_faults_never_publish_or_clear_dirty(self) -> None:
        module = _cluster2b()
        expected = {
            "stage": "PROJECT.SAVE.STAGE_FAILED",
            "validation": "PROJECT.SAVE.VALIDATION_FAILED",
            "arm": "PROJECT.SAVE.VALIDATION_FAILED",
        }
        for phase, safe_code in expected.items():
            with self.subTest(phase=phase):
                baseline = _baseline_workspace()
                current = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="draft-a",
                    confirmed=False,
                )
                port = _InMemoryPersistencePort(baseline, faults=(phase,))
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertEqual(report.safe_code, safe_code)
                self.assertNotIn("publication", port.calls)
                self.assertIsNone(port.pending)
                self.assertEqual(port.installed_workspace, baseline)
                self.assertEqual(port.last_known_good_workspace, baseline)
                self.assertEqual(service.workspace_service.workspace, current)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
                self.assertFalse(hasattr(report, "success"))
                self.assertNotIn("/private/", repr(report))
                self.assertNotIn("source-body", repr(report))

    def test_public_workspace_change_after_validation_is_stale_before_arm(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        service = _service(module, current, baseline)
        before_revision = service.workspace_service.revision
        before_digest = service.workspace_service.workspace_content_digest
        port = _InMemoryPersistencePort(
            baseline,
            after_validation=lambda: _apply_public_display_name_reconciliation(
                service.workspace_service
            ),
        )

        report = service.save_workspace(port)

        self.assertEqual(report.safe_code, "PROJECT.SAVE.SOURCE_STALE")
        self.assertEqual(
            port.calls,
            [
                "recovery-inspect",
                "stage",
                "validation",
                "recovery-inspect",
            ],
        )
        self.assertNotIn("arm", port.calls)
        self.assertNotIn("publication", port.calls)
        self.assertEqual(service.workspace_service.revision, before_revision + 1)
        self.assertNotEqual(
            service.workspace_service.workspace_content_digest,
            before_digest,
        )
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(port.installed_workspace, baseline)
        self.assertIsNone(port.pending)

    def test_workspace_change_during_candidate_build_is_stale_before_recovery_gate(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-build-race",
            confirmed=False,
        )
        service = _service(module, current, baseline)
        source_revision = service.workspace_service.revision
        source_digest = service.workspace_service.workspace_content_digest
        original_resign = module._resign_document
        expected_candidate = replace(
            current,
            documents=tuple(
                original_resign(document) for document in current.documents
            ),
        )
        expected_candidate_digest = _workspace_content_digest(expected_candidate)
        port = _InMemoryPersistencePort(baseline)
        advanced = False

        def racing_resign(document: ProjectDocument) -> ProjectDocument:
            nonlocal advanced
            if not advanced:
                advanced = True
                _apply_public_display_name_reconciliation(
                    service.workspace_service
                )
            return original_resign(document)

        with patch("project_save._resign_document", side_effect=racing_resign):
            report = service.save_workspace(port)

        self.assertTrue(advanced)
        self.assertEqual(report.safe_code, "PROJECT.SAVE.SOURCE_STALE")
        self.assertIs(report.journal_state, module.SaveJournalState.CLEAN)
        self.assertFalse(report.recovery_required)
        self.assertEqual(port.calls, [])
        self.assertEqual(report.workspace_revision, source_revision)
        self.assertEqual(
            report.workspace_content_digest,
            expected_candidate_digest,
        )
        self.assertEqual(source_digest, expected_candidate_digest)
        self.assertEqual(service.workspace_service.revision, source_revision + 1)
        self.assertNotEqual(
            service.workspace_service.workspace_content_digest,
            source_digest,
        )
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(port.staged, [])

    def test_publication_or_readback_fault_with_proved_rollback_is_rolled_back(
        self,
    ) -> None:
        module = _cluster2b()
        for phase in ("publication_after", "readback", "readback_mismatch"):
            with self.subTest(phase=phase):
                baseline = _baseline_workspace()
                current = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="draft-a",
                    confirmed=False,
                )
                port = _InMemoryPersistencePort(baseline, faults=(phase,))
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertIn("rollback", port.calls)
                self.assertEqual(
                    port.calls[-2:],
                    ["rollback", "recovery-inspect"],
                )
                self.assertEqual(port.installed_workspace, baseline)
                self.assertEqual(port.last_known_good_workspace, baseline)
                self.assertEqual(service.workspace_service.workspace, current)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
                self.assertEqual(report.rolled_back_count, 1)
                self.assertEqual(report.failed_count, 0)
                self.assertIs(
                    report.document_results[0].status,
                    module.DocumentSaveStatus.ROLLED_BACK,
                )
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.ROLLED_BACK,
                )
                self.assertFalse(report.recovery_required)
                self.assertEqual(report.safe_code, "PROJECT.SAVE.COMMIT_FAILED")

    def test_exact_rollback_with_uncleared_pending_is_recovery_required(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )

        class RollbackResiduePort(_InMemoryPersistencePort):
            def rollback_candidate(
                self, candidate_handle: object
            ) -> ProjectWorkspace | None:
                self.calls.append("rollback")
                candidate = self._require_candidate(candidate_handle)
                self.installed_workspace = candidate.last_known_good_workspace
                self.last_known_good_workspace = candidate.last_known_good_workspace
                # Return exact LKG but deliberately retain the pending record.
                return self.installed_workspace

        port = RollbackResiduePort(baseline, faults=("readback",))
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        self.assertEqual(
            port.calls[-2:],
            ["rollback", "recovery-inspect"],
        )
        self.assertEqual(port.installed_workspace, baseline)
        self.assertIsNotNone(port.pending)
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
        self.assertEqual(report.rolled_back_count, 1)
        self.assertEqual(report.failed_count, 0)
        self.assertIs(
            report.document_results[0].status,
            module.DocumentSaveStatus.ROLLED_BACK,
        )
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertTrue(report.recovery_required)
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")

    def test_exact_rollback_with_cleanup_inspect_fault_is_recovery_required(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )

        class RollbackTailInspectFaultPort(_InMemoryPersistencePort):
            def __init__(self, last_known_good_workspace: ProjectWorkspace) -> None:
                super().__init__(
                    last_known_good_workspace,
                    faults=("readback",),
                )
                self.inspect_count = 0

            def inspect_pending_recovery(self) -> object | None:
                self.calls.append("recovery-inspect")
                self.inspect_count += 1
                if self.inspect_count == 2:
                    raise OSError(
                        "/private/carrier/rollback-tail-inspect/source-body"
                    )
                return self.pending

        port = RollbackTailInspectFaultPort(baseline)
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        self.assertEqual(
            port.calls[-2:],
            ["rollback", "recovery-inspect"],
        )
        self.assertEqual(port.installed_workspace, baseline)
        self.assertIsNone(port.pending)
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
        self.assertEqual(report.rolled_back_count, 1)
        self.assertEqual(report.failed_count, 0)
        self.assertIs(
            report.document_results[0].status,
            module.DocumentSaveStatus.ROLLED_BACK,
        )
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertTrue(report.recovery_required)
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")
        self.assertNotIn("/private/", repr(report))
        self.assertNotIn("source-body", repr(report))

    def test_post_readback_finalize_fault_adopts_baseline_and_requires_recovery(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="durable-a",
            confirmed=False,
        )
        current = _edit_document(
            current,
            _DOCUMENT_B,
            target="durable-b",
            confirmed=True,
        )
        port = _InMemoryPersistencePort(baseline, faults=("commit",))
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        candidate = port.staged[0].candidate_workspace
        self.assertEqual(
            port.calls,
            [
                "recovery-inspect",
                "stage",
                "validation",
                "arm",
                "publication",
                "readback",
                "commit",
                "readback",
            ],
        )
        self.assertNotIn("rollback", port.calls)
        self.assertEqual(port.installed_workspace, candidate)
        self.assertIsNotNone(port.pending)
        self.assertEqual(port.pending.phase, "commit-uncertain")
        self.assertEqual(service.saved_workspace_snapshot, candidate)
        self.assertEqual(service.dirty_document_ids, ())
        self.assertEqual(
            tuple(result.status for result in report.document_results),
            (module.DocumentSaveStatus.SAVED, module.DocumentSaveStatus.SAVED),
        )
        self.assertEqual(report.saved_count, 2)
        self.assertEqual(report.failed_count, 0)
        self.assertTrue(report.recovery_required)
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")

    def test_hostile_finalize_cannot_hide_candidate_loss_after_returning_none(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )

        class HostileFinalizePort(_InMemoryPersistencePort):
            def commit_candidate(self, candidate_handle: object) -> None:
                self.calls.append("commit")
                candidate = self._require_candidate(candidate_handle)
                if self.pending is None:
                    raise AssertionError("publication was not armed")
                self.pending.phase = "commit-uncertain"
                self.installed_workspace = candidate.last_known_good_workspace
                self.last_known_good_workspace = candidate.last_known_good_workspace

        port = HostileFinalizePort(baseline)
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        self.assertEqual(port.calls[:8], [
            "recovery-inspect",
            "stage",
            "validation",
            "arm",
            "publication",
            "readback",
            "commit",
            "readback",
        ])
        self.assertIsNot(
            report.journal_state,
            module.SaveJournalState.COMMITTED,
        )
        self.assertEqual(report.saved_count, 0)
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))

    def test_exact_finalize_with_uncleared_journal_adopts_baseline_but_recovers(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="durable-with-journal-residue",
            confirmed=False,
        )

        class JournalResiduePort(_InMemoryPersistencePort):
            def commit_candidate(self, candidate_handle: object) -> None:
                self.calls.append("commit")
                candidate = self._require_candidate(candidate_handle)
                if self.pending is None:
                    raise AssertionError("publication was not armed")
                self.pending.phase = "commit-uncertain"
                self.installed_workspace = candidate.candidate_workspace
                self.last_known_good_workspace = candidate.candidate_workspace
                # Deliberately return success without clearing the journal.

        port = JournalResiduePort(baseline)
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        candidate = port.staged[0].candidate_workspace
        self.assertEqual(port.calls, [
            "recovery-inspect",
            "stage",
            "validation",
            "arm",
            "publication",
            "readback",
            "commit",
            "readback",
            "recovery-inspect",
        ])
        self.assertEqual(service.saved_workspace_snapshot, candidate)
        self.assertEqual(service.dirty_document_ids, ())
        self.assertEqual(report.saved_count, 1)
        self.assertTrue(report.recovery_required)
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")
        self.assertIsNotNone(port.pending)

    def test_post_commit_residue_inspection_fault_keeps_adopted_baseline_visible(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="durable-before-inspect-fault",
            confirmed=False,
        )
        class TailInspectFaultPort(_InMemoryPersistencePort):
            def __init__(self, last_known_good_workspace: ProjectWorkspace) -> None:
                super().__init__(last_known_good_workspace)
                self.inspect_count = 0

            def inspect_pending_recovery(self) -> object | None:
                self.calls.append("recovery-inspect")
                self.inspect_count += 1
                if self.inspect_count == 2:
                    raise OSError("/private/carrier/tail-inspect/source-body")
                return self.pending

        port = TailInspectFaultPort(baseline)
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        candidate = port.staged[0].candidate_workspace
        self.assertEqual(port.calls[-3:], [
            "commit",
            "readback",
            "recovery-inspect",
        ])
        self.assertEqual(service.saved_workspace_snapshot, candidate)
        self.assertEqual(service.dirty_document_ids, ())
        self.assertEqual(report.saved_count, 1)
        self.assertTrue(report.recovery_required)
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")

    def test_prepublication_fault_with_residue_is_recovery_required(self) -> None:
        module = _cluster2b()
        for phase in ("stage_residue", "validation_residue", "arm_residue"):
            with self.subTest(phase=phase):
                baseline = _baseline_workspace()
                current = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="draft-a",
                    confirmed=False,
                )
                port = _InMemoryPersistencePort(baseline, faults=(phase,))
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertTrue(report.recovery_required)
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.RECOVERY_REQUIRED,
                )
                self.assertEqual(
                    report.safe_code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIsNotNone(port.pending)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))
                self.assertNotIn("publication", port.calls)

    def test_unproved_rollback_keeps_candidate_and_returns_recovery_required(self) -> None:
        module = _cluster2b()
        for rollback_fault in ("rollback", "rollback_readback_mismatch"):
            with self.subTest(rollback_fault=rollback_fault):
                baseline = _baseline_workspace()
                current = _edit_document(
                    baseline,
                    _DOCUMENT_A,
                    target="draft-a",
                    confirmed=False,
                )
                port = _InMemoryPersistencePort(
                    baseline,
                    faults=("readback", rollback_fault),
                )
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertTrue(report.recovery_required)
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.RECOVERY_REQUIRED,
                )
                self.assertEqual(report.failed_count, 1)
                self.assertIs(
                    report.document_results[0].status,
                    module.DocumentSaveStatus.FAILED,
                )
                self.assertEqual(
                    report.safe_code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIsNotNone(port.pending)
                self.assertEqual(service.workspace_service.workspace, current)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A,))

    def test_known_failures_are_body_safe_and_programmer_faults_remain_visible(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        port = _InMemoryPersistencePort(baseline, faults=("stage",))
        report = _service(module, current, baseline).save_workspace(port)
        self.assertNotIn("/private/", repr(report))
        self.assertNotIn("source-body", repr(report))

        class ProgrammerFaultPort(_InMemoryPersistencePort):
            def stage_candidate(self, **kwargs: object) -> object:
                del kwargs
                raise AssertionError("programmer-fault")

        with self.assertRaisesRegex(AssertionError, "programmer-fault"):
            fault_port = ProgrammerFaultPort(baseline)
            _service(module, current, baseline).save_workspace(fault_port)


class Cluster2BColdRecoveryTests(unittest.TestCase):
    def test_cold_recovery_accepts_truly_opaque_string_handle(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="opaque-token-candidate",
            confirmed=False,
        )

        class OpaqueTokenPort(_InMemoryPersistencePort):
            token = "carrier-private-token-with-no-attributes"

            def inspect_pending_recovery(self) -> object | None:
                self.calls.append("recovery-inspect")
                return None if self.pending is None else self.token

            def _record_for_token(self, handle: object) -> _RecoveryRecord:
                if handle != self.token or self.pending is None:
                    raise OSError("/private/carrier/unknown-token")
                return self.pending

            def describe_pending_recovery(self, handle: object) -> object:
                self.calls.append("recovery-describe")
                record = self._record_for_token(handle)
                phase = next(
                    item
                    for item in module.RecoveryPhase
                    if item.value == record.phase
                )
                lkg = record.candidate.last_known_good_workspace
                return module.PendingRecoveryFacts(
                    operation_id=record.candidate.operation_id,
                    project_id=record.candidate.candidate_workspace.project_id,
                    phase=phase,
                    candidate_digest=_workspace_content_digest(
                        record.candidate.candidate_workspace
                    ),
                    last_known_good_digest=(
                        None if lkg is None else _workspace_content_digest(lkg)
                    ),
                )

            def read_recovery_last_known_good(
                self, handle: object
            ) -> ProjectWorkspace | None:
                return super().read_recovery_last_known_good(
                    self._record_for_token(handle)
                )

            def read_recovery_candidate(
                self, handle: object
            ) -> ProjectWorkspace:
                return super().read_recovery_candidate(
                    self._record_for_token(handle)
                )

            def complete_pending_commit(
                self, handle: object
            ) -> ProjectWorkspace:
                return super().complete_pending_commit(
                    self._record_for_token(handle)
                )

        port = OpaqueTokenPort(baseline)
        operation_id = port.prime_recovery(candidate, phase="publishing")

        preview = module.ProjectSaveService.inspect_cold_recovery(port)
        report = module.ProjectSaveService.cold_recover(
            port,
            operation_id=operation_id,
            choice=module.RecoveryAction.COMPLETE_COMMIT,
        )

        self.assertEqual(preview.operation_id, operation_id)
        self.assertEqual(
            preview.available_actions,
            (
                module.RecoveryAction.COMPLETE_COMMIT,
                module.RecoveryAction.ROLLBACK,
            ),
        )
        self.assertIs(report.journal_state, module.SaveJournalState.COMMITTED)
        self.assertFalse(report.recovery_required)
        self.assertEqual(port.installed_workspace, candidate)
        self.assertIsNone(port.pending)
        self.assertGreaterEqual(port.calls.count("recovery-describe"), 2)
        self.assertGreaterEqual(port.calls.count("recovery-read-candidate"), 2)

    def test_staging_opaque_handle_abandons_without_reading_partial_candidate(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="partial-candidate-must-not-be-read",
            confirmed=False,
        )

        class OpaqueStagingPort(_InMemoryPersistencePort):
            token = "carrier-private-staging-token"

            def inspect_pending_recovery(self) -> object | None:
                self.calls.append("recovery-inspect")
                return None if self.pending is None else self.token

            def _record_for_token(self, handle: object) -> _RecoveryRecord:
                if handle != self.token or self.pending is None:
                    raise OSError("/private/carrier/unknown-staging-token")
                return self.pending

            def describe_pending_recovery(self, handle: object) -> object:
                self.calls.append("recovery-describe")
                record = self._record_for_token(handle)
                lkg = record.candidate.last_known_good_workspace
                return module.PendingRecoveryFacts(
                    operation_id=record.candidate.operation_id,
                    project_id=record.candidate.candidate_workspace.project_id,
                    phase=module.RecoveryPhase.STAGING,
                    candidate_digest=_workspace_content_digest(
                        record.candidate.candidate_workspace
                    ),
                    last_known_good_digest=(
                        None if lkg is None else _workspace_content_digest(lkg)
                    ),
                )

            def read_recovery_last_known_good(
                self, handle: object
            ) -> ProjectWorkspace | None:
                return super().read_recovery_last_known_good(
                    self._record_for_token(handle)
                )

            def read_recovery_candidate(
                self, handle: object
            ) -> ProjectWorkspace:
                del handle
                raise AssertionError("STAGING candidate is partial and unreadable")

            def abandon_staged_copy(
                self, handle: object
            ) -> ProjectWorkspace | None:
                return super().abandon_staged_copy(
                    self._record_for_token(handle)
                )

        port = OpaqueStagingPort(baseline)
        operation_id = port.prime_recovery(candidate, phase="staging")

        preview = module.ProjectSaveService.inspect_cold_recovery(port)
        report = module.ProjectSaveService.cold_recover(
            port,
            operation_id=operation_id,
            choice=module.RecoveryAction.ABANDON_STAGED_COPY,
        )

        self.assertEqual(
            preview.available_actions,
            (module.RecoveryAction.ABANDON_STAGED_COPY,),
        )
        self.assertEqual(
            preview.candidate_digest,
            _workspace_content_digest(candidate),
        )
        self.assertIs(report.journal_state, module.SaveJournalState.CLEAN)
        self.assertEqual(port.installed_workspace, baseline)
        self.assertEqual(port.last_known_good_workspace, baseline)
        self.assertIsNone(port.pending)
        self.assertNotIn("recovery-read-candidate", port.calls)

    def test_first_save_residue_preserves_absent_lkg_and_can_complete(self) -> None:
        module = _cluster2b()
        current = _baseline_workspace()
        port = _InMemoryPersistencePort(None, faults=("commit",))
        service = _service(module, current, None)

        save_report = service.save_workspace(port)

        self.assertTrue(save_report.recovery_required)
        self.assertEqual(save_report.saved_count, 2)
        self.assertTrue(
            all(
                result.before_digest is None
                for result in save_report.document_results
            )
        )
        self.assertIsNone(port.pending.candidate.last_known_good_workspace)
        preview = module.ProjectSaveService.inspect_cold_recovery(port)
        self.assertIsNone(preview.last_known_good_digest)
        self.assertEqual(
            preview.available_actions,
            (
                module.RecoveryAction.COMPLETE_COMMIT,
                module.RecoveryAction.ROLLBACK,
            ),
        )

        recovery_report = module.ProjectSaveService.cold_recover(
            port,
            operation_id=preview.operation_id,
            choice=module.RecoveryAction.COMPLETE_COMMIT,
        )

        self.assertIs(
            recovery_report.journal_state,
            module.SaveJournalState.COMMITTED,
        )
        self.assertFalse(recovery_report.recovery_required)
        self.assertEqual(port.last_known_good_workspace, current)
        self.assertIsNone(port.pending)

    def test_recovery_actions_are_an_exact_phase_closed_set(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )
        expected = (
            (
                "staging",
                (module.RecoveryAction.ABANDON_STAGED_COPY,),
            ),
            (
                "staged",
                (module.RecoveryAction.ABANDON_STAGED_COPY,),
            ),
            (
                "armed",
                (module.RecoveryAction.ABANDON_STAGED_COPY,),
            ),
            (
                "publishing",
                (
                    module.RecoveryAction.COMPLETE_COMMIT,
                    module.RecoveryAction.ROLLBACK,
                ),
            ),
            (
                "published",
                (
                    module.RecoveryAction.COMPLETE_COMMIT,
                    module.RecoveryAction.ROLLBACK,
                ),
            ),
            (
                "commit-uncertain",
                (
                    module.RecoveryAction.COMPLETE_COMMIT,
                    module.RecoveryAction.ROLLBACK,
                ),
            ),
        )
        for phase, actions in expected:
            with self.subTest(phase=phase):
                port = _InMemoryPersistencePort(baseline)
                port.prime_recovery(candidate, phase=phase)
                preview = module.ProjectSaveService.inspect_cold_recovery(port)
                self.assertEqual(preview.available_actions, actions)

    def test_wrong_or_forged_recovery_action_fails_closed(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )
        forbidden = (
            ("staging", module.RecoveryAction.COMPLETE_COMMIT),
            ("staged", module.RecoveryAction.COMPLETE_COMMIT),
            ("armed", module.RecoveryAction.ROLLBACK),
            ("publishing", module.RecoveryAction.ABANDON_STAGED_COPY),
            ("published", module.RecoveryAction.ABANDON_STAGED_COPY),
            ("commit-uncertain", module.RecoveryAction.ABANDON_STAGED_COPY),
        )
        recovery_calls = {
            "recovery-complete",
            "recovery-rollback",
            "recovery-abandon",
        }
        for phase, action in forbidden:
            with self.subTest(phase=phase, action=action):
                port = _InMemoryPersistencePort(baseline)
                operation_id = port.prime_recovery(candidate, phase=phase)
                with self.assertRaises(ProjectWorkspaceError) as caught:
                    module.ProjectSaveService.cold_recover(
                        port,
                        operation_id=operation_id,
                        choice=action,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIsNotNone(port.pending)
                self.assertTrue(recovery_calls.isdisjoint(port.calls))

        forged_port = _InMemoryPersistencePort(baseline)
        operation_id = forged_port.prime_recovery(candidate, phase="published")
        with self.assertRaises(ProjectWorkspaceError) as forged:
            module.ProjectSaveService.cold_recover(
                forged_port,
                operation_id=operation_id,
                choice="rollback",
            )
        self.assertEqual(forged.exception.code, "PROJECT.SAVE.RECOVERY_REQUIRED")
        self.assertIsNotNone(forged_port.pending)
        self.assertTrue(recovery_calls.isdisjoint(forged_port.calls))

        wrong_id_port = _InMemoryPersistencePort(baseline)
        wrong_id_port.prime_recovery(candidate, phase="published")
        with self.assertRaises(ProjectWorkspaceError) as wrong_id:
            module.ProjectSaveService.cold_recover(
                wrong_id_port,
                operation_id="save-" + "e" * 64,
                choice=module.RecoveryAction.ROLLBACK,
            )
        self.assertEqual(wrong_id.exception.code, "PROJECT.SAVE.RECOVERY_REQUIRED")
        self.assertIsNotNone(wrong_id_port.pending)
        self.assertTrue(recovery_calls.isdisjoint(wrong_id_port.calls))

    def test_noop_candidate_still_has_explicit_recovery(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        port = _InMemoryPersistencePort(baseline)
        operation_id = port.prime_recovery(baseline, phase="published")

        preview = module.ProjectSaveService.inspect_cold_recovery(port)

        self.assertEqual(
            preview.last_known_good_digest,
            preview.candidate_digest,
        )
        self.assertEqual(
            preview.available_actions,
            (
                module.RecoveryAction.COMPLETE_COMMIT,
                module.RecoveryAction.ROLLBACK,
            ),
        )
        report = module.ProjectSaveService.cold_recover(
            port,
            operation_id=operation_id,
            choice=module.RecoveryAction.COMPLETE_COMMIT,
        )
        self.assertIs(report.journal_state, module.SaveJournalState.COMMITTED)
        self.assertFalse(report.recovery_required)
        self.assertIsNone(port.pending)

    def test_same_operation_candidate_replacement_after_preview_is_stale(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )
        replacement_candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="replacement-after-preview",
            confirmed=False,
        )
        port = _InMemoryPersistencePort(baseline)
        operation_id = port.prime_recovery(candidate, phase="published")
        preview = module.ProjectSaveService.inspect_cold_recovery(port)
        self.assertEqual(preview.operation_id, operation_id)
        port.pending = _RecoveryRecord(
            _Candidate(
                operation_id=operation_id,
                candidate_workspace=replacement_candidate,
                last_known_good_workspace=baseline,
                requested_document_ids=(_DOCUMENT_A, _DOCUMENT_B),
            ),
            "published",
        )
        port.installed_workspace = replacement_candidate

        report = module.ProjectSaveService.cold_recover(
            port,
            operation_id=operation_id,
            choice=module.RecoveryAction.COMPLETE_COMMIT,
        )

        self.assertTrue(report.recovery_required)
        self.assertIs(
            report.journal_state,
            module.SaveJournalState.RECOVERY_REQUIRED,
        )
        self.assertEqual(report.safe_code, "PROJECT.SAVE.RECOVERY_REQUIRED")
        self.assertIsNotNone(port.pending)
        self.assertNotIn("recovery-complete", port.calls)

    def test_cross_project_candidate_or_lkg_is_rejected(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        foreign = replace(baseline, project_id="prj-" + "8" * 64)
        scenarios = (
            (baseline, foreign),
            (foreign, baseline),
        )
        for lkg, candidate in scenarios:
            with self.subTest(
                lkg_project=lkg.project_id,
                candidate_project=candidate.project_id,
            ):
                port = _InMemoryPersistencePort(lkg)
                port.prime_recovery(candidate, phase="published")

                with self.assertRaises(ProjectWorkspaceError) as caught:
                    module.ProjectSaveService.inspect_cold_recovery(port)

                self.assertEqual(
                    caught.exception.code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIsNotNone(port.pending)
                self.assertNotIn("recovery-complete", port.calls)
                self.assertNotIn("recovery-rollback", port.calls)

    def test_cold_recovery_complete_rollback_and_abandon_are_distinct(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="durable-a",
            confirmed=False,
        )
        scenarios = (
            (
                "publishing",
                module.RecoveryAction.COMPLETE_COMMIT,
                candidate,
                module.SaveJournalState.COMMITTED,
                "recovery-complete",
            ),
            (
                "publishing",
                module.RecoveryAction.ROLLBACK,
                baseline,
                module.SaveJournalState.ROLLED_BACK,
                "recovery-rollback",
            ),
            (
                "published",
                module.RecoveryAction.COMPLETE_COMMIT,
                candidate,
                module.SaveJournalState.COMMITTED,
                "recovery-complete",
            ),
            (
                "published",
                module.RecoveryAction.ROLLBACK,
                baseline,
                module.SaveJournalState.ROLLED_BACK,
                "recovery-rollback",
            ),
            (
                "staging",
                module.RecoveryAction.ABANDON_STAGED_COPY,
                baseline,
                module.SaveJournalState.CLEAN,
                "recovery-abandon",
            ),
            (
                "staged",
                module.RecoveryAction.ABANDON_STAGED_COPY,
                baseline,
                module.SaveJournalState.CLEAN,
                "recovery-abandon",
            ),
        )
        for phase, action, expected, journal_state, expected_call in scenarios:
            with self.subTest(action=action):
                port = _InMemoryPersistencePort(baseline)
                operation_id = port.prime_recovery(candidate, phase=phase)

                preview = module.ProjectSaveService.inspect_cold_recovery(port)
                self.assertEqual(preview.operation_id, operation_id)
                self.assertNotIn("Source for", repr(preview))
                report = module.ProjectSaveService.cold_recover(
                    port,
                    operation_id=operation_id,
                    choice=action,
                )

                self.assertIs(type(report), module.ProjectRecoveryReport)
                self.assertFalse(hasattr(report, "success"))
                self.assertEqual(report.operation_id, operation_id)
                self.assertIs(report.action, action)
                self.assertIs(report.journal_state, journal_state)
                self.assertFalse(report.recovery_required)
                self.assertEqual(port.installed_workspace, expected)
                self.assertEqual(port.last_known_good_workspace, expected)
                self.assertIsNone(port.pending)
                self.assertIn("recovery-read-lkg", port.calls)
                if phase == "staging":
                    self.assertNotIn("recovery-read-candidate", port.calls)
                else:
                    self.assertIn("recovery-read-candidate", port.calls)
                self.assertIn(expected_call, port.calls)

    def test_cold_recovery_correct_workspace_with_uncleared_pending_is_uncertain(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )
        scenarios = (
            ("publishing", module.RecoveryAction.COMPLETE_COMMIT, candidate),
            ("published", module.RecoveryAction.COMPLETE_COMMIT, candidate),
            ("published", module.RecoveryAction.ROLLBACK, baseline),
            ("staging", module.RecoveryAction.ABANDON_STAGED_COPY, baseline),
            ("staged", module.RecoveryAction.ABANDON_STAGED_COPY, baseline),
        )
        for phase, action, expected_workspace in scenarios:
            with self.subTest(phase=phase, action=action):
                port = _InMemoryPersistencePort(
                    baseline,
                    faults=("recovery_keep_pending",),
                )
                operation_id = port.prime_recovery(candidate, phase=phase)
                preview = module.ProjectSaveService.inspect_cold_recovery(port)

                report = module.ProjectSaveService.cold_recover(
                    port,
                    operation_id=operation_id,
                    choice=action,
                )

                self.assertEqual(port.installed_workspace, expected_workspace)
                self.assertIsNotNone(port.pending)
                self.assertTrue(report.recovery_required)
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.RECOVERY_REQUIRED,
                )
                self.assertEqual(
                    report.safe_code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertEqual(
                    report.workspace_content_digest,
                    (
                        preview.candidate_digest
                        if action is module.RecoveryAction.COMPLETE_COMMIT
                        else preview.last_known_good_digest
                    ),
                )
                self.assertEqual(port.calls[-1], "recovery-inspect")

    def test_cold_recovery_mismatch_or_fault_never_guesses_from_mtime(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        candidate = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="candidate-a",
            confirmed=False,
        )
        for fault in ("recovery_candidate_mismatch", "recovery_complete"):
            with self.subTest(fault=fault):
                port = _InMemoryPersistencePort(baseline, faults=(fault,))
                operation_id = port.prime_recovery(candidate, phase="published")

                preview = module.ProjectSaveService.inspect_cold_recovery(port)
                self.assertEqual(preview.operation_id, operation_id)
                report = module.ProjectSaveService.cold_recover(
                    port,
                    operation_id=operation_id,
                    choice=module.RecoveryAction.COMPLETE_COMMIT,
                )

                self.assertTrue(report.recovery_required)
                self.assertIs(
                    report.journal_state,
                    module.SaveJournalState.RECOVERY_REQUIRED,
                )
                self.assertEqual(
                    report.safe_code,
                    "PROJECT.SAVE.RECOVERY_REQUIRED",
                )
                self.assertIsNotNone(port.pending)
                self.assertNotIn("/private/", repr(report))


class Cluster2BReaderOnlyAndAtomicityBoundaryTests(unittest.TestCase):
    def test_explicit_selected_attached_is_unsupported_and_detached_is_unbound(
        self,
    ) -> None:
        module = _cluster2b()
        first_two = _baseline_workspace()
        detached = _document(
            document_id=_DOCUMENT_C,
            source_ref="chapter-c.txt",
            order=2,
            format_id="line-text-v1",
            codec_identity=CodecIdentity("localcat", "line-text", "1"),
            source_fingerprint=_SOURCE_C,
            target="old-c",
            confirmed=False,
        )
        detached = replace(
            detached,
            source_segments=(
                replace(
                    detached.source_segments[0],
                    source_presence=SourcePresence.DETACHED,
                ),
            ),
        )
        workspace = replace(
            first_two,
            documents=(*first_two.documents, detached),
        )
        service = _service(module, workspace, workspace)

        self.assertEqual(
            tuple(
                (item.document_id, item.state)
                for item in service.origin_write_state
            ),
            (
                (_DOCUMENT_A, module.OriginWriteState.UNSUPPORTED),
                (_DOCUMENT_B, module.OriginWriteState.UNSUPPORTED),
                (_DOCUMENT_C, module.OriginWriteState.UNBOUND),
            ),
        )

    def test_reader_only_write_back_returns_structured_unsupported_result(
        self,
    ) -> None:
        module = _cluster2b()
        workspace = _baseline_workspace()
        service = _service(module, workspace, workspace)
        before_workspace = service.workspace_service.workspace
        before_baseline = service.saved_workspace_snapshot

        result = service.write_back_to_source(
            _DOCUMENT_A,
            writer_port=None,
        )

        self.assertIs(type(result), module.DocumentOriginWriteState)
        self.assertEqual(result.document_id, _DOCUMENT_A)
        self.assertIs(result.state, module.OriginWriteState.UNSUPPORTED)
        self.assertFalse(hasattr(result, "success"))
        self.assertNotIn("Source for", repr(result))
        self.assertEqual(service.workspace_service.workspace, before_workspace)
        self.assertEqual(service.saved_workspace_snapshot, before_baseline)

    def test_live_looking_writer_cannot_widen_unsupported_or_unbound_origin(
        self,
    ) -> None:
        module = _cluster2b()

        class LiveLookingWriter:
            write_mode = "canonical"

            def __init__(self, document: ProjectDocument) -> None:
                self.codec_identity = document.codec_identity
                self.format_id = document.format_id
                self.prepare_calls: list[tuple[object, ...]] = []

            def prepare(self, *args: object) -> object:
                self.prepare_calls.append(args)
                raise AssertionError("reader-only boundary called prepare")

        attached_workspace = _baseline_workspace()
        attached_document = attached_workspace.documents[0]
        attached_writer = LiveLookingWriter(attached_document)
        attached_service = _service(
            module,
            attached_workspace,
            attached_workspace,
        )

        attached_result = attached_service.source_write_back_status(
            attached_document.document_id,
            attached_writer,
        )

        self.assertIs(type(attached_result), module.DocumentSourceWriteResult)
        self.assertIs(
            attached_result.status,
            module.DocumentSourceWriteStatus.UNSUPPORTED,
        )
        self.assertEqual(
            attached_result.safe_code,
            "PROJECT.SAVE.WRITER_UNAVAILABLE",
        )
        self.assertEqual(attached_writer.prepare_calls, [])

        detached_document = _document(
            document_id=_DOCUMENT_C,
            source_ref="chapter-c.txt",
            order=2,
            format_id="line-text-v1",
            codec_identity=CodecIdentity("localcat", "line-text", "1"),
            source_fingerprint=_SOURCE_C,
            target="old-c",
            confirmed=False,
        )
        detached_document = replace(
            detached_document,
            source_segments=(
                replace(
                    detached_document.source_segments[0],
                    source_presence=SourcePresence.DETACHED,
                ),
            ),
        )
        detached_workspace = replace(
            attached_workspace,
            documents=(*attached_workspace.documents, detached_document),
        )
        detached_writer = LiveLookingWriter(detached_document)
        detached_service = _service(
            module,
            detached_workspace,
            detached_workspace,
        )

        detached_result = detached_service.source_write_back_status(
            detached_document.document_id,
            detached_writer,
        )

        self.assertIs(type(detached_result), module.DocumentSourceWriteResult)
        self.assertIs(
            detached_result.status,
            module.DocumentSourceWriteStatus.UNSUPPORTED,
        )
        self.assertEqual(
            detached_result.safe_code,
            "PROJECT.SAVE.WRITER_UNAVAILABLE",
        )
        self.assertEqual(detached_writer.prepare_calls, [])

    def test_nonlegacy_origins_short_circuit_and_legacy_writer_is_exact(self) -> None:
        module = _cluster2b()

        class AttrOnlyWriter:
            def __init__(self, document: ProjectDocument) -> None:
                self.codec_identity = document.codec_identity
                self.format_id = document.format_id
                self.write_mode = "canonical"

        class CallableWriter(AttrOnlyWriter):
            def __init__(self, document: ProjectDocument) -> None:
                super().__init__(document)
                self.prepare_calls: list[tuple[object, ...]] = []

            def prepare(self, *args: object) -> object:
                self.prepare_calls.append(args)
                return object()

        baseline = _baseline_workspace()
        workbook = replace(
            _edit_document(
                baseline,
                _DOCUMENT_A,
                target="dirty-workbook-overlay",
                confirmed=False,
            ),
            origin=ProjectOrigin(
                kind=ProjectOriginKind.WORKBOOK,
                profile_version="workbook-contract-model-v1",
                portable_root_ref="book.xlsx",
            ),
        )
        workbook_service = _service(module, workbook, baseline)
        workbook_document = workbook.documents[0]
        workbook_ports = (
            AttrOnlyWriter(workbook_document),
            CallableWriter(workbook_document),
        )
        for writer in workbook_ports:
            with self.subTest(workbook_writer=type(writer).__name__):
                result = workbook_service.source_write_back_status(
                    workbook_document.document_id,
                    writer,
                )
                self.assertIs(
                    result.status,
                    module.DocumentSourceWriteStatus.UNSUPPORTED,
                )
                self.assertEqual(
                    result.safe_code,
                    "PROJECT.SAVE.WRITER_UNAVAILABLE",
                )
                if isinstance(writer, CallableWriter):
                    self.assertEqual(writer.prepare_calls, [])

        legacy_codec = CodecIdentity("localcat", "localcat-json", "1")
        legacy_document = replace(
            baseline.documents[0],
            source_ref="legacy.json",
            display_name="Legacy project",
            order=0,
            format_id="localcat-json-v1",
            codec_identity=legacy_codec,
            writer_capability_snapshot=WriterCapabilitySnapshot(
                canonical_write=True,
                source_round_trip_write=False,
                format_profile="localcat-json-v1",
            ),
            codec_private_member=None,
        )
        legacy_workspace = ProjectWorkspace(
            schema_version=1,
            project_id=baseline.project_id,
            name="Legacy project",
            source_locale=baseline.source_locale,
            target_locale=baseline.target_locale,
            origin=ProjectOrigin(
                kind=ProjectOriginKind.SINGLE_FILE,
                profile_version="localcat-json-v1",
                portable_root_ref="legacy.json",
            ),
            persistence_kind=ProjectPersistenceKind.LEGACY_SINGLE_JSON,
            documents=(legacy_document,),
        )
        legacy_service = _service(module, baseline, baseline)
        legacy_origin_state = (
            module.DocumentOriginWriteState(
                legacy_document.document_id,
                module.OriginWriteState.IN_SYNC,
            ),
        )

        class ForgedIdentity:
            def __eq__(self, other: object) -> bool:
                del other
                return True

        class ForgedWriter(CallableWriter):
            def __init__(self, document: ProjectDocument) -> None:
                super().__init__(document)
                self.codec_identity = ForgedIdentity()

        with (
            patch.object(
                ProjectWorkspaceService,
                "workspace",
                new_callable=PropertyMock,
                return_value=legacy_workspace,
            ),
            patch.object(
                module.ProjectSaveService,
                "origin_write_state",
                new_callable=PropertyMock,
                return_value=legacy_origin_state,
            ),
        ):
            with self.assertRaises(TypeError):
                legacy_service.source_write_back_status(
                    legacy_document.document_id,
                    AttrOnlyWriter(legacy_document),
                )
            forged_writer = ForgedWriter(legacy_document)
            with self.assertRaises(TypeError):
                legacy_service.source_write_back_status(
                    legacy_document.document_id,
                    forged_writer,
                )
            self.assertEqual(forged_writer.prepare_calls, [])

            exact_writer = CallableWriter(legacy_document)
            result = legacy_service.source_write_back_status(
                legacy_document.document_id,
                exact_writer,
            )
            self.assertIs(
                result.status,
                module.DocumentSourceWriteStatus.AVAILABLE,
            )
            self.assertIsNone(result.safe_code)
            self.assertEqual(exact_writer.prepare_calls, [])

    def test_future_directory_mixed_results_are_structured_not_boolean(self) -> None:
        module = _cluster2b()
        statuses = (
            module.DocumentSaveStatus.SAVED,
            module.DocumentSaveStatus.ROLLED_BACK,
            module.DocumentSaveStatus.UNCHANGED,
            module.DocumentSaveStatus.FAILED,
        )
        document_ids = (_DOCUMENT_A, _DOCUMENT_B, _DOCUMENT_C, _DOCUMENT_D)
        results = tuple(
            module.DocumentSaveResult(
                document_id=document_id,
                status=status,
                before_digest=hashlib.sha256(
                    f"before-{index}".encode("ascii")
                ).hexdigest(),
                after_digest=hashlib.sha256(
                    f"after-{index}".encode("ascii")
                ).hexdigest(),
                safe_code=(
                    "PROJECT.SAVE.RECOVERY_REQUIRED"
                    if status
                    in {
                        module.DocumentSaveStatus.ROLLED_BACK,
                        module.DocumentSaveStatus.FAILED,
                    }
                    else None
                ),
            )
            for index, (document_id, status) in enumerate(
                zip(document_ids, statuses, strict=True)
            )
        )

        report = module.ProjectSaveReport(
            operation_id="save-" + "f" * 64,
            scope=module.SaveScope.WORKSPACE,
            origin_kind=ProjectOriginKind.DIRECTORY,
            workspace_revision=42,
            workspace_content_digest=_workspace_content_digest(
                _baseline_workspace()
            ),
            requested_count=4,
            saved_count=1,
            rolled_back_count=1,
            unchanged_count=1,
            failed_count=1,
            document_results=results,
            journal_state=module.SaveJournalState.RECOVERY_REQUIRED,
            recovery_required=True,
            retryable=True,
            safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
        )

        self.assertFalse(hasattr(report, "success"))
        self.assertEqual(
            tuple(result.status for result in report.document_results),
            statuses,
        )
        self.assertEqual(
            (
                report.saved_count,
                report.rolled_back_count,
                report.unchanged_count,
                report.failed_count,
            ),
            (1, 1, 1, 1),
        )
        self.assertNotIn("DirectoryProjectWriter", module.__dict__)

    def test_future_workbook_carrier_unit_fault_never_advances_partial_baseline(
        self,
    ) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        current = _edit_document(
            current,
            _DOCUMENT_B,
            target="draft-b",
            confirmed=True,
        )
        scenarios = (
            (
                ("readback",),
                module.DocumentSaveStatus.ROLLED_BACK,
                False,
            ),
            (
                ("readback", "rollback"),
                module.DocumentSaveStatus.FAILED,
                True,
            ),
        )
        for faults, status, recovery_required in scenarios:
            with self.subTest(faults=faults):
                port = _InMemoryPersistencePort(baseline, faults=faults)
                service = _service(module, current, baseline)

                report = service.save_workspace(port)

                self.assertEqual(len(port.staged), 1)
                self.assertEqual(
                    _targets(port.staged[0].candidate_workspace),
                    ("draft-a", "draft-b"),
                )
                self.assertEqual(
                    tuple(result.status for result in report.document_results),
                    (status, status),
                )
                self.assertEqual(report.recovery_required, recovery_required)
                self.assertEqual(service.saved_workspace_snapshot, baseline)
                self.assertEqual(
                    service.dirty_document_ids,
                    (_DOCUMENT_A, _DOCUMENT_B),
                )
                self.assertNotIn("WorkbookProjectWriter", module.__dict__)

    def test_reader_only_source_bytes_never_change_when_package_baseline_saves(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            txt = root / "chapter-a.txt"
            po = root / "chapter-b.po"
            txt.write_bytes(b"source-only txt\n")
            po.write_bytes(b'msgid "Source"\nmsgstr ""\n')
            before = {path: path.read_bytes() for path in (txt, po)}
            port = _InMemoryPersistencePort(baseline)

            report = _service(module, current, baseline).save_workspace(port)

            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )
            self.assertEqual(report.saved_count, 1)
            self.assertEqual(
                tuple(
                    document.writer_capability_snapshot.canonical_write
                    or document.writer_capability_snapshot.source_round_trip_write
                    for document in current.documents
                ),
                (False, False),
            )

    def test_multi_document_candidate_never_partially_advances_baseline(self) -> None:
        module = _cluster2b()
        baseline = _baseline_workspace()
        current = _edit_document(
            baseline,
            _DOCUMENT_A,
            target="draft-a",
            confirmed=False,
        )
        current = _edit_document(
            current,
            _DOCUMENT_B,
            target="draft-b",
            confirmed=True,
        )
        port = _InMemoryPersistencePort(
            baseline,
            faults=("readback", "rollback"),
        )
        service = _service(module, current, baseline)

        report = service.save_workspace(port)

        self.assertTrue(report.recovery_required)
        self.assertEqual(
            tuple(result.status for result in report.document_results),
            (
                module.DocumentSaveStatus.FAILED,
                module.DocumentSaveStatus.FAILED,
            ),
        )
        self.assertEqual(service.saved_workspace_snapshot, baseline)
        self.assertEqual(service.dirty_document_ids, (_DOCUMENT_A, _DOCUMENT_B))
        self.assertNotEqual(port.installed_workspace, baseline)
        self.assertIsNotNone(port.pending)


if __name__ == "__main__":
    unittest.main()
