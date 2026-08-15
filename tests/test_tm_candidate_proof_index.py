"""Focused current-schema index tests for candidate proof-query-v2."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from tm_benchmark import (
    iter_oracle_queries,
    iter_oracle_subset_records,
    load_benchmark_contract,
)
from tm_benchmark_oracle import _run_candidate_path
from tm_contracts import BenchmarkExecutionPath
from tm_sqlite_store import SQLiteStoreSchemaError
from tests.test_tm_candidate_proof_query import _draft, _store


class CandidateProofIndexV15Tests(unittest.TestCase):
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
                _store_kind, _fixture_digest, rows = _run_candidate_path(
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
