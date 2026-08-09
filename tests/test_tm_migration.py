from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

import tm_sqlite_store
from tm_contracts import (
    CanonicalResourceIdentity,
    DiagnosticDisposition,
    SnapshotKind,
    SnapshotManifest,
    contract_from_json,
)
from tm_migration import (
    MigrationPreflightError,
    MigrationStageBuild,
    TMMigrationService,
)
from tm_sqlite_store import SQLiteTMStore


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

    def test_preflight_recognizes_only_same_digest_completed_migration(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        for case in ("same", "different", "failed", "wrong-identity"):
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

                    if case == "same":
                        result = _service(identity).preflight(
                            identity.configured_jsonl_path
                        )
                        self.assertEqual(result.source_digest, source_digest)
                    else:
                        expected = {
                            "different": "MIGRATION.SIDECAR_DIFFERENT_SOURCE",
                            "failed": "MIGRATION.SIDECAR_NOT_REUSABLE",
                            "wrong-identity": "MIGRATION.SIDECAR_IDENTITY_MISMATCH",
                        }[case]
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

    def test_same_digest_active_sidecar_returns_existing_revision(self) -> None:
        source_bytes = b'{"source":"a","target":"b"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            identity.configured_jsonl_path.write_bytes(source_bytes)
            _write_active_sidecar(
                identity,
                source_digest=hashlib.sha256(source_bytes).hexdigest(),
            )

            result = _service(identity).build_mutable_stage(
                identity.configured_jsonl_path
            )

            self.assertIsNone(result.mutable_stage)
            self.assertEqual(result.reused_completed_revision, 1)

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
                            "append_batch",
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
                            if path != identity.configured_jsonl_path
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


if __name__ == "__main__":
    unittest.main()
