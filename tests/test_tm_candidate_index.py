from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tm_candidate_index import (
    CANDIDATE_CONTRACT_FLOOR,
    CandidateRetriever,
    GRAM_LONG_QUERY_FTS_SELECTED_CODE,
    GramPostingIndex,
    FTS5TrigramIndex,
    build_candidate_write_plan,
    build_fts5_match_expression,
    unique_character_ngrams,
    unique_character_trigrams,
)
from tm_contracts import (
    CandidateStage,
    CanonicalResourceIdentity,
    MutableStageRef,
    TMRecordDraft,
    candidate_budget_v1,
)
from tm_sqlite_store import (
    SQLiteCandidateRecord,
    SQLiteCandidateRecallSnapshot,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    initialize_stage_schema,
)


def _stage(root: Path) -> MutableStageRef:
    configured = (root / "primary.jsonl").resolve()
    return MutableStageRef(
        stage_id="stage.primary",
        resource_identity=CanonicalResourceIdentity.from_configured_jsonl(
            "tm.primary",
            configured,
        ),
        staged_db_path=(root / ".primary.stage.sqlite3").resolve(),
        manifest_temp_path=(root / ".primary.snapshot.tmp").resolve(),
    )


def _draft(source: str, target: str) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "candidate-test"),),
    )


