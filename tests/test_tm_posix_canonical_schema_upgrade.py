"""Core 10.11：从磁盘取得旧 canonical owner 的显式升级与恢复。"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_activation_recovery as recovery
import tm_migration as migration
import tm_sqlite_store as store
from tm_contracts import SchemaUpgradeFailure, SchemaUpgradeReport
from tests.test_tm_schema_upgrade import _legacy_fixture, _service


def stopped_legacy(root: Path, *, fts: bool = False):
    identity, prior, old, _ = _legacy_fixture(root, fts5_available=fts)
    old.close_family_session()
    prior.staged_db_path.rename(identity.canonical_sidecar_path)
    return identity


@unittest.skipIf(sys.platform == "win32", "POSIX canonical predecessor")
class CanonicalSchemaUpgradeTests(unittest.TestCase):
    def fixture(self, *, fts=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        identity = stopped_legacy(Path(temporary.name).resolve(), fts=fts)
        owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
        self.addCleanup(owner.close_family_session)
        return identity, owner, _service(owner, identity)

    def test_public_canonical_upgrade_both_indexes_and_cold_reopen(self):
        for fts in (False, True):
            with self.subTest(fts=fts), patch.object(store, "_probe_fts5", return_value=fts):
                identity, owner, service = self.fixture(fts=fts)
                assert owner._family is not None
                root = owner._family._root
                result = service.upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(result), SchemaUpgradeReport, result)
                self.assertEqual(owner.current_generation, 4)
                self.assertEqual(owner.canonical_store_id, "store.primary")
                self.assertIs(owner._family._root, root)
                runtime = store.SQLiteTMStore.from_coordinator(owner)
                self.assertEqual(len(runtime.records_by_id((1, 2, 3, 4, 5))), 5)
                owner.close_family_session()
                cold = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                self.addCleanup(cold.close_family_session)
                cold.rehydrate_runtime_authority()
                self.assertEqual(cold.current_generation, 4)

    def test_copy_failure_preserves_old_canonical_records(self):
        with patch.object(store, "_probe_fts5", return_value=False):
            identity, owner, service = self.fixture()
            before = identity.canonical_sidecar_path.read_bytes()
            with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("copy failed")):
                result = service.upgrade_schema(identity.canonical_sidecar_path)
            self.assertIs(type(result), SchemaUpgradeFailure, result)
            self.assertEqual(owner.current_generation, 3)
            self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
            runtime = store.SQLiteTMStore.from_coordinator(owner)
            self.assertEqual(len(runtime.records_by_id((1, 2, 3, 4, 5))), 5)
            self.assertEqual({r.target_raw for r in runtime.exact_records("same")}, {"first", "winner"})

    def test_ordinary_runtime_rejects_v1_and_explicit_fresh_owner_reopens_data(self):
        with patch.object(store, "_probe_fts5", return_value=False):
            identity, owner, service = self.fixture()
            with self.assertRaises(Exception):
                owner.rehydrate_runtime_authority()
            self.assertIsNone(owner.current_generation)
            owner.close_family_session()
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
            self.addCleanup(fresh.close_family_session)
            with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("copy stopped")):
                result = _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
            self.assertIs(type(result), SchemaUpgradeFailure, result)
            rows = store.SQLiteTMStore.from_coordinator(fresh).records_by_id((1, 2, 3, 4, 5))
            self.assertEqual([(r.source_raw, r.target_raw) for r in rows],
                [("same", "first"), ("other", "value"), ("same", "winner"), ("alpha", "beta"), ("gamma", "delta")])

    def test_strict_legacy_proof_rejects_corrupt_semantics_identity_and_lineage(self):
        from tm_activation_journal import _activation_lineage_marker_path
        mutations = {
            "fold": "UPDATE tm_record SET source_fold_v1='forged' WHERE record_id=3",
            "gram": "DELETE FROM tm_gram WHERE record_id=3",
            "fts": "DELETE FROM tm_fts WHERE record_id=3",
            "head": "UPDATE tm_meta SET value='4' WHERE key='head_revision'",
            "identity": "UPDATE tm_meta SET value='foreign' WHERE key='resource_id'",
            "receipt": "UPDATE tm_snapshot_receipt SET record_count=5",
            "higher": "UPDATE tm_meta SET value='99' WHERE key='schema_version'",
            "unknown": "UPDATE tm_meta SET value='0' WHERE key='schema_version'",
            "marker": None,
        }
        for name, sql in mutations.items():
            with self.subTest(name=name), patch.object(store, "_probe_fts5", return_value=True):
                identity, owner, service = self.fixture(fts=True)
                if sql is None:
                    _activation_lineage_marker_path(identity).unlink()
                else:
                    with sqlite3.connect(identity.canonical_sidecar_path) as connection:
                        connection.execute(sql).close()
                    connection.close()
                before = identity.canonical_sidecar_path.read_bytes()
                with self.assertRaises(Exception):
                    service.upgrade_schema(identity.canonical_sidecar_path)
                self.assertIsNone(owner.current_generation)
                self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)

    def test_first_factory_and_later_sql_close_retain_exact_owner_until_recovery(self):
        import tm_activation_recovery as recovery
        import tm_schema_upgrade as schema
        for fail_at in (1, 5):
            with self.subTest(fail_at=fail_at), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, service = self.fixture()
                original_connect = sqlite3.connect
                original_capture = schema.SchemaUpgradeIO.capture
                connections = []
                blocked = True
                primary = OSError("exact schema SQL close failure")
                assert owner._family is not None
                original_root = owner._family._root
                class Connection(sqlite3.Connection):
                    def close(self):
                        if blocked and len(connections) >= fail_at and self is connections[fail_at - 1]:
                            raise primary
                        return super().close()
                def connect(*args, **kwargs):
                    kwargs["factory"] = Connection
                    connection = original_connect(*args, **kwargs)
                    connections.append(connection)
                    return connection
                def capture(io, path, **kwargs):
                    if blocked and len(connections) >= fail_at:
                        self.fail("raw observation after SQL close failure")
                    return original_capture(io, path, **kwargs)
                with patch.object(sqlite3, "connect", connect), patch.object(schema.SchemaUpgradeIO, "capture", capture):
                    with self.assertRaises(OSError) as caught:
                        service.upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(caught.exception, primary)
                exact = connections[fail_at - 1]
                self.assertEqual(exact.execute("SELECT 1").fetchone(), (1,))
                self.assertTrue(owner._family._pending_activation_closes)
                if fail_at == 1:
                    self.assertIsNone(owner.current_generation)
                prior_view = owner._view
                blocked = False
                with patch.object(recovery, "_discover_active_canonical", side_effect=AssertionError("generic discovery forbidden")):
                    owner.recover_durable_activation()
                self.assertIs(owner._family._root, original_root)
                if prior_view is not None:
                    self.assertIs(owner._view, prior_view)
                self.assertEqual(owner.current_generation, 3)
                self.assertEqual(owner.state, "READY")
                self.assertFalse(owner._family._pending_activation_closes)
                self.assertFalse(owner._family._recovery_required)
                with self.assertRaises(sqlite3.ProgrammingError):
                    exact.execute("SELECT 1")
                self.assertEqual(len(store.SQLiteTMStore.from_coordinator(owner).records_by_id((1, 2, 3, 4, 5))), 5)

    def test_live_legacy_owner_never_adopts_same_path_clone(self):
        import os
        for action in ("upgrade", "query"):
            with self.subTest(action=action), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, service = self.fixture()
                with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("keep prior")):
                    self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeFailure)
                original_view = owner._view
                path = identity.canonical_sidecar_path
                clone = path.with_name(path.name + ".clone")
                clone.write_bytes(path.read_bytes())
                os.replace(clone, path)
                with self.assertRaises(Exception):
                    if action == "upgrade":
                        service.upgrade_schema(path)
                    else:
                        store.SQLiteTMStore.from_coordinator(owner).exact_records("same")
                self.assertIs(owner._view, original_view)
                with self.assertRaises(Exception):
                    service.upgrade_schema(path)
                with self.assertRaises(Exception):
                    store.SQLiteTMStore.from_coordinator(owner).exact_records("same")

    def test_each_failure_phase_preserves_full_old_data_and_explicit_reopen(self):
        from tests.test_tm_schema_upgrade import _record_rows, _fail_first_journal_write
        cases = (
            (store, "_create_schema_upgrade_backup", OSError("backup failed")),
            (migration, "_migrate_schema_copy", OSError("copy failed")),
            (store.ResourceStoreCoordinator, "_seal_stage", OSError("seal failed")),
            (store, "_write_activation_journal", None),
            (recovery, "_publish_activation_manifest", OSError("manifest failed")),
        )
        for module, seam, error in cases:
            with self.subTest(seam=seam), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, service = self.fixture()
                before = identity.canonical_sidecar_path.read_bytes()
                rows = _record_rows(identity.canonical_sidecar_path)
                effect = error if error is not None else _fail_first_journal_write()
                with patch.object(module, seam, side_effect=effect):
                    result = service.upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(result), SchemaUpgradeFailure, result)
                self.assertEqual(owner.current_generation, 3)
                self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
                runtime = store.SQLiteTMStore.from_coordinator(owner)
                prior_records = runtime.records_by_id((1, 2, 3, 4, 5))
                self.assertEqual(len(prior_records), 5)
                self.assertEqual({r.target_raw for r in runtime.exact_records("same")}, {"first", "winner"})
                owner.close_family_session()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                self.addCleanup(fresh.close_family_session)
                with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("explicit reopen stops before copy")):
                    reopened = _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(reopened), SchemaUpgradeFailure, reopened)
                self.assertEqual(store.SQLiteTMStore.from_coordinator(fresh).records_by_id((1, 2, 3, 4, 5)), prior_records)
                self.assertEqual(fresh.current_generation, 3)
                self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
                result = _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(result), SchemaUpgradeReport, result)
                self.assertEqual(fresh.current_generation, 4)

    def test_real_process_exit_at_each_durable_phase_recovers_twice(self):
        import subprocess
        from tests.test_tm_schema_upgrade import _record_rows
        crash_script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
import tm_sqlite_store as store
from tests.test_tm_activation import _identity
from tests.test_tm_schema_upgrade import _service
identity = _identity(Path(sys.argv[1]))
owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
write = store._write_activation_journal
phase = sys.argv[2]
def exit_after_write(record, *args, **kwargs):
    result = write(record, *args, **kwargs)
    if record.phase.value == phase:
        os._exit(73)
    return result
with patch.object(store, "_probe_fts5", return_value=False), patch.object(store, "_write_activation_journal", exit_after_write):
    _service(owner, identity).upgrade_schema(identity.canonical_sidecar_path)
raise AssertionError("phase seam missed")
'''
        for phase in ("PREPARED", "DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"):
            with self.subTest(phase=phase), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, _ = self.fixture()
                rows = _record_rows(identity.canonical_sidecar_path)
                owner.close_family_session()
                result = subprocess.run([sys.executable, "-B", "-c", crash_script,
                    str(identity.canonical_sidecar_path.parent), phase], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 73, result.stderr)
                generations = []
                for _ in range(2):
                    fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                    self.addCleanup(fresh.close_family_session)
                    fresh.recover_durable_activation()
                    generations.append(fresh.current_generation)
                    self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
                    self.assertEqual(len(store.SQLiteTMStore.from_coordinator(fresh).records_by_id((1, 2, 3, 4, 5))), 5)
                    fresh.close_family_session()
                self.assertEqual(generations, [3, 3] if phase == "PREPARED" else [4, 4])

    def test_cancel_cleanup_failure_keeps_proven_recovery_assets(self):
        from tests.test_tm_schema_upgrade import _fail_first_journal_write, _record_rows
        with patch.object(store, "_probe_fts5", return_value=False):
            identity, owner, service = self.fixture()
            prior = identity.canonical_sidecar_path.read_bytes()
            rows = _record_rows(identity.canonical_sidecar_path)
            with patch.object(store, "_write_activation_journal", side_effect=_fail_first_journal_write()), patch.object(store, "_complete_prepared_cancellation", side_effect=OSError("cancellation cleanup blocked")):
                result = service.upgrade_schema(identity.canonical_sidecar_path)
            self.assertIs(type(result), SchemaUpgradeFailure)
            self.assertNotEqual(owner.state, "READY")
            self.assertEqual(identity.canonical_sidecar_path.read_bytes(), prior)
            assert isinstance(result, SchemaUpgradeFailure)
            self.assertTrue(result.recovery_locators)
            for locator in result.recovery_locators:
                self.assertEqual(locator.path.read_bytes(), prior)
            owner.recover_durable_activation()
            self.assertEqual(owner.state, "READY")
            self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
            self.assertEqual(len(store.SQLiteTMStore.from_coordinator(owner).records_by_id((1, 2, 3, 4, 5))), 5)

    def test_factory_business_and_close_failure_preserve_primary_and_exact_connection(self):
        with patch.object(store, "_probe_fts5", return_value=False):
            identity, owner, service = self.fixture()
            real_connect = sqlite3.connect
            primary = RuntimeError("factory meta read failed")
            closing = OSError("factory SQL close failed")
            blocked = True
            connections = []
            class Connection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if blocked and "SELECT key, value FROM tm_meta" in sql:
                        raise primary
                    return super().execute(sql, *args, **kwargs)
                def close(self):
                    if blocked:
                        raise closing
                    return super().close()
            def connect(*args, **kwargs):
                kwargs["factory"] = Connection
                connection = real_connect(*args, **kwargs)
                connections.append(connection)
                return connection
            with patch.object(sqlite3, "connect", connect):
                with self.assertRaises(RuntimeError) as caught:
                    service.upgrade_schema(identity.canonical_sidecar_path)
            self.assertIs(caught.exception, primary)
            self.assertTrue(any("factory SQL close failed" in note for note in primary.__notes__))
            self.assertEqual(len(connections), 1)
            self.assertIsNone(owner.current_generation)
            blocked = False
            owner.recover_durable_activation()
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute("SELECT 1")
            self.assertEqual(owner.current_generation, 3)

    def test_legacy_fts_query_after_failed_upgrade_and_normal_v2_write(self):
        from tests.test_tm_sqlite_store import _draft
        with patch.object(store, "_probe_fts5", return_value=True):
            identity, owner, service = self.fixture(fts=True)
            with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("keep legacy")):
                self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeFailure)
            runtime = store.SQLiteTMStore.from_coordinator(owner)
            self.assertEqual(runtime.fts5_candidate_ids('"gam"'), (5,))
            self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeReport)
            added = runtime.append(_draft("new source", "new target"))
            self.assertEqual(runtime.records_by_id((added.record_id,)), (added,))
            self.assertEqual(runtime.exact_records("new source")[0].target_raw, "new target")
            self.assertEqual(owner.current_generation, 4)

    def _retirement_unlink_failure(self, *, persistent_during_recovery=False):
        import errno
        import os
        from platform_fs_contracts import PlatformFileError
        from tests.test_tm_schema_upgrade import _fail_first_journal_write, _record_rows
        from tests.test_tm_sqlite_store import _draft
        for limit in (1, None):
            with self.subTest(limit=limit), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, service = self.fixture()
                paths = (identity.canonical_sidecar_path, identity.configured_jsonl_path,
                    identity.snapshot_manifest_path)
                before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths}
                rows = _record_rows(identity.canonical_sidecar_path)
                assert owner._family is not None
                original_root = owner._family._root
                real_unlink = os.unlink
                calls = []
                target = None
                def unlink(path, *args, **kwargs):
                    nonlocal target
                    name = str(path)
                    if target is None and name.endswith(".bak.pending"):
                        target = name
                    if name == target:
                        calls.append(name)
                        if limit is None or len(calls) <= limit:
                            raise OSError(errno.EIO, "retirement I/O unavailable", name)
                    return real_unlink(path, *args, **kwargs)
                with patch.object(store, "_write_activation_journal", side_effect=_fail_first_journal_write()), patch.object(os, "unlink", unlink):
                    try:
                        result = service.upgrade_schema(identity.canonical_sidecar_path)
                    except PlatformFileError as error:
                        self.assertEqual(error.code, "PLATFORM.FS.PUBLISH_FAILED")
                    else:
                        self.assertIs(type(result), SchemaUpgradeFailure)
                self.assertTrue(calls)
                self.assertNotEqual(owner.state, "READY")
                preparation = owner._preparation
                self.assertIsNotNone(preparation)
                self.assertTrue(owner._family._recovery_required)
                self.assertTrue(owner._family._recovery_preparations)
                self.assertEqual({p: (p.stat().st_ino, p.read_bytes()) for p in paths}, before)
                backup = next(identity.canonical_sidecar_path.parent.glob("*.database.bak"))
                self.assertEqual(backup.read_bytes(), before[identity.canonical_sidecar_path][1])
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    store.SQLiteTMStore.from_coordinator(owner).exact_records("same")
                with self.assertRaises(store.SQLiteStoreLifecycleError):
                    store.SQLiteTMStore.from_coordinator(owner).append(_draft("blocked", "blocked"))
                if persistent_during_recovery and limit is None:
                    with patch.object(os, "unlink", unlink):
                        for _ in range(2):
                            with self.assertRaises(Exception):
                                owner.recover_durable_activation()
                            self.assertIs(owner._preparation, preparation)
                            self.assertTrue(owner._family._recovery_required)
                            self.assertNotEqual(owner.state, "READY")
                            self.assertEqual(backup.read_bytes(), before[identity.canonical_sidecar_path][1])
                            self.assertEqual({p: (p.stat().st_ino, p.read_bytes()) for p in paths}, before)
                with patch.object(recovery, "_discover_active_canonical", side_effect=AssertionError("generic discovery forbidden")):
                    owner.recover_durable_activation()
                    owner.recover_durable_activation()
                self.assertIs(owner._family._root, original_root)
                self.assertEqual(owner.state, "READY")
                self.assertEqual(owner.current_generation, 3)
                self.assertFalse(owner._family._recovery_required)
                self.assertFalse(owner._family._recovery_preparations)
                self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
                runtime = store.SQLiteTMStore.from_coordinator(owner)
                records = runtime.records_by_id((1, 2, 3, 4, 5))
                self.assertEqual({r.target_raw for r in runtime.exact_records("same")}, {"first", "winner"})
                self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeReport)
                self.assertEqual(runtime.records_by_id((1, 2, 3, 4, 5)), records)
                added = runtime.append(_draft("after recovery", "persisted"))
                self.assertEqual(runtime.records_by_id((added.record_id,)), (added,))
                self.assertEqual(owner.current_generation, 4)
                owner.close_family_session()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                self.addCleanup(fresh.close_family_session)
                fresh.rehydrate_runtime_authority()
                cold = store.SQLiteTMStore.from_coordinator(fresh)
                self.assertEqual(cold.records_by_id((1, 2, 3, 4, 5)), records)
                self.assertEqual(cold.exact_records("after recovery"), (added,))
                self.assertEqual(fresh.current_generation, 4)

    def test_retirement_syscall_once_and_persistent_recover_first_attempt(self):
        self._retirement_unlink_failure()

    def test_retirement_syscall_stays_unavailable_until_io_recovers(self):
        self._retirement_unlink_failure(persistent_during_recovery=True)

    def test_fresh_current_upgrade_after_append_preserves_usable_owner(self):
        from tests.test_tm_schema_upgrade import _record_rows
        from tests.test_tm_sqlite_store import _draft
        for fts in (False, True):
            with self.subTest(fts=fts), patch.object(store, "_probe_fts5", return_value=fts):
                identity, owner, service = self.fixture(fts=fts)
                self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeReport)
                runtime = store.SQLiteTMStore.from_coordinator(owner)
                added = runtime.append(_draft("legal v2 append", "current target"))
                original_records = runtime.records_by_id((1, 2, 3, 4, 5, added.record_id))
                before = identity.canonical_sidecar_path.read_bytes()
                rows = _record_rows(identity.canonical_sidecar_path)
                repeated = service.upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(repeated), SchemaUpgradeFailure)
                assert isinstance(repeated, SchemaUpgradeFailure)
                self.assertEqual(repeated.error_code, "SCHEMA.SCHEMA_ALREADY_CURRENT")
                self.assertEqual(repeated.stage, "PREFLIGHT")
                self.assertEqual(repeated.active_store_preservation.state.value, "VERIFIED_UNCHANGED")
                self.assertEqual(owner.state, "READY")
                owner.close_family_session()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                self.addCleanup(fresh.close_family_session)
                assert fresh._family is not None
                root = fresh._family._root
                result = _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
                self.assertIs(type(result), SchemaUpgradeFailure)
                assert isinstance(result, SchemaUpgradeFailure)
                self.assertEqual(result.error_code, "SCHEMA.SCHEMA_ALREADY_CURRENT")
                self.assertEqual(result.stage, "PREFLIGHT")
                self.assertFalse(result.retryable)
                self.assertEqual(result.active_store_preservation.state.value, "VERIFIED_UNCHANGED")
                self.assertEqual(result.active_generation, 4)
                self.assertEqual(fresh.state, "READY")
                self.assertIs(fresh._family._root, root)
                self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                self.assertEqual(_record_rows(identity.canonical_sidecar_path), rows)
                reopened = store.SQLiteTMStore.from_coordinator(fresh)
                self.assertEqual(reopened.records_by_id((1, 2, 3, 4, 5, added.record_id)), original_records)
                self.assertEqual(reopened.exact_records("legal v2 append"), (added,))
                next_record = reopened.append(_draft("next append", "still available"))
                self.assertEqual(reopened.records_by_id((next_record.record_id,)), (next_record,))
                self.assertEqual(fresh.current_generation, 4)

    def test_explicit_current_upgrade_keeps_pending_namespace_and_tamper_strict(self):
        import subprocess
        import tm_activation_journal as journal
        from tests.test_tm_sqlite_store import _draft
        crash = r'''
import os,sys
from pathlib import Path
from unittest.mock import patch
import tm_sqlite_store as store
from tests.test_tm_activation import _identity
from tests.test_tm_schema_upgrade import _service
identity=_identity(Path(sys.argv[1]))
owner=store.ResourceStoreCoordinator(resource_identity=identity,canonical_store_id="store.primary")
write=store._write_activation_journal
def after_write(record,*args,**kwargs):
    result=write(record,*args,**kwargs)
    if record.phase.value == "MANIFEST_PUBLISHED": os._exit(73)
    return result
with patch.object(store,"_probe_fts5",return_value=False),patch.object(store,"_write_activation_journal",after_write):
    _service(owner,identity).upgrade_schema(identity.canonical_sidecar_path)
raise AssertionError("pending seam missed")
'''
        for kind in ("pending", "journal-temp", "terminal-temp", "coexist", "index-tamper"):
            with self.subTest(kind=kind), patch.object(store, "_probe_fts5", return_value=False):
                identity, owner, service = self.fixture()
                prior = identity.canonical_sidecar_path.read_bytes()
                if kind == "pending":
                    owner.close_family_session()
                    result = subprocess.run([sys.executable, "-B", "-c", crash,
                        str(identity.canonical_sidecar_path.parent)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 73, result.stderr)
                    # A page-header change keeps records/indexes intact but
                    # invalidates the strict in-flight active byte proof.
                    with sqlite3.connect(identity.canonical_sidecar_path) as connection:
                        connection.execute("PRAGMA application_id=77").close()
                    connection.close()
                else:
                    self.assertIs(type(service.upgrade_schema(identity.canonical_sidecar_path)), SchemaUpgradeReport)
                    store.SQLiteTMStore.from_coordinator(owner).append(_draft("legal append", "saved"))
                    owner.close_family_session()
                    main = journal._activation_journal_path(identity)
                    terminal = journal._activation_terminal_path(identity)
                    if kind == "journal-temp":
                        journal._activation_journal_temp_path(main).write_bytes(b"unknown pending")
                    elif kind == "terminal-temp":
                        journal._activation_terminal_temp_path(terminal).write_bytes(b"unknown pending")
                    elif kind == "coexist":
                        terminal.write_bytes(main.read_bytes())
                    else:
                        with sqlite3.connect(identity.canonical_sidecar_path) as connection:
                            connection.execute("DELETE FROM tm_gram WHERE record_id=3").close()
                        connection.close()
                before = identity.canonical_sidecar_path.read_bytes()
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store.primary")
                self.addCleanup(fresh.close_family_session)
                with patch.object(store, "_rehydrate_completed_activation", wraps=store._rehydrate_completed_activation) as completed:
                    if kind == "pending":
                        # Strict recovery can safely roll back to proven v1;
                        # stop the explicitly requested new copy afterwards.
                        with patch.object(migration, "_migrate_schema_copy", side_effect=OSError("stop after strict rollback")):
                            outcome = _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
                        self.assertIs(type(outcome), SchemaUpgradeFailure)
                        self.assertEqual(fresh.current_generation, 3)
                        self.assertEqual(fresh.state, "READY")
                        self.assertEqual(identity.canonical_sidecar_path.read_bytes(), prior)
                    else:
                        with self.assertRaises(Exception):
                            _service(fresh, identity).upgrade_schema(identity.canonical_sidecar_path)
                    self.assertEqual(completed.call_count, 1 if kind == "index-tamper" else 0)
                if kind != "pending":
                    self.assertNotEqual(fresh.state, "READY")
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
