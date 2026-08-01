from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tm_contracts import CanonicalResourceIdentity, MutableStageRef
from tm_sqlite_store import (
    CANDIDATE_INDEX_VERSION,
    FOLD_VERSION_V1,
    TM_SCHEMA_VERSION,
    SQLiteStoreSchemaError,
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
