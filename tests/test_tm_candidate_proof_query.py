from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tm_candidate_index import CandidateRetriever
from tm_contracts import (
    CandidateStage,
    CanonicalResourceIdentity,
    TMMatchType,
    TMQuery,
    TMRecordDraft,
    TMResourceHandle,
    contract_from_json,
    contract_to_json,
)
from tm_migration import TMMigrationService
from tm_retrieval import TMRetrievalService, prove_and_score_fuzzy_candidates
from tm_similarity import SimilarityScorerV1
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    SQLiteCandidateProofSnapshot,
    SQLiteTMQueryView,
    SQLiteTMStore,
    character_ngram_frequencies,
    initialize_stage_schema,
)
from tm_stage_sealer import StageSealer
from tests.test_tm_sqlite_store import _stage
from tests.test_tm_retrieval import _retrieval_capability_publisher


def _unchecked_replace(value, **changes):
    """Forge a malformed frozen contract for encode-boundary rejection tests."""

    forged = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(value, field.name)),
        )
    return forged


def _draft(source: str, ordinal: int) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=f"target-{ordinal}",
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "proof-test"),),
    )


def _store(
    root: Path,
    sources: tuple[str, ...],
    *,
    fts5_available: bool,
) -> SQLiteTMStore:
    stage = _stage(root)
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        store = SQLiteTMStore(stage, canonical_store_id="store.primary")
    store.append_batch(
        batch_id="import.proof-query",
        kind="import",
        drafts=tuple(_draft(source, index) for index, source in enumerate(sources)),
        source_digest="9" * 64,
        source_path=(root / "source.jsonl").resolve(),
    )
    return store


def _query(source: str, *, threshold: float = 0.60, limit: int = 10) -> TMQuery:
    return TMQuery(
        query_source=source,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        minimum_similarity=threshold,
        limit=limit,
        resource_order=("tm.primary",),
    )


def _activated_store(
    root: Path,
    sources: tuple[str, ...],
    *,
    resource_id: str,
    fts5_available: bool,
) -> SQLiteTMStore:
    root.mkdir()
    fixture = root / "source.jsonl"
    fixture.write_text(
        "".join(
            json.dumps(
                {"source": source, "target": f"target-{index}", "speaker": "speaker"},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            for index, source in enumerate(sources)
        ),
        encoding="utf-8",
    )
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        fixture.resolve(),
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=f"store.{resource_id}",
        resource_identity=identity,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        build = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=f"store.{resource_id}",
        ).build_mutable_stage(fixture)
        assert build.mutable_stage is not None
        sealed = StageSealer(
            registry=coordinator.sealed_registry,
            canonical_store_id=f"store.{resource_id}",
        ).seal(build.mutable_stage, expected_prior_generation=None)
        prepared = coordinator.activate(sealed)
        journal = coordinator.publish_prepared_activation(prepared)
        coordinator.publish_activation(prepared, journal)
    return SQLiteTMStore.from_coordinator(coordinator)


class _AppendingScorer:
    def __init__(self, store: SQLiteTMStore, root: Path) -> None:
        self._store = store
        self._root = root
        self._called = False

    def score(self, query: str, candidate: str):
        if not self._called:
            self._called = True
            self._store.append_batch(
                batch_id="import.concurrent-proof",
                kind="import",
                drafts=(_draft("concurrent row", 999),),
                source_digest="8" * 64,
                source_path=(self._root / "concurrent.jsonl").resolve(),
            )
        return SimilarityScorerV1().score(query, candidate)


