"""Contract tests for the shared bounded JSON lexical preflight."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch


from parser_json_support import (
    JsonBomPolicy,
    JsonPreflightError,
    JsonPreflightLimits,
    load_bounded_json,
)


class BoundedJsonPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = JsonPreflightLimits(
            max_input_bytes=4096,
            max_string_chars=64,
            max_structure_depth=8,
        )

    def test_result_matches_the_single_stdlib_materialization(self) -> None:
        payloads = (
            b'{"nested":[true,false,null,-12.5e+2,{"value":"ok"}]}',
            b'"root string"',
            b"123456789012345678901234567890",
            b"null",
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                result = load_bounded_json(payload, self.limits)
                self.assertEqual(result.value, json.loads(payload.decode("utf-8")))
                self.assertEqual(result.input_bytes, len(payload))
                self.assertFalse(result.had_utf8_bom)

    def test_legal_escapes_and_surrogate_pairs_use_decoded_character_count(self) -> None:
        payload = b'{"text":"A\\n\\t\\/\\b\\f\\r\\u0061\\ud83d\\ude00"}'

        result = load_bounded_json(
            payload,
            JsonPreflightLimits(
                max_input_bytes=4096,
                max_string_chars=10,
                max_structure_depth=2,
            ),
        )

        self.assertEqual(result.value, json.loads(payload.decode("utf-8")))
        self.assertEqual(result.value["text"], "A\n\t/\b\f\ra😀")
        self.assertEqual(result.max_string_chars_seen, 9)

    def test_bom_policy_is_explicit_and_does_not_change_utf8_strictness(self) -> None:
        payload = b"\xef\xbb\xbf{\"ok\":true}"

        allowed = load_bounded_json(
            payload,
            self.limits,
            bom_policy=JsonBomPolicy.ALLOW,
        )
        self.assertEqual(allowed.value, {"ok": True})
        self.assertTrue(allowed.had_utf8_bom)

        with self.assertRaises(JsonPreflightError) as caught:
            load_bounded_json(
                payload,
                self.limits,
                bom_policy=JsonBomPolicy.REJECT,
            )
        self.assertEqual(caught.exception.code, "PARSER.SYNTAX.MALFORMED")
        self.assertNotIn("ok", caught.exception.safe_summary)

    def test_invalid_utf8_is_a_stable_encoding_failure(self) -> None:
        with self.assertRaises(JsonPreflightError) as caught:
            load_bounded_json(b'{"text":"\xff"}', self.limits)

        error = caught.exception
        self.assertEqual(error.code, "PARSER.SOURCE.ENCODING_FAILED")
        self.assertEqual(error.byte_offset, 9)
        self.assertIsNone(error.char_offset)
        self.assertNotIn("text", error.safe_summary)

    def test_input_limit_is_checked_before_decoding_or_materialization(self) -> None:
        with patch("parser_json_support.json.loads") as materialize:
            with self.assertRaises(JsonPreflightError) as caught:
                load_bounded_json(
                    b"{}\xff",
                    JsonPreflightLimits(
                        max_input_bytes=2,
                        max_string_chars=64,
                        max_structure_depth=8,
                    ),
                )

        self.assertEqual(caught.exception.code, "PARSER.LIMIT.INPUT")
        materialize.assert_not_called()

    def test_success_materializes_once_after_preflight(self) -> None:
        sentinel = object()

        with patch("parser_json_support.json.loads", return_value=sentinel) as materialize:
            result = load_bounded_json(b'{"safe":true}', self.limits)

        self.assertIs(result.value, sentinel)
        materialize.assert_called_once_with('{"safe":true}')

    def test_decoded_string_limit_covers_keys_values_and_escapes(self) -> None:
        cases = (
            b'{"12345":0}',
            b'{"x":"12345"}',
            b'{"x":"\\u0031\\u0032\\u0033\\u0034\\u0035"}',
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(JsonPreflightError) as caught:
                    load_bounded_json(
                        payload,
                        JsonPreflightLimits(
                            max_input_bytes=4096,
                            max_string_chars=4,
                            max_structure_depth=8,
                        ),
                    )
                self.assertEqual(caught.exception.code, "PARSER.LIMIT.FIELD")

    def test_structure_depth_is_bounded_before_materialization(self) -> None:
        accepted = load_bounded_json(
            b'{"a":[{"b":0}]}',
            JsonPreflightLimits(
                max_input_bytes=4096,
                max_string_chars=64,
                max_structure_depth=3,
            ),
        )
        self.assertEqual(accepted.max_structure_depth_seen, 3)

        with patch("parser_json_support.json.loads") as materialize:
            with self.assertRaises(JsonPreflightError) as caught:
                load_bounded_json(
                    b'{"a":[[{"b":0}]]}',
                    JsonPreflightLimits(
                        max_input_bytes=4096,
                        max_string_chars=64,
                        max_structure_depth=3,
                    ),
                )
        self.assertEqual(caught.exception.code, "PARSER.LIMIT.DEPTH")
        materialize.assert_not_called()

    def test_truncated_string_container_and_escape_are_stable_syntax_failures(self) -> None:
        payloads = (
            b'{"text":"unterminated}',
            b'{"text":"bad\\',
            b'{"text":"bad\\u12"}',
            b'{"text":[1,2}',
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(JsonPreflightError) as caught:
                    load_bounded_json(payload, self.limits)
                self.assertEqual(caught.exception.code, "PARSER.SYNTAX.MALFORMED")
                self.assertNotIn("unterminated", caught.exception.safe_summary)

    def test_extra_tail_after_any_complete_root_is_rejected(self) -> None:
        payloads = (
            b"{} []",
            b'"ok" false',
            b"1 2",
            b"true {}",
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(JsonPreflightError) as caught:
                    load_bounded_json(payload, self.limits)
                self.assertEqual(caught.exception.code, "PARSER.SYNTAX.MALFORMED")

    def test_invalid_string_escape_and_control_character_fail_preflight(self) -> None:
        payloads = (b'"\\x20"', b'"line\nfeed"')

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(JsonPreflightError) as caught:
                    load_bounded_json(payload, self.limits)
                self.assertEqual(caught.exception.code, "PARSER.SYNTAX.MALFORMED")

    def test_stdlib_grammar_errors_are_wrapped_without_input_content(self) -> None:
        with self.assertRaises(JsonPreflightError) as caught:
            load_bounded_json(b'{"secret" 1}', self.limits)

        error = caught.exception
        self.assertEqual(error.code, "PARSER.SYNTAX.MALFORMED")
        self.assertIsNotNone(error.char_offset)
        self.assertNotIn("secret", error.safe_summary)

    def test_limit_contract_rejects_bool_and_non_positive_values(self) -> None:
        invalid_values = (0, -1, True, 1.5)

        for field_name in (
            "max_input_bytes",
            "max_string_chars",
            "max_structure_depth",
        ):
            for invalid in invalid_values:
                with self.subTest(field_name=field_name, invalid=invalid):
                    values = {
                        "max_input_bytes": 1,
                        "max_string_chars": 1,
                        "max_structure_depth": 1,
                    }
                    values[field_name] = invalid
                    with self.assertRaises((TypeError, ValueError)):
                        JsonPreflightLimits(**values)


if __name__ == "__main__":
    unittest.main()
