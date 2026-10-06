"""Cancellation and literal bounds independently exercise the shared text leaf."""

import unittest

from rpy_text_rules import RpyTextError, check_text, encode_target, protection_tokens


class RpyTextRulesTests(unittest.TestCase):
    def test_jumping_over_modulo_positions_does_not_starve_cancellation(self):
        # Odd prefix + two-character escapes used to miss every %4096 point.
        for text in ('x' + '[[' * 20000, '["x' + '\\\\' * 20000 + '"]',
                     'x' + '{{' * 20000, '{custom=' + 'x' * 40000 + '}'):
            calls = 0
            def checkpoint():
                nonlocal calls
                calls += 1
            tuple(protection_tokens(text, checkpoint))
            self.assertGreaterEqual(calls, 10)
            calls = 0
            def cancel():
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise InterruptedError()
            with self.assertRaises(InterruptedError):
                tuple(protection_tokens(text, cancel))

    def test_text_byte_budget_is_utf8_and_not_character_count(self):
        check_text('雪😀', 7, lambda: None)
        with self.assertRaises(RpyTextError) as caught:
            check_text('雪😀', 6, lambda: None)
        self.assertEqual(caught.exception.category, 'limit-exceeded')

    def test_encoded_expansion_rejects_before_exceeding_remaining_output(self):
        self.assertEqual(encode_target('"\n', '"', 4, lambda: None), b'\\"\\n')
        with self.assertRaises(RpyTextError) as caught:
            encode_target('"\n', '"', 3, lambda: None)
        self.assertEqual(caught.exception.category, 'limit-exceeded')


if __name__ == '__main__':
    unittest.main()
