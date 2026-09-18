"""Bounded prelaunch payload evidence; never a full W3 acceptance result."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import verify_windows_frozen_payload as payload
from tools.windows_frozen_manifest import SourceEntry, prepare_manifest
from tools.windows_frozen_packaging import fact
from tools.windows_frozen_release import create_release_binding, ReleaseBindingError


class PayloadTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[1] / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="payload-unit-", dir=artifacts)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / "original"
        self.dist.mkdir()
        entries = []
        for number, (name, role) in enumerate(payload.PROFILE.items()):
            content = b"value = 1\n" if name.endswith(".py") else b"fixture or native bytes\n"
            entries.append(SourceEntry("entry-%d" % number, name, role, content))
            path = self.dist / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        prepared = prepare_manifest(entries, candidate_input_digest="a" * 64,
                                    applied_sources_digest="b" * 64)
        (self.dist / payload.EXE).write_bytes(b"unchanged test PE")
        (self.dist / payload.MANIFEST).write_bytes(prepared.runtime_bytes)
        data = create_release_binding(
            self.dist, source_context={"repository_commit": "c" * 40,
                                      "source_inventory": {"producer.py": fact(b"producer")}},
            prepared=prepared, build_evidence={"prelink.json": fact(prepared.prelink_bytes),
                "applied-sources.json": {"bytes": 7, "sha256": "b" * 64}})
        self.release = self.root / "release.json"
        self.release.write_bytes(data)
        self.arguments = dict(dist=self.dist, release=self.release,
            expected_release_sha256=fact(data)["sha256"],
            expected_repository_commit="c" * 40,
            expected_candidate_input_digest="a" * 64,
            output=self.root / "evidence", timeout=2)
        self.calls = []

    def runner(self, command, *, cwd, env, timeout, stdout_path, stderr_path):
        case = next(case for case in payload.CASES if case.name == Path(command[0]).parent.parent.name)
        self.calls.append((case, command, cwd, env))
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(("\r\n".join(case.markers) + "\r\n").encode("ascii"))
        return {"returncode": case.exit_code, "timed_out": False}

    def run_matrix(self, runner=None, **overrides):
        arguments = dict(self.arguments, **overrides)
        return payload.run_verification(**arguments, _runner=runner or self.runner)

    def test_complete_bounded_matrix_and_evidence(self):
        before = payload.inventory(self.dist)
        result = self.run_matrix()
        self.assertTrue(result["passed"])
        self.assertEqual(result["classification"], "PRELAUNCH_PAYLOAD_EVIDENCE_NOT_FULL_W3_GATE")
        self.assertEqual(len(result["cases"]), 32)
        self.assertEqual(len(self.calls), 32)
        self.assertEqual(payload.inventory(self.dist), before)
        self.assertTrue(result["source_unchanged"])
        self.assertTrue(result["release_unchanged"])
        self.assertTrue(result["anchors_unchanged"])
        self.assertTrue(result["observer_unchanged"])
        for case in result["cases"]:
            self.assertTrue(case["passed"], case)
            self.assertTrue(case["exact_delta"])
            self.assertTrue(case["executable_unchanged"])
            self.assertEqual(case["pre"], case["post"])
            path = Path(result["output"]) / case["name"]
            self.assertTrue((path / "stdout.bin").is_file())
            self.assertTrue((path / "stderr.bin").is_file())
            self.assertTrue((path / "command.json").is_file())
        saved = json.loads((Path(result["output"]) / "result.json").read_bytes())
        self.assertEqual(saved, result)

    def test_each_member_has_independent_tamper_and_missing_case(self):
        for name in payload.PROFILE:
            operations = {case.operation for case in payload.CASES if case.path == name}
            self.assertTrue({"tamper", "missing"}.issubset(operations), name)
        calls = [case for case in payload.CASES if case.operation == "append"]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(case.path.endswith("_bootstrap_external.py") for case in calls))
        self.assertTrue(any(b"nt.stat" in case.content for case in calls))
        self.assertTrue(any(b"nt.startfile" in case.content for case in calls))
        self.assertTrue(any(b"_imp.create_dynamic" in case.content for case in calls))
        dynamic = next(case for case in calls if b"_imp.create_dynamic" in case.content)
        self.assertIn(b"ModuleSpec(", dynamic.content)
        self.assertNotIn(b"create_dynamic(None)", dynamic.content)

    def test_external_anchor_mismatch_rejects_before_execution(self):
        with self.assertRaises(ReleaseBindingError):
            self.run_matrix(expected_release_sha256="f" * 64)
        self.assertFalse(self.calls)

    def test_output_must_be_new_and_beneath_artifacts(self):
        with self.assertRaises(ValueError):
            self.run_matrix(output=self.root)
        with self.assertRaises(ValueError):
            self.run_matrix(output=Path(__file__).resolve().parents[1] / "not-artifacts")
        self.assertFalse(self.calls)

    def test_output_inside_dist_is_rejected_without_changing_source(self):
        before = payload.inventory(self.dist)
        with self.assertRaises(ValueError):
            self.run_matrix(output=self.dist / "new-evidence")
        self.assertEqual(payload.inventory(self.dist), before)
        self.assertFalse(self.calls)

    def test_undeclared_mutation_is_not_launched(self):
        original = payload._mutate
        def bad(bundle, case):
            original(bundle, case)
            (bundle / "surprise.txt").write_bytes(b"extra")
        with mock.patch.object(payload, "_mutate", bad):
            result = self.run_matrix()
        self.assertFalse(result["passed"])
        self.assertFalse(self.calls)

    def test_observer_snapshot_can_run_from_arbitrary_cwd(self):
        result = self.run_matrix()
        observer = Path(result["output"]) / "observer/tools/verify_windows_frozen_payload.py"
        process = subprocess.run([sys.executable, "-B", str(observer), "--help"],
                                 cwd=self.root, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_wrong_exit_marker_timeout_and_post_mutation_cannot_pass(self):
        for mode in ("exit", "marker", "late-marker", "timeout", "exe", "extra"):
            def bad(command, **kwargs):
                result = self.runner(command, **kwargs)
                if mode == "exit":
                    result["returncode"] = 77
                elif mode == "marker":
                    kwargs["stderr_path"].write_bytes(b"FROZEN_ENTRY.NOT_THE_EXPECTED_ERROR\n")
                elif mode == "late-marker":
                    with kwargs["stderr_path"].open("ab") as stream:
                        stream.write(b"FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\n")
                elif mode == "timeout":
                    result["timed_out"] = True
                elif mode == "exe":
                    with Path(command[0]).open("ab") as stream:
                        stream.write(b"changed")
                else:
                    (Path(command[0]).parent / "unexpected.py").write_bytes(b"changed")
                return result
            with self.subTest(mode=mode):
                result = self.run_matrix(runner=bad, output=self.root / ("bad-" + mode))
                self.assertFalse(result["passed"])
                self.assertTrue(any(not case["passed"] for case in result["cases"]))

    def test_source_release_mutation_is_recorded_even_when_child_raises(self):
        def bad(command, **kwargs):
            self.release.write_bytes(b"changed release")
            (self.dist / payload.EXE).write_bytes(b"changed source")
            raise OSError("launch failed")
        result = self.run_matrix(runner=bad)
        self.assertFalse(result["passed"])
        self.assertFalse(result["source_unchanged"])
        self.assertFalse(result["release_unchanged"])
        self.assertIn("launch failed", result["cases"][0]["error"])
        self.assertTrue((Path(result["output"]) / "result.json").exists())

    def test_pollution_bytes_environment_and_sentinel_are_bound(self):
        result = self.run_matrix()
        polluted = [case for case in result["cases"] if case["probes_pre"]["files"]]
        self.assertEqual(len(polluted), 2)
        for case in polluted:
            self.assertEqual(case["probes_pre"], case["probes_post"])
            self.assertTrue(case["probe_not_executed"])
            self.assertTrue(any(name.endswith(".pyd") for name in case["probes_pre"]["files"]))
        for case, command, cwd, environment in self.calls:
            self.assertNotEqual(Path(cwd), Path(__file__).resolve().parents[1])
            self.assertEqual("PYTHONPATH" in environment, case.name == "pythonpath-pollution")
            self.assertFalse(any(name.upper().startswith(("QT_", "MIMALLOC_")) for name in environment))

    def test_probe_side_effect_or_stdout_is_rejected(self):
        def bad(command, **kwargs):
            result = self.runner(command, **kwargs)
            kwargs["stdout_path"].write_bytes(payload.PROBE_TOKEN.encode("ascii"))
            return result
        self.assertFalse(self.run_matrix(runner=bad)["passed"])

    def test_inventory_records_empty_directories_and_forbidden_names(self):
        (self.dist / "__pycache__").mkdir()
        (self.dist / "x.pyc").write_bytes(b"bytecode")
        inventory = payload.inventory(self.dist)
        self.assertIn("__pycache__", inventory["directories"])
        self.assertIn("x.pyc", inventory["files"])

    def test_cli_help_from_unrelated_cwd(self):
        command = [sys.executable, "-B", str(Path(payload.__file__).resolve()), "--help"]
        process = subprocess.run(command, cwd=self.root, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(b"--expected-candidate-input-digest", process.stdout)

    def test_timeout_only_terminates_owned_child_and_saves_output(self):
        result = payload.run_child([sys.executable, "-c", "import time;print('started',flush=True);time.sleep(30)"],
            cwd=self.root, env=os.environ.copy(), timeout=0.5,
            stdout_path=self.root / "timeout.out", stderr_path=self.root / "timeout.err")
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn(b"started", (self.root / "timeout.out").read_bytes())


if __name__ == "__main__":
    unittest.main()
