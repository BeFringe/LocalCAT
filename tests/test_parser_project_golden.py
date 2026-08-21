"""Contract checks for the distributable synthetic project-document goldens."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "project"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

FORMAT_IDS = {
    "localcat-json-v1",
    "line-text-v1",
    "gettext-po-v1",
    "gettext-pot-v1",
}
REQUIRED_AXES = {"valid", "format_boundary", "encoding", "limit", "cancel"}
ALLOWED_OUTCOMES = {"success", "fatal", "cancelled"}


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _recipe_bytes(payload: dict[str, object]) -> bytes:
    if payload.get("generator") != "repeat-fragments-v1":
        raise AssertionError(f"unsupported fixture generator: {payload.get('generator')}")
    repeat = payload["sample_repeat"]
    if type(repeat) is not int or repeat < 1:
        raise AssertionError("sample_repeat must be a positive exact int")
    prefix = payload["prefix"]
    item_template = payload["item_template"]
    separator = payload["separator"]
    suffix = payload["suffix"]
    if not all(
        isinstance(value, str)
        for value in (prefix, item_template, separator, suffix)
    ):
        raise AssertionError("repeat-fragments-v1 fragments must be strings")
    items = [item_template.replace("{index}", str(index)) for index in range(1, repeat + 1)]
    return (prefix + separator.join(items) + suffix).encode("utf-8")


def _materialize(case: dict[str, object]) -> bytes:
    payload = case["payload"]
    if not isinstance(payload, dict):
        raise AssertionError("payload must be an object")
    kind = payload.get("kind")
    if kind in {"file", "hex"}:
        relative_path = payload.get("path")
        if not isinstance(relative_path, str):
            raise AssertionError("fixture path must be a string")
        source_path = FIXTURE_ROOT / relative_path
        if source_path.is_symlink():
            raise AssertionError(f"fixture must not be a symlink: {relative_path}")
        path = source_path.resolve()
        root = FIXTURE_ROOT.resolve()
        if path.parent != root and root not in path.parents:
            raise AssertionError(f"fixture escapes root: {relative_path}")
        if not path.is_file():
            raise AssertionError(f"fixture must be a regular checked-in file: {relative_path}")
        stored = path.read_bytes()
        return stored if kind == "file" else bytes.fromhex(stored.decode("ascii"))
    if kind == "recipe":
        return _recipe_bytes(payload)
    raise AssertionError(f"unsupported payload kind: {kind}")


class ProjectGoldenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load_manifest()
        formats = cls.manifest.get("formats")
        if not isinstance(formats, list):
            raise AssertionError("formats must be a list")
        cls.formats = formats

    def test_manifest_is_synthetic_distributable_and_versioned(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["purpose"], "project_document")
        self.assertEqual(
            self.manifest["provenance"],
            {
                "origin": "localcat-synthetic",
                "external_downloads": [],
                "contains_user_content": False,
            },
        )
        self.assertEqual(
            self.manifest["generators"]["repeat-fragments-v1"],
            {
                "version": 1,
                "encoding": "utf-8",
                "algorithm": "prefix + separator.join(item_template[index=1..repeat]) + suffix",
            },
        )

    def test_four_format_ids_cover_required_axes_without_invented_warnings(self) -> None:
        by_format = {entry["format_id"]: entry for entry in self.formats}
        self.assertEqual(len(self.formats), len(FORMAT_IDS))
        self.assertEqual(set(by_format), FORMAT_IDS)

        expected_capabilities = {
            "localcat-json-v1": {
                "read": True,
                "canonical_write": True,
                "source_round_trip_write": False,
                "streaming_input": False,
            },
            "line-text-v1": {
                "read": True,
                "canonical_write": False,
                "source_round_trip_write": False,
                "streaming_input": True,
            },
            "gettext-po-v1": {
                "read": True,
                "canonical_write": False,
                "source_round_trip_write": False,
                "streaming_input": True,
            },
            "gettext-pot-v1": {
                "read": True,
                "canonical_write": False,
                "source_round_trip_write": False,
                "streaming_input": True,
            },
        }

        case_ids: set[str] = set()
        for format_id, entry in by_format.items():
            with self.subTest(format_id=format_id):
                self.assertEqual(entry["capabilities"], expected_capabilities[format_id])
                cases = entry["cases"]
                axes = {axis for case in cases for axis in case["axes"]}
                self.assertTrue(REQUIRED_AXES.issubset(axes))
                encoding_outcomes = {
                    case["expectation"]["outcome"]
                    for case in cases
                    if "encoding" in case["axes"]
                }
                self.assertEqual(encoding_outcomes, {"success", "fatal"})
                for case in cases:
                    self.assertNotIn(case["case_id"], case_ids)
                    case_ids.add(case["case_id"])
                    expectation = case["expectation"]
                    self.assertIn(expectation["outcome"], ALLOWED_OUTCOMES)
                    self.assertEqual(expectation["recoverable_warning_count"], 0)
                    if expectation["outcome"] == "success":
                        self.assertTrue(expectation["terminal_success"])
                    else:
                        self.assertFalse(expectation["terminal_success"])

    def test_fatal_tail_exists_only_for_declared_applicable_formats(self) -> None:
        for entry in self.formats:
            fatal_tail_cases = [
                case for case in entry["cases"] if "fatal_tail" in case["axes"]
            ]
            applicability = entry["fatal_tail"]
            with self.subTest(format_id=entry["format_id"]):
                self.assertIs(type(applicability["applicable"]), bool)
                self.assertIsInstance(applicability["reason"], str)
                self.assertTrue(applicability["reason"])
                if applicability["applicable"]:
                    self.assertGreaterEqual(len(fatal_tail_cases), 1)
                    self.assertTrue(
                        all(
                            case["expectation"]["outcome"] == "fatal"
                            for case in fatal_tail_cases
                        )
                    )
                else:
                    self.assertEqual(fatal_tail_cases, [])

    def test_payload_bytes_are_reproducible_and_digest_bound(self) -> None:
        for entry in self.formats:
            for case in entry["cases"]:
                with self.subTest(case=case["case_id"]):
                    first = _materialize(case)
                    second = _materialize(case)
                    self.assertEqual(first, second)
                    self.assertEqual(len(first), case["materialized_byte_length"])
                    self.assertEqual(hashlib.sha256(first).hexdigest(), case["sha256"])
                    self.assertEqual(
                        case["payload"]["materialized_suffix"],
                        entry["filename_suffix"],
                    )

    def test_limit_and_cancel_recipes_publish_runtime_parameters(self) -> None:
        for entry in self.formats:
            for axis in ("limit", "cancel"):
                matching = [case for case in entry["cases"] if axis in case["axes"]]
                self.assertGreaterEqual(len(matching), 1, (entry["format_id"], axis))
                for case in matching:
                    with self.subTest(case=case["case_id"]):
                        payload = case["payload"]
                        self.assertEqual(payload["kind"], "recipe")
                        generator = self.manifest["generators"][payload["generator"]]
                        self.assertEqual(payload["generator_version"], generator["version"])
                        runtime = payload["runtime"]
                        if axis == "limit":
                            self.assertEqual(case["expectation"]["outcome"], "fatal")
                            self.assertTrue(
                                case["expectation"]["issue_family"].startswith(
                                    "PARSER.LIMIT."
                                )
                            )
                            self.assertEqual(runtime["value"], "profile_limit_plus_offset")
                            self.assertGreater(runtime["offset"], 0)
                            self.assertIn(
                                runtime["profile_field"],
                                {"max_input_bytes", "max_field_chars", "max_records"},
                            )
                        else:
                            self.assertEqual(case["expectation"]["outcome"], "cancelled")
                            self.assertEqual(runtime["action"], "cancel")
                            self.assertGreaterEqual(runtime["after_checkpoint"], 1)
                            self.assertIn(
                                runtime["checkpoint"],
                                {"bounded_byte_chunk", "line", "gettext_entry"},
                            )


if __name__ == "__main__":
    unittest.main()
