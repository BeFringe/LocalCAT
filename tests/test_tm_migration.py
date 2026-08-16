from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
import tm_sqlite_store
from tm_contracts import (
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    SnapshotKind,
    SnapshotManifest,
    contract_from_json,
)
from tm_migration import (
    MIGRATION_STREAM_CHUNK_SIZE,
    MigrationPreflightError,
    MigrationStageBuild,
    TMMigrationService,
)
from tm_sqlite_store import SQLiteTMStore, SQLiteStoreSchemaError


def _identity(root: Path) -> CanonicalResourceIdentity:
    return CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        (root / "tm.primary.jsonl").resolve(),
    )


def _service(identity: CanonicalResourceIdentity) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _write_active_sidecar(
    identity: CanonicalResourceIdentity,
    *,
    source_digest: str,
    status: str = "completed",
    canonical_store_id: str = "store.primary",
) -> None:
    connection = sqlite3.connect(identity.canonical_sidecar_path)
    try:
        connection.execute(
            "CREATE TABLE tm_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
            (
                ("activation_status", "ACTIVE"),
                ("canonical_store_id", canonical_store_id),
                ("resource_id", identity.resource_id),
                ("target_identity", identity.target_identity),
            ),
        )
        connection.execute(
            "CREATE TABLE tm_origin_batch("
            "batch_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "source_digest TEXT, status TEXT NOT NULL, "
            "completed_revision INTEGER)"
        )
        connection.execute(
            "INSERT INTO tm_origin_batch("
            "batch_id, kind, source_digest, status, completed_revision"
            ") VALUES ('migration.seed', 'migration', ?, ?, ?)",
            (source_digest, status, 1 if status == "completed" else None),
        )
        connection.commit()
    finally:
        connection.close()


