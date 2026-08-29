"""WA-02 package-coupled Windows source acceptance on real ProjectPackage APIs."""

from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

from project_workspace_intake import SelectedProjectDocumentsRequest
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from collaborative_chunk_contracts import EMPTY_CHUNK_AUDIT_DIGEST


WORKER = Path(__file__).with_name("windows_chunk_store_worker.py")
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
INITIAL_FAULT_PHASES = tuple(
    phase for phase in FAULT_PHASES if phase != "lkg_after_publish"
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


def _write_document(path: Path, name: str, source: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "shared",
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


@unittest.skipUnless(sys.platform == "win32", "WA-02 package acceptance requires Windows")
class WindowsChunkProjectPackageAcceptanceTests(unittest.TestCase):
    def _run(self, cwd: Path, *args: Path | str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(WORKER), *(str(value) for value in args)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            (completed.stdout + completed.stderr).decode("utf-8", errors="replace"),
        )
        result = Path(args[-1])
        return json.loads(result.read_text(encoding="utf-8"))

    @staticmethod
    def _wait_for(path: Path, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 30.0
        while not path.exists() or path.stat().st_size == 0:
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
        if not terminate(int(process._handle), 91):
            raise ctypes.WinError(ctypes.get_last_error())
        process.wait(timeout=10)

    def _run_fault_matrix(
        self,
        package: Path,
        root: Path,
        before_digest: str,
        baseline: dict[str, object],
        *,
        initial: bool,
    ) -> None:
        phases = INITIAL_FAULT_PHASES if initial else FAULT_PHASES
        for phase in phases:
            with self.subTest(initial=initial, phase=phase):
                app_data = root / ("initial-" if initial else "existing-") / phase
                app_data.mkdir(parents=True)
                seed_result = root / f"{'initial' if initial else 'existing'}-{phase}-seed.json"
                if not initial:
                    seeded = self._run(
                        root,
                        "package-create",
                        package,
                        app_data,
                        seed_result,
                    )
                    baseline = seeded["facts"]
                ready = root / f"{'initial' if initial else 'existing'}-{phase}.ready"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(WORKER),
                        "package-initial-fault" if initial else "package-fault",
                        str(package),
                        str(app_data),
                        phase,
                        str(ready),
                    ],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                did_terminate = False
                try:
                    self._wait_for(ready, process)
                    marker = json.loads(ready.read_text(encoding="utf-8"))
                    self.assertEqual(marker["phase"], phase)
                    project_id = baseline["workspace"]["project_id"]
                    metadata_root = app_data / "collaborative-chunks" / project_id
                    target = metadata_root / "chunks.json"
                    if not initial and phase == "candidate_after_create":
                        with self.assertRaises(PermissionError):
                            (metadata_root / ".chunks.json.candidate-v1").open("rb")
                    if not initial and phase == "target_after_publish":
                        replacement = metadata_root / "replacement.tmp"
                        replacement.write_bytes(b"hostile replacement")
                        try:
                            with self.assertRaises(PermissionError):
                                os.replace(replacement, target)
                        finally:
                            replacement.unlink(missing_ok=True)
                finally:
                    if process.poll() is None:
                        self._terminate(process)
                        did_terminate = True
                    process.stderr.close()
                self.assertTrue(did_terminate)
                self.assertEqual(process.returncode, 91)
                recovered = self._run(
                    root,
                    "package-recover",
                    package,
                    app_data,
                    root / f"{'initial' if initial else 'existing'}-{phase}-recovered.json",
                )
                facts = recovered["facts"]
                self.assertEqual(facts["metadata_digest"], recovered["metadata_digest"])
                self.assertEqual(facts["package_sha256"], before_digest)
                self.assertEqual(facts["workspace"], baseline["workspace"])
                new_expected = phase in NEW_PHASES
                target_expected = (not initial) or new_expected
                self.assertEqual(
                    facts["metadata_files"],
                    (
                        [".chunks.json.lock-v1", "chunks.json"]
                        if target_expected
                        else [".chunks.json.lock-v1"]
                    ),
                )
                metadata = facts["metadata"]
                if initial:
                    if new_expected:
                        record = metadata["audit_records"][0]
                        self._assert_preview_receipt(marker["preview"], record)
                        self.assertEqual(
                            record["previous_audit_head_digest"],
                            EMPTY_CHUNK_AUDIT_DIGEST,
                        )
                        expected_active = {
                            "schema_version": 1,
                            "namespace": "localcat.collaboration.chunks.v1",
                            "chunk_plan_id": marker["preview"]["chunk_plan_id"],
                            "project_id": marker["preview"]["project_id"],
                            "revision": marker["preview"]["published_revision"],
                            "segment_universe_digest": marker[
                                "segment_universe_digest"
                            ],
                            "audit_head_digest": record["record_digest"],
                            "plan_digest": marker["preview"]["after_plan_digest"],
                            "chunks": [
                                {
                                    "chunk_id": marker["preview"][
                                        "created_chunk_ids"
                                    ][0],
                                    "name": "Windows initial creator",
                                    "order": 0,
                                    "members": sorted(
                                        marker["selected_members"],
                                        key=lambda item: (
                                            item["document_id"],
                                            item["local_segment_id"],
                                        ),
                                    ),
                                    "assignee": None,
                                }
                            ],
                        }
                        self.assertEqual(metadata["active_snapshot"], expected_active)
                    else:
                        self.assertEqual(
                            metadata,
                            {
                                "active_snapshot": None,
                                "active_name": None,
                                "audit_head": None,
                                "audit_records": [],
                                "chunk_count": 0,
                            },
                        )
                elif new_expected:
                    record = metadata["audit_records"][-1]
                    self._assert_preview_receipt(marker["preview"], record)
                    self.assertEqual(
                        metadata["audit_records"][:-1],
                        baseline["metadata"]["audit_records"],
                    )
                    self.assertEqual(len(metadata["audit_records"]), 2)
                    self.assertEqual(
                        metadata["audit_records"][-1]["previous_audit_head_digest"],
                        baseline["metadata"]["audit_head"],
                    )
                    self.assertEqual(
                        metadata["audit_head"],
                        record["record_digest"],
                    )
                    expected_active = json.loads(
                        json.dumps(baseline["metadata"]["active_snapshot"])
                    )
                    expected_active["revision"] = marker["preview"][
                        "published_revision"
                    ]
                    expected_active["plan_digest"] = marker["preview"][
                        "after_plan_digest"
                    ]
                    expected_active["audit_head_digest"] = record[
                        "record_digest"
                    ]
                    self.assertEqual(
                        marker["preview"]["affected_chunk_ids"],
                        [expected_active["chunks"][0]["chunk_id"]],
                    )
                    expected_active["chunks"][0]["name"] = "Windows renamed"
                    self.assertEqual(metadata["active_snapshot"], expected_active)
                else:
                    self.assertEqual(metadata, baseline["metadata"])
                if new_expected:
                    self.assertEqual(recovered["outcome"], "rolled_forward")
                else:
                    self.assertEqual(recovered["outcome"], "rolled_back")
                self.assertEqual(
                    facts["metadata_files"],
                    sorted(set(facts["metadata_files"])),
                )

    def _assert_preview_receipt(
        self,
        preview: dict[str, object],
        record: dict[str, object],
    ) -> None:
        self.assertEqual(record["outcome"], "published")
        receipt = record["receipt"]
        for name, expected in preview.items():
            self.assertEqual(receipt[name], expected, name)
        self.assertEqual(receipt["audit_record_digest"], record["record_digest"])
        self.assertEqual(
            receipt["actor_ref"],
            {
                "authority_id": "localcat-local-reference",
                "subject_id": "device-workflow",
            },
        )

    def test_two_process_chunk_journey_uses_real_movable_project_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa02-package-") as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            first = sources / "a.json"
            second = sources / "b.json"
            _write_document(first, "A", "alpha")
            _write_document(second, "B", "beta")
            package = root / "project.localcat-project"
            controller, _composition = _compose_editor_controller(
                ResourceRepository(root / "creator-app-data")
            )
            created = controller.create_workspace_project_from_selected_files(
                sources,
                (first, second),
                SelectedProjectDocumentsRequest("WA-02", "en", "zh-CN"),
                package,
            )
            before = package.read_bytes()
            before_digest = hashlib.sha256(before).hexdigest()
            app_data = root / "chunk-app-data"
            non_repository_cwd = root / "outside-cwd"
            non_repository_cwd.mkdir()

            created_result = self._run(
                non_repository_cwd,
                "package-create",
                package,
                app_data,
                root / "created.json",
            )
            self.assertEqual(created_result["project_id"], created.session.project.project_id)
            self.assertEqual(created_result["chunk_count"], 1)
            self.assertEqual(created_result["member_count"], 2)
            self.assertEqual(created_result["package_sha256"], before_digest)
            self.assertEqual(package.read_bytes(), before)

            moved = root / "moved-project.localcat-project"
            os.replace(package, moved)
            reopened_result = self._run(
                non_repository_cwd,
                "package-open",
                moved,
                app_data,
                root / "reopened.json",
            )
            self.assertEqual(reopened_result["project_id"], created.session.project.project_id)
            self.assertEqual(reopened_result["chunk_count"], 1)
            self.assertEqual(reopened_result["chunk_name"], "Windows package slice")
            self.assertEqual(reopened_result["member_count"], 2)
            self.assertEqual(reopened_result["package_sha256"], before_digest)
            self.assertEqual(moved.read_bytes(), before)

    def test_real_package_publish_faults_recover_with_exact_workspace_and_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa02-package-faults-") as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            first = sources / "a.json"
            second = sources / "b.json"
            _write_document(first, "A", "alpha")
            _write_document(second, "B", "beta")
            package = root / "project.localcat-project"
            controller, _composition = _compose_editor_controller(
                ResourceRepository(root / "creator-app-data")
            )
            created = controller.create_workspace_project_from_selected_files(
                sources,
                (first, second),
                SelectedProjectDocumentsRequest("WA-02 faults", "en", "zh-CN"),
                package,
            )
            before = package.read_bytes()
            before_digest = hashlib.sha256(before).hexdigest()
            baseline_result = self._run(
                root,
                "package-create",
                package,
                root / "baseline-app-data",
                root / "baseline.json",
            )
            baseline = baseline_result["facts"]
            self.assertEqual(baseline["workspace"]["project_id"], created.session.project.project_id)
            self._run_fault_matrix(
                package,
                root,
                before_digest,
                baseline,
                initial=False,
            )
            self._run_fault_matrix(
                package,
                root,
                before_digest,
                baseline,
                initial=True,
            )
            self.assertEqual(package.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
