"""Ordinary distribution facts: content and input digests stay distinct."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from frozen_candidate import (
    CandidateError, canonical_json, load_candidate, write_candidate_record,
)


class OrdinaryBuildEnvironmentTests(unittest.TestCase):
    def test_tool_path_and_python_injection_do_not_enter_collection(self):
        from tools.build_windows_ordinary import build_environment
        environment = build_environment({"SystemRoot": "C:/Windows", "PATH": "C:/unrelated-dlls",
                                         "PYTHONPATH": "C:/other-python", "QT_PLUGIN_PATH": "C:/other-qt",
                                         "TEMP": "C:/Temp"})
        self.assertNotIn("unrelated-dlls", environment["PATH"])
        self.assertIn(str(Path("C:/Windows") / "System32"), environment["PATH"].split(os.pathsep))
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("QT_PLUGIN_PATH", environment)
        self.assertEqual(environment["TEMP"], "C:/Temp")


class OrdinaryCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "_internal"
        self.bundle.mkdir()
        self.exe = self.root / "LocalCAT.exe"
        self.exe.write_bytes(b"test executable")
        data = b'{"fixture": [1, 2, 3]}'
        (self.bundle / "fixture.json").write_bytes(data)
        self.inputs = {
            "schema": "localcat-ordinary-inputs-v1",
            "input_digests": {
                "owner.py": hashlib.sha256(b"actual build input").hexdigest(),
                "fixture.json": hashlib.sha256(data).hexdigest(),
            },
            "data_ids": ["fixture.json"],
            "core_execution": {"collection": "pyz", "optimization": 0},
        }
        (self.bundle / "localcat-owner-inputs.json").write_bytes(canonical_json(self.inputs))
        write_candidate_record(self.root, {"repository_commit": "a" * 40})

    def test_digest_is_not_a_source_file_and_data_is_actual_content(self):
        candidate = load_candidate(self.exe)
        self.assertEqual(candidate.input_digest("owner.py"), self.inputs["input_digests"]["owner.py"])
        with self.assertRaises(CandidateError):
            candidate.read_data("owner.py")
        self.assertEqual(candidate.read_data("fixture.json"), b'{"fixture": [1, 2, 3]}')
        self.assertFalse((self.bundle / "owner.py").exists())

    def test_required_data_cannot_be_replaced_by_digest_text(self):
        candidate = load_candidate(self.exe)
        (self.bundle / "fixture.json").write_text(self.inputs["input_digests"]["fixture.json"])
        with self.assertRaises(CandidateError):
            candidate.read_data("fixture.json")

    def test_changed_executable_or_owner_input_record_is_rejected(self):
        for path in (self.exe, self.bundle / "localcat-owner-inputs.json"):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(path=path.name), self.assertRaises(CandidateError):
                load_candidate(self.exe)
            path.write_bytes(original)

    def test_payload_change_changes_candidate_but_not_core_execution(self):
        first = load_candidate(self.exe)
        (self.bundle / "avatar.png").write_bytes(b"unrelated resource")
        write_candidate_record(self.root, {"repository_commit": "b" * 40})
        second = load_candidate(self.exe)
        self.assertNotEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(first.core_execution_digest, second.core_execution_digest)

    def test_build_annotation_alone_does_not_change_candidate(self):
        first = load_candidate(self.exe)
        write_candidate_record(self.root, {"repository_commit": "b" * 40})
        self.assertEqual(first.candidate_id, load_candidate(self.exe).candidate_id)

    def test_payload_inventory_verification_rejects_missing_or_extra_files(self):
        candidate = load_candidate(self.exe, verify_payload=True)
        self.assertEqual(len(candidate.candidate_id), 64)
        (self.bundle / "extra.dll").write_bytes(b"not collected")
        with self.assertRaises(CandidateError):
            load_candidate(self.exe, verify_payload=True)

    def test_bad_candidate_id_and_escaped_data_fail(self):
        record_path = self.root / "localcat-candidate.json"
        record = json.loads(record_path.read_bytes())
        record["candidate_id"] = "0" * 64
        record_path.write_bytes(canonical_json(record))
        with self.assertRaises(CandidateError):
            load_candidate(self.exe)
        write_candidate_record(self.root, {"repository_commit": "a" * 40})
        candidate = load_candidate(self.exe)
        for name in ("../owner.py", "/fixture.json", "fixture.json:stream", "absent.json"):
            with self.subTest(name=name), self.assertRaises(CandidateError):
                candidate.read_data(name)


if __name__ == "__main__":
    unittest.main()
