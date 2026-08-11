"""Task 5.12 arbitrary-path JSONL export (``export_jsonl``) tests."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
import tm_snapshot_recovery
import tm_sqlite_store
from tm_activation_journal import (
    _activation_journal_path,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_lineage_marker_temp_path,
    _activation_terminal_path,
    _activation_terminal_temp_path,
)
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    ExportFailure,
    ExportReport,
    SNAPSHOT_MANIFEST_VERSION,
    AssetKind,
    AssetPreservationState,
    CanonicalResourceIdentity,
    MutableStageRef,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    TMRecordDraft,
    contract_from_json,
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
_EXPORT_ROW_KEYS = frozenset(
    {
        "record_id",
        "source",
        "target",
        "speaker",
        "context_prev",
        "context_next",
        "file_source",
        "provenance",
        "legacy_line_no",
        "usage_count",
        "last_used",
        "origin_batch_id",
        "origin_ordinal",
    }
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


def _destination(root: Path, name: str = "export.jsonl") -> Path:
    directory = (root / "exports").resolve()
    directory.mkdir(exist_ok=True)
    return directory / name


def _prepared_store(
    root: Path,
) -> tuple[MutableStageRef, SQLiteTMStore]:
    """One mutable stage seeded with every supported draft variant."""

    stage = _stage(root)
    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        store = SQLiteTMStore(stage, canonical_store_id="store.primary")
    _ = store.append_batch(
        batch_id="migration.seed.export",
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


def _set_usage(store_path: Path, record_id: int, *, count: int, last_used: str) -> None:
    connection = sqlite3.connect(store_path)
    try:
        connection.execute(
            "UPDATE tm_record SET usage_count = ?, last_used = ? "
            "WHERE record_id = ?",
            (count, last_used, record_id),
        )
        connection.commit()
    finally:
        connection.close()


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


def _ledger_rows(
    store_path: Path,
    destination: Path | None = None,
) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(store_path)
    try:
        if destination is None:
            rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version, destination_jsonl_path, "
                "destination_manifest_path, status "
                "FROM tm_snapshot_receipt"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version, destination_jsonl_path, "
                "destination_manifest_path, status "
                "FROM tm_snapshot_receipt "
                "WHERE destination_jsonl_path = ?",
                (str(destination),),
            ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


def _ledger_status(store_path: Path, destination: Path) -> str | None:
    rows = _ledger_rows(store_path, destination)
    if not rows:
        return None
    statuses = tuple(str(row[9]) for row in rows)
    if len(set(statuses)) != 1:
        raise AssertionError(f"ledger statuses disagree: {statuses}")
    return statuses[0]


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


def _ledger_receipt(
    store: SQLiteTMStore,
    *,
    snapshot_id: str,
    exported_revision: int | None = None,
    record_count: int | None = None,
    canonical_store_id: str | None = None,
    resource_id: str | None = None,
) -> SnapshotReceipt:
    revision = store.capture_export_snapshot().revision
    return SnapshotReceipt(
        snapshot_id=snapshot_id,
        resource_id=(
            revision.resource_id if resource_id is None else resource_id
        ),
        canonical_store_id=(
            revision.canonical_store_id
            if canonical_store_id is None
            else canonical_store_id
        ),
        exported_revision=(
            revision.head_revision
            if exported_revision is None
            else exported_revision
        ),
        jsonl_digest="a" * 64,
        record_count=(
            revision.record_count if record_count is None else record_count
        ),
    )


def _write_ledger_pair(
    store: SQLiteTMStore,
    *,
    snapshot_id: str,
    destination: Path,
    manifest: Path,
    payload: bytes = b'{"source":"ledger","target":"pair"}\n',
) -> tuple[SnapshotReceipt, tuple[int, int], tuple[int, int]]:
    """Write a real destination pair and register one issued receipt.

    Returns the receipt plus the exact published identities so the
    caller can pass them to ``complete_issued_export_receipt``.  The
    registration records a durable handoff journal whose temporary
    identities are the pair identities and whose prior record is the
    explicit proven absence of the fresh destination pair.
    """

    revision = store.capture_export_snapshot().revision
    receipt = SnapshotReceipt(
        snapshot_id=snapshot_id,
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(payload).hexdigest(),
        record_count=revision.record_count,
    )
    manifest_bytes = contract_to_json(
        SnapshotManifest(
            manifest_version=SNAPSHOT_MANIFEST_VERSION,
            snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
            receipt=receipt,
            receipt_digest=snapshot_receipt_digest(receipt),
        )
    ).encode("utf-8")
    paths = _export_artifact_paths(destination)
    paths.jsonl_temp.write_bytes(payload)
    paths.manifest_temp.write_bytes(manifest_bytes)
    jsonl_temp_identity = _identity_of(paths.jsonl_temp)
    manifest_temp_identity = _identity_of(paths.manifest_temp)
    store.register_issued_export_receipt(
        receipt,
        destination_jsonl_path=destination,
        destination_manifest_path=manifest,
        expected_generation=revision.generation,
        jsonl_temp_identity=jsonl_temp_identity,
        manifest_temp_identity=manifest_temp_identity,
        artifact_parent_identity=_identity_of(destination.parent),
        prior_jsonl_absent=True,
        prior_manifest_absent=True,
    )
    os.replace(paths.jsonl_temp, destination)
    os.replace(paths.manifest_temp, manifest)
    jsonl_identity = (
        os.lstat(destination).st_dev,
        os.lstat(destination).st_ino,
    )
    manifest_identity = (
        os.lstat(manifest).st_dev,
        os.lstat(manifest).st_ino,
    )
    return receipt, jsonl_identity, manifest_identity


def _identity_of(path: Path) -> tuple[int, int]:
    """The exact device/inode identity of one existing entry."""

    observed = os.lstat(path)
    return (observed.st_dev, observed.st_ino)


def _record_fields(record: Any) -> tuple[object, ...]:
    return (
        record.source_raw,
        record.target_raw,
        record.speaker_raw,
        record.context_prev_raw,
        record.context_next_raw,
        record.file_source,
        record.provenance,
    )


def _assert_safe_failure(self: Any, failure: Any, root: Path) -> None:
    self.assertRegex(failure.error_code, _IDENTIFIER)
    self.assertRegex(failure.stage, _IDENTIFIER)
    for diagnostic in failure.diagnostics:
        self.assertRegex(diagnostic.code, _IDENTIFIER)
        self.assertRegex(diagnostic.safe_summary, _IDENTIFIER)
    root_text = str(root)
    self.assertNotIn(root_text, failure.error_code)
    self.assertNotIn(root_text, failure.stage)


class TMExportSuccessTests(unittest.TestCase):
    def test_export_publishes_complete_deterministic_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            usage_record = store.capture_export_snapshot().records[1]
            _set_usage(
                stage.staged_db_path,
                usage_record.record.record_id,
                count=7,
                last_used="2026-08-01T00:00:00+00:00",
            )
            generation_before = store.coordinator.current_generation
            revision_before = store.canonical_revision()

            result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportReport)
            self.assertEqual(result.skipped_count, 0)
            self.assertEqual(result.diagnostics, ())
            self.assertEqual(result.canonical_generation, generation_before)
            self.assertEqual(
                result.exported_revision,
                revision_before.head_revision,
            )
            self.assertEqual(result.exported_count, revision_before.record_count)
            self.assertEqual(
                result.destination_digest,
                hashlib.sha256(destination.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result.snapshot_receipt_digest,
                snapshot_receipt_digest(result.snapshot_receipt),
            )
            self.assertEqual(
                result.snapshot_receipt.jsonl_digest,
                result.destination_digest,
            )
            self.assertEqual(
                result.snapshot_receipt.record_count,
                result.exported_count,
            )
            self.assertEqual(
                result.snapshot_receipt.exported_revision,
                result.exported_revision,
            )
            self.assertEqual(
                result.snapshot_receipt.snapshot_id,
                result.snapshot_id,
            )
            self.assertRegex(result.snapshot_id, r"snapshot\.export\.[0-9a-f]{32}")

            manifest = contract_from_json(
                paths.manifest.read_text(encoding="utf-8")
            )
            assert isinstance(manifest, SnapshotManifest)
            self.assertIs(manifest.snapshot_kind, SnapshotKind.EXPLICIT_EXPORT)
            self.assertEqual(manifest.receipt, result.snapshot_receipt)
            self.assertEqual(
                manifest.receipt_digest,
                result.snapshot_receipt_digest,
            )

            snapshot = store.capture_export_snapshot()
            rows = [
                json.loads(line)
                for line in destination.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), snapshot.revision.record_count)
            record_ids = [int(row["record_id"]) for row in rows]
            self.assertEqual(record_ids, sorted(record_ids))
            self.assertEqual(
                record_ids,
                [item.record.record_id for item in snapshot.records],
            )
            for row, item in zip(rows, snapshot.records, strict=True):
                self.assertEqual(set(row), _EXPORT_ROW_KEYS)
                record = item.record
                self.assertEqual(row["record_id"], record.record_id)
                self.assertEqual(row["source"], record.source_raw)
                self.assertEqual(row["target"], record.target_raw)
                self.assertEqual(row["speaker"], record.speaker_raw)
                self.assertEqual(row["context_prev"], record.context_prev_raw)
                self.assertEqual(row["context_next"], record.context_next_raw)
                self.assertEqual(row["file_source"], record.file_source)
                self.assertEqual(
                    row["provenance"],
                    [[key, value] for key, value in record.provenance],
                )
                self.assertEqual(row["legacy_line_no"], record.legacy_line_no)
                self.assertEqual(row["usage_count"], item.usage_count)
                self.assertEqual(row["last_used"], item.last_used)
                self.assertEqual(
                    row["origin_batch_id"],
                    record.origin_batch_id,
                )
                self.assertEqual(row["origin_ordinal"], record.origin_ordinal)

            self.assertEqual(
                _ledger_rows(stage.staged_db_path, destination),
                (
                    (
                        result.snapshot_id,
                        identity.resource_id,
                        "store.primary",
                        result.exported_revision,
                        result.destination_digest,
                        result.exported_count,
                        SNAPSHOT_FORMAT_VERSION,
                        str(destination),
                        str(paths.manifest),
                        "completed",
                    ),
                ),
            )
            self.assertEqual(store.coordinator.current_generation, generation_before)
            self.assertEqual(
                store.canonical_revision().head_revision,
                revision_before.head_revision,
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                binding_count = connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_binding"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(binding_count, 0)

    def test_export_is_deterministic_across_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            first_destination = _destination(root, "first.jsonl")
            second_destination = _destination(root, "second.jsonl")

            first = service.export_jsonl(store, first_destination)
            second = service.export_jsonl(store, second_destination)

            assert isinstance(first, ExportReport)
            assert isinstance(second, ExportReport)
            self.assertEqual(first.destination_digest, second.destination_digest)
            self.assertEqual(first.exported_count, second.exported_count)
            self.assertEqual(first.exported_revision, second.exported_revision)
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(
                first_destination.read_bytes(),
                second_destination.read_bytes(),
            )
            first_manifest = contract_from_json(
                _export_artifact_paths(first_destination)
                .manifest.read_text(encoding="utf-8")
            )
            second_manifest = contract_from_json(
                _export_artifact_paths(second_destination)
                .manifest.read_text(encoding="utf-8")
            )
            assert isinstance(first_manifest, SnapshotManifest)
            assert isinstance(second_manifest, SnapshotManifest)
            self.assertEqual(first_manifest.snapshot_kind, SnapshotKind.EXPLICIT_EXPORT)
            self.assertEqual(
                first_manifest.receipt.jsonl_digest,
                second_manifest.receipt.jsonl_digest,
            )
            self.assertEqual(
                first_manifest.receipt.record_count,
                second_manifest.receipt.record_count,
            )
            self.assertEqual(
                first_manifest.receipt.exported_revision,
                second_manifest.receipt.exported_revision,
            )
            self.assertEqual(
                first_manifest.receipt.resource_id,
                second_manifest.receipt.resource_id,
            )
            self.assertEqual(
                first_manifest.receipt.canonical_store_id,
                second_manifest.receipt.canonical_store_id,
            )
            self.assertNotEqual(
                first_manifest.receipt.snapshot_id,
                second_manifest.receipt.snapshot_id,
            )

    def test_repeated_export_mints_fresh_snapshot_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)

            first = service.export_jsonl(store, destination)
            second = service.export_jsonl(store, destination)

            assert isinstance(first, ExportReport)
            assert isinstance(second, ExportReport)
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(
                first.destination_digest,
                second.destination_digest,
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {str(row[9]) for row in rows},
                {"completed"},
            )

    def test_export_requires_exact_store_and_path_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            with self.assertRaises(TypeError):
                service.export_jsonl(
                    cast(Any, object()),
                    destination,
                )
            with self.assertRaises(TypeError):
                service.export_jsonl(
                    store,
                    cast(Any, str(destination)),
                )

    def test_export_rejects_store_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            foreign_service = TMMigrationService(
                resource_identity=stage.resource_identity,
                canonical_store_id="store.foreign",
            )
            destination = _destination(root)

            result = foreign_service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
            self.assertEqual(
                result.error_code,
                "EXPORT.STORE_IDENTITY_MISMATCH",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                result.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertFalse(destination.exists())
            self._assert_no_artifacts(_export_artifact_paths(destination))

    def _assert_no_artifacts(self, paths: Any) -> None:
        for artifact in (
            paths.jsonl_temp,
            paths.manifest_temp,
            paths.jsonl_recovery,
            paths.manifest_recovery,
        ):
            self.assertFalse(artifact.exists())


class TMExportRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_fields_provenance_and_exact_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = _destination(root)
            result = _service(stage.resource_identity).export_jsonl(
                store,
                destination,
            )
            assert isinstance(result, ExportReport)
            original = store.capture_export_snapshot().records

            parity_directory = (root / "parity").resolve()
            parity_directory.mkdir()
            parity_source = parity_directory / "source.jsonl"
            parity_source.write_bytes(destination.read_bytes())
            migrated_identity = CanonicalResourceIdentity.from_configured_jsonl(
                "tm.parity",
                parity_source,
            )
            migrated_service = TMMigrationService(
                resource_identity=migrated_identity,
                canonical_store_id="store.parity",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                build = migrated_service.build_mutable_stage(parity_source)
                self.assertIsNotNone(build.mutable_stage)
                assert build.mutable_stage is not None
                migrated_store = SQLiteTMStore(
                    build.mutable_stage,
                    canonical_store_id="store.parity",
                )

            self.assertEqual(
                tuple(
                    _record_fields(record)
                    for record in migrated_store.export_records()
                ),
                tuple(
                    _record_fields(item.record)
                    for item in original
                ),
            )
            self.assertEqual(
                _record_fields(migrated_store.exact_records("same")[0]),
                _record_fields(store.exact_records("same")[0]),
            )
            self.assertEqual(
                tuple(
                    record.source_raw
                    for record in migrated_store.exact_records("Straße")
                ),
                ("Straße",),
            )
            self.assertEqual(
                tuple(
                    record.provenance
                    for record in migrated_store.exact_records("Straße")
                ),
                ((
                    ("batch", "seed"),
                    ("source", "legacy-jsonl"),
                ),),
            )


class TMExportDivergenceTests(unittest.TestCase):
    def test_export_leaves_divergence_latch_and_configured_pair_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(
                store,
                stage,
                b'{"source":"bound","target":"pair"}\n',
            )
            monitor = store.source_binding_monitor
            self.assertEqual(
                monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            configured = identity.configured_jsonl_path
            tampered = b'{"source":"tampered","target":"source"}\n'
            configured.write_bytes(tampered)
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            diverged = monitor.observe()
            self.assertEqual(diverged.state, SourceBindingState.SOURCE_DIVERGED)
            generation_before = store.coordinator.current_generation
            head_before = store.canonical_revision().head_revision

            result = service.export_jsonl(store, _destination(root))

            assert isinstance(result, ExportReport)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                latched = connection.execute(
                    "SELECT value FROM tm_meta "
                    "WHERE key = 'divergence_latched'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(latched, ("1",))
            self.assertEqual(configured.read_bytes(), tampered)
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(store.coordinator.current_generation, generation_before)
            self.assertEqual(
                store.canonical_revision().head_revision,
                head_before,
            )
            after = monitor.observe()
            self.assertEqual(after.state, SourceBindingState.SOURCE_DIVERGED)
            self.assertEqual(
                after.diagnostic_codes,
                ("SOURCE_BINDING.DIVERGENCE_LATCHED",),
            )

    def test_damaged_export_destination_never_affects_configured_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _ = _bind_current_snapshot(
                store,
                stage,
                b'{"source":"bound","target":"pair"}\n',
            )
            configured = identity.configured_jsonl_path
            configured.write_bytes(b'{"source":"tampered","target":"source"}\n')
            diverged = store.source_binding_monitor.observe()
            self.assertEqual(diverged.state, SourceBindingState.SOURCE_DIVERGED)

            destination = _destination(root)
            result = service.export_jsonl(store, destination)
            assert isinstance(result, ExportReport)
            destination.write_bytes(b'{"source":"foreign","target":"damage"}\n')
            destination.unlink()
            _export_artifact_paths(destination).manifest.unlink()

            still = store.source_binding_monitor.observe()
            self.assertEqual(still.state, SourceBindingState.SOURCE_DIVERGED)
            self.assertEqual(
                still.diagnostic_codes,
                ("SOURCE_BINDING.DIVERGENCE_LATCHED",),
            )
            self.assertEqual(
                configured.read_bytes(),
                b'{"source":"tampered","target":"source"}\n',
            )


class TMExportSnapshotIsolationTests(unittest.TestCase):
    def test_export_pins_one_stable_revision_under_concurrent_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            before = store.capture_export_snapshot()
            capture_done = threading.Event()
            append_done = threading.Event()
            original_stream = tm_migration._stream_export_jsonl_temp

            def interleave_stream(
                path: Path,
                records: tuple[object, ...],
                *,
                parent_handle: Any = None,
            ) -> tuple[str, int, Any]:
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
                    raise AssertionError("export never captured its snapshot")
                _ = store.append(_draft("concurrent", "appended during export"))
                append_done.set()

            thread = threading.Thread(target=appender)
            thread.start()
            try:
                with patch(
                    "tm_migration._stream_export_jsonl_temp",
                    side_effect=interleave_stream,
                ):
                    result = service.export_jsonl(store, destination)
            finally:
                thread.join(10)
            self.assertFalse(thread.is_alive())

            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_revision, before.revision.head_revision)
            self.assertEqual(result.exported_count, before.revision.record_count)
            rows = [
                json.loads(line)
                for line in destination.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), before.revision.record_count)
            self.assertTrue(all(row["source"] != "concurrent" for row in rows))
            after = store.capture_export_snapshot()
            self.assertEqual(
                after.revision.head_revision,
                before.revision.head_revision + 1,
            )
            self.assertEqual(
                after.revision.record_count,
                before.revision.record_count + 1,
            )
            self.assertEqual(_ledger_status(stage.staged_db_path, destination), "completed")


class TMExportPreflightTests(unittest.TestCase):
    def test_export_fails_closed_on_authority_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            journal = _activation_journal_path(identity)
            marker = _activation_lineage_marker_path(identity)
            terminal = _activation_terminal_path(identity)
            configured = _export_artifact_paths(
                identity.configured_jsonl_path
            )
            cases = (
                (identity.configured_jsonl_path, "EXPORT.PATH_ALIASED"),
                (identity.snapshot_manifest_path, "EXPORT.PATH_ALIASED"),
                (identity.canonical_sidecar_path, "EXPORT.PATH_ALIASED"),
                (journal, "EXPORT.PATH_ALIASED"),
                (_activation_journal_temp_path(journal), "EXPORT.PATH_ALIASED"),
                (marker, "EXPORT.PATH_ALIASED"),
                (_activation_lineage_marker_temp_path(marker), "EXPORT.PATH_ALIASED"),
                (terminal, "EXPORT.PATH_ALIASED"),
                (_activation_terminal_temp_path(terminal), "EXPORT.PATH_ALIASED"),
                (configured.jsonl_temp, "EXPORT.PATH_ALIASED"),
                (configured.manifest_temp, "EXPORT.PATH_ALIASED"),
                (configured.jsonl_recovery, "EXPORT.PATH_ALIASED"),
                (configured.manifest_recovery, "EXPORT.PATH_ALIASED"),
            )
            for destination, expected_code in cases:
                with self.subTest(destination=destination.name):
                    result = service.export_jsonl(store, destination)
                    assert isinstance(result, ExportFailure)
                    self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
                    self.assertEqual(result.error_code, expected_code)
                    self.assertFalse(result.retryable)
                    self.assertEqual(
                        result.previous_destination_preservation.state,
                        AssetPreservationState.NOT_APPLICABLE,
                    )
                    self.assertEqual(result.recovery_locators, ())
                    _assert_safe_failure(self, result, root)

    def test_export_fails_closed_on_authority_family_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            sidecar = identity.canonical_sidecar_path
            cases = (
                root / f".{sidecar.name}.localcat-anything.jsonl",
                root / f".localcat-{identity.target_identity[:16]}-x.jsonl",
                root / f".localcat-{identity.target_identity[:16]}",
            )
            for destination in cases:
                with self.subTest(destination=destination.name):
                    result = service.export_jsonl(store, destination)
                    assert isinstance(result, ExportFailure)
                    self.assertEqual(result.error_code, "EXPORT.PATH_ALIASED")
                    self.assertEqual(result.stage, "EXPORT.PREFLIGHT")

    def test_export_fails_closed_on_unsafe_destination_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            exports = (root / "exports").resolve()
            exports.mkdir()
            directory_destination = exports / "directory.jsonl"
            directory_destination.mkdir()
            plain = exports / "plain.jsonl"
            plain.write_bytes(b'{"source":"real","target":"file"}\n')
            symlink_destination = exports / "symlink-destination.jsonl"
            symlink_destination.symlink_to(plain)
            manifest_symlink = exports / "manifest-symlink.jsonl"
            _export_artifact_paths(manifest_symlink).manifest.symlink_to(plain)
            manifest_directory = exports / "manifest-directory.jsonl"
            _export_artifact_paths(manifest_directory).manifest.mkdir()
            relative = Path("relative/export.jsonl")
            dotdot = Path(str(root) + "/../dotdot.jsonl")
            missing_parent = (root / "missing" / "export.jsonl").resolve()
            symlink_parent = root / "symlink-parent"
            symlink_parent.symlink_to(exports, target_is_directory=True)
            parent_symlink_destination = symlink_parent / "via-link.jsonl"
            cases = (
                (directory_destination, "EXPORT.DESTINATION_UNSAFE"),
                (symlink_destination, "EXPORT.PRIOR_STATE_UNRECOVERABLE"),
                (manifest_symlink, "EXPORT.MANIFEST_UNSAFE"),
                (manifest_directory, "EXPORT.MANIFEST_UNSAFE"),
                (relative, "EXPORT.PATH_INVALID"),
                (dotdot, "EXPORT.PATH_INVALID"),
                (missing_parent, "EXPORT.PARENT_UNSAFE"),
                (parent_symlink_destination, "EXPORT.PARENT_UNSAFE"),
            )
            for destination, expected_code in cases:
                with self.subTest(destination=str(destination)):
                    if expected_code == "EXPORT.PRIOR_STATE_UNRECOVERABLE":
                        with self.assertRaises(ExportPreflightError) as fail_stop:
                            service.export_jsonl(store, destination)
                        self.assertEqual(
                            fail_stop.exception.error_code,
                            "EXPORT.PRIOR_STATE_UNRECOVERABLE",
                        )
                    else:
                        result = service.export_jsonl(store, destination)
                        assert isinstance(result, ExportFailure)
                        self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
                        self.assertEqual(result.error_code, expected_code)
                        self.assertFalse(result.retryable)

    def test_export_fails_closed_on_artifact_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            cases = (
                (paths.jsonl_temp, "EXPORT.TEMP_CONFLICT"),
                (paths.manifest_temp, "EXPORT.TEMP_CONFLICT"),
                (paths.jsonl_recovery, "EXPORT.RECOVERY_CONFLICT"),
                (paths.manifest_recovery, "EXPORT.RECOVERY_CONFLICT"),
            )
            for artifact, expected_code in cases:
                with self.subTest(artifact=artifact.name):
                    artifact.write_bytes(b'{"source":"stale","target":"artifact"}\n')
                    try:
                        result = service.export_jsonl(store, destination)
                    finally:
                        artifact.unlink()
                    assert isinstance(result, ExportFailure)
                    self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
                    self.assertEqual(result.error_code, expected_code)

    def test_export_fails_closed_on_asymmetric_prior_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            paths.manifest.write_bytes(b'{"manifest":"orphan"}\n')
            try:
                result = service.export_jsonl(store, destination)
            finally:
                paths.manifest.unlink()
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.PAIR_INCONSISTENT")
            self.assertEqual(result.stage, "EXPORT.PREFLIGHT")

    def test_export_preflight_failures_never_leave_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            paths.jsonl_recovery.write_bytes(b'{"source":"prior","target":"x"}\n')
            try:
                result = service.export_jsonl(store, destination)
            finally:
                paths.jsonl_recovery.unlink()
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.RECOVERY_CONFLICT")
            for artifact in (
                paths.destination,
                paths.manifest,
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists())

    def test_export_bind_ancestor_symlink_swap_fails_closed(self) -> None:
        """P1 B bind-time ancestor-symlink race.

        The full real parent chain is validated, then an ancestor of the
        immediate parent is renamed aside and replaced by a symlink into
        an attacker tree exactly at the late-bound seam between the
        validation and the component-wise ``O_DIRECTORY|O_NOFOLLOW``
        descriptor binding.  The component walk must fail closed on the
        symlinked ancestor: the export returns ``EXPORT.PARENT_UNSAFE``
        and never creates any deterministic temp or canonical bytes in
        the attacker tree, and no ledger row or handoff is left behind.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            exports = (root / "exports").resolve()
            sub = exports / "sub"
            sub.mkdir(parents=True)
            destination = sub / "out.jsonl"
            attacker = (root / "attacker").resolve()
            attacker.mkdir()
            marker = attacker / "marker.txt"
            marker.write_bytes(b"attacker marker\n")
            original_seam = tm_migration._after_export_parent_chain_validated
            renamed_exports = (root / "exports-real").resolve()

            def swap_ancestor_to_symlink(destination_path: Path) -> None:
                original_seam(destination_path)
                if destination_path != destination:
                    return
                exports.rename(renamed_exports)
                os.symlink(attacker, exports)

            with patch(
                "tm_migration._after_export_parent_chain_validated",
                side_effect=swap_ancestor_to_symlink,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.PARENT_UNSAFE")
            self.assertEqual(result.stage, "EXPORT.PUBLISH")
            self.assertFalse(result.retryable)
            self.assertTrue(os.path.islink(exports))
            self.assertEqual(os.readlink(exports), str(attacker))
            self.assertEqual(
                sorted(path.name for path in attacker.iterdir()),
                ["marker.txt"],
            )
            self.assertEqual(marker.read_bytes(), b"attacker marker\n")
            self.assertEqual(
                sorted(path.name for path in (renamed_exports / "sub").iterdir()),
                [],
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_receipt "
                    "WHERE status = 'issued'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], 0)
            self.assertIsNone(
                _meta_value(stage.staged_db_path, "artifact_handoff")
            )

    def test_embedded_nul_path_returns_stable_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = Path(f"{root}/bad\0name.jsonl")

            result = _service(stage.resource_identity).export_jsonl(
                store,
                destination,
            )

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "EXPORT.PREFLIGHT")
            self.assertEqual(result.error_code, "EXPORT.DESTINATION_UNSAFE")
            self.assertEqual(
                result.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )


class TMExportFailureInjectionTests(unittest.TestCase):
    def _jsonl_write_factory(
        self,
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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
        _destination: Path,
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

    def _ledger_complete_factory(
        self,
        _destination: Path,
        _paths: Any,
        store: SQLiteTMStore,
    ) -> Any:
        def failing_complete(
            snapshot_id: str,
            *,
            expected_generation: int,
            jsonl_identity: tuple[int, int] | None = None,
            manifest_identity: tuple[int, int] | None = None,
        ) -> None:
            raise SQLiteStoreLifecycleError(
                "STORE.LEDGER_UNAVAILABLE",
                resource_id="tm.primary",
                generation=expected_generation,
                retryable=True,
            )

        return patch.object(
            store,
            "complete_issued_export_receipt",
            side_effect=failing_complete,
        )

    def _recovery_copy_factory(
        self,
        _destination: Path,
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
            raise ExportPreflightError(
                "EXPORT.JSONL_RECOVERY_COPY_FAILED"
            )

        return patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=failing_recovery_copy,
        )

    def _injection_catalog(
        self,
    ) -> tuple[
        tuple[str, Callable[[Path, Any, SQLiteTMStore], Any], bool, str, frozenset[str], bool],
        ...,
    ]:
        return (
            (
                "jsonl_write",
                self._jsonl_write_factory,
                False,
                "EXPORT.JSONL_WRITE_FAILED",
                frozenset(),
                False,
            ),
            (
                "jsonl_fsync",
                self._jsonl_fsync_factory,
                False,
                "EXPORT.JSONL_FSYNC_FAILED",
                frozenset(),
                False,
            ),
            (
                "manifest_write",
                self._manifest_write_factory,
                False,
                "EXPORT.MANIFEST_WRITE_FAILED",
                frozenset(),
                False,
            ),
            (
                "manifest_fsync",
                self._manifest_fsync_factory,
                False,
                "EXPORT.MANIFEST_FSYNC_FAILED",
                frozenset(),
                False,
            ),
            (
                "jsonl_replace",
                self._jsonl_replace_factory,
                True,
                "EXPORT.FAILED",
                frozenset(),
                False,
            ),
            (
                "jsonl_dir_fsync",
                self._jsonl_dir_fsync_factory,
                True,
                "EXPORT.FAILED",
                frozenset(),
                False,
            ),
            (
                "manifest_replace",
                self._manifest_replace_factory,
                True,
                "EXPORT.FAILED",
                frozenset(),
                False,
            ),
            (
                "manifest_dir_fsync",
                self._manifest_dir_fsync_factory,
                True,
                "EXPORT.FAILED",
                frozenset(),
                False,
            ),
            (
                "ledger_complete",
                self._ledger_complete_factory,
                True,
                "STORE.LEDGER_UNAVAILABLE",
                frozenset(),
                True,
            ),
            (
                "recovery_copy",
                self._recovery_copy_factory,
                True,
                "EXPORT.JSONL_RECOVERY_COPY_FAILED",
                frozenset(),
                False,
            ),
        )

    def test_failure_injection_restores_prior_pair_and_cancels_ledger(
        self,
    ) -> None:
        for (
            name,
            factory,
            issued,
            expected_code,
            leftover_names,
            retryable,
        ) in self._injection_catalog():
            for prior in (True, False):
                with self.subTest(injection=name, prior_pair=prior):
                    self._run_failure_case(
                        factory=factory,
                        issued=issued,
                        expected_code=expected_code,
                        leftover_names=leftover_names,
                        retryable=retryable,
                        prior=prior,
                    )

    def _run_failure_case(
        self,
        *,
        factory: Callable[[Path, Any, SQLiteTMStore], Any],
        issued: bool,
        expected_code: str,
        leftover_names: frozenset[str],
        retryable: bool,
        prior: bool,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            if prior:
                destination.write_bytes(prior_jsonl)
                paths.manifest.write_bytes(prior_manifest)

            with factory(destination, paths, store):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, expected_code)
            self.assertEqual(result.stage, "EXPORT.PUBLISH")
            self.assertEqual(result.retryable, retryable)
            self.assertEqual(result.diagnostics, ())
            _assert_safe_failure(self, result, root)
            if prior:
                self.assertEqual(destination.read_bytes(), prior_jsonl)
                self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
                self.assertEqual(
                    result.previous_destination_preservation.state,
                    AssetPreservationState.VERIFIED_UNCHANGED,
                )
                self.assertEqual(
                    result.previous_destination_preservation.before_digest,
                    hashlib.sha256(prior_jsonl).hexdigest(),
                )
                self.assertEqual(
                    result.previous_destination_preservation.observed_digest,
                    hashlib.sha256(prior_jsonl).hexdigest(),
                )
            else:
                self.assertFalse(destination.exists())
                self.assertFalse(paths.manifest.exists())
                self.assertEqual(
                    result.previous_destination_preservation.state,
                    AssetPreservationState.NOT_APPLICABLE,
                )
            self.assertEqual(result.recovery_locators, ())
            for artifact_name, artifact in (
                ("jsonl_temp", paths.jsonl_temp),
                ("manifest_temp", paths.manifest_temp),
                ("jsonl_recovery", paths.jsonl_recovery),
                ("manifest_recovery", paths.manifest_recovery),
            ):
                self.assertEqual(
                    artifact.exists(),
                    artifact_name in leftover_names,
                    artifact_name,
                )
            if issued:
                self.assertEqual(
                    _ledger_status(stage.staged_db_path, destination),
                    "cancelled",
                )
            else:
                self.assertEqual(
                    _ledger_rows(stage.staged_db_path, destination),
                    (),
                )
            if leftover_names:
                if prior:
                    with self.assertRaises(ExportPreflightError) as fail_stop:
                        service.export_jsonl(store, destination)
                    self.assertEqual(
                        fail_stop.exception.error_code,
                        "EXPORT.PRIOR_STATE_UNRECOVERABLE",
                    )
                else:
                    retry = service.export_jsonl(store, destination)
                    assert isinstance(retry, ExportFailure)
                    self.assertEqual(retry.error_code, "EXPORT.TEMP_CONFLICT")

    def test_second_recovery_copy_failure_cleans_first_owned_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_copy = tm_migration._copy_export_recovery_file
            calls = 0

            def failing_second_copy(
                source: Path,
                recovery: Path,
                *,
                expected_digest: str,
                code: str,
                parent_handle: Any = None,
            ) -> Any:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ExportPreflightError(
                        "EXPORT.MANIFEST_RECOVERY_COPY_FAILED"
                    )
                return original_copy(
                    source,
                    recovery,
                    expected_digest=expected_digest,
                    code=code,
                    parent_handle=parent_handle,
                )

            with patch(
                "tm_migration._copy_export_recovery_file",
                side_effect=failing_second_copy,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(
                result.error_code,
                "EXPORT.MANIFEST_RECOVERY_COPY_FAILED",
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "cancelled",
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists())
            retry = service.export_jsonl(store, destination)
            self.assertIsInstance(retry, ExportReport)

    def test_initial_temp_fstat_failure_recovers_identity_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            original_fstat = tm_migration.os.fstat
            calls = 0

            def flaky_fstat(descriptor: int) -> os.stat_result:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected initial temp fstat failure")
                return original_fstat(descriptor)

            with patch("tm_migration.os.fstat", side_effect=flaky_fstat):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.TEMP_IDENTITY_FAILED")
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists())
            retry = service.export_jsonl(store, destination)
            self.assertIsInstance(retry, ExportReport)

    def test_initial_recovery_fstat_failure_cleans_owned_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_copy = tm_migration._copy_export_recovery_file
            original_fstat = tm_migration.os.fstat

            def failing_copy(
                source: Path,
                recovery: Path,
                *,
                expected_digest: str,
                code: str,
                parent_handle: Any = None,
            ) -> Any:
                first = True

                def flaky_fstat(descriptor: int) -> os.stat_result:
                    nonlocal first
                    if first:
                        first = False
                        raise OSError(
                            "injected initial recovery fstat failure"
                        )
                    return original_fstat(descriptor)

                with patch(
                    "tm_migration.os.fstat",
                    side_effect=flaky_fstat,
                ):
                    return original_copy(
                        source,
                        recovery,
                        expected_digest=expected_digest,
                        code=code,
                        parent_handle=parent_handle,
                    )

            with patch(
                "tm_migration._copy_export_recovery_file",
                side_effect=failing_copy,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(
                result.error_code,
                "EXPORT.RECOVERY_IDENTITY_FAILED",
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists())
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "cancelled",
            )
            retry = service.export_jsonl(store, destination)
            self.assertIsInstance(retry, ExportReport)

    def test_foreign_destination_created_at_replace_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            foreign = b'{"source":"foreign","target":"concurrent"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_replace = tm_migration._replace_path
            injected = False

            def hostile_replace(
                source: Path,
                target: Path,
                **kwargs: Any,
            ) -> None:
                nonlocal injected
                if not injected and source == paths.jsonl_temp:
                    injected = True
                    destination.write_bytes(foreign)
                original_replace(source, target, **kwargs)

            with patch(
                "tm_migration._replace_path",
                side_effect=hostile_replace,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(injected)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertEqual(destination.read_bytes(), foreign)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), prior_jsonl)
            self.assertEqual(len(result.recovery_locators), 1)
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )

    def test_foreign_manifest_created_at_replace_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            foreign_manifest = b'{"manifest":"foreign"}\n'
            destination.write_bytes(prior_jsonl)
            original_replace = tm_migration._replace_path
            injected = False

            def hostile_replace(
                source: Path,
                target: Path,
                **kwargs: Any,
            ) -> None:
                nonlocal injected
                if not injected and source == paths.manifest_temp:
                    injected = True
                    paths.manifest.write_bytes(foreign_manifest)
                original_replace(source, target, **kwargs)

            with patch(
                "tm_migration._replace_path",
                side_effect=hostile_replace,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(injected)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.MANIFEST_RESTORE_FAILED",
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), foreign_manifest)
            self.assertEqual(result.recovery_locators, ())
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )

    def test_hostile_manifest_swap_preserves_manifest_recovery_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            foreign = b'{"manifest":"foreign"}\n'
            calls = 0
            original_dir_fsync = tm_migration._fsync_directory

            def hostile_second_dir_fsync(
                path: Path,
                *,
                parent_handle: Any = None,
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    paths.manifest.write_bytes(foreign)
                    raise OSError("injected manifest directory fsync failure")
                original_dir_fsync(path, parent_handle=parent_handle)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=hostile_second_dir_fsync,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.MANIFEST_RESTORE_FAILED",
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), foreign)
            self.assertFalse(paths.jsonl_recovery.exists())
            self.assertEqual(paths.manifest_recovery.read_bytes(), prior_manifest)
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )

    def test_same_bytes_swap_before_identity_proof_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_identity = tm_migration._published_file_identity
            swapped = False

            def hostile_identity(
                path: Path,
                digest: str,
                *,
                parent_handle: Any = None,
            ) -> tuple[int, int]:
                nonlocal swapped
                if not swapped and path == destination:
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
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertNotEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest_recovery.read_bytes(), prior_manifest)
            self.assertEqual(len(result.recovery_locators), 1)
            self.assertEqual(
                result.recovery_locators[0].path,
                paths.jsonl_recovery,
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )

    def test_hostile_swap_during_restore_fails_closed_with_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
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
                    destination.write_bytes(foreign)
                    raise OSError("injected jsonl directory fsync failure")
                original_dir_fsync(path, parent_handle=parent_handle)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=hostile_dir_fsync,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(result.error_code, "EXPORT.JSONL_RESTORE_FAILED")
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            self.assertEqual(destination.read_bytes(), foreign)
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(
                evidence.before_digest,
                hashlib.sha256(prior_jsonl).hexdigest(),
            )
            self.assertEqual(
                evidence.observed_digest,
                hashlib.sha256(foreign).hexdigest(),
            )
            self.assertEqual(len(result.recovery_locators), 1)
            locator = result.recovery_locators[0]
            self.assertIs(locator.asset_kind, AssetKind.EXPORT_DESTINATION)
            self.assertEqual(locator.path, paths.jsonl_recovery)
            self.assertEqual(
                locator.expected_digest,
                hashlib.sha256(prior_jsonl).hexdigest(),
            )
            self.assertEqual(paths.jsonl_recovery.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest_recovery.read_bytes(), prior_manifest)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )
            with self.assertRaises(ExportPreflightError) as fail_stop:
                service.export_jsonl(store, destination)
            self.assertEqual(
                fail_stop.exception.error_code,
                "EXPORT.PRIOR_STATE_UNRECOVERABLE",
            )

    def test_same_byte_foreign_swap_at_completion_seam_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_complete = store.complete_issued_export_receipt
            swapped = False
            published_jsonl = b""
            published_manifest = b""
            foreign_identity: tuple[int, int] | None = None

            def hostile_complete(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal swapped
                nonlocal published_jsonl
                nonlocal published_manifest
                nonlocal foreign_identity
                if not swapped:
                    swapped = True
                    published_jsonl = paths.destination.read_bytes()
                    published_manifest = paths.manifest.read_bytes()
                    foreign = paths.destination.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    foreign.write_bytes(published_jsonl)
                    os.replace(foreign, paths.destination)
                    observed = os.lstat(paths.destination)
                    foreign_identity = (observed.st_dev, observed.st_ino)
                original_complete(snapshot_id, **kwargs)

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=hostile_complete,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertEqual(result.stage, "EXPORT.RESTORE")
            self.assertEqual(
                result.error_code,
                "EXPORT.JSONL_RESTORE_FAILED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(diagnostic.code for diagnostic in result.diagnostics),
                ("EXPORT.RESTORE_FAILED",),
            )
            self.assertEqual(destination.read_bytes(), published_jsonl)
            observed = os.lstat(destination)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                foreign_identity,
            )
            self.assertEqual(paths.manifest.read_bytes(), published_manifest)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), prior_jsonl)
            self.assertEqual(
                paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(
                evidence.before_digest,
                hashlib.sha256(prior_jsonl).hexdigest(),
            )
            self.assertEqual(
                evidence.observed_digest,
                hashlib.sha256(published_jsonl).hexdigest(),
            )
            self.assertEqual(len(result.recovery_locators), 1)
            locator = result.recovery_locators[0]
            self.assertIs(locator.asset_kind, AssetKind.EXPORT_DESTINATION)
            self.assertEqual(locator.path, paths.jsonl_recovery)
            self.assertEqual(
                locator.expected_digest,
                hashlib.sha256(prior_jsonl).hexdigest(),
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )

    def test_failure_error_codes_are_stable_identifiers_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            destination.write_bytes(b'{"source":"prior","target":"pair"}\n')
            paths.manifest.write_bytes(b'{"manifest":"prior"}\n')
            original_replace = tm_migration._replace_path

            def failing_replace(
                source: Path,
                target: Path,
                **kwargs: Any,
            ) -> None:
                if source == paths.manifest_temp:
                    raise OSError("injected manifest replace failure")
                original_replace(source, target, **kwargs)

            with patch(
                "tm_migration._replace_path",
                side_effect=failing_replace,
            ):
                result = service.export_jsonl(store, destination)
            assert isinstance(result, ExportFailure)
            _assert_safe_failure(self, result, root)
            self.assertEqual(str(ExportPreflightError("EXPORT.X")), "EXPORT.X")
            self.assertNotIn(str(root), repr(result.previous_destination_preservation))

    def test_source_swap_at_pre_mutation_seam_fails_closed(self) -> None:
        """P1 2 regression: a source-name swap at the pre-mutation seam.

        The exact created temp identity is proved immediately before
        the rename and re-proved again after the late-bound seam
        returns: a foreign same-byte inode swapped in exactly at the
        seam is detected before the rename, so the publisher fails stop
        at the pre-rename source proof and never renames the foreign
        inode or reports ``ExportReport``.  The receipt is cancelled
        without completion, the durable handoff stays retained because
        cleanup cannot remove the unprovable foreign temp, and the
        foreign inode and bytes survive untouched.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_seam = tm_migration._after_replace_source_proved
            swapped = False
            foreign_identity: tuple[int, int] | None = None
            swapped_payload = b""

            def seam_swap_source(
                source: Path,
                target: Path,
                expected_source_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity, swapped_payload
                original_seam(source, target, expected_source_identity)
                if swapped:
                    return
                swapped = True
                swapped_payload = source.read_bytes()
                foreign = source.with_name("foreign-same-bytes.jsonl")
                foreign.write_bytes(swapped_payload)
                os.replace(foreign, source)
                observed = os.lstat(source)
                foreign_identity = (observed.st_dev, observed.st_ino)

            with patch(
                "tm_migration._after_replace_source_proved",
                side_effect=seam_swap_source,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "EXPORT.PUBLISH")
            self.assertEqual(
                result.error_code,
                "EXPORT.SOURCE_UNPROVEN",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                ("EXPORT.CLEANUP_PENDING",),
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "cancelled",
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            observed = os.lstat(paths.jsonl_temp)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                foreign_identity,
            )
            self.assertEqual(paths.jsonl_temp.read_bytes(), swapped_payload)
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            self.assertFalse(paths.jsonl_recovery.exists())
            self.assertFalse(paths.manifest_recovery.exists())
            self.assertEqual(len(result.recovery_locators), 0)


    def test_destination_swap_at_pre_mutation_seam_fails_closed(
        self,
    ) -> None:
        """P1 2 regression: a destination swap at the pre-mutation seam.

        The exact prior destination digest and inode are proved before
        the publication and re-proved again after the late-bound seam
        returns: a foreign same-byte inode swapped in exactly at the
        seam is detected before the rename, so the publisher fails stop
        at the pre-rename destination proof and never overwrites the
        foreign destination.  The receipt is cancelled without
        completion, the foreign destination inode and bytes survive
        cleanup untouched, and no ``ExportReport`` is ever returned.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_seam = tm_migration._after_replace_source_proved
            swapped = False
            foreign_identity: tuple[int, int] | None = None

            def seam_swap_destination(
                source: Path,
                target: Path,
                expected_source_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity
                original_seam(source, target, expected_source_identity)
                if swapped:
                    return
                swapped = True
                foreign = destination.with_name("foreign-same-byte.jsonl")
                foreign.write_bytes(prior_jsonl)
                os.replace(foreign, destination)
                observed = os.lstat(destination)
                foreign_identity = (observed.st_dev, observed.st_ino)

            with patch(
                "tm_migration._after_replace_source_proved",
                side_effect=seam_swap_destination,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            self.assertEqual(result.stage, "EXPORT.PUBLISH")
            self.assertEqual(
                result.error_code,
                "EXPORT.PRIOR_PAIR_CHANGED",
            )
            self.assertFalse(result.retryable)
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                (),
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "cancelled",
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            observed = os.lstat(destination)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                foreign_identity,
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            self.assertFalse(paths.jsonl_temp.exists())
            self.assertFalse(paths.manifest_temp.exists())
            self.assertFalse(paths.jsonl_recovery.exists())
            self.assertFalse(paths.manifest_recovery.exists())
            self.assertEqual(len(result.recovery_locators), 0)


class TMExportLedgerTests(unittest.TestCase):
    def _prepared(self, root: Path) -> tuple[MutableStageRef, SQLiteTMStore]:
        return _prepared_store(root)

    def test_register_complete_and_cancel_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            destination = (root / "ledger.jsonl").resolve()
            paths = _export_artifact_paths(destination)
            receipt, jsonl_identity, manifest_identity = _write_ledger_pair(
                store,
                snapshot_id="snapshot.export." + "a" * 32,
                destination=destination,
                manifest=paths.manifest,
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "issued",
            )
            store.complete_issued_export_receipt(
                receipt.snapshot_id,
                expected_generation=0,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "completed",
            )
            self.assertEqual(
                _ledger_rows(stage.staged_db_path, destination),
                (
                    (
                        receipt.snapshot_id,
                        receipt.resource_id,
                        receipt.canonical_store_id,
                        receipt.exported_revision,
                        receipt.jsonl_digest,
                        receipt.record_count,
                        receipt.format_version,
                        str(destination),
                        str(paths.manifest),
                        "completed",
                    ),
                ),
            )

            cancelled, _cjsonl_identity, _cmanifest_identity = (
                _write_ledger_pair(
                    store,
                    snapshot_id="snapshot.export." + "b" * 32,
                    destination=destination,
                    manifest=paths.manifest,
                    payload=b'{"source":"second","target":"pair"}\n',
                )
            )
            store.cancel_issued_export_receipt(
                cancelled.snapshot_id,
                expected_generation=0,
            )
            statuses = tuple(
                str(row[9])
                for row in _ledger_rows(stage.staged_db_path, destination)
            )
            self.assertEqual(
                statuses,
                ("completed", "cancelled"),
            )

    def test_duplicate_and_identity_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            destination = (root / "ledger.jsonl").resolve()
            paths = _export_artifact_paths(destination)
            receipt, jsonl_identity, manifest_identity = _write_ledger_pair(
                store,
                snapshot_id="snapshot.export." + "c" * 32,
                destination=destination,
                manifest=paths.manifest,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as duplicate:
                store.register_issued_export_receipt(
                    receipt,
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=0,
                )
            self.assertEqual(
                str(duplicate.exception),
                "STORE.RECEIPT_DUPLICATE",
            )
            with self.assertRaises(SQLiteStoreSchemaError) as unknown:
                store.complete_issued_export_receipt(
                    "snapshot.export." + "d" * 32,
                    expected_generation=0,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )
            self.assertEqual(str(unknown.exception), "STORE.RECEIPT_UNKNOWN")
            with self.assertRaises(TypeError):
                store.complete_issued_export_receipt(
                    receipt.snapshot_id,
                    expected_generation=0,
                )  # pyright: ignore[reportCallIssue]
            absent_destination = (root / "absent.jsonl").resolve()
            absent, absent_jsonl, absent_manifest = _write_ledger_pair(
                store,
                snapshot_id="snapshot.export." + "dd" * 16,
                destination=absent_destination,
                manifest=(
                    absent_destination.with_name(
                        f"{absent_destination.name}.localcat-snapshot.json"
                    )
                ),
            )
            os.unlink(absent_destination)
            os.unlink(
                absent_destination.with_name(
                    f"{absent_destination.name}.localcat-snapshot.json"
                )
            )
            with self.assertRaises(SQLiteStoreSchemaError) as missing:
                store.complete_issued_export_receipt(
                    absent.snapshot_id,
                    expected_generation=0,
                    jsonl_identity=absent_jsonl,
                    manifest_identity=absent_manifest,
                )
            self.assertEqual(
                str(missing.exception),
                "STORE.RECEIPT_PAIR_INVALID",
            )
            with self.assertRaises(ValueError):
                store.register_issued_export_receipt(
                    _ledger_receipt(
                        store,
                        snapshot_id="snapshot.export." + "e" * 32,
                        canonical_store_id="store.foreign",
                    ),
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=0,
                )
            with self.assertRaises(ValueError):
                store.register_issued_export_receipt(
                    _ledger_receipt(
                        store,
                        snapshot_id="snapshot.export." + "f" * 32,
                        resource_id="tm.foreign",
                    ),
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=0,
                )

    def test_stale_and_generation_transitions_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            destination = (root / "ledger.jsonl").resolve()
            paths = _export_artifact_paths(destination)
            receipt, jsonl_identity, manifest_identity = _write_ledger_pair(
                store,
                snapshot_id="snapshot.export." + "g" * 32,
                destination=destination,
                manifest=paths.manifest,
            )
            with self.assertRaises(SQLiteStoreLifecycleError) as generation:
                store.complete_issued_export_receipt(
                    receipt.snapshot_id,
                    expected_generation=1,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )
            self.assertEqual(generation.exception.code, "STORE.GENERATION_CHANGED")
            self.assertTrue(generation.exception.retryable)
            with self.assertRaises(SQLiteStoreLifecycleError):
                store.register_issued_export_receipt(
                    _ledger_receipt(
                        store,
                        snapshot_id="snapshot.export." + "h" * 32,
                    ),
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=1,
                )
            store.complete_issued_export_receipt(
                receipt.snapshot_id,
                expected_generation=0,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
            )
            with self.assertRaises(SQLiteStoreSchemaError) as stale:
                store.complete_issued_export_receipt(
                    receipt.snapshot_id,
                    expected_generation=0,
                    jsonl_identity=jsonl_identity,
                    manifest_identity=manifest_identity,
                )
            self.assertEqual(str(stale.exception), "STORE.RECEIPT_STALE")
            with self.assertRaises(SQLiteStoreSchemaError) as stale_cancel:
                store.cancel_issued_export_receipt(
                    receipt.snapshot_id,
                    expected_generation=0,
                )
            self.assertEqual(str(stale_cancel.exception), "STORE.RECEIPT_STALE")

    def test_receipt_revision_ancestry_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            destination = (root / "ledger.jsonl").resolve()
            paths = _export_artifact_paths(destination)
            head = store.capture_export_snapshot().revision.head_revision
            with self.assertRaises(SQLiteStoreSchemaError) as stale:
                store.register_issued_export_receipt(
                    _ledger_receipt(
                        store,
                        snapshot_id="snapshot.export." + "i" * 32,
                        exported_revision=head + 1,
                    ),
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=0,
                )
            self.assertEqual(
                str(stale.exception),
                "STORE.RECEIPT_REVISION_STALE",
            )
            with self.assertRaises(SQLiteStoreSchemaError) as ancestry:
                store.register_issued_export_receipt(
                    _ledger_receipt(
                        store,
                        snapshot_id="snapshot.export." + "j" * 32,
                        record_count=999,
                    ),
                    destination_jsonl_path=destination,
                    destination_manifest_path=paths.manifest,
                    expected_generation=0,
                )
            self.assertEqual(
                str(ancestry.exception),
                "STORE.RECEIPT_ANCESTRY_INVALID",
            )
            self.assertEqual(
                _ledger_rows(stage.staged_db_path, destination),
                (),
            )

    def test_register_rejects_alias_and_invalid_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            identity = stage.resource_identity
            receipt = _ledger_receipt(
                store,
                snapshot_id="snapshot.export." + "k" * 32,
            )
            configured_manifest = identity.snapshot_manifest_path
            other = (root / "other.json").resolve()
            cases = (
                (
                    identity.configured_jsonl_path,
                    configured_manifest,
                ),
                (
                    identity.snapshot_manifest_path,
                    (root / "x.jsonl.localcat-snapshot.json").resolve(),
                ),
                (
                    identity.canonical_sidecar_path,
                    (root / "x.jsonl.localcat-snapshot.json").resolve(),
                ),
                (
                    other,
                    configured_manifest,
                ),
                (
                    other,
                    other,
                ),
                (
                    other,
                    (root / "not-deterministic.json").resolve(),
                ),
                (
                    Path("relative.jsonl"),
                    (root / "relative.jsonl.localcat-snapshot.json").resolve(),
                ),
            )
            for jsonl_path, manifest_path in cases:
                with self.subTest(jsonl=str(jsonl_path)):
                    with self.assertRaises(ValueError):
                        store.register_issued_export_receipt(
                            receipt,
                            destination_jsonl_path=jsonl_path,
                            destination_manifest_path=manifest_path,
                            expected_generation=0,
                        )
            with self.assertRaises(ValueError):
                store.register_issued_export_receipt(
                    receipt,
                    destination_jsonl_path=(root / "x.jsonl").resolve(),
                    destination_manifest_path=(root / "x.jsonl.localcat-snapshot.json").resolve(),
                    expected_generation=-1,
                )

    def test_completed_export_history_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            result = service.export_jsonl(store, destination)
            assert isinstance(result, ExportReport)
            with self.assertRaises(SQLiteStoreSchemaError) as stale:
                store.complete_issued_export_receipt(
                    result.snapshot_id,
                    expected_generation=result.canonical_generation,
                    jsonl_identity=_identity_of(destination),
                    manifest_identity=_identity_of(
                        _export_artifact_paths(destination).manifest
                    ),
                )
            self.assertEqual(str(stale.exception), "STORE.RECEIPT_STALE")
            with self.assertRaises(SQLiteStoreSchemaError):
                store.cancel_issued_export_receipt(
                    result.snapshot_id,
                    expected_generation=result.canonical_generation,
                )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertEqual(str(rows[0][0]), result.snapshot_id)


class TMExportHandoffDurabilityTests(unittest.TestCase):
    def _prepared(self, root: Path) -> tuple[MutableStageRef, SQLiteTMStore]:
        return _prepared_store(root)

    def test_handoff_clear_requires_all_four_deterministic_paths_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            destination = (root / "ledger.jsonl").resolve()
            paths = _export_artifact_paths(destination)
            receipt, jsonl_identity, manifest_identity = _write_ledger_pair(
                store,
                snapshot_id="snapshot.export." + "h" * 32,
                destination=destination,
                manifest=paths.manifest,
            )
            store.complete_issued_export_receipt(
                receipt.snapshot_id,
                expected_generation=0,
                jsonl_identity=jsonl_identity,
                manifest_identity=manifest_identity,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                artifact.write_bytes(b"foreign different bytes\n")
                with self.assertRaises(SQLiteStoreSchemaError) as blocked:
                    store.clear_issued_receipt_handoff(
                        receipt.snapshot_id,
                        expected_generation=0,
                    )
                self.assertEqual(
                    str(blocked.exception),
                    "STORE.HANDOFF_CLEANUP_PENDING",
                )
                self.assertIsNotNone(
                    _meta_value(stage.staged_db_path, meta_key)
                )
                artifact.unlink()
            same_bytes = destination.with_name("foreign-same-bytes.jsonl")
            same_bytes.write_bytes(destination.read_bytes())
            os.replace(same_bytes, paths.jsonl_temp)
            with self.assertRaises(SQLiteStoreSchemaError) as same_byte:
                store.clear_issued_receipt_handoff(
                    receipt.snapshot_id,
                    expected_generation=0,
                )
            self.assertEqual(
                str(same_byte.exception),
                "STORE.HANDOFF_CLEANUP_PENDING",
            )
            paths.jsonl_temp.unlink()
            os.symlink(destination, paths.manifest_temp)
            with self.assertRaises(SQLiteStoreSchemaError) as symlink:
                store.clear_issued_receipt_handoff(
                    receipt.snapshot_id,
                    expected_generation=0,
                )
            self.assertEqual(
                str(symlink.exception),
                "STORE.HANDOFF_CLEANUP_PENDING",
            )
            paths.manifest_temp.unlink()
            paths.jsonl_recovery.mkdir()
            with self.assertRaises(SQLiteStoreSchemaError) as directory:
                store.clear_issued_receipt_handoff(
                    receipt.snapshot_id,
                    expected_generation=0,
                )
            self.assertEqual(
                str(directory.exception),
                "STORE.HANDOFF_CLEANUP_PENDING",
            )
            paths.jsonl_recovery.rmdir()
            os.link(destination, paths.manifest_recovery)
            with self.assertRaises(SQLiteStoreSchemaError) as hardlink:
                store.clear_issued_receipt_handoff(
                    receipt.snapshot_id,
                    expected_generation=0,
                )
            self.assertEqual(
                str(hardlink.exception),
                "STORE.HANDOFF_CLEANUP_PENDING",
            )
            paths.manifest_recovery.unlink()
            store.clear_issued_receipt_handoff(
                receipt.snapshot_id,
                expected_generation=0,
            )
            self.assertIsNone(_meta_value(stage.staged_db_path, meta_key))

    def test_cleanup_fsync_failure_before_handoff_clear_returns_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            original_dir_fsync = tm_migration._fsync_directory
            calls: list[int] = []

            def failing_cleanup_fsync(
                path: Path,
                *,
                parent_handle: Any = None,
            ) -> None:
                calls.append(1)
                if len(calls) == 3:
                    raise OSError(
                        "injected cleanup directory fsync failure"
                    )
                original_dir_fsync(path, parent_handle=parent_handle)

            with patch(
                "tm_migration._fsync_directory",
                side_effect=failing_cleanup_fsync,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.CLEANUP_PENDING")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertTrue(result.publication_committed)
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                ("EXPORT.CLEANUP_PENDING",),
            )
            self.assertEqual(
                result.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertEqual(result.recovery_locators, ())
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            recovered = store.recover_configured_refresh()
            self.assertIs(
                recovered.state,
                tm_snapshot_recovery.RefreshRecoveryState.COMPLETED,
            )
            retry = service.export_jsonl(store, destination)
            self.assertIsInstance(retry, ExportReport)

    def test_cancellation_failure_durably_releases_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_replace = tm_migration._replace_path

            def failing_manifest_replace(
                source: Path,
                target: Path,
                **kwargs: Any,
            ) -> None:
                if source == paths.manifest_temp:
                    raise OSError("injected manifest replace failure")
                original_replace(source, target, **kwargs)

            with patch(
                "tm_migration._replace_path",
                side_effect=failing_manifest_replace,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "EXPORT.PUBLISH")
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "cancelled")
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            self.assertEqual(destination.read_bytes(), prior_jsonl)
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            retry = service.export_jsonl(store, destination)
            self.assertIsInstance(retry, ExportReport)

    def test_committed_probe_with_cleanup_conflict_returns_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            original_complete = store.complete_issued_export_receipt
            conflict_planted = False

            def commit_clear_then_conflict(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal conflict_planted
                original_complete(snapshot_id, **kwargs)
                for artifact in (
                    paths.jsonl_recovery,
                    paths.manifest_recovery,
                ):
                    if artifact.exists():
                        artifact.unlink()
                store.clear_issued_receipt_handoff(
                    snapshot_id,
                    expected_generation=kwargs["expected_generation"],
                )
                paths.manifest_temp.write_bytes(
                    b"foreign post-clear bytes\n"
                )
                conflict_planted = True
                raise OSError("injected post-clear exception")

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=commit_clear_then_conflict,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertTrue(conflict_planted)
            self.assertEqual(result.error_code, "EXPORT.CLEANUP_PENDING")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertTrue(result.publication_committed)
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                ("EXPORT.CLEANUP_PENDING",),
            )
            self.assertEqual(
                result.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertEqual(result.recovery_locators, ())
            self.assertEqual(
                paths.manifest_temp.read_bytes(),
                b"foreign post-clear bytes\n",
            )
            self.assertEqual(
                _ledger_status(stage.staged_db_path, destination),
                "completed",
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            self.assertTrue(destination.exists())
            self.assertTrue(paths.manifest.exists())

    def test_committed_unclean_probe_at_fresh_destination_returns_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            original_complete = store.complete_issued_export_receipt

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
                "complete_issued_export_receipt",
                side_effect=commit_then_raise,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.CLEANUP_PENDING")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertTrue(result.publication_committed)
            self.assertEqual(result.recovery_locators, ())
            self.assertEqual(
                result.previous_destination_preservation.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertIsNone(
                result.previous_destination_preservation.before_digest
            )
            self.assertIsNone(
                result.previous_destination_preservation.observed_digest
            )
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                ("EXPORT.CLEANUP_PENDING", "EXPORT.LEDGER_UNCLEAN"),
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            self.assertTrue(destination.exists())
            self.assertTrue(paths.manifest.exists())

    def test_committed_unclean_probe_retains_prior_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_complete = store.complete_issued_export_receipt

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
                "complete_issued_export_receipt",
                side_effect=commit_then_raise,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "EXPORT.CLEANUP_PENDING")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertTrue(result.publication_committed)
            self.assertEqual(result.recovery_locators, ())
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(
                evidence.before_digest,
                hashlib.sha256(prior_jsonl).hexdigest(),
            )
            self.assertEqual(
                evidence.observed_digest,
                hashlib.sha256(destination.read_bytes()).hexdigest(),
            )
            self.assertNotEqual(
                evidence.before_digest,
                evidence.observed_digest,
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )


class TMExportProbeErrorWindowTests(unittest.TestCase):
    """Probe-raise post-completion window contract for export/refresh.

    When ``probe_issued_receipt_completed`` itself raises after the
    destination pair has been published, both entry points must return a
    fail-stop ``ExportFailure`` (never let ``_export_failure`` raise
    ``EXPORT.PRIOR_STATE_UNRECOVERABLE``).  A fresh prior-absent
    destination is ``NOT_APPLICABLE`` with no locator and
    ``publication_committed`` False; a prior-existing destination keeps
    the digest-backed evidence and proven recovery locator.
    """

    def _prepared(self, root: Path) -> tuple[MutableStageRef, SQLiteTMStore]:
        return _prepared_store(root)

    def _commit_then_raise(
        self,
        original_complete: Any,
    ) -> Callable[..., None]:
        def commit_then_raise(
            snapshot_id: str,
            **kwargs: Any,
        ) -> None:
            original_complete(snapshot_id, **kwargs)
            raise SQLiteStoreLifecycleError(
                "STORE.LEDGER_UNAVAILABLE",
                resource_id="tm.primary",
                generation=kwargs["expected_generation"],
                retryable=True,
            )

        return commit_then_raise

    def _raising_probe(self, *args: Any, **kwargs: Any) -> None:
        raise SQLiteStoreLifecycleError(
            "STORE.PROBE_UNAVAILABLE",
            resource_id="tm.primary",
            generation=0,
            retryable=True,
        )

    def _assert_probe_error_window(
        self,
        result: Any,
        *,
        stage: str,
        before_digest: str | None,
        observed_digest: str | None,
    ) -> None:
        assert isinstance(result, ExportFailure)
        self.assertEqual(result.stage, stage)
        self.assertEqual(result.error_code, "STORE.PROBE_UNAVAILABLE")
        self.assertFalse(result.retryable)
        self.assertFalse(result.publication_committed)
        self.assertTrue(result.publication_commit_ambiguous)
        self.assertEqual(
            tuple(d.code for d in result.diagnostics),
            ("EXPORT.LEDGER_AMBIGUOUS",),
        )
        evidence = result.previous_destination_preservation
        if before_digest is None:
            self.assertEqual(
                evidence.state,
                AssetPreservationState.NOT_APPLICABLE,
            )
            self.assertIsNone(evidence.before_digest)
            self.assertIsNone(evidence.observed_digest)
        else:
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(evidence.before_digest, before_digest)
            self.assertEqual(evidence.observed_digest, observed_digest)
            self.assertNotEqual(
                evidence.before_digest,
                evidence.observed_digest,
            )
        self.assertEqual(result.recovery_locators, ())

    def test_probe_error_at_fresh_destination_returns_fail_stop_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            original_complete = store.complete_issued_export_receipt

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=self._commit_then_raise(original_complete),
            ), patch.object(
                store,
                "probe_issued_receipt_completed",
                side_effect=self._raising_probe,
            ):
                result = service.export_jsonl(store, destination)

            self._assert_probe_error_window(
                result,
                stage="EXPORT.LEDGER",
                before_digest=None,
                observed_digest=None,
            )
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )
            self.assertTrue(destination.exists())
            self.assertTrue(paths.manifest.exists())

    def test_probe_error_retains_prior_digests_without_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_complete = store.complete_issued_export_receipt

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=self._commit_then_raise(original_complete),
            ), patch.object(
                store,
                "probe_issued_receipt_completed",
                side_effect=self._raising_probe,
            ):
                result = service.export_jsonl(store, destination)

            self._assert_probe_error_window(
                result,
                stage="EXPORT.LEDGER",
                before_digest=hashlib.sha256(prior_jsonl).hexdigest(),
                observed_digest=hashlib.sha256(
                    destination.read_bytes()
                ).hexdigest(),
            )
            self.assertTrue(paths.jsonl_recovery.exists())
            self.assertTrue(destination.exists())

    def test_refresh_probe_error_retains_prior_digests_without_locator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            identity = stage.resource_identity
            prior_jsonl = b'{"source":"bound","target":"pair"}\n'
            _ = _bind_current_snapshot(store, stage, prior_jsonl)
            paths = _export_artifact_paths(identity.configured_jsonl_path)
            original_complete = store.complete_issued_refresh_receipt

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=self._commit_then_raise(original_complete),
            ), patch.object(
                store,
                "probe_issued_receipt_completed",
                side_effect=self._raising_probe,
            ):
                result = service.refresh_configured_snapshot(store)

            self._assert_probe_error_window(
                result,
                stage="REFRESH.LEDGER",
                before_digest=hashlib.sha256(prior_jsonl).hexdigest(),
                observed_digest=hashlib.sha256(
                    identity.configured_jsonl_path.read_bytes()
                ).hexdigest(),
            )
            self.assertTrue(paths.jsonl_recovery.exists())
            self.assertTrue(identity.configured_jsonl_path.exists())
            self.assertTrue(identity.snapshot_manifest_path.exists())
            rows = _ledger_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            refresh_rows = tuple(
                row for row in rows if str(row[0]).startswith(
                    "snapshot.refresh."
                )
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")

    def test_probe_error_with_replaced_recovery_locator_never_fabricates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_complete = store.complete_issued_export_receipt
            before_digest = hashlib.sha256(prior_jsonl).hexdigest()

            def raising_probe_with_replaced_locator(
                *args: Any,
                **kwargs: Any,
            ) -> None:
                if paths.jsonl_recovery.exists():
                    paths.jsonl_recovery.unlink()
                raise SQLiteStoreLifecycleError(
                    "STORE.PROBE_UNAVAILABLE",
                    resource_id="tm.primary",
                    generation=0,
                    retryable=True,
                )

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=self._commit_then_raise(original_complete),
            ), patch.object(
                store,
                "probe_issued_receipt_completed",
                side_effect=raising_probe_with_replaced_locator,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "STORE.PROBE_UNAVAILABLE")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertFalse(result.publication_committed)
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(result.recovery_locators, ())
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(evidence.before_digest, before_digest)
            self.assertEqual(
                evidence.observed_digest,
                hashlib.sha256(destination.read_bytes()).hexdigest(),
            )
            self.assertFalse(paths.jsonl_recovery.exists())
            rows = _ledger_rows(stage.staged_db_path, destination)
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "completed")
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + str(rows[0][0]),
                )
            )

    def test_probe_error_unreadable_destination_keeps_unverified_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = self._prepared(root)
            service = _service(stage.resource_identity)
            destination = _destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            original_complete = store.complete_issued_export_receipt
            before_digest = hashlib.sha256(prior_jsonl).hexdigest()

            def raising_probe_with_unreadable_destination(
                *args: Any,
                **kwargs: Any,
            ) -> None:
                destination.unlink()
                destination.mkdir()
                raise SQLiteStoreLifecycleError(
                    "STORE.PROBE_UNAVAILABLE",
                    resource_id="tm.primary",
                    generation=0,
                    retryable=True,
                )

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=self._commit_then_raise(original_complete),
            ), patch.object(
                store,
                "probe_issued_receipt_completed",
                side_effect=raising_probe_with_unreadable_destination,
            ):
                result = service.export_jsonl(store, destination)

            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "STORE.PROBE_UNAVAILABLE")
            self.assertEqual(result.stage, "EXPORT.LEDGER")
            self.assertFalse(result.retryable)
            self.assertFalse(result.publication_committed)
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(result.recovery_locators, ())
            evidence = result.previous_destination_preservation
            self.assertEqual(
                evidence.state,
                AssetPreservationState.UNVERIFIED,
            )
            self.assertEqual(evidence.before_digest, before_digest)
            self.assertIsNone(evidence.observed_digest)
            self.assertEqual(
                tuple(d.code for d in result.diagnostics),
                ("EXPORT.LEDGER_AMBIGUOUS",),
            )
            self.assertTrue(destination.is_dir())
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff."
                    + str(_ledger_rows(stage.staged_db_path, destination)[0][0]),
                )
            )


if __name__ == "__main__":
    unittest.main()
