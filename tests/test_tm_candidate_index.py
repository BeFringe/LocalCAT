from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tm_candidate_index import (
    FTS5TrigramIndex,
    build_fts5_match_expression,
    unique_character_trigrams,
)
from tm_contracts import CanonicalResourceIdentity, MutableStageRef, TMRecordDraft
from tm_sqlite_store import (
    SQLiteCandidateRecord,
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
                    connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

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


if __name__ == "__main__":
    unittest.main()
