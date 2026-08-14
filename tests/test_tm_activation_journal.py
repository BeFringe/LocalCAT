"""Task 5.6 durable activation journal tests.

The suite covers the PREPARED publication, the strict monotonic
PREPARED -> DB_REPLACED -> MANIFEST_PUBLISHED -> GENERATION_PUBLISHED
transitions, canonical on-disk encoding, closure revalidation, file
safety, fault injection, parse strictness, concurrency, and code-only
diagnostics.  Production Task 5.6 flow stops at PREPARED; transitions are
module-private primitives for Tasks 5.7-5.9 and are exercised only here.
"""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
from tm_activation_journal import _ensure_activation_lineage_marker
from tm_content_attestation import _create_active_content_attestation
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
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteTMStore,
    _ActivationJournalHandle,
    _ActivationJournalPhase,
    _ActivationJournalRecord,
    _ActivationPreparation,
    _SQLiteGenerationView,
    _activation_journal_path,
    _read_activation_journal_file,
    _activation_journal_temp_path,
    _fsync_activation_directory,
    _parse_activation_journal_bytes,
    _serialize_activation_journal_record,
    initialize_stage_schema,
)
from tm_stage_sealer import SealedArtifactRegistry, StageSealer


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)

DB_REPLACED = _ActivationJournalPhase.DB_REPLACED
MANIFEST_PUBLISHED = _ActivationJournalPhase.MANIFEST_PUBLISHED
GENERATION_PUBLISHED = _ActivationJournalPhase.GENERATION_PUBLISHED


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
    return cast(SealedArtifactRegistry, coordinator.sealed_registry)


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
        sealed = StageSealer(
            registry=coordinator.sealed_registry,
            canonical_store_id=canonical_store_id,
        ).seal(
            stage,
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
    stage, sealed = _candidate(
        coordinator,
        identity,
        canonical_store_id="store.primary",
        fts5_available=fts5_available,
        expected_prior_generation=0,
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


class _ActivationJournalHandleSubclass(_ActivationJournalHandle):
    pass


class _StringSubclass(str):
    pass


def _first_prepared(
    root: Path,
    *,
    fts5_available: bool = True,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    SealedStage,
    _ActivationPreparation,
    _ActivationJournalHandle,
]:
    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
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
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        prepared = coordinator.activate(sealed)
    handle = coordinator.publish_prepared_activation(prepared)
    return identity, coordinator, sealed, prepared, handle


def _first_activated(
    root: Path,
    *,
    fts5_available: bool = True,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    SealedStage,
    _ActivationPreparation,
]:
    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
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
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        prepared = coordinator.activate(sealed)
    return identity, coordinator, sealed, prepared


def _advance_all(
    coordinator: ResourceStoreCoordinator,
    prepared: _ActivationPreparation,
    handle: _ActivationJournalHandle,
) -> _ActivationJournalHandle:
    return coordinator._advance_activation_journal(
        prepared,
        handle,
        DB_REPLACED,
    )


def _journal_bytes(journal_path: Path) -> bytes:
    return journal_path.read_bytes()


def _overwrite_journal(journal_path: Path, payload: bytes) -> None:
    journal_path.write_bytes(payload)


class ActivationJournalHappyPathTests(unittest.TestCase):
    def test_published_phase_strictly_closes_active_attestation_facts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, _sealed, prepared, handle = _first_prepared(
                Path(temporary)
            )
            coordinator.publish_activation(prepared, handle)
            record = _parse_activation_journal_bytes(
                handle.journal_path.read_bytes(),
                expected_journal_path=handle.journal_path,
            )
            active = record.active_content_attestation
            self.assertIsNotNone(active)
            assert active is not None

            with self.assertRaises(TypeError):
                replace(record, active_content_attestation=None)

            wrong_generation = _create_active_content_attestation(
                sealed_attestation_digest=active.sealed_attestation_digest,
                journal_id=active.journal_id,
                resource_id=active.resource_id,
                target_identity=active.target_identity,
                canonical_store_id=active.canonical_store_id,
                snapshot_receipt_digest=active.snapshot_receipt_digest,
                generation=active.generation + 1,
                activation_digest=active.activation_digest,
                database=active.database,
                manifest=active.manifest,
                source=active.source,
                semantic_facts=active.semantic_facts,
            )
            with self.assertRaises(ValueError):
                replace(
                    record,
                    active_content_attestation=wrong_generation,
                )

            wrong_count_facts = replace(
                active.semantic_facts,
                record_count=active.semantic_facts.record_count + 1,
            )
            wrong_count = _create_active_content_attestation(
                sealed_attestation_digest=active.sealed_attestation_digest,
                journal_id=active.journal_id,
                resource_id=active.resource_id,
                target_identity=active.target_identity,
                canonical_store_id=active.canonical_store_id,
                snapshot_receipt_digest=active.snapshot_receipt_digest,
                generation=active.generation,
                activation_digest=active.activation_digest,
                database=active.database,
                manifest=active.manifest,
                source=active.source,
                semantic_facts=wrong_count_facts,
            )
            with self.assertRaises(ValueError):
                replace(record, active_content_attestation=wrong_count)

    def test_first_activation_prepared_journal_is_canonical_and_closed(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        coordinator,
                        sealed,
                        prepared,
                        handle,
                    ) = _first_prepared(root, fts5_available=fts5_available)
                    journal_path = handle.journal_path
                    self.assertEqual(
                        journal_path,
                        _activation_journal_path(identity),
                    )
                    self.assertTrue(journal_path.is_file())
                    self.assertFalse(journal_path.is_symlink())
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
                    payload = _journal_bytes(journal_path)
                    decoded = json.loads(payload.decode("utf-8"))
                    self.assertEqual(decoded["phase"], "PREPARED")
                    self.assertEqual(
                        decoded["journal_version"],
                        "activation-journal-v2",
                    )
                    canonical = json.dumps(
                        decoded,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    self.assertEqual(
                        payload.decode("utf-8"),
                        canonical,
                    )
                    record = handle._record
                    self.assertEqual(record.phase, _ActivationJournalPhase.PREPARED)
                    self.assertEqual(
                        record.preparation_id,
                        prepared.preparation_id,
                    )
                    self.assertEqual(
                        record.token_id,
                        prepared._token.token_id,
                    )
                    self.assertEqual(
                        record.token_version,
                        prepared._token.token_version,
                    )
                    self.assertEqual(
                        record.activation_nonce,
                        prepared._token.activation_nonce,
                    )
                    self.assertEqual(
                        record.artifact_id,
                        sealed.artifact.artifact_id,
                    )
                    self.assertEqual(
                        record.artifact_seal_digest,
                        sealed.artifact.seal_digest,
                    )
                    self.assertEqual(
                        record.sealed_stage_digest,
                        sealed.sealed_stage_digest,
                    )
                    self.assertEqual(record.resource_id, identity.resource_id)
                    self.assertEqual(
                        record.target_identity,
                        identity.target_identity,
                    )
                    self.assertEqual(
                        record.canonical_store_id,
                        "store.primary",
                    )
                    self.assertIsNone(record.expected_prior_generation)
                    self.assertIsNone(record.prior_generation)
                    self.assertFalse(record.had_prior_canonical)
                    self.assertIsNone(record.prior_db_path)
                    self.assertIsNone(record.prior_manifest_path)
                    self.assertIsNone(record.prior_binding_snapshot_id)
                    self.assertIsNone(record.prior_receipt_digest)
                    self.assertIsNone(record.prior_manifest_digest)
                    self.assertIsNone(record.prior_db_identity)
                    self.assertIsNone(record.prior_db_backup_path)
                    self.assertEqual(
                        record.new_manifest_path,
                        identity.snapshot_manifest_path,
                    )
                    self.assertEqual(
                        record.new_manifest_digest,
                        record.manifest_temp_digest,
                    )
                    self.assertEqual(
                        record.source_jsonl_digest,
                        hashlib.sha256(SOURCE_BYTES).hexdigest(),
                    )
                    reparsed = _parse_activation_journal_bytes(
                        payload,
                        expected_journal_path=journal_path,
                    )
                    self.assertEqual(reparsed, record)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertIsNone(coordinator.current_generation)
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.TOKEN_ISSUED,
                    )
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(identity.snapshot_manifest_path.exists())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")

    def test_existing_canonical_prepared_journal_closes_prior_facts(
        self,
    ) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        store,
                        coordinator,
                        stage,
                        sealed,
                    ) = _existing_fixture(root, fts5_available=fts5_available)
                    prior_view = coordinator._view
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
                    handle = coordinator.publish_prepared_activation(prepared)
                    record = handle._record
                    self.assertTrue(record.had_prior_canonical)
                    self.assertEqual(record.prior_generation, 0)
                    self.assertEqual(record.expected_prior_generation, 0)
                    self.assertEqual(record.prior_db_path, prior_db_path)
                    self.assertEqual(
                        record.prior_manifest_path,
                        identity.snapshot_manifest_path,
                    )
                    self.assertEqual(
                        record.prior_binding_snapshot_id,
                        "snapshot.prior.1",
                    )
                    self.assertEqual(
                        record.prior_db_digest,
                        hashlib.sha256(prior_db).hexdigest(),
                    )
                    self.assertEqual(
                        record.prior_manifest_digest,
                        hashlib.sha256(prior_manifest).hexdigest(),
                    )
                    self.assertEqual(
                        record.prior_db_digest,
                        record.prior_db_backup_digest,
                    )
                    self.assertEqual(
                        record.prior_manifest_digest,
                        record.prior_manifest_backup_digest,
                    )
                    self.assertIsNotNone(record.prior_db_backup_path)
                    self.assertIsNotNone(record.prior_db_identity)
                    self.assertIsNotNone(record.prior_db_backup_identity)
                    backups = {
                        item.asset_kind: item for item in prepared._backup_assets
                    }
                    self.assertEqual(
                        record.prior_db_backup_path,
                        backups["DATABASE"].backup_path,
                    )
                    self.assertEqual(
                        record.prior_manifest_backup_path,
                        backups["MANIFEST"].backup_path,
                    )
                    self.assertEqual(
                        record.candidate_stage_db_path,
                        stage.staged_db_path,
                    )
                    self.assertEqual(
                        record.candidate_manifest_temp_path,
                        stage.manifest_temp_path,
                    )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(prior_db_path.read_bytes(), prior_db)
                    self.assertEqual(
                        identity.snapshot_manifest_path.read_bytes(),
                        prior_manifest,
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        source_before,
                    )
                    self.assertTrue(
                        all(
                            item.backup_path.is_file()
                            for item in prepared._backup_assets
                        )
                    )
                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")

    def test_phase_transitions_persist_each_phase_with_distinct_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            previous = handle
            digests = [handle.record_digest]
            identities = [handle.file_identity]
            for phase in (DB_REPLACED,):
                advanced = coordinator._advance_activation_journal(
                    prepared,
                    previous,
                    phase,
                )
                self.assertIs(advanced.phase, phase)
                self.assertNotEqual(
                    advanced.record_digest,
                    previous.record_digest,
                )
                self.assertNotEqual(
                    advanced.file_identity,
                    previous.file_identity,
                )
                self.assertEqual(
                    advanced.journal_id,
                    previous.journal_id,
                )
                disk_record = _parse_activation_journal_bytes(
                    _journal_bytes(journal_path),
                    expected_journal_path=journal_path,
                )
                self.assertIs(disk_record.phase, phase)
                self.assertEqual(disk_record, advanced._record)
                digests.append(advanced.record_digest)
                identities.append(advanced.file_identity)
                previous = advanced
            self.assertEqual(len(set(digests)), 2)
            self.assertEqual(len(set(identities)), 2)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator).state(sealed),
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            coordinator.cancel_prepared_activation(prepared)

    def test_prepared_replay_returns_same_journal_without_rewriting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            payload_before = _journal_bytes(journal_path)
            replayed = coordinator.publish_prepared_activation(prepared)
            self.assertEqual(replayed.journal_id, handle.journal_id)
            self.assertEqual(replayed.record_digest, handle.record_digest)
            self.assertEqual(replayed.file_identity, handle.file_identity)
            self.assertEqual(_journal_bytes(journal_path), payload_before)
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_journal_write_ordering_is_file_fsync_replace_directory_fsync(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared = _first_activated(root)
            order: list[str] = []
            real_replace = os.replace

            def recording_replace(source: Any, target: Any) -> None:
                order.append("replace")
                real_replace(source, target)

            with (
                patch(
                    "tm_activation_journal._fsync_activation_journal",
                    side_effect=lambda descriptor: order.append("file-fsync"),
                ),
                patch(
                    "tm_sqlite_store.os.replace",
                    side_effect=recording_replace,
                ),
                patch(
                    "tm_activation_journal._fsync_activation_directory",
                    side_effect=lambda path: order.append("dir-fsync"),
                ),
            ):
                journal_path = _activation_journal_path(identity)
                handle = coordinator.publish_prepared_activation(prepared)
            self.assertEqual(order, ["file-fsync", "replace", "dir-fsync"])
            self.assertTrue(handle.journal_path.is_file())
            coordinator.cancel_prepared_activation(prepared)

    def test_advance_never_touches_assets_generation_or_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            source_before = identity.configured_jsonl_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            handle = coordinator.publish_prepared_activation(prepared)
            _advance_all(coordinator, prepared, handle)
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(coordinator._view, prior_view)
            self.assertIs(
                _registry(coordinator).state(sealed),
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_db,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_before,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertTrue(
                all(
                    item.backup_path.is_file()
                    for item in prepared._backup_assets
                )
            )
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(
                tuple(store.exact_records("prior"))[0].target_raw,
                "canonical",
            )


class ActivationJournalPhaseBoundaryTests(unittest.TestCase):
    def test_phase_sequence_rejects_skip_backward_repeated_and_strings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    MANIFEST_PUBLISHED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_PHASE_SKIP",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    _ActivationJournalPhase.PREPARED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_PHASE_REPEATED",
            )
            for phase in ("DB_REPLACED", _StringSubclass("DB_REPLACED"), 7):
                with self.subTest(phase=phase):
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator._advance_activation_journal(
                            prepared,
                            handle,
                            cast(Any, phase),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_PHASE_INVALID",
                    )
            advanced = coordinator._advance_activation_journal(
                prepared,
                handle,
                DB_REPLACED,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    advanced,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_PHASE_REPEATED",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    advanced,
                    _ActivationJournalPhase.PREPARED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_PHASE_BACKWARD",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    advanced,
                    GENERATION_PUBLISHED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_PHASE_SKIP",
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_stale_handle_denies_transition_after_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            _ = coordinator._advance_activation_journal(
                prepared,
                handle,
                DB_REPLACED,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    MANIFEST_PUBLISHED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_foreign_preparation_and_handle_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            foreign_preparation = object.__new__(type(prepared))
            for item in fields(prepared):
                value = getattr(prepared, item.name)
                if item.name == "preparation_id":
                    value = "preparation.foreign"
                object.__setattr__(foreign_preparation, item.name, value)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    foreign_preparation,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_STATE_INVALID",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(
                    foreign_preparation,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_STATE_INVALID",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    cast(Any, "not-a-handle"),
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_INVALID",
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_subclass_replace_and_forged_handle_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            with self.assertRaises(TypeError):
                replace(handle, phase=DB_REPLACED)
            subclass = object.__new__(_ActivationJournalHandleSubclass)
            for item in fields(handle):
                object.__setattr__(subclass, item.name, getattr(handle, item.name))
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    subclass,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_INVALID",
            )
            forged = object.__new__(_ActivationJournalHandle)
            for item in fields(handle):
                value = getattr(handle, item.name)
                if item.name == "phase":
                    value = DB_REPLACED
                object.__setattr__(forged, item.name, value)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    forged,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            wrong_path = object.__new__(_ActivationJournalHandle)
            for item in fields(handle):
                value = getattr(handle, item.name)
                if item.name == "journal_path":
                    value = root / "foreign-journal.json"
                object.__setattr__(wrong_path, item.name, value)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    wrong_path,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_INVALID",
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_cancelled_or_consumed_token_denies_publish_and_advance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            _registry(coordinator).cancel(prepared._token)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TOKEN_INVALID",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TOKEN_INVALID",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            _registry(coordinator).consume(prepared._token)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TOKEN_INVALID",
            )

    def test_registry_entry_and_token_mutation_deny_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, sealed, prepared, handle = _first_prepared(root)
            registry = _registry(coordinator)
            entry = registry._entries[sealed.artifact.artifact_id]
            registry._entries[sealed.artifact.artifact_id] = replace(
                entry,
                state=ActivationCapabilityState.CONSUMED,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TOKEN_INVALID",
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity, coordinator, sealed, prepared, handle = _first_prepared(root)
            registry = _registry(coordinator)
            token = prepared._token
            forged_token = contract_module._create_activation_token(
                token_id="token.forged",
                stage=prepared._sealed_stage,
            )
            registry._tokens[token.token_id] = (
                sealed.artifact.artifact_id,
                forged_token,
            )
            entry = registry._entries[sealed.artifact.artifact_id]
            registry._entries[sealed.artifact.artifact_id] = replace(
                entry,
                token=forged_token,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TOKEN_INVALID",
            )

    def test_candidate_source_prior_and_backup_mutations_deny_advance(
        self,
    ) -> None:
        targets = (
            "candidate_database",
            "candidate_manifest",
            "source",
            "prior_database",
            "prior_manifest",
            "prior_database_backup",
            "prior_manifest_backup",
        )
        for target in targets:
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, _store, coordinator, stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                    )
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        prepared = coordinator.activate(sealed)
                    handle = coordinator.publish_prepared_activation(prepared)
                    backups = {
                        item.asset_kind: item
                        for item in prepared._backup_assets
                    }
                    prior_view = coordinator._view
                    assert prior_view is not None
                    path = {
                        "candidate_database": stage.staged_db_path,
                        "candidate_manifest": stage.manifest_temp_path,
                        "source": identity.configured_jsonl_path,
                        "prior_database": prior_view.stage.staged_db_path,
                        "prior_manifest": identity.snapshot_manifest_path,
                        "prior_database_backup": backups[
                            "DATABASE"
                        ].backup_path,
                        "prior_manifest_backup": backups[
                            "MANIFEST"
                        ].backup_path,
                    }[target]
                    with open(path, "ab") as stream:
                        stream.write(b"tamper")
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator._advance_activation_journal(
                            prepared,
                            handle,
                            DB_REPLACED,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    coordinator.cancel_prepared_activation(prepared)

    def test_inode_swap_of_candidate_and_source_deny_advance(self) -> None:
        for target in ("candidate_database", "candidate_manifest", "source"):
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, _store, coordinator, stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                    )
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        prepared = coordinator.activate(sealed)
                    handle = coordinator.publish_prepared_activation(prepared)
                    path = {
                        "candidate_database": stage.staged_db_path,
                        "candidate_manifest": stage.manifest_temp_path,
                        "source": identity.configured_jsonl_path,
                    }[target]
                    replacement = path.with_name(".inode-replacement")
                    shutil.copyfile(path, replacement)
                    os.replace(replacement, path)
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator._advance_activation_journal(
                            prepared,
                            handle,
                            DB_REPLACED,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_ASSET_MUTATED",
                    )
                    coordinator.cancel_prepared_activation(prepared)

    def test_generation_view_change_denies_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            handle = coordinator.publish_prepared_activation(prepared)
            view = coordinator._view
            assert view is not None
            coordinator._view = _SQLiteGenerationView(
                stage=view.stage,
                canonical_store_id=view.canonical_store_id,
                generation=7,
                fts5_available=view.fts5_available,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_CLOSURE_INVALID",
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_journal_write_failure_keeps_preparation_live_for_retry_and_cancel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            with patch(
                "tm_activation_journal._write_activation_journal_bytes",
                side_effect=OSError("injected write failure"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator).state(sealed),
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_db,
            )
            journal_path = _activation_journal_path(identity)
            self.assertFalse(journal_path.exists())
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            handle = coordinator.publish_prepared_activation(prepared)
            self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")


class ActivationJournalFileSafetyTests(unittest.TestCase):
    def test_preexisting_final_entries_are_never_overwritten(self) -> None:
        cases = (
            ("symlink", "link"),
            ("dangling", "dangling"),
            ("directory", "dir"),
            ("hardlink", "hardlink"),
            ("foreign", "foreign"),
        )
        for label, kind in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, coordinator, _sealed, prepared = _first_activated(
                        root,
                    )
                    journal_path = _activation_journal_path(identity)
                    marker = root / ".marker"
                    marker.write_text("foreign-content")
                    if kind == "link":
                        journal_path.symlink_to(marker)
                        payload_before = marker.read_bytes()
                    elif kind == "dangling":
                        journal_path.symlink_to(root / ".missing-target")
                        payload_before = b""
                    elif kind == "dir":
                        journal_path.mkdir()
                        payload_before = b""
                    elif kind == "hardlink":
                        os.link(marker, journal_path)
                        payload_before = marker.read_bytes()
                    else:
                        journal_path.write_text("foreign-content")
                        payload_before = journal_path.read_bytes()
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
                    code = raised.exception.code
                    self.assertIn(
                        code,
                        {
                            "ACTIVATION.JOURNAL_FINAL_EXISTS",
                            "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
                        },
                    )
                    if kind == "hardlink":
                        self.assertEqual(
                            code,
                            "ACTIVATION.JOURNAL_FINAL_EXISTS",
                        )
                        self.assertEqual(
                            os.lstat(journal_path).st_nlink,
                            2,
                        )
                        self.assertEqual(
                            os.lstat(marker).st_nlink,
                            2,
                        )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    if kind in {"link", "hardlink", "foreign"}:
                        self.assertEqual(
                            (marker if kind == "link" else journal_path).read_bytes(),
                            payload_before,
                        )
                    if kind == "dangling":
                        self.assertTrue(journal_path.is_symlink())
                    if kind == "dir":
                        self.assertTrue(journal_path.is_dir())
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")

    def test_preexisting_temp_entries_are_never_overwritten(self) -> None:
        cases = (
            ("symlink", "link"),
            ("dangling", "dangling"),
            ("directory", "dir"),
            ("hardlink", "hardlink"),
            ("foreign", "foreign"),
        )
        for label, kind in cases:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, coordinator, _sealed, prepared = _first_activated(
                        root,
                    )
                    journal_path = _activation_journal_path(identity)
                    temp_path = _activation_journal_temp_path(journal_path)
                    marker = root / ".marker"
                    marker.write_text("foreign-content")
                    if kind == "link":
                        temp_path.symlink_to(marker)
                        payload_before = marker.read_bytes()
                    elif kind == "dangling":
                        temp_path.symlink_to(root / ".missing-target")
                        payload_before = b""
                    elif kind == "dir":
                        temp_path.mkdir()
                        payload_before = b""
                    elif kind == "hardlink":
                        os.link(marker, temp_path)
                        payload_before = marker.read_bytes()
                    else:
                        temp_path.write_text("foreign-content")
                        payload_before = temp_path.read_bytes()
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_TEMP_EXISTS",
                    )
                    if kind == "hardlink":
                        self.assertEqual(
                            os.lstat(temp_path).st_nlink,
                            2,
                        )
                        self.assertEqual(
                            os.lstat(marker).st_nlink,
                            2,
                        )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    if kind in {"link", "hardlink", "foreign"}:
                        self.assertEqual(
                            (marker if kind == "link" else temp_path).read_bytes(),
                            payload_before,
                        )
                    if kind == "dangling":
                        self.assertTrue(temp_path.is_symlink())
                    if kind == "dir":
                        self.assertTrue(temp_path.is_dir())
                    self.assertFalse(journal_path.exists())
                    coordinator.cancel_prepared_activation(prepared)


    def test_preexisting_hardlinked_final_and_temp_are_never_authority(
        self,
    ) -> None:
        for entry in ("final", "temp"):
            with self.subTest(entry=entry):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, coordinator, _sealed, prepared = _first_activated(
                        root,
                    )
                    journal_path = _activation_journal_path(identity)
                    entry_path = (
                        journal_path
                        if entry == "final"
                        else _activation_journal_temp_path(journal_path)
                    )
                    peer = root / ".hardlink-peer"
                    peer.write_text("foreign-content")
                    os.link(peer, entry_path)
                    payload_before = peer.read_bytes()
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
                    expected = (
                        "ACTIVATION.JOURNAL_FINAL_EXISTS"
                        if entry == "final"
                        else "ACTIVATION.JOURNAL_TEMP_EXISTS"
                    )
                    self.assertEqual(raised.exception.code, expected)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertEqual(peer.read_bytes(), payload_before)
                    self.assertEqual(os.lstat(peer).st_nlink, 2)
                    self.assertEqual(os.lstat(entry_path).st_nlink, 2)
                    if entry == "temp":
                        self.assertFalse(journal_path.exists())
                    else:
                        self.assertEqual(
                            os.lstat(journal_path).st_ino,
                            os.lstat(peer).st_ino,
                        )
                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")

    def test_hardlinked_prepared_journal_denies_advance_and_republish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            payload_before = _journal_bytes(journal_path)
            peer = root / ".journal-peer"
            os.link(journal_path, peer)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_FINAL_EXISTS",
            )
            self.assertEqual(_journal_bytes(journal_path), payload_before)
            self.assertEqual(peer.read_bytes(), payload_before)
            self.assertEqual(os.lstat(journal_path).st_nlink, 2)
            self.assertEqual(coordinator.state, "ACTIVATING")
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")

    def test_hardlink_added_during_replay_and_read_windows_fails_closed(
        self,
    ) -> None:
        for window in ("replay", "advance"):
            with self.subTest(window=window):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        coordinator,
                        _sealed,
                        prepared,
                        handle,
                    ) = _first_prepared(root)
                    journal_path = handle.journal_path
                    payload_before = _journal_bytes(journal_path)
                    journal_identity = handle.file_identity
                    peer = root / ".journal-peer"
                    real_read = os.read
                    linked = False

                    def linking_read(descriptor: int, size: int) -> bytes:
                        nonlocal linked
                        if not linked:
                            observed = os.fstat(descriptor)
                            if (
                                observed.st_dev,
                                observed.st_ino,
                            ) == (
                                journal_identity.device,
                                journal_identity.inode,
                            ):
                                linked = True
                                os.link(journal_path, peer)
                        return real_read(descriptor, size)

                    with patch(
                        "tm_sqlite_store.os.read",
                        side_effect=linking_read,
                    ):
                        with self.assertRaises(ActivationPreparationError) as raised:
                            if window == "replay":
                                coordinator.publish_prepared_activation(prepared)
                            else:
                                coordinator._advance_activation_journal(
                                    prepared,
                                    handle,
                                    DB_REPLACED,
                                )
                    expected = (
                        "ACTIVATION.JOURNAL_REPLAY_MISMATCH"
                        if window == "replay"
                        else "ACTIVATION.JOURNAL_HANDLE_STALE"
                    )
                    self.assertEqual(raised.exception.code, expected)
                    self.assertTrue(linked)
                    self.assertEqual(os.lstat(journal_path).st_nlink, 2)
                    self.assertEqual(
                        _journal_bytes(journal_path),
                        payload_before,
                    )
                    self.assertEqual(peer.read_bytes(), payload_before)
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    coordinator.cancel_prepared_activation(prepared)
                    self.assertEqual(coordinator.state, "READY")


