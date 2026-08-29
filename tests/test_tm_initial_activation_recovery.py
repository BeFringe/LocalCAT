"""Task 2.4 initial-activation published-tail and ambiguity contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import platform_fs_windows
import tm_migration
from tm_contracts import (
    AssetKind,
    AssetPreservationEvidence,
    AssetPreservationState,
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    RecoveryLocator,
    contract_from_json,
    contract_to_json,
)
from tm_engine import TMEngine
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
)
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


def _fresh_service(
    identity: CanonicalResourceIdentity,
) -> tuple[ResourceStoreCoordinator, TMMigrationService]:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    return coordinator, TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )


def _remove_windows_initial_artifacts(root: Path) -> None:
    for path in root.glob(".localcat-migration.initial-*"):
        path.unlink()
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


def _ambiguous_failure() -> MigrationFailure:
    return MigrationFailure(
        stage="RECOVERY",
        error_code="MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
        retryable=False,
        diagnostics=(),
        active_generation=None,
        original_source_preservation=AssetPreservationEvidence(
            asset_kind=AssetKind.ORIGINAL_SOURCE,
            state=AssetPreservationState.VERIFIED_UNCHANGED,
            before_digest="a" * 64,
            observed_digest="a" * 64,
        ),
        active_store_preservation=AssetPreservationEvidence(
            asset_kind=AssetKind.ACTIVE_STORE,
            state=AssetPreservationState.NOT_APPLICABLE,
            before_digest=None,
            observed_digest=None,
        ),
        recovery_locators=(),
        canonical_authority_ambiguous=True,
    )


def _published_failure() -> MigrationFailure:
    return replace(
        _ambiguous_failure(),
        active_generation=0,
        canonical_authority_published=True,
        canonical_authority_ambiguous=False,
    )


def _legacy_failure() -> MigrationFailure:
    return replace(
        _ambiguous_failure(),
        error_code="MIGRATION.INITIAL_IO_FAILED",
        canonical_authority_ambiguous=False,
    )


class MigrationAuthorityFailureContractTests(unittest.TestCase):
    def test_ambiguity_is_frozen_fail_stop_and_round_trips(self) -> None:
        failure = _ambiguous_failure()
        self.assertFalse(failure.canonical_authority_published)
        self.assertTrue(failure.canonical_authority_ambiguous)
        self.assertFalse(failure.retryable)
        self.assertEqual(failure.recovery_locators, ())
        with self.assertRaises(FrozenInstanceError):
            failure.canonical_authority_ambiguous = False  # pyright: ignore[reportAttributeAccessIssue]

        encoded = contract_to_json(failure)
        payload = json.loads(encoded)["payload"]
        self.assertFalse(payload["canonical_authority_published"])
        self.assertTrue(payload["canonical_authority_ambiguous"])
        self.assertEqual(contract_from_json(encoded), failure)
        self.assertEqual(contract_to_json(contract_from_json(encoded)), encoded)

    def test_published_unavailable_is_frozen_fail_stop_and_round_trips(
        self,
    ) -> None:
        failure = _published_failure()
        self.assertTrue(failure.canonical_authority_published)
        self.assertFalse(failure.canonical_authority_ambiguous)
        self.assertEqual(failure.active_generation, 0)
        self.assertEqual(contract_from_json(contract_to_json(failure)), failure)

        contradictory = json.loads(contract_to_json(failure))
        contradictory["payload"]["canonical_authority_ambiguous"] = True
        with self.assertRaisesRegex(ValueError, "contradictory"):
            contract_from_json(json.dumps(contradictory, sort_keys=True))

        wrong_type = json.loads(contract_to_json(failure))
        wrong_type["payload"]["canonical_authority_published"] = 1
        with self.assertRaises(TypeError):
            contract_from_json(json.dumps(wrong_type, sort_keys=True))

    def test_new_flags_are_mutually_exclusive_and_legacy_payload_defaults_false(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "contradictory"):
            replace(
                _ambiguous_failure(),
                canonical_authority_published=True,
            )

        with self.assertRaisesRegex(ValueError, "authority state"):
            replace(
                _ambiguous_failure(),
                canonical_authority_ambiguous=False,
            )

        legacy_failure = _legacy_failure()
        encoded = json.loads(contract_to_json(legacy_failure))
        self.assertNotIn(
            "canonical_authority_published",
            encoded["payload"],
        )
        self.assertNotIn(
            "canonical_authority_ambiguous",
            encoded["payload"],
        )
        decoded = contract_from_json(json.dumps(encoded, sort_keys=True))
        self.assertIs(type(decoded), MigrationFailure)
        assert isinstance(decoded, MigrationFailure)
        self.assertFalse(decoded.canonical_authority_published)
        self.assertFalse(decoded.canonical_authority_ambiguous)

    def test_authority_flags_are_one_closed_union_and_decode_as_a_pair(
        self,
    ) -> None:
        for changes in (
            {"stage": "BUILD"},
            {"error_code": "MIGRATION.INITIAL_IO_FAILED"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(_ambiguous_failure(), **changes)
        with self.assertRaises(ValueError):
            replace(_published_failure(), retryable=True)
        with self.assertRaises(ValueError):
            replace(
                _published_failure(),
                recovery_locators=(
                    RecoveryLocator(
                        path=Path("/safe/recovery/source.jsonl"),
                        asset_kind=AssetKind.ORIGINAL_SOURCE,
                        expected_digest="a" * 64,
                    ),
                ),
            )

        encoded = json.loads(contract_to_json(_published_failure()))
        both_false = json.loads(json.dumps(encoded))
        both_false["payload"]["canonical_authority_published"] = False
        both_false["payload"]["canonical_authority_ambiguous"] = False
        with self.assertRaises(ValueError):
            contract_from_json(json.dumps(both_false, sort_keys=True))

        legacy_both_false = json.loads(contract_to_json(_legacy_failure()))
        legacy_both_false["payload"][
            "canonical_authority_published"
        ] = False
        legacy_both_false["payload"][
            "canonical_authority_ambiguous"
        ] = False
        with self.assertRaises(ValueError):
            contract_from_json(
                json.dumps(legacy_both_false, sort_keys=True)
            )

        absent_unavailable = json.loads(json.dumps(encoded))
        del absent_unavailable["payload"]["canonical_authority_published"]
        del absent_unavailable["payload"]["canonical_authority_ambiguous"]
        with self.assertRaises(ValueError):
            contract_from_json(json.dumps(absent_unavailable, sort_keys=True))

        for stripped in (
            "canonical_authority_published",
            "canonical_authority_ambiguous",
        ):
            with self.subTest(stripped=stripped):
                malformed = json.loads(json.dumps(encoded))
                del malformed["payload"][stripped]
                with self.assertRaises(ValueError):
                    contract_from_json(json.dumps(malformed, sort_keys=True))

    def test_contract_decoder_rejects_duplicate_keys_at_every_depth(
        self,
    ) -> None:
        encoded = contract_to_json(_published_failure())
        duplicate_envelope = encoded.replace(
            '"contract_type":"MigrationFailure"',
            '"contract_type":"MigrationFailure",'
            '"contract_type":"MigrationFailure"',
            1,
        )
        duplicate_payload = encoded.replace(
            '"error_code":"MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE"',
            '"error_code":"MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",'
            '"error_code":"MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE"',
            1,
        )
        duplicate_nested = encoded.replace(
            '"asset_kind":"ACTIVE_STORE"',
            '"asset_kind":"ACTIVE_STORE",'
            '"asset_kind":"ACTIVE_STORE"',
            1,
        )
        for malformed in (
            duplicate_envelope,
            duplicate_payload,
            duplicate_nested,
        ):
            with self.subTest(malformed=malformed[:80]):
                with self.assertRaises(ValueError):
                    contract_from_json(malformed)


class InitialActivationRecoveryTests(unittest.TestCase):
    def test_legacy_post_seal_failure_does_not_use_portable_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, service = _fixture(root)
            source_digest = tm_migration._try_file_digest(
                identity.configured_jsonl_path
            )
            if source_digest is None:
                raise AssertionError("expected source digest")
            stage = tm_migration._deterministic_stage_ref(
                identity,
                source_digest=source_digest,
                stage_prefix="migration",
                path_salt="initial-legacy-routing",
            )
            attempt = tm_migration._InitialStageAttempt(
                stage=stage,
                parent_identity=(1, 1),
                quarantine_dir=root / "quarantine",
            )
            registry_type = type(coordinator._sealed_registry)
            with (
                patch.object(
                    registry_type,
                    "retire_unissued_portable",
                    side_effect=AssertionError("portable retirement used"),
                ) as retire,
                patch(
                    "tm_migration._cleanup_initial_unpublished_stage"
                ) as cleanup,
                patch(
                    "tm_migration._require_proven_initial_legacy_state"
                ) as prove_legacy,
            ):
                outcome = service._reconcile_initial_activation_failure(
                    MigrationPreflightError(
                        "MIGRATION.INITIAL_STAGE_UNAVAILABLE"
                    ),
                    stage_label="PREPARE",
                    source_before=source_digest,
                    preflight=None,
                    attempt=attempt,
                    sealed=object(),
                    prepared=None,
                    activation_attempted=False,
                    coordinator=coordinator,
                )

            retire.assert_not_called()
            cleanup.assert_called_once_with(attempt)
            prove_legacy.assert_called_once()
            self.assertIs(type(outcome), MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_STAGE_UNAVAILABLE",
            )

    def test_stage_freeze_programmer_type_error_crosses_public_seam(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, _coordinator, service = _fixture(Path(temporary))
            target = (
                patch.object(
                    platform_fs_windows.WindowsPlatformAdapter,
                    "_reserve_mutable_file",
                    side_effect=TypeError("programmer stage freeze type"),
                )
                if sys.platform == "win32"
                else patch(
                    "tm_migration._ExportParentHandle.bind",
                    side_effect=TypeError("programmer stage freeze type"),
                )
            )
            with target:
                with self.assertRaisesRegex(
                    TypeError,
                    "programmer stage freeze type",
                ):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

    def test_cleanup_programmer_type_error_crosses_public_seam(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            cleanup_target = (
                patch.object(
                    platform_fs_windows.WindowsPlatformAdapter,
                    "_retire_owned_exclusive",
                    side_effect=TypeError("programmer cleanup type"),
                )
                if sys.platform == "win32"
                else patch(
                    "tm_migration._exclusive_initial_quarantine_move",
                    side_effect=TypeError("programmer cleanup type"),
                )
            )
            with (
                patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=StageSealError("SEALER.STAGE_INVALID"),
                ),
                cleanup_target,
            ):
                with self.assertRaisesRegex(
                    TypeError,
                    "programmer cleanup type",
                ):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
            if sys.platform == "win32":
                _remove_windows_initial_artifacts(Path(temporary))

    def test_legacy_rehydrate_programmer_errors_cross_public_seam(self) -> None:
        for programmer_error in (
            AttributeError("programmer rehydrate attribute"),
            AssertionError("programmer rehydrate assertion"),
        ):
            with self.subTest(programmer_error=type(programmer_error).__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, coordinator, service = _fixture(Path(temporary))
                    rehydrate_target = (
                        patch.object(
                            platform_fs_windows.WindowsPlatformAdapter,
                            "_open_regular",
                            side_effect=programmer_error,
                        )
                        if sys.platform == "win32"
                        else patch.object(
                            ResourceStoreCoordinator,
                            "rehydrate_runtime_authority",
                            side_effect=programmer_error,
                        )
                    )
                    with (
                        patch.object(
                            coordinator,
                            "_seal_stage",
                            side_effect=StageSealError(
                                "SEALER.STAGE_INVALID"
                            ),
                        ),
                        rehydrate_target,
                    ):
                        with self.assertRaisesRegex(
                            type(programmer_error),
                            str(programmer_error),
                        ):
                            service.activate_initial(
                                identity.configured_jsonl_path,
                                identity.resource_id,
                            )
                    if sys.platform == "win32":
                        _remove_windows_initial_artifacts(Path(temporary))

    def test_programmer_errors_are_never_normalized_as_authority_outcomes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                service,
                "_build_stage",
                side_effect=AssertionError("programmer assertion"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "programmer assertion",
                ):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
            self.assertIsNone(coordinator.current_generation)

        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch.object(
                type(coordinator),
                "recover_durable_activation",
                side_effect=AttributeError("programmer attribute"),
            ):
                with self.assertRaisesRegex(
                    AttributeError,
                    "programmer attribute",
                ):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

    @unittest.skipIf(
        sys.platform == "win32",
        "legacy post-publication seam is not the Windows pre-journal route",
    )
    def test_legacy_post_publication_programmer_error_crosses_public_seam(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_publish = coordinator.publish_activation

            def publish_then_raise(*args: Any) -> int:
                real_publish(*args)
                raise OSError("tail return lost")

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=publish_then_raise,
                ),
                patch.object(
                    service,
                    "_verify_initial_activation_runtime",
                    side_effect=TypeError("programmer type"),
                ),
            ):
                with self.assertRaisesRegex(TypeError, "programmer type"):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

    def test_post_publication_verify_tail_recovers_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_verify = service._verify_initial_activation_runtime
            verify_calls = 0

            def fail_first_verify(**kwargs: Any) -> None:
                nonlocal verify_calls
                verify_calls += 1
                if verify_calls == 1:
                    raise MigrationPreflightError(
                        "MIGRATION.INITIAL_RUNTIME_REOPEN_FAILED"
                    )
                real_verify(**kwargs)

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    service,
                    "_verify_initial_activation_runtime",
                    side_effect=fail_first_verify,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationReport)
            assert isinstance(outcome, MigrationReport)
            self.assertEqual(outcome.activated_generation, 0)
            self.assertEqual(verify_calls, 3)
            self.assertEqual(coordinator.current_generation, 0)

    def test_published_tail_recovers_same_generation_without_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_publish = coordinator.publish_activation
            publish_calls = 0

            def publish_then_raise(*args: Any) -> int:
                nonlocal publish_calls
                publish_calls += 1
                real_publish(*args)
                raise OSError("tail return lost")

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=publish_then_raise,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationReport)
            report = outcome
            assert isinstance(report, MigrationReport)
            self.assertEqual(report.activated_generation, 0)
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(publish_calls, 1)
            self.assertEqual(
                tuple(
                    record.target_raw
                    for record in SQLiteTMStore.from_coordinator(
                        coordinator
                    ).exact_records("same")
                ),
                ("winner", "first"),
            )

            with self.assertRaises(MigrationPreflightError) as raised:
                service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertEqual(raised.exception.error_code, "MIGRATION.ALREADY_ACTIVE")

    def test_restart_rehydrates_published_generation_and_returns_same_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                first = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(first), MigrationReport)

            fresh, restarted = _fresh_service(identity)
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    restarted,
                    "_build_stage",
                    side_effect=AssertionError("recovery must not rebuild"),
                ),
            ):
                recovered = restarted.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(recovered, first)
            self.assertEqual(fresh.current_generation, 0)
            self.assertEqual(
                SQLiteTMStore.from_coordinator(fresh).exact_records("other")[0].target_raw,
                "value",
            )

    def test_persistent_post_publication_health_failure_is_unavailable_not_legacy_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    SQLiteTMStore,
                    "health",
                    side_effect=SQLiteStoreSchemaError(
                        "STORE.FORCED_HEALTH_FAILURE"
                    ),
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationFailure)
            failure = outcome
            assert isinstance(failure, MigrationFailure)
            self.assertFalse(failure.canonical_authority_ambiguous)
            self.assertTrue(failure.canonical_authority_published)
            self.assertEqual(
                failure.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertFalse(failure.retryable)
            self.assertEqual(failure.active_generation, 0)
            self.assertTrue(identity.canonical_sidecar_path.exists())
            self.assertTrue(identity.snapshot_manifest_path.exists())
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_unreadable_durable_phase_is_stable_ambiguous_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            coordinator_type = type(coordinator)
            with patch.object(
                coordinator_type,
                "recover_durable_activation",
                side_effect=ActivationPreparationError(
                    "ACTIVATION.JOURNAL_INVALID",
                    retryable=False,
                ),
            ):
                first = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                second = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            for outcome in (first, second):
                self.assertIs(type(outcome), MigrationFailure)
                failure = outcome
                assert isinstance(failure, MigrationFailure)
                self.assertTrue(failure.canonical_authority_ambiguous)
                self.assertEqual(
                    failure.error_code,
                    "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                )
                self.assertNotIn("private", repr(failure))
                self.assertNotIn("unsafe body", repr(failure))
            self.assertIsNone(coordinator.current_generation)
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)

    def test_corrupt_durable_journal_is_stably_ambiguous_and_blocks_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            journal = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}.localcat-activation-journal.json"
            )
            journal.write_bytes(b"not a trusted activation journal")
            with patch.object(
                service,
                "_build_stage",
                side_effect=AssertionError("ambiguous facts must not rebuild"),
            ):
                first = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                second = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            for outcome in (first, second):
                self.assertIs(type(outcome), MigrationFailure)
                assert isinstance(outcome, MigrationFailure)
                self.assertTrue(outcome.canonical_authority_ambiguous)
                self.assertFalse(outcome.canonical_authority_published)
                self.assertEqual(
                    outcome.error_code,
                    "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                )
            self.assertIsNone(coordinator.current_generation)
            with self.assertRaisesRegex(
                ValueError,
                "TM.CANONICAL_ACTIVATION_AMBIGUOUS",
            ):
                TMEngine(str(identity.configured_jsonl_path), update=False)

    def test_unprovable_pending_publish_is_stably_ambiguous_after_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=OSError("publication interrupted"),
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
                interrupted = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(interrupted), MigrationFailure)
            assert isinstance(interrupted, MigrationFailure)
            self.assertTrue(interrupted.canonical_authority_ambiguous)

            outcomes: list[MigrationFailure] = []
            for _ in range(2):
                fresh, restarted = _fresh_service(identity)
                with patch.object(
                    restarted,
                    "_build_stage",
                    side_effect=AssertionError("recovery must not rebuild"),
                ):
                    recovered = restarted.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(recovered), MigrationFailure)
                assert isinstance(recovered, MigrationFailure)
                outcomes.append(recovered)
                self.assertEqual(recovered.active_generation, None)
                self.assertFalse(recovered.canonical_authority_published)
                self.assertTrue(recovered.canonical_authority_ambiguous)
                self.assertFalse(recovered.retryable)
                self.assertIsNone(fresh.current_generation)

            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(
                outcomes[0].error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)


if __name__ == "__main__":
    unittest.main()
