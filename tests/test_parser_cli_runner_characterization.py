"""Wave 0 baselines for retained CLI behavior and explicitly retired gaps.

``Retirement*`` tests document behavior that tasks 3.8/4.7 (normalized JSON)
or 3.9/3.10/4.8 (gettext) must replace.  They are not compatibility promises.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
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
from logic_controller import ProjectSourceLoadError, load_gettext_source_units


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
    def test_directory_discovery_keeps_deterministic_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.json").write_text("[]", encoding="utf-8")
            (root / "a.JSON").write_text("[]", encoding="utf-8")
            (root / "ignored.txt").write_text("[]", encoding="utf-8")

            resolved = tm_json_importer.resolve_input_files([str(root)])

        self.assertEqual([path.name for path in resolved], ["a.JSON", "b.json"])

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

    def test_utf8_bom_is_rejected_by_the_normalized_json_codec_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bom.json"
            source.write_bytes(b'\xef\xbb\xbf[{"source":"a","target":"b"}]')
            with self.assertRaises(tm_json_importer.TMJSONImportError) as raised:
                tm_json_importer.load_records([source])

        self.assertEqual(raised.exception.code, "PARSER.SYNTAX.MALFORMED")

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

class MigratedNormalizedTMJSONContractTests(unittest.TestCase):
    """Versioned behavior after tasks 3.8 and 4.7 retire the legacy grammar."""

    def test_bad_rows_and_non_string_speaker_are_rejected_before_batch_mapping(self) -> None:
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

            with self.assertRaises(tm_json_importer.TMJSONImportError) as raised:
                tm_json_importer.load_records([source])

        self.assertEqual(raised.exception.code, "PARSER.SYNTAX.EMPTY_INPUT")

    def test_bad_file_contributes_nothing_while_a_terminal_success_file_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "01-bad.json"
            good = root / "02-good.json"
            bad.write_text(
                json.dumps([{"source": "poison", "target": "bad", "speaker": 42}]),
                encoding="utf-8",
            )
            good.write_text(
                json.dumps([{"source": "kept", "target": "good"}]),
                encoding="utf-8",
            )

            records = tm_json_importer.load_records([bad, good])

        self.assertEqual(tuple(records), ("kept",))
        self.assertEqual(records["kept"]["file_source"], "02-good.json")

    def test_record_warnings_remain_in_the_successful_per_file_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mixed.json"
            source.write_text(
                json.dumps(
                    [
                        {"source": "rejected", "target": "bad", "speaker": 42},
                        {"source": "kept", "target": "good", "speaker": "raw"},
                    ]
                ),
                encoding="utf-8",
            )

            result = tm_json_importer._read_one_file(source)

        self.assertEqual([record.source for record in result.records], ["kept"])
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["PARSER.SYNTAX.INVALID_FIELD"],
        )

    def test_structured_batch_continue_counts_only_successful_files_and_keeps_issues_internal(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fatal = root / "01-fatal.json"
            warning_good = root / "02-warning-good.json"
            good = root / "03-good.json"
            output = root / "output.jsonl"
            fatal.write_text('{"not":"an array"}', encoding="utf-8")
            warning_good.write_text(
                json.dumps(
                    [
                        {"source": "rejected", "target": "bad", "speaker": 42},
                        {"source": "A", "target": "first"},
                        {"source": "B", "target": "bee"},
                    ]
                ),
                encoding="utf-8",
            )
            good.write_text(
                json.dumps(
                    [
                        {"source": "A", "target": "second"},
                        {"source": "C", "target": "see"},
                    ]
                ),
                encoding="utf-8",
            )
            inputs = [fatal, warning_good, good]

            batch = tm_json_importer.load_batch(
                inputs,
                tm_json_importer._TMJSONBatchPolicy.CONTINUE,
            )
            arguments = argparse.Namespace(
                input=[str(path) for path in inputs],
                output=str(output),
            )
            with mock.patch.object(
                tm_json_importer,
                "parse_args",
                return_value=arguments,
            ), redirect_stdout(stdout), mock.patch("sys.stderr", stderr):
                result = tm_json_importer.main()

            output_records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result, 0)
        self.assertEqual(batch.successful_file_count, 2)
        self.assertEqual([item.succeeded for item in batch.file_outcomes], [False, True, True])
        self.assertEqual(
            [issue.code for issue in batch.file_outcomes[1].issues],
            ["PARSER.SYNTAX.INVALID_FIELD"],
        )
        self.assertEqual([record["source"] for record in output_records], ["B", "A", "C"])
        self.assertEqual(output_records[1]["target"], "second")
        self.assertEqual(output_records[1]["file_source"], "03-good.json")
        self.assertIn("Imported 2 JSON file(s).", stdout.getvalue())
        self.assertNotIn("Imported 3 JSON file(s).", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("PARSER.", stdout.getvalue())
        with self.assertRaises(FrozenInstanceError):
            batch.successful_file_count = 99

    def test_stop_policy_does_not_consume_any_file_after_first_failure(self) -> None:
        first = Path("first.json")
        second = Path("second.json")
        failed = tm_json_importer._TMJSONFileOutcome(
            input_file=first,
            records=(),
            issues=(),
            failure_code="PARSER.SYNTAX.MALFORMED",
            failure_summary="normalized TM JSON is malformed",
        )
        with mock.patch.object(
            tm_json_importer,
            "_read_one_file",
            return_value=failed,
        ) as read_one:
            batch = tm_json_importer.load_batch(
                [first, second],
                tm_json_importer._TMJSONBatchPolicy.STOP,
            )

        read_one.assert_called_once_with(first)
        self.assertEqual(batch.file_outcomes, (failed,))
        self.assertEqual(batch.successful_file_count, 0)

    def test_parser_programmer_fault_is_not_relabelled_as_input_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "valid.json"
            source.write_text('[{"source":"a","target":"b"}]', encoding="utf-8")
            surface = mock.Mock()
            surface.open_input.side_effect = AssertionError("programmer fault")

            with mock.patch.object(
                tm_json_importer,
                "create_parser_application_surface",
                return_value=surface,
            ):
                with self.assertRaisesRegex(AssertionError, "programmer fault"):
                    tm_json_importer.load_records([source])

    def test_output_failure_preserves_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "tm.jsonl"
            output.write_bytes(b"last-known-good\n")
            records = OrderedDict()
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

            self.assertEqual(output.read_bytes(), b"last-known-good\n")

    def test_atomic_replace_failure_preserves_target_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "tm.jsonl"
            output.write_bytes(b"last-known-good\n")
            records = OrderedDict(
                [
                    (
                        "source",
                        {
                            "source": "source",
                            "target": "target",
                            "speaker": "",
                            "file_source": "input.json",
                        },
                    )
                ]
            )

            with mock.patch.object(
                tm_json_importer.os,
                "replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    tm_json_importer.write_jsonl(output, records)

            self.assertEqual(output.read_bytes(), b"last-known-good\n")
            self.assertEqual(
                [path.name for path in root.iterdir()],
                ["tm.jsonl"],
            )

    def test_public_entry_maps_output_failure_to_nonzero_system_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.json"
            source.write_text('[{"source":"a","target":"b"}]', encoding="utf-8")
            arguments = argparse.Namespace(
                input=[str(source)],
                output=str(root / "output.jsonl"),
            )
            with mock.patch.object(
                tm_json_importer,
                "parse_args",
                return_value=arguments,
            ), mock.patch.object(
                tm_json_importer,
                "write_jsonl",
                side_effect=OSError("injected output failure"),
            ):
                with self.assertRaises(SystemExit) as raised:
                    tm_json_importer.main()

        self.assertNotEqual(raised.exception.code, 0)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_input_resolution_does_not_resolve_a_symlink_around_source_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual.json"
            alias = root / "alias.json"
            actual.write_text('[{"source":"a","target":"b"}]', encoding="utf-8")
            alias.symlink_to(actual)

            with self.assertRaises(ValueError):
                tm_json_importer.resolve_input_files([str(alias)])


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
        self.assertNotIn("Context: 'button'", stress.stdout)
        self.assertIn("[NO MATCH] No suggestions.", stress.stdout)


class MigratedGettextRunnerContractTests(unittest.TestCase):
    """Versioned runner behavior after tasks 3.9, 3.10 and 4.8."""

    def test_singular_entry_keeps_msgctxt_as_opaque_metadata_not_tm_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.po"
            source.write_text(
                'msgctxt "menu"\nmsgid "Hello"\nmsgstr "\u4f60\u597d"\n',
                encoding="utf-8",
            )

            units = load_gettext_source_units(source)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].id, "sample.po_0")
        self.assertEqual(units[0].text, "Hello")
        self.assertIsNone(units[0].context_prev)
        self.assertEqual(units[0].metadata["gettext.msgctxt"], "menu")
        self.assertFalse(hasattr(units[0], "target"))

    def test_singular_pot_is_read_like_po_but_has_no_target_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "template.pot"
            source.write_text('msgid "Source only"\nmsgstr ""\n', encoding="utf-8")

            units = load_gettext_source_units(source)

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].id, "template.pot_0")
        self.assertEqual(units[0].text, "Source only")
        self.assertFalse(hasattr(units[0], "target"))

    def test_multiline_entry_is_decoded_by_the_single_gettext_grammar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "multiline.po"
            source.write_text(
                'msgid ""\n"Hello "\n"world"\nmsgstr "translated"\n',
                encoding="utf-8",
            )

            units = load_gettext_source_units(source)

        self.assertEqual([unit.text for unit in units], ["Hello world"])

    def test_invalid_utf8_is_explicit_failure_not_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invalid.po"
            source.write_bytes(b'msgid "\xff"\nmsgstr "x"\n')
            with self.assertRaises(ProjectSourceLoadError) as raised:
                load_gettext_source_units(source)

        self.assertEqual(raised.exception.code, "PARSER.SOURCE.ENCODING_FAILED")

    def test_plural_fatal_discards_preceding_singular_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "plural.po"
            source.write_text(
                'msgid "accepted only provisionally"\nmsgstr "x"\n\n'
                'msgid "one"\nmsgid_plural "many"\nmsgstr[0] "one"\n',
                encoding="utf-8",
            )

            with self.assertRaises(ProjectSourceLoadError) as raised:
                load_gettext_source_units(source)

        self.assertEqual(raised.exception.code, "PARSER.GETTEXT.PLURAL_UNSUPPORTED")

    def test_both_runners_exit_nonzero_on_gettext_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "invalid.po"
            source.write_bytes(b'msgid "\xff"\nmsgstr "x"\n')
            for module_name in ("translation_runner", "stress_runner"):
                with self.subTest(module_name=module_name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            (
                                "import sys; import " + module_name + " as runner; "
                                "runner.PO_FILE=sys.argv[1]; "
                                "raise SystemExit(runner.main())"
                            ),
                            str(source),
                        ],
                        cwd=_ROOT,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(_ROOT)},
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("Processing Units", result.stdout)


if __name__ == "__main__":
    unittest.main()
