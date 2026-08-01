from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import inspect
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    TMRecord,
    TMRecordDraft,
)
from tm_sqlite_store import (
    CANDIDATE_INDEX_VERSION,
    FOLD_VERSION_V1,
    TM_SCHEMA_VERSION,
    SQLiteCandidateRecord,
    SQLiteCandidateWritePlan,
    SQLiteGramRow,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    _schema_digest,
    _open_configured_connection,
    initialize_stage_schema,
    inspect_stage_schema,
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
                    "valid_count, invalid_count, duplicate_source_count "
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
            self.assertEqual(gram_rows, [(2, "ab", records[0].record_id)])

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
                            "tm_sqlite_store.fold_text_v1",
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
                    "tm_gram",
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


if __name__ == "__main__":
    unittest.main()
