from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest import mock

from tools.record_windows_frozen_entry_probe import _canonical_log_fact
from tools.prepare_windows_frozen_entry_inputs import (
    CandidateInputError,
    _directory_input_fact,
    _expected_entry_imports,
    _json_digest,
    _load_entry_contract,
    _pe_fact,
    _target_platform_fact,
    _toolchain_probe_fact,
    _validated_vs_instance,
    _validate_probe_build_evidence,
    _validate_dynamic_loader,
    _venv_semantics,
    _verified_date,
    python_api_table_for_314,
)


class WindowsFrozenEntryInputTests(unittest.TestCase):
    def test_patch_owner_inventory_requires_real_pristine_existing_files(self) -> None:
        from tools.prepare_windows_frozen_entry_inputs import _validate_patch_owner_inventory
        policy = {"owned_existing_files": ["bootloader/main.c"],
                  "owned_new_files": ["bootloader/native.c"]}
        _validate_patch_owner_inventory(policy, {"bootloader/main.c"})
        for names in (set(), {"bootloader/main.c", "bootloader/native.c"}):
            with self.subTest(names=names), self.assertRaises(CandidateInputError):
                _validate_patch_owner_inventory(policy, names)
        with self.assertRaises(CandidateInputError):
            _validate_patch_owner_inventory(
                {**policy, "owned_existing_files": ["bootloader/main.c", "bootloader/main.c"]},
                {"bootloader/main.c"})

    def test_custom_api_target_removes_stock_only_functions_explicitly(self) -> None:
        from tools.prepare_windows_frozen_entry_inputs import _target_python_functions
        policy = {
            "removed_base_functions": ["Py_Finalize"],
            "additional_functions": ["Py_SetPath"],
            "function_signatures": {"Py_SetPath": "void (*)(const wchar_t *)"},
        }
        self.assertEqual(
            _target_python_functions(["Py_IsInitialized", "Py_Finalize"], policy),
            ["Py_IsInitialized", "Py_SetPath"],
        )
        for removed in (["missing"], ["Py_Finalize", "Py_Finalize"]):
            with self.subTest(removed=removed), self.assertRaises(CandidateInputError):
                _target_python_functions(["Py_IsInitialized", "Py_Finalize"],
                                         {**policy, "removed_base_functions": removed})
        with self.assertRaises(CandidateInputError):
            _target_python_functions(["Py_IsInitialized", "Py_Finalize"],
                                     {**policy, "removed_base_functions": ["Py_SetPath"]})

    def test_e9_path_contract_has_only_approved_compatibility_abi_and_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract, _ = _load_entry_contract(root / "packaging/windows/frozen-entry/candidate-contract.json")
        api = contract["python_c_api"]
        self.assertIn("Py_SetPath", api["additional_functions"])
        self.assertEqual(api["function_signatures"]["Py_SetPath"], "void (*)(const wchar_t *)")
        self.assertNotIn("PyList_SetSlice", api["additional_functions"])
        self.assertEqual(contract["interpreter_sources"], [
            {"id": "encoding-package", "module": "encodings", "path": "Lib/encodings/__init__.py"},
            {"id": "encoding-aliases", "module": "encodings.aliases", "path": "Lib/encodings/aliases.py"},
            {"id": "encoding-utf8", "module": "encodings.utf_8", "path": "Lib/encodings/utf_8.py"},
            {"id": "encoding-win", "module": "encodings._win_cp_codecs", "path": "Lib/encodings/_win_cp_codecs.py"},
            {"id": "importlib-external", "module": "_frozen_importlib_external", "path": "Lib/importlib/_bootstrap_external.py"},
        ])

    def test_repository_custom_api_is_exact_minimal_entry_table(self) -> None:
        from tools.prepare_windows_frozen_entry_inputs import _target_python_functions
        root = Path(__file__).resolve().parents[1] / "packaging/windows/frozen-entry"
        contract, _ = _load_entry_contract(root / "candidate-contract.json")
        record = json.loads((root / "candidate-input.lock.json").read_text(encoding="utf-8"))
        expected = set("""
            PyBool_FromLong PyBytes_FromStringAndSize PyErr_Clear PyErr_SetString
            PyEval_EvalCode PyImport_ImportModule PyInitConfig_AddModule
            PyInitConfig_Create PyInitConfig_Free PyInitConfig_SetInt
            PyInitConfig_SetStr PyInitConfig_SetStrList PyLong_FromLong
            PyLong_FromUnsignedLongLong PyModule_AddObjectRef PyModule_Create2
            PyModule_GetDict PyObject_CallFunctionObjArgs PyObject_Free
            PyObject_GetAttrString PyObject_SetAttrString PyObject_Str
            PySys_GetObject PyThreadState_Get PyThreadState_GetInterpreter
            PyThread_get_thread_ident PyTuple_New PyTuple_SetItem PyType_FromSpec
            PyType_GenericNew PyUnicode_AsUTF8 PyUnicode_FromString
            Py_CompileStringExFlags Py_DecRef Py_InitializeFromInitConfig
            Py_IsInitialized Py_SetPath
        """.split())
        target = _target_python_functions(
            record["runtime"]["pyinstaller"]["python_314_api"]["symbols"],
            contract["python_c_api"],
        )
        self.assertEqual(set(target), expected)
        self.assertEqual(len(target), 37)
        actual = record["entry_contract"]["python_c_api"]
        self.assertEqual(actual["function_symbols"], target)
        self.assertEqual(actual["data_symbols"], ["PyExc_RuntimeError"])
        self.assertEqual(actual["symbol_count"], 38)
        imports = {item["dll"]: item["symbols"]
                   for item in record["entry_contract"]["expected_pe"]["imports"]}
        self.assertEqual(set(imports), {"KERNEL32.DLL", "USER32.DLL"})
        self.assertEqual(imports["USER32.DLL"], ["MessageBoxA"])
        self.assertIn("OutputDebugStringW", imports["KERNEL32.DLL"])

    def test_interpreter_input_fact_binds_exact_bytes_and_rejects_missing(self) -> None:
        from tools import prepare_windows_frozen_entry_inputs as producer
        root = Path(__file__).resolve().parents[1]
        contract = json.loads((root / "packaging/windows/frozen-entry/candidate-contract.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            for index, member in enumerate(contract["interpreter_sources"]):
                source = runtime / member["path"]
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"# fixture " + str(index).encode() + b"\r\n")
            facts = producer._interpreter_source_facts(runtime, contract["interpreter_sources"])
            self.assertEqual(len(facts), 5)
            for member, fact in zip(contract["interpreter_sources"], facts):
                data = (runtime / member["path"]).read_bytes()
                self.assertEqual(fact, {**member, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            source.write_bytes(b"changed source\n")
            changed = producer._interpreter_source_facts(runtime, contract["interpreter_sources"])
            self.assertNotEqual(facts[-1]["sha256"], changed[-1]["sha256"])
            source.unlink()
            with self.assertRaisesRegex(CandidateInputError, "missing"):
                producer._interpreter_source_facts(runtime, contract["interpreter_sources"])

    def test_interpreter_source_declaration_cannot_drift(self) -> None:
        root = Path(__file__).resolve().parents[1]
        original = json.loads((root / "packaging/windows/frozen-entry/candidate-contract.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            for mutation in ("missing", "extra", "duplicate", "path", "module", "id", "unknown"):
                changed = json.loads(json.dumps(original))
                members = changed["interpreter_sources"]
                if mutation == "missing": members.pop()
                elif mutation == "extra": members.append({"id": "other", "module": "other", "path": "Lib/other.py"})
                elif mutation == "duplicate": members[-1] = members[0]
                elif mutation == "path": members[-1]["path"] = "../external.py"
                elif mutation == "module": members[-1]["module"] = "importlib._bootstrap_external"
                elif mutation == "id": members[-1]["id"] = "other"
                else: members[-1]["fallback"] = "frozen"
                path.write_text(json.dumps(changed))
                with self.subTest(mutation=mutation), self.assertRaises(CandidateInputError):
                    _load_entry_contract(path)

    def test_directory_digest_is_ordered_and_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "z").mkdir()
            (root / "z/value.h").write_bytes(b"same")
            (root / "a.h").write_bytes(b"other")
            first = _directory_input_fact(root, "fixture")
            second = _directory_input_fact(root, "fixture")

            self.assertEqual(first, second)
            self.assertEqual(first["file_count"], 2)
            (root / "z/value.h").rename(root / "z/renamed.h")
            renamed = _directory_input_fact(root, "fixture")

        self.assertNotEqual(first["sha256"], renamed["sha256"])

    def test_directory_digest_rejects_empty_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(CandidateInputError, "empty fixture"):
                _directory_input_fact(Path(temporary_directory), "fixture")

    def test_python_314_api_selects_pep741_branch_only(self) -> None:
        source = """
static int _pyi_dylib_python_import_symbols(void) {
    _IMPORT_FUNCTION(Py_DecRef)
    if (dylib->has_pep741) {
        PYI_EXT_FUNC_BIND(dylib->handle, PyInitConfig_Create, dylib->PyInitConfig_Create);
        _IMPORT_FUNCTION(PyInitConfig_Free)
        _IMPORT_FUNCTION(Py_InitializeFromInitConfig)
    } else {
        _IMPORT_FUNCTION(PyConfig_Clear)
        _IMPORT_FUNCTION(Py_InitializeFromConfig)
    }
    _IMPORT_FUNCTION(PyErr_Clear)
}
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "pyi_dylib_python.c"
            path.write_text(source, encoding="utf-8")
            path.with_name("pyi_dylib_python.h").write_text(
                "/* pinned binding declarations */\n", encoding="utf-8"
            )
            table = python_api_table_for_314(path)

        self.assertEqual(
            table["symbols"],
            [
                "Py_DecRef",
                "PyInitConfig_Create",
                "PyInitConfig_Free",
                "Py_InitializeFromInitConfig",
                "PyErr_Clear",
            ],
        )
        self.assertNotIn("PyConfig_Clear", table["symbols"])
        self.assertEqual(table["binding_header_name"], "pyi_dylib_python.h")

    def test_python_api_rejects_unknown_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "pyi_dylib_python.c"
            path.write_text("_IMPORT_FUNCTION(Py_DecRef)\n", encoding="utf-8")
            with self.assertRaisesRegex(CandidateInputError, "unrecognized"):
                python_api_table_for_314(path)

    def test_host_gate_rejects_non_windows_or_non_amd64(self) -> None:
        with mock.patch("platform.system", return_value="Linux"):
            with self.assertRaisesRegex(CandidateInputError, "Windows AMD64"):
                _target_platform_fact()
        with (
            mock.patch("platform.system", return_value="Windows"),
            mock.patch("platform.machine", return_value="ARM64"),
        ):
            with self.assertRaisesRegex(CandidateInputError, "Windows AMD64"):
                _target_platform_fact()

    def test_visual_studio_gate_rejects_incomplete_instance(self) -> None:
        instance = {
            "installation_version": "17.14.37614.0",
            "product_id": "Microsoft.VisualStudio.Product.BuildTools",
            "product_line_version": "2022",
            "product_display_version": "17.14.39",
            "is_complete": False,
            "is_launchable": True,
        }
        with mock.patch(
            "tools.prepare_windows_frozen_entry_inputs._vs_instance",
            return_value=instance,
        ):
            with self.assertRaisesRegex(CandidateInputError, "not complete"):
                _validated_vs_instance(Path("vswhere.exe"), Path("VS"))

    def test_verified_date_is_exact_iso_calendar_date(self) -> None:
        self.assertEqual(_verified_date("2026-08-27"), "2026-08-27")
        for value in ("2026-8-27", "2026-02-30", "not-a-date"):
            with self.assertRaises(Exception):
                _verified_date(value)

    def test_venv_semantics_ignore_absolute_install_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.cfg"
            second = root / "second.cfg"
            first.write_text(
                "home = C:\\Python314\ninclude-system-site-packages = false\n",
                encoding="utf-8",
            )
            second.write_text(
                "home = D:\\Different\\Python314\n"
                "include-system-site-packages = false\n",
                encoding="utf-8",
            )

            self.assertEqual(_venv_semantics(first), _venv_semantics(second))

    def test_probe_log_fact_normalizes_root_case_and_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.log"
            second = root / "second.log"
            first.write_bytes(
                b"Setting top: E:\\ProbeA\\bootloader\n"
                b"Compiler: E:\\SDK\\MSVC\\cl.exe\n"
                b"finished (6.140s)\n"
            )
            second.write_bytes(
                b"Setting top: d:\\probeB\\bootloader\r\n"
                b"Compiler: e:\\sdk\\msvc\\cl.exe\r\n"
                b"finished (5.849s)\r\n"
            )

            first_fact = _canonical_log_fact(
                first,
                "probe stdout",
                replacements=[("E:\\ProbeA", "<PROBE>"), ("E:\\SDK\\MSVC", "<MSVC>")],
            )
            second_fact = _canonical_log_fact(
                second,
                "probe stdout",
                replacements=[("D:\\ProbeB", "<PROBE>"), ("E:\\SDK\\MSVC", "<MSVC>")],
            )

            self.assertEqual(first_fact, second_fact)

    def test_entry_import_target_is_exact_stock_delta(self) -> None:
        stock = {
            "sha256": "a" * 64,
            "delay_imports": [],
            "imports": [
                {"dll": "COMCTL32.dll", "symbols": ["ordinal:380"]},
                {
                    "dll": "KERNEL32.dll",
                    "symbols": ["CloseHandle", "SetDllDirectoryW"],
                },
            ],
        }
        contract = {
            "pe_allowlist": {
                "base_stock_runw_sha256": "a" * 64,
                "expected_dll_names": ["KERNEL32.DLL"],
                "remove_dlls": ["COMCTL32.DLL"],
                "remove_symbols": {"KERNEL32.DLL": ["SetDllDirectoryW"]},
                "add_symbols": {"KERNEL32.DLL": ["SetDefaultDllDirectories"]},
            }
        }

        expected = _expected_entry_imports(stock, contract)

        self.assertEqual(
            expected,
            [
                {
                    "dll": "KERNEL32.DLL",
                    "symbols": ["CloseHandle", "SetDefaultDllDirectories"],
                }
            ],
        )

    def test_toolchain_probe_binds_import_table_not_probe_bytes(self) -> None:
        stock = {
            "imports": [{"dll": "KERNEL32.dll", "symbols": ["CloseHandle"]}],
            "delay_imports": [],
        }
        probe = {
            "sha256": "different-build-bytes",
            "machine": 0x8664,
            "imports": [
                {
                    "dll": "KERNEL32.dll",
                    "symbols": ["CloseHandle", "TlsAlloc"],
                }
            ],
            "delay_imports": [],
        }
        import_digest = _json_digest(
            {"imports": probe["imports"], "delay_imports": probe["delay_imports"]}
        )
        contract = {
            "pe_allowlist": {
                "compiler_probe": {
                    "purpose": "test observation",
                    "import_table_sha256": import_digest,
                    "added_symbols": {"KERNEL32.DLL": ["TlsAlloc"]},
                    "removed_symbols": {},
                }
            }
        }

        summary = _toolchain_probe_fact(stock, probe, contract)

        self.assertNotIn("sha256", summary)
        self.assertEqual(summary["import_table_sha256"], import_digest)

    def test_entry_contract_rejects_unknown_and_duplicate_keys(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source = json.loads(
            (
                repository_root
                / "packaging/windows/frozen-entry/candidate-contract.json"
            ).read_text(encoding="utf-8")
        )
        source["unexpected"] = True
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "unknown.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(CandidateInputError, "unknown"):
                _load_entry_contract(path)

            duplicate = Path(temporary_directory) / "duplicate.json"
            duplicate.write_text(
                '{"schema":"localcat.windows-frozen-entry-target-contract.v1",'
                '"schema":"duplicate"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CandidateInputError, "duplicate JSON key"):
                _load_entry_contract(duplicate)

    def test_repository_entry_contract_uses_product_runtime_names(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[1]
            / "packaging/windows/frozen-entry/candidate-contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(contract["runtime_abi"]["module"], "_localcat_frozen_bootstrap")
        self.assertNotIn("w3", json.dumps(contract["runtime_abi"]).lower())
        self.assertIn("PyInitConfig_AddModule", contract["python_c_api"]["additional_functions"])
        self.assertNotIn(
            "PyImport_AppendInittab", contract["python_c_api"]["additional_functions"]
        )
        self.assertEqual(
            set(contract["python_c_api"]["function_signatures"]),
            set(contract["python_c_api"]["additional_functions"]),
        )
        self.assertEqual(
            contract["pe_allowlist"]["expected_dll_names"],
            ["KERNEL32.DLL", "USER32.DLL"],
        )

    def test_os_service_is_separate_from_non_system_dispatcher(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract, _ = _load_entry_contract(
            root / "packaging/windows/frozen-entry/candidate-contract.json"
        )
        loader = contract["dynamic_loader"]
        self.assertEqual(
            loader["non_system_dispatcher"], "localcat_native_closure_load_verified"
        )
        calls = loader["application_system_calls"]
        self.assertEqual(len(calls), 10)
        self.assertEqual(loader["pre_authority_external_calls"], [
            "LoadLibraryExW through localcat_native_closure_load_verified",
            *[call["id"] for call in calls],
        ])
        self.assertEqual([call["flags"] for call in calls[:3]], [0x800] * 3)
        self.assertEqual([call["targets"][-1] for call in calls[:3]],
                         ["kernel32", "kernel32", "kernelbase"])
        self.assertEqual([call["targets"] for call in calls[5:]], [
            ["kernelbase.dll"], ["ntdll.dll"], ["kernel32.dll"], ["bcrypt.dll"], ["psapi.dll"]])
        self.assertEqual(calls[-1]["phase"], "AFTER_E1_INCLUDING_EARLY_EXIT")
        self.assertEqual(calls[-2]["condition"], "os-random-request-and-empty-function-cache")
        target = loader["system_service_boundary"]
        self.assertEqual(target["authority"], "Windows OS")
        self.assertEqual(target["rng"], {
            "dll": "bcrypt.dll", "symbol": "BCryptGenRandom",
            "hAlgorithm": 0, "flags": 2,
        })
        self.assertNotIn("members", target)

    def test_crt_exit_call_does_not_assume_policy_success_or_authorize_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract, _ = _load_entry_contract(root / "packaging/windows/frozen-entry/candidate-contract.json")
        calls = {item["id"]: item for item in contract["dynamic_loader"]["application_system_calls"]}
        self.assertIn("crt-ucrt-process-termination", calls)
        self.assertEqual(calls["crt-ucrt-process-termination"], {
            "id": "crt-ucrt-process-termination", "owner": "custom-entry-static-crt",
            "phase": "CRT_EXIT_INCLUDING_E1_FAILURE", "loader": "LoadLibraryExW",
            "targets": ["api-ms-win-appmodel-runtime-l1-1-2"], "flags": 0x800,
            "condition": "crt-termination-non-secure-and-empty-function-and-module-cache",
            "fallback": None, "requires": "POLICY_INDEPENDENT_SYSTEM_RESOLUTION_PROOF",
        })

    def test_crt_multibyte_call_has_exact_pre_entry_resolution_and_symbol_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract, _ = _load_entry_contract(root / "packaging/windows/frozen-entry/candidate-contract.json")
        calls = {item["id"]: item for item in contract["dynamic_loader"]["application_system_calls"]}
        self.assertIn("crt-ucrt-multibyte-casing", calls)
        self.assertEqual(calls["crt-ucrt-multibyte-casing"], {
            "id": "crt-ucrt-multibyte-casing", "owner": "custom-entry-static-crt",
            "phase": "E0.5", "loader": "LoadLibraryExW",
            "targets": ["api-ms-win-core-localization-l1-2-1", "kernel32"], "flags": 0x800,
            "condition": "crt-multibyte-initialization-non-utf8-acp-and-getcpinfo-success-and-empty-function-and-module-cache",
            "module_selection": "first-available-module-next-candidate-on-load-failure-only",
            "symbols": ["LCMapStringEx", "LocaleNameToLCID"],
            "symbol_fallback": {
                "LCMapStringEx": "static-LCMapStringW-with-LocaleNameToLCID-conversion",
                "LocaleNameToLCID": "downlevel-conversion-no-loader",
                "resolution": "same-candidate-list-and-module-cache-no-next-candidate-on-missing-symbol",
            },
            "fallback": {"error": 87, "targets": ["kernel32"], "flags": 0,
                         "excluded_prefixes": ["api-ms-", "ext-ms-"]},
            "requires": "PRE_ENTRY_SYSTEM_RESOLUTION_PROOF",
        })

    def test_multibyte_contract_rejects_missing_or_widened_resolution_details(self) -> None:
        root = Path(__file__).resolve().parents[1]
        loader = json.loads((root / "packaging/windows/frozen-entry/candidate-contract.json").read_bytes())["dynamic_loader"]
        self.assertIn("crt-ucrt-multibyte-casing", [item["id"] for item in loader["application_system_calls"]])
        edits = [
            lambda call: call.update(phase="AFTER_E1"),
            lambda call: call.update(condition="always"),
            lambda call: call.update(flags=0),
            lambda call: call["targets"].append("api-ms-win-core-localization-obsolete-l1-2-0"),
            lambda call: call["targets"].reverse(),
            lambda call: call.pop("module_selection"),
            lambda call: call.update(module_selection="next-candidate-on-missing-symbol"),
            lambda call: call["symbols"].append("LoadLibraryW"),
            lambda call: call["symbols"].remove("LocaleNameToLCID"),
            lambda call: call["symbol_fallback"].update(LCMapStringEx="dynamic-LCMapStringW"),
            lambda call: call["symbol_fallback"].update(LocaleNameToLCID="load-other-locale-provider"),
            lambda call: call["symbol_fallback"].update(resolution="next-candidate-on-missing-symbol"),
            lambda call: call["fallback"].update(error=0),
            lambda call: call["fallback"]["targets"].append("api-ms-win-core-localization-l1-2-1"),
            lambda call: call["fallback"].update(excluded_prefixes=["api-ms-"]),
            lambda call: call.update(requires="E1_SYSTEM32_AND_SYSTEM_RESOLUTION_PROOF"),
        ]
        for index, edit in enumerate(edits):
            changed = json.loads(json.dumps(loader))
            call = next(item for item in changed["application_system_calls"] if item["id"] == "crt-ucrt-multibyte-casing")
            edit(call)
            with self.subTest(index=index), self.assertRaises(CandidateInputError):
                _validate_dynamic_loader(changed)

    def test_system_call_contract_rejects_widened_targets_phases_flags_and_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        loader = json.loads((root / "packaging/windows/frozen-entry/candidate-contract.json").read_bytes())["dynamic_loader"]
        _validate_dynamic_loader(loader)
        edits = [
            lambda item: item.pop("application_system_calls"),
            lambda item: item["application_system_calls"].pop(),
            lambda item: item["application_system_calls"].append(item["application_system_calls"][0]),
            lambda item: item["pre_authority_external_calls"].append("unreviewed"),
            lambda item: item["allowed_flags"].append("LOAD_LIBRARY_SEARCH_DEFAULT_DIRS"),
            lambda item: item["application_system_calls"][0].update(flags=0),
            lambda item: item["application_system_calls"][0]["targets"].append("plugin.dll"),
            lambda item: item["application_system_calls"][0]["fallback"].update(error=0),
            lambda item: item["application_system_calls"][0]["fallback"].update(excluded_prefixes=[]),
            lambda item: item["application_system_calls"][0].update(requires="none"),
            lambda item: item["application_system_calls"][-1].update(phase="E0.5"),
            lambda item: item["application_system_calls"][-2].update(condition="always"),
            lambda item: item["application_system_calls"][3].update(phase="AFTER_E1"),
            lambda item: item["application_system_calls"][3].update(flags=0),
            lambda item: item["application_system_calls"][3].update(fallback={"targets": ["kernel32"], "flags": 0}),
            lambda item: item["application_system_calls"][3]["targets"].append("kernel.appcore.dll"),
            lambda item: item["application_system_calls"][3].update(condition="always"),
            lambda item: item["application_system_calls"][3].update(requires="E1_SYSTEM32_AND_SYSTEM_RESOLUTION_PROOF"),
        ]
        for index, edit in enumerate(edits):
            changed = json.loads(json.dumps(loader))
            edit(changed)
            with self.subTest(index=index), self.assertRaises(CandidateInputError):
                _validate_dynamic_loader(changed)

    def test_historical_no_go_cannot_reject_new_system_provider_boundary(self) -> None:
        from tools.audit_windows_frozen_custom_entry import validate_legacy_target as producer_gate
        from tools.validate_windows_frozen_custom_entry_no_go import validate_legacy_target as reviewer_gate
        old = {"schema": "localcat.windows-frozen-entry-candidate-input.v1"}
        revised = {"schema": "localcat.windows-frozen-entry-candidate-input.v2"}
        os_boundary = {"schema": "localcat.windows-frozen-entry-candidate-input.v3"}
        disguised = {**old, "entry_contract": {"dynamic_loader": {"system_provider_target": {}}}}
        disguised_os = {**old, "entry_contract": {"dynamic_loader": {"system_service_boundary": {}}}}
        for gate in (producer_gate, reviewer_gate):
            gate(old)
            for invalid in (revised, os_boundary, disguised, disguised_os):
                with self.subTest(gate=gate.__module__, invalid=invalid):
                    with self.assertRaisesRegex(SystemExit, "historical NO-GO"):
                        gate(invalid)

    def test_repository_candidate_lock_is_self_consistent_and_portable(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        lock_path = (
            repository_root
            / "packaging/windows/frozen-entry/candidate-input.lock.json"
        )
        record = json.loads(lock_path.read_text(encoding="utf-8"))
        authority_payload = {
            key: value
            for key, value in record.items()
            if key not in {"assertions", "candidate_input_digest", "status"}
        }

        self.assertEqual(record["status"], "MATERIALIZED")
        self.assertEqual({item["status"] for item in record["assertions"]}, {"PASS"})
        self.assertEqual(record["candidate_input_digest"], _json_digest(authority_payload))
        self.assertNotIn("host_observation", record)
        self.assertNotIn("catalogs", record["toolchain"]["windows_sdk"])
        self.assertIsNone(
            re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", json.dumps(record, ensure_ascii=False))
        )
        producer = record["evidence_producer"]
        from tools.windows_frozen_packaging import APPLICATION_MANIFEST, BUILD_SOURCE_FILES, fact
        packaging = producer["custom_packaging"]
        self.assertEqual(set(packaging["source_files"]), set(BUILD_SOURCE_FILES))
        for name in BUILD_SOURCE_FILES:
            self.assertEqual(packaging["source_files"][name], fact((repository_root / name).read_bytes()))
        self.assertEqual(packaging["application_manifest"], fact(APPLICATION_MANIFEST))
        self.assertGreater(packaging["dependency_inventory"]["file_count"], 0)
        self.assertEqual(
            producer["preparer"]["sha256"],
            hashlib.sha256(
                (repository_root / "tools/prepare_windows_frozen_entry_inputs.py").read_bytes()
            ).hexdigest(),
        )
        self.assertNotIn("system_profile_collector", producer)
        contract, contract_fact = _load_entry_contract(repository_root /
            "packaging/windows/frozen-entry/candidate-contract.json"
        )
        self.assertEqual(record["entry_contract"]["contract_file"], contract_fact)
        self.assertEqual(record["entry_contract"]["dynamic_loader"], contract["dynamic_loader"])
        self.assertEqual(record["entry_contract"]["interpreter_sources"], contract["interpreter_sources"])
        self.assertEqual(record["entry_contract"]["python_c_api"]["additional_function_signatures"]["Py_SetPath"],
                         "void (*)(const wchar_t *)")
        self.assertEqual([{key: fact[key] for key in ("id", "module", "path")}
                          for fact in record["runtime"]["cpython"]["interpreter_sources"]], contract["interpreter_sources"])
        for fact in record["runtime"]["cpython"]["interpreter_sources"]:
            self.assertGreater(fact["bytes"], 0)
            self.assertRegex(fact["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("system_provider_profile", record["entry_contract"])
        self.assertEqual(
            producer["replay_wrapper"]["sha256"],
            hashlib.sha256(
                (repository_root / "tools/replay_windows_frozen_entry_inputs.ps1").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            producer["probe_prebuild_evidence_recorder"]["sha256"],
            hashlib.sha256(
                (
                    repository_root
                    / "tools/record_windows_frozen_entry_probe_prebuild.py"
                ).read_bytes()
            ).hexdigest(),
        )
        probe_evidence = record["toolchain"]["msvc"]["stock_rebuild_probe"][
            "build_evidence"
        ]
        self.assertEqual(probe_evidence["status"], "RECORDED")
        evidence_payload = {
            key: value
            for key, value in probe_evidence.items()
            if key not in {"evidence_digest", "status"}
        }
        self.assertEqual(probe_evidence["evidence_digest"], _json_digest(evidence_payload))
        self.assertEqual(
            probe_evidence["source"]["actual_build_copy_source_scan"],
            record["runtime"]["pyinstaller"]["source_scan"],
        )
        self.assertEqual(
            probe_evidence["source"]["actual_build_copy_build_driver"],
            record["runtime"]["pyinstaller"]["build_driver"],
        )
        prebuild_payload = {
            "schema": "localcat.windows-frozen-entry-toolchain-probe-prebuild-evidence.v1",
            "source": {
                key: value
                for key, value in probe_evidence["source"].items()
                if key != "prebuild_evidence_digest"
            },
        }
        self.assertEqual(
            probe_evidence["source"]["prebuild_evidence_digest"],
            _json_digest(prebuild_payload),
        )

    def test_repository_probe_evidence_rejects_command_tamper(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        record = json.loads(
            (
                repository_root
                / "packaging/windows/frozen-entry/candidate-input.lock.json"
            ).read_text(encoding="utf-8")
        )
        probe = record["toolchain"]["msvc"]["stock_rebuild_probe"]
        evidence = json.loads(json.dumps(probe["build_evidence"]))
        evidence["build"]["arguments"].append("--unapproved")

        with self.assertRaisesRegex(CandidateInputError, "command/environment"):
            _validate_probe_build_evidence(
                evidence,
                source_audit={
                    "source_scan": record["runtime"]["pyinstaller"]["source_scan"]
                },
                archive_audit=record["runtime"]["pyinstaller"]["archive_source_scan"],
                build_driver=record["runtime"]["pyinstaller"]["build_driver"],
                tools=record["toolchain"]["msvc"]["tools"],
                msvc_version=record["toolchain"]["msvc"]["toolset_version"],
                sdk_version=record["toolchain"]["windows_sdk"]["version"],
                probe_fact=probe,
            )

    def test_pe_fact_records_imports_delay_imports_and_exports(self) -> None:
        class FakeImport:
            def __init__(self, name: bytes | None, ordinal: int | None = None):
                self.name = name
                self.ordinal = ordinal

        class FakeDirectory:
            def __init__(self, dll: bytes, imports: list[FakeImport]):
                self.dll = dll
                self.imports = imports

        fake_pe = types.SimpleNamespace(
            FILE_HEADER=types.SimpleNamespace(Machine=0x8664),
            DIRECTORY_ENTRY_IMPORT=[
                FakeDirectory(b"KERNEL32.dll", [FakeImport(b"CloseHandle")])
            ],
            DIRECTORY_ENTRY_DELAY_IMPORT=[
                FakeDirectory(b"LATE.dll", [FakeImport(None, 7)])
            ],
            DIRECTORY_ENTRY_EXPORT=types.SimpleNamespace(
                symbols=[FakeImport(b"Py_DecRef"), FakeImport(None, 9)]
            ),
            parse_data_directories=lambda **_kwargs: None,
            close=lambda: None,
        )
        fake_pefile = types.SimpleNamespace(
            DIRECTORY_ENTRY={
                "IMAGE_DIRECTORY_ENTRY_IMPORT": 1,
                "IMAGE_DIRECTORY_ENTRY_EXPORT": 0,
                "IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT": 13,
            },
            PE=lambda *_args, **_kwargs: fake_pe,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "python314.dll"
            path.write_bytes(b"fake PE")
            with mock.patch.dict("sys.modules", {"pefile": fake_pefile}):
                fact = _pe_fact(path, "fixture", exports=True)

        self.assertEqual(fact["machine"], 0x8664)
        self.assertEqual(fact["imports"][0]["symbols"], ["CloseHandle"])
        self.assertEqual(fact["delay_imports"][0]["symbols"], ["ordinal:7"])
        self.assertEqual(fact["exports"], ["Py_DecRef", "ordinal:9"])


if __name__ == "__main__":
    unittest.main()
