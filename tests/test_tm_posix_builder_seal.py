"""Task 10.3：真实初始 builder/seal 的原家族借用。"""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_migration as migration
import tm_sqlite_store as store
import tm_stage_sealer as sealer
import tm_gate_b as gate_b
from tm_contracts import CanonicalResourceIdentity
from tm_content_attestation import SealedContentAttestation, _RecoveryProfile
from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode, LockPolicy, LockWait


@unittest.skipIf(sys.platform == "win32", "POSIX 原家族")
class PosixBuilderSealTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name).resolve()
        self.root = self.parent / "resource"
        self.root.mkdir()
        self.source = self.root / "memory.jsonl"
        self.source.write_text('{"source":"hello","target":"你好"}\n', encoding="utf-8")
        self.identity = CanonicalResourceIdentity.from_configured_jsonl("tm.builder", self.source)
        self.owner = store.ResourceStoreCoordinator(resource_identity=self.identity, canonical_store_id="store.builder")
        self.addCleanup(self.owner.close_family_session)
        self.service = migration.TMMigrationService(resource_identity=self.identity, canonical_store_id="store.builder", coordinator=self.owner)

    @contextmanager
    def attempt(self):
        with self.service._initial_stage_reservation() as reservation:
            preflight = self.service.preflight(self.source)
            salt = "initial-test"
            stage = migration._deterministic_stage_ref(self.identity, source_digest=preflight.source_digest, stage_prefix="migration", path_salt=salt)
            attempt = migration._freeze_initial_stage_attempt(stage, path_salt=salt, resource_reservation=reservation)
            try:
                yield reservation, preflight, attempt, salt
            finally:
                migration._close_initial_stage_attempt_authorities(attempt)

    def build(self, preflight, attempt, salt):
        return self.service._build_stage(self.source, preflight=preflight, canonical_store_id="store.builder", immediate_seal=True, batch_kind="migration", batch_prefix="migration", snapshot_prefix="snapshot.migration", stage_prefix="migration", path_salt=salt, initial_attempt=attempt)

    def test_real_builder_and_seal_borrow_every_connection_then_same_inode_handoff(self):
        connections = []
        purposes = []
        original_connect = sqlite3.connect
        original_open = store._open_configured_connection
        original_capture = sealer._capture_posix_bound_content
        original_enter = sealer._CallerHeldSealBorrow.enter_sql
        capture_count = []
        seal_borrows = []

        def capture(platform, opened):
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            capture_count.append(True)
            return original_capture(platform, opened)

        def enter(borrow, path, purpose):
            self.assertIs(borrow, attempt.stage_borrow)
            seal_borrows.append(purpose)
            return original_enter(borrow, path, purpose)

        def connect(*args, **kwargs):
            opened = original_connect(*args, **kwargs)
            connections.append(opened)
            self.assertIs(type(opened), sqlite3.Connection)
            return opened

        @contextmanager
        def open_connection(path, **kwargs):
            self.assertIs(kwargs.get("_stage_borrow"), attempt.stage_borrow)
            purposes.append(kwargs.get("purpose"))
            with original_open(path, **kwargs) as connection:
                yield connection

        with self.attempt() as (reservation, preflight, attempt, salt):
            self.assertIs(reservation._root, self.owner._family._root)
            self.assertEqual(self.owner._family._state, "ACTIVATING")
            with patch.object(store, "_open_configured_connection", open_connection), patch.object(sqlite3, "connect", connect), patch.object(sealer, "_capture_posix_bound_content", capture), patch.object(sealer._CallerHeldSealBorrow, "enter_sql", enter):
                self.build(preflight, attempt, salt)
                sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            self.assertGreaterEqual(len(capture_count), 8)
            self.assertGreaterEqual(seal_borrows.count("seal"), 3)
            self.assertGreaterEqual(len(purposes), 6)
            self.assertIn("stage-schema", purposes)
            self.assertIn("stage-inspect", purposes)
            self.assertIn("stage-append", purposes)
            self.assertIn("stage-revision", purposes)
            self.assertIn("stage-issued-receipt", purposes)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            entry = self.owner._sealed_registry._entries[sealed.artifact.artifact_id]
            self.assertIs(type(entry.sealed_content_attestation), SealedContentAttestation)
            proof = entry.sealed_content_attestation.database
            self.assertEqual(proof.inode, attempt.stage.staged_db_path.stat().st_ino)
            before = attempt.stage_reservation.identity()
            candidate = reservation.handoff_stage_candidate(attempt, proof)
            self.addCleanup(candidate.close)
            self.assertEqual(candidate.identity(), before)
            self.assertIsNone(attempt.stage_reservation)

    def test_real_builder_rejects_replaced_parent_before_connect(self):
        with self.assertRaises(store.SQLiteStoreLifecycleError), self.attempt() as (reservation, preflight, attempt, salt):
            moved = self.parent / "moved"
            self.root.rename(moved)
            shutil.copytree(moved, self.root)
            clone_before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
            entered = []
            real_build = self.service._build_stage
            def actual_builder(*args, **kwargs):
                entered.append(True)
                return real_build(*args, **kwargs)
            with patch.object(self.service, "_build_stage", actual_builder), patch.object(sqlite3, "connect", side_effect=AssertionError("clone SQL access")):
                with self.assertRaises((migration.MigrationPreflightError, store.SQLiteStoreLifecycleError, sealer.StageSealError)):
                    self.build(preflight, attempt, salt)
            self.assertEqual(entered, [True])
            self.assertEqual(self.owner._family._state, "FAILED")
            self.assertEqual(clone_before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
            # Restore names only for this test's context terminal; original
            # family failure remains latched and must still reject.
            shutil.rmtree(self.root)
            moved.rename(self.root)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                with self.owner._family_operation():
                    self.fail("已失效的原 owner 不可重新接纳同路径")

    def test_same_parent_wrong_resource_borrow_rejected(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            wrong = CanonicalResourceIdentity.from_configured_jsonl("tm.other", self.root / "other.jsonl")
            bad = migration._deterministic_stage_ref(wrong, source_digest=preflight.source_digest, stage_prefix="migration", path_salt=salt)
            with self.assertRaises(sealer.StageSealError):
                attempt.stage_borrow.reprove(reservation._backend, bad)
            with self.assertRaises(sealer.StageSealError):
                sealer._create_caller_held_seal_borrow(identity=self.identity, platform=reservation._backend, root=reservation._root, lease=reservation._lease, lock_name=".wrong.lock", lock_payload=b"wrong", stage=attempt.stage, database_reservation=attempt.stage_reservation, manifest_reservation=attempt.manifest_reservation)

    def test_real_second_resource_lease_cannot_authorize_first_stage(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            wrong = CanonicalResourceIdentity.from_configured_jsonl("tm.other", self.root / "other.jsonl")
            name = f".{wrong.canonical_sidecar_path.name}.localcat-initial-activation.lock"
            payload = migration._InitialActivationResourceReservation._payload(wrong)
            other_lease = reservation._backend.acquire(reservation._root, name, payload, LockPolicy(LockWait.FAIL_FAST))
            try:
                bad = sealer._create_caller_held_seal_borrow(
                    identity=self.identity, platform=reservation._backend,
                    root=reservation._root, lease=other_lease,
                    lock_name=reservation._lock_name,
                    lock_payload=reservation._payload(self.identity), stage=attempt.stage,
                    database_reservation=attempt.stage_reservation,
                    manifest_reservation=attempt.manifest_reservation,
                )
                with patch.object(sqlite3, "connect", side_effect=AssertionError("wrong family SQL")):
                    with self.assertRaises(sealer.StageSealError):
                        with store._open_configured_connection(attempt.stage.staged_db_path, _stage_borrow=bad, purpose="stage-schema"):
                            self.fail("wrong W1 accepted")
            finally:
                other_lease.close()

    def test_initial_transition_drains_before_acquiring_w1(self):
        family = self.owner._family
        acquired = []
        entered = threading.Event()
        real_acquire = family._acquire
        def acquire(*args):
            acquired.append((family._state, family._active_count))
            return real_acquire(*args)
        def transition():
            with self.service._initial_stage_reservation() as reservation:
                self.assertIs(reservation._root, family._root)
                entered.set()
        with self.owner._family_operation():
            with patch.object(family, "_acquire", acquire):
                pool = ThreadPoolExecutor(max_workers=1)
                self.addCleanup(pool.shutdown)
                future = pool.submit(transition)
                with family._condition:
                    self.assertTrue(family._condition.wait_for(lambda: family._state == "DRAINING", 1))
                self.assertFalse(entered.is_set())
                self.assertEqual(acquired, [])
            # Keep the acquisition observer installed until the old lease exits.
            observer = patch.object(family, "_acquire", acquire)
            observer.start()
        try:
            future.result(timeout=3)
        finally:
            observer.stop()
        self.assertEqual(acquired, [("ACTIVATING", 0)])
        self.assertTrue(entered.is_set())

    @contextmanager
    def held_operation(self):
        ready = threading.Event()
        release = threading.Event()
        self.owner._drain_timeout_seconds = 0.06

        def hold():
            with self.owner._family_operation():
                ready.set()
                if not release.wait(3):
                    raise AssertionError("held operation release timed out")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hold)
            self.assertTrue(ready.wait(2))
            try:
                yield
            finally:
                release.set()
                future.result(timeout=2)

    def test_public_initial_drain_timeout_returns_retryable_preserved_failure(self):
        family = self.owner._family
        before = self.source.read_bytes()
        original_root = family._root
        original_open = family._backend.open_regular

        def source_only(root, path, *args, **kwargs):
            self.assertIs(root, original_root)
            self.assertEqual(str(path), self.source.name)
            return original_open(root, path, *args, **kwargs)

        with self.held_operation():
            with patch.object(family, "_acquire", side_effect=AssertionError("W1 before drain")), patch.object(family._backend, "open_regular", source_only), patch.object(family._backend, "bind_root", side_effect=AssertionError("owner rebind")), patch.object(migration, "ResourceStoreCoordinator", side_effect=AssertionError("fresh recovery owner")), patch.object(self.service, "_build_stage", side_effect=AssertionError("stage before drain")):
                outcome = self.service.activate_initial(self.source, self.identity.resource_id)
            self.assertIsInstance(outcome, migration.MigrationFailure)
            self.assertEqual(outcome.stage, "BUILD")
            self.assertEqual(outcome.error_code, "STORE.FAMILY_BUSY")
            self.assertTrue(outcome.retryable)
            self.assertEqual(outcome.original_source_preservation.state, migration.AssetPreservationState.VERIFIED_UNCHANGED)
            self.assertEqual(outcome.active_store_preservation.state, migration.AssetPreservationState.NOT_APPLICABLE)
            self.assertFalse(outcome.canonical_authority_ambiguous)
            self.assertEqual(outcome.recovery_locators, ())
            self.assertEqual(family._state, "READY")
            self.assertEqual(family._active_count, 1)
            self.assertEqual(len(family._admitted_threads), 1)
            self.assertFalse(self.owner._initial_activation_authority_is_unavailable())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(family._active_count, 0)
        self.assertEqual(family._admitted_threads, {})
        self.assertIs(family._root, original_root)
        with self.owner._family_operation():
            pass

    def test_initial_busy_with_durable_facts_does_not_open_database_or_claim_legacy(self):
        # Existing facts cannot be classified without W1. A metadata check is
        # sufficient to refuse legacy-safe projection while SQL remains live.
        database = self.identity.canonical_sidecar_path
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE retained(value)")
        connection.commit()
        with self.held_operation(), patch.object(self.owner._family._backend, "open_regular", side_effect=AssertionError("raw file capture while SQL live")), patch.object(migration, "ResourceStoreCoordinator", side_effect=AssertionError("fresh recovery owner")):
            outcome = self.service.activate_initial(self.source, self.identity.resource_id)
            self.assertEqual(connection.execute("SELECT count(*) FROM retained").fetchone(), (0,))
        self.assertIsInstance(outcome, migration.MigrationFailure)
        self.assertFalse(outcome.retryable)
        self.assertTrue(outcome.canonical_authority_ambiguous)
        self.assertEqual(outcome.original_source_preservation.state, migration.AssetPreservationState.UNVERIFIED)

    def test_initial_admission_identity_failure_never_reobserves_clone(self):
        original_digest = migration._try_file_digest
        moved = self.parent / "moved-admission"
        digest_calls = []

        def replace_after_digest(path):
            digest_calls.append(path)
            self.assertEqual(len(digest_calls), 1, "failure projection read clone")
            result = original_digest(path)
            self.root.rename(moved)
            shutil.copytree(moved, self.root)
            return result

        with patch.object(migration, "_try_file_digest", replace_after_digest), patch.object(self.service, "_build_stage", side_effect=AssertionError("stage on clone")), patch.object(migration, "ResourceStoreCoordinator", side_effect=AssertionError("owner rebind")):
            outcome = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIsInstance(outcome, migration.MigrationFailure)
        self.assertFalse(outcome.retryable)
        self.assertTrue(outcome.canonical_authority_ambiguous)
        self.assertEqual(outcome.original_source_preservation.state, migration.AssetPreservationState.UNVERIFIED)
        self.assertEqual(self.owner._family._state, "FAILED")
        shutil.rmtree(self.root)
        moved.rename(self.root)
        with self.assertRaises(migration.MigrationPreflightError) as caught:
            self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertEqual(caught.exception.error_code, "MIGRATION.ACTIVATION_NOT_READY")

    def test_admission_mapping_preserves_programmer_and_body_errors(self):
        for error in (TypeError("bad caller"), self.owner._family.error("STORE.OPERATION_BORROW_INVALID")):
            with self.subTest(error=error), patch.object(self.owner, "_family_transition", side_effect=error):
                with self.assertRaises(type(error)) as caught:
                    self.service.activate_initial(self.source, self.identity.resource_id)
                self.assertIs(caught.exception, error)
        body_error = self.owner._family.error("STORE.FAMILY_BUSY", retryable=True)
        with patch.object(self.service, "_activate_initial_with_resource_reservation", side_effect=body_error):
            with self.assertRaises(store.SQLiteStoreLifecycleError) as caught:
                self.service.activate_initial(self.source, self.identity.resource_id)
            self.assertIs(caught.exception, body_error)

    def test_apply_payload_drain_timeout_keeps_prepublication_error_contract(self):
        before = self.source.read_bytes()
        with self.held_operation(), patch.object(self.owner._family._backend, "bind_root", side_effect=AssertionError("payload opened before drain")):
            with self.assertRaises(migration.MigrationPreflightError) as caught:
                self.service.apply_snapshot_payload(self.source, self.identity.resource_id, expected_digest=migration.hashlib.sha256(before).hexdigest(), expected_record_count=1, initial=False)
        self.assertEqual(caught.exception.error_code, "IMPORT.RESOURCE_LOCK_UNAVAILABLE")
        self.assertIsInstance(caught.exception.__cause__, store.SQLiteStoreLifecycleError)
        self.assertEqual(caught.exception.__cause__.code, "STORE.FAMILY_BUSY")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.owner._family._active_count, 0)
        self.assertEqual(self.owner._family._admitted_threads, {})
        self.assertEqual(self.owner._family._state, "READY")

    def test_windows_private_capability_missing_does_not_fallback(self):
        self.service._recovery_profile = _RecoveryProfile.WINDOWS_V3
        self.service._platform_backend = compose_platform_file_backend(self.root)
        unavailable = PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False)
        with patch.object(migration, "narrow_windows_persistent_private_proof", side_effect=unavailable), patch.object(migration._InitialActivationResourceReservation, "acquire", side_effect=AssertionError("POSIX fallback")), patch.object(self.service, "_build_stage", side_effect=AssertionError("builder must not run")):
            with self.assertRaises(migration._InitialActivationReservationError):
                with self.service._initial_stage_reservation():
                    self.fail("missing private capability accepted")

    def test_real_existing_generation_replacement_reaches_borrowed_v2_seal(self):
        from tests.test_tm_activation_journal import _identity, SOURCE_BYTES
        replacement_root = self.parent / "replacement"
        replacement_root.mkdir()
        identity = _identity(replacement_root)
        identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
        owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
        self.addCleanup(owner.close_family_session)
        service = migration.TMMigrationService(resource_identity=identity,
            canonical_store_id=owner.canonical_store_id, coordinator=owner)
        initial = service.activate_initial(identity.configured_jsonl_path, identity.resource_id)
        self.assertIs(type(initial), migration.MigrationReport, initial)
        sealed = []
        class PublicationBoundary(BaseException):
            pass
        def boundary(stage, **kwargs):
            self.assertIs(kwargs["_posix_borrow"].root, owner._family._root)
            sealed.append(stage)
            entry = owner._sealed_registry._entries[stage.artifact.artifact_id]
            self.assertIs(type(entry.sealed_content_attestation), SealedContentAttestation)
            self.assertEqual(owner._family._state, "ACTIVATING")
            raise PublicationBoundary()
        # The boundary is after the real build and real seal, at 10.4's entry.
        with patch.object(owner, "activate_replacement", side_effect=boundary):
            with self.assertRaises(PublicationBoundary):
                service.import_snapshot(identity.configured_jsonl_path, identity.resource_id)
        self.assertEqual(len(sealed), 1)
        self.assertEqual(sealed[0].generation.expected_prior_generation, 0)

    def test_post_seal_failure_restores_unpublished_through_same_borrow(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            self.build(preflight, attempt, salt)
            with patch.object(self.owner._sealed_registry, "_commit_verified", side_effect=RuntimeError("injected registry rejection")):
                with self.assertRaises(RuntimeError):
                    self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            with store._open_configured_connection(attempt.stage.staged_db_path, _stage_borrow=attempt.stage_borrow, purpose="verify-rollback") as connection:
                self.assertEqual(connection.execute("SELECT value FROM tm_meta WHERE key='activation_status'").fetchone(), ("UNPUBLISHED",))
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            self.assertIsNotNone(sealed)

    def test_unknown_profile_is_rejected_before_any_stage_access(self):
        for profile in ("POSIX_V2", "WINDOWS_V3", "", 0, object()):
            with self.subTest(profile=profile), self.assertRaises(TypeError):
                sealer.StageSealer(registry=self.owner._sealed_registry,
                    canonical_store_id="store.builder", recovery_profile=profile)

    def test_handoff_requires_completed_seal_and_wrong_resource_creates_nothing(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            self.build(preflight, attempt, salt)
            proof = attempt.stage_borrow.capture_posix(attempt.stage.staged_db_path)
            with self.assertRaises(sealer.StageSealError):
                reservation.handoff_stage_candidate(attempt, proof)
            before = set(self.root.iterdir())
            wrong = CanonicalResourceIdentity.from_configured_jsonl("tm.other", self.root / "other.jsonl")
            stage = migration._deterministic_stage_ref(wrong, source_digest=preflight.source_digest, stage_prefix="migration", path_salt=salt)
            with self.assertRaises(migration.MigrationPreflightError):
                migration._freeze_initial_stage_attempt(stage, path_salt=salt, resource_reservation=reservation)
            self.assertEqual(set(self.root.iterdir()), before)

    def test_raw_capture_rejects_live_sql(self):
        with self.attempt() as (_, preflight, attempt, salt):
            self.build(preflight, attempt, salt)
            with store._open_configured_connection(attempt.stage.staged_db_path, _stage_borrow=attempt.stage_borrow, purpose="test-live-sql"):
                with self.assertRaises(sealer.StageSealError):
                    attempt.stage_borrow.capture_posix(attempt.stage.staged_db_path)

    @unittest.skipUnless(sys.platform == "darwin", "macOS 系统 /var 别名")
    def test_raw_system_alias_initial_activation_keeps_original_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(root.parts[:2], ("/", "var"))
            source = root / "memory.jsonl"
            source.write_bytes(self.source.read_bytes())
            identity = CanonicalResourceIdentity.from_configured_jsonl("tm.raw-alias", source)
            owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.raw-alias")
            try:
                assert owner._family is not None
                original_root = owner._family._root
                service = migration.TMMigrationService(resource_identity=identity, canonical_store_id=owner.canonical_store_id, coordinator=owner)
                outcome = service.activate_initial(source, identity.resource_id)
                self.assertIs(type(outcome), migration.MigrationReport, outcome)
                assert isinstance(outcome, migration.MigrationReport)
                self.assertIs(owner._family._root, original_root)
                self.assertEqual(outcome.activated_generation, 0)
            finally:
                owner.close_family_session()

    def test_gate_b_rehashes_all_three_files_through_original_borrow(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            assert reservation._backend is not None
            assert reservation._lease is not None
            assert attempt.stage_borrow is not None
            self.build(preflight, attempt, salt)
            with patch.object(reservation._backend, "bind_root", wraps=reservation._backend.bind_root) as seal_bind_root:
                sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            seal_bind_root.assert_not_called()
            registry = self.owner._sealed_registry
            entry = registry._entries[sealed.artifact.artifact_id]
            snapshot = registry.resolve_physical_readiness(sealed)
            self.assertIs(entry.caller_borrow, attempt.stage_borrow)
            self.assertIs(snapshot.caller_borrow, attempt.stage_borrow)
            self.assertTrue(entry.caller_borrow_required)
            self.assertTrue(snapshot.caller_borrow_required)
            with self.assertRaises(sealer.StageSealError):
                attempt.stage_borrow.capture_posix(self.parent / self.source.name)
            opened_names = []
            proofs = []
            lease_roots = []
            original_open = reservation._backend.open_existing_for_synchronization
            original_capture = sealer._capture_posix_bound_content
            original_reprove = reservation._lease.reprove_binding

            def open_file(root, name):
                self.assertIs(root, reservation._root)
                opened_names.append(str(name))
                return original_open(root, name)

            def capture(platform, opened):
                self.assertIs(platform, reservation._backend)
                proof = original_capture(platform, opened)
                proofs.append(proof)
                return proof

            def reprove(root, *args):
                lease_roots.append(root)
                return original_reprove(root, *args)

            with patch.object(reservation._backend, "open_existing_for_synchronization", open_file), patch.object(sealer, "_capture_posix_bound_content", capture), patch.object(reservation._lease, "reprove_binding", reprove), patch.object(reservation._backend, "bind_root", wraps=reservation._backend.bind_root) as bind_root:
                report = gate_b.GateBEvaluator(registry=self.owner._sealed_registry._readiness_view()).evaluate(sealed)
            self.assertTrue(report.granted, report)
            self.assertEqual(opened_names, [attempt.stage.staged_db_path.name, attempt.stage.manifest_temp_path.name, self.source.name])
            attestation = self.owner._sealed_registry._entries[sealed.artifact.artifact_id].sealed_content_attestation
            self.assertEqual(proofs, [attestation.database, attestation.manifest, attestation.source])
            self.assertTrue(lease_roots)
            self.assertTrue(all(root is reservation._root for root in lease_roots))
            bind_root.assert_not_called()

    def test_gate_b_rejects_root_drift_after_claim_closure_and_latches_failure(self):
        attempt_context = self.attempt()
        reservation, preflight, attempt, salt = attempt_context.__enter__()
        try:
            assert reservation._backend is not None
            assert self.owner._family is not None
            self.build(preflight, attempt, salt)
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            moved = self.parent / "gate-b-moved"
            original_claim = gate_b._require_claim_closure
            clone_before = {}

            def drift_after_claim(snapshot, attestation):
                original_claim(snapshot, attestation)
                self.root.rename(moved)
                shutil.copytree(moved, self.root)
                clone_before.update({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

            try:
                with patch.object(gate_b, "_require_claim_closure", drift_after_claim), patch.object(reservation._backend, "bind_root", wraps=reservation._backend.bind_root) as bind_root:
                    report = gate_b.GateBEvaluator(registry=self.owner._sealed_registry._readiness_view()).evaluate(sealed)
                self.assertFalse(report.granted)
                self.assertIsNone(report.grant)
                self.assertEqual(self.owner._family._state, "FAILED")
                self.assertEqual(clone_before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
                bind_root.assert_not_called()
            finally:
                if moved.exists():
                    shutil.rmtree(self.root)
                    moved.rename(self.root)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                with self.owner._family_operation():
                    self.fail("Gate B 发现的原 root 失效不可被恢复目录名称清除")
        finally:
            # Only the reservation terminal may raise the owner lifecycle error.
            # An error from evaluate must escape instead of skipping its assertions.
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                _ = attempt_context.__exit__(None, None, None)

    def test_gate_b_rejects_released_and_cross_thread_borrow(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            assert attempt.stage_borrow is not None
            self.build(preflight, attempt, salt)
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            evaluator = gate_b.GateBEvaluator(registry=self.owner._sealed_registry._readiness_view())
            with ThreadPoolExecutor(max_workers=1) as pool:
                report = pool.submit(evaluator.evaluate, sealed).result(timeout=3)
            self.assertFalse(report.granted, report)
            self.assertIsNone(report.grant)
            attempt.stage_borrow._release()
            report = evaluator.evaluate(sealed)
            self.assertFalse(report.granted, report)
            self.assertIsNone(report.grant)

    def test_gate_b_missing_source_returns_denial_after_claim_closure(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            self.build(preflight, attempt, salt)
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            original_claim = gate_b._require_claim_closure
            source_bytes = self.source.read_bytes()

            def remove_source(snapshot, attestation):
                original_claim(snapshot, attestation)
                self.source.unlink()

            try:
                with patch.object(gate_b, "_require_claim_closure", remove_source):
                    report = gate_b.GateBEvaluator(registry=self.owner._sealed_registry._readiness_view()).evaluate(sealed)
                self.assertFalse(report.granted, report)
                self.assertIsNone(report.grant)
                self.assertEqual(report.error_code, "GATE_B.SOURCE_MISMATCH")
            finally:
                self.source.write_bytes(source_bytes)

    def test_gate_b_required_borrow_missing_or_wrong_stage_never_falls_back(self):
        with self.attempt() as (reservation, preflight, attempt, salt):
            assert reservation._backend is not None
            assert reservation._root is not None
            assert reservation._lease is not None
            self.build(preflight, attempt, salt)
            sealed = self.owner._seal_stage(attempt.stage, canonical_store_id="store.builder", expected_prior_generation=None, **reservation.stage_seal_inputs(attempt))
            registry = self.owner._sealed_registry
            entry = registry._entries[sealed.artifact.artifact_id]
            wrong_stage = replace(attempt.stage, stage_id="another-operation")
            wrong_borrow = sealer._create_caller_held_seal_borrow(
                identity=self.identity, platform=reservation._backend, root=reservation._root,
                lease=reservation._lease, lock_name=reservation._lock_name,
                lock_payload=reservation._payload(self.identity), stage=wrong_stage,
                database_reservation=attempt.stage_reservation, manifest_reservation=attempt.manifest_reservation,
            )
            try:
                for borrow in (None, wrong_borrow):
                    with self.subTest(borrow_missing=borrow is None):
                        registry._entries[sealed.artifact.artifact_id] = replace(entry, caller_borrow=borrow)
                        report = gate_b.GateBEvaluator(registry=registry._readiness_view()).evaluate(sealed)
                        self.assertFalse(report.granted, report)
                        self.assertIsNone(report.grant)
            finally:
                registry._entries[sealed.artifact.artifact_id] = entry
                wrong_borrow._release()


if __name__ == "__main__":
    unittest.main()
