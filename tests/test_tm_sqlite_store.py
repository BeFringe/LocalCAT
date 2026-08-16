from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
import copy
from dataclasses import replace
import hashlib
import inspect
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_sqlite_store
from tm_contracts import (
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    CanonicalResourceIdentity,
    MutableStageRef,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    TMRecord,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_sqlite_store import (
    CANDIDATE_PROOF_BLOCK_SIZE,
    CANDIDATE_INDEX_VERSION,
    build_candidate_write_plan,
    character_ngram_frequencies,
    unique_character_ngrams,
    FOLD_VERSION_V1,
    TM_SCHEMA_VERSION,
    SQLiteCandidateRecord,
    SQLiteCandidateWritePlan,
    SQLiteGramRow,
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    _SQLiteGenerationView,
    _schema_digest,
    _open_configured_connection,
    initialize_stage_schema,
    inspect_stage_schema,
    validate_candidate_proof_index,
)


def _stage(root: Path, resource_id: str = "tm.primary") -> MutableStageRef:
    configured = (root / f"{resource_id}.jsonl").resolve()
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        configured,
    )
    return MutableStageRef(
        stage_id=f"stage.{resource_id}",
        resource_identity=identity,
        staged_db_path=(root / f".{resource_id}.stage.sqlite3").resolve(),
        manifest_temp_path=(
            root / f".{resource_id}.snapshot.tmp"
        ).resolve(),
    )


def _draft(
    source: str,
    target: str,
    *,
    speaker: str | None = None,
    previous: str | None = None,
    following: str | None = None,
    file_source: str | None = None,
    provenance: tuple[tuple[str, str], ...] = (("source", "test"),),
) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=speaker,
        context_prev_raw=previous,
        context_next_raw=following,
        file_source=file_source,
        provenance=provenance,
    )


