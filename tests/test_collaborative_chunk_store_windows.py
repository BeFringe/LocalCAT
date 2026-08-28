"""WA-02 real Windows source-process and Chunk publication acceptance."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from collaborative_chunk_contracts import ChunkError
from collaborative_chunk_store import CollaborativeChunkStore
from platform_fs import compose_platform_file_backend
from project_workspace_identity import issue_project_id


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "windows_chunk_store_worker.py"
PROJECT_ID = issue_project_id(b"S" * 32)
TARGET_NAME = "project.chunks.json"
LOCK_NAME = f".{TARGET_NAME}.lock-v1"
CANDIDATE_NAME = f".{TARGET_NAME}.candidate-v1"
FAULT_PHASES = (
    "candidate_after_create",
    "candidate_after_write",
    "candidate_after_flush",
    "lkg_after_publish",
    "journal_after_arm",
    "target_before_publish",
    "target_after_publish",
    "target_after_readback",
    "owner_after_commit",
    "terminal_after_reproof",
    "target_after_close",
)
NEW_PHASES = frozenset(
    {
        "target_after_publish",
        "target_after_readback",
        "owner_after_commit",
        "terminal_after_reproof",
        "target_after_close",
    }
)


class WindowsChunkStoreAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.platform != "win32":
            self.fail("WA-02 Windows acceptance must run on Windows")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def _command(self, *args: object) -> list[str]:
        return [sys.executable, str(WORKER), *(str(arg) for arg in args)]

    def _run(self, *args: object) -> dict[str, object]:
        result_path = Path(args[-1])
        completed = subprocess.run(
            self._command(*args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=40,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        return json.loads(result_path.read_text(encoding="utf-8"))

    @staticmethod
    def _wait_for(path: Path, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 30.0
        while not path.exists():
            if process.poll() is not None:
                raise AssertionError(f"worker exited before marker: {process.returncode}")
            if time.monotonic() >= deadline:
                raise AssertionError("worker marker timeout")
            time.sleep(0.01)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        terminate.restype = ctypes.c_int32
        if not terminate(int(process._handle), 87):
            raise ctypes.WinError(ctypes.get_last_error())
        process.wait(timeout=10)

    def _seed(self, root: Path) -> None:
        root.mkdir()
        result = self.base / f"seed-{root.name}.json"
        self.assertEqual(self._run("seed", root, result)["outcome"], "ok")

    def _store(self, root: Path) -> CollaborativeChunkStore:
        return CollaborativeChunkStore(
            root,
            TARGET_NAME,
            project_id=PROJECT_ID,
            platform_backend=compose_platform_file_backend(root),
        )

    def test_source_qt_startup_does_not_import_posix_adapter(self) -> None:
        result = self.base / "startup.json"
        outcome = self._run("qt-startup", self.base / "app-data", result)
        self.assertEqual(outcome, {"outcome": "ok", "platform": "win32"})

    def test_two_creators_serialize_and_one_stale_writer_fails_closed(self) -> None:
        root = self.base / "race"
        root.mkdir()
        barrier = self.base / "go"
        processes = []
        results = []
        try:
            for index in range(2):
                ready = self.base / f"ready-{index}"
                result = self.base / f"result-{index}.json"
                process = subprocess.Popen(
                    self._command("race-create", root, ready, barrier, result),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                processes.append((process, ready, result))
            for process, ready, _result in processes:
                self._wait_for(ready, process)
            barrier.write_bytes(b"go")
            for process, _ready, result in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(
                    process.returncode,
                    0,
                    (stdout + stderr).decode("utf-8", errors="replace"),
                )
                results.append(json.loads(result.read_text(encoding="utf-8")))
        finally:
            for process, _ready, _result in processes:
                if process.poll() is None:
                    process.kill()
        self.assertEqual(
            sorted(result["outcome"] for result in results),
            ["CHUNK.DESTINATION_STALE", "ok"],
        )
        state = self._store(root).load()
        self.assertIsNotNone(state)
        self.assertEqual(len(state.audit_records), 1)

    def test_unknown_lock_payload_is_not_rewritten(self) -> None:
        root = self.base / "unknown-lock"
        self._seed(root)
        hostile = b"unknown lock payload"
        lock_path = root / LOCK_NAME
        lock_path.write_bytes(hostile)
        with self.assertRaises(ChunkError) as caught:
            self._store(root).load()
        self.assertEqual(caught.exception.code, "CHUNK.METADATA_UNAVAILABLE")
        self.assertEqual(lock_path.read_bytes(), hostile)

    def test_terminate_process_matrix_recovers_exact_old_or_new_state(self) -> None:
        for phase in FAULT_PHASES:
            with self.subTest(phase=phase):
                root = self.base / phase
                self._seed(root)
                ready = self.base / f"{phase}.ready"
                process = subprocess.Popen(
                    self._command("fault-rename", root, phase, ready),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    self._wait_for(ready, process)
                    if phase == "candidate_after_create":
                        with self.assertRaises(PermissionError):
                            (root / CANDIDATE_NAME).open("rb")
                    if phase == "target_after_publish":
                        replacement = root / "replacement.tmp"
                        replacement.write_bytes(b"hostile replacement")
                        with self.assertRaises(PermissionError):
                            os.replace(replacement, root / TARGET_NAME)
                    self._terminate(process)
                finally:
                    if process.poll() is None:
                        process.kill()
                report = self._store(root).recover()
                self.assertIn(report.outcome, {"rolled_back", "rolled_forward"})
                self.assertIsNotNone(report.state)
                name = report.state.active_snapshot.chunks[0].name
                self.assertEqual(name, "new" if phase in NEW_PHASES else "old")
                self.assertEqual(self._store(root).load(), report.state)
                for suffix in (
                    "candidate-v1",
                    "lkg-v1",
                    "lkg-v1.tmp",
                    "journal-v1",
                    "journal-v1.tmp",
                ):
                    self.assertFalse((root / f".{TARGET_NAME}.{suffix}").exists())

    def test_owner_kill_releases_lock_to_waiting_process(self) -> None:
        root = self.base / "owner-kill"
        self._seed(root)
        ready = self.base / "owner.ready"
        owner = subprocess.Popen(
            self._command("fault-rename", root, "journal_after_arm", ready),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        waiter_result = self.base / "waiter.json"
        waiter = None
        try:
            self._wait_for(ready, owner)
            waiter = subprocess.Popen(
                self._command("load", root, waiter_result),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.4)
            self.assertIsNone(waiter.poll())
            self._terminate(owner)
            waiter.wait(timeout=20)
            self.assertEqual(waiter.returncode, 0)
        finally:
            for process in (owner, waiter):
                if process is not None and process.poll() is None:
                    process.kill()
        self.assertEqual(
            json.loads(waiter_result.read_text(encoding="utf-8"))["outcome"],
            "CHUNK.RECOVERY_REQUIRED",
        )
        recovered = self._store(root).recover()
        self.assertEqual(recovered.state.active_snapshot.chunks[0].name, "old")


if __name__ == "__main__":
    unittest.main()
