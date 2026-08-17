"""Focused current-schema index tests for candidate proof-query-v3."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sqlite3
import tempfile
import unittest

import tm_sqlite_store
from tm_benchmark import (
    iter_corpus_records,
    iter_fuzzy_queries,
    iter_oracle_queries,
    iter_oracle_subset_records,
    load_benchmark_contract,
)
from tm_benchmark_oracle import _run_candidate_path
from tm_candidate_index import (
    _ExactLCSQueryProjection,
    _dense_phase2_upper_bound,
)
from tm_contracts import BenchmarkExecutionPath
from tm_sqlite_store import SQLiteStoreSchemaError, unique_character_ngrams
from text_matcher import fold_text_value_v1
from tests.test_tm_candidate_proof_query import _draft, _store


class CandidateProofIndexV16Tests(unittest.TestCase):
    def test_fallback_seed_is_real_deterministic_and_posting_bounded(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE tm_gram("
            "gram_size INTEGER NOT NULL, gram TEXT NOT NULL, "
            "record_id INTEGER NOT NULL)"
        )
        short_query = "abcdefghijklmnopqrstuvwxyz"
        long_query = "".join(chr(0x400 + index) for index in range(300))
        inserted: set[tuple[int, str, int]] = set()
        for query in (short_query, long_query):
            for size in (1, 2, 3):
                grams = unique_character_ngrams(query, size)
                for record_id in range(1, 321):
                    for offset, gram in enumerate(grams):
                        if (record_id * 7 + offset * 11 + size) % 23 < 5:
                            inserted.add((size, gram, record_id))
        connection.executemany(
            "INSERT INTO tm_gram(gram_size, gram, record_id) VALUES (?, ?, ?)",
            sorted(inserted),
        )
        postings: dict[tuple[int, str], list[int]] = {}
        for gram_size, gram, record_id in inserted:
            postings.setdefault((gram_size, gram), []).append(record_id)
        for record_ids in postings.values():
            record_ids.sort(reverse=True)

        def expected(query: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
            stages = []
            for size in (3, 2, 1):
                grams = unique_character_ngrams(query, size)
                posting_budget = 4096
                per_gram, remainder = divmod(posting_budget, len(grams))
                counts: Counter[int] = Counter()
                for ordinal, gram in enumerate(grams):
                    gram_limit = per_gram + (1 if ordinal < remainder else 0)
                    counts.update(postings.get((size, gram), ())[:gram_limit])
                stages.append((
                    f"GRAM_{size}",
                    tuple(
                        record_id
                        for record_id, _count in sorted(
                            counts.items(),
                            key=lambda item: (-item[1], -item[0]),
                        )[:37]
                    ),
                ))
            return tuple(stages)

        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            self.assertEqual(
                tm_sqlite_store._bounded_seed_stages(
                    connection,
                    folded_query=short_query,
                    fts5_available=False,
                    seed_limit=37,
                ),
                expected(short_query),
            )
            self.assertFalse(any("GROUP BY" in statement for statement in statements))
            statements.clear()
            observed = tm_sqlite_store._bounded_seed_stages(
                connection,
                folded_query=long_query,
                fts5_available=False,
                seed_limit=37,
            )
            self.assertEqual(observed, expected(long_query))
            expected_statement_count = sum(
                len(unique_character_ngrams(long_query, size))
                for size in (3, 2, 1)
            )
            self.assertEqual(
                sum(
                    "SELECT record_id FROM tm_gram" in statement
                    for statement in statements
                ),
                expected_statement_count,
            )
            for stage_name, record_ids in observed:
                size = int(stage_name.removeprefix("GRAM_"))
                query_grams = set(unique_character_ngrams(long_query, size))
                self.assertTrue(all(
                    any(record_id in postings.get((size, gram), ()) for gram in query_grams)
                    for record_id in record_ids
                ))
        finally:
            connection.close()

    def test_fallback_seed_never_exceeds_posting_cap_for_many_unique_grams(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE tm_gram("
            "gram_size INTEGER NOT NULL, gram TEXT NOT NULL, "
            "record_id INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX idx_tm_gram_lookup "
            "ON tm_gram(gram_size, gram, record_id)"
        )
        query = "".join(chr(0x10000 + index) for index in range(4_205))
        rows = []
        expected_statement_count = 0
        for size in (3, 2, 1):
            grams = unique_character_ngrams(query, size)
            self.assertGreater(len(grams), 4_096)
            expected_statement_count += len(grams)
            rows.extend(
                (size, gram, ordinal)
                for ordinal, gram in enumerate(grams, start=1)
            )
        connection.executemany(
            "INSERT INTO tm_gram(gram_size, gram, record_id) VALUES (?, ?, ?)",
            rows,
        )
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            stages = tm_sqlite_store._bounded_seed_stages(
                connection,
                folded_query=query,
                fts5_available=False,
                seed_limit=5_000,
            )
        finally:
            connection.close()

        self.assertEqual(tuple(stage for stage, _ids in stages), (
            "GRAM_3",
            "GRAM_2",
            "GRAM_1",
        ))
        for _stage, record_ids in stages:
            self.assertEqual(len(record_ids), 4_096)
            self.assertEqual(record_ids[0], 4_096)
            self.assertEqual(record_ids[-1], 1)
        self.assertEqual(
            sum(
                statement.startswith("SELECT record_id FROM tm_gram")
                for statement in statements
            ),
            expected_statement_count,
        )

    def test_nine_frozen_near_edits_retain_u3_budget_regression(self) -> None:
        expected_threshold_competitors = {
            183: 2_800,
            185: 2_104,
            186: 2_192,
            189: 3_620,
            193: 3_224,
            195: 2_261,
            196: 3_074,
            197: 2_414,
            199: 2_290,
        }
        queries = tuple(iter_fuzzy_queries())
        query_facts = tuple(
            (
                query_id,
                folded_query,
                Counter(
                    folded_query[offset : offset + 2]
                    for offset in range(len(folded_query) - 1)
                ),
                _ExactLCSQueryProjection(folded_query),
            )
            for query_id in expected_threshold_competitors
            for folded_query in (
                fold_text_value_v1(queries[query_id - 1].query_raw),
            )
        )
        observed = dict.fromkeys(expected_threshold_competitors, 0)
        for record in iter_corpus_records():
            source_fold = fold_text_value_v1(record.source_raw)
            source_bigrams = Counter(
                source_fold[offset : offset + 2]
                for offset in range(len(source_fold) - 1)
            )
            for query_id, folded_query, query_bigrams, projection in query_facts:
                bigram_intersection = sum(
                    min(frequency, source_bigrams.get(gram, 0))
                    for gram, frequency in query_bigrams.items()
                )
                lcs_length = projection.facts(source_fold, len(source_fold))
                if (
                    _dense_phase2_upper_bound(
                        query_length=len(folded_query),
                        record_length=len(source_fold),
                        lcs_length=lcs_length,
                        bigram_intersection=bigram_intersection,
                    )
                    >= 0.60
                ):
                    observed[query_id] += 1

        self.assertEqual(observed, expected_threshold_competitors)
        self.assertTrue(all(count > 2_048 for count in observed.values()))

    def test_legacy_12_query_27_identity_oracle_misses_are_regressions(
        self,
    ) -> None:
        legacy_missing = {
            182: (20010,),
            183: (80037,),
            184: (20044,),
            187: (60725,),
            189: (28000, 80904, 80037),
            190: (22101,),
            191: (30176,),
            192: (20112, 20010, 71248),
            194: (20010, 81482, 71418, 49148),
            197: (20010, 61711, 50151, 20112, 17545, 17375),
            198: (18616, 18786),
            200: (42025, 72200, 20299),
        }
        self.assertEqual(sum(map(len, legacy_missing.values())), 27)
        project_root = Path(__file__).resolve().parents[1]
        contract = load_benchmark_contract(
            project_root / "benchmark_tm_contract.json"
        )
        records = tuple(
            iter_oracle_subset_records(
                seed=contract.corpus_seed,
                record_count=contract.corpus_record_count,
                subset_count=contract.oracle_subset_record_count,
            )
        )
        queries = tuple(
            query
            for query in iter_oracle_queries(
                seed=contract.corpus_seed,
                record_count=contract.corpus_record_count,
                subset_count=contract.oracle_subset_record_count,
                query_count=contract.oracle_query_count,
            )
            if query.query_id in legacy_missing
        )
        self.assertEqual(tuple(query.query_id for query in queries), tuple(legacy_missing))

        for execution_path, suffix in (
            (BenchmarkExecutionPath.FTS5_TRIGRAM, "fts5"),
            (BenchmarkExecutionPath.GRAM_FALLBACK, "fallback"),
        ):
            with self.subTest(path=execution_path), tempfile.TemporaryDirectory() as temporary:
                    (
                        _fixture_digest,
                        _store_kind,
                        rows,
                        _proof_query_version,
                    ) = _run_candidate_path(
                    contract=contract,
                    execution_path=execution_path,
                    records=records,
                    queries=queries,
                    run_root=Path(temporary).resolve(),
                    resource_id=f"tm.benchmark.{suffix}",
                    canonical_store_id=f"store.benchmark.{suffix}",
                )
            self.assertEqual(tuple(row[0] for row in rows), tuple(legacy_missing))
            for query_id, candidate_ids, _kind, available, code, _truncated in rows:
                self.assertTrue(available)
                self.assertIsNone(code)
                self.assertTrue(
                    set(legacy_missing[query_id]).issubset(candidate_ids),
                    (query_id, legacy_missing[query_id]),
                )

    def _ordered_projection(
        self,
        root: Path,
        *,
        fts5_available: bool,
    ) -> tuple[tuple[int, ...], tuple[str, ...], tuple[int, ...]]:
        sources = (
            "Open the door",
            "Close the window",
            "猫狗",
            "aaaaa",
            "aaaba",
        )
        store = _store(root, sources, fts5_available=fts5_available)
        with store.query_lease() as view:
            snapshot = view.candidate_proof_snapshot(
                folded_query="open",
                seed_limit=256,
            )
            phase1 = view.candidate_proof_dense_phase1(
                folded_query="open",
                blocks=snapshot.blocks,
                head_revision=snapshot.head_revision,
                total_record_count=snapshot.total_record_count,
                query_maxima_digest=snapshot.query_maxima_digest,
            )
            request = (1, 3, 5)
            requested_lengths = tuple(
                phase1.source_fold_lengths[record_id - 1]
                for record_id in request
            )
            phase2 = view.candidate_proof_dense_phase2(
                folded_query="open",
                blocks=snapshot.blocks,
                head_revision=snapshot.head_revision,
                total_record_count=snapshot.total_record_count,
                query_maxima_digest=snapshot.query_maxima_digest,
                binding_digest=phase1.binding_digest,
                record_ids=request,
                source_fold_lengths=requested_lengths,
            )
            self.assertFalse(view._candidate_connection().in_transaction)
            return (
                phase2.record_ids,
                phase2.source_folds_v1,
                phase2.source_fold_lengths,
            )

    def test_fts_and_fallback_share_the_same_ordered_projection(self) -> None:
        observed = []
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available), tempfile.TemporaryDirectory() as temporary:
                observed.append(
                    self._ordered_projection(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                )
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(
            observed[0],
            ((1, 3, 5), ("open the door", "猫狗", "aaaba"), (13, 2, 5)),
        )

    def test_committed_phase2_does_not_bypass_final_head_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(
                root,
                ("open the door", "close the window", "猫狗"),
                fts5_available=False,
            )
            with store.query_lease() as view:
                snapshot = view.candidate_proof_snapshot(
                    folded_query="open",
                    seed_limit=256,
                )
                phase1 = view.candidate_proof_dense_phase1(
                    folded_query="open",
                    blocks=snapshot.blocks,
                    head_revision=snapshot.head_revision,
                    total_record_count=snapshot.total_record_count,
                    query_maxima_digest=snapshot.query_maxima_digest,
                )
                view.candidate_proof_dense_phase2(
                    folded_query="open",
                    blocks=snapshot.blocks,
                    head_revision=snapshot.head_revision,
                    total_record_count=snapshot.total_record_count,
                    query_maxima_digest=snapshot.query_maxima_digest,
                    binding_digest=phase1.binding_digest,
                    record_ids=(1, 2, 3),
                    source_fold_lengths=phase1.source_fold_lengths,
                )
                self.assertFalse(view._candidate_connection().in_transaction)
                store.append_batch(
                    batch_id="import.after-proof-index-phase2",
                    kind="import",
                    drafts=(_draft("concurrent row", 99),),
                    source_digest="8" * 64,
                    source_path=(root / "concurrent.jsonl").resolve(),
                )
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "^STORE.CANDIDATE_PROOF_STALE$",
                ):
                    view.validate_candidate_proof_generation(
                        head_revision=snapshot.head_revision,
                        total_record_count=snapshot.total_record_count,
                    )

    def test_current_schema_does_not_persist_lcs_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _store(
                root,
                ("open the door", "close the window", "猫狗"),
                fts5_available=False,
            )
            database = (root / ".tm.primary.stage.sqlite3").resolve()
            connection = sqlite3.connect(database)
            try:
                schema_rows = connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'index')"
                ).fetchall()
                record_columns = connection.execute(
                    "PRAGMA table_info(tm_record)"
                ).fetchall()
            finally:
                connection.close()
            self.assertFalse(any("lcs" in str(value).lower() for row in schema_rows for value in row))
            self.assertNotIn("lcs", {str(row[1]).lower() for row in record_columns})


if __name__ == "__main__":
    unittest.main()
