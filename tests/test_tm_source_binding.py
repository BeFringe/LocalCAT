from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
    SQLiteTMStore,
    SourceBindingMonitor,
    initialize_stage_schema,
)


def _stage(root: Path, resource_id: str = "tm.primary") -> MutableStageRef:
    configured = (root / f"{resource_id}.jsonl").resolve()
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        configured,
    )
    return MutableStageRef(
        stage_id=f"stage.{resource_id}",
        resource_identity=identity,
        staged_db_path=(root / f".{resource_id}.stage.sqlite3").resolve(),
        manifest_temp_path=(root / f".{resource_id}.manifest.tmp").resolve(),
    )


def _draft(source: str, target: str) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "test"),),
    )


def _completed_binding(
    store: SQLiteTMStore,
    stage: MutableStageRef,
    jsonl_bytes: bytes,
) -> SnapshotBinding:
    revision = store.canonical_revision()
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.{stage.resource_identity.resource_id}",
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(jsonl_bytes).hexdigest(),
        record_count=revision.record_count,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return SnapshotBinding(
        configured_jsonl_path=(
            stage.resource_identity.configured_jsonl_path
        ),
        manifest_path=stage.resource_identity.snapshot_manifest_path,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        manifest=manifest,
    )


def _publish_fixture_pair(binding: SnapshotBinding, jsonl_bytes: bytes) -> None:
    binding.configured_jsonl_path.write_bytes(jsonl_bytes)
    binding.manifest_path.write_text(
        contract_to_json(binding.manifest),
        encoding="utf-8",
    )


def _prepared_store(
    root: Path,
    resource_id: str = "tm.primary",
) -> tuple[MutableStageRef, SQLiteTMStore, SnapshotBinding, bytes, bytes]:
    stage = _stage(root, resource_id)
    initialize_stage_schema(
        stage,
        canonical_store_id=f"store.{resource_id}",
    )
    store = SQLiteTMStore(
        stage,
        canonical_store_id=f"store.{resource_id}",
    )
    _ = store.append(_draft("seed", "seed target"))
    jsonl_bytes = b'{"source":"seed","target":"seed target"}\n'
    binding = _completed_binding(store, stage, jsonl_bytes)
    _publish_fixture_pair(binding, jsonl_bytes)
    manifest_bytes = binding.manifest_path.read_bytes()
    store.register_completed_snapshot_binding(binding)
    return stage, store, binding, jsonl_bytes, manifest_bytes


