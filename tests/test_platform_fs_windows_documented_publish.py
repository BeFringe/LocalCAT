"""Task 3.6 WindowsDocumentedPublishV1 recovery-contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest import mock

from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from platform_fs_windows import WindowsPlatformAdapter
from tests import windows_publish_recovery_worker as worker
from tools.run_windows_documented_publish_evidence import COMMANDS, CONTRACT_KEY
from windows_file_api import Win32CallError


def _assert_platform_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode,
) -> None:
    case.assertEqual(caught.exception.code, code.value)
    case.assertEqual(caught.exception.args, (code.value,))
    case.assertIsNone(caught.exception.__cause__)


@unittest.skipUnless(sys.platform == "win32", "Task 3.6 requires real Windows")
class WindowsDocumentedPublishRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_success_is_reproved_new_after_process_restart(self) -> None:
        result = worker.run_success(self.root_path / "success")
        self.assertEqual(
            result,
            {"recovery": "NEW", "schema": worker.SCHEMA},
        )

    def test_instruction_boundary_matrix_has_only_expected_recovery_states(self) -> None:
        result = worker.run_matrix(self.root_path / "instruction", "instruction")
        self.assertEqual(result["results"], worker.EXPECTED_RECOVERY)

    def test_process_termination_matrix_has_only_expected_recovery_states(self) -> None:
        result = worker.run_matrix(self.root_path / "termination", "terminate")
        self.assertEqual(result["results"], worker.EXPECTED_RECOVERY)

    def test_same_boot_session_keeps_normal_reboot_not_run(self) -> None:
        root = self.root_path / "reboot"
        prepared = worker.prepare_reboot(root, "same-boot-session")
        self.assertEqual(prepared["reboot"], "PREPARED")
        resumed = worker.resume_reboot(root, "same-boot-session")
        self.assertEqual(resumed["reboot"], "NOT_RUN")
        self.assertEqual(resumed["recovery"], "NOT_OBSERVED")
        self.assertEqual(
            resumed["source_snapshot_sha256"],
            prepared["source_snapshot_sha256"],
        )

    def test_reboot_resume_accepts_the_same_source_snapshot(self) -> None:
        root = self.root_path / "reboot-snapshot-positive"
        prepared = worker.prepare_reboot(root, "boot-before")
        resumed = worker.resume_reboot(root, "boot-after")
        self.assertEqual(resumed["reboot"], "PASS")
        self.assertEqual(resumed["recovery"], "NEW")
        self.assertEqual(
            resumed["source_snapshot_sha256"],
            prepared["source_snapshot_sha256"],
        )

    def test_reboot_resume_rejects_source_snapshot_tamper(self) -> None:
        root = self.root_path / "reboot-snapshot-tamper"
        with mock.patch.object(worker, "source_snapshot_sha256", return_value="a" * 64):
            worker.prepare_reboot(root, "boot-before")
        with (
            mock.patch.object(worker, "source_snapshot_sha256", return_value="b" * 64),
            self.assertRaisesRegex(RuntimeError, "reboot ticket authority mismatch"),
        ):
            worker.resume_reboot(root, "boot-after")

    def test_flush_api_failure_is_pre_arm_durability_unavailable(self) -> None:
        root = self.root_path / "flush-unavailable"
        root.mkdir()
        adapter = WindowsPlatformAdapter()
        bound_root = adapter.bind_root(root)
        parent = adapter.bind_parent(bound_root, PureWindowsPath("placeholder.bin"))
        candidate = parent.create_candidate("candidate.tmp", private=False)
        candidate.write_all(b"payload")
        original_checked_bool = parent._api.checked_bool

        def checked_bool(operation: str, function: object, *args: object) -> None:
            if operation == "FlushFileBuffers":
                raise Win32CallError(operation, 5)
            original_checked_bool(operation, function, *args)

        try:
            with (
                mock.patch.object(parent._api, "checked_bool", side_effect=checked_bool),
                self.assertRaises(PlatformFileError) as caught,
            ):
                candidate.flush_content()
            _assert_platform_error(
                self,
                caught,
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
            )
            self.assertFalse((root / "destination.bin").exists())
        finally:
            candidate.close()
            parent.close()
            bound_root.close()


class WindowsDocumentedPublishContractTests(unittest.TestCase):
    def test_scenario_contract_command_hashes_match_portable_commands(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        contract = json.loads((repository / CONTRACT_KEY).read_text(encoding="utf-8"))
        actual = {
            command["id"]: command["command_sha256"]
            for scenario in contract["scenarios"]
            for command in scenario["commands"]
        }
        expected = {
            command_id: hashlib.sha256(command.encode("utf-8")).hexdigest()
            for command_id, command in COMMANDS.items()
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
