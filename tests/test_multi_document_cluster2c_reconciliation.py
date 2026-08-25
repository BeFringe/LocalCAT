"""C2C contracts for binding-neutral same-project package reconciliation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import project_workspace as workspace_module
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import ProjectWorkspaceError
from tests.test_multi_document_cluster2a_aggregation import (
    _cluster2a_intake,
    _request,
    _stage,
    _write_localcat_project,
)


class Cluster2CPackageReconciliationTests(unittest.TestCase):
    def _current_and_incoming(self, root: Path) -> tuple[object, object]:
        intake = _cluster2a_intake()
        primary = _write_localcat_project(
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
        anchor = _write_localcat_project(
            root,
            "anchor.json",
            (("anchor", "Anchor source", "anchor-target", True),),
        )
        current = _stage(intake, root, (primary, anchor))
        _write_localcat_project(
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
        incoming = _stage(
            intake,
            root,
            (primary, anchor),
            request=_request(
                intake,
                origin_binding=current.origin_binding,
                expected_binding_revision=current.origin_binding.revision,
            ),
        )
        return current, incoming

    @staticmethod
    def _associations(current: object, incoming: object) -> tuple[object, ...]:
        document_id = current.workspace.documents[0].document_id
        identity = SegmentIdentity
        return (
            workspace_module.ReconciliationAssociation(
                current_identity=identity(document_id, "ambiguous-old"),
                incoming_identities=(
                    identity(document_id, "ambiguous-new-1"),
                    identity(document_id, "ambiguous-new-2"),
                ),
            ),
            workspace_module.ReconciliationAssociation(
                current_identity=identity(document_id, "unresolved-old"),
                incoming_identities=(),
            ),
        )

    def test_package_workspace_uses_same_six_categories_and_exact_apply_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, incoming = self._current_and_incoming(root)
            associations = self._associations(current, incoming)
            selected_service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="selected-session",
                revision=7,
            )
            package_service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="package-session",
                revision=7,
            )
            selected_preview = selected_service.stage_reconciliation(
                incoming,
                associations=associations,
                session_id="selected-session",
                base_revision=7,
            )
            preview = package_service.stage_workspace_reconciliation(
                incoming.workspace,
                associations=associations,
                session_id="package-session",
                base_revision=7,
            )

            category_fields = (
                "unchanged_identities",
                "source_changed_identities",
                "new_identities",
                "removed_identities",
                "ambiguous_identities",
                "unresolved_identities",
                "required_decision_identities",
                "association_options",
            )
            for field in category_fields:
                self.assertEqual(getattr(preview, field), getattr(selected_preview, field))
            self.assertEqual(preview.unchanged_count, 2)
            self.assertEqual(preview.source_changed_count, 1)
            self.assertEqual(preview.new_count, 1)
            self.assertEqual(preview.removed_count, 1)
            self.assertEqual(preview.ambiguous_count, 1)
            self.assertEqual(preview.unresolved_count, 1)
            self.assertEqual(
                preview.proposed_workspace_digest,
                selected_preview.proposed_workspace_digest,
            )
            for body in (
                "Same source",
                "Old source",
                "New source",
                "keep-confirmed",
                "recover-removed",
                "incoming-new-target",
            ):
                self.assertNotIn(body, repr(preview))

            document_id = current.workspace.documents[0].document_id
            decision = workspace_module.ReconciliationDecision
            disposition = workspace_module.ReconciliationDisposition
            decisions = (
                decision(
                    identity=SegmentIdentity(document_id, "removed"),
                    disposition=disposition.REMOVE,
                ),
                decision(
                    identity=SegmentIdentity(document_id, "ambiguous-old"),
                    disposition=disposition.ACCEPT_ASSOCIATION,
                    accepted_incoming_identity=SegmentIdentity(
                        document_id,
                        "ambiguous-new-1",
                    ),
                ),
                decision(
                    identity=SegmentIdentity(document_id, "unresolved-old"),
                    disposition=disposition.KEEP_DETACHED,
                ),
            )
            binding_before = package_service.origin_binding
            receipt = package_service.apply_workspace_reconciliation(
                preview.operation_id,
                incoming=incoming.workspace,
                decisions=decisions,
                session_id="package-session",
                base_revision=7,
            )

            primary = package_service.workspace.documents[0]
            by_local_id = {
                segment.identity.local_segment_id: segment
                for segment in primary.segments
            }
            self.assertEqual(by_local_id["unchanged"].target, "keep-confirmed")
            self.assertTrue(by_local_id["unchanged"].confirmed)
            self.assertEqual(by_local_id["changed"].source, "New source")
            self.assertEqual(by_local_id["changed"].target, "keep-draft")
            self.assertFalse(by_local_id["changed"].confirmed)
            self.assertEqual(by_local_id["new"].target, "incoming-new-target")
            self.assertFalse(by_local_id["new"].confirmed)
            self.assertNotIn("removed", by_local_id)
            self.assertEqual(
                by_local_id["ambiguous-old"].target,
                "recover-ambiguous",
            )
            self.assertFalse(by_local_id["ambiguous-old"].confirmed)
            unresolved = next(
                source
                for source in primary.source_segments
                if source.local_segment_id == "unresolved-old"
            )
            self.assertIs(unresolved.source_presence, SourcePresence.DETACHED)
            self.assertEqual(package_service.origin_binding, binding_before)
            self.assertEqual(package_service.revision, 8)
            self.assertEqual(receipt.published_revision, 8)

    def test_required_decisions_failure_consumes_capability_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, incoming = self._current_and_incoming(root)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="single-use",
                revision=11,
            )
            preview = service.stage_workspace_reconciliation(
                incoming.workspace,
                associations=self._associations(current, incoming),
                session_id="single-use",
                base_revision=11,
            )
            before = (service.workspace, service.origin_binding, service.revision)

            with self.assertRaises(ProjectWorkspaceError) as caught:
                service.apply_workspace_reconciliation(
                    preview.operation_id,
                    incoming=incoming.workspace,
                    decisions=(),
                    session_id="single-use",
                    base_revision=11,
                )
            self.assertEqual(caught.exception.code, "PROJECT.RECONCILE.DECISION_REQUIRED")
            self.assertEqual(
                (service.workspace, service.origin_binding, service.revision),
                before,
            )
            with self.assertRaises(ProjectWorkspaceError) as reused:
                service.apply_workspace_reconciliation(
                    preview.operation_id,
                    incoming=incoming.workspace,
                    decisions=(),
                    session_id="single-use",
                    base_revision=11,
                )
            self.assertEqual(reused.exception.code, "PROJECT.RECONCILE.PREVIEW_STALE")

    def test_workspace_session_revision_and_digest_stale_are_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, incoming = self._current_and_incoming(root)
            scenarios = ("session", "revision", "incoming-digest", "current-digest")
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    service = workspace_module.ProjectWorkspaceService(
                        current.workspace,
                        current.origin_binding,
                        session_id="stale-session",
                        revision=19,
                    )
                    preview = service.stage_workspace_reconciliation(
                        incoming.workspace,
                        associations=(),
                        session_id="stale-session",
                        base_revision=19,
                    )
                    apply_incoming = incoming.workspace
                    session_id = "stale-session"
                    revision = 19
                    expected_code = "PROJECT.RECONCILE.PREVIEW_STALE"
                    if scenario == "session":
                        session_id = "other-session"
                    elif scenario == "revision":
                        revision = 20
                    elif scenario == "incoming-digest":
                        apply_incoming = replace(incoming.workspace, name="changed incoming")
                        expected_code = "PROJECT.RECONCILE.SOURCE_STALE"
                    else:
                        service._workspace = replace(  # type: ignore[attr-defined]
                            current.workspace,
                            name="changed current",
                        )
                    before = (service.workspace, service.origin_binding, service.revision)

                    with self.assertRaises(ProjectWorkspaceError) as caught:
                        service.apply_workspace_reconciliation(
                            preview.operation_id,
                            incoming=apply_incoming,
                            decisions=(),
                            session_id=session_id,
                            base_revision=revision,
                        )
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertEqual(
                        (service.workspace, service.origin_binding, service.revision),
                        before,
                    )
                    self.assertNotIn("Same source", str(caught.exception))
                    with self.assertRaises(ProjectWorkspaceError) as reused:
                        service.apply_workspace_reconciliation(
                            preview.operation_id,
                            incoming=incoming.workspace,
                            decisions=(),
                            session_id="stale-session",
                            base_revision=19,
                        )
                    self.assertEqual(
                        reused.exception.code,
                        "PROJECT.RECONCILE.PREVIEW_STALE",
                    )

    def test_exact_workspace_and_same_project_are_required_before_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, incoming = self._current_and_incoming(root)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                current.origin_binding,
                session_id="input-session",
                revision=3,
            )
            before = (service.workspace, service.origin_binding, service.revision)
            invalid_inputs = (
                incoming,
                replace(incoming.workspace, project_id="prj-" + "f" * 64),
            )
            for invalid in invalid_inputs:
                with self.subTest(invalid_type=type(invalid).__name__):
                    with self.assertRaises(ProjectWorkspaceError) as caught:
                        service.stage_workspace_reconciliation(
                            invalid,  # type: ignore[arg-type]
                            associations=(),
                            session_id="input-session",
                            base_revision=3,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "PROJECT.RECONCILE.INPUT_INVALID",
                    )
            self.assertEqual(
                (service.workspace, service.origin_binding, service.revision),
                before,
            )

    def test_cold_package_workspace_is_explicitly_unbound_until_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, incoming = self._current_and_incoming(root)
            service = workspace_module.ProjectWorkspaceService(
                current.workspace,
                None,
                session_id="cold-package-session",
                revision=0,
            )

            preview = service.stage_workspace_reconciliation(
                current.workspace,
                associations=(),
                session_id="cold-package-session",
                base_revision=0,
            )
            service.apply_workspace_reconciliation(
                preview.operation_id,
                incoming=current.workspace,
                decisions=(),
                session_id="cold-package-session",
                base_revision=0,
            )
            self.assertIsNone(service.origin_binding)
            self.assertEqual(service.revision, 1)

            before = (service.workspace, service.origin_binding, service.revision)
            with self.assertRaises(ProjectWorkspaceError) as caught:
                service.stage_reconciliation(
                    incoming,
                    associations=(),
                    session_id="cold-package-session",
                    base_revision=1,
                )
            self.assertEqual(
                caught.exception.code,
                "PROJECT.RECONCILE.SOURCE_STALE",
            )
            self.assertEqual(
                (service.workspace, service.origin_binding, service.revision),
                before,
            )


if __name__ == "__main__":
    unittest.main()
