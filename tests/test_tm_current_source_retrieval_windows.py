from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tm_retrieval_capability import (
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
)


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_WORKER = "tests.windows_tm_current_source_retrieval_worker"


def _remove_activation_quarantine(root: Path) -> None:
    quarantine = root / ".localcat-activation-quarantine-v1"
    if not quarantine.exists():
        return
    for attempt in quarantine.iterdir():
        for path in attempt.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt))
    os.rmdir("\\\\?\\" + str(quarantine))


def _run_worker(root: Path, mode: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", _WORKER, mode, str(root)],
        cwd=_SOURCE_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1200,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"worker {mode} failed with {completed.returncode}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(
            f"worker {mode} returned an invalid JSON stream: {lines!r}"
        )
    payload = json.loads(lines[0])
    if type(payload) is not dict:
        raise AssertionError("worker result must be one JSON object")
    return payload


@unittest.skipUnless(sys.platform == "win32", "Windows current-source matrix")
class WindowsCurrentSourceRetrievalTests(unittest.TestCase):
    def test_public_activation_supports_gate_d_nested_long_path(self) -> None:
        from tm_benchmark_gate import (
            _cleanup_windows_gate_d_run_tree,
            _create_windows_gate_d_run_tree,
        )
        from tm_benchmark_platform_io import create_windows_private_work_root
        from tm_contracts import CanonicalResourceIdentity, MigrationReport
        from tm_migration import TMMigrationService
        from tm_sqlite_store import ResourceStoreCoordinator

        outer = create_windows_private_work_root("localcat-gate-d-long-path-")
        tree = _create_windows_gate_d_run_tree(outer)
        root = tree.children["oracle_fts5"][0]
        source = root / "oracle.fixture.jsonl"
        source.write_bytes(b'{"source":"alpha","target":"beta"}\n')
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            "tm.benchmark.oracle.fts5",
            source,
        )
        coordinator = ResourceStoreCoordinator(
            canonical_store_id="store.tm.benchmark.oracle.fts5",
            resource_identity=identity,
        )
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id="store.tm.benchmark.oracle.fts5",
            coordinator=coordinator,
        )
        try:
            outcome = service.activate_initial(source, identity.resource_id)
            self.assertIs(type(outcome), MigrationReport, repr(outcome))
            assert isinstance(outcome, MigrationReport)
            self.assertEqual(outcome.activated_generation, 0)
            self.assertEqual(outcome.migrated_count, 1)
        finally:
            _cleanup_windows_gate_d_run_tree(
                tree,
                process_fts5=None,
                process_fallback=None,
                oracle_fts5=None,
                oracle_fallback=None,
            )
            if outer.exists() and not any(outer.iterdir()):
                outer.rmdir()

    def test_public_activation_cold_reopen_and_real_gate_d_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "tm-owner"
            try:
                activated = _run_worker(root, "activate")
                queried = _run_worker(root, "query")
            finally:
                _remove_activation_quarantine(root)

        self.assertNotEqual(activated["pid"], queried["pid"])
        self.assertEqual(activated["generation"], 0)
        self.assertEqual(activated["record_count"], 4)
        self.assertEqual(
            activated["store_health"],
            {"generation": 0, "index_kind": "FTS5_TRIGRAM", "record_count": 4},
        )

        self.assertEqual(queried["mode"], "query")
        self.assertEqual(
            queried["cold_store"],
            {
                "canonical_active": True,
                "generation": 0,
                "index_kind": "FTS5_TRIGRAM",
                "record_count": 4,
            },
        )
        self.assertTrue(queried["sqlite_runtime"]["fts5_available"])
        self.assertTrue(queried["gate_c"]["context_available"])
        self.assertFalse(queried["gate_c"]["fuzzy_available"])

        exact = queried["queries"]["exact"]
        context = queried["queries"]["context"]
        fuzzy = queried["queries"]["fuzzy"]
        self.assertEqual(exact["failures"], [])
        self.assertIn(
            ("EXACT", "exact target"),
            [
                (result["match_type"], result["target"])
                for result in exact["results"]
            ],
        )
        self.assertEqual(context["failures"], [])
        self.assertIn(
            ("CONTEXT", "context target"),
            [
                (result["match_type"], result["target"])
                for result in context["results"]
            ],
        )

        gate_d = queried["gate_d"]
        self.assertEqual(gate_d["started_state"], "RUNNING")
        self.assertEqual(gate_d["finished_state"], "SUCCEEDED")
        self.assertIsNone(gate_d["safe_code"])
        fts5_path_available = fuzzy["metadata"][0]["fuzzy_available"]
        if fts5_path_available:
            self.assertTrue(gate_d["display"]["fuzzy_available"])
            self.assertEqual(fuzzy["failures"], [])
            self.assertEqual(fuzzy["metadata"][0]["index_kind"], "FTS5_TRIGRAM")
            self.assertTrue(fuzzy["metadata"][0]["fuzzy_available"])
            self.assertIn(
                ("FUZZY", "fuzzy target"),
                [
                    (result["match_type"], result["target"])
                    for result in fuzzy["results"]
                ],
            )
        else:
            self.assertEqual(
                fuzzy["metadata"][0]["fuzzy_unavailable_code"],
                RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
            )
            self.assertNotIn(
                "FUZZY",
                [result["match_type"] for result in fuzzy["results"]],
            )


if __name__ == "__main__":
    unittest.main()
