"""Task 5.5 activation preparation, drain, token, and backup tests."""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
import tm_stage_sealer
from tm_activation_journal import _ensure_activation_lineage_marker
from tm_contracts import (
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    MutableStageRef,
    SealedStage,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_gate_b import GateBEvaluator
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    SQLiteTMStore,
    initialize_stage_schema,
)
from tm_stage_sealer import (
    _SealedArtifactRegistry as SealedArtifactRegistry,
    StageSealer,
)


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _identity(
    root: Path,
    resource_id: str = "tm.primary",
) -> CanonicalResourceIdentity:
    return CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        (root / f"{resource_id}.jsonl").resolve(),
    )


def _registry(
    coordinator: ResourceStoreCoordinator,
) -> SealedArtifactRegistry:
    return cast(SealedArtifactRegistry, coordinator._sealed_registry)


def _candidate(
    coordinator: ResourceStoreCoordinator,
    identity: CanonicalResourceIdentity,
    *,
    canonical_store_id: str = "store.primary",
    fts5_available: bool,
    expected_prior_generation: int | None,
) -> tuple[MutableStageRef, SealedStage]:
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        build = service.build_mutable_stage(identity.configured_jsonl_path)
        stage = build.mutable_stage
        if stage is None:
            raise AssertionError("expected a fresh mutable stage")
        sealed = coordinator._seal_stage(
            stage,
            canonical_store_id=canonical_store_id,
            expected_prior_generation=expected_prior_generation,
        )
    return stage, sealed


def _prior_stage(root: Path, identity: CanonicalResourceIdentity) -> MutableStageRef:
    return MutableStageRef(
        stage_id="stage.prior",
        resource_identity=identity,
        staged_db_path=(root / ".prior.sqlite3").resolve(),
        manifest_temp_path=(root / ".prior.manifest.tmp").resolve(),
    )


def _draft(source: str, target: str) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "prior"),),
    )


