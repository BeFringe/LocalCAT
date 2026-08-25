from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from collaborative_chunk_conflict import (
    ChunkConflictClassification,
    ChunkConflictResolution,
    ChunkMetadataConflictService,
)
from collaborative_chunk_contracts import (
    ChunkError,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    LocalReferenceManagerHandle,
    TopologyAction,
    chunk_plan_binding,
    chunk_segment_ref_from_ids,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
)
from collaborative_chunk_store import (
    CollaborativeChunkStore,
    encode_chunk_metadata_state,
)
from collaborative_chunks import ChunkTopologyPublicationAuthority
from project_workspace_contracts import SourcePresence
from project_workspace_identity import issue_project_id


class _Issuer:
    def __init__(self, kind: str, start: int) -> None:
        self.kind = kind
        self.value = start

    def __call__(self) -> str:
        seed = self.value.to_bytes(32, "big")
        self.value += 1
        if self.kind == "chunk":
            return issue_chunk_id(seed)
        if self.kind == "plan":
            return issue_chunk_plan_id(seed)
        return issue_chunk_operation_id(seed)


class CollaborativeChunkCluster3ConflictUndoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="localcat-chunk-c3b-")
        self.root = Path(self.temporary.name).resolve()
        self.project_id = issue_project_id(b"P" * 32)
        document_id = "doc-" + b"D".hex() * 32
        self.member = chunk_segment_ref_from_ids(
            self.project_id,
            document_id,
            "001",
        )
        self.entries = (
            ChunkUniverseEntry(self.member, SourcePresence.ATTACHED),
        )
        self.binding = ChunkWorkspaceBinding(
            project_id=self.project_id,
            workspace_session_id="c3b-session",
            workspace_revision=0,
            segment_universe_digest=segment_universe_digest_v1(
                self.project_id,
                self.entries,
            ),
        )
        self.manager = LocalReferenceManagerHandle("local", "manager")
        self.current_root = self.root / "current"
        self.current_root.mkdir()
        self.current, self.current_topology = self._authority(
            self.current_root,
            1,
        )
        self._create(self.current, self.current_topology)
        self.common = self.current._metadata_state_for_conflict_owner()
        assert self.common is not None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _authority(self, root: Path, seed: int):
        authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.binding,
            metadata_store=CollaborativeChunkStore(
                root,
                "chunks.json",
                project_id=self.project_id,
            ),
        )
        topology = authority.create_topology_service(
            workspace_universe_provider=lambda: ChunkWorkspaceUniverseProjection(
                self.binding,
                self.entries,
            ),
            chunk_id_issuer=_Issuer("chunk", seed),
            plan_id_issuer=_Issuer("plan", seed + 100),
            operation_id_issuer=_Issuer("operation", seed + 200),
        )
        return authority, topology

    def _create(self, authority, topology) -> None:
        capability = authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        preview = topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=None,
            name="Original",
            members=(self.member,),
        )
        topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=None,
        )

    def _rename(self, authority, topology, name: str):
        expected = chunk_plan_binding(authority.current_snapshot())
        capability = authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.RENAME,
        )
        preview = topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            chunk_id=authority.current_snapshot().chunks[0].chunk_id,
            name=name,
        )
        receipt = topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )
        return receipt

    def _dissolve(self, authority, topology):
        expected = chunk_plan_binding(authority.current_snapshot())
        capability = authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.DISSOLVE_PLAN,
        )
        preview = topology.preview_dissolve_plan(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )
        return topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )

    def _cold_from_common(self, name: str, seed: int):
        root = self.root / name
        root.mkdir()
        store = CollaborativeChunkStore(
            root,
            "chunks.json",
            project_id=self.project_id,
        )
        store.publish(self.common, expected_metadata_digest=None)
        return self._authority(root, seed)

    def _cold_from_state(self, name: str, state, seed: int):
        root = self.root / name
        root.mkdir()
        root.joinpath("chunks.json").write_bytes(
            encode_chunk_metadata_state(state)
        )
        return self._authority(root, seed)

    def _service(self, authority=None, seed: int = 900):
        return ChunkMetadataConflictService(
            self.current if authority is None else authority,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", seed),
        )

    def test_six_way_core_classification_and_diverged_explicit_replace(self) -> None:
        stale_payload = encode_chunk_metadata_state(self.common)
        self._rename(self.current, self.current_topology, "Current")
        incoming, incoming_topology = self._cold_from_common("incoming", 20)
        self._rename(incoming, incoming_topology, "Incoming")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        incoming_payload = encode_chunk_metadata_state(incoming_state)

        service = self._service(seed=1000)
        diverged = service.preview(incoming_payload)
        self.assertEqual(
            diverged.classification,
            ChunkConflictClassification.DIVERGED,
        )
        self.assertIsNotNone(diverged.replacement)
        self.assertEqual(diverged.replacement.affected_chunk_count, 1)
        self.assertEqual(
            diverged.required_action,
            TopologyAction.CONFLICT_REPLACE,
        )
        baseline = self.current.current_snapshot()
        self.assertIsNone(
            service.apply(diverged, ChunkConflictResolution.KEEP_CURRENT)
        )
        self.assertEqual(self.current.current_snapshot(), baseline)

        replace_preview = service.preview(incoming_payload)
        expected = chunk_plan_binding(self.current.current_snapshot())
        capability = self.current.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.CONFLICT_REPLACE,
        )
        receipt = service.apply(
            replace_preview,
            ChunkConflictResolution.REPLACE_INCOMING,
            capability=capability,
            manager=self.manager,
        )
        self.assertEqual(receipt.action, TopologyAction.CONFLICT_REPLACE)
        self.assertEqual(self.current.current_snapshot().chunks[0].name, "Incoming")
        self.assertEqual(
            self.current.current_snapshot().revision,
            baseline.revision + 1,
        )

        identical_payload = encode_chunk_metadata_state(
            self.current._metadata_state_for_conflict_owner()
        )
        identical = service.preview(identical_payload)
        self.assertEqual(
            identical.classification,
            ChunkConflictClassification.IDENTICAL,
        )
        self.assertIsNone(
            service.apply(identical, ChunkConflictResolution.AUTO)
        )
        stale = service.preview(stale_payload)
        self.assertEqual(stale.classification, ChunkConflictClassification.STALE)

    def test_verified_single_successor_is_fast_forward(self) -> None:
        base, _base_topology = self._cold_from_common("base", 40)
        incoming, incoming_topology = self._cold_from_common("forward", 60)
        self._rename(incoming, incoming_topology, "Forward")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        service = ChunkMetadataConflictService(
            base,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", 1200),
        )
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.FAST_FORWARD,
        )
        expected = chunk_plan_binding(base.current_snapshot())
        capability = base.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.CONFLICT_REPLACE,
        )
        receipt = service.apply(
            preview,
            ChunkConflictResolution.AUTO,
            capability=capability,
            manager=self.manager,
        )
        self.assertEqual(receipt.action, TopologyAction.RENAME)
        self.assertEqual(base.current_snapshot().chunks[0].name, "Forward")

    def test_initial_create_envelope_is_fast_forward_from_empty_store(self) -> None:
        empty_root = self.root / "empty-base"
        empty_root.mkdir()
        empty, _topology = self._authority(empty_root, 70)
        service = ChunkMetadataConflictService(
            empty,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", 1250),
        )
        preview = service.preview(encode_chunk_metadata_state(self.common))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.FAST_FORWARD,
        )
        self.assertEqual(preview.required_action, TopologyAction.CREATE)
        capability = empty.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        receipt = service.apply(
            preview,
            ChunkConflictResolution.AUTO,
            capability=capability,
            manager=self.manager,
        )
        self.assertEqual(receipt.action, TopologyAction.CREATE)
        self.assertEqual(empty.current_snapshot().chunks[0].name, "Original")

    def test_create_after_dissolve_is_exact_fast_forward_not_foreign(self) -> None:
        base, base_topology = self._cold_from_common("dissolved-base", 72)
        self._dissolve(base, base_topology)
        dissolved = base._metadata_state_for_conflict_owner()
        assert dissolved is not None

        incoming, incoming_topology = self._cold_from_state(
            "created-after-dissolve",
            dissolved,
            74,
        )
        self._create(incoming, incoming_topology)
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None

        service = ChunkMetadataConflictService(
            base,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", 1275),
        )
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.FAST_FORWARD,
        )
        self.assertEqual(preview.required_action, TopologyAction.CREATE)
        capability = base.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=None,
            action=TopologyAction.CREATE,
        )
        receipt = service.apply(
            preview,
            ChunkConflictResolution.AUTO,
            capability=capability,
            manager=self.manager,
        )
        self.assertEqual(receipt.action, TopologyAction.CREATE)
        self.assertEqual(base.current_snapshot(), incoming.current_snapshot())

    def test_diverged_dissolved_incoming_keeps_safe_non_replace_choices(self) -> None:
        self._rename(self.current, self.current_topology, "Current branch")
        incoming, incoming_topology = self._cold_from_common(
            "incoming-dissolved",
            76,
        )
        self._dissolve(incoming, incoming_topology)
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        payload = encode_chunk_metadata_state(incoming_state)
        service = self._service(seed=1285)

        preview = service.preview(payload)
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.DIVERGED,
        )
        self.assertIsNone(preview.replacement)
        self.assertIn("CHUNK.CONFLICT_REPLACE_UNAVAILABLE", preview.blockers)
        baseline = self.current.current_snapshot()
        self.assertIsNone(
            service.apply(preview, ChunkConflictResolution.KEEP_CURRENT)
        )
        self.assertEqual(self.current.current_snapshot(), baseline)

        replace_preview = service.preview(payload)
        with self.assertRaises(ChunkError) as caught:
            service.apply(
                replace_preview,
                ChunkConflictResolution.REPLACE_INCOMING,
            )
        self.assertEqual(
            caught.exception.code,
            "CHUNK.CONFLICT_REPLACE_UNAVAILABLE",
        )

    def test_multi_step_envelope_is_not_guessed_as_fast_forward(self) -> None:
        base, _base_topology = self._cold_from_common("multibase", 80)
        incoming, incoming_topology = self._cold_from_common("multiforward", 100)
        self._rename(incoming, incoming_topology, "Step one")
        self._rename(incoming, incoming_topology, "Step two")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        service = ChunkMetadataConflictService(
            base,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", 1300),
        )
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.DIVERGED,
        )

    def test_foreign_plan_generation_is_distinct_from_stale_lineage(self) -> None:
        dissolved, dissolved_topology = self._cold_from_common(
            "foreign-dissolved",
            106,
        )
        self._dissolve(dissolved, dissolved_topology)
        dissolved_state = dissolved._metadata_state_for_conflict_owner()
        assert dissolved_state is not None

        branches = []
        for name, seed in (("foreign-a", 108), ("foreign-b", 112)):
            authority, topology = self._cold_from_state(
                name,
                dissolved_state,
                seed,
            )
            self._create(authority, topology)
            branches.append(authority)
        incoming_state = branches[1]._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        preview = ChunkMetadataConflictService(
            branches[0],
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=_Issuer("operation", 1325),
        ).preview(encode_chunk_metadata_state(incoming_state))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.FOREIGN,
        )

    def test_preview_replay_and_workspace_drift_are_rejected(self) -> None:
        incoming, incoming_topology = self._cold_from_common("preview-cas", 116)
        self._rename(incoming, incoming_topology, "Incoming CAS")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        binding = [self.binding]
        service = ChunkMetadataConflictService(
            self.current,
            workspace_binding_provider=lambda: binding[0],
            operation_id_issuer=_Issuer("operation", 1335),
        )
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        binding[0] = replace(self.binding, workspace_revision=1)
        with self.assertRaises(ChunkError) as drift:
            service.apply(preview, ChunkConflictResolution.KEEP_CURRENT)
        self.assertEqual(drift.exception.code, "CHUNK.PREVIEW_STALE")
        with self.assertRaises(ChunkError) as replay:
            service.apply(preview, ChunkConflictResolution.KEEP_CURRENT)
        self.assertEqual(replay.exception.code, "CHUNK.PREVIEW_STALE")

    def test_preview_apply_rejects_current_head_drift_before_publication(self) -> None:
        incoming, incoming_topology = self._cold_from_common("head-cas", 118)
        self._rename(incoming, incoming_topology, "Incoming head")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        service = self._service(seed=1345)
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        self._rename(self.current, self.current_topology, "Current drift")
        with self.assertRaises(ChunkError) as caught:
            service.apply(preview, ChunkConflictResolution.AUTO)
        self.assertEqual(caught.exception.code, "CHUNK.PREVIEW_STALE")
        self.assertEqual(self.current.current_snapshot().chunks[0].name, "Current drift")

    def test_duplicate_preview_operation_id_is_rejected(self) -> None:
        fixed = issue_chunk_operation_id(b"Z" * 32)
        service = ChunkMetadataConflictService(
            self.current,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=lambda: fixed,
        )
        payload = encode_chunk_metadata_state(self.common)
        service.preview(payload)
        with self.assertRaises(ChunkError) as caught:
            service.preview(payload)
        self.assertEqual(caught.exception.code, "CHUNK.IDENTITY_DUPLICATE")

    def test_universe_mismatch_precedes_semantic_comparison(self) -> None:
        incoming, incoming_topology = self._cold_from_common("universe", 120)
        self._rename(incoming, incoming_topology, "Other universe")
        incoming_state = incoming._metadata_state_for_conflict_owner()
        assert incoming_state is not None
        foreign_binding = replace(
            self.binding,
            segment_universe_digest="0" * 64,
        )
        service = ChunkMetadataConflictService(
            self.current,
            workspace_binding_provider=lambda: foreign_binding,
            operation_id_issuer=_Issuer("operation", 1350),
        )
        preview = service.preview(encode_chunk_metadata_state(incoming_state))
        self.assertEqual(
            preview.classification,
            ChunkConflictClassification.UNIVERSE_MISMATCH,
        )

    def test_current_head_undo_restores_previous_as_new_revision(self) -> None:
        rename = self._rename(self.current, self.current_topology, "Changed")
        before = self.current.current_snapshot()
        service = self._service(seed=1400)
        preview = service.preview_undo(
            rename.operation_id,
            manager=self.manager,
        )
        expected = chunk_plan_binding(before)
        capability = self.current.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.UNDO,
        )
        receipt = service.apply_undo(preview, capability, self.manager)
        after = self.current.current_snapshot()
        self.assertEqual(receipt.action, TopologyAction.UNDO)
        self.assertEqual(after.chunks[0].name, "Original")
        self.assertEqual(after.revision, before.revision + 1)
        with self.assertRaises(ChunkError) as caught:
            service.preview_undo(rename.operation_id, manager=self.manager)
        self.assertEqual(caught.exception.code, "CHUNK.UNDO_UNAVAILABLE")

    def test_cold_reopen_can_undo_the_current_head(self) -> None:
        rename = self._rename(self.current, self.current_topology, "Cold changed")
        before = self.current.current_snapshot()
        cold, _topology = self._authority(self.current_root, 170)
        service = self._service(authority=cold, seed=1450)
        preview = service.preview_undo(
            rename.operation_id,
            manager=self.manager,
        )
        capability = cold.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=chunk_plan_binding(before),
            action=TopologyAction.UNDO,
        )
        receipt = service.apply_undo(preview, capability, self.manager)
        self.assertEqual(receipt.action, TopologyAction.UNDO)
        self.assertEqual(cold.current_snapshot().chunks[0].name, "Original")
        reopened, _reopened_topology = self._authority(self.current_root, 180)
        self.assertEqual(reopened.current_snapshot(), cold.current_snapshot())

    def test_create_head_without_previous_snapshot_is_not_undoable(self) -> None:
        create_receipt = self.current.operation_receipts()[0]
        fresh_root = self.root / "create-only"
        fresh_root.mkdir()
        fresh, fresh_topology = self._authority(fresh_root, 190)
        self._create(fresh, fresh_topology)
        with self.assertRaises(ChunkError) as caught:
            self._service(authority=fresh, seed=1475).preview_undo(
                fresh.operation_receipts()[0].operation_id,
                manager=self.manager,
            )
        self.assertEqual(create_receipt.action, TopologyAction.CREATE)
        self.assertEqual(caught.exception.code, "CHUNK.UNDO_UNAVAILABLE")

    def test_retiring_head_is_not_resurrected_by_undo(self) -> None:
        expected = chunk_plan_binding(self.current.current_snapshot())
        capability = self.current.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=TopologyAction.DISSOLVE_PLAN,
        )
        preview = self.current_topology.preview_dissolve_plan(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )
        receipt = self.current_topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )
        with self.assertRaises(ChunkError) as caught:
            self._service(seed=1500).preview_undo(
                receipt.operation_id,
                manager=self.manager,
            )
        self.assertEqual(caught.exception.code, "CHUNK.UNDO_UNAVAILABLE")

    def test_undo_validates_manager_and_operation_id_issuer(self) -> None:
        rename = self._rename(self.current, self.current_topology, "Changed")
        service = self._service(seed=1600)
        with self.assertRaises(ChunkError) as manager_error:
            service.preview_undo(rename.operation_id, manager=object())
        self.assertEqual(manager_error.exception.code, "CHUNK.ACTOR_UNAVAILABLE")
        invalid = ChunkMetadataConflictService(
            self.current,
            workspace_binding_provider=lambda: self.binding,
            operation_id_issuer=lambda: "not-an-operation-id",
        )
        with self.assertRaises(ChunkError) as operation_error:
            invalid.preview_undo(rename.operation_id, manager=self.manager)
        self.assertEqual(operation_error.exception.code, "CHUNK.CONTRACT_INVALID")


if __name__ == "__main__":
    unittest.main()
