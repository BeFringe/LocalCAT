"""Checks for the observer, not substitutes for real E9 fault executions."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tools import trace_windows_frozen_e9_faults as faults


PREFIX = (
    "W3_TRACE_BEGIN\nW3_PE_ENTRY\nW3_DLL_POLICY\nW3_PYTHON_DLL_ENTRY\n"
    "FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN\nW3_E9_SET_PATH\n"
)


def exited(body, code):
    return body + "W3_PROCESS_EXIT\nLast event: 123.456: Exit process 0:123, code " + code + "\n"


class E9FaultObserverTests(unittest.TestCase):
    def test_control_requires_real_success_and_no_fault(self):
        output = exited(PREFIX + "W3_E9_INIT_CONFIG\nFROZEN_ENTRY.SPIKE_COMPLETED\n", "0")
        self.assertTrue(faults.summarize_fault(output, "", "control")["expected_path_observed"])
        detached = output.replace("W3_PROCESS_EXIT", "W3_PYTHON_DLL_ENTRY\nW3_PROCESS_EXIT")
        self.assertTrue(faults.summarize_fault(detached, "", "control")["expected_path_observed"])
        for broken in (output + "W3_E9_ALLOC_NULL\n", output.replace("code 0", "code 1"),
                       output.replace("W3_E9_INIT_CONFIG\n", "")):
            self.assertFalse(faults.summarize_fault(broken, "", "control")["expected_path_observed"])

    def test_path_fault_requires_fatal_before_initialize_and_actual_exit(self):
        output = exited(PREFIX + "W3_E9_ALLOC_NULL\nW3_E9_NULL_RETURN_BRANCH\nW3_E9_FATAL\n", "c0000409")
        stderr = "Fatal Python error: Py_SetPath: out of memory\n"
        result = faults.summarize_fault(output, stderr, "path-alloc-null")
        self.assertTrue(result["expected_path_observed"])
        self.assertEqual(result["classification"], "CONTROLLED_ALLOCATION_FAILURE_NOT_W3_GATE")
        for broken, error in (
            (output, ""), (output + "W3_E9_INIT_CONFIG\n", stderr),
            (output.replace("W3_E9_FATAL\n", ""), stderr),
            (output.replace("W3_E9_NULL_RETURN_BRANCH\n", ""), stderr),
            (output.replace("code c0000409", "code 0"), stderr),
            (output + "W3_E9_ALLOC_NULL\n", stderr),
        ):
            self.assertFalse(faults.summarize_fault(broken, error, "path-alloc-null")["expected_path_observed"])

    def test_inittab_fault_requires_internal_call_and_caller_diagnostic(self):
        output = exited(PREFIX + "W3_E9_INIT_CONFIG\nW3_E9_EXTEND_INITTAB\n"
                        "W3_E9_ALLOC_NULL\nW3_E9_NULL_RETURN_BRANCH\nFROZEN_ENTRY.INITIALIZATION_FAILED\n", "1")
        stderr = "FROZEN_ENTRY.INITIALIZATION_FAILED\n"
        self.assertTrue(faults.summarize_fault(output, stderr, "inittab-alloc-null")["expected_path_observed"])
        quoted = output.replace("\nFROZEN_ENTRY.INITIALIZATION_FAILED\n",
                                '\n00007ff6`00000000  "FROZEN_ENTRY.INITIALIZATION_FAILED"\n')
        self.assertTrue(faults.summarize_fault(quoted, stderr, "inittab-alloc-null")["expected_path_observed"])
        self.assertFalse(faults.summarize_fault(output, "", "inittab-alloc-null")["expected_path_observed"])
        for broken in (output.replace("W3_E9_EXTEND_INITTAB\n", ""),
                       output.replace("FROZEN_ENTRY.INITIALIZATION_FAILED\n", ""),
                       output.replace("W3_E9_NULL_RETURN_BRANCH\n", ""),
                       output + "W3_E9_FATAL\n", output.replace("code 1", "code 0")):
            self.assertFalse(faults.summarize_fault(broken, stderr, "inittab-alloc-null")["expected_path_observed"])

    def test_partial_or_misordered_or_bad_debugger_trace_never_completes(self):
        output = exited(PREFIX + "W3_E9_INIT_CONFIG\nW3_E9_EXTEND_INITTAB\n"
                        "W3_E9_ALLOC_NULL\nW3_E9_NULL_RETURN_BRANCH\nFROZEN_ENTRY.INITIALIZATION_FAILED\n", "1")
        for broken, stderr in (
            (output.replace("W3_DLL_POLICY\n", ""), ""),
            (output.replace("W3_E9_ALLOC_NULL\n", "") + "W3_E9_ALLOC_NULL\n", ""),
            (output + "FROZEN_ENTRY.SPIKE_COMPLETED\n", ""),
            (output + "W3_ACCESS_VIOLATION\n", ""),
            (output, "Unable to insert breakpoint\n"),
            (output, "Numeric expression missing from '& condition'\n"),
            (output, "Range error in bp 80\n"),
            (output.replace("Last event: 123.456: Exit process 0:123, code 1", ""), ""),
            (output + "Last event: 123.456: Exit process 0:123, code 1\n", ""),
        ):
            self.assertFalse(faults.summarize_fault(broken, stderr + "FROZEN_ENTRY.INITIALIZATION_FAILED\n",
                                                  "inittab-alloc-null")["expected_path_observed"])

    def test_unknown_profiles_and_unbound_python_are_rejected(self):
        with self.assertRaises(faults.ProbeInputError):
            faults.summarize_fault("", "", "unknown")
        with self.assertRaises(faults.ProbeInputError):
            faults.verify_fault_sites(b"unbound runtime")

    def test_only_the_single_allocation_call_is_skipped(self):
        for profile in ("control", "path-alloc-null", "inittab-alloc-null"):
            script = faults.render_fault_commands(profile, 0x1000, 0x1000)
            self.assertIn("bu python314!Py_SetPath", script)
            self.assertIn("bu python314!Py_InitializeFromInitConfig", script)
            self.assertEqual(script.splitlines()[-1], "g")
            if profile != "control":
                self.assertIn("NtCreateFile", script)
                self.assertIn("NtQueryAttributesFile", script)
                self.assertIn("bp80 ntdll!NtCreateFile", script)
                self.assertIn("bd 80 81 82 83", script)
            else:
                self.assertNotIn("NtCreateFile", script)
            self.assertNotIn("r rsp=", script)
            self.assertNotIn("r rip=poi(@rsp)", script)
            self.assertNotIn("r rcx=", script)
            self.assertNotIn("eb ", script)
            self.assertNotIn("&&", script)
            self.assertEqual(script.count("r rax=0;"), 0 if profile == "control" else 1)
            if profile != "control":
                self.assertIn("r @$t9=1;", script)
                self.assertIn(".if (@$t9 == 0)", script)

    @unittest.skipUnless(os.name == "nt", "observer launch orchestration is Windows-only")
    def test_post_execution_drift_does_not_persist_a_successful_observation(self):
        # Mock orchestration only. The real packaged executions are separate.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts/windows").mkdir(parents=True)
            build = root / "build"
            bundle = build / "packaging/dist/localcat-spike"
            bundle.mkdir(parents=True)
            (bundle / "localcat-spike.exe").write_bytes(b"exe")
            cdb = root / "cdb.exe"
            cdb.write_bytes(b"cdb")
            (build / "diagnostic.json").write_text(json.dumps({
                "status": "EXECUTION_DIAGNOSTIC_COLLECTED", "candidate_input_digest": "candidate",
                "packaging": {"dist": []}, "packaged_pe": {},
            }), encoding="utf-8")

            def simulate_command(command, cwd, environment, directory, name, records, **kwargs):
                output = exited(PREFIX + "W3_E9_INIT_CONFIG\nFROZEN_ENTRY.SPIKE_COMPLETED\n", "0")
                (directory / "cdb.stdout").write_text(output, encoding="utf-8")
                (directory / "cdb.stderr").write_text("", encoding="utf-8")
                records.append({"exit_code": 0})

            with (patch.object(faults, "ROOT", root),
                  patch.object(faults, "load_candidate", return_value={"candidate_input_digest": "candidate",
                      "runtime": {"cpython": {"dll": {}}}}),
                  patch.object(faults, "checked_bytes", return_value=b"python"),
                  patch.object(faults, "verify_fault_sites", return_value=(0x1000, 0x1000)),
                  patch.object(faults, "tree_inventory", side_effect=[[], [{"drift": True}]]),
                  patch.object(faults, "record_command", side_effect=simulate_command)):
                with self.assertRaisesRegex(faults.ProbeInputError, "changed on-disk"):
                    faults.run(SimpleNamespace(diagnostic_build=build, cdb=cdb, profile="control"))
            evidence_path, = (root / "artifacts/windows").glob("e9-fault-*/evidence.json")
            self.assertEqual(json.loads(evidence_path.read_bytes())["status"], "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
