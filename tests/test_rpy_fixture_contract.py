"""Validate synthetic RPY oracle contents, without implementing or invoking a codec."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import unittest


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rpy"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

# Independent semantic expectations: attributes and transitions never enter speaker/text.
ACCEPTED_RECORDS = {
    "mixed_dialogue_strings": [
        ("The lantern glows.", "灯笼亮着。", "guide"),
        ("Open", "", None),
        ("Open{#verb}", "开启{#verb}", None),
        ("The bridge is quiet.", "桥上很安静。", None),
        ("Close", "关闭", None),
        ("The lantern glows.", "", "guide"),
    ],
    "special_say": [
        ("And the bell rings.", "钟声接着响起。", "extend"),
        ("Synthetic interlude", "合成幕间", "centered"),
        ("A small signal.", "一个小小的信号。", "guide"),
    ],
    "literal_escapes": [
        ('It\'s a "sample" # path \\ folder\nnext\rrow\tend.',
         '这是 \'样例\' "引号" # 路径 \\ 目录\n下一行\r回车\t结尾。', None),
        ('A "quoted" # marker.', '一个 "引用" # 标记。', None),
        ('Hello [visitor!t], {b}bright{/b} {{glyph [[token.',
         '你好 [visitor!t]，{b}明亮{/b} {{glyph [[token。', "guide"),
        ('A {localtoken=quiet}signal{/localtoken}.',
         '一个{localtoken=quiet}信号{/localtoken}。', None),
    ],
    "none_language_strings": [("Badge", "徽章", None)],
    "controls_only": [],
    "bom_crlf": [("Snow # lantern", "雪 # 灯笼", None)],
    "no_final_newline": [("Finish", "完成", None)],
}

# Each rejection is bound to a concrete witness, not merely to a case count.
# Values are (fixture category, raw witness, occurrences in the complete input).
REJECTION_WITNESSES = {
    "unsupported_escape": ("invalid-escape", b'"Bad \\q escape."', 1),
    "physical_multiline": ("unsupported-syntax", b'"Across\n    two lines."', 1),
    "triple_quotes": ("unsupported-syntax", b'"""Three quotes."""', 1),
    "pass_target": ("unsupported-syntax", b"    pass", 1),
    "multiple_targets": ("ambiguous-slot", b'    guide "Second target."', 1),
    "multiple_sources": ("ambiguous-slot", b'    # guide "Second source."', 1),
    "missing_source": ("ambiguous-slot", b'    guide "Target without source."', 1),
    "missing_target": ("ambiguous-slot", b'    # guide "Source without target."', 1),
    "orphan_new": ("ambiguous-slot", b'    new "Unpaired target."', 1),
    "orphan_old": ("ambiguous-slot", b'    old "Unpaired source."', 1),
    "speaker_mismatch": ("ambiguous-slot", b'    keeper "Changed speaker."', 1),
    "mixed_languages": ("mixed-language", b"translate fr synthetic_two:", 1),
    "duplicate_label": ("duplicate-identity", b"translate zh_Hans repeated_label:", 2),
    "duplicate_old": ("duplicate-identity", b'    old "Repeated key"', 2),
    "duplicate_old_across_blocks": ("duplicate-identity", b"    old 'Repeated key'", 1),
    "none_language_dialogue": ("unsupported-syntax", b"translate None synthetic_none:", 1),
    "python_block": ("unsupported-syntax", b"    python:", 1),
    "style_block": ("unsupported-syntax", b"translate zh_Hans style default:", 1),
    "conditional_block": ("unsupported-syntax", b"    if synthetic_condition:", 1),
    "raw_game_statement": ("unsupported-syntax", b"label synthetic_start:", 1),
    "speaker_expression": ("unsupported-syntax", b'    cast.guide "A dotted speaker."', 1),
    "say_arguments": ("unsupported-syntax", b'    guide "Call arguments." (interact=False)', 1),
    "transition_expression": ("unsupported-syntax", b'    guide "Expression transition." with Dissolve(0.5)', 1),
    "empty_source_dialogue": ("invalid-source", b'    # guide ""', 1),
    "empty_source_strings": ("invalid-source", b'    old ""', 1),
    "tab_indent": ("unsupported-syntax", b'\tguide "Tab target."', 1),
    "unbalanced_interpolation": ("invalid-source", b'    # "Hello [visitor"', 1),
    "unterminated_string": ("unsupported-syntax", b'    guide "Unfinished target.\n', 1),
    "temporary_attribute_without_value": ("unsupported-syntax", b'    guide @ "Missing attribute."', 1),
    "duplicate_temporary_separator": ("unsupported-syntax", b'    guide @ happy @ calm "Two separators."', 1),
    "malformed_attribute": ("unsupported-syntax", b'    guide - "Missing attribute name."', 1),
    "control_expression": ("unsupported-syntax", b"    voice synthetic_audio_path", 1),
    "invalid_utf8": ("invalid-encoding", b"\xff", 1),
}


def materialize(case: dict[str, object]) -> bytes:
    """Read only checked-in bytes beneath this fixture root; usable by future goldens."""
    payload = case["payload"]
    relative = PurePosixPath(payload["path"])
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2:
        raise AssertionError("payload must be a fixture-local path")
    if relative.parts[0] != "payloads":
        raise AssertionError("payload must live in payloads")
    path = FIXTURE_ROOT.joinpath(*relative.parts)
    if any(part.is_symlink() for part in (path, path.parent, FIXTURE_ROOT)):
        raise AssertionError("fixture paths must not be symlinks")
    if not path.is_file():
        raise AssertionError(f"missing payload: {relative}")
    stored = path.read_bytes()
    if payload["kind"] == "file":
        return stored
    if payload["kind"] == "hex":
        return bytes.fromhex(stored.decode("ascii"))
    raise AssertionError("unknown payload representation")


def _decode_declared_literal(literal: str) -> str:
    """Check one predeclared oracle anchor; never scan or interpret an RPY program."""
    if len(literal) < 2 or literal[0] not in "\"'" or literal[-1] != literal[0]:
        raise AssertionError("expected one quoted literal")
    quote = literal[0]
    escapes = {"\\": "\\", quote: quote, "n": "\n", "r": "\r", "t": "\t"}
    decoded = []
    position = 1
    while position < len(literal) - 1:
        character = literal[position]
        if character == "\\":
            position += 1
            if position >= len(literal) - 1 or literal[position] not in escapes:
                raise AssertionError("undeclared escape in supported oracle literal")
            character = escapes[literal[position]]
        elif character == quote or character in "\r\n":
            raise AssertionError("unsupported physical quote/newline in oracle literal")
        decoded.append(character)
        position += 1
    return "".join(decoded)


class RpyFixtureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.cases = cls.manifest["cases"]
        cls.by_id = {case["case_id"]: case for case in cls.cases}

    def test_manifest_is_a_synthetic_specification_oracle(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["profile"], "renpy-tl-v1")
        self.assertEqual(self.manifest["provider_id"], "localcat.rpy")
        self.assertEqual(self.manifest["codec_id"], "renpy-tl")
        self.assertEqual(self.manifest["expectation_kind"], "specification-oracle")
        self.assertEqual(self.manifest["provenance"], {
            "origin": "localcat-synthetic",
            "contains_user_content": False,
            "external_inputs": [],
            "requires_renpy": False,
        })
        self.assertEqual(len(self.by_id), len(self.cases), "duplicate case ID")
        self.assertEqual(set(self.by_id), set(ACCEPTED_RECORDS) | set(REJECTION_WITNESSES))

    def test_all_payloads_are_local_closed_and_bound_to_exact_bytes(self) -> None:
        referenced = set()
        for case in self.cases:
            with self.subTest(case=case["case_id"]):
                raw = materialize(case)
                payload_path = case["payload"]["path"]
                self.assertNotIn(payload_path, referenced)
                referenced.add(payload_path)
                features = case["byte_features"]
                self.assertEqual(features["size_bytes"], len(raw))
                self.assertEqual(features["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(features["utf8_bom"], raw.startswith(b"\xef\xbb\xbf"))
                self.assertEqual(features["terminal_newline"], raw.endswith(b"\n"))
                newline = "CRLF" if b"\r\n" in raw else "LF"
                self.assertEqual(features["newline"], newline)
                self.assertNotIn(b"\r", raw.replace(b"\r\n", b""))
                if newline == "CRLF":
                    self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
                if case["case_id"] == "invalid_utf8":
                    self.assertFalse(features["valid_utf8"])
                    with self.assertRaises(UnicodeDecodeError):
                        raw.decode("utf-8-sig")
                else:
                    self.assertTrue(features["valid_utf8"])
                    raw.decode("utf-8-sig")
        checked_in = {path.relative_to(FIXTURE_ROOT).as_posix()
                      for path in (FIXTURE_ROOT / "payloads").iterdir()}
        self.assertEqual(referenced, checked_in, "orphan or missing payload")

    def test_supported_records_match_independent_neutral_expectations(self) -> None:
        for case_id, expected in ACCEPTED_RECORDS.items():
            with self.subTest(case=case_id):
                expectation = self.by_id[case_id]["expectation"]
                self.assertEqual(expectation["classification"], "supported")
                self.assertEqual(expectation["diagnostics"], [])
                self.assertEqual(expectation["records"], [
                    {"source": source, "target": target, "speaker": speaker, "confirmed": False}
                    for source, target, speaker in expected
                ])
                self.assertEqual(expectation["language"],
                                 "None" if case_id == "none_language_strings" else "zh_Hans")

    def test_supported_anchors_cover_every_physical_line_and_decode_exact_records(self) -> None:
        for case_id in ACCEPTED_RECORDS:
            with self.subTest(case=case_id):
                case = self.by_id[case_id]
                expectation = case["expectation"]
                raw = materialize(case)
                lines = raw.decode("utf-8-sig").splitlines()
                slots = expectation["slots"]
                records = expectation["records"]
                self.assertEqual(len(slots), len(records))
                self.assertEqual([slot["record_index"] for slot in slots], list(range(len(records))))
                covered = set()
                identities = set()
                previous_target_line = 0
                for slot, record in zip(slots, records, strict=True):
                    basis = slot["identity_basis"]
                    self.assertEqual(basis["language"], expectation["language"])
                    self.assertIn(basis["kind"], {"dialogue", "strings"})
                    if basis["kind"] == "dialogue":
                        self.assertEqual(set(basis), {"kind", "language", "label"})
                        label = basis["label"]
                    else:
                        self.assertEqual(set(basis), {"kind", "language", "old_sha256"})
                        self.assertIsNone(record["speaker"])
                        label = "strings"
                        self.assertEqual(basis["old_sha256"],
                                         hashlib.sha256(record["source"].encode("utf-8")).hexdigest())
                    header_line = slot["header_line"]
                    self.assertEqual(lines[header_line - 1],
                                     f'translate {basis["language"]} {label}:')
                    identity = json.dumps(basis, sort_keys=True)
                    self.assertNotIn(identity, identities)
                    identities.add(identity)
                    self.assertLess(previous_target_line, slot["source_anchor"]["line"])
                    self.assertLess(header_line, slot["source_anchor"]["line"])
                    self.assertLess(slot["source_anchor"]["line"], slot["target_anchor"]["line"])
                    previous_target_line = slot["target_anchor"]["line"]
                    for side in ("source", "target"):
                        anchor = slot[f"{side}_anchor"]
                        line = anchor["line"]
                        self.assertNotIn(line, covered)
                        covered.add(line)
                        self.assertEqual(lines[line - 1],
                                         anchor["prefix"] + anchor["literal"] + anchor["suffix"])
                        self.assertEqual(_decode_declared_literal(anchor["literal"]), record[side])
                        start, end = anchor["literal_byte_span"]
                        self.assertIs(type(start), int)
                        self.assertIs(type(end), int)
                        prefix_bytes = sum(len(part) for part in raw.splitlines(keepends=True)[:line - 1])
                        self.assertEqual(start, prefix_bytes + len(anchor["prefix"].encode("utf-8")))
                        self.assertEqual(raw[start:end], anchor["literal"].encode("utf-8"))
                    self.assertTrue(record["source"], "empty source cannot be a supported slot")
                for preserved in expectation["preserved_lines"]:
                    line = preserved["line"]
                    self.assertNotIn(line, covered)
                    covered.add(line)
                    self.assertEqual(lines[line - 1], preserved["text"])
                    self.assertIn(preserved["kind"], {"header", "comment", "blank", "control"})
                    if preserved["kind"] == "control":
                        self.assertTrue(preserved["text"].startswith("    voice ")
                                        or preserved["text"] == "    nvl clear")
                self.assertEqual(covered, set(range(1, len(lines) + 1)), "unaccounted source lines")

    def test_speaker_attributes_transitions_and_control_lines_remain_structural(self) -> None:
        mixed = self.by_id["mixed_dialogue_strings"]["expectation"]
        slot = mixed["slots"][0]
        self.assertEqual(slot["source_anchor"]["prefix"], "    # guide thinking ")
        self.assertEqual(slot["source_anchor"]["suffix"], " with dissolve")
        self.assertEqual(slot["target_anchor"]["prefix"], "    guide -thinking @ happy ")
        self.assertEqual(slot["target_anchor"]["suffix"], " with fade")
        special = self.by_id["special_say"]["expectation"]
        self.assertEqual(special["slots"][2]["source_anchor"]["prefix"],
                         "    # guide thinking calm -sad @ happy bright ")
        self.assertEqual(special["slots"][2]["target_anchor"]["prefix"], "    guide ")
        controls = [line["text"] for line in mixed["preserved_lines"] if line["kind"] == "control"]
        self.assertEqual(controls, ['    voice "audio/synthetic_lantern.ogg"', "    nvl clear"])
        self.assertEqual(self.by_id["controls_only"]["expectation"]["records"], [])
        self.assertEqual(mixed["records"][0]["source"], mixed["records"][-1]["source"])
        self.assertNotEqual(mixed["slots"][0]["identity_basis"], mixed["slots"][-1]["identity_basis"])
        self.assertEqual([slot["identity_basis"]["kind"] for slot in mixed["slots"]],
                         ["dialogue", "strings", "strings", "dialogue", "strings", "dialogue"])

    def test_rejected_inputs_have_real_witnesses_locations_and_no_partial_records(self) -> None:
        for case_id, (category, witness, occurrences) in REJECTION_WITNESSES.items():
            with self.subTest(case=case_id):
                case = self.by_id[case_id]
                raw = materialize(case)
                expectation = case["expectation"]
                self.assertEqual(expectation["classification"], "rejected")
                self.assertIsNone(expectation["language"])
                self.assertEqual(expectation["records"], [])
                self.assertEqual(expectation["slots"], [])
                self.assertEqual(len(expectation["diagnostics"]), 1)
                diagnostic = expectation["diagnostics"][0]
                self.assertEqual(diagnostic["category"], category)
                self.assertTrue(diagnostic["reason"])
                self.assertEqual(bytes.fromhex(diagnostic["witness_hex"]), witness)
                self.assertEqual(raw.count(witness), occurrences)
                start = diagnostic["byte_offset"]
                self.assertEqual(raw[start:start + len(witness)], witness)
                self.assertEqual(diagnostic["line"], raw[:start].count(b"\n") + 1)
                # Fixture diagnostic columns are one-based byte columns, documented in README.
                last_newline = raw.rfind(b"\n", 0, start)
                self.assertEqual(diagnostic["byte_column"], start - last_newline)
                if occurrences > 1:
                    self.assertEqual(start, raw.rfind(witness), "locate the conflicting occurrence")

    def test_rejection_witnesses_exercise_mapping_and_whole_document_boundaries(self) -> None:
        decoded = {case_id: materialize(self.by_id[case_id]).decode("utf-8")
                   for case_id in REJECTION_WITNESSES if case_id != "invalid_utf8"}
        self.assertIn('    # guide "First source."', decoded["multiple_sources"])
        self.assertIn('    guide "First target."', decoded["multiple_targets"])
        self.assertIn('    # guide "Original speaker."', decoded["speaker_mismatch"])
        self.assertIn('    old "Repeated key"', decoded["duplicate_old_across_blocks"])
        self.assertEqual(decoded["duplicate_old_across_blocks"].count("translate zh_Hans strings:"), 2)
        self.assertIn("translate zh_Hans synthetic_one:", decoded["mixed_languages"])
        self.assertIn('    guide "Valid target before invalid tail."', decoded["python_block"])
        self.assertNotIn("    new ", decoded["orphan_old"])
        self.assertNotIn("    old ", decoded["orphan_new"])
        self.assertNotIn("    # ", decoded["missing_source"])
        self.assertNotIn('\n    guide "', decoded["missing_target"])

    def test_bom_crlf_and_missing_final_newline_are_actual_bytes(self) -> None:
        raw = materialize(self.by_id["bom_crlf"])
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf#"))
        self.assertEqual(raw.count(b"\r\n"), 5)
        self.assertIn('    "雪 # 灯笼"\r\n'.encode("utf-8"), raw)
        self.assertFalse(materialize(self.by_id["no_final_newline"]).endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
