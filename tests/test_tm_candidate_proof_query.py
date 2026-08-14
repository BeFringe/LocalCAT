from __future__ import annotations

from dataclasses import fields, replace
import itertools
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tm_sqlite_store

from tm_candidate_index import (
    CandidateRetriever,
    _dense_phase1_upper_bound,
    _dense_phase2_upper_bound,
    _should_use_dense_traversal,
)
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
    SQLiteCandidateProofDensePhase2,
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


class _CountingScorer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def score(self, query: str, candidate: str):
        self.calls.append((query, candidate))
        return SimilarityScorerV1().score(query, candidate)


def _counting_production_score(calls: list[tuple[str, str]]):
    original = SimilarityScorerV1.score

    def score(owner: SimilarityScorerV1, query: str, candidate: str):
        calls.append((query, candidate))
        return original(owner, query, candidate)

    return score


class CandidateProofQueryTests(unittest.TestCase):
    def test_dense_crossover_includes_flat_top_k_frontiers_below_threshold(
        self,
    ) -> None:
        self.assertTrue(
            _should_use_dense_traversal(
                (0.46,) * 391,
                minimum_similarity=0.60,
            )
        )
        self.assertFalse(
            _should_use_dense_traversal(
                (0.93,) * 8 + (0.29,) * 383,
                minimum_similarity=0.60,
            )
        )

    def test_two_phase_dense_bounds_order_true_score_u2_u1(self) -> None:
        scorer = SimilarityScorerV1()
        alphabet = "ab"
        values = tuple(
            "".join(characters)
            for length in range(1, 6)
            for characters in itertools.product(
                alphabet,
                repeat=length,
            )
        )
        for query_source in values:
            query_characters = dict(
                character_ngram_frequencies(query_source, 1)
            )
            query_bigrams = dict(
                character_ngram_frequencies(query_source, 2)
            )
            for candidate_source in values:
                candidate_characters = dict(
                    character_ngram_frequencies(candidate_source, 1)
                )
                candidate_bigrams = dict(
                    character_ngram_frequencies(candidate_source, 2)
                )
                character_intersection = sum(
                    min(frequency, candidate_characters.get(gram, 0))
                    for gram, frequency in query_characters.items()
                )
                bigram_intersection = sum(
                    min(frequency, candidate_bigrams.get(gram, 0))
                    for gram, frequency in query_bigrams.items()
                )
                phase1 = _dense_phase1_upper_bound(
                    query_length=len(query_source),
                    record_length=len(candidate_source),
                    bigram_intersection=bigram_intersection,
                )
                phase2 = _dense_phase2_upper_bound(
                    query_length=len(query_source),
                    record_length=len(candidate_source),
                    character_intersection=character_intersection,
                    bigram_intersection=bigram_intersection,
                )
                true_score = scorer.score(
                    query_source,
                    candidate_source,
                ).final_similarity
                self.assertLessEqual(true_score, phase2 + 1e-12)
                self.assertLessEqual(phase2, phase1 + 1e-12)

    def test_two_phase_dense_bounds_hold_for_fixed_random_vectors(self) -> None:
        generator = random.Random(0xC0FFEE)
        scorer = SimilarityScorerV1()
        for _index in range(2_000):
            query_source = "".join(
                generator.choice("abcde")
                for _ in range(generator.randint(1, 40))
            )
            candidate_source = "".join(
                generator.choice("abcde")
                for _ in range(generator.randint(1, 40))
            )
            query_characters = dict(character_ngram_frequencies(query_source, 1))
            query_bigrams = dict(character_ngram_frequencies(query_source, 2))
            candidate_characters = dict(
                character_ngram_frequencies(candidate_source, 1)
            )
            candidate_bigrams = dict(
                character_ngram_frequencies(candidate_source, 2)
            )
            character_intersection = sum(
                min(frequency, candidate_characters.get(gram, 0))
                for gram, frequency in query_characters.items()
            )
            bigram_intersection = sum(
                min(frequency, candidate_bigrams.get(gram, 0))
                for gram, frequency in query_bigrams.items()
            )
            phase1 = _dense_phase1_upper_bound(
                query_length=len(query_source),
                record_length=len(candidate_source),
                bigram_intersection=bigram_intersection,
            )
            phase2 = _dense_phase2_upper_bound(
                query_length=len(query_source),
                record_length=len(candidate_source),
                character_intersection=character_intersection,
                bigram_intersection=bigram_intersection,
            )
            true_score = scorer.score(
                query_source,
                candidate_source,
            ).final_similarity
            self.assertLessEqual(true_score, phase2 + 1e-12)
            self.assertLessEqual(phase2, phase1 + 1e-12)

    def test_two_phase_dense_bounds_cover_single_character_and_repeats(
        self,
    ) -> None:
        self.assertEqual(
            _dense_phase1_upper_bound(
                query_length=1,
                record_length=1,
                bigram_intersection=0,
            ),
            1.0,
        )
        scorer = SimilarityScorerV1()
        true_score = scorer.score("aaaaa", "aaaba").final_similarity
        phase1 = _dense_phase1_upper_bound(
            query_length=5,
            record_length=5,
            bigram_intersection=2,
        )
        phase2 = _dense_phase2_upper_bound(
            query_length=5,
            record_length=5,
            character_intersection=4,
            bigram_intersection=2,
        )
        self.assertLessEqual(true_score, phase2 + 1e-12)
        self.assertLessEqual(phase2, phase1 + 1e-12)

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
                    self.assertEqual(opened, [1])
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

        legacy = json.loads(encoded)
        legacy_proof = legacy["payload"]["metadata"]["proof"]
        legacy_proof["proof_version"] = "candidate-proof-query-v1"
        legacy_proof["scored_count"] = legacy_proof.pop(
            "accounted_identity_count"
        )
        legacy_proof["unscored_count"] = legacy_proof.pop(
            "unscored_identity_count"
        )
        for field_name in (
            "scorer_invocation_count",
            "scanned_block_count",
            "traversal_mode",
            "traversal_version",
        ):
            legacy_proof.pop(field_name)
        with self.assertRaises(ValueError):
            contract_from_json(json.dumps(legacy))

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
            (
                "invocations-exceed-accounted",
                {
                    "scorer_invocation_count": (
                        proof.accounted_identity_count + 1
                    )
                },
            ),
            (
                "identity-conservation-drift",
                {
                    "unscored_identity_count": (
                        proof.unscored_identity_count + 1
                    )
                },
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
                )

        self.assertEqual(fuzzy.accepted, ())
        self.assertIsNotNone(report.metadata.proof)
        proof = report.metadata.proof
        assert proof is not None
        self.assertTrue(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertEqual(proof.scorer_invocation_count, 10)
        self.assertEqual(proof.accounted_identity_count, 10)
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
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertTrue(proof.threshold_closed)
        self.assertEqual(proof.unscored_identity_count, 0)
        self.assertIn(1, tuple(result.record_id for result in _fuzzy.accepted))

    def test_2049_identities_sharing_one_fold_use_one_scorer_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple("same folded source" for _ in range(2049)),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            phase2_calls = 0
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2

            def count_phase2(view: SQLiteTMQueryView, **kwargs):
                nonlocal phase2_calls
                phase2_calls += 1
                return original_phase2(view, **kwargs)

            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase2",
                new=count_phase2,
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("same folded source", threshold=1.0),
                    view=view,
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(len(calls), 1)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 2049)
        self.assertEqual(proof.unscored_identity_count, 0)
        self.assertEqual(len(report.candidates), 2049)
        self.assertEqual(phase2_calls, 1)

    def test_2049_distinct_folds_exhaust_after_exactly_2048_calls(self) -> None:
        query_source = "Shared source sentence 6 about recordx"
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(
                    f"Shared source sentence 6 about record{index}"
                    for index in range(2049)
                ),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            phase2_calls = 0
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2

            def count_phase2(view: SQLiteTMQueryView, **kwargs):
                nonlocal phase2_calls
                phase2_calls += 1
                return original_phase2(view, **kwargs)

            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase2",
                new=count_phase2,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^CANDIDATE.PROOF_BUDGET_EXHAUSTED$",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query(query_source),
                        view=view,
                    )

        self.assertEqual(len(calls), 2048)
        self.assertEqual(phase2_calls, 1)

    def test_3000_identities_in_300_folds_close_with_300_calls(self) -> None:
        query_source = "Shared source sentence 6 about recordx"
        sources = tuple(
            f"Shared source sentence 6 about record{fold_index}"
            for fold_index in range(300)
            for _multiplicity in range(10)
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                sources,
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(len(calls), 300)
        self.assertEqual(proof.scorer_invocation_count, 300)
        self.assertEqual(proof.accounted_identity_count, 3000)
        self.assertEqual(proof.unscored_identity_count, 0)
        refinement = proof.refinement
        assert refinement is not None
        self.assertEqual(refinement.a0_accounted_identity_count, 10)
        self.assertEqual(
            refinement.refinement_request_count,
            refinement.refinement_returned_count,
        )
        self.assertEqual(
            refinement.a0_accounted_identity_count
            + refinement.p1_unscored_identity_count
            + refinement.r_refinement_identity_count,
            proof.total_record_count,
        )
        self.assertEqual(
            refinement.a1_accounted_identity_count
            + refinement.p2_unscored_identity_count,
            refinement.r_refinement_identity_count,
        )

    def test_partial_final_batch_stops_at_the_closed_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                (("candidate 0",) * 11)
                + tuple(chr(0x400 + index) for index in range(31)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("candidate x"),
                    view=view,
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 11)
        self.assertEqual(proof.unscored_identity_count, 31)
        self.assertTrue(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertTrue(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)

    def test_full_fold_equivalence_reuses_evidence_and_preserves_raw_rows(
        self,
    ) -> None:
        sources = ("CAFÉ SOURCE", "cafe\u0301 source", "Café Source")
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary), sources, fts5_available=False)
            calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ):
                fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("café source!", threshold=0.0),
                    view=view,
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(len(calls), 1)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 3)
        self.assertEqual(
            {result.matched_source for result in fuzzy.accepted},
            set(sources),
        )
        serialized = contract_to_json(report)
        self.assertNotIn("CAFÉ SOURCE", serialized)
        self.assertNotIn("cafe\\u0301 source", serialized)

    def test_equal_length_character_and_bigram_facts_do_not_authorize_reuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                ("abaca", "acaba"),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("adada", threshold=0.0),
                    view=view,
                )

        proof = report.metadata.proof
        assert proof is not None
        self.assertEqual(len(calls), 2)
        self.assertEqual(proof.scorer_invocation_count, 2)
        self.assertEqual(proof.accounted_identity_count, 2)

    def test_dense_and_sparse_proof_modes_are_semantically_identical(self) -> None:
        sources = tuple(
            f"shared source sentence about record{index % 30}"
            for index in range(600)
        )
        query = _query("shared source sentence about recordx")
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    store = _store(
                        Path(temporary),
                        sources,
                        fts5_available=fts5_available,
                    )
                    reports = []
                    results = []
                    for dense in (False, True):
                        with store.query_lease() as view, patch(
                            "tm_candidate_index._should_use_dense_traversal",
                            return_value=dense,
                        ):
                            fuzzy, report = prove_and_score_fuzzy_candidates(
                                resource_id="tm.primary",
                                resource_order=0,
                                query=query,
                                view=view,
                            )
                        reports.append(report)
                        results.append(tuple(
                            (row.record_id, row.similarity)
                            for row in fuzzy.accepted
                        ))

                self.assertEqual(results[0], results[1])
                sparse = reports[0].metadata.proof
                dense = reports[1].metadata.proof
                assert sparse is not None and dense is not None
                self.assertEqual(sparse.traversal_mode, "SPARSE")
                self.assertEqual(dense.traversal_mode, "DENSE")
                self.assertEqual(
                    sparse.scorer_invocation_count,
                    dense.scorer_invocation_count,
                )

    def test_dense_equal_score_record_id_tie_closes_only_below_k0(self) -> None:
        sources = tuple(chr(0x400 + index) for index in range(600))
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary), sources, fts5_available=False)
            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("a"),
                    view=view,
                )
        proof = report.metadata.proof
        assert proof is not None and proof.refinement is not None
        refinement = proof.refinement
        self.assertEqual(proof.kth_score, 0.0)
        self.assertEqual(proof.kth_record_id, 591)
        self.assertEqual(refinement.k0_score, 0.0)
        self.assertEqual(refinement.k0_record_id, 591)
        self.assertEqual(refinement.p1_unscored_identity_count, 0)
        self.assertEqual(refinement.p2_unscored_identity_count, 590)
        self.assertEqual(refinement.p2_max_upper_bound, 0.0)
        self.assertEqual(refinement.p2_possible_record_id, 590)

    def test_dense_mixed_frontier_contract_rejects_count_and_tie_forgery(
        self,
    ) -> None:
        sources = tuple("abcdefghij" + ("z" * 90) for _ in range(600))
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary), sources, fts5_available=False)
            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("abcdefghij"),
                    view=view,
                )
        proof = report.metadata.proof
        assert proof is not None and proof.refinement is not None
        refinement = proof.refinement
        self.assertEqual(refinement.p1_unscored_identity_count, 590)
        encoded = contract_to_json(report)
        nested_mutations = (
            {
                "refinement_request_count": (
                    refinement.refinement_request_count + 1
                )
            },
            {
                "refinement_returned_count": (
                    refinement.refinement_returned_count + 1
                )
            },
            {
                "p1_unscored_identity_count": (
                    refinement.p1_unscored_identity_count + 1
                )
            },
        )
        for changes in nested_mutations:
            with self.subTest(changes=changes, boundary="constructor"):
                with self.assertRaises(ValueError):
                    _ = replace(
                        proof,
                        refinement=replace(refinement, **changes),
                    )
            with self.subTest(changes=changes, boundary="decode"):
                payload = json.loads(encoded)
                payload["payload"]["metadata"]["proof"]["refinement"].update(
                    changes
                )
                with self.assertRaises(ValueError):
                    contract_from_json(json.dumps(payload))

        assert proof.kth_score is not None and proof.kth_record_id is not None
        with self.assertRaises(ValueError):
            _ = replace(
                proof,
                unscored_max_upper_bound=proof.kth_score,
                unscored_possible_record_id=proof.kth_record_id,
                refinement=replace(
                    refinement,
                    p1_max_upper_bound=proof.kth_score,
                    p1_possible_record_id=proof.kth_record_id,
                ),
            )

    def test_dense_fact_transaction_commits_before_scorer_and_append_is_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                tuple(
                    f"shared source sentence about record{index % 30}"
                    for index in range(600)
                ),
                fts5_available=False,
            )
            original = SimilarityScorerV1.score
            phase1_calls = 0
            phase2_calls = 0
            sparse_calls = 0
            append_elapsed = 0.0
            appended = False

            def append_then_score(
                owner: SimilarityScorerV1,
                query: str,
                candidate: str,
            ):
                nonlocal append_elapsed, appended
                if not appended:
                    appended = True
                    started = time.perf_counter()
                    store.append_batch(
                        batch_id="import.concurrent-proof",
                        kind="import",
                        drafts=(_draft("concurrent row", 999),),
                        source_digest="8" * 64,
                        source_path=(root / "concurrent.jsonl").resolve(),
                    )
                    append_elapsed = time.perf_counter() - started
                return original(owner, query, candidate)

            original_phase1 = SQLiteTMQueryView.candidate_proof_dense_phase1
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2
            original_sparse = SQLiteTMQueryView.candidate_proof_block_records

            def count_phase1(view: SQLiteTMQueryView, **kwargs):
                nonlocal phase1_calls
                phase1_calls += 1
                return original_phase1(view, **kwargs)

            def count_phase2(view: SQLiteTMQueryView, **kwargs):
                nonlocal phase2_calls
                phase2_calls += 1
                return original_phase2(view, **kwargs)

            def count_sparse(view: SQLiteTMQueryView, **kwargs):
                nonlocal sparse_calls
                sparse_calls += 1
                return original_sparse(view, **kwargs)

            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase1",
                new=count_phase1,
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase2",
                new=count_phase2,
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_block_records",
                new=count_sparse,
            ), patch.object(
                SimilarityScorerV1,
                "score",
                new=append_then_score,
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_STALE",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query(
                            "shared source sentence about recordx"
                        ),
                        view=view,
                    )

        self.assertEqual(phase1_calls, 1)
        self.assertEqual(phase2_calls, 1)
        self.assertEqual(sparse_calls, 0)
        self.assertLess(append_elapsed, 1.0)

    def test_dense_query_maxima_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(f"candidate {index}" for index in range(2_100)),
                fts5_available=False,
            )
            with store.query_lease() as view:
                snapshot = view.candidate_proof_snapshot(
                    folded_query="candidate x",
                    seed_limit=256,
                )
                forged = replace(snapshot, query_maxima_digest="0" * 64)
                with patch.object(
                    SQLiteTMQueryView,
                    "candidate_proof_snapshot",
                    return_value=forged,
                ), patch(
                    "tm_candidate_index._should_use_dense_traversal",
                    return_value=True,
                ), self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_INVALID",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("candidate x"),
                        view=view,
                    )

    def test_dense_refinement_response_is_strict_ordered_and_bound(self) -> None:
        sources = tuple(
            f"shared source sentence about record{index % 30}"
            for index in range(600)
        )
        mutations = ("missing", "duplicate", "order", "outside", "extra", "binding")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                store = _store(Path(temporary), sources, fts5_available=False)
                original = SQLiteTMQueryView.candidate_proof_dense_phase2

                def mutate(view: SQLiteTMQueryView, **kwargs):
                    response = original(view, **kwargs)
                    ids = response.record_ids
                    characters = response.character_multiset_intersections
                    self.assertGreaterEqual(len(ids), 2)
                    if mutation == "missing":
                        return replace(
                            response,
                            record_ids=ids[:-1],
                            character_multiset_intersections=characters[:-1],
                        )
                    if mutation == "duplicate":
                        return replace(response, record_ids=(ids[0], ids[0], *ids[2:]))
                    if mutation == "order":
                        return replace(response, record_ids=(ids[1], ids[0], *ids[2:]))
                    if mutation == "outside":
                        return replace(
                            response,
                            record_ids=(len(sources) + 1, *ids[1:]),
                        )
                    if mutation == "extra":
                        return replace(
                            response,
                            record_ids=(*ids, 1),
                            character_multiset_intersections=(*characters, 0),
                        )
                    return replace(response, binding_digest="0" * 64)

                with store.query_lease() as view, patch(
                    "tm_candidate_index._should_use_dense_traversal",
                    return_value=True,
                ), patch.object(
                    SQLiteTMQueryView,
                    "candidate_proof_dense_phase2",
                    new=mutate,
                ), self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_INVALID",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("shared source sentence about recordx"),
                        view=view,
                    )

    def test_append_during_dense_phase2_is_stale_without_scorer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                tuple(
                    f"shared source sentence about record{index % 30}"
                    for index in range(600)
                ),
                fts5_available=False,
            )
            original_validate = tm_sqlite_store._validate_candidate_proof_dense_binding
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2
            validation_calls = 0
            writer: threading.Thread | None = None
            writer_errors: list[BaseException] = []

            def append() -> None:
                try:
                    store.append_batch(
                        batch_id="import.concurrent-phase2",
                        kind="import",
                        drafts=(_draft("concurrent phase2 row", 999),),
                        source_digest="7" * 64,
                        source_path=(root / "concurrent-phase2.jsonl").resolve(),
                    )
                except BaseException as error:
                    writer_errors.append(error)

            def race_validate(connection, **kwargs):
                nonlocal validation_calls, writer
                binding = original_validate(connection, **kwargs)
                validation_calls += 1
                if validation_calls == 2:
                    writer = threading.Thread(target=append)
                    writer.start()
                return binding

            def join_phase2(view: SQLiteTMQueryView, **kwargs):
                response = original_phase2(view, **kwargs)
                assert writer is not None
                writer.join(timeout=2.0)
                self.assertFalse(writer.is_alive())
                return response

            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch(
                "tm_sqlite_store._validate_candidate_proof_dense_binding",
                new=race_validate,
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase2",
                new=join_phase2,
            ), self.assertRaisesRegex(
                Exception,
                "STORE.CANDIDATE_PROOF_STALE",
            ):
                prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("shared source sentence about recordx"),
                    view=view,
                )
            self.assertEqual(writer_errors, [])

    def test_append_after_phase2_during_scorer_is_stale_and_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                tuple(
                    f"shared source sentence about record{index % 30}"
                    for index in range(600)
                ),
                fts5_available=False,
            )
            original = SimilarityScorerV1.score
            calls = 0
            append_elapsed = 0.0

            def append_on_phase2_score(
                owner: SimilarityScorerV1,
                query: str,
                candidate: str,
            ):
                nonlocal calls, append_elapsed
                calls += 1
                if calls == 11:
                    started = time.perf_counter()
                    store.append_batch(
                        batch_id="import.concurrent-after-phase2",
                        kind="import",
                        drafts=(_draft("concurrent scorer row", 999),),
                        source_digest="6" * 64,
                        source_path=(root / "concurrent-scorer.jsonl").resolve(),
                    )
                    append_elapsed = time.perf_counter() - started
                return original(owner, query, candidate)

            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                SimilarityScorerV1,
                "score",
                new=append_on_phase2_score,
            ), self.assertRaisesRegex(
                Exception,
                "STORE.CANDIDATE_PROOF_STALE",
            ):
                prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("shared source sentence about recordx"),
                    view=view,
                )
            self.assertGreaterEqual(calls, 11)
            self.assertLess(append_elapsed, 1.0)

    def test_injected_scorer_uses_nonproof_path_without_fold_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _activated_store(
                Path(temporary) / "custom",
                ("CAFÉ SOURCE", "cafe\u0301 source"),
                resource_id="tm.custom",
                fts5_available=False,
            )
            scorer = _CountingScorer()
            report = TMRetrievalService(
                scorer=scorer,
                capability_publisher=_retrieval_capability_publisher(),
            ).query(
                (TMResourceHandle("tm.custom", store, True, True, True, 0),),
                TMQuery(
                    query_source="café source!",
                    speaker_raw=None,
                    context_prev_raw=None,
                    context_next_raw=None,
                    minimum_similarity=0.0,
                    limit=10,
                    resource_order=("tm.custom",),
                ),
            )

        self.assertEqual(len(scorer.calls), 2)
        self.assertIsNone(report.resource_metadata[0].recall.proof)

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
                original_score = SimilarityScorerV1.score
                appended = False

                def append_then_score(scorer, query_source, candidate_source):
                    nonlocal appended
                    if not appended:
                        appended = True
                        store.append_batch(
                            batch_id="import.concurrent-proof",
                            kind="import",
                            drafts=(_draft("concurrent row", 999),),
                            source_digest="8" * 64,
                            source_path=(root / "concurrent.jsonl").resolve(),
                        )
                    return original_score(scorer, query_source, candidate_source)

                with patch.object(
                    SimilarityScorerV1,
                    "score",
                    new=append_then_score,
                ), self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_STALE",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("candidate 0"),
                        view=view,
                    )

    def test_budget_failure_preserves_exact_context_and_other_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed_store = _activated_store(
                root / "failed",
                tuple(f"distinct folded source {index}" for index in range(2049)),
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
                query_source="distinct folded source 0",
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