class FTS5TrigramIndexTests(unittest.TestCase):
    def test_unified_write_plan_has_exact_unique_grams_for_each_path(self) -> None:
        records = (
            SQLiteCandidateRecord(origin_ordinal=0, source_fold_v1="ababa"),
            SQLiteCandidateRecord(origin_ordinal=1, source_fold_v1="中文中"),
        )

        fast = build_candidate_write_plan(records, fts5_available=True)
        fallback = build_candidate_write_plan(records, fts5_available=False)

        self.assertEqual(fast.fts_origin_ordinals, (0, 1))
        self.assertEqual(
            tuple((row.origin_ordinal, row.gram_size, row.gram) for row in fast.gram_rows),
            (
                (0, 1, "a"),
                (0, 1, "b"),
                (0, 2, "ab"),
                (0, 2, "ba"),
                (1, 1, "中"),
                (1, 1, "文"),
                (1, 2, "中文"),
                (1, 2, "文中"),
            ),
        )
        self.assertEqual(fallback.fts_origin_ordinals, ())
        self.assertEqual(
            tuple((row.origin_ordinal, row.gram_size, row.gram) for row in fallback.gram_rows),
            (
                (0, 1, "a"),
                (0, 1, "b"),
                (0, 2, "ab"),
                (0, 2, "ba"),
                (0, 3, "aba"),
                (0, 3, "bab"),
                (1, 1, "中"),
                (1, 1, "文"),
                (1, 2, "中文"),
                (1, 2, "文中"),
                (1, 3, "中文中"),
            ),
        )

    def test_ngram_generation_uses_pre_folded_text_without_refolding(self) -> None:
        folded = "already-FOLDEDß"
        self.assertEqual(unique_character_ngrams(folded, 1)[-1], "ß")
        plan = build_candidate_write_plan(
            (SQLiteCandidateRecord(origin_ordinal=0, source_fold_v1=folded),),
            fts5_available=False,
        )
        self.assertIn(
            (1, "ß"),
            tuple((row.gram_size, row.gram) for row in plan.gram_rows),
        )
        self.assertNotIn(
            (2, "ss"),
            tuple((row.gram_size, row.gram) for row in plan.gram_rows),
        )

    def test_write_plan_indexes_pre_folded_input_without_transforming_it(self) -> None:
        index = FTS5TrigramIndex(available=True)
        records = (
            SQLiteCandidateRecord(
                origin_ordinal=0,
                source_fold_v1='already-FOLDED\xdf "value"',
            ),
        )

        plan = index.write_plan(records)

        self.assertEqual(plan.fts_origin_ordinals, (0,))
        self.assertEqual(records[0].source_fold_v1, 'already-FOLDED\xdf "value"')

    def test_unique_trigrams_and_phrase_escaping_are_exact(self) -> None:
        query = 'a"b OR*a"b'

        trigrams = unique_character_trigrams(query)
        expression = build_fts5_match_expression(trigrams)

        self.assertEqual(
            trigrams,
            ('a"b', '"b ', 'b O', ' OR', 'OR*', 'R*a', '*a"'),
        )
        self.assertEqual(
            expression,
            '"a""b" OR """b " OR "b O" OR " OR" OR "OR*" '
            'OR "R*a" OR "*a"""',
        )

    def test_contentful_index_and_escaped_or_union_return_stable_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            snapshot = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            if not snapshot.fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            index = FTS5TrigramIndex(available=True)
            inserted = store.append_batch(
                batch_id="import.fts",
                kind="import",
                drafts=(
                    _draft('A"B OR*', "one"),
                    _draft("xx or yy", "two"),
                    _draft("unrelated", "three"),
                ),
                source_digest="1" * 64,
                source_path=(Path(temporary) / "source.jsonl").resolve(),
                extension=index.write_plan,
            )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                content = connection.execute(
                    "SELECT source_fold_v1, record_id FROM tm_fts "
                    "ORDER BY CAST(record_id AS INTEGER)"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                content,
                [
                    ('a"b or*', inserted[0].record_id),
                    ("xx or yy", inserted[1].record_id),
                    ("unrelated", inserted[2].record_id),
                ],
            )

            first = index.candidates(store, 'a"b OR*'.casefold())
            self.assertTrue(first.available)
            self.assertIsNone(first.unavailable_code)
            self.assertEqual(
                first.record_ids,
                tuple(sorted((inserted[0].record_id, inserted[1].record_id))),
            )
            self.assertEqual(
                index.candidates(store, 'a"b OR*'.casefold()).record_ids,
                first.record_ids,
            )

    def test_candidate_set_is_deterministic_when_sql_rows_are_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            snapshot = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            if not snapshot.fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            index = FTS5TrigramIndex(available=True)
            inserted = store.append_batch(
                batch_id="import.order",
                kind="import",
                drafts=(
                    _draft("abcdef", "one"),
                    _draft("zzabc", "two"),
                    _draft("abczz", "three"),
                ),
                source_digest="2" * 64,
                source_path=(Path(temporary) / "source.jsonl").resolve(),
                extension=index.write_plan,
            )
            original = store.fts5_candidate_ids

            def reversed_rows(expression: str) -> tuple[int, ...]:
                rows = original(expression)
                self.assertIsNotNone(rows)
                return tuple(reversed(rows or ()))

            with patch.object(store, "fts5_candidate_ids", side_effect=reversed_rows):
                result = index.candidates(store, "abcdef")

            self.assertEqual(
                result.record_ids,
                tuple(sorted(record.record_id for record in inserted)),
            )

    def test_fts_write_failure_rolls_back_origin_record_index_and_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            snapshot = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            if not snapshot.fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            index = FTS5TrigramIndex(available=True)
            from tm_sqlite_store import (  # pyright: ignore[reportPrivateUsage]
                _ValidatedCandidateWritePlan,
                _apply_candidate_write_plan,
            )

            def write_then_fail(
                connection: sqlite3.Connection,
                plan: _ValidatedCandidateWritePlan,
                *,
                record_ids_by_ordinal: dict[int, int],
                folded_sources_by_ordinal: dict[int, str],
            ) -> None:
                _apply_candidate_write_plan(
                    connection,
                    plan,
                    record_ids_by_ordinal=record_ids_by_ordinal,
                    folded_sources_by_ordinal=folded_sources_by_ordinal,
                )
                raise SQLiteStoreSchemaError("STORE.INJECTED_INDEX_FAILURE")

            with (
                patch(
                    "tm_sqlite_store._apply_candidate_write_plan",
                    side_effect=write_then_fail,
                ),
                self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "STORE.INJECTED_INDEX_FAILURE",
                ),
            ):
                _ = store.append_batch(
                    batch_id="import.rollback",
                    kind="import",
                    drafts=(_draft("abcdef", "target"),),
                    source_digest="3" * 64,
                    source_path=(Path(temporary) / "source.jsonl").resolve(),
                    extension=index.write_plan,
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), (0,), ("0",)))

    def test_no_fts_is_explicitly_unavailable_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                snapshot = initialize_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )
                self.assertFalse(snapshot.fts5_available)
                store = SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )
                index = FTS5TrigramIndex(available=False)

                record = store.append_batch(
                    batch_id="import.no-fts",
                    kind="import",
                    drafts=(_draft("abcdef", "target"),),
                    source_digest="4" * 64,
                    source_path=(Path(temporary) / "source.jsonl").resolve(),
                    extension=index.write_plan,
                )[0]
                result = index.candidates(store, "abcdef")

            self.assertFalse(result.available)
            self.assertEqual(result.unavailable_code, "CANDIDATE.FTS5_UNAVAILABLE")
            self.assertEqual(result.record_ids, ())
            self.assertNotIn(record.record_id, result.record_ids)


