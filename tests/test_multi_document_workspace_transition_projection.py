"""Published, body-free workspace transition facts for downstream rebase."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from pathlib import Path
import tempfile
import unittest

import project_workspace as workspace_module
from collaborative_chunk_contracts import ChunkError
from collaborative_chunk_workspace_adapter import capture_live_workspace_transition
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import ProjectWorkspaceError
from tests.test_multi_document_cluster2a_aggregation import (
    _cluster2a_intake,
    _request,
    _stage,
    _write_localcat_project,
)


def _current_and_incoming(root: Path) -> tuple[object, object]:
    intake = _cluster2a_intake()
    primary = _write_localcat_project(
        root,
        "primary.json",
        (
            ("unchanged", "Same source", "old-unallocated", False),
            ("changed", "Old source", "draft", False),
            ("detached", "Detached source", "detached-target", True),
            ("removed", "Removed source", "removed-target", True),
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
            ("unchanged", "Same source", "incoming-loses", False),
            ("changed", "New source", "incoming-loses", False),
            ("new", "Brand new source", "new-target", False),
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


def _remove_decision(document_id: str) -> tuple[object, ...]:
    return (
        workspace_module.ReconciliationDecision(
            identity=SegmentIdentity(document_id, "detached"),
            disposition=workspace_module.ReconciliationDisposition.KEEP_DETACHED,
        ),
        workspace_module.ReconciliationDecision(
            identity=SegmentIdentity(document_id, "removed"),
            disposition=workspace_module.ReconciliationDisposition.REMOVE,
        ),
    )


def _field_names(value: object) -> set[str]:
    result: set[str] = set()
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            result.add(field.name)
            result.update(_field_names(getattr(value, field.name)))
    elif type(value) is tuple:
        for item in value:
            result.update(_field_names(item))
    return result


class PublishedWorkspaceTransitionProjectionTests(unittest.TestCase):
    def _publish(self, root: Path) -> tuple[object, object, object, object]:
        current, incoming = _current_and_incoming(root)
        service = workspace_module.ProjectWorkspaceService(
            current.workspace,
            current.origin_binding,
            session_id="transition-session",
            revision=7,
        )
        preview = service.stage_workspace_reconciliation(
            incoming.workspace,
            associations=(),
            session_id=service.session_id,
            base_revision=service.revision,
        )
        with self.assertRaises(ProjectWorkspaceError) as unpublished:
            service.published_workspace_transition(preview)  # type: ignore[arg-type]
        self.assertEqual(
            unpublished.exception.code,
            "PROJECT.RECONCILE.PREVIEW_STALE",
        )
        document_id = current.workspace.documents[0].document_id
        receipt = service.apply_workspace_reconciliation(
            preview.operation_id,
            incoming=incoming.workspace,
            decisions=_remove_decision(document_id),
            session_id=service.session_id,
            base_revision=service.revision,
        )
        transition = service.published_workspace_transition(receipt)
        return current, service, receipt, transition

    def test_publication_issues_complete_old_and_current_universes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            current, service, receipt, transition = self._publish(root)

            self.assertEqual(transition.operation_id, receipt.operation_id)
            self.assertEqual(
                transition.previous.binding.workspace_revision,
                receipt.base_revision,
            )
            self.assertEqual(
                transition.previous.binding.workspace_composition_revision,
                0,
            )
            self.assertEqual(
                transition.current.binding.workspace_revision,
                receipt.published_revision,
            )
            self.assertEqual(
                transition.current.binding.workspace_composition_revision,
                1,
            )
            self.assertEqual(service.composition_revision, 1)
            self.assertEqual(
                transition.previous.binding.workspace_digest,
                receipt.previous_workspace_digest,
            )
            self.assertEqual(
                transition.current.binding.workspace_digest,
                receipt.published_workspace_digest,
            )
            self.assertEqual(
                transition.previous.binding.workspace_session_id,
                service.session_id,
            )

            previous_ids = {entry.identity for entry in transition.previous.entries}
            current_ids = {entry.identity for entry in transition.current.entries}
            document_id = current.workspace.documents[0].document_id
            old_unallocated = SegmentIdentity(document_id, "unchanged")
            true_new = SegmentIdentity(document_id, "new")
            source_changed = SegmentIdentity(document_id, "changed")
            detached = SegmentIdentity(document_id, "detached")
            removed = SegmentIdentity(document_id, "removed")
            self.assertIn(old_unallocated, previous_ids)
            self.assertIn(old_unallocated, current_ids)
            self.assertNotIn(true_new, previous_ids)
            self.assertIn(true_new, current_ids)
            self.assertIn(detached, previous_ids)
            self.assertIn(detached, current_ids)
            self.assertIn(removed, previous_ids)
            self.assertNotIn(removed, current_ids)
            current_presence = {
                entry.identity: entry.source_presence
                for entry in transition.current.entries
            }
            self.assertIs(current_presence[detached], SourcePresence.DETACHED)
            self.assertEqual(
                transition.source_changed_identities,
                (source_changed,),
            )
            self.assertEqual(
                transition.previous.binding.segment_universe_digest,
                workspace_module.workspace_segment_universe_digest_v1(
                    transition.previous.binding.project_id,
                    transition.previous.entries,
                ),
            )
            self.assertEqual(
                transition.current.binding.segment_universe_digest,
                workspace_module.workspace_segment_universe_digest_v1(
                    transition.current.binding.project_id,
                    transition.current.entries,
                ),
            )

            forbidden = {
                "source",
                "target",
                "speaker",
                "raw_speaker",
                "source_ref",
                "path",
                "display_name",
                "order",
            }
            self.assertTrue(forbidden.isdisjoint(_field_names(transition)))
            for body in (
                "Same source",
                "New source",
                "Brand new source",
                "old-unallocated",
                str(root),
            ):
                self.assertNotIn(body, repr(transition))

    def test_tamper_foreign_and_noncanonical_facts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _current, _service, _receipt, transition = self._publish(root)

            with self.assertRaises(ProjectWorkspaceError) as changed:
                replace(transition, source_changed_identities=())
            self.assertEqual(changed.exception.code, "PROJECT.RECONCILE.INPUT_INVALID")

            reversed_entries = tuple(reversed(transition.current.entries))
            with self.assertRaises(ProjectWorkspaceError) as noncanonical:
                replace(transition.current, entries=reversed_entries)
            self.assertEqual(
                noncanonical.exception.code,
                "PROJECT.RECONCILE.INPUT_INVALID",
            )

            first = transition.current.entries[0]
            changed_presence = (
                SourcePresence.DETACHED
                if first.source_presence is SourcePresence.ATTACHED
                else SourcePresence.ATTACHED
            )
            tampered_entries = (
                replace(first, source_presence=changed_presence),
                *transition.current.entries[1:],
            )
            tampered_entries = tuple(
                sorted(tampered_entries, key=lambda item: (
                    item.identity.document_id,
                    item.identity.local_segment_id,
                ))
            )
            with self.assertRaises(ProjectWorkspaceError) as presence:
                replace(transition.current, entries=tampered_entries)
            self.assertEqual(presence.exception.code, "PROJECT.RECONCILE.INPUT_INVALID")

            foreign_project = "prj-" + "f" * 64
            foreign_binding = replace(
                transition.current.binding,
                project_id=foreign_project,
                segment_universe_digest=(
                    workspace_module.workspace_segment_universe_digest_v1(
                        foreign_project,
                        transition.current.entries,
                    )
                ),
            )
            foreign_current = replace(
                transition.current,
                binding=foreign_binding,
            )
            foreign_digest = workspace_module.published_workspace_transition_digest_v1(
                transition.operation_id,
                transition.previous,
                foreign_current,
                transition.source_changed_identities,
            )
            with self.assertRaises(ProjectWorkspaceError) as foreign:
                workspace_module.PublishedWorkspaceTransitionProjection(
                    operation_id=transition.operation_id,
                    previous=transition.previous,
                    current=foreign_current,
                    source_changed_identities=transition.source_changed_identities,
                    transition_digest=foreign_digest,
                )
            self.assertEqual(foreign.exception.code, "PROJECT.RECONCILE.INPUT_INVALID")

    def test_only_live_issuing_owner_revalidates_and_cold_service_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _current, service, receipt, transition = self._publish(root)

            cold = workspace_module.ProjectWorkspaceService(
                service.workspace,
                None,
                session_id="cold-session",
                revision=0,
            )
            with self.assertRaises(ProjectWorkspaceError) as cold_carried:
                cold.validate_published_workspace_transition(transition)
            self.assertEqual(
                cold_carried.exception.code,
                "PROJECT.RECONCILE.PREVIEW_STALE",
            )

            identity = transition.current.entries[0].identity
            cold.update_segment_edit(
                identity,
                target="post-transition edit",
                confirmed=False,
                session_id=cold.session_id,
                base_revision=cold.revision,
            )
            with self.assertRaises(ProjectWorkspaceError) as cold_after_edit:
                cold.validate_published_workspace_transition(transition)
            self.assertEqual(
                cold_after_edit.exception.code,
                "PROJECT.RECONCILE.PREVIEW_STALE",
            )

            service.update_segment_edit(
                identity,
                target="issuing-session edit",
                confirmed=False,
                session_id=service.session_id,
                base_revision=service.revision,
            )
            self.assertEqual(service.composition_revision, 1)
            self.assertIs(service.published_workspace_transition(receipt), transition)

            with self.assertRaises(ProjectWorkspaceError) as reconstructed:
                service.validate_published_workspace_transition(replace(transition))
            self.assertEqual(
                reconstructed.exception.code,
                "PROJECT.RECONCILE.PREVIEW_STALE",
            )

            first_document = cold.workspace.documents[0]
            stale_workspace = replace(
                cold.workspace,
                documents=(
                    replace(
                        first_document,
                        source_segments=first_document.source_segments[1:],
                        editing_overlay=first_document.editing_overlay[1:],
                    ),
                    *cold.workspace.documents[1:],
                ),
            )
            stale_service = workspace_module.ProjectWorkspaceService(
                stale_workspace,
                None,
                session_id="stale-universe-session",
                revision=0,
            )
            with self.assertRaises(ProjectWorkspaceError) as stale:
                stale_service.validate_published_workspace_transition(transition)
            self.assertEqual(stale.exception.code, "PROJECT.RECONCILE.PREVIEW_STALE")

    def test_chunk_adapter_requires_the_exact_live_issuing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            _current, service, _receipt, transition = self._publish(root)

            translated = capture_live_workspace_transition(service, transition)
            self.assertEqual(
                translated.previous.binding.segment_universe_digest,
                transition.previous.binding.segment_universe_digest,
            )
            self.assertEqual(
                translated.current.binding.segment_universe_digest,
                transition.current.binding.segment_universe_digest,
            )
            self.assertEqual(
                tuple(
                    member.identity
                    for member in translated.source_changed_members
                ),
                transition.source_changed_identities,
            )

            cold = workspace_module.ProjectWorkspaceService(
                service.workspace,
                None,
                session_id="adapter-cold",
                revision=0,
            )
            with self.assertRaises(ChunkError) as rejected:
                capture_live_workspace_transition(cold, transition)
            self.assertEqual(rejected.exception.code, "CHUNK.PREVIEW_STALE")


if __name__ == "__main__":
    unittest.main()