class ActivationJournalFaultInjectionTests(unittest.TestCase):
    def _assert_assets_untouched(
        self,
        coordinator: ResourceStoreCoordinator,
        identity: CanonicalResourceIdentity,
        stage: MutableStageRef,
        *,
        prior_db_bytes: bytes | None = None,
        prior_manifest_bytes: bytes | None = None,
    ) -> None:
        self.assertEqual(coordinator.state, "ACTIVATING")
        self.assertEqual(coordinator.current_generation, 0)
        prior_view = coordinator._view
        assert prior_view is not None
        if prior_db_bytes is not None:
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_db_bytes,
            )
        if prior_manifest_bytes is not None:
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest_bytes,
            )
        self.assertEqual(
            identity.configured_jsonl_path.read_bytes(),
            SOURCE_BYTES,
        )
        self.assertEqual(stage.staged_db_path.is_file(), True)
        self.assertEqual(stage.manifest_temp_path.is_file(), True)

    def test_injected_failures_at_prepared_clean_owned_temp_and_retry(
        self,
    ) -> None:
        injections = (
            (
                "_open_activation_journal_temp",
                OSError("injected create failure"),
            ),
            (
                "_write_activation_journal_bytes",
                OSError("injected write failure"),
            ),
            (
                "_fsync_activation_journal",
                OSError("injected fsync failure"),
            ),
            (
                "_close_activation_journal",
                OSError("injected close failure"),
            ),
        )
        for target, error in injections:
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, _store, coordinator, stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                    )
                    prior_view = coordinator._view
                    assert prior_view is not None
                    prior_db = prior_view.stage.staged_db_path.read_bytes()
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        prepared = coordinator.activate(sealed)
                    with patch(
                        f"tm_activation_journal.{target}",
                        side_effect=error,
                    ):
                        with self.assertRaises(ActivationPreparationError) as raised:
                            coordinator.publish_prepared_activation(prepared)
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_WRITE_FAILED",
                    )
                    journal_path = _activation_journal_path(identity)
                    self.assertFalse(journal_path.exists())
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
                    self._assert_assets_untouched(
                        coordinator,
                        identity,
                        stage,
                        prior_db_bytes=prior_db,
                        prior_manifest_bytes=(
                            identity.snapshot_manifest_path.read_bytes()
                        ),
                    )
                    self.assertIs(
                        _registry(coordinator).state(sealed),
                        ActivationCapabilityState.TOKEN_ISSUED,
                    )
                    handle = coordinator.publish_prepared_activation(prepared)
                    self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
                    coordinator.cancel_prepared_activation(prepared)

    def test_short_write_no_progress_cleans_temp_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            original_write = os.write

            def zero_write(descriptor: int, payload: Any) -> int:
                return 0

            with patch("tm_sqlite_store.os.write", side_effect=zero_write):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            journal_path = _activation_journal_path(identity)
            self.assertFalse(journal_path.exists())
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            self._assert_assets_untouched(coordinator, identity, stage)
            handle = coordinator.publish_prepared_activation(prepared)
            self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
            coordinator.cancel_prepared_activation(prepared)

    def test_replace_failure_cleans_temp_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            with patch(
                "tm_sqlite_store.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            journal_path = _activation_journal_path(identity)
            self.assertFalse(journal_path.exists())
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            self._assert_assets_untouched(
                coordinator,
                identity,
                stage,
                prior_db_bytes=prior_db,
                prior_manifest_bytes=prior_manifest,
            )
            handle = coordinator.publish_prepared_activation(prepared)
            self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
            coordinator.cancel_prepared_activation(prepared)

    def test_directory_fsync_failure_after_publish_fail_stops_with_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            with patch(
                "tm_activation_journal._fsync_activation_directory",
                side_effect=OSError("injected directory fsync failure"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            )
            self.assertFalse(raised.exception.retryable)
            journal_path = _activation_journal_path(identity)
            self.assertTrue(journal_path.is_file())
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            self._assert_assets_untouched(
                coordinator,
                identity,
                stage,
                prior_db_bytes=prior_db,
                prior_manifest_bytes=prior_manifest,
            )
            handle = coordinator.publish_prepared_activation(prepared)
            self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
            self.assertTrue(journal_path.is_file())
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")


    def test_post_replace_directory_fsync_failure_then_replay_fsyncs_and_revalidates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared = _first_activated(root)
            journal_path = _activation_journal_path(identity)
            real_fsync = _fsync_activation_directory
            calls = 0

            def one_shot_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("injected directory fsync failure")
                real_fsync(path)

            with patch(
                "tm_activation_journal._fsync_activation_directory",
                side_effect=one_shot_fsync,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
                self.assertEqual(
                    raised.exception.code,
                    "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertTrue(journal_path.is_file())
                self.assertFalse(
                    _activation_journal_temp_path(journal_path).exists()
                )
                handle = coordinator.publish_prepared_activation(prepared)
            self.assertEqual(calls, 2)
            self.assertIs(handle.phase, _ActivationJournalPhase.PREPARED)
            observed = os.lstat(journal_path)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                (handle.file_identity.device, handle.file_identity.inode),
            )
            self.assertEqual(observed.st_nlink, 1)
            self.assertEqual(coordinator.state, "ACTIVATING")
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")

    def test_replay_directory_fsync_failure_never_returns_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            payload_before = _journal_bytes(journal_path)
            with patch(
                "tm_activation_journal._fsync_activation_directory",
                side_effect=OSError("injected replay directory fsync failure"),
            ):
                for _ in range(2):
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
                    )
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    self.assertEqual(
                        _journal_bytes(journal_path),
                        payload_before,
                    )
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")

    def test_reread_failure_after_publish_fail_stops_durability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            journal_path = _activation_journal_path(identity)
            real_read = _read_activation_journal_file

            def failing_read(
                path: Path,
                expected_identity: Any,
            ) -> tuple[bytes, Any]:
                if path == journal_path:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_PARSE_INVALID",
                        retryable=False,
                    )
                return real_read(path, expected_identity)

            with patch(
                "tm_activation_journal._read_activation_journal_file",
                side_effect=failing_read,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            )
            self.assertTrue(journal_path.is_file())
            prior_view = coordinator._view
            assert prior_view is not None
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_db,
            )
            self.assertEqual(coordinator.state, "ACTIVATING")
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")

    def test_cleanup_failure_fail_stops_with_recovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_db = prior_view.stage.staged_db_path.read_bytes()
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            with patch(
                "tm_activation_journal._write_activation_journal_bytes",
                side_effect=OSError("injected write failure"),
            ):
                with patch(
                    "tm_activation_journal._remove_owned_activation_journal_temp",
                    return_value=False,
                ):
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_CLEANUP_FAILED",
            )
            journal_path = _activation_journal_path(identity)
            self.assertTrue(
                _activation_journal_temp_path(journal_path).exists()
            )
            self.assertFalse(journal_path.exists())
            self.assertEqual(coordinator.state, "ACTIVATING")
            prior_view = coordinator._view
            assert prior_view is not None
            self.assertEqual(
                prior_view.stage.staged_db_path.read_bytes(),
                prior_db,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_TEMP_EXISTS",
            )
            self.assertEqual(coordinator.state, "ACTIVATING")
            coordinator.cancel_prepared_activation(prepared)
            self.assertEqual(coordinator.state, "READY")

    def test_advance_failures_leave_phase_unchanged_and_retryable(self) -> None:
        injections = (
            (
                "_write_activation_journal_bytes",
                OSError("injected advance write failure"),
            ),
            (
                "_fsync_activation_journal",
                OSError("injected advance fsync failure"),
            ),
        )
        for target, error in injections:
            with self.subTest(target=target):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, _store, coordinator, stage, sealed = _existing_fixture(
                        root,
                        fts5_available=True,
                    )
                    prior_view = coordinator._view
                    assert prior_view is not None
                    prior_db = prior_view.stage.staged_db_path.read_bytes()
                    prior_manifest = (
                        identity.snapshot_manifest_path.read_bytes()
                    )
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        prepared = coordinator.activate(sealed)
                    handle = coordinator.publish_prepared_activation(prepared)
                    journal_path = handle.journal_path
                    with patch(
                        f"tm_activation_journal.{target}",
                        side_effect=error,
                    ):
                        with self.assertRaises(ActivationPreparationError) as raised:
                            coordinator._advance_activation_journal(
                                prepared,
                                handle,
                                DB_REPLACED,
                            )
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_WRITE_FAILED",
                    )
                    disk_record = _parse_activation_journal_bytes(
                        _journal_bytes(journal_path),
                        expected_journal_path=journal_path,
                    )
                    self.assertIs(
                        disk_record.phase,
                        _ActivationJournalPhase.PREPARED,
                    )
                    self.assertFalse(
                        _activation_journal_temp_path(journal_path).exists()
                    )
                    self._assert_assets_untouched(
                        coordinator,
                        identity,
                        stage,
                        prior_db_bytes=prior_db,
                        prior_manifest_bytes=prior_manifest,
                    )
                    advanced = coordinator._advance_activation_journal(
                        prepared,
                        handle,
                        DB_REPLACED,
                    )
                    self.assertIs(advanced.phase, DB_REPLACED)
                    coordinator.cancel_prepared_activation(prepared)

    def test_advance_replace_failure_keeps_phase_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            handle = coordinator.publish_prepared_activation(prepared)
            journal_path = handle.journal_path
            with patch(
                "tm_sqlite_store.os.replace",
                side_effect=OSError("injected advance replace failure"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator._advance_activation_journal(
                        prepared,
                        handle,
                        DB_REPLACED,
                    )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            disk_record = _parse_activation_journal_bytes(
                _journal_bytes(journal_path),
                expected_journal_path=journal_path,
            )
            self.assertIs(disk_record.phase, _ActivationJournalPhase.PREPARED)
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            self._assert_assets_untouched(coordinator, identity, stage)
            advanced = coordinator._advance_activation_journal(
                prepared,
                handle,
                DB_REPLACED,
            )
            self.assertIs(advanced.phase, DB_REPLACED)
            coordinator.cancel_prepared_activation(prepared)


class ActivationJournalParseTests(unittest.TestCase):
    def _mutated(self, payload: bytes, kind: str) -> bytes:
        decoded = json.loads(payload.decode("utf-8"))
        if kind == "byte_mutation":
            mutated = bytearray(payload)
            mutated[len(mutated) // 2] ^= 0x01
            return bytes(mutated)
        if kind == "noncanonical":
            return json.dumps(
                decoded,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        if kind == "duplicate_key":
            serialized = payload.decode("utf-8")
            marker = '"phase":"PREPARED"'
            return serialized.replace(
                marker,
                f'{marker},"phase":"PREPARED"',
                1,
            ).encode("utf-8")
        if kind == "unknown_field":
            serialized = payload.decode("utf-8")
            return (serialized[:-1] + ',"hacker":1}').encode("utf-8")
        if kind == "missing_field":
            serialized = payload.decode("utf-8")
            marker = '"phase":"PREPARED",'
            assert marker in serialized
            return serialized.replace(marker, "", 1).encode("utf-8")
        if kind == "bad_version":
            serialized = payload.decode("utf-8")
            return serialized.replace(
                '"journal_version":"activation-journal-v2"',
                '"journal_version":"activation-journal-v9"',
            ).encode("utf-8")
        if kind == "bad_digest":
            serialized = payload.decode("utf-8")
            return (serialized[:-3] + "000").encode("utf-8")
        if kind == "relative_path":
            serialized = payload.decode("utf-8")
            return serialized.replace(
                f'"journal_path":"{decoded["journal_path"]}"',
                '"journal_path":"relative/journal.json"',
            ).encode("utf-8")
        if kind == "deceptive_type":
            serialized = payload.decode("utf-8")
            return serialized.replace(
                '"had_prior_canonical":false',
                '"had_prior_canonical":0',
            ).encode("utf-8")
        raise AssertionError(kind)

    def test_mutated_journal_invalidates_handle_and_denies_transition(
        self,
    ) -> None:
        kinds = (
            "byte_mutation",
            "noncanonical",
            "duplicate_key",
            "unknown_field",
            "missing_field",
            "bad_version",
            "bad_digest",
            "relative_path",
            "deceptive_type",
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, coordinator, _sealed, prepared, handle = _first_prepared(
                        root,
                    )
                    journal_path = handle.journal_path
                    _overwrite_journal(
                        journal_path,
                        self._mutated(_journal_bytes(journal_path), kind),
                    )
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator._advance_activation_journal(
                            prepared,
                            handle,
                            DB_REPLACED,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_HANDLE_STALE",
                    )
                    with self.assertRaises(ActivationPreparationError) as raised:
                        coordinator.publish_prepared_activation(prepared)
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
                    )
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    coordinator.cancel_prepared_activation(prepared)

    def test_inode_swap_after_publication_invalidates_handle_but_replays(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            payload = _journal_bytes(journal_path)
            replacement = root / ".journal-replacement"
            replacement.write_bytes(payload)
            os.replace(replacement, journal_path)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            replayed = coordinator.publish_prepared_activation(prepared)
            self.assertEqual(replayed.record_digest, handle.record_digest)
            self.assertNotEqual(
                replayed.file_identity,
                handle.file_identity,
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_foreign_valid_journal_never_replays_or_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            foreign_record = replace(
                handle._record,
                preparation_id="preparation.foreign",
                journal_id="journal.preparation.foreign",
            )
            _overwrite_journal(
                journal_path,
                _serialize_activation_journal_record(
                    foreign_record
                ).encode("utf-8"),
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    DB_REPLACED,
                )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_REPLAY_MISMATCH",
            )
            coordinator.cancel_prepared_activation(prepared)


class ActivationJournalConcurrencyTests(unittest.TestCase):
    def test_concurrent_publish_has_one_winner_and_no_second_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            results: list[Any] = []
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def worker() -> None:
                try:
                    barrier.wait()
                    results.append(
                        coordinator.publish_prepared_activation(prepared)
                    )
                except BaseException as error:
                    errors.append(error)

            threads = [
                threading.Thread(target=worker) for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(
                {result.journal_id for result in results},
                {handle.journal_id},
            )
            self.assertEqual(
                {result.file_identity for result in results},
                {handle.file_identity},
            )
            self.assertEqual(
                _parse_activation_journal_bytes(
                    _journal_bytes(journal_path),
                    expected_journal_path=journal_path,
                ).phase,
                _ActivationJournalPhase.PREPARED,
            )
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            coordinator.cancel_prepared_activation(prepared)

    def test_concurrent_advance_has_one_winner_and_no_phase_aba(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            journal_path = handle.journal_path
            outcomes: list[Any] = []
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def worker() -> None:
                try:
                    barrier.wait()
                    outcomes.append(
                        coordinator._advance_activation_journal(
                            prepared,
                            handle,
                            DB_REPLACED,
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            threads = [
                threading.Thread(target=worker) for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ActivationPreparationError)
            loser = cast(ActivationPreparationError, errors[0])
            self.assertEqual(
                loser.code,
                "ACTIVATION.JOURNAL_HANDLE_STALE",
            )
            disk_record = _parse_activation_journal_bytes(
                _journal_bytes(journal_path),
                expected_journal_path=journal_path,
            )
            self.assertIs(disk_record.phase, DB_REPLACED)
            self.assertEqual(
                disk_record,
                outcomes[0]._record,
            )
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )
            self.assertIsNone(coordinator.current_generation)
            coordinator.cancel_prepared_activation(prepared)


class ActivationJournalDiagnosticsTests(unittest.TestCase):
    def test_handle_and_errors_do_not_render_paths_text_or_token_facts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, handle = _first_prepared(root)
            token = prepared._token
            rendered = repr(handle)
            journal_payload = _journal_bytes(handle.journal_path).decode(
                "utf-8"
            )
            for forbidden in (
                str(root),
                "same",
                "winner",
                ".sqlite3",
                token.token_id,
                token.activation_nonce,
                journal_payload,
            ):
                self.assertNotIn(forbidden, rendered)
            failures: list[ActivationPreparationError] = []
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    handle,
                    MANIFEST_PUBLISHED,
                )
            failures.append(raised.exception)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator._advance_activation_journal(
                    prepared,
                    cast(Any, "DB_REPLACED"),
                    DB_REPLACED,
                )
            failures.append(raised.exception)
            _overwrite_journal(
                handle.journal_path,
                b"{not-json",
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.publish_prepared_activation(prepared)
            failures.append(raised.exception)
            for failure in failures:
                rendered_error = f"{failure!r} {failure!s}"
                self.assertEqual(failure.__str__(), failure.code)
                for forbidden in (
                    str(root),
                    "same",
                    "winner",
                    ".sqlite3",
                    token.token_id,
                    token.activation_nonce,
                    journal_payload,
                    "{not-json",
                ):
                    self.assertNotIn(forbidden, rendered_error)
            coordinator.cancel_prepared_activation(prepared)


if __name__ == "__main__":
    unittest.main()
