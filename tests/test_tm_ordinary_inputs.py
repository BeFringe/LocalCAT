"""Real Core aggregators consume digests; parsers consume actual data."""
import hashlib
import platform
import sqlite3
import sys
import unittest
from unittest.mock import patch

from frozen_candidate import load_candidate, write_candidate_record, canonical_json, INPUT_RECORD
from tests import test_windows_ordinary_candidate as candidate_tests
from tm_gate_inputs import _compose_ordinary_input_owner, _input_digest, _read_input_bytes
from tm_gate_a import aggregate_paths_digest, canonical_digest


class OrdinaryCoreInputTests(unittest.TestCase):
    def setUp(self):
        fixture = candidate_tests.OrdinaryCandidateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        runtime_files = {}
        for name in ("python314.dll", "sqlite3.dll", "_sqlite3.pyd"):
            (fixture.bundle / name).write_bytes(b"test runtime " + name.encode())
            runtime_files["_internal/" + name] = hashlib.sha256((fixture.bundle / name).read_bytes()).hexdigest()
        fixture.inputs["core_execution"] = {
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "pyinstaller": "6.22.2", "collection": "pyz", "optimization": sys.flags.optimize,
            "worker_inputs": {name: "a" * 64 for name in (
                "frozen_candidate.py", "frozen_ordinary_entry.py", "frozen_worker_entry.py", "frozen_worker_transport.py")},
            "runtime_files": runtime_files,
        }
        (fixture.bundle / INPUT_RECORD).write_bytes(canonical_json(fixture.inputs))
        write_candidate_record(fixture.root, {"repository_commit": "a" * 40})
        self.candidate = load_candidate(fixture.exe)

    def test_digest_aggregation_uses_single_hash_and_source_read_is_rejected(self):
        with patch("frozen_candidate.running_candidate", return_value=self.candidate):
            owner = _compose_ordinary_input_owner()
        self.addCleanup(owner.close)
        with owner.open_session() as session:
            source = session.input("owner.py")
            digest = hashlib.sha256(b"actual build input").hexdigest()
            self.assertEqual(_input_digest(source), digest)
            self.assertEqual(aggregate_paths_digest(session, ("owner.py",)),
                             canonical_digest([{"path": "owner.py", "sha256": digest}]))
            with self.assertRaises(RuntimeError):
                _read_input_bytes(source)
            self.assertEqual(session.read_bytes("fixture.json"), b'{"fixture": [1, 2, 3]}')
            self.assertEqual(session.consumed_ids, ("fixture.json", "owner.py"))
        with self.assertRaises(RuntimeError):
            _input_digest(source)

    def test_revocation_invalidates_digest_as_well_as_data(self):
        with patch("frozen_candidate.running_candidate", return_value=self.candidate):
            owner = _compose_ordinary_input_owner()
        self.addCleanup(owner.close)
        with self.assertRaises(RuntimeError):
            with owner.open_session() as session:
                owner._revoke_publication()
                _input_digest(session.input("owner.py"))

    def test_source_process_cannot_compose_ordinary_production(self):
        with self.assertRaises(RuntimeError):
            _compose_ordinary_input_owner()

    def test_consumed_data_change_is_detected_at_terminal(self):
        with patch("frozen_candidate.running_candidate", return_value=self.candidate):
            owner = _compose_ordinary_input_owner()
        self.addCleanup(owner.close)
        with self.assertRaises(RuntimeError):
            with owner.open_session() as session:
                session.read_bytes("fixture.json")
                (self.fixture.bundle / "fixture.json").write_bytes(b"changed after consumption")
                session.terminal_reproof()

    def test_missing_or_wrong_runtime_facts_cannot_enter_core_fingerprint(self):
        for facts in ({}, {**self.fixture.inputs["core_execution"], "sqlite": "foreign"}):
            self.fixture.inputs["core_execution"] = facts
            (self.fixture.bundle / INPUT_RECORD).write_bytes(canonical_json(self.fixture.inputs))
            write_candidate_record(self.fixture.root, {})
            candidate = load_candidate(self.fixture.exe)
            with patch("frozen_candidate.running_candidate", return_value=candidate), self.assertRaises(RuntimeError):
                _compose_ordinary_input_owner()


if __name__ == "__main__":
    unittest.main()
