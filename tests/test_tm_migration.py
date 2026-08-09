from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

from tm_contracts import (
    CanonicalResourceIdentity,
    DiagnosticDisposition,
)
from tm_migration import MigrationPreflightError, TMMigrationService


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


if __name__ == "__main__":
    unittest.main()