class GramPostingIndexTests(unittest.TestCase):
    def _store_with_records(
        self,
        root: Path,
        *,
        fts5_available: bool,
        sources: tuple[str, ...],
    ) -> tuple[SQLiteTMStore, GramPostingIndex, tuple[int, ...]]:
        stage = _stage(root)
        probe = patch("tm_sqlite_store._probe_fts5", return_value=fts5_available)
        with probe:
            snapshot = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
        self.assertEqual(snapshot.fts5_available, fts5_available)
        index = GramPostingIndex(fts5_available=fts5_available)
        inserted = store.append_batch(
            batch_id="import.grams",
            kind="import",
            drafts=tuple(_draft(source, f"target-{offset}") for offset, source in enumerate(sources)),
            source_digest="5" * 64,
            source_path=(root / "source.jsonl").resolve(),
            extension=index.write_plan,
        )
        return store, index, tuple(record.record_id for record in inserted)

    def test_one_and_two_character_queries_use_only_corresponding_postings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, index, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("猫", "猫狗", "狗猫"),
            )

            one = index.candidates(store, "猫", limit=10)
            two = index.candidates(store, "猫狗", limit=10)

        self.assertTrue(one.available)
        self.assertEqual(one.path, "GRAM_1_SHORT")
        self.assertEqual(one.query_postings, ((1, "猫"),))
        self.assertEqual(one.record_ids, record_ids)
        self.assertTrue(two.available)
        self.assertEqual(two.path, "GRAM_2_SHORT")
        self.assertEqual(two.query_postings, ((2, "猫狗"),))
        self.assertEqual(two.record_ids, (record_ids[1],))

    def test_no_fts_long_cjk_query_unions_123_postings_with_overlap_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, index, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("你好世界", "你好大家", "世界你好", "完全无关"),
            )

            first = index.candidates(store, "你好世界", limit=10)
            second = index.candidates(store, "你好世界", limit=10)

        self.assertTrue(first.available)
        self.assertEqual(first.path, "GRAM_123_FALLBACK")
        self.assertEqual(first, second)
        self.assertTrue(first.record_ids)
        self.assertEqual(first.record_ids[0], record_ids[0])
        self.assertNotIn(record_ids[3], first.record_ids)
        self.assertEqual(first.evidence[0].matched_postings, len(first.query_postings))
        self.assertEqual(first.evidence[0].overlap_ratio, 1.0)
        self.assertEqual(
            tuple(size for size, _gram in first.query_postings[:2]),
            (3, 3),
        )

    def test_sql_row_order_does_not_change_order_and_limits_obey_both_caps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _index, _ = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abc", "abd", "abe", "abf"),
            )
            index = GramPostingIndex(fts5_available=False, hard_cap=2)
            original = store.gram_candidate_overlaps

            def reversed_rows(
                query_postings: tuple[tuple[int, str], ...],
                *,
                candidate_cap: int,
            ) -> tuple[tuple[int, int], ...]:
                return tuple(reversed(original(query_postings, candidate_cap=candidate_cap)))

            with patch.object(store, "gram_candidate_overlaps", side_effect=reversed_rows):
                hard_capped = index.candidates(store, "abz", limit=20)
                caller_capped = index.candidates(store, "abz", limit=1)

        self.assertEqual(len(hard_capped.record_ids), 2)
        self.assertEqual(len(caller_capped.record_ids), 1)
        self.assertEqual(caller_capped.record_ids, hard_capped.record_ids[:1])

    def test_fts_long_query_reports_that_gram_path_was_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, index, _ = self._store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("abcdef",),
            )
            with patch.object(
                store,
                "gram_candidate_overlaps",
                side_effect=AssertionError("gram query must not run"),
            ):
                result = index.candidates(store, "abcdef", limit=10)

        self.assertFalse(result.available)
        self.assertEqual(result.path, "FTS_TRIGRAM")
        self.assertEqual(result.unavailable_code, GRAM_LONG_QUERY_FTS_SELECTED_CODE)

    def test_gram_write_failure_rolls_back_record_index_batch_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                _ = initialize_stage_schema(stage, canonical_store_id="store.primary")
                store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            index = GramPostingIndex(fts5_available=False)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "CREATE TRIGGER inject_gram_failure BEFORE INSERT ON tm_gram "
                    "BEGIN SELECT RAISE(ABORT, 'injected gram failure'); END"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(sqlite3.IntegrityError):
                _ = store.append_batch(
                    batch_id="import.gram-rollback",
                    kind="import",
                    drafts=(_draft("abcdef", "target"),),
                    source_digest="6" * 64,
                    source_path=(root / "source.jsonl").resolve(),
                    extension=index.write_plan,
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                state = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute("SELECT value FROM tm_meta WHERE key='head_revision'").fetchone(),
                )
            finally:
                connection.close()
        self.assertEqual(state, ((0,), (0,), (0,), ("0",)))

    def test_gram_query_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            store, index, _ = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abcdef",),
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_gram")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(SQLiteStoreSchemaError, "STORE.GRAM_QUERY_FAILED"):
                _ = index.candidates(store, "abc", limit=10)

    def test_subtype_inputs_are_rejected_before_store_query(self) -> None:
        calls: list[str] = []

        class StringSubtype(str):
            def __hash__(self) -> int:
                calls.append("hash")
                return super().__hash__()

        class IntSubtype(int):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            store, index, _ = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abcdef",),
            )
            with patch.object(
                store,
                "gram_candidate_overlaps",
                side_effect=AssertionError("store queried"),
            ) as query:
                with self.assertRaises(TypeError):
                    _ = index.candidates(store, StringSubtype("abc"), limit=10)
                with self.assertRaises(TypeError):
                    _ = index.candidates(store, "abc", limit=IntSubtype(10))
                query.assert_not_called()
        self.assertEqual(calls, [])

    def test_nested_plan_and_store_query_subtypes_are_rejected_before_hash_or_connection(
        self,
    ) -> None:
        dispatches: list[str] = []

        class StringSubtype(str):
            def __hash__(self) -> int:
                dispatches.append("hash")
                return super().__hash__()

        class IntSubtype(int):
            pass

        class TupleSubtype(tuple[tuple[int, str], ...]):
            pass

        forged = object.__new__(SQLiteCandidateRecord)
        object.__setattr__(forged, "origin_ordinal", 0)
        object.__setattr__(forged, "source_fold_v1", StringSubtype("abc"))
        with self.assertRaises(ValueError):
            _ = build_candidate_write_plan((forged,), fts5_available=False)
        self.assertEqual(dispatches, [])

        with tempfile.TemporaryDirectory() as temporary:
            store, _index, _ = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abcdef",),
            )
            calls = (
                lambda: store.gram_candidate_overlaps(
                    TupleSubtype(((1, "a"),)), candidate_cap=1
                ),
                lambda: store.gram_candidate_overlaps(
                    ((IntSubtype(1), "a"),), candidate_cap=1
                ),
                lambda: store.gram_candidate_overlaps(
                    ((1, StringSubtype("a")),), candidate_cap=1
                ),
                lambda: store.gram_candidate_overlaps(
                    ((1, "a"),), candidate_cap=IntSubtype(1)
                ),
            )
            for call in calls:
                with patch(
                    "tm_sqlite_store._open_configured_connection",
                    side_effect=AssertionError("connection opened"),
                ) as open_connection:
                    with self.assertRaises(TypeError):
                        _ = call()
                    open_connection.assert_not_called()
        self.assertEqual(dispatches, [])


