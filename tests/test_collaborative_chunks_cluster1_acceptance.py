"""C1 cumulative acceptance on a formally exported, cold-opened ProjectPackage."""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collaborative_chunk_contracts import (
    AssigneeRef,
    ChunkAuditRecord,
    ChunkError,
    ChunkSegmentRef,
    ChunkSplitChild,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    CollaborativeChunk,
    DISSOLVED_CHUNK_PLAN_DIGEST,
    EMPTY_CHUNK_AUDIT_DIGEST,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_plan_binding,
    chunk_segment_ref_from_ids,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
    validate_c1_operation_receipt,
    validate_c1_snapshot,
)
from collaborative_chunk_store import (
    ChunkMetadataState,
    CollaborativeChunkStore,
    decode_chunk_metadata_state,
    encode_chunk_metadata_state,
)
from collaborative_chunks import (
    ChunkScopeProjectionService,
    ChunkTopologyPublicationAuthority,
)
from project_package import ProjectPackageService
from project_save import ProjectSaveService
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import SourcePresence
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)


_SOURCE_SENTINELS = (
    "SOURCE-ALPHA-PRIVATE",
    "SOURCE-BETA-PRIVATE",
    "TARGET-PRIVATE",
    "SPEAKER-PRIVATE",
)


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


def _write_document(root: Path, name: str, prefix: str) -> Path:
    path = root / "sources" / name
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
                        "source": f"SOURCE-{prefix}-PRIVATE shared",
                        "target": "TARGET-PRIVATE" if prefix == "ALPHA" else "",
                        "speaker": "SPEAKER-PRIVATE",
                        "confirmed": prefix == "ALPHA",
                    },
                    {
                        "id": f"{prefix.casefold()}-1",
                        "source": f"SOURCE-{prefix}-PRIVATE one",
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    },
                    {
                        "id": f"{prefix.casefold()}-2",
                        "source": f"SOURCE-{prefix}-PRIVATE two",
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class _RealPackageHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        first = _write_document(root, "alpha.json", "ALPHA")
        second = _write_document(root, "beta.json", "BETA")
        staged = stage_selected_project_documents(
            root,
            (first, second),
            SelectedProjectDocumentsRequest(
                name="Chunk acceptance",
                source_locale="en",
                target_locale="zh-CN",
            ),
        )
        workspace_service = ProjectWorkspaceService(
            staged.workspace,
            staged.origin_binding,
            session_id="package-export",
            revision=4,
        )
        self.package_path = root / "project.localcat-project"
        exported = ProjectPackageService().export_workspace(
            ProjectSaveService(workspace_service, baseline=None),
            self.package_path,
        )
        self.package_bytes = self.package_path.read_bytes()
        self.package_digest = hashlib.sha256(self.package_bytes).hexdigest()
        assert exported.receipt.artifact_digest == self.package_digest
        self.opened = ProjectPackageService().open(self.package_path)
        self.workspace_service = self.opened.create_workspace_service(
            session_id="chunk-cold-open",
            revision=0,
        )
        self.save_service = self.opened.create_save_service(
            session_id="chunk-dirty-observer",
            revision=0,
        )
        self.workspace = self.workspace_service.workspace
        self.workspace_content_digest = self.workspace_service.workspace_content_digest
        self.entries = tuple(
            ChunkUniverseEntry(
                chunk_segment_ref_from_ids(
                    self.workspace.project_id,
                    document.document_id,
                    source.local_segment_id,
                ),
                source.source_presence,
            )
            for document in self.workspace.documents
            for source in document.source_segments
        )
        self.binding = ChunkWorkspaceBinding(
            project_id=self.workspace.project_id,
            workspace_session_id=self.workspace_service.session_id,
            workspace_revision=self.workspace_service.revision,
            segment_universe_digest=segment_universe_digest_v1(
                self.workspace.project_id,
                self.entries,
            ),
        )
        self.members = {
            (entry.segment.identity.document_id, entry.segment.identity.local_segment_id):
            entry.segment
            for entry in self.entries
        }
        self.metadata_root = root / "chunk-metadata"
        self.metadata_root.mkdir()
        self.manager = LocalReferenceManagerHandle("local", "acceptance-manager")
        self.chunk_issuer = _Issuer("chunk", 1)
        self.plan_issuer = _Issuer("plan", 100)
        self.operation_issuer = _Issuer("operation", 200)
        self.dirty_baseline = self.dirty_state()

    def dirty_state(self):
        save = self.save_service
        workspace = save.workspace_service
        return (
            save.dirty_document_ids,
            save.manifest_dirty,
            save.project_dirty,
            save.saved_workspace_snapshot,
            save.saved_package_digest,
            save.saved_workspace_revision,
            workspace.workspace,
            workspace.workspace_digest,
            workspace.workspace_content_digest,
            workspace.revision,
        )

    def store(self, filename: str = "chunks.json") -> CollaborativeChunkStore:
        return CollaborativeChunkStore(
            self.metadata_root,
            filename,
            project_id=self.workspace.project_id,
        )

    def universe(self) -> ChunkWorkspaceUniverseProjection:
        return ChunkWorkspaceUniverseProjection(self.binding, self.entries)

    def open_chunks(self, filename: str = "chunks.json"):
        authority = ChunkTopologyPublicationAuthority(
            project_id=self.workspace.project_id,
            workspace_binding_provider=lambda: self.binding,
            metadata_store=self.store(filename),
        )
        topology = authority.create_topology_service(
            workspace_universe_provider=self.universe,
            chunk_id_issuer=self.chunk_issuer,
            plan_id_issuer=self.plan_issuer,
            operation_id_issuer=self.operation_issuer,
        )
        return authority, topology

    def assert_package_and_workspace_unchanged(self, testcase: unittest.TestCase) -> None:
        testcase.assertEqual(self.package_path.read_bytes(), self.package_bytes)
        testcase.assertEqual(
            hashlib.sha256(self.package_path.read_bytes()).hexdigest(),
            self.package_digest,
        )
        reopened = ProjectPackageService().open(self.package_path)
        testcase.assertEqual(reopened.validation.artifact_digest, self.package_digest)
        testcase.assertEqual(reopened.workspace, self.workspace)
        testcase.assertEqual(
            self.workspace_service.workspace_content_digest,
            self.workspace_content_digest,
        )
        testcase.assertEqual(self.workspace_service.revision, 0)
        testcase.assertEqual(self.dirty_state(), self.dirty_baseline)

    def relocate_package(self, testcase: unittest.TestCase, filename: str) -> None:
        previous_workspace = self.workspace
        previous_entries = self.entries
        previous_binding = self.binding
        moved = self.root / filename
        self.package_path.replace(moved)
        self.package_path = moved
        reopened = ProjectPackageService().open(self.package_path)
        testcase.assertEqual(reopened.validation.artifact_digest, self.package_digest)
        testcase.assertEqual(reopened.workspace, previous_workspace)
        self.opened = reopened
        self.workspace_service = reopened.create_workspace_service(
            session_id="chunk-cold-open",
            revision=0,
        )
        self.save_service = reopened.create_save_service(
            session_id="chunk-dirty-observer",
            revision=0,
        )
        self.workspace = self.workspace_service.workspace
        self.entries = tuple(
            ChunkUniverseEntry(
                chunk_segment_ref_from_ids(
                    self.workspace.project_id,
                    document.document_id,
                    source.local_segment_id,
                ),
                source.source_presence,
            )
            for document in self.workspace.documents
            for source in document.source_segments
        )
        self.binding = ChunkWorkspaceBinding(
            project_id=self.workspace.project_id,
            workspace_session_id=self.workspace_service.session_id,
            workspace_revision=self.workspace_service.revision,
            segment_universe_digest=segment_universe_digest_v1(
                self.workspace.project_id,
                self.entries,
            ),
        )
        self.members = {
            (entry.segment.identity.document_id, entry.segment.identity.local_segment_id):
            entry.segment
            for entry in self.entries
        }
        testcase.assertEqual(self.entries, previous_entries)
        testcase.assertEqual(self.binding, previous_binding)
        self.assert_package_and_workspace_unchanged(testcase)


class CollaborativeChunkCluster1AcceptanceTests(unittest.TestCase):
    def assert_error(self, code: str, callback) -> ChunkError:
        with self.assertRaises(ChunkError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    @staticmethod
    def _unchecked_clone(value, **changes):
        candidate = object.__new__(type(value))
        for field in fields(value):
            object.__setattr__(
                candidate,
                field.name,
                changes.get(field.name, getattr(value, field.name)),
            )
        return candidate

    @staticmethod
    def _expected(authority: ChunkTopologyPublicationAuthority):
        snapshot = authority.current_snapshot()
        return None if snapshot is None else chunk_plan_binding(snapshot)

    def _capability(self, harness, authority, action):
        expected = self._expected(authority)
        capability = authority.issue_manager_capability(
            harness.manager,
            workspace_binding=harness.binding,
            expected_plan_binding=expected,
            action=action,
        )
        return capability, expected

    def _apply(
        self,
        harness,
        authority,
        topology,
        preview,
        capability,
        expected,
        *,
        metadata_filename="chunks.json",
    ):
        self.assertEqual(preview.assignment_count, 0)
        receipt = topology.apply_topology(
            preview,
            capability,
            harness.manager,
            workspace_binding=harness.binding,
            expected_plan_binding=expected,
        )
        self.assertEqual(receipt.assignment_count, 0)
        snapshot = authority.current_snapshot()
        if snapshot is not None:
            self.assertTrue(all(chunk.assignee is None for chunk in snapshot.chunks))
        state = harness.store(metadata_filename).load()
        assert state is not None
        self.assertTrue(
            all(record.receipt.assignment_count == 0 for record in state.audit_records)
        )
        if state.active_snapshot is not None:
            self.assertTrue(
                all(chunk.assignee is None for chunk in state.active_snapshot.chunks)
            )
        harness.assert_package_and_workspace_unchanged(self)
        return receipt

    def test_real_package_topology_persists_exact_composite_membership(self) -> None:
        with TemporaryDirectory() as directory:
            harness = _RealPackageHarness(Path(directory).resolve())
            self.assertEqual(len(harness.workspace.documents), 2)
            shared = tuple(
                entry.segment
                for entry in harness.entries
                if entry.segment.identity.local_segment_id == "shared"
            )
            self.assertEqual(len(shared), 2)
            self.assertNotEqual(
                shared[0].identity.document_id,
                shared[1].identity.document_id,
            )
            authority, topology = harness.open_chunks()

            by_local = {
                member.identity.local_segment_id: member
                for member in (entry.segment for entry in harness.entries)
                if member.identity.local_segment_id != "shared"
            }

            def create(name: str, members: tuple[ChunkSegmentRef, ...]) -> str:
                capability, expected = self._capability(
                    harness, authority, TopologyAction.CREATE
                )
                preview = topology.preview_create(
                    capability,
                    harness.manager,
                    workspace_binding=harness.binding,
                    expected_plan_binding=expected,
                    name=name,
                    members=canonicalize_chunk_members(members),
                )
                self._apply(
                    harness, authority, topology, preview, capability, expected
                )
                return preview.created_chunk_ids[0]

            continuous_id = create(
                "continuous",
                (shared[0], by_local["alpha-1"]),
            )
            discrete_id = create(
                "discrete",
                (by_local["alpha-2"], by_local["beta-2"]),
            )
            cross_id = create(
                "cross-document",
                (shared[1], by_local["beta-1"]),
            )
            before_identity = {
                chunk.chunk_id: chunk.members
                for chunk in authority.current_snapshot().chunks
            }

            capability, expected = self._capability(
                harness, authority, TopologyAction.RENAME
            )
            preview = topology.preview_rename(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                chunk_id=continuous_id,
                name="continuous-renamed",
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            capability, expected = self._capability(
                harness, authority, TopologyAction.REORDER
            )
            preview = topology.preview_reorder(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                ordered_chunk_ids=(cross_id, continuous_id, discrete_id),
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            self.assertEqual(
                {
                    chunk.chunk_id: chunk.members
                    for chunk in authority.current_snapshot().chunks
                },
                before_identity,
            )

            authority, topology = harness.open_chunks()
            self.assertEqual(
                {
                    chunk.chunk_id: chunk.members
                    for chunk in authority.current_snapshot().chunks
                },
                before_identity,
            )
            before_package_move = authority.current_snapshot()
            harness.relocate_package(self, "relocated.localcat-project")
            authority, topology = harness.open_chunks()
            self.assertEqual(authority.current_snapshot(), before_package_move)

            capability, expected = self._capability(
                harness, authority, TopologyAction.SPLIT
            )
            preview = topology.preview_split(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                source_chunk_id=continuous_id,
                children=(
                    ChunkSplitChild("continuous-a", (shared[0],)),
                    ChunkSplitChild("continuous-b", (by_local["alpha-1"],)),
                ),
            )
            split_ids = preview.created_chunk_ids
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            split_chunks = {
                chunk.chunk_id: chunk for chunk in authority.current_snapshot().chunks
            }
            self.assertTrue(all(split_chunks[value].assignee is None for value in split_ids))

            capability, expected = self._capability(
                harness, authority, TopologyAction.MERGE
            )
            preview = topology.preview_merge(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                source_chunk_ids=(split_ids[1], cross_id),
                result_name="merged-cross-document",
            )
            merged_id = preview.created_chunk_ids[0]
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            merged = next(
                chunk
                for chunk in authority.current_snapshot().chunks
                if chunk.chunk_id == merged_id
            )
            self.assertIsNone(merged.assignee)
            self.assertEqual(
                {member.identity.document_id for member in merged.members},
                {document.document_id for document in harness.workspace.documents},
            )

            capability, expected = self._capability(
                harness, authority, TopologyAction.MOVE
            )
            preview = topology.preview_move(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                source_chunk_id=discrete_id,
                destination_chunk_id=merged_id,
                members=(by_local["alpha-2"],),
                retire_source_if_empty=False,
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            final = authority.current_snapshot()
            assert final is not None
            self.assertEqual(
                {
                    (
                        member.project_id,
                        member.identity.document_id,
                        member.identity.local_segment_id,
                    )
                    for chunk in final.chunks
                    for member in chunk.members
                },
                {
                    (
                        entry.segment.project_id,
                        entry.segment.identity.document_id,
                        entry.segment.identity.local_segment_id,
                    )
                    for entry in harness.entries
                },
            )
            scope_service = ChunkScopeProjectionService(
                authority.current_snapshot,
                retired_chunk_ids_provider=authority.retired_chunk_ids,
            )
            projection = scope_service.issue_scope_projection(
                merged_id,
                chunk_plan_binding(final),
            )
            self.assertEqual(
                projection,
                scope_service.revalidate_scope_projection(projection),
            )
            self.assertGreaterEqual(
                len({member.identity.document_id for member in projection.members}),
                2,
            )

            capability, expected = self._capability(
                harness, authority, TopologyAction.RELEASE
            )
            preview = topology.preview_release(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                source_chunk_id=merged_id,
                members=(by_local["alpha-2"],),
                retire_source_if_empty=False,
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            self.assertIn(
                by_local["alpha-2"],
                topology.unallocated_members(
                    workspace_binding=harness.binding,
                    expected_plan_binding=self._expected(authority),
                ),
            )

            capability, expected = self._capability(
                harness, authority, TopologyAction.DISSOLVE_CHUNK
            )
            preview = topology.preview_dissolve_chunk(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                chunk_id=discrete_id,
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            self.assertIn(discrete_id, authority.retired_chunk_ids())
            self.assertIn(
                by_local["beta-2"],
                topology.unallocated_members(
                    workspace_binding=harness.binding,
                    expected_plan_binding=self._expected(authority),
                ),
            )

            active_before_dissolve = authority.current_snapshot()
            assert active_before_dissolve is not None
            old_plan_id = active_before_dissolve.chunk_plan_id
            retired_before_dissolve = set(authority.retired_chunk_ids())
            active_ids_before_dissolve = {
                chunk.chunk_id for chunk in active_before_dissolve.chunks
            }
            audit_count_before_dissolve = len(authority.operation_receipts())
            capability, expected = self._capability(
                harness, authority, TopologyAction.DISSOLVE_PLAN
            )
            preview = topology.preview_dissolve_plan(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
            )
            self.assertEqual(
                preview.after_plan_digest,
                DISSOLVED_CHUNK_PLAN_DIGEST,
            )
            self._apply(harness, authority, topology, preview, capability, expected)
            authority, topology = harness.open_chunks()
            self.assertIsNone(authority.current_snapshot())
            self.assertEqual(
                set(authority.retired_chunk_ids()),
                retired_before_dissolve | active_ids_before_dissolve,
            )
            self.assertIn(old_plan_id, authority.used_chunk_plan_ids())
            self.assertEqual(
                len(authority.operation_receipts()),
                audit_count_before_dissolve + 1,
            )

            capability, expected = self._capability(
                harness, authority, TopologyAction.CREATE
            )
            preview = topology.preview_create(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                name="new-generation",
                members=(by_local["alpha-2"],),
            )
            generation_receipt = self._apply(
                harness, authority, topology, preview, capability, expected
            )
            authority, _ = harness.open_chunks()
            regenerated = authority.current_snapshot()
            assert regenerated is not None
            self.assertNotEqual(regenerated.chunk_plan_id, old_plan_id)
            self.assertEqual(regenerated.revision, 1)
            self.assertEqual(generation_receipt.base_revision, 0)
            self.assertEqual(regenerated.chunks[0].members, (by_local["alpha-2"],))
            self.assertTrue(
                {old_plan_id, regenerated.chunk_plan_id}.issubset(
                    authority.used_chunk_plan_ids()
                )
            )
            regenerated_state = harness.store().load()
            assert regenerated_state is not None
            self.assertEqual(
                regenerated_state.audit_records[-1].previous_audit_head_digest,
                EMPTY_CHUNK_AUDIT_DIGEST,
            )

            metadata_payload = (harness.metadata_root / "chunks.json").read_bytes()
            for sentinel in _SOURCE_SENTINELS + (str(harness.root),):
                self.assertNotIn(sentinel.encode(), metadata_payload)
            for entry in harness.metadata_root.iterdir():
                if entry.name.endswith(".lock-v1"):
                    continue
                self.assertEqual(entry.name, "chunks.json")
            harness.assert_package_and_workspace_unchanged(self)

    def test_assignment_intents_fail_before_durable_or_package_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            harness = _RealPackageHarness(Path(directory).resolve())
            authority, topology = harness.open_chunks()
            member = harness.entries[0].segment
            capability, expected = self._capability(
                harness, authority, TopologyAction.CREATE
            )
            preview = topology.preview_create(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                name="A",
                members=(member,),
            )
            receipt = self._apply(
                harness, authority, topology, preview, capability, expected
            )
            baseline_snapshot = authority.current_snapshot()
            baseline_receipts = authority.operation_receipts()
            baseline_digest = authority.metadata_digest()
            baseline_bytes = (harness.metadata_root / "chunks.json").read_bytes()
            store = harness.store()
            state, store_digest = store.load_with_digest()
            self.assertIsInstance(state, ChunkMetadataState)
            assert state is not None and store_digest is not None
            baseline_artifacts = {
                entry.name: entry.read_bytes()
                for entry in harness.metadata_root.iterdir()
                if not entry.name.endswith(".lock-v1")
            }

            for command in (
                topology.preview_assign,
                topology.preview_reassign,
                topology.preview_unassign,
            ):
                self.assert_error("CHUNK.ASSIGNMENT_UNAVAILABLE", command)

            current = authority.current_snapshot()
            assert current is not None
            assigned_chunk = replace(
                current.chunks[0],
                assignee=AssigneeRef("local", "assignee"),
            )
            assigned_snapshot = replace(current, chunks=(assigned_chunk,))
            self.assert_error(
                "CHUNK.CONTRACT_INVALID",
                lambda: validate_c1_snapshot(assigned_snapshot),
            )
            nonzero = replace(receipt, assignment_count=1)
            self.assert_error(
                "CHUNK.CONTRACT_INVALID",
                lambda: validate_c1_operation_receipt(nonzero),
            )

            payload = encode_chunk_metadata_state(state)
            value = json.loads(payload)
            value["active_metadata"]["chunks"][0]["assignee"] = {
                "authority_id": "local",
                "subject_id": "assignee",
            }
            assigned_payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(assigned_payload),
            )
            value = json.loads(payload)
            value["lifecycle"]["audit_records"][-1]["receipt"][
                "assignment_count"
            ] = 1
            counted_payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            self.assert_error(
                "CHUNK.DIGEST_MISMATCH",
                lambda: decode_chunk_metadata_state(counted_payload),
            )

            assigned_state = self._unchecked_clone(
                state,
                active_snapshot=assigned_snapshot,
            )
            self.assertIsInstance(assigned_state, ChunkMetadataState)
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: store.publish(
                    assigned_state,
                    expected_metadata_digest=store_digest,
                ),
            )
            last_record = state.audit_records[-1]
            self.assertIsInstance(last_record, ChunkAuditRecord)
            counted_record = self._unchecked_clone(
                last_record,
                receipt=nonzero,
            )
            counted_state = self._unchecked_clone(
                state,
                audit_records=state.audit_records[:-1] + (counted_record,),
            )
            self.assert_error(
                "CHUNK.DIGEST_MISMATCH",
                lambda: store.publish(
                    counted_state,
                    expected_metadata_digest=store_digest,
                ),
            )

            self.assertEqual(authority.current_snapshot(), baseline_snapshot)
            self.assertEqual(authority.operation_receipts(), baseline_receipts)
            self.assertEqual(authority.metadata_digest(), baseline_digest)
            self.assertEqual(
                (harness.metadata_root / "chunks.json").read_bytes(),
                baseline_bytes,
            )
            self.assertEqual(store.load(), state)
            self.assertEqual(store.current_digest(), store_digest)
            self.assertEqual(
                {
                    entry.name: entry.read_bytes()
                    for entry in harness.metadata_root.iterdir()
                    if not entry.name.endswith(".lock-v1")
                },
                baseline_artifacts,
            )
            harness.assert_package_and_workspace_unchanged(self)

    def test_real_package_stays_immutable_across_cold_recovery_windows(self) -> None:
        with TemporaryDirectory() as directory:
            harness = _RealPackageHarness(Path(directory).resolve())

            authority, topology = harness.open_chunks("first.json")
            capability, expected = self._capability(
                harness, authority, TopologyAction.CREATE
            )
            preview = topology.preview_create(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                name="first",
                members=(harness.entries[0].segment,),
            )
            real_replace = os.replace

            def fail_before_target(src, dst, *args, **kwargs):
                if dst == "first.json":
                    raise OSError("before target")
                return real_replace(src, dst, *args, **kwargs)

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_before_target):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: topology.apply_topology(
                        preview,
                        capability,
                        harness.manager,
                        workspace_binding=harness.binding,
                        expected_plan_binding=expected,
                    ),
                )
            report = harness.store("first.json").recover()
            self.assertIsNone(report.state)
            self.assertIsNone(harness.store("first.json").load())
            harness.assert_package_and_workspace_unchanged(self)

            authority, topology = harness.open_chunks("forward.json")
            capability, expected = self._capability(
                harness, authority, TopologyAction.CREATE
            )
            preview = topology.preview_create(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                name="forward",
                members=(harness.entries[1].segment,),
            )
            raised = False

            def fail_after_target(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "forward.json" and not raised:
                    raised = True
                    raise OSError("after target")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: topology.apply_topology(
                        preview,
                        capability,
                        harness.manager,
                        workspace_binding=harness.binding,
                        expected_plan_binding=expected,
                    ),
                )
            report = harness.store("forward.json").recover()
            self.assertEqual(report.outcome, "rolled_forward")
            assert report.state is not None
            self.assertEqual(len(report.state.audit_records), 1)
            cold, _ = harness.open_chunks("forward.json")
            self.assertEqual(len(cold.operation_receipts()), 1)
            harness.assert_package_and_workspace_unchanged(self)

            authority, topology = harness.open_chunks("rollback.json")
            capability, expected = self._capability(
                harness, authority, TopologyAction.CREATE
            )
            preview = topology.preview_create(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                name="old",
                members=(harness.entries[2].segment,),
            )
            self._apply(
                harness,
                authority,
                topology,
                preview,
                capability,
                expected,
                metadata_filename="rollback.json",
            )
            old_state = harness.store("rollback.json").load()
            old_bytes = (
                harness.metadata_root / "rollback.json"
            ).read_bytes()
            old_id = authority.current_snapshot().chunks[0].chunk_id
            capability, expected = self._capability(
                harness, authority, TopologyAction.RENAME
            )
            preview = topology.preview_rename(
                capability,
                harness.manager,
                workspace_binding=harness.binding,
                expected_plan_binding=expected,
                chunk_id=old_id,
                name="new",
            )
            raised = False

            def fail_after_rollback_target(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "rollback.json" and not raised:
                    raised = True
                    raise OSError("after target")
                return result

            with patch(
                "collaborative_chunk_store.os.replace",
                side_effect=fail_after_rollback_target,
            ):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: topology.apply_topology(
                        preview,
                        capability,
                        harness.manager,
                        workspace_binding=harness.binding,
                        expected_plan_binding=expected,
                    ),
                )
            (harness.metadata_root / "rollback.json").write_bytes(b"unproven")
            report = harness.store("rollback.json").recover()
            self.assertEqual(report.outcome, "rolled_back")
            self.assertEqual(report.state, old_state)
            self.assertEqual(
                (harness.metadata_root / "rollback.json").read_bytes(),
                old_bytes,
            )
            cold, _ = harness.open_chunks("rollback.json")
            self.assertEqual(cold.current_snapshot().chunks[0].name, "old")
            harness.assert_package_and_workspace_unchanged(self)


if __name__ == "__main__":
    unittest.main()
