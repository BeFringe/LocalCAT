"""Wave 0 baselines for retained CLI behavior and explicitly retired gaps.

``Retirement*`` tests document behavior that tasks 3.8/4.7 (normalized JSON)
or 3.9/3.10/4.8 (gettext) must replace.  They are not compatibility promises.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tm_json_importer
from tm_engine import POHandler


_ROOT = Path(__file__).resolve().parent.parent


def _run_runner(script_name: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(_ROOT)
    return subprocess.run(
        [sys.executable, "-B", str(_ROOT / script_name)],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class RetainedNormalizedTMJSONCharacterizationTests(unittest.TestCase):
    def test_batch_keeps_last_source_value_and_moves_it_to_latest_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "01.json"
            second = root / "02.json"
            first.write_text(
                json.dumps(
                    [
                        {"source": " A ", "target": " first ", "speaker": " S "},
                        {"source": "B", "target": "bee"},
                    ]
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps([{"source": "A", "target": "second"}]),
                encoding="utf-8",
            )

            records = tm_json_importer.load_records([first, second])

        self.assertEqual(tuple(records), ("B", "A"))
        self.assertEqual(records["A"]["target"], "second")
        self.assertEqual(records["A"]["speaker"], "")
        self.assertEqual(records["A"]["file_source"], "02.json")

    def test_utf8_bom_is_not_accepted_by_the_legacy_normalized_json_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bom.json"
            source.write_bytes(b'\xef\xbb\xbf[{"source":"a","target":"b"}]')
            with self.assertRaises(json.JSONDecodeError):
                tm_json_importer.load_records([source])

    def test_cli_main_wires_inputs_output_and_success_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.json"
            output = root / "output.jsonl"
            source.write_text(
                json.dumps([{"source": "hello", "target": "\u4f60\u597d"}]),
                encoding="utf-8",
            )
            arguments = argparse.Namespace(input=[str(source)], output=str(output))
            stdout = io.StringIO()

            with mock.patch.object(tm_json_importer, "parse_args", return_value=arguments):
                with redirect_stdout(stdout):
                    result = tm_json_importer.main()

            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {
                    "source": "hello",
                    "target": "\u4f60\u597d",
                    "speaker": "",
                    "file_source": "input.json",
                },
            )
            self.assertIn("Imported 1 JSON file(s).", stdout.getvalue())
            self.assertIn("Wrote 1 TM records", stdout.getvalue())

class RetirementNormalizedTMJSONCharacterizationTests(unittest.TestCase):
    """Legacy gaps replaced by tasks 3.8 and 4.7."""

    def test_bad_rows_are_silent_and_non_string_speaker_is_legacy_empty(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "input.json"
            source.write_text(
                json.dumps(
                    [
                        None,
                        {"source": "", "target": "empty source"},
                        {"source": "valid", "target": "target", "speaker": 42},
                    ]
                ),
                encoding="utf-8",
            )

            with redirect_stdout(stdout), mock.patch("sys.stderr", stderr):
                records = tm_json_importer.load_records([source])

        self.assertEqual(tuple(records), ("valid",))
        self.assertEqual(records["valid"]["speaker"], "")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_output_failure_can_truncate_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "tm.jsonl"
            output.write_bytes(b"last-known-good\n")
            records = tm_json_importer.load_records([])
            records["source"] = {
                "source": "source",
                "target": "target",
                "speaker": "",
                "file_source": "input.json",
            }

            with mock.patch.object(
                tm_json_importer.json,
                "dumps",
                side_effect=RuntimeError("injected serialization failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    tm_json_importer.write_jsonl(output, records)

            self.assertEqual(output.read_bytes(), b"")

    def test_public_entry_currently_propagates_output_failure(self) -> None:
        arguments = argparse.Namespace(input=["input.json"], output="output.jsonl")
        with mock.patch.object(tm_json_importer, "parse_args", return_value=arguments):
            with mock.patch.object(tm_json_importer, "resolve_input_files", return_value=[]):
                with mock.patch.object(tm_json_importer, "load_records", return_value={}):
                    with mock.patch.object(
                        tm_json_importer,
                        "write_jsonl",
                        side_effect=OSError("injected output failure"),
                    ):
                        with self.assertRaisesRegex(OSError, "injected output failure"):
                            tm_json_importer.main()


class RetainedRunnerOutputCharacterizationTests(unittest.TestCase):
    def test_runners_process_real_units_and_preserve_tm_term_context_output(self) -> None:
        translation = _run_runner("translation_runner.py")
        stress = _run_runner("stress_runner.py")

        self.assertEqual(translation.returncode, 0, translation.stderr)
        self.assertIn("Parsed 3 units", translation.stdout)
        self.assertIn("Unit #1: [System Ready]", translation.stdout)
        self.assertIn(">>> Translation: \u7cfb\u7edf\u5c31\u7eea", translation.stdout)
        self.assertIn("[TERMS] Found 2 term(s)", translation.stdout)

        self.assertEqual(stress.returncode, 0, stress.stderr)
        self.assertIn("[Unit #1] ID: stress_test.po_0", stress.stdout)
        self.assertIn("[TM HIT] Source: 'Glossary Engine Ready'", stress.stdout)
        self.assertIn("[Unit #3] ID: stress_test.po_2", stress.stdout)
        self.assertIn("Context: 'button'", stress.stdout)
        self.assertIn("[NO MATCH] No suggestions.", stress.stdout)


class RetirementGettextCharacterizationTests(unittest.TestCase):
    """Legacy gaps replaced by tasks 3.9, 3.10 and 4.8."""

    def test_singular_entry_maps_msgctxt_to_context_prev_and_drops_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.po"
            source.write_text(
                'msgctxt "menu"\nmsgid "Hello"\nmsgstr "\u4f60\u597d"\n',
                encoding="utf-8",
            )

            units = POHandler.parse_file(str(source))

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].id, "sample.po_0")
        self.assertEqual(units[0].text, "Hello")
        self.assertEqual(units[0].context_prev, "menu")
        self.assertFalse(hasattr(units[0], "target"))

    def test_singular_pot_is_read_like_po_but_has_no_target_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "template.pot"
            source.write_text('msgid "Source only"\nmsgstr ""\n', encoding="utf-8")

            units = POHandler.parse_file(str(source))

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].id, "template.pot_0")
        self.assertEqual(units[0].text, "Source only")
        self.assertFalse(hasattr(units[0], "target"))

    def test_multiline_entry_currently_returns_no_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "multiline.po"
            source.write_text(
                'msgid ""\n"Hello "\n"world"\nmsgstr "translated"\n',
                encoding="utf-8",
            )

            units = POHandler.parse_file(str(source))

        self.assertEqual(units, [])

    def test_invalid_utf8_is_printed_and_returned_as_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invalid.po"
            source.write_bytes(b'msgid "\xff"\nmsgstr "x"\n')
            output = io.StringIO()

            with redirect_stdout(output):
                units = POHandler.parse_file(str(source))

        self.assertEqual(units, [])
        self.assertIn("Error parsing PO file", output.getvalue())


if __name__ == "__main__":
    unittest.main()
