"""Task 5.13 configured JSONL snapshot refresh tests.

``refresh_configured_snapshot`` republishes the active canonical store
as the configured JSONL plus deterministic adjacent manifest, reusing
the Task 5.12 export publication protocol without touching canonical
records, generation or head revision.
"""

from __future__ import annotations

from collections.abc import Callable
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
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
import tm_sqlite_store
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    AssetKind,
    AssetPreservationState,
    CanonicalResourceIdentity,
    ExportFailure,
    ExportReport,
    MutableStageRef,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_migration import (
    ExportPreflightError,
    TMMigrationService,
    _export_artifact_paths,
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


def _service(identity: CanonicalResourceIdentity) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _prepared_store(
    root: Path,
) -> tuple[MutableStageRef, SQLiteTMStore]:
    """One live canonical store seeded with every supported draft variant."""

    stage = _stage(root)
    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        store = SQLiteTMStore(stage, canonical_store_id="store.primary")
    _ = store.append_batch(
        batch_id="migration.seed.refresh",
        kind="migration",
        drafts=(
            _draft(
                "same",
                "first",
                speaker="alice",
                previous="before",
                following="after",
                file_source="chapter.json",
                provenance=(("source", "legacy-jsonl"),),
            ),
            _draft("same", "second", speaker="alice"),
            _draft(
                "Straße",
                "übersetzt",
                provenance=(
                    ("batch", "seed"),
                    ("source", "legacy-jsonl"),
                ),
            ),
            _draft("minimal", "target only", provenance=()),
            _draft("context", "both", previous="p", following="f"),
            _draft("file", "src", file_source="book.txt"),
        ),
        source_digest="c" * 64,
        source_path=(root / "seed-source.jsonl").resolve(),
        legacy_line_nos=(1, 2, 3, 4, 5, 6),
    )
    return stage, store


def _bind_current_snapshot(
    store: SQLiteTMStore,
    stage: MutableStageRef,
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


def _write_explicit_pair(
    identity: CanonicalResourceIdentity,
    receipt: SnapshotReceipt,
    jsonl_bytes: bytes,
) -> None:
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    identity.configured_jsonl_path.write_bytes(jsonl_bytes)
    identity.snapshot_manifest_path.write_text(
        contract_to_json(manifest),
        encoding="utf-8",
    )


def _manifest_bytes_for(receipt: SnapshotReceipt) -> bytes:
    """Deterministic adjacent manifest bytes for one issued receipt."""

    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return contract_to_json(manifest).encode("utf-8")


def _identity_of(path: Path) -> tuple[int, int]:
    """The exact device/inode identity of one existing entry."""

    observed = os.lstat(path)
    return (observed.st_dev, observed.st_ino)


def _prior_state(
    path: Path,
) -> tuple[tuple[int, int] | None, str | None, bool]:
    """One (identity, digest, absent) prior-entry record for a path."""

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return (None, None, True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ((observed.st_dev, observed.st_ino), digest, False)



_PRIOR_JSONL = b'{"source":"bound","target":"pair"}\n'


def _paths(identity: CanonicalResourceIdentity) -> Any:
    return _export_artifact_paths(identity.configured_jsonl_path)


def _pair(identity: CanonicalResourceIdentity) -> tuple[bytes, bytes]:
    return (
        identity.configured_jsonl_path.read_bytes(),
        identity.snapshot_manifest_path.read_bytes(),
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


def _assert_safe_failure(self: Any, failure: Any, root: Path) -> None:
    self.assertRegex(failure.error_code, _IDENTIFIER)
    self.assertRegex(failure.stage, _IDENTIFIER)
    for diagnostic in failure.diagnostics:
        self.assertRegex(diagnostic.code, _IDENTIFIER)
        self.assertRegex(diagnostic.safe_summary, _IDENTIFIER)
    self.assertNotIn(str(root), failure.error_code)
    self.assertNotIn(str(root), failure.stage)
    self.assertNotIn("tampered", failure.error_code)
    self.assertNotIn("tampered", failure.stage)


class TMRefreshSuccessTests(unittest.TestCase):
    def test_refresh_publishes_complete_pair_and_reports_verified_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            monitor = store.source_binding_monitor
            self.assertEqual(
                monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

            result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_count, 6)
            self.assertEqual(result.skipped_count, 0)
            revision = store.capture_export_snapshot().revision
            self.assertEqual(result.exported_revision, revision.head_revision)
            self.assertEqual(result.canonical_generation, revision.generation)
            self.assertEqual(
                result.destination_digest,
                hashlib.sha256(
                    identity.configured_jsonl_path.read_bytes()
                ).hexdigest(),
            )
            self.assertTrue(result.snapshot_id.startswith("snapshot.refresh."))
            self.assertEqual(
                result.snapshot_receipt_digest,
                snapshot_receipt_digest(result.snapshot_receipt),
            )
            rows = [
                json.loads(line)
                for line in identity.configured_jsonl_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(rows), 6)
            self.assertEqual(
                tuple(row["source"] for row in rows),
                ("same", "same", "Straße", "minimal", "context", "file"),
            )
            observed = monitor.observe()
            self.assertEqual(observed.state, SourceBindingState.VERIFIED_CURRENT)
            self.assertEqual(observed.diagnostic_codes, ())

    def test_refresh_from_history_binding_reports_verified_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _ = store.append(_draft("newer", "appended after binding"))
            monitor = store.source_binding_monitor
            self.assertEqual(
                monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )

            result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_count, 7)
            observed = monitor.observe()
            self.assertEqual(observed.state, SourceBindingState.VERIFIED_CURRENT)
            self.assertEqual(observed.diagnostic_codes, ())
            self.assertTrue(
                b'"source":"newer","target":"appended after binding"'
                in identity.configured_jsonl_path.read_bytes()
            )

    def test_refresh_keeps_records_revision_and_generation_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            bind = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            before_snapshot = store.capture_export_snapshot()
            before_records = _record_dump(stage.staged_db_path)
            before_head = _meta_value(stage.staged_db_path, "head_revision")
            before_generation = store.coordinator.current_generation

            result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportReport)
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
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertNotEqual(
                _binding_row(stage.staged_db_path)[4],
                bind.receipt.snapshot_id,
            )

    def test_refresh_ledger_and_binding_share_same_completed_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)

            result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportReport)
            binding = _binding_row(stage.staged_db_path)
            self.assertEqual(str(binding[0]), "1")
            self.assertEqual(str(binding[1]), str(identity.configured_jsonl_path))
            self.assertEqual(str(binding[2]), str(identity.snapshot_manifest_path))
            self.assertEqual(str(binding[3]), "EXPLICIT_EXPORT")
            self.assertEqual(str(binding[4]), result.snapshot_id)
            self.assertEqual(str(binding[5]), "snapshot-binding-v1")
            self.assertEqual(
                _status_for(stage.staged_db_path, result.snapshot_id),
                "completed",
            )
            rows = _ledger_rows(stage.staged_db_path, identity.configured_jsonl_path)
            by_id = {str(row[0]): row for row in rows}
            issued_rows = tuple(
                row for row in rows if str(row[9]) == "issued"
            )
            self.assertEqual(issued_rows, ())
            new_row = by_id[result.snapshot_id]
            self.assertEqual(str(new_row[1]), identity.resource_id)
            self.assertEqual(str(new_row[2]), "store.primary")
            self.assertEqual(str(new_row[7]), str(identity.configured_jsonl_path))
            self.assertEqual(
                str(new_row[8]),
                str(identity.snapshot_manifest_path),
            )
            self.assertEqual(str(new_row[9]), "completed")
            self.assertEqual(str(new_row[6]), SNAPSHOT_FORMAT_VERSION)

    def test_refresh_publication_order_jsonl_before_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            order: list[str] = []
            original_replace = tm_migration._replace_path

            def recording_replace(
                source: Path,
                target: Path,
                **kwargs: Any,
            ) -> None:
                order.append(f"{source.name}->{target.name}")
                original_replace(source, target, **kwargs)

            with patch(
                "tm_migration._replace_path",
                side_effect=recording_replace,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportReport)
            self.assertEqual(
                order,
                [
                    f"{artifact_paths.jsonl_temp.name}"
                    f"->{artifact_paths.destination.name}",
                    f"{artifact_paths.manifest_temp.name}"
                    f"->{artifact_paths.manifest.name}",
                ],
            )

    def test_refresh_pins_stable_snapshot_under_concurrent_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            before = store.capture_export_snapshot()
            capture_done = threading.Event()
            append_done = threading.Event()
            observed_records: list[Any] = []
            original_stream = tm_migration._stream_export_jsonl_temp

            def interleave_stream(
                path: Path,
                records: tuple[object, ...],
                *,
                parent_handle: Any = None,
            ) -> tuple[str, int, Any]:
                observed_records.extend(records)
                capture_done.set()
                if not append_done.wait(10):
                    raise AssertionError("concurrent append never completed")
                return original_stream(
                    path,
                    records,
                    parent_handle=parent_handle,
                )

            def appender() -> None:
                if not capture_done.wait(10):
                    raise AssertionError("refresh never captured its snapshot")
                _ = store.append(
                    _draft("concurrent", "appended during refresh")
                )
                append_done.set()

            thread = threading.Thread(target=appender)
            thread.start()
            try:
                with patch(
                    "tm_migration._stream_export_jsonl_temp",
                    side_effect=interleave_stream,
                ):
                    result = service.refresh_configured_snapshot(store)
            finally:
                thread.join(10)
            self.assertFalse(thread.is_alive())

            self.assertEqual(
                len(observed_records),
                before.revision.record_count,
            )
            self.assertTrue(
                all(
                    record.record.source_raw != "concurrent"
                    for record in observed_records
                )
            )
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.PUBLISH")
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, _pair(identity)[1]))
            self.assertEqual(
                _refresh_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                f"snapshot.{identity.resource_id}.bound",
            )
            after = store.capture_export_snapshot()
            self.assertEqual(
                after.revision.head_revision,
                before.revision.head_revision + 1,
            )


