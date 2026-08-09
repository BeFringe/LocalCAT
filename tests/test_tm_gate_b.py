"""Adversarial unit tests for the Task 5.4 Gate B physical readiness gate."""

from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from typing import Any, cast
from unittest.mock import patch

import tm_contracts as contract_module
import tm_gate_b
import tm_stage_sealer
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SealedStage,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
    stage_validation_evidence_digest,
)
from tm_gate_b import (
    GateBEvaluator,
    GateBPhysicalFacts,
    GateBPhysicalReadinessReport,
    gate_b_facts_digest,
    gate_b_grant_digest,
    gate_b_report_digest,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import unique_character_ngrams
from tm_stage_sealer import (
    SealedArtifactRegistry,
    StageSealer,
)


SOURCE_BYTES = (
    b'{"source":"same","target":"first","speaker":"alice",'
    b'"context_prev":"before","file_source":"chapter.json"}\n'
    b"{bad-json}\n"
    b'{"source":"same","target":"second","context_next":"after"}\n'
    b'{"source":"x","target":"short"}\n'
)
_ERROR_CODE = re.compile(r"GATE_B\.[A-Z0-9_]+")


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


def _registry() -> SealedArtifactRegistry:
    return SealedArtifactRegistry(registry_namespace="coordinator.primary")


def _sealer(registry: SealedArtifactRegistry) -> StageSealer:
    return StageSealer(
        registry=registry,
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
    registry: SealedArtifactRegistry,
    stage: MutableStageRef,
    *,
    fts5_available: bool,
    expected_prior_generation: int | None = 0,
) -> SealedStage:
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        return _sealer(registry).seal(
            stage,
            expected_prior_generation=expected_prior_generation,
        )


def _evaluate(
    registry: SealedArtifactRegistry,
    sealed: SealedStage,
    *,
    fts5_available: bool,
) -> GateBPhysicalReadinessReport:
    evaluator = GateBEvaluator(registry=registry)
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        return evaluator.evaluate(sealed)


def _fixture(
    root: Path,
    *,
    fts5_available: bool,
    tamper: Any = None,
) -> tuple[
    CanonicalResourceIdentity,
    MutableStageRef,
    SealedArtifactRegistry,
    SealedStage,
]:
    identity, stage = _build_stage(root, fts5_available=fts5_available)
    registry = _registry()
    sealed = _seal(
        registry,
        stage,
        fts5_available=fts5_available,
    )
    if tamper is not None:
        tamper(stage, identity)
    return identity, stage, registry, sealed


def _raw_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path))
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _expect_code(
    test: unittest.TestCase,
    report: GateBPhysicalReadinessReport,
    error_code: str,
) -> None:
    test.assertFalse(report.granted)
    test.assertIsNone(report.grant)
    test.assertIsNone(report.facts)
    test.assertIsNone(report.facts_digest)
    test.assertIsNone(report.grant_digest)
    test.assertEqual(report.error_code, error_code)
    test.assertRegex(report.error_code or "", _ERROR_CODE)


class _SealedStageSubclass(SealedStage):
    pass


class _RegistrySubclass(SealedArtifactRegistry):
    pass


class _StructuralFakeRegistry:
    """Protocol-shaped registry that must fail the exact-type boundary."""

    def __init__(
        self,
        *,
        namespace: str,
        snapshot: tm_stage_sealer._PhysicalReadinessSnapshot,
    ) -> None:
        self._namespace = namespace
        self._snapshot = snapshot

    @property
    def registry_namespace(self) -> str:
        return self._namespace

    def resolve_physical_readiness(
        self,
        stage: SealedStage,
    ) -> tm_stage_sealer._PhysicalReadinessSnapshot:
        return self._snapshot