class CandidateRetrieverTests(unittest.TestCase):
    def _store_with_records(
        self,
        root: Path,
        *,
        fts5_available: bool,
        sources: tuple[str, ...],
    ) -> tuple[SQLiteTMStore, tuple[int, ...]]:
        stage = _stage(root)
        with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
            snapshot = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(stage, canonical_store_id="store.primary")
        self.assertEqual(snapshot.fts5_available, fts5_available)
        records = store.append_batch(
            batch_id="import.retriever",
            kind="import",
            drafts=tuple(
                _draft(source, f"target-{ordinal}")
                for ordinal, source in enumerate(sources)
            ),
            source_digest="7" * 64,
            source_path=(root / "source.jsonl").resolve(),
            extension=lambda values: build_candidate_write_plan(
                values,
                fts5_available=fts5_available,
            ),
        )
        return store, tuple(record.record_id for record in records)

    def test_budget_formula_and_short_cjk_path_return_frozen_recall_report(self) -> None:
        self.assertEqual(CANDIDATE_CONTRACT_FLOOR, 2048)
        self.assertEqual(candidate_budget_v1(1), 2048)
        self.assertEqual(candidate_budget_v1(16), 2048)
        self.assertEqual(candidate_budget_v1(17), 2176)
        self.assertEqual(candidate_budget_v1(1000), 8192)
        with tempfile.TemporaryDirectory() as temporary:
            store, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("猫", "猫狗", "狗猫"),
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "猫", result_limit=1
            )

        self.assertEqual(
            tuple(candidate.record_id for candidate in report.candidates),
            record_ids,
        )
        self.assertEqual(report.metadata.index_kind, "GRAM_FALLBACK")
        self.assertEqual(report.metadata.result_limit, 1)
        self.assertEqual(report.metadata.candidate_budget, 2048)
        self.assertEqual(
            tuple(stage.stage for stage in report.metadata.stages),
            (
                CandidateStage.GRAM_1,
                CandidateStage.UNION,
                CandidateStage.DEDUPLICATE,
            ),
        )
        self.assertTrue(all(candidate.overlap_ratio == 1.0 for candidate in report.candidates))
        self.assertTrue(
            all(
                candidate.pretruncate_rank is not None
                for candidate in report.candidates
            )
        )

    def test_fts_empty_or_low_pool_unions_grams_with_continuous_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("abcdef", "abcxyz", "abzzzz", "a-only"),
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "abcdef", result_limit=10
            )

        self.assertEqual(
            tuple(stage.stage for stage in report.metadata.stages),
            (
                CandidateStage.FTS_TRIGRAM,
                CandidateStage.GRAM_2,
                CandidateStage.GRAM_1,
                CandidateStage.UNION,
                CandidateStage.DEDUPLICATE,
            ),
        )
        prior = 0
        for stage in report.metadata.stages:
            self.assertEqual(stage.input_count, prior)
            self.assertEqual(
                stage.input_count + stage.added_unique_count - stage.dropped_count,
                stage.output_unique_count,
            )
            prior = stage.output_unique_count
        self.assertEqual(
            {candidate.record_id for candidate in report.candidates},
            set(record_ids),
        )
        exact = next(
            candidate
            for candidate in report.candidates
            if candidate.record_id == record_ids[0]
        )
        self.assertEqual(
            exact.recall_stages,
            (CandidateStage.FTS_TRIGRAM, CandidateStage.GRAM_2, CandidateStage.GRAM_1),
        )
        self.assertEqual(exact.overlap_ratio, 1.0)

    def test_empty_fts_pool_is_rescued_by_two_gram_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=True,
                sources=("abx", "unrelated"),
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "abc", result_limit=10
            )
        self.assertEqual(report.metadata.stages[0].stage, CandidateStage.FTS_TRIGRAM)
        self.assertEqual(report.metadata.stages[0].output_unique_count, 0)
        self.assertEqual(report.metadata.stages[1].stage, CandidateStage.GRAM_2)
        self.assertIn(record_ids[0], tuple(item.record_id for item in report.candidates))
        rescued = next(
            item for item in report.candidates if item.record_id == record_ids[0]
        )
        self.assertEqual((rescued.matched_grams, rescued.query_grams), (3, 6))
        self.assertEqual(rescued.overlap_ratio, 0.5)

    def test_no_fts_long_path_is_321_and_preorder_uses_ratio_length_then_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abcx", "abcy", "abc-long", "zzz"),
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "abcd", result_limit=10
            )

        self.assertEqual(report.metadata.index_kind, "GRAM_FALLBACK")
        self.assertEqual(
            tuple(stage.stage for stage in report.metadata.stages[:3]),
            (CandidateStage.GRAM_3, CandidateStage.GRAM_2, CandidateStage.GRAM_1),
        )
        self.assertEqual(
            tuple(candidate.record_id for candidate in report.candidates[:2]),
            record_ids[:2],
        )
        self.assertNotIn(record_ids[3], tuple(c.record_id for c in report.candidates))

    def test_empty_query_is_explicitly_unavailable_and_does_not_fake_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary), fts5_available=False, sources=("abc",)
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "", result_limit=10
            )
        self.assertFalse(report.metadata.fuzzy_available)
        self.assertEqual(report.metadata.fuzzy_unavailable_code, "CANDIDATE.GRAM_QUERY_EMPTY")
        self.assertEqual(report.metadata.stages, ())
        self.assertEqual(report.candidates, ())

    def test_sql_row_order_does_not_affect_pretruncate_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary),
                fts5_available=False,
                sources=("abcx", "abcy", "abcz"),
            )
            first = CandidateRetriever().candidates(
                "tm.primary", store, "abcd", result_limit=10
            )
            original = store.candidate_recall_snapshot

            def disorder(*args: object, **kwargs: object):
                snapshot = original(*args, **kwargs)
                return type(snapshot)(
                    fts5_available=snapshot.fts5_available,
                    stage_matches=tuple(
                        (stage, tuple(reversed(matches)))
                        for stage, matches in snapshot.stage_matches
                    ),
                    folded_sources=tuple(reversed(snapshot.folded_sources)),
                )

            with patch.object(store, "candidate_recall_snapshot", side_effect=disorder):
                second = CandidateRetriever().candidates(
                    "tm.primary", store, "abcd", result_limit=10
                )
        self.assertEqual(first, second)

    def test_pool_above_budget_truncates_only_after_stable_preorder(self) -> None:
        sources = tuple(f"abc{i:04d}" for i in range(CANDIDATE_CONTRACT_FLOOR + 1))
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary), fts5_available=False, sources=sources
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "abc0000", result_limit=1
            )
        self.assertTrue(report.metadata.truncated)
        self.assertEqual(len(report.candidates), 2048)
        self.assertEqual(report.metadata.union_unique_count, 2049)
        self.assertEqual(report.metadata.deduplicated_count, 2049)
        self.assertEqual(report.metadata.stages[-1].stage, CandidateStage.TRUNCATE)
        self.assertEqual(report.metadata.stages[-1].dropped_count, 1)
        self.assertEqual(
            tuple(candidate.pretruncate_rank for candidate in report.candidates),
            tuple(range(1, 2049)),
        )

    def test_non_degenerate_fts_pool_at_contract_floor_skips_fallback(self) -> None:
        sources = tuple(
            f"abcd-{ordinal:04d}" for ordinal in range(CANDIDATE_CONTRACT_FLOOR)
        )
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary), fts5_available=True, sources=sources
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "abcd", result_limit=1
            )
        self.assertEqual(
            tuple(stage.stage for stage in report.metadata.stages),
            (
                CandidateStage.FTS_TRIGRAM,
                CandidateStage.UNION,
                CandidateStage.DEDUPLICATE,
            ),
        )
        self.assertEqual(len(report.candidates), CANDIDATE_CONTRACT_FLOOR)
        self.assertFalse(report.metadata.truncated)

    def test_degenerate_repeated_trigram_forces_two_gram_union(self) -> None:
        sources = tuple(
            f"aaaa-{ordinal:04d}" for ordinal in range(CANDIDATE_CONTRACT_FLOOR)
        )
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary), fts5_available=True, sources=sources
            )
            report = CandidateRetriever().candidates(
                "tm.primary", store, "aaaa", result_limit=1
            )
        self.assertEqual(
            tuple(stage.stage for stage in report.metadata.stages[:2]),
            (CandidateStage.FTS_TRIGRAM, CandidateStage.GRAM_2),
        )
        self.assertNotIn(
            CandidateStage.GRAM_1,
            tuple(stage.stage for stage in report.metadata.stages),
        )

    def test_candidate_query_failure_is_resource_local_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store_with_records(
                root, fts5_available=False, sources=("abcdef",)
            )
            connection = sqlite3.connect(_stage(root).staged_db_path)
            try:
                connection.execute("DROP TABLE tm_gram")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError, "STORE.CANDIDATE_QUERY_FAILED"
            ):
                _ = CandidateRetriever().candidates(
                    "tm.primary", store, "abcdef", result_limit=10
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store_with_records(
                root, fts5_available=True, sources=("abcdef",)
            )
            connection = sqlite3.connect(_stage(root).staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError, "STORE.CANDIDATE_QUERY_FAILED"
            ):
                _ = CandidateRetriever().candidates(
                    "tm.primary", store, "abcdef", result_limit=10
                )

    def test_rejects_nested_forgery_before_store_query(self) -> None:
        class StringSubtype(str):
            def __hash__(self) -> int:
                raise AssertionError("forged query was hashed")

        class IntSubtype(int):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store_with_records(
                Path(temporary), fts5_available=False, sources=("abc",)
            )
            with patch.object(
                store,
                "candidate_recall_snapshot",
                side_effect=AssertionError("store queried"),
            ) as query:
                with self.assertRaises(TypeError):
                    _ = CandidateRetriever().candidates(
                        StringSubtype("tm.primary"), store, "abc", result_limit=10
                    )
                with self.assertRaises(TypeError):
                    _ = CandidateRetriever().candidates(
                        "tm.primary", store, StringSubtype("abc"), result_limit=10
                    )
                with self.assertRaises(TypeError):
                    _ = CandidateRetriever().candidates(
                        "tm.primary", store, "abc", result_limit=IntSubtype(10)
                    )
                query.assert_not_called()

            forged = object.__new__(SQLiteCandidateRecallSnapshot)
            object.__setattr__(forged, "fts5_available", False)
            object.__setattr__(
                forged,
                "stage_matches",
                (("GRAM_3", ((1, 1),)),),
            )
            object.__setattr__(
                forged,
                "folded_sources",
                ((1, StringSubtype("abc")),),
            )
            with patch.object(
                store, "candidate_recall_snapshot", return_value=forged
            ):
                with self.assertRaises(TypeError):
                    _ = CandidateRetriever().candidates(
                        "tm.primary", store, "abc", result_limit=10
                    )


if __name__ == "__main__":
    unittest.main()
