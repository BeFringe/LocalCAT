"""WA-06 5.10a public Windows explicit import/rebuild acceptance."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import tm_migration
import tm_sqlite_store
from tm_contracts import (
    AssetKind,
    AssetPreservationState,
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    RecoveryLocator,
    SnapshotManifest,
    SourceBindingState,
    contract_from_json,
)
from tm_engine import TMEngine
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteTMStore,
)
from tests.test_tm_portable_publication_windows import (
    _SOURCE_BYTES,
    _fixture as _portable_initial_fixture,
    _remove_long_quarantine,
)


_STORE_IMPORT_RE = re.compile(r"^store\.import\.[0-9a-f]{32}$")
_DIVERGED_BYTES = _SOURCE_BYTES + b'{"source":"new","target":"external"}\n'
_FRESH_DIVERGED_BYTES = (
    _DIVERGED_BYTES + b'{"source":"newer","target":"fresh"}\n'
)


def _manifest(identity_path: Path) -> SnapshotManifest:
    value = contract_from_json(identity_path.read_text(encoding="utf-8"))
    if type(value) is not SnapshotManifest:
        raise AssertionError("expected exact SnapshotManifest")
    return value


def _origin(identity_path: Path) -> tuple[str, str, int]:
    connection = sqlite3.connect(identity_path)
    try:
        rows = connection.execute(
            "SELECT batch_id, kind, completed_revision FROM tm_origin_batch"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise AssertionError("replacement canonical must have one origin")
    return str(rows[0][0]), str(rows[0][1]), int(rows[0][2])


def _divergence_latched(identity: CanonicalResourceIdentity) -> bool:
    connection = sqlite3.connect(identity.canonical_sidecar_path)
    try:
        row = connection.execute(
            "SELECT value FROM tm_meta WHERE key = 'divergence_latched'"
        ).fetchone()
    finally:
        connection.close()
    return row == ("1",)


def _fault_after_owner_phase(
    phase: str,
    *,
    after_phase: Callable[[], None] | None = None,
) -> mock._patch:
    original = tm_sqlite_store._PortableReplacementRecordOwner.publish

    def publish(**kwargs: object) -> object:
        record = original(**kwargs)
        unsigned = kwargs["unsigned"]
        if getattr(unsigned, "phase", None) == phase:
            if after_phase is not None:
                after_phase()
            raise OSError(f"injected after {phase} owner")
        return record

    return mock.patch.object(
        tm_sqlite_store._PortableReplacementRecordOwner,
        "publish",
        new=staticmethod(publish),
    )


def _cleanup_test_root(root: Path, *, ignore_locked: bool = False) -> None:
    """Remove only test-owned long Windows private/quarantine namespaces."""

    _remove_long_quarantine(root)
    for path in root.glob(".localcat-activation-private-v1.*"):
        if path.is_dir():
            try:
                shutil.rmtree("\\\\?\\" + str(path))
            except OSError:
                if not ignore_locked:
                    raise


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsExplicitImportRebuildTests(unittest.TestCase):
    def _public_diverged_fixture(
        self,
        root: Path,
        *,
        fts5_available: bool,
    ) -> tuple[
        CanonicalResourceIdentity,
        ResourceStoreCoordinator,
        TMMigrationService,
    ]:
        """Create gen0 only through the public portable initial activation."""

        identity, coordinator, service = _portable_initial_fixture(root)
        with mock.patch(
            "tm_sqlite_store._probe_fts5",
            return_value=fts5_available,
        ):
            initial = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )
        self.assertIs(type(initial), MigrationReport, initial)
        self.assertEqual(initial.activated_generation, 0)
        identity.configured_jsonl_path.write_bytes(_DIVERGED_BYTES)
        store = SQLiteTMStore.from_coordinator(coordinator)
        self.assertIs(
            store.source_binding_monitor.observe().state,
            SourceBindingState.SOURCE_DIVERGED,
        )
        self.assertTrue(_divergence_latched(identity))
        return identity, coordinator, service

    def _assert_terminal(
        self,
        outcome: object,
        coordinator: ResourceStoreCoordinator,
        identity: CanonicalResourceIdentity,
        *,
        expected_generation: int,
        fts5_available: bool,
        expected_source_bytes: bytes = _DIVERGED_BYTES,
        expected_migrated_count: int = 4,
    ) -> MigrationReport:
        self.assertIs(type(outcome), MigrationReport, outcome)
        report = outcome
        assert isinstance(report, MigrationReport)
        self.assertRegex(report.canonical_store_id, _STORE_IMPORT_RE)
        self.assertEqual(report.activated_generation, expected_generation)
        self.assertEqual(
            report.source_digest,
            hashlib.sha256(expected_source_bytes).hexdigest(),
        )
        self.assertEqual(report.migrated_count, expected_migrated_count)
        self.assertEqual(coordinator.state, "READY")
        self.assertEqual(coordinator.current_generation, expected_generation)
        self.assertEqual(coordinator.canonical_store_id, report.canonical_store_id)
        self.assertEqual(
            coordinator.active_store_path,
            identity.canonical_sidecar_path,
        )
        self.assertEqual(
            identity.configured_jsonl_path.read_bytes(),
            expected_source_bytes,
        )
        manifest = _manifest(identity.snapshot_manifest_path)
        self.assertEqual(manifest.receipt, report.snapshot_receipt)
        batch_id, kind, revision = _origin(identity.canonical_sidecar_path)
        self.assertRegex(batch_id, r"^import\.[0-9a-f]{32}$")
        self.assertEqual((kind, revision), ("import", 1))
        connection = sqlite3.connect(identity.canonical_sidecar_path)
        try:
            meta = dict(connection.execute("SELECT key, value FROM tm_meta"))
        finally:
            connection.close()
        self.assertEqual(meta["fts5_available"], "1" if fts5_available else "0")
        self.assertIs(
            SQLiteTMStore.from_coordinator(coordinator)
            .source_binding_monitor.observe().state,
            SourceBindingState.VERIFIED_CURRENT,
        )
        return report

    def test_import_and_rebuild_publish_portable_generation_one(self) -> None:
        for fts5_available in (True, False):
            for operation in ("import_snapshot", "rebuild_from_snapshot"):
                with self.subTest(
                    fts5_available=fts5_available,
                    operation=operation,
                ):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory).resolve()
                        try:
                            identity, coordinator, service = (
                                self._public_diverged_fixture(
                                    root,
                                    fts5_available=fts5_available,
                                )
                            )
                            with mock.patch(
                                "tm_sqlite_store._probe_fts5",
                                return_value=fts5_available,
                            ):
                                outcome = getattr(service, operation)(
                                    identity.configured_jsonl_path,
                                    identity.resource_id,
                                )
                            self._assert_terminal(
                                outcome,
                                coordinator,
                                identity,
                                expected_generation=1,
                                fts5_available=fts5_available,
                            )
                        finally:
                            _cleanup_test_root(root)

    def test_same_service_publishes_continuous_generation_one_then_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    first = service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                first_report = self._assert_terminal(
                    first,
                    coordinator,
                    identity,
                    expected_generation=1,
                    fts5_available=False,
                )
                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    second = service.rebuild_from_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                second_report = self._assert_terminal(
                    second,
                    coordinator,
                    identity,
                    expected_generation=2,
                    fts5_available=False,
                )
                self.assertNotEqual(
                    first_report.canonical_store_id,
                    second_report.canonical_store_id,
                )
                self.assertNotEqual(
                    first_report.snapshot_receipt.snapshot_id,
                    second_report.snapshot_receipt.snapshot_id,
                )
            finally:
                _cleanup_test_root(root)

    def test_fresh_service_adopts_current_then_imports_generation_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    first = service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self._assert_terminal(
                    first,
                    coordinator,
                    identity,
                    expected_generation=1,
                    fts5_available=False,
                )
                identity.configured_jsonl_path.write_bytes(
                    _FRESH_DIVERGED_BYTES
                )

                fresh_coordinator = ResourceStoreCoordinator(
                    canonical_store_id="store.primary",
                    resource_identity=identity,
                )
                fresh_service = TMMigrationService(
                    resource_identity=identity,
                    canonical_store_id="store.primary",
                    coordinator=fresh_coordinator,
                )
                self.assertIsNone(fresh_coordinator.current_generation)
                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    second = fresh_service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                second_report = self._assert_terminal(
                    second,
                    fresh_coordinator,
                    identity,
                    expected_generation=2,
                    fts5_available=False,
                    expected_source_bytes=_FRESH_DIVERGED_BYTES,
                    expected_migrated_count=5,
                )
                self.assertEqual(
                    fresh_service.canonical_store_id,
                    second_report.canonical_store_id,
                )

                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    reopened = TMEngine(
                        str(identity.configured_jsonl_path),
                        update=False,
                        expected_resource_id=identity.resource_id,
                    )
                self.assertTrue(reopened.canonical_active)
                reopened_store = reopened.canonical_store
                assert reopened_store is not None
                self.assertEqual(
                    reopened_store.canonical_revision().generation,
                    2,
                )
                match = reopened.query_exact("newer")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.target, "fresh")
            finally:
                _cleanup_test_root(root)

    def test_pre_arm_failure_preserves_latched_prior_and_programmer_errors_pass(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                source_before = identity.configured_jsonl_path.read_bytes()
                database_before = identity.canonical_sidecar_path.read_bytes()
                manifest_before = identity.snapshot_manifest_path.read_bytes()
                injected = ActivationPreparationError(
                    "ACTIVATION.TEST_PRE_ARM",
                    retryable=False,
                )
                with mock.patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=injected,
                ):
                    outcome = service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(outcome), MigrationFailure)
                assert isinstance(outcome, MigrationFailure)
                self.assertEqual(outcome.error_code, "ACTIVATION.TEST_PRE_ARM")
                self.assertIs(
                    outcome.original_source_preservation.state,
                    AssetPreservationState.VERIFIED_UNCHANGED,
                )
                self.assertIs(
                    outcome.active_store_preservation.state,
                    AssetPreservationState.VERIFIED_UNCHANGED,
                )
                self.assertEqual(coordinator.state, "READY")
                self.assertEqual(coordinator.current_generation, 0)
                self.assertEqual(coordinator.canonical_store_id, "store.primary")
                self.assertEqual(
                    identity.configured_jsonl_path.read_bytes(), source_before
                )
                self.assertEqual(
                    identity.canonical_sidecar_path.read_bytes(), database_before
                )
                self.assertEqual(
                    identity.snapshot_manifest_path.read_bytes(), manifest_before
                )
                self.assertTrue(_divergence_latched(identity))
                self.assertEqual(list(root.glob(".localcat-import.*")), [])

                programmer = AttributeError("injected programmer fault")
                with mock.patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=programmer,
                ):
                    with self.assertRaises(AttributeError) as caught:
                        service.rebuild_from_snapshot(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                self.assertIs(caught.exception, programmer)
                self.assertEqual(coordinator.state, "READY")
                self.assertEqual(
                    identity.configured_jsonl_path.read_bytes(), source_before
                )
                self.assertEqual(
                    identity.canonical_sidecar_path.read_bytes(), database_before
                )
                self.assertEqual(
                    identity.snapshot_manifest_path.read_bytes(), manifest_before
                )
            finally:
                _cleanup_test_root(root)

    def test_db_manifest_and_ready_owner_instruction_faults_are_terminal(self) -> None:
        for phase in ("DB_REPLACED", "MANIFEST_PUBLISHED", "READY"):
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True
                ) as directory:
                    root = Path(directory).resolve()
                    try:
                        identity, coordinator, service = (
                            self._public_diverged_fixture(
                                root,
                                fts5_available=False,
                            )
                        )
                        source_before = identity.configured_jsonl_path.read_bytes()
                        database_before = identity.canonical_sidecar_path.read_bytes()
                        foreign = root / "foreign.keep"
                        foreign.write_bytes(b"foreign")
                        with (
                            mock.patch(
                                "tm_sqlite_store._probe_fts5",
                                return_value=False,
                            ),
                            _fault_after_owner_phase(phase),
                        ):
                            outcome = service.import_snapshot(
                                identity.configured_jsonl_path,
                                identity.resource_id,
                            )
                        self.assertEqual(
                            identity.configured_jsonl_path.read_bytes(),
                            source_before,
                        )
                        self.assertEqual(foreign.read_bytes(), b"foreign")
                        if phase == "READY":
                            self._assert_terminal(
                                outcome,
                                coordinator,
                                identity,
                                expected_generation=1,
                                fts5_available=False,
                            )
                        else:
                            self.assertIs(type(outcome), MigrationFailure)
                            assert isinstance(outcome, MigrationFailure)
                            self.assertEqual(
                                outcome.error_code,
                                "ACTIVATION.RECOVERY_REQUIRED",
                            )
                            self.assertFalse(outcome.retryable)
                            self.assertEqual(outcome.active_generation, 0)
                            self.assertIs(
                                outcome.original_source_preservation.state,
                                AssetPreservationState.VERIFIED_UNCHANGED,
                            )
                            self.assertIs(
                                outcome.active_store_preservation.state,
                                AssetPreservationState.UNVERIFIED,
                            )
                            self.assertEqual(
                                outcome.active_store_preservation.before_digest,
                                hashlib.sha256(database_before).hexdigest(),
                            )
                            self.assertEqual(len(outcome.recovery_locators), 1)
                            locator = outcome.recovery_locators[0]
                            self.assertIs(type(locator), RecoveryLocator)
                            self.assertIs(
                                locator.asset_kind,
                                AssetKind.ACTIVE_STORE,
                            )
                            self.assertTrue(locator.path.is_absolute())
                            self.assertEqual(
                                locator.expected_digest,
                                hashlib.sha256(database_before).hexdigest(),
                            )
                            self.assertEqual(
                                hashlib.sha256(locator.path.read_bytes()).hexdigest(),
                                locator.expected_digest,
                            )
                            self.assertEqual(coordinator.state, "ACTIVATING")
                            self.assertIsNone(coordinator.current_generation)
                    finally:
                        _cleanup_test_root(
                            root,
                            ignore_locked=(phase != "READY"),
                        )

    def test_terminal_stage_retirement_fault_retries_exact_live_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                source_before = identity.configured_jsonl_path.read_bytes()
                foreign = root / "foreign.keep"
                foreign.write_bytes(b"foreign")
                original = tm_migration._cleanup_initial_unpublished_stage
                cleanup_calls = 0

                def fail_once(
                    attempt: tm_migration._InitialStageAttempt,
                ) -> None:
                    nonlocal cleanup_calls
                    cleanup_calls += 1
                    if cleanup_calls == 1:
                        raise tm_migration.MigrationPreflightError(
                            "MIGRATION.INITIAL_CLEANUP_UNPROVEN"
                        )
                    original(attempt)

                with (
                    mock.patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ),
                    mock.patch(
                        "tm_migration._cleanup_initial_unpublished_stage",
                        new=fail_once,
                    ),
                ):
                    outcome = service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertEqual(cleanup_calls, 2)
                self._assert_terminal(
                    outcome,
                    coordinator,
                    identity,
                    expected_generation=1,
                    fts5_available=False,
                )
                self.assertEqual(
                    identity.configured_jsonl_path.read_bytes(),
                    source_before,
                )
                self.assertEqual(foreign.read_bytes(), b"foreign")
                self.assertEqual(list(root.glob(".localcat-import.*")), [])
            finally:
                _cleanup_test_root(root)

    def test_source_reproof_fault_still_projects_legal_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                source_before = identity.configured_jsonl_path.read_bytes()
                original = tm_migration._PortableRootedRead.reprove_digest
                calls = 0

                def reprove_digest(
                    rooted: tm_migration._PortableRootedRead,
                    expected_digest: str,
                ) -> str:
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise OSError("injected terminal source reproof fault")
                    return original(rooted, expected_digest)

                with (
                    mock.patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ),
                    _fault_after_owner_phase("DB_REPLACED"),
                    mock.patch.object(
                        tm_migration._PortableRootedRead,
                        "reprove_digest",
                        new=reprove_digest,
                    ),
                ):
                    outcome = service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertEqual(calls, 3)
                self.assertIs(type(outcome), MigrationFailure)
                assert isinstance(outcome, MigrationFailure)
                self.assertEqual(
                    outcome.error_code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertIs(
                    outcome.original_source_preservation.state,
                    AssetPreservationState.VERIFIED_UNCHANGED,
                )
                self.assertEqual(
                    outcome.original_source_preservation.before_digest,
                    hashlib.sha256(source_before).hexdigest(),
                )
                self.assertEqual(
                    outcome.original_source_preservation.observed_digest,
                    outcome.original_source_preservation.before_digest,
                )
                self.assertEqual(len(outcome.recovery_locators), 1)
                self.assertIs(
                    outcome.recovery_locators[0].asset_kind,
                    AssetKind.ACTIVE_STORE,
                )
                self.assertFalse(outcome.retryable)
                self.assertEqual(coordinator.state, "ACTIVATING")
            finally:
                _cleanup_test_root(root, ignore_locked=True)

    def test_fresh_service_recovers_prepared_prior_then_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = self._public_diverged_fixture(
                    root,
                    fts5_available=False,
                )
                source_before = identity.configured_jsonl_path.read_bytes()
                recovery_patch = mock.patch.object(
                    coordinator,
                    "recover_portable_replacement_activation",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    ),
                )
                recovery_mock: mock.Mock | None = None

                def arm_recovery_fault() -> None:
                    nonlocal recovery_mock
                    recovery_mock = recovery_patch.start()

                try:
                    with (
                        _fault_after_owner_phase(
                            "PREPARED",
                            after_phase=arm_recovery_fault,
                        ),
                        mock.patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=False,
                        ),
                    ):
                        failed = service.import_snapshot(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                finally:
                    recovery_patch.stop()
                self.assertIsNotNone(recovery_mock)
                assert recovery_mock is not None
                self.assertEqual(recovery_mock.call_count, 1)
                self.assertIs(type(failed), MigrationFailure)
                assert isinstance(failed, MigrationFailure)
                self.assertEqual(
                    failed.error_code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertTrue(failed.retryable)
                self.assertIs(
                    failed.active_store_preservation.state,
                    AssetPreservationState.VERIFIED_UNCHANGED,
                )
                self.assertEqual(failed.recovery_locators, ())
                self.assertEqual(coordinator.state, "ACTIVATING")
                self.assertIsNone(coordinator.current_generation)
                self.assertEqual(
                    identity.configured_jsonl_path.read_bytes(), source_before
                )

                fresh_coordinator = ResourceStoreCoordinator(
                    canonical_store_id="store.primary",
                    resource_identity=identity,
                )
                fresh_service = TMMigrationService(
                    resource_identity=identity,
                    canonical_store_id="store.primary",
                    coordinator=fresh_coordinator,
                )
                with mock.patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ):
                    recovered_retry = fresh_service.import_snapshot(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self._assert_terminal(
                    recovered_retry,
                    fresh_coordinator,
                    identity,
                    expected_generation=1,
                    fts5_available=False,
                )
            finally:
                _cleanup_test_root(root)


if __name__ == "__main__":
    unittest.main()
