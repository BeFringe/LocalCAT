from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from typing import Any, cast

import text_matcher
from text_matcher import (
    TEXT_MATCHER_SEMANTICS_VERSION,
    UNICODE_VERSION,
    FoldProjection,
    fold_text_v1,
    is_pure_cjk_v1,
    is_word_boundary_v1,
    project_folded_span_v1,
    word_boundaries_v1,
)
from unicode_word_break_data import SOURCE_DIGESTS


_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_FIXTURE_PATH = _FIXTURE_DIR / "text_matcher_unicode_vectors.json"
_WORD_BREAK_TEST_PATH = _FIXTURE_DIR / "unicode-16.0.0-WordBreakTest.txt"


def _load_fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")),
    )


_FIXTURE = _load_fixture()


class TextMatcherUnicodeDataTests(unittest.TestCase):
    def test_generated_data_is_bound_to_unicode_16_sources(self) -> None:
        self.assertEqual(UNICODE_VERSION, "16.0.0")
        self.assertEqual(
            TEXT_MATCHER_SEMANTICS_VERSION,
            "text-v1-unicode-16.0.0",
        )
        self.assertEqual(
            SOURCE_DIGESTS,
            _FIXTURE["source_digests"],
        )
        for digest in SOURCE_DIGESTS.values():
            self.assertEqual(len(digest), 64)
            int(digest, 16)

    def test_fold_v1_golden_vectors_preserve_original_spans(self) -> None:
        for raw_vector in cast(
            list[dict[str, object]],
            _FIXTURE["fold_vectors"],
        ):
            with self.subTest(vector=raw_vector["id"]):
                projection = fold_text_v1(cast(str, raw_vector["raw"]))
                self.assertIsInstance(projection, FoldProjection)
                self.assertEqual(
                    projection.folded_text,
                    raw_vector["folded"],
                )
                self.assertEqual(
                    projection.source_spans,
                    tuple(
                        tuple(cast(list[int], span))
                        for span in cast(
                            list[list[int]],
                            raw_vector["source_spans"],
                        )
                    ),
                )

    def test_folded_ranges_project_to_minimal_original_spans(self) -> None:
        for raw_vector in cast(
            list[dict[str, object]],
            _FIXTURE["projection_vectors"],
        ):
            with self.subTest(vector=raw_vector["id"]):
                projection = fold_text_v1(cast(str, raw_vector["raw"]))
                expected_raw = raw_vector["source_span"]
                expected = (
                    None
                    if expected_raw is None
                    else tuple(cast(list[int], expected_raw))
                )
                self.assertEqual(
                    project_folded_span_v1(
                        projection,
                        cast(int, raw_vector["folded_start"]),
                        cast(int, raw_vector["folded_end"]),
                    ),
                    expected,
                )

    def test_word_boundary_golden_vectors_use_original_offsets(self) -> None:
        for raw_vector in cast(
            list[dict[str, object]],
            _FIXTURE["word_boundary_vectors"],
        ):
            with self.subTest(vector=raw_vector["id"]):
                text = cast(str, raw_vector["text"])
                expected = tuple(cast(list[int], raw_vector["boundaries"]))
                self.assertEqual(word_boundaries_v1(text), expected)
                self.assertEqual(
                    tuple(
                        index
                        for index in range(len(text) + 1)
                        if is_word_boundary_v1(text, index)
                    ),
                    expected,
                )

    def test_pure_cjk_golden_vectors_are_strict(self) -> None:
        for raw_vector in cast(
            list[dict[str, object]],
            _FIXTURE["pure_cjk_vectors"],
        ):
            with self.subTest(vector=raw_vector["id"]):
                self.assertEqual(
                    is_pure_cjk_v1(cast(str, raw_vector["query"])),
                    raw_vector["expected"],
                )

    def test_official_unicode_16_word_break_conformance(self) -> None:
        self.assertEqual(
            hashlib.sha256(_WORD_BREAK_TEST_PATH.read_bytes()).hexdigest(),
            "ad985d5721f3fa6b45495663dfe44180f2f68976100dee0ea7451ef1a8f838e8",
        )
        checked = 0
        for line_number, raw_line in enumerate(
            _WORD_BREAK_TEST_PATH.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            payload = raw_line.split("#", 1)[0].strip()
            if not payload:
                continue
            tokens = payload.split()
            code_points: list[int] = []
            expected_boundaries: list[int] = []
            offset = 0
            for token in tokens:
                if token == "÷":
                    expected_boundaries.append(offset)
                elif token == "×":
                    continue
                else:
                    code_points.append(int(token, 16))
                    offset += 1
            text = "".join(chr(code_point) for code_point in code_points)
            with self.subTest(line=line_number):
                self.assertEqual(
                    word_boundaries_v1(text),
                    tuple(expected_boundaries),
                )
            checked += 1
        self.assertGreater(checked, 1_000)

    def test_runtime_is_strictly_gated_to_host_ucd_16_without_regex(self) -> None:
        source = (
            Path(__file__).parents[1] / "text_matcher.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        from_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertIn("unicodedata", imports)
        self.assertNotIn("re", imports)
        self.assertNotIn("regex", imports)
        self.assertNotIn("re", from_imports)
        self.assertNotIn("regex", from_imports)
        with patch.object(
            text_matcher.unicodedata,
            "unidata_version",
            "15.1.0",
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^Unicode runtime mismatch: expected 16.0.0, got 15.1.0$",
            ):
                fold_text_v1("Office")
            with self.assertRaisesRegex(
                RuntimeError,
                "^Unicode runtime mismatch: expected 16.0.0, got 15.1.0$",
            ):
                word_boundaries_v1("Office")
            with self.assertRaisesRegex(
                RuntimeError,
                "^Unicode runtime mismatch: expected 16.0.0, got 15.1.0$",
            ):
                is_pure_cjk_v1("中文")

    def test_invalid_inputs_and_indices_fail_closed(self) -> None:
        with self.assertRaisesRegex(TypeError, "^text must be a string$"):
            fold_text_v1(cast(Any, None))
        with self.assertRaisesRegex(TypeError, "^text must be a string$"):
            word_boundaries_v1(cast(Any, 1))
        with self.assertRaisesRegex(TypeError, "^query must be a string$"):
            is_pure_cjk_v1(cast(Any, []))
        projection = fold_text_v1("text")
        with self.assertRaisesRegex(
            ValueError,
            "^folded range is outside the projection$",
        ):
            project_folded_span_v1(projection, -1, 1)
        with self.assertRaisesRegex(
            ValueError,
            "^boundary index is outside the text$",
        ):
            is_word_boundary_v1("text", 5)


if __name__ == "__main__":
    unittest.main()