class SQLiteTMStoreTests(unittest.TestCase):
    def test_candidate_proof_index_stores_exact_lengths_tf_and_block_maxima(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage, canonical_store_id="store.primary"
                        )
                        store = SQLiteTMStore(
                            stage, canonical_store_id="store.primary"
                        )
                    _ = store.append_batch(
                        batch_id="import.proof-index",
                        kind="import",
                        drafts=(
                            _draft("aaba", "first"),
                            _draft("aaaa", "second"),
                        ),
                        source_digest="8" * 64,
                        source_path=(Path(temporary) / "proof.jsonl").resolve(),
                    )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT source_fold_length FROM tm_record "
                                "ORDER BY record_id"
                            ).fetchall(),
                            [(4,), (4,)],
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT term_frequency FROM tm_gram "
                                "WHERE record_id = 1 AND gram_size = 1 "
                                "AND gram = 'a'"
                            ).fetchone(),
                            (3,),
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT * FROM tm_candidate_block"
                            ).fetchall(),
                            [(0, 1, CANDIDATE_PROOF_BLOCK_SIZE, 2, 4, 4)],
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT max_term_frequency "
                                "FROM tm_gram_block_max WHERE block_id = 0 "
                                "AND gram_size = 1 AND gram = 'a'"
                            ).fetchone(),
                            (4,),
                        )
                        counts, fts_count = validate_candidate_proof_index(
                            connection,
                            required_sizes=(
                                (1, 2) if fts5_available else (1, 2, 3)
                            ),
                            fts5_available=fts5_available,
                        )
                        self.assertTrue(counts)
                        self.assertEqual(fts_count, 2 if fts5_available else 0)
                        connection.execute(
                            "UPDATE tm_gram SET term_frequency = 1 "
                            "WHERE record_id = 1 AND gram_size = 1 AND gram = 'a'"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_INDEX_INVALID$",
                    ):
                        _ = store.health()

    def test_proof_summary_sql_failure_rolls_back_every_candidate_fact(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage, canonical_store_id="store.primary"
                        )
                        store = SQLiteTMStore(
                            stage, canonical_store_id="store.primary"
                        )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(
                            "CREATE TRIGGER inject_proof_failure "
                            "BEFORE INSERT ON tm_candidate_block BEGIN "
                            "SELECT RAISE(ABORT, 'injected proof failure'); END"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "injected proof failure"
                    ):
                        _ = store.append_batch(
                            batch_id="import.proof-rollback",
                            kind="import",
                            drafts=(_draft("aaaa", "target"),),
                            source_digest="7" * 64,
                            source_path=(
                                Path(temporary) / "proof-rollback.jsonl"
                            ).resolve(),
                        )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        counts = tuple(
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()
                            for table in (
                                "tm_origin_batch",
                                "tm_record",
                                "tm_gram",
                                "tm_candidate_block",
                                "tm_gram_block_max",
                            )
                        )
                        fts_count = (
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_fts"
                            ).fetchone()
                            if fts5_available
                            else (0,)
                        )
                        revision = connection.execute(
                            "SELECT value FROM tm_meta "
                            "WHERE key = 'head_revision'"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(counts, ((0,),) * 5)
                    self.assertEqual(fts_count, (0,))
                    self.assertEqual(revision, ("0",))

    def test_character_ngram_frequencies_preserve_exact_order_and_counts(
        self,
    ) -> None:
        self.assertEqual(
            character_ngram_frequencies("ababa", 2),
            (("ab", 2), ("ba", 2)),
        )
        self.assertEqual(character_ngram_frequencies("", 1), ())
        self.assertEqual(character_ngram_frequencies("a", 2), ())
        self.assertEqual(
            character_ngram_frequencies("😀a😀", 2),
            (("😀a", 1), ("a😀", 1)),
        )
        self.assertEqual(
            character_ngram_frequencies("a\u0301a\u0301", 2),
            (("a\u0301", 2), ("\u0301a", 1)),
        )

    def test_character_ngram_frequencies_reject_non_exact_inputs(
        self,
    ) -> None:
        class StrSubclass(str):
            pass

        class IntSubclass(int):
            pass

        for folded_text in (StrSubclass("ab"), None, 1):
            with self.subTest(folded_text=folded_text):
                with self.assertRaisesRegex(
                    TypeError,
                    "^folded_text must be a built-in string$",
                ):
                    _ = character_ngram_frequencies(
                        cast(Any, folded_text),
                        2,
                    )
        for gram_size in (IntSubclass(2), True, 2.0, "2"):
            with self.subTest(gram_size=gram_size):
                with self.assertRaisesRegex(
                    TypeError,
                    "^gram_size must be a built-in integer$",
                ):
                    _ = character_ngram_frequencies(
                        "ab",
                        cast(Any, gram_size),
                    )
        for gram_size in (0, 4):
            with self.subTest(gram_size=gram_size):
                with self.assertRaisesRegex(
                    ValueError,
                    "^gram_size must be 1, 2, or 3$",
                ):
                    _ = character_ngram_frequencies("ab", gram_size)

    def test_chunked_candidate_helpers_hold_one_read_snapshot(self) -> None:
        query = "".join(chr(0x1000 + offset) for offset in range(300))
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage,
                            canonical_store_id="store.primary",
                        )
                        store = SQLiteTMStore(
                            stage,
                            canonical_store_id="store.primary",
                        )
                    _ = store.append(_draft(query, "target"))

                    original_read_meta = tm_sqlite_store._read_meta
                    transaction_states: list[bool] = []

                    def record_transaction_state(
                        connection: sqlite3.Connection,
                    ) -> dict[str, str]:
                        transaction_states.append(connection.in_transaction)
                        return original_read_meta(connection)

                    with patch(
                        "tm_sqlite_store._read_meta",
                        side_effect=record_transaction_state,
                    ):
                        if fts5_available:
                            _ = store.fts5_candidate_ids_for_trigrams(
                                tm_sqlite_store.unique_character_ngrams(query, 3)
                            )
                        else:
                            _ = store.gram_candidate_overlaps(
                                tuple((1, character) for character in query),
                                candidate_cap=10,
                            )

                    self.assertTrue(transaction_states)
                    self.assertTrue(all(transaction_states))

    def test_append_without_extension_builds_required_candidate_indexes(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage,
                            canonical_store_id="store.primary",
                        )
                        store = SQLiteTMStore(
                            stage,
                            canonical_store_id="store.primary",
                        )
                    local = store.append(_draft("abcdef", "local"))
                    imported = store.append_batch(
                        batch_id="import.default-index",
                        kind="import",
                        drafts=(_draft("uvwxyz", "imported"),),
                        source_digest="9" * 64,
                        source_path=(Path(temporary) / "source.jsonl").resolve(),
                    )[0]

                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        gram_sizes = {
                            row[0]
                            for row in connection.execute(
                                "SELECT DISTINCT gram_size FROM tm_gram"
                            ).fetchall()
                        }
                        proof_blocks = connection.execute(
                            "SELECT block_id, first_record_id, last_record_id, "
                            "record_count FROM tm_candidate_block "
                            "ORDER BY block_id"
                        ).fetchall()
                        fts_count = (
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_fts"
                            ).fetchone()[0]
                            if fts5_available
                            else 0
                        )
                    finally:
                        connection.close()
                    self.assertEqual(
                        gram_sizes,
                        {1, 2} if fts5_available else {1, 2, 3},
                    )
                    self.assertEqual(
                        proof_blocks,
                        [(0, 1, CANDIDATE_PROOF_BLOCK_SIZE, 2)],
                    )
                    self.assertEqual(fts_count, 2 if fts5_available else 0)
                    for source, record_id in (
                        ("abcdef", local.record_id),
                        ("uvwxyz", imported.record_id),
                    ):
                        from tm_candidate_index import CandidateRetriever
                        recalled = CandidateRetriever().candidates(
                            "tm.primary", store, source, result_limit=10
                        )
                        self.assertIn(
                            record_id,
                            tuple(item.record_id for item in recalled.candidates),
                        )

    def test_default_and_additional_plan_failures_leave_no_write_state(self) -> None:
        for failure_kind in ("default", "additional"):
            with self.subTest(failure_kind=failure_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    initialize_stage_schema(
                        stage,
                        canonical_store_id="store.primary",
                    )
                    store = SQLiteTMStore(
                        stage,
                        canonical_store_id="store.primary",
                    )

                    def fail_additional(
                        _records: tuple[SQLiteCandidateRecord, ...],
                    ) -> SQLiteCandidateWritePlan:
                        raise RuntimeError("injected additional plan failure")

                    context = (
                        patch(
                            "tm_sqlite_store.build_candidate_write_plan",
                            side_effect=RuntimeError("injected default plan failure"),
                        )
                        if failure_kind == "default"
                        else nullcontext()
                    )
                    with context:
                        with self.assertRaisesRegex(RuntimeError, "plan failure"):
                            _ = store.append_batch(
                                batch_id=f"import.{failure_kind}-plan-failure",
                                kind="import",
                                drafts=(_draft("abcdef", "target"),),
                                source_digest=(
                                    "a" if failure_kind == "default" else "b"
                                )
                                * 64,
                                source_path=(Path(temporary) / "source.jsonl").resolve(),
                                extension=(
                                    fail_additional
                                    if failure_kind == "additional"
                                    else None
                                ),
                            )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        state = (
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_origin_batch"
                            ).fetchone(),
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_origin_batch "
                                "WHERE completed_revision IS NOT NULL"
                            ).fetchone(),
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_record"
                            ).fetchone(),
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_gram"
                            ).fetchone(),
                            connection.execute(
                                "SELECT value FROM tm_meta "
                                "WHERE key = 'head_revision'"
                            ).fetchone(),
                        )
                    finally:
                        connection.close()
                    self.assertEqual(state, ((0,), (0,), (0,), (0,), ("0",)))

    def test_canonical_revision_concurrent_append_is_one_complete_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            _ = store.append(_draft("seed", "seed target"))
            original_read_meta = tm_sqlite_store._read_meta
            original_revision = (
                tm_sqlite_store._canonical_revision_from_connection
            )
            revision_meta_reads = 0
            append_interleaved = False

            def interleave_after_meta(
                connection: sqlite3.Connection,
            ) -> dict[str, str]:
                nonlocal append_interleaved, revision_meta_reads
                meta = original_read_meta(connection)
                revision_meta_reads += 1
                if (
                    not append_interleaved
                    and revision_meta_reads == 2
                    and not connection.in_transaction
                ):
                    append_interleaved = True
                    _ = store.append(_draft("concurrent", "new target"))
                return meta

            def interleave_after_snapshot(
                connection: sqlite3.Connection,
                lease: _SQLiteGenerationView,
            ) -> tm_sqlite_store.CanonicalRevisionSnapshot:
                nonlocal append_interleaved
                revision = original_revision(connection, lease)
                if not append_interleaved:
                    append_interleaved = True
                    _ = store.append(_draft("concurrent", "new target"))
                return revision

            with (
                patch(
                    "tm_sqlite_store._read_meta",
                    side_effect=interleave_after_meta,
                ),
                patch(
                    "tm_sqlite_store._canonical_revision_from_connection",
                    side_effect=interleave_after_snapshot,
                ),
            ):
                revision = store.canonical_revision()

            self.assertIn(
                (revision.head_revision, revision.record_count),
                {(1, 1), (2, 2)},
            )
            self.assertEqual(
                store.exact_records("concurrent")[0].target_raw,
                "new target",
            )
            self.assertEqual(store.canonical_revision().head_revision, 2)

    def test_local_append_preserves_variants_and_raw_exact_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            first = store.append(
                _draft(
                    "Open Door",
                    "开门",
                    speaker="alice",
                    previous="Before",
                    following="After",
                    file_source="chapter-01.json",
                    provenance=(("writer", "local"),),
                )
            )
            second = store.append(
                _draft(
                    "Open Door",
                    "打开门",
                    provenance=(("writer", "revision"),),
                )
            )

            exact = store.exact_records("Open Door")
            self.assertEqual(exact, (second, first))
            self.assertEqual(exact[0].target_raw, "打开门")
            self.assertEqual(exact[1].speaker_raw, "alice")
            self.assertEqual(exact[1].context_prev_raw, "Before")
            self.assertEqual(exact[1].context_next_raw, "After")
            self.assertEqual(exact[1].file_source, "chapter-01.json")
            self.assertEqual(exact[1].provenance, (("writer", "local"),))
            self.assertEqual(store.exact_records("open door"), ())
            self.assertEqual(store.exact_records("Open Door "), ())

    def test_store_constructor_rejects_caller_subtypes_before_connection(
        self,
    ) -> None:
        dispatches: list[str] = []

        class ForgedIdentity(str):
            def strip(self, chars: str | None = None, /) -> str:
                dispatches.append("strip")
                return "store.primary"

            def __ne__(self, value: object, /) -> bool:
                dispatches.append("ne")
                return False

        class StageSubclass(MutableStageRef):
            pass

        class IdentitySubclass(CanonicalResourceIdentity):
            pass

        def path_is_symlink(_value: object) -> bool:
            dispatches.append("path")
            return False

        path_subclass = type(
            "DispatchingPath",
            (type(Path()),),
            {"is_symlink": path_is_symlink},
        )

        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            identity = stage.resource_identity
            identity_subclass = IdentitySubclass(
                resource_id=identity.resource_id,
                configured_jsonl_path=identity.configured_jsonl_path,
                canonical_sidecar_path=identity.canonical_sidecar_path,
                snapshot_manifest_path=identity.snapshot_manifest_path,
                target_identity=identity.target_identity,
                identity_version=identity.identity_version,
            )

            def forged_stage(**overrides: object) -> MutableStageRef:
                values: dict[str, object] = {
                    "stage_id": stage.stage_id,
                    "resource_identity": identity,
                    "staged_db_path": stage.staged_db_path,
                    "manifest_temp_path": stage.manifest_temp_path,
                }
                values.update(overrides)
                forged = object.__new__(MutableStageRef)
                for field_name, value in values.items():
                    object.__setattr__(forged, field_name, value)
                return forged

            invalid_stages = (
                StageSubclass(
                    stage_id=stage.stage_id,
                    resource_identity=identity,
                    staged_db_path=stage.staged_db_path,
                    manifest_temp_path=stage.manifest_temp_path,
                ),
                forged_stage(resource_identity=identity_subclass),
                forged_stage(
                    staged_db_path=path_subclass(stage.staged_db_path)
                ),
            )
            calls = (
                lambda: SQLiteTMStore(
                    stage,
                    canonical_store_id=ForgedIdentity("store.wrong"),
                ),
                *(
                    lambda invalid_stage=invalid_stage: SQLiteTMStore(
                        invalid_stage,
                        canonical_store_id="store.primary",
                    )
                    for invalid_stage in invalid_stages
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

    def test_store_uses_private_stage_snapshot_after_caller_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = _stage(root, "tm.primary")
            secondary = _stage(root, "tm.secondary")
            for stage in (primary, secondary):
                initialize_stage_schema(
                    stage,
                    canonical_store_id="store.shared",
                )
            primary_path = primary.staged_db_path
            secondary_path = secondary.staged_db_path
            store = SQLiteTMStore(
                primary,
                canonical_store_id="store.shared",
            )

            object.__setattr__(
                primary,
                "resource_identity",
                secondary.resource_identity,
            )
            object.__setattr__(
                primary,
                "staged_db_path",
                secondary.staged_db_path,
            )
            object.__setattr__(
                primary,
                "manifest_temp_path",
                secondary.manifest_temp_path,
            )

            record = store.append(_draft("private stage", "primary only"))
            self.assertEqual(store.exact_records("private stage"), (record,))
            counts: list[int] = []
            for path in (primary_path, secondary_path):
                connection = sqlite3.connect(path)
                try:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM tm_record"
                    ).fetchone()
                    self.assertIsNotNone(row)
                    counts.append(cast(int, row[0]))
                finally:
                    connection.close()
            self.assertEqual(counts, [1, 0])

    def test_migration_batch_preserves_order_origin_and_fold_on_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            source_path = (root / "legacy.jsonl").resolve()

            records = store.append_batch(
                batch_id="migration.batch-001",
                kind="migration",
                drafts=(
                    _draft("Straße", "first", speaker="alice"),
                    _draft("Straße", "second", previous="before"),
                    _draft("STRASSE", "case variant"),
                ),
                source_digest="a" * 64,
                source_path=source_path,
                legacy_line_nos=(3, 5, 8),
                invalid_count=2,
                duplicate_source_count=1,
                created_at="2026-08-01T00:00:00+00:00",
            )

            self.assertEqual(
                tuple(record.origin_ordinal for record in records),
                (0, 1, 2),
            )
            self.assertEqual(
                tuple(record.legacy_line_no for record in records),
                (3, 5, 8),
            )
            self.assertEqual(
                tuple(record.target_raw for record in store.exact_records("Straße")),
                ("second", "first"),
            )
            self.assertEqual(store.exact_records("STRASSE"), (records[2],))

            reopened = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            self.assertEqual(reopened.exact_records("Straße")[0], records[1])
            self.assertEqual(
                reopened.records_by_id((records[2].record_id, records[0].record_id)),
                (records[2], records[0]),
            )
            self.assertEqual(tuple(reopened.export_records()), records)

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                batch = connection.execute(
                    "SELECT kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "completed_revision "
                    "FROM tm_origin_batch WHERE batch_id = ?",
                    ("migration.batch-001",),
                ).fetchone()
                folds = connection.execute(
                    "SELECT source_fold_v1 FROM tm_record ORDER BY record_id"
                ).fetchall()
                revision = connection.execute(
                    "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                batch,
                (
                    "migration",
                    "a" * 64,
                    str(source_path),
                    "completed",
                    3,
                    2,
                    1,
                    1,
                ),
            )
            self.assertEqual(folds, [("strasse",), ("strasse",), ("strasse",)])
            self.assertEqual(revision, ("1",))

    def test_batch_extension_failure_rolls_back_origin_records_and_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def fail_extension(
                _records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                raise RuntimeError("injected candidate extension failure")

            with self.assertRaisesRegex(RuntimeError, "injected candidate"):
                _ = store.append_batch(
                    batch_id="import.batch-fail",
                    kind="import",
                    drafts=(_draft("same", "one"), _draft("same", "two")),
                    source_digest="b" * 64,
                    source_path=(Path(temporary) / "import.jsonl").resolve(),
                    extension=fail_extension,
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), ("0",)))

    def test_extension_cannot_commit_partial_batch_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def commit_then_fail(
                records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                exposed = getattr(
                    records,
                    "_SQLiteWriteTransaction__connection",
                )
                exposed.commit()
                return SQLiteCandidateWritePlan()

            with self.assertRaises(AttributeError):
                _ = store.append_batch(
                    batch_id="import.commit-exploit",
                    kind="import",
                    drafts=(_draft("abcd", "target"),),
                    source_digest="2" * 64,
                    source_path=(root / "exploit.jsonl").resolve(),
                    extension=commit_then_fail,
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

    def test_extension_cannot_reach_transaction_through_caller_frame(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def introspect_commit_then_fail(
                records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                frame = inspect.currentframe()
                if frame is None:
                    self.fail("current frame unavailable")
                caller = frame.f_back if frame is not None else None
                if caller is None:
                    self.fail("caller frame unavailable")
                connection = caller.f_locals["connection"]
                connection.execute(
                    "INSERT INTO tm_gram(gram_size, gram, record_id) "
                    "VALUES (2, 'ab', ?)",
                    (records[0].origin_ordinal,),
                )
                connection.commit()
                raise RuntimeError("frame introspection committed")

            with self.assertRaises(KeyError):
                _ = store.append_batch(
                    batch_id="import.frame-exploit",
                    kind="import",
                    drafts=(_draft("abcd", "target"),),
                    source_digest="3" * 64,
                    source_path=(root / "frame-exploit.jsonl").resolve(),
                    extension=introspect_commit_then_fail,
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

    def test_origin_and_record_stage_failures_leave_no_partial_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            source_path = (root / "import.jsonl").resolve()
            first = store.append_batch(
                batch_id="import.unique",
                kind="import",
                drafts=(_draft("kept", "target"),),
                source_digest="d" * 64,
                source_path=source_path,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                _ = store.append_batch(
                    batch_id="import.unique",
                    kind="import",
                    drafts=(_draft("not inserted", "target"),),
                    source_digest="e" * 64,
                    source_path=source_path,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "CREATE TRIGGER inject_record_failure "
                    "BEFORE INSERT ON tm_record BEGIN "
                    "SELECT RAISE(ABORT, 'injected record failure'); END"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(sqlite3.IntegrityError):
                _ = store.append_batch(
                    batch_id="import.record-failure",
                    kind="import",
                    drafts=(_draft("record fails", "target"),),
                    source_digest="f" * 64,
                    source_path=source_path,
                )

            self.assertEqual(store.exact_records("kept"), first)
            self.assertEqual(store.exact_records("not inserted"), ())
            self.assertEqual(store.exact_records("record fails"), ())
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((1,), (1,), ("1",)))

    def test_controlled_extension_writes_candidate_rows_in_same_transaction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def write_candidate_rows(
                records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                return SQLiteCandidateWritePlan(
                    gram_rows=(
                        SQLiteGramRow(
                            origin_ordinal=records[0].origin_ordinal,
                            gram_size=2,
                            gram="ab",
                        ),
                    ),
                )

            records = store.append_batch(
                batch_id="import.indexed",
                kind="import",
                drafts=(_draft("abcd", "target"),),
                source_digest="1" * 64,
                source_path=(root / "indexed.jsonl").resolve(),
                extension=write_candidate_rows,
            )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                gram_rows = connection.execute(
                    "SELECT gram_size, gram, record_id FROM tm_gram"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                gram_rows,
                [
                    (1, "a", records[0].record_id),
                    (1, "b", records[0].record_id),
                    (1, "c", records[0].record_id),
                    (1, "d", records[0].record_id),
                    (2, "ab", records[0].record_id),
                    (2, "bc", records[0].record_id),
                    (2, "cd", records[0].record_id),
                ],
            )

    def test_batch_rejects_scalar_subclasses_before_extension_or_connection(
        self,
    ) -> None:
        conversions: list[str] = []
        extension_calls: list[str] = []

        class DeceptiveStr(str):
            def __str__(self) -> str:
                conversions.append("str")
                return "forged"

        class DeceptiveInt(int):
            def __int__(self) -> int:
                conversions.append("int")
                return 0

        class TupleSubclass(tuple[Any, ...]):
            pass

        def deceptive_path_str(_value: object) -> str:
            conversions.append("path")
            return "/forged.jsonl"

        deceptive_path_type = type(
            "DeceptivePath",
            (type(Path()),),
            {"__str__": deceptive_path_str},
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def extension(
                _records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                extension_calls.append("called")
                return SQLiteCandidateWritePlan()

            source_path = (root / "source.jsonl").resolve()
            cases: tuple[tuple[str, dict[str, Any]], ...] = (
                ("batch_id", {"batch_id": DeceptiveStr("import.batch")}),
                ("kind", {"kind": DeceptiveStr("import")}),
                (
                    "source_digest",
                    {"source_digest": DeceptiveStr("a" * 64)},
                ),
                (
                    "source_path",
                    {"source_path": deceptive_path_type(source_path)},
                ),
                (
                    "created_at",
                    {"created_at": DeceptiveStr("2026-08-01T00:00:00Z")},
                ),
                ("invalid_count", {"invalid_count": DeceptiveInt(1)}),
                (
                    "duplicate_source_count",
                    {"duplicate_source_count": DeceptiveInt(-1)},
                ),
                (
                    "drafts",
                    {"drafts": TupleSubclass((_draft("source", "target"),))},
                ),
                (
                    "legacy_line_nos",
                    {"legacy_line_nos": TupleSubclass((1,))},
                ),
                (
                    "legacy_line_no",
                    {"legacy_line_nos": (DeceptiveInt(7),)},
                ),
            )
            for name, overrides in cases:
                with self.subTest(field=name):
                    arguments: dict[str, Any] = {
                        "batch_id": "import.batch",
                        "kind": "import",
                        "drafts": (_draft("source", "target"),),
                        "source_digest": "a" * 64,
                        "source_path": source_path,
                        "legacy_line_nos": (1,),
                        "invalid_count": 0,
                        "duplicate_source_count": 0,
                        "created_at": "2026-08-01T00:00:00Z",
                        "extension": extension,
                    }
                    arguments.update(overrides)
                    with patch(
                        "tm_sqlite_store._open_configured_connection",
                        side_effect=AssertionError("connection opened"),
                    ) as open_connection:
                        with self.assertRaises(TypeError):
                            _ = store.append_batch(**arguments)
                        open_connection.assert_not_called()
                    self.assertEqual(extension_calls, [])
                    self.assertEqual(conversions, [])
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        state = (
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_origin_batch"
                            ).fetchone(),
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_record"
                            ).fetchone(),
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_gram"
                            ).fetchone(),
                            connection.execute(
                                "SELECT value FROM tm_meta "
                                "WHERE key = 'head_revision'"
                            ).fetchone(),
                        )
                    finally:
                        connection.close()
                    self.assertEqual(state, ((0,), (0,), (0,), ("0",)))

    def test_batch_rejects_draft_field_subclasses_before_fold_or_connection(
        self,
    ) -> None:
        conversions: list[str] = []
        extension_calls: list[str] = []

        class DeceptiveStr(str):
            def __str__(self) -> str:
                conversions.append("str")
                return "forged"

        class TupleSubclass(tuple[Any, ...]):
            pass

        class DraftSubclass(TMRecordDraft):
            pass

        def forged_draft(**overrides: object) -> TMRecordDraft:
            values: dict[str, object] = {
                "source_raw": "source",
                "target_raw": "target",
                "speaker_raw": None,
                "context_prev_raw": None,
                "context_next_raw": None,
                "file_source": None,
                "provenance": (("source", "test"),),
            }
            values.update(overrides)
            draft = object.__new__(TMRecordDraft)
            for field_name, value in values.items():
                object.__setattr__(draft, field_name, value)
            return draft

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def extension(
                _records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                extension_calls.append("called")
                return SQLiteCandidateWritePlan()

            cases = (
                DraftSubclass(
                    source_raw="source",
                    target_raw="target",
                    speaker_raw=None,
                    context_prev_raw=None,
                    context_next_raw=None,
                    file_source=None,
                    provenance=(("source", "test"),),
                ),
                forged_draft(source_raw=DeceptiveStr("")),
                forged_draft(target_raw=DeceptiveStr("target")),
                forged_draft(speaker_raw=DeceptiveStr("speaker")),
                forged_draft(context_prev_raw=DeceptiveStr("previous")),
                forged_draft(context_next_raw=DeceptiveStr("following")),
                forged_draft(file_source=DeceptiveStr("chapter.json")),
                forged_draft(
                    provenance=TupleSubclass((("source", "test"),))
                ),
                forged_draft(
                    provenance=(TupleSubclass(("source", "test")),)
                ),
                forged_draft(
                    provenance=((DeceptiveStr(""), "test"),)
                ),
                forged_draft(
                    provenance=(("source", DeceptiveStr("test")),)
                ),
            )
            for draft in cases:
                with self.subTest(draft=draft):
                    with (
                        patch(
                            "tm_sqlite_store.fold_text_value_v1",
                            side_effect=AssertionError("source folded"),
                        ) as fold,
                        patch(
                            "tm_sqlite_store._open_configured_connection",
                            side_effect=AssertionError("connection opened"),
                        ) as open_connection,
                    ):
                        with self.assertRaises(TypeError):
                            _ = store.append_batch(
                                batch_id="import.draft-subclass",
                                kind="import",
                                drafts=(draft,),
                                source_digest="b" * 64,
                                source_path=(root / "source.jsonl").resolve(),
                                extension=extension,
                            )
                        fold.assert_not_called()
                        open_connection.assert_not_called()
                    self.assertEqual(extension_calls, [])
                    self.assertEqual(conversions, [])
            with patch(
                "tm_sqlite_store.fold_text_value_v1",
                side_effect=AssertionError("source folded"),
            ) as fold:
                with self.assertRaises(TypeError):
                    store.append_streamed_batch(
                        batch_id="migration.draft-subclass",
                        kind="migration",
                        drafts=iter(
                            ((forged_draft(source_raw=DeceptiveStr("")), 1),)
                        ),
                        source_digest="c" * 64,
                        source_path=(root / "streamed.jsonl").resolve(),
                        invalid_count=0,
                        duplicate_source_count=0,
                        chunk_size=500,
                    )
                fold.assert_not_called()
            self.assertEqual(conversions, [])
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                state = (
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_origin_batch"
                    ).fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(state, ((0,), (0,), (0,), ("0",)))

    def test_candidate_field_subclasses_are_rejected_before_connection(
        self,
    ) -> None:
        conversions: list[str] = []

        class DeceptiveStr(str):
            def __str__(self) -> str:
                conversions.append("str")
                return "ab"

        class DeceptiveInt(int):
            def __int__(self) -> int:
                conversions.append("int")
                return 0

        def candidate_plan(**overrides: object) -> SQLiteCandidateWritePlan:
            values: dict[str, object] = {
                "origin_ordinal": 0,
                "gram_size": 2,
                "gram": "ab",
            }
            values.update(overrides)
            row = object.__new__(SQLiteGramRow)
            for field_name, value in values.items():
                object.__setattr__(row, field_name, value)
            return SQLiteCandidateWritePlan(gram_rows=(row,))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            cases = (
                {"origin_ordinal": DeceptiveInt(-1)},
                {"gram_size": DeceptiveInt(0)},
                {"gram": DeceptiveStr("")},
            )
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    def extension(
                        _records: tuple[SQLiteCandidateRecord, ...],
                    ) -> SQLiteCandidateWritePlan:
                        return candidate_plan(**overrides)

                    with patch(
                        "tm_sqlite_store._open_configured_connection",
                        side_effect=AssertionError("connection opened"),
                    ) as open_connection:
                        with self.assertRaises(ValueError):
                            _ = store.append_batch(
                                batch_id="import.candidate-subclass",
                                kind="import",
                                drafts=(_draft("abcd", "target"),),
                                source_digest="c" * 64,
                                source_path=(root / "source.jsonl").resolve(),
                                extension=extension,
                            )
                        open_connection.assert_not_called()
                    self.assertEqual(conversions, [])
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                state = (
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_origin_batch"
                    ).fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(state, ((0,), (0,), (0,), ("0",)))

    def test_public_queries_reject_scalar_subclasses_before_connection(
        self,
    ) -> None:
        conversions: list[str] = []

        class DeceptiveStr(str):
            def __str__(self) -> str:
                conversions.append("str")
                return "source"

        class DeceptiveInt(int):
            def __int__(self) -> int:
                conversions.append("int")
                return 1

        class TupleSubclass(tuple[Any, ...]):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            calls = (
                lambda: store.exact_records(DeceptiveStr("source")),
                lambda: store.records_by_id((DeceptiveInt(1),)),
                lambda: store.records_by_id(TupleSubclass((1,))),
            )
            for call in calls:
                with patch(
                    "tm_sqlite_store._open_configured_connection",
                    side_effect=AssertionError("connection opened"),
                ) as open_connection:
                    with self.assertRaises(TypeError):
                        _ = call()
                    open_connection.assert_not_called()
                self.assertEqual(conversions, [])

    def test_candidate_plan_is_closed_strict_and_batch_local(self) -> None:
        invalid_rows: tuple[dict[str, Any], ...] = (
            {"origin_ordinal": True, "gram_size": 1, "gram": "a"},
            {"origin_ordinal": 0, "gram_size": True, "gram": "a"},
            {"origin_ordinal": 0, "gram_size": 2.0, "gram": "ab"},
            {"origin_ordinal": 0, "gram_size": 2, "gram": "a"},
            {"origin_ordinal": 0, "gram_size": 1, "gram": ""},
        )
        for values in invalid_rows:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    _ = SQLiteGramRow(**values)
        row = SQLiteGramRow(
            origin_ordinal=0,
            gram_size=2,
            gram="ab",
        )
        with self.assertRaises(ValueError):
            _ = SQLiteCandidateWritePlan(gram_rows=(row, row))
        with self.assertRaises(ValueError):
            _ = SQLiteCandidateWritePlan(fts_origin_ordinals=(0, 0))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def cross_batch_plan(
                _records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                return SQLiteCandidateWritePlan(
                    gram_rows=(
                        SQLiteGramRow(
                            origin_ordinal=1,
                            gram_size=2,
                            gram="ab",
                        ),
                    ),
                )

            with self.assertRaisesRegex(ValueError, "current batch ordinal"):
                _ = store.append_batch(
                    batch_id="import.cross-batch-plan",
                    kind="import",
                    drafts=(_draft("abcd", "target"),),
                    source_digest="4" * 64,
                    source_path=(root / "cross-batch.jsonl").resolve(),
                    extension=cross_batch_plan,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

    def test_candidate_plan_validates_nested_fields_before_hashing(self) -> None:
        dispatches: list[str] = []

        class DispatchingStr(str):
            def __hash__(self) -> int:
                dispatches.append("hash")
                return 0

            def __eq__(self, other: object) -> bool:
                dispatches.append("eq")
                return str.__eq__(self, other)

        def forged_row(gram: str) -> SQLiteGramRow:
            row = object.__new__(SQLiteGramRow)
            object.__setattr__(row, "origin_ordinal", 0)
            object.__setattr__(row, "gram_size", 2)
            object.__setattr__(row, "gram", gram)
            return row

        with self.assertRaises(ValueError) as raised:
            _ = SQLiteCandidateWritePlan(
                gram_rows=(
                    forged_row(DispatchingStr("ab")),
                    forged_row(DispatchingStr("ab")),
                )
            )

        self.assertEqual(dispatches, [])
        self.assertEqual(
            str(raised.exception),
            "gram length must equal gram_size",
        )

    def test_candidate_sql_failure_rolls_back_entire_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "CREATE TRIGGER inject_gram_failure "
                    "BEFORE INSERT ON tm_gram BEGIN "
                    "SELECT RAISE(ABORT, 'injected gram failure'); END"
                )
                connection.commit()
            finally:
                connection.close()

            def gram_plan(
                records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                return SQLiteCandidateWritePlan(
                    gram_rows=(
                        SQLiteGramRow(
                            origin_ordinal=records[0].origin_ordinal,
                            gram_size=2,
                            gram="ab",
                        ),
                    ),
                )

            with self.assertRaises(sqlite3.IntegrityError):
                _ = store.append_batch(
                    batch_id="import.gram-failure",
                    kind="import",
                    drafts=(_draft("abcd", "target"),),
                    source_digest="5" * 64,
                    source_path=(root / "gram-failure.jsonl").resolve(),
                    extension=gram_plan,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

    def test_commit_failure_rolls_back_origin_record_and_candidate_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            class CommitFailingConnection:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self._connection = connection

                def execute(self, *args: Any, **kwargs: Any):
                    return self._connection.execute(*args, **kwargs)

                def executemany(self, *args: Any, **kwargs: Any):
                    return self._connection.executemany(*args, **kwargs)

                def commit(self) -> None:
                    raise sqlite3.OperationalError("injected commit failure")

                def rollback(self) -> None:
                    self._connection.rollback()

            @contextmanager
            def failing_connection(
                database_path: Path,
                **kwargs: Any,
            ) -> Iterator[sqlite3.Connection]:
                with _open_configured_connection(
                    database_path,
                    **kwargs,
                ) as connection:
                    yield cast(
                        sqlite3.Connection,
                        cast(object, CommitFailingConnection(connection)),
                    )

            def gram_plan(
                records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                return SQLiteCandidateWritePlan(
                    gram_rows=(
                        SQLiteGramRow(
                            origin_ordinal=records[0].origin_ordinal,
                            gram_size=2,
                            gram="ab",
                        ),
                    ),
                )

            with patch(
                "tm_sqlite_store._open_configured_connection",
                failing_connection,
            ):
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "injected commit failure",
                ):
                    _ = store.append_batch(
                        batch_id="import.commit-failure",
                        kind="import",
                        drafts=(_draft("abcd", "target"),),
                        source_digest="6" * 64,
                        source_path=(root / "commit-failure.jsonl").resolve(),
                        extension=gram_plan,
                    )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute("SELECT COUNT(*) FROM tm_origin_batch").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
                    connection.execute("SELECT COUNT(*) FROM tm_gram").fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,), (0,), ("0",)))

    def test_reader_observes_only_precommit_or_completed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            entered = threading.Event()
            release = threading.Event()
            writer_errors: list[BaseException] = []

            def pause_extension(
                _records: tuple[SQLiteCandidateRecord, ...],
            ) -> SQLiteCandidateWritePlan:
                entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("reader did not release writer")
                return SQLiteCandidateWritePlan()

            def write() -> None:
                try:
                    _ = store.append_batch(
                        batch_id="import.concurrent",
                        kind="import",
                        drafts=(_draft("concurrent", "first"), _draft("concurrent", "winner")),
                        source_digest="c" * 64,
                        source_path=(Path(temporary) / "concurrent.jsonl").resolve(),
                        extension=pause_extension,
                    )
                except BaseException as error:
                    writer_errors.append(error)

            writer = threading.Thread(target=write)
            writer.start()
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(store.exact_records("concurrent"), ())
            release.set()
            writer.join(timeout=2)
            self.assertFalse(writer.is_alive())
            self.assertEqual(writer_errors, [])
            self.assertEqual(
                tuple(record.target_raw for record in store.exact_records("concurrent")),
                ("winner", "first"),
            )

    def test_drain_rejects_new_operations_without_opening_connections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary_stage = _stage(root, "tm.primary")
            secondary_stage = _stage(root, "tm.secondary")
            initialize_stage_schema(
                primary_stage,
                canonical_store_id="store.primary",
            )
            initialize_stage_schema(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            primary = SQLiteTMStore(
                primary_stage,
                canonical_store_id="store.primary",
            )
            secondary = SQLiteTMStore(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            entered = threading.Event()
            release = threading.Event()
            primary_connection_closed = threading.Event()
            reader_errors: list[BaseException] = []
            switch_errors: list[BaseException] = []
            switch_closed_observations: list[bool] = []
            opened_paths: list[Path] = []

            @contextmanager
            def held_connection(
                database_path: Path,
                **kwargs: Any,
            ) -> Iterator[sqlite3.Connection]:
                opened_paths.append(database_path)
                try:
                    with _open_configured_connection(
                        database_path,
                        **kwargs,
                    ) as connection:
                        if database_path == primary_stage.staged_db_path:
                            entered.set()
                            if not release.wait(timeout=2):
                                raise RuntimeError("test did not release reader")
                        yield connection
                finally:
                    if database_path == primary_stage.staged_db_path:
                        primary_connection_closed.set()

            def read_primary() -> None:
                try:
                    _ = primary.exact_records("source")
                except BaseException as error:
                    reader_errors.append(error)

            def switch_primary() -> None:
                try:
                    _ = primary.coordinator._transition_generation(
                        primary_stage,
                        canonical_store_id="store.primary",
                        expected_prior_generation=0,
                        timeout_seconds=1.0,
                    )
                    switch_closed_observations.append(
                        primary_connection_closed.is_set()
                    )
                except BaseException as error:
                    switch_errors.append(error)

            with patch(
                "tm_sqlite_store._open_configured_connection",
                held_connection,
            ):
                reader = threading.Thread(target=read_primary)
                reader.start()
                self.assertTrue(entered.wait(timeout=2))
                switcher = threading.Thread(target=switch_primary)
                switcher.start()
                self.assertTrue(
                    primary.coordinator.wait_for_state(
                        "DRAINING",
                        timeout_seconds=1.0,
                    )
                )

                calls_before_rejection = len(opened_paths)
                blocked_operations = (
                    lambda: primary.exact_records("source"),
                    lambda: primary.records_by_id(()),
                    lambda: tuple(primary.export_records()),
                    lambda: primary.append(_draft("source", "target")),
                )
                for operation in blocked_operations:
                    with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                        _ = operation()
                    self.assertEqual(
                        raised.exception.code,
                        "STORE.RESOURCE_DRAINING",
                    )
                    self.assertEqual(
                        raised.exception.resource_id,
                        "tm.primary",
                    )
                    self.assertTrue(raised.exception.retryable)
                self.assertEqual(len(opened_paths), calls_before_rejection)
                self.assertEqual(secondary.exact_records("source"), ())

                release.set()
                reader.join(timeout=2)
                switcher.join(timeout=2)

            self.assertFalse(reader.is_alive())
            self.assertFalse(switcher.is_alive())
            self.assertEqual(reader_errors, [])
            self.assertEqual(switch_errors, [])
            self.assertEqual(switch_closed_observations, [True])
            self.assertEqual(primary.coordinator.current_generation, 1)
            self.assertEqual(secondary.coordinator.current_generation, 0)

    def test_drain_timeout_keeps_prior_generation_and_recovers_ready_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            entered = threading.Event()
            release = threading.Event()
            reader_errors: list[BaseException] = []

            @contextmanager
            def held_connection(
                database_path: Path,
                **kwargs: Any,
            ) -> Iterator[sqlite3.Connection]:
                with _open_configured_connection(
                    database_path,
                    **kwargs,
                ) as connection:
                    entered.set()
                    if not release.wait(timeout=2):
                        raise RuntimeError("test did not release reader")
                    yield connection

            def read() -> None:
                try:
                    _ = store.exact_records("source")
                except BaseException as error:
                    reader_errors.append(error)

            with patch(
                "tm_sqlite_store._open_configured_connection",
                held_connection,
            ):
                reader = threading.Thread(target=read)
                reader.start()
                self.assertTrue(entered.wait(timeout=2))
                with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                    _ = store.coordinator._transition_generation(
                        stage,
                        canonical_store_id="store.primary",
                        expected_prior_generation=0,
                        timeout_seconds=0.01,
                    )
                self.assertEqual(raised.exception.code, "STORE.DRAIN_TIMEOUT")
                self.assertEqual(raised.exception.generation, 0)
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(store.coordinator.current_generation, 0)
                self.assertEqual(store.coordinator.state, "READY")
                release.set()
                reader.join(timeout=2)

            self.assertFalse(reader.is_alive())
            self.assertEqual(reader_errors, [])
            self.assertEqual(store.exact_records("source"), ())

    def test_drain_window_tamper_rejects_next_generation_and_keeps_prior(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prior_stage = _stage(root, "tm.primary")
            initialize_stage_schema(
                prior_stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                prior_stage,
                canonical_store_id="store.primary",
            )
            prior_record = store.append(_draft("versioned", "prior"))
            next_stage = MutableStageRef(
                stage_id="stage.tm.primary.next",
                resource_identity=prior_stage.resource_identity,
                staged_db_path=(root / ".tm.primary.next.sqlite3").resolve(),
                manifest_temp_path=(
                    root / ".tm.primary.next.manifest.tmp"
                ).resolve(),
            )
            initialize_stage_schema(
                next_stage,
                canonical_store_id="store.primary",
            )
            next_store = SQLiteTMStore(
                next_stage,
                canonical_store_id="store.primary",
            )
            _ = next_store.append(_draft("versioned", "next"))
            lease_entered = threading.Event()
            release_lease = threading.Event()
            switch_errors: list[BaseException] = []

            def hold_prior_lease() -> None:
                with store.coordinator._operation_lease():
                    lease_entered.set()
                    if not release_lease.wait(timeout=2):
                        raise RuntimeError("test did not release prior lease")

            def switch_generation() -> None:
                try:
                    _ = store.coordinator._transition_generation(
                        next_stage,
                        canonical_store_id="store.primary",
                        expected_prior_generation=0,
                        timeout_seconds=1.0,
                    )
                except BaseException as error:
                    switch_errors.append(error)

            holder = threading.Thread(target=hold_prior_lease)
            switcher = threading.Thread(target=switch_generation)
            holder.start()
            self.assertTrue(lease_entered.wait(timeout=2))
            switcher.start()
            self.assertTrue(
                store.coordinator.wait_for_state(
                    "DRAINING",
                    timeout_seconds=1.0,
                )
            )
            connection = sqlite3.connect(next_stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = 'store.tampered' "
                    "WHERE key = 'canonical_store_id'"
                )
                connection.commit()
            finally:
                connection.close()
            release_lease.set()
            holder.join(timeout=2)
            switcher.join(timeout=2)

            self.assertFalse(holder.is_alive())
            self.assertFalse(switcher.is_alive())
            self.assertEqual(len(switch_errors), 1)
            self.assertIsInstance(
                switch_errors[0],
                SQLiteStoreLifecycleError,
            )
            lifecycle_error = cast(
                SQLiteStoreLifecycleError,
                switch_errors[0],
            )
            self.assertEqual(
                lifecycle_error.code,
                "STORE.NEXT_GENERATION_INVALID",
            )
            self.assertEqual(lifecycle_error.resource_id, "tm.primary")
            self.assertEqual(lifecycle_error.generation, 0)
            self.assertFalse(lifecycle_error.retryable)
            self.assertEqual(store.coordinator.current_generation, 0)
            self.assertEqual(store.coordinator.state, "READY")
            self.assertEqual(store.exact_records("versioned"), (prior_record,))

    def test_generation_switch_exposes_only_complete_old_or_new_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_stage = _stage(root, "tm.primary")
            initialize_stage_schema(
                old_stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                old_stage,
                canonical_store_id="store.primary",
            )
            _ = store.append_batch(
                batch_id="import.old",
                kind="import",
                drafts=(
                    _draft("versioned", "old-first"),
                    _draft("versioned", "old-winner"),
                ),
                source_digest="a" * 64,
                source_path=(root / "old.jsonl").resolve(),
            )

            new_stage = MutableStageRef(
                stage_id="stage.tm.primary.next",
                resource_identity=old_stage.resource_identity,
                staged_db_path=(root / ".tm.primary.next.sqlite3").resolve(),
                manifest_temp_path=(root / ".tm.primary.next.manifest").resolve(),
            )
            initialize_stage_schema(
                new_stage,
                canonical_store_id="store.primary",
            )
            new_store = SQLiteTMStore(
                new_stage,
                canonical_store_id="store.primary",
            )
            _ = new_store.append_batch(
                batch_id="import.new",
                kind="import",
                drafts=(
                    _draft("versioned", "new-first"),
                    _draft("versioned", "new-winner"),
                ),
                source_digest="b" * 64,
                source_path=(root / "new.jsonl").resolve(),
            )

            observations: list[tuple[str, ...]] = []
            stop = threading.Event()
            saw_old = threading.Event()
            reader_errors: list[BaseException] = []

            def observe() -> None:
                try:
                    while not stop.is_set():
                        try:
                            observed = tuple(
                                record.target_raw
                                for record in store.exact_records("versioned")
                            )
                            observations.append(observed)
                            if observed == ("old-winner", "old-first"):
                                saw_old.set()
                        except SQLiteStoreLifecycleError as error:
                            if error.code != "STORE.RESOURCE_DRAINING":
                                raise
                except BaseException as error:
                    reader_errors.append(error)

            readers = tuple(threading.Thread(target=observe) for _ in range(4))
            for reader in readers:
                reader.start()
            self.assertTrue(saw_old.wait(timeout=2))
            self.assertEqual(
                store.coordinator._transition_generation(
                    new_stage,
                    canonical_store_id="store.primary",
                    expected_prior_generation=0,
                    timeout_seconds=1.0,
                ),
                1,
            )
            for _ in range(20):
                observations.append(
                    tuple(
                        record.target_raw
                        for record in store.exact_records("versioned")
                    )
                )
            stop.set()
            for reader in readers:
                reader.join(timeout=2)

            self.assertTrue(all(not reader.is_alive() for reader in readers))
            self.assertEqual(reader_errors, [])
            self.assertIn(("old-winner", "old-first"), observations)
            self.assertIn(("new-winner", "new-first"), observations)
            self.assertEqual(
                set(observations),
                {
                    ("old-winner", "old-first"),
                    ("new-winner", "new-first"),
                },
            )

    def test_busy_timeout_is_resource_local_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary_stage = _stage(root, "tm.primary")
            secondary_stage = _stage(root, "tm.secondary")
            initialize_stage_schema(
                primary_stage,
                canonical_store_id="store.primary",
            )
            initialize_stage_schema(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            primary = SQLiteTMStore(
                primary_stage,
                canonical_store_id="store.primary",
            )
            secondary = SQLiteTMStore(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            blocker = sqlite3.connect(
                primary_stage.staged_db_path,
                isolation_level=None,
            )
            try:
                blocker.execute("PRAGMA journal_mode=DELETE")
                blocker.execute("BEGIN EXCLUSIVE")
                with patch("tm_sqlite_store.BUSY_TIMEOUT_MS", 25):
                    with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                        _ = primary.append(_draft("locked", "not written"))
                self.assertEqual(raised.exception.code, "STORE.BUSY_TIMEOUT")
                self.assertEqual(raised.exception.resource_id, "tm.primary")
                self.assertEqual(raised.exception.generation, 0)
                self.assertTrue(raised.exception.retryable)

                secondary_record = secondary.append(
                    _draft("available", "written")
                )
                self.assertEqual(
                    secondary.exact_records("available"),
                    (secondary_record,),
                )
            finally:
                blocker.rollback()
                blocker.close()
            self.assertEqual(primary.exact_records("locked"), ())


    def test_streamed_append_completes_one_batch_across_bounded_chunks(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage = _stage(root)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage,
                            canonical_store_id="store.primary",
                        )
                        store = SQLiteTMStore(
                            stage,
                            canonical_store_id="store.primary",
                        )
                    sources = (
                        "A",
                        "a",
                        "same",
                        "same",
                        *(f"source-{index:04d}" for index in range(4, 1250)),
                    )
                    drafts = (
                        _draft(source, f"target-{index:04d}")
                        for index, source in enumerate(sources)
                    )
                    stream = ((draft, index + 1) for index, draft in enumerate(drafts))
                    original_frequencies = (
                        tm_sqlite_store.character_ngram_frequencies
                    )
                    frequency_calls: dict[tuple[str, int], int] = {}

                    def recording_frequencies(
                        folded_text: str,
                        gram_size: int,
                    ) -> tuple[tuple[str, int], ...]:
                        key = (folded_text, gram_size)
                        frequency_calls[key] = frequency_calls.get(key, 0) + 1
                        return original_frequencies(folded_text, gram_size)

                    with patch(
                        "tm_sqlite_store.character_ngram_frequencies",
                        side_effect=recording_frequencies,
                    ):
                        store.append_streamed_batch(
                            batch_id="migration.streamed",
                            kind="migration",
                            drafts=stream,
                            source_digest="a" * 64,
                            source_path=(root / "streamed.jsonl").resolve(),
                            invalid_count=0,
                            duplicate_source_count=0,
                            chunk_size=500,
                        )
                    required_sizes = (
                        (1, 2) if fts5_available else (1, 2, 3)
                    )
                    expected_frequency_calls = Counter(
                        (tm_sqlite_store.fold_text_value_v1(source), gram_size)
                        for source in sources
                        for gram_size in required_sizes
                    )
                    self.assertEqual(
                        frequency_calls,
                        dict(expected_frequency_calls),
                    )
                    revision = store.canonical_revision()
                    self.assertEqual(revision.head_revision, 1)
                    self.assertEqual(revision.record_count, 1250)
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        batch = connection.execute(
                            "SELECT batch_id, kind, status, valid_count, "
                            "invalid_count, duplicate_source_count, "
                            "completed_revision FROM tm_origin_batch"
                        ).fetchone()
                        bounds = connection.execute(
                            "SELECT COUNT(*), MIN(record_id), MAX(record_id), "
                            "MIN(origin_ordinal), MAX(origin_ordinal) "
                            "FROM tm_record"
                        ).fetchone()
                        gram_sizes = {
                            int(row[0])
                            for row in connection.execute(
                                "SELECT DISTINCT gram_size FROM tm_gram"
                            ).fetchall()
                        }
                        proof_blocks = connection.execute(
                            "SELECT block_id, first_record_id, last_record_id, "
                            "record_count FROM tm_candidate_block "
                            "ORDER BY block_id"
                        ).fetchall()
                        proof_validation = validate_candidate_proof_index(
                            connection,
                            required_sizes=required_sizes,
                            fts5_available=fts5_available,
                        )
                    finally:
                        connection.close()
                    self.assertEqual(
                        batch,
                        (
                            "migration.streamed",
                            "migration",
                            "completed",
                            1250,
                            0,
                            0,
                            1,
                        ),
                    )
                    self.assertEqual(
                        bounds,
                        (1250, 1, 1250, 0, 1249),
                    )
                    self.assertEqual(
                        gram_sizes,
                        set(required_sizes),
                    )
                    self.assertEqual(
                        tuple(size for size, _count in proof_validation[0]),
                        required_sizes,
                    )
                    self.assertEqual(
                        proof_validation[1],
                        1250 if fts5_available else 0,
                    )
                    self.assertEqual(
                        proof_blocks,
                        [
                            (block_id, block_id * 256 + 1, block_id * 256 + 256, count)
                            for block_id, count in (
                                (0, 256),
                                (1, 256),
                                (2, 256),
                                (3, 256),
                                (4, 226),
                            )
                        ],
                    )
                    records = tuple(store.export_records())
                    self.assertEqual(
                        tuple(
                            (
                                record.record_id,
                                record.origin_ordinal,
                                record.legacy_line_no,
                            )
                            for record in (records[0], records[-1])
                        ),
                        ((1, 0, 1), (1250, 1249, 1250)),
                    )

    def test_streamed_append_mid_stream_failure_never_completes_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def broken_stream():
                for index in range(600):
                    if index == 550:
                        raise RuntimeError("injected mid-stream failure")
                    yield _draft(f"s{index}", f"t{index}"), index + 1

            with self.assertRaisesRegex(
                RuntimeError,
                "injected mid-stream failure",
            ):
                store.append_streamed_batch(
                    batch_id="migration.streamed-failure",
                    kind="migration",
                    drafts=broken_stream(),
                    source_digest="b" * 64,
                    source_path=(root / "streamed-failure.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=500,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                state = (
                    connection.execute(
                        "SELECT status, completed_revision FROM tm_origin_batch"
                    ).fetchone(),
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_record"
                    ).fetchone(),
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(
                state,
                (("staged", None), (500,), ("0",)),
            )

    def test_streamed_append_rejects_noncontiguous_record_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            store.append_streamed_batch(
                batch_id="migration.initial-stream",
                kind="migration",
                drafts=iter(
                    (_draft(f"s{index}", f"t{index}"), index + 1)
                    for index in range(3)
                ),
                source_digest="e" * 64,
                source_path=(root / "initial-stream.jsonl").resolve(),
                invalid_count=0,
                duplicate_source_count=0,
                chunk_size=3,
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("DELETE FROM tm_record WHERE record_id = 2")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE.RECORD_ID_SEQUENCE_INVALID$",
            ):
                store.append_streamed_batch(
                    batch_id="migration.rejected-stream",
                    kind="migration",
                    drafts=iter(((_draft("new", "not inserted"), 4),)),
                    source_digest="f" * 64,
                    source_path=(root / "rejected-stream.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=3,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT record_id, source_raw FROM tm_record "
                        "ORDER BY record_id"
                    ).fetchall(),
                    [(1, "s0"), (3, "s2")],
                )
            finally:
                connection.close()

    def test_second_streamed_chunk_summary_failure_rolls_back_only_chunk(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage = _stage(root)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        initialize_stage_schema(
                            stage,
                            canonical_store_id="store.primary",
                        )
                        store = SQLiteTMStore(
                            stage,
                            canonical_store_id="store.primary",
                        )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(
                            "CREATE TRIGGER inject_second_chunk_failure "
                            "BEFORE INSERT ON tm_gram_block_max "
                            "WHEN NEW.gram = 'z' BEGIN "
                            "SELECT RAISE(ABORT, 'injected second chunk failure'); "
                            "END"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError,
                        "injected second chunk failure",
                    ):
                        store.append_streamed_batch(
                            batch_id="migration.second-chunk-failure",
                            kind="migration",
                            drafts=iter(
                                (
                                    (_draft("aa", "first"), 1),
                                    (_draft("bb", "second"), 2),
                                    (_draft("zz", "rolled-back"), 3),
                                )
                            ),
                            source_digest="d" * 64,
                            source_path=(root / "second-chunk.jsonl").resolve(),
                            invalid_count=0,
                            duplicate_source_count=0,
                            chunk_size=2,
                        )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        batch = connection.execute(
                            "SELECT status, valid_count, completed_revision "
                            "FROM tm_origin_batch"
                        ).fetchone()
                        record_rows = connection.execute(
                            "SELECT record_id, source_raw FROM tm_record "
                            "ORDER BY record_id"
                        ).fetchall()
                        gram_record_ids = connection.execute(
                            "SELECT DISTINCT record_id FROM tm_gram "
                            "ORDER BY record_id"
                        ).fetchall()
                        proof_blocks = connection.execute(
                            "SELECT block_id, record_count "
                            "FROM tm_candidate_block ORDER BY block_id"
                        ).fetchall()
                        z_maxima = connection.execute(
                            "SELECT COUNT(*) FROM tm_gram_block_max "
                            "WHERE gram IN ('z', 'zz')"
                        ).fetchone()
                        head = connection.execute(
                            "SELECT value FROM tm_meta "
                            "WHERE key = 'head_revision'"
                        ).fetchone()
                        fts_count = (
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_fts"
                            ).fetchone()
                            if fts5_available
                            else (0,)
                        )
                        validation = validate_candidate_proof_index(
                            connection,
                            required_sizes=(
                                (1, 2) if fts5_available else (1, 2, 3)
                            ),
                            fts5_available=fts5_available,
                        )
                    finally:
                        connection.close()
                    self.assertEqual(batch, ("staged", 0, None))
                    self.assertEqual(record_rows, [(1, "aa"), (2, "bb")])
                    self.assertEqual(gram_record_ids, [(1,), (2,)])
                    self.assertEqual(proof_blocks, [(0, 2)])
                    self.assertEqual(z_maxima, (0,))
                    self.assertEqual(head, ("0",))
                    self.assertEqual(
                        fts_count,
                        (2,) if fts5_available else (0,),
                    )
                    self.assertEqual(
                        validation[1],
                        2 if fts5_available else 0,
                    )

    def test_streamed_append_empty_stream_completes_zero_record_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            store.append_streamed_batch(
                batch_id="migration.streamed-empty",
                kind="migration",
                drafts=iter(()),
                source_digest="c" * 64,
                source_path=(root / "streamed-empty.jsonl").resolve(),
                invalid_count=1,
                duplicate_source_count=0,
                chunk_size=500,
            )
            revision = store.canonical_revision()
            self.assertEqual(revision.head_revision, 1)
            self.assertEqual(revision.record_count, 0)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                batch = connection.execute(
                    "SELECT status, valid_count, invalid_count, "
                    "completed_revision FROM tm_origin_batch"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                batch,
                ("completed", 0, 1, 1),
            )

    def test_streamed_append_rejects_deceptive_values_before_completion(
        self,
    ) -> None:
        class DeceptiveStr(str):
            pass

        class DraftSubclass(TMRecordDraft):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )

            def stream() -> Iterator[tuple[TMRecordDraft, int]]:
                yield _draft("ok", "target"), 1
                yield cast(
                    TMRecordDraft,
                    DraftSubclass(
                        "bad",
                        "target",
                        None,
                        None,
                        None,
                        None,
                        (("source", "test"),),
                    ),
                ), 2

            with self.assertRaises(TypeError):
                store.append_streamed_batch(
                    batch_id="migration.streamed-strict",
                    kind="migration",
                    drafts=stream(),
                    source_digest=DeceptiveStr("d" * 64),
                    source_path=(root / "strict.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=500,
                )
            with self.assertRaisesRegex(
                TypeError,
                "draft stream entries",
            ):
                store.append_streamed_batch(
                    batch_id="migration.streamed-shape",
                    kind="migration",
                    drafts=iter(
                        cast(tuple[tuple[Any, Any], ...], (("not", "a", "pair"),))
                    ),
                    source_digest="e" * 64,
                    source_path=(root / "shape.jsonl").resolve(),
                    invalid_count=0,
                    duplicate_source_count=0,
                    chunk_size=500,
                )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_origin_batch"
                    ).fetchone(),
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_record"
                    ).fetchone(),
                )
            finally:
                connection.close()
            self.assertEqual(counts, ((0,), (0,)))


class SQLiteSchemaTests(unittest.TestCase):
    def test_dangling_symlink_stage_never_creates_reserved_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe_stage = _stage(root)
            targets = (
                safe_stage.resource_identity.configured_jsonl_path,
                safe_stage.resource_identity.canonical_sidecar_path,
            )
            for index, target in enumerate(targets):
                with self.subTest(target=target.name):
                    staged_path = (
                        root / f".dangling-{index}.stage.sqlite3"
                    ).resolve()
                    os.symlink(target, staged_path)
                    unsafe_stage = MutableStageRef(
                        stage_id=f"stage.dangling.{index}",
                        resource_identity=safe_stage.resource_identity,
                        staged_db_path=staged_path,
                        manifest_temp_path=(
                            root / f".dangling-{index}.manifest.tmp"
                        ).resolve(),
                    )

                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "STORE.STAGE_PATH_UNSAFE",
                    ):
                        _ = initialize_stage_schema(
                            unsafe_stage,
                            canonical_store_id="store.primary",
                        )
                    self.assertFalse(target.exists())
                    self.assertTrue(staged_path.is_symlink())
                    staged_path.unlink()

    def test_stage_schema_rejects_reserved_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = (root / "tm.primary.jsonl").resolve()
            identity = CanonicalResourceIdentity.from_configured_jsonl(
                "tm.primary",
                configured,
            )
            unsafe_stage = MutableStageRef(
                stage_id="stage.unsafe",
                resource_identity=identity,
                staged_db_path=configured,
                manifest_temp_path=(
                    root / ".tm.primary.snapshot.tmp"
                ).resolve(),
            )

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.STAGE_PATH_RESERVED",
            ):
                _ = initialize_stage_schema(
                    unsafe_stage,
                    canonical_store_id="store.primary",
                )
            self.assertFalse(configured.exists())

    def test_stage_schema_records_safe_connection_and_runtime_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            created = initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            inspected = inspect_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )

            self.assertEqual(created, inspected)
            self.assertEqual(inspected.schema_version, TM_SCHEMA_VERSION)
            self.assertEqual(inspected.resource_id, "tm.primary")
            self.assertEqual(
                inspected.canonical_store_id,
                "store.primary",
            )
            self.assertEqual(inspected.generation, 0)
            self.assertEqual(inspected.head_revision, 0)
            self.assertEqual(inspected.fold_version, FOLD_VERSION_V1)
            self.assertEqual(
                inspected.candidate_index_version,
                CANDIDATE_INDEX_VERSION,
            )
            self.assertEqual(inspected.journal_mode, "delete")
            self.assertEqual(inspected.synchronous, "FULL")
            self.assertTrue(inspected.foreign_keys)
            self.assertEqual(inspected.busy_timeout_ms, 5000)
            self.assertFalse(inspected.wal_enabled)
            self.assertFalse(inspected.extension_loading_enabled)
            self.assertEqual(
                set(inspected.table_names),
                {
                    "tm_candidate_block",
                    "tm_gram",
                    "tm_gram_block_max",
                    "tm_meta",
                    "tm_origin_batch",
                    "tm_record",
                    "tm_snapshot_binding",
                    "tm_snapshot_receipt",
                    *(
                        ("tm_fts",)
                        if inspected.fts5_available
                        else ()
                    ),
                },
            )
            self.assertEqual(
                set(inspected.index_names),
                {
                    "idx_tm_context_speaker",
                    "idx_tm_exact",
                    "idx_tm_gram_lookup",
                    "idx_tm_gram_block_lookup",
                },
            )
            self.assertEqual(inspected.activation_status, "UNPUBLISHED")
            self.assertIsNone(inspected.activation_digest)

    def test_schema_keeps_resources_physically_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = _stage(root, "tm.primary")
            secondary = _stage(root, "tm.secondary")
            initialize_stage_schema(
                primary,
                canonical_store_id="store.primary",
            )
            initialize_stage_schema(
                secondary,
                canonical_store_id="store.secondary",
            )

            first = inspect_stage_schema(
                primary,
                canonical_store_id="store.primary",
            )
            second = inspect_stage_schema(
                secondary,
                canonical_store_id="store.secondary",
            )

            self.assertNotEqual(
                primary.staged_db_path,
                secondary.staged_db_path,
            )
            self.assertEqual(first.resource_id, "tm.primary")
            self.assertEqual(second.resource_id, "tm.secondary")
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.IDENTITY_MISMATCH",
            ):
                _ = inspect_stage_schema(
                    primary,
                    canonical_store_id="store.secondary",
                )

    def test_schema_rejects_too_new_or_incomplete_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = ? WHERE key = ?",
                    (str(TM_SCHEMA_VERSION + 1), "schema_version"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.SCHEMA_TOO_NEW",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "DELETE FROM tm_meta WHERE key = ?",
                    ("resource_id",),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.META_INCOMPLETE",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_schema_rejects_tampered_semantics_and_stage_state(self) -> None:
        cases = (
            ("fold_version", "fold-v999", "STORE.META_VERSION_MISMATCH"),
            ("scorer_version", "scorer-v999", "STORE.META_VERSION_MISMATCH"),
            (
                "text_semantics_version",
                "text-v999",
                "STORE.META_VERSION_MISMATCH",
            ),
            (
                "candidate_index_version",
                "candidate-v999",
                "STORE.META_VERSION_MISMATCH",
            ),
            (
                "candidate_index_kind",
                "UNKNOWN",
                "STORE.CANDIDATE_INDEX_MISMATCH",
            ),
            ("journal_mode", "wal", "STORE.JOURNAL_MODE_UNSAFE"),
            ("activation_status", "ACTIVE", "STORE.STAGE_PUBLISHED"),
            ("divergence_latched", "1", "STORE.STAGE_DIVERGED"),
        )
        for key, value, error_code in cases:
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as temporary:
                    stage = _stage(Path(temporary))
                    initialize_stage_schema(
                        stage,
                        canonical_store_id="store.primary",
                    )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(
                            "UPDATE tm_meta SET value = ? WHERE key = ?",
                            (value, key),
                        )
                        connection.commit()
                    finally:
                        connection.close()

                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        error_code,
                    ):
                        _ = inspect_stage_schema(
                            stage,
                            canonical_store_id="store.primary",
                        )

    def test_schema_rejects_unexpected_objects_for_current_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("CREATE TABLE tm_unexpected(value TEXT)")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.SCHEMA_UNEXPECTED",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_schema_rejects_weakened_table_with_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DROP TABLE tm_origin_batch")
                connection.execute(
                    "CREATE TABLE tm_origin_batch ("
                    "batch_id TEXT PRIMARY KEY)"
                )
                resigned = _schema_digest(
                    connection,
                    fts5_available=(
                        connection.execute(
                            "SELECT value FROM tm_meta WHERE key = ?",
                            ("fts5_available",),
                        ).fetchone()
                        == ("1",)
                    ),
                )
                connection.execute(
                    "UPDATE tm_meta SET value = ? WHERE key = ?",
                    (resigned, "schema_digest"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.TABLE_SCHEMA_MISMATCH",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_fts_schema_rejects_hidden_extra_and_missing_shadow(
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
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("CREATE TABLE tm_fts_evil(value TEXT)")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.SCHEMA_UNEXPECTED",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts_evil")
                connection.execute("DROP TABLE tm_fts_data")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.SCHEMA_INCOMPLETE",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_fts_virtual_table_cannot_be_replaced_and_resigned(
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
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts")
                for table_name in (
                    "tm_fts",
                    "tm_fts_config",
                    "tm_fts_content",
                    "tm_fts_data",
                    "tm_fts_docsize",
                    "tm_fts_idx",
                ):
                    connection.execute(
                        f"CREATE TABLE {table_name}(value TEXT)"
                    )
                resigned = _schema_digest(
                    connection,
                    fts5_available=True,
                )
                connection.execute(
                    "UPDATE tm_meta SET value = ? WHERE key = ?",
                    (resigned, "schema_digest"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.TABLE_SCHEMA_MISMATCH",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_fts_shadow_cannot_be_replaced_same_name_and_resigned(
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
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute("DROP TABLE tm_fts_data")
                connection.execute("CREATE TABLE tm_fts_data(value TEXT)")
                resigned = _schema_digest(
                    connection,
                    fts5_available=True,
                )
                connection.execute(
                    "UPDATE tm_meta SET value = ? WHERE key = ?",
                    (resigned, "schema_digest"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.TABLE_SCHEMA_MISMATCH",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_schema_rejects_unapproved_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "CREATE TRIGGER tm_meta_mutator "
                    "AFTER INSERT ON tm_meta BEGIN "
                    "DELETE FROM tm_meta WHERE key = NEW.key; END"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.SCHEMA_UNEXPECTED",
            ):
                _ = inspect_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

    def test_failed_schema_creation_removes_unpublished_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            broken_schema = (
                "CREATE TABLE partial(value TEXT)",
                "THIS IS NOT VALID SQL",
            )

            with patch(
                "tm_sqlite_store._SCHEMA_STATEMENTS",
                broken_schema,
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    _ = initialize_stage_schema(
                        stage,
                        canonical_store_id="store.primary",
                    )

            self.assertFalse(stage.staged_db_path.exists())

    def test_connection_setup_failure_removes_new_stage_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            with patch(
                "tm_sqlite_store._pragma_text",
                return_value="wal",
            ):
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "STORE.WAL_FORBIDDEN",
                ):
                    _ = initialize_stage_schema(
                        stage,
                        canonical_store_id="store.primary",
                    )

            self.assertFalse(stage.staged_db_path.exists())

    def test_existing_wal_database_is_rejected_not_converted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = (Path(temporary) / "unsafe.sqlite3").resolve()
            connection = sqlite3.connect(path)
            try:
                mode = connection.execute(
                    "PRAGMA journal_mode=WAL"
                ).fetchone()
                self.assertEqual(mode, ("wal",))
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "STORE.WAL_FORBIDDEN",
            ):
                with _open_configured_connection(path):
                    self.fail("WAL database must not be opened")

    def test_extension_loading_is_disabled_for_every_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = (Path(temporary) / "safe.sqlite3").resolve()
            with _open_configured_connection(path) as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    _ = connection.execute(
                        "SELECT load_extension('not-a-real-extension')"
                    ).fetchone()

    def test_no_fts_runtime_uses_fallback_schema_without_claiming_fuzzy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = _stage(Path(temporary))
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                snapshot = initialize_stage_schema(
                    stage,
                    canonical_store_id="store.primary",
                )

            self.assertFalse(snapshot.fts5_available)
            self.assertEqual(
                snapshot.candidate_index_kind,
                "GRAM_FALLBACK",
            )
            self.assertNotIn("tm_fts", snapshot.table_names)
            self.assertFalse(snapshot.fuzzy_available)


def _query_view_snapshot(
    source: SQLiteTMStore | tm_sqlite_store.SQLiteTMQueryView,
    folded_query: str,
) -> tm_sqlite_store.SQLiteCandidateRecallSnapshot:
    trigrams = (
        None
        if len(folded_query) < 3
        else unique_character_ngrams(folded_query, 3)
    )
    return source.candidate_recall_snapshot(
        fts_query_trigrams=trigrams,
        query_grams_by_size=tuple(
            (
                gram_size,
                unique_character_ngrams(folded_query, gram_size),
            )
            for gram_size in (
                (3, 2, 1)
                if len(folded_query) >= 3
                else (len(folded_query),)
            )
        ),
        candidate_floor=2048,
        fts_query_degenerate=(
            len(trigrams) <= 1 if trigrams is not None else False
        ),
    )


class SQLiteTMQueryViewTests(unittest.TestCase):
    def _store_with_records(
        self,
        root: Path,
        *,
        fts5_available: bool,
        sources: tuple[str, ...] = ("Open Door", "Close Window", "猫狗"),
    ) -> tuple[SQLiteTMStore, tuple[int, ...]]:
        stage = _stage(root)
        with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
        records = store.append_batch(
            batch_id="import.query-view",
            kind="import",
            drafts=tuple(
                _draft(source, f"target-{ordinal}")
                for ordinal, source in enumerate(sources)
            ),
            source_digest="a" * 64,
            source_path=(root / "query-view.jsonl").resolve(),
            extension=lambda values: build_candidate_write_plan(
                values,
                fts5_available=fts5_available,
            ),
        )
        return store, tuple(record.record_id for record in records)

    def test_dense_phase2_returns_only_strict_ordered_fold_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = self._store_with_records(
                Path(temporary),
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
                request = (1, 2, 3)
                response = view.candidate_proof_dense_phase2(
                    folded_query="open",
                    blocks=snapshot.blocks,
                    head_revision=snapshot.head_revision,
                    total_record_count=snapshot.total_record_count,
                    query_maxima_digest=snapshot.query_maxima_digest,
                    binding_digest=phase1.binding_digest,
                    record_ids=request,
                    source_fold_lengths=tuple(
                        phase1.source_fold_lengths[record_id - 1]
                        for record_id in request
                    ),
                )
                tm_sqlite_store._validate_candidate_proof_dense_phase1_result(
                    phase1,
                    view=view,
                    folded_query="open",
                    blocks=snapshot.blocks,
                    head_revision=snapshot.head_revision,
                    total_record_count=snapshot.total_record_count,
                    query_maxima_digest=snapshot.query_maxima_digest,
                )
                tm_sqlite_store._validate_candidate_proof_dense_phase2_result(
                    response,
                    binding_digest=phase1.binding_digest,
                    record_ids=request,
                    source_fold_lengths=tuple(
                        phase1.source_fold_lengths[record_id - 1]
                        for record_id in request
                    ),
                )
                self.assertEqual(
                    response.record_ids,
                    request,
                )
                self.assertEqual(
                    response.source_folds_v1,
                    ("open door", "close window", "猫狗"),
                )
                self.assertEqual(
                    response.source_fold_lengths,
                    tuple(len(value) for value in response.source_folds_v1),
                )
                self.assertFalse(any(
                    hasattr(response, attribute)
                    for attribute in (
                        "character_multiset_intersections",
                        "scorer_evidence",
                        "similarity_evidence",
                    )
                ))

                forged_lengths = replace(
                    phase1,
                    source_fold_lengths=(
                        phase1.source_fold_lengths[0] + 1,
                        *phase1.source_fold_lengths[1:],
                    ),
                )
                forged_bigrams = replace(
                    phase1,
                    bigram_multiset_intersections=(
                        0,
                        *phase1.bigram_multiset_intersections[1:],
                    ),
                )
                forged_fold = replace(
                    response,
                    source_folds_v1=(
                        "x" * response.source_fold_lengths[0],
                        *response.source_folds_v1[1:],
                    ),
                )
                forged_receipt = replace(response, _receipt=phase1._receipt)
                with self.assertRaises(TypeError):
                    replace(
                        cast(Any, response._receipt),
                        source_folds_v1=forged_fold.source_folds_v1,
                    )
                coordinated_phase1 = copy.deepcopy(
                    phase1,
                    {
                        id(phase1.source_fold_lengths): forged_lengths.source_fold_lengths,
                        id(phase1.bigram_multiset_intersections): (
                            forged_bigrams.bigram_multiset_intersections
                        ),
                    },
                )
                coordinated_phase2 = copy.deepcopy(
                    response,
                    {
                        id(response.source_folds_v1): forged_fold.source_folds_v1,
                    },
                )
                for forged_phase1 in (
                    forged_lengths,
                    forged_bigrams,
                    coordinated_phase1,
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_INVALID$",
                    ):
                        tm_sqlite_store._validate_candidate_proof_dense_phase1_result(
                            forged_phase1,
                            view=view,
                            folded_query="open",
                            blocks=snapshot.blocks,
                            head_revision=snapshot.head_revision,
                            total_record_count=snapshot.total_record_count,
                            query_maxima_digest=snapshot.query_maxima_digest,
                        )
                for forged_phase2 in (
                    forged_fold,
                    forged_receipt,
                    coordinated_phase2,
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.CANDIDATE_PROOF_INVALID$",
                    ):
                        tm_sqlite_store._validate_candidate_proof_dense_phase2_result(
                            forged_phase2,
                            binding_digest=phase1.binding_digest,
                            record_ids=request,
                            source_fold_lengths=tuple(
                                phase1.source_fold_lengths[record_id - 1]
                                for record_id in request
                            ),
                        )

                other_snapshot = view.candidate_proof_snapshot(
                    folded_query="other",
                    seed_limit=256,
                )
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "^STORE.CANDIDATE_PROOF_INVALID$",
                ):
                    tm_sqlite_store._validate_candidate_proof_dense_phase1_result(
                        phase1,
                        view=view,
                        folded_query="other",
                        blocks=other_snapshot.blocks,
                        head_revision=other_snapshot.head_revision,
                        total_record_count=other_snapshot.total_record_count,
                        query_maxima_digest=other_snapshot.query_maxima_digest,
                    )

                invalid_calls: tuple[dict[str, Any], ...] = (
                    {"folded_query": "other"},
                    {"head_revision": snapshot.head_revision + 1},
                    {"total_record_count": snapshot.total_record_count + 1},
                    {"query_maxima_digest": "0" * 64},
                    {"binding_digest": "0" * 64},
                    {"record_ids": (2, 1, 3)},
                    {"record_ids": (1, 1, 3)},
                    {"record_ids": (1, 2, 4)},
                )
                base: dict[str, Any] = {
                    "folded_query": "open",
                    "blocks": snapshot.blocks,
                    "head_revision": snapshot.head_revision,
                    "total_record_count": snapshot.total_record_count,
                    "query_maxima_digest": snapshot.query_maxima_digest,
                    "binding_digest": phase1.binding_digest,
                    "record_ids": request,
                    "source_fold_lengths": tuple(
                        phase1.source_fold_lengths[record_id - 1]
                        for record_id in request
                    ),
                }
                for changes in invalid_calls:
                    with self.subTest(changes=changes), self.assertRaises(
                        (SQLiteStoreSchemaError, ValueError),
                    ):
                        view.candidate_proof_dense_phase2(
                            **{**base, **changes},
                        )

    def test_query_view_is_read_only_and_matches_store_ports_under_one_lease(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    store, record_ids = self._store_with_records(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    query = "Open Door" if fts5_available else "猫狗"
                    folded = query
                    with store.query_lease() as view:
                        self.assertIs(
                            type(view),
                            tm_sqlite_store.SQLiteTMQueryView,
                        )
                        self.assertEqual(view.resource_id, "tm.primary")
                        self.assertEqual(view.generation, 0)
                        self.assertEqual(
                            store.coordinator._active_lease_count,
                            1,
                        )
                        self.assertEqual(
                            view.exact_records("Open Door"),
                            store.exact_records("Open Door"),
                        )
                        self.assertEqual(
                            view.records_by_id(record_ids),
                            store.records_by_id(record_ids),
                        )
                        self.assertEqual(
                            view.records_by_id(record_ids[::-1]),
                            store.records_by_id(record_ids[::-1]),
                        )
                        self.assertEqual(
                            _query_view_snapshot(view, folded),
                            _query_view_snapshot(store, folded),
                        )
                        self.assertEqual(view.health(), store.health())
                        for missing_port in (
                            "append",
                            "append_batch",
                            "export_records",
                            "activate",
                            "update",
                        ):
                            self.assertFalse(hasattr(view, missing_port))
                    self.assertEqual(
                        store.coordinator._active_lease_count,
                        0,
                    )
                    for call in (
                        lambda: view.exact_records("Open Door"),
                        lambda: view.records_by_id(record_ids),
                        lambda: _query_view_snapshot(view, folded),
                        lambda: view.health(),
                    ):
                        with self.assertRaises(
                            SQLiteStoreLifecycleError
                        ) as raised:
                            _ = call()
                        self.assertEqual(
                            raised.exception.code,
                            "STORE.QUERY_VIEW_EXPIRED",
                        )
                        self.assertFalse(raised.exception.retryable)

    def test_expired_query_view_fails_closed_without_connection_or_new_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=True,
            )
            with store.query_lease() as view:
                pass
            with patch(
                "tm_sqlite_store._open_configured_connection",
                side_effect=AssertionError("connection opened after expiry"),
            ) as open_connection:
                with self.assertRaises(
                    SQLiteStoreLifecycleError
                ) as raised:
                    _ = view.exact_records("Open Door")
                self.assertEqual(
                    raised.exception.code,
                    "STORE.QUERY_VIEW_EXPIRED",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(raised.exception.generation, 0)
                self.assertEqual(raised.exception.resource_id, "tm.primary")
                open_connection.assert_not_called()
            self.assertEqual(store.coordinator._active_lease_count, 0)

    def test_query_view_survives_drain_but_rejects_drift_or_foreign_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary_stage = _stage(root, "tm.primary")
            secondary_stage = _stage(root, "tm.secondary")
            initialize_stage_schema(
                primary_stage,
                canonical_store_id="store.primary",
            )
            initialize_stage_schema(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            primary = SQLiteTMStore(
                primary_stage,
                canonical_store_id="store.primary",
            )
            secondary = SQLiteTMStore(
                secondary_stage,
                canonical_store_id="store.secondary",
            )
            with primary.query_lease() as view:
                with primary.coordinator._condition:
                    primary.coordinator._state = "DRAINING"
                self.assertEqual(view.exact_records("source"), ())
                with primary.coordinator._condition:
                    primary.coordinator._state = "READY"
                    primary.coordinator._view = (
                        secondary.coordinator._view
                    )
                with self.assertRaises(
                    SQLiteStoreLifecycleError
                ) as raised:
                    _ = view.exact_records("source")
                self.assertEqual(
                    raised.exception.code,
                    "STORE.GENERATION_CHANGED",
                )
                view._store = secondary
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError,
                    "STORE.QUERY_VIEW_FOREIGN",
                ):
                    _ = view.exact_records("source")

    def test_query_lease_blocks_generation_publication_until_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            initialize_stage_schema(
                stage,
                canonical_store_id="store.primary",
            )
            store = SQLiteTMStore(
                stage,
                canonical_store_id="store.primary",
            )
            transition_done = threading.Event()
            transition_errors: list[BaseException] = []

            def publish_next_generation() -> None:
                try:
                    _ = store.coordinator._transition_generation(
                        stage,
                        canonical_store_id="store.primary",
                        expected_prior_generation=0,
                        timeout_seconds=2.0,
                    )
                except BaseException as error:
                    transition_errors.append(error)
                finally:
                    transition_done.set()

            with store.query_lease() as view:
                _ = view.exact_records("source")
                publisher = threading.Thread(
                    target=publish_next_generation
                )
                publisher.start()
                self.assertTrue(
                    store.coordinator.wait_for_state(
                        "DRAINING",
                        timeout_seconds=1.0,
                    )
                )
                self.assertFalse(transition_done.is_set())
                with self.assertRaises(
                    SQLiteStoreLifecycleError
                ) as raised:
                    with store.query_lease():
                        pass
                self.assertEqual(
                    raised.exception.code,
                    "STORE.RESOURCE_DRAINING",
                )
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(view.exact_records("source"), ())

            publisher.join(timeout=3)
            self.assertFalse(publisher.is_alive())
            self.assertEqual(transition_errors, [])
            self.assertTrue(transition_done.is_set())
            self.assertEqual(store.coordinator.current_generation, 1)

    def test_health_is_read_only_and_parity_between_public_and_view(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage = _stage(root)
                    configured = (
                        stage.resource_identity.configured_jsonl_path
                    )
                    manifest = configured.with_name(
                        configured.name + ".localcat-snapshot.json"
                    )
                    store, _record_ids = self._store_with_records(
                        root,
                        fts5_available=fts5_available,
                    )
                    database_bytes = stage.staged_db_path.read_bytes()
                    public_health = store.health()
                    with store.query_lease() as view:
                        view_health = view.health()
                    self.assertEqual(public_health, view_health)
                    self.assertTrue(public_health.healthy)
                    self.assertFalse(public_health.exact_available)
                    self.assertFalse(public_health.context_available)
                    self.assertFalse(public_health.fuzzy_available)
                    self.assertEqual(
                        public_health.diagnostic_codes,
                        ("STORE.CANONICAL_NOT_ACTIVE",),
                    )
                    self.assertIsNone(public_health.source_binding_state)
                    self.assertIsNone(
                        public_health.snapshot_binding_digest
                    )
                    self.assertEqual(
                        public_health.schema_version,
                        TM_SCHEMA_VERSION,
                    )
                    self.assertEqual(public_health.generation, 0)
                    self.assertEqual(public_health.record_count, 3)
                    self.assertEqual(
                        public_health.index_kind,
                        (
                            "FTS5_TRIGRAM"
                            if fts5_available
                            else "GRAM_FALLBACK"
                        ),
                    )
                    self.assertEqual(
                        stage.staged_db_path.read_bytes(),
                        database_bytes,
                    )
                    self.assertFalse(configured.exists())
                    self.assertFalse(manifest.exists())

    def test_health_reports_truthful_source_binding_ledger_states(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            configured = stage.resource_identity.configured_jsonl_path
            manifest = configured.with_name(
                configured.name + ".localcat-snapshot.json"
            )
            store, _record_ids = self._store_with_records(
                root,
                fts5_available=False,
                sources=("first", "second"),
            )

            def ledger_binding(
                exported_revision: int,
                record_count: int,
            ) -> None:
                connection = sqlite3.connect(stage.staged_db_path)
                try:
                    connection.execute(
                        "INSERT INTO tm_snapshot_receipt("
                        "snapshot_id, resource_id, canonical_store_id, "
                        "exported_revision, jsonl_digest, record_count, "
                        "format_version, destination_jsonl_path, "
                        "destination_manifest_path, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)",
                        (
                            "snapshot.current",
                            "tm.primary",
                            "store.primary",
                            exported_revision,
                            "b" * 64,
                            record_count,
                            SNAPSHOT_FORMAT_VERSION,
                            str(configured),
                            str(manifest),
                            "2026-08-12T00:00:00+00:00",
                        ),
                    )
                    connection.execute(
                        "INSERT INTO tm_snapshot_binding("
                        "binding_id, configured_jsonl_path, manifest_path, "
                        "snapshot_kind, snapshot_id, binding_version) "
                        "VALUES (1, ?, ?, 'EXPLICIT_EXPORT', "
                        "'snapshot.current', ?)",
                        (
                            str(configured),
                            str(manifest),
                            SNAPSHOT_BINDING_VERSION,
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()

            def set_meta(key: str, value: str) -> None:
                connection = sqlite3.connect(stage.staged_db_path)
                try:
                    connection.execute(
                        "UPDATE tm_meta SET value = ? WHERE key = ?",
                        (value, key),
                    )
                    connection.commit()
                finally:
                    connection.close()

            def meta_value(key: str) -> str:
                connection = sqlite3.connect(stage.staged_db_path)
                try:
                    row = connection.execute(
                        "SELECT value FROM tm_meta WHERE key = ?",
                        (key,),
                    ).fetchone()
                    self.assertIsNotNone(row)
                    return str(row[0])
                finally:
                    connection.close()

            def expected_digest(
                exported_revision: int,
                record_count: int,
            ) -> str:
                receipt = SnapshotReceipt(
                    snapshot_id="snapshot.current",
                    resource_id="tm.primary",
                    canonical_store_id="store.primary",
                    exported_revision=exported_revision,
                    jsonl_digest="b" * 64,
                    record_count=record_count,
                    format_version=SNAPSHOT_FORMAT_VERSION,
                )
                binding = SnapshotBinding(
                    configured_jsonl_path=configured,
                    manifest_path=manifest,
                    snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                    receipt=receipt,
                    manifest=SnapshotManifest(
                        manifest_version=SNAPSHOT_MANIFEST_VERSION,
                        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
                        receipt=receipt,
                        receipt_digest=snapshot_receipt_digest(receipt),
                    ),
                    binding_version=SNAPSHOT_BINDING_VERSION,
                )
                return hashlib.sha256(
                    contract_to_json(binding).encode("utf-8")
                ).hexdigest()

            no_binding = store.health()
            self.assertIsNone(no_binding.source_binding_state)
            self.assertIsNone(no_binding.snapshot_binding_digest)
            self.assertEqual(
                no_binding.diagnostic_codes,
                ("STORE.CANONICAL_NOT_ACTIVE",),
            )

            ledger_binding(exported_revision=1, record_count=2)
            current = store.health()
            self.assertIs(
                current.source_binding_state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                current.snapshot_binding_digest,
                expected_digest(exported_revision=1, record_count=2),
            )
            self.assertEqual(
                current.diagnostic_codes,
                ("STORE.CANONICAL_NOT_ACTIVE",),
            )

            set_meta("head_revision", "9")
            drifted_ancestry = store.health()
            self.assertIs(
                drifted_ancestry.source_binding_state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(
                drifted_ancestry.diagnostic_codes,
                (
                    "SOURCE_BINDING.ANCESTRY_INVALID",
                    "STORE.CANONICAL_NOT_ACTIVE",
                ),
            )
            self.assertEqual(meta_value("divergence_latched"), "0")

            set_meta("head_revision", "1")
            set_meta("divergence_latched", "1")
            latched = store.health()
            self.assertIs(
                latched.source_binding_state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(
                latched.diagnostic_codes,
                (
                    "SOURCE_BINDING.DIVERGENCE_LATCHED",
                    "STORE.CANONICAL_NOT_ACTIVE",
                ),
            )
            self.assertEqual(
                latched.snapshot_binding_digest,
                expected_digest(exported_revision=1, record_count=2),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            configured = stage.resource_identity.configured_jsonl_path
            manifest = configured.with_name(
                configured.name + ".localcat-snapshot.json"
            )
            store, _record_ids = self._store_with_records(
                root,
                fts5_available=False,
                sources=("first",),
            )
            _ = store.append_batch(
                batch_id="import.history",
                kind="import",
                drafts=(_draft("second", "target-second"),),
                source_digest="d" * 64,
                source_path=(root / "history.jsonl").resolve(),
                extension=lambda values: build_candidate_write_plan(
                    values,
                    fts5_available=False,
                ),
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "INSERT INTO tm_snapshot_receipt("
                    "snapshot_id, resource_id, canonical_store_id, "
                    "exported_revision, jsonl_digest, record_count, "
                    "format_version, destination_jsonl_path, "
                    "destination_manifest_path, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)",
                    (
                        "snapshot.current",
                        "tm.primary",
                        "store.primary",
                        1,
                        "b" * 64,
                        1,
                        SNAPSHOT_FORMAT_VERSION,
                        str(configured),
                        str(manifest),
                        "2026-08-12T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    "INSERT INTO tm_snapshot_binding("
                    "binding_id, configured_jsonl_path, manifest_path, "
                    "snapshot_kind, snapshot_id, binding_version) "
                    "VALUES (1, ?, ?, 'EXPLICIT_EXPORT', "
                    "'snapshot.current', ?)",
                    (
                        str(configured),
                        str(manifest),
                        SNAPSHOT_BINDING_VERSION,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            history = store.health()
            self.assertIs(
                history.source_binding_state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(
                history.diagnostic_codes,
                ("STORE.CANONICAL_NOT_ACTIVE",),
            )

    def test_query_view_requires_a_live_store_registered_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _record_ids = self._store_with_records(
                Path(temporary),
                fts5_available=False,
            )
            lease = store.coordinator._view
            self.assertIsNotNone(lease)
            with self.assertRaisesRegex(
                TypeError,
                "query view is not directly constructible",
            ):
                _ = tm_sqlite_store.SQLiteTMQueryView(
                    store,
                    cast(Any, lease),
                    token=object(),
                )


if __name__ == "__main__":
    unittest.main()