def _publish_prior_binding(
    store: SQLiteTMStore,
    identity: CanonicalResourceIdentity,
) -> SnapshotBinding:
    revision = store.canonical_revision()
    receipt = SnapshotReceipt(
        snapshot_id="snapshot.prior.1",
        resource_id=identity.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(
            identity.configured_jsonl_path.read_bytes()
        ).hexdigest(),
        record_count=revision.record_count,
    )
    manifest = SnapshotManifest(
        manifest_version=contract_module.SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    identity.snapshot_manifest_path.write_text(
        contract_to_json(manifest),
        encoding="utf-8",
    )
    binding = SnapshotBinding(
        configured_jsonl_path=identity.configured_jsonl_path,
        manifest_path=identity.snapshot_manifest_path,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        manifest=manifest,
    )
    store.register_completed_snapshot_binding(binding)
    return binding


def _existing_fixture(
    root: Path,
    *,
    fts5_available: bool,
    timeout_seconds: float = 1.0,
    candidate_resource_id: str = "tm.primary",
    candidate_store_id: str = "store.primary",
    candidate_expected_generation: int | None = 0,
) -> tuple[
    CanonicalResourceIdentity,
    SQLiteTMStore,
    ResourceStoreCoordinator,
    MutableStageRef,
    SealedStage,
]:
    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
    prior = _prior_stage(root, identity)
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        initialize_stage_schema(prior, canonical_store_id="store.primary")
        store = SQLiteTMStore(
            prior,
            canonical_store_id="store.primary",
            drain_timeout_seconds=timeout_seconds,
        )
    coordinator = store.coordinator
    candidate_identity = (
        identity
        if candidate_resource_id == identity.resource_id
        else _identity(root, candidate_resource_id)
    )
    if candidate_identity is not identity:
        candidate_identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
    stage, sealed = _candidate(
        coordinator,
        candidate_identity,
        canonical_store_id=candidate_store_id,
        fts5_available=fts5_available,
        expected_prior_generation=candidate_expected_generation,
    )
    _ = store.append_batch(
        batch_id="migration.prior",
        kind="migration",
        drafts=(_draft("prior", "canonical"),),
        source_digest=hashlib.sha256(SOURCE_BYTES).hexdigest(),
        source_path=identity.configured_jsonl_path,
    )
    _publish_prior_binding(store, identity)
    _ensure_activation_lineage_marker(identity)
    return identity, store, coordinator, stage, sealed


def _clone_sealed_stage(
    registry: SealedArtifactRegistry,
    stage: MutableStageRef,
    sealed: SealedStage,
    *,
    label: str,
) -> SealedStage:
    clone = MutableStageRef(
        stage_id=f"stage.clone.{label}",
        resource_identity=stage.resource_identity,
        staged_db_path=stage.staged_db_path.with_name(
            f".{label}.clone.sqlite3"
        ),
        manifest_temp_path=stage.manifest_temp_path.with_name(
            f".{label}.clone.manifest"
        ),
    )
    shutil.copyfile(stage.staged_db_path, clone.staged_db_path)
    shutil.copyfile(stage.manifest_temp_path, clone.manifest_temp_path)
    connection = sqlite3.connect(str(clone.staged_db_path))
    try:
        connection.execute(
            "UPDATE tm_meta SET value = 'UNPUBLISHED' "
            "WHERE key = 'activation_status' AND value = 'SEALED'"
        )
        connection.commit()
    finally:
        connection.close()
    return StageSealer(
        registry=registry,
        canonical_store_id=(
            sealed.evidence.source_binding.receipt.canonical_store_id
        ),
    ).seal(
        clone,
        expected_prior_generation=sealed.expected_prior_generation,
    )


class _SealedStageSubclass(SealedStage):
    pass


class _StringSubclass(str):
    pass


class ActivationPreparationHappyPathTests(unittest.TestCase):
    def test_two_gate_b_passes_each_rehash_three_attested_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            real_capture = tm_stage_sealer._capture_content_file
            with (
                patch(
                    "tm_stage_sealer._validate_stage_facts",
                    side_effect=AssertionError(
                        "activation Gate B must not semantically rescan"
                    ),
                ),
                patch(
                    "tm_stage_sealer._capture_content_file",
                    wraps=real_capture,
                ) as capture,
            ):
                prepared = coordinator.activate(sealed)

            self.assertEqual(capture.call_count, 6)
            coordinator.cancel_prepared_activation(prepared)

    def test_first_activation_prepares_without_publishing_or_backup(self) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
                    source_before = identity.configured_jsonl_path.read_bytes()
                    coordinator = ResourceStoreCoordinator(
                        canonical_store_id="store.primary",
                        resource_identity=identity,
                    )
                    stage, sealed = _candidate(
                        coordinator,
                        identity,
                        fts5_available=fts5_available,
                        expected_prior_generation=None,
                    )
                    stage_before = stage.staged_db_path.read_bytes()
                    manifest_before = stage.manifest_temp_path.read_bytes()

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        prepared = coordinator.activate(sealed)

                    self.assertFalse(prepared.had_prior_canonical)
                    self.assertEqual(prepared.backup_evidence, ())
                    self.assertIsNone(coordinator.current_generation)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.TOKEN_ISSUED,
                    )
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(identity.snapshot_manifest_path.exists())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        source_before,
                    )
                    self.assertEqual(stage.staged_db_path.read_bytes(), stage_before)
                    self.assertEqual(
                        stage.manifest_temp_path.read_bytes(),
                        manifest_before,
                    )
                    self.assertFalse(
                        any("journal" in path.name for path in root.iterdir())
                    )

                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.CANCELLED,
                    )

    def test_existing_canonical_backup_is_closed_and_assets_are_not_replaced(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, store, coordinator, stage, sealed = _existing_fixture(
                        root,
                        fts5_available=fts5_available,
                    )
                    prior_view = store.coordinator._view
                    assert prior_view is not None
                    prior_db_path = prior_view.stage.staged_db_path
                    prior_db = prior_db_path.read_bytes()
                    prior_manifest = identity.snapshot_manifest_path.read_bytes()
                    source_before = identity.configured_jsonl_path.read_bytes()

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        prepared = coordinator.activate(sealed)

                    self.assertTrue(prepared.had_prior_canonical)
                    self.assertEqual(len(prepared.backup_evidence), 2)
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertEqual(prior_db_path.read_bytes(), prior_db)
                    self.assertEqual(
                        identity.snapshot_manifest_path.read_bytes(),
                        prior_manifest,
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        source_before,
                    )
                    self.assertTrue(stage.staged_db_path.exists())
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    backup_payloads = {
                        item.asset_kind: item.backup_path.read_bytes()
                        for item in prepared._backup_assets
                    }
                    self.assertEqual(backup_payloads["DATABASE"], prior_db)
                    self.assertEqual(backup_payloads["MANIFEST"], prior_manifest)
                    for asset, evidence in zip(
                        prepared._backup_assets,
                        prepared.backup_evidence,
                        strict=True,
                    ):
                        self.assertEqual(
                            asset.backup_path.parent,
                            asset.original_path.parent,
                        )
                        self.assertFalse(asset.backup_path.is_symlink())
                        self.assertEqual(
                            evidence.original_digest,
                            evidence.backup_digest,
                        )
                        self.assertNotEqual(
                            evidence.original_identity,
                            evidence.backup_identity,
                        )

                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(
                        tuple(store.exact_records("prior"))[0].target_raw,
                        "canonical",
                    )
                    self.assertTrue(
                        all(
                            not item.backup_path.exists()
                            for item in prepared._backup_assets
                        )
                    )


class ActivationPreparationBoundaryTests(unittest.TestCase):
    def test_unactivated_coordinator_requires_exact_store_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            with self.assertRaises(TypeError):
                cast(Any, ResourceStoreCoordinator)(
                    resource_identity=identity,
                )
            for value in (None, 7, _StringSubclass("store.primary")):
                with self.subTest(value=value):
                    with self.assertRaises(TypeError):
                        ResourceStoreCoordinator(
                            canonical_store_id=cast(str, value),
                            resource_identity=identity,
                        )
            for value in ("", "   "):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        ResourceStoreCoordinator(
                            canonical_store_id=value,
                            resource_identity=identity,
                        )

    def test_only_exact_same_registry_sealed_stage_can_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            stage, sealed = _candidate(
                coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            subclass = _SealedStageSubclass(
                artifact=sealed.artifact,
                evidence=sealed.evidence,
                generation=sealed.generation,
                activation_nonce=sealed.activation_nonce,
                sealed_stage_digest=sealed.sealed_stage_digest,
            )
            foreign = SealedArtifactRegistry(registry_namespace="foreign")
            foreign_stage = _clone_sealed_stage(
                foreign,
                stage,
                sealed,
                label="foreign",
            )
            forged_equal = object.__new__(SealedStage)
            for item in fields(sealed):
                object.__setattr__(
                    forged_equal,
                    item.name,
                    getattr(sealed, item.name),
                )
            for value in (
                stage.staged_db_path,
                stage,
                sealed.evidence,
                True,
                subclass,
                forged_equal,
                foreign_stage,
            ):
                with self.subTest(value_type=type(value).__name__):
                    with self.assertRaises(ActivationPreparationError):
                        coordinator.activate(cast(SealedStage, value))
                    self.assertEqual(coordinator.state, "READY")

    def test_stale_generation_and_wrong_resource_or_store_deny_before_backup(
        self,
    ) -> None:
        cases = (
            ("stale", "tm.primary", "store.primary", 1),
            ("resource", "tm.other", "store.primary", 0),
            ("store", "tm.primary", "store.other", 0),
        )
        for label, resource_id, store_id, expected in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, store, coordinator, _stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                        candidate_resource_id=resource_id,
                        candidate_store_id=store_id,
                        candidate_expected_generation=expected,
                    )
                    with self.assertRaises(ActivationPreparationError):
                        coordinator.activate(sealed)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")
                    self.assertFalse(any("recovery" in item.name for item in root.iterdir()))

    def test_first_activation_wrong_store_denies_before_token_or_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                coordinator,
                identity,
                canonical_store_id="store.attacker",
                fts5_available=True,
                expected_prior_generation=None,
            )

            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)

            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.IDENTITY_MISMATCH",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertIsNone(coordinator.current_generation)
            self.assertIsNone(coordinator._preparation)
            self.assertIsNone(coordinator._cleanup_reservation)
            self.assertIs(
                _registry(coordinator).state(sealed),
                ActivationCapabilityState.SEALED,
            )
            self.assertFalse(
                any("recovery" in item.name for item in root.iterdir())
            )

    def test_prior_gate_success_is_not_trusted_after_artifact_or_source_change(
        self,
    ) -> None:
        for target in ("database", "database_inode", "manifest", "source"):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
                    coordinator = ResourceStoreCoordinator(
                        canonical_store_id="store.primary",
                        resource_identity=identity,
                    )
                    stage, sealed = _candidate(
                        coordinator,
                        identity,
                        fts5_available=True,
                        expected_prior_generation=None,
                    )
                    report = GateBEvaluator(
                        registry=_registry(coordinator)._readiness_view()
                    ).evaluate(sealed)
                    self.assertTrue(report.granted)
                    path = {
                        "database": stage.staged_db_path,
                        "database_inode": stage.staged_db_path,
                        "manifest": stage.manifest_temp_path,
                        "source": identity.configured_jsonl_path,
                    }[target]
                    if target == "database_inode":
                        replacement = path.with_name(".inode-replacement")
                        shutil.copyfile(path, replacement)
                        os.replace(replacement, path)
                    else:
                        path.write_bytes(path.read_bytes() + b"\n")
                    with self.assertRaises(ActivationPreparationError):
                        coordinator.activate(sealed)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertIs(
                        _registry(coordinator)._entries[
                            sealed.artifact.artifact_id
                        ].state,
                        ActivationCapabilityState.SEALED,
                    )

    def test_wrong_current_source_binding_denies_without_backup_or_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_database = prior_view.stage.staged_db_path.read_bytes()
            identity.snapshot_manifest_path.write_bytes(b"{}")

            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)

            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_BINDING_INVALID",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_database,
            )
            self.assertIs(
                _registry(coordinator)._entries[
                    sealed.artifact.artifact_id
                ].state,
                ActivationCapabilityState.CANCELLED,
            )
            self.assertFalse(
                any("recovery" in item.name for item in root.iterdir())
            )
            self.assertEqual(
                store.exact_records("prior")[0].target_raw,
                "canonical",
            )


