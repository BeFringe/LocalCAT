"""Deterministic source-snapshot tests for the Windows C3B evidence lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import run_windows_c3b_capability_gate as gate
from tools.windows_c3b_source_snapshot import (
    C3B_SOURCE_KEYS,
    SNAPSHOT_DOMAIN,
    source_snapshot_sha256,
)


class WindowsC3BSourceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "nested").mkdir()
        (self.root / "a.py").write_bytes(b"a\x00body\n")
        (self.root / "nested" / "b.json").write_bytes(b'{"b":2}\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_uses_sorted_domain_separated_path_and_bytes(self) -> None:
        digest = hashlib.sha256()
        digest.update(SNAPSHOT_DOMAIN)
        for key in ("a.py", "nested/b.json"):
            key_bytes = key.encode("utf-8")
            body = self.root.joinpath(*key.split("/")).read_bytes()
            digest.update(b"path\x00")
            digest.update(len(key_bytes).to_bytes(8, "big"))
            digest.update(key_bytes)
            digest.update(b"bytes\x00")
            digest.update(len(body).to_bytes(8, "big"))
            digest.update(body)
        self.assertEqual(
            source_snapshot_sha256(
                self.root,
                ("nested/b.json", "a.py"),
            ),
            digest.hexdigest(),
        )

    def test_byte_tamper_changes_snapshot(self) -> None:
        keys = ("a.py", "nested/b.json")
        before = source_snapshot_sha256(self.root, keys)
        (self.root / "a.py").write_bytes(b"changed\n")
        self.assertNotEqual(source_snapshot_sha256(self.root, keys), before)

    def test_path_tamper_changes_snapshot_even_when_bytes_match(self) -> None:
        before = source_snapshot_sha256(self.root, ("a.py",))
        (self.root / "renamed.py").write_bytes((self.root / "a.py").read_bytes())
        self.assertNotEqual(
            source_snapshot_sha256(self.root, ("renamed.py",)),
            before,
        )

    def test_default_inventory_covers_runtime_tests_workers_and_evidence(self) -> None:
        required = {
            "platform_fs_contracts.py",
            "platform_fs_windows.py",
            "windows_file_api.py",
            "tests/test_platform_fs_windows_lock.py",
            "tests/windows_publish_recovery_worker.py",
            "tools/run_windows_c3b_capability_gate.py",
            "tools/run_windows_documented_publish_evidence.py",
            "packaging/windows/evidence-scenarios/windows-documented-publish-v1.json",
        }
        self.assertTrue(required.issubset(C3B_SOURCE_KEYS))


class WindowsC3BEvidenceAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_snapshot = "a" * 64
        self.commit = "b" * 40

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_elevated(self, source_snapshot: str) -> tuple[Path, str]:
        rows = []
        for index, test_name in enumerate(gate.ELEVATED_TESTS):
            row_id = ("elevated-private-positive", "elevated-owner-tamper")[index]
            log_key = f"logs/{row_id}.log"
            log_path = self.root.joinpath(*log_key.split("/"))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{test_name} ... ok\n", encoding="utf-8")
            rows.append(
                {
                    "coverage": ["private"],
                    "errors": 0,
                    "failures": 0,
                    "id": row_id,
                    "log": log_key,
                    "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                    "skipped": 0,
                    "test": test_name,
                    "tests_run": 1,
                    "verdict": "PASS",
                }
            )
        report = {
            "environment": {},
            "repository_commit": self.commit,
            "rows": rows,
            "schema": gate.ELEVATED_SCHEMA,
            "source_snapshot_sha256": source_snapshot,
            "status": "PASS",
        }
        path = self.root / "c3b-elevated.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_reboot(self, source_snapshot: str) -> dict[str, object]:
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "reboot-resume.stdout.log").write_text(
            json.dumps(
                {
                    "reboot": "PASS",
                    "source_snapshot_sha256": source_snapshot,
                }
            ),
            encoding="utf-8",
        )
        (self.root / "matrix.json").write_text(
            json.dumps(
                {
                    "scenarios": [
                        {"id": "normal-os-reboot", "verdict": "PASS"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.root / "windows-fs-lock-adaptation-checklist.md").write_text(
            f"- C3B source snapshot SHA-256: {source_snapshot}\n",
            encoding="utf-8",
        )
        return {
            "commands": [
                {
                    "id": "reboot-resume",
                    "status": "PASS",
                    "stdout_log": "logs/reboot-resume.stdout.log",
                }
            ],
            "outputs": {
                "matrix": {"path": "matrix.json"},
                "windows_fs_lock_checklist": {
                    "path": "windows-fs-lock-adaptation-checklist.md"
                },
            },
            "run": {"status": "PASS"},
        }

    def test_elevated_aggregation_accepts_matching_source_snapshot(self) -> None:
        path, digest = self._write_elevated(self.source_snapshot)
        rows = gate._load_elevated_evidence(
            path,
            self.commit,
            digest,
            self.source_snapshot,
        )
        self.assertEqual([row["verdict"] for row in rows], ["PASS", "PASS"])

    def test_elevated_aggregation_rejects_source_snapshot_tamper(self) -> None:
        path, digest = self._write_elevated("c" * 64)
        with self.assertRaisesRegex(ValueError, "source snapshot mismatch"):
            gate._load_elevated_evidence(
                path,
                self.commit,
                digest,
                self.source_snapshot,
            )

    def test_reboot_aggregation_accepts_matching_manifest_and_log_snapshot(self) -> None:
        manifest = self._write_reboot(self.source_snapshot)
        with mock.patch.object(gate, "validate_evidence_bundle", return_value=manifest):
            row = gate._load_reboot_evidence(
                self.root,
                self.commit,
                "d" * 64,
                self.source_snapshot,
            )
        self.assertEqual(row["verdict"], "PASS")

    def test_reboot_aggregation_rejects_log_snapshot_tamper(self) -> None:
        manifest = self._write_reboot("c" * 64)
        with (
            mock.patch.object(gate, "validate_evidence_bundle", return_value=manifest),
            self.assertRaisesRegex(ValueError, "log source snapshot mismatch"),
        ):
            gate._load_reboot_evidence(
                self.root,
                self.commit,
                "d" * 64,
                self.source_snapshot,
            )

    def test_reboot_aggregation_rejects_manifest_output_snapshot_tamper(self) -> None:
        manifest = self._write_reboot(self.source_snapshot)
        (self.root / "windows-fs-lock-adaptation-checklist.md").write_text(
            f"- C3B source snapshot SHA-256: {'c' * 64}\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(gate, "validate_evidence_bundle", return_value=manifest),
            self.assertRaisesRegex(ValueError, "manifest output source snapshot mismatch"),
        ):
            gate._load_reboot_evidence(
                self.root,
                self.commit,
                "d" * 64,
                self.source_snapshot,
            )


if __name__ == "__main__":
    unittest.main()
