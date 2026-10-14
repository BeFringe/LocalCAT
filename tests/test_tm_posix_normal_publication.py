"""Core 10.5：原家族内完整正常 activation 应用发布。"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

import tm_activation_recovery as recovery
import tm_migration as migration
import tm_sqlite_store as store
from tests import test_tm_posix_builder_seal as fixtures


@unittest.skipIf(sys.platform == "win32", "POSIX 正常发布")
class PosixNormalPublicationTests(unittest.TestCase):
    setUp = fixtures.PosixBuilderSealTests.setUp

    def test_complete_initial_activation_with_real_terminal_runtime(self):
        result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationReport, result)
        self.assertEqual(result.activated_generation, 0)
        self.assertEqual(self.owner.state, "READY")
        self.assertEqual(self.owner.durable_activation_phase, "GENERATION_PUBLISHED")
        runtime = store.SQLiteTMStore.from_coordinator(self.owner)
        self.assertTrue(runtime.health().healthy)
        self.assertEqual(runtime.canonical_revision().record_count, 1)
        with runtime.query_lease() as view:
            self.assertEqual(view.exact_records("hello")[0].target_raw, "你好")

    def test_complete_existing_generation_replacement(self):
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        result = self.service.import_snapshot(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationReport, result)
        self.assertEqual(result.activated_generation, 1)
        self.assertEqual(self.owner.state, "READY")
        runtime = store.SQLiteTMStore.from_coordinator(self.owner)
        self.assertTrue(runtime.health().healthy)
        self.assertEqual(runtime.canonical_revision().generation, 1)

    def test_every_application_publication_borrows_one_original_root_and_w1(self):
        from platform_fs_contracts import BoundDirectoryAuthority
        import tm_activation_journal as journal
        roots = []
        acquisitions = []
        publications = []
        backend_type = type(self.owner._family._backend)
        real_acquire = backend_type.acquire
        real_publish = BoundDirectoryAuthority.begin_publish
        real_capture = journal._CallerHeldPosixActivationBorrow.capture
        real_phase = store.ResourceStoreCoordinator._read_durable_activation_phase

        def acquire(backend, root, *args, **kwargs):
            self.assertFalse(self.owner._condition._is_owned())
            self.assertFalse(self.owner._family._condition._is_owned())
            acquisitions.append(root)
            return real_acquire(backend, root, *args, **kwargs)

        def publish(root, candidate, destination, **kwargs):
            self.assertIs(root, self.owner._family._root)
            publications.append(destination)
            return real_publish(root, candidate, destination, **kwargs)

        def capture(borrow, path):
            roots.append(borrow.root)
            self.assertIs(borrow.root, self.owner._family._root)
            self.assertNotEqual(borrow._sql_path, path)
            return real_capture(borrow, path)

        def phase(owner, **kwargs):
            self.assertIsNotNone(kwargs.get("caller_borrow"))
            return real_phase(owner, **kwargs)

        with patch.object(backend_type, "acquire", acquire), patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(journal._CallerHeldPosixActivationBorrow, "capture", capture), patch.object(store.ResourceStoreCoordinator, "_read_durable_activation_phase", phase), patch.object(recovery, "_replace_activation_file", side_effect=AssertionError("bare publication")), patch.object(journal, "_unlink_recovery_backup", side_effect=AssertionError("bare backup cleanup")):
            first = self.service.activate_initial(self.source, self.identity.resource_id)
            self.assertIs(type(first), migration.MigrationReport, first)
            for generation in (1, 2):
                result = self.service.import_snapshot(self.source, self.identity.resource_id)
                self.assertIs(type(result), migration.MigrationReport, result)
                self.assertEqual(result.activated_generation, generation)
        self.assertEqual(len(acquisitions), 3)
        self.assertGreater(len(roots), 20)
        self.assertIn(self.identity.snapshot_manifest_path.name, publications)
        self.assertIn(journal._activation_lineage_marker_path(self.identity).name, publications)
        self.assertIn(journal._activation_terminal_path(self.identity).name, publications)
        self.assertTrue(any(name.endswith(".bak") for name in publications))
        self.assertEqual(self.owner._active_lease_count, 0)
        self.assertEqual(self.owner._family._active_count, 0)

    def _payload(self):
        payload = self.parent / "payload.jsonl"
        payload.write_text('{"source":"second","target":"第二"}\n', encoding="utf-8")
        return payload, migration.hashlib.sha256(payload.read_bytes()).hexdigest()

    def test_payload_source_pending_survives_receipt_and_real_owner_callback(self):
        from platform_fs_contracts import BoundDirectoryAuthority, PendingPublication
        payload, digest = self._payload()
        self.source.unlink()
        events = []
        pending_names = {}
        real_publish = BoundDirectoryAuthority.begin_publish
        real_terminal = PendingPublication.terminal_reproof
        real_close = PendingPublication.close
        real_receipt = recovery._publish_activation_receipt

        def publish(parent, candidate, destination, **kwargs):
            value = real_publish(parent, candidate, destination, **kwargs)
            pending_names[value] = destination
            return value

        def terminal(value):
            name = pending_names.get(value)
            if name == self.source.name:
                events.append("source-terminal")
            if name == self.identity.snapshot_manifest_path.name:
                self.assertEqual(self.owner._read_durable_activation_phase(caller_borrow=self.service._active_owner_reservation._activation_borrows[-1]), "MANIFEST_PUBLISHED")
                events.append("manifest-terminal")
            return real_terminal(value)

        def close(value):
            if pending_names.get(value) == self.source.name:
                events.append("source-close")
            return real_close(value)

        def receipt(*args, **kwargs):
            self.assertNotIn("source-terminal", events)
            result = real_receipt(*args, **kwargs)
            events.append("receipt")
            return result

        def commit(report):
            self.assertNotIn("source-terminal", events)
            facts = self.service.project_applied_owner(report, expected_digest=digest, expected_record_count=1)
            self.assertEqual(facts[:3], (0, 1, 1))
            self.assertEqual(self.owner._family._state, "ACTIVATING")
            events.append("callback")

        with patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(PendingPublication, "terminal_reproof", terminal), patch.object(PendingPublication, "close", close), patch.object(recovery, "_publish_activation_receipt", receipt):
            result = self.service.apply_snapshot_payload(payload, self.identity.resource_id, expected_digest=digest, expected_record_count=1, initial=True, owner_commit=commit)
        self.assertIs(type(result), migration.MigrationReport, result)
        self.assertEqual(events, ["receipt", "manifest-terminal", "callback", "source-terminal", "source-close"])
        self.assertEqual(self.source.read_bytes(), payload.read_bytes())

    def test_payload_replacement_waits_for_query_drain_before_source_mutation(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        original = self.source.read_bytes()
        payload, digest = self._payload()
        runtime = store.SQLiteTMStore.from_coordinator(self.owner)
        started = threading.Event()
        real_operation = self.owner._family.operation
        from contextlib import contextmanager

        @contextmanager
        def operation(*args, **kwargs):
            if kwargs.get("transition"):
                started.set()
            with real_operation(*args, **kwargs) as borrow:
                yield borrow

        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(self.owner._family, "operation", operation):
                with runtime.query_lease() as view:
                    self.assertEqual(view.exact_records("hello")[0].target_raw, "你好")
                    future = executor.submit(self.service.apply_snapshot_payload, payload, self.identity.resource_id, expected_digest=digest, expected_record_count=1, initial=False)
                    self.assertTrue(started.wait(2))
                    with self.owner._family._condition:
                        self.assertTrue(self.owner._family._condition.wait_for(lambda: self.owner._family._state == "DRAINING", timeout=2))
                    self.assertEqual(self.source.read_bytes(), original)
                    self.assertFalse(future.done())
                result = future.result(timeout=8)
        self.assertIs(type(result), migration.MigrationReport, result)
        self.assertEqual(result.activated_generation, 1)
        self.assertEqual(self.source.read_bytes(), payload.read_bytes())
        self.assertTrue(runtime.health().healthy)

    def test_callback_failure_finishes_source_terminal_and_preserves_primary(self):
        from platform_fs_contracts import PendingPublication
        payload, digest = self._payload()
        self.source.unlink()
        primary = RuntimeError("registry callback primary")
        terminals = []
        real_terminal = PendingPublication.terminal_reproof
        def terminal(pending):
            if pending.preliminary_facts().content_sha256.hex() == digest:
                terminals.append(True)
            return real_terminal(pending)
        def commit(report):
            self.service.project_applied_owner(report, expected_digest=digest, expected_record_count=1)
            raise primary
        with patch.object(PendingPublication, "terminal_reproof", terminal):
            with self.assertRaises(RuntimeError) as caught:
                self.service.apply_snapshot_payload(payload, self.identity.resource_id, expected_digest=digest, expected_record_count=1, initial=True, owner_commit=commit)
        self.assertIs(caught.exception, primary)
        self.assertEqual(terminals, [True])
        self.assertEqual(self.owner.state, "READY")
        self.assertTrue(store.SQLiteTMStore.from_coordinator(self.owner).health().healthy)

    def test_observed_root_drift_before_manifest_runs_real_rejection_without_clone_writes(self):
        import shutil
        import tm_activation_journal as journal
        moved = self.parent / "original"
        before = []
        real_manifest = recovery._publish_activation_manifest
        def manifest(*args, **kwargs):
            self.root.rename(moved)
            shutil.copytree(moved, self.root)
            before.append({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
            return real_manifest(*args, **kwargs)
        with patch.object(recovery, "_publish_activation_manifest", manifest):
            result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationFailure)
        self.assertTrue(result.canonical_authority_ambiguous)
        self.assertFalse(result.canonical_authority_published)
        self.assertFalse(result.retryable)
        self.assertEqual(result.original_source_preservation.state.value, "UNVERIFIED")
        self.assertEqual(self.owner.state, "FAILED")
        self.assertEqual(before[0], {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        path = moved / journal._activation_journal_path(self.identity).name
        record = journal._parse_activation_journal_bytes(path.read_bytes(), expected_journal_path=journal._activation_journal_path(self.identity))
        self.assertEqual(record.phase.value, "DB_REPLACED")
        self.assertIsNone(self.owner.current_generation)

    def test_manifest_terminal_failure_cannot_publish_generation(self):
        from platform_fs_contracts import BoundDirectoryAuthority, PendingPublication
        import tm_activation_journal as journal
        pending_names = {}
        real_publish = BoundDirectoryAuthority.begin_publish
        real_terminal = PendingPublication.terminal_reproof
        def publish(parent, candidate, destination, **kwargs):
            value = real_publish(parent, candidate, destination, **kwargs)
            pending_names[value] = destination
            return value
        def terminal(pending):
            if pending_names.get(pending) == self.identity.snapshot_manifest_path.name:
                raise OSError("manifest terminal")
            return real_terminal(pending)
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(PendingPublication, "terminal_reproof", terminal):
            result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationFailure)
        self.assertTrue(result.canonical_authority_ambiguous)
        self.assertIsNone(self.owner.current_generation)
        record = journal._parse_activation_journal_bytes(journal._activation_journal_path(self.identity).read_bytes(), expected_journal_path=journal._activation_journal_path(self.identity))
        self.assertEqual(record.phase.value, "MANIFEST_PUBLISHED")

    def test_source_failure_cleanup_finishes_before_reservation_release(self):
        from pathlib import Path
        source: Path = getattr(self, "source")
        root: Path = getattr(self, "root")
        first = self.service.activate_initial(source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        before = (source.stat().st_ino, source.read_bytes())
        payload, digest = self._payload()
        events = []
        real_candidate = migration._bound_candidate_from_opened
        real_return = migration._return_snapshot_source
        real_release = migration._InitialActivationResourceReservation.release
        primary = OSError("source candidate failure")

        def candidate(parent, name, *args, **kwargs):
            if ".localcat-import-" in name and "recovery" not in name:
                raise primary
            return real_candidate(parent, name, *args, **kwargs)

        def returned(owner, prior, recovery_name, publication):
            result = real_return(owner, prior, recovery_name, publication)
            reservation = self.service._active_owner_reservation
            self.assertIsNotNone(reservation)
            reservation.reprove()
            self.assertFalse(reservation._lease.closed)
            self.assertEqual((source.stat().st_ino, source.read_bytes()), before)
            self.assertFalse(any("localcat-import-recovery" in p.name for p in root.iterdir()))
            self.assertFalse(owner.borrow.io_incomplete)
            events.append("handoff-close")
            return result

        def release(reservation):
            self.assertEqual(events, ["handoff-close"])
            events.append("release")
            return real_release(reservation)

        with patch.object(migration, "_bound_candidate_from_opened", candidate), patch.object(migration, "_return_snapshot_source", returned), patch.object(migration._InitialActivationResourceReservation, "release", release):
            with self.assertRaises(OSError) as caught:
                self.service.apply_snapshot_payload(payload, self.identity.resource_id, expected_digest=digest, expected_record_count=1, initial=False)
        self.assertIs(caught.exception, primary)
        self.assertEqual(events, ["handoff-close", "release"])
        self.assertEqual((source.stat().st_ino, source.read_bytes()), before)
        self.assertFalse(any("localcat-import" in p.name for p in root.iterdir()))

    def test_source_backup_prepublication_failure_cleans_owned_temporary(self):
        from platform_fs_contracts import BoundDirectoryAuthority
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        before = self.source.read_bytes()
        payload, digest = self._payload()
        primary = OSError("source backup publication")
        real_publish = BoundDirectoryAuthority.begin_publish
        def publish(parent, candidate, destination, **kwargs):
            if "localcat-import-recovery" in destination:
                raise primary
            return real_publish(parent, candidate, destination, **kwargs)
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish):
            with self.assertRaises(OSError) as caught:
                self.service.apply_snapshot_payload(payload, self.identity.resource_id, expected_digest=digest, expected_record_count=1, initial=False)
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(any("localcat-import" in p.name for p in self.root.iterdir()))

    def test_validation_sql_entry_failure_closes_real_guard(self):
        import tm_activation_journal as journal
        guards = []
        borrows = []
        primary = OSError("validation guard entry reproof")
        real_reprove = journal._CallerHeldPosixActivationBorrow.reprove_sql

        def reprove(borrow, path, purpose):
            if purpose == "activation-validation" and borrow._sql_guard is not None:
                guards.append(borrow._sql_guard)
                borrows.append(borrow)
                raise primary
            return real_reprove(borrow, path, purpose)

        with patch.object(journal._CallerHeldPosixActivationBorrow, "reprove_sql", reprove):
            result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationFailure)
        self.assertTrue(guards)
        self.assertTrue(all(guard.closed for guard in guards))
        self.assertTrue(all(borrow._sql_guard is None and borrow._sql_path is None for borrow in borrows))
        self.assertIsNone(self.owner._view)

    def test_validation_sql_exit_failure_keeps_body_error_and_closes_guard(self):
        from contextlib import contextmanager
        import tm_activation_journal as journal
        guards = []
        caught_errors = []
        primary = TypeError("validation body")
        cleanup = OSError("validation exit reproof")
        real_open = store._CoordinatorStorePort.open_configured_connection

        @contextmanager
        def configured(port, path, **kwargs):
            closing_patch = None
            try:
                with real_open(port, path, **kwargs) as connection:
                    if port.receipt_handoff is None and port.posix_activation_borrow is not None:
                        guards.append(port.posix_activation_borrow._sql_guard)
                        closing_patch = patch.object(journal._CallerHeldPosixActivationBorrow, "reprove_sql", side_effect=cleanup)
                        closing_patch.start()
                        raise primary
                    yield connection
            except BaseException as error:
                caught_errors.append(error)
                raise
            finally:
                if closing_patch is not None:
                    closing_patch.stop()

        with patch.object(store._CoordinatorStorePort, "open_configured_connection", configured):
            result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), migration.MigrationFailure)
        self.assertEqual(caught_errors, [primary])
        self.assertTrue(guards)
        self.assertTrue(all(guard.closed for guard in guards))

    def _source_close_failure_matrix(self, replacement):
        for has_primary in (False, True):
            for close_first in (False, True):
                with self.subTest(replacement=replacement, has_primary=has_primary, close_first=close_first):
                    case = PosixNormalPublicationTests()
                    case.setUp()
                    attempts, authorities, readers, events = [], [], [], []
                    primary = TypeError("body after actual builder")
                    secondary = OSError("source close")
                    tertiary = OSError("attempt cleanup after actual close")
                    real_build = migration.TMMigrationService._build_stage
                    real_source_close = migration._PortableRootedRead.close
                    real_attempt_close = migration._close_initial_stage_attempt_authorities
                    real_release = migration._InitialActivationResourceReservation.release

                    def build(service, *args, **kwargs):
                        result = real_build(service, *args, **kwargs)
                        attempt = kwargs["initial_attempt"]
                        attempts.append(attempt)
                        authorities.extend((attempt.stage_reservation, attempt.manifest_reservation))
                        if has_primary:
                            raise primary
                        return result

                    def close(reader):
                        readers.append(reader)
                        events.append("source-close")
                        if close_first:
                            real_source_close(reader)
                        raise secondary

                    def close_attempt(attempt):
                        self.assertIsNotNone(case.owner._family._root)
                        reservation = active_reservations[-1]
                        reservation.reprove()
                        self.assertFalse(reservation._lease.closed)
                        events.append("attempt-close")
                        real_attempt_close(attempt)
                        raise tertiary

                    active_reservations = []
                    real_acquire = migration.TMMigrationService._acquire_initial_reservation
                    def acquire(service, **kwargs):
                        reservation = real_acquire(service, **kwargs)
                        active_reservations.append(reservation)
                        return reservation

                    def release(reservation):
                        events.append("release")
                        self.assertTrue(all(reader.authority.closed for reader in readers))
                        self.assertTrue(all(authority.closed for authority in authorities))
                        return real_release(reservation)

                    try:
                        if replacement:
                            first = case.service.activate_initial(case.source, case.identity.resource_id)
                            self.assertIs(type(first), migration.MigrationReport)
                        caught = None
                        with patch.object(migration.TMMigrationService, "_build_stage", build), patch.object(migration._PortableRootedRead, "close", close), patch.object(migration, "_close_initial_stage_attempt_authorities", close_attempt), patch.object(migration.TMMigrationService, "_acquire_initial_reservation", acquire), patch.object(migration._InitialActivationResourceReservation, "release", release):
                            try:
                                method = case.service.import_snapshot if replacement else case.service.activate_initial
                                method(case.source, case.identity.resource_id)
                            except BaseException as error:
                                caught = error
                        self.assertIs(caught, primary if has_primary else secondary)
                        self.assertEqual(events, ["source-close", "attempt-close", "release"])
                        self.assertEqual(case.owner._family._active_count, 0)
                        self.assertTrue(all(reservation._initial_attempt is None for reservation in active_reservations))
                    finally:
                        for reader in readers:
                            reader.authority.close()
                        for attempt in attempts:
                            real_attempt_close(attempt)
                        case.doCleanups()

    def test_initial_source_close_failure_matrix(self):
        self._source_close_failure_matrix(False)

    def test_replacement_source_close_failure_matrix(self):
        self._source_close_failure_matrix(True)

    def test_backup_prepublication_failure_matrix_cleans_live_owned_temp(self):
        from platform_fs_contracts import BoundDirectoryAuthority, CandidateFile
        import tm_activation_journal as journal
        for stage in ("write", "flush", "publish"):
            with self.subTest(stage=stage):
                case = PosixNormalPublicationTests()
                case.setUp()
                candidates, removals, borrows = [], [], []
                primary = OSError("backup " + stage)
                real_create = BoundDirectoryAuthority.create_candidate
                real_write = CandidateFile.write_chunks
                real_flush = CandidateFile.flush_content
                real_publish = BoundDirectoryAuthority.begin_publish
                real_unlink = BoundDirectoryAuthority.unlink_owned
                real_backup = journal._create_recovery_backup

                def create(root, name, **kwargs):
                    result = real_create(root, name, **kwargs)
                    if ".localcat-recovery." in name:
                        candidates.append(result)
                    return result

                def write(candidate, *args, **kwargs):
                    result = real_write(candidate, *args, **kwargs)
                    if candidate in candidates and stage == "write":
                        raise primary
                    return result

                def flush(candidate):
                    if candidate in candidates and stage == "flush":
                        raise primary
                    return real_flush(candidate)

                def publish(root, candidate, destination, **kwargs):
                    if candidate in candidates and stage == "publish":
                        raise primary
                    return real_publish(root, candidate, destination, **kwargs)

                def unlink(root, name, identity):
                    if ".localcat-recovery." in name:
                        self.assertIs(root, case.owner._family._root)
                        borrow = borrows[-1]
                        borrow.reprove()
                        self.assertFalse(borrow.lease.closed)
                        removals.append(name)
                    return real_unlink(root, name, identity)

                def backup(*args, **kwargs):
                    borrows.append(kwargs["caller_borrow"])
                    return real_backup(*args, **kwargs)

                try:
                    first = case.service.activate_initial(case.source, case.identity.resource_id)
                    self.assertIs(type(first), migration.MigrationReport)
                    before = set(case.root.iterdir())
                    with patch.object(BoundDirectoryAuthority, "create_candidate", create), patch.object(CandidateFile, "write_chunks", write), patch.object(CandidateFile, "flush_content", flush), patch.object(BoundDirectoryAuthority, "begin_publish", publish), patch.object(BoundDirectoryAuthority, "unlink_owned", unlink), patch.object(journal, "_create_recovery_backup", backup):
                        outcome = case.service.import_snapshot(case.source, case.identity.resource_id)
                    self.assertIs(type(outcome), migration.MigrationFailure)
                    self.assertEqual(outcome.error_code, "ACTIVATION.BACKUP_FAILED")
                    self.assertTrue(outcome.retryable)
                    self.assertEqual(outcome.recovery_locators, ())
                    self.assertEqual(case.owner.state, "READY")
                    self.assertTrue(candidates)
                    self.assertTrue(all(candidate.closed for candidate in candidates))
                    self.assertEqual(len(removals), 1)
                    self.assertEqual([p.name for p in set(case.root.iterdir()) - before if ".localcat-recovery." in p.name], [])
                finally:
                    case.doCleanups()

    def test_backup_unknown_naming_retains_cleanup_owner_and_final(self):
        from platform_fs_contracts import BoundDirectoryAuthority
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        named = []
        real_publish = BoundDirectoryAuthority.begin_publish
        def publish(root, candidate, destination, **kwargs):
            pending = real_publish(root, candidate, destination, **kwargs)
            if ".localcat-recovery." in destination:
                named.append(destination)
                pending.close()
                raise OSError("unknown backup result after actual naming")
            return pending
        with patch.object(BoundDirectoryAuthority, "begin_publish", publish):
            outcome = self.service.import_snapshot(self.source, self.identity.resource_id)
        self.assertIs(type(outcome), migration.MigrationFailure)
        self.assertEqual(outcome.error_code, "ACTIVATION.CLEANUP_FAILED")
        self.assertEqual(self.owner.state, "ACTIVATING")
        self.assertIsNotNone(self.owner._cleanup_reservation)
        self.assertTrue(named)
        self.assertTrue(all((self.root / name).is_file() for name in named))
        self.assertTrue(any(owned.path.name in named and owned.identity is None and owned.live_identity is None for owned in self.owner._cleanup_reservation.owned_paths))
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            store.SQLiteTMStore.from_coordinator(self.owner).canonical_revision()

    def test_backup_cleanup_refuses_replaced_temporary(self):
        from platform_fs_contracts import BoundDirectoryAuthority, CandidateFile
        first = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(first), migration.MigrationReport)
        candidates = {}
        real_create = BoundDirectoryAuthority.create_candidate
        real_write = CandidateFile.write_chunks
        def create(root, name, **kwargs):
            value = real_create(root, name, **kwargs)
            if ".localcat-recovery." in name:
                candidates[value] = self.root / name
            return value
        def write(candidate, *args, **kwargs):
            if candidate in candidates:
                path = candidates[candidate]
                path.unlink()
                path.write_bytes(b"foreign backup temporary")
                raise OSError("backup write failed after observed replacement")
            return real_write(candidate, *args, **kwargs)
        with patch.object(BoundDirectoryAuthority, "create_candidate", create), patch.object(CandidateFile, "write_chunks", write):
            outcome = self.service.import_snapshot(self.source, self.identity.resource_id)
        self.assertIs(type(outcome), migration.MigrationFailure)
        self.assertEqual(outcome.error_code, "ACTIVATION.CLEANUP_FAILED")
        self.assertEqual(self.owner.state, "ACTIVATING")
        self.assertIsNotNone(self.owner._cleanup_reservation)
        self.assertTrue(candidates)
        self.assertTrue(all(path.read_bytes() == b"foreign backup temporary" for path in candidates.values()))
