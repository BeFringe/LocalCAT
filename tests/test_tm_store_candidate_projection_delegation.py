"""Wave 2 transaction/error guards for Store -> projection delegation."""

from __future__ import annotations

from pathlib import Path
import inspect
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import tm_sqlite_candidate_projection as projection
from tests.test_tm_candidate_index import _retriever_store_with_records
from tm_candidate_index import CandidateRetriever
from tm_candidate_store_contracts import SQLiteStoreSchemaError


class CandidateProjectionDelegationTests(unittest.TestCase):
    def test_posting_projections_preserve_each_wrapper_transaction_shape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = _retriever_store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("alpha", "alpine", "omega"),
            )
            observed: dict[str, list[bool]] = {
                "single": [],
                "chunked": [],
                "gram": [],
            }
            originals = {
                "single": projection.fts5_candidate_ids,
                "chunked": projection.fts5_candidate_ids_for_trigrams,
                "gram": projection.gram_candidate_overlaps,
            }

            def observer(label: str):
                def observe(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    observed[label].append(connection.in_transaction)
                    return originals[label](connection, *args, **kwargs)

                return observe

            with patch.object(
                projection,
                "fts5_candidate_ids",
                side_effect=observer("single"),
            ) as single:
                self.assertTrue(store.fts5_candidate_ids('"alp"'))
            with patch.object(
                projection,
                "fts5_candidate_ids_for_trigrams",
                side_effect=observer("chunked"),
            ) as chunked:
                self.assertTrue(
                    store.fts5_candidate_ids_for_trigrams(("alp", "lph"))
                )
            with patch.object(
                projection,
                "gram_candidate_overlaps",
                side_effect=observer("gram"),
            ) as gram:
                self.assertTrue(
                    store.gram_candidate_overlaps(
                        ((1, "a"), (2, "al")),
                        candidate_cap=8,
                    )
                )
            single.assert_called_once()
            chunked.assert_called_once()
            gram.assert_called_once()
            self.assertEqual(
                observed,
                {"single": [False], "chunked": [True], "gram": [True]},
            )

            raw_sqlite_fault = sqlite3.OperationalError("raw single FTS fault")
            with patch.object(
                projection,
                "fts5_candidate_ids",
                side_effect=raw_sqlite_fault,
            ):
                with self.assertRaises(sqlite3.OperationalError) as raw_raised:
                    store.fts5_candidate_ids('"alp"')
            self.assertIs(raw_raised.exception, raw_sqlite_fault)

            with patch.object(
                projection,
                "fts5_candidate_ids_for_trigrams",
                side_effect=sqlite3.OperationalError("secret FTS body"),
            ):
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "^STORE.FTS5_QUERY_FAILED$",
                ) as raised:
                    store.fts5_candidate_ids_for_trigrams(("alp",))
            self.assertNotIn("secret FTS body", str(raised.exception))

            sentinel = RuntimeError("programmer fault")
            with patch.object(
                projection,
                "gram_candidate_overlaps",
                side_effect=sentinel,
            ):
                with self.assertRaises(RuntimeError) as raised_fault:
                    store.gram_candidate_overlaps(
                        ((1, "a"),),
                        candidate_cap=8,
                    )
            self.assertIs(raised_fault.exception, sentinel)

    def test_recall_projection_runs_inside_store_transaction_and_keeps_mapping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = _retriever_store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("猫狗", "猫咪", "小狗"),
            )
            original = projection.candidate_recall_snapshot
            observed_transactions: list[bool] = []

            def observe(connection: sqlite3.Connection, **kwargs: object) -> object:
                observed_transactions.append(connection.in_transaction)
                return original(connection, **kwargs)

            with patch.object(
                projection,
                "candidate_recall_snapshot",
                side_effect=observe,
            ) as delegated:
                report = CandidateRetriever().candidates(
                    "tm.primary",
                    store,
                    "猫狗",
                    result_limit=10,
                )
            self.assertTrue(report.candidates)
            delegated.assert_called_once()
            self.assertEqual(observed_transactions, [True])

            with patch.object(
                projection,
                "candidate_recall_snapshot",
                side_effect=sqlite3.OperationalError("secret SQL body"),
            ):
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "STORE.CANDIDATE_QUERY_FAILED",
                ) as raised:
                    CandidateRetriever().candidates(
                        "tm.primary",
                        store,
                        "猫狗",
                        result_limit=10,
                    )
            self.assertNotIn("secret SQL body", str(raised.exception))

            sentinel = RuntimeError("programmer fault")
            with patch.object(
                projection,
                "candidate_recall_snapshot",
                side_effect=sentinel,
            ):
                with self.assertRaises(RuntimeError) as raised_programmer_fault:
                    CandidateRetriever().candidates(
                        "tm.primary",
                        store,
                        "猫狗",
                        result_limit=10,
                    )
            self.assertIs(raised_programmer_fault.exception, sentinel)

            with patch.object(
                projection,
                "candidate_recall_snapshot",
                return_value=(
                    (("FTS_TRIGRAM", ((1, 1),)),),
                    ((1, "猫狗"),),
                ),
            ):
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "^STORE.CANDIDATE_CAPABILITY_MISMATCH$",
                ):
                    store.candidate_recall_snapshot(
                        fts_query_trigrams=None,
                        query_grams_by_size=((1, ("猫",)),),
                        candidate_floor=8,
                        fts_query_degenerate=False,
                    )

    def test_proof_projection_runs_inside_view_transaction_and_keeps_mapping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = _retriever_store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("alpha", "alpine", "omega"),
            )
            original = projection.candidate_proof_snapshot
            observed_transactions: list[bool] = []

            def observe(connection: sqlite3.Connection, **kwargs: object) -> object:
                observed_transactions.append(connection.in_transaction)
                return original(connection, **kwargs)

            with store.query_lease() as view:
                with patch.object(
                    projection,
                    "candidate_proof_snapshot",
                    side_effect=observe,
                ) as delegated:
                    snapshot = view.candidate_proof_snapshot(
                        folded_query="alpha",
                        seed_limit=8,
                    )
                self.assertEqual(snapshot.total_record_count, 3)
                delegated.assert_called_once()
                self.assertEqual(observed_transactions, [True])

                with patch.object(
                    projection,
                    "candidate_proof_snapshot",
                    side_effect=sqlite3.OperationalError("secret proof body"),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "STORE.CANDIDATE_PROOF_QUERY_FAILED",
                    ) as raised:
                        view.candidate_proof_snapshot(
                            folded_query="alpha",
                            seed_limit=8,
                        )
                self.assertNotIn("secret proof body", str(raised.exception))

                sentinel = RuntimeError("programmer fault")
                with patch.object(
                    projection,
                    "candidate_proof_snapshot",
                    side_effect=sentinel,
                ):
                    with self.assertRaises(RuntimeError) as raised_fault:
                        view.candidate_proof_snapshot(
                            folded_query="alpha",
                            seed_limit=8,
                        )
                self.assertIs(raised_fault.exception, sentinel)

                with patch.object(
                    projection,
                    "candidate_proof_snapshot",
                    return_value=((), (), "0" * 64),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_INVALID$",
                    ):
                        view.candidate_proof_snapshot(
                            folded_query="alpha",
                            seed_limit=8,
                        )
                self.assertNotIn(
                    "head_revision",
                    inspect.signature(
                        projection.candidate_proof_snapshot
                    ).parameters,
                )

    def test_block_and_dense_projections_use_view_transaction_and_store_receipts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = _retriever_store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("alpha", "alpine", "omega"),
            )
            with store.query_lease() as view:
                snapshot = view.candidate_proof_snapshot(
                    folded_query="alpha",
                    seed_limit=8,
                )
                observed: list[tuple[str, bool]] = []
                originals = {
                    "block": projection.candidate_proof_block_records,
                    "phase1": projection.candidate_proof_dense_phase1,
                    "phase2": projection.candidate_proof_dense_phase2,
                }

                def observer(label: str):
                    def observe(
                        connection: sqlite3.Connection,
                        **kwargs: object,
                    ) -> object:
                        observed.append((label, connection.in_transaction))
                        return originals[label](connection, **kwargs)

                    return observe

                with patch.object(
                    projection,
                    "candidate_proof_block_records",
                    side_effect=observer("block"),
                ) as block_call:
                    records = view.candidate_proof_block_records(
                        folded_query="alpha",
                        block=snapshot.blocks[0],
                        head_revision=snapshot.head_revision,
                        total_record_count=snapshot.total_record_count,
                    )
                self.assertTrue(records)
                block_call.assert_called_once()
                self.assertFalse(view._candidate_connection().in_transaction)

                with patch.object(
                    projection,
                    "candidate_proof_dense_phase1",
                    side_effect=observer("phase1"),
                ) as phase1_call:
                    phase1 = view.candidate_proof_dense_phase1(
                        folded_query="alpha",
                        blocks=snapshot.blocks,
                        head_revision=snapshot.head_revision,
                        total_record_count=snapshot.total_record_count,
                        query_maxima_digest=snapshot.query_maxima_digest,
                    )
                phase1_call.assert_called_once()
                self.assertFalse(view._candidate_connection().in_transaction)

                requested = (1, 3)
                lengths = tuple(
                    phase1.source_fold_lengths[record_id - 1]
                    for record_id in requested
                )
                with patch.object(
                    projection,
                    "candidate_proof_dense_phase2",
                    side_effect=observer("phase2"),
                ) as phase2_call:
                    phase2 = view.candidate_proof_dense_phase2(
                        folded_query="alpha",
                        blocks=snapshot.blocks,
                        head_revision=snapshot.head_revision,
                        total_record_count=snapshot.total_record_count,
                        query_maxima_digest=snapshot.query_maxima_digest,
                        binding_digest=phase1.binding_digest,
                        record_ids=requested,
                        source_fold_lengths=lengths,
                    )
                phase2_call.assert_called_once()
                self.assertEqual(phase2.record_ids, requested)
                self.assertFalse(view._candidate_connection().in_transaction)
                self.assertEqual(
                    observed,
                    [("block", True), ("phase1", True), ("phase2", True)],
                )

                with patch.object(
                    projection,
                    "candidate_proof_block_records",
                    side_effect=sqlite3.OperationalError("secret block body"),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_QUERY_FAILED$",
                    ) as raised:
                        view.candidate_proof_block_records(
                            folded_query="alpha",
                            block=snapshot.blocks[0],
                            head_revision=snapshot.head_revision,
                            total_record_count=snapshot.total_record_count,
                        )
                self.assertNotIn("secret block body", str(raised.exception))
                self.assertFalse(view._candidate_connection().in_transaction)

                sentinel = RuntimeError("programmer fault")
                with patch.object(
                    projection,
                    "candidate_proof_dense_phase1",
                    side_effect=sentinel,
                ):
                    with self.assertRaises(RuntimeError) as raised_fault:
                        view.candidate_proof_dense_phase1(
                            folded_query="alpha",
                            blocks=snapshot.blocks,
                            head_revision=snapshot.head_revision,
                            total_record_count=snapshot.total_record_count,
                            query_maxima_digest=snapshot.query_maxima_digest,
                        )
                self.assertIs(raised_fault.exception, sentinel)
                self.assertFalse(view._candidate_connection().in_transaction)

                with patch.object(
                    projection,
                    "candidate_proof_dense_phase1",
                    return_value=((1,), (0,)),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_INVALID$",
                    ):
                        view.candidate_proof_dense_phase1(
                            folded_query="alpha",
                            blocks=snapshot.blocks,
                            head_revision=snapshot.head_revision,
                            total_record_count=snapshot.total_record_count,
                            query_maxima_digest=snapshot.query_maxima_digest,
                        )
                self.assertFalse(view._candidate_connection().in_transaction)

                with patch.object(
                    projection,
                    "candidate_proof_dense_phase2",
                    side_effect=sqlite3.OperationalError(
                        "secret dense phase2 body"
                    ),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_QUERY_FAILED$",
                    ) as dense_raised:
                        view.candidate_proof_dense_phase2(
                            folded_query="alpha",
                            blocks=snapshot.blocks,
                            head_revision=snapshot.head_revision,
                            total_record_count=snapshot.total_record_count,
                            query_maxima_digest=snapshot.query_maxima_digest,
                            binding_digest=phase1.binding_digest,
                            record_ids=requested,
                            source_fold_lengths=lengths,
                        )
                self.assertNotIn(
                    "secret dense phase2 body",
                    str(dense_raised.exception),
                )
                self.assertFalse(view._candidate_connection().in_transaction)


if __name__ == "__main__":
    unittest.main()
