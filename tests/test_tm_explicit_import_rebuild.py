"""Task 5.10 explicit import/rebuild disambiguation tests.

The suite drives the two public entry points
``TMMigrationService.import_snapshot`` and
``TMMigrationService.rebuild_from_snapshot`` over an already-active
resource whose configured JSONL has diverged.  A successful call must
replace the active canonical with one exact snapshot under a fresh
collision-resistant ``store.import.<uuid>`` store id and the next
generation, clearing ``SOURCE_DIVERGED``; the same bytes imported twice
succeed again with a new store id and snapshot id.  Public failures
auto-restore the READY prior service (cancel before any durable
journal, rollback for durable pending phases, recovery completion for
a durable ``GENERATION_PUBLISHED`` journal whose candidate set is
provable) without any caller-side ``rollback_durable_activation``, and
a rollback that cannot be proven fails stop with honest UNVERIFIED
preservation evidence.  The replacement journals are proven across the
Task 5.8/5.9 seams: pending phases accept the exact prior store id,
``GENERATION_PUBLISHED`` accepts prior (crash window) or candidate
(idempotent replay), and any third id is rejected.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable, cast
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
import tm_gate_b
import tm_activation_recovery
import tm_sqlite_store
from tm_activation_journal import (
    _ActivationFileIdentity,
    _ActivationJournalHandle,
    _ActivationJournalRecord,
    _ActivationPreparation,
    _ensure_activation_lineage_marker,
)
from tm_contracts import (
    AssetPreservationState,
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    MutableStageRef,
    SealedStage,
    SourceBindingState,
    snapshot_receipt_digest,
)
from tm_migration import (
    MigrationPreflightError,
    TMMigrationService,
    _scan_jsonl,
)
from tm_sqlite_store import (
    ActivationPreparationError,
    ActivationRecoveryReport,
    ResourceStoreCoordinator,
    SQLiteTMStore,
    _ActivationJournalPhase,
    _activation_journal_path,
    _parse_activation_journal_bytes,
    initialize_stage_schema,
)
from tm_stage_sealer import StageSealer
from tests.test_tm_activation import (
    SOURCE_BYTES,
    _draft,
    _existing_fixture,
    _identity,
    _publish_prior_binding,
    _prior_stage,
)

_STORE_IMPORT_RE = re.compile(r"^store\.import\.[0-9a-f]{32}$")
_BATCH_IMPORT_RE = re.compile(r"^import\.[0-9a-f]{32}$")
_SNAPSHOT_IMPORT_RE = re.compile(r"^snapshot\.import\.[0-9a-f]{24}$")


def _diverged_fixture(
    root: Path,
    *,
    fts5_available: bool,
) -> tuple[
    CanonicalResourceIdentity,
    MutableStageRef,
    SQLiteTMStore,
    ResourceStoreCoordinator,
]:
    """One active prior canonical whose configured JSONL has diverged."""

    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
    prior = _prior_stage(root, identity)
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        initialize_stage_schema(prior, canonical_store_id="store.primary")
        store = SQLiteTMStore(
            prior,
            canonical_store_id="store.primary",
        )
    coordinator = store.coordinator
    _ = store.append_batch(
        batch_id="migration.prior",
        kind="migration",
        drafts=(_draft("prior", "canonical"),),
        source_digest=hashlib.sha256(SOURCE_BYTES).hexdigest(),
        source_path=identity.configured_jsonl_path,
    )
    _publish_prior_binding(store, identity)
    _ensure_activation_lineage_marker(identity)
    identity.configured_jsonl_path.write_bytes(
        SOURCE_BYTES + b'{"source":"new","target":"ext"}\n'
    )
    observation = store.source_binding_monitor.observe()
    if observation.state is not SourceBindingState.SOURCE_DIVERGED:
        raise AssertionError("fixture must start in SOURCE_DIVERGED")
    return identity, prior, store, coordinator


def _service(
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=coordinator.canonical_store_id,
        coordinator=coordinator,
    )


def _explicit_preflight(
    service: TMMigrationService,
    identity: CanonicalResourceIdentity,
) -> contract_module.MigrationPreflight:
    """Explicit scan/build seam without the initial-migration sidecar preflight.

    Task 5.10 candidate helpers must not call the public initial-migration
    ``preflight`` (whose fail-closed ``MIGRATION.MANIFEST_WITHOUT_SIDECAR``
    rejection applies to manifest-without-sidecar claims regardless of
    coordinator authority); they use the same explicit scan/build seam as
    ``TMMigrationService.import_snapshot``.
    """

    source = identity.configured_jsonl_path
    service._validate_source_preconditions(source)
    return _scan_jsonl(source)


def _read_manifest(
    identity: CanonicalResourceIdentity,
) -> contract_module.SnapshotManifest:
    manifest = contract_module.contract_from_json(
        identity.snapshot_manifest_path.read_text(encoding="utf-8")
    )
    if not isinstance(manifest, contract_module.SnapshotManifest):
        raise AssertionError("expected a SnapshotManifest")
    return manifest


def _journal_phase(
    identity: CanonicalResourceIdentity,
) -> _ActivationJournalPhase:
    journal_path = _activation_journal_path(identity)
    return _parse_activation_journal_bytes(
        journal_path.read_bytes(),
        expected_journal_path=journal_path,
    ).phase


def _import_batch_row(
    identity: CanonicalResourceIdentity,
) -> tuple[str, str]:
    connection = sqlite3.connect(identity.canonical_sidecar_path)
    try:
        rows = connection.execute(
            "SELECT batch_id, kind FROM tm_origin_batch"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise AssertionError("expected exactly one origin batch")
    return str(rows[0][0]), str(rows[0][1])


def _assert_success_report(
    testcase: unittest.TestCase,
    report: MigrationReport,
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
    *,
    jsonl_before: bytes,
    expected_generation: int,
) -> None:
    testcase.assertIsInstance(report, MigrationReport)
    new_store_id = report.canonical_store_id
    testcase.assertRegex(new_store_id, _STORE_IMPORT_RE)
    testcase.assertNotEqual(
        new_store_id,
        f"store.import.{hashlib.sha256(jsonl_before).hexdigest()[:16]}",
    )
    testcase.assertEqual(report.resource_id, identity.resource_id)
    testcase.assertEqual(
        report.source_digest,
        hashlib.sha256(jsonl_before).hexdigest(),
    )
    testcase.assertEqual(report.activated_generation, expected_generation)
    testcase.assertEqual(report.migrated_count, 4)
    testcase.assertEqual(report.variant_count, 1)
    testcase.assertEqual(report.skipped_count, 0)
    testcase.assertEqual(len(report.diagnostics), 1)
    testcase.assertRegex(
        report.snapshot_receipt.snapshot_id,
        _SNAPSHOT_IMPORT_RE,
    )
    testcase.assertEqual(
        report.snapshot_receipt.canonical_store_id,
        new_store_id,
    )
    testcase.assertEqual(
        report.snapshot_receipt.jsonl_digest,
        hashlib.sha256(jsonl_before).hexdigest(),
    )
    testcase.assertEqual(report.snapshot_receipt.record_count, 4)
    testcase.assertTrue(report.canonical_exact_available)
    testcase.assertFalse(report.context_available)
    testcase.assertFalse(report.fuzzy_available)

    testcase.assertEqual(coordinator.state, "READY")
    testcase.assertEqual(coordinator.canonical_store_id, new_store_id)
    testcase.assertEqual(coordinator.current_generation, expected_generation)
    testcase.assertEqual(
        coordinator.active_store_path,
        identity.canonical_sidecar_path,
    )
    batch_id, batch_kind = _import_batch_row(identity)
    testcase.assertEqual(batch_kind, "import")
    testcase.assertRegex(batch_id, _BATCH_IMPORT_RE)
    manifest = _read_manifest(identity)
    testcase.assertEqual(manifest.receipt.canonical_store_id, new_store_id)
    testcase.assertEqual(
        manifest.receipt.snapshot_id,
        report.snapshot_receipt.snapshot_id,
    )
    testcase.assertEqual(
        manifest.receipt_digest,
        snapshot_receipt_digest(manifest.receipt),
    )


def _assert_failure_preserves_prior(
    testcase: unittest.TestCase,
    failure: MigrationFailure,
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
    prior: MutableStageRef,
    *,
    jsonl_before: bytes,
    manifest_before: bytes | None,
    store_before: bytes,
    expected_code: str,
    expected_stage: str,
    expected_retryable: bool,
    expected_state: str = "READY",
    expected_sidecar_present: bool = False,
    expected_store_state: AssetPreservationState = (
        AssetPreservationState.VERIFIED_UNCHANGED
    ),
    expected_journal_present: bool = False,
) -> None:
    testcase.assertIsInstance(failure, MigrationFailure)
    testcase.assertEqual(failure.error_code, expected_code)
    testcase.assertEqual(failure.stage, expected_stage)
    testcase.assertEqual(failure.retryable, expected_retryable)
    testcase.assertEqual(failure.active_generation, 0)
    testcase.assertEqual(
        failure.original_source_preservation.state,
        AssetPreservationState.VERIFIED_UNCHANGED,
    )
    testcase.assertEqual(
        failure.original_source_preservation.before_digest,
        hashlib.sha256(jsonl_before).hexdigest(),
    )
    testcase.assertEqual(
        failure.active_store_preservation.state,
        expected_store_state,
    )
    testcase.assertEqual(
        failure.active_store_preservation.before_digest,
        hashlib.sha256(store_before).hexdigest(),
    )
    testcase.assertEqual(coordinator.state, expected_state)
    testcase.assertEqual(coordinator.canonical_store_id, "store.primary")
    testcase.assertEqual(coordinator.current_generation, 0)
    testcase.assertEqual(
        identity.configured_jsonl_path.read_bytes(),
        jsonl_before,
    )
    if manifest_before is None:
        testcase.assertFalse(identity.snapshot_manifest_path.exists())
    else:
        testcase.assertEqual(
            identity.snapshot_manifest_path.read_bytes(),
            manifest_before,
        )
    testcase.assertEqual(prior.staged_db_path.read_bytes(), store_before)
    testcase.assertEqual(
        identity.canonical_sidecar_path.exists(),
        expected_sidecar_present,
    )
    testcase.assertEqual(
        _activation_journal_path(identity).exists(),
        expected_journal_present,
    )


def _drive_to_prepared(
    service: TMMigrationService,
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
    *,
    fts5_available: bool = False,
) -> tuple[_ActivationPreparation, _ActivationJournalHandle, str]:
    """Drive one replacement activation to a durable PREPARED journal.

    Coordinator-level seam for the recovery authority matrix: the service
    auto-reconciles public failures, so these tests build the journal
    phases directly through the coordinator public API.
    """

    import uuid

    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        preflight = _explicit_preflight(service, identity)
        origin_token = uuid.uuid4().hex
        new_store_id = f"store.import.{origin_token}"
        stage = service._build_stage(
            identity.configured_jsonl_path,
            preflight=preflight,
            canonical_store_id=new_store_id,
            batch_kind="import",
            batch_prefix="import",
            snapshot_prefix="snapshot.import",
            stage_prefix="import",
            path_salt=uuid.uuid4().hex,
            batch_id=f"import.{origin_token}",
        )
        sealed = StageSealer(
            registry=coordinator.sealed_registry,
            canonical_store_id=new_store_id,
        ).seal(
            stage,
            expected_prior_generation=coordinator.current_generation,
        )
        prepared = coordinator.activate_replacement(sealed)
        handle = coordinator.publish_prepared_activation(prepared)
    return prepared, handle, new_store_id


class ExplicitImportRebuildSuccessTests(unittest.TestCase):
    def test_import_and_rebuild_replace_the_active_canonical(self) -> None:
        for fts5_available in (True, False):
            for operation in ("import_snapshot", "rebuild_from_snapshot"):
                with self.subTest(
                    fts5_available=fts5_available,
                    operation=operation,
                ):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        (
                            identity,
                            _prior,
                            store,
                            coordinator,
                        ) = _diverged_fixture(
                            root,
                            fts5_available=fts5_available,
                        )
                        jsonl_before = (
                            identity.configured_jsonl_path.read_bytes()
                        )
                        self.assertEqual(coordinator.current_generation, 0)
                        self.assertEqual(
                            store.source_binding_monitor.observe().state,
                            SourceBindingState.SOURCE_DIVERGED,
                        )
                        service = _service(coordinator, identity)
                        with patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=fts5_available,
                        ):
                            outcome = getattr(service, operation)(
                                identity.configured_jsonl_path,
                                identity.resource_id,
                            )
                        self.assertIsInstance(outcome, MigrationReport)
                        _assert_success_report(
                            self,
                            cast(MigrationReport, outcome),
                            coordinator,
                            identity,
                            jsonl_before=jsonl_before,
                            expected_generation=1,
                        )
                        self.assertEqual(
                            _journal_phase(identity),
                            _ActivationJournalPhase.GENERATION_PUBLISHED,
                        )
                        # a fresh candidate-id coordinator replays the
                        # completed journal idempotently without a second
                        # generation
                        fresh = ResourceStoreCoordinator(
                            resource_identity=identity,
                            canonical_store_id=cast(
                                MigrationReport, outcome
                            ).canonical_store_id,
                        )
                        with patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=fts5_available,
                        ):
                            replay = fresh.recover_durable_activation()
                        self.assertEqual(
                            replay,
                            ActivationRecoveryReport(
                                phase="GENERATION_PUBLISHED",
                                action="COMPLETED",
                                generation=1,
                            ),
                        )
                        self.assertEqual(fresh.current_generation, 1)

    def test_identical_snapshot_import_twice_succeeds_with_new_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            service = _service(coordinator, identity)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                first = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(first, MigrationReport)
            first_report = cast(MigrationReport, first)
            _assert_success_report(
                self,
                first_report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )
            first_store_id = first_report.canonical_store_id
            first_snapshot_id = first_report.snapshot_receipt.snapshot_id
            first_batch_id, _ = _import_batch_row(identity)

            # the exact same bytes again succeed as generation N+1 with a
            # distinct fresh store id and snapshot id (never a digest
            # collision and never IMPORT.COORDINATOR_MISMATCH); the exact
            # same service instance adopts the published authority and
            # needs no caller-side renewal
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                second = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(second, MigrationReport)
            second_report = cast(MigrationReport, second)
            _assert_success_report(
                self,
                second_report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=2,
            )
            self.assertNotEqual(
                second_report.canonical_store_id,
                first_store_id,
            )
            self.assertNotEqual(
                second_report.snapshot_receipt.snapshot_id,
                first_snapshot_id,
            )
            second_batch_id, _ = _import_batch_row(identity)
            self.assertNotEqual(second_batch_id, first_batch_id)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )


class ExplicitImportValidationTests(unittest.TestCase):
    def test_validation_failures_never_mutate(self) -> None:
        def run_case(
            case_name: str,
            invoke: Callable[
                [TMMigrationService, Path],
                contract_module.MigrationOutcome,
            ],
            *,
            expected_code: str,
        ) -> None:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        prior,
                        store,
                        coordinator,
                    ) = _diverged_fixture(
                        root,
                        fts5_available=False,
                    )
                    jsonl_before = (
                        identity.configured_jsonl_path.read_bytes()
                    )
                    manifest_before = (
                        identity.snapshot_manifest_path.read_bytes()
                    )
                    store_before = prior.staged_db_path.read_bytes()
                    service = _service(coordinator, identity)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        outcome = invoke(service, root)
                    self.assertIsInstance(outcome, MigrationFailure)
                    failure = cast(MigrationFailure, outcome)
                    _assert_failure_preserves_prior(
                        self,
                        failure,
                        coordinator,
                        identity,
                        prior,
                        jsonl_before=jsonl_before,
                        manifest_before=manifest_before,
                        store_before=store_before,
                        expected_code=expected_code,
                        expected_stage="PREFLIGHT",
                        expected_retryable=False,
                    )
                    self.assertEqual(
                        store.source_binding_monitor.observe().state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )

        run_case(
            "wrong_source_path",
            lambda service, root: service.import_snapshot(
                root / "other.jsonl",
                "tm.primary",
            ),
            expected_code="MIGRATION.RESOURCE_IDENTITY_MISMATCH",
        )
        run_case(
            "wrong_resource_id",
            lambda service, root: service.import_snapshot(
                _identity(root).configured_jsonl_path,
                "tm.other",
            ),
            expected_code="MIGRATION.RESOURCE_IDENTITY_MISMATCH",
        )
        for invalid in ("", "   ", 7):
            run_case(
                f"invalid_resource_id_{invalid!r}",
                lambda service, root, value=invalid: service.import_snapshot(
                    _identity(root).configured_jsonl_path,
                    cast(str, value),
                ),
                expected_code="IMPORT.RESOURCE_ID_INVALID",
            )
        run_case(
            "non_native_source_path",
            lambda service, root: service.import_snapshot(
                cast(
                    Path,
                    cast(object, str(_identity(root).configured_jsonl_path)),
                ),
                "tm.primary",
            ),
            expected_code="IMPORT.FAILED",
        )

    def test_empty_configured_source_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            identity.configured_jsonl_path.write_bytes(b"")
            service = _service(coordinator, identity)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=b"",
                manifest_before=(
                    identity.snapshot_manifest_path.read_bytes()
                ),
                store_before=prior.staged_db_path.read_bytes(),
                expected_code="MIGRATION.SOURCE_EMPTY",
                expected_stage="PREFLIGHT",
                expected_retryable=False,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_coordinator_authority_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            mismatched = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.other",
                coordinator=coordinator,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome = mismatched.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="IMPORT.COORDINATOR_MISMATCH",
                expected_stage="PREFLIGHT",
                expected_retryable=False,
            )

            without_coordinator = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with self.assertRaises(MigrationPreflightError) as raised:
                without_coordinator.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertEqual(
                raised.exception.error_code,
                "IMPORT.COORDINATOR_UNAVAILABLE",
            )
            with self.assertRaises(TypeError):
                TMMigrationService(
                    resource_identity=identity,
                    canonical_store_id="store.primary",
                    coordinator=cast(Any, "not-a-coordinator"),
                )

    def test_gate_b_rejection_restores_ready_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_gate_b.GateBEvaluator.evaluate",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.GATE_B_REJECTED",
                        retryable=False,
                    ),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="ACTIVATION.GATE_B_REJECTED",
                expected_stage="ACTIVATION",
                expected_retryable=False,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )


class ExplicitImportCanonicalCorruptionTests(unittest.TestCase):
    def test_canonical_ledger_and_ancestry_tamper_fails_before_publication(
        self,
    ) -> None:
        cases = (
            (
                "ancestry",
                "UPDATE tm_origin_batch SET status = 'staged', "
                "completed_revision = NULL",
            ),
            (
                "ledger status",
                "UPDATE tm_snapshot_receipt SET status = 'issued'",
            ),
            (
                "ledger path",
                "UPDATE tm_snapshot_receipt SET destination_jsonl_path = "
                "'/other/tm.jsonl'",
            ),
        )
        for name, statement in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        prior,
                        store,
                        coordinator,
                    ) = _diverged_fixture(root, fts5_available=False)
                    jsonl_before = (
                        identity.configured_jsonl_path.read_bytes()
                    )
                    manifest_before = (
                        identity.snapshot_manifest_path.read_bytes()
                    )
                    connection = sqlite3.connect(prior.staged_db_path)
                    try:
                        connection.execute(statement)
                        connection.commit()
                    finally:
                        connection.close()
                    # the service observes the tampered DB as the prior
                    # authority: the exact tampered bytes are what the
                    # import must preserve byte-for-byte
                    store_before = prior.staged_db_path.read_bytes()
                    service = _service(coordinator, identity)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        outcome = service.import_snapshot(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    # canonical ledger/ancestry corruption is never
                    # repaired by an explicit import: the operation fails
                    # before any durable publication, preserves the exact
                    # three assets, and leaves the prior canonical in
                    # place with its divergence intact
                    self.assertIsInstance(outcome, MigrationFailure)
                    failure = cast(MigrationFailure, outcome)
                    _assert_failure_preserves_prior(
                        self,
                        failure,
                        coordinator,
                        identity,
                        prior,
                        jsonl_before=jsonl_before,
                        manifest_before=manifest_before,
                        store_before=store_before,
                        expected_code="ACTIVATION.PRIOR_BINDING_INVALID",
                        expected_stage="ACTIVATION",
                        expected_retryable=False,
                    )
                    self.assertEqual(
                        store.source_binding_monitor.observe().state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertFalse(
                        _activation_journal_path(identity).exists()
                    )


class ExplicitImportPublicationFailureTests(unittest.TestCase):
    def test_publish_prepared_failure_cancels_and_restores_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._write_activation_journal",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="ACTIVATION.JOURNAL_WRITE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                retry = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(retry, MigrationReport)
            _assert_success_report(
                self,
                cast(MigrationReport, retry),
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )

    def test_db_replace_failure_auto_restores_ready_old_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._replace_activation_file",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="ACTIVATION.DB_REPLACE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            # a fresh prior-id coordinator replays the retained CANCELLED
            # terminal without any caller-side rollback call
            fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                replay = fresh.recover_durable_activation()
            self.assertEqual(
                replay,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)
            # a retry after the auto-restored failure succeeds with a fresh
            # store id and the next generation
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                retry = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(retry, MigrationReport)
            retry_report = cast(MigrationReport, retry)
            _assert_success_report(
                self,
                retry_report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_manifest_publish_failure_auto_restores_ready_old_service(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._publish_activation_manifest",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="IMPORT.FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=False,
                expected_sidecar_present=False,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                retry = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(retry, MigrationReport)
            _assert_success_report(
                self,
                cast(MigrationReport, retry),
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )

    def test_generation_journal_write_failure_auto_restores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            calls = {"count": 0}
            original_write = tm_sqlite_store._write_activation_journal

            def fail_generation_journal(
                record: _ActivationJournalRecord,
                journal_path: Path,
                *,
                expected_final_identity: _ActivationFileIdentity | None,
            ) -> _ActivationJournalHandle:
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
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            _assert_failure_preserves_prior(
                self,
                failure,
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="ACTIVATION.JOURNAL_WRITE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )

    def test_unprovable_rollback_fails_stop_with_unverified_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._replace_activation_file",
                    side_effect=OSError("injected"),
                ),
                patch(
                    "tm_activation_recovery._revalidate_recovered_prior_set",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.RECOVERY_PRIOR_SET_INVALID",
                        retryable=False,
                    ),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            failure = cast(MigrationFailure, outcome)
            # rollback could not prove the restored prior set, so the
            # service fails stop with UNVERIFIED evidence and never claims
            # VERIFIED_UNCHANGED
            self.assertEqual(
                failure.active_store_preservation.state,
                AssetPreservationState.UNVERIFIED,
            )
            self.assertFalse(failure.retryable)
            self.assertEqual(len(failure.recovery_locators), 1)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertEqual(
                _journal_phase(identity),
                _ActivationJournalPhase.PREPARED,
            )
            self.assertEqual(prior.staged_db_path.read_bytes(), store_before)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )

    def test_crash_window_token_consume_failure_completes_via_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            service = _service(coordinator, identity)
            def fail_consume(
                token: contract_module._ActivationToken,
            ) -> None:
                _ = token
                raise __import__(
                    "tm_stage_sealer"
                ).StageSealError("SEALER.TOKEN_CONSUME_FAILED")

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                    patch.object(
                        coordinator.sealed_registry,
                        "consume",
                        side_effect=fail_consume,
                    ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            # the GENERATION_PUBLISHED journal is durable and the candidate
            # active set is provable: the service completes the candidate
            # and reports success with the new generation
            self.assertIsInstance(outcome, MigrationReport)
            report = cast(MigrationReport, outcome)
            _assert_success_report(
                self,
                report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )

    def test_crash_window_marker_failure_high_level_op_returns_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            service = _service(coordinator, identity)
            calls = {"count": 0}

            def fail_marker_once(value: CanonicalResourceIdentity) -> None:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ActivationPreparationError(
                        "ACTIVATION.LINEAGE_MARKER_FAILED",
                        retryable=True,
                    )
                _ensure_activation_lineage_marker(value)

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._ensure_activation_lineage_marker",
                    side_effect=fail_marker_once,
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationReport)
            report = cast(MigrationReport, outcome)
            _assert_success_report(
                self,
                report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )
            # the completed journal is retained: a fresh prior-id
            # coordinator recovers the candidate (crash-window authority)
            fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                recovered = fresh.recover_durable_activation()
            self.assertEqual(
                recovered,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(
                fresh.canonical_store_id,
                report.canonical_store_id,
            )
            self.assertEqual(fresh.current_generation, 1)
            # the recovered authority was adopted by the original
            # coordinator and service: the exact same service instance
            # performs a second byte-identical import as generation 2
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                second = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(second, MigrationReport)
            second_report = cast(MigrationReport, second)
            _assert_success_report(
                self,
                second_report,
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=2,
            )
            self.assertNotEqual(
                second_report.canonical_store_id,
                report.canonical_store_id,
            )


class ExplicitImportManifestDivergenceTests(unittest.TestCase):
    def test_missing_prior_manifest_import_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            identity.snapshot_manifest_path.unlink()
            service = _service(coordinator, identity)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationReport)
            _assert_success_report(
                self,
                cast(MigrationReport, outcome),
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )
            self.assertTrue(identity.snapshot_manifest_path.is_file())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_missing_prior_manifest_failure_preserves_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            identity.snapshot_manifest_path.unlink()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._replace_activation_file",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            _assert_failure_preserves_prior(
                self,
                cast(MigrationFailure, outcome),
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=None,
                store_before=store_before,
                expected_code="ACTIVATION.DB_REPLACE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_externally_altered_manifest_import_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            altered = b'{"external":"tampered"}\n'
            identity.snapshot_manifest_path.write_bytes(altered)
            service = _service(coordinator, identity)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationReport)
            _assert_success_report(
                self,
                cast(MigrationReport, outcome),
                coordinator,
                identity,
                jsonl_before=jsonl_before,
                expected_generation=1,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_externally_altered_manifest_failure_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            altered = b'{"external":"tampered"}\n'
            identity.snapshot_manifest_path.write_bytes(altered)
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._replace_activation_file",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            _assert_failure_preserves_prior(
                self,
                cast(MigrationFailure, outcome),
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=altered,
                store_before=store_before,
                expected_code="ACTIVATION.DB_REPLACE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                altered,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_foreign_manifest_entries_fail_closed(self) -> None:
        for entry_kind in ("symlink", "directory"):
            with self.subTest(entry_kind=entry_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        prior,
                        store,
                        coordinator,
                    ) = _diverged_fixture(root, fts5_available=False)
                    jsonl_before = (
                        identity.configured_jsonl_path.read_bytes()
                    )
                    store_before = prior.staged_db_path.read_bytes()
                    manifest_path = identity.snapshot_manifest_path
                    original_bytes = manifest_path.read_bytes()
                    manifest_path.unlink()
                    if entry_kind == "symlink":
                        manifest_path.symlink_to(identity.configured_jsonl_path)
                    else:
                        manifest_path.mkdir()
                    service = _service(coordinator, identity)
                    with (
                        patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=False,
                        ),
                        patch(
                            "tm_activation_recovery._replace_activation_file",
                            side_effect=OSError("injected"),
                        ),
                    ):
                        outcome = service.import_snapshot(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    self.assertIsInstance(outcome, MigrationFailure)
                    failure = cast(MigrationFailure, outcome)
                    # the foreign entry is never touched or followed
                    if entry_kind == "symlink":
                        self.assertTrue(manifest_path.is_symlink())
                    else:
                        self.assertTrue(manifest_path.is_dir())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        jsonl_before,
                    )
                    self.assertEqual(
                        prior.staged_db_path.read_bytes(),
                        store_before,
                    )
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(
                        store.source_binding_monitor.observe().state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertFalse(
                        identity.canonical_sidecar_path.exists()
                    )
                    self.assertIsNotNone(failure.error_code)
                    # restore for the next subtest iteration is unnecessary
                    # (fresh tempdir), but keep the fixture tidy
                    if entry_kind == "symlink":
                        manifest_path.unlink()
                    else:
                        manifest_path.rmdir()
                    manifest_path.write_bytes(original_bytes)


class ExplicitImportRecoveryTests(unittest.TestCase):
    def test_prepared_replacement_cancels_on_prior_id_coordinator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            manifest_before = identity.snapshot_manifest_path.read_bytes()
            store_before = prior.staged_db_path.read_bytes()
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_sqlite_store._write_activation_journal",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            _assert_failure_preserves_prior(
                self,
                cast(MigrationFailure, outcome),
                coordinator,
                identity,
                prior,
                jsonl_before=jsonl_before,
                manifest_before=manifest_before,
                store_before=store_before,
                expected_code="ACTIVATION.JOURNAL_WRITE_FAILED",
                expected_stage="ACTIVATION",
                expected_retryable=True,
            )
            # a fresh prior-id coordinator replays the CANCELLED terminal
            fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                replay = fresh.recover_durable_activation()
            self.assertEqual(
                replay,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                rehydrated = SQLiteTMStore(
                    prior,
                    canonical_store_id="store.primary",
                )
            self.assertEqual(
                rehydrated.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_pending_phase_recovery_authority_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            service = _service(coordinator, identity)

            # PREPARED accepts the exact prior id (cancelled)
            _drive_to_prepared(service, coordinator, identity)
            prior_fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                report = prior_fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(prior_fresh.current_generation, 0)
            # the live coordinator still holds the retired preparation;
            # the no-journal rollback replays the CANCELLED terminal and
            # returns the same coordinator to READY for the next drive
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                coordinator.rollback_durable_activation()

            # PREPARED rejects the candidate id (still pending)
            _drive_to_prepared(service, coordinator, identity)
            candidate_fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.import.candidate",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    candidate_fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_MISMATCH",
            )
            # retire the still-pending journal before the next drive
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                coordinator.rollback_durable_activation()

            # PREPARED rejects an arbitrary third id
            _drive_to_prepared(service, coordinator, identity)
            third_fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.other",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    third_fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_MISMATCH",
            )

    def test_db_replaced_replacement_completes_on_prior_id_coordinator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            service = _service(coordinator, identity)
            prepared, handle, new_store_id = _drive_to_prepared(
                service,
                coordinator,
                identity,
            )
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._publish_activation_manifest",
                    side_effect=OSError("injected"),
                ),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, handle)
            self.assertEqual(
                _journal_phase(identity),
                _ActivationJournalPhase.DB_REPLACED,
            )
            # a fresh prior-id coordinator completes the DB_REPLACED
            # replacement and adopts the candidate store id
            fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                report = fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(fresh.state, "READY")
            self.assertEqual(fresh.current_generation, 1)
            self.assertEqual(fresh.canonical_store_id, new_store_id)
            self.assertRegex(fresh.canonical_store_id, _STORE_IMPORT_RE)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )
            self.assertEqual(
                fresh.active_store_path,
                identity.canonical_sidecar_path,
            )
            manifest = _read_manifest(identity)
            self.assertEqual(
                manifest.receipt.canonical_store_id,
                fresh.canonical_store_id,
            )

    def test_completed_replacement_prior_id_recovers_and_rollback_refused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            jsonl_before = identity.configured_jsonl_path.read_bytes()
            service = _service(coordinator, identity)
            prepared, handle, new_store_id = _drive_to_prepared(
                service,
                coordinator,
                identity,
            )
            calls = {"count": 0}

            def fail_marker_once(value: CanonicalResourceIdentity) -> None:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ActivationPreparationError(
                        "ACTIVATION.LINEAGE_MARKER_FAILED",
                        retryable=True,
                    )
                _ensure_activation_lineage_marker(value)

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._ensure_activation_lineage_marker",
                    side_effect=fail_marker_once,
                ),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, handle)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_FAILED",
            )
            self.assertEqual(
                _journal_phase(identity),
                _ActivationJournalPhase.GENERATION_PUBLISHED,
            )
            # a prior-id coordinator refuses rollback of the valid completed
            # candidate and completes it via recovery instead
            prior_fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    prior_fresh.rollback_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.ROLLBACK_COMPLETED_INVALID",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                recovered = prior_fresh.recover_durable_activation()
            self.assertEqual(
                recovered,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(prior_fresh.state, "READY")
            self.assertEqual(prior_fresh.canonical_store_id, new_store_id)
            self.assertEqual(prior_fresh.current_generation, 1)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                jsonl_before,
            )
            # a candidate-id coordinator replays idempotently without a
            # second generation
            candidate_fresh = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id=new_store_id,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                replay = candidate_fresh.recover_durable_activation()
            self.assertEqual(
                replay,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(candidate_fresh.current_generation, 1)
            # an arbitrary third store id is rejected for the completed
            # journal (still retained after the idempotent replays)
            third = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.other",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    third.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_MISMATCH",
            )

class ExplicitImportRecoveryAuthorityAdoptionTests(unittest.TestCase):
    def test_adopt_recovered_authority_guards_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                prior,
                _store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            service = _service(coordinator, identity)
            prepared, handle, new_store_id = _drive_to_prepared(
                service,
                coordinator,
                identity,
            )
            calls = {"count": 0}

            def fail_marker_once(value: CanonicalResourceIdentity) -> None:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise ActivationPreparationError(
                        "ACTIVATION.LINEAGE_MARKER_FAILED",
                        retryable=True,
                    )
                _ensure_activation_lineage_marker(cast(Any, value))

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._ensure_activation_lineage_marker",
                    side_effect=fail_marker_once,
                ),
            ):
                with self.assertRaisesRegex(
                    ActivationPreparationError,
                    "ACTIVATION.LINEAGE_MARKER_FAILED",
                ):
                    coordinator.publish_activation(
                        cast(Any, prepared),
                        cast(Any, handle),
                    )
            # the original coordinator is fail-stopped with a durable
            # GENERATION_PUBLISHED journal (crash-window authority)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertEqual(
                _journal_phase(identity),
                _ActivationJournalPhase.GENERATION_PUBLISHED,
            )
            recovered = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                recovery_report = recovered.recover_durable_activation()
            self.assertEqual(
                recovery_report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.canonical_store_id, new_store_id)
            self.assertEqual(recovered.current_generation, 1)

            # wrong type is rejected before any state is inspected
            with self.assertRaises(TypeError):
                coordinator.adopt_recovered_authority(
                    cast(Any, "not-a-coordinator")
                )

            # different immutable resource identity is rejected
            foreign = ResourceStoreCoordinator(
                resource_identity=_identity(root, "tm.other"),
                canonical_store_id="store.primary",
            )
            with self.assertRaisesRegex(
                ActivationPreparationError,
                "ACTIVATION.RECOVERY_IDENTITY_MISMATCH",
            ):
                coordinator.adopt_recovered_authority(foreign)

            # a non-READY recovered authority is rejected
            not_ready = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            not_ready._state = "ACTIVATING"
            with self.assertRaisesRegex(
                ActivationPreparationError,
                "ACTIVATION.RECOVERY_STATE_INVALID",
            ):
                coordinator.adopt_recovered_authority(not_ready)

            # an arbitrary unactivated authority (no recovered view) is
            # rejected even with the same resource identity
            arbitrary = ResourceStoreCoordinator(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with self.assertRaisesRegex(
                ActivationPreparationError,
                "ACTIVATION.RECOVERY_VIEW_INVALID",
            ):
                coordinator.adopt_recovered_authority(arbitrary)

            # a view whose canonical paths do not match the resource's
            # canonical sidecar pair is rejected
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                wrong_path = ResourceStoreCoordinator(
                    stage=prior,
                    canonical_store_id="store.primary",
                )
            with self.assertRaisesRegex(
                ActivationPreparationError,
                "ACTIVATION.RECOVERY_PATH_MISMATCH",
            ):
                coordinator.adopt_recovered_authority(wrong_path)

            # the proven recovered authority is adopted by the
            # coordinator-owned transition and returns READY
            self.assertEqual(
                coordinator.adopt_recovered_authority(recovered),
                "READY",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(
                coordinator.canonical_store_id,
                recovered.canonical_store_id,
            )
            self.assertEqual(coordinator.current_generation, 1)
            self.assertEqual(
                coordinator.active_store_path,
                identity.canonical_sidecar_path,
            )

            # adoption is not a generic setter: a second adoption on an
            # already-READY coordinator is rejected without mutation
            with self.assertRaisesRegex(
                ActivationPreparationError,
                "ACTIVATION.RECOVERY_ADOPTION_INVALID",
            ):
                coordinator.adopt_recovered_authority(recovered)
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(
                coordinator.canonical_store_id,
                recovered.canonical_store_id,
            )


class ExplicitImportLocalWriteTests(unittest.TestCase):
    def test_success_discards_writes_made_while_diverged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            store.append(_draft("local", "write-while-diverged"))
            service = _service(coordinator, identity)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationReport)
            _assert_success_report(
                self,
                cast(MigrationReport, outcome),
                coordinator,
                identity,
                jsonl_before=identity.configured_jsonl_path.read_bytes(),
                expected_generation=1,
            )
            self.assertEqual(store.exact_records("local"), ())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_failure_retains_writes_made_while_diverged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _prior,
                store,
                coordinator,
            ) = _diverged_fixture(root, fts5_available=False)
            store.append(_draft("local", "write-while-diverged"))
            service = _service(coordinator, identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch(
                    "tm_activation_recovery._publish_activation_manifest",
                    side_effect=OSError("injected"),
                ),
            ):
                outcome = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIsInstance(outcome, MigrationFailure)
            # the service auto-restored the READY prior authority; no
            # caller-side rollback was needed
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.canonical_store_id, "store.primary")
            self.assertEqual(coordinator.current_generation, 0)
            local = store.exact_records("local")
            self.assertEqual(len(local), 1)
            self.assertEqual(local[0].target_raw, "write-while-diverged")
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )


class ExplicitImportOriginBindingTests(unittest.TestCase):
    def test_gate_b_binds_the_actual_import_batch_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                preflight = _explicit_preflight(service, identity)
                stage = service._build_stage(
                    identity.configured_jsonl_path,
                    preflight=preflight,
                    canonical_store_id="store.import.new",
                    batch_kind="import",
                    batch_prefix="import",
                    snapshot_prefix="snapshot.import",
                    stage_prefix="import",
                    path_salt="f" * 32,
                    batch_id="import." + "e" * 32,
                )
            registry = __import__(
                "tm_stage_sealer"
            ).SealedArtifactRegistry(
                registry_namespace="coordinator.primary"
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                sealed = StageSealer(
                    registry=registry,
                    canonical_store_id="store.import.new",
                ).seal(
                    stage,
                    expected_prior_generation=0,
                )
                report = tm_gate_b.GateBEvaluator(
                    registry=registry
                ).evaluate(sealed)
            self.assertTrue(report.granted)
            facts = report.facts
            self.assertIsInstance(facts, tm_gate_b.GateBPhysicalFacts)
            assert facts is not None
            self.assertEqual(
                facts.migration_batch_id,
                "import." + "e" * 32,
            )
            self.assertEqual(facts.origin_batch_count, 1)
            self.assertRegex(facts.migration_batch_id, _BATCH_IMPORT_RE)
            self.assertRegex(
                sealed.evidence.source_binding.receipt.snapshot_id,
                _SNAPSHOT_IMPORT_RE,
            )


class ExplicitImportIdentityGuardTests(unittest.TestCase):
    def test_ordinary_activate_cannot_smuggle_a_different_store_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(
                root,
                fts5_available=False,
                candidate_store_id="store.other",
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.IDENTITY_MISMATCH",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.canonical_store_id, "store.primary")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_activate_replacement_rejects_the_current_store_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(
                root,
                fts5_available=False,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.activate_replacement(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.IDENTITY_MISMATCH",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.canonical_store_id, "store.primary")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )


if __name__ == "__main__":
    unittest.main()
