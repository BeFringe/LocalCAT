"""Core 10.2b：默认公开入口使用真实 POSIX 家族生命周期。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tm_contracts import CanonicalResourceIdentity, MigrationFailure, MigrationReport
from tm_engine import SourceUnit, TMEngine, open_canonical_tm_store
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


@unittest.skipIf(sys.platform == "win32", "POSIX 公开生命周期")
class PosixPublicLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "memory.jsonl"
        self.source.write_text('{"source":"hello","target":"你好"}\n', encoding="utf-8")
        self.original = self.source.read_bytes()
        self.identity = CanonicalResourceIdentity.from_configured_jsonl("tm.public", self.source)
        self.owner = ResourceStoreCoordinator(resource_identity=self.identity, canonical_store_id="store.public")
        self.addCleanup(self.owner.close_family_session)
        self.service = TMMigrationService(resource_identity=self.identity, canonical_store_id="store.public", coordinator=self.owner)

    def activate(self):
        result = self.service.activate_initial(self.source, self.identity.resource_id)
        self.assertIs(type(result), MigrationReport, result)
        self.assertEqual(result.activated_generation, 0)
        return SQLiteTMStore.from_coordinator(self.owner)

    def test_public_initial_activation_and_lazy_query_hold_original_family(self):
        family = self.owner._family
        assert family is not None
        original_root = family._root
        acquire = type(family._backend).acquire
        observations = []

        def observe(backend, root, *args, **kwargs):
            observations.append((root, family._state, family._active_count))
            return acquire(backend, root, *args, **kwargs)

        with patch.object(type(family._backend), "acquire", observe):
            runtime = self.activate()
            with runtime.query_lease() as view:
                self.assertEqual(family._active_count, 1)
                self.assertEqual(view.exact_records("hello")[0].target_raw, "你好")
                self.assertTrue(view.health().healthy)
        self.assertEqual(observations, [(original_root, "ACTIVATING", 0), (original_root, "READY", 1)])
        self.assertEqual(self.owner._active_lease_count, 0)
        self.assertEqual(family._active_count, 0)

    def test_public_engine_query_save_and_reopen_keep_canonical_generation(self):
        self.activate()
        engine = TMEngine(str(self.source))
        self.assertTrue(engine.canonical_active)
        self.assertEqual(engine.query_exact("hello").target, "你好")
        self.assertTrue(engine.save_record(SourceUnit(id="one", text="saved"), "保存"))
        self.assertEqual(self.source.read_bytes(), self.original)
        reopened = open_canonical_tm_store(self.source)
        assert reopened is not None
        self.addCleanup(reopened.coordinator.close_family_session)
        self.assertEqual(reopened.canonical_revision().generation, 0)
        self.assertEqual(reopened.exact_records("saved")[0].target_raw, "保存")

    def test_public_replacement_waits_for_query_and_publishes_one_generation(self):
        runtime = self.activate()
        family = self.owner._family
        assert family is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            with runtime.query_lease() as view:
                self.assertEqual(view.exact_records("hello")[0].target_raw, "你好")
                future = pool.submit(self.service.import_snapshot, self.source, self.identity.resource_id)
                with family._condition:
                    self.assertTrue(family._condition.wait_for(lambda: family._state == "DRAINING", 1))
                self.assertFalse(future.done())
            result = future.result(timeout=5)
        self.assertIs(type(result), MigrationReport, result)
        self.assertEqual(result.activated_generation, 1)
        self.assertEqual(runtime.exact_records("hello")[0].target_raw, "你好")
        self.assertEqual(family._active_count, 0)

    def test_invalid_replacement_preserves_last_known_good_and_query(self):
        runtime = self.activate()
        before = self.identity.canonical_sidecar_path.read_bytes()
        missing = self.root / "missing.jsonl"
        result = self.service.import_snapshot(missing, self.identity.resource_id)
        self.assertIs(type(result), MigrationFailure, result)
        self.assertEqual(self.owner.current_generation, 0)
        self.assertEqual(self.identity.canonical_sidecar_path.read_bytes(), before)
        self.assertEqual(runtime.exact_records("hello")[0].target_raw, "你好")
        self.assertTrue(runtime.health().healthy)

    def test_published_verify_tail_recovers_under_original_borrow(self):
        family = self.owner._family
        assert family is not None
        root = family._root
        verify = self.service._verify_initial_activation_runtime
        recover = self.owner.recover_durable_activation
        acquire = type(family._backend).acquire
        verifications = []
        recoveries = []
        acquisitions = []

        def observe_acquire(backend, observed_root, *args, **kwargs):
            acquisitions.append(observed_root)
            return acquire(backend, observed_root, *args, **kwargs)

        def fail_after_verified(**kwargs):
            verify(**kwargs)
            borrow = kwargs["_borrow"]
            self.assertIs(borrow._family, family)
            self.assertIs(kwargs["_activation_borrow"].root, root)
            verifications.append(borrow)
            if len(verifications) == 1:
                raise OSError("lost verified return")

        def observe_recovery(**kwargs):
            result = recover(**kwargs)
            if verifications:
                self.assertIs(kwargs["_posix_borrow"].root, root)
                self.assertIs(kwargs["_posix_borrow"].lease, verifications[0]._lease)
                self.assertEqual(result.action, "COMPLETED")
                recoveries.append(result)
            return result

        with patch.object(type(family._backend), "acquire", observe_acquire), patch.object(
            self.service, "_verify_initial_activation_runtime", fail_after_verified
        ), patch.object(self.owner, "recover_durable_activation", observe_recovery):
            runtime = self.activate()
        self.assertEqual(acquisitions, [root])
        self.assertEqual(len(verifications), 2)
        self.assertIs(verifications[0], verifications[1])
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(family._state, "READY")
        self.assertEqual(runtime.exact_records("hello")[0].target_raw, "你好")

    def test_second_initial_caller_waits_after_preparation_then_reuses_generation(self):
        prepared = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        publish = self.owner.publish_prepared_activation
        family = self.owner._family
        assert family is not None
        wait = family._condition.wait

        def hold(*args, **kwargs):
            prepared.set()
            self.assertTrue(release.wait(3))
            return publish(*args, **kwargs)

        def observe_wait(*args, **kwargs):
            waiting.set()
            return wait(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(self.owner, "publish_prepared_activation", hold), patch.object(family._condition, "wait", observe_wait):
                first = pool.submit(self.service.activate_initial, self.source, self.identity.resource_id)
                try:
                    self.assertTrue(prepared.wait(3))
                    second = pool.submit(self.service.activate_initial, self.source, self.identity.resource_id)
                    self.assertTrue(waiting.wait(2))
                    self.assertFalse(second.done())
                finally:
                    release.set()
                first_result = first.result(timeout=5)
                second_result = second.result(timeout=5)
        self.assertIs(type(first_result), MigrationReport, first_result)
        self.assertEqual(first_result, second_result)
        self.assertEqual(self.owner.current_generation, 0)


if __name__ == "__main__":
    unittest.main()
