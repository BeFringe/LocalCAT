from __future__ import annotations

import ast
import json
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from excel_adapter_openpyxl import run_file_mode_benchmark
import logic_controller
from tests.test_tm_activation_journal import SOURCE_BYTES, _first_prepared


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExcelAdapterContractTest(unittest.TestCase):
    def test_legacy_and_activated_sidecar_parity_for_identical_inputs(self) -> None:
        inputs = (
            "same",  # exact TM source that is also a glossary term (TM priority)
            "A high performance Glossary Engine",  # exact miss, glossary hit
            "Completely unrelated phrase",  # exact and glossary miss
        )

        def render_file_mode(tag: str) -> tuple[list[object], dict[str, int]]:
            input_path = root / f"input_{tag}.xlsx"
            output_path = root / f"output_{tag}.xlsx"
            timing_path = root / f"timing_{tag}.json"
            workbook = Workbook()
            sheet = workbook.active
            if sheet is None:
                self.fail("input workbook has no active sheet")
            for index, text in enumerate(inputs, start=1):
                sheet[f"A{index}"] = text
            workbook.save(input_path)

            _artifact_path = run_file_mode_benchmark(
                input_xlsx=input_path,
                output_xlsx=output_path,
                timing_json=timing_path,
                source_column="A",
                target_column="B",
                sheet_name=None,
                max_rows=None,
            )
            output = load_workbook(output_path, read_only=True)
            output_sheet = output.active
            if output_sheet is None:
                self.fail("output workbook has no active sheet")
            rendered = [
                cast(object, output_sheet[f"B{index}"].value)
                for index in range(1, len(inputs) + 1)
            ]
            output.close()
            status_counts = cast(
                dict[str, int],
                json.loads(timing_path.read_text(encoding="utf-8"))["status_counts"],
            )
            return rendered, status_counts

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tm_path = root / "tm.primary.jsonl"
            _ = tm_path.write_bytes(SOURCE_BYTES)
            glossary_path = root / "terms.csv"
            _ = glossary_path.write_text(
                "same,同様\nGlossary,术语\n",
                encoding="utf-8",
            )

            with patch.object(
                logic_controller, "TM_FILE", str(tm_path)
            ), patch.object(
                logic_controller, "GLOSSARY_FILE", str(glossary_path)
            ):
                legacy_controller = logic_controller.LogicController()
                legacy_results = [
                    legacy_controller.get_suggestions(text) for text in inputs
                ]
                legacy_rendered, legacy_counts = render_file_mode("legacy")

            identity, coordinator, _sealed, prepared, handle = _first_prepared(
                root,
                fts5_available=True,
            )
            self.assertEqual(
                identity.configured_jsonl_path.resolve(),
                tm_path.resolve(),
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                _ = coordinator.publish_activation(prepared, handle)

            with patch.object(
                logic_controller, "TM_FILE", str(tm_path)
            ), patch.object(
                logic_controller, "GLOSSARY_FILE", str(glossary_path)
            ):
                activated_controller = logic_controller.LogicController()
                activated_results = [
                    activated_controller.get_suggestions(text) for text in inputs
                ]
                activated_rendered, activated_counts = render_file_mode("activated")

        self.assertEqual(activated_results, legacy_results)
        self.assertEqual(activated_rendered, legacy_rendered)
        self.assertEqual(activated_counts, legacy_counts)
        self.assertEqual(
            legacy_results[0],
            {
                "status": "TM_HIT",
                "tm_match": {
                    "source": "same",
                    "target": "winner",
                    "match_type": "EXACT",
                    "similarity": 1.0,
                },
            },
        )
        self.assertEqual(legacy_results[1]["status"], "TERMS_FOUND")
        terms = cast(list[dict[str, object]], legacy_results[1]["terms"])
        self.assertEqual(
            [(term["source_term"], term["target_term"]) for term in terms],
            [("Glossary", "术语")],
        )
        self.assertEqual(legacy_results[2], {"status": "NO_MATCH"})
        self.assertEqual(
            legacy_rendered,
            ["[TM] winner", "[Terms] Glossary->术语", "[No Match]"],
        )
        self.assertEqual(
            legacy_counts,
            {"TM_HIT": 1, "TERMS_FOUND": 1, "NO_MATCH": 1},
        )
        self.assertEqual(
            set(legacy_counts),
            {"TM_HIT", "TERMS_FOUND", "NO_MATCH"},
        )
        self.assertEqual(
            set(activated_counts),
            {"TM_HIT", "TERMS_FOUND", "NO_MATCH"},
        )
        self.assertEqual(
            {cast(str, result["status"]) for result in legacy_results},
            {"TM_HIT", "TERMS_FOUND", "NO_MATCH"},
        )
        self.assertEqual(
            {cast(str, result["status"]) for result in activated_results},
            {"TM_HIT", "TERMS_FOUND", "NO_MATCH"},
        )

    def test_logic_controller_self_test_matches_default_resources(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "logic_controller.py")],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Logic Controller Self-Test Passed", result.stdout)

    def test_headless_file_adapter_preserves_three_status_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.xlsx"
            output_path = root / "output.xlsx"
            timing_path = root / "timing.json"
            workbook = Workbook()
            sheet = workbook.active
            if sheet is None:
                self.fail("input workbook has no active sheet")
            sheet["A1"] = "System Ready"
            sheet["A2"] = "Initializing Glossary Engine"
            sheet["A3"] = "Completely unrelated phrase"
            workbook.save(input_path)

            result_path = run_file_mode_benchmark(
                input_xlsx=input_path,
                output_xlsx=output_path,
                timing_json=timing_path,
                source_column="A",
                target_column="B",
                sheet_name=None,
                max_rows=None,
            )
            output = load_workbook(output_path, read_only=True)
            output_sheet = output.active
            if output_sheet is None:
                self.fail("output workbook has no active sheet")
            values = [output_sheet[f"B{row}"].value for row in range(1, 4)]
            output.close()
            timing = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(values[0], "[TM] 系统就绪")
        self.assertTrue(str(values[1]).startswith("[Terms]"))
        self.assertEqual(values[2], "[No Match]")
        self.assertEqual(
            timing["status_counts"],
            {"TM_HIT": 1, "TERMS_FOUND": 1, "NO_MATCH": 1},
        )

    def test_interactive_adapter_compiles_and_only_reaches_engine_via_logic(self) -> None:
        adapter_path = PROJECT_ROOT / "excel_adapter.py"
        py_compile.compile(str(adapter_path), doraise=True)
        tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }

        self.assertIn("logic_controller", imported_modules)
        self.assertNotIn("tm_engine", imported_modules)
        self.assertNotIn("glossary_engine", imported_modules)
        self.assertFalse(any(module.startswith("PySide6") for module in imported_modules))


if __name__ == "__main__":
    unittest.main()
