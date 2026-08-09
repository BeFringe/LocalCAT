"""Task 5.11 schema upgrade copy-and-switch tests.

The suite drives ``TMMigrationService.upgrade_schema`` over a realistic
ACTIVE pre-v2 canonical bound to a strict coordinator.  A successful
upgrade first proves the strict v1 record-block ancestry and the full
binding/manifest/receipt/source closure, mints one single-use snapshot
ticket backed by a ``Connection.backup()`` recovery backup, migrates a
fresh mutable copy in place (proven completion order, records preserved
verbatim, candidate indexes rebuilt), seals it in the private
schema-upgrade mode (historical issued receipt at the exported
revision), and reuses the existing seal/activate pipeline to publish
the equivalent new generation under the same canonical store id guarded
by the ticket.  The old store is never mutated in place; every failure
stage leaves it byte-identical and reopenable, divergence/tampering/
unprovable order fails closed and is never repaired, and the recovery
backup is reported as digest-backed restoration evidence.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
import tm_migration
import tm_sqlite_store
from tm_activation_journal import (
    _ActivationFileIdentity,
    _ActivationJournalHandle,
    _ActivationJournalRecord,
    _ActivationPreparation,
    _CanonicalStoreRef,
    _ensure_activation_lineage_marker,
    ActivationPreparationError,
)
from tm_contracts import (
    AssetPreservationState,
    CanonicalResourceIdentity,
    MutableStageRef,
    SchemaUpgradeFailure,
    SchemaUpgradeReport,
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    initialize_stage_schema,
    inspect_stage_schema,
    unique_character_ngrams,
)
from text_matcher import fold_text_v1
from tests.test_tm_activation import (
    _identity,
    _prior_stage,
)

_LEGACY_CREATED_AT = "2026-01-01T00:00:00+00:00"
_LEGACY_PROVENANCE_JSON = '[["source","legacy-jsonl"]]'

# The configured historical JSONL: two rows that exactly match the first
# completed origin batch (records 1-2) and the binding receipt.
_HISTORICAL_SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"other","target":"value"}\n'
)

# Block 2 (local_write.zz..., records 3-4) and block 3 (import.00...,
# record 5).  The proven append order is the reverse of the batch-id
# lexical order so batch-id sorting can never substitute for the strict
# record-block proof.
_BLOCK_RECORDS = (
    (
        "local_write.zz00000000000000000000000000000001",
        "local_write",
        None,
        None,
        (
            ("same", "winner", "narrator", "ctx-prev", "ctx-next", "overlay", 7, "2026-03-01T12:00:00+00:00"),
            ("alpha", "beta", None, None, None, None, 3, None),
        ),
    ),
    (
        "import.0000000000000000000000000000000a",
        "import",
        "a" * 64,
        None,
        (
            ("gamma", "delta", None, None, None, None, 1, "2026-04-01T00:00:00+00:00"),
        ),
    ),
)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_meta(path: Path) -> dict[str, str]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT key, value FROM tm_meta"
            ).fetchall()
        }
    finally:
        connection.close()


def _legacy_fixture(
    root: Path,
    *,
    fts5_available: bool,
    store_id: str = "store.primary",
) -> tuple[
    CanonicalResourceIdentity,
    MutableStageRef,
    ResourceStoreCoordinator,
    str,
]:
    """One complete ACTIVE old-schema canonical with a published pair.

    The v1 store is a realistic published canonical: three completed
    origin batches (migration, local_write, import in proven append
    order, deliberately scrambled batch-id lexical order), contiguous
    record blocks 1-2/3-4/5 with 0-based origin ordinals, head revision
    and generation 3, ACTIVE status with a durable activation digest,
    one completed binding and its adjacent manifest for the historical
    two-line JSONL at exported revision 1 (record count 2), and a
    complete lineage marker so a same-id activation can prepare.
    """

    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(_HISTORICAL_SOURCE_BYTES)
    source_digest = hashlib.sha256(_HISTORICAL_SOURCE_BYTES).hexdigest()
    prior = _prior_stage(root, identity)
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        initialize_stage_schema(
            prior,
            canonical_store_id=store_id,
            _legacy_schema=True,
        )
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.migration.{source_digest[:24]}",
        resource_id=identity.resource_id,
        canonical_store_id=store_id,
        exported_revision=1,
        jsonl_digest=source_digest,
        record_count=2,
        format_version=SNAPSHOT_FORMAT_VERSION,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    identity.snapshot_manifest_path.write_text(
        contract_to_json(manifest),
        encoding="utf-8",
    )
    connection = sqlite3.connect(prior.staged_db_path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        required_sizes = (1, 2) if fts5_available else (1, 2, 3)
        record_id = 0
        blocks = (
            (
                f"migration.{source_digest}",
                "migration",
                source_digest,
                str(identity.configured_jsonl_path),
                (
                    ("same", "first", None, None, None, None, 0, None),
                    ("other", "value", None, None, None, None, 0, None),
                ),
            ),
        ) + _BLOCK_RECORDS
        for batch_id, kind, batch_source_digest, batch_source_path, records in blocks:
            if kind == "local_write":
                connection.execute(
                    "INSERT INTO tm_origin_batch("
                    "batch_id, kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "created_at) VALUES (?, 'local_write', NULL, NULL, "
                    "'completed', ?, 0, 0, ?)",
                    (batch_id, len(records), _LEGACY_CREATED_AT),
                )
            else:
                if batch_source_path is None:
                    batch_source_path = str(identity.configured_jsonl_path)
                connection.execute(
                    "INSERT INTO tm_origin_batch("
                    "batch_id, kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "created_at) VALUES (?, ?, ?, ?, 'completed', ?, 0, 0, ?)",
                    (
                        batch_id,
                        kind,
                        batch_source_digest,
                        batch_source_path,
                        len(records),
                        _LEGACY_CREATED_AT,
                    ),
                )
            for ordinal, record in enumerate(records):
                (
                    source_raw,
                    target_raw,
                    speaker_raw,
                    context_prev_raw,
                    context_next_raw,
                    file_source,
                    usage_count,
                    last_used,
                ) = record
                record_id += 1
                folded = fold_text_v1(source_raw).folded_text
                connection.execute(
                    "INSERT INTO tm_record("
                    "record_id, source_raw, target_raw, source_fold_v1, "
                    "speaker_raw, context_prev_raw, context_next_raw, "
                    "file_source, provenance_json, legacy_line_no, "
                    "usage_count, last_used, origin_batch_id, origin_ordinal) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record_id,
                        source_raw,
                        target_raw,
                        folded,
                        speaker_raw,
                        context_prev_raw,
                        context_next_raw,
                        file_source,
                        _LEGACY_PROVENANCE_JSON,
                        record_id if kind in {"migration", "import"} else None,
                        usage_count,
                        last_used,
                        batch_id,
                        ordinal,
                    ),
                )
                for gram_size in required_sizes:
                    for gram in unique_character_ngrams(folded, gram_size):
                        connection.execute(
                            "INSERT INTO tm_gram(gram_size, gram, record_id) "
                            "VALUES (?, ?, ?)",
                            (gram_size, gram, record_id),
                        )
                if fts5_available:
                    connection.execute(
                        "INSERT INTO tm_fts(source_fold_v1, record_id) "
                        "VALUES (?, ?)",
                        (folded, record_id),
                    )
        connection.execute(
            "INSERT INTO tm_snapshot_receipt("
            "snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, "
            "format_version, destination_jsonl_path, "
            "destination_manifest_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)",
            (
                receipt.snapshot_id,
                receipt.resource_id,
                receipt.canonical_store_id,
                receipt.exported_revision,
                receipt.jsonl_digest,
                receipt.record_count,
                receipt.format_version,
                str(identity.configured_jsonl_path),
                str(identity.snapshot_manifest_path),
                _LEGACY_CREATED_AT,
            ),
        )
        connection.execute(
            "INSERT INTO tm_snapshot_binding("
            "binding_id, configured_jsonl_path, manifest_path, "
            "snapshot_kind, snapshot_id, binding_version) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (
                str(identity.configured_jsonl_path),
                str(identity.snapshot_manifest_path),
                SnapshotKind.MIGRATION_SOURCE.value,
                receipt.snapshot_id,
                SNAPSHOT_BINDING_VERSION,
            ),
        )
        connection.execute(
            "UPDATE tm_meta SET value = '3' WHERE key = 'head_revision'"
        )
        connection.execute(
            "UPDATE tm_meta SET value = '3' WHERE key = 'generation'"
        )
        connection.execute(
            "UPDATE tm_meta SET value = 'ACTIVE' WHERE key = 'activation_status'"
        )
        connection.commit()
    finally:
        connection.close()
    activation_digest = hashlib.sha256(
        prior.staged_db_path.read_bytes()
    ).hexdigest()
    connection = sqlite3.connect(prior.staged_db_path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO tm_meta(key, value) VALUES ('activation_digest', ?)",
            (activation_digest,),
        )
        connection.commit()
    finally:
        connection.close()
    _ensure_activation_lineage_marker(identity)
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        coordinator = ResourceStoreCoordinator(
            prior,
            canonical_store_id=store_id,
            _allow_legacy_schema=True,
            _allow_active=True,
            _expected_active_generation=3,
            _expected_activation_digest=activation_digest,
        )
    return identity, prior, coordinator, activation_digest


def _service(
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
    *,
    store_id: str = "store.primary",
) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=store_id,
        coordinator=coordinator,
    )


def _record_rows(path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT record_id, source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, file_source, "
            "provenance_json, legacy_line_no, usage_count, last_used, "
            "origin_batch_id, origin_ordinal "
            "FROM tm_record ORDER BY record_id"
        ).fetchall()
    finally:
        connection.close()


def _assert_index_parity(
    testcase: unittest.TestCase,
    path: Path,
    *,
    fts5_available: bool,
) -> None:
    required_sizes = (1, 2) if fts5_available else (1, 2, 3)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        for record_id, folded in connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record ORDER BY record_id"
        ):
            expected = {
                (size, gram)
                for size in required_sizes
                for gram in unique_character_ngrams(folded, size)
            }
            actual = set(
                connection.execute(
                    "SELECT gram_size, gram FROM tm_gram WHERE record_id = ?",
                    (record_id,),
                )
            )
            testcase.assertEqual(actual, expected)
            if fts5_available:
                fts_rows = connection.execute(
                    "SELECT source_fold_v1 FROM tm_fts WHERE record_id = ?",
                    (record_id,),
                ).fetchall()
                testcase.assertEqual(fts_rows, [(folded,)])
    finally:
        connection.close()


def _assert_v2_active_store(
    testcase: unittest.TestCase,
    path: Path,
    identity: CanonicalResourceIdentity,
    *,
    fts5_available: bool,
    expected_generation: int = 4,
    expected_head_revision: int = 3,
    store_id: str = "store.primary",
) -> None:
    meta = _active_meta(path)
    testcase.assertEqual(meta["activation_status"], "ACTIVE")
    testcase.assertEqual(meta["generation"], str(expected_generation))
    testcase.assertEqual(meta["schema_version"], "2")
    testcase.assertEqual(meta["head_revision"], str(expected_head_revision))
    testcase.assertEqual(meta["schema_upgrade_origin"], "schema-upgrade-v1")
    activation_digest = meta["activation_digest"]
    ref = _CanonicalStoreRef(
        stage_id="canonical.schema-upgrade",
        resource_identity=identity,
        staged_db_path=path,
        manifest_temp_path=identity.snapshot_manifest_path,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        snapshot = inspect_stage_schema(
            ref,
            canonical_store_id=store_id,
            _allow_active=True,
            _expected_active_generation=expected_generation,
            _expected_activation_digest=activation_digest,
        )
    testcase.assertEqual(snapshot.schema_version, 2)
    testcase.assertEqual(snapshot.generation, expected_generation)
    testcase.assertEqual(snapshot.head_revision, expected_head_revision)
    testcase.assertEqual(snapshot.activation_status, "ACTIVE")


def _assert_legacy_reopenable(
    testcase: unittest.TestCase,
    path: Path,
    identity: CanonicalResourceIdentity,
    *,
    fts5_available: bool,
    store_id: str = "store.primary",
    expected_head_revision: int = 3,
    expected_generation: int = 3,
    allow_diverged_runtime: bool = False,
) -> None:
    meta = _active_meta(path)
    ref = MutableStageRef(
        stage_id="stage.schema-upgrade.source",
        resource_identity=identity,
        staged_db_path=path,
        manifest_temp_path=path.with_name(
            f"{path.name}.localcat-schema-upgrade.inspect.tmp"
        ),
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        snapshot = inspect_stage_schema(
            ref,
            canonical_store_id=store_id,
            _allow_legacy_schema=True,
            _allow_active=True,
            _allow_diverged_runtime=allow_diverged_runtime,
            _expected_active_generation=expected_generation,
            _expected_activation_digest=meta["activation_digest"],
        )
    testcase.assertEqual(snapshot.schema_version, 1)
    testcase.assertEqual(snapshot.generation, expected_generation)
    testcase.assertEqual(snapshot.head_revision, expected_head_revision)


class SchemaUpgradeHappyPathTests(unittest.TestCase):
    def test_upgrade_publishes_equivalent_generation_with_digest_evidence(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, prior, coordinator, _digest = _legacy_fixture(
                        root,
                        fts5_available=fts5_available,
                    )
                    store_before = prior.staged_db_path.read_bytes()
                    manifest_before = identity.snapshot_manifest_path.read_bytes()
                    source_before = identity.configured_jsonl_path.read_bytes()
                    records_before = _record_rows(prior.staged_db_path)
                    service = _service(coordinator, identity)
                    with (
                        patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=fts5_available,
                        ),
                        patch(
                            "tm_migration._copy_store_into_stage",
                            wraps=tm_migration._copy_store_into_stage,
                        ) as copy_store,
                    ):
                        outcome = service.upgrade_schema(
                            prior.staged_db_path
                        )
                    self.assertIsInstance(outcome, SchemaUpgradeReport)
                    report = cast(SchemaUpgradeReport, outcome)
                    self.assertEqual(
                        report.canonical_store_id,
                        "store.primary",
                    )
                    self.assertEqual(report.from_version, 1)
                    self.assertEqual(report.to_version, 2)
                    self.assertEqual(report.activated_generation, 4)
                    self.assertTrue(report.backup_path.is_file())
                    # success keeps exactly its reported backup and no
                    # hidden byte-exact locator snapshot
                    backups = list(
                        root.glob(
                            "..prior.sqlite3."
                            "localcat-schema-upgrade.*.bak"
                        )
                    )
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(
                        _file_digest(backups[0]),
                        report.backup_digest,
                    )
                    self.assertEqual(
                        list(
                            root.glob(
                                "..prior.sqlite3."
                                "localcat-schema-upgrade.*.locator"
                            )
                        ),
                        [],
                    )
                    self.assertEqual(
                        copy_store.call_args.args[0],
                        report.backup_path,
                    )
                    self.assertEqual(
                        _file_digest(report.backup_path),
                        report.backup_digest,
                    )
                    new_store = identity.canonical_sidecar_path
                    self.assertTrue(new_store.is_file())
                    self.assertEqual(
                        report.success_digest,
                        _file_digest(new_store),
                    )
                    self.assertNotEqual(
                        report.success_digest,
                        report.backup_digest,
                    )
                    self.assertEqual(coordinator.current_generation, 4)
                    self.assertEqual(coordinator.state, "READY")
                    _assert_v2_active_store(
                        self,
                        new_store,
                        identity,
                        fts5_available=fts5_available,
                    )
                    # the old store, snapshot pair, and source stay
                    # byte-identical, and the old schema stays reopenable
                    self.assertEqual(
                        prior.staged_db_path.read_bytes(),
                        store_before,
                    )
                    self.assertEqual(
                        identity.snapshot_manifest_path.read_bytes(),
                        manifest_before,
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        source_before,
                    )
                    # every record column survives verbatim
                    self.assertEqual(
                        _record_rows(new_store),
                        records_before,
                    )
                    # the migrated gram/FTS indexes are exactly rebuilt
                    _assert_index_parity(
                        self,
                        new_store,
                        fts5_available=fts5_available,
                    )
                    _assert_legacy_reopenable(
                        self,
                        prior.staged_db_path,
                        identity,
                        fts5_available=fts5_available,
                    )
                    _assert_legacy_reopenable(
                        self,
                        report.backup_path,
                        identity,
                        fts5_available=fts5_available,
                    )

    def test_second_upgrade_after_success_fails_closed_already_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeReport)
            new_store = identity.canonical_sidecar_path
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                second = service.upgrade_schema(new_store)
            self.assertIsInstance(second, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, second)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.SCHEMA_ALREADY_CURRENT",
            )
            self.assertFalse(failure.retryable)
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertEqual(failure.active_generation, 4)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(failure.recovery_locators, ())

    def test_upgrade_retry_after_preparation_failure_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_migration.StageSealer.seal",
                    side_effect=MigrationPreflightError(
                        "SCHEMA.SEAL_FAILED"
                    ),
                ),
            ):
                first = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(first, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, first)
            self.assertEqual(failure.error_code, "SCHEMA.SEAL_FAILED")
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(coordinator.state, "READY")
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                retry = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(retry, SchemaUpgradeReport)
            report = cast(SchemaUpgradeReport, retry)
            self.assertEqual(report.activated_generation, 4)
            self.assertTrue(report.backup_path.is_file())


class SchemaUpgradeFailClosedTests(unittest.TestCase):
    def test_coordinator_authority_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            mismatched = _service(
                coordinator,
                identity,
                store_id="store.other",
            )
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = mismatched.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.COORDINATOR_MISMATCH",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

            without_coordinator = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with self.assertRaises(MigrationPreflightError) as raised:
                without_coordinator.upgrade_schema(prior.staged_db_path)
            self.assertEqual(
                raised.exception.error_code,
                "SCHEMA.COORDINATOR_UNAVAILABLE",
            )

    def test_candidate_store_id_authority_seam_is_module_private(self) -> None:
        # the authority mutation is only reachable through the private
        # port protocol name; no generic coordinator or port setter exists
        self.assertTrue(
            hasattr(
                tm_sqlite_store._CoordinatorStorePort,
                "_activate_candidate_store_id",
            )
        )
        self.assertFalse(
            hasattr(
                tm_sqlite_store._CoordinatorStorePort,
                "activate_candidate_store_id",
            )
        )
        self.assertFalse(
            hasattr(
                tm_sqlite_store.ResourceStoreCoordinator,
                "activate_candidate_store_id",
            )
        )

    def test_store_path_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            service = _service(coordinator, identity)
            other = root / ".not-the-active-store.sqlite3"
            other.write_bytes(b"not a database")
            with self.assertRaises(MigrationPreflightError) as raised:
                service.upgrade_schema(other)
            self.assertEqual(
                raised.exception.error_code,
                "SCHEMA.ACTIVE_STORE_REQUIRED",
            )

    def test_diverged_old_store_fails_closed_and_preserves_divergence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_meta SET value = '1' "
                    "WHERE key = 'divergence_latched'"
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            # the store is untouched, still diverged, and still v1
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(
                _active_meta(prior.staged_db_path)["divergence_latched"],
                "1",
            )
            _assert_legacy_reopenable(
                self,
                prior.staged_db_path,
                identity,
                fts5_available=False,
                allow_diverged_runtime=True,
            )

    def test_tampered_old_store_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_meta SET value = ? "
                    "WHERE key = 'schema_digest'",
                    ("0" * 64,),
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_missing_manifest_fails_closed_and_is_never_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            identity.snapshot_manifest_path.unlink()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_BINDING_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertFalse(identity.snapshot_manifest_path.exists())

    def test_symlink_manifest_fails_closed_and_is_never_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            manifest = identity.snapshot_manifest_path
            manifest_bytes = manifest.read_bytes()
            manifest.unlink()
            target = root / ".manifest-target.json"
            target.write_bytes(manifest_bytes)
            os.symlink(target, manifest)
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertTrue(manifest.is_symlink())

    def test_multilink_manifest_fails_closed_and_is_never_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            manifest = identity.snapshot_manifest_path
            extra = root / ".manifest-extra-link.json"
            os.link(manifest, extra)
            try:
                service = _service(coordinator, identity)
                with patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    outcome = service.upgrade_schema(prior.staged_db_path)
            finally:
                extra.unlink()
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_mismatched_receipt_record_count_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_snapshot_receipt SET record_count = 3"
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_BINDING_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_mismatched_source_digest_fails_closed_and_is_never_repaired(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            identity.configured_jsonl_path.write_bytes(
                _HISTORICAL_SOURCE_BYTES + b'{"source":"extra","target":"x"}\n'
            )
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_BINDING_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertNotEqual(
                hashlib.sha256(
                    identity.configured_jsonl_path.read_bytes()
                ).hexdigest(),
                _digest,
            )

    def test_mismatched_binding_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_snapshot_binding "
                    "SET configured_jsonl_path = ?",
                    (str(root / "other.jsonl"),),
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.PRIOR_BINDING_INVALID",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_mismatched_receipt_store_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_snapshot_receipt "
                    "SET canonical_store_id = 'store.other'"
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.UPGRADE_UNSUPPORTED",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_unprovable_ancestry_zero_record_batch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO tm_origin_batch("
                    "batch_id, kind, source_digest, source_path, status, "
                    "valid_count, invalid_count, duplicate_source_count, "
                    "created_at) VALUES (?, 'local_write', NULL, NULL, "
                    "'completed', 0, 0, 0, ?)",
                    (
                        "local_write.zero",
                        _LEGACY_CREATED_AT,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.ANCESTRY_UNPROVABLE",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_unprovable_ancestry_interleaved_blocks_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in ("tm_record", "tm_gram"):
                    connection.execute(
                        f"UPDATE {table} SET record_id = 999 "
                        "WHERE record_id = 4"
                    )
                    connection.execute(
                        f"UPDATE {table} SET record_id = 4 "
                        "WHERE record_id = 5"
                    )
                    connection.execute(
                        f"UPDATE {table} SET record_id = 5 "
                        "WHERE record_id = 999"
                    )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.ANCESTRY_UNPROVABLE",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)

    def test_unprovable_ancestry_ordinal_gaps_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            connection = sqlite3.connect(
                prior.staged_db_path,
                isolation_level=None,
            )
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE tm_record SET origin_ordinal = 2 "
                    "WHERE record_id = 3"
                )
                connection.commit()
            finally:
                connection.close()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(
                failure.error_code,
                "SCHEMA.ANCESTRY_UNPROVABLE",
            )
            self.assertEqual(failure.stage, "PREFLIGHT")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)


class SchemaUpgradeConcurrencyTests(unittest.TestCase):
    def test_concurrent_write_between_ticket_and_guard_fails_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            original_prepare = coordinator.prepare_schema_upgrade_ticket

            def prepare_and_write() -> object:
                ticket = original_prepare()
                connection = sqlite3.connect(
                    prior.staged_db_path,
                    isolation_level=None,
                )
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO tm_origin_batch("
                        "batch_id, kind, source_digest, source_path, "
                        "status, valid_count, invalid_count, "
                        "duplicate_source_count, created_at) "
                        "VALUES (?, 'local_write', NULL, NULL, "
                        "'completed', 1, 0, 0, ?)",
                        (
                            "local_write.concurrent",
                            _LEGACY_CREATED_AT,
                        ),
                    )
                    folded = fold_text_v1("concurrent").folded_text
                    connection.execute(
                        "INSERT INTO tm_record("
                        "record_id, source_raw, target_raw, "
                        "source_fold_v1, speaker_raw, context_prev_raw, "
                        "context_next_raw, file_source, provenance_json, "
                        "legacy_line_no, usage_count, last_used, "
                        "origin_batch_id, origin_ordinal) "
                        "VALUES (6, 'concurrent', 'kept', ?, NULL, NULL, "
                        "NULL, NULL, ?, NULL, 1, ?, ?, 0)",
                        (
                            folded,
                            _LEGACY_PROVENANCE_JSON,
                            _LEGACY_CREATED_AT,
                            "local_write.concurrent",
                        ),
                    )
                    connection.execute(
                        "UPDATE tm_meta SET value = '4' "
                        "WHERE key = 'head_revision'"
                    )
                    connection.commit()
                finally:
                    connection.close()
                new_digest = hashlib.sha256(
                    prior.staged_db_path.read_bytes()
                ).hexdigest()
                connection = sqlite3.connect(
                    prior.staged_db_path,
                    isolation_level=None,
                )
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE tm_meta SET value = ? "
                        "WHERE key = 'activation_digest'",
                        (new_digest,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                return ticket

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinator,
                    "prepare_schema_upgrade_ticket",
                    side_effect=prepare_and_write,
                ),
            ):
                first = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(first, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, first)
            self.assertEqual(
                failure.error_code,
                "ACTIVATION.UPGRADE_SNAPSHOT_STALE",
            )
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_CHANGED,
            )
            self.assertEqual(len(failure.recovery_locators), 1)
            locator = failure.recovery_locators[0]
            self.assertEqual(locator.asset_kind.value, "ACTIVE_STORE")
            # the locator is a strict regular single-link file whose
            # current bytes honestly equal the preserved before digest;
            # it is never the byte-changed live canonical
            self.assertTrue(locator.path.exists())
            self.assertNotEqual(locator.path, prior.staged_db_path)
            locator_stat = os.lstat(locator.path)
            self.assertTrue(stat.S_ISREG(locator_stat.st_mode))
            self.assertEqual(locator_stat.st_nlink, 1)
            self.assertEqual(
                _file_digest(locator.path),
                locator.expected_digest,
            )
            self.assertEqual(
                locator.expected_digest,
                failure.active_store_preservation.before_digest,
            )
            self.assertEqual(
                failure.active_store_preservation.before_digest,
                hashlib.sha256(store_before).hexdigest(),
            )
            self.assertEqual(coordinator.state, "READY")
            # the concurrent write is preserved in the live store
            self.assertNotEqual(
                prior.staged_db_path.read_bytes(),
                store_before,
            )
            # the retired Connection.backup is removed; exactly the one
            # exposed byte-exact locator snapshot remains
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.bak"
                    )
                ),
                [],
            )
            locator_snapshots = list(
                root.glob(
                    "..prior.sqlite3.localcat-schema-upgrade.*.locator"
                )
            )
            self.assertEqual(locator_snapshots, [locator.path])
            self.assertEqual(
                _active_meta(prior.staged_db_path)["head_revision"],
                "4",
            )
            # a fresh ticket retry succeeds from the new state
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                retry = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(retry, SchemaUpgradeReport)
            report = cast(SchemaUpgradeReport, retry)
            self.assertEqual(report.activated_generation, 4)
            # Retrying transfers ownership to a new ticket but must not
            # invalidate the recovery locator already returned above.
            self.assertTrue(locator.path.is_file())
            self.assertEqual(
                _file_digest(locator.path),
                locator.expected_digest,
            )
            _assert_v2_active_store(
                self,
                identity.canonical_sidecar_path,
                identity,
                fts5_available=False,
                expected_head_revision=4,
            )


class SchemaUpgradeIndexRebuildTests(unittest.TestCase):
    def test_corrupt_legacy_indexes_are_rebuilt_by_upgrade(self) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, prior, coordinator, _digest = _legacy_fixture(
                        root,
                        fts5_available=fts5_available,
                    )
                    connection = sqlite3.connect(
                        prior.staged_db_path,
                        isolation_level=None,
                    )
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute("DELETE FROM tm_gram")
                        if fts5_available:
                            connection.execute("DELETE FROM tm_fts")
                        connection.commit()
                    finally:
                        connection.close()
                    store_before = prior.staged_db_path.read_bytes()
                    records_before = _record_rows(prior.staged_db_path)
                    service = _service(coordinator, identity)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        outcome = service.upgrade_schema(
                            prior.staged_db_path
                        )
                    self.assertIsInstance(outcome, SchemaUpgradeReport)
                    self.assertEqual(
                        prior.staged_db_path.read_bytes(),
                        store_before,
                    )
                    new_store = identity.canonical_sidecar_path
                    self.assertEqual(
                        _record_rows(new_store),
                        records_before,
                    )
                    _assert_index_parity(
                        self,
                        new_store,
                        fts5_available=fts5_available,
                    )
                    _assert_v2_active_store(
                        self,
                        new_store,
                        identity,
                        fts5_available=fts5_available,
                    )


class SchemaUpgradeFailureReconciliationTests(unittest.TestCase):
    def _assert_failure_preserves_prior(
        self,
        failure: SchemaUpgradeFailure,
        coordinator: ResourceStoreCoordinator,
        identity: CanonicalResourceIdentity,
        prior: MutableStageRef,
        *,
        store_before: bytes,
        expected_code: str,
        expected_stage: str,
        expected_retryable: bool,
    ) -> None:
        self.assertEqual(failure.error_code, expected_code)
        self.assertEqual(failure.stage, expected_stage)
        self.assertEqual(failure.retryable, expected_retryable)
        self.assertEqual(
            failure.active_store_preservation.state,
            AssetPreservationState.VERIFIED_UNCHANGED,
        )
        self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
        self.assertEqual(coordinator.state, "READY")
        self.assertFalse(identity.canonical_sidecar_path.exists())
        _assert_legacy_reopenable(
            self,
            prior.staged_db_path,
            identity,
            fts5_available=False,
        )

    def test_copy_failure_removes_owned_backup_and_keeps_old_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_migration._copy_store_into_stage",
                    side_effect=MigrationPreflightError(
                        "SCHEMA.COPY_FAILED"
                    ),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self._assert_failure_preserves_prior(
                failure,
                coordinator,
                identity,
                prior,
                store_before=store_before,
                expected_code="SCHEMA.COPY_FAILED",
                expected_stage="COPY",
                expected_retryable=False,
            )
            # the store is provably unchanged, so no recovery locator is
            # needed and no hidden schema-upgrade backup may remain
            self.assertEqual(failure.recovery_locators, ())
            backups = list(
                root.glob("..prior.sqlite3.localcat-schema-upgrade.*.bak")
            )
            self.assertEqual(backups, [])
            locator_snapshots = list(
                root.glob("..prior.sqlite3.localcat-schema-upgrade.*.locator")
            )
            self.assertEqual(locator_snapshots, [])
            _assert_legacy_reopenable(
                self,
                prior.staged_db_path,
                identity,
                fts5_available=False,
            )
            # a second fresh attempt fails identically without accumulating
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_migration._copy_store_into_stage",
                    side_effect=MigrationPreflightError(
                        "SCHEMA.COPY_FAILED"
                    ),
                ),
            ):
                second = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(second, SchemaUpgradeFailure)
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.bak"
                    )
                ),
                [],
            )
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.locator"
                    )
                ),
                [],
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(coordinator.state, "READY")

    def test_seal_failure_keeps_old_schema_and_ready_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_migration.StageSealer.seal",
                    side_effect=MigrationPreflightError(
                        "SCHEMA.SEAL_FAILED"
                    ),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self._assert_failure_preserves_prior(
                failure,
                coordinator,
                identity,
                prior,
                store_before=store_before,
                expected_code="SCHEMA.SEAL_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=False,
            )

    def test_journal_write_failure_cancels_and_restores_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._write_activation_journal",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self._assert_failure_preserves_prior(
                failure,
                coordinator,
                identity,
                prior,
                store_before=store_before,
                expected_code="ACTIVATION.JOURNAL_WRITE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                retry = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(retry, SchemaUpgradeReport)
            report = cast(SchemaUpgradeReport, retry)
            self.assertEqual(report.activated_generation, 4)

    def test_db_replace_failure_auto_restores_ready_old_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._replace_activation_file",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self._assert_failure_preserves_prior(
                failure,
                coordinator,
                identity,
                prior,
                store_before=store_before,
                expected_code="ACTIVATION.DB_REPLACE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )

    def test_manifest_publish_failure_rolls_back_ready_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._publish_activation_manifest",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.error_code, "SCHEMA.FAILED")
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertFalse(identity.canonical_sidecar_path.exists())
            _assert_legacy_reopenable(
                self,
                prior.staged_db_path,
                identity,
                fts5_available=False,
            )

    def test_generation_journal_write_failure_rolls_back_ready_service(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            calls = {"count": 0}
            original_write = tm_sqlite_store._write_activation_journal

            def fail_generation_journal(
                record: _ActivationJournalRecord,
                journal_path: Path,
                *,
                expected_final_identity: _ActivationFileIdentity | None,
            ) -> object:
                calls["count"] += 1
                if calls["count"] == 4:
                    raise OSError("injected")
                return original_write(
                    record,
                    journal_path,
                    expected_final_identity=expected_final_identity,
                )

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._write_activation_journal",
                    side_effect=fail_generation_journal,
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self._assert_failure_preserves_prior(
                failure,
                coordinator,
                identity,
                prior,
                store_before=store_before,
                expected_code="ACTIVATION.JOURNAL_WRITE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )

    def test_crash_window_recovery_completes_durable_upgrade(self) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, prior, coordinator, _digest = _legacy_fixture(
                        root,
                        fts5_available=fts5_available,
                    )
                    store_before = prior.staged_db_path.read_bytes()
                    service = _service(coordinator, identity)
                    calls = {"count": 0}
                    original_publish = (
                        tm_sqlite_store.ResourceStoreCoordinator.publish_activation
                    )

                    def crash_after_publish(
                        self_ref: ResourceStoreCoordinator,
                        preparation: _ActivationPreparation,
                        handle: _ActivationJournalHandle,
                    ) -> int:
                        calls["count"] += 1
                        if calls["count"] == 1:
                            generation = original_publish(
                                self_ref,
                                preparation,
                                handle,
                            )
                            raise OSError("injected crash after durable publish")
                        return original_publish(self_ref, preparation, handle)

                    with (
                        patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=fts5_available,
                        ),
                        patch.object(
                            tm_sqlite_store.ResourceStoreCoordinator,
                            "publish_activation",
                            crash_after_publish,
                        ),
                    ):
                        outcome = service.upgrade_schema(
                            prior.staged_db_path
                        )
                    self.assertIsInstance(outcome, SchemaUpgradeReport)
                    report = cast(SchemaUpgradeReport, outcome)
                    self.assertEqual(report.activated_generation, 4)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(coordinator.current_generation, 4)
                    self.assertTrue(identity.canonical_sidecar_path.is_file())
                    _assert_v2_active_store(
                        self,
                        identity.canonical_sidecar_path,
                        identity,
                        fts5_available=fts5_available,
                    )
                    self.assertEqual(
                        prior.staged_db_path.read_bytes(),
                        store_before,
                    )

    def test_unprovable_rollback_fails_stop_with_unverified_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._write_activation_journal",
                    side_effect=OSError("injected"),
                ),
                patch(
                    "tm_sqlite_store.ResourceStoreCoordinator."
                    "cancel_prepared_activation",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.UNVERIFIED,
            )
            self.assertEqual(len(failure.recovery_locators), 1)
            locator = failure.recovery_locators[0]
            self.assertEqual(
                locator.expected_digest,
                failure.active_store_preservation.before_digest,
            )
            # the locator is a strict regular single-link file whose
            # current bytes honestly equal the preserved before digest
            self.assertTrue(locator.path.exists())
            locator_stat = os.lstat(locator.path)
            self.assertTrue(stat.S_ISREG(locator_stat.st_mode))
            self.assertEqual(locator_stat.st_nlink, 1)
            self.assertEqual(
                _file_digest(locator.path),
                locator.expected_digest,
            )
            self.assertEqual(
                locator.expected_digest,
                hashlib.sha256(store_before).hexdigest(),
            )
            # the unexposed Connection.backup is removed, leaving only the
            # journal-owned byte-exact activation backup that is exposed
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.bak"
                    )
                ),
                [],
            )
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.locator"
                    )
                ),
                [],
            )

    def test_db_replaced_plus_unprovable_rollback_exposes_byte_exact_locator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._publish_activation_manifest",
                    side_effect=OSError("injected"),
                ),
                patch.object(
                    tm_sqlite_store.ResourceStoreCoordinator,
                    "rollback_durable_activation",
                    side_effect=OSError("injected rollback"),
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertFalse(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.UNVERIFIED,
            )
            self.assertEqual(coordinator.state, "ACTIVATING")
            # DB_REPLACED is durable: the canonical now holds the v2 store
            # while the old store path stays byte-identical
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            self.assertEqual(
                _active_meta(identity.canonical_sidecar_path)[
                    "schema_version"
                ],
                "2",
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(len(failure.recovery_locators), 1)
            locator = failure.recovery_locators[0]
            self.assertEqual(locator.asset_kind.value, "ACTIVE_STORE")
            # the locator is a strict regular single-link file whose
            # current bytes honestly equal the preserved before digest,
            # and it is the journal-owned byte-exact activation backup,
            # never the v2 live canonical
            self.assertTrue(locator.path.exists())
            self.assertNotEqual(locator.path, identity.canonical_sidecar_path)
            self.assertNotEqual(locator.path, prior.staged_db_path)
            self.assertIn("localcat-recovery", locator.path.name)
            locator_stat = os.lstat(locator.path)
            self.assertTrue(stat.S_ISREG(locator_stat.st_mode))
            self.assertEqual(locator_stat.st_nlink, 1)
            self.assertEqual(
                _file_digest(locator.path),
                locator.expected_digest,
            )
            self.assertEqual(
                locator.expected_digest,
                failure.active_store_preservation.before_digest,
            )
            self.assertEqual(
                locator.expected_digest,
                hashlib.sha256(store_before).hexdigest(),
            )
            # only the exposed byte-exact activation backup remains: the
            # unexposed Connection.backup and locator snapshot are gone
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.bak"
                    )
                ),
                [],
            )
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.locator"
                    )
                ),
                [],
            )
            recovery_backups = list(
                root.glob("..prior.sqlite3.localcat-recovery.*.database.bak")
            )
            self.assertEqual(recovery_backups, [locator.path])

    def test_repeated_seal_and_journal_failures_do_not_accumulate_backups(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            for _attempt in range(2):
                with (
                    patch("tm_sqlite_store._probe_fts5", return_value=False),
                    patch(
                        "tm_migration.StageSealer.seal",
                        side_effect=MigrationPreflightError(
                            "SCHEMA.SEAL_FAILED"
                        ),
                    ),
                ):
                    outcome = service.upgrade_schema(prior.staged_db_path)
                self.assertIsInstance(outcome, SchemaUpgradeFailure)
                self.assertEqual(
                    list(
                        root.glob(
                            "..prior.sqlite3."
                            "localcat-schema-upgrade.*.bak"
                        )
                    ),
                    [],
                )
                self.assertEqual(
                    list(
                        root.glob(
                            "..prior.sqlite3."
                            "localcat-schema-upgrade.*.locator"
                        )
                    ),
                    [],
                )
                self.assertEqual(
                    prior.staged_db_path.read_bytes(),
                    store_before,
                )
                self.assertEqual(coordinator.state, "READY")
            for _attempt in range(2):
                with (
                    patch("tm_sqlite_store._probe_fts5", return_value=False),
                    patch(
                        "tm_sqlite_store._write_activation_journal",
                        side_effect=OSError("injected"),
                    ),
                ):
                    outcome = service.upgrade_schema(prior.staged_db_path)
                self.assertIsInstance(outcome, SchemaUpgradeFailure)
                self.assertEqual(
                    list(
                        root.glob(
                            "..prior.sqlite3."
                            "localcat-schema-upgrade.*.bak"
                        )
                    ),
                    [],
                )
                self.assertEqual(
                    list(
                        root.glob(
                            "..prior.sqlite3."
                            "localcat-schema-upgrade.*.locator"
                        )
                    ),
                    [],
                )
                self.assertEqual(
                    prior.staged_db_path.read_bytes(),
                    store_before,
                )
                self.assertEqual(coordinator.state, "READY")
            # one fresh successful upgrade keeps exactly its reported backup
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                success = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(success, SchemaUpgradeReport)
            report = cast(SchemaUpgradeReport, success)
            backups = list(
                root.glob(
                    "..prior.sqlite3.localcat-schema-upgrade.*.bak"
                )
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(_file_digest(backups[0]), report.backup_digest)
            self.assertEqual(
                list(
                    root.glob(
                        "..prior.sqlite3.localcat-schema-upgrade.*.locator"
                    )
                ),
                [],
            )

    def test_activate_cleanup_failure_retries_cleanup_and_fails_honestly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, prior, coordinator, _digest = _legacy_fixture(
                root,
                fts5_available=False,
            )
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            calls = {"count": 0}
            original_remove = tm_sqlite_store._remove_recovery_backups
            original_guard = tm_sqlite_store._require_schema_upgrade_ticket_guard

            def fail_guard_once(
                coordinator_ref: ResourceStoreCoordinator,
                view: Any,
                ticket: Any,
                captures: Any,
            ) -> None:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ActivationPreparationError(
                        "ACTIVATION.UPGRADE_SNAPSHOT_STALE",
                        retryable=True,
                    )
                original_guard(coordinator_ref, view, ticket, captures)

            cleanup_calls = {"count": 0}

            def fail_cleanup_once(owned_paths: Any) -> None:
                cleanup_calls["count"] += 1
                if cleanup_calls["count"] == 1:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_BACKUP_INVALID",
                        retryable=False,
                    )
                original_remove(owned_paths)

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._require_schema_upgrade_ticket_guard",
                    side_effect=fail_guard_once,
                ),
                patch(
                    "tm_sqlite_store._remove_recovery_backups",
                    side_effect=fail_cleanup_once,
                ),
            ):
                outcome = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(outcome, SchemaUpgradeFailure)
            failure = cast(SchemaUpgradeFailure, outcome)
            self.assertEqual(failure.error_code, "ACTIVATION.CLEANUP_FAILED")
            self.assertEqual(failure.stage, "ACTIVATION")
            self.assertTrue(failure.retryable)
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.VERIFIED_UNCHANGED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(coordinator.state, "READY")
            # a fresh upgrade succeeds after the retried cleanup
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                retry = service.upgrade_schema(prior.staged_db_path)
            self.assertIsInstance(retry, SchemaUpgradeReport)
            report = cast(SchemaUpgradeReport, retry)
            self.assertEqual(report.activated_generation, 4)


if __name__ == "__main__":
    unittest.main()
