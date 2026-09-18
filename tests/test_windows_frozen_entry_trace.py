"""Observer tests never grant E0.5/E4 authority or prove unreachability."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tools import trace_windows_frozen_entry as trace


class EntryTraceTests(unittest.TestCase):
    def test_observer_breakpoints_are_set_before_execution_and_cover_all_sites(self):
        script = trace.render_commands(0x1234, 0x1000)
        self.assertIn("bp @$exentry", script)
        self.assertIn("bu KERNELBASE!SetDefaultDllDirectories", script)
        self.assertIn("bu ntdll!LdrLoadDll", script)
        self.assertIn("bu KERNELBASE!LoadLibraryExW", script)
        self.assertIn("W3_RANDOM_REINIT_CHECK", script)
        self.assertIn("W3_RANDOM_HELPER", script)
        self.assertIn("bu python314!Py_Initialize+0x234", script)
        for rva in trace.PYTHON_LOADER_SITES:
            self.assertIn(f"bu python314!Py_Initialize+0x{rva - 0x1000:x}", script)
        self.assertEqual(script.splitlines()[-1], "g")
        self.assertNotIn("ed ", script)
        self.assertNotIn("eb ", script)
        self.assertNotIn("r rcx=", script)

    def test_missing_marker_or_debugger_error_is_not_complete_trace(self):
        normal = ("W3_TRACE_BEGIN\nW3_PE_ENTRY\nW3_DLL_POLICY\nW3_PYTHON_DLL_ENTRY\n"
                  "FROZEN_ENTRY.SPIKE_COMPLETED\nW3_PROCESS_EXIT\n"
                  "Last event: 123.456: Exit process 0:123, code 0\n")
        self.assertTrue(trace.summarize(normal)["observed_sequence_complete"])
        self.assertTrue(trace.summarize(normal.replace("\n", "\r\n"))["observed_sequence_complete"])
        for broken in (normal.replace("W3_PE_ENTRY", ""), normal + "Syntax error\n",
                       normal.replace("W3_DLL_POLICY", ""),
                       normal.replace("W3_PYTHON_DLL_ENTRY", ""),
                       normal + "*** Unable to resolve unqualified symbol in Bp expression 'python314+0x1234'.\n",
                       normal + "*** Bp expression 'python314+0x1234' contains symbols not qualified with module name.\n",
                       normal.replace("code 0", "code 1"),
                       normal.replace("Last event: 123.456: Exit process 0:123, code 0", ""),
                       normal + "W3_ACCESS_VIOLATION\n"):
            self.assertFalse(trace.summarize(broken)["observed_sequence_complete"])
        self.assertEqual(trace.summarize(normal)["classification"], "OBSERVER_NOT_CALL_CLOSURE_PROOF")

    def test_child_profiles_do_not_inherit_mimalloc_or_python_overrides(self):
        original = {"SystemRoot": "C:/Windows", "PATH": "ambient", "PYTHONPATH": "ambient",
                    "MIMALLOC_VERBOSE": "1", "mimalloc_show_stats": "9", "QT_PLUGIN_PATH": "ambient"}
        normal = trace.trace_environment(original, "normal")
        self.assertNotIn("MIMALLOC_VERBOSE", normal)
        self.assertNotIn("mimalloc_show_stats", normal)
        self.assertNotIn("PYTHONPATH", normal)
        self.assertNotIn("QT_PLUGIN_PATH", normal)
        self.assertEqual(trace.trace_environment(original, "mimalloc-stats")["MIMALLOC_SHOW_STATS"], "1")
        self.assertEqual(original["MIMALLOC_VERBOSE"], "1")
        with self.assertRaises(trace.ProbeInputError):
            trace.trace_environment(original, "unknown")

    @unittest.skipUnless(os.name == "nt", "observer launch orchestration is Windows-only")
    def test_post_observation_dist_drift_never_persists_observed_status(self):
        # Exercise orchestration only; mocked CDB/PE are not runtime evidence.
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
            pe = MagicMock()
            pe.__enter__.return_value = SimpleNamespace(
                OPTIONAL_HEADER=SimpleNamespace(AddressOfEntryPoint=1), DIRECTORY_ENTRY_IMPORT=[],
                DIRECTORY_ENTRY_EXPORT=SimpleNamespace(symbols=[SimpleNamespace(
                    address=1, name=b"Py_Initialize", forwarder=None)]))
            stdout = (b"W3_TRACE_BEGIN\nW3_PE_ENTRY\nW3_DLL_POLICY\nW3_PYTHON_DLL_ENTRY\n"
                      b"FROZEN_ENTRY.SPIKE_COMPLETED\nW3_PROCESS_EXIT\n"
                      b"Last event: 123.456: Exit process 0:123, code 0\n")
            with (patch.object(trace, "ROOT", root),
                  patch.object(trace, "load_candidate", return_value={"candidate_input_digest": "candidate",
                      "runtime": {"cpython": {"dll": {}}}}),
                  patch.object(trace, "checked_bytes", return_value=b"python"),
                  patch.object(trace, "PINNED_PYTHON_SHA256", trace.byte_fact(b"python")["sha256"]),
                  patch.object(trace, "PYTHON_LOADER_SITES", {}),
                  patch.dict(sys.modules, {"pefile": SimpleNamespace(PE=lambda **kwargs: pe)}),
                  patch.object(trace, "tree_inventory", side_effect=[[], [{"drift": True}]]),
                  patch.object(trace, "record_command", return_value=SimpleNamespace(stdout=stdout))):
                with self.assertRaisesRegex(trace.ProbeInputError, "changed on-disk"):
                    trace.run(SimpleNamespace(diagnostic_build=build, cdb=cdb, profile="normal"))
            evidence_path, = (root / "artifacts/windows").glob("entry-trace-*/evidence.json")
            evidence = json.loads(evidence_path.read_bytes())
            self.assertNotEqual(evidence["status"], "OBSERVED")


if __name__ == "__main__":
    unittest.main()
