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
    _venv_semantics,
    _verified_date,
    python_api_table_for_314,
)


class WindowsFrozenEntryInputTests(unittest.TestCase):
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
            ["ADVAPI32.DLL", "GDI32.DLL", "KERNEL32.DLL", "USER32.DLL"],
        )

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
        self.assertIsNone(
            re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", json.dumps(record, ensure_ascii=False))
        )
        producer = record["evidence_producer"]
        self.assertEqual(
            producer["preparer"]["sha256"],
            hashlib.sha256(
                (repository_root / "tools/prepare_windows_frozen_entry_inputs.py").read_bytes()
            ).hexdigest(),
        )
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
