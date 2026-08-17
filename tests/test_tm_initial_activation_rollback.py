"""Task 2.3 proven initial-activation failure and rollback contract."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

import tm_migration
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
            original_move = tm_migration._exclusive_initial_quarantine_move
            moved_owned: list[Path] = []
            quarantined_foreign: list[Path] = []
            foreign_payload = b"foreign replacement must survive"

            def substitute_before_quarantine(
                source_parent: Any,
                source_name: str,
                target_parent: Any,
                target_name: str,
            ) -> None:
                source = root / source_name
                target = target_parent.destination.parent / target_name
                moved = root / f"owned-away-{source.name}"
                source.rename(moved)
                source.write_bytes(foreign_payload)
                moved_owned.append(moved)
                quarantined_foreign.append(target)
                original_move(
                    source_parent,
                    source_name,
                    target_parent,
                    target_name,
                )

            with (
                patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=StageSealError("SEALER.STAGE_INVALID"),
                ),
                patch.object(
                    tm_migration,
                    "_exclusive_initial_quarantine_move",
                    side_effect=substitute_before_quarantine,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertTrue(moved_owned)
            self.assertTrue(all(path.exists() for path in moved_owned))
            self.assertTrue(quarantined_foreign)
            self.assertEqual(quarantined_foreign[0].read_bytes(), foreign_payload)
            _assert_authority_unavailable(self, outcome)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_quarantine_target_race_preserves_foreign_and_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            original_move = tm_migration._exclusive_initial_quarantine_move
            raced_targets: list[Path] = []
            foreign_payload = b"foreign quarantine target must survive"

            def occupy_target_before_move(
                source_parent: Any,
                source_name: str,
                target_parent: Any,
                target_name: str,
            ) -> None:
                target = target_parent.destination.parent / target_name
                target.write_bytes(foreign_payload)
                raced_targets.append(target)
                original_move(
                    source_parent,
                    source_name,
                    target_parent,
                    target_name,
                )

            with (
                patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=StageSealError("SEALER.STAGE_INVALID"),
                ),
                patch.object(
                    tm_migration,
                    "_exclusive_initial_quarantine_move",
                    side_effect=occupy_target_before_move,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            _assert_authority_unavailable(self, outcome)
            self.assertTrue(raced_targets)
            self.assertEqual(raced_targets[0].read_bytes(), foreign_payload)
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_builder_residue_before_return_never_reports_legacy_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            residue: list[Path] = []

            def leave_attempt_asset(
                source: Path,
                **kwargs: Any,
            ) -> Any:
                del source
                preflight = kwargs["preflight"]
                stage = tm_migration._deterministic_stage_ref(
                    identity,
                    source_digest=preflight.source_digest,
                    stage_prefix=kwargs["stage_prefix"],
                    path_salt=kwargs["path_salt"],
                )
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
                    "append_streamed_batch",
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

    def test_tampered_rollback_terminal_fails_cold_recovery_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_rollback = coordinator.rollback_durable_activation
            terminal = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}.localcat-activation-terminal.json"
            )

            def rollback_then_tamper() -> Any:
                report = real_rollback()
                terminal.write_bytes(b"tampered terminal")
                return report

            with (
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=OSError("sensitive publication payload"),
                ),
                patch.object(
                    coordinator,
                    "rollback_durable_activation",
                    side_effect=rollback_then_tamper,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            _assert_authority_unavailable(self, outcome)
            self.assertEqual(terminal.read_bytes(), b"tampered terminal")
            with self.assertRaisesRegex(
                ValueError,
                "TM.CANONICAL_ACTIVATION_AMBIGUOUS",
            ):
                TMEngine(str(identity.configured_jsonl_path), update=False)

    def test_tampered_rollback_quarantine_fails_cold_recovery_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, service = _fixture(root)
            real_rollback = coordinator.rollback_durable_activation
            tampered: list[Path] = []
            displaced: list[Path] = []

            def rollback_then_tamper() -> Any:
                report = real_rollback()
                candidates = sorted(
                    path
                    for path in root.rglob("*")
                    if path.is_file()
                    and ".localcat-activation-quarantine-v1" in path.parts
                )
                self.assertTrue(candidates)
                moved = root / f"displaced-{candidates[0].name}"
                candidates[0].rename(moved)
                candidates[0].write_bytes(b"tampered quarantine payload")
                tampered.append(candidates[0])
                displaced.append(moved)
                return report

            with (
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=OSError("sensitive publication payload"),
                ),
                patch.object(
                    coordinator,
                    "rollback_durable_activation",
                    side_effect=rollback_then_tamper,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            _assert_authority_unavailable(self, outcome)
            self.assertEqual(len(tampered), 1)
            self.assertEqual(len(displaced), 1)
            self.assertTrue(displaced[0].is_file())
            self.assertEqual(
                tampered[0].read_bytes(),
                b"tampered quarantine payload",
            )

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
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                SQLiteTMStore,
                "append_streamed_batch",
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
            with (
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=OSError("sensitive publication payload"),
                ),
                patch.object(
                    coordinator,
                    "rollback_durable_activation",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.ROLLBACK_RESTORE_FAILED",
                        retryable=True,
                    ),
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
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