class SourceBindingMonitorTests(unittest.TestCase):
    def test_completed_binding_is_current_then_append_makes_history_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage, store, binding, jsonl_before, manifest_before = (
                _prepared_store(Path(temporary))
            )

            current = store.source_binding_monitor.observe()
            self.assertEqual(current.state, SourceBindingState.VERIFIED_CURRENT)
            self.assertEqual(
                current.head_revision,
                binding.receipt.exported_revision,
            )
            self.assertEqual(current.generation, 0)

            appended = store.append(_draft("new", "canonical only"))
            history = store.source_binding_monitor.observe()

            self.assertEqual(history.state, SourceBindingState.VERIFIED_HISTORY)
            self.assertEqual(
                history.head_revision,
                binding.receipt.exported_revision + 1,
            )
            self.assertEqual(history.generation, current.generation)
            self.assertEqual(store.exact_records("new"), (appended,))
            self.assertEqual(
                stage.resource_identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )
            self.assertEqual(
                stage.resource_identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            _ = store.append_batch(
                batch_id="import.after-binding",
                kind="import",
                drafts=(_draft("imported", "canonical import"),),
                source_digest="a" * 64,
                source_path=(
                    stage.staged_db_path.parent / "validated-import.jsonl"
                ).resolve(),
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(
                stage.resource_identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )
            self.assertEqual(
                stage.resource_identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )

    def test_completed_registration_is_closed_and_never_publishes_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(stage, canonical_store_id="store.primary")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            jsonl_bytes = b""
            binding = _completed_binding(store, stage, jsonl_bytes)

            with self.assertRaises(FileNotFoundError):
                store.register_completed_snapshot_binding(binding)
            self.assertFalse(binding.configured_jsonl_path.exists())
            self.assertFalse(binding.manifest_path.exists())

            _publish_fixture_pair(binding, jsonl_bytes)
            store.register_completed_snapshot_binding(binding)
            self.assertEqual(store.canonical_revision().generation, 0)
            self.assertEqual(store.canonical_revision().head_revision, 0)
            with self.assertRaisesRegex(ValueError, "already registered"):
                store.register_completed_snapshot_binding(binding)

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                statuses = connection.execute(
                    "SELECT status FROM tm_snapshot_receipt"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(statuses, [("completed",)])

    def test_external_jsonl_or_manifest_change_latches_divergence(self) -> None:
        mutations = (
            "jsonl changed",
            "manifest missing",
            "manifest changed",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    stage, store, binding, jsonl_before, manifest_before = (
                        _prepared_store(Path(temporary))
                    )
                    if mutation == "jsonl changed":
                        binding.configured_jsonl_path.write_bytes(
                            jsonl_before + b"external\n"
                        )
                    elif mutation == "manifest missing":
                        binding.manifest_path.unlink()
                    else:
                        binding.manifest_path.write_bytes(
                            manifest_before + b"\n"
                        )

                    observation = store.source_binding_monitor.observe()
                    self.assertEqual(
                        observation.state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertTrue(observation.diagnostic_codes)

    def test_ledger_identity_digest_and_ancestry_mismatch_diverge(self) -> None:
        cases = (
            (
                "resource identity",
                "UPDATE tm_snapshot_receipt SET resource_id = 'tm.other'",
            ),
            (
                "canonical identity",
                "UPDATE tm_snapshot_receipt "
                "SET canonical_store_id = 'store.other'",
            ),
            (
                "digest",
                "UPDATE tm_snapshot_receipt SET jsonl_digest = 'f' || "
                "substr(jsonl_digest, 2)",
            ),
            (
                "ancestry",
                "UPDATE tm_snapshot_receipt SET exported_revision = 999",
            ),
            (
                "canonical revision ancestry",
                "UPDATE tm_meta SET value = '999' "
                "WHERE key = 'head_revision'",
            ),
            (
                "binding path",
                "UPDATE tm_snapshot_binding "
                "SET configured_jsonl_path = '/other/tm.jsonl'",
            ),
        )
        for name, statement in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    stage, store, _binding, _jsonl, _manifest = (
                        _prepared_store(Path(temporary))
                    )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()

                    observation = store.source_binding_monitor.observe()
                    self.assertEqual(
                        observation.state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )

    def test_diverged_store_remains_canonical_and_append_cannot_clear_latch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage, store, binding, jsonl_before, manifest_before = (
                _prepared_store(Path(temporary))
            )
            binding.configured_jsonl_path.write_bytes(b"external source\n")
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

            appended = store.append(_draft("authority", "canonical"))
            self.assertEqual(store.exact_records("authority"), (appended,))
            self.assertEqual(
                binding.configured_jsonl_path.read_bytes(),
                b"external source\n",
            )
            self.assertEqual(binding.manifest_path.read_bytes(), manifest_before)

            binding.configured_jsonl_path.write_bytes(jsonl_before)
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(store.exact_records("seed")[0].target_raw, "seed target")

            reopened = SQLiteTMStore(
                stage,
                canonical_store_id="store.tm.primary",
            )
            self.assertEqual(
                reopened.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(
                reopened.exact_records("authority")[0].target_raw,
                "canonical",
            )

    def test_divergence_and_revision_are_resource_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, primary, primary_binding, _, _ = _prepared_store(
                root,
                "tm.primary",
            )
            _, secondary, _, _, _ = _prepared_store(root, "tm.secondary")
            primary_binding.configured_jsonl_path.write_bytes(b"changed\n")

            self.assertEqual(
                primary.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(
                secondary.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            _ = secondary.append(_draft("secondary", "only"))
            self.assertEqual(
                secondary.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(
                primary.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_registration_rejects_caller_owned_subtypes_before_sql(self) -> None:
        class SnapshotBindingSubclass(SnapshotBinding):
            pass

        class ScalarSubclass(str):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(stage, canonical_store_id="store.primary")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            binding = _completed_binding(store, stage, b"")
            forged = SnapshotBindingSubclass(
                configured_jsonl_path=binding.configured_jsonl_path,
                manifest_path=binding.manifest_path,
                snapshot_kind=binding.snapshot_kind,
                receipt=binding.receipt,
                manifest=binding.manifest,
                binding_version=binding.binding_version,
            )

            with self.assertRaises(TypeError):
                store.register_completed_snapshot_binding(forged)
            self.assertFalse(binding.configured_jsonl_path.exists())

            forged_receipt = replace(
                binding.receipt,
                canonical_store_id=ScalarSubclass(
                    binding.receipt.canonical_store_id
                ),
            )
            forged_manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=binding.snapshot_kind,
                receipt=forged_receipt,
                receipt_digest=snapshot_receipt_digest(forged_receipt),
            )
            nested_forgery = SnapshotBinding(
                configured_jsonl_path=binding.configured_jsonl_path,
                manifest_path=binding.manifest_path,
                snapshot_kind=binding.snapshot_kind,
                receipt=forged_receipt,
                manifest=forged_manifest,
            )
            with patch(
                "tm_sqlite_store._open_configured_connection",
                side_effect=AssertionError("SQL opened"),
            ) as open_connection:
                with self.assertRaises(TypeError):
                    store.register_completed_snapshot_binding(nested_forgery)
                open_connection.assert_not_called()

    def test_store_exposes_one_bound_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(stage, canonical_store_id="store.primary")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            self.assertIsInstance(
                store.source_binding_monitor,
                SourceBindingMonitor,
            )
            self.assertIs(
                store.source_binding_monitor,
                store.source_binding_monitor,
            )


if __name__ == "__main__":
    unittest.main()
