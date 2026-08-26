from __future__ import annotations

import argparse
import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import tools.audit_w3_stock_bootloader as audit_module
from tools.audit_w3_stock_bootloader import (
    AuditInputError,
    SOURCE_FILES,
    audit_pe,
    audit_sources,
    build_audit,
    evaluate_assertions,
    file_fact,
    main,
    validate_pinned_inputs,
)


class W3StockBootloaderAuditTests(unittest.TestCase):
    def _stock_source_tree(self, root: Path) -> Path:
        contents = {
            "bootloader/src/pyi_main.c": "\n".join(
                (
                    "pyi_path_dirname(executable_dir, pyi_ctx->executable_filename);",
                    "SetDllDirectoryW(dllpath_w);",
                    "ret = pyi_launch_execute(pyi_ctx);",
                )
            ),
            "bootloader/src/pyi_launch.c": "\n".join(
                (
                    "pyi_ctx->dylib_python = pyi_dylib_python_load(",
                    "if (pyi_python_start_interpreter(pyi_ctx))",
                    "if (pyi_python_import_modules(pyi_ctx))",
                )
            ),
            "bootloader/src/pyi_dylib_python.c": (
                "LoadLibraryExW(dll_fullpath, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);"
            ),
            "bootloader/src/pyi_utils_win32_low_level.c": "GetFinalPathNameByHandleW",
            "PyInstaller/loader/pyiboot01_bootstrap.py": "sys._MEIPASS",
            "PyInstaller/loader/pyimod03_ctypes.py": (
                "search_dirs = [sys._MEIPASS] + os.environ['PATH'].split(os.pathsep)"
            ),
        }
        self.assertEqual(set(contents), set(SOURCE_FILES))
        for relative_path, text in contents.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        return root

    def test_stock_source_order_does_not_imply_w3_primitives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            facts = audit_sources(
                self._stock_source_tree(Path(temporary_directory) / "source")
            )

        self.assertTrue(facts["stock_order_confirmed"])
        self.assertEqual(facts["token_counts"]["GetFinalPathNameByHandleW"], 1)
        self.assertEqual(facts["token_counts"]["GetFileInformationByHandleEx"], 0)
        self.assertEqual(facts["token_counts"]["SetDefaultDllDirectories"], 0)
        self.assertEqual(facts["token_counts"]["TrustedSourceAuthority"], 0)

    def test_required_stock_assertions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = audit_sources(
                self._stock_source_tree(Path(temporary_directory) / "source")
            )
        assertions = evaluate_assertions(
            source,
            {"non_allowlisted_static_import_names": ["COMCTL32.DLL"]},
            {"ctypes_runtime_present": True},
        )

        self.assertEqual(
            [item["id"] for item in assertions],
            [
                "W3.STOCK.DLL_POLICY_BEFORE_PYTHON",
                "W3.STOCK.NATIVE_CLOSURE_PRELOAD",
                "W3.STOCK.ACTUAL_MODULE_REPROOF",
                "W3.STOCK.ATTESTATION_HANDOFF",
                "W3.STOCK.PREAUTHORITY_HOOK",
                "W3.STOCK.PE_SYSTEM_ONLY",
            ],
        )
        self.assertEqual({item["status"] for item in assertions}, {"FAIL"})

    def test_non_stock_static_facts_are_not_forced_to_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = audit_sources(
                self._stock_source_tree(Path(temporary_directory) / "source")
            )
        source["stock_order_confirmed"] = False
        for token in source["token_counts"]:
            source["token_counts"][token] = 1

        assertions = evaluate_assertions(
            source,
            {"non_allowlisted_static_import_names": []},
            {"ctypes_runtime_present": False},
        )

        self.assertEqual({item["status"] for item in assertions}, {"INDETERMINATE"})

    def test_pe_parser_records_static_delay_and_text_sections(self) -> None:
        class FakeImport:
            def __init__(self, name: bytes | None, ordinal: int | None = None):
                self.name = name
                self.ordinal = ordinal

        class FakeDirectoryEntry:
            def __init__(self, dll: bytes, imports: list[FakeImport]):
                self.dll = dll
                self.imports = imports

        class FakeSection:
            Name = b".text\0\0\0"

            @staticmethod
            def get_data() -> bytes:
                return b"native text"

        class FakePe:
            FILE_HEADER = types.SimpleNamespace(Machine=0x8664)
            OPTIONAL_HEADER = types.SimpleNamespace(Subsystem=2)
            DIRECTORY_ENTRY_IMPORT = [
                FakeDirectoryEntry(b"KERNEL32.dll", [FakeImport(b"LoadLibraryExW")])
            ]
            DIRECTORY_ENTRY_DELAY_IMPORT = [
                FakeDirectoryEntry(b"LATE.dll", [FakeImport(None, 7)])
            ]
            sections = [FakeSection()]

            @staticmethod
            def close() -> None:
                return None

        fake_pefile = types.SimpleNamespace(PE=lambda *_args, **_kwargs: FakePe())
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "sample.exe"
            binary.write_bytes(b"not parsed by fake pefile")
            with mock.patch.dict(sys.modules, {"pefile": fake_pefile}):
                fact = audit_pe(binary, "sample")

        self.assertEqual(fact["imports"][0]["dll"], "KERNEL32.dll")
        self.assertEqual(fact["delay_imports"][0]["dll"], "LATE.dll")
        self.assertEqual(fact["delay_imports"][0]["symbols"], ["ordinal:7"])
        self.assertEqual(fact["text_sha256"], fact["sections"][0]["sha256"])

    def test_pin_mismatch_is_an_input_error(self) -> None:
        source = {
            "pinned_hashes_match": True,
            "source_scan": {
                "count": audit_module.EXPECTED_SOURCE_SCAN_COUNT,
                "sha256": audit_module.EXPECTED_SOURCE_SCAN_SHA256,
            },
            "files": [{"path": "marker.c", "sha256": "a" * 64}],
        }
        sdist_source = {
            "count": audit_module.EXPECTED_SOURCE_SCAN_COUNT,
            "sha256": audit_module.EXPECTED_SOURCE_SCAN_SHA256,
            "marker_file_hashes": {"marker.c": "a" * 64},
        }
        stock_pe = {
            "sha256": audit_module.EXPECTED_STOCK_RUNW_SHA256,
            "text_sha256": audit_module.EXPECTED_STOCK_TEXT_SHA256,
        }
        baseline_pe = {
            "sha256": audit_module.EXPECTED_BASELINE_EXE_SHA256,
            "text_sha256": audit_module.EXPECTED_STOCK_TEXT_SHA256,
        }
        baseline = {
            "matrix": {"sha256": audit_module.EXPECTED_BASELINE_MATRIX_SHA256},
            "dist_inventory": {"sha256": audit_module.EXPECTED_BASELINE_INVENTORY_SHA256},
            "analysis_toc": {"sha256": audit_module.EXPECTED_BASELINE_ANALYSIS_SHA256},
            "checksums": {"sha256": audit_module.EXPECTED_BASELINE_CHECKSUMS_SHA256},
            "schema": "localcat.windows.baseline-matrix.v1",
            "repository_commit": audit_module.EXPECTED_BASELINE_COMMIT,
            "run_id": audit_module.EXPECTED_BASELINE_RUN_ID,
            "environment": {
                "python": "CPython 3.14.7 x64",
                "pyinstaller": audit_module.EXPECTED_PYINSTALLER_VERSION,
            },
            "inventory_file_count": 171,
            "relationships": {"all": True},
        }

        with self.assertRaisesRegex(AuditInputError, "SDIST_DIGEST"):
            validate_pinned_inputs(
                python_version="3.14.7",
                pyinstaller_version=audit_module.EXPECTED_PYINSTALLER_VERSION,
                source_archive_fact={"sha256": "0" * 64},
                python_dll_fact={"sha256": audit_module.EXPECTED_PYTHON_DLL_SHA256},
                source=source,
                sdist_source=sdist_source,
                stock_pe=stock_pe,
                baseline_pe=baseline_pe,
                baseline=baseline,
            )

    def test_current_pinned_windows_inputs_produce_relational_no_go(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        task_root = repository.parent
        baseline_root = (
            repository
            / "artifacts/windows"
            / audit_module.EXPECTED_BASELINE_COMMIT
            / audit_module.EXPECTED_BASELINE_RUN_ID
        )
        paths = {
            "stock_bootloader": task_root
            / "fresh-task1-1-b925-20260826/venv/Lib/site-packages/PyInstaller/bootloader/Windows-64bit-intel/runw.exe",
            "baseline_exe": baseline_root / "dist-clean/LocalCAT-ui-mvp/LocalCAT-ui-mvp.exe",
            "python_dll": task_root / "cpython-3.14.7/python314.dll",
            "source_archive": task_root / "w3-sources/pyinstaller-6.22.2.tar.gz",
            "source_root": task_root / "w3-sources/pyinstaller-6.22.2",
            "baseline_matrix": baseline_root / "matrix.json",
            "baseline_inventory": baseline_root / "dist-clean-sha256.json",
            "baseline_checksums": baseline_root / "checksums.sha256",
            "baseline_analysis": baseline_root / "build-clean/LocalCAT-ui-mvp/Analysis-00.toc",
        }
        if os.name != "nt" or not all(path.exists() for path in paths.values()):
            self.skipTest("pinned Windows Task 1.4 integration inputs are unavailable")

        audit = build_audit(argparse.Namespace(**paths))

        self.assertEqual(audit["input_gate"]["status"], "PASS")
        self.assertEqual(audit["result"], "NO_GO")
        self.assertTrue(audit["gate_effect"]["stock_task_1_4_complete"])
        self.assertTrue(audit["gate_effect"]["source_lane_allowed"])
        self.assertFalse(audit["gate_effect"]["w3_feasibility_task_1_6_complete"])
        self.assertFalse(audit["gate_effect"]["task_7_allowed"])
        self.assertEqual({item["status"] for item in audit["required_assertions"]}, {"FAIL"})
        self.assertEqual(audit["stock_bootloader_pe"]["delay_imports"], [])
        self.assertEqual(audit["baseline_executable_pe"]["delay_imports"], [])
        self.assertEqual(
            audit["pinned_sdist_source"]["sha256"],
            audit["pinned_source"]["source_scan"]["sha256"],
        )
        self.assertTrue(audit["baseline_provenance"]["ctypes_runtime_present"])
        self.assertTrue(
            audit["baseline_provenance"]["relationships"]["stock_baseline_text_equal"]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            tampered_archive = Path(temporary_directory) / "pyinstaller-6.22.2.tar.gz"
            shutil.copyfile(paths["source_archive"], tampered_archive)
            with tampered_archive.open("ab") as stream:
                stream.write(b"tamper")
            tampered_paths = dict(paths)
            tampered_paths["source_archive"] = tampered_archive
            with self.assertRaisesRegex(AuditInputError, "SDIST_DIGEST"):
                build_audit(argparse.Namespace(**tampered_paths))

    def test_file_fact_does_not_persist_private_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "input.bin"
            path.write_bytes(b"pinned bytes")
            fact = file_fact("fixture", path)

        self.assertEqual(fact["name"], "input.bin")
        self.assertNotIn("path", fact)
        self.assertEqual(len(fact["sha256"]), 64)

    def test_missing_inputs_return_input_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        missing = "definitely-missing-w3-audit-input"
        with contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "--stock-bootloader",
                    missing,
                    "--baseline-exe",
                    missing,
                    "--python-dll",
                    missing,
                    "--source-archive",
                    missing,
                    "--source-root",
                    missing,
                    "--baseline-matrix",
                    missing,
                    "--baseline-inventory",
                    missing,
                    "--baseline-checksums",
                    missing,
                    "--baseline-analysis",
                    missing,
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("W3_AUDIT_INPUT_ERROR", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_pin_mismatch_main_exit_is_two(self) -> None:
        stderr = io.StringIO()
        argv = []
        for option in (
            "stock-bootloader",
            "baseline-exe",
            "python-dll",
            "source-archive",
            "source-root",
            "baseline-matrix",
            "baseline-inventory",
            "baseline-checksums",
            "baseline-analysis",
        ):
            argv.extend((f"--{option}", "unused"))
        with mock.patch.object(
            audit_module,
            "build_audit",
            side_effect=AuditInputError("pinned input mismatch: TEST"),
        ), contextlib.redirect_stderr(stderr):
            result = main(argv)

        self.assertEqual(result, 2)
        self.assertIn("pinned input mismatch", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