class TMRefreshDivergenceTests(unittest.TestCase):
    def test_refresh_rejected_when_diverged_with_zero_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            configured = identity.configured_jsonl_path
            tampered = b'{"source":"tampered","target":"source"}\n'
            configured.write_bytes(tampered)
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            diverged = store.source_binding_monitor.observe()
            self.assertEqual(
                diverged.state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            generation_before = store.coordinator.current_generation
            head_before = store.canonical_revision().head_revision

            result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
            self.assertEqual(result.error_code, "REFRESH.SOURCE_DIVERGED")
            self.assertFalse(result.retryable)
            _assert_safe_failure(self, result, root)
            self.assertEqual(configured.read_bytes(), tampered)
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                _refresh_rows(stage.staged_db_path, configured),
                (),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                f"snapshot.{identity.resource_id}.bound",
            )
            self.assertEqual(store.coordinator.current_generation, generation_before)
            self.assertEqual(
                store.canonical_revision().head_revision,
                head_before,
            )
            artifact_paths = _paths(identity)
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists())
            after = store.source_binding_monitor.observe()
            self.assertEqual(after.state, SourceBindingState.SOURCE_DIVERGED)
            self.assertEqual(
                after.diagnostic_codes,
                ("SOURCE_BINDING.DIVERGENCE_LATCHED",),
            )