class GateBHappyPathTests(unittest.TestCase):
    def test_gate_b_grants_both_index_modes_with_closed_component_facts(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage, registry, sealed = _fixture(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    report = _evaluate(
                        registry,
                        sealed,
                        fts5_available=fts5_available,
                    )
                    self.assertTrue(report.granted)
                    self.assertIsNone(report.error_code)
                    self.assertIsNotNone(report.grant)
                    facts = report.facts
                    self.assertIsNotNone(facts)
                    assert facts is not None
                    self.assertEqual(facts.fts5_available, fts5_available)
                    self.assertEqual(facts.schema_version, 2)
                    self.assertEqual(facts.record_count, 3)
                    self.assertEqual(facts.origin_batch_count, 1)
                    self.assertEqual(facts.completed_revision, 1)
                    self.assertEqual(
                        facts.migration_batch_id,
                        f"migration.{facts.source_digest}",
                    )
                    expected_gram_sizes = (1, 2) if fts5_available else (
                        1,
                        2,
                        3,
                    )
                    self.assertEqual(
                        tuple(size for size, _count in facts.gram_counts),
                        expected_gram_sizes,
                    )
                    self.assertEqual(
                        facts.fts_count,
                        facts.record_count if fts5_available else 0,
                    )
                    self.assertTrue(facts.integrity_ok)
                    self.assertTrue(facts.foreign_keys_ok)
                    self.assertEqual(facts.journal_mode, "delete")
                    self.assertEqual(facts.synchronous, "FULL")
                    self.assertFalse(facts.wal_enabled)
                    self.assertFalse(facts.extension_loading_enabled)
                    self.assertEqual(
                        facts.candidate_index_kind,
                        "FTS5_TRIGRAM"
                        if fts5_available
                        else "GRAM_FALLBACK",
                    )
                    claim = sealed.evidence
                    self.assertEqual(
                        facts.snapshot_receipt_digest,
                        snapshot_receipt_digest(
                            claim.source_binding.receipt
                        ),
                    )
                    self.assertEqual(
                        facts.evidence_digest,
                        stage_validation_evidence_digest(claim),
                    )
                    self.assertEqual(
                        facts.manifest_temp_digest,
                        hashlib.sha256(
                            stage.manifest_temp_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        facts.stage_file_digest,
                        hashlib.sha256(
                            stage.staged_db_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        facts.expected_prior_generation,
                        sealed.expected_prior_generation,
                    )
                    self.assertEqual(
                        facts.registry_namespace,
                        registry.registry_namespace,
                    )
                    self.assertEqual(
                        facts.artifact_id,
                        sealed.artifact.artifact_id,
                    )
                    self.assertEqual(
                        facts.artifact_seal_digest,
                        sealed.artifact.seal_digest,
                    )
                    self.assertEqual(
                        facts.sealed_stage_digest,
                        sealed.sealed_stage_digest,
                    )
                    self.assertEqual(
                        facts.resource_id,
                        identity.resource_id,
                    )
                    self.assertEqual(
                        facts.target_identity,
                        identity.target_identity,
                    )
                    self.assertEqual(
                        facts.canonical_store_id,
                        "store.primary",
                    )
                    connection = _raw_connection(stage.staged_db_path)
                    try:
                        schema_digest_row = connection.execute(
                            "SELECT value FROM tm_meta "
                            "WHERE key = 'schema_digest'"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertIsNotNone(schema_digest_row)
                    assert schema_digest_row is not None
                    self.assertEqual(
                        facts.schema_digest,
                        str(schema_digest_row[0]),
                    )
                    self.assertEqual(
                        facts.source_digest,
                        claim.source_binding.receipt.jsonl_digest,
                    )
                    self.assertEqual(
                        report.facts_digest,
                        gate_b_facts_digest(facts),
                    )
                    grant = report.grant
                    assert grant is not None
                    self.assertEqual(
                        report.grant_digest,
                        gate_b_grant_digest(grant),
                    )
                    self.assertEqual(
                        report.report_digest,
                        gate_b_report_digest(report),
                    )
                    self.assertEqual(
                        grant.registry_namespace,
                        registry.registry_namespace,
                    )
                    self.assertEqual(
                        grant.artifact_id,
                        sealed.artifact.artifact_id,
                    )
                    self.assertEqual(
                        grant.artifact_seal_digest,
                        sealed.artifact.seal_digest,
                    )
                    self.assertEqual(
                        grant.sealed_stage_digest,
                        sealed.sealed_stage_digest,
                    )
                    self.assertEqual(
                        grant.snapshot_receipt_digest,
                        facts.snapshot_receipt_digest,
                    )
                    self.assertEqual(
                        grant.stage_db_digest,
                        facts.stage_file_digest,
                    )
                    self.assertEqual(
                        grant.manifest_temp_digest,
                        facts.manifest_temp_digest,
                    )
                    self.assertEqual(
                        grant.evidence_digest,
                        facts.evidence_digest,
                    )
                    self.assertEqual(
                        grant.expected_prior_generation,
                        sealed.expected_prior_generation,
                    )

    def test_fresh_recomputation_is_deterministic_and_bound_to_stage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            first = _evaluate(registry, sealed, fts5_available=True)
            second = _evaluate(registry, sealed, fts5_available=True)
            self.assertTrue(first.granted)
            self.assertEqual(first, second)
            self.assertEqual(first.report_digest, second.report_digest)
            self.assertEqual(first.grant, second.grant)
            third = _evaluate(registry, sealed, fts5_available=True)
            self.assertEqual(third, first)

    def test_foreign_registry_and_unregistered_artifact_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            foreign_registry = SealedArtifactRegistry(
                registry_namespace="coordinator.other"
            )
            report = _evaluate(
                foreign_registry,
                sealed,
                fts5_available=True,
            )
            _expect_code(self, report, "GATE_B.REGISTRY_MISMATCH")

            forged = contract_module._create_sealed_stage(
                registry_namespace=registry.registry_namespace,
                artifact_id="artifact.forged",
                mutable_stage=stage,
                evidence=sealed.evidence,
                generation=sealed.generation,
                activation_nonce="nonce.forged",
            )
            report = _evaluate(registry, forged, fts5_available=True)
            _expect_code(self, report, "GATE_B.REGISTRY_MISMATCH")


class GateBNoStateAdvanceTests(unittest.TestCase):
    def test_evaluation_never_advances_generation_or_token_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            db_bytes = stage.staged_db_path.read_bytes()
            manifest_bytes = stage.manifest_temp_path.read_bytes()
            jsonl_bytes = stage.resource_identity.configured_jsonl_path.read_bytes()
            report = _evaluate(registry, sealed, fts5_available=True)
            self.assertTrue(report.granted)
            self.assertEqual(
                registry.state(sealed),
                contract_module.ActivationCapabilityState.SEALED,
            )
            second_report = _evaluate(registry, sealed, fts5_available=True)
            self.assertTrue(second_report.granted)
            self.assertEqual(
                registry.state(sealed),
                contract_module.ActivationCapabilityState.SEALED,
            )
            connection = _raw_connection(stage.staged_db_path)
            try:
                meta = dict(
                    connection.execute(
                        "SELECT key, value FROM tm_meta"
                    ).fetchall()
                )
            finally:
                connection.close()
            self.assertEqual(meta["generation"], "0")
            self.assertEqual(meta["head_revision"], "1")
            self.assertEqual(meta["activation_status"], "SEALED")
            self.assertNotIn("activation_digest", meta)
            self.assertEqual(
                stage.staged_db_path.read_bytes(),
                db_bytes,
            )
            self.assertEqual(
                stage.manifest_temp_path.read_bytes(),
                manifest_bytes,
            )
            self.assertEqual(
                stage.resource_identity.configured_jsonl_path.read_bytes(),
                jsonl_bytes,
            )


class GateBFailClosedMatrixTests(unittest.TestCase):
    def test_incomplete_and_extra_gram_content_denies_both_modes(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):

                def missing_gram(stage: MutableStageRef, _identity: Any) -> None:
                    connection = _raw_connection(stage.staged_db_path)
                    try:
                        connection.execute(
                            "DELETE FROM tm_gram "
                            "WHERE record_id = 1 AND gram_size = 1 "
                            "AND gram = 's'"
                        )
                        connection.commit()
                    finally:
                        connection.close()

                with tempfile.TemporaryDirectory() as temporary:
                    _, _stage, registry, sealed = _fixture(
                        Path(temporary),
                        fts5_available=fts5_available,
                        tamper=missing_gram,
                    )
                    report = _evaluate(
                        registry,
                        sealed,
                        fts5_available=fts5_available,
                    )
                    _expect_code(
                        self,
                        report,
                        "GATE_B.CANDIDATE_INDEX_INCOMPLETE",
                    )

                def extra_gram(stage: MutableStageRef, _identity: Any) -> None:
                    connection = _raw_connection(stage.staged_db_path)
                    try:
                        connection.execute(
                            "INSERT INTO tm_gram(gram_size, gram, record_id) "
                            "VALUES (1, 'z', 3)"
                        )
                        connection.commit()
                    finally:
                        connection.close()

                with tempfile.TemporaryDirectory() as temporary:
                    _, _stage, registry, sealed = _fixture(
                        Path(temporary),
                        fts5_available=fts5_available,
                        tamper=extra_gram,
                    )
                    report = _evaluate(
                        registry,
                        sealed,
                        fts5_available=fts5_available,
                    )
                    _expect_code(
                        self,
                        report,
                        "GATE_B.CANDIDATE_INDEX_INCOMPLETE",
                    )

    def test_missing_extra_and_broken_fts_rows_deny(self) -> None:
        def missing_fts(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "DELETE FROM tm_fts WHERE record_id = 1"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=missing_fts,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.FTS_INDEX_INCOMPLETE")

        def extra_fts(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "INSERT INTO tm_fts(record_id, source_fold_v1) "
                    "VALUES (999, 'zzz')"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=extra_fts,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.FTS_INDEX_INCOMPLETE")

        def broken_fts(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_fts SET source_fold_v1 = 'zzz' "
                    "WHERE record_id = 1"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=broken_fts,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.FTS_INDEX_INCOMPLETE")

    def test_same_count_record_and_parity_tamper_denies(self) -> None:
        def target_tamper(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_record SET target_raw = 'tampered' "
                    "WHERE record_id = 1"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=target_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.RECORD_MISMATCH")

        def order_swap(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_record SET "
                    "source_raw = CASE record_id WHEN 2 THEN 'x' "
                    "ELSE 'same' END, "
                    "target_raw = CASE record_id WHEN 2 THEN 'short' "
                    "ELSE 'second' END, "
                    "source_fold_v1 = CASE record_id WHEN 2 THEN 'x' "
                    "ELSE 'same' END "
                    "WHERE record_id IN (2, 3)"
                )
                connection.execute(
                    "UPDATE tm_fts SET source_fold_v1 = "
                    "CASE record_id WHEN 2 THEN 'x' ELSE 'same' END "
                    "WHERE record_id IN (2, 3)"
                )
                connection.execute(
                    "DELETE FROM tm_gram WHERE record_id IN (2, 3)"
                )
                for record_id, folded in ((2, "x"), (3, "same")):
                    for gram_size in (1, 2, 3):
                        for gram in unique_character_ngrams(
                            folded,
                            gram_size,
                        ):
                            connection.execute(
                                "INSERT INTO tm_gram("
                                "gram_size, gram, record_id) "
                                "VALUES (?, ?, ?)",
                                (gram_size, gram, record_id),
                            )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=order_swap,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.RECORD_MISMATCH")

        def jsonl_target_tamper(
            _stage: MutableStageRef,
            identity: CanonicalResourceIdentity,
        ) -> None:
            identity.configured_jsonl_path.write_bytes(
                b'{"source":"same","target":"first"}\n'
                b'{"source":"same","target":"tampered"}\n'
                b'{"source":"x","target":"short"}\n'
            )

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=jsonl_target_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.RECORD_MISMATCH")

        def jsonl_byte_tamper(
            _stage: MutableStageRef,
            identity: CanonicalResourceIdentity,
        ) -> None:
            identity.configured_jsonl_path.write_bytes(
                SOURCE_BYTES.replace(b"{bad-json}", b"{bad-json-x}")
            )

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=jsonl_byte_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.SOURCE_MISMATCH")

    def test_coordinated_fold_tamper_denies_even_when_index_rows_follow(
        self,
    ) -> None:
        def fold_tamper(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_record SET source_fold_v1 = 'tampered' "
                    "WHERE record_id = 1"
                )
                connection.execute(
                    "UPDATE tm_fts SET source_fold_v1 = 'tampered' "
                    "WHERE record_id = 1"
                )
                connection.execute(
                    "DELETE FROM tm_gram WHERE record_id = 1"
                )
                for gram_size in (1, 2, 3):
                    for gram in unique_character_ngrams(
                        "tampered",
                        gram_size,
                    ):
                        connection.execute(
                            "INSERT INTO tm_gram("
                            "gram_size, gram, record_id) "
                            "VALUES (?, ?, 1)",
                            (gram_size, gram),
                        )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=fold_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.FOLD_MISMATCH")

    def test_receipt_manifest_and_binding_mismatch_denies(self) -> None:
        def receipt_tamper(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_receipt SET resource_id = 'tm.other' "
                    "WHERE snapshot_id = ("
                    "SELECT snapshot_id FROM tm_snapshot_receipt LIMIT 1)"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=receipt_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.RECEIPT_MISMATCH")

        def foreign_manifest(
            stage: MutableStageRef,
            _identity: Any,
        ) -> None:
            foreign = SnapshotReceipt(
                snapshot_id="snapshot.foreign",
                resource_id="tm.foreign",
                canonical_store_id="store.foreign",
                exported_revision=0,
                jsonl_digest="0" * 64,
                record_count=0,
            )
            stage.manifest_temp_path.write_bytes(
                contract_to_json(foreign).encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=foreign_manifest,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.MANIFEST_MISMATCH")

        def manifest_receipt_mismatch(
            stage: MutableStageRef,
            _identity: Any,
            sealed: SealedStage,
        ) -> None:
            claim = sealed.evidence
            tampered_receipt = SnapshotReceipt(
                snapshot_id=claim.source_binding.receipt.snapshot_id,
                resource_id=claim.source_binding.receipt.resource_id,
                canonical_store_id=(
                    claim.source_binding.receipt.canonical_store_id
                ),
                exported_revision=(
                    claim.source_binding.receipt.exported_revision
                ),
                jsonl_digest="1" * 64,
                record_count=claim.source_binding.receipt.record_count,
            )
            tampered_manifest = contract_module.SnapshotManifest(
                manifest_version=contract_module.SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=contract_module.SnapshotKind.MIGRATION_SOURCE,
                receipt=tampered_receipt,
                receipt_digest=snapshot_receipt_digest(tampered_receipt),
            )
            stage.manifest_temp_path.write_bytes(
                contract_to_json(tampered_manifest).encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as temporary:
            identity, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            manifest_receipt_mismatch(stage, identity, sealed)
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.MANIFEST_MISMATCH")

        def binding_tamper(
            stage: MutableStageRef,
            identity: CanonicalResourceIdentity,
        ) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "INSERT INTO tm_snapshot_binding("
                    "binding_id, configured_jsonl_path, manifest_path, "
                    "snapshot_kind, snapshot_id, binding_version) "
                    "VALUES (1, ?, ?, 'MIGRATION_SOURCE', "
                    "(SELECT snapshot_id FROM tm_snapshot_receipt LIMIT 1), "
                    "'snapshot-binding-v1')",
                    (
                        str(identity.configured_jsonl_path),
                        str(identity.snapshot_manifest_path),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=binding_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.BINDING_MISMATCH")

    def test_schema_runtime_and_version_mismatch_denies(self) -> None:
        meta_tampers = {
            "GATE_B.SCHEMA_MISMATCH": ("schema_version", "1"),
            "GATE_B.RUNTIME_MISMATCH": (
                "sqlite_runtime_version",
                "3.0.0",
            ),
            "GATE_B.RUNTIME_MISMATCH": ("journal_mode", "wal"),
            "GATE_B.VERSION_MISMATCH": (
                "fold_version",
                "fold-v1-other",
            ),
            "GATE_B.CANDIDATE_INDEX_MISMATCH": (
                "candidate_index_kind",
                "GRAM_FALLBACK",
            ),
            "GATE_B.IDENTITY_MISMATCH": ("resource_id", "tm.other"),
            "GATE_B.IDENTITY_MISMATCH": (
                "canonical_store_id",
                "store.other",
            ),
        }
        for expected_code, (key, value) in meta_tampers.items():
            with self.subTest(key=key):

                def tamper(
                    stage: MutableStageRef,
                    _identity: Any,
                    *,
                    key: str = key,
                    value: str = value,
                ) -> None:
                    connection = _raw_connection(stage.staged_db_path)
                    try:
                        connection.execute(
                            "UPDATE tm_meta SET value = ? WHERE key = ?",
                            (value, key),
                        )
                        connection.commit()
                    finally:
                        connection.close()

                with tempfile.TemporaryDirectory() as temporary:
                    _, _stage, registry, sealed = _fixture(
                        Path(temporary),
                        fts5_available=True,
                        tamper=tamper,
                    )
                    report = _evaluate(
                        registry,
                        sealed,
                        fts5_available=True,
                    )
                    _expect_code(self, report, expected_code)

        def drop_index(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute("DROP INDEX idx_tm_exact")
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=drop_index,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.SCHEMA_INCOMPLETE")

        def extra_table(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute("CREATE TABLE tamper(x)")
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=extra_table,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.SCHEMA_UNEXPECTED")

        def target_identity_tamper(
            stage: MutableStageRef,
            _identity: Any,
        ) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = ? "
                    "WHERE key = 'target_identity'",
                    ("2" * 64,),
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=target_identity_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.IDENTITY_MISMATCH")

    def test_db_and_manifest_digest_mutation_denies(self) -> None:
        def usage_count_tamper(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_record SET usage_count = 99 "
                    "WHERE record_id = 1"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=usage_count_tamper,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.EVIDENCE_MISMATCH")

        def manifest_append(stage: MutableStageRef, _identity: Any) -> None:
            with stage.manifest_temp_path.open("ab") as stream:
                stream.write(b"\n")

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=manifest_append,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.EVIDENCE_MISMATCH")

    def test_non_sealed_and_unregistered_input_denies(self) -> None:
        def revert_marker(stage: MutableStageRef, _identity: Any) -> None:
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = 'UNPUBLISHED' "
                    "WHERE key = 'activation_status'"
                )
                connection.commit()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=revert_marker,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.STAGE_NOT_SEALED")

        with tempfile.TemporaryDirectory() as temporary_a, tempfile.TemporaryDirectory() as temporary_b:
            identity_a = _identity(Path(temporary_a))
            identity_a.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            service_a = _service(identity_a)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=True,
            ):
                build_a = service_a.build_mutable_stage(
                    identity_a.configured_jsonl_path
                )
            stage_a = build_a.mutable_stage
            if stage_a is None:
                raise AssertionError("expected a mutable stage")
            identity_b = _identity(Path(temporary_b))
            identity_b.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            service_b = _service(identity_b)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=True,
            ):
                build_b = service_b.build_mutable_stage(
                    identity_b.configured_jsonl_path
                )
            stage_b = build_b.mutable_stage
            if stage_b is None:
                raise AssertionError("expected a mutable stage")
            registry_a = _registry()
            sealed_a = _seal(
                registry_a,
                stage_a,
                fts5_available=True,
            )
            registry_b = SealedArtifactRegistry(
                registry_namespace="coordinator.other"
            )
            sealed_b = _seal(
                registry_b,
                stage_b,
                fts5_available=True,
            )
            report = _evaluate(
                registry_a,
                sealed_b,
                fts5_available=True,
            )
            _expect_code(self, report, "GATE_B.REGISTRY_MISMATCH")
            report = _evaluate(
                registry_b,
                sealed_a,
                fts5_available=True,
            )
            _expect_code(self, report, "GATE_B.REGISTRY_MISMATCH")

            forged = contract_module._create_sealed_stage(
                registry_namespace=registry_a.registry_namespace,
                artifact_id="artifact.forged",
                mutable_stage=stage_a,
                evidence=sealed_a.evidence,
                generation=sealed_a.generation,
                activation_nonce="nonce.forged",
            )
            report = _evaluate(
                registry_a,
                forged,
                fts5_available=True,
            )
            _expect_code(self, report, "GATE_B.REGISTRY_MISMATCH")


class GateBStalenessTests(unittest.TestCase):
    def test_prior_success_cannot_authorize_after_file_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            first = _evaluate(registry, sealed, fts5_available=True)
            self.assertTrue(first.granted)
            assert first.grant is not None
            prior_db_digest = first.grant.stage_db_digest
            current_digest = hashlib.sha256(
                stage.staged_db_path.read_bytes()
            ).hexdigest()
            self.assertEqual(prior_db_digest, current_digest)

            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "DELETE FROM tm_gram "
                    "WHERE record_id = 1 AND gram_size = 1 AND gram = 's'"
                )
                connection.commit()
            finally:
                connection.close()

            second = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(
                self,
                second,
                "GATE_B.CANDIDATE_INDEX_INCOMPLETE",
            )
            self.assertIsNone(second.grant)
            self.assertIsNone(second.facts)
            self.assertNotEqual(
                hashlib.sha256(
                    stage.staged_db_path.read_bytes()
                ).hexdigest(),
                prior_db_digest,
            )
            self.assertEqual(first.grant.artifact_id, sealed.artifact.artifact_id)


class GateBAdversarialClosureTests(unittest.TestCase):
    """Finding A: terminal identity+digest closure before any grant."""

    def _swap_with_identical_bytes(self, target: Path) -> None:
        payload = target.read_bytes()
        replacement = target.with_name(f"{target.name}.replacement")
        replacement.write_bytes(payload)
        os.replace(replacement, target)

    def test_same_inode_db_mutation_after_db_digest_before_grant_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_evidence = tm_stage_sealer._build_evidence

            def mutate_db_then_build_evidence(*args: Any, **kwargs: Any) -> Any:
                connection = _raw_connection(stage.staged_db_path)
                try:
                    connection.execute("PRAGMA user_version=99")
                    connection.commit()
                finally:
                    connection.close()
                return real_build_evidence(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_evidence",
                side_effect=mutate_db_then_build_evidence,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_MUTATED")
            self.assertFalse(report.granted)
            self.assertIsNone(report.grant)

    def test_same_inode_manifest_mutation_after_digest_before_grant_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_binding = tm_stage_sealer._build_binding

            def mutate_manifest_then_build_binding(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                with stage.manifest_temp_path.open("ab") as stream:
                    stream.write(b"\n")
                return real_build_binding(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=mutate_manifest_then_build_binding,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_MUTATED")

    def test_source_mutation_after_validation_before_grant_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_binding = tm_stage_sealer._build_binding

            def mutate_source_then_build_binding(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                with identity.configured_jsonl_path.open("ab") as stream:
                    stream.write(b'{"source":"y","target":"late"}\n')
                return real_build_binding(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=mutate_source_then_build_binding,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.SOURCE_MISMATCH")

    def test_byte_identical_db_swap_during_evaluation_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_binding = tm_stage_sealer._build_binding

            def swap_db_then_build_binding(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                self._swap_with_identical_bytes(stage.staged_db_path)
                return real_build_binding(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_db_then_build_binding,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_UNSAFE")

    def test_byte_identical_manifest_swap_during_evaluation_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_binding = tm_stage_sealer._build_binding

            def swap_manifest_then_build_binding(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                self._swap_with_identical_bytes(stage.manifest_temp_path)
                return real_build_binding(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_manifest_then_build_binding,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_UNSAFE")

    def test_byte_identical_source_swap_during_evaluation_denies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_build_binding = tm_stage_sealer._build_binding

            def swap_source_then_build_binding(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                self._swap_with_identical_bytes(
                    identity.configured_jsonl_path
                )
                return real_build_binding(*args, **kwargs)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_source_then_build_binding,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.SOURCE_MISMATCH")

    def test_byte_identical_db_swap_after_registration_denies(
        self,
    ) -> None:
        def swap_db(stage: MutableStageRef, _identity: Any) -> None:
            self._swap_with_identical_bytes(stage.staged_db_path)

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=swap_db,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_UNSAFE")

    def test_byte_identical_manifest_swap_after_registration_denies(
        self,
    ) -> None:
        def swap_manifest(stage: MutableStageRef, _identity: Any) -> None:
            self._swap_with_identical_bytes(stage.manifest_temp_path)

        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
                tamper=swap_manifest,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_UNSAFE")


class GateBLinearizationTests(unittest.TestCase):
    """Finding B: terminal closure re-runs at the Gate B linearization point.

    Claim closure and grant minting happen after the recomputation returns,
    so the recomputation-internal closure cannot see a mutation made in or
    after _require_claim_closure.  Gate B re-runs the same no-follow
    identity+digest closure at its linearization point, immediately before
    the grant is minted, so a grant's digests always describe the exact
    artifact bytes at that point.
    """

    def test_claim_closure_mutation_before_grant_denies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            real_require_claim_closure = tm_gate_b._require_claim_closure

            def mutate_after_claim_closure(
                snapshot: tm_stage_sealer._PhysicalReadinessSnapshot,
                recomputed: tm_stage_sealer._SealedRecomputation,
            ) -> None:
                real_require_claim_closure(snapshot, recomputed)
                connection = _raw_connection(stage.staged_db_path)
                try:
                    connection.execute(
                        "UPDATE tm_record SET target_raw = 'tampered' "
                        "WHERE record_id = 1"
                    )
                    connection.commit()
                finally:
                    connection.close()

            with patch(
                "tm_gate_b._require_claim_closure",
                side_effect=mutate_after_claim_closure,
            ):
                report = _evaluate(registry, sealed, fts5_available=True)
            _expect_code(self, report, "GATE_B.ARTIFACT_MUTATED")
            self.assertNotEqual(
                hashlib.sha256(
                    stage.staged_db_path.read_bytes()
                ).hexdigest(),
                sealed.evidence.stage_file_digest,
            )


class GateBBoundaryTests(unittest.TestCase):
    def test_structural_fake_registry_cannot_supply_readiness_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            snapshot = registry.resolve_physical_readiness(sealed)
            fake: Any = _StructuralFakeRegistry(
                namespace=registry.registry_namespace,
                snapshot=snapshot,
            )
            with self.assertRaises(TypeError):
                GateBEvaluator(registry=fake)

    def test_registry_subclass_cannot_supply_readiness_authority(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            GateBEvaluator(
                registry=_RegistrySubclass(
                    registry_namespace="coordinator.primary"
                )
            )

    def test_raw_path_mutable_stage_bool_and_subclass_cannot_enter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            evaluator = GateBEvaluator(registry=registry)
            for invalid in (
                stage.staged_db_path,
                stage,
                True,
                sealed.evidence,
                _SealedStageSubclass(
                    artifact=sealed.artifact,
                    evidence=sealed.evidence,
                    generation=sealed.generation,
                    activation_nonce=sealed.activation_nonce,
                    sealed_stage_digest=sealed.sealed_stage_digest,
                ),
            ):
                with self.subTest(type=type(invalid).__name__):
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        report = evaluator.evaluate(
                            cast(SealedStage, invalid)
                        )
                    _expect_code(self, report, "GATE_B.TYPE_INVALID")

    def test_callers_cannot_construct_or_forge_grants_or_reports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            assert report.grant is not None
            grant = report.grant
            with self.assertRaises(TypeError):
                tm_gate_b.GateBGrant(
                    grant_version=tm_gate_b.GATE_B_GRANT_VERSION,
                    registry_namespace=grant.registry_namespace,
                    artifact_id=grant.artifact_id,
                    artifact_seal_digest=grant.artifact_seal_digest,
                    sealed_stage_digest=grant.sealed_stage_digest,
                    resource_id=grant.resource_id,
                    target_identity=grant.target_identity,
                    canonical_store_id=grant.canonical_store_id,
                    snapshot_receipt_digest=grant.snapshot_receipt_digest,
                    expected_prior_generation=grant.expected_prior_generation,
                    stage_db_digest=grant.stage_db_digest,
                    manifest_temp_digest=grant.manifest_temp_digest,
                    evidence_digest=grant.evidence_digest,
                    grant_digest=grant.grant_digest,
                )
            with self.assertRaises(TypeError):
                dataclasses.replace(grant, registry_namespace="forged")
            with self.assertRaises(TypeError):
                tm_gate_b.GateBPhysicalReadinessReport(
                    report_version=tm_gate_b.GATE_B_REPORT_VERSION,
                    granted=True,
                    error_code=None,
                    facts=report.facts,
                    grant=grant,
                    facts_digest=report.facts_digest,
                    grant_digest=report.grant_digest,
                    report_digest=report.report_digest,
                )
            with self.assertRaises(TypeError):
                dataclasses.replace(report, granted=False, error_code=None)


class GateBSafeDiagnosticsTests(unittest.TestCase):
    def test_reports_never_contain_paths_or_tm_text(self) -> None:
        forbidden = {
            "tm.primary.jsonl",
            "localcat-migration",
            ".sqlite3",
            "first",
            "second",
            "short",
            "alice",
        }
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            connection = _raw_connection(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_record SET target_raw = 'tampered' "
                    "WHERE record_id = 1"
                )
                connection.commit()
            finally:
                connection.close()
            report = _evaluate(registry, sealed, fts5_available=True)
            self.assertFalse(report.granted)
            self.assertEqual(report.error_code, "GATE_B.RECORD_MISMATCH")
            rendered = f"{report!r} {report!s}"
            for token in forbidden:
                self.assertNotIn(token, rendered)
            for token in (str(temporary), "/"):
                self.assertNotIn(token, rendered)

    def test_granted_report_diagnostics_are_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage, registry, sealed = _fixture(
                Path(temporary),
                fts5_available=True,
            )
            report = _evaluate(registry, sealed, fts5_available=True)
            self.assertTrue(report.granted)
            rendered = f"{report!r} {report!s}"
            for token in (
                "first",
                "second",
                "short",
                str(temporary),
                stage.staged_db_path.name,
            ):
                self.assertNotIn(token, rendered)

if __name__ == "__main__":
    unittest.main()
