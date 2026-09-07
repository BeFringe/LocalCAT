"""Windows 5.7a integration contract for first canonical publication."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest import mock

import tm_activation_journal
import tm_migration
import platform_fs_contracts
import platform_fs_windows
import tm_sqlite_store
import tm_stage_sealer
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from tm_candidate_store_contracts import SQLiteStoreSchemaError
from tm_contracts import (
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    SnapshotManifest,
    SourceBindingState,
    TMRecordDraft,
    contract_from_json,
)
from tm_content_attestation import ContentAttestationError
from tm_migration import MigrationFailure, MigrationReport, TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteTMStore,
)


_SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _fixture(
    root: Path,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    TMMigrationService,
]:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(_SOURCE_BYTES)
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        source,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    return identity, coordinator, service


def _fresh_completed_rehydrate(
    identity: CanonicalResourceIdentity,
    *,
    platform_backend: platform_fs_windows.WindowsPlatformAdapter | None = None,
) -> tuple[ResourceStoreCoordinator, object]:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
        platform_backend=platform_backend,
    )
    with service._acquire_initial_reservation() as reservation:
        outcome = coordinator.rehydrate_completed_portable_activation(
            **reservation.portable_runtime_inputs()
        )
        reservation.reprove()
    return coordinator, outcome


@contextmanager
def _observe_completed_integrity_checks(
    checks: list[str],
    *,
    reject_checks: bool,
) -> Iterator[None]:
    real_open = tm_sqlite_store._open_completed_authority_read_connection

    @contextmanager
    def observed_open(
        database_path: Path,
    ) -> Iterator[sqlite3.Connection]:
        with real_open(database_path) as connection:
            def authorize(
                action: int,
                argument_one: str | None,
                _argument_two: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if (
                    action == sqlite3.SQLITE_PRAGMA
                    and type(argument_one) is str
                    and argument_one.lower()
                    in {"integrity_check", "foreign_key_check"}
                ):
                    checks.append(f"PRAGMA {argument_one.upper()}")
                    if reject_checks:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            yield connection

    with mock.patch.object(
        tm_sqlite_store,
        "_open_completed_authority_read_connection",
        new=observed_open,
    ):
        yield


def _private_root(root: Path, identity: CanonicalResourceIdentity) -> Path:
    return root / tm_activation_journal._portable_activation_private_directory_name(
        identity
    )


def _publication_records(
    root: Path,
    identity: CanonicalResourceIdentity,
) -> tuple[
    tm_activation_journal._PortableActivationJournalRecord,
    dict[str, tm_activation_journal._PortablePublicationPhaseRecord],
]:
    private_root = _private_root(root, identity)
    prepared = tm_activation_journal._parse_portable_activation_journal_bytes(
        (private_root / "activation-journal-v3.json").read_bytes()
    )
    phases = {
        phase: tm_activation_journal._parse_portable_publication_phase_bytes(
            (
                private_root
                / tm_activation_journal._portable_publication_phase_name(phase)
            ).read_bytes()
        )
        for phase in (
            "DB_REPLACED",
            "MANIFEST_PUBLISHED",
            "GENERATION_PUBLISHED",
        )
        if (
            private_root
            / tm_activation_journal._portable_publication_phase_name(phase)
        ).is_file()
    }
    return prepared, phases


def _remove_long_quarantine(root: Path) -> None:
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


def _publication_phase_set(
    root: Path,
    identity: CanonicalResourceIdentity,
) -> set[str]:
    private_root = _private_root(root, identity)
    return {
        phase
        for phase in (
            "DB_REPLACED",
            "MANIFEST_PUBLISHED",
            "GENERATION_PUBLISHED",
        )
        if (
            private_root
            / tm_activation_journal._portable_publication_phase_name(phase)
        ).is_file()
    }


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableInitialPublicationTests(unittest.TestCase):
    def test_completed_rehydrate_reuses_exact_attested_semantic_facts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                checks: list[str] = []
                with (
                    _observe_completed_integrity_checks(
                        checks,
                        reject_checks=True,
                    ),
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        side_effect=AssertionError(
                            "exact completed rehydrate must reuse index facts"
                        ),
                    ) as validator,
                ):
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                self.assertEqual(checks, [])
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                self.assertEqual(fresh.current_generation, 0)
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_validates_after_legal_database_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                SQLiteTMStore.from_coordinator(coordinator).append(
                    TMRecordDraft(
                        source_raw="appended source",
                        target_raw="appended target",
                        speaker_raw=None,
                        context_prev_raw=None,
                        context_next_raw=None,
                        file_source=None,
                        provenance=(("source", "rehydrate-test"),),
                    )
                )
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                checks: list[str] = []
                with (
                    _observe_completed_integrity_checks(
                        checks,
                        reject_checks=False,
                    ),
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        wraps=real_validator,
                    ) as validator,
                ):
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                self.assertEqual(
                    checks,
                    ["PRAGMA INTEGRITY_CHECK", "PRAGMA FOREIGN_KEY_CHECK"],
                )
                validator.assert_called_once()
                self.assertEqual(outcome.action, "COMPLETED")
                self.assertEqual(fresh.current_generation, 0)
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_keeps_source_divergence_classification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                identity.configured_jsonl_path.write_bytes(
                    _SOURCE_BYTES
                    + b'{"source":"changed","target":"outside"}\n'
                )
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                checks: list[str] = []
                with (
                    _observe_completed_integrity_checks(
                        checks,
                        reject_checks=False,
                    ),
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        wraps=real_validator,
                    ) as validator,
                ):
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                self.assertEqual(checks, [])
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                observation = (
                    SQLiteTMStore.from_coordinator(fresh)
                    .source_binding_monitor.observe()
                )
                self.assertIs(
                    observation.state,
                    SourceBindingState.SOURCE_DIVERGED,
                )
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_reuses_db_after_manifest_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                identity.snapshot_manifest_path.write_bytes(
                    identity.snapshot_manifest_path.read_bytes() + b"\n"
                )
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                checks: list[str] = []
                with (
                    _observe_completed_integrity_checks(
                        checks,
                        reject_checks=False,
                    ),
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        wraps=real_validator,
                    ) as validator,
                ):
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                self.assertEqual(checks, [])
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                self.assertEqual(fresh.state, "READY")
                self.assertEqual(fresh.current_generation, 0)
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_reuses_db_when_source_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                identity.configured_jsonl_path.unlink()
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                with mock.patch.object(
                    tm_sqlite_store,
                    "validate_candidate_proof_index",
                    wraps=real_validator,
                ) as validator:
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                observation = (
                    SQLiteTMStore.from_coordinator(fresh)
                    .source_binding_monitor.observe()
                )
                self.assertIs(
                    observation.state,
                    SourceBindingState.SOURCE_DIVERGED,
                )
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_reuses_db_for_source_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            alias = root / "source-hardlink-alias"
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                os.link(identity.configured_jsonl_path, alias)
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                with mock.patch.object(
                    tm_sqlite_store,
                    "validate_candidate_proof_index",
                    wraps=real_validator,
                ) as validator:
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                observation = (
                    SQLiteTMStore.from_coordinator(fresh)
                    .source_binding_monitor.observe()
                )
                self.assertIs(
                    observation.state,
                    SourceBindingState.SOURCE_DIVERGED,
                )
            finally:
                if alias.exists():
                    alias.unlink()
                _remove_long_quarantine(root)

    def test_completed_rehydrate_reuses_db_for_source_reparse_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            junction_target = root / "source-junction-target"
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                identity.configured_jsonl_path.unlink()
                junction_target.mkdir()
                completed = subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(identity.configured_jsonl_path),
                        str(junction_target),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr or completed.stdout,
                )
                real_validator = tm_sqlite_store.validate_candidate_proof_index
                with mock.patch.object(
                    tm_sqlite_store,
                    "validate_candidate_proof_index",
                    wraps=real_validator,
                ) as validator:
                    fresh, outcome = _fresh_completed_rehydrate(identity)
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                observation = (
                    SQLiteTMStore.from_coordinator(fresh)
                    .source_binding_monitor.observe()
                )
                self.assertIs(
                    observation.state,
                    SourceBindingState.SOURCE_DIVERGED,
                )
            finally:
                if identity.configured_jsonl_path.exists():
                    os.rmdir(identity.configured_jsonl_path)
                if junction_target.exists():
                    junction_target.rmdir()
                _remove_long_quarantine(root)

    def test_completed_rehydrate_reuses_db_despite_optional_authority_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                fresh = ResourceStoreCoordinator(
                    canonical_store_id="store.primary",
                    resource_identity=identity,
                )
                fresh_service = TMMigrationService(
                    resource_identity=identity,
                    canonical_store_id="store.primary",
                    coordinator=fresh,
                )
                real_open = platform_fs_windows.WindowsPlatformAdapter._open_regular

                def reject_source_authority(
                    backend: object,
                    rooted: object,
                    relative: object,
                ) -> object:
                    if Path(str(relative)).name == identity.configured_jsonl_path.name:
                        raise PlatformFileError(
                            PlatformFileErrorCode.IDENTITY_STALE,
                            retryable=True,
                        )
                    return real_open(backend, rooted, relative)

                real_validator = tm_sqlite_store.validate_candidate_proof_index
                with (
                    mock.patch.object(
                        platform_fs_windows.WindowsPlatformAdapter,
                        "_open_regular",
                        new=reject_source_authority,
                    ),
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        wraps=real_validator,
                    ) as validator,
                    fresh_service._acquire_initial_reservation() as reservation,
                ):
                    outcome = fresh.rehydrate_completed_portable_activation(
                        **reservation.portable_runtime_inputs()
                    )
                validator.assert_not_called()
                self.assertEqual(outcome.action, "COMPLETED")
                self.assertEqual(fresh.state, "READY")
                self.assertEqual(fresh.current_generation, 0)
                self.assertEqual(
                    fresh.active_store_path,
                    identity.canonical_sidecar_path,
                )
            finally:
                _remove_long_quarantine(root)

    def test_completed_rehydrate_rejects_corrupt_database_after_reuse_miss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)
            try:
                activated = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(activated), MigrationReport)
                payload = bytearray(identity.canonical_sidecar_path.read_bytes())
                payload[0] ^= 0xFF
                identity.canonical_sidecar_path.write_bytes(payload)
                fresh = ResourceStoreCoordinator(
                    canonical_store_id="store.primary",
                    resource_identity=identity,
                )
                fresh_service = TMMigrationService(
                    resource_identity=identity,
                    canonical_store_id="store.primary",
                    coordinator=fresh,
                )
                with fresh_service._acquire_initial_reservation() as reservation:
                    with self.assertRaisesRegex(
                        ActivationPreparationError,
                        "^ACTIVATION.RECOVERY_REQUIRED$",
                    ):
                        fresh.rehydrate_completed_portable_activation(
                            **reservation.portable_runtime_inputs()
                        )
                    reservation.reprove()
                self.assertNotEqual(fresh.state, "READY")
                self.assertIsNone(fresh.current_generation)
            finally:
                _remove_long_quarantine(root)

    def test_normal_portable_activation_reuses_sealer_projection_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            real_full_validate = (
                tm_stage_sealer._validate_candidate_proof_index_with_digest
            )
            real_projection_digest = (
                tm_sqlite_store._candidate_proof_projection_digest
            )
            try:
                with (
                    mock.patch.object(
                        tm_stage_sealer,
                        "_validate_candidate_proof_index_with_digest",
                        wraps=real_full_validate,
                    ) as full_validate,
                    mock.patch.object(
                        tm_sqlite_store,
                        "_validate_activation_indexes",
                        side_effect=AssertionError(
                            "normal activation must use the sealed projection digest"
                        ),
                    ) as active_full_validate,
                    mock.patch.object(
                        tm_sqlite_store,
                        "_candidate_proof_projection_digest",
                        wraps=real_projection_digest,
                    ) as active_projection_digest,
                ):
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(
                    type(outcome),
                    MigrationReport,
                    getattr(outcome, "error_code", None),
                )
                full_validate.assert_called_once()
                active_full_validate.assert_not_called()
                active_projection_digest.assert_called_once()
                self.assertEqual(coordinator.current_generation, 0)
            finally:
                _remove_long_quarantine(root)

    def test_projection_digest_mismatch_never_publishes_active_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            try:
                with mock.patch.object(
                    tm_sqlite_store,
                    "_candidate_proof_projection_digest",
                    return_value="0" * 64,
                ):
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(outcome), MigrationFailure)
                self.assertIsNone(coordinator.current_generation)
                self.assertNotEqual(coordinator.state, "READY")
            finally:
                _remove_long_quarantine(root)

    def test_portable_health_rebinds_database_and_falls_back_after_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            try:
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(
                    type(outcome),
                    MigrationReport,
                    getattr(outcome, "error_code", None),
                )
                store = SQLiteTMStore.from_coordinator(coordinator)
                real_capture = tm_sqlite_store._capture_platform_content_file
                with (
                    mock.patch(
                        "tm_sqlite_store._capture_platform_content_file",
                        wraps=real_capture,
                    ) as capture,
                    mock.patch(
                        "tm_sqlite_store.validate_candidate_proof_index",
                        side_effect=AssertionError(
                            "matched portable bytes must reuse semantic facts"
                        ),
                    ),
                ):
                    health = store.health()
                self.assertTrue(health.healthy)
                self.assertEqual(capture.call_count, 1)

                with mock.patch(
                    "tm_sqlite_store._capture_platform_content_file",
                    side_effect=ContentAttestationError(
                        "CONTENT_ATTESTATION.FILE_UNSAFE"
                    ),
                ):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError,
                        "^STORE.ACTIVE_ATTESTATION_INVALID$",
                    ):
                        store.health()

                store.append(
                    TMRecordDraft(
                        source_raw="new source",
                        target_raw="new target",
                        speaker_raw=None,
                        context_prev_raw=None,
                        context_next_raw=None,
                        file_source=None,
                        provenance=(("source", "health-test"),),
                    )
                )
                real_validate = tm_sqlite_store.validate_candidate_proof_index
                with mock.patch(
                    "tm_sqlite_store.validate_candidate_proof_index",
                    wraps=real_validate,
                ) as validate:
                    changed = store.health()
                validate.assert_called_once()
                self.assertEqual(changed.record_count, 4)
            finally:
                _remove_long_quarantine(root)

    def test_portable_health_reuses_db_despite_optional_pair_capture_failure(
        self,
    ) -> None:
        cases = (
            ("manifest", "CONTENT_ATTESTATION.FILE_MISSING"),
            ("source", "CONTENT_ATTESTATION.FILE_UNSAFE"),
        )
        for role, error_code in cases:
            with self.subTest(role=role, error_code=error_code):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity, coordinator, service = _fixture(root)
                    try:
                        outcome = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                        self.assertIs(
                            type(outcome),
                            MigrationReport,
                            getattr(outcome, "error_code", None),
                        )
                        rejected_name = (
                            identity.snapshot_manifest_path.name
                            if role == "manifest"
                            else identity.configured_jsonl_path.name
                        )
                        real_capture = (
                            tm_sqlite_store._capture_platform_content_file
                        )

                        def reject_optional_pair(
                            backend: object,
                            root_path: Path,
                            relative: object,
                        ) -> object:
                            if Path(str(relative)).name == rejected_name:
                                raise ContentAttestationError(error_code)
                            return real_capture(backend, root_path, relative)

                        real_validator = (
                            tm_sqlite_store.validate_candidate_proof_index
                        )
                        with (
                            mock.patch.object(
                                tm_sqlite_store,
                                "_capture_platform_content_file",
                                side_effect=reject_optional_pair,
                            ),
                            mock.patch.object(
                                tm_sqlite_store,
                                "validate_candidate_proof_index",
                                wraps=real_validator,
                            ) as validator,
                        ):
                            health = SQLiteTMStore.from_coordinator(
                                coordinator
                            ).health()
                        self.assertTrue(health.healthy)
                        validator.assert_not_called()
                    finally:
                        _remove_long_quarantine(root)

    def test_portable_health_reuses_unchanged_db_with_missing_pair_and_monitor_detects_divergence(self) -> None:
        for role in ("source", "manifest"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                identity, coordinator, service = _fixture(root)
                try:
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path, identity.resource_id
                    )
                    self.assertIs(type(outcome), MigrationReport)
                    original_db = identity.canonical_sidecar_path.read_bytes()
                    missing = (
                        identity.configured_jsonl_path
                        if role == "source" else identity.snapshot_manifest_path
                    )
                    missing.unlink()
                    store = SQLiteTMStore.from_coordinator(coordinator)
                    with mock.patch.object(
                        tm_sqlite_store, "validate_candidate_proof_index",
                        side_effect=AssertionError("unchanged DB must reuse semantic facts"),
                    ):
                        self.assertTrue(store.health().healthy)
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), original_db)
                    self.assertIs(
                        store.source_binding_monitor.observe().state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                finally:
                    _remove_long_quarantine(root)

    def test_public_activation_publishes_exact_generation_zero_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            registry = coordinator._sealed_registry
            issued: list[tuple[object, object]] = []
            consumed: list[object] = []
            real_issue = registry.issue_token
            real_consume = registry.consume

            def issue_token(*args: object, **kwargs: object) -> object:
                token = real_issue(*args, **kwargs)
                entry = registry._token_entry(token)
                issued.append((token, entry.live_authority))
                return token

            def consume_token(token: object) -> None:
                consumed.append(token)
                real_consume(token)

            try:
                self.assertTrue(
                    tm_sqlite_store.detect_sqlite_runtime().fts5_available
                )
                with (
                    mock.patch.object(
                        registry,
                        "issue_token",
                        side_effect=issue_token,
                    ),
                    mock.patch.object(
                        registry,
                        "consume",
                        side_effect=consume_token,
                    ),
                ):
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

                self.assertIs(
                    type(outcome),
                    MigrationReport,
                    getattr(outcome, "error_code", None),
                )
                report = outcome
                assert isinstance(report, MigrationReport)
                self.assertEqual(report.activated_generation, 0)
                self.assertEqual(report.snapshot_receipt.record_count, 3)
                self.assertEqual(coordinator.state, "READY")
                self.assertEqual(coordinator.current_generation, 0)

                self.assertEqual(len(issued), 1)
                token, live_authority = issued[0]
                self.assertEqual(consumed, [token])
                self.assertTrue(live_authority.closed)
                entry = registry._token_entry(token)
                self.assertIs(entry.state, ActivationCapabilityState.CONSUMED)
                self.assertIsNone(entry.live_authority)
                with self.assertRaisesRegex(
                    tm_stage_sealer.StageSealError,
                    "SEALER.TOKEN_NOT_ACTIVE",
                ):
                    registry.consume(token)

                database_bytes = identity.canonical_sidecar_path.read_bytes()
                manifest_bytes = identity.snapshot_manifest_path.read_bytes()
                manifest = contract_from_json(manifest_bytes.decode("utf-8"))
                self.assertIs(type(manifest), SnapshotManifest)
                assert isinstance(manifest, SnapshotManifest)
                self.assertEqual(manifest.receipt, report.snapshot_receipt)

                connection = sqlite3.connect(str(identity.canonical_sidecar_path))
                try:
                    meta = dict(
                        connection.execute("SELECT key, value FROM tm_meta")
                    )
                    self.assertEqual(meta["activation_status"], "ACTIVE")
                    self.assertEqual(meta["generation"], "0")
                    self.assertEqual(meta["fts5_available"], "1")
                    self.assertEqual(meta["candidate_index_kind"], "FTS5_TRIGRAM")
                    self.assertEqual(
                        connection.execute(
                            "SELECT snapshot_id, resource_id, "
                            "canonical_store_id, record_count, status "
                            "FROM tm_snapshot_receipt"
                        ).fetchall(),
                        [
                            (
                                report.snapshot_receipt.snapshot_id,
                                identity.resource_id,
                                report.canonical_store_id,
                                3,
                                "completed",
                            )
                        ],
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT configured_jsonl_path, manifest_path, "
                            "snapshot_kind, snapshot_id FROM tm_snapshot_binding"
                        ).fetchall(),
                        [
                            (
                                str(identity.configured_jsonl_path),
                                str(identity.snapshot_manifest_path),
                                "MIGRATION_SOURCE",
                                report.snapshot_receipt.snapshot_id,
                            )
                        ],
                    )
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone(),
                        (3,),
                    )
                finally:
                    connection.close()

                store = SQLiteTMStore.from_coordinator(coordinator)
                health = store.health()
                self.assertTrue(health.healthy)
                self.assertTrue(health.exact_available)
                self.assertEqual(health.generation, 0)
                self.assertEqual(health.index_kind, "FTS5_TRIGRAM")
                self.assertEqual(health.record_count, 3)

                prepared, phases = _publication_records(root, identity)
                self.assertEqual(prepared.unsigned.closure, "PENDING")
                self.assertEqual(prepared.unsigned.phase, "PREPARED")
                self.assertEqual(
                    set(phases),
                    {
                        "DB_REPLACED",
                        "MANIFEST_PUBLISHED",
                        "GENERATION_PUBLISHED",
                    },
                )
                database_phase = phases["DB_REPLACED"]
                manifest_phase = phases["MANIFEST_PUBLISHED"]
                generation_phase = phases["GENERATION_PUBLISHED"]
                self.assertEqual(
                    database_phase.unsigned.predecessor_digest,
                    prepared.record_digest,
                )
                self.assertEqual(
                    manifest_phase.unsigned.predecessor_digest,
                    database_phase.record_digest,
                )
                self.assertEqual(
                    generation_phase.unsigned.predecessor_digest,
                    manifest_phase.record_digest,
                )
                self.assertIsNone(
                    database_phase.unsigned.active_content_attestation
                )
                self.assertEqual(
                    database_phase.unsigned.sealed_content_attestation,
                    prepared.unsigned.sealed_content_attestation,
                )
                active = manifest_phase.unsigned.active_content_attestation
                self.assertIsNotNone(active)
                self.assertEqual(
                    generation_phase.unsigned.active_content_attestation,
                    active,
                )
                self.assertEqual(
                    manifest_phase.unsigned.canonical_database_sha256,
                    hashlib.sha256(database_bytes).hexdigest(),
                )
                self.assertEqual(
                    manifest_phase.unsigned.canonical_database_size,
                    len(database_bytes),
                )
                self.assertEqual(
                    manifest_phase.unsigned.canonical_manifest_sha256,
                    hashlib.sha256(manifest_bytes).hexdigest(),
                )
                self.assertEqual(
                    manifest_phase.unsigned.canonical_manifest_size,
                    len(manifest_bytes),
                )
                self.assertEqual(
                    list(root.rglob("*.candidate")),
                    [],
                )
                self.assertEqual(
                    list(root.glob(".localcat-migration.initial-*")),
                    [],
                )
            finally:
                _remove_long_quarantine(root)

    def test_receipt_activation_failure_stops_after_database_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            try:
                with mock.patch.object(
                    coordinator,
                    "_apply_portable_receipt_activation",
                    create=True,
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.TEST_RECEIPT_FAILURE",
                        retryable=False,
                    ),
                ) as fail_receipt:
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

                self.assertEqual(
                    fail_receipt.call_count,
                    1,
                    "portable receipt/ACTIVE owner seam was not reached",
                )
                self.assertIs(type(outcome), MigrationFailure)
                self.assertNotEqual(coordinator.state, "READY")
                self.assertIsNone(coordinator.current_generation)
                _prepared, phases = _publication_records(root, identity)
                self.assertEqual(set(phases), {"DB_REPLACED"})
                self.assertTrue(identity.canonical_sidecar_path.is_file())
                self.assertFalse(identity.snapshot_manifest_path.exists())
            finally:
                _remove_long_quarantine(root)

    def test_generation_failure_stops_after_manifest_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, coordinator, service = _fixture(root)
            try:
                with mock.patch.object(
                    coordinator,
                    "_publish_portable_generation",
                    create=True,
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.TEST_GENERATION_FAILURE",
                        retryable=False,
                    ),
                ) as fail_generation:
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

                self.assertEqual(
                    fail_generation.call_count,
                    1,
                    "portable generation owner seam was not reached",
                )
                self.assertIs(type(outcome), MigrationFailure)
                self.assertNotEqual(coordinator.state, "READY")
                self.assertIsNone(coordinator.current_generation)
                _prepared, phases = _publication_records(root, identity)
                self.assertEqual(
                    set(phases),
                    {"DB_REPLACED", "MANIFEST_PUBLISHED"},
                )
                self.assertTrue(identity.canonical_sidecar_path.is_file())
                self.assertTrue(identity.snapshot_manifest_path.is_file())
                self.assertIsNotNone(
                    phases[
                        "MANIFEST_PUBLISHED"
                    ].unsigned.active_content_attestation
                )
            finally:
                _remove_long_quarantine(root)

    def test_asset_candidate_flush_failures_preserve_closed_residue(self) -> None:
        for asset, expected_phases in (
            ("database", set()),
            ("manifest", {"DB_REPLACED"}),
        ):
            with self.subTest(asset=asset):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                identity, coordinator, service = _fixture(root)
                target_candidates: set[int] = set()
                captured: list[tuple[object, Path]] = []
                flush_failures = 0
                real_copy = (
                    tm_stage_sealer._PortableAuthorityBorrow
                    .copy_asset_to_new_candidate
                )
                real_flush = (
                    platform_fs_windows._WindowsCandidateFile._flush_content
                )

                def capture_asset_candidate(
                    borrow: object,
                    *,
                    platform: object,
                    parent: object,
                    asset: str,
                    candidate_name: str,
                ) -> tuple[object, object]:
                    candidate, facts = real_copy(
                        borrow,
                        platform=platform,
                        parent=parent,
                        asset=asset,
                        candidate_name=candidate_name,
                    )
                    if asset == target_asset:
                        target_candidates.add(id(candidate))
                        captured.append(
                            (
                                candidate,
                                Path(candidate._entry_path),
                            )
                        )
                    return candidate, facts

                def fail_target_flush(
                    candidate: object,
                    expected: object,
                ) -> object:
                    nonlocal flush_failures
                    if id(candidate) in target_candidates:
                        flush_failures += 1
                        raise PlatformFileError(
                            PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                            retryable=False,
                        )
                    return real_flush(candidate, expected)

                target_asset = asset
                try:
                    with (
                        mock.patch.object(
                            tm_stage_sealer._PortableAuthorityBorrow,
                            "copy_asset_to_new_candidate",
                            new=capture_asset_candidate,
                        ),
                        mock.patch.object(
                            platform_fs_windows._WindowsCandidateFile,
                            "_flush_content",
                            new=fail_target_flush,
                        ),
                    ):
                        outcome = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertEqual(
                        flush_failures,
                        1,
                        f"{asset} publication candidate flush was not reached",
                    )
                    self.assertIs(type(outcome), MigrationFailure)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertIsNone(coordinator.current_generation)
                    self.assertIsNone(coordinator._view)
                    self.assertEqual(
                        _publication_phase_set(root, identity),
                        expected_phases,
                    )
                    self.assertEqual(len(captured), 1)
                    candidate, residue = captured[0]
                    self.assertTrue(candidate.closed)
                    self.assertTrue(residue.is_file())
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()
                self.assertFalse(root.exists())

    def test_tail_failures_withhold_view_after_complete_phase_chain(self) -> None:
        for boundary in (
            "final_active_reproof",
            "token_consume",
            "stage_retire",
            "lineage_marker",
        ):
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity, coordinator, service = _fixture(root)
                    registry = coordinator._sealed_registry
                    reached = 0
                    if boundary == "token_consume":
                        patchers = [
                            mock.patch.object(
                                registry,
                                "consume",
                                side_effect=tm_stage_sealer.StageSealError(
                                    "SEALER.ATTESTATION_UNAVAILABLE"
                                ),
                            )
                        ]
                    elif boundary == "final_active_reproof":
                        real_publish_generation = (
                            coordinator._publish_portable_generation
                        )
                        real_reprove = (
                            tm_sqlite_store._PortableActiveSetAuthority.reprove
                        )
                        generation_published = False

                        def publish_generation(*args: object, **kwargs: object) -> object:
                            nonlocal generation_published
                            result = real_publish_generation(*args, **kwargs)
                            generation_published = True
                            return result

                        def fail_final_reproof(
                            authority: object,
                        ) -> object:
                            nonlocal reached
                            if generation_published:
                                reached += 1
                                raise ActivationPreparationError(
                                    "ACTIVATION.TEST_TAIL_FAILURE",
                                    retryable=False,
                                )
                            return real_reprove(authority)

                        patchers = [
                            mock.patch.object(
                                coordinator,
                                "_publish_portable_generation",
                                side_effect=publish_generation,
                            ),
                            mock.patch.object(
                                tm_sqlite_store._PortableActiveSetAuthority,
                                "reprove",
                                new=fail_final_reproof,
                            ),
                        ]
                    elif boundary == "stage_retire":
                        patchers = [
                            mock.patch.object(
                                tm_migration,
                                "_cleanup_initial_unpublished_stage",
                                side_effect=ActivationPreparationError(
                                    "ACTIVATION.TEST_TAIL_FAILURE",
                                    retryable=False,
                                ),
                            )
                        ]
                    else:
                        patchers = [
                            mock.patch.object(
                                coordinator,
                                "_ensure_portable_activation_lineage_marker",
                                side_effect=ActivationPreparationError(
                                    "ACTIVATION.TEST_TAIL_FAILURE",
                                    retryable=False,
                                ),
                            )
                        ]
                    try:
                        with ExitStack() as stack:
                            boundaries = [
                                stack.enter_context(patcher)
                                for patcher in patchers
                            ]
                            outcome = service.activate_initial(
                                identity.configured_jsonl_path,
                                identity.resource_id,
                            )

                        if boundary == "final_active_reproof":
                            self.assertEqual(
                                reached,
                                1,
                                "final portable active reproof was not reached",
                            )
                        else:
                            self.assertEqual(
                                boundaries[0].call_count,
                                1,
                                f"portable {boundary} boundary was not reached",
                            )
                        self.assertIs(type(outcome), MigrationFailure)
                        self.assertEqual(coordinator.state, "ACTIVATING")
                        self.assertIsNone(coordinator.current_generation)
                        self.assertIsNone(coordinator._view)
                        self.assertEqual(
                            _publication_phase_set(root, identity),
                            {
                                "DB_REPLACED",
                                "MANIFEST_PUBLISHED",
                                "GENERATION_PUBLISHED",
                            },
                        )
                    finally:
                        _remove_long_quarantine(root)

    def test_programmer_errors_escape_publication_with_all_handles_closed(
        self,
    ) -> None:
        for error_type in (TypeError, AssertionError, AttributeError):
            with self.subTest(error_type=error_type.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity, coordinator, service = _fixture(root)
                    registry = coordinator._sealed_registry
                    injected = error_type("portable programmer boundary")
                    reservations: list[object] = []
                    live_authorities: list[object] = []
                    candidates: list[object] = []
                    pending_publications: list[object] = []
                    real_acquire = service._acquire_initial_reservation
                    real_issue = registry.issue_token
                    candidate_type = platform_fs_windows._WindowsCandidateFile
                    pending_type = (
                        platform_fs_windows._WindowsPendingPublication
                    )
                    real_candidate_init = candidate_type.__init__
                    real_pending_init = pending_type.__init__

                    def acquire() -> object:
                        reservation = real_acquire()
                        reservations.append(reservation)
                        return reservation

                    def issue(*args: object, **kwargs: object) -> object:
                        token = real_issue(*args, **kwargs)
                        entry = registry._token_entry(token)
                        live_authorities.append(entry.live_authority)
                        return token

                    def capture_candidate(
                        candidate: object,
                        *args: object,
                        **kwargs: object,
                    ) -> None:
                        real_candidate_init(candidate, *args, **kwargs)
                        candidates.append(candidate)

                    def capture_pending(
                        pending: object,
                        *args: object,
                        **kwargs: object,
                    ) -> None:
                        real_pending_init(pending, *args, **kwargs)
                        pending_publications.append(pending)

                    try:
                        with (
                            mock.patch.object(
                                service,
                                "_acquire_initial_reservation",
                                side_effect=acquire,
                            ),
                            mock.patch.object(
                                registry,
                                "issue_token",
                                side_effect=issue,
                            ),
                            mock.patch.object(
                                coordinator,
                                "publish_portable_activation",
                                create=True,
                                side_effect=injected,
                            ) as publish,
                            mock.patch.object(
                                candidate_type,
                                "__init__",
                                new=capture_candidate,
                            ),
                            mock.patch.object(
                                pending_type,
                                "__init__",
                                new=capture_pending,
                            ),
                        ):
                            with self.assertRaises(error_type) as caught:
                                service.activate_initial(
                                    identity.configured_jsonl_path,
                                    identity.resource_id,
                                )

                        self.assertIs(caught.exception, injected)
                        self.assertEqual(publish.call_count, 1)
                        self.assertEqual(len(reservations), 1)
                        reservation = reservations[0]
                        self.assertTrue(reservation._root.closed)
                        self.assertTrue(reservation._lease.closed)
                        self.assertTrue(candidates)
                        self.assertTrue(
                            all(candidate.closed for candidate in candidates)
                        )
                        self.assertTrue(pending_publications)
                        self.assertTrue(
                            all(
                                pending.closed
                                for pending in pending_publications
                            )
                        )
                        self.assertTrue(live_authorities)
                        self.assertTrue(
                            all(
                                authority is None or authority.closed
                                for authority in live_authorities
                            )
                        )
                    finally:
                        for authority in live_authorities:
                            if authority is not None and not authority.closed:
                                authority.close()
                        for pending in pending_publications:
                            if not pending.closed:
                                pending.close()
                        for candidate in candidates:
                            if not candidate.closed:
                                candidate.close()
                        for reservation in reservations:
                            if not reservation._lease.closed:
                                reservation._lease.close()
                            if not reservation._root.closed:
                                reservation._root.close()
                        _remove_long_quarantine(root)

    def test_internal_cleanup_fault_does_not_mask_programmer_error(self) -> None:
        for boundary, error_type in (
            ("candidate", TypeError),
            ("pending", AssertionError),
            ("pending", AttributeError),
        ):
            with self.subTest(boundary=boundary, error_type=error_type.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity, coordinator, service = _fixture(root)
                    injected = error_type("portable programmer boundary")
                    target_authorities: set[int] = set()
                    close_faults = 0
                    candidate_type = platform_fs_windows._WindowsCandidateFile
                    pending_type = platform_fs_windows._WindowsPendingPublication
                    real_copy = (
                        tm_stage_sealer._PortableAuthorityBorrow
                        .copy_asset_to_new_candidate
                    )
                    real_candidate_flush = candidate_type._flush_content
                    real_candidate_close = candidate_type._close_authority
                    real_pending_init = pending_type.__init__
                    real_pending_close = (
                        platform_fs_contracts.PendingPublication._close_authority
                    )
                    real_phase_publish = (
                        tm_activation_journal._WindowsPortablePublicationPhaseOwner
                        .publish
                    )

                    def capture_database_candidate(
                        borrow: object,
                        *,
                        platform: object,
                        parent: object,
                        asset: str,
                        candidate_name: str,
                    ) -> tuple[object, object]:
                        result = real_copy(
                            borrow,
                            platform=platform,
                            parent=parent,
                            asset=asset,
                            candidate_name=candidate_name,
                        )
                        if boundary == "candidate" and asset == "database":
                            target_authorities.add(id(result[0]))
                        return result

                    def fail_candidate_flush(
                        candidate: object,
                        expected: object,
                    ) -> object:
                        if id(candidate) in target_authorities:
                            raise injected
                        return real_candidate_flush(candidate, expected)

                    def close_candidate_with_fault(candidate: object) -> None:
                        nonlocal close_faults
                        real_candidate_close(candidate)
                        if id(candidate) in target_authorities:
                            close_faults += 1
                            raise PlatformFileError(
                                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                                retryable=False,
                            )

                    def capture_pending(
                        pending: object,
                        *args: object,
                        **kwargs: object,
                    ) -> None:
                        real_pending_init(pending, *args, **kwargs)
                        retained = args[1]
                        if (
                            boundary == "pending"
                            and Path(retained._entry_path).name
                            == identity.canonical_sidecar_path.name
                        ):
                            target_authorities.add(id(pending))

                    def fail_database_phase(**kwargs: object) -> object:
                        if (
                            boundary == "pending"
                            and kwargs["unsigned"].phase == "DB_REPLACED"
                        ):
                            raise injected
                        return real_phase_publish(**kwargs)

                    def close_pending_with_fault(pending: object) -> None:
                        nonlocal close_faults
                        real_pending_close(pending)
                        if id(pending) in target_authorities:
                            close_faults += 1
                            raise PlatformFileError(
                                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                                retryable=False,
                            )

                    try:
                        with (
                            mock.patch.object(
                                tm_stage_sealer._PortableAuthorityBorrow,
                                "copy_asset_to_new_candidate",
                                new=capture_database_candidate,
                            ),
                            mock.patch.object(
                                candidate_type,
                                "_flush_content",
                                new=fail_candidate_flush,
                            ),
                            mock.patch.object(
                                candidate_type,
                                "_close_authority",
                                new=close_candidate_with_fault,
                            ),
                            mock.patch.object(
                                pending_type,
                                "__init__",
                                new=capture_pending,
                            ),
                            mock.patch.object(
                                tm_activation_journal
                                ._WindowsPortablePublicationPhaseOwner,
                                "publish",
                                new=fail_database_phase,
                            ),
                            mock.patch.object(
                                pending_type,
                                "_close_authority",
                                new=close_pending_with_fault,
                            ),
                        ):
                            with self.assertRaises(error_type) as caught:
                                service.activate_initial(
                                    identity.configured_jsonl_path,
                                    identity.resource_id,
                                )

                        self.assertIs(caught.exception, injected)
                        self.assertEqual(close_faults, 1)
                        self.assertEqual(coordinator.state, "ACTIVATING")
                        self.assertIsNone(coordinator.current_generation)
                        self.assertIsNone(coordinator._view)
                    finally:
                        _remove_long_quarantine(root)


if __name__ == "__main__":
    unittest.main()
