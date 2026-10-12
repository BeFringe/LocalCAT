"""10.1 家族会话端口；真实 SQL 与双进程技术门属于 10.2。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import gc
import os
from pathlib import Path
import pickle
import shutil
import sys
import tempfile
import threading
import unittest
import weakref
from unittest.mock import patch

from platform_fs_contracts import LockPolicy, LockWait, PlatformFileError
from tm_contracts import CanonicalResourceIdentity
from tm_engine import TMEngine
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteStoreLifecycleError


@unittest.skipIf(sys.platform == "win32", "POSIX 家族会话")
class PosixFamilyLifecycleTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.owners: list[ResourceStoreCoordinator] = []

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "family"
        self.root.mkdir()
        self.owners = []
        self.addCleanup(self.close_owners)

    def close_owners(self) -> None:
        for owner in reversed(self.owners):
            owner.close_family_session()

    def owner(self, name: str = "tm", resource: str = "resource") -> ResourceStoreCoordinator:
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            resource, self.root / f"{name}.jsonl",
        )
        result = ResourceStoreCoordinator(
            resource_identity=identity, canonical_store_id="store",
            drain_timeout_seconds=0.5,
        )
        self.owners.append(result)
        return result

    def test_shared_family_retains_root_until_last_explicit_close(self) -> None:
        first, second = self.owner(), self.owner()
        self.assertIs(first._family, second._family)
        assert first._family is not None
        root = first._family._root
        with first._family_operation() as borrow:
            borrow.reprove(first)
        root.reprove_location()
        first.close_family_session()
        with second._family_operation() as borrow:
            borrow.reprove(second)
        second.close_family_session()
        with self.assertRaises(PlatformFileError):
            root.reprove_location()

    def test_same_family_different_resource_is_rejected(self) -> None:
        self.owner()
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_CONFLICT"):
            self.owner(resource="conflict")

    def test_admission_precedes_w1_and_condition_is_not_held(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        acquire = family._backend.acquire
        observations: list[int] = []

        def observed(*args, **kwargs):
            def inspect() -> int:
                with family._condition:
                    return family._active_count
            with ThreadPoolExecutor(max_workers=1) as pool:
                observations.append(pool.submit(inspect).result(timeout=1))
            return acquire(*args, **kwargs)

        with patch.object(type(family._backend), "acquire", side_effect=observed):
            with owner._family_operation() as borrow:
                borrow.reprove(owner)
        self.assertEqual(observations, [1])
        self.assertEqual(family._active_count, 0)

    def test_borrow_nonclosing_nonserializable_thread_resource_and_expiry(self) -> None:
        owner, other = self.owner(), self.owner("other", "other")
        family = owner._family
        assert family is not None
        with patch.object(type(family._backend), "acquire", wraps=family._backend.acquire) as acquire:
            with owner._family_operation() as borrow:
                with owner._family_operation(borrow=borrow) as inner:
                    self.assertIs(inner, borrow)
                    inner.reprove(owner)
                borrow.reprove(owner)
                self.assertFalse(hasattr(borrow, "close"))
                for function in (copy.copy, copy.deepcopy, pickle.dumps):
                    with self.assertRaises(TypeError):
                        function(borrow)
                with self.assertRaises(SQLiteStoreLifecycleError):
                    borrow.reprove(other)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(SQLiteStoreLifecycleError):
                        pool.submit(borrow.reprove, owner).result(timeout=1)
            self.assertEqual(acquire.call_count, 1)
        with self.assertRaises(SQLiteStoreLifecycleError):
            borrow.reprove(owner)

    def test_implicit_nested_operation_and_self_drain_reject_immediately(self) -> None:
        owner, sibling = self.owner(), self.owner()
        with owner._family_operation():
            for context in (owner._family_operation(), sibling._family_operation(), owner._family_transition()):
                with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_REENTRY"):
                    with context:
                        self.fail("不允许隐式重入")

    def test_transition_drains_before_w1_and_blocks_new_admission(self) -> None:
        owner, sibling = self.owner(), self.owner()
        family = owner._family
        assert family is not None
        entered = threading.Event()
        release = threading.Event()
        states: list[tuple[str, int]] = []
        acquire = family._backend.acquire

        def observed(*args, **kwargs):
            states.append((family._state, family._active_count))
            return acquire(*args, **kwargs)

        def operation() -> None:
            with owner._family_operation():
                entered.set()
                self.assertTrue(release.wait(2))

        def transition() -> None:
            with sibling._family_transition() as borrow:
                borrow.reprove(sibling)

        with patch.object(type(family._backend), "acquire", side_effect=observed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                normal = pool.submit(operation)
                self.assertTrue(entered.wait(1))
                replacing = pool.submit(transition)
                with family._condition:
                    self.assertTrue(family._condition.wait_for(lambda: family._state == "DRAINING", 1))
                try:
                    with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.RESOURCE_DRAINING"):
                        with owner._family_operation():
                            self.fail("不允许新准入")
                    self.assertEqual(len(states), 1)
                finally:
                    release.set()
                normal.result(timeout=2)
                replacing.result(timeout=2)
        self.assertEqual(states, [("READY", 1), ("ACTIVATING", 0)])
        self.assertEqual(family._state, "READY")

    def test_w1_timeout_returns_admission_and_keeps_resource_ready(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        lease = family._backend.acquire(family._root, family._lock_name, family._payload, LockPolicy(LockWait.FAIL_FAST))
        try:
            with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_BUSY"):
                with owner._family_operation(timeout_seconds=0.05):
                    self.fail("不允许越过 W1")
        finally:
            lease.close()
        self.assertEqual(family._active_count, 0)
        with owner._family_operation():
            pass

    def test_cancel_and_exception_return_admission_and_lease(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.OPERATION_CANCELLED"):
            with owner._family_operation(cancelled=cancelled):
                self.fail("已取消")
        with self.assertRaisesRegex(RuntimeError, "body failure"):
            with owner._family_operation():
                raise RuntimeError("body failure")
        self.assertEqual(family._active_count, 0)
        with owner._family_transition():
            pass

    def test_cancel_while_waiting_for_w1_returns_admission(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        lease = family._backend.acquire(family._root, family._lock_name, family._payload, LockPolicy(LockWait.FAIL_FAST))
        cancelled = threading.Event()
        waiting = threading.Event()
        acquire = family._backend.acquire

        def observed(*args, **kwargs):
            waiting.set()
            return acquire(*args, **kwargs)

        def operation() -> None:
            with owner._family_operation(cancelled=cancelled):
                self.fail("已取消操作不得取得授权")

        try:
            with patch.object(type(family._backend), "acquire", side_effect=observed):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(operation)
                    self.assertTrue(waiting.wait(1))
                    cancelled.set()
                    with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.OPERATION_CANCELLED"):
                        future.result(timeout=1)
        finally:
            lease.close()
        self.assertEqual(family._active_count, 0)
        self.assertEqual(family._state, "READY")

    def test_drain_timeout_does_not_acquire_w1_and_restores_admission(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None

        def transition() -> None:
            with owner._family_transition(timeout_seconds=0.03):
                self.fail("活操作尚未排空")

        with owner._family_operation():
            with patch.object(type(family._backend), "acquire", side_effect=AssertionError("排空前不得取 W1")):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_BUSY"):
                        pool.submit(transition).result(timeout=1)
            self.assertEqual(family._state, "READY")
            self.assertEqual(family._active_count, 1)
        with owner._family_transition():
            pass

    def test_close_during_operation_rejects_without_closing_authority(self) -> None:
        owner = self.owner()
        with owner._family_operation() as borrow:
            with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_BUSY"):
                owner.close_family_session()
            borrow.reprove(owner)
        owner.close_family_session()
        with self.assertRaises(SQLiteStoreLifecycleError):
            with owner._family_operation():
                self.fail("已关闭 owner 不得刷新授权")

    def test_lock_replacement_latches_and_uses_existing_w1_format(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        with owner._family_operation():
            pass
        lock = self.root / family._lock_name
        expected = (
            "localcat-initial-activation-lock-v1\0"
            f"{owner._resource_identity.target_identity}\n"
        ).encode("ascii")
        self.assertEqual(lock.read_bytes(), expected)
        replacement = self.root / "clone.lock"
        replacement.write_bytes(expected)
        os.replace(replacement, lock)
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.RESOURCE_IDENTITY_STALE"):
            with owner._family_operation():
                self.fail("不接受替换锁对象")
        self.assertEqual(owner.state, "FAILED")

    def test_same_physical_family_alias_is_rejected_from_live_lock_identity(self) -> None:
        owner = self.owner()
        with owner._family_operation():
            pass
        # POSIX 的双斜线保留不同 lexical path，但落在同一个实际目录。
        alias = Path("/" + str(self.root)) / "tm.jsonl"
        self.assertNotEqual(alias, self.root / "tm.jsonl")
        identity = CanonicalResourceIdentity.from_configured_jsonl("resource", alias)
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_CONFLICT"):
            ResourceStoreCoordinator(resource_identity=identity, canonical_store_id="store")

    def test_expired_borrow_does_not_authorize_later_operation(self) -> None:
        owner = self.owner()
        with owner._family_operation() as prior:
            pass
        with owner._family_operation():
            with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.OPERATION_BORROW_INVALID"):
                prior.reprove(owner)

    def test_released_owner_finalizer_closes_root_without_retaining_owner(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        reference = weakref.ref(owner)
        self.owners.clear()
        del owner
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(family._owners, set())
        with self.assertRaises(PlatformFileError):
            family._root.reprove_location()
        self.assertIsNot(self.owner()._family, family)

    def test_released_shared_owner_keeps_active_sibling_authority(self) -> None:
        owner, sibling = self.owner(), self.owner()
        family = owner._family
        assert family is not None
        nonce = sibling._owner_nonce
        self.owners.remove(sibling)
        with owner._family_operation() as borrow:
            del sibling
            gc.collect()
            self.assertNotIn(nonce, family._owners)
            borrow.reprove(owner)

    def test_failed_last_owner_release_closes_root_but_keeps_tombstone(self) -> None:
        owner = self.owner()
        family = owner._family
        assert family is not None
        self.root.rename(self.root.with_name("moved"))
        self.root.mkdir()
        with self.assertRaises(SQLiteStoreLifecycleError):
            with owner._family_operation():
                pass
        owner.close_family_session()
        with self.assertRaises(PlatformFileError):
            family._root.reprove_location()
        with self.assertRaises(SQLiteStoreLifecycleError):
            self.owner()

    def test_close_observes_drift_before_retiring_last_owner(self) -> None:
        owner = self.owner()
        self.root.rename(self.root.with_name("moved"))
        self.root.mkdir()
        owner.close_family_session()
        self.assertEqual(owner.state, "FAILED")
        with self.assertRaises(SQLiteStoreLifecycleError):
            self.owner()

    def test_failed_engine_construction_does_not_keep_an_owner(self) -> None:
        # 使用真实小 canonical 资源覆盖 coordinator 已建立后的 engine 失败。
        from tests.test_tm_engine_compat import _activate_resource
        import tm_sqlite_store

        configured = _activate_resource(self.root)
        gc.collect()
        with patch("tm_engine._rehydrate_canonical_runtime_authority", side_effect=OSError("failure")):
            with self.assertRaises(ValueError):
                TMEngine(str(configured))
        gc.collect()
        self.assertNotIn(configured, tm_sqlite_store._FAMILY_SESSIONS)

    def test_close_respects_existing_store_generation_lease(self) -> None:
        from tests.test_tm_engine_compat import _activate_resource

        engine = TMEngine(str(_activate_resource(self.root)))
        store = engine.canonical_store
        assert store is not None
        owner = store.coordinator
        self.owners.append(owner)
        with owner._operation_lease():
            with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.FAMILY_BUSY"):
                owner.close_family_session()
        self.assertEqual(store.canonical_revision().record_count, 3)

    def test_closed_tombstone_identity_cannot_block_an_unrelated_reused_inode(self) -> None:
        owner = self.owner()
        original_path = self.root / "tm.jsonl"
        retired = owner._family
        assert retired is not None
        with owner._family_operation():
            pass
        self.root.rename(self.root.with_name("moved"))
        self.root.mkdir()
        owner.close_family_session()
        self.assertTrue(retired._root.closed)

        self.root = self.root.parent / "healthy"
        self.root.mkdir()
        identity = CanonicalResourceIdentity.from_configured_jsonl("healthy", self.root / "healthy.jsonl")
        lock_name = f".{identity.canonical_sidecar_path.name}.localcat-initial-activation.lock"
        payload = f"localcat-initial-activation-lock-v1\0{identity.target_identity}\n".encode("ascii")
        root = retired._backend.bind_root(self.root)
        with root:
            with retired._backend.acquire(root, lock_name, payload, LockPolicy(LockWait.FAIL_FAST)):
                current = root.inspect_entry(lock_name)
                assert current is not None

        # 模拟 inode 回收后 opaque identity 恰好等值。旧 root 已关闭，
        # 历史值不得参与另一路径的新 owner/operation 冲突裁决。
        with patch.object(retired, "_lock_identity", current.identity):
            with self.owner("healthy", "healthy")._family_operation():
                pass
            with self.assertRaises(ValueError):
                TMEngine(str(original_path))

    def _assert_registry_scan_survives_finalizer(self, *, joining: bool) -> None:
        owner = self.owner("active", "active")
        if joining:
            # 留下真实 W1，使下一次 join 必须遍历已有物理家族。
            with owner._family_operation():
                pass
            owner.close_family_session()
        peer = self.owner("retiring", "retiring")
        family = peer._family
        assert family is not None
        reference = weakref.ref(peer)
        self.owners.remove(peer)
        original = family.reprove
        invoked = False
        gc_was_enabled = gc.isenabled()
        gc.disable()
        # 仅延迟消费方的循环引用回收，不改 root、锁或 finalizer。
        holder: list[object] = [peer]
        holder.append(holder)
        del peer, holder

        def collect_during_reproof() -> None:
            nonlocal invoked
            if not invoked:
                invoked = True
                gc.collect()
            original()

        try:
            with patch.object(family, "reprove", collect_during_reproof):
                if joining:
                    owner = self.owner("active", "active")
                with owner._family_operation() as borrow:
                    borrow.reprove(owner)
        finally:
            if gc_was_enabled:
                gc.enable()
        self.assertTrue(invoked)
        self.assertIsNone(reference())
        self.assertTrue(family._root.closed)
        self.assertEqual(family._owners, set())
        assert owner._family is not None
        self.assertEqual(owner._family._active_count, 0)
        self.assertEqual(owner._family._state, "READY")

    def test_operation_registry_scan_survives_unrelated_gc_finalizer(self) -> None:
        self._assert_registry_scan_survives_finalizer(joining=False)

    def test_join_registry_scan_survives_unrelated_gc_finalizer(self) -> None:
        self._assert_registry_scan_survives_finalizer(joining=True)

    def test_root_drift_latches_across_owner_engine_and_clone(self) -> None:
        owner, peer = self.owner(), self.owner("peer", "peer")
        with owner._family_operation():
            pass
        original = self.root.with_name("original")
        self.root.rename(original)
        shutil.copytree(original, self.root)
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.RESOURCE_IDENTITY_STALE"):
            with owner._family_transition():
                self.fail("不接管副本")
        self.assertEqual(owner.state, "FAILED")
        with patch("tm_engine._activation_facts", side_effect=AssertionError("不得先读 clone")):
            with self.assertRaisesRegex(ValueError, "TM.CANONICAL_UNHEALTHY"):
                TMEngine(str(self.root / "tm.jsonl"))
        with self.assertRaises(SQLiteStoreLifecycleError):
            self.owner()
        # 同目录 peer 的位置同样失效；真正独立的目录应继续运行。
        self.assertNotEqual(owner._family, peer._family)
        self.root = self.root.parent / "healthy"
        self.root.mkdir()
        with self.owner("healthy", "healthy")._family_operation():
            pass

    def test_drift_during_operation_cannot_be_restored_ready_by_finally(self) -> None:
        owner = self.owner()
        with self.assertRaisesRegex(SQLiteStoreLifecycleError, "STORE.RESOURCE_IDENTITY_STALE"):
            with owner._family_transition():
                self.root.rename(self.root.with_name("moved"))
                self.root.mkdir()
        self.assertEqual(owner.state, "FAILED")
        assert owner._family is not None
        self.assertEqual(owner._family._active_count, 0)


if __name__ == "__main__":
    unittest.main()
