"""Wave 3 write-path delegation, transaction, and authority guards."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, ExitStack
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_sqlite_candidate_projection as projection
import tm_sqlite_store
from tm_candidate_store_contracts import SQLiteCandidateWritePlan
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    TMRecordDraft,
)
from tm_sqlite_store import (
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
        provenance=(("source", "wave3-write-delegation"),),
    )


def _store(root: Path) -> tuple[MutableStageRef, SQLiteTMStore, bool]:
    stage = _stage(root)
    snapshot = initialize_stage_schema(
        stage,
        canonical_store_id="store.primary",
    )
    return (
        stage,
        SQLiteTMStore(stage, canonical_store_id="store.primary"),
        snapshot.fts5_available,
    )


def _disk_state(stage: MutableStageRef) -> tuple[bytes, tuple[object, ...]]:
    connection = sqlite3.connect(stage.staged_db_path)
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        def count(table: str) -> tuple[int] | None:
            return (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                if table in table_names
                else None
            )

        fts_count = (
            connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone()
            if "tm_fts" in table_names
            else None
        )
        facts: tuple[object, ...] = (
            connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'head_revision'"
            ).fetchone(),
            connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
            count("tm_gram"),
            count("tm_candidate_block"),
            count("tm_gram_block_max"),
            fts_count,
            connection.execute(
                "SELECT batch_id, status, valid_count, completed_revision "
                "FROM tm_origin_batch ORDER BY batch_id"
            ).fetchall(),
        )
    finally:
        connection.close()
    return stage.staged_db_path.read_bytes(), facts


@contextmanager
def _track_leased_connections(
    exits: list[bool],
) -> Iterator[None]:
    original = tm_sqlite_store._open_leased_connection

    @contextmanager
    def tracked(lease: object) -> Iterator[sqlite3.Connection]:
        with original(lease) as connection:
            try:
                yield connection
            finally:
                exits.append(connection.in_transaction)

    with patch.object(
        tm_sqlite_store,
        "_open_leased_connection",
        tracked,
    ):
        yield


class _HostileAuthorityResult:
    generation = 99_999
    head_revision = 99_999
    fts5_available = not False
    candidate_index_kind = "HOSTILE"
    receipt = object()
    binding = object()


class CandidateProjectionWriteDelegationTests(unittest.TestCase):
    def test_append_apply_delegates_inside_store_transaction_without_authority_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, _fts5_available = _store(root)
            original_project = projection.project_candidate_write_plan
            original_gram = projection.insert_candidate_gram_rows
            original_fts = projection.insert_candidate_fts_rows
            original_summary = projection.maintain_candidate_proof_summaries
            original_seam = tm_sqlite_store._apply_candidate_write_plan
            observed_transactions: list[tuple[str, bool]] = []
            observed_keywords: list[frozenset[str]] = []
            connection_exits: list[bool] = []

            def observe_project(
                *args: object,
                **kwargs: object,
            ) -> object:
                observed_keywords.append(frozenset(kwargs))
                return original_project(*args, **kwargs)

            def observe_sql(label: str, implementation: object):
                def observe(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    observed_transactions.append(
                        (label, connection.in_transaction)
                    )
                    result = cast(Any, implementation)(
                        connection,
                        *args,
                        **kwargs,
                    )
                    self.assertTrue(connection.in_transaction)
                    self.assertIsNone(result)
                    return result

                return observe

            def observe_seam(
                connection: sqlite3.Connection,
                *args: object,
                **kwargs: object,
            ) -> object:
                observed_transactions.append(("seam", connection.in_transaction))
                result = original_seam(connection, *args, **kwargs)
                self.assertTrue(connection.in_transaction)
                return result

            with (
                _track_leased_connections(connection_exits),
                patch.object(
                    tm_sqlite_store,
                    "_apply_candidate_write_plan",
                    side_effect=observe_seam,
                ) as late_bound_seam,
                patch.object(
                    projection,
                    "project_candidate_write_plan",
                    side_effect=observe_project,
                ) as projected,
                patch.object(
                    projection,
                    "insert_candidate_gram_rows",
                    side_effect=observe_sql("gram", original_gram),
                ) as gram_insert,
                patch.object(
                    projection,
                    "insert_candidate_fts_rows",
                    side_effect=observe_sql("fts", original_fts),
                ) as fts_insert,
                patch.object(
                    projection,
                    "maintain_candidate_proof_summaries",
                    side_effect=observe_sql("summary", original_summary),
                ) as summary_insert,
            ):
                inserted = store.append_batch(
                    batch_id="import.delegated",
                    kind="import",
                    drafts=(_draft("alpha", "target"),),
                    source_digest="a" * 64,
                    source_path=(root / "source.jsonl").resolve(),
                )

            self.assertEqual(projected.call_count, 3)
            gram_insert.assert_called_once()
            fts_insert.assert_called_once()
            summary_insert.assert_called_once()
            late_bound_seam.assert_called_once()
            self.assertEqual(
                observed_transactions,
                [
                    ("seam", True),
                    ("gram", True),
                    ("fts", True),
                    ("summary", True),
                ],
            )
            self.assertEqual(connection_exits, [False])
            self.assertEqual(len(inserted), 1)
            self.assertEqual(store.canonical_revision().head_revision, 1)
            self.assertEqual(store.canonical_revision().record_count, 1)
            self.assertEqual(
                observed_keywords,
                [
                    frozenset(
                        {
                            "record_ids_by_ordinal",
                            "folded_sources_by_ordinal",
                        }
                    )
                ]
                * 3,
            )
            self.assertEqual(
                _disk_state(stage)[1][-1],
                [("import.delegated", "completed", 1, 1)],
            )

    def test_streamed_write_and_index_build_delegate_inside_store_transactions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, _fts5_available = _store(root)
            original_gram = projection.insert_streamed_candidate_gram_rows
            original_fts = projection.insert_streamed_candidate_fts_rows
            original_proof = projection.insert_streamed_candidate_proof_rows
            original_suspend = (
                projection.suspend_streamed_stage_secondary_indexes
            )
            original_restore = (
                projection.restore_streamed_stage_secondary_indexes
            )
            original_insert_seam = tm_sqlite_store._insert_streamed_candidate_index
            original_suspend_seam = (
                tm_sqlite_store._suspend_streamed_stage_secondary_indexes
            )
            original_restore_seam = (
                tm_sqlite_store._restore_streamed_stage_secondary_indexes
            )
            original_frequencies = tm_sqlite_store.character_ngram_frequencies
            observed: list[tuple[str, bool, frozenset[str]]] = []
            gram_sizes: list[int] = []
            connection_exits: list[bool] = []

            def observer(label: str, implementation: object):
                def observe(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    observed.append(
                        (label, connection.in_transaction, frozenset(kwargs))
                    )
                    result = cast(Any, implementation)(
                        connection,
                        *args,
                        **kwargs,
                    )
                    self.assertTrue(connection.in_transaction)
                    self.assertIsNone(result)
                    return result

                return observe

            def observe_gram(
                connection: sqlite3.Connection,
                *args: object,
                **kwargs: object,
            ) -> object:
                gram_sizes.append(cast(int, kwargs["gram_size"]))
                return observer("gram", original_gram)(
                    connection,
                    *args,
                    **kwargs,
                )

            with (
                _track_leased_connections(connection_exits),
                patch.object(
                    tm_sqlite_store,
                    "_insert_streamed_candidate_index",
                    wraps=original_insert_seam,
                ) as insert_seam,
                patch.object(
                    tm_sqlite_store,
                    "_suspend_streamed_stage_secondary_indexes",
                    wraps=original_suspend_seam,
                ) as suspend_seam,
                patch.object(
                    tm_sqlite_store,
                    "_restore_streamed_stage_secondary_indexes",
                    wraps=original_restore_seam,
                ) as restore_seam,
                patch.object(
                    projection,
                    "insert_streamed_candidate_gram_rows",
                    side_effect=observe_gram,
                ) as gram_projection,
                patch.object(
                    projection,
                    "insert_streamed_candidate_fts_rows",
                    side_effect=observer("fts", original_fts),
                ) as fts_projection,
                patch.object(
                    projection,
                    "insert_streamed_candidate_proof_rows",
                    side_effect=observer("proof", original_proof),
                ) as proof_projection,
                patch.object(
                    projection,
                    "suspend_streamed_stage_secondary_indexes",
                    side_effect=observer("suspend", original_suspend),
                ) as suspend_projection,
                patch.object(
                    projection,
                    "restore_streamed_stage_secondary_indexes",
                    side_effect=observer("restore", original_restore),
                ) as restore_projection,
                patch.object(
                    tm_sqlite_store,
                    "character_ngram_frequencies",
                    wraps=original_frequencies,
                ) as frequency_seam,
            ):
                store.append_streamed_batch(
                    batch_id="migration.streamed-delegated",
                    kind="migration",
                    drafts=iter(
                        (
                            (_draft("alpha", "first"), 1),
                            (_draft("beta", "second"), 2),
                            (_draft("gamma", "third"), 3),
                        )
                    ),
                    source_digest="b" * 64,
                    source_path=(root / "streamed.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=2,
                    _defer_secondary_indexes=True,
                )

            required_sizes = (1, 2) if _fts5_available else (1, 2, 3)
            self.assertEqual(gram_sizes, list(required_sizes) * 2)
            self.assertEqual(
                gram_projection.call_count,
                len(required_sizes) * 2,
            )
            self.assertEqual(fts_projection.call_count, 2)
            self.assertEqual(proof_projection.call_count, 2)
            self.assertEqual(insert_seam.call_count, 2)
            suspend_projection.assert_called_once()
            suspend_seam.assert_called_once()
            restore_projection.assert_called_once()
            restore_seam.assert_called_once()
            self.assertEqual(
                frequency_seam.call_count,
                3 * len(required_sizes),
            )
            self.assertEqual(
                tuple(label for label, _transaction, _keys in observed),
                (
                    "suspend",
                    *("gram",) * len(required_sizes),
                    "fts",
                    "proof",
                    *("gram",) * len(required_sizes),
                    "fts",
                    "proof",
                    "restore",
                ),
            )
            self.assertTrue(all(transaction for _, transaction, _ in observed))
            self.assertEqual(connection_exits, [False])
            revision = store.canonical_revision()
            self.assertEqual(
                (revision.head_revision, revision.record_count),
                (1, 3),
            )
            self.assertEqual(
                _disk_state(stage)[1][-1],
                [("migration.streamed-delegated", "completed", 3, 1)],
            )

    def test_streamed_projection_materializes_one_gram_size_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _stage_ref, store, fts5_available = _store(root)
            original_gram = projection.insert_streamed_candidate_gram_rows
            observed: list[tuple[int, frozenset[int], int]] = []

            def observe_gram(
                connection: sqlite3.Connection,
                candidate_records: object,
                record_ids_by_ordinal: object,
                candidate_gram_facts: object,
                *,
                gram_size: int,
            ) -> object:
                self.assertIs(type(candidate_records), tuple)
                self.assertIs(type(record_ids_by_ordinal), tuple)
                self.assertIs(type(candidate_gram_facts), tuple)
                facts = cast(tuple[tuple[int, int, str, int], ...], candidate_gram_facts)
                observed.append(
                    (
                        gram_size,
                        frozenset(fact[1] for fact in facts),
                        len(facts),
                    )
                )
                return original_gram(
                    connection,
                    cast(tuple[tuple[int, str], ...], candidate_records),
                    cast(tuple[tuple[int, int], ...], record_ids_by_ordinal),
                    facts,
                    gram_size=gram_size,
                )

            with (
                patch.object(
                    projection,
                    "insert_streamed_candidate_gram_rows",
                    side_effect=observe_gram,
                ) as gram_insert,
            ):
                store.append_streamed_batch(
                    batch_id="migration.gram-size-projection",
                    kind="migration",
                    drafts=(
                        (
                            _draft(f"source-{index:05d}", f"target-{index:05d}"),
                            index + 1,
                        )
                        for index in range(513)
                    ),
                    source_digest="d" * 64,
                    source_path=(root / "gram-size.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=1_024,
                )

            required_sizes = (1, 2) if fts5_available else (1, 2, 3)
            self.assertEqual(
                tuple(item[0] for item in observed),
                required_sizes,
            )
            self.assertTrue(all(item[1] == {item[0]} for item in observed))
            self.assertTrue(all(item[2] > 0 for item in observed))
            self.assertEqual(gram_insert.call_count, len(required_sizes))
            revision = store.canonical_revision()
            self.assertEqual(
                (revision.head_revision, revision.record_count),
                (1, 513),
            )

    def test_streamed_gram_partition_preserves_frozen_rowids_and_digest(
        self,
    ) -> None:
        cases = (
            (
                "fallback",
                False,
                2_658,
                "233621e2f66bf934118fac352585ec6e2e215a7f84504ca7d2f42116d8054e10",
            ),
            (
                "fts",
                True,
                2_007,
                "7ad37835f6d7fb3efa85590eacc40db6a0cf601bcb41d9caafe57566898698b2",
            ),
        )
        for label, expected_fts, expected_grams, expected_digest in cases:
            with self.subTest(path=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if expected_fts:
                    stage, store, fts5_available = _store(root)
                    if not fts5_available:
                        continue
                else:
                    with patch.object(
                        tm_sqlite_store,
                        "_probe_fts5",
                        return_value=False,
                    ):
                        stage, store, fts5_available = _store(root)
                self.assertEqual(fts5_available, expected_fts)
                sources = (
                    "z",
                    "a",
                    "Ω",
                    "Cafe\u0301",
                    *(
                        ("ba" if index % 2 else "ab") + str(index)
                        for index in range(253)
                    ),
                )
                store.append_streamed_batch(
                    batch_id=f"migration.rowid-parity-{label}",
                    kind="migration",
                    drafts=iter(
                        (
                            _draft(source, f"target-{index}"),
                            index + 1,
                        )
                        for index, source in enumerate(sources)
                    ),
                    source_digest="a" * 64,
                    source_path=(root / f"rowid-parity-{label}.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=129,
                )
                connection = sqlite3.connect(stage.staged_db_path)
                try:
                    gram_rows = connection.execute(
                        "SELECT rowid, gram_size, gram, record_id, "
                        "term_frequency FROM tm_gram ORDER BY rowid"
                    ).fetchall()
                    maximum_rows = connection.execute(
                        "SELECT rowid, gram_size, gram, block_id, "
                        "max_term_frequency FROM tm_gram_block_max "
                        "ORDER BY rowid"
                    ).fetchall()
                    block_rows = connection.execute(
                        "SELECT block_id, record_count FROM tm_candidate_block "
                        "ORDER BY block_id"
                    ).fetchall()
                    digest = projection.candidate_proof_projection_digest(
                        connection,
                        fts5_available=fts5_available,
                    )
                finally:
                    connection.close()

                self.assertEqual(len(gram_rows), expected_grams)
                self.assertEqual(len(maximum_rows), 149)
                self.assertEqual(
                    tuple(row[0] for row in gram_rows),
                    tuple(range(1, len(gram_rows) + 1)),
                )
                self.assertEqual(
                    tuple(row[0] for row in maximum_rows),
                    tuple(range(1, len(maximum_rows) + 1)),
                )
                maximum_payload = json.dumps(
                    maximum_rows,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(maximum_payload).hexdigest(),
                    "eb6e682e51c75760e2257290eb9ac4b5a504dace33ded4a493a2d4ff6bd5ab39",
                )
                self.assertEqual(block_rows, [(0, 256), (1, 1)])
                self.assertEqual(digest, expected_digest)

    def test_streamed_fts_fault_mapping_is_narrow(self) -> None:
        cases = (
            (
                "gram",
                "insert_streamed_candidate_gram_rows",
                sqlite3.OperationalError,
            ),
            ("fts", "insert_streamed_candidate_fts_rows", sqlite3.OperationalError),
            (
                "proof",
                "insert_streamed_candidate_proof_rows",
                sqlite3.OperationalError,
            ),
        )
        for label, helper_name, expected_error in cases:
            with self.subTest(helper=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store, fts5_available = _store(root)
                if label == "fts" and not fts5_available:
                    continue
                before = _disk_state(stage)
                fault = sqlite3.OperationalError("secret projection SQL body")
                with (
                    patch.object(projection, helper_name, side_effect=fault),
                    self.assertRaises(expected_error) as raised,
                ):
                    store.append_streamed_batch(
                        batch_id=f"migration.{label}-fault",
                        kind="migration",
                        drafts=iter(((_draft("alpha", "target"), 1),)),
                        source_digest="f" * 64,
                        source_path=(root / f"{label}.jsonl").resolve(),
                        invalid_count=0,
                        duplicate_source_count=0,
                        chunk_size=2,
                    )
                self.assertIs(raised.exception, fault)
                self.assertEqual(_disk_state(stage), before)

    def test_streamed_fts_maps_only_the_actual_sql_write_fault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, fts5_available = _store(root)
            if not fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts")
                connection.commit()
            finally:
                connection.close()
            before = _disk_state(stage)
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE.FTS5_UNAVAILABLE$",
            ) as raised:
                store.append_streamed_batch(
                    batch_id="migration.fts-sql-fault",
                    kind="migration",
                    drafts=iter(((_draft("alpha", "target"), 1),)),
                    source_digest="6" * 64,
                    source_path=(root / "fts-sql-fault.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=2,
                )
            self.assertNotIn("no such table", str(raised.exception).lower())
            self.assertEqual(_disk_state(stage), before)

    def test_streamed_fts_prepare_fault_is_not_relabelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, fts5_available = _store(root)
            if not fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            before = _disk_state(stage)
            fault = sqlite3.OperationalError("programmer prepare fault")
            with (
                patch.object(
                    projection,
                    "insert_streamed_candidate_gram_rows",
                    return_value=None,
                ),
                patch.object(
                    projection,
                    "_prepare_streamed_candidate_records",
                    side_effect=fault,
                ),
                self.assertRaises(sqlite3.OperationalError) as raised,
            ):
                store.append_streamed_batch(
                    batch_id="migration.fts-prepare-fault",
                    kind="migration",
                    drafts=iter(((_draft("alpha", "target"), 1),)),
                    source_digest="5" * 64,
                    source_path=(root / "fts-prepare-fault.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=2,
                )
            self.assertIs(raised.exception, fault)
            self.assertEqual(_disk_state(stage), before)

    def test_deferred_indexes_restore_before_validation_and_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _stage_ref, store, _fts5_available = _store(root)
            original_restore = (
                tm_sqlite_store._restore_streamed_stage_secondary_indexes
            )
            original_validate = tm_sqlite_store.validate_candidate_proof_index
            original_complete = tm_sqlite_store._complete_streamed_batch
            original_gram = projection.insert_streamed_candidate_gram_rows
            original_fts = projection.insert_streamed_candidate_fts_rows
            original_proof = projection.insert_streamed_candidate_proof_rows
            observed: list[tuple[str, bool]] = []

            def observe(label: str, implementation: object):
                def wrapper(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    observed.append((label, connection.in_transaction))
                    return cast(Any, implementation)(
                        connection,
                        *args,
                        **kwargs,
                    )

                return wrapper

            with (
                patch.object(
                    projection,
                    "insert_streamed_candidate_gram_rows",
                    side_effect=observe("gram", original_gram),
                ),
                patch.object(
                    projection,
                    "insert_streamed_candidate_fts_rows",
                    side_effect=observe("fts", original_fts),
                ),
                patch.object(
                    projection,
                    "insert_streamed_candidate_proof_rows",
                    side_effect=observe("proof", original_proof),
                ),
                patch.object(
                    tm_sqlite_store,
                    "_restore_streamed_stage_secondary_indexes",
                    side_effect=observe("restore", original_restore),
                ),
                patch.object(
                    tm_sqlite_store,
                    "validate_candidate_proof_index",
                    side_effect=observe("validate", original_validate),
                ),
                patch.object(
                    tm_sqlite_store,
                    "_complete_streamed_batch",
                    side_effect=observe("publish", original_complete),
                ),
            ):
                store.append_streamed_batch(
                    batch_id="migration.restore-before-validation",
                    kind="migration",
                    drafts=iter(
                        (
                            (_draft("alpha", "first"), 1),
                            (_draft("beta", "second"), 2),
                        )
                    ),
                    source_digest="c" * 64,
                    source_path=(root / "streamed.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=4,
                    _defer_secondary_indexes=True,
                )

            self.assertEqual(
                observed,
                [
                    *(("gram", True),) * (
                        2 if _fts5_available else 3
                    ),
                    ("fts", True),
                    ("proof", True),
                    ("restore", True),
                    ("validate", True),
                    ("publish", True),
                ],
            )

    def test_projection_helpers_cannot_return_authority_or_publish_partial_state(
        self,
    ) -> None:
        helpers = (
            ("ordinary-gram", "insert_candidate_gram_rows", False),
            ("ordinary-fts", "insert_candidate_fts_rows", False),
            ("ordinary-proof", "maintain_candidate_proof_summaries", False),
            ("streamed-gram", "insert_streamed_candidate_gram_rows", True),
            ("streamed-fts", "insert_streamed_candidate_fts_rows", True),
            ("streamed-proof", "insert_streamed_candidate_proof_rows", True),
        )
        for label, helper_name, streamed in helpers:
            with self.subTest(helper=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store, _fts5_available = _store(root)
                before = _disk_state(stage)
                with (
                    patch.object(
                        projection,
                        helper_name,
                        return_value=_HostileAuthorityResult(),
                    ) as hostile_helper,
                    self.assertRaisesRegex(
                        TypeError,
                        "projection returned authority",
                    ),
                ):
                    if streamed:
                        store.append_streamed_batch(
                            batch_id=f"migration.{label}",
                            kind="migration",
                            drafts=iter(((_draft("alpha", "target"), 1),)),
                            source_digest="9" * 64,
                            source_path=(root / "forged.jsonl").resolve(),
                            invalid_count=0,
                            duplicate_source_count=0,
                            chunk_size=2,
                        )
                    else:
                        store.append_batch(
                            batch_id=f"import.{label}",
                            kind="import",
                            drafts=(_draft("alpha", "target"),),
                            source_digest="9" * 64,
                            source_path=(root / "forged.jsonl").resolve(),
                        )
                hostile_helper.assert_called_once()
                self.assertEqual(_disk_state(stage), before)

    def test_noop_projection_helpers_fail_proof_validation_before_publication(
        self,
    ) -> None:
        helper_groups = (
            (
                "ordinary",
                (
                    "insert_candidate_gram_rows",
                    "insert_candidate_fts_rows",
                    "maintain_candidate_proof_summaries",
                ),
            ),
            (
                "streamed",
                (
                    "insert_streamed_candidate_gram_rows",
                    "insert_streamed_candidate_fts_rows",
                    "insert_streamed_candidate_proof_rows",
                ),
            ),
        )
        for label, helper_names in helper_groups:
            with self.subTest(path=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store, _fts5_available = _store(root)
                before = _disk_state(stage)
                patches = tuple(
                    patch.object(projection, helper_name, return_value=None)
                    for helper_name in helper_names
                )
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    validator = stack.enter_context(
                        patch.object(
                            tm_sqlite_store,
                            "validate_candidate_proof_index",
                            wraps=(
                                tm_sqlite_store.validate_candidate_proof_index
                            ),
                        )
                    )
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_INDEX_INVALID$",
                    ):
                        if label == "ordinary":
                            store.append_batch(
                                batch_id="import.noop-projection",
                                kind="import",
                                drafts=(_draft("alpha", "target"),),
                                source_digest="8" * 64,
                                source_path=(root / "noop.jsonl").resolve(),
                            )
                        else:
                            store.append_streamed_batch(
                                batch_id="migration.noop-projection",
                                kind="migration",
                                drafts=iter(((_draft("alpha", "target"), 1),)),
                                source_digest="8" * 64,
                                source_path=(root / "noop.jsonl").resolve(),
                                invalid_count=0,
                                duplicate_source_count=0,
                                chunk_size=2,
                            )
                    validator.assert_called_once()
                self.assertEqual(_disk_state(stage), before)

    def test_partial_streamed_projection_is_rejected_before_publication(
        self,
    ) -> None:
        cases = ("gram-size-two", "gram-size-three", "fts", "block", "maximum")
        for label in cases:
            with self.subTest(partial=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if label == "gram-size-three":
                    with patch.object(
                        tm_sqlite_store,
                        "_probe_fts5",
                        return_value=False,
                    ):
                        stage, store, fts5_available = _store(root)
                else:
                    stage, store, fts5_available = _store(root)
                if label == "fts" and not fts5_available:
                    continue
                before = _disk_state(stage)
                original_gram = projection.insert_streamed_candidate_gram_rows
                original_proof = projection.insert_streamed_candidate_proof_rows

                def partial_gram(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    skipped_size = 3 if label == "gram-size-three" else 2
                    if kwargs.get("gram_size") == skipped_size:
                        return None
                    return original_gram(connection, *args, **kwargs)

                def delete_maxima(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    result = original_proof(connection, *args, **kwargs)
                    connection.execute("DELETE FROM tm_gram_block_max")
                    return result

                def delete_blocks(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    result = original_proof(connection, *args, **kwargs)
                    connection.execute("DELETE FROM tm_candidate_block")
                    return result

                if label in {"gram-size-two", "gram-size-three"}:
                    helper_patch = patch.object(
                        projection,
                        "insert_streamed_candidate_gram_rows",
                        side_effect=partial_gram,
                    )
                elif label == "fts":
                    helper_patch = patch.object(
                        projection,
                        "insert_streamed_candidate_fts_rows",
                        return_value=None,
                    )
                elif label == "block":
                    helper_patch = patch.object(
                        projection,
                        "insert_streamed_candidate_proof_rows",
                        side_effect=delete_blocks,
                    )
                else:
                    helper_patch = patch.object(
                        projection,
                        "insert_streamed_candidate_proof_rows",
                        side_effect=delete_maxima,
                    )
                with (
                    helper_patch,
                    patch.object(
                        tm_sqlite_store,
                        "_complete_streamed_batch",
                        wraps=tm_sqlite_store._complete_streamed_batch,
                    ) as publish,
                    self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_INDEX_INVALID$",
                    ),
                ):
                    store.append_streamed_batch(
                        batch_id=f"migration.partial-{label}",
                        kind="migration",
                        drafts=iter(((_draft("alpha", "target"), 1),)),
                        source_digest="7" * 64,
                        source_path=(root / f"partial-{label}.jsonl").resolve(),
                        invalid_count=0,
                        duplicate_source_count=0,
                        chunk_size=2,
                    )
                publish.assert_not_called()
                self.assertEqual(_disk_state(stage), before)

    def test_proof_validation_delegates_inside_store_transaction_and_maps_sqlite_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _stage_ref, store, _fts5_available = _store(root)
            store.append_batch(
                batch_id="import.validation",
                kind="import",
                drafts=(_draft("alpha", "target"),),
                source_digest="c" * 64,
                source_path=(root / "validation.jsonl").resolve(),
            )
            original = projection._validate_candidate_proof_index_core
            original_seam = tm_sqlite_store.validate_candidate_proof_index
            observed_transactions: list[bool] = []
            connection_exits: list[bool] = []

            def observe(
                connection: sqlite3.Connection,
                *args: object,
                **kwargs: object,
            ) -> object:
                observed_transactions.append(connection.in_transaction)
                return original(connection, *args, **kwargs)

            with (
                _track_leased_connections(connection_exits),
                patch.object(
                    projection,
                    "_validate_candidate_proof_index_core",
                    side_effect=observe,
                ) as delegated,
                patch.object(
                    tm_sqlite_store,
                    "validate_candidate_proof_index",
                    wraps=original_seam,
                ) as late_bound_seam,
            ):
                health = store.health()
            delegated.assert_called_once()
            late_bound_seam.assert_called_once()
            self.assertEqual(observed_transactions, [True])
            self.assertEqual(connection_exits, [False])
            self.assertEqual(health.record_count, 1)
            self.assertEqual(store.canonical_revision().head_revision, 1)

            connection_exits.clear()
            with (
                _track_leased_connections(connection_exits),
                patch.object(
                    projection,
                    "_validate_candidate_proof_index_core",
                    side_effect=sqlite3.OperationalError(
                        "secret candidate validation body"
                    ),
                ),
                self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "^STORE.CANDIDATE_INDEX_INVALID$",
                ) as raised,
            ):
                store.health()
            self.assertNotIn("secret candidate validation body", str(raised.exception))
            self.assertEqual(connection_exits, [False])

            sentinel = RuntimeError("programmer validation fault")
            with patch.object(
                projection,
                "_validate_candidate_proof_index_core",
                side_effect=sentinel,
            ):
                with self.assertRaises(RuntimeError) as programmer_fault:
                    store.health()
            self.assertIs(programmer_fault.exception, sentinel)

    def test_append_extension_plan_sql_summary_and_commit_faults_restore_old_bytes(
        self,
    ) -> None:
        cases = ("extension", "plan", "sql", "summary", "commit")
        for label in cases:
            with self.subTest(fault=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store, _fts5_available = _store(root)
                if label == "summary":
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(
                            "CREATE TRIGGER fail_candidate_summary "
                            "BEFORE INSERT ON tm_candidate_block BEGIN "
                            "SELECT RAISE(ABORT, 'secret summary body'); END"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                before = _disk_state(stage)
                sentinel = RuntimeError(f"programmer {label} fault")

                def extension(
                    _records: object,
                ) -> SQLiteCandidateWritePlan:
                    if label == "extension":
                        raise sentinel
                    if label == "plan":
                        return cast(SQLiteCandidateWritePlan, object())
                    raise AssertionError("extension is unexpected")

                original_summary = projection.maintain_candidate_proof_summaries

                def summary_fault(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    if label == "sql":
                        original_summary(connection, *args, **kwargs)
                        raise sqlite3.IntegrityError(
                            "secret candidate SQL body"
                        )
                    result = original_summary(connection, *args, **kwargs)
                    if label == "commit":
                        denied = False

                        def deny_first_commit(
                            action: int,
                            argument1: str | None,
                            _argument2: str | None,
                            _database: str | None,
                            _trigger: str | None,
                        ) -> int:
                            nonlocal denied
                            if (
                                action == sqlite3.SQLITE_TRANSACTION
                                and argument1 == "COMMIT"
                                and not denied
                            ):
                                denied = True
                                return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK

                        connection.set_authorizer(deny_first_commit)
                    return result

                extension_argument = (
                    extension if label in {"extension", "plan"} else None
                )
                summary_patch = (
                    patch.object(
                        projection,
                        "maintain_candidate_proof_summaries",
                        side_effect=summary_fault,
                    )
                    if label in {"sql", "commit"}
                    else patch.object(
                        projection,
                        "maintain_candidate_proof_summaries",
                        wraps=original_summary,
                    )
                )
                with summary_patch:
                    if label == "extension":
                        with self.assertRaises(RuntimeError) as raised:
                            store.append_batch(
                                batch_id=f"import.{label}",
                                kind="import",
                                drafts=(_draft("alpha", "target"),),
                                source_digest="d" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                                extension=extension_argument,
                            )
                        self.assertIs(raised.exception, sentinel)
                    elif label == "plan":
                        with self.assertRaises(TypeError):
                            store.append_batch(
                                batch_id=f"import.{label}",
                                kind="import",
                                drafts=(_draft("alpha", "target"),),
                                source_digest="d" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                                extension=extension_argument,
                            )
                    elif label in {"sql", "summary"}:
                        with self.assertRaises(sqlite3.IntegrityError) as raised:
                            store.append_batch(
                                batch_id=f"import.{label}",
                                kind="import",
                                drafts=(_draft("alpha", "target"),),
                                source_digest="d" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                            )
                        self.assertIn(
                            "secret candidate SQL body"
                            if label == "sql"
                            else "secret summary body",
                            str(raised.exception),
                        )
                    else:
                        with self.assertRaises(sqlite3.DatabaseError):
                            store.append_batch(
                                batch_id=f"import.{label}",
                                kind="import",
                                drafts=(_draft("alpha", "target"),),
                                source_digest="d" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                            )

                self.assertEqual(_disk_state(stage), before)

    def test_second_streamed_chunk_fault_keeps_only_committed_checkpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, _fts5_available = _store(root)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "CREATE TRIGGER fail_second_chunk_summary "
                    "BEFORE INSERT ON tm_gram_block_max "
                    "WHEN NEW.gram = 'z' BEGIN "
                    "SELECT RAISE(ABORT, 'secret second chunk body'); END"
                )
                connection.commit()
            finally:
                connection.close()
            checkpoint: list[tuple[bytes, tuple[object, ...]]] = []

            def stream() -> Iterator[tuple[TMRecordDraft, int]]:
                yield _draft("aa", "first"), 1
                yield _draft("bb", "second"), 2
                checkpoint.append(_disk_state(stage))
                yield _draft("zz", "rolled-back"), 3

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "secret second chunk body",
            ):
                store.append_streamed_batch(
                    batch_id="migration.second-chunk-fault",
                    kind="migration",
                    drafts=stream(),
                    source_digest="9" * 64,
                    source_path=(root / "second-chunk.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=2,
                )

            self.assertEqual(len(checkpoint), 1)
            self.assertEqual(_disk_state(stage), checkpoint[0])
            facts = checkpoint[0][1]
            self.assertEqual(facts[0], ("0",))
            self.assertEqual(facts[1], (2,))
            self.assertEqual(
                facts[-1],
                [("migration.second-chunk-fault", "staged", 0, None)],
            )

    def test_streamed_projection_and_commit_faults_restore_first_transaction_bytes(
        self,
    ) -> None:
        for label in ("projection", "commit"):
            with self.subTest(fault=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store, _fts5_available = _store(root)
                before = _disk_state(stage)
                sentinel = RuntimeError("programmer streamed projection fault")
                original = projection.insert_streamed_candidate_proof_rows

                def fail_chunk(
                    connection: sqlite3.Connection,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    if label == "projection":
                        raise sentinel
                    result = original(connection, *args, **kwargs)
                    denied = False

                    def deny_first_commit(
                        action: int,
                        argument1: str | None,
                        _argument2: str | None,
                        _database: str | None,
                        _trigger: str | None,
                    ) -> int:
                        nonlocal denied
                        if (
                            action == sqlite3.SQLITE_TRANSACTION
                            and argument1 == "COMMIT"
                            and not denied
                        ):
                            denied = True
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    connection.set_authorizer(deny_first_commit)
                    return result

                with patch.object(
                    projection,
                    "insert_streamed_candidate_proof_rows",
                    side_effect=fail_chunk,
                ):
                    if label == "projection":
                        with self.assertRaises(RuntimeError) as raised:
                            store.append_streamed_batch(
                                batch_id=f"migration.{label}",
                                kind="migration",
                                drafts=iter(((_draft("alpha", "target"), 1),)),
                                source_digest="e" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                                invalid_count=0,
                                duplicate_source_count=0,
                                chunk_size=2,
                            )
                        self.assertIs(raised.exception, sentinel)
                    else:
                        with self.assertRaises(sqlite3.DatabaseError):
                            store.append_streamed_batch(
                                batch_id=f"migration.{label}",
                                kind="migration",
                                drafts=iter(((_draft("alpha", "target"), 1),)),
                                source_digest="e" * 64,
                                source_path=(root / f"{label}.jsonl").resolve(),
                                invalid_count=0,
                                duplicate_source_count=0,
                                chunk_size=2,
                            )

                self.assertEqual(_disk_state(stage), before)

    def test_fts_operational_fault_keeps_body_safe_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, fts5_available = _store(root)
            if not fts5_available:
                self.skipTest("runtime does not provide FTS5 trigram")
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts")
                connection.commit()
            finally:
                connection.close()
            before = _disk_state(stage)

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE.FTS5_UNAVAILABLE$",
            ) as raised:
                store.append_batch(
                    batch_id="import.fts-fault",
                    kind="import",
                    drafts=(_draft("alpha", "target"),),
                    source_digest="f" * 64,
                    source_path=(root / "fts-fault.jsonl").resolve(),
                )

            self.assertNotIn("no such table", str(raised.exception).lower())
            self.assertEqual(_disk_state(stage), before)

    def test_non_fts_projection_sqlite_fault_is_not_widened_to_fts_code(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, _fts5_available = _store(root)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_gram")
                connection.commit()
            finally:
                connection.close()
            before = _disk_state(stage)

            with self.assertRaises(sqlite3.OperationalError) as raised:
                store.append_batch(
                    batch_id="import.gram-fault",
                    kind="import",
                    drafts=(_draft("alpha", "target"),),
                    source_digest="0" * 64,
                    source_path=(root / "gram-fault.jsonl").resolve(),
                )

            self.assertIn("tm_gram", str(raised.exception))
            self.assertNotIsInstance(raised.exception, SQLiteStoreSchemaError)
            self.assertEqual(_disk_state(stage), before)

    def test_store_digest_chunk_patch_reaches_late_bound_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store, fts5_available = _store(root)
            store.append_batch(
                batch_id="import.digest-chunk",
                kind="import",
                drafts=(_draft("alpha", "target"),),
                source_digest="1" * 64,
                source_path=(root / "digest-chunk.jsonl").resolve(),
            )
            original = projection.candidate_proof_projection_digest
            observed: list[tuple[bool, int | None]] = []

            def observe(
                connection: sqlite3.Connection,
                *args: object,
                **kwargs: object,
            ) -> object:
                observed.append(
                    (connection.in_transaction, kwargs.get("gram_chunk_rows"))
                )
                return original(connection, *args, **kwargs)

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("BEGIN")
                with (
                    patch.object(
                        tm_sqlite_store,
                        "_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS",
                        1,
                    ),
                    patch.object(
                        projection,
                        "candidate_proof_projection_digest",
                        side_effect=observe,
                    ) as delegated,
                ):
                    digest = tm_sqlite_store._candidate_proof_projection_digest(
                        connection,
                        fts5_available=fts5_available,
                    )
                connection.commit()
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

            delegated.assert_called_once()
            self.assertEqual(observed, [(True, 1)])
            self.assertEqual(len(digest), 64)

    def test_streamed_index_build_rejects_caller_supplied_sql(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage, _store_ref, _fts5_available = _store(Path(temporary))
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("BEGIN")
                hostile_statements = (
                    "COMMIT",
                    "ROLLBACK",
                    "DROP TABLE tm_record",
                )
                with self.assertRaises(TypeError):
                    projection.suspend_streamed_stage_secondary_indexes(
                        connection,
                        index_statements=hostile_statements,
                    )
                with self.assertRaises(TypeError):
                    projection.restore_streamed_stage_secondary_indexes(
                        connection,
                        index_statements=hostile_statements,
                    )
                self.assertTrue(connection.in_transaction)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'tm_record'"
                    ).fetchone(),
                    (1,),
                )
                connection.rollback()
                self.assertFalse(connection.in_transaction)
            finally:
                connection.close()

    def test_write_projection_signatures_exclude_authority_inputs(self) -> None:
        forbidden = {
            "generation",
            "head_revision",
            "receipt",
            "binding",
            "capability",
            "canonical_store_id",
            "resource_id",
            "batch_id",
            "completed_revision",
            "statement",
            "statements",
            "index_statements",
        }
        functions = (
            projection.project_candidate_write_plan,
            projection.insert_candidate_gram_rows,
            projection.insert_candidate_fts_rows,
            projection.maintain_candidate_proof_summaries,
            projection.insert_streamed_candidate_gram_rows,
            projection.insert_streamed_candidate_proof_rows,
            projection.candidate_proof_projection_digest,
            projection._validate_candidate_proof_index_core,
            projection.validate_candidate_proof_index_with_digest,
            projection.validate_candidate_proof_index,
            projection.suspend_streamed_stage_secondary_indexes,
            projection.restore_streamed_stage_secondary_indexes,
        )
        for function in functions:
            with self.subTest(function=function.__name__):
                parameters = set(inspect.signature(function).parameters)
                self.assertTrue(parameters.isdisjoint(forbidden))

        source = Path(projection.__file__).read_text(encoding="utf-8")
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn(".commit(", source)
        self.assertNotIn(".rollback(", source)


if __name__ == "__main__":
    unittest.main()