class CandidateProofQueryTests(unittest.TestCase):
    def test_global_frontier_opens_high_bound_nonseed_before_low_bound_seed(
        self,
    ) -> None:
        query_source = "abcdefghij"
        low_bound_seed = query_source + ("x" * 990)
        high_bound_nonseed = "acebdzzzzz"
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    store = _store(
                        Path(temporary),
                        (low_bound_seed,) * 256 + (high_bound_nonseed,) * 44,
                        fts5_available=fts5_available,
                    )
                    opened: list[int] = []
                    original = SQLiteTMQueryView.candidate_proof_block_records

                    def observe_open(view: SQLiteTMQueryView, **kwargs):
                        opened.append(kwargs["block"].block_id)
                        return original(view, **kwargs)

                    with store.query_lease() as view, patch.object(
                        SQLiteTMQueryView,
                        "candidate_proof_block_records",
                        new=observe_open,
                    ):
                        snapshot = view.candidate_proof_snapshot(
                            folded_query=query_source,
                            seed_limit=256,
                        )
                        self.assertTrue(snapshot.seed_stages)
                        self.assertTrue(all(
                            record_id <= 256
                            for _stage, record_ids in snapshot.seed_stages
                            for record_id in record_ids
                        ))
                        self.assertGreater(
                            snapshot.blocks[1].character_intersection_upper,
                            0,
                        )
                        session = CandidateRetriever().proof_session_from_view(
                            "tm.primary",
                            view,
                            query_source,
                            minimum_similarity=0.60,
                            result_limit=10,
                        )
                        self.assertEqual(opened, [])
                        _ = session.next_batch()

                    self.assertTrue(opened)
                    self.assertEqual(opened[0], 1)
                    opened.clear()
                    with store.query_lease() as view, patch.object(
                        SQLiteTMQueryView,
                        "candidate_proof_block_records",
                        new=observe_open,
                    ):
                        _fuzzy, report = prove_and_score_fuzzy_candidates(
                            resource_id="tm.primary",
                            resource_order=0,
                            query=_query(query_source),
                            view=view,
                        )
                    proof = report.metadata.proof
                    assert proof is not None
                    self.assertEqual(opened, [1, 0])
                    self.assertEqual(proof.seed_unique_count, 256)

    def test_session_initialization_and_unopened_blocks_are_lazy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(chr(0x400 + (index % 200)) for index in range(600)),
                fts5_available=False,
            )
            opened: list[int] = []
            original = SQLiteTMQueryView.candidate_proof_block_records

            def observe_open(view: SQLiteTMQueryView, **kwargs):
                opened.append(kwargs["block"].block_id)
                return original(view, **kwargs)

            with store.query_lease() as view, patch.object(
                SQLiteTMQueryView,
                "candidate_proof_block_records",
                new=observe_open,
            ):
                session = CandidateRetriever().proof_session_from_view(
                    "tm.primary",
                    view,
                    "a",
                    minimum_similarity=0.60,
                    result_limit=10,
                )
                self.assertEqual(opened, [])
                batch = session.next_batch()

        self.assertEqual(opened, [2])
        self.assertLessEqual(len(batch), 32)
        self.assertTrue(all(record_id > 512 for record_id in batch))

    def test_proof_contract_round_trip_and_shape_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                ("proof target", "other source"),
                fts5_available=False,
            )
            with store.query_lease() as view:
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("proof target"),
                    view=view,
                )
        encoded = contract_to_json(report)
        self.assertEqual(contract_from_json(encoded), report)
        payload = json.loads(encoded)
        proof = payload["payload"]["metadata"]["proof"]
        proof["caller_complete"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            contract_from_json(json.dumps(payload))

    def test_proof_contract_closes_universe_stage_and_scored_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(chr(0x400 + index) for index in range(40)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("a"),
                    view=view,
                )
        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(proof.total_record_count, 40)
        self.assertEqual(proof.kth_record_id, 31)
        self.assertEqual(proof.unscored_possible_record_id, 30)

        with self.assertRaises(ValueError):
            _ = replace(proof, kth_record_id=41)
        with self.assertRaises(ValueError):
            _ = replace(
                proof,
                kth_score=0.1,
                unscored_possible_record_id=41,
            )

        with self.assertRaises(ValueError):
            _ = replace(
                report.metadata,
                proof=replace(proof, seed_unique_count=1),
            )

        mismatched_proofs = (
            replace(proof, kth_score=0.1, kth_record_id=1),
            replace(
                proof,
                kth_score=0.1,
                unscored_possible_record_id=40,
            ),
        )
        for mismatched in mismatched_proofs:
            with self.subTest(mismatched=mismatched):
                metadata = replace(report.metadata, proof=mismatched)
                with self.assertRaises(ValueError):
                    _ = replace(report, metadata=metadata)

    def test_proof_contract_rejects_identity_mismatches_at_all_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(chr(0x400 + index) for index in range(40)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("a"),
                    view=view,
                )
        proof = report.metadata.proof
        assert proof is not None
        mutations = (
            ("kth-outside-universe", {"kth_record_id": 41}),
            (
                "frontier-outside-universe",
                {"kth_score": 0.1, "unscored_possible_record_id": 41},
            ),
            ("seed-stage-mismatch", {"seed_unique_count": 1}),
            (
                "kth-not-scored",
                {"kth_score": 0.1, "kth_record_id": 1},
            ),
            (
                "frontier-is-scored",
                {"kth_score": 0.1, "unscored_possible_record_id": 40},
            ),
        )
        encoded = contract_to_json(report)
        for name, changes in mutations:
            with self.subTest(name=name, boundary="constructor"):
                with self.assertRaises(ValueError):
                    bad_proof = replace(proof, **changes)
                    bad_metadata = replace(report.metadata, proof=bad_proof)
                    _ = replace(report, metadata=bad_metadata)
            with self.subTest(name=name, boundary="decode"):
                payload = json.loads(encoded)
                payload["payload"]["metadata"]["proof"].update(changes)
                with self.assertRaises(ValueError):
                    _ = contract_from_json(json.dumps(payload))
            with self.subTest(name=name, boundary="encode"):
                forged_proof = _unchecked_replace(proof, **changes)
                forged_metadata = _unchecked_replace(
                    report.metadata,
                    proof=forged_proof,
                )
                forged_report = _unchecked_replace(
                    report,
                    metadata=forged_metadata,
                )
                with self.assertRaises(ValueError):
                    _ = contract_to_json(forged_report)

    def test_zero_and_short_proof_corpora_remain_strictly_valid(self) -> None:
        for total_record_count in (0, 2):
            with self.subTest(total_record_count=total_record_count):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    if total_record_count:
                        store = _store(
                            root,
                            ("proof target", "other source"),
                            fts5_available=False,
                        )
                    else:
                        stage = _stage(root)
                        with patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=False,
                        ):
                            initialize_stage_schema(
                                stage,
                                canonical_store_id="store.primary",
                            )
                            store = SQLiteTMStore(
                                stage,
                                canonical_store_id="store.primary",
                            )
                    with store.query_lease() as view:
                        _fuzzy, report = prove_and_score_fuzzy_candidates(
                            resource_id="tm.primary",
                            resource_order=0,
                            query=_query("proof target"),
                            view=view,
                        )
                proof = report.metadata.proof
                assert proof is not None
                self.assertEqual(proof.total_record_count, total_record_count)
                self.assertTrue(proof.threshold_closed)
                self.assertTrue(proof.top_k_closed)
                self.assertIsNone(proof.kth_record_id)
                self.assertIsNone(proof.unscored_possible_record_id)
                self.assertEqual(contract_from_json(contract_to_json(report)), report)

    def test_zero_overlap_true_top_k_closes_by_record_id_tie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(chr(0x400 + index) for index in range(40)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("a"),
                    view=view,
                    retriever=CandidateRetriever(),
                    scorer=SimilarityScorerV1(),
                )

        self.assertEqual(fuzzy.accepted, ())
        self.assertIsNotNone(report.metadata.proof)
        proof = report.metadata.proof
        assert proof is not None
        self.assertTrue(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertEqual(proof.scored_count, 10)
        self.assertEqual(proof.kth_score, 0.0)
        self.assertEqual(proof.kth_record_id, 31)
        self.assertIn(CandidateStage.BOUND_PROOF, tuple(
            stage.stage for stage in report.metadata.stages
        ))

    def test_threshold_equality_remains_eligible_and_forces_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                ("CANDIDATE 0",) * 12,
                fts5_available=True,
            )
            with store.query_lease() as view:
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("candidate 0", threshold=1.0),
                    view=view,
                    retriever=CandidateRetriever(),
                    scorer=SimilarityScorerV1(),
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertTrue(proof.threshold_closed)
        self.assertEqual(proof.unscored_count, 0)
        self.assertIn(1, tuple(result.record_id for result in _fuzzy.accepted))

    def test_budget_exhaustion_is_stable_and_returns_no_fuzzy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple("same folded source" for _ in range(2049)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^CANDIDATE.PROOF_BUDGET_EXHAUSTED$",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("same folded source"),
                        view=view,
                        retriever=CandidateRetriever(),
                        scorer=SimilarityScorerV1(),
                    )

    def test_understated_or_missing_proof_facts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                ("proof target", "other source"),
                fts5_available=False,
            )
            with store.query_lease() as view:
                snapshot = view.candidate_proof_snapshot(
                    folded_query="proof target",
                    seed_limit=32,
                )
                first = snapshot.blocks[0]
                understated = replace(
                    snapshot,
                    blocks=(replace(first, character_intersection_upper=0),),
                )
                missing = replace(
                    snapshot,
                    blocks=(),
                )
                for malformed in (understated, missing):
                    with self.subTest(malformed=malformed):
                        with patch.object(
                            SQLiteTMQueryView,
                            "candidate_proof_snapshot",
                            return_value=malformed,
                        ):
                            with self.assertRaisesRegex(
                                Exception,
                                "STORE.CANDIDATE_PROOF_INVALID",
                            ):
                                prove_and_score_fuzzy_candidates(
                                    resource_id="tm.primary",
                                    resource_order=0,
                                    query=_query("proof target"),
                                    view=view,
                                )

    def test_generation_change_after_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                tuple(f"candidate {index}" for index in range(20)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                with self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_STALE",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("candidate 0"),
                        view=view,
                        scorer=_AppendingScorer(store, root),
                    )

    def test_budget_failure_preserves_exact_context_and_other_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed_store = _activated_store(
                root / "failed",
                ("same folded source",) * 2049,
                resource_id="tm.failed",
                fts5_available=False,
            )
            healthy_store = _activated_store(
                root / "healthy",
                ("same folded source", "near folded source"),
                resource_id="tm.healthy",
                fts5_available=False,
            )
            query = TMQuery(
                query_source="same folded source",
                speaker_raw="speaker",
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=0.60,
                limit=10,
                resource_order=("tm.failed", "tm.healthy"),
            )
            report = TMRetrievalService(
                capability_publisher=_retrieval_capability_publisher(),
            ).query(
                (
                    TMResourceHandle("tm.failed", failed_store, True, True, True, 0),
                    TMResourceHandle("tm.healthy", healthy_store, True, True, True, 1),
                ),
                query,
            )

        self.assertEqual(
            tuple((failure.resource_id, failure.stage, failure.error_code)
                  for failure in report.resource_failures),
            (("tm.failed", "PROOF", "CANDIDATE.PROOF_BUDGET_EXHAUSTED"),),
        )
        self.assertEqual(
            {metadata.resource_id for metadata in report.resource_metadata},
            {"tm.failed", "tm.healthy"},
        )
        self.assertTrue(any(
            result.resource_id == "tm.failed"
            and result.match_type in {TMMatchType.EXACT, TMMatchType.CONTEXT}
            for result in report.results
        ))
        self.assertTrue(any(
            result.resource_id == "tm.healthy"
            for result in report.results
        ))
        self.assertFalse(any(
            result.resource_id == "tm.failed"
            and result.match_type is TMMatchType.FUZZY
            for result in report.results
        ))

    def test_closed_fuzzy_gate_reads_no_candidate_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _activated_store(
                Path(temporary) / "closed",
                ("same folded source",),
                resource_id="tm.closed",
                fts5_available=False,
            )
            query = TMQuery(
                query_source="same folded source",
                speaker_raw="speaker",
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=0.60,
                limit=10,
                resource_order=("tm.closed",),
            )
            with patch.object(
                SQLiteTMQueryView,
                "candidate_proof_snapshot",
                side_effect=AssertionError("closed gate read proof"),
            ):
                report = TMRetrievalService(
                    capability_publisher=_retrieval_capability_publisher(
                        fuzzy_core_open=False,
                        fts5_open=False,
                        gram_open=False,
                    ),
                ).query(
                    (TMResourceHandle("tm.closed", store, True, True, True, 0),),
                    query,
                )
        self.assertEqual(report.resource_failures, ())
        self.assertFalse(report.resource_metadata[0].recall.fuzzy_available)

    def test_service_health_rejects_coordinated_proof_index_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tampered"
            store = _activated_store(
                root,
                ("proof target",) + tuple(
                    f"unrelated row {index}" for index in range(300)
                ),
                resource_id="tm.tampered",
                fts5_available=False,
            )
            database = root / "source.jsonl.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM tm_gram WHERE record_id = 1")
                connection.execute("UPDATE tm_record SET source_fold_length = 100")
                connection.execute(
                    "UPDATE tm_candidate_block SET "
                    "min_source_fold_length = 100, max_source_fold_length = 100"
                )
                connection.execute(
                    "DELETE FROM tm_gram_block_max WHERE block_id = 0"
                )
                connection.execute(
                    "INSERT INTO tm_gram_block_max("
                    "block_id, gram_size, gram, max_term_frequency) "
                    "SELECT 0, gram_size, gram, MAX(term_frequency) "
                    "FROM tm_gram WHERE record_id BETWEEN 1 AND 256 "
                    "AND gram_size IN (1, 2) "
                    "GROUP BY gram_size, gram"
                )
                connection.commit()
            finally:
                connection.close()
            query = TMQuery(
                query_source="proof targat",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=0.60,
                limit=10,
                resource_order=("tm.tampered",),
            )
            report = TMRetrievalService(
                capability_publisher=_retrieval_capability_publisher(),
            ).query(
                (
                    TMResourceHandle(
                        "tm.tampered", store, True, True, True, 0
                    ),
                ),
                query,
            )

        self.assertEqual(report.results, ())
        self.assertEqual(report.resource_metadata, ())
        self.assertEqual(
            tuple(
                (failure.stage, failure.error_code)
                for failure in report.resource_failures
            ),
            (("HEALTH", "STORE.CANDIDATE_INDEX_INVALID"),),
        )

    def test_service_health_rejects_coordinated_fold_root_tamper(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "fold-root-tampered"
                    store = _activated_store(
                        root,
                        ("proof target",) + tuple(
                            f"unrelated row {index}" for index in range(300)
                        ),
                        resource_id="tm.tampered",
                        fts5_available=fts5_available,
                    )
                    database = root / "source.jsonl.sqlite3"
                    forged_fold = "forged source"
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            "UPDATE tm_record SET source_fold_v1 = ?, "
                            "source_fold_length = ? WHERE record_id = 1",
                            (forged_fold, len(forged_fold)),
                        )
                        connection.execute(
                            "DELETE FROM tm_gram WHERE record_id = 1"
                        )
                        required_sizes = (
                            (1, 2) if fts5_available else (1, 2, 3)
                        )
                        connection.executemany(
                            "INSERT INTO tm_gram("
                            "record_id, gram_size, gram, term_frequency) "
                            "VALUES (1, ?, ?, ?)",
                            tuple(
                                (size, gram, frequency)
                                for size in required_sizes
                                for gram, frequency in character_ngram_frequencies(
                                    forged_fold, size
                                )
                            ),
                        )
                        if fts5_available:
                            connection.execute(
                                "DELETE FROM tm_fts WHERE record_id = 1"
                            )
                            connection.execute(
                                "INSERT INTO tm_fts(source_fold_v1, record_id) "
                                "VALUES (?, 1)",
                                (forged_fold,),
                            )
                        connection.execute(
                            "UPDATE tm_candidate_block SET "
                            "min_source_fold_length = ("
                            "SELECT MIN(source_fold_length) FROM tm_record "
                            "WHERE record_id BETWEEN 1 AND 256), "
                            "max_source_fold_length = ("
                            "SELECT MAX(source_fold_length) FROM tm_record "
                            "WHERE record_id BETWEEN 1 AND 256) "
                            "WHERE block_id = 0"
                        )
                        connection.execute(
                            "DELETE FROM tm_gram_block_max WHERE block_id = 0"
                        )
                        connection.execute(
                            "INSERT INTO tm_gram_block_max("
                            "block_id, gram_size, gram, max_term_frequency) "
                            "SELECT 0, gram_size, gram, MAX(term_frequency) "
                            "FROM tm_gram WHERE record_id BETWEEN 1 AND 256 "
                            "AND gram_size IN (1, 2) "
                            "GROUP BY gram_size, gram"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    report = TMRetrievalService(
                        capability_publisher=_retrieval_capability_publisher(),
                    ).query(
                        (
                            TMResourceHandle(
                                "tm.tampered", store, True, True, True, 0
                            ),
                        ),
                        TMQuery(
                            query_source="proof targat",
                            speaker_raw=None,
                            context_prev_raw=None,
                            context_next_raw=None,
                            minimum_similarity=0.60,
                            limit=10,
                            resource_order=("tm.tampered",),
                        ),
                    )

                self.assertEqual(report.results, ())
                self.assertEqual(report.resource_metadata, ())
                self.assertEqual(
                    tuple(
                        (failure.stage, failure.error_code)
                        for failure in report.resource_failures
                    ),
                    (("HEALTH", "STORE.CANDIDATE_INDEX_INVALID"),),
                )


if __name__ == "__main__":
    unittest.main()
