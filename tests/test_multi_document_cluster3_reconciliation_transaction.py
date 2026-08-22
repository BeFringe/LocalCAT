"""Focused C3 contracts for prepared source-reconciliation publication."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from project_save import ProjectSaveService, WorkspaceSaveBaseline
from project_workspace import (
    PreparedReconciliationToken,
    ProjectWorkspaceService,
)
from project_workspace_contracts import SegmentIdentity
from project_workspace_identity import ProjectWorkspaceError
from tests.test_multi_document_cluster2a_aggregation import (
    _cluster2a_intake,
    _stage,
    _write_localcat_project,
)


class Cluster3PreparedReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-c3-reconcile-transaction-",
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        intake = _cluster2a_intake()
        source = _write_localcat_project(
            self.root,
            "chapter.json",
            (("segment-1", "Source", "saved target", True),),
        )
        anchor = _write_localcat_project(
            self.root,
            "anchor.json",
            (("anchor-1", "Anchor", "", False),),
        )
        self.current = _stage(intake, self.root, (source, anchor))
        current_document = self.current.workspace.documents[0]
        incoming_workspace = replace(
            self.current.workspace,
            documents=(
                replace(current_document, display_name="Reconciled chapter"),
                self.current.workspace.documents[1],
            ),
        )
        self.incoming = replace(self.current, workspace=incoming_workspace)
        self.service = ProjectWorkspaceService(
            self.current.workspace,
            self.current.origin_binding,
            session_id="prepared-session",
            revision=5,
        )
        self.save_service = ProjectSaveService(
            self.service,
            baseline=WorkspaceSaveBaseline.from_workspace(
                self.current.workspace,
                workspace_revision=5,
            ),
        )

    def _preview(self):
        return self.service.stage_reconciliation(
            self.incoming,
            associations=(),
            session_id="prepared-session",
            base_revision=5,
        )

    def _prepare(self, operation_id: str):
        return self.service.prepare_reconciliation(
            operation_id,
            decisions=(),
            session_id="prepared-session",
            base_revision=5,
            incoming_source_identities=self.incoming.source_identities,
        )

    def test_prepare_preserves_operation_and_old_authority_until_commit(self) -> None:
        preview = self._preview()
        old_workspace = self.service.workspace
        old_binding = self.service.origin_binding

        token = self._prepare(preview.operation_id)
        candidate = self.service.prepared_workspace_service(token)
        candidate_save = self.save_service.fork_for_workspace_service(candidate)

        self.assertIs(type(token), PreparedReconciliationToken)
        self.assertEqual(token.operation_id, preview.operation_id)
        self.assertIs(self.service.workspace, old_workspace)
        self.assertIs(self.service.origin_binding, old_binding)
        self.assertEqual(self.service.revision, 5)
        self.assertIsNot(candidate, self.service)
        self.assertEqual(candidate.session_id, self.service.session_id)
        self.assertEqual(candidate.revision, 6)
        self.assertEqual(
            candidate.workspace.documents[0].display_name,
            "Reconciled chapter",
        )
        self.assertIs(
            candidate_save.saved_workspace_snapshot,
            self.save_service.saved_workspace_snapshot,
        )
        self.assertEqual(
            candidate_save.saved_package_digest,
            self.save_service.saved_package_digest,
        )
        self.assertTrue(candidate_save.project_dirty)

        receipt = self.service.commit_reconciliation(token)

        self.assertEqual(receipt.operation_id, preview.operation_id)
        self.assertEqual(receipt.published_revision, 6)
        self.assertIs(self.service.workspace, candidate.workspace)
        self.assertIs(self.service.origin_binding, candidate.origin_binding)
        self.assertEqual(self.service.revision, candidate.revision)
        with self.assertRaisesRegex(
            ProjectWorkspaceError,
            "PROJECT.RECONCILE.PREVIEW_STALE",
        ):
            self.service.commit_reconciliation(token)

    def test_forged_and_foreign_tokens_do_not_consume_the_issued_token(self) -> None:
        preview = self._preview()
        token = self._prepare(preview.operation_id)
        forged = PreparedReconciliationToken(preview.operation_id)
        other = ProjectWorkspaceService(
            self.current.workspace,
            self.current.origin_binding,
            session_id="foreign-session",
            revision=5,
        )

        for owner, candidate_token in (
            (self.service, forged),
            (other, token),
        ):
            with self.assertRaisesRegex(
                ProjectWorkspaceError,
                "PROJECT.RECONCILE.PREVIEW_STALE",
            ):
                owner.commit_reconciliation(candidate_token)

        receipt = self.service.commit_reconciliation(token)
        self.assertEqual(receipt.operation_id, preview.operation_id)

    def test_candidate_drift_makes_commit_stale_without_old_mutation(self) -> None:
        preview = self._preview()
        token = self._prepare(preview.operation_id)
        candidate = self.service.prepared_workspace_service(token)
        old_workspace = self.service.workspace
        old_binding = self.service.origin_binding
        document = candidate.workspace.documents[0]
        identity = SegmentIdentity(
            document.document_id,
            document.source_segments[0].local_segment_id,
        )
        candidate.update_segment_edit(
            identity,
            target="candidate-only edit",
            confirmed=False,
            session_id=candidate.session_id,
            base_revision=candidate.revision,
        )

        with self.assertRaisesRegex(
            ProjectWorkspaceError,
            "PROJECT.RECONCILE.PREVIEW_STALE",
        ):
            self.service.commit_reconciliation(token)

        self.assertIs(self.service.workspace, old_workspace)
        self.assertIs(self.service.origin_binding, old_binding)
        self.assertEqual(self.service.revision, 5)
        with self.assertRaisesRegex(
            ProjectWorkspaceError,
            "PROJECT.RECONCILE.PREVIEW_STALE",
        ):
            self.service.commit_reconciliation(token)

    def test_prepare_fault_leaves_old_authority_and_original_preview_usable(self) -> None:
        preview = self._preview()
        old_workspace = self.service.workspace
        old_binding = self.service.origin_binding

        with mock.patch.object(
            ProjectWorkspaceService,
            "_build_candidate",
            side_effect=RuntimeError("injected projection fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected projection fault"):
                self._prepare(preview.operation_id)

        self.assertIs(self.service.workspace, old_workspace)
        self.assertIs(self.service.origin_binding, old_binding)
        self.assertEqual(self.service.revision, 5)
        token = self._prepare(preview.operation_id)
        self.service.commit_reconciliation(token)
        self.assertEqual(self.service.revision, 6)

    def test_legacy_apply_is_prepare_then_commit_compatible(self) -> None:
        preview = self._preview()

        receipt = self.service.apply_reconciliation(
            preview.operation_id,
            decisions=(),
            session_id="prepared-session",
            base_revision=5,
            incoming_source_identities=self.incoming.source_identities,
        )

        self.assertEqual(receipt.operation_id, preview.operation_id)
        self.assertEqual(self.service.revision, 6)
        with self.assertRaisesRegex(
            ProjectWorkspaceError,
            "PROJECT.RECONCILE.PREVIEW_STALE",
        ):
            self.service.prepare_reconciliation(
                preview.operation_id,
                decisions=(),
                session_id="prepared-session",
                base_revision=5,
                incoming_source_identities=self.incoming.source_identities,
            )


if __name__ == "__main__":
    unittest.main()
