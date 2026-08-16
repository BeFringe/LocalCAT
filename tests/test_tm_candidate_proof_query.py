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

import tm_candidate_index
import tm_sqlite_store

from text_matcher import fold_text_v1
from tm_candidate_index import (
    CandidateRetriever,
    _balanced_lcs_partition_v1,
    _dense_phase1_upper_bound,
    _dense_phase2_upper_bound,
    _dense_phase3_upper_bound,
    _dense_u2_upper_bound,
    _exact_lcs_length,
    _partition_additive_lcs_distance_v1,
    _should_use_dense_traversal,
)
from tm_contracts import (
    CandidateProofMetadata,
    CandidateProofRefinementMetadata,
    CandidateRetrievalReport,
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
    receipt = getattr(value, "_receipt", None)
    if receipt is not None:
        object.__setattr__(forged, "_receipt", receipt)
    return forged


def _current_proof(report: CandidateRetrievalReport) -> CandidateProofMetadata:
    proof = report.metadata.proof
    if type(proof) is not CandidateProofMetadata:
        raise AssertionError("expected current proof-query-v3 metadata")
    return proof


def _current_refinement(
    proof: CandidateProofMetadata,
) -> CandidateProofRefinementMetadata:
    refinement = proof.refinement
    if type(refinement) is not CandidateProofRefinementMetadata:
        raise AssertionError("expected current dense refinement metadata")
    return refinement


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
        ).build_mutable_stage(fixture.resolve())
        assert build.mutable_stage is not None
        sealed = StageSealer(
            registry=coordinator._sealed_registry,
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

    def test_dense_bounds_order_true_score_u4_u3_u2_u1(self) -> None:
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
                phase2 = _dense_u2_upper_bound(
                    query_length=len(query_source),
                    record_length=len(candidate_source),
                    character_intersection=character_intersection,
                    bigram_intersection=bigram_intersection,
                )
                phase3 = _dense_phase2_upper_bound(
                    query_length=len(query_source),
                    record_length=len(candidate_source),
                    lcs_length=_exact_lcs_length(
                        query_source,
                        candidate_source,
                    ),
                    bigram_intersection=bigram_intersection,
                )
                phase4 = _dense_phase3_upper_bound(
                    query_length=len(query_source),
                    record_length=len(candidate_source),
                    lcs_length=_exact_lcs_length(
                        query_source,
                        candidate_source,
                    ),
                    partition_lcs_distance=(
                        _partition_additive_lcs_distance_v1(
                            query_source,
                            candidate_source,
                        )
                    ),
                    bigram_intersection=bigram_intersection,
                )
                true_score = scorer.score(
                    query_source,
                    candidate_source,
                ).final_similarity
                self.assertLessEqual(true_score, phase4 + 1e-12)
                self.assertLessEqual(phase4, phase3 + 1e-12)
                self.assertLessEqual(phase3, phase2 + 1e-12)
                self.assertLessEqual(phase2, phase1 + 1e-12)

    def test_two_phase_dense_bounds_hold_for_fixed_random_vectors(self) -> None:
        generator = random.Random(0xC0FFEE)
        scorer = SimilarityScorerV1()
        for _index in range(2_000):
            alphabet = (
                "a",
                "b",
                "ß",
                "é",
                "e\u0301",
                "中",
                "한",
                "🙂",
            )
            query_raw = "".join(
                generator.choice(alphabet)
                for _ in range(generator.randint(1, 40))
            )
            candidate_raw = "".join(
                generator.choice(alphabet)
                for _ in range(generator.randint(1, 40))
            )
            query_source = fold_text_v1(query_raw).folded_text
            candidate_source = fold_text_v1(candidate_raw).folded_text
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
            phase2 = _dense_u2_upper_bound(
                query_length=len(query_source),
                record_length=len(candidate_source),
                character_intersection=character_intersection,
                bigram_intersection=bigram_intersection,
            )
            phase3 = _dense_phase2_upper_bound(
                query_length=len(query_source),
                record_length=len(candidate_source),
                lcs_length=_exact_lcs_length(query_source, candidate_source),
                bigram_intersection=bigram_intersection,
            )
            phase4 = _dense_phase3_upper_bound(
                query_length=len(query_source),
                record_length=len(candidate_source),
                lcs_length=_exact_lcs_length(query_source, candidate_source),
                partition_lcs_distance=_partition_additive_lcs_distance_v1(
                    query_source,
                    candidate_source,
                ),
                bigram_intersection=bigram_intersection,
            )
            true_score = scorer.score(
                query_raw,
                candidate_raw,
            ).final_similarity
            self.assertLessEqual(true_score, phase4 + 1e-12)
            self.assertLessEqual(phase4, phase3 + 1e-12)
            self.assertLessEqual(phase3, phase2 + 1e-12)
            self.assertLessEqual(phase2, phase1 + 1e-12)

    def test_fused_lcs_facts_match_independent_unicode_reference(self) -> None:
        def reference_lcs(left: str, right: str) -> int:
            prior = [0] * (len(right) + 1)
            for left_code_point in left:
                current = [0]
                for offset, right_code_point in enumerate(right, start=1):
                    current.append(
                        prior[offset - 1] + 1
                        if left_code_point == right_code_point
                        else max(prior[offset], current[-1])
                    )
                prior = current
            return prior[-1]

        vectors = (
            ("abca", "caba"),
            ("Straße", "STRASSE"),
            ("é中한🙂", "e\u0301x中한🙂"),
            ("かなカナ", "xかカナ"),
        )
        for query_raw, candidate_raw in vectors:
            with self.subTest(query=query_raw, candidate=candidate_raw):
                query = fold_text_v1(query_raw).folded_text
                candidate = fold_text_v1(candidate_raw).folded_text
                lcs_length = tm_candidate_index._ExactLCSQueryProjection(
                    query
                ).facts(
                    candidate,
                    len(candidate),
                )
                self.assertEqual(lcs_length, reference_lcs(query, candidate))

    def test_ascii_lcs_transition_memo_is_bounded_and_falls_back_exactly(
        self,
    ) -> None:
        def reference_lcs(left: str, right: str) -> int:
            prior = [0] * (len(right) + 1)
            for left_code_point in left:
                current = [0]
                for offset, right_code_point in enumerate(right, start=1):
                    current.append(
                        prior[offset - 1] + 1
                        if left_code_point == right_code_point
                        else max(prior[offset], current[-1])
                    )
                prior = current
            return prior[-1]

        query = "abcdefghij"
        generator = random.Random(0x5A7E)
        candidates = (
            "jihgfedcba",
            "a🙂j",
            "acegibdfhj",
            *(
                "".join(
                    generator.choice("abcdefghijXYZ")
                    for _offset in range(40)
                )
                for _identity in range(200)
            ),
        )
        with patch(
            "tm_candidate_index._ASCII_LCS_TRANSITION_STATE_LIMIT",
            3,
        ):
            projection = tm_candidate_index._ExactLCSQueryProjection(query)
            first = projection.facts(candidates[0], len(candidates[0]))
            self.assertTrue(projection._ascii_reset_pending)
            second = projection.facts(candidates[1], len(candidates[1]))
            self.assertFalse(projection._ascii_reset_pending)
            observed = (
                first,
                second,
                *(
                    projection.facts(candidate, len(candidate))
                    for candidate in candidates[2:]
                ),
            )

        self.assertEqual(
            observed,
            tuple(reference_lcs(query, candidate) for candidate in candidates),
        )
        self.assertTrue(projection._ascii_cache_saturated)
        self.assertLessEqual(len(projection._ascii_frontiers), 3)
        self.assertLessEqual(len(projection._ascii_frontier_bit_counts), 3)
        self.assertLessEqual(len(projection._ascii_state_by_frontier), 3)
        self.assertLessEqual(len(projection._ascii_transitions), 3)

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
        phase2 = _dense_u2_upper_bound(
            query_length=5,
            record_length=5,
            character_intersection=4,
            bigram_intersection=2,
        )
        phase3 = _dense_phase2_upper_bound(
            query_length=5,
            record_length=5,
            lcs_length=_exact_lcs_length("aaaaa", "aaaba"),
            bigram_intersection=2,
        )
        phase4 = _dense_phase3_upper_bound(
            query_length=5,
            record_length=5,
            lcs_length=_exact_lcs_length("aaaaa", "aaaba"),
            partition_lcs_distance=_partition_additive_lcs_distance_v1(
                "aaaaa",
                "aaaba",
            ),
            bigram_intersection=2,
        )
        self.assertLessEqual(true_score, phase4 + 1e-12)
        self.assertLessEqual(phase4, phase3 + 1e-12)
        self.assertLessEqual(phase3, phase2 + 1e-12)
        self.assertLessEqual(phase2, phase1 + 1e-12)
        self.assertEqual(
            _dense_phase2_upper_bound(
                query_length=1,
                record_length=1,
                lcs_length=_exact_lcs_length("a", "a"),
                bigram_intersection=0,
            ),
            1.0,
        )
        self.assertEqual(
            _dense_phase2_upper_bound(
                query_length=1,
                record_length=1,
                lcs_length=_exact_lcs_length("a", "b"),
                bigram_intersection=0,
            ),
            0.0,
        )

    def test_partition_additive_lcs_matches_naive_definition(self) -> None:
        def naive(query: str, candidate: str) -> int:
            unreachable = len(query) + len(candidate) + 1
            previous = [0] + [unreachable] * len(candidate)
            for segment in _balanced_lcs_partition_v1(query):
                previous = [
                    min(
                        previous[start]
                        + max(len(segment), boundary - start)
                        - _exact_lcs_length(
                            segment,
                            candidate[start:boundary],
                        )
                        for start in range(boundary + 1)
                    )
                    for boundary in range(len(candidate) + 1)
                ]
            return previous[-1]

        values = tuple(
            "".join(characters)
            for length in range(1, 7)
            for characters in itertools.product("ab", repeat=length)
        )
        for query_source in values:
            partition = _balanced_lcs_partition_v1(query_source)
            self.assertEqual("".join(partition), query_source)
            self.assertTrue(all(len(segment) in (1, 2) for segment in partition))
            if len(query_source) > 1:
                self.assertLess(len(partition), len(query_source))
            for candidate_source in values:
                self.assertEqual(
                    _partition_additive_lcs_distance_v1(
                        query_source,
                        candidate_source,
                    ),
                    naive(query_source, candidate_source),
                )

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
                            completion_policy="oracle_full",
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
                            completion_policy="oracle_full",
                        )
                    proof = _current_proof(report)
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
                    completion_policy="oracle_full",
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
        encoded_lower = encoded.lower()
        for forbidden in (
            "source_fold_v1",
            "lcs",
            "equivalence",
            "proof target",
            "other source",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded_lower)
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
                    completion_policy="oracle_full",
                )
        proof = _current_proof(report)
        self.assertEqual(proof.total_record_count, 40)
        self.assertEqual(proof.ranked_kth_record_id, 31)
        self.assertEqual(proof.unscored_possible_record_id, 30)

        with self.assertRaises(ValueError):
            _ = replace(proof, ranked_kth_record_id=41)
        with self.assertRaises(ValueError):
            _ = replace(
                proof,
                ranked_kth_score=0.1,
                unscored_possible_record_id=41,
            )

        with self.assertRaises(ValueError):
            _ = replace(
                report.metadata,
                proof=replace(proof, seed_unique_count=1),
            )

        mismatched_proofs = (
            replace(
                proof,
                ranked_kth_score=0.1,
                ranked_kth_record_id=1,
            ),
            replace(
                proof,
                ranked_kth_score=0.1,
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
                    completion_policy="oracle_full",
                )
        proof = _current_proof(report)
        mutations = (
            ("kth-outside-universe", {"ranked_kth_record_id": 41}),
            (
                "frontier-outside-universe",
                {
                    "ranked_kth_score": 0.1,
                    "unscored_possible_record_id": 41,
                },
            ),
            ("seed-stage-mismatch", {"seed_unique_count": 1}),
            (
                "kth-not-scored",
                {
                    "ranked_kth_score": 0.1,
                    "ranked_kth_record_id": 1,
                },
            ),
            (
                "frontier-is-scored",
                {
                    "ranked_kth_score": 0.1,
                    "unscored_possible_record_id": 40,
                },
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
                proof = _current_proof(report)
                self.assertEqual(proof.total_record_count, total_record_count)
                self.assertTrue(proof.threshold_closed)
                self.assertTrue(proof.top_k_closed)
                self.assertIsNone(proof.ranked_kth_record_id)
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
                    completion_policy="oracle_full",
                )

        self.assertEqual(fuzzy.accepted, ())
        self.assertIsNotNone(report.metadata.proof)
        proof = _current_proof(report)
        self.assertTrue(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertEqual(proof.scorer_invocation_count, 10)
        self.assertEqual(proof.accounted_identity_count, 10)
        self.assertEqual(proof.ranked_kth_score, 0.0)
        self.assertEqual(proof.ranked_kth_record_id, 31)
        self.assertIn(CandidateStage.BOUND_PROOF, tuple(
            stage.stage for stage in report.metadata.stages
        ))

    def test_threshold_equality_remains_unclosed_but_top_k_complete(self) -> None:
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

        proof = _current_proof(report)
        self.assertFalse(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertTrue(proof.result_complete)
        self.assertEqual(proof.accounted_identity_count, 10)
        self.assertEqual(proof.ranked_eligible_count, 10)
        self.assertEqual(proof.unscored_identity_count, 2)
        self.assertEqual(
            tuple(result.record_id for result in _fuzzy.accepted),
            tuple(range(12, 2, -1)),
        )

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

        proof = _current_proof(report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 2049)
        self.assertEqual(proof.unscored_identity_count, 0)
        self.assertEqual(len(report.candidates), 2049)
        self.assertEqual(phase2_calls, 0)

    def test_projected_2049th_invocation_batch_is_rejected_atomically(self) -> None:
        query_source = "Shared source sentence 6 about recordx"
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(
                    f"Shared source sentence 6 about record{index:04d}"
                    for index in range(2049)
                ),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            phase2_calls = 0
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2
            original_next_batch = (
                tm_candidate_index.CandidateProofSession.next_batch
            )
            last_issued_state: tuple[
                tm_candidate_index.CandidateProofSession,
                tuple[object, ...],
            ] | None = None

            def private_session_state(
                session: tm_candidate_index.CandidateProofSession,
            ) -> tuple[object, ...]:
                return (
                    dict(session._scores),
                    dict(session._ranked_scores),
                    tuple(session._observation_order),
                    frozenset(session._outstanding),
                    session._scorer_invocation_count,
                )

            def capture_next_batch(
                session: tm_candidate_index.CandidateProofSession,
            ) -> tuple[int, ...]:
                nonlocal last_issued_state
                record_ids = original_next_batch(session)
                if record_ids:
                    last_issued_state = (
                        session,
                        private_session_state(session),
                    )
                return record_ids

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
            ), patch.object(
                tm_candidate_index.CandidateProofSession,
                "next_batch",
                new=capture_next_batch,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^CANDIDATE.PROOF_BUDGET_EXHAUSTED$",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query(query_source, threshold=0.0),
                        view=view,
                        completion_policy="oracle_full",
                    )

        # Candidate exposes the exact remaining 22 slots, then issues the
        # next batch so Retrieval can reject invocation 2,049 before scoring
        # or observing any member of that batch.
        self.assertEqual(len(calls), 2048)
        self.assertEqual(phase2_calls, 1)
        self.assertIsNotNone(last_issued_state)
        assert last_issued_state is not None
        failed_session, state_before_rejection = last_issued_state
        self.assertEqual(
            private_session_state(failed_session),
            state_before_rejection,
        )

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
                    completion_policy="oracle_full",
                )

        proof = _current_proof(report)
        self.assertEqual(len(calls), 300)
        self.assertEqual(proof.scorer_invocation_count, 300)
        self.assertEqual(proof.accounted_identity_count, 3000)
        self.assertEqual(proof.unscored_identity_count, 0)
        refinement = _current_refinement(proof)
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
            refinement.p2_unscored_identity_count
            + refinement.s_post_u3_identity_count,
            refinement.r_refinement_identity_count,
        )
        self.assertEqual(
            refinement.a1_accounted_identity_count
            + refinement.p3_unscored_identity_count,
            refinement.s_post_u3_identity_count,
        )
        self.assertLessEqual(
            refinement.p3_unscored_identity_count,
            refinement.u4_evaluated_identity_count,
        )
        self.assertLessEqual(
            refinement.u4_evaluated_identity_count,
            refinement.s_post_u3_identity_count,
        )

    def test_production_conditional_closes_same_3000_identity_top10(self) -> None:
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
            production_calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(production_calls),
            ):
                production, production_report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                )
            with store.query_lease() as view:
                oracle, _oracle_report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                    completion_policy="oracle_full",
                )

        proof = _current_proof(production_report)
        self.assertEqual(len(production_calls), 1)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.ranked_eligible_count, 10)
        self.assertTrue(proof.result_complete)
        self.assertFalse(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertEqual(
            tuple(row.record_id for row in production.accepted[:10]),
            tuple(row.record_id for row in oracle.accepted[:10]),
        )

    def test_production_u3_probe_closes_without_any_u4_evaluation(self) -> None:
        query_source = "abaca"
        exact_fold_peers = tuple(
            "".join(
                character.upper() if (mask >> index) & 1 else character
                for index, character in enumerate(query_source)
            )
            for mask in range(1, 33)
        )
        sources = (("acaba",) * 558) + exact_fold_peers + (("acaba",) * 10)
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                sources,
                fts5_available=False,
            )
            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                tm_candidate_index._PartitionLCSQueryProjectionV1,
                "distance",
                side_effect=AssertionError("U4 must remain lazy"),
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                )

        proof = _current_proof(report)
        refinement = _current_refinement(proof)
        self.assertEqual(proof.scorer_invocation_count, 2)
        self.assertEqual(proof.accounted_identity_count, 42)
        self.assertFalse(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertTrue(proof.result_complete)
        self.assertEqual(refinement.a0_accounted_identity_count, 10)
        self.assertEqual(refinement.r_refinement_identity_count, 590)
        self.assertEqual(refinement.a1_accounted_identity_count, 32)
        self.assertEqual(refinement.p2_unscored_identity_count, 558)
        self.assertEqual(refinement.s_post_u3_identity_count, 32)
        self.assertEqual(refinement.u4_evaluated_identity_count, 0)
        self.assertEqual(refinement.p3_unscored_identity_count, 0)
        self.assertEqual(refinement.p2_max_upper_bound, 0.8)
        self.assertEqual(refinement.p2_possible_record_id, 558)

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

        proof = _current_proof(report)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 10)
        self.assertEqual(proof.ranked_eligible_count, 10)
        self.assertEqual(proof.unscored_identity_count, 32)
        self.assertFalse(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertTrue(proof.result_complete)

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

        proof = _current_proof(report)
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

    def test_raw_exact_high_ids_do_not_enter_dense_k0_or_ranked_kth(self) -> None:
        query_source = "alpha beta gamma"
        fuzzy_sources = tuple(
            f"{query_source} variant {index}"
            for index in range(10)
        )
        sources = fuzzy_sources + ((query_source,) * 10)
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary), sources, fts5_available=False)
            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ):
                fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source, threshold=0.0),
                    view=view,
                    completion_policy="oracle_full",
                )

        proof = _current_proof(report)
        refinement = _current_refinement(proof)
        self.assertEqual(proof.accounted_identity_count, 20)
        self.assertEqual(proof.ranked_eligible_count, 10)
        self.assertEqual(
            set(row.record_id for row in fuzzy.accepted),
            set(range(1, 11)),
        )
        self.assertIsNotNone(proof.ranked_kth_record_id)
        assert proof.ranked_kth_record_id is not None
        self.assertLessEqual(proof.ranked_kth_record_id, 10)
        self.assertEqual(
            (
                refinement.k0_score,
                refinement.k0_record_id,
            ),
            (
                proof.ranked_kth_score,
                proof.ranked_kth_record_id,
            ),
        )

    def test_same_fold_raw_exact_and_peers_keep_three_domains_distinct(self) -> None:
        query_source = "CAFÉ SOURCE"
        lower = "café source"
        peers = tuple(
            "".join(
                character.upper()
                if character.isalpha() and (mask >> index) & 1
                else character
                for index, character in enumerate(lower)
            )
            for mask in range(12)
        )
        self.assertEqual(len(set(peers)), 12)
        self.assertTrue(all(
            fold_text_v1(peer) == fold_text_v1(query_source)
            for peer in peers
        ))
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                peers + (query_source,),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ):
                fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source, threshold=1.0),
                    view=view,
                )

        proof = _current_proof(report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(proof.scorer_invocation_count, 1)
        self.assertEqual(proof.accounted_identity_count, 11)
        self.assertEqual(proof.ranked_eligible_count, 10)
        self.assertEqual(proof.ranked_kth_score, 1.0)
        self.assertEqual(proof.ranked_kth_record_id, 3)
        self.assertEqual(
            tuple(row.record_id for row in fuzzy.accepted),
            tuple(range(12, 2, -1)),
        )

    def test_retrieval_rejects_forged_ranked_subset_containing_raw_exact(self) -> None:
        query_source = "CAFÉ SOURCE"
        lower = "café source"
        peers = tuple(
            "".join(
                character.upper()
                if character.isalpha() and (mask >> index) & 1
                else character
                for index, character in enumerate(lower)
            )
            for mask in range(10)
        )
        original_observe = tm_candidate_index.CandidateProofSession.observe

        def forge_ranked_subset(session, observations, *, ranked_record_ids):
            observation_ids = tuple(item[0] for item in observations)
            forged_ranked_ids = tuple(
                record_id
                for record_id in observation_ids
                if record_id == 11 or record_id in ranked_record_ids
            )
            return original_observe(
                session,
                observations,
                ranked_record_ids=forged_ranked_ids,
            )

        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                peers + (query_source,),
                fts5_available=False,
            )
            with store.query_lease() as view, patch.object(
                tm_candidate_index.CandidateProofSession,
                "observe",
                new=forge_ranked_subset,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^proof report must publish closed scorer conservation$",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query(query_source, threshold=1.0),
                        view=view,
                    )

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

        proof = _current_proof(report)
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
                    completion_policy="oracle_full",
                )
        proof = _current_proof(report)
        refinement = _current_refinement(proof)
        self.assertEqual(proof.ranked_kth_score, 0.0)
        self.assertEqual(proof.ranked_kth_record_id, 591)
        self.assertEqual(refinement.k0_score, 0.0)
        self.assertEqual(refinement.k0_record_id, 591)
        self.assertEqual(refinement.p1_unscored_identity_count, 0)
        self.assertEqual(refinement.p2_unscored_identity_count, 590)
        self.assertEqual(refinement.p2_max_upper_bound, 0.0)
        self.assertEqual(refinement.p2_possible_record_id, 590)

    def test_u4_excludes_p3_without_partition_equivalence_reuse(self) -> None:
        query_source = "aaabb"
        high_score_prefix = tuple(
            "".join(
                character.upper() if (mask >> index) & 1 else character
                for index, character in enumerate(query_source)
            )
            for mask in range(1, 11)
        )
        partition_calls: list[str] = []
        original_partition_distance = (
            tm_candidate_index._PartitionLCSQueryProjectionV1.distance
        )

        def count_partition_distance(
            projection: tm_candidate_index._PartitionLCSQueryProjectionV1,
            candidate: str,
        ) -> int:
            partition_calls.append(candidate)
            return original_partition_distance(projection, candidate)

        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                high_score_prefix + (("abbaa",) * 590),
                fts5_available=False,
            )
            calls: list[tuple[str, str]] = []
            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                tm_candidate_index._PartitionLCSQueryProjectionV1,
                "distance",
                new=count_partition_distance,
            ), patch.object(
                SimilarityScorerV1,
                "score",
                new=_counting_production_score(calls),
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                    completion_policy="oracle_full",
                )

        proof = _current_proof(report)
        refinement = _current_refinement(proof)
        self.assertEqual(
            len(partition_calls),
            refinement.u4_evaluated_identity_count,
        )
        self.assertEqual(set(partition_calls), {"abbaa"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(proof.scorer_invocation_count, 2)
        self.assertEqual(refinement.a0_accounted_identity_count, 10)
        self.assertEqual(refinement.p1_unscored_identity_count, 0)
        self.assertEqual(refinement.r_refinement_identity_count, 590)
        self.assertEqual(refinement.p2_unscored_identity_count, 0)
        self.assertEqual(refinement.s_post_u3_identity_count, 590)
        self.assertEqual(refinement.a1_accounted_identity_count, 32)
        self.assertEqual(refinement.u4_evaluated_identity_count, 558)
        self.assertEqual(refinement.p3_unscored_identity_count, 558)
        self.assertEqual(refinement.p3_max_upper_bound, 0.475)
        self.assertEqual(refinement.p3_possible_record_id, 568)
        self.assertEqual(
            (
                proof.unscored_max_upper_bound,
                proof.unscored_possible_record_id,
            ),
            (
                refinement.p3_max_upper_bound,
                refinement.p3_possible_record_id,
            ),
        )

    def test_append_during_lazy_u4_is_caught_by_final_validation(self) -> None:
        query_source = "aaabb"
        high_score_prefix = tuple(
            "".join(
                character.upper() if (mask >> index) & 1 else character
                for index, character in enumerate(query_source)
            )
            for mask in range(1, 11)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                high_score_prefix + (("abbaa",) * 590),
                fts5_available=False,
            )
            original_distance = (
                tm_candidate_index._PartitionLCSQueryProjectionV1.distance
            )
            distance_calls = 0
            append_elapsed = 0.0

            def append_during_distance(
                projection: tm_candidate_index._PartitionLCSQueryProjectionV1,
                candidate: str,
            ) -> int:
                nonlocal distance_calls, append_elapsed
                distance_calls += 1
                if distance_calls == 1:
                    started = time.perf_counter()
                    store.append_batch(
                        batch_id="import.concurrent-u4",
                        kind="import",
                        drafts=(_draft("concurrent U4 row", 999),),
                        source_digest="5" * 64,
                        source_path=(root / "concurrent-u4.jsonl").resolve(),
                    )
                    append_elapsed = time.perf_counter() - started
                return original_distance(projection, candidate)

            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                tm_candidate_index._PartitionLCSQueryProjectionV1,
                "distance",
                new=append_during_distance,
            ), self.assertRaisesRegex(
                Exception,
                "STORE.CANDIDATE_PROOF_STALE",
            ):
                prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query(query_source),
                    view=view,
                    completion_policy="oracle_full",
                )

        self.assertGreater(distance_calls, 0)
        self.assertLess(append_elapsed, 1.0)

    def test_public_candidate_evidence_contains_only_exact_bigram_facts(
        self,
    ) -> None:
        cases = (
            ("aaaaa", "bbbbb", 0, 4),
            ("abaca", "acaba", 4, 4),
        )
        for query_source, candidate_source, matched, query_grams in cases:
            with self.subTest(
                query=query_source,
                candidate=candidate_source,
            ), tempfile.TemporaryDirectory() as temporary:
                store = _store(
                    Path(temporary),
                    (candidate_source,) * 600,
                    fts5_available=False,
                )
                with store.query_lease() as view, patch(
                    "tm_candidate_index._should_use_dense_traversal",
                    return_value=True,
                ):
                    _fuzzy, report = prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query(query_source),
                        view=view,
                        completion_policy="oracle_full",
                    )

            self.assertTrue(report.candidates)
            self.assertEqual(
                {candidate.matched_grams for candidate in report.candidates},
                {matched},
            )
            self.assertEqual(
                {candidate.query_grams for candidate in report.candidates},
                {query_grams},
            )
            self.assertEqual(
                {candidate.overlap_ratio for candidate in report.candidates},
                {matched / query_grams},
            )

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
                    completion_policy="oracle_full",
                )
        proof = _current_proof(report)
        refinement = _current_refinement(proof)
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

        assert (
            proof.ranked_kth_score is not None
            and proof.ranked_kth_record_id is not None
        )
        with self.assertRaises(ValueError):
            _ = replace(
                proof,
                unscored_max_upper_bound=proof.ranked_kth_score,
                unscored_possible_record_id=proof.ranked_kth_record_id,
                refinement=replace(
                    refinement,
                    p1_max_upper_bound=proof.ranked_kth_score,
                    p1_possible_record_id=proof.ranked_kth_record_id,
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

    def test_dense_phase2_fuses_each_lcs_fact_before_scorer_resumes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                tuple(
                    f"shared source sentence about record{index % 30}"
                    for index in range(600)
                ),
                fts5_available=False,
            )
            events: list[str] = []
            batch_sizes: list[int] = []
            phase2_calls = 0
            original_phase2 = SQLiteTMQueryView.candidate_proof_dense_phase2
            original_records = SQLiteTMQueryView.records_by_id
            original_facts = tm_candidate_index._ExactLCSQueryProjection.facts
            original_score = SimilarityScorerV1.score

            def ordered_phase2(view: SQLiteTMQueryView, **kwargs):
                nonlocal phase2_calls
                phase2_calls += 1
                events.append("phase2-enter")
                response = original_phase2(view, **kwargs)
                self.assertFalse(view._candidate_connection().in_transaction)
                events.append("phase2-return")
                return response

            def bounded_records(view: SQLiteTMQueryView, record_ids: tuple[int, ...]):
                batch_sizes.append(len(record_ids))
                return original_records(view, record_ids)

            def ordered_facts(owner, candidate: str, source_length: int):
                events.append("lcs-u3")
                return original_facts(owner, candidate, source_length)

            def ordered_score(
                owner: SimilarityScorerV1,
                query: str,
                candidate: str,
            ):
                events.append("score")
                return original_score(owner, query, candidate)

            with store.query_lease() as view, patch(
                "tm_candidate_index._should_use_dense_traversal",
                return_value=True,
            ), patch.object(
                SQLiteTMQueryView,
                "candidate_proof_dense_phase2",
                new=ordered_phase2,
            ), patch.object(
                SQLiteTMQueryView,
                "records_by_id",
                new=bounded_records,
            ), patch.object(
                tm_candidate_index._ExactLCSQueryProjection,
                "facts",
                new=ordered_facts,
            ), patch.object(
                SimilarityScorerV1,
                "score",
                new=ordered_score,
            ):
                _fuzzy, report = prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("shared source sentence about recordx"),
                    view=view,
                    completion_policy="oracle_full",
                )

        proof = _current_proof(report)
        refinement = _current_refinement(proof)
        phase2_return = events.index("phase2-return")
        post_phase2 = events[phase2_return + 1 :]
        refinement_count = refinement.r_refinement_identity_count
        self.assertEqual(phase2_calls, 1)
        self.assertTrue(batch_sizes)
        self.assertLessEqual(max(batch_sizes), 32)
        self.assertIn(32, batch_sizes)
        self.assertEqual(
            post_phase2[:refinement_count],
            ["lcs-u3"] * refinement_count,
        )
        self.assertNotIn("lcs-u3", post_phase2[refinement_count:])
        self.assertNotIn("score", post_phase2[:refinement_count])
        self.assertIn("score", post_phase2[refinement_count:])

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

    def test_dense_phase1_receipt_rejects_replaced_fact_tuples(self) -> None:
        sources = tuple(
            (
                "shared source sentence about abaca"
                if index % 2 == 0
                else "shared source sentence about acabadelta"
            )
            for index in range(600)
        )
        for mutation in ("length", "bigram"):
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as temporary:
                store = _store(Path(temporary), sources, fts5_available=False)
                original = SQLiteTMQueryView.candidate_proof_dense_phase1

                def mutate(view: SQLiteTMQueryView, **kwargs):
                    response = original(view, **kwargs)
                    if mutation == "length":
                        lengths = list(response.source_fold_lengths)
                        self.assertNotEqual(lengths[0], lengths[1])
                        lengths[1] = lengths[0]
                        return _unchecked_replace(
                            response,
                            source_fold_lengths=tuple(lengths),
                        )
                    bigrams = list(response.bigram_multiset_intersections)
                    self.assertGreater(bigrams[1], 0)
                    bigrams[1] -= 1
                    return _unchecked_replace(
                        response,
                        bigram_multiset_intersections=tuple(bigrams),
                    )

                with store.query_lease() as view, patch(
                    "tm_candidate_index._should_use_dense_traversal",
                    return_value=True,
                ), patch.object(
                    SQLiteTMQueryView,
                    "candidate_proof_dense_phase1",
                    new=mutate,
                ), self.assertRaisesRegex(
                    Exception,
                    "STORE.CANDIDATE_PROOF_INVALID",
                ):
                    prove_and_score_fuzzy_candidates(
                        resource_id="tm.primary",
                        resource_order=0,
                        query=_query("shared source sentence about abaca"),
                        view=view,
                    )

    def test_dense_refinement_response_is_strict_ordered_and_bound(self) -> None:
        sources = tuple(
            f"shared source sentence about record{index % 30}"
            for index in range(600)
        )
        mutations = (
            "missing",
            "duplicate",
            "order",
            "outside",
            "extra",
            "fold_missing",
            "length_mismatch",
            "receipt_fold_same_length",
            "receipt_ids_equal",
            "receipt_lengths_equal",
            "binding",
        )
        for mutation in mutations:
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as temporary:
                store = _store(Path(temporary), sources, fts5_available=False)
                original = SQLiteTMQueryView.candidate_proof_dense_phase2

                def mutate(view: SQLiteTMQueryView, **kwargs):
                    response = original(view, **kwargs)
                    ids = response.record_ids
                    folds = response.source_folds_v1
                    lengths = response.source_fold_lengths
                    self.assertGreaterEqual(len(ids), 2)
                    if mutation == "missing":
                        return _unchecked_replace(
                            response,
                            record_ids=ids[:-1],
                            source_folds_v1=folds[:-1],
                            source_fold_lengths=lengths[:-1],
                        )
                    if mutation == "duplicate":
                        return _unchecked_replace(
                            response,
                            record_ids=(ids[0], ids[0], *ids[2:]),
                        )
                    if mutation == "order":
                        return _unchecked_replace(
                            response,
                            record_ids=(ids[1], ids[0], *ids[2:]),
                        )
                    if mutation == "outside":
                        return _unchecked_replace(
                            response,
                            record_ids=(len(sources) + 1, *ids[1:]),
                        )
                    if mutation == "extra":
                        return _unchecked_replace(
                            response,
                            record_ids=(*ids, ids[0]),
                            source_folds_v1=(*folds, folds[0]),
                            source_fold_lengths=(*lengths, lengths[0]),
                        )
                    if mutation == "fold_missing":
                        return _unchecked_replace(
                            response,
                            source_folds_v1=("", *folds[1:]),
                        )
                    if mutation == "length_mismatch":
                        return _unchecked_replace(
                            response,
                            source_fold_lengths=(lengths[0] + 1, *lengths[1:]),
                        )
                    if mutation == "receipt_fold_same_length":
                        replacement = folds[0][::-1]
                        self.assertEqual(len(replacement), lengths[0])
                        self.assertNotEqual(replacement, folds[0])
                        return _unchecked_replace(
                            response,
                            source_folds_v1=(replacement, *folds[1:]),
                        )
                    if mutation == "receipt_ids_equal":
                        replacement_ids = tuple([*ids])
                        self.assertEqual(replacement_ids, ids)
                        self.assertIsNot(replacement_ids, ids)
                        return _unchecked_replace(
                            response,
                            record_ids=replacement_ids,
                        )
                    if mutation == "receipt_lengths_equal":
                        replacement_lengths = tuple([*lengths])
                        self.assertEqual(replacement_lengths, lengths)
                        self.assertIsNot(replacement_lengths, lengths)
                        return _unchecked_replace(
                            response,
                            source_fold_lengths=replacement_lengths,
                        )
                    return _unchecked_replace(response, binding_digest="0" * 64)

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
                    completion_policy="oracle_full",
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
                    completion_policy="oracle_full",
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

    def test_proof_path_owns_exact_scorer_marker_and_rejects_subclass(
        self,
    ) -> None:
        class ScorerSubclass(SimilarityScorerV1):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            store = _store(
                Path(temporary),
                ("open source", "other source") * 300,
                fts5_available=False,
            )
            marker = SimilarityScorerV1()
            marker_calls: list[tuple[str, str]] = []
            marker.__dict__["score"] = lambda left, right: marker_calls.append(
                (left, right)
            )
            with store.query_lease() as view:
                prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("open source!"),
                    view=view,
                    scorer=marker,
                )
            self.assertEqual(marker_calls, [])

            with store.query_lease() as view, self.assertRaisesRegex(
                ValueError,
                "production scorer-v1 owner",
            ):
                prove_and_score_fuzzy_candidates(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=_query("open source!"),
                    view=view,
                    scorer=ScorerSubclass(),
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
                                    completion_policy="oracle_full",
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
            original_proof = prove_and_score_fuzzy_candidates

            def fail_one_resource(**kwargs):
                if kwargs["resource_id"] == "tm.failed":
                    raise tm_candidate_index.CandidateProofBudgetExhausted()
                return original_proof(**kwargs)

            with patch(
                "tm_retrieval.prove_and_score_fuzzy_candidates",
                new=fail_one_resource,
            ):
                report = TMRetrievalService(
                    capability_publisher=_retrieval_capability_publisher(),
                ).query(
                    (
                        TMResourceHandle(
                            "tm.failed",
                            failed_store,
                            True,
                            True,
                            True,
                            0,
                        ),
                        TMResourceHandle(
                            "tm.healthy",
                            healthy_store,
                            True,
                            True,
                            True,
                            1,
                        ),
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