class ActivationDrainAndFailureTests(unittest.TestCase):
    def test_concurrent_prepare_is_rejected_without_second_token_or_aba(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _identity_value, _store, coordinator, stage, sealed = (
                _existing_fixture(Path(temporary), fts5_available=True)
            )
            second = _clone_sealed_stage(
                _registry(coordinator),
                stage,
                sealed,
                label="concurrent",
            )
            entered = threading.Event()
            release = threading.Event()
            preparations: list[Any] = []

            def hold() -> None:
                with coordinator._operation_lease():
                    entered.set()
                    release.wait(timeout=2)

            def prepare_first() -> None:
                preparations.append(coordinator.activate(sealed))

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=prepare_first)
            worker.start()
            self.assertTrue(
                coordinator.wait_for_state("DRAINING", timeout_seconds=1)
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(second)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.CONCURRENT_PREPARATION",
            )
            self.assertIs(
                _registry(coordinator).state(second),
                ActivationCapabilityState.SEALED,
            )
            release.set()
            holder.join(timeout=2)
            worker.join(timeout=2)
            self.assertEqual(len(preparations), 1)
            coordinator.cancel_prepared_activation(preparations[0])

    def test_prior_database_change_during_drain_fails_post_drain_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity_value, store, coordinator, _stage, sealed = (
                _existing_fixture(root, fts5_available=True)
            )
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def hold() -> None:
                with coordinator._operation_lease():
                    entered.set()
                    release.wait(timeout=2)

            def prepare() -> None:
                try:
                    coordinator.activate(sealed)
                except BaseException as error:
                    errors.append(error)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=prepare)
            worker.start()
            self.assertTrue(
                coordinator.wait_for_state("DRAINING", timeout_seconds=1)
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_path = prior_view.stage.staged_db_path
            connection = sqlite3.connect(prior_path)
            try:
                connection.execute("PRAGMA user_version=17")
                connection.commit()
            finally:
                connection.close()
            release.set()
            holder.join(timeout=2)
            worker.join(timeout=2)
            self.assertEqual(len(errors), 1)
            error = cast(ActivationPreparationError, errors[0])
            self.assertEqual(
                error.code,
                "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertIs(
                _registry(coordinator)._entries[
                    sealed.artifact.artifact_id
                ].state,
                ActivationCapabilityState.CANCELLED,
            )
            self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")
            self.assertFalse(any("recovery" in item.name for item in root.iterdir()))

    def test_held_lease_drains_then_prepares_and_rejects_new_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, store, coordinator, _stage, sealed = _existing_fixture(
                Path(temporary),
                fts5_available=True,
            )
            entered = threading.Event()
            release = threading.Event()
            preparation: list[Any] = []
            errors: list[BaseException] = []

            def hold() -> None:
                with coordinator._operation_lease():
                    entered.set()
                    release.wait(timeout=2)

            def prepare() -> None:
                try:
                    preparation.append(coordinator.activate(sealed))
                except BaseException as error:
                    errors.append(error)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=prepare)
            worker.start()
            self.assertTrue(coordinator.wait_for_state("DRAINING", timeout_seconds=1))
            with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                _ = store.exact_records("prior")
            self.assertEqual(raised.exception.code, "STORE.RESOURCE_DRAINING")
            release.set()
            holder.join(timeout=2)
            worker.join(timeout=2)
            self.assertEqual(errors, [])
            self.assertEqual(len(preparation), 1)
            self.assertEqual(coordinator.state, "ACTIVATING")
            coordinator.cancel_prepared_activation(preparation[0])

    def test_drain_timeout_restores_ready_and_cancels_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, store, coordinator, _stage, sealed = _existing_fixture(
                Path(temporary),
                fts5_available=True,
                timeout_seconds=0.01,
            )
            entered = threading.Event()
            release = threading.Event()

            def hold() -> None:
                with coordinator._operation_lease():
                    entered.set()
                    release.wait(timeout=2)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(raised.exception.code, "ACTIVATION.DRAIN_TIMEOUT")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertIs(
                _registry(coordinator)._entries[
                    sealed.artifact.artifact_id
                ].state,
                ActivationCapabilityState.CANCELLED,
            )
            self.assertFalse(any("recovery" in item.name for item in Path(temporary).iterdir()))
            release.set()
            holder.join(timeout=2)
            self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")

    def test_backup_failures_restore_ready_without_partial_authority(self) -> None:
        for seam in (
            "_open_recovery_backup",
            "_write_recovery_backup",
            "_fsync_recovery_backup",
            "_fsync_recovery_directory",
        ):
            with self.subTest(seam=seam):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, store, coordinator, _stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                    )
                    prior_view = store.coordinator._view
                    assert prior_view is not None
                    prior = prior_view.stage.staged_db_path.read_bytes()
                    with patch(
                        f"tm_activation_journal.{seam}",
                        side_effect=OSError("injected"),
                    ):
                        with self.assertRaises(ActivationPreparationError) as raised:
                            coordinator.activate(sealed)
                    self.assertEqual(raised.exception.code, "ACTIVATION.BACKUP_FAILED")
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(
                        prior_view.stage.staged_db_path.read_bytes(),
                        prior,
                    )
                    self.assertTrue(identity.snapshot_manifest_path.exists())
                    self.assertFalse(any("recovery" in item.name for item in root.iterdir()))
                    self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")

    def test_post_backup_revalidation_failure_removes_complete_backup_set(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            injected = ActivationPreparationError(
                "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                retryable=False,
            )
            with patch(
                "tm_sqlite_store._revalidate_prior_assets",
                side_effect=injected,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")
            self.assertTrue(identity.snapshot_manifest_path.exists())
            self.assertFalse(any("recovery" in item.name for item in root.iterdir()))

    def test_cancel_cleanup_failure_retains_preparation_for_retry(self) -> None:
        for seam in (
            "_unlink_recovery_backup",
            "_fsync_recovery_deletion_directory",
        ):
            with self.subTest(seam=seam):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _identity_value, store, coordinator, _stage, sealed = (
                        _existing_fixture(root, fts5_available=True)
                    )
                    prepared = coordinator.activate(sealed)

                    with patch(
                        f"tm_activation_journal.{seam}",
                        side_effect=OSError("injected"),
                    ):
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as raised:
                            coordinator.cancel_prepared_activation(prepared)
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as repeated:
                            coordinator.cancel_prepared_activation(prepared)
                        self.assertEqual(
                            repeated.exception.code,
                            "ACTIVATION.CLEANUP_FAILED",
                        )
                        self.assertEqual(coordinator.state, "ACTIVATING")
                        self.assertIs(coordinator._preparation, prepared)

                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.CLEANUP_FAILED",
                    )
                    self.assertTrue(raised.exception.retryable)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertIs(coordinator._preparation, prepared)
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.TOKEN_ISSUED,
                    )
                    with self.assertRaises(SQLiteStoreLifecycleError):
                        store.exact_records("prior")
                    rendered = f"{raised.exception!r} {raised.exception!s}"
                    for forbidden in (str(root), "prior", "canonical"):
                        self.assertNotIn(forbidden, rendered)

                    coordinator.cancel_prepared_activation(prepared)

                    self.assertEqual(coordinator.state, "READY")
                    self.assertIsNone(coordinator._preparation)
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.CANCELLED,
                    )
                    self.assertFalse(
                        any("recovery" in item.name for item in root.iterdir())
                    )
                    self.assertEqual(
                        store.exact_records("prior")[0].target_raw,
                        "canonical",
                    )

    def test_failed_prepare_cleanup_failure_retains_internal_retry(
        self,
    ) -> None:
        for seam in (
            "_unlink_recovery_backup",
            "_fsync_recovery_deletion_directory",
        ):
            with self.subTest(seam=seam):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _identity_value, store, coordinator, _stage, sealed = (
                        _existing_fixture(root, fts5_available=True)
                    )
                    validation_failure = ActivationPreparationError(
                        "ACTIVATION.POST_DRAIN_VALIDATION_FAILED",
                        retryable=False,
                    )
                    with patch(
                        "tm_sqlite_store._revalidate_prior_assets",
                        side_effect=validation_failure,
                    ), patch(
                        f"tm_activation_journal.{seam}",
                        side_effect=OSError("injected"),
                    ):
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as raised:
                            coordinator.activate(sealed)
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as repeated:
                            coordinator.retry_failed_activation_cleanup()
                        self.assertEqual(
                            repeated.exception.code,
                            "ACTIVATION.CLEANUP_FAILED",
                        )
                        self.assertEqual(coordinator.state, "ACTIVATING")
                        self.assertIsNotNone(coordinator._cleanup_reservation)

                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.CLEANUP_FAILED",
                    )
                    self.assertTrue(raised.exception.retryable)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertIsNone(coordinator._preparation)
                    self.assertIsNotNone(coordinator._cleanup_reservation)
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.TOKEN_ISSUED,
                    )
                    with self.assertRaises(SQLiteStoreLifecycleError):
                        store.exact_records("prior")
                    rendered = f"{raised.exception!r} {raised.exception!s}"
                    for forbidden in (str(root), "prior", "canonical"):
                        self.assertNotIn(forbidden, rendered)

                    coordinator.retry_failed_activation_cleanup()

                    self.assertEqual(coordinator.state, "READY")
                    self.assertIsNone(coordinator._cleanup_reservation)
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.CANCELLED,
                    )
                    self.assertFalse(
                        any("recovery" in item.name for item in root.iterdir())
                    )
                    self.assertEqual(
                        store.exact_records("prior")[0].target_raw,
                        "canonical",
                    )

    def test_preparation_and_errors_do_not_render_paths_or_tm_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            prepared = coordinator.activate(sealed)
            rendered = repr(prepared)
            for forbidden in (str(root), "same", "winner", ".sqlite3"):
                self.assertNotIn(forbidden, rendered)
            with self.assertRaises(TypeError):
                replace(prepared, preparation_id="preparation.forged")
            coordinator.cancel_prepared_activation(prepared)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PREPARATION_NOT_ACTIVE",
            )
            try:
                coordinator.activate(sealed)
            except ActivationPreparationError as error:
                rendered_error = f"{error!r} {error!s}"
            else:
                self.fail("cancelled stage unexpectedly replayed")
            for forbidden in (str(root), "same", "winner", ".sqlite3"):
                self.assertNotIn(forbidden, rendered_error)


if __name__ == "__main__":
    unittest.main()
