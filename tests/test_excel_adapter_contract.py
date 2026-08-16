from __future__ import annotations

import ast
import json
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from excel_adapter_openpyxl import run_file_mode_benchmark


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExcelAdapterContractTest(unittest.TestCase):
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
            self.assertIsNotNone(sheet)
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
            self.assertIsNotNone(output_sheet)
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
