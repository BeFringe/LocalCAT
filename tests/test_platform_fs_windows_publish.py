"""Task 3.5 Windows handle-bound publication lifecycle tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest import mock

from platform_fs_contracts import (
    LockPolicy,
    LockWait,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
)
import platform_fs_windows
from platform_fs_windows import WindowsPlatformAdapter
from windows_file_api import NtStatusError, Win32CallError


LOCK_PAYLOAD = (
    b"LOCALCAT-PROTOCOL-CONTROL-LOCK\x00"
    b"schema=1\nresource-family=publish-tests\nrange-map=exclusive-byte-0\n"
)


def _assert_platform_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode,
) -> None:
    case.assertEqual(caught.exception.code, code.value)
    case.assertEqual(caught.exception.args, (code.value,))
    case.assertIsNone(caught.exception.__cause__)


@unittest.skipUnless(sys.platform == "win32", "Task 3.5 requires real Windows")
class WindowsPublishRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name)
        self.adapter = WindowsPlatformAdapter()
        self.root = self.adapter.bind_root(self.root_path)
        self.parent = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("placeholder.bin"),
        )

    def tearDown(self) -> None:
        self.parent.close()
        self.root.close()
        self.temporary.cleanup()

    def _candidate(self, name: str, payload: bytes, *, private: bool = False):
        candidate = self.parent.create_candidate(name, private=private)
        candidate.write_all(payload)
        candidate.flush_content()
        return candidate

    def _lease(self, name: str = "resource.lock"):
        return self.adapter.acquire(
            self.parent,
            name,
            LOCK_PAYLOAD,
            LockPolicy(LockWait.FAIL_FAST),
        )

    def test_create_if_absent_returns_pending_with_retained_readback(self) -> None:
        payload = b"published bytes"
        candidate = self._candidate("candidate.tmp", payload)
        pending = self.parent.begin_publish(
            candidate,
            "destination.bin",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        try:
            self.assertTrue(candidate.closed)
            preliminary = pending.preliminary_facts()
            self.assertEqual(preliminary.mode, PublishMode.CREATE_IF_ABSENT)
            self.assertEqual(preliminary.content_sha256, hashlib.sha256(payload).digest())
            self.assertEqual(preliminary.byte_count, len(payload))
            self.assertEqual(pending.retained_destination().read_all(), payload)
            self.assertEqual(pending.terminal_reproof(), preliminary)
            (self.root_path / "replacement.bin").write_bytes(b"replacement")
            with self.assertRaises(PermissionError):
                os.replace(
                    self.root_path / "replacement.bin",
                    self.root_path / "destination.bin",
                )
        finally:
            pending.close()
        self.assertEqual((self.root_path / "destination.bin").read_bytes(), payload)

    def test_retained_publish_facts_hash_once_then_terminal_reproves_cache(self) -> None:
        payload = b"publish-facts" * (2 * 1024 * 1024 // len(b"publish-facts") + 1)
        candidate = self._candidate("candidate.tmp", payload)
        retained_type = platform_fs_windows._WindowsBoundRegularFile
        real_capture = retained_type._capture_whole_file_locked
        real_reprove = retained_type._reprove
        capture_calls = 0
        reprove_calls = 0

        def counted_capture(bound: object, *, materialize: bool) -> object:
            nonlocal capture_calls
            capture_calls += 1
            return real_capture(bound, materialize=materialize)

        def counted_reprove(bound: object) -> object:
            nonlocal reprove_calls
            reprove_calls += 1
            return real_reprove(bound)

        with mock.patch.object(
            retained_type,
            "_capture_whole_file_locked",
            new=counted_capture,
        ), mock.patch.object(
            retained_type,
            "_reprove",
            new=counted_reprove,
        ):
            pending = self.parent.begin_publish(
                candidate,
                "destination.bin",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
        try:
            preliminary = pending.preliminary_facts()
            self.assertEqual(capture_calls, 1)
            self.assertEqual(reprove_calls, 3)
            self.assertEqual(preliminary.byte_count, len(payload))
            self.assertEqual(
                preliminary.content_sha256,
                hashlib.sha256(payload).digest(),
            )

            retained = pending.retained_destination()
            capture_calls = 0
            reprove_calls = 0
            with mock.patch.object(
                retained_type,
                "_capture_whole_file_locked",
                new=counted_capture,
            ), mock.patch.object(
                retained_type,
                "_reprove",
                new=counted_reprove,
            ), mock.patch.object(
                retained._api,
                "ReadFile",
                wraps=retained._api.ReadFile,
            ) as read_file:
                self.assertEqual(pending.terminal_reproof(), preliminary)
            self.assertEqual(capture_calls, 0)
            self.assertEqual(reprove_calls, 1)
            read_file.assert_not_called()
        finally:
            pending.close()

    def test_terminal_retained_drift_and_consumer_fault_require_recovery(self) -> None:
        retained_type = platform_fs_windows._WindowsBoundRegularFile
        for case in ("drift", "consumer"):
            with self.subTest(case=case):
                candidate = self._candidate(f"{case}.tmp", case.encode("ascii") * 257)
                pending = self.parent.begin_publish(
                    candidate,
                    f"{case}.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
                retained = pending.retained_destination()
                try:
                    if case == "drift":
                        real_reprove = retained_type._reprove

                        def drifting_reprove(bound: object) -> object:
                            proof = real_reprove(bound)
                            if bound is not retained:
                                return proof
                            snapshot = platform_fs_windows.EntrySnapshot(
                                identity=proof.snapshot.identity,
                                byte_count=proof.snapshot.byte_count,
                                modified_token=hashlib.sha256(
                                    proof.snapshot.modified_token + b"drift"
                                ).digest(),
                                reparse_free=proof.snapshot.reparse_free,
                            )
                            return platform_fs_windows._WindowsHandleProof(
                                proof.identity,
                                snapshot,
                                proof.final_path,
                            )

                        patcher = mock.patch.object(
                            retained_type,
                            "_reprove",
                            new=drifting_reprove,
                        )
                    else:
                        real_content_facts = retained_type._content_facts

                        def consumer_fault(bound: object) -> object:
                            if bound is retained:
                                raise RuntimeError("consumer fault")
                            return real_content_facts(bound)

                        patcher = mock.patch.object(
                            retained_type,
                            "_content_facts",
                            new=consumer_fault,
                        )
                    with patcher, self.assertRaises(PlatformFileError) as caught:
                        pending.terminal_reproof()
                    _assert_platform_error(
                        self,
                        caught,
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                    )
                finally:
                    pending.close()

    def test_same_parent_native_name_is_independent_of_current_directory(self) -> None:
        payload = b"root-owned payload"
        candidate = self._candidate("candidate.tmp", payload)
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as decoy_temporary:
            decoy = Path(decoy_temporary)
            decoy_destination = decoy / "destination.bin"
            decoy_destination.write_bytes(b"cwd decoy")
            try:
                os.chdir(decoy)
                pending = self.parent.begin_publish(
                    candidate,
                    "destination.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            finally:
                os.chdir(previous_cwd)
            pending.close()
            self.assertEqual(decoy_destination.read_bytes(), b"cwd decoy")
        self.assertEqual((self.root_path / "destination.bin").read_bytes(), payload)

    def test_invalid_destination_forms_never_reach_native_rename(self) -> None:
        for index, destination in enumerate(
            (
                "nested\\destination.bin",
                "nested/destination.bin",
                r"C:\absolute.bin",
                "destination.bin:stream",
            )
        ):
            with self.subTest(destination=destination):
                candidate = self._candidate(f"candidate-{index}.tmp", b"payload")
                with (
                    mock.patch.object(
                        self.parent._api,
                        "self_probe_nt_set_information_file",
                    ) as probe,
                    mock.patch.object(
                        self.parent._api,
                        "rename_file_same_parent",
                    ) as rename,
                    self.assertRaises((PlatformFileError, ValueError)),
                ):
                    self.parent.begin_publish(
                        candidate,
                        destination,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                probe.assert_not_called()
                rename.assert_not_called()
                candidate.close()

    def test_foreign_parent_candidate_is_rejected_before_native_call(self) -> None:
        foreign_directory = self.root_path / "foreign"
        foreign_directory.mkdir()
        foreign_parent = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("foreign/placeholder.bin"),
        )
        candidate = foreign_parent.create_candidate("candidate.tmp", private=False)
        candidate.write_all(b"foreign parent")
        candidate.flush_content()
        try:
            with (
                mock.patch.object(
                    self.parent._api,
                    "self_probe_nt_set_information_file",
                ) as probe,
                mock.patch.object(
                    self.parent._api,
                    "rename_file_same_parent",
                ) as rename,
                self.assertRaises(PlatformFileError) as caught,
            ):
                self.parent.begin_publish(
                    candidate,
                    "destination.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            _assert_platform_error(self, caught, PlatformFileErrorCode.PUBLISH_FAILED)
            probe.assert_not_called()
            rename.assert_not_called()
            self.assertFalse((self.root_path / "destination.bin").exists())
        finally:
            foreign_parent.close()

    def test_native_capability_failure_is_pre_arm_and_does_not_name_destination(self) -> None:
        candidate_path = self.root_path / "candidate.tmp"
        candidate = self._candidate(candidate_path.name, b"payload")
        with (
            mock.patch.object(
                self.parent._api,
                "self_probe_nt_set_information_file",
                side_effect=RuntimeError("unavailable"),
            ),
            mock.patch.object(
                self.parent._api,
                "rename_file_same_parent",
            ) as rename,
            self.assertRaises(PlatformFileError) as caught,
        ):
            self.parent.begin_publish(
                candidate,
                "destination.bin",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.DURABILITY_UNAVAILABLE)
        rename.assert_not_called()
        self.assertTrue(candidate.closed)
        self.assertTrue(candidate_path.exists())
        self.assertFalse((self.root_path / "destination.bin").exists())

    def test_native_failure_after_arm_requires_recovery(self) -> None:
        candidate_path = self.root_path / "candidate.tmp"
        candidate = self._candidate(candidate_path.name, b"payload")
        with (
            mock.patch.object(
                self.parent._api,
                "self_probe_nt_set_information_file",
                return_value=None,
            ),
            mock.patch.object(
                self.parent._api,
                "rename_file_same_parent",
                side_effect=NtStatusError("NtSetInformationFile", 0xC000000D),
            ),
            self.assertRaises(PlatformFileError) as caught,
        ):
            self.parent.begin_publish(
                candidate,
                "destination.bin",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.RECOVERY_REQUIRED)
        self.assertTrue(candidate.closed)
        self.assertTrue(candidate_path.exists())
        self.assertFalse((self.root_path / "destination.bin").exists())

    def test_candidate_profile_is_create_new_share_none_write_through(self) -> None:
        calls: list[dict[str, object]] = []
        original_open = self.parent._api.open_handle

        def recording_open(path: str, **keywords: object):
            calls.append(dict(keywords))
            return original_open(path, **keywords)

        with mock.patch.object(
            self.parent._api,
            "open_handle",
            side_effect=recording_open,
        ):
            candidate = self.parent.create_candidate("candidate.tmp", private=False)
        candidate.close()
        candidate_calls = [
            call
            for call in calls
            if int(call["desired_access"]) & platform_fs_windows.GENERIC_WRITE
        ]
        self.assertEqual(len(candidate_calls), 1)
        profile = candidate_calls[0]
        self.assertEqual(profile["creation_disposition"], platform_fs_windows.CREATE_NEW)
        self.assertEqual(profile["share_mode"], 0)
        self.assertTrue(
            int(profile["flags"]) & platform_fs_windows.FILE_FLAG_WRITE_THROUGH
        )
        self.assertTrue(
            int(profile["flags"]) & platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT
        )
        self.assertTrue(int(profile["desired_access"]) & platform_fs_windows.DELETE)

    def test_candidate_reopen_blocks_before_close_and_succeeds_after_close(self) -> None:
        payload = b"share lifecycle"
        candidate = self._candidate("candidate.tmp", payload)
        facts = self.parent._rename_candidate(
            candidate,
            "destination.bin",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        with self.assertRaises(Win32CallError) as caught:
            self.parent._api.open_handle(
                self.parent._leaf_path + "\\destination.bin",
                desired_access=platform_fs_windows.GENERIC_READ,
                share_mode=platform_fs_windows.FILE_SHARE_READ,
                creation_disposition=platform_fs_windows.OPEN_EXISTING,
                flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        self.assertEqual(caught.exception.winerror, platform_fs_windows.ERROR_SHARING_VIOLATION)
        candidate.close()
        retained = self.parent._open_published_destination("destination.bin", facts)
        retained.close()

    def test_create_if_absent_collision_preserves_existing_destination(self) -> None:
        destination = self.root_path / "destination.bin"
        destination.write_bytes(b"old")
        candidate = self._candidate("candidate.tmp", b"new")
        with self.assertRaises(PlatformFileError) as caught:
            self.parent.begin_publish(
                candidate,
                destination.name,
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
        _assert_platform_error(self, caught, PlatformFileErrorCode.RECOVERY_REQUIRED)
        self.assertTrue(candidate.closed)
        self.assertEqual(destination.read_bytes(), b"old")

    def test_open_destination_rejects_replace_until_reader_closes(self) -> None:
        destination = self.root_path / "destination.bin"
        destination.write_bytes(b"old")
        lease = self._lease()
        reader = self.parent._api.open_handle(
            self.parent._leaf_path + "\\destination.bin",
            desired_access=platform_fs_windows.GENERIC_READ,
            share_mode=platform_fs_windows.FILE_SHARE_READ,
            creation_disposition=platform_fs_windows.OPEN_EXISTING,
            flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
        )
        candidate = self._candidate("blocked.tmp", b"blocked")
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.parent.begin_publish(
                    candidate,
                    destination.name,
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=lease,
                )
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.RECOVERY_REQUIRED,
            )
            self.assertTrue(candidate.closed)
            self.assertEqual(destination.read_bytes(), b"old")
        finally:
            reader.close()

        retry = self._candidate("retry.tmp", b"new")
        pending = self.parent.begin_publish(
            retry,
            destination.name,
            mode=PublishMode.REPLACE_UNDER_LOCK,
            lease=lease,
        )
        try:
            self.assertEqual(pending.retained_destination().read_all(), b"new")
            self.assertEqual(pending.terminal_reproof(), pending.preliminary_facts())
        finally:
            pending.close()
            lease.close()

    def test_replace_requires_live_same_parent_lease(self) -> None:
        destination = self.root_path / "destination.bin"
        destination.write_bytes(b"old")
        lease = self._lease()
        candidate = self._candidate("candidate.tmp", b"new")
        pending = self.parent.begin_publish(
            candidate,
            destination.name,
            mode=PublishMode.REPLACE_UNDER_LOCK,
            lease=lease,
        )
        try:
            self.assertEqual(pending.retained_destination().read_all(), b"new")
            self.assertEqual(pending.terminal_reproof(), pending.preliminary_facts())
        finally:
            pending.close()
            lease.close()

        foreign_directory = self.root_path / "foreign"
        foreign_directory.mkdir()
        foreign_parent = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("foreign/placeholder.bin"),
        )
        foreign_lease = self.adapter.acquire(
            foreign_parent,
            "resource.lock",
            LOCK_PAYLOAD,
            LockPolicy(LockWait.FAIL_FAST),
        )
        candidate = self._candidate("foreign-lease.tmp", b"rejected")
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.parent.begin_publish(
                    candidate,
                    destination.name,
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=foreign_lease,
                )
            _assert_platform_error(self, caught, PlatformFileErrorCode.LOCK_UNAVAILABLE)
        finally:
            foreign_lease.close()
            foreign_parent.close()

    def test_uncooperative_swap_attempt_is_blocked_and_requires_recovery(self) -> None:
        replacement = self.root_path / "replacement.bin"
        replacement.write_bytes(b"foreign")
        attempted = False

        def swap(phase: str) -> None:
            nonlocal attempted
            if phase == "publish_before_destination_reopen" and not attempted:
                attempted = True
                os.replace(replacement, self.root_path / "destination.bin")

        adapter = WindowsPlatformAdapter(_fault_injector=swap)
        root = adapter.bind_root(self.root_path)
        parent = adapter.bind_parent(root, PureWindowsPath("placeholder.bin"))
        candidate = parent.create_candidate("candidate.tmp", private=False)
        candidate.write_all(b"expected")
        candidate.flush_content()
        try:
            with self.assertRaises(PlatformFileError) as caught:
                parent.begin_publish(
                    candidate,
                    "destination.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            _assert_platform_error(self, caught, PlatformFileErrorCode.RECOVERY_REQUIRED)
            self.assertTrue(attempted)
            self.assertEqual((self.root_path / "destination.bin").read_bytes(), b"expected")
            self.assertEqual(replacement.read_bytes(), b"foreign")
        finally:
            parent.close()
            root.close()

    def test_post_rename_fault_requires_recovery_and_candidate_closes(self) -> None:
        def fail(phase: str) -> None:
            if phase == "publish_after_rename":
                raise RuntimeError("must not escape")

        adapter = WindowsPlatformAdapter(_fault_injector=fail)
        root = adapter.bind_root(self.root_path)
        parent = adapter.bind_parent(root, PureWindowsPath("placeholder.bin"))
        candidate = parent.create_candidate("candidate.tmp", private=False)
        candidate.write_all(b"new")
        candidate.flush_content()
        try:
            with self.assertRaises(PlatformFileError) as caught:
                parent.begin_publish(
                    candidate,
                    "destination.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
            _assert_platform_error(self, caught, PlatformFileErrorCode.RECOVERY_REQUIRED)
            self.assertTrue(candidate.closed)
        finally:
            parent.close()
            root.close()

    def test_private_candidate_remains_provable_after_publish(self) -> None:
        candidate = self._candidate("candidate.tmp", b"private", private=True)
        pending = self.parent.begin_publish(
            candidate,
            "destination.bin",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        evidence = self.adapter.prove_private(pending.retained_destination())
        evidence.close()
        pending.close()


if __name__ == "__main__":
    unittest.main()
