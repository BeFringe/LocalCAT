from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_WORKER = Path(__file__).with_name("windows_resource_source_worker.py")


class ResourceWindowsSourceLaneTests(unittest.TestCase):
    def _run_worker(self, *arguments: object) -> dict[str, object]:
        worker_cwd = Path(str(arguments[1])) / "non-repository-cwd"
        worker_cwd.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(_WORKER), *(str(value) for value in arguments)],
            check=False,
            capture_output=True,
            text=True,
            cwd=worker_cwd,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def _run_crash_worker(self, *arguments: object) -> None:
        worker_cwd = Path(str(arguments[1])) / "non-repository-crash-cwd"
        worker_cwd.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(_WORKER), *(str(value) for value in arguments)],
            check=False,
            capture_output=True,
            text=True,
            cwd=worker_cwd,
        )
        self.assertEqual(completed.returncode, 91, completed.stderr)

    def test_process_restart_cold_reopens_direct_package_import_and_ledgers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            produced = self._run_worker("produce", root)
            reopened = self._run_worker("reopen", root)

        self.assertEqual(reopened["direct_digest"], produced["direct_digest"])
        self.assertEqual(reopened["package_digest"], produced["package_digest"])
        self.assertEqual(reopened["payload_digest"], produced["payload_digest"])
        self.assertEqual(reopened["imported_digest"], produced["payload_digest"])
        self.assertEqual(
            reopened["imported_resource_id"],
            produced["imported_resource_id"],
        )
        self.assertEqual(reopened["source_receipts"], 2)
        self.assertEqual(reopened["source_pending"], 0)
        self.assertEqual(reopened["destination_receipts"], 1)
        self.assertEqual(reopened["destination_pending"], 0)

    def test_competing_publication_resumes_after_lock_owner_process_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")
            holder_cwd = root / "holder-cwd"
            publisher_cwd = root / "publisher-cwd"
            holder_cwd.mkdir()
            publisher_cwd.mkdir()
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(_WORKER),
                    "hold-lock",
                    str(root),
                    destination.name,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=holder_cwd,
            )
            publisher: subprocess.Popen[str] | None = None
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "READY")
                publisher = subprocess.Popen(
                    [
                        sys.executable,
                        str(_WORKER),
                        "publish",
                        str(root),
                        candidate.name,
                        destination.name,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=publisher_cwd,
                )
                with self.assertRaises(subprocess.TimeoutExpired):
                    publisher.wait(timeout=0.5)
                holder.terminate()
                _holder_stdout, holder_stderr = holder.communicate(timeout=10)
                self.assertEqual(holder.returncode, 1, holder_stderr)
                stdout, stderr = publisher.communicate(timeout=20)
                self.assertEqual(publisher.returncode, 0, stderr)
                publication = json.loads(stdout)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=10)
                if publisher is not None and publisher.poll() is None:
                    publisher.kill()
                    publisher.communicate(timeout=10)

            self.assertEqual(
                publication["before"],
                hashlib.sha256(b"old").hexdigest(),
            )
            self.assertEqual(
                publication["after"],
                hashlib.sha256(b"new").hexdigest(),
            )
            self.assertEqual(destination.read_bytes(), b"new")

    @unittest.skipUnless(sys.platform == "win32", "Windows publication phases")
    def test_process_termination_at_publish_boundaries_cold_reopens_old_or_new(
        self,
    ) -> None:
        phases = (
            ("publish_before_rename", "old"),
            ("publish_after_rename", "new"),
            ("publish_after_final_facts", "new"),
            ("publish_before_destination_reopen", "new"),
            ("publish_after_destination_reopen", "new"),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for phase, target_state in phases:
                with self.subTest(phase=phase):
                    case = root / phase
                    case.mkdir()
                    self._run_crash_worker("crash-direct-platform", case, phase)
                    inspected = self._run_worker("inspect-crash", case)
                    self.assertEqual(inspected["target_state"], target_state)
                    self.assertEqual(inspected["pending_phase"], "armed")
                    self.assertEqual(inspected["disposition"], "manual_required")
                    self.assertEqual(inspected["recovery"], "retained")
                    self.assertEqual(inspected["pending_after"], 1)
                    self.assertEqual(inspected["receipts_after"], 0)
                    if target_state == "new":
                        self.assertEqual(
                            inspected["target_digest"],
                            inspected["expected_digest"],
                        )

    @unittest.skipUnless(sys.platform == "win32", "Windows replace phases")
    def test_process_termination_during_replace_preserves_exact_old_or_new_and_lkg(
        self,
    ) -> None:
        phases = (
            ("publish_before_rename", "old", 1),
            ("publish_after_rename", "new", 0),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for phase, target_state, candidate_count in phases:
                with self.subTest(phase=phase):
                    case = root / phase
                    case.mkdir()
                    self._run_crash_worker(
                        "crash-direct-platform-replace",
                        case,
                        phase,
                    )
                    inspected = self._run_worker("inspect-crash", case)
                    self.assertEqual(inspected["target_state"], target_state)
                    self.assertEqual(inspected["pending_phase"], "armed")
                    self.assertEqual(inspected["disposition"], "manual_required")
                    self.assertEqual(inspected["recovery"], "retained")
                    self.assertEqual(inspected["pending_after"], 1)
                    self.assertEqual(inspected["receipts_after"], 1)
                    self.assertEqual(inspected["lkg_after"], 1)
                    self.assertEqual(
                        inspected["candidate_after"],
                        candidate_count,
                    )
                    expected = (
                        inspected["before_digest"]
                        if target_state == "old"
                        else inspected["expected_digest"]
                    )
                    self.assertEqual(inspected["target_digest"], expected)

    @unittest.skipUnless(sys.platform == "win32", "Windows receipt phases")
    def test_process_termination_distinguishes_ready_from_durable_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for occurrence in (2, 3):
                with self.subTest(occurrence=occurrence):
                    case = root / f"ledger-{occurrence}"
                    case.mkdir()
                    self._run_crash_worker("crash-direct-ledger", case, occurrence)
                    inspected = self._run_worker("inspect-crash", case)
                    self.assertEqual(inspected["target_state"], "new")
                    self.assertEqual(inspected["pending_phase"], "receipt_ready")
                    if occurrence == 2:
                        self.assertEqual(inspected["disposition"], "manual_required")
                        self.assertEqual(inspected["recovery"], "retained")
                        self.assertEqual(inspected["pending_after"], 1)
                        self.assertEqual(inspected["receipts_after"], 0)
                    else:
                        self.assertEqual(
                            inspected["disposition"],
                            "complete_available",
                        )
                        self.assertEqual(inspected["recovery"], "completed")
                        self.assertEqual(inspected["pending_after"], 0)
                        self.assertEqual(inspected["receipts_after"], 1)
                    self.assertEqual(
                        inspected["target_digest"],
                        inspected["expected_digest"],
                    )


if __name__ == "__main__":
    unittest.main()
