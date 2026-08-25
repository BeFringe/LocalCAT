from __future__ import annotations

import ast
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from collaborative_chunk_contracts import (
    CHUNK_METADATA_NAMESPACE,
    EMPTY_CHUNK_AUDIT_DIGEST,
    ChunkError,
    ChunkOperationReceipt,
    ChunkSegmentRef,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceUniverseProjection,
    LocalReferenceManagerHandle,
    TopologyAction,
    canonicalize_chunk_members,
    chunk_plan_binding,
    issue_chunk_id,
    issue_chunk_operation_id,
    issue_chunk_plan_id,
    segment_universe_digest_v1,
)
from collaborative_chunk_store import (
    ChunkMetadataState,
    CollaborativeChunkStore,
    decode_chunk_metadata_state,
    encode_chunk_metadata_state,
)
from collaborative_chunks import ChunkTopologyPublicationAuthority
from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import issue_project_id


def _document(seed: int) -> str:
    return "doc-" + bytes([seed]).hex() * 32


def _member(project_id: str, document_seed: int, local_id: str) -> ChunkSegmentRef:
    return ChunkSegmentRef(
        project_id,
        SegmentIdentity(_document(document_seed), local_id),
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


class _DurableRuntime:
    def __init__(self, root: Path, project_seed: bytes = b"S") -> None:
        self.project_id = issue_project_id(project_seed * 32)
        self.a = _member(self.project_id, 1, "a")
        self.b = _member(self.project_id, 1, "b")
        self.c = _member(self.project_id, 2, "c")
        self.entries = tuple(
            ChunkUniverseEntry(member, SourcePresence.ATTACHED)
            for member in (self.a, self.b, self.c)
        )
        self.binding = ChunkWorkspaceBinding(
            project_id=self.project_id,
            workspace_session_id="store-session",
            workspace_revision=9,
            segment_universe_digest=segment_universe_digest_v1(
                self.project_id,
                self.entries,
            ),
        )
        self.manager = LocalReferenceManagerHandle("local", "manager")
        self.root = root
        self.chunk_issuer = _Issuer("chunk", 1)
        self.plan_issuer = _Issuer("plan", 100)
        self.operation_issuer = _Issuer("operation", 200)
        self.open()

    def store(self) -> CollaborativeChunkStore:
        return CollaborativeChunkStore(
            self.root,
            "project.chunks.json",
            project_id=self.project_id,
        )

    def universe(self) -> ChunkWorkspaceUniverseProjection:
        return ChunkWorkspaceUniverseProjection(self.binding, self.entries)

    def open(self) -> None:
        self.authority = ChunkTopologyPublicationAuthority(
            project_id=self.project_id,
            workspace_binding_provider=lambda: self.binding,
            metadata_store=self.store(),
        )
        self.topology = self.authority.create_topology_service(
            workspace_universe_provider=self.universe,
            chunk_id_issuer=self.chunk_issuer,
            plan_id_issuer=self.plan_issuer,
            operation_id_issuer=self.operation_issuer,
        )

    def expected(self):
        snapshot = self.authority.current_snapshot()
        return None if snapshot is None else chunk_plan_binding(snapshot)

    def capability(self, action: TopologyAction):
        expected = self.expected()
        capability = self.authority.issue_manager_capability(
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            action=action,
        )
        return capability, expected

    def apply(self, preview, capability, expected) -> ChunkOperationReceipt:
        return self.topology.apply_topology(
            preview,
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )

    def create(self, name: str, members: tuple[ChunkSegmentRef, ...]) -> str:
        capability, expected = self.capability(TopologyAction.CREATE)
        preview = self.topology.preview_create(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            name=name,
            members=canonicalize_chunk_members(members),
        )
        self.apply(preview, capability, expected)
        return preview.created_chunk_ids[0]

    def rename(self, chunk_id: str, name: str):
        capability, expected = self.capability(TopologyAction.RENAME)
        preview = self.topology.preview_rename(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
            chunk_id=chunk_id,
            name=name,
        )
        return self.apply(preview, capability, expected)

    def dissolve(self):
        capability, expected = self.capability(TopologyAction.DISSOLVE_PLAN)
        preview = self.topology.preview_dissolve_plan(
            capability,
            self.manager,
            workspace_binding=self.binding,
            expected_plan_binding=expected,
        )
        return self.apply(preview, capability, expected)


class CollaborativeChunkStoreTests(unittest.TestCase):
    def assert_error(self, code: str, callback) -> ChunkError:
        with self.assertRaises(ChunkError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_canonical_codec_is_exact_and_c1_assignment_stays_null(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            runtime.create("第一组", (runtime.a, runtime.b))
            state = runtime.store().load()
            self.assertIsInstance(state, ChunkMetadataState)
            assert state is not None
            payload = encode_chunk_metadata_state(state)
            self.assertEqual(decode_chunk_metadata_state(payload), state)
            self.assertNotIn(b"\n", payload)
            self.assertIn('"assignee":null'.encode(), payload)
            self.assertNotIn(str(runtime.root).encode(), payload)
            self.assertEqual(payload, json.dumps(
                json.loads(payload),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode())
            active = state.active_snapshot
            assert active is not None
            self.assertTrue(all(chunk.assignee is None for chunk in active.chunks))
            self.assertTrue(all(
                record.receipt.assignment_count == 0
                for record in state.audit_records
            ))

    def test_strict_decoder_rejects_noncanonical_duplicate_extra_and_assignment(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            runtime.create("A", (runtime.a,))
            state = runtime.store().load()
            assert state is not None
            payload = encode_chunk_metadata_state(state)
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(payload + b"\n"),
            )
            duplicate = payload.replace(
                b'{"active_metadata":',
                b'{"schema":"localcat-collaborative-chunk-store-v1","active_metadata":',
                1,
            )
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(duplicate),
            )
            value = json.loads(payload)
            value["extra"] = 1
            extra = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(extra),
            )
            value.pop("extra")
            value["active_metadata"]["chunks"][0]["assignee"] = {
                "authority_id": "x",
                "subject_id": "y",
            }
            assigned = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(assigned),
            )

    def test_strict_decoder_rejects_invalid_utf8_bool_depth_and_audit_tamper(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            runtime.create("A", (runtime.a,))
            state = runtime.store().load()
            assert state is not None
            payload = encode_chunk_metadata_state(state)
            for invalid, code in (
                (b"\xef\xbb\xbf" + payload, "CHUNK.METADATA_INVALID"),
                (b"\xff", "CHUNK.METADATA_INVALID"),
                (b"[" * 40 + b"]" * 40, "CHUNK.LIMIT_EXCEEDED"),
            ):
                self.assert_error(
                    code,
                    lambda invalid=invalid: decode_chunk_metadata_state(invalid),
                )
            value = json.loads(payload)
            value["active_metadata"]["revision"] = True
            bool_revision = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(bool_revision),
            )
            value = json.loads(payload)
            value["lifecycle"]["audit_records"][0]["outcome"] = "failed"
            tampered_audit = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: decode_chunk_metadata_state(tampered_audit),
            )

    def test_oversized_payload_is_rejected_before_json_materialization(self) -> None:
        self.assert_error(
            "CHUNK.LIMIT_EXCEEDED",
            lambda: decode_chunk_metadata_state(b" " * (32 * 1024 * 1024 + 1)),
        )

    def test_durable_publish_cold_reopen_dissolve_and_new_generation(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            first_id = runtime.create("A", (runtime.a, runtime.b))
            first_snapshot = runtime.authority.current_snapshot()
            first_receipts = runtime.authority.operation_receipts()
            first_digest = runtime.authority.metadata_digest()

            runtime.open()
            self.assertEqual(runtime.authority.current_snapshot(), first_snapshot)
            self.assertEqual(runtime.authority.operation_receipts(), first_receipts)
            self.assertEqual(runtime.authority.metadata_digest(), first_digest)
            runtime.rename(first_id, "B")
            runtime.dissolve()
            self.assertIsNone(runtime.authority.current_snapshot())
            dissolved_state = runtime.store().load()
            assert dissolved_state is not None
            self.assertIsNone(dissolved_state.active_snapshot)
            self.assertIn(first_id, dissolved_state.retired_chunk_ids)
            old_plan = first_snapshot.chunk_plan_id
            self.assertIn(old_plan, dissolved_state.used_chunk_plan_ids)

            runtime.open()
            second_id = runtime.create("C", (runtime.c,))
            second_state = runtime.store().load()
            assert second_state is not None and second_state.active_snapshot is not None
            self.assertNotEqual(second_state.active_snapshot.chunk_plan_id, old_plan)
            self.assertIn(second_id, {
                chunk.chunk_id for chunk in second_state.active_snapshot.chunks
            })
            self.assertEqual(
                second_state.audit_records[-1].previous_audit_head_digest,
                EMPTY_CHUNK_AUDIT_DIGEST,
            )
            self.assertEqual(second_state.audit_records[-1].receipt.base_revision, 0)

    def test_pre_replace_failure_rolls_back_first_publish(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            real_replace = os.replace

            def fail_target_replace(src, dst, *args, **kwargs):
                if dst == "project.chunks.json":
                    raise OSError("replace failed")
                return real_replace(src, dst, *args, **kwargs)

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.create("A", (runtime.a,)),
                )
            report = runtime.store().recover()
            self.assertEqual(report.outcome, "rolled_back")
            self.assertIsNone(report.state)
            self.assertIsNone(runtime.store().load())

    def test_after_replace_uncertainty_rolls_forward_once(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.create("A", (runtime.a,)),
                )
            report = runtime.store().recover()
            self.assertEqual(report.outcome, "rolled_forward")
            assert report.state is not None
            self.assertEqual(len(report.state.audit_records), 1)
            runtime.open()
            self.assertEqual(len(runtime.authority.operation_receipts()), 1)

    def test_overwrite_uncertainty_can_restore_exact_lkg(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            chunk_id = runtime.create("A", (runtime.a,))
            old_payload = (runtime.root / "project.chunks.json").read_bytes()
            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.rename(chunk_id, "B"),
                )
            (runtime.root / "project.chunks.json").write_bytes(b"tampered")
            report = runtime.store().recover()
            self.assertEqual(report.outcome, "rolled_back")
            self.assertEqual(
                (runtime.root / "project.chunks.json").read_bytes(),
                old_payload,
            )
            runtime.open()
            snapshot = runtime.authority.current_snapshot()
            assert snapshot is not None
            self.assertEqual(snapshot.chunks[0].name, "A")
            self.assertEqual(len(runtime.authority.operation_receipts()), 1)

    def test_cleanup_interruption_after_verified_replace_rolls_forward_without_lkg(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            chunk_id = runtime.create("A", (runtime.a,))
            original_unlink = CollaborativeChunkStore._unlink

            def fail_journal_cleanup(parent, name, *, missing_ok=True):
                if name.endswith(".journal-v1"):
                    raise OSError("cleanup interrupted")
                return original_unlink(parent, name, missing_ok=missing_ok)

            with patch.object(
                CollaborativeChunkStore,
                "_unlink",
                side_effect=fail_journal_cleanup,
            ):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.rename(chunk_id, "B"),
                )
            report = runtime.store().recover()
            self.assertEqual(report.outcome, "rolled_forward")
            assert report.state is not None and report.state.active_snapshot is not None
            self.assertEqual(report.state.active_snapshot.chunks[0].name, "B")
            self.assertEqual(len(report.state.audit_records), 2)

    def test_compare_and_swap_stale_writer_does_not_replace_current(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            runtime.create("A", (runtime.a,))
            stale_store = runtime.store()
            stale_state, stale_digest = stale_store.load_with_digest()
            assert stale_state is not None and stale_digest is not None
            chunk_id = stale_state.active_snapshot.chunks[0].chunk_id
            runtime.rename(chunk_id, "B")
            current_bytes = (runtime.root / "project.chunks.json").read_bytes()
            self.assert_error(
                "CHUNK.DESTINATION_STALE",
                lambda: stale_store.publish(
                    stale_state,
                    expected_metadata_digest=stale_digest,
                ),
            )
            self.assertEqual(
                (runtime.root / "project.chunks.json").read_bytes(),
                current_bytes,
            )

    def test_store_rejects_history_rewrite_and_multi_record_jump(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            current_root = base / "current"
            rewritten_root = base / "rewritten"
            jumped_root = base / "jumped"
            current_root.mkdir()
            rewritten_root.mkdir()
            jumped_root.mkdir()

            current = _DurableRuntime(current_root)
            current.create("ORIGINAL", (current.a,))
            current_state, current_digest = current.store().load_with_digest()
            assert current_state is not None and current_digest is not None
            current_bytes = (current_root / "project.chunks.json").read_bytes()

            rewritten = _DurableRuntime(rewritten_root)
            rewritten.create("REWRITTEN", (rewritten.a,))
            rewritten_state = rewritten.store().load()
            assert rewritten_state is not None
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: current.store().publish(
                    rewritten_state,
                    expected_metadata_digest=current_digest,
                ),
            )

            jumped = _DurableRuntime(jumped_root)
            jumped_id = jumped.create("ORIGINAL", (jumped.a,))
            jumped.rename(jumped_id, "STEP-2")
            jumped.rename(jumped_id, "STEP-3")
            jumped_state = jumped.store().load()
            assert jumped_state is not None
            self.assert_error(
                "CHUNK.METADATA_INVALID",
                lambda: current.store().publish(
                    jumped_state,
                    expected_metadata_digest=current_digest,
                ),
            )
            self.assertEqual(
                (current_root / "project.chunks.json").read_bytes(),
                current_bytes,
            )

    def test_recovery_rolls_back_self_consistent_non_successor_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            own_root = base / "own"
            rewritten_root = base / "rewritten"
            own_root.mkdir()
            rewritten_root.mkdir()
            own = _DurableRuntime(own_root)
            own_id = own.create("ORIGINAL", (own.a,))
            old_payload = (own_root / "project.chunks.json").read_bytes()
            rewritten = _DurableRuntime(rewritten_root)
            rewritten.create("REWRITTEN", (rewritten.a,))
            rewritten_payload = (
                rewritten_root / "project.chunks.json"
            ).read_bytes()

            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: own.rename(own_id, "NEXT"),
                )
            journal_path = own_root / ".project.chunks.json.journal-v1"
            journal = json.loads(journal_path.read_bytes())
            (own_root / "project.chunks.json").write_bytes(rewritten_payload)
            journal["candidate_digest"] = hashlib.sha256(
                b"localcat.chunk.store-envelope.v1\0" + rewritten_payload
            ).hexdigest()
            journal_path.write_bytes(
                json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
            )
            report = own.store().recover()
            self.assertEqual(report.outcome, "rolled_back")
            self.assertEqual(
                (own_root / "project.chunks.json").read_bytes(),
                old_payload,
            )

    def test_first_publish_multi_record_candidate_recovers_to_none(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            pending_root = base / "pending"
            source_root = base / "source"
            pending_root.mkdir()
            source_root.mkdir()
            pending = _DurableRuntime(pending_root)
            source = _DurableRuntime(source_root)
            source_id = source.create("A", (source.a,))
            source.rename(source_id, "B")
            source_payload = (source_root / "project.chunks.json").read_bytes()

            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: pending.create("P", (pending.a,)),
                )
            journal_path = pending_root / ".project.chunks.json.journal-v1"
            journal = json.loads(journal_path.read_bytes())
            (pending_root / "project.chunks.json").write_bytes(source_payload)
            journal["candidate_digest"] = hashlib.sha256(
                b"localcat.chunk.store-envelope.v1\0" + source_payload
            ).hexdigest()
            journal_path.write_bytes(
                json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
            )
            report = pending.store().recover()
            self.assertEqual(report.outcome, "rolled_back")
            self.assertIsNone(report.state)
            self.assertFalse((pending_root / "project.chunks.json").exists())

    def test_journal_expected_and_lkg_digest_pair_is_exact(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.create("A", (runtime.a,)),
                )
            journal_path = runtime.root / ".project.chunks.json.journal-v1"
            journal = json.loads(journal_path.read_bytes())
            journal["lkg_digest"] = "a" * 64
            journal_path.write_bytes(
                json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()
            )
            self.assert_error(
                "CHUNK.RECOVERY_REQUIRED",
                lambda: runtime.store().recover(),
            )
            self.assertTrue(journal_path.exists())

    def test_destination_stale_seals_old_authority_until_cold_reopen(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            chunk_id = runtime.create("A", (runtime.a,))
            authority_a = runtime.authority
            topology_a = runtime.topology
            runtime.open()
            authority_b = runtime.authority
            topology_b = runtime.topology
            expected = chunk_plan_binding(authority_a.current_snapshot())
            capability_a = authority_a.issue_manager_capability(
                runtime.manager,
                workspace_binding=runtime.binding,
                expected_plan_binding=expected,
                action=TopologyAction.RENAME,
            )
            capability_b = authority_b.issue_manager_capability(
                runtime.manager,
                workspace_binding=runtime.binding,
                expected_plan_binding=expected,
                action=TopologyAction.RENAME,
            )
            preview_a = topology_a.preview_rename(
                capability_a,
                runtime.manager,
                workspace_binding=runtime.binding,
                expected_plan_binding=expected,
                chunk_id=chunk_id,
                name="A1",
            )
            preview_b = topology_b.preview_rename(
                capability_b,
                runtime.manager,
                workspace_binding=runtime.binding,
                expected_plan_binding=expected,
                chunk_id=chunk_id,
                name="B1",
            )
            topology_a.apply_topology(
                preview_a,
                capability_a,
                runtime.manager,
                workspace_binding=runtime.binding,
                expected_plan_binding=expected,
            )
            self.assert_error(
                "CHUNK.DESTINATION_STALE",
                lambda: topology_b.apply_topology(
                    preview_b,
                    capability_b,
                    runtime.manager,
                    workspace_binding=runtime.binding,
                    expected_plan_binding=expected,
                ),
            )
            self.assert_error(
                "CHUNK.RECOVERY_REQUIRED",
                lambda: authority_b.current_snapshot(),
            )
            cold = ChunkTopologyPublicationAuthority(
                project_id=runtime.project_id,
                workspace_binding_provider=lambda: runtime.binding,
                metadata_store=runtime.store(),
            )
            self.assertEqual(cold.current_snapshot().chunks[0].name, "A1")

    def test_tampered_journal_is_retained_and_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            real_replace = os.replace
            raised = False

            def fail_after_target_replace(src, dst, *args, **kwargs):
                nonlocal raised
                result = real_replace(src, dst, *args, **kwargs)
                if dst == "project.chunks.json" and not raised:
                    raised = True
                    raise OSError("interrupted after replace")
                return result

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_after_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.create("A", (runtime.a,)),
                )
            journal = runtime.root / ".project.chunks.json.journal-v1"
            journal.write_bytes(b'{"schema":"tampered"}')
            self.assert_error(
                "CHUNK.RECOVERY_REQUIRED",
                lambda: runtime.store().recover(),
            )
            self.assertTrue(journal.exists())

    def test_recovery_rejects_foreign_canonical_target_and_lkg(self) -> None:
        def store_digest(payload: bytes) -> str:
            return hashlib.sha256(
                b"localcat.chunk.store-envelope.v1\0" + payload
            ).hexdigest()

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            foreign_root = base / "foreign"
            foreign_root.mkdir()
            foreign = _DurableRuntime(foreign_root, b"F")
            foreign.create("F", (foreign.a,))
            foreign_payload = (foreign_root / "project.chunks.json").read_bytes()

            for branch in ("target", "lkg"):
                own_root = base / branch
                own_root.mkdir()
                runtime = _DurableRuntime(own_root)
                chunk_id = runtime.create("A", (runtime.a,))
                real_replace = os.replace
                raised = False

                def fail_after_target_replace(src, dst, *args, **kwargs):
                    nonlocal raised
                    result = real_replace(src, dst, *args, **kwargs)
                    if dst == "project.chunks.json" and not raised:
                        raised = True
                        raise OSError("interrupted after replace")
                    return result

                with patch(
                    "collaborative_chunk_store.os.replace",
                    side_effect=fail_after_target_replace,
                ):
                    self.assert_error(
                        "CHUNK.RECOVERY_REQUIRED",
                        lambda: runtime.rename(chunk_id, "B"),
                    )
                journal_path = own_root / ".project.chunks.json.journal-v1"
                journal = json.loads(journal_path.read_bytes())
                if branch == "target":
                    (own_root / "project.chunks.json").write_bytes(foreign_payload)
                    journal["expected_digest"] = store_digest(foreign_payload)
                else:
                    (own_root / "project.chunks.json").write_bytes(b"unknown")
                    (own_root / ".project.chunks.json.lkg-v1").write_bytes(
                        foreign_payload
                    )
                    journal["lkg_digest"] = store_digest(foreign_payload)
                journal_path.write_bytes(
                    json.dumps(
                        journal,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                )
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda runtime=runtime: runtime.store().recover(),
                )
                self.assertTrue(journal_path.exists())

    def test_public_io_failures_are_body_safe_and_recovery_is_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            with patch(
                "collaborative_chunk_store.os.stat",
                side_effect=PermissionError("SENSITIVE /private/path"),
            ):
                error = self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.store().load(),
                )
            self.assertTrue(error.retryable)
            self.assertNotIn("SENSITIVE", str(error))

    def test_recovery_replace_failure_is_stable_and_preserves_journal(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _DurableRuntime(Path(directory).resolve())
            chunk_id = runtime.create("A", (runtime.a,))
            real_replace = os.replace

            def fail_target_replace(src, dst, *args, **kwargs):
                if dst == "project.chunks.json":
                    raise OSError("prepare interruption")
                return real_replace(src, dst, *args, **kwargs)

            with patch("collaborative_chunk_store.os.replace", side_effect=fail_target_replace):
                self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.rename(chunk_id, "B"),
                )
            (runtime.root / "project.chunks.json").write_bytes(b"unknown")
            journal = runtime.root / ".project.chunks.json.journal-v1"
            with patch(
                "collaborative_chunk_store.os.replace",
                side_effect=PermissionError("SENSITIVE /private/path"),
            ):
                error = self.assert_error(
                    "CHUNK.RECOVERY_REQUIRED",
                    lambda: runtime.store().recover(),
                )
            self.assertTrue(error.retryable)
            self.assertNotIn("SENSITIVE", str(error))
            self.assertTrue(journal.exists())

    def test_store_module_dependency_and_public_contract_boundaries_are_closed(self) -> None:
        source = Path(__file__).parents[1].joinpath(
            "collaborative_chunk_store.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
        self.assertTrue(
            imported.issubset(
                {
                    "__future__",
                    "contextlib",
                    "dataclasses",
                    "fcntl",
                    "hashlib",
                    "json",
                    "os",
                    "pathlib",
                    "secrets",
                    "stat",
                    "threading",
                    "typing",
                    "unicodedata",
                    "collaborative_chunk_contracts",
                }
            ),
            imported,
        )
        forbidden = {"path", "source", "target", "speaker", "confirmed", "payload", "carrier", "tmx"}
        for contract in (ChunkMetadataState,):
            self.assertFalse(
                {field.name.casefold() for field in fields(contract)} & forbidden
            )


if __name__ == "__main__":
    unittest.main()
