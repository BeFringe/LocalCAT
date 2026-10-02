"""Ordinary distribution facts: content and input digests stay distinct."""
from __future__ import annotations

import hashlib
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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


@unittest.skipUnless(sys.platform == "win32" and importlib.util.find_spec("PyInstaller"),
                     "Windows frozen build dependencies")
class OrdinaryVersionMetadataTests(unittest.TestCase):
    def test_version_resource_is_parseable_and_matches_qt(self):
        from PyInstaller.utils.win32.versioninfo import StringFileInfo, load_version_info_from_text_file
        from tools.build_windows_ordinary import ROOT, assignment, version_resource

        info = load_version_info_from_text_file(version_resource(ROOT))
        strings = {item.name: item.val for group in info.kids if isinstance(group, StringFileInfo)
                   for table in group.kids for item in table.kids}
        self.assertEqual(assignment(ROOT / "qt_editor.py", "APPLICATION_VERSION"), "0.5.2")
        self.assertEqual(strings["FileVersion"], "0.5.2")
        self.assertEqual(strings["ProductVersion"], "0.5.2")
        self.assertEqual(strings["ProductName"], "LocalCAT")
        self.assertEqual(strings["FileDescription"], "LocalCAT")
        self.assertEqual((info.ffi.fileVersionMS, info.ffi.fileVersionLS), (5, 2 << 16))
        self.assertEqual((info.ffi.productVersionMS, info.ffi.productVersionLS), (5, 2 << 16))

    def test_version_drift_is_rejected_before_collection(self):
        from tools.build_windows_ordinary import ROOT, version_resource

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resource = root / "packaging/windows/version_info.txt"
            resource.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "packaging/windows/version_info.txt", resource)
            (root / "qt_editor.py").write_text('APPLICATION_VERSION = "0.5.3"\n')
            with self.assertRaisesRegex(ValueError, "version metadata"):
                version_resource(root)

    def test_recipe_consumes_version_resource_and_build_record_tracks_it(self):
        from tools import build_windows_ordinary as builder

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)

            def collect(_command, **_options):
                # Exercise the real recipe/record producer without running a full build.
                dist = output / "dist/LocalCAT"
                (dist / "_internal").mkdir(parents=True)
                (dist / "LocalCAT.exe").write_bytes(b"test executable")
                (output / "analysis-inputs.json").write_text(json.dumps(
                    {"pure": [], "scripts": [], "binaries": [], "datas": []}))

            archive = Mock(toc={"PYZ.pyz": ()})
            archive.open_embedded_archive.return_value.toc = {}
            with patch.object(builder, "collect_inputs", return_value=({}, set(), set())), \
                    patch.object(builder.subprocess, "check_output", side_effect=lambda command, **kw:
                                 "a" * 40 if command[1] == "rev-parse" else b""), \
                    patch.object(builder.subprocess, "run", side_effect=collect), \
                    patch("PyInstaller.archive.readers.CArchiveReader", return_value=archive):
                executable = builder.build(output)

            tree = ast.parse((output / "LocalCAT.spec").read_text())
            exe_call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == "EXE")
            options = {item.arg: item.value for item in exe_call.keywords}
            resource = builder.ROOT / "packaging/windows/version_info.txt"
            self.assertIn("version", options)
            self.assertEqual(Path(ast.literal_eval(options["version"])), resource)
            record = json.loads((executable.parent / "localcat-candidate.json").read_bytes())
            self.assertEqual(record["build"]["build_input_digests"]["packaging/windows/version_info.txt"],
                             hashlib.sha256(resource.read_bytes()).hexdigest())
            runtime_inputs = json.loads((executable.parent / "_internal/localcat-owner-inputs.json").read_bytes())
            self.assertNotIn("packaging/windows/version_info.txt", runtime_inputs["input_digests"])
            self.assertNotIn("packaging/windows/version_info.txt", runtime_inputs["data_ids"])

    def test_candidate_check_reads_actual_pe_version_resource_and_rejects_wrong_version(self):
        import PyInstaller
        from PyInstaller.utils.win32.versioninfo import (
            load_version_info_from_text_file, write_version_info_to_executable,
        )
        from tools.build_windows_ordinary import ROOT, version_resource
        from tools.check_windows_ordinary import check_executable_metadata

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "LocalCAT.exe"
            bootloader = Path(PyInstaller.__file__).parent / "bootloader" / PyInstaller.PLATFORM / "runw.exe"
            shutil.copyfile(bootloader, executable)
            info = load_version_info_from_text_file(version_resource(ROOT))
            write_version_info_to_executable(str(executable), info)
            facts = check_executable_metadata(executable)
            self.assertEqual(facts["file_version"], [0, 5, 2, 0])
            self.assertEqual(facts["product_version"], [0, 5, 2, 0])
            self.assertEqual(facts["strings"]["ProductName"], "LocalCAT")
            info.ffi.productVersionLS = 3 << 16
            write_version_info_to_executable(str(executable), info)
            with self.assertRaisesRegex(AssertionError, "version metadata"):
                check_executable_metadata(executable)


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
