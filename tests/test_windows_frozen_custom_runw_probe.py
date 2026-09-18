"""Diagnostic producer boundaries; these tests cannot grant W3 approval."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import probe_windows_frozen_custom_runw as probe
from tools.probe_windows_frozen_custom_runw import (
    ProbeInputError, import_delta, load_candidate, parser, prepare_source_manifest, run_probe,
)
from tools.windows_frozen_manifest import SourceEntry


def candidate():
    contract = json.loads((Path(__file__).resolve().parents[1] /
        "packaging/windows/frozen-entry/candidate-contract.json").read_bytes())
    value = {"schema": "localcat.windows-frozen-entry-candidate-input.v3",
             "runtime": {}, "entry_contract": {"dynamic_loader": contract["dynamic_loader"]}, "toolchain": {}}
    value["candidate_input_digest"] = hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value.update(status="MATERIALIZED", assertions=[])
    return value


class CustomRunwProbeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "runw producer is Windows only")
    def test_input_drift_stops_before_any_toolchain_or_child_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = parser().parse_args(["--pristine-source", "unused", "--packaging-dependencies", "unused",
                                        "--output-parent", str(root / "artifacts/windows")])
            lock = {"evidence_producer": {"custom_packaging": {"locked": "original"}}}
            with mock.patch.object(probe, "ROOT", root), mock.patch.object(probe, "load_candidate", return_value=lock), \
                    mock.patch.object(probe, "candidate_packaging_fact", return_value={"locked": "changed"}), \
                    mock.patch.object(probe, "toolchain_inputs") as toolchain, \
                    mock.patch.object(probe, "record_command") as commands:
                with self.assertRaisesRegex(ProbeInputError, "packaging input"):
                    run_probe(args)
            toolchain.assert_not_called()
            commands.assert_not_called()
            outputs = list((root / "artifacts/windows").glob("*/diagnostic.json"))
            self.assertEqual(len(outputs), 1)
            report = json.loads(outputs[0].read_bytes())
            self.assertEqual(report["status"], "DIAGNOSTIC_FAILED")
            self.assertEqual(report["commands"], [])

    def test_packaging_preflight_rejects_old_lock_and_driver_or_dependency_drift(self):
        expected = {"source": "exact", "dependency": "exact"}
        with mock.patch.object(probe, "candidate_packaging_fact", return_value=expected):
            with self.assertRaisesRegex(ProbeInputError, "packaging input"):
                probe.packaging_preflight({}, Path("dependencies"))
            probe.packaging_preflight({"evidence_producer": {"custom_packaging": expected}}, Path("dependencies"))
            with self.assertRaisesRegex(ProbeInputError, "packaging input"):
                probe.packaging_preflight({"evidence_producer": {"custom_packaging": {"source": "changed"}}}, Path("dependencies"))

    @unittest.skipUnless(os.name == "nt", "requires Windows STARTUPINFO")
    def test_no_standard_handles_mode_does_not_create_or_inherit_pipes(self):
        records = []
        with mock.patch.object(probe.subprocess, "Popen") as factory:
            child = factory.return_value
            child.pid = 1234
            child.wait.return_value = 0
            probe.record_windowed_no_standard_handles(["test.exe"], Path("non-bundle-cwd"), {"PATH": "System32"}, records, timeout=2)
        options = factory.call_args.kwargs
        startup = options["startupinfo"]
        self.assertEqual(startup.dwFlags, probe.subprocess.STARTF_USESTDHANDLES | probe.subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual((startup.hStdInput, startup.hStdOutput, startup.hStdError), (0, 0, 0))
        self.assertEqual(startup.wShowWindow, probe.subprocess.SW_HIDE)
        self.assertEqual(options["creationflags"], probe.subprocess.CREATE_NO_WINDOW)
        self.assertTrue(options["close_fds"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertIsNone(options.get(name))
        child.kill.assert_not_called()
        self.assertEqual(records[0]["exit_code"], 0)
        self.assertEqual(records[0]["pid"], 1234)
        self.assertEqual(records[0]["native_markers"], "NOT_OBSERVED_NO_OUTPUT_CHANNEL")

    @unittest.skipUnless(os.name == "nt", "requires Windows STARTUPINFO")
    def test_no_standard_handles_timeout_only_terminates_the_owned_child(self):
        records = []
        with mock.patch.object(probe.subprocess, "Popen") as factory:
            child = factory.return_value
            child.pid = 1234
            child.wait.side_effect = [probe.subprocess.TimeoutExpired(["test.exe"], 2), 1]
            with self.assertRaisesRegex(ProbeInputError, "timed out"):
                probe.record_windowed_no_standard_handles(["test.exe"], Path("non-bundle-cwd"), {}, records, timeout=2)
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=2), mock.call(timeout=5)])
        self.assertTrue(records[0]["timed_out"])
        self.assertEqual(records[0]["exit_code"], 1)
        self.assertEqual(records[0]["termination"], "TEST_CHILD_KILLED_ON_TIMEOUT")

    @unittest.skipUnless(os.name == "nt", "requires Windows STARTUPINFO")
    def test_no_standard_handles_keeps_failure_and_unconfirmed_termination_distinct(self):
        for outcomes in ([3], [probe.subprocess.TimeoutExpired(["test.exe"], 2),
                               probe.subprocess.TimeoutExpired(["test.exe"], 5)]):
            with self.subTest(outcomes=outcomes), mock.patch.object(probe.subprocess, "Popen") as factory:
                records = []
                child = factory.return_value
                child.pid = 1234
                child.wait.side_effect = outcomes
                with self.assertRaises(ProbeInputError):
                    probe.record_windowed_no_standard_handles(["test.exe"], Path("non-bundle-cwd"), {}, records, timeout=2)
                if len(outcomes) == 1:
                    self.assertEqual(records[0]["exit_code"], 3)
                    child.kill.assert_not_called()
                    self.assertNotIn("timed_out", records[0])
                else:
                    child.kill.assert_called_once_with()
                    self.assertEqual(child.wait.call_count, 2)
                    self.assertIsNone(records[0]["exit_code"])
                    self.assertEqual(records[0]["termination"], "TEST_CHILD_TERMINATION_UNCONFIRMED")

    @unittest.skipUnless(os.environ.get("LOCALCAT_W3_RUN_BUILD") == "1", "explicit real Waf/runw integration")
    def test_real_windowed_entry_builds_and_executes_retained_bundle(self):
        root = Path(__file__).resolve().parents[1]
        argv = ["--pristine-source", str(root.parent / "w3-sources/pyinstaller-6.22.2"), "--run"]
        if os.environ.get("LOCALCAT_W3_NATIVE_DIRECTORY"):
            argv += ["--native-directory", os.environ["LOCALCAT_W3_NATIVE_DIRECTORY"]]
        if os.environ.get("LOCALCAT_W3_PACKAGE_DEPS"):
            argv += ["--packaging-dependencies", os.environ["LOCALCAT_W3_PACKAGE_DEPS"]]
        directory, report = run_probe(parser().parse_args(argv))
        self.assertEqual(report["status"], "EXECUTION_DIAGNOSTIC_COLLECTED")
        commands = {command["name"]: command for command in report["commands"]}
        self.assertIn("runw-no-standard-handles", commands)
        detached = commands["runw-no-standard-handles"]
        self.assertEqual(detached["exit_code"], 0)
        self.assertEqual(detached["command"], commands["runw"]["command"])
        self.assertEqual(detached["cwd"], commands["runw"]["cwd"])
        self.assertEqual(detached["standard_handles"], {"stdin": "NULL", "stdout": "NULL", "stderr": "NULL"})
        self.assertFalse(detached["inherit_handles"])
        self.assertEqual(detached["native_markers"], "NOT_OBSERVED_NO_OUTPUT_CHANNEL")
        self.assertNotIn("stdout", detached)
        self.assertNotIn("stderr", detached)
        self.assertFalse((directory / "runw-no-standard-handles.stdout").exists())
        self.assertFalse((directory / "runw-no-standard-handles.stderr").exists())
        self.assertTrue((directory / "bundle/localcat-custom-runw.exe").is_file())
        self.assertEqual((directory / "runw.stderr").read_bytes().count(b"FROZEN_ENTRY.SPIKE_COMPLETED"), 1)
        if os.environ.get("LOCALCAT_W3_PACKAGE_DEPS"):
            self.assertTrue(report["packaged_pe_delta"]["exact_match"])
            self.assertEqual(report["packaged_pe"]["debug_directory_types"], [16])
            self.assertEqual(report["packaged_pe"]["security_directory_size"], 0)
            self.assertEqual(report["packaging"]["carchive"], {"entries": [], "options": []})
            self.assertTrue(commands["runw"]["command"][0].endswith("localcat-spike.exe"))

    def test_candidate_self_digest_is_rechecked_and_old_schema_rejected(self):
        with tempfile.TemporaryDirectory(prefix="runw-probe-lock-") as directory:
            path = Path(directory) / "candidate.json"
            value = candidate()
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_candidate(path), value)
            value["runtime"]["changed"] = True
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ProbeInputError):
                load_candidate(path)
            value = candidate(); value["schema"] = "old"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ProbeInputError):
                load_candidate(path)

    def test_duplicate_json_keys_never_silently_select_a_lock(self):
        with tempfile.TemporaryDirectory(prefix="runw-probe-lock-") as directory:
            path = Path(directory) / "candidate.json"
            path.write_text('{"schema":1,"schema":2}', encoding="utf-8")
            with self.assertRaises(ProbeInputError):
                load_candidate(path)

    def test_recomputed_digest_cannot_authorize_missing_or_widened_system_calls(self):
        with tempfile.TemporaryDirectory(prefix="runw-probe-lock-") as directory:
            path = Path(directory) / "candidate.json"
            for mutation in ("missing", "widened", "early"):
                value = candidate()
                loader = value["entry_contract"]["dynamic_loader"]
                if mutation == "missing":
                    del loader["application_system_calls"]
                elif mutation == "widened":
                    loader["application_system_calls"][0]["targets"].append("unreviewed.dll")
                else:
                    loader["application_system_calls"][-1]["phase"] = "E0.5"
                payload = {key: item for key, item in value.items()
                           if key not in {"candidate_input_digest", "status", "assertions"}}
                value["candidate_input_digest"] = hashlib.sha256(probe.canonical(payload)).hexdigest()
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaisesRegex(ProbeInputError, "system-call boundary"):
                    load_candidate(path)

    def test_source_digest_uses_actual_templates_before_rendering_header(self):
        template = "\n".join("@@" + key + "@@" for key in (
            "CANDIDATE_INPUT_DIGEST", "PRELINK_INPUT_DIGEST",
            "RUNTIME_MANIFEST_DIGEST", "RUNTIME_ROOT_DIGEST"))
        sources = {"bootloader/src/localcat_manifest.h": template.encode(),
                   "bootloader/src/main.c": b"int main(void) { return 0; }\n"}
        entries = [SourceEntry("fixture", "fixture.txt", "fixture", b"content")]
        prepared, header, facts = prepare_source_manifest(sources, entries, candidate()["candidate_input_digest"],
                                                        applied_sources_digest="b" * 64)
        self.assertNotIn("@@", header)
        self.assertEqual(sources["bootloader/src/localcat_manifest.h"], template.encode())
        self.assertEqual(json.loads(prepared.prelink_bytes)["applied_sources_digest"], "b" * 64)
        sources["bootloader/src/main.c"] += b"/* real edit */"
        other, _, changed = prepare_source_manifest(sources, entries, candidate()["candidate_input_digest"],
                                                   applied_sources_digest="c" * 64)
        self.assertNotEqual(facts["sha256"], changed["sha256"])
        self.assertNotEqual(prepared.runtime_bytes, other.runtime_bytes)

    def test_rehashed_candidate_cannot_omit_crt_exit_call(self):
        value = candidate()
        loader = value["entry_contract"]["dynamic_loader"]
        loader["application_system_calls"] = [item for item in loader["application_system_calls"]
                                               if item["id"] != "crt-ucrt-process-termination"]
        loader["pre_authority_external_calls"] = [item for item in loader["pre_authority_external_calls"]
                                                  if item != "crt-ucrt-process-termination"]
        payload = {key: item for key, item in value.items()
                   if key not in {"candidate_input_digest", "status", "assertions"}}
        value["candidate_input_digest"] = hashlib.sha256(probe.canonical(payload)).hexdigest()
        with tempfile.TemporaryDirectory(prefix="runw-exit-call-") as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ProbeInputError, "system-call boundary"):
                load_candidate(path)

    def test_rehashed_candidate_cannot_omit_multibyte_call(self):
        value = candidate()
        loader = value["entry_contract"]["dynamic_loader"]
        loader["application_system_calls"] = [item for item in loader["application_system_calls"]
                                               if item["id"] != "crt-ucrt-multibyte-casing"]
        loader["pre_authority_external_calls"] = [item for item in loader["pre_authority_external_calls"]
                                                  if item != "crt-ucrt-multibyte-casing"]
        payload = {key: item for key, item in value.items()
                   if key not in {"candidate_input_digest", "status", "assertions"}}
        value["candidate_input_digest"] = hashlib.sha256(probe.canonical(payload)).hexdigest()
        with tempfile.TemporaryDirectory(prefix="runw-multibyte-call-") as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ProbeInputError, "system-call boundary"):
                load_candidate(path)

    def test_rehashed_candidate_cannot_widen_multibyte_resolution(self):
        for key, replacement in (
            ("module_selection", "try-next-module-if-symbol-missing"),
            ("symbols", ["LCMapStringEx"]),
            ("symbol_fallback", {"LCMapStringEx": "load-plugin.dll"}),
            ("phase", "AFTER_E1"),
            ("fallback", {"error": 87, "targets": ["kernel32"], "flags": 0, "excluded_prefixes": []}),
        ):
            value = candidate()
            loader = value["entry_contract"]["dynamic_loader"]
            calls = {item["id"]: item for item in loader["application_system_calls"]}
            self.assertIn("crt-ucrt-multibyte-casing", calls)
            calls["crt-ucrt-multibyte-casing"][key] = replacement
            payload = {name: item for name, item in value.items()
                       if name not in {"candidate_input_digest", "status", "assertions"}}
            value["candidate_input_digest"] = hashlib.sha256(probe.canonical(payload)).hexdigest()
            with tempfile.TemporaryDirectory(prefix="runw-multibyte-widen-") as directory:
                path = Path(directory) / "candidate.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(key=key), self.assertRaisesRegex(ProbeInputError, "system-call boundary"):
                    load_candidate(path)

    def test_prelink_binds_patch_provenance_not_only_resulting_source_bytes(self):
        template = "\n".join("@@" + key + "@@" for key in (
            "CANDIDATE_INPUT_DIGEST", "PRELINK_INPUT_DIGEST",
            "RUNTIME_MANIFEST_DIGEST", "RUNTIME_ROOT_DIGEST"))
        sources = {"bootloader/src/localcat_manifest.h": template.encode()}
        entries = [SourceEntry("fixture", "fixture.txt", "fixture", b"content")]
        prepared, _, facts = prepare_source_manifest(
            sources, entries, candidate()["candidate_input_digest"],
            applied_sources_digest="b" * 64)
        self.assertEqual(json.loads(prepared.prelink_bytes)["applied_sources_digest"], "b" * 64)
        self.assertNotEqual(facts["sha256"], "b" * 64)

    def test_replayed_native_source_cannot_be_overwritten_by_a_different_template(self):
        from tools.probe_windows_frozen_custom_runw import replay_native_sources
        from tools.windows_frozen_patch_series import Patch, PatchInputError
        pristine = {"main.c": b"int main(void) { return 0; }\n"}
        contract = {"owned_existing_files": ["main.c"], "owned_new_files": ["native.h"],
                    "series": [{"id": "native"}]}
        patch = Patch("native", b"diff --git a/native.h b/native.h\nnew file mode 100644\n"
                      b"--- /dev/null\n+++ b/native.h\n@@ -0,0 +1 @@\n+/* bound */\n")
        replayed = replay_native_sources(pristine, [patch], contract, {"native.h": b"/* bound */\n"})
        self.assertEqual(replayed.files["native.h"], b"/* bound */\n")
        with self.assertRaisesRegex(ProbeInputError, "native template"):
            replay_native_sources(pristine, [patch], contract, {"native.h": b"/* changed */\n"})
        with self.assertRaisesRegex(PatchInputError, "series order"):
            replay_native_sources(pristine, [], contract, {"native.h": b"/* bound */\n"})

    def test_import_difference_preserves_static_versus_delay_and_is_not_a_pass(self):
        expected = {"imports": [{"dll": "KERNEL32.DLL", "symbols": ["A", "B"]}], "delay_imports": []}
        actual = {"imports": [{"dll": "kernel32.dll", "symbols": ["A", "C"]}],
                  "delay_imports": [{"dll": "KERNEL32.DLL", "symbols": ["B"]}]}
        result = import_delta(actual, expected)
        self.assertEqual(result["imports"]["added"], [["KERNEL32.DLL", "C"]])
        self.assertEqual(result["imports"]["removed"], [["KERNEL32.DLL", "B"]])
        self.assertEqual(result["delay_imports"]["added"], [["KERNEL32.DLL", "B"]])
        self.assertFalse(result["exact_match"])


if __name__ == "__main__":
    unittest.main()
