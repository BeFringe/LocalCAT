"""Contract guard for the distributable translation-memory golden corpus.

Wave 0 intentionally freezes synthetic inputs and expected observations before
the Parser Foundation or either translation-memory codec exists.  The tests in
this module do not parse through production code; later codec tests consume the
same manifest and payload bytes.
"""

from __future__ import annotations

import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "tm"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

FORMAT_IDS = {"tmx-level1-v1", "normalized-tm-json-v1"}
REQUIRED_COVERS = {
    "valid",
    "format_boundary",
    "record_warning",
    "fatal_tail",
    "encoding",
    "limit",
    "cancel",
}
SOURCE_KINDS = {"path", "hex", "recipe"}


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _case_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        case["id"]: case
        for format_entry in manifest["formats"]
        for case in format_entry["cases"]
    }


def _iter_recipe(recipe: dict[str, Any]) -> Iterator[bytes]:
    if recipe.get("kind") != "concat-v1":
        raise AssertionError(f"unsupported fixture recipe: {recipe.get('kind')!r}")
    for part in recipe["parts"]:
        if set(part) == {"utf8"}:
            yield part["utf8"].encode("utf-8")
            continue
        if set(part) == {"hex"}:
            yield bytes.fromhex(part["hex"])
            continue
        if set(part) == {"repeat_utf8", "count"}:
            encoded = part["repeat_utf8"].encode("utf-8")
            count = part["count"]
            if not encoded or not isinstance(count, int) or count < 0:
                raise AssertionError("repeat recipe requires non-empty bytes and count >= 0")
            repetitions_per_chunk = max(1, 64 * 1024 // len(encoded))
            while count:
                current = min(count, repetitions_per_chunk)
                yield encoded * current
                count -= current
            continue
        raise AssertionError(f"invalid recipe part keys: {sorted(part)}")


def _iter_case_bytes(case: dict[str, Any]) -> Iterator[bytes]:
    source = case["source"]
    populated = SOURCE_KINDS.intersection(source)
    if len(populated) != 1:
        raise AssertionError(f"case {case['id']} must have exactly one source kind")
    kind = next(iter(populated))
    if kind == "path":
        relative = Path(source["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise AssertionError(f"unsafe fixture path: {relative}")
        path = (FIXTURE_ROOT / relative).resolve()
        if not path.is_relative_to(FIXTURE_ROOT.resolve()):
            raise AssertionError(f"fixture escaped root: {relative}")
        yield path.read_bytes()
    elif kind == "hex":
        yield bytes.fromhex(source["hex"])
    else:
        yield from _iter_recipe(source["recipe"])


def _case_bytes(case: dict[str, Any]) -> bytes:
    return b"".join(_iter_case_bytes(case))


def _fingerprint(case: dict[str, Any]) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in _iter_case_bytes(case):
        byte_count += len(chunk)
        digest.update(chunk)
    return byte_count, digest.hexdigest()


class TranslationMemoryGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load_manifest()
        cls.cases = _case_map(cls.manifest)

    def test_manifest_is_canonical_and_only_contains_synthetic_sources(self) -> None:
        raw = MANIFEST_PATH.read_text(encoding="utf-8")
        canonical = json.dumps(
            self.manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        self.assertEqual(raw, canonical)
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(
            self.manifest["purpose"],
            "language_resource.translation_memory",
        )
        self.assertEqual(
            self.manifest["provenance"],
            {
                "external_inputs": [],
                "kind": "synthetic",
                "redistributable": True,
            },
        )
        forbidden = ("/Users/", "Downloads/", "MVol2Ch5", "mymemory")
        for token in forbidden:
            self.assertNotIn(token, raw)

    def test_each_format_has_the_complete_contract_matrix(self) -> None:
        entries = {entry["format_id"]: entry for entry in self.manifest["formats"]}
        self.assertEqual(set(entries), FORMAT_IDS)
        all_ids: list[str] = []
        for format_id, entry in entries.items():
            with self.subTest(format_id=format_id):
                self.assertEqual(entry["effective_purpose"], self.manifest["purpose"])
                self.assertEqual(entry["limit_profile"]["input_bytes"], 100 * 1024 * 1024)
                covers = {
                    category
                    for case in entry["cases"]
                    for category in case["covers"]
                }
                self.assertEqual(covers, REQUIRED_COVERS)
                self.assertTrue(any("record_warning" in case["covers"] for case in entry["cases"]))
                self.assertTrue(any("fatal_tail" in case["covers"] for case in entry["cases"]))
                for case in entry["cases"]:
                    all_ids.append(case["id"])
                    self.assertEqual(case["format_id"], format_id)
                    self.assertTrue(set(case["covers"]).issubset(REQUIRED_COVERS))
                    self.assertIn("terminal", case["expect"])
                    self.assertIn("byte_length", case)
                    self.assertRegex(case["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_all_payloads_and_recipes_are_byte_reproducible(self) -> None:
        referenced_paths: set[Path] = set()
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                first = _fingerprint(case)
                second = _fingerprint(case)
                self.assertEqual(first, second)
                self.assertEqual(first, (case["byte_length"], case["sha256"]))
                if "path" in case["source"]:
                    referenced_paths.add(Path(case["source"]["path"]))
        actual_paths = {
            path.relative_to(FIXTURE_ROOT)
            for path in (FIXTURE_ROOT / "payloads").glob("**/*")
            if path.is_file()
        }
        self.assertEqual(referenced_paths, actual_paths)

    def test_tmx_valid_fixture_freezes_locale_fallback_variants_and_order(self) -> None:
        case = self.cases["tmx-valid-locale-fallback-variants"]
        root = ET.fromstring(_case_bytes(case))
        units = root.findall("./body/tu")
        self.assertEqual(len(units), 4)
        observed = [
            [
                (variant.attrib["{http://www.w3.org/XML/1998/namespace}lang"], variant.findtext("seg"))
                for variant in unit.findall("tuv")
            ]
            for unit in units
        ]
        self.assertEqual(
            observed,
            [
                [
                    ("en-GB", "Wrong exact source"),
                    ("en_US", "Exact"),
                    ("zh-Hans", "错误的精确译文"),
                    ("zh_CN", "精确译文"),
                ],
                [("en-GB", "Fallback"), ("zh-Hans", "回退译文")],
                [("en-US", "Variant"), ("zh-CN", "变体一")],
                [("en-US", "Variant"), ("zh-CN", "变体二")],
            ],
        )
        self.assertEqual(case["expect"]["accepted_ids"], ["tu-1", "tu-2", "tu-3", "tu-4"])
        self.assertEqual(case["expect"]["source_locale"], "en-US")
        self.assertEqual(case["expect"]["target_locale"], "zh-CN")
        self.assertEqual(case["expect"]["duplicate_sources"], ["Variant"])

    def test_tmx_warning_and_fatal_boundaries_are_explicit(self) -> None:
        warning_case = self.cases["tmx-record-warnings"]
        root = ET.fromstring(_case_bytes(warning_case))
        self.assertEqual(len(root.findall("./body/tu")), 4)
        self.assertIsNotNone(root.find(".//ph"))
        self.assertEqual(warning_case["expect"]["accepted_ids"], ["tu-1"])
        self.assertEqual(warning_case["expect"]["warning_ordinals"], [2, 3, 4])
        self.assertEqual(
            warning_case["expect"]["warning_reasons"],
            ["inline_xml", "missing_locale_pair", "ambiguous_base_fallback"],
        )

        with self.assertRaises(ET.ParseError):
            ET.fromstring(_case_bytes(self.cases["tmx-fatal-tail-after-valid-unit"]))
        boundary = _case_bytes(self.cases["tmx-dtd-entity-boundary"])
        self.assertIn(b"<!DOCTYPE", boundary)
        self.assertIn(b"<!ENTITY", boundary)
        with self.assertRaises(UnicodeDecodeError):
            _case_bytes(self.cases["tmx-invalid-utf8"]).decode("utf-8")

    def test_normalized_json_fixtures_freeze_rows_speaker_and_duplicates(self) -> None:
        valid_case = self.cases["normalized-valid-speaker-and-duplicates"]
        records = json.loads(_case_bytes(valid_case).decode("utf-8"))
        self.assertEqual([record["source"].strip() for record in records], ["Alpha", "Duplicate", "Duplicate"])
        self.assertEqual(records[0]["speaker"], " Alice Smith ")
        self.assertIsNone(records[1]["speaker"])
        self.assertNotIn("speaker", records[2])
        self.assertEqual(valid_case["expect"]["accepted_ids"], ["record-1", "record-2", "record-3"])
        self.assertEqual(valid_case["expect"]["duplicate_sources"], ["Duplicate"])
        self.assertEqual(valid_case["expect"]["speaker_values"], ["Alice Smith", "", ""])

        warning_case = self.cases["normalized-record-warnings"]
        warning_rows = json.loads(_case_bytes(warning_case).decode("utf-8"))
        self.assertEqual(len(warning_rows), 7)
        self.assertEqual(warning_rows[4]["speaker"], 42)
        self.assertEqual(warning_case["expect"]["accepted_ids"], ["record-1", "record-6", "record-7"])
        self.assertEqual(warning_case["expect"]["warning_ordinals"], [2, 3, 4, 5])
        self.assertEqual(
            warning_case["expect"]["warning_reasons"],
            ["not_object", "empty_source", "invalid_target", "non_string_speaker"],
        )

    def test_normalized_json_input_boundaries_are_explicit(self) -> None:
        boundary = json.loads(
            _case_bytes(self.cases["normalized-non-array-root"]).decode("utf-8")
        )
        self.assertIsInstance(boundary, dict)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(
                _case_bytes(self.cases["normalized-fatal-tail-after-valid-row"]).decode("utf-8")
            )
        with self.assertRaises(UnicodeDecodeError):
            _case_bytes(self.cases["normalized-invalid-utf8"]).decode("utf-8")
        bom = _case_bytes(self.cases["normalized-utf8-bom-rejected"])
        self.assertTrue(bom.startswith(b"\xef\xbb\xbf"))
        self.assertIsInstance(json.loads(bom.decode("utf-8-sig")), list)

    def test_limit_and_cancel_recipes_name_exact_contract_checkpoints(self) -> None:
        tmx_segment = self.cases["tmx-segment-limit-warning"]
        self.assertEqual(tmx_segment["recipe_parameters"]["segment_chars"], 1_000_001)
        self.assertEqual(tmx_segment["expect"]["warning_reason"], "segment_text_limit")
        self.assertEqual(tmx_segment["expect"]["terminal"], "success_with_warnings")

        expected_limits = {
            "tmx-input-byte-limit": ("input_bytes", 100 * 1024 * 1024 + 1),
            "normalized-input-byte-limit": ("input_bytes", 100 * 1024 * 1024 + 1),
            "normalized-record-limit": ("records", 100_001),
        }
        for case_id, (dimension, observed) in expected_limits.items():
            with self.subTest(case=case_id):
                case = self.cases[case_id]
                self.assertEqual(case["expect"]["limit_dimension"], dimension)
                self.assertEqual(case["expect"]["observed"], observed)
                self.assertEqual(case["expect"]["terminal"], "fatal")

        checkpoints = {
            "tmx-cancel-after-first-tu": ("after_tu", 1, 3),
            "normalized-cancel-after-first-record": ("after_record", 1, 3),
        }
        for case_id, (checkpoint, cancel_after, physical_records) in checkpoints.items():
            with self.subTest(case=case_id):
                recipe = self.cases[case_id]["source"]["recipe"]
                self.assertEqual(
                    recipe["cancellation"],
                    {
                        "cancel_after": cancel_after,
                        "checkpoint": checkpoint,
                        "physical_records": physical_records,
                    },
                )
                self.assertEqual(self.cases[case_id]["expect"]["terminal"], "cancelled")
                self.assertFalse(self.cases[case_id]["expect"]["commit_authorized"])


if __name__ == "__main__":
    unittest.main()
