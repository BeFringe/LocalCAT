from __future__ import annotations

import unittest

from renpy_tm_compat import build_dialogue_alias, unwrap_dialogue_target


class RenPyTMCompatTest(unittest.TestCase):
    def test_builds_alias_only_for_safe_speaker_token(self) -> None:
        self.assertEqual(
            build_dialogue_alias("NVLHED", 'She said "hello".'),
            'NVLHED "She said \\"hello\\"."',
        )
        self.assertIsNone(build_dialogue_alias("bad speaker", "Hello"))
        self.assertIsNone(build_dialogue_alias("", "Hello"))

    def test_unwraps_same_speaker_and_unescapes_quote_payload(self) -> None:
        self.assertEqual(
            unwrap_dialogue_target('NVLHED "她说：\\"你好\\"。"', "NVLHED"),
            '她说："你好"。',
        )
        self.assertIsNone(unwrap_dialogue_target('OTHER "你好。"', "NVLHED"))
        self.assertIsNone(unwrap_dialogue_target("没有封装", "NVLHED"))


if __name__ == "__main__":
    unittest.main()
