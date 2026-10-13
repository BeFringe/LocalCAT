"""Task 2.3 proven initial-activation failure and rollback contract."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

import tm_migration
import tm_activation_recovery as activation_recovery
from tm_activation_journal import ActivationPreparationError
from tm_contracts import (
    AssetPreservationState,
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
)
from tm_engine import TMEngine
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore
from tm_stage_sealer import StageSealError


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _remove_windows_long_quarantine(root: Path) -> None:
    """Remove the retained exact pair through Win32 extended paths."""

    if os.name != "nt":
        return
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


def _fixture(
    root: Path,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    TMMigrationService,
]:
    source = (root / "primary.jsonl").resolve()
    source.write_bytes(SOURCE_BYTES)
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        source,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    return (
        identity,
        coordinator,
        TMMigrationService(
            resource_identity=identity,
            canonical_store_id="store.primary",
            coordinator=coordinator,
        ),
    )


def _assert_legacy_safe(
    testcase: unittest.TestCase,
    *,
    identity: CanonicalResourceIdentity,
    coordinator: ResourceStoreCoordinator,
    outcome: object,
    expected_stage: str,
) -> None:
    testcase.assertIs(type(outcome), MigrationFailure)
    failure = outcome
    assert isinstance(failure, MigrationFailure)
    testcase.assertEqual(failure.stage, expected_stage)
    testcase.assertIsNone(failure.active_generation)
    testcase.assertIs(
        failure.original_source_preservation.state,
        AssetPreservationState.VERIFIED_UNCHANGED,
    )
    testcase.assertIs(
        failure.active_store_preservation.state,
        AssetPreservationState.NOT_APPLICABLE,
    )
    testcase.assertEqual(failure.recovery_locators, ())
    testcase.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)
    testcase.assertEqual(coordinator.state, "READY")
    testcase.assertIsNone(coordinator.current_generation)
    testcase.assertIsNone(coordinator.active_store_path)
    testcase.assertIsNone(coordinator.durable_activation_phase)
    testcase.assertFalse(identity.canonical_sidecar_path.exists())
    testcase.assertFalse(identity.snapshot_manifest_path.exists())
    testcase.assertFalse(
        identity.canonical_sidecar_path.with_name(
            f".{identity.canonical_sidecar_path.name}.localcat-activation-journal.json"
        ).exists()
    )
    testcase.assertEqual(tuple(identity.canonical_sidecar_path.parent.glob("*.stage")), ())
    testcase.assertEqual(
        tuple(identity.canonical_sidecar_path.parent.glob("*.manifest.tmp")),
        (),
    )
    legacy = TMEngine(str(identity.configured_jsonl_path), update=False)
    testcase.assertFalse(legacy.canonical_active)
    exact = legacy.query_exact("same")
    testcase.assertIsNotNone(exact)
    assert exact is not None
    testcase.assertEqual(exact.target, "winner")
    rendered = repr(failure)
    testcase.assertNotIn(str(identity.configured_jsonl_path.parent), rendered)
    testcase.assertNotIn("winner", rendered)
    testcase.assertNotIn("sensitive", rendered)


def _assert_authority_unavailable(
    testcase: unittest.TestCase,
    outcome: object,
) -> MigrationFailure:
    testcase.assertIs(type(outcome), MigrationFailure)
    failure = outcome
    assert isinstance(failure, MigrationFailure)
    testcase.assertEqual(
        failure.error_code,
        "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
    )
    testcase.assertFalse(failure.retryable)
    testcase.assertFalse(failure.canonical_authority_published)
    testcase.assertTrue(failure.canonical_authority_ambiguous)
    testcase.assertIsNone(failure.active_generation)
    testcase.assertEqual(failure.recovery_locators, ())
    return failure


class InitialActivationRollbackTests(unittest.TestCase):
    def test_linux_exclusive_move_uses_renameat2_noreplace_flag(self) -> None:
        renameat2 = Mock(return_value=0)
        libc = SimpleNamespace(renameat2=renameat2)
        with patch.object(tm_migration.ctypes, "CDLL", return_value=libc):
            tm_migration._linux_rename_noreplace_at(
                11,
                "source.stage",
                22,
                "target.stage",
            )
        renameat2.assert_called_once_with(
            11,
            b"source.stage",
            22,
            b"target.stage",
            0x00000001,
        )

    def test_cleanup_basename_substitution_preserves_foreign_and_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, service = _fixture(root)
            real_cleanup = tm_migration._cleanup_platform_initial_unpublished_stage
            moved_owned = []
            preserved_foreign = []
            quarantine_targets = []
            foreign_payload = b"foreign replacement must survive"

            def cleanup_with_substitution(attempt):
                backend = attempt.platform_backend
                real_retire = backend.retire_owned_exclusive

                def substitute(source_parent, source_name, reservation, target_parent, target_name):
                    self.assertIs(source_parent, attempt.platform_parent)
                    self.assertIs(reservation, attempt.stage_reservation)
                    source = root / source_name
                    target = attempt.quarantine_dir / target_name
                    moved = root / f"owned-away-{source_name}"
                    source.rename(moved)
                    source.write_bytes(foreign_payload)
                    moved_owned.append(moved)
                    preserved_foreign.append(source)
                    quarantine_targets.append(target)
                    return real_retire(source_parent, source_name, reservation, target_parent, target_name)

                with patch.object(backend, "retire_owned_exclusive", substitute):
                    return real_cleanup(attempt)

            with patch.object(coordinator, "_seal_stage", side_effect=StageSealError("SEALER.STAGE_INVALID")), patch.object(
                tm_migration, "_cleanup_platform_initial_unpublished_stage", cleanup_with_substitution
            ):
                outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)

            self.assertEqual(len(moved_owned), 1)
            self.assertTrue(moved_owned[0].exists())
            self.assertEqual(preserved_foreign[0].read_bytes(), foreign_payload)
            self.assertFalse(quarantine_targets[0].exists())
            _assert_authority_unavailable(self, outcome)
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_quarantine_target_race_preserves_foreign_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_cleanup = tm_migration._cleanup_platform_initial_unpublished_stage
            raced_targets = []
            preserved_sources = []
            foreign_payload = b"foreign quarantine target must survive"

            def cleanup_with_race(attempt):
                backend = attempt.platform_backend
                real_retire = backend.retire_owned_exclusive

                def occupy(source_parent, source_name, reservation, target_parent, target_name):
                    self.assertIs(source_parent, attempt.platform_parent)
                    target = attempt.quarantine_dir / target_name
                    target.write_bytes(foreign_payload)
                    raced_targets.append(target)
                    source = identity.configured_jsonl_path.parent / source_name
                    preserved_sources.append((source, source.read_bytes()))
                    return real_retire(source_parent, source_name, reservation, target_parent, target_name)

                with patch.object(backend, "retire_owned_exclusive", occupy):
                    return real_cleanup(attempt)

            with patch.object(coordinator, "_seal_stage", side_effect=StageSealError("SEALER.STAGE_INVALID")), patch.object(
                tm_migration, "_cleanup_platform_initial_unpublished_stage", cleanup_with_race
            ):
                outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)

            _assert_authority_unavailable(self, outcome)
            self.assertEqual(len(raced_targets), 1)
            self.assertEqual(raced_targets[0].read_bytes(), foreign_payload)
            self.assertEqual(preserved_sources[0][0].read_bytes(), preserved_sources[0][1])
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_builder_residue_before_return_never_reports_legacy_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            residue: list[Path] = []
            real_build = service._build_stage

            def leave_attempt_asset(
                source: Path,
                **kwargs: Any,
            ) -> Any:
                real_build(source, **kwargs)
                preflight = kwargs["preflight"]
                stage = tm_migration._deterministic_stage_ref(
                    identity,
                    source_digest=preflight.source_digest,
                    stage_prefix=kwargs["stage_prefix"],
                    path_salt=kwargs["path_salt"],
                )
                stage.staged_db_path.rename(stage.staged_db_path.with_suffix(".owned-away"))
                stage.staged_db_path.write_bytes(b"foreign attempt residue")
                residue.append(stage.staged_db_path)
                raise OSError("sensitive builder payload")

            with (
                patch.object(
                    service,
                    "_build_stage",
                    side_effect=leave_attempt_asset,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            _assert_authority_unavailable(self, outcome)
            self.assertEqual(len(residue), 1)
            self.assertEqual(residue[0].read_bytes(), b"foreign attempt residue")
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_retryable_primary_build_error_survives_transient_cleanup_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            real_cleanup = tm_migration._cleanup_initial_unpublished_stage
            cleanup_calls = 0

            def fail_cleanup_once(attempt: Any) -> None:
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 1:
                    raise MigrationPreflightError(
                        "MIGRATION.INITIAL_CLEANUP_UNPROVEN"
                    )
                real_cleanup(attempt)

            with (
                patch.object(
                    SQLiteTMStore,
                    "_append_streamed_batch_for_immediate_seal",
                    side_effect=OSError("sensitive primary build payload"),
                ),
                patch(
                    "tm_migration._cleanup_initial_unpublished_stage",
                    side_effect=fail_cleanup_once,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(cleanup_calls, 2)
            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(outcome.stage, "BUILD")
            self.assertEqual(outcome.error_code, "MIGRATION.INITIAL_IO_FAILED")
            self.assertTrue(outcome.retryable)
            self.assertNotIn("sensitive", repr(outcome))

    def test_tampered_cancel_terminal_fails_cold_recovery_proof(self) -> None:
        import tm_activation_journal as journal
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            terminal = journal._activation_terminal_path(identity)
            real_read = journal._CallerHeldPosixActivationBorrow.read_journal
            hits = []
            def tamper_then_read(borrow, path, *args, **kwargs):
                if path == terminal and terminal.exists() and not hits:
                    terminal.write_bytes(b"tampered terminal")
                    hits.append(path)
                return real_read(borrow, path, *args, **kwargs)
            with patch.object(coordinator, "publish_activation", side_effect=OSError("publication fault")), patch.object(journal._CallerHeldPosixActivationBorrow, "read_journal", tamper_then_read), patch.object(coordinator, "cancel_prepared_activation", wraps=coordinator.cancel_prepared_activation) as cancel:
                outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
            cancel.assert_called_once()
            self.assertEqual(hits, [terminal])
            _assert_authority_unavailable(self, outcome)
            self.assertEqual(terminal.read_bytes(), b"tampered terminal")
            with self.assertRaisesRegex(ValueError, "^TM.CANONICAL_UNHEALTHY$"):
                TMEngine(str(identity.configured_jsonl_path), update=False)

    def test_tampered_cancel_quarantine_fails_cold_recovery_proof(self) -> None:
        from platform_fs_contracts import RetainedRetirement
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, service = _fixture(root)
            real_reprove = RetainedRetirement.reprove
            tampered, displaced = [], []
            def tamper_then_reprove(retained):
                candidates = sorted(path for path in root.rglob("*") if path.is_file() and ".localcat-activation-quarantine-v1" in path.parts)
                if candidates and not tampered:
                    moved = root / ("displaced-" + candidates[0].name)
                    candidates[0].rename(moved)
                    candidates[0].write_bytes(b"tampered quarantine payload")
                    tampered.append(candidates[0])
                    displaced.append(moved)
                return real_reprove(retained)
            with patch.object(coordinator, "publish_activation", side_effect=OSError("publication fault")), patch.object(RetainedRetirement, "reprove", tamper_then_reprove), patch.object(coordinator, "cancel_prepared_activation", wraps=coordinator.cancel_prepared_activation) as cancel:
                outcome = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
            cancel.assert_called_once()
            _assert_authority_unavailable(self, outcome)
            self.assertEqual(len(tampered), 1)
            self.assertEqual(len(displaced), 1)
            self.assertTrue(displaced[0].is_file())
            self.assertEqual(tampered[0].read_bytes(), b"tampered quarantine payload")

    def test_fully_rolled_back_plain_io_failure_is_stably_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            with patch.object(
                service._coordinator,
                "publish_activation",
                side_effect=OSError("sensitive publication payload"),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(outcome.error_code, "MIGRATION.INITIAL_IO_FAILED")
            self.assertTrue(outcome.retryable)

    def test_build_failure_cleans_created_stage_and_returns_legacy_safe_failure(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.addCleanup(_remove_windows_long_quarantine, root)
        identity, coordinator, service = _fixture(root)
        with patch.object(
            SQLiteTMStore,
            "_append_streamed_batch_for_immediate_seal",
            side_effect=OSError("sensitive source payload"),
        ):
            outcome = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )
        _assert_legacy_safe(
            self,
            identity=identity,
            coordinator=coordinator,
            outcome=outcome,
            expected_stage="BUILD",
        )
        if os.name == "nt":
            quarantined = tuple(
                (root / ".localcat-activation-quarantine-v1").glob(
                    "initial-*/*"
                )
            )
            self.assertEqual(len(quarantined), 2)
            self.assertEqual(
                {path.suffix for path in quarantined},
                {".stage", ".tmp"},
            )

    def test_seal_failure_identity_safely_removes_unpublished_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                coordinator,
                "_seal_stage",
                side_effect=StageSealError("SEALER.STAGE_INVALID"),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="SEAL",
            )

    def test_prepare_failure_identity_safely_removes_unpublished_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                coordinator,
                "activate",
                side_effect=ActivationPreparationError(
                    "ACTIVATION.GATE_B_DENIED",
                    retryable=False,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="PREPARE",
            )

    def test_pre_journal_failure_cancels_preparation_and_preserves_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                coordinator,
                "publish_prepared_activation",
                side_effect=ActivationPreparationError(
                    "ACTIVATION.JOURNAL_WRITE_FAILED",
                    retryable=True,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="JOURNAL",
            )

            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                retry = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(retry), MigrationReport)
            assert isinstance(retry, MigrationReport)
            self.assertEqual(retry.activated_generation, 0)
            self.assertEqual(coordinator.current_generation, 0)

    def test_durable_prepared_publish_failure_rolls_back_before_reporting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                coordinator,
                "publish_activation",
                side_effect=OSError("sensitive publication payload"),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="PUBLISH",
            )

    def test_durable_db_replaced_failure_rolls_back_before_reporting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch(
                "tm_activation_recovery._publish_activation_receipt",
                side_effect=OSError("sensitive receipt payload"),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="PUBLISH",
            )

    def test_durable_manifest_published_failure_rolls_back_before_reporting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_advance = coordinator._advance_activation_journal_after_effect_locked

            def fail_generation_journal(
                preparation: Any,
                handle: Any,
                next_phase: Any,
                **kwargs: Any,
            ) -> Any:
                if getattr(next_phase, "value", None) == "GENERATION_PUBLISHED":
                    raise OSError("sensitive generation payload")
                return real_advance(
                    preparation,
                    handle,
                    next_phase,
                    **kwargs,
                )

            with patch.object(
                coordinator,
                "_advance_activation_journal_after_effect_locked",
                side_effect=fail_generation_journal,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_legacy_safe(
                self,
                identity=identity,
                coordinator=coordinator,
                outcome=outcome,
                expected_stage="PUBLISH",
            )

    def test_unprovable_durable_rollback_never_returns_legacy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            actual_replace = activation_recovery._replace_activation_database
            hits = []
            def named_then_failed(*args, **kwargs):
                actual_replace(*args, **kwargs)
                self.assertTrue(identity.canonical_sidecar_path.is_file())
                hits.append(True)
                raise OSError("sensitive publication payload")
            with patch.object(activation_recovery, '_replace_activation_database', named_then_failed):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertEqual(hits, [True])
            self.assertTrue(coordinator._family._recovery_required)
            failure = _assert_authority_unavailable(self, outcome)
            self.assertNotIn("sensitive", repr(failure))

    def test_unprovable_stage_cleanup_never_returns_legacy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with (
                patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=StageSealError("SEALER.STAGE_INVALID"),
                ),
                patch(
                    "tm_migration._cleanup_initial_unpublished_stage",
                    side_effect=MigrationPreflightError(
                        "MIGRATION.INITIAL_CLEANUP_UNPROVEN"
                    ),
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            _assert_authority_unavailable(self, outcome)

    def test_ambiguous_existing_terminal_never_returns_legacy_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            terminal = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}.localcat-activation-terminal.json"
            )
            terminal.write_bytes(b"sensitive foreign terminal")
            with (
                patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=StageSealError("SEALER.STAGE_INVALID"),
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            failure = _assert_authority_unavailable(self, outcome)
            self.assertNotIn("sensitive", repr(failure))

    def test_public_signature_accepts_no_cancellation_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            with self.assertRaises(TypeError):
                cast(Any, service.activate_initial)(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                    cancellation_token=object(),
                )


if __name__ == "__main__":
    unittest.main()
