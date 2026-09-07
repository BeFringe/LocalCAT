"""ADR-027 Windows output-control lock retirement tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from platform_fs_contracts import (
    LockPolicy,
    LockWait,
    OutputArtifactLockRetirement,
    OutputLockFinishResult,
    PlatformFileBackend,
)
import platform_fs_windows
from platform_fs_windows import (
    WindowsPlatformAdapter,
    WindowsProcessFileLock,
    WindowsRootedFileSystem,
)


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_PREFIX = b"localcat.resource-artifact.lock.v1\0"
TMX_PREFIX = b"localcat.tmx-direct.lock.v1\0"


def _family(destination: str, *, tmx: bool = False) -> tuple[str, bytes]:
    digest = hashlib.sha256(destination.encode("utf-8", "strict")).digest()
    if tmx:
        return f".localcat-tmx-{digest.hex()[:32]}.lock", TMX_PREFIX + digest
    return f".resource-artifact-{digest.hex()[:32]}.lock", RESOURCE_PREFIX + digest


@unittest.skipUnless(sys.platform == "win32", "ADR-027 requires real Windows")
class WindowsOutputLockRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bound(
        self,
        destination: str = "artifact.localcat-resource",
        *,
        fault=None,
    ):
        backend = WindowsPlatformAdapter(_fault_injector=fault)
        root = backend.bind_root(self.root_path)
        parent = backend.bind_parent(root, PureWindowsPath(destination))
        return backend, root, parent

    def _lease(self, backend, parent, destination: str, *, tmx: bool = False):
        name, payload = _family(destination, tmx=tmx)
        lease = backend.acquire(
            parent,
            name,
            payload,
            LockPolicy(LockWait.FAIL_FAST),
        )
        return name, payload, lease

    def test_capability_is_optional_and_retires_both_approved_families(self) -> None:
        self.assertNotIn("finish_output_lock", PlatformFileBackend.__dict__)
        self.assertNotIsInstance(WindowsRootedFileSystem(), OutputArtifactLockRetirement)
        self.assertNotIsInstance(WindowsProcessFileLock(), OutputArtifactLockRetirement)
        for destination, tmx in (
            ("resource.localcat-resource", False),
            ("scope.tmx", True),
        ):
            with self.subTest(destination=destination):
                backend, root, parent = self._bound(destination)
                try:
                    self.assertIsInstance(backend, OutputArtifactLockRetirement)
                    name, _payload, lease = self._lease(
                        backend, parent, destination, tmx=tmx
                    )
                    result = backend.finish_output_lock(parent, lease)
                    self.assertIs(result, OutputLockFinishResult.RETIRED)
                    self.assertTrue(lease.closed)
                    self.assertFalse(parent.closed)
                    parent.reprove()
                    self.assertFalse((self.root_path / name).exists())
                finally:
                    parent.close()
                    root.close()

    def test_ordinary_close_keeps_the_protocol_carrier(self) -> None:
        destination = "ordinary.localcat-resource"
        backend, root, parent = self._bound(destination)
        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            lease.close()
            self.assertTrue((self.root_path / name).is_file())
        finally:
            parent.close()
            root.close()

    def test_non_family_and_wrong_parent_are_rejected_before_consuming_lease(self) -> None:
        backend, root, parent = self._bound("one.localcat-resource")
        (self.root_path / "nested").mkdir()
        other_parent = backend.bind_parent(root, PureWindowsPath("nested/two.localcat-resource"))
        try:
            arbitrary = backend.acquire(
                parent,
                "ordinary.lock",
                b"ordinary payload",
                LockPolicy(LockWait.FAIL_FAST),
            )
            with self.assertRaises(ValueError):
                backend.finish_output_lock(parent, arbitrary)
            self.assertFalse(arbitrary.closed)
            arbitrary.close()

            _name, _payload, lease = self._lease(
                backend, parent, "one.localcat-resource"
            )
            with self.assertRaises(ValueError):
                backend.finish_output_lock(other_parent, lease)
            self.assertFalse(lease.closed)
            lease.close()
        finally:
            other_parent.close()
            parent.close()
            root.close()

    def test_missing_after_normal_lease_close_returns_absent_without_recreation(self) -> None:
        destination = "missing.localcat-resource"
        name, _payload = _family(destination)

        def remove_after_close(phase: str) -> None:
            if phase == "output_lock_retirement_after_lease_close":
                (self.root_path / name).unlink()

        backend, root, parent = self._bound(destination, fault=remove_after_close)
        try:
            _name, _payload, lease = self._lease(backend, parent, destination)
            result = backend.finish_output_lock(parent, lease)
            self.assertIs(result, OutputLockFinishResult.ABSENT)
            self.assertFalse((self.root_path / name).exists())
        finally:
            parent.close()
            root.close()

    def test_live_holder_and_old_init_gap_return_in_use(self) -> None:
        destination = "holder.localcat-resource"
        backend, root, parent = self._bound(destination)
        holder = None
        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            holder = backend._api.open_handle(
                str(self.root_path / name),
                desired_access=(
                    platform_fs_windows.GENERIC_READ
                    | platform_fs_windows.GENERIC_WRITE
                    | platform_fs_windows.SYNCHRONIZE
                ),
                share_mode=(
                    platform_fs_windows.FILE_SHARE_READ
                    | platform_fs_windows.FILE_SHARE_WRITE
                ),
                creation_disposition=platform_fs_windows.OPEN_EXISTING,
                flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            self.assertIs(
                backend.finish_output_lock(parent, lease),
                OutputLockFinishResult.IN_USE,
            )
            self.assertTrue((self.root_path / name).exists())
        finally:
            if holder is not None:
                holder.close()
            parent.close()
            root.close()

        gap_handle = None

        def enter_old_init_gap(phase: str) -> None:
            nonlocal gap_handle
            if phase == "output_lock_retirement_after_lease_close":
                gap_handle = gap_backend._api.open_handle(
                    str(self.root_path / gap_name),
                    desired_access=(
                        platform_fs_windows.GENERIC_READ
                        | platform_fs_windows.GENERIC_WRITE
                        | platform_fs_windows.SYNCHRONIZE
                    ),
                    share_mode=0,
                    creation_disposition=platform_fs_windows.OPEN_EXISTING,
                    flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
                )

        gap_backend, gap_root, gap_parent = self._bound(
            "gap.localcat-resource", fault=enter_old_init_gap
        )
        gap_name, _gap_payload = _family("gap.localcat-resource")
        try:
            _name, _payload, gap_lease = self._lease(
                gap_backend, gap_parent, "gap.localcat-resource"
            )
            self.assertIs(
                gap_backend.finish_output_lock(gap_parent, gap_lease),
                OutputLockFinishResult.IN_USE,
            )
            self.assertTrue((self.root_path / gap_name).exists())
        finally:
            if gap_handle is not None:
                gap_handle.close()
            gap_parent.close()
            gap_root.close()

    def test_opened_waiter_is_not_bypassed(self) -> None:
        destination = "waiter.tmx"
        backend, root, parent = self._bound(destination)
        waiter_entered = threading.Event()
        release_waiter = threading.Event()
        acquired = threading.Event()
        waiter_errors: list[BaseException] = []
        try:
            name, payload, owner = self._lease(backend, parent, destination, tmx=True)
            real_lock_range = platform_fs_windows._lock_range

            def observed_lock_range(api, handle, policy, deadline):
                if threading.current_thread().name == "output-lock-waiter":
                    waiter_entered.set()
                return real_lock_range(api, handle, policy, deadline)

            def wait_for_lock() -> None:
                try:
                    waiter = backend.acquire(
                        parent,
                        name,
                        payload,
                        LockPolicy(LockWait.BLOCK),
                    )
                    acquired.set()
                    release_waiter.wait(5)
                    waiter.close()
                except BaseException as error:
                    waiter_errors.append(error)

            with mock.patch.object(
                platform_fs_windows, "_lock_range", side_effect=observed_lock_range
            ):
                thread = threading.Thread(target=wait_for_lock, name="output-lock-waiter")
                thread.start()
                self.assertTrue(waiter_entered.wait(5))
                self.assertIs(
                    backend.finish_output_lock(parent, owner),
                    OutputLockFinishResult.IN_USE,
                )
                self.assertTrue(acquired.wait(5))
                release_waiter.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(waiter_errors, [])
            self.assertTrue((self.root_path / name).exists())
        finally:
            release_waiter.set()
            parent.close()
            root.close()

    def test_killed_holder_does_not_authorize_cleanup_until_a_fresh_lease(self) -> None:
        destination = "killed.localcat-resource"
        backend, root, parent = self._bound(destination)
        process = None
        try:
            name, payload, lease = self._lease(backend, parent, destination)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.windows_lock_worker",
                    "open-carrier",
                    str(self.root_path / name),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "READY")
            self.assertIs(
                backend.finish_output_lock(parent, lease),
                OutputLockFinishResult.IN_USE,
            )
            process.kill()
            process.wait(10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            process = None
            fresh = backend.acquire(
                parent,
                name,
                payload,
                LockPolicy(LockWait.FAIL_FAST),
            )
            self.assertIs(
                backend.finish_output_lock(parent, fresh),
                OutputLockFinishResult.RETIRED,
            )
        finally:
            if process is not None:
                process.kill()
                process.wait(10)
            parent.close()
            root.close()

    def test_unknown_empty_prefix_and_hardlink_carriers_are_not_proven(self) -> None:
        mutations = (b"", RESOURCE_PREFIX, b"foreign")
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                destination = f"payload-{index}.localcat-resource"
                backend, root, parent = self._bound(destination)
                try:
                    name, _payload, lease = self._lease(backend, parent, destination)
                    (self.root_path / name).write_bytes(mutation)
                    self.assertIs(
                        backend.finish_output_lock(parent, lease),
                        OutputLockFinishResult.NOT_PROVEN,
                    )
                    self.assertTrue((self.root_path / name).exists())
                finally:
                    parent.close()
                    root.close()

        destination = "hardlink.localcat-resource"
        hardlink_name, _hardlink_payload = _family(destination)

        def add_hardlink_after_close(phase: str) -> None:
            if phase == "output_lock_retirement_after_lease_close":
                os.link(self.root_path / hardlink_name, self.root_path / "lock-alias")

        backend, root, parent = self._bound(
            destination,
            fault=add_hardlink_after_close,
        )
        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            self.assertIs(
                backend.finish_output_lock(parent, lease),
                OutputLockFinishResult.NOT_PROVEN,
            )
            self.assertTrue((self.root_path / name).exists())
        finally:
            parent.close()
            root.close()

    def _assert_fresh_acl_branch_rejected(self, branch: str) -> None:
        destination = f"security-{branch}.localcat-resource"
        after_close = False
        observed: list[str] = []

        def mark_after_close(phase: str) -> None:
            nonlocal after_close
            if phase == "output_lock_retirement_after_lease_close":
                after_close = True

        backend, root, parent = self._bound(destination, fault=mark_after_close)
        real_acl_entries = platform_fs_windows._acl_entries

        def reject_selected_acl(api, acl):
            entries = real_acl_entries(api, acl)
            if not after_close:
                return entries
            current = "dacl" if not observed else "mic"
            observed.append(current)
            return () if current == branch else entries

        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            with mock.patch.object(
                platform_fs_windows,
                "_acl_entries",
                side_effect=reject_selected_acl,
            ):
                self.assertIs(
                    backend.finish_output_lock(parent, lease),
                    OutputLockFinishResult.NOT_PROVEN,
                )
            self.assertEqual(observed[-1], branch)
            self.assertEqual(observed, ["dacl"] if branch == "dacl" else ["dacl", "mic"])
            self.assertTrue((self.root_path / name).exists())
        finally:
            parent.close()
            root.close()

    def test_fresh_dacl_mismatch_is_rejected_without_deleting_carrier(self) -> None:
        self._assert_fresh_acl_branch_rejected("dacl")

    def test_fresh_mic_mismatch_is_rejected_without_deleting_carrier(self) -> None:
        self._assert_fresh_acl_branch_rejected("mic")

    def test_parent_drift_after_lease_close_is_not_proven(self) -> None:
        destination = "parent-drift.localcat-resource"
        after_close = False

        def mark_after_close(phase: str) -> None:
            nonlocal after_close
            if phase == "output_lock_retirement_after_lease_close":
                after_close = True

        backend, root, parent = self._bound(destination, fault=mark_after_close)
        real_reprove = platform_fs_windows._reprove_directory_chain

        def reject_drifted_parent(api, records):
            if after_close:
                raise RuntimeError("injected parent identity drift")
            return real_reprove(api, records)

        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            with mock.patch.object(
                platform_fs_windows,
                "_reprove_directory_chain",
                side_effect=reject_drifted_parent,
            ):
                self.assertIs(
                    backend.finish_output_lock(parent, lease),
                    OutputLockFinishResult.NOT_PROVEN,
                )
            self.assertTrue(lease.closed)
            self.assertTrue((self.root_path / name).exists())
        finally:
            parent.close()
            root.close()

    def test_after_delete_close_fault_is_not_reported_as_retired(self) -> None:
        destination = "after-delete-close.localcat-resource"

        def fail_after_close(phase: str) -> None:
            if phase == "output_lock_retirement_after_delete_close":
                raise RuntimeError("injected post-close uncertainty")

        backend, root, parent = self._bound(destination, fault=fail_after_close)
        try:
            name, _payload, lease = self._lease(backend, parent, destination)
            self.assertIs(
                backend.finish_output_lock(parent, lease),
                OutputLockFinishResult.NOT_PROVEN,
            )
            self.assertTrue(lease.closed)
            self.assertFalse(parent.closed)
            self.assertFalse((self.root_path / name).exists())
            parent.reprove()
        finally:
            parent.close()
            root.close()

    def test_reparse_terminal_and_post_delete_faults_fail_closed(self) -> None:
        for phase in (
            "output_lock_retirement_before_terminal_reproof",
            "output_lock_retirement_after_delete_mark",
        ):
            with self.subTest(phase=phase):
                def fail(selected: str, *, expected: str = phase) -> None:
                    if selected == expected:
                        raise RuntimeError("injected retirement fault")

                fault_backend, fault_root, fault_parent = self._bound(
                    f"fault-{phase}.localcat-resource", fault=fail
                )
                try:
                    _name, _payload, fault_lease = self._lease(
                        fault_backend,
                        fault_parent,
                        f"fault-{phase}.localcat-resource",
                    )
                    self.assertIs(
                        fault_backend.finish_output_lock(fault_parent, fault_lease),
                        OutputLockFinishResult.NOT_PROVEN,
                    )
                finally:
                    fault_parent.close()
                    fault_root.close()

        destination = "reparse.localcat-resource"
        link_name, _payload = _family(destination)
        outside = self.root_path / "outside.lock"
        outside.write_bytes(b"outside")
        privilege_probe = self.root_path / "symlink-privilege-probe"
        try:
            privilege_probe.symlink_to(outside)
        except OSError:
            return
        else:
            privilege_probe.unlink()

        def swap_to_reparse(phase: str) -> None:
            if phase != "output_lock_retirement_after_lease_close":
                return
            carrier = self.root_path / link_name
            carrier.unlink()
            carrier.symlink_to(outside)

        link_backend, link_root, link_parent = self._bound(destination, fault=swap_to_reparse)
        try:
            _name, _payload, link_lease = self._lease(
                link_backend, link_parent, destination
            )
            self.assertIs(
                link_backend.finish_output_lock(link_parent, link_lease),
                OutputLockFinishResult.NOT_PROVEN,
            )
            self.assertTrue(os.path.lexists(self.root_path / link_name))
            self.assertEqual(outside.read_bytes(), b"outside")
        finally:
            link_parent.close()
            link_root.close()


if __name__ == "__main__":
    unittest.main()
