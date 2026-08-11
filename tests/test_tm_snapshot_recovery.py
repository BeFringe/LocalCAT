"""Task 5.14 configured snapshot refresh crash recovery tests.

``recover_configured_refresh`` closes every Task 5.13 issued-receipt
crash window durably and idempotently: an old completed pair cancels
the issued receipt, an issued JSONL with an old or absent manifest
reconstructs the exact ledger manifest and completes, a full issued
pair completes and rebinds without republishing, and every unsafe,
foreign, ambiguous or invalid observation durably latches
``SOURCE_DIVERGED`` without touching foreign paths or canonical
authority.  Task 5.12 arbitrary-destination export receipts reuse the
same protocol without ever touching the active binding or divergence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

import tm_migration
import tm_snapshot_recovery
import tm_sqlite_store
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    CanonicalResourceIdentity,
    ExportReport,
    ExportFailure,
    MutableStageRef,
    SourceBindingState,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_migration import (
    ExportPreflightError,
    TMMigrationService,
    _export_artifact_paths,
)
from tm_snapshot_recovery import (
    IssuedReceiptRecovery,
    RecoveryError,
    RefreshRecoveryOutcome,
    RefreshRecoveryState,
)
from tm_sqlite_store import (
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    initialize_stage_schema,
)


_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")


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
        manifest_temp_path=(root / f".{resource_id}.snapshot.tmp").resolve(),
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


def _service(identity: Any) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _prepared_store(
    root: Path,
) -> tuple[Any, SQLiteTMStore]:
    """One live canonical store seeded with every supported draft variant."""

    stage = _stage(root)
    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        store = SQLiteTMStore(stage, canonical_store_id="store.primary")
    _ = store.append_batch(
        batch_id="migration.seed.recovery",
        kind="migration",
        drafts=(
            _draft("same", "first", speaker="alice"),
            _draft("same", "second", speaker="alice"),
            _draft("Straße", "übersetzt"),
            _draft("minimal", "target only", provenance=()),
            _draft("context", "both", previous="p", following="f"),
            _draft("file", "src", file_source="book.txt"),
        ),
        source_digest="c" * 64,
        source_path=(root / "seed-source.jsonl").resolve(),
        legacy_line_nos=(1, 2, 3, 4, 5, 6),
    )
    return stage, store


def _fresh_store(stage: Any) -> SQLiteTMStore:
    """A second store instance over the same stage (process restart)."""

    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        return SQLiteTMStore(stage, canonical_store_id="store.primary")


def _bind_current_snapshot(
    store: SQLiteTMStore,
    stage: Any,
    jsonl_bytes: bytes,
) -> SnapshotBinding:
    """Publish and register one completed binding for the current revision."""

    revision = store.canonical_revision()
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.{stage.resource_identity.resource_id}.bound",
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(jsonl_bytes).hexdigest(),
        record_count=revision.record_count,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    binding = SnapshotBinding(
        configured_jsonl_path=stage.resource_identity.configured_jsonl_path,
        manifest_path=stage.resource_identity.snapshot_manifest_path,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        manifest=manifest,
    )
    binding.configured_jsonl_path.write_bytes(jsonl_bytes)
    binding.manifest_path.write_text(
        contract_to_json(manifest),
        encoding="utf-8",
    )
    store.register_completed_snapshot_binding(binding)
    return binding


_PRIOR_JSONL = b'{"source":"bound","target":"pair"}\n'
_NEW_JSONL = b'{"source":"new","target":"content"}\n'


def _manifest_bytes_for(receipt: SnapshotReceipt) -> bytes:
    """Deterministic adjacent manifest bytes for one issued receipt."""

    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return contract_to_json(manifest).encode("utf-8")


def _paths(identity: Any) -> Any:
    return _export_artifact_paths(identity.configured_jsonl_path)


def _pair(identity: Any) -> tuple[bytes, bytes]:
    return (
        identity.configured_jsonl_path.read_bytes(),
        identity.snapshot_manifest_path.read_bytes(),
    )


def _current_receipt(
    store: SQLiteTMStore,
    *,
    prefix: str,
    payload: bytes,
) -> SnapshotReceipt:
    """One ledger-valid receipt describing the current canonical revision."""

    revision = store.capture_export_snapshot().revision
    return SnapshotReceipt(
        snapshot_id=f"{prefix}{hashlib.sha256(payload).hexdigest()[:12]}",
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(payload).hexdigest(),
        record_count=revision.record_count,
    )


def _register_refresh(
    store: SQLiteTMStore,
    receipt: SnapshotReceipt,
) -> None:
    revision = store.capture_export_snapshot().revision
    store.register_issued_refresh_receipt(
        receipt,
        expected_generation=revision.generation,
    )


def _write_new_pair(identity: Any, receipt: SnapshotReceipt, payload: bytes) -> None:
    identity.configured_jsonl_path.write_bytes(payload)
    identity.snapshot_manifest_path.write_bytes(
        _manifest_bytes_for(receipt)
    )


def _ledger_rows(
    store_path: Path,
    configured_jsonl: Path,
) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(store_path)
    try:
        rows = connection.execute(
            "SELECT snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, "
            "format_version, destination_jsonl_path, "
            "destination_manifest_path, status "
            "FROM tm_snapshot_receipt "
            "WHERE destination_jsonl_path = ?",
            (str(configured_jsonl),),
        ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


def _refresh_rows(
    store_path: Path,
    configured_jsonl: Path,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        row
        for row in _ledger_rows(store_path, configured_jsonl)
        if str(row[0]).startswith("snapshot.refresh.")
    )


def _issued_refresh_snapshot_id(store_path: Path) -> str:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT snapshot_id FROM tm_snapshot_receipt "
            "WHERE snapshot_id LIKE 'snapshot.refresh.%' "
            "AND status = 'issued' ORDER BY snapshot_id"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("issued refresh receipt row is missing")
    return str(row[0])


def _status_for(store_path: Path, snapshot_id: str) -> str | None:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT status FROM tm_snapshot_receipt "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _binding_row(store_path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT binding_id, configured_jsonl_path, manifest_path, "
            "snapshot_kind, snapshot_id, binding_version "
            "FROM tm_snapshot_binding WHERE binding_id = 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("snapshot binding row is missing")
    return row


def _record_dump(store_path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(store_path)
    try:
        rows = connection.execute(
            "SELECT record_id, source_raw, target_raw, speaker_raw, "
            "context_prev_raw, context_next_raw, file_source, "
            "provenance_json, legacy_line_no, usage_count, last_used, "
            "origin_batch_id, origin_ordinal "
            "FROM tm_record ORDER BY record_id ASC"
        ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


def _meta_value(store_path: Path, key: str) -> str | None:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT value FROM tm_meta WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _insert_ledger_row(
    store_path: Path,
    receipt: SnapshotReceipt,
    *,
    destination_jsonl_path: Path,
    destination_manifest_path: Path,
    status: str,
    resource_id: str | None = None,
    canonical_store_id: str | None = None,
) -> None:
    """Insert one receipt row directly, bypassing the store validation seams."""

    connection = sqlite3.connect(store_path)
    try:
        connection.execute(
            "INSERT INTO tm_snapshot_receipt("
            "snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, "
            "format_version, destination_jsonl_path, "
            "destination_manifest_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.snapshot_id,
                (
                    resource_id
                    if resource_id is not None
                    else receipt.resource_id
                ),
                (
                    canonical_store_id
                    if canonical_store_id is not None
                    else receipt.canonical_store_id
                ),
                receipt.exported_revision,
                receipt.jsonl_digest,
                receipt.record_count,
                receipt.format_version,
                str(destination_jsonl_path),
                str(destination_manifest_path),
                status,
                "2026-08-11T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _simulate_crash(
    service: TMMigrationService,
    store: SQLiteTMStore,
    *,
    crash_at: str,
) -> None:
    """Run one refresh that aborts at the exact crash boundary.

    A ``KeyboardInterrupt`` (BaseException) propagates past the export
    failure handling exactly like process death, leaving the issued
    ledger row and the intermediate filesystem state in place.
    """

    identity = service._resource_identity
    paths = _paths(identity)
    original_replace = tm_migration._replace_path

    def crashing_replace(
        source: Path,
        target: Path,
        **kwargs: Any,
    ) -> None:
        if crash_at == "before-jsonl" and source == paths.jsonl_temp:
            raise KeyboardInterrupt(
                "injected crash before first publication replace"
            )
        if crash_at == "jsonl" and source == paths.jsonl_temp:
            original_replace(source, target, **kwargs)
            raise KeyboardInterrupt("injected crash after jsonl replace")
        if crash_at == "manifest" and source == paths.manifest_temp:
            original_replace(source, target, **kwargs)
            raise KeyboardInterrupt("injected crash after manifest replace")
        original_replace(source, target, **kwargs)

    original_copy = tm_migration._copy_export_prior_pair

    def copying_then_raise(
        paths: Any,
        **kwargs: Any,
    ) -> Any:
        original_copy(paths, **kwargs)
        raise KeyboardInterrupt(
            "injected crash after recovery copies, before handoff"
        )

    if crash_at == "issued":
        inject = patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=KeyboardInterrupt(
                "injected crash after issued receipt"
            ),
        )
    elif crash_at == "after-copies":
        inject = patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=copying_then_raise,
        )
    elif crash_at in {"jsonl", "manifest"}:
        inject = patch(
            "tm_migration._replace_path",
            side_effect=crashing_replace,
        )
    elif crash_at == "before-jsonl":
        inject = patch(
            "tm_migration._replace_path",
            side_effect=crashing_replace,
        )
    else:
        raise AssertionError(f"unknown crash boundary {crash_at!r}")
    with inject:
        try:
            service.refresh_configured_snapshot(store)
        except KeyboardInterrupt:
            return
    raise AssertionError(f"refresh did not crash at {crash_at!r}")


def _assert_recovery_outcome_shape(
    testcase: unittest.TestCase,
    outcome: RefreshRecoveryOutcome,
) -> None:
    testcase.assertIsInstance(outcome.state, RefreshRecoveryState)
    testcase.assertIsInstance(outcome.receipts, tuple)
    for item in outcome.receipts:
        testcase.assertIsInstance(item, IssuedReceiptRecovery)
        testcase.assertIsInstance(item.state, RefreshRecoveryState)
        testcase.assertIsInstance(item.diagnostics, tuple)
        for code in item.diagnostics:
            testcase.assertRegex(code, _IDENTIFIER)
    testcase.assertIsInstance(outcome.diagnostics, tuple)
    for code in outcome.diagnostics:
        testcase.assertRegex(code, _IDENTIFIER)
    if outcome.error_code is not None:
        testcase.assertRegex(outcome.error_code, _IDENTIFIER)
    testcase.assertIsInstance(outcome.retryable, bool)


class TMRecoveryConfiguredDecisionTests(unittest.TestCase):
    def test_old_pair_plus_issued_cancels_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.CANCELLED,
                    ),
                ),
            )
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "cancelled",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_jsonl_only_new_digest_reconstructs_manifest_and_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertNotEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_full_new_pair_completes_without_republish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            jsonl_identity = os.lstat(identity.configured_jsonl_path)
            manifest_identity = os.lstat(identity.snapshot_manifest_path)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                os.lstat(identity.configured_jsonl_path).st_ino,
                jsonl_identity.st_ino,
            )
            self.assertEqual(
                os.lstat(identity.snapshot_manifest_path).st_ino,
                manifest_identity.st_ino,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertNotEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_history_revision_pair_completes_as_verified_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            _ = store.append(_draft("newer", "appended after crash"))

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_foreign_manifest_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            foreign_manifest = b"foreign manifest bytes\n"
            identity.snapshot_manifest_path.write_bytes(foreign_manifest)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.MANIFEST_FOREIGN",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.MANIFEST_FOREIGN", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                foreign_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_missing_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.unlink()
            identity.snapshot_manifest_path.unlink()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNMATCHED", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_symlink_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            target = identity.configured_jsonl_path.with_name("foreign.jsonl")
            target.write_bytes(_NEW_JSONL)
            identity.configured_jsonl_path.unlink()
            identity.configured_jsonl_path.symlink_to(target)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertTrue(identity.configured_jsonl_path.is_symlink())
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_hardlink_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            other = identity.configured_jsonl_path.with_name("linked.jsonl")
            other.write_bytes(_NEW_JSONL)
            os.replace(other, identity.configured_jsonl_path)
            os.link(identity.configured_jsonl_path, other)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(os.lstat(identity.configured_jsonl_path).st_nlink, 2)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_directory_manifest_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            identity.snapshot_manifest_path.unlink()
            identity.snapshot_manifest_path.mkdir()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertTrue(identity.snapshot_manifest_path.is_dir())
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_ambiguous_issued_receipts_latch_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            first = _current_receipt(store, prefix="snapshot.refresh.a.", payload=_NEW_JSONL)
            second = _current_receipt(store, prefix="snapshot.refresh.b.", payload=_NEW_JSONL)
            _register_refresh(store, first)
            _register_refresh(store, second)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            identity.snapshot_manifest_path.write_bytes(b"foreign manifest\n")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.AMBIGUOUS_JSONL", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_foreign_ledger_path_issued_is_export_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=(root / "foreign.jsonl").resolve(),
                destination_manifest_path=(
                    root / "foreign.jsonl.localcat-snapshot.json"
                ).resolve(),
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PAIR_UNPROVABLE",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_ancestry_invalid_receipt_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.configured_jsonl_path,
                destination_manifest_path=identity.snapshot_manifest_path,
                status="issued",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_receipt SET exported_revision = 999 "
                    "WHERE snapshot_id = ?",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()
            _write_new_pair(identity, receipt, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.ANCESTRY_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_binding_invalid_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.BINDING_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_binding_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            prior_pair = _pair(identity)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.BINDING_INVALID",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.BINDING_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)

    def test_receipt_destination_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.snapshot_manifest_path,
                destination_manifest_path=identity.configured_jsonl_path,
                status="issued",
            )
            prior_pair = _pair(identity)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_foreign_identity_issued_never_noops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.configured_jsonl_path,
                destination_manifest_path=identity.snapshot_manifest_path,
                status="issued",
                resource_id="tm.foreign",
            )
            _write_new_pair(identity, receipt, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.LEDGER_IDENTITY_INVALID",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.LEDGER_IDENTITY_INVALID",
                outcome.diagnostics,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), (_NEW_JSONL, _manifest_bytes_for(receipt)))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_pre_existing_divergence_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = '1' "
                    "WHERE key = 'divergence_latched'"
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.NOOP)
            self.assertIn("RECOVERY.DIVERGENCE_PRESERVED", outcome.diagnostics)
            self.assertEqual(outcome.snapshot_id, None)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )


class TMRecoveryCrashBoundaryTests(unittest.TestCase):
    def test_crash_after_issued_cancels_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            _simulate_crash(service, store, crash_at="issued")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "cancelled")
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_crash_after_recovery_copies_cancels_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            _simulate_crash(service, store, crash_at="before-jsonl")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(
                    stage.staged_db_path,
                    snapshot_id,
                ),
                "cancelled",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_crash_before_recovery_handoff_preserves_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_pair = _pair(identity)
            _simulate_crash(service, store, crash_at="after-copies")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            fresh = _fresh_store(stage)
            paths = _paths(identity)
            jsonl_recovery_before = os.lstat(paths.jsonl_recovery)
            manifest_recovery_before = os.lstat(paths.manifest_recovery)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIn("RECOVERY.ARTIFACT_UNPROVEN", outcome.diagnostics)
            self.assertEqual(
                _status_for(
                    stage.staged_db_path,
                    snapshot_id,
                ),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                (os.lstat(paths.jsonl_recovery).st_dev, os.lstat(paths.jsonl_recovery).st_ino),
                (jsonl_recovery_before.st_dev, jsonl_recovery_before.st_ino),
            )
            self.assertEqual(
                (
                    os.lstat(paths.manifest_recovery).st_dev,
                    os.lstat(paths.manifest_recovery).st_ino,
                ),
                (
                    manifest_recovery_before.st_dev,
                    manifest_recovery_before.st_ino,
                ),
            )
            self.assertTrue(paths.jsonl_temp.exists())
            self.assertTrue(paths.manifest_temp.exists())

    def test_crash_after_jsonl_replace_reconstructs_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_crash_after_manifest_replace_completes_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_crash_after_complete_commit_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            original_complete = store.complete_issued_refresh_receipt

            def commit_then_raise(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                original_complete(snapshot_id, **kwargs)
                raise SQLiteStoreLifecycleError(
                    "STORE.LEDGER_UNAVAILABLE",
                    resource_id="tm.primary",
                    generation=0,
                    retryable=True,
                )

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=commit_then_raise,
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIsInstance(result, ExportReport)
            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_count, 6)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            fresh = _fresh_store(stage)
            replay = fresh.recover_configured_refresh()
            _assert_recovery_outcome_shape(self, replay)
            self.assertIs(replay.state, RefreshRecoveryState.NOOP)
            self.assertEqual(replay.diagnostics, ())

    def test_observe_recovers_before_misclassifying_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            observation = fresh.source_binding_monitor.observe()

            self.assertEqual(
                observation.state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(observation.diagnostic_codes, ())
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_refresh_after_crash_recovery_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            result = service.refresh_configured_snapshot(fresh)

            self.assertIsInstance(result, ExportReport)
            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_count, 6)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 2)
            self.assertEqual(
                tuple(str(row[9]) for row in refresh_rows),
                ("completed", "completed"),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMRecoveryIdempotencyTests(unittest.TestCase):
    def test_repeated_recovery_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)

            first = store.recover_configured_refresh()
            second = store.recover_configured_refresh()
            third = store.recover_configured_refresh()

            self.assertIs(first.state, RefreshRecoveryState.COMPLETED)
            self.assertIs(second.state, RefreshRecoveryState.NOOP)
            self.assertIs(third.state, RefreshRecoveryState.NOOP)
            self.assertEqual(second.diagnostics, ())
            self.assertEqual(third.diagnostics, ())
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )

    def test_fresh_store_replay_reaches_same_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)

            first = store.recover_configured_refresh()
            fresh = _fresh_store(stage)
            replay = fresh.recover_configured_refresh()

            self.assertIs(first.state, RefreshRecoveryState.COMPLETED)
            self.assertIs(replay.state, RefreshRecoveryState.NOOP)
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_cold_store_without_receipts_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                initialize_stage_schema(stage, canonical_store_id="store.primary")
                store = SQLiteTMStore(stage, canonical_store_id="store.primary")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.NOOP)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertFalse(
                stage.resource_identity.configured_jsonl_path.exists()
            )

    def test_cold_store_issued_receipt_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                initialize_stage_schema(stage, canonical_store_id="store.primary")
                store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            revision = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.cold",
                resource_id=revision.resource_id,
                canonical_store_id=revision.canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=hashlib.sha256(_NEW_JSONL).hexdigest(),
                record_count=revision.record_count,
            )
            store.register_issued_refresh_receipt(
                receipt,
                expected_generation=store.capture_export_snapshot().revision.generation,
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNMATCHED", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )


class TMRecoveryExportTests(unittest.TestCase):
    def _destination(self, root: Path) -> Path:
        destination = (root / "exports" / "out.jsonl").resolve()
        destination.parent.mkdir(parents=True)
        return destination

    def _register_export(
        self,
        store: SQLiteTMStore,
        destination: Path,
        payload: bytes,
    ) -> SnapshotReceipt:
        paths = _export_artifact_paths(destination)
        revision = store.capture_export_snapshot().revision
        receipt = SnapshotReceipt(
            snapshot_id=f"snapshot.export.{hashlib.sha256(payload).hexdigest()[:12]}",
            resource_id=revision.resource_id,
            canonical_store_id=revision.canonical_store_id,
            exported_revision=revision.head_revision,
            jsonl_digest=hashlib.sha256(payload).hexdigest(),
            record_count=revision.record_count,
        )
        store.register_issued_export_receipt(
            receipt,
            destination_jsonl_path=paths.destination,
            destination_manifest_path=paths.manifest,
            expected_generation=revision.generation,
        )
        return receipt

    def test_export_full_pair_completes_without_touching_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_pair = _pair(identity)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(_manifest_bytes_for(receipt))

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_jsonl_only_reconstructs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                paths.manifest.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_old_pair_cancels_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            first = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(_manifest_bytes_for(first))
            revision = store.capture_export_snapshot().revision
            store.complete_issued_export_receipt(
                first.snapshot_id,
                expected_generation=revision.generation,
            )
            second = self._register_export(store, destination, b'{"source":"later"}\n')

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        second.snapshot_id,
                        RefreshRecoveryState.CANCELLED,
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "cancelled",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_binding_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            first = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(_manifest_bytes_for(first))
            revision = store.capture_export_snapshot().revision
            store.complete_issued_export_receipt(
                first.snapshot_id,
                expected_generation=revision.generation,
            )
            second = self._register_export(
                store,
                destination,
                b'{"source":"pending"}\n',
            )
            prior_pair = _pair(identity)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (second.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        second.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.EXPORT_BINDING_INVALID",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_BINDING_INVALID",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "completed",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                second.snapshot_id,
            )

    def test_export_foreign_manifest_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(b"foreign export manifest\n")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_MANIFEST_FOREIGN",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_unsafe_destination_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(b"temporary manifest")
            paths.manifest.unlink()
            paths.manifest.symlink_to(paths.destination)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PAIR_UNSAFE",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_parent_symlink_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            real = (root / "real").resolve()
            real.mkdir()
            link = (root / "link").resolve()
            link.symlink_to(real)
            destination = link / "out.jsonl"
            receipt = self._register_export(store, destination, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_UNSAFE",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_dotdot_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.export.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=(
                    root / "exports" / ".." / "out.jsonl"
                ),
                destination_manifest_path=(
                    root / "exports" / ".." / "out.jsonl.localcat-snapshot.json"
                ),
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_authority_alias_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            fragment = identity.target_identity[:16]
            destination = root / f".localcat-{fragment}.jsonl"
            receipt = self._register_export(store, destination, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_ALIASED",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_same_byte_swap_at_complete_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(_manifest_bytes_for(receipt))
            original_complete = store.complete_issued_export_receipt
            swapped = False

            def hostile_complete(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    foreign = paths.destination.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    foreign.write_bytes(_NEW_JSONL)
                    os.replace(foreign, paths.destination)
                original_complete(snapshot_id, **kwargs)

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=hostile_complete,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertTrue(swapped)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.RECEIPT_PAIR_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                paths.destination.read_bytes(),
                _NEW_JSONL,
            )

    def test_mixed_refresh_and_export_reconcile_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            refresh = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, refresh)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            export = self._register_export(store, destination, b'{"source":"exported"}\n')
            paths.destination.write_bytes(b'{"source":"exported"}\n')
            paths.manifest.write_bytes(_manifest_bytes_for(export))

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                tuple(item.snapshot_id for item in outcome.receipts),
                (refresh.snapshot_id, export.snapshot_id),
            )
            self.assertEqual(
                tuple(item.state for item in outcome.receipts),
                (
                    RefreshRecoveryState.COMPLETED,
                    RefreshRecoveryState.COMPLETED,
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, refresh.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, export.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                refresh.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMRecoveryArtifactSafetyTests(unittest.TestCase):
    def test_foreign_temp_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            paths = _paths(identity)
            foreign = b"foreign temp bytes\n"
            paths.jsonl_temp.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertIn("RECOVERY.ARTIFACT_CONFLICT", outcome.diagnostics)
            self.assertEqual(paths.jsonl_temp.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )

    def test_foreign_recovery_copy_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = b"foreign recovery bytes\n"
            paths.jsonl_recovery.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertIn("RECOVERY.ARTIFACT_CONFLICT", outcome.diagnostics)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )

    def test_foreign_manifest_temp_blocks_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = b"foreign manifest temp\n"
            paths.manifest_temp.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_CONFLICT",),
                    ),
                ),
            )
            self.assertEqual(paths.manifest_temp.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_jsonl_temp_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            paths.jsonl_temp.write_bytes(_NEW_JSONL)
            temp_before = os.lstat(paths.jsonl_temp)
            manifest_before = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.jsonl_temp.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                (
                    os.lstat(paths.jsonl_temp).st_dev,
                    os.lstat(paths.jsonl_temp).st_ino,
                ),
                (temp_before.st_dev, temp_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_manifest_temp_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = _manifest_bytes_for(receipt)
            paths.manifest_temp.write_bytes(foreign)
            temp_before = os.lstat(paths.manifest_temp)
            manifest_before = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.manifest_temp.read_bytes(), foreign)
            self.assertEqual(
                (
                    os.lstat(paths.manifest_temp).st_dev,
                    os.lstat(paths.manifest_temp).st_ino,
                ),
                (temp_before.st_dev, temp_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_jsonl_recovery_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            paths = _paths(identity)
            prior_pair = _pair(identity)
            paths.jsonl_recovery.write_bytes(_PRIOR_JSONL)
            recovery_before = os.lstat(paths.jsonl_recovery)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.jsonl_recovery.read_bytes(), _PRIOR_JSONL)
            self.assertEqual(
                (
                    os.lstat(paths.jsonl_recovery).st_dev,
                    os.lstat(paths.jsonl_recovery).st_ino,
                ),
                (recovery_before.st_dev, recovery_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_manifest_recovery_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            paths = _paths(identity)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            paths.manifest_recovery.write_bytes(prior_manifest)
            recovery_before = os.lstat(paths.manifest_recovery)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(
                paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                (
                    os.lstat(paths.manifest_recovery).st_dev,
                    os.lstat(paths.manifest_recovery).st_ino,
                ),
                (recovery_before.st_dev, recovery_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_swap_at_complete_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            original_complete = store.complete_issued_refresh_receipt
            swapped = False

            def hostile_complete(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    payload = identity.configured_jsonl_path.read_bytes()
                    foreign = identity.configured_jsonl_path.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    foreign.write_bytes(payload)
                    os.replace(foreign, identity.configured_jsonl_path)
                original_complete(snapshot_id, **kwargs)

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=hostile_complete,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertTrue(swapped)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.REFRESH_PAIR_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMRecoveryServiceTests(unittest.TestCase):
    def test_service_recovery_delegates_to_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)

            outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )

    def test_service_recovery_normalizes_store_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)

            with patch.object(
                store,
                "recover_configured_refresh",
                side_effect=SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id="tm.primary",
                    generation=7,
                    retryable=True,
                ),
            ):
                outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(outcome.error_code, "STORE.GENERATION_CHANGED")
            self.assertTrue(outcome.retryable)

            with patch.object(
                store,
                "recover_configured_refresh",
                side_effect=SQLiteStoreSchemaError(
                    "STORE.RECEIPT_STALE"
                ),
            ):
                outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(outcome.error_code, "STORE.RECEIPT_STALE")
            self.assertFalse(outcome.retryable)


class TMRecoverySerializationTests(unittest.TestCase):
    def test_recovery_is_reentrant_inside_refresh_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)

            with store.configured_refresh_reservation():
                outcome = store.recover_configured_refresh()

            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_recovery_serializes_with_held_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            held = threading.Event()
            release = threading.Event()
            holder_error: list[Exception] = []

            def holder() -> None:
                try:
                    with store.configured_refresh_reservation():
                        held.set()
                        if not release.wait(10):
                            raise AssertionError("holder never released")
                except Exception as error:  # pragma: no cover - failure path
                    holder_error.append(error)

            results: list[RefreshRecoveryOutcome] = []
            runner_error: list[Exception] = []

            def runner() -> None:
                try:
                    results.append(store.recover_configured_refresh())
                except Exception as error:  # pragma: no cover - failure path
                    runner_error.append(error)

            holder_thread = threading.Thread(target=holder)
            runner_thread = threading.Thread(target=runner)
            holder_thread.start()
            if not held.wait(10):
                raise AssertionError("reservation never held")
            runner_thread.start()
            time.sleep(0.2)
            self.assertTrue(runner_thread.is_alive(), "recovery bypassed the gate")
            release.set()
            holder_thread.join(30)
            runner_thread.join(30)
            if holder_thread.is_alive() or runner_thread.is_alive():
                raise AssertionError("serialization threads never finished")
            self.assertEqual(holder_error, [])
            self.assertEqual(runner_error, [])
            self.assertEqual(len(results), 1)
            self.assertIs(results[0].state, RefreshRecoveryState.COMPLETED)


class TMRecoveryCanonicalInvariantTests(unittest.TestCase):
    def test_recovery_never_changes_canonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            before_snapshot = store.capture_export_snapshot()
            before_records = _record_dump(stage.staged_db_path)
            before_head = _meta_value(stage.staged_db_path, "head_revision")
            before_generation = store.coordinator.current_generation

            outcome = store.recover_configured_refresh()

            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            after_snapshot = store.capture_export_snapshot()
            self.assertEqual(after_snapshot.revision, before_snapshot.revision)
            self.assertEqual(after_snapshot.records, before_snapshot.records)
            self.assertEqual(_record_dump(stage.staged_db_path), before_records)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "head_revision"),
                before_head,
            )
            self.assertEqual(
                store.coordinator.current_generation,
                before_generation,
            )


if __name__ == "__main__":
    unittest.main()
