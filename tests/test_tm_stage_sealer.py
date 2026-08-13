"""Adversarial unit tests for the Task 5.3 immutable stage sealer."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import patch

import tm_contracts as contract_module
import tm_sqlite_store
import tm_stage_sealer
from tm_contracts import (
    SNAPSHOT_MANIFEST_VERSION,
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    GenerationExpectation,
    MutableStageRef,
    SealedStage,
    SnapshotKind,
    SnapshotBinding,
    SnapshotManifest,
    SnapshotReceipt,
    StageValidationEvidence,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
    stage_validation_evidence_digest,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import SQLiteStoreSchemaError, SQLiteTMStore
from tm_stage_sealer import (
    SealedArtifactRegistry,
    StageSealError,
    StageSealer,
)


SOURCE_BYTES = (
    b'{"source":"same","target":"first","speaker":"alice",'
    b'"context_prev":"before","file_source":"chapter.json"}\n'
    b"{bad-json}\n"
    b'{"source":"same","target":"second","context_next":"after"}\n'
    b'{"source":"x","target":"short"}\n'
)


def _identity(root: Path) -> CanonicalResourceIdentity:
    return CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        (root / "tm.primary.jsonl").resolve(),
    )


def _service(identity: CanonicalResourceIdentity) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _sealer() -> StageSealer:
    return StageSealer(
        registry=SealedArtifactRegistry(
            registry_namespace="coordinator.primary"
        ),
        canonical_store_id="store.primary",
    )


def _build_stage(
    root: Path,
    *,
    fts5_available: bool,
) -> tuple[CanonicalResourceIdentity, MutableStageRef]:
    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
    service = _service(identity)
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        build = service.build_mutable_stage(identity.configured_jsonl_path)
    stage = build.mutable_stage
    if stage is None:
        raise AssertionError("expected a fresh mutable stage")
    return identity, stage


def _seal(
    sealer: StageSealer,
    stage: MutableStageRef,
    *,
    fts5_available: bool,
    expected_prior_generation: int | None = None,
) -> SealedStage:
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        return sealer.seal(
            stage,
            expected_prior_generation=expected_prior_generation,
        )


def _registry(sealer: StageSealer) -> SealedArtifactRegistry:
    return cast(SealedArtifactRegistry, sealer.registry)


def _raw_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path))
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _expect_seal_code(
    test: unittest.TestCase,
    function: Callable[[], object],
    error_code: str,
) -> StageSealError:
    with test.assertRaisesRegex(StageSealError, f"^{error_code}$") as raised:
        function()
    return raised.exception


def _assert_clean_unpublished(
    test: unittest.TestCase,
    sealer: StageSealer,
    stage: MutableStageRef,
) -> None:
    registry = _registry(sealer)
    test.assertEqual(len(registry._entries), 0)
    test.assertEqual(len(registry._reservations), 0)
    connection = sqlite3.connect(str(stage.staged_db_path))
    try:
        status_rows = connection.execute(
            "SELECT value FROM tm_meta "
            "WHERE key = 'activation_status'"
        ).fetchall()
    finally:
        connection.close()
    test.assertEqual(status_rows, [("UNPUBLISHED",)])
    test.assertTrue(stage.staged_db_path.is_file())
    test.assertTrue(stage.manifest_temp_path.is_file())


class _PathSubclass(Path):
    pass


class _FakeRegistry:
    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    @property
    def registry_namespace(self) -> Any:
        return self._namespace

    def seal(
        self,
        mutable_stage: object,
        evidence: object,
        generation: object,
    ) -> object:
        raise AssertionError("seal must not be reached")

    def reserve(
        self,
        mutable_stage: object,
        *,
        database_identity: object,
        manifest_identity: object,
    ) -> object:
        raise AssertionError("reserve must not be reached")

    def commit(
        self,
        reservation: object,
        evidence: object,
        generation: object,
    ) -> object:
        raise AssertionError("commit must not be reached")

    def release(self, reservation: object) -> None:
        raise AssertionError("release must not be reached")


class StageSealerHappyPathTests(unittest.TestCase):
    def test_seal_completes_frozen_artifact_in_both_index_modes(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    sealer = _sealer()
                    sealed = _seal(
                        sealer,
                        stage,
                        fts5_available=fts5_available,
                    )

                    self.assertIs(type(sealed), SealedStage)
                    self.assertIsNone(sealed.expected_prior_generation)
                    evidence = sealed.evidence
                    self.assertEqual(evidence.resource_id, "tm.primary")
                    self.assertEqual(evidence.record_count, 3)
                    self.assertEqual(evidence.origin_batch_count, 1)
                    self.assertTrue(evidence.integrity_ok)
                    self.assertTrue(evidence.foreign_keys_ok)
                    self.assertEqual(
                        evidence.source_binding.receipt.jsonl_digest,
                        hashlib.sha256(SOURCE_BYTES).hexdigest(),
                    )
                    self.assertEqual(
                        evidence.source_binding.configured_jsonl_path,
                        identity.configured_jsonl_path,
                    )
                    self.assertEqual(
                        evidence.source_binding.manifest_path,
                        identity.snapshot_manifest_path,
                    )
                    if fts5_available:
                        self.assertEqual(evidence.fts_count, 3)
                        self.assertEqual(
                            evidence.gram_counts,
                            ((1, 9), (2, 6)),
                        )
                    else:
                        self.assertEqual(evidence.fts_count, 0)
                        self.assertEqual(
                            evidence.gram_counts,
                            ((1, 9), (2, 6), (3, 4)),
                        )
                    self.assertEqual(
                        evidence.stage_file_digest,
                        hashlib.sha256(
                            stage.staged_db_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        evidence.manifest_temp_digest,
                        hashlib.sha256(
                            stage.manifest_temp_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(
                        identity.snapshot_manifest_path.exists()
                    )

    def test_expected_prior_generation_any_non_negative_value_closes(
        self,
    ) -> None:
        for expected in (None, 0, 1, 2, 7):
            with self.subTest(expected_prior_generation=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    sealed = _seal(
                        sealer,
                        stage,
                        fts5_available=True,
                        expected_prior_generation=expected,
                    )
                    self.assertEqual(
                        sealed.expected_prior_generation,
                        expected,
                    )
                    self.assertEqual(
                        sealed.generation.expected_prior_generation,
                        expected,
                    )
                    self.assertEqual(
                        sealed.generation.resource_id,
                        sealed.evidence.resource_id,
                    )
                    self.assertEqual(
                        sealed.generation.target_identity,
                        sealed.evidence.target_identity,
                    )
                    self.assertEqual(
                        sealed.generation.canonical_store_id,
                        sealed.evidence.source_binding.receipt
                        .canonical_store_id,
                    )
                    self.assertEqual(
                        sealed.generation.snapshot_receipt_digest,
                        sealed.evidence.snapshot_receipt_digest,
                    )

    def test_evidence_and_seal_digests_are_deterministic_and_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            evidence = sealed.evidence

            self.assertEqual(
                stage_validation_evidence_digest(evidence),
                stage_validation_evidence_digest(evidence),
            )
            contract_module._validate_sealed_stage(sealed)
            artifact = sealed.artifact
            self.assertEqual(
                artifact.seal_digest,
                contract_module._artifact_seal_digest(
                    registry_namespace=artifact.registry_namespace,
                    artifact_id=artifact.artifact_id,
                    mutable_stage=stage,
                    evidence=evidence,
                ),
            )
            self.assertEqual(
                sealed.sealed_stage_digest,
                contract_module._sealed_stage_contract_digest(
                    artifact=artifact,
                    evidence=evidence,
                    generation=sealed.generation,
                    activation_nonce=sealed.activation_nonce,
                ),
            )

    def test_registry_tracks_sealed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertEqual(
                sealer.registry.registry_namespace,
                "coordinator.primary",
            )
            self.assertEqual(sealer.canonical_store_id, "store.primary")
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(
                sealer.registry.state(sealed),
                ActivationCapabilityState.SEALED,
            )


class StageSealerWriteAuthorityTests(unittest.TestCase):
    def test_post_seal_store_authority_revoked_but_raw_file_writable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=True,
            ):
                store = SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))

            draft = TMRecordDraft(
                source_raw="z",
                target_raw="z",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                file_source=None,
                provenance=(("source", "test"),),
            )
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_SEALED$",
            ):
                store.append(draft)
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_SEALED$",
            ):
                store.exact_records("same")

            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA user_version=99")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_PUBLISHED$",
            ):
                SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )


class StageSealerTamperTests(unittest.TestCase):
    def test_record_target_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET target_raw='tampered' "
                "WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )
            self.assertEqual(len(_registry(sealer)._entries), 0)

    def test_record_order_swap_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            rows = connection.execute(
                "SELECT record_id, source_raw, target_raw "
                "FROM tm_record ORDER BY record_id LIMIT 2"
            ).fetchall()
            connection.execute(
                "UPDATE tm_record SET source_raw=?, target_raw=? "
                "WHERE record_id=?",
                (rows[1][1], rows[1][2], rows[0][0]),
            )
            connection.execute(
                "UPDATE tm_record SET source_raw=?, target_raw=? "
                "WHERE record_id=?",
                (rows[0][1], rows[0][2], rows[1][0]),
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )

    def test_legacy_line_number_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET legacy_line_no=99 WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_LINEAGE_INVALID",
            )

    def test_provenance_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET provenance_json="
                "'[[\"source\",\"other\"]]' WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.PROVENANCE_MISMATCH",
            )

    def test_source_jsonl_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            with identity.configured_jsonl_path.open("ab") as stream:
                stream.write(b"{bad-json}\n")
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.SOURCE_DIGEST_MISMATCH",
            )

    def test_origin_batch_digest_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_origin_batch SET source_digest=? "
                "WHERE batch_id LIKE 'migration.%'",
                ("0" * 64,),
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.SOURCE_DIGEST_MISMATCH",
            )

    def test_missing_gram_row_rejected_in_both_index_modes(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    connection = _raw_connection(stage.staged_db_path)
                    connection.execute(
                        "DELETE FROM tm_gram "
                        "WHERE record_id=1 AND gram_size=1"
                    )
                    connection.commit()
                    connection.close()
                    sealer = _sealer()
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=fts5_available,
                        ),
                        "SEALER.CANDIDATE_INDEX_INCOMPLETE",
                    )

    def test_extra_gram_row_rejected_in_both_index_modes(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    connection = _raw_connection(stage.staged_db_path)
                    connection.execute(
                        "INSERT INTO tm_gram("
                        "gram_size, gram, record_id, term_frequency) "
                        "VALUES (1, 'q', 1, 1)"
                    )
                    connection.commit()
                    connection.close()
                    sealer = _sealer()
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=fts5_available,
                        ),
                        "SEALER.CANDIDATE_INDEX_INCOMPLETE",
                    )

    def test_length_tf_and_block_proof_tamper_matrix_is_rejected(self) -> None:
        mutations = (
            (
                "length",
                "UPDATE tm_record SET source_fold_length = "
                "source_fold_length + 1 WHERE record_id = 1",
                "SEALER.FOLD_MISMATCH",
            ),
            (
                "term-frequency",
                "UPDATE tm_gram SET term_frequency = term_frequency + 1 "
                "WHERE record_id = 1 AND gram_size = 1",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "block-count",
                "UPDATE tm_candidate_block SET record_count = record_count + 1 "
                "WHERE block_id = 0",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "missing-maximum",
                "DELETE FROM tm_gram_block_max WHERE rowid IN "
                "(SELECT rowid FROM tm_gram_block_max LIMIT 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "wrong-maximum",
                "UPDATE tm_gram_block_max SET max_term_frequency = "
                "max_term_frequency + 1 WHERE rowid IN "
                "(SELECT rowid FROM tm_gram_block_max LIMIT 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "extra-block",
                "INSERT INTO tm_candidate_block VALUES (99, 25345, 25600, 1, 1, 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
        )
        for name, statement, expected_code in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                _, stage = _build_stage(Path(temporary), fts5_available=True)
                connection = _raw_connection(stage.staged_db_path)
                connection.execute(statement)
                connection.commit()
                connection.close()
                _expect_seal_code(
                    self,
                    lambda: _seal(_sealer(), stage, fts5_available=True),
                    expected_code,
                )

    def test_missing_fts_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute("DELETE FROM tm_fts WHERE record_id=1")
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FTS_INDEX_INCOMPLETE",
            )

    def test_extra_fts_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "INSERT INTO tm_fts(record_id, source_fold_v1) "
                "VALUES (1, 'extra')"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FTS_INDEX_INCOMPLETE",
            )

    def test_coordinated_fold_gram_fts_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET source_fold_v1='x' WHERE record_id=1"
            )
            connection.execute(
                "UPDATE tm_gram SET gram=gram || 'x' WHERE record_id=1"
            )
            connection.execute(
                "UPDATE tm_fts SET source_fold_v1='x' WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FOLD_MISMATCH",
            )

    def test_manifest_replaced_with_foreign_contract_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.migration.000000000000000000000000",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=1,
                jsonl_digest="0" * 64,
                record_count=1,
            )
            stage.manifest_temp_path.write_text(
                contract_to_json(receipt),
                encoding="utf-8",
            )
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.MANIFEST_INVALID",
            )

    def test_manifest_receipt_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            connection = _raw_connection(stage.staged_db_path)
            receipt_row = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version FROM tm_snapshot_receipt"
            ).fetchone()
            connection.close()
            assert receipt_row is not None
            other_receipt = SnapshotReceipt(
                snapshot_id=receipt_row[0] + ".other",
                resource_id=receipt_row[1],
                canonical_store_id=receipt_row[2],
                exported_revision=receipt_row[3],
                jsonl_digest=receipt_row[4],
                record_count=receipt_row[5] + 1,
                format_version=receipt_row[6],
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=other_receipt,
                receipt_digest=snapshot_receipt_digest(other_receipt),
            )
            stage.manifest_temp_path.write_text(
                contract_to_json(manifest),
                encoding="utf-8",
            )
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.MANIFEST_MISMATCH",
            )


class StageSealerValidationRaceTests(unittest.TestCase):
    def test_committed_update_between_validation_and_marker_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            real_fsync_assets = tm_stage_sealer._fsync_stage_assets

            def commit_tamper_then_fsync(*args: Any) -> None:
                connection = _raw_connection(stage.staged_db_path)
                connection.execute(
                    "UPDATE tm_record SET target_raw='post-validation' "
                    "WHERE record_id=1"
                )
                connection.commit()
                connection.close()
                real_fsync_assets(*args)

            sealer = _sealer()
            with patch(
                "tm_stage_sealer._fsync_stage_assets",
                side_effect=commit_tamper_then_fsync,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION",
                )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                status_rows = connection.execute(
                    "SELECT value FROM tm_meta "
                    "WHERE key = 'activation_status'"
                ).fetchall()
                record_count = connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()
                target_rows = connection.execute(
                    "SELECT target_raw FROM tm_record WHERE record_id=1"
                ).fetchall()
                integrity_rows = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(status_rows, [("UNPUBLISHED",)])
            self.assertEqual(record_count, (3,))
            self.assertEqual(target_rows, [("post-validation",)])
            self.assertEqual(integrity_rows, [("ok",)])
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )


class StageSealerPhysicalFailureTests(unittest.TestCase):
    def test_integrity_corruption_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA writable_schema=ON")
            indexes = connection.execute(
                "SELECT name, rootpage FROM sqlite_master "
                "WHERE type='index' AND name IN "
                "('idx_tm_exact','idx_tm_gram_lookup') ORDER BY name"
            ).fetchall()
            connection.execute(
                "UPDATE sqlite_master SET rootpage=? WHERE name=?",
                (indexes[1][1], indexes[0][0]),
            )
            connection.execute(
                "UPDATE sqlite_master SET rootpage=? WHERE name=?",
                (indexes[0][1], indexes[1][0]),
            )
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.INTEGRITY_FAILED",
            )
            self.assertEqual(len(_registry(sealer)._entries), 0)

    def test_foreign_key_violation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "INSERT INTO tm_gram("
                "gram_size, gram, record_id, term_frequency) "
                "VALUES (1, '\u0001', 999999, 1)"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FOREIGN_KEY_FAILED",
            )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertTrue(stage.staged_db_path.is_file())
            self.assertTrue(stage.manifest_temp_path.is_file())

    def test_open_write_transaction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            locker = sqlite3.connect(str(stage.staged_db_path))
            try:
                locker.execute("BEGIN EXCLUSIVE")
                sealer = _sealer()
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_INVALID",
                )
                self.assertEqual(len(_registry(sealer)._entries), 0)
                self.assertTrue(stage.staged_db_path.is_file())
                self.assertTrue(stage.manifest_temp_path.is_file())
            finally:
                locker.close()

    def test_fsync_sequence_closes_marker_before_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            calls: list[tuple[str, str]] = []
            real_file_fsync = tm_stage_sealer._fsync_file
            real_directory_fsync = tm_stage_sealer._fsync_directory
            real_mark_sealed = tm_stage_sealer._mark_stage_sealed
            real_commit = SealedArtifactRegistry.commit

            def record_file(path: Path, expected: object) -> None:
                calls.append(("file", path.name))
                real_file_fsync(path, cast(Any, expected))

            def record_directory(path: Path) -> None:
                calls.append(("dir", path.name))
                real_directory_fsync(path)

            def record_marker(
                path: Path,
                expected: object,
                *,
                expected_closure_digest: str,
            ) -> None:
                calls.append(("marker", path.name))
                real_mark_sealed(
                    path,
                    cast(Any, expected),
                    expected_closure_digest=expected_closure_digest,
                )

            def record_commit(
                self: SealedArtifactRegistry,
                reservation: tm_stage_sealer._RegistryReservation,
                evidence: StageValidationEvidence,
                generation: GenerationExpectation,
            ) -> SealedStage:
                calls.append(("commit", reservation.mutable.staged_db_path.name))
                return real_commit(self, reservation, evidence, generation)

            sealer = _sealer()
            with (
                patch(
                    "tm_stage_sealer._fsync_file",
                    side_effect=record_file,
                ),
                patch(
                    "tm_stage_sealer._fsync_directory",
                    side_effect=record_directory,
                ),
                patch(
                    "tm_stage_sealer._mark_stage_sealed",
                    side_effect=record_marker,
                ),
                patch.object(
                    SealedArtifactRegistry,
                    "commit",
                    autospec=True,
                    side_effect=record_commit,
                ),
            ):
                sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(
                calls,
                [
                    ("file", stage.staged_db_path.name),
                    ("file", stage.manifest_temp_path.name),
                    ("dir", stage.staged_db_path.parent.name),
                    ("marker", stage.staged_db_path.name),
                    ("file", stage.staged_db_path.name),
                    ("dir", stage.staged_db_path.parent.name),
                    ("commit", stage.staged_db_path.name),
                ],
            )

    def test_fsync_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with patch(
                "tm_stage_sealer.os.fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.FSYNC_FAILED",
                )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertTrue(stage.staged_db_path.is_file())
            self.assertTrue(stage.manifest_temp_path.is_file())
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_one_shot_fsync_failure_is_deterministically_retryable(
        self,
    ) -> None:
        real_fsync = tm_stage_sealer.os.fsync
        for failed_call in (1, 2, 3, 4, 5):
            with self.subTest(failed_call=failed_call):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    calls = [0]

                    def one_shot_fsync(descriptor: int) -> None:
                        calls[0] += 1
                        if calls[0] == failed_call:
                            raise OSError(
                                "injected one-shot fsync failure"
                            )
                        real_fsync(descriptor)

                    with patch(
                        "tm_stage_sealer.os.fsync",
                        side_effect=one_shot_fsync,
                    ):
                        _expect_seal_code(
                            self,
                            lambda: _seal(
                                sealer,
                                stage,
                                fts5_available=True,
                            ),
                            "SEALER.FSYNC_FAILED",
                        )
                    if failed_call <= 3:
                        self.assertEqual(calls[0], failed_call)
                    else:
                        self.assertEqual(
                            calls[0],
                            failed_call + 2,
                        )
                    self.assertEqual(
                        len(_registry(sealer)._entries),
                        0,
                    )
                    self.assertEqual(
                        len(_registry(sealer)._reservations),
                        0,
                    )
                    self.assertTrue(stage.staged_db_path.is_file())
                    self.assertTrue(stage.manifest_temp_path.is_file())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        status_rows = connection.execute(
                            "SELECT value FROM tm_meta "
                            "WHERE key = 'activation_status'"
                        ).fetchall()
                    finally:
                        connection.close()
                    self.assertEqual(status_rows, [("UNPUBLISHED",)])

                    sealed = _seal(sealer, stage, fts5_available=True)
                    self.assertTrue(sealer.registry.contains(sealed))
                    self.assertEqual(
                        len(_registry(sealer)._entries),
                        1,
                    )
                    self.assertEqual(sealed.evidence.record_count, 3)
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        record_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(record_count, (3,))
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=True,
                        ),
                        "SEALER.STAGE_INVALID",
                    )



class StageSealerDurableRetryTests(unittest.TestCase):
    """Finding B: failure-injection retry determinism with disk-state proof."""

    def test_reservation_failure_leaves_no_state_and_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with patch.object(
                SealedArtifactRegistry,
                "reserve",
                side_effect=StageSealError("SEALER.ALREADY_RESERVED"),
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.ALREADY_RESERVED",
                )
            _assert_clean_unpublished(self, sealer, stage)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(_registry(sealer)._entries), 1)

    def test_post_marker_failures_restore_unpublished_and_retry_succeeds(
        self,
    ) -> None:
        real_verify_sealed = tm_stage_sealer._verify_sealed_stage
        real_verify_manifest = tm_stage_sealer._verify_manifest_at_digest
        real_sha256 = tm_stage_sealer._file_sha256

        def one_shot(
            function: Any,
            *,
            code: str,
        ) -> Any:
            state = [0]

            def injected(*args: Any, **kwargs: Any) -> Any:
                state[0] += 1
                if state[0] == 1:
                    raise StageSealError(code)
                return function(*args, **kwargs)

            return injected

        cases: tuple[tuple[str, str, str], ...] = (
            (
                "post_marker_db_digest",
                "tm_stage_sealer._file_sha256",
                "SEALER.DIGEST_UNREADABLE",
            ),
            (
                "post_marker_manifest",
                "tm_stage_sealer._verify_manifest_at_digest",
                "SEALER.MANIFEST_INVALID",
            ),
            (
                "post_marker_reopen_evidence",
                "tm_stage_sealer._verify_sealed_stage",
                "SEALER.INTEGRITY_FAILED",
            ),
        )
        functions = {
            "tm_stage_sealer._file_sha256": real_sha256,
            "tm_stage_sealer._verify_manifest_at_digest": real_verify_manifest,
            "tm_stage_sealer._verify_sealed_stage": real_verify_sealed,
        }
        for name, patch_target, expected_code in cases:
            with self.subTest(failure=name):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    with patch(
                        patch_target,
                        side_effect=one_shot(
                            functions[patch_target],
                            code=expected_code,
                        ),
                    ):
                        _expect_seal_code(
                            self,
                            lambda: _seal(
                                sealer,
                                stage,
                                fts5_available=True,
                            ),
                            expected_code,
                        )
                    _assert_clean_unpublished(self, sealer, stage)
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    sealed = _seal(sealer, stage, fts5_available=True)
                    self.assertTrue(sealer.registry.contains(sealed))
                    self.assertEqual(len(_registry(sealer)._entries), 1)
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        record_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(record_count, (3,))

    def test_registry_commit_failure_restores_unpublished_and_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            real_commit = SealedArtifactRegistry.commit
            state = [0]

            def one_shot_commit(
                self_registry: SealedArtifactRegistry,
                reservation: tm_stage_sealer._RegistryReservation,
                evidence: StageValidationEvidence,
                generation: GenerationExpectation,
            ) -> SealedStage:
                state[0] += 1
                if state[0] == 1:
                    raise OSError("injected registry commit failure")
                return real_commit(
                    self_registry,
                    reservation,
                    evidence,
                    generation,
                )

            with patch.object(
                SealedArtifactRegistry,
                "commit",
                autospec=True,
                side_effect=one_shot_commit,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_INVALID",
                )
            _assert_clean_unpublished(self, sealer, stage)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(_registry(sealer)._entries), 1)
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                record_count = connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(record_count, (3,))


class StageSealerIdentitySwapTests(unittest.TestCase):
    """Finding A: creation-time identity enforcement at registration."""

    def _swap_with_identical_bytes(self, target: Path) -> None:
        payload = target.read_bytes()
        replacement = target.with_name(f"{target.name}.replacement")
        replacement.write_bytes(payload)
        os.replace(replacement, target)

    def _identity_of(self, path: Path) -> tuple[int, int]:
        observed = tm_stage_sealer._artifact_file_identity(
            path,
            missing_code="SEALER.STAGE_DATABASE_MISSING",
            unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
        )
        return (observed.device, observed.inode)

    def test_byte_identical_db_inode_swap_before_registration_denied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            original_identity = self._identity_of(stage.staged_db_path)
            swapped_bytes: list[bytes] = []
            real_build_binding = tm_stage_sealer._build_binding

            def swap_db_before_registration(
                identity: CanonicalResourceIdentity,
                receipt: SnapshotReceipt,
                manifest: SnapshotManifest,
            ) -> SnapshotBinding:
                swapped_bytes.append(stage.staged_db_path.read_bytes())
                self._swap_with_identical_bytes(stage.staged_db_path)
                return real_build_binding(identity, receipt, manifest)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_db_before_registration,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_DATABASE_UNSAFE",
                )
            registry = _registry(sealer)
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)
            self.assertNotEqual(
                self._identity_of(stage.staged_db_path),
                original_identity,
            )
            self.assertEqual(
                stage.staged_db_path.read_bytes(),
                swapped_bytes[0],
            )
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.STAGE_INVALID",
            )
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)

    def test_byte_identical_manifest_inode_swap_before_registration_denied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            original_identity = self._identity_of(stage.manifest_temp_path)
            real_build_binding = tm_stage_sealer._build_binding

            def swap_manifest_before_registration(
                identity: CanonicalResourceIdentity,
                receipt: SnapshotReceipt,
                manifest: SnapshotManifest,
            ) -> SnapshotBinding:
                self._swap_with_identical_bytes(stage.manifest_temp_path)
                return real_build_binding(identity, receipt, manifest)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_manifest_before_registration,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_MANIFEST_UNSAFE",
                )
            registry = _registry(sealer)
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)
            self.assertNotEqual(
                self._identity_of(stage.manifest_temp_path),
                original_identity,
            )
            _assert_clean_unpublished(self, sealer, stage)
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)

    def test_registry_rejects_double_reservation_and_stale_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            registry = _registry(_sealer())
            database_identity = tm_stage_sealer._artifact_file_identity(
                stage.staged_db_path,
                missing_code="SEALER.STAGE_DATABASE_MISSING",
                unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
            )
            manifest_identity = tm_stage_sealer._artifact_file_identity(
                stage.manifest_temp_path,
                missing_code="SEALER.STAGE_MANIFEST_MISSING",
                unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
            )
            reservation = registry.reserve(
                stage,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.ALREADY_RESERVED$",
            ):
                registry.reserve(
                    stage,
                    database_identity=database_identity,
                    manifest_identity=manifest_identity,
                )
            registry.release(reservation)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.RESERVATION_MISMATCH$",
            ):
                registry.commit(
                    reservation,
                    cast(Any, object()),
                    cast(Any, object()),
                )
            self.assertEqual(len(registry._reservations), 0)
            self.assertEqual(len(registry._entries), 0)
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.ALREADY_SEALED$",
            ):
                registry.reserve(
                    stage,
                    database_identity=database_identity,
                    manifest_identity=manifest_identity,
                )

    def test_release_is_idempotent_and_retry_seals_exactly_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            registry = _registry(_sealer())
            database_identity = tm_stage_sealer._artifact_file_identity(
                stage.staged_db_path,
                missing_code="SEALER.STAGE_DATABASE_MISSING",
                unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
            )
            manifest_identity = tm_stage_sealer._artifact_file_identity(
                stage.manifest_temp_path,
                missing_code="SEALER.STAGE_MANIFEST_MISSING",
                unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
            )
            reservation = registry.reserve(
                stage,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
            registry.release(reservation)
            registry.release(reservation)
            self.assertEqual(len(registry._reservations), 0)
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)


class StageSealerRegistryTests(unittest.TestCase):

    def _sealed_fixture(
        self,
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        StageSealer,
        MutableStageRef,
        SealedStage,
    ]:
        temporary = tempfile.TemporaryDirectory()
        try:
            _, stage = _build_stage(
                Path(temporary.name),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
        except BaseException:
            temporary.cleanup()
            raise
        return temporary, sealer, stage, sealed

    def test_post_seal_database_mutation_detected(self) -> None:
        temporary, sealer, stage, sealed = self._sealed_fixture()
        try:
            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA user_version=99")
            connection.commit()
            connection.close()
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.ARTIFACT_MUTATED",
            )
        finally:
            temporary.cleanup()

    def test_post_seal_manifest_mutation_detected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            mutable = _registry(sealer)._entries[
                sealed.artifact.artifact_id
            ].mutable
            with mutable.manifest_temp_path.open("ab") as stream:
                stream.write(b"\n")
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.ARTIFACT_MUTATED",
            )
        finally:
            temporary.cleanup()

    def test_second_sealer_seal_of_same_stage_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            _ = _seal(sealer, stage, fts5_available=True)
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.STAGE_INVALID",
            )

    def test_direct_registry_double_seal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            _expect_seal_code(
                self,
                lambda: sealer.registry.seal(
                    stage,
                    sealed.evidence,
                    sealed.generation,
                ),
                "SEALER.ALREADY_SEALED",
            )

    def test_forged_stage_rejected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            forged = object.__new__(SealedStage)
            for field_name, value in vars(sealed).items():
                object.__setattr__(forged, field_name, value)
            self.assertFalse(sealer.registry.contains(forged))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(forged),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_foreign_registry_rejected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            foreign = StageSealer(
                registry=SealedArtifactRegistry(
                    registry_namespace="coordinator.other"
                ),
                canonical_store_id="store.primary",
            )
            self.assertFalse(foreign.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: foreign.registry.state(sealed),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_registry_entry_mutation_rejected(self) -> None:
        temporary, sealer, stage, sealed = self._sealed_fixture()
        try:
            entry = _registry(sealer)._entries[sealed.artifact.artifact_id]
            other_stage = MutableStageRef(
                stage_id=stage.stage_id,
                resource_identity=stage.resource_identity,
                staged_db_path=stage.staged_db_path.with_name("other.db"),
                manifest_temp_path=stage.manifest_temp_path.with_name(
                    "other.manifest.json"
                ),
            )
            object.__setattr__(entry, "mutable", other_stage)
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_token_lifecycle_is_exact_single_use_and_terminal(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            registry = _registry(sealer)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.SEALED,
            )
            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    sealed,
                    current_generation=2,
                ),
                "SEALER.GENERATION_MISMATCH",
            )
            token = registry.issue_token(
                sealed,
                current_generation=None,
            )
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    sealed,
                    current_generation=None,
                ),
                "SEALER.TOKEN_ALREADY_ISSUED",
            )
            registry.consume(token)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.CONSUMED,
            )
            _expect_seal_code(
                self,
                lambda: registry.consume(token),
                "SEALER.TOKEN_NOT_ACTIVE",
            )
            _expect_seal_code(
                self,
                lambda: registry.cancel(token),
                "SEALER.TOKEN_NOT_ACTIVE",
            )
            _expect_seal_code(
                self,
                lambda: registry.cancel(
                    cast(contract_module._ActivationToken, object())
                ),
                "SEALER.TOKEN_INVALID",
            )
        finally:
            temporary.cleanup()

    def test_activation_nonce_replay_is_global_across_registry_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = SealedArtifactRegistry(
                registry_namespace="coordinator.primary"
            )
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            (root / "first").mkdir()
            _, first_stage = _build_stage(
                root / "first",
                fts5_available=True,
            )
            first = _seal(sealer, first_stage, fts5_available=True)
            first_token = registry.issue_token(
                first,
                current_generation=None,
            )
            registry.cancel(first_token)

            (root / "second").mkdir()
            _, second_stage = _build_stage(
                root / "second",
                fts5_available=True,
            )
            second = _seal(sealer, second_stage, fts5_available=True)
            second_entry = registry._entries[second.artifact.artifact_id]
            replay = contract_module._create_sealed_stage(
                registry_namespace=registry.registry_namespace,
                artifact_id=second.artifact.artifact_id,
                mutable_stage=second_entry.mutable,
                evidence=second.evidence,
                generation=second.generation,
                activation_nonce=first.activation_nonce,
            )
            object.__setattr__(second_entry, "stage", replay)

            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    replay,
                    current_generation=None,
                ),
                "SEALER.NONCE_REPLAY",
            )
            self.assertIs(
                registry.state(replay),
                ActivationCapabilityState.SEALED,
            )


class StageSealerInputValidationTests(unittest.TestCase):
    def test_init_rejects_invalid_registry_and_store_id(self) -> None:
        invalid_constructors: tuple[tuple[Any, Any], ...] = (
            (object(), "store.primary"),
            (_FakeRegistry(""), "store.primary"),
            (_FakeRegistry("   "), "store.primary"),
            (_FakeRegistry(7), "store.primary"),
            (_FakeRegistry("coordinator.primary"), ""),
            (_FakeRegistry("coordinator.primary"), "  "),
            (_FakeRegistry("coordinator.primary"), 7),
        )
        for registry_value, store_id in invalid_constructors:
            with self.subTest(
                registry_value=registry_value,
                store_id=store_id,
            ):
                _expect_seal_code(
                    self,
                    lambda: StageSealer(
                        registry=cast(Any, registry_value),
                        canonical_store_id=cast(Any, store_id),
                    ),
                    "SEALER.TYPE_INVALID",
                )
        with self.assertRaisesRegex(ValueError, "^registry_namespace"):
            SealedArtifactRegistry(registry_namespace="")
        with self.assertRaisesRegex(TypeError, "registry_namespace"):
            SealedArtifactRegistry(registry_namespace=cast(Any, 7))

    def test_seal_rejects_invalid_expected_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            for bad_value in (True, "3", 3.0):
                with self.subTest(bad_value=bad_value):
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=True,
                            expected_prior_generation=cast(int, bad_value),
                        ),
                        "SEALER.TYPE_INVALID",
                    )
            _expect_seal_code(
                self,
                lambda: _seal(
                    sealer,
                    stage,
                    fts5_available=True,
                    expected_prior_generation=-1,
                ),
                "SEALER.GENERATION_INVALID",
            )

    def test_seal_rejects_exact_type_violations_before_fs_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with self.assertRaises(TypeError):
                _seal(
                    sealer,
                    cast(MutableStageRef, object()),
                    fts5_available=True,
                )
            subclass_stage = MutableStageRef(
                stage_id=stage.stage_id,
                resource_identity=stage.resource_identity,
                staged_db_path=_PathSubclass(stage.staged_db_path),
                manifest_temp_path=stage.manifest_temp_path,
            )
            with self.assertRaises(TypeError):
                _seal(sealer, subclass_stage, fts5_available=True)
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_seal_error_codes_never_embed_paths_or_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET target_raw='tampered' "
                "WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            error = _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )
            self.assertEqual(str(error), "SEALER.RECORD_MISMATCH")
            self.assertNotIn(str(stage.staged_db_path), str(error))
            with self.assertRaises(TypeError):
                StageSealError(cast(str, cast(object, 7)))


if __name__ == "__main__":
    unittest.main()