class TMMigrationPreflightTests(unittest.TestCase):
    def test_preflight_streams_counts_digest_and_safe_diagnostics(self) -> None:
        source_bytes = (
            b'{"source":"alpha","target":"first"}\n'
            b'{not-json}\n'
            b'{"source":"alpha","target":"second"}\n'
            b'{"source":"beta","target":"target"}\n'
            b'{"source":"gamma","target":""}\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(source_bytes)

            with (
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("preflight must stream"),
                ),
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError("preflight must stream"),
                ),
            ):
                result = _service(identity).preflight(
                    identity.configured_jsonl_path
                )

            self.assertEqual(
                result.source_digest,
                hashlib.sha256(source_bytes).hexdigest(),
            )
            self.assertEqual(result.valid_count, 3)
            self.assertEqual(result.invalid_count, 2)
            self.assertEqual(result.duplicate_source_count, 1)
            self.assertEqual(result.variant_count, 1)
            self.assertEqual(
                tuple(
                    (
                        item.code,
                        item.stage,
                        item.line_number,
                        item.disposition,
                        item.safe_summary,
                    )
                    for item in result.diagnostics
                ),
                (
                    (
                        "ROW.INVALID_JSON",
                        "PREFLIGHT.PARSE",
                        2,
                        DiagnosticDisposition.REJECTED,
                        "ROW_SKIPPED_INVALID_JSON",
                    ),
                    (
                        "ROW.DUPLICATE_SOURCE",
                        "PREFLIGHT.VALIDATE",
                        3,
                        DiagnosticDisposition.WARNING,
                        "ROW_PRESERVED_AS_VARIANT",
                    ),
                    (
                        "ROW.INVALID_REQUIRED_FIELD",
                        "PREFLIGHT.VALIDATE",
                        5,
                        DiagnosticDisposition.REJECTED,
                        "ROW_SKIPPED_INVALID_REQUIRED_FIELD",
                    ),
                ),
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertEqual(
                tuple(path.name for path in root.iterdir()),
                (identity.configured_jsonl_path.name,),
            )

    def test_preflight_rejects_non_utf8_and_non_object_rows_safely(self) -> None:
        source_bytes = (
            b'[]\n'
            b'{"source":1,"target":"target"}\n'
            b'\xff\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)

            result = _service(identity).preflight(
                identity.configured_jsonl_path
            )

            self.assertEqual(result.valid_count, 0)
            self.assertEqual(result.invalid_count, 3)
            self.assertEqual(
                tuple(item.code for item in result.diagnostics),
                (
                    "ROW.INVALID_SHAPE",
                    "ROW.INVALID_REQUIRED_FIELD",
                    "ROW.INVALID_UTF8",
                ),
            )
            serialized = "|".join(
                f"{item.code}:{item.safe_summary}"
                for item in result.diagnostics
            )
            self.assertNotIn("target", serialized)
            self.assertNotIn("source", serialized.lower())

    def test_preflight_validates_identity_and_permissions_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_text(
                '{"source":"a","target":"b"}\n',
                encoding="utf-8",
            )
            other_source = (root / "other.jsonl").resolve()
            other_source.write_text(
                '{"source":"x","target":"y"}\n',
                encoding="utf-8",
            )
            service = _service(identity)

            with (
                patch.object(
                    Path,
                    "open",
                    side_effect=AssertionError("identity must fail first"),
                ),
                self.assertRaisesRegex(
                    MigrationPreflightError,
                    "MIGRATION.RESOURCE_IDENTITY_MISMATCH",
                ),
            ):
                _ = service.preflight(other_source)

            original_mode = root.stat().st_mode
            os.chmod(root, 0o500)
            try:
                with self.assertRaisesRegex(
                    MigrationPreflightError,
                    "MIGRATION.TARGET_NOT_WRITABLE",
                ):
                    _ = service.preflight(identity.configured_jsonl_path)
            finally:
                os.chmod(root, original_mode & 0o777)

    def test_preflight_requires_exact_native_values_before_side_effects(self) -> None:
        class PathSubclass(type(Path())):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_text(
                '{"source":"a","target":"b"}\n',
                encoding="utf-8",
            )
            deceptive = PathSubclass(identity.configured_jsonl_path)

            with self.assertRaisesRegex(TypeError, "exact native Path"):
                _ = _service(identity).preflight(cast(Path, deceptive))

    def test_preflight_fails_closed_on_every_sidecar_claim(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        for case, expected in (
            ("same", "MIGRATION.SIDECAR_NOT_REUSABLE"),
            ("different", "MIGRATION.SIDECAR_DIFFERENT_SOURCE"),
            ("failed", "MIGRATION.SIDECAR_NOT_REUSABLE"),
            ("wrong-identity", "MIGRATION.SIDECAR_IDENTITY_MISMATCH"),
        ):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    _write_active_sidecar(
                        identity,
                        source_digest=(
                            source_digest if case != "different" else "f" * 64
                        ),
                        status="failed" if case == "failed" else "completed",
                        canonical_store_id=(
                            "store.other"
                            if case == "wrong-identity"
                            else "store.primary"
                        ),
                    )

                    with self.assertRaisesRegex(
                        MigrationPreflightError,
                        expected,
                    ):
                        _ = _service(identity).preflight(
                            identity.configured_jsonl_path
                        )

    def test_preflight_rejects_empty_source_and_manifest_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(b"")
            with self.assertRaisesRegex(
                MigrationPreflightError,
                "MIGRATION.SOURCE_EMPTY",
            ):
                _ = _service(identity).preflight(
                    identity.configured_jsonl_path
                )

            identity.configured_jsonl_path.write_text(
                '{"source":"a","target":"b"}\n',
                encoding="utf-8",
            )
            identity.snapshot_manifest_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                MigrationPreflightError,
                "MIGRATION.MANIFEST_WITHOUT_SIDECAR",
            ):
                _ = _service(identity).preflight(
                    identity.configured_jsonl_path
                )


class TMMigrationStageBuildTests(unittest.TestCase):
    def test_build_writes_complete_ordered_stage_receipt_and_manifest(self) -> None:
        source_bytes = (
            b'{"source":"same","target":"first","speaker":"alice",'
            b'"context_prev":"before","file_source":"chapter.json"}\n'
            b'{bad-json}\n'
            b'{"source":"same","target":"second","context_next":"after"}\n'
            b'{"source":"x","target":"short"}\n'
        )
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        result = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )

                    self.assertIsInstance(result, MigrationStageBuild)
                    self.assertIsNone(result.reused_completed_revision)
                    stage = result.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    self.assertEqual(
                        stage.staged_db_path.parent,
                        identity.canonical_sidecar_path.parent,
                    )
                    self.assertEqual(
                        stage.manifest_temp_path.parent,
                        identity.snapshot_manifest_path.parent,
                    )
                    self.assertTrue(stage.staged_db_path.is_file())
                    self.assertTrue(stage.manifest_temp_path.is_file())
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(identity.snapshot_manifest_path.exists())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        source_bytes,
                    )

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        store = SQLiteTMStore(
                            stage,
                            canonical_store_id="store.primary",
                        )
                    records = tuple(store.export_records())
                    self.assertEqual(
                        tuple(
                            (
                                record.source_raw,
                                record.target_raw,
                                record.speaker_raw,
                                record.context_prev_raw,
                                record.context_next_raw,
                                record.file_source,
                                record.legacy_line_no,
                                record.origin_ordinal,
                            )
                            for record in records
                        ),
                        (
                            (
                                "same",
                                "first",
                                "alice",
                                "before",
                                None,
                                "chapter.json",
                                1,
                                0,
                            ),
                            (
                                "same",
                                "second",
                                None,
                                None,
                                "after",
                                None,
                                3,
                                1,
                            ),
                            ("x", "short", None, None, None, None, 4, 2),
                        ),
                    )
                    self.assertEqual(
                        tuple(record.provenance for record in records),
                        ((("source", "legacy-jsonl"),),) * 3,
                    )
                    self.assertEqual(store.exact_records("same")[0], records[1])

                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        batch = connection.execute(
                            "SELECT kind, source_digest, source_path, status, "
                            "valid_count, invalid_count, "
                            "duplicate_source_count, completed_revision "
                            "FROM tm_origin_batch"
                        ).fetchone()
                        receipt = connection.execute(
                            "SELECT resource_id, canonical_store_id, "
                            "exported_revision, jsonl_digest, record_count, "
                            "destination_jsonl_path, "
                            "destination_manifest_path, status "
                            "FROM tm_snapshot_receipt"
                        ).fetchone()
                        gram_sizes = {
                            int(row[0])
                            for row in connection.execute(
                                "SELECT DISTINCT gram_size FROM tm_gram"
                            ).fetchall()
                        }
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
                        batch,
                        (
                            "migration",
                            hashlib.sha256(source_bytes).hexdigest(),
                            str(identity.configured_jsonl_path),
                            "completed",
                            3,
                            1,
                            1,
                            1,
                        ),
                    )
                    self.assertEqual(
                        receipt,
                        (
                            identity.resource_id,
                            "store.primary",
                            1,
                            hashlib.sha256(source_bytes).hexdigest(),
                            3,
                            str(identity.configured_jsonl_path),
                            str(identity.snapshot_manifest_path),
                            "issued",
                        ),
                    )
                    self.assertEqual(
                        gram_sizes,
                        {1, 2} if fts5_available else {1, 2, 3},
                    )
                    self.assertEqual(fts_count, 3 if fts5_available else 0)

                    decoded = contract_from_json(
                        stage.manifest_temp_path.read_text(encoding="utf-8")
                    )
                    self.assertIsInstance(decoded, SnapshotManifest)
                    assert isinstance(decoded, SnapshotManifest)
                    self.assertIs(decoded.snapshot_kind, SnapshotKind.MIGRATION_SOURCE)
                    self.assertEqual(decoded.receipt.record_count, 3)
                    self.assertEqual(
                        decoded.receipt.jsonl_digest,
                        hashlib.sha256(source_bytes).hexdigest(),
                    )

    def test_same_digest_reuses_complete_mutable_stage_without_duplicates(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            service = _service(identity)

            first = service.build_mutable_stage(identity.configured_jsonl_path)
            second = service.build_mutable_stage(identity.configured_jsonl_path)

            self.assertEqual(second, first)
            stage = first.mutable_stage
            assert stage is not None
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                counts = (
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_origin_batch"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_record"
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_receipt"
                    ).fetchone()[0],
                )
            finally:
                connection.close()
            self.assertEqual(counts, (1, 1, 1))

    def test_fresh_build_skips_reuse_scan_but_existing_stage_revalidates(
        self,
    ) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            service = _service(identity)
            real_validate = tm_migration._validate_reusable_stage

            with patch(
                "tm_migration._validate_reusable_stage",
                wraps=real_validate,
            ) as validate:
                first = service.build_mutable_stage(
                    identity.configured_jsonl_path
                )
                self.assertEqual(validate.call_count, 0)
                second = service.build_mutable_stage(
                    identity.configured_jsonl_path
                )

            self.assertEqual(second, first)
            self.assertEqual(validate.call_count, 1)

    def test_naked_sidecar_is_not_authority_and_never_reports_reuse(
        self,
    ) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            _write_active_sidecar(
                identity,
                source_digest=hashlib.sha256(source_bytes).hexdigest(),
            )

            with self.assertRaisesRegex(
                MigrationPreflightError,
                "MIGRATION.SIDECAR_NOT_REUSABLE",
            ):
                _ = _service(identity).build_mutable_stage(
                    identity.configured_jsonl_path
                )
            self.assertEqual(
                tuple(
                    path.name
                    for path in Path(temporary).iterdir()
                    if path.resolve()
                    != identity.configured_jsonl_path.resolve()
                ),
                (identity.canonical_sidecar_path.name,),
            )

    def test_symlinked_foreign_sidecar_is_rejected_before_reuse(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(source_bytes)
            foreign = root / "foreign.jsonl"
            foreign.write_bytes(b'{"source":"x","target":"y"}\n')
            identity.canonical_sidecar_path.symlink_to(foreign)

            with self.assertRaisesRegex(
                MigrationPreflightError,
                "MIGRATION.SIDECAR_INVALID",
            ):
                _ = _service(identity).preflight(
                    identity.configured_jsonl_path
                )

    def test_incomplete_sidecar_schema_is_rejected_as_invalid(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                connection.execute(
                    "CREATE TABLE tm_meta("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
                    (
                        ("activation_status", "ACTIVE"),
                        ("canonical_store_id", "store.primary"),
                        ("resource_id", identity.resource_id),
                        ("target_identity", identity.target_identity),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                MigrationPreflightError,
                "MIGRATION.SIDECAR_INVALID",
            ):
                _ = _service(identity).preflight(
                    identity.configured_jsonl_path
                )

    def test_digest_change_or_write_failure_leaves_no_stage_artifacts(self) -> None:
        original = b'{"source":"a","target":"b"}\n'
        changed = b'{"source":"a","target":"changed"}\n'
        for failure_kind in ("digest-change", "append-failure"):
            with self.subTest(failure_kind=failure_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(original)
                    service = _service(identity)

                    if failure_kind == "digest-change":
                        preflight = service.preflight(
                            identity.configured_jsonl_path
                        )

                        def change_after_preflight(_source: Path):
                            identity.configured_jsonl_path.write_bytes(changed)
                            return preflight

                        context = patch.object(
                            service,
                            "preflight",
                            side_effect=change_after_preflight,
                        )
                        expected = "MIGRATION.SOURCE_CHANGED"
                    else:
                        context = patch.object(
                            SQLiteTMStore,
                            "append_streamed_batch",
                            side_effect=RuntimeError("injected failure"),
                        )
                        expected = "injected failure"
                    with context:
                        with self.assertRaisesRegex(Exception, expected):
                            _ = service.build_mutable_stage(
                                identity.configured_jsonl_path
                            )

                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(identity.snapshot_manifest_path.exists())
                    self.assertEqual(
                        tuple(
                            path.name
                            for path in Path(temporary).iterdir()
                            if path.resolve()
                            != identity.configured_jsonl_path.resolve()
                        ),
                        (),
                    )

    def test_issued_receipt_failure_rolls_back_all_stage_files(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            with patch.object(
                SQLiteTMStore,
                "register_issued_snapshot_receipt",
                side_effect=RuntimeError("injected receipt failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "receipt failure"):
                    _ = _service(identity).build_mutable_stage(
                        identity.configured_jsonl_path
                    )
            self.assertEqual(
                tuple(path.name for path in Path(temporary).iterdir()),
                (identity.configured_jsonl_path.name,),
            )


    def test_reuse_rejects_missing_or_wrong_temporary_manifest(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        for tamper in ("missing", "wrong-content"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)
                    first = service.build_mutable_stage(
                        identity.configured_jsonl_path
                    )
                    stage = first.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    if tamper == "missing":
                        stage.manifest_temp_path.unlink()
                    else:
                        stage.manifest_temp_path.write_text(
                            "{}",
                            encoding="utf-8",
                        )
                    with self.assertRaisesRegex(
                        MigrationPreflightError,
                        "MIGRATION.STAGE_CONFLICT",
                    ):
                        _ = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )

    def test_reuse_rejects_tampered_batch_receipt_or_binding(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        for tamper in ("batch", "receipt", "binding"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)
                    first = service.build_mutable_stage(
                        identity.configured_jsonl_path
                    )
                    stage = first.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        if tamper == "batch":
                            connection.execute(
                                "UPDATE tm_origin_batch SET source_digest = ?",
                                ("0" * 64,),
                            )
                        elif tamper == "receipt":
                            connection.execute(
                                "UPDATE tm_snapshot_receipt "
                                "SET jsonl_digest = ?",
                                ("0" * 64,),
                            )
                        else:
                            connection.execute(
                                "INSERT INTO tm_snapshot_binding("
                                "binding_id, configured_jsonl_path, "
                                "manifest_path, snapshot_kind, snapshot_id, "
                                "binding_version) VALUES (1, 'configured', "
                                "'manifest', 'MIGRATION_SOURCE', "
                                "'snapshot.migration.0', 'binding-v1')"
                            )
                        connection.commit()
                    finally:
                        connection.close()
                    with self.assertRaisesRegex(
                        MigrationPreflightError,
                        "MIGRATION.STAGE_CONFLICT",
                    ):
                        _ = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )

    def test_reuse_rejects_incomplete_candidate_index(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        for fts5_available, tamper, statement in (
            (
                False,
                "gram",
                "DELETE FROM tm_gram WHERE gram_size = 1 AND gram = 'a'",
            ),
            (True, "fts", "DELETE FROM tm_fts"),
            (
                False,
                "length",
                "UPDATE tm_record SET source_fold_length = 2",
            ),
            (
                True,
                "term-frequency",
                "UPDATE tm_gram SET term_frequency = 2",
            ),
            (
                False,
                "block",
                "UPDATE tm_candidate_block SET record_count = 2",
            ),
            (
                True,
                "block-maximum",
                "UPDATE tm_gram_block_max SET max_term_frequency = 2",
            ),
        ):
            with self.subTest(
                fts5_available=fts5_available,
                tamper=tamper,
            ):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        first = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )
                    stage = first.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()
                    with (
                        patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=fts5_available,
                        ),
                        self.assertRaisesRegex(
                            MigrationPreflightError,
                            "MIGRATION.STAGE_CONFLICT",
                        ),
                    ):
                        _ = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )

    def test_reuse_rejects_schema_or_runtime_tamper(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        for tamper in ("schema", "runtime"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        first = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )
                    stage = first.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    if tamper == "schema":
                        connection = sqlite3.connect(stage.staged_db_path)
                        try:
                            connection.execute("DROP INDEX idx_tm_exact")
                            connection.commit()
                        finally:
                            connection.close()
                        with self.assertRaises(
                            SQLiteStoreSchemaError
                        ):
                            _ = service.build_mutable_stage(
                                identity.configured_jsonl_path
                            )
                    else:
                        with self.assertRaises(
                            SQLiteStoreSchemaError
                        ):
                            with patch(
                                "tm_sqlite_store._probe_fts5",
                                return_value=True,
                            ):
                                _ = service.build_mutable_stage(
                                    identity.configured_jsonl_path
                                )

    def test_reuse_rejects_symlinked_stage_files(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        for target in ("db", "manifest"):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    identity = _identity(Path(temporary))
                    identity.configured_jsonl_path.write_bytes(source_bytes)
                    service = _service(identity)
                    first = service.build_mutable_stage(
                        identity.configured_jsonl_path
                    )
                    stage = first.mutable_stage
                    self.assertIsNotNone(stage)
                    assert stage is not None
                    if target == "db":
                        stage.staged_db_path.unlink()
                        stage.staged_db_path.symlink_to(
                            identity.configured_jsonl_path
                        )
                    else:
                        stage.manifest_temp_path.unlink()
                        stage.manifest_temp_path.symlink_to(
                            identity.configured_jsonl_path
                        )
                    with self.assertRaisesRegex(
                        MigrationPreflightError,
                        "MIGRATION.STAGE_CONFLICT",
                    ):
                        _ = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )

    def test_build_streams_bounded_batches_and_preserves_order(self) -> None:
        source_lines = [
            {
                "source": f"source-{index:05d}-" + "x" * (index % 40),
                "target": f"target-{index:05d}",
            }
            for index in range(1, 12_501)
        ]
        source_bytes = "".join(
            json.dumps(line, ensure_ascii=False) + "\n"
            for line in source_lines
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            observed_sizes: list[int] = []
            observed_calls = 0
            original_prepare = tm_sqlite_store._prepare_record_drafts

            def recording_prepare(
                drafts: tuple[Any, ...],
                legacy_line_nos: tuple[Any, ...],
                **kwargs: int,
            ):
                nonlocal observed_calls
                observed_calls += 1
                observed_sizes.append(len(drafts))
                return original_prepare(
                    drafts,
                    legacy_line_nos,
                    **kwargs,
                )

            with (
                patch(
                    "tm_sqlite_store._prepare_record_drafts",
                    side_effect=recording_prepare,
                ),
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
            ):
                result = _service(identity).build_mutable_stage(
                    identity.configured_jsonl_path
                )
                stage = result.mutable_stage
                self.assertIsNotNone(stage)
                assert stage is not None
                expected_calls = (
                    len(source_lines) + MIGRATION_STREAM_CHUNK_SIZE - 1
                ) // MIGRATION_STREAM_CHUNK_SIZE
                self.assertEqual(observed_calls, expected_calls)
                self.assertLessEqual(
                    max(observed_sizes),
                    MIGRATION_STREAM_CHUNK_SIZE,
                )
                store = SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )
                records = tuple(store.export_records())
            self.assertEqual(len(records), 12_500)
            self.assertEqual(
                tuple(record.source_raw for record in records),
                tuple(line["source"] for line in source_lines),
            )
            self.assertEqual(
                tuple(record.legacy_line_no for record in records),
                tuple(range(1, 12_501)),
            )
            self.assertEqual(
                tuple(record.origin_ordinal for record in records),
                tuple(range(0, 12_500)),
            )
            self.assertEqual(
                tuple(record.record_id for record in records),
                tuple(range(1, 12_501)),
            )

    def test_stream_failure_mid_build_discards_partial_stage(self) -> None:
        source_bytes = "".join(
            json.dumps({"source": f"s{i}", "target": f"t{i}"}) + "\n"
            for i in range(600)
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            service = _service(identity)
            original_append = SQLiteTMStore.append_streamed_batch

            def failing_append(
                store: SQLiteTMStore,
                *,
                drafts: Iterator[tuple[Any, int | None]],
                **kwargs: Any,
            ):
                consumed = 0

                def broken() -> Iterator[tuple[Any, int | None]]:
                    nonlocal consumed
                    for pair in drafts:
                        consumed += 1
                        if consumed == 550:
                            raise RuntimeError(
                                "injected mid-stream failure"
                            )
                        yield pair

                return original_append(
                    store,
                    drafts=broken(),
                    **kwargs,
                )

            with patch.object(
                SQLiteTMStore,
                "append_streamed_batch",
                new=failing_append,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected mid-stream failure",
                ):
                    _ = service.build_mutable_stage(
                        identity.configured_jsonl_path
                    )
            self.assertEqual(
                tuple(
                    path.name
                    for path in Path(temporary).iterdir()
                    if path.resolve()
                    != identity.configured_jsonl_path.resolve()
                ),
                (),
            )


def _write_envelope_corpus(
    path: Path,
    record_count: int,
) -> None:
    """Write a deterministic 100k-record fallback corpus with mixed rows."""

    rng = random.Random(0x5EED_1001)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for index in range(record_count):
            source = "".join(
                rng.choice("abcdefghijklmnopqrstuvwxyz ")
                for _ in range(rng.randint(12, 64))
            )
            target = "".join(
                rng.choice("abcdefghijklmnopqrstuvwxyz ")
                for _ in range(rng.randint(12, 64))
            )
            if index != 0 and index % 997 == 0:
                stream.write("{not-json}\n")
            elif index % 991 == 0:
                stream.write(
                    json.dumps(
                        {
                            "source": "envelope-shared-source",
                            "target": target,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            else:
                stream.write(
                    json.dumps(
                        {"source": source, "target": target},
                        ensure_ascii=False,
                    )
                    + "\n"
                )


def _run_envelope_build(corpus: Path) -> dict[str, int]:
    """Build one 100k fallback stage and return measured facts."""

    import resource

    identity = CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        corpus.resolve(),
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        result = service.build_mutable_stage(corpus.resolve())
    stage = result.mutable_stage
    if stage is None:
        raise AssertionError("envelope build returned no mutable stage")
    connection = sqlite3.connect(stage.staged_db_path)
    try:
        record_bounds = connection.execute(
            "SELECT COUNT(*), MIN(record_id), MAX(record_id) "
            "FROM tm_record"
        ).fetchone()
        gram_sizes = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT gram_size FROM tm_gram"
            ).fetchall()
        }
    finally:
        connection.close()
    if record_bounds != (
        result.preflight.valid_count,
        1,
        result.preflight.valid_count,
    ):
        raise AssertionError("envelope record identity does not close")
    if gram_sizes != {1, 2, 3}:
        raise AssertionError("envelope fallback gram index is incomplete")
    rss_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "rss_kib": rss_kib,
        "record_count": int(result.preflight.valid_count),
        "valid_count": int(result.preflight.valid_count),
        "invalid_count": int(result.preflight.invalid_count),
        "duplicate_source_count": int(
            result.preflight.duplicate_source_count
        ),
    }


@unittest.skipUnless(
    os.environ.get("TMMIGRATION_RESOURCE_ENVELOPE") == "1",
    "set TMMIGRATION_RESOURCE_ENVELOPE=1 to run the 100k envelope test",
)
class TMMigrationResourceEnvelopeTests(unittest.TestCase):
    def test_100k_fallback_build_stays_below_512MiB_in_subprocess(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary) / "envelope-100k.jsonl"
            _write_envelope_corpus(corpus, 100_000)
            environment = dict(os.environ)
            environment["TMMIGRATION_ENVELOPE_BUILD"] = "1"
            environment["TMMIGRATION_ENVELOPE_CORPUS"] = str(corpus)
            repository_root = Path(__file__).resolve().parent.parent
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.test_tm_migration",
                ],
                cwd=repository_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            output = completed.stdout
            facts = {}
            for line in output.splitlines():
                if line.startswith("ENVELOPE_"):
                    key, value = line.split("=", 1)
                    facts[key] = int(value)
            self.assertEqual(
                completed.returncode,
                0,
                msg=output + completed.stderr,
            )
            self.assertEqual(facts.get("ENVELOPE_RECORD_COUNT"), 99_900)
            self.assertEqual(facts.get("ENVELOPE_INVALID_COUNT"), 100)
            self.assertEqual(facts.get("ENVELOPE_DUPLICATE_COUNT"), 1)
            self.assertLessEqual(
                facts.get("ENVELOPE_RSS_KIB", 0),
                524288,
                msg=output + completed.stderr,
            )
            self.assertLessEqual(
                facts.get("ENVELOPE_ELAPSED_MS", 0),
                120_000,
                msg=output + completed.stderr,
            )


def _main_envelope_build() -> None:
    corpus_env = os.environ.get("TMMIGRATION_ENVELOPE_CORPUS")
    if corpus_env:
        corpus = Path(corpus_env)
        if not corpus.is_file():
            raise SystemExit(f"missing envelope corpus: {corpus}")
    else:
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary) / "envelope-100k.jsonl"
            _write_envelope_corpus(corpus, 100_000)
            _report_envelope(corpus)
        return
    _report_envelope(corpus)


def _report_envelope(corpus: Path) -> None:
    started = time.monotonic()
    facts = _run_envelope_build(corpus)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"ENVELOPE_ELAPSED_MS={elapsed_ms}")
    print(f"ENVELOPE_RSS_KIB={facts['rss_kib']}")
    print(f"ENVELOPE_RECORD_COUNT={facts['record_count']}")
    print(f"ENVELOPE_VALID_COUNT={facts['valid_count']}")
    print(f"ENVELOPE_INVALID_COUNT={facts['invalid_count']}")
    print(f"ENVELOPE_DUPLICATE_COUNT={facts['duplicate_source_count']}")



if __name__ == "__main__":
    if os.environ.get("TMMIGRATION_ENVELOPE_BUILD") == "1":
        _main_envelope_build()
        raise SystemExit(0)
    unittest.main()