class TMMonitorUnsafePairTests(unittest.TestCase):
    def test_monitor_latches_unsafe_configured_entries_deterministically(
        self,
    ) -> None:
        mutations = (
            (
                "jsonl symlink",
                "jsonl",
                "symlink",
                "SOURCE_BINDING.JSONL_UNSAFE",
            ),
            (
                "jsonl hardlink",
                "jsonl",
                "hardlink",
                "SOURCE_BINDING.JSONL_UNSAFE",
            ),
            (
                "manifest symlink",
                "manifest",
                "symlink",
                "SOURCE_BINDING.MANIFEST_UNSAFE",
            ),
            (
                "manifest hardlink",
                "manifest",
                "hardlink",
                "SOURCE_BINDING.MANIFEST_UNSAFE",
            ),
        )
        for name, entry, kind, expected_code in mutations:
            with self.subTest(mutation=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage, store = _prepared_store(root)
                    identity = stage.resource_identity
                    _ = _bind_current_snapshot(
                        store,
                        stage,
                        _PRIOR_JSONL,
                    )
                    target = (
                        identity.configured_jsonl_path
                        if entry == "jsonl"
                        else identity.snapshot_manifest_path
                    )
                    plain = root / f"plain-{name.replace(' ', '-')}.dat"
                    if entry == "jsonl":
                        plain.write_bytes(_PRIOR_JSONL)
                    else:
                        plain.write_bytes(target.read_bytes())
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(plain)
                    else:
                        os.link(plain, target)

                    first = store.source_binding_monitor.observe()

                    self.assertEqual(
                        first.state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertEqual(first.diagnostic_codes, (expected_code,))
                    self.assertEqual(
                        _meta_value(
                            stage.staged_db_path,
                            "divergence_latched",
                        ),
                        "1",
                    )
                    second = store.source_binding_monitor.observe()
                    self.assertEqual(
                        second.state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertEqual(
                        second.diagnostic_codes,
                        ("SOURCE_BINDING.DIVERGENCE_LATCHED",),
                    )


class TMRefreshPreflightTests(unittest.TestCase):
    def test_refresh_requires_exact_store_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, _store = _prepared_store(root)
            service = _service(stage.resource_identity)
            with self.assertRaises(TypeError):
                service.refresh_configured_snapshot(cast(Any, object()))

    def test_refresh_fails_closed_on_artifact_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            cases = (
                (artifact_paths.jsonl_temp, "REFRESH.TEMP_CONFLICT"),
                (artifact_paths.manifest_temp, "REFRESH.TEMP_CONFLICT"),
                (artifact_paths.jsonl_recovery, "REFRESH.RECOVERY_CONFLICT"),
                (artifact_paths.manifest_recovery, "REFRESH.RECOVERY_CONFLICT"),
            )
            for artifact, expected_code in cases:
                with self.subTest(artifact=artifact.name):
                    artifact.write_bytes(b'{"source":"stale","target":"x"}\n')
                    try:
                        result = service.refresh_configured_snapshot(store)
                    finally:
                        artifact.unlink()
                    assert isinstance(result, ExportFailure)
                    self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
                    self.assertEqual(result.error_code, expected_code)
                    self.assertFalse(result.retryable)
                    _assert_safe_failure(self, result, root)
                    self.assertEqual(_pair(identity)[0], _PRIOR_JSONL)
                    self.assertEqual(
                        _refresh_rows(
                            stage.staged_db_path,
                            identity.configured_jsonl_path,
                        ),
                        (),
                    )

    def test_refresh_latches_divergence_on_asymmetric_prior_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            identity.configured_jsonl_path.unlink()
            result = service.refresh_configured_snapshot(store)
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
            self.assertEqual(result.error_code, "REFRESH.SOURCE_DIVERGED")
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_refresh_unsafe_configured_entry_latches_divergence(self) -> None:
        mutations = (
            ("jsonl symlink", "jsonl", "symlink"),
            ("jsonl hardlink", "jsonl", "hardlink"),
            ("manifest symlink", "manifest", "symlink"),
            ("manifest hardlink", "manifest", "hardlink"),
        )
        for name, entry, kind in mutations:
            with self.subTest(mutation=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage, store = _prepared_store(root)
                    identity = stage.resource_identity
                    service = _service(identity)
                    binding = _bind_current_snapshot(
                        store,
                        stage,
                        _PRIOR_JSONL,
                    )
                    target = (
                        identity.configured_jsonl_path
                        if entry == "jsonl"
                        else identity.snapshot_manifest_path
                    )
                    plain = root / f"plain-{name.replace(' ', '-')}.dat"
                    if entry == "jsonl":
                        plain.write_bytes(_PRIOR_JSONL)
                    else:
                        plain.write_bytes(target.read_bytes())
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(plain)
                    else:
                        os.link(plain, target)

                    result = service.refresh_configured_snapshot(store)

                    assert isinstance(result, ExportFailure)
                    self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
                    self.assertEqual(
                        result.error_code,
                        "REFRESH.SOURCE_DIVERGED",
                    )
                    self.assertFalse(result.retryable)
                    _assert_safe_failure(self, result, root)
                    observed = store.source_binding_monitor.observe()
                    self.assertEqual(
                        observed.state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertEqual(
                        observed.diagnostic_codes,
                        ("SOURCE_BINDING.DIVERGENCE_LATCHED",),
                    )
                    self.assertEqual(
                        _meta_value(
                            stage.staged_db_path,
                            "divergence_latched",
                        ),
                        "1",
                    )
                    self.assertEqual(
                        _refresh_rows(
                            stage.staged_db_path,
                            identity.configured_jsonl_path,
                        ),
                        (),
                    )
                    self.assertEqual(
                        _binding_row(stage.staged_db_path)[4],
                        binding.receipt.snapshot_id,
                    )
                    artifact_paths = _paths(identity)
                    for artifact in (
                        artifact_paths.jsonl_temp,
                        artifact_paths.manifest_temp,
                        artifact_paths.jsonl_recovery,
                        artifact_paths.manifest_recovery,
                    ):
                        self.assertFalse(artifact.exists())
                    observed_target = os.lstat(target)
                    self.assertTrue(
                        stat.S_ISLNK(observed_target.st_mode)
                        or observed_target.st_nlink == 2
                    )


class TMRefreshSerializationTests(unittest.TestCase):
    def test_concurrent_refreshes_serialize_and_never_poison_divergence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            original_stream = tm_migration._stream_export_jsonl_temp
            original_reservation = store.configured_refresh_reservation
            entries: list[int] = []
            entries_lock = threading.Lock()
            attempts: list[int] = []
            attempt_lock = threading.Lock()
            first_entered = threading.Event()
            release_first = threading.Event()
            max_concurrent = 0

            def blocking_stream(
                path: Path,
                records: tuple[object, ...],
                *,
                parent_handle: Any = None,
            ) -> tuple[str, int, Any]:
                nonlocal max_concurrent
                with entries_lock:
                    entries.append(1)
                    max_concurrent = max(max_concurrent, len(entries))
                first_entered.set()
                if not release_first.wait(10):
                    raise AssertionError("first refresh was never released")
                try:
                    return original_stream(
                        path,
                        records,
                        parent_handle=parent_handle,
                    )
                finally:
                    with entries_lock:
                        entries.pop()

            def recording_reservation(
                timeout_seconds: float | None = None,
            ) -> Any:
                with attempt_lock:
                    attempts.append(1)
                return original_reservation(timeout_seconds=timeout_seconds)

            results: list[Any] = []

            def refresher() -> None:
                results.append(service.refresh_configured_snapshot(store))

            with patch(
                "tm_migration._stream_export_jsonl_temp",
                side_effect=blocking_stream,
            ), patch.object(
                store,
                "configured_refresh_reservation",
                side_effect=recording_reservation,
            ):
                first = threading.Thread(target=refresher)
                second = threading.Thread(target=refresher)
                first.start()
                second.start()
                if not first_entered.wait(10):
                    raise AssertionError("first refresh never entered stream")
                deadline = time.monotonic() + 10
                while (
                    len(attempts) < 2
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(
                    len(attempts),
                    2,
                    "second refresh never attempted the reservation",
                )
                with entries_lock:
                    self.assertEqual(
                        len(entries),
                        1,
                        "second refresh entered publication while "
                        "the first refresh was in flight",
                    )
                    self.assertEqual(max_concurrent, 1)
                release_first.set()
                first.join(30)
                second.join(30)
                if first.is_alive() or second.is_alive():
                    raise AssertionError(
                        "concurrent refreshes never finished"
                    )

            reports = tuple(
                result
                for result in results
                if isinstance(result, ExportReport)
            )
            failures = tuple(
                result
                for result in results
                if not isinstance(result, ExportReport)
            )
            self.assertEqual(len(results), 2)
            self.assertEqual(len(reports), 2)
            self.assertEqual(failures, ())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 2)
            self.assertEqual(
                tuple(str(row[9]) for row in refresh_rows),
                ("completed", "completed"),
            )
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)


class TMRefreshConcurrencyTests(unittest.TestCase):
    def test_monitor_observation_during_jsonl_only_window_waits_and_reconciles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            prior_jsonl = identity.configured_jsonl_path.read_bytes()
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            original_fsync = tm_migration._fsync_directory
            fsync_calls = 0
            jsonl_published = threading.Event()
            observation_started = threading.Event()
            release_publication = threading.Event()
            observations: list[Any] = []
            observation_errors: list[BaseException] = []
            refresh_results: list[Any] = []

            def blocking_fsync(
                path: Path,
                *,
                parent_handle: Any = None,
            ) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    jsonl_published.set()
                    if not release_publication.wait(10):
                        raise AssertionError(
                            "test never released the publication window"
                        )
                original_fsync(path, parent_handle=parent_handle)

            def observer() -> None:
                observation_started.set()
                try:
                    observations.append(
                        store.source_binding_monitor.observe()
                    )
                except BaseException as error:
                    observation_errors.append(error)

            def refresher() -> None:
                try:
                    refresh_results.append(
                        service.refresh_configured_snapshot(store)
                    )
                except BaseException as error:
                    observation_errors.append(error)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=blocking_fsync,
            ):
                refresh = threading.Thread(target=refresher)
                refresh.start()
                if not jsonl_published.wait(10):
                    raise AssertionError(
                        "refresh never entered the JSONL-only window"
                    )
                self.assertNotEqual(
                    identity.configured_jsonl_path.read_bytes(),
                    prior_jsonl,
                    "the JSONL was not republished first",
                )
                self.assertEqual(
                    identity.snapshot_manifest_path.read_bytes(),
                    prior_manifest,
                    "the manifest was replaced before the JSONL",
                )
                watcher = threading.Thread(target=observer)
                watcher.start()
                if not observation_started.wait(10):
                    raise AssertionError("observer never started")
                time.sleep(0.05)
                self.assertEqual(
                    observations,
                    [],
                    "observation escaped the refresh publication window",
                )
                self.assertEqual(
                    _meta_value(
                        stage.staged_db_path,
                        "divergence_latched",
                    ),
                    "0",
                    "observation latched divergence inside the window",
                )
                release_publication.set()
                refresh.join(30)
                watcher.join(30)
                if refresh.is_alive() or watcher.is_alive():
                    raise AssertionError(
                        "refresh or observation never finished"
                    )

            self.assertEqual(observation_errors, [])
            self.assertEqual(len(refresh_results), 1)
            self.assertIsInstance(refresh_results[0], ExportReport)
            self.assertEqual(len(observations), 1)
            self.assertEqual(
                observations[0].state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_owning_preflight_observe_is_reentrant_while_reserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            outcomes: list[Any] = []
            errors: list[BaseException] = []

            def holder() -> None:
                try:
                    with store.configured_refresh_reservation():
                        outcomes.append(
                            store.source_binding_monitor.observe().state
                        )
                        outcomes.append(
                            store.source_binding_monitor.observe().state
                        )
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=holder)
            thread.start()
            thread.join(10)
            self.assertFalse(
                thread.is_alive(),
                "owning preflight observation deadlocked "
                "under its own reservation",
            )
            self.assertEqual(errors, [])
            self.assertEqual(
                outcomes,
                [
                    SourceBindingState.VERIFIED_CURRENT,
                    SourceBindingState.VERIFIED_CURRENT,
                ],
            )

    def test_second_refresh_waits_out_jsonl_window_and_never_poisons(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            original_fsync = tm_migration._fsync_directory
            original_reservation = store.configured_refresh_reservation
            fsync_calls = 0
            jsonl_published = threading.Event()
            second_attempted = threading.Event()
            release_publication = threading.Event()
            attempts: list[int] = []
            attempt_lock = threading.Lock()
            results: list[Any] = []
            errors: list[BaseException] = []

            def blocking_fsync(
                path: Path,
                *,
                parent_handle: Any = None,
            ) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    jsonl_published.set()
                    if not release_publication.wait(10):
                        raise AssertionError(
                            "test never released the publication window"
                        )
                original_fsync(path, parent_handle=parent_handle)

            def recording_reservation(
                timeout_seconds: float | None = None,
            ) -> Any:
                with attempt_lock:
                    attempts.append(1)
                second_attempted.set()
                return original_reservation(
                    timeout_seconds=timeout_seconds
                )

            def refresher() -> None:
                try:
                    results.append(
                        service.refresh_configured_snapshot(store)
                    )
                except BaseException as error:
                    errors.append(error)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=blocking_fsync,
            ), patch.object(
                store,
                "configured_refresh_reservation",
                side_effect=recording_reservation,
            ):
                first = threading.Thread(target=refresher)
                second = threading.Thread(target=refresher)
                first.start()
                if not jsonl_published.wait(10):
                    raise AssertionError(
                        "first refresh never entered the JSONL-only window"
                    )
                second.start()
                if not second_attempted.wait(10):
                    raise AssertionError(
                        "second refresh never attempted the reservation"
                    )
                time.sleep(0.05)
                with attempt_lock:
                    self.assertEqual(
                        len(attempts),
                        2,
                        "second refresh never attempted the reservation",
                    )
                self.assertEqual(
                    results,
                    [],
                    "second refresh entered publication during the window",
                )
                self.assertEqual(
                    _meta_value(
                        stage.staged_db_path,
                        "divergence_latched",
                    ),
                    "0",
                )
                release_publication.set()
                first.join(30)
                second.join(30)
                if first.is_alive() or second.is_alive():
                    raise AssertionError(
                        "concurrent refreshes never finished"
                    )

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertTrue(
                all(isinstance(result, ExportReport) for result in results)
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 2)
            self.assertEqual(
                tuple(str(row[9]) for row in refresh_rows),
                ("completed", "completed"),
            )
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_reservation_acquisition_errors_return_preflight_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            artifact_paths = _paths(identity)
            prior_pair = _pair(identity)
            holder_entered = threading.Event()
            release_holder = threading.Event()
            holder_errors: list[BaseException] = []

            def wedged_holder() -> None:
                try:
                    with store.configured_refresh_reservation():
                        holder_entered.set()
                        if not release_holder.wait(10):
                            raise AssertionError(
                                "test never released the holder"
                            )
                except BaseException as error:
                    holder_errors.append(error)

            holder = threading.Thread(target=wedged_holder)
            holder.start()
            self.assertTrue(holder_entered.wait(10))
            with patch(
                "tm_sqlite_store._REFRESH_RESERVATION_TIMEOUT_SECONDS",
                0.05,
            ):
                busy = service.refresh_configured_snapshot(store)
            release_holder.set()
            holder.join(10)
            self.assertFalse(holder.is_alive())
            self.assertEqual(holder_errors, [])
            assert isinstance(busy, ExportFailure)
            self.assertEqual(busy.stage, "REFRESH.PREFLIGHT")
            self.assertEqual(busy.error_code, "STORE.REFRESH_BUSY")
            self.assertTrue(busy.retryable)
            self.assertEqual(
                busy.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertEqual(busy.recovery_locators, ())
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _refresh_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

            for code, retryable in (
                ("STORE.RESOURCE_DRAINING", True),
                ("STORE.CANONICAL_UNAVAILABLE", False),
            ):
                acquisition_error = SQLiteStoreLifecycleError(
                    code,
                    resource_id=identity.resource_id,
                    generation=1,
                    retryable=retryable,
                )
                with patch.object(
                    store,
                    "configured_refresh_reservation",
                    side_effect=acquisition_error,
                ):
                    failure = service.refresh_configured_snapshot(store)
                assert isinstance(failure, ExportFailure)
                self.assertEqual(failure.stage, "REFRESH.PREFLIGHT")
                self.assertEqual(failure.error_code, code)
                self.assertEqual(failure.retryable, retryable)
                self.assertEqual(
                    failure.previous_destination_preservation.state,
                    AssetPreservationState.NOT_APPLICABLE,
                )
                self.assertEqual(failure.recovery_locators, ())
                self.assertEqual(_pair(identity), prior_pair)
                self.assertEqual(
                    _refresh_rows(
                        stage.staged_db_path,
                        identity.configured_jsonl_path,
                    ),
                    (),
                )
                self.assertEqual(
                    _meta_value(stage.staged_db_path, "divergence_latched"),
                    "0",
                )
                for artifact in (
                    artifact_paths.jsonl_temp,
                    artifact_paths.manifest_temp,
                    artifact_paths.jsonl_recovery,
                    artifact_paths.manifest_recovery,
                ):
                    self.assertFalse(artifact.exists(), artifact.name)


class TMRefreshFailureInjectionTests(unittest.TestCase):
    def _jsonl_write_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._stream_export_jsonl_temp

        def failing_stream(
            path: Path,
            records: tuple[object, ...],
            *,
            parent_handle: Any = None,
        ) -> tuple[str, int, Any]:
            with patch(
                "tm_migration.os.write",
                side_effect=OSError("injected jsonl write failure"),
            ):
                return original(
                    path,
                    records,
                    parent_handle=parent_handle,
                )

        return patch(
            "tm_migration._stream_export_jsonl_temp",
            side_effect=failing_stream,
        )

    def _jsonl_fsync_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._fsync_file
        calls: list[int] = []

        def failing_fsync(descriptor: int) -> None:
            calls.append(descriptor)
            if len(calls) == 1:
                raise OSError("injected jsonl fsync failure")
            original(descriptor)

        return patch("tm_migration._fsync_file", side_effect=failing_fsync)

    def _manifest_write_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._write_export_payload_temp

        def failing_manifest_write(
            path: Path,
            payload: bytes,
            *,
            parent_handle: Any = None,
        ) -> Any:
            with patch(
                "tm_migration.os.write",
                side_effect=OSError("injected manifest write failure"),
            ):
                return original(
                    path,
                    payload,
                    parent_handle=parent_handle,
                )

        return patch(
            "tm_migration._write_export_payload_temp",
            side_effect=failing_manifest_write,
        )

    def _manifest_fsync_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._fsync_file
        calls: list[int] = []

        def failing_fsync(descriptor: int) -> None:
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("injected manifest fsync failure")
            original(descriptor)

        return patch("tm_migration._fsync_file", side_effect=failing_fsync)

    def _jsonl_replace_factory(
        self,
        _identity: CanonicalResourceIdentity,
        paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._replace_path

        def failing_replace(
            source: Path,
            target: Path,
            **kwargs: Any,
        ) -> None:
            if source == paths.jsonl_temp:
                raise OSError("injected jsonl replace failure")
            original(source, target, **kwargs)

        return patch("tm_migration._replace_path", side_effect=failing_replace)

    def _manifest_replace_factory(
        self,
        _identity: CanonicalResourceIdentity,
        paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._replace_path

        def failing_replace(
            source: Path,
            target: Path,
            **kwargs: Any,
        ) -> None:
            if source == paths.manifest_temp:
                raise OSError("injected manifest replace failure")
            original(source, target, **kwargs)

        return patch("tm_migration._replace_path", side_effect=failing_replace)

    def _jsonl_dir_fsync_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._fsync_directory
        calls: list[int] = []

        def failing_dir_fsync(
            path: Path,
            *,
            parent_handle: Any = None,
        ) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("injected jsonl directory fsync failure")
            original(path, parent_handle=parent_handle)

        return patch(
            "tm_migration._fsync_directory",
            side_effect=failing_dir_fsync,
        )

    def _manifest_dir_fsync_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        original = tm_migration._fsync_directory
        calls: list[int] = []

        def failing_dir_fsync(
            path: Path,
            *,
            parent_handle: Any = None,
        ) -> None:
            calls.append(1)
            if len(calls) == 2:
                raise OSError("injected manifest directory fsync failure")
            original(path, parent_handle=parent_handle)

        return patch(
            "tm_migration._fsync_directory",
            side_effect=failing_dir_fsync,
        )

    def _pair_verify_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        def failing_verify(
            paths: Any,
            *,
            jsonl_digest: str,
            manifest_bytes: bytes,
            jsonl_identity: tuple[int, int],
            manifest_identity: tuple[int, int],
            parent_handle: Any = None,
        ) -> None:
            raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")

        return patch(
            "tm_migration._verify_export_pair",
            side_effect=failing_verify,
        )

    def _ledger_complete_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        store: SQLiteTMStore,
    ) -> Any:
        def failing_complete(
            snapshot_id: str,
            *,
            expected_generation: int,
            jsonl_identity: tuple[int, int],
            manifest_identity: tuple[int, int],
        ) -> None:
            raise SQLiteStoreLifecycleError(
                "STORE.LEDGER_UNAVAILABLE",
                resource_id="tm.primary",
                generation=expected_generation,
                retryable=True,
            )

        return patch.object(
            store,
            "complete_issued_refresh_receipt",
            side_effect=failing_complete,
        )

    def _recovery_copy_factory(
        self,
        _identity: CanonicalResourceIdentity,
        _paths: Any,
        _store: SQLiteTMStore,
    ) -> Any:
        def failing_recovery_copy(
            paths: Any,
            *,
            destination_before: str | None,
            manifest_before: str | None,
            parent_handle: Any = None,
        ) -> Any:
            raise ExportPreflightError("EXPORT.JSONL_RECOVERY_COPY_FAILED")

        return patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=failing_recovery_copy,
        )

    def _injection_catalog(
        self,
    ) -> tuple[
        tuple[
            str,
            Callable[[CanonicalResourceIdentity, Any, SQLiteTMStore], Any],
            bool,
            str,
            bool,
        ],
        ...,
    ]:
        return (
            ("jsonl_write", self._jsonl_write_factory, False, "EXPORT.JSONL_WRITE_FAILED", False),
            ("jsonl_fsync", self._jsonl_fsync_factory, False, "EXPORT.JSONL_FSYNC_FAILED", False),
            ("manifest_write", self._manifest_write_factory, False, "EXPORT.MANIFEST_WRITE_FAILED", False),
            ("manifest_fsync", self._manifest_fsync_factory, False, "EXPORT.MANIFEST_FSYNC_FAILED", False),
            ("jsonl_replace", self._jsonl_replace_factory, True, "EXPORT.FAILED", False),
            ("jsonl_dir_fsync", self._jsonl_dir_fsync_factory, True, "EXPORT.FAILED", False),
            ("manifest_replace", self._manifest_replace_factory, True, "EXPORT.FAILED", False),
            ("manifest_dir_fsync", self._manifest_dir_fsync_factory, True, "EXPORT.FAILED", False),
            ("pair_verify", self._pair_verify_factory, True, "EXPORT.PUBLISH_VERIFY_FAILED", False),
            ("ledger_complete", self._ledger_complete_factory, True, "STORE.LEDGER_UNAVAILABLE", True),
            ("recovery_copy", self._recovery_copy_factory, True, "EXPORT.JSONL_RECOVERY_COPY_FAILED", False),
        )

    def test_failure_injection_restores_pair_and_cancels_ledger(self) -> None:
        for (
            name,
            factory,
            issued,
            expected_code,
            retryable,
        ) in self._injection_catalog():
            with self.subTest(injection=name):
                self._run_failure_case(
                    factory=factory,
                    issued=issued,
                    expected_code=expected_code,
                    retryable=retryable,
                )

    def _run_failure_case(
        self,
        *,
        factory: Callable[[CanonicalResourceIdentity, Any, SQLiteTMStore], Any],
        issued: bool,
        expected_code: str,
        retryable: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()

            with factory(identity, artifact_paths, store):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, expected_code)
            self.assertEqual(result.stage, "REFRESH.PUBLISH")
            self.assertEqual(result.retryable, retryable)
            self.assertEqual(result.diagnostics, ())
            _assert_safe_failure(self, result, root)
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            for artifact in (
                artifact_paths.jsonl_temp,
                artifact_paths.manifest_temp,
                artifact_paths.jsonl_recovery,
                artifact_paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            if issued:
                refresh_rows = tuple(
                    row
                    for row in _ledger_rows(
                        stage.staged_db_path,
                        identity.configured_jsonl_path,
                    )
                    if str(row[0]).startswith("snapshot.refresh.")
                )
                self.assertEqual(len(refresh_rows), 1)
                self.assertEqual(str(refresh_rows[0][9]), "cancelled")
            else:
                self.assertEqual(
                    _refresh_rows(
                        stage.staged_db_path,
                        identity.configured_jsonl_path,
                    ),
                    (),
                )

            retry = service.refresh_configured_snapshot(store)
            assert isinstance(retry, ExportReport)
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )


class TMRefreshHostileSwapTests(unittest.TestCase):
    def test_same_bytes_foreign_swap_at_complete_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            original_complete = store.complete_issued_refresh_receipt
            swapped = False
            foreign_identity: tuple[int, int] | None = None
            foreign_payload = b""

            def hostile_complete(
                snapshot_id: str,
                *,
                expected_generation: int,
                jsonl_identity: tuple[int, int],
                manifest_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity, foreign_payload
                if not swapped:
                    swapped = True
                    foreign_payload = (
                        identity.configured_jsonl_path.read_bytes()
                    )
                    replacement = identity.configured_jsonl_path.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    replacement.write_bytes(foreign_payload)
                    os.replace(
                        replacement,
                        identity.configured_jsonl_path,
                    )
                    observed = os.lstat(
                        identity.configured_jsonl_path
                    )
                    foreign_identity = (
                        observed.st_dev,
                        observed.st_ino,
                    )
                original_complete(
                    snapshot_id,
                    expected_generation=expected_generation,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=hostile_complete,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            after = os.lstat(identity.configured_jsonl_path)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                foreign_identity,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                foreign_payload,
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")

    def test_same_bytes_foreign_swap_before_pair_verify_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            original_verify = tm_migration._verify_export_pair
            swapped = False
            foreign_identity: tuple[int, int] | None = None
            foreign_payload = b""

            def hostile_verify(
                paths: Any,
                *,
                jsonl_digest: str,
                manifest_bytes: bytes,
                jsonl_identity: tuple[int, int],
                manifest_identity: tuple[int, int],
                parent_handle: Any = None,
            ) -> None:
                nonlocal swapped, foreign_identity, foreign_payload
                if not swapped:
                    swapped = True
                    foreign_payload = paths.destination.read_bytes()
                    replacement = paths.destination.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    replacement.write_bytes(foreign_payload)
                    os.replace(replacement, paths.destination)
                    observed = os.lstat(paths.destination)
                    foreign_identity = (observed.st_dev, observed.st_ino)
                original_verify(
                    paths,
                    jsonl_digest=jsonl_digest,
                    manifest_bytes=manifest_bytes,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )

            with patch(
                "tm_migration._verify_export_pair",
                side_effect=hostile_verify,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            _assert_safe_failure(self, result, root)
            after = os.lstat(identity.configured_jsonl_path)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                foreign_identity,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                foreign_payload,
            )
            self.assertNotEqual(
                identity.configured_jsonl_path.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(len(result.recovery_locators), 1)
            self.assertEqual(
                result.recovery_locators[0].path,
                artifact_paths.jsonl_recovery,
            )
            self.assertEqual(
                result.recovery_locators[0].expected_digest,
                hashlib.sha256(_PRIOR_JSONL).hexdigest(),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")

    def test_same_bytes_foreign_swap_at_identity_proof_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            original_identity = tm_migration._published_file_identity
            swapped = False
            foreign_identity: tuple[int, int] | None = None
            foreign_payload = b""

            def hostile_identity(
                path: Path,
                digest: str,
                *,
                parent_handle: Any = None,
            ) -> tuple[int, int]:
                nonlocal swapped, foreign_identity, foreign_payload
                if not swapped and path == identity.configured_jsonl_path:
                    swapped = True
                    foreign_payload = path.read_bytes()
                    replacement = path.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    replacement.write_bytes(foreign_payload)
                    os.replace(replacement, path)
                    observed = os.lstat(path)
                    foreign_identity = (observed.st_dev, observed.st_ino)
                return original_identity(
                    path,
                    digest,
                    parent_handle=parent_handle,
                )

            with patch(
                "tm_migration._published_file_identity",
                side_effect=hostile_identity,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            after = os.lstat(identity.configured_jsonl_path)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                foreign_identity,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                foreign_payload,
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")

    def test_same_bytes_foreign_manifest_swap_before_pair_verify_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            original_verify = tm_migration._verify_export_pair
            swapped = False
            foreign_identity: tuple[int, int] | None = None
            foreign_payload = b""

            def hostile_verify(
                paths: Any,
                *,
                jsonl_digest: str,
                manifest_bytes: bytes,
                jsonl_identity: tuple[int, int],
                manifest_identity: tuple[int, int],
                parent_handle: Any = None,
            ) -> None:
                nonlocal swapped, foreign_identity, foreign_payload
                if not swapped:
                    swapped = True
                    foreign_payload = paths.manifest.read_bytes()
                    replacement = paths.manifest.with_name(
                        "foreign-same-bytes.manifest"
                    )
                    replacement.write_bytes(foreign_payload)
                    os.replace(replacement, paths.manifest)
                    observed = os.lstat(paths.manifest)
                    foreign_identity = (observed.st_dev, observed.st_ino)
                original_verify(
                    paths,
                    jsonl_digest=jsonl_digest,
                    manifest_bytes=manifest_bytes,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )

            with patch(
                "tm_migration._verify_export_pair",
                side_effect=hostile_verify,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.MANIFEST_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            after = os.lstat(identity.snapshot_manifest_path)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                foreign_identity,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                foreign_payload,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.exists(),
                False,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")

    def test_same_bytes_swap_at_identity_proof_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            original_identity = tm_migration._published_file_identity
            swapped = False

            def hostile_identity(
                path: Path,
                digest: str,
                *,
                parent_handle: Any = None,
            ) -> tuple[int, int]:
                nonlocal swapped
                if not swapped and path == identity.configured_jsonl_path:
                    swapped = True
                    payload = path.read_bytes()
                    replacement = path.with_name("foreign-same-bytes.jsonl")
                    replacement.write_bytes(payload)
                    os.replace(replacement, path)
                    raise ExportPreflightError(
                        "EXPORT.PUBLISH_VERIFY_FAILED"
                    )
                return original_identity(
                    path,
                    digest,
                    parent_handle=parent_handle,
                )

            with patch(
                "tm_migration._published_file_identity",
                side_effect=hostile_identity,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            self.assertNotEqual(
                identity.configured_jsonl_path.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                _pair(identity)[1],
            )
            self.assertEqual(len(result.recovery_locators), 1)
            self.assertEqual(
                result.recovery_locators[0].path,
                artifact_paths.jsonl_recovery,
            )
            self.assertEqual(
                result.recovery_locators[0].asset_kind,
                AssetKind.EXPORT_DESTINATION,
            )
            self.assertEqual(
                result.recovery_locators[0].expected_digest,
                hashlib.sha256(_PRIOR_JSONL).hexdigest(),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, "missing"),
                None,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")

    def test_foreign_swap_during_restore_fails_closed_with_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            artifact_paths = _paths(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            foreign = b'{"source":"foreign","target":"swap"}\n'
            swapped = False
            original_dir_fsync = tm_migration._fsync_directory

            def hostile_dir_fsync(
                path: Path,
                *,
                parent_handle: Any = None,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    identity.configured_jsonl_path.write_bytes(foreign)
                    raise OSError("injected jsonl directory fsync failure")
                original_dir_fsync(path, parent_handle=parent_handle)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=hostile_dir_fsync,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertEqual(result.stage, "REFRESH.RESTORE")
            self.assertEqual(result.error_code, "EXPORT.JSONL_RESTORE_FAILED")
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                foreign,
            )
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(
                evidence.before_digest,
                hashlib.sha256(_PRIOR_JSONL).hexdigest(),
            )
            self.assertEqual(
                evidence.observed_digest,
                hashlib.sha256(foreign).hexdigest(),
            )
            self.assertEqual(len(result.recovery_locators), 1)
            locator = result.recovery_locators[0]
            self.assertEqual(locator.path, artifact_paths.jsonl_recovery)
            self.assertEqual(
                locator.expected_digest,
                hashlib.sha256(_PRIOR_JSONL).hexdigest(),
            )
            self.assertEqual(
                artifact_paths.jsonl_recovery.read_bytes(),
                _PRIOR_JSONL,
            )
            self.assertEqual(
                artifact_paths.manifest_recovery.read_bytes(),
                _pair(identity)[1],
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            refresh_rows = tuple(
                row
                for row in _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                )
                if str(row[0]).startswith("snapshot.refresh.")
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "issued")


class TMRefreshLedgerTests(unittest.TestCase):
    def test_register_requires_exact_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            revision = store.canonical_revision()
            stale = SnapshotReceipt(
                snapshot_id="snapshot.refresh.stale",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision - 1,
                jsonl_digest="a" * 64,
                record_count=revision.record_count,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as stale_error:
                store.register_issued_refresh_receipt(
                    stale,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                stale_error.exception.args[0],
                "STORE.RECEIPT_REVISION_STALE",
            )
            wrong_count = SnapshotReceipt(
                snapshot_id="snapshot.refresh.wrong-count",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest="a" * 64,
                record_count=revision.record_count + 1,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as count_error:
                store.register_issued_refresh_receipt(
                    wrong_count,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                count_error.exception.args[0],
                "STORE.RECEIPT_REVISION_STALE",
            )
            self.assertEqual(
                _refresh_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )

    def test_register_rejects_diverged_and_foreign_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            identity.configured_jsonl_path.write_bytes(
                b'{"source":"tampered","target":"source"}\n'
            )
            _ = store.source_binding_monitor.observe()
            revision = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.diverged",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest="a" * 64,
                record_count=revision.record_count,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as diverged_error:
                store.register_issued_refresh_receipt(
                    receipt,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                diverged_error.exception.args[0],
                "STORE.DIVERGENCE_LATCHED",
            )
            foreign = SnapshotReceipt(
                snapshot_id="snapshot.refresh.foreign",
                resource_id="tm.other",
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest="a" * 64,
                record_count=revision.record_count,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as identity_error:
                store.register_issued_refresh_receipt(
                    foreign,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                identity_error.exception.args[0],
                "STORE.RECEIPT_IDENTITY_MISMATCH",
            )
            with self.assertRaises(SQLiteStoreLifecycleError) as generation_error:
                store.register_issued_refresh_receipt(
                    receipt,
                    expected_generation=revision.generation + 1,
                )
            self.assertEqual(
                generation_error.exception.args[0],
                "STORE.GENERATION_CHANGED",
            )

    def test_complete_transitions_and_rebinds_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            revision = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.atomic",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest=hashlib.sha256(_PRIOR_JSONL).hexdigest(),
                record_count=revision.record_count,
            )
            paths = _paths(identity)
            paths.jsonl_temp.write_bytes(_PRIOR_JSONL)
            paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
            jsonl_temp_identity = _identity_of(paths.jsonl_temp)
            manifest_temp_identity = _identity_of(paths.manifest_temp)
            prior_jsonl = _prior_state(identity.configured_jsonl_path)
            prior_manifest = _prior_state(identity.snapshot_manifest_path)
            store.register_issued_refresh_receipt(
                receipt,
                expected_generation=revision.generation,
                jsonl_temp_identity=jsonl_temp_identity,
                manifest_temp_identity=manifest_temp_identity,
                artifact_parent_identity=_identity_of(
                    identity.configured_jsonl_path.parent
                ),
                prior_jsonl_identity=prior_jsonl[0],
                prior_jsonl_digest=prior_jsonl[1],
                prior_jsonl_absent=prior_jsonl[2],
                prior_manifest_identity=prior_manifest[0],
                prior_manifest_digest=prior_manifest[1],
                prior_manifest_absent=prior_manifest[2],
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            os.replace(paths.jsonl_temp, identity.configured_jsonl_path)
            os.replace(
                paths.manifest_temp,
                identity.snapshot_manifest_path,
            )

            store.complete_issued_refresh_receipt(
                receipt.snapshot_id,
                expected_generation=revision.generation,
            )

            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as stale_error:
                store.complete_issued_refresh_receipt(
                    receipt.snapshot_id,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                stale_error.exception.args[0],
                "STORE.RECEIPT_STALE",
            )

    def test_complete_requires_strict_published_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            revision = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.missing-pair",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest="a" * 64,
                record_count=revision.record_count,
            )
            paths = _paths(identity)
            paths.jsonl_temp.write_bytes(_PRIOR_JSONL)
            paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
            jsonl_temp_identity = _identity_of(paths.jsonl_temp)
            manifest_temp_identity = _identity_of(paths.manifest_temp)
            prior_jsonl = _prior_state(identity.configured_jsonl_path)
            prior_manifest = _prior_state(identity.snapshot_manifest_path)
            store.register_issued_refresh_receipt(
                receipt,
                expected_generation=revision.generation,
                jsonl_temp_identity=jsonl_temp_identity,
                manifest_temp_identity=manifest_temp_identity,
                artifact_parent_identity=_identity_of(
                    identity.configured_jsonl_path.parent
                ),
                prior_jsonl_identity=prior_jsonl[0],
                prior_jsonl_digest=prior_jsonl[1],
                prior_jsonl_absent=prior_jsonl[2],
                prior_manifest_identity=prior_manifest[0],
                prior_manifest_digest=prior_manifest[1],
                prior_manifest_absent=prior_manifest[2],
            )

            with self.assertRaises(SQLiteStoreSchemaError) as pair_error:
                store.complete_issued_refresh_receipt(
                    receipt.snapshot_id,
                    expected_generation=revision.generation,
                )

            self.assertEqual(
                pair_error.exception.args[0],
                "STORE.REFRESH_PAIR_INVALID",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_complete_rejects_foreign_receipt_and_stale_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _ = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            revision = store.canonical_revision()
            destination = (root / "exports").resolve()
            destination.mkdir()
            arbitrary = SnapshotReceipt(
                snapshot_id="snapshot.export.other-destination",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=revision.head_revision,
                jsonl_digest="a" * 64,
                record_count=revision.record_count,
            )
            store.register_issued_export_receipt(
                arbitrary,
                destination_jsonl_path=destination / "elsewhere.jsonl",
                destination_manifest_path=(
                    destination / "elsewhere.jsonl.localcat-snapshot.json"
                ),
                expected_generation=revision.generation,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as path_error:
                store.complete_issued_refresh_receipt(
                    arbitrary.snapshot_id,
                    expected_generation=revision.generation,
                )
            self.assertEqual(
                path_error.exception.args[0],
                "STORE.RECEIPT_PATH_MISMATCH",
            )

            _ = store.append(_draft("newer", "appended"))
            later = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.stale-complete",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=later.head_revision,
                jsonl_digest="a" * 64,
                record_count=later.record_count,
            )
            store.register_issued_refresh_receipt(
                receipt,
                expected_generation=later.generation,
            )
            _ = store.append(_draft("newer2", "appended again"))
            with self.assertRaises(SQLiteStoreSchemaError) as stale_error:
                store.complete_issued_refresh_receipt(
                    receipt.snapshot_id,
                    expected_generation=later.generation,
                )
            self.assertEqual(
                stale_error.exception.args[0],
                "STORE.RECEIPT_REVISION_STALE",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )


if __name__ == "__main__":
    unittest.main()
