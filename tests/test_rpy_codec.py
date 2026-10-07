"""Full-input grammar checks against independent synthetic TL oracles."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest import mock

from parser_contracts import (
    IssueSeverity, ParseIssue, ParsedSegment, RawSpeaker,
    TargetPresence, TranslationState,
)
from parser_rpy_codec import RpyInputError, RpyLexicalLimits, parse_tl, scan_tl
from tests.test_rpy_fixture_contract import MANIFEST_PATH, materialize


def dialogue(source='"Source"', target='"Target"', extra='') -> bytes:
    return (f'translate zh_Hans unit:\n    # {source}\n'
            f'    {target}\n{extra}').encode('utf-8')


class RpyLexicalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))['cases']

    def assert_rejected(self, raw, category=None, **kwargs):
        with self.assertRaises(RpyInputError) as captured:
            scan_tl(raw, **kwargs)
        error = captured.exception
        self.assertIs(type(error.issue), ParseIssue)
        self.assertIs(error.issue.severity, IssueSeverity.FATAL)
        self.assertTrue(error.issue.code.startswith('PARSER.RPY.'))
        self.assertGreaterEqual(error.issue.line_number, 1)
        self.assertGreaterEqual(error.issue.byte_offset, 0)
        self.assertLessEqual(error.issue.byte_offset, len(raw))
        self.assertGreaterEqual(error.byte_column, 1)
        self.assertFalse(hasattr(error, 'slots'))
        if category:
            self.assertEqual(error.category, category)
        return error

    def test_supported_fixtures_have_exact_text_and_literal_byte_spans(self):
        for case in self.cases:
            expected = case['expectation']
            if expected['classification'] != 'supported':
                continue
            raw = materialize(case)
            with self.subTest(case=case['case_id']):
                document = scan_tl(raw)
                self.assertIs(document.raw, raw)
                self.assertEqual(document.language, expected['language'])
                self.assertEqual(len(document.slots), len(expected['records']))
                for slot, record, oracle in zip(document.slots, expected['records'], expected['slots'], strict=True):
                    self.assertEqual(slot.source.literal.text, record['source'])
                    self.assertEqual(slot.target.literal.text, record['target'])
                    self.assertEqual(slot.source.speaker, record['speaker'])
                    self.assertEqual(slot.target.speaker, record['speaker'])
                    self.assertEqual(slot.header_line, oracle['header_line'])
                    for actual, anchor in ((slot.source.literal, oracle['source_anchor']),
                                           (slot.target.literal, oracle['target_anchor'])):
                        self.assertEqual((actual.start, actual.end),
                                         tuple(anchor['literal_byte_span']))
                        self.assertEqual(raw[actual.start:actual.end].decode('utf-8'), anchor['literal'])
                        self.assertEqual(actual.line, anchor['line'])

    def test_rejected_fixtures_have_expected_safe_category(self):
        for case in self.cases:
            if case['expectation']['classification'] == 'supported':
                continue
            with self.subTest(case=case['case_id']):
                self.assert_rejected(materialize(case), case['expectation']['diagnostics'][0]['category'])

    def test_attributes_and_transition_are_separate_lexical_facts(self):
        slot = scan_tl(dialogue('guide calm -sad @ happy bright "S" with dissolve',
                                'guide -calm "T" with fade')).slots[0]
        self.assertEqual(slot.source.speaker, 'guide')
        self.assertEqual(slot.source.attributes, ('calm', '-sad'))
        self.assertEqual(slot.source.temporary_attributes, ('happy', 'bright'))
        self.assertEqual(slot.source.transition, 'dissolve')
        self.assertEqual(slot.target.attributes, ('-calm',))
        self.assertEqual(slot.target.transition, 'fade')

    def test_quoted_speaker_maps_only_dialogue_to_translation_slot(self):
        for name, speaker in (('"???"', '???'), ('"Alarm clock"', 'Alarm clock'),
                              ('"[character]"', '[character]'),
                              (r'"A \"name\" #"', 'A "name" #'),
                              ("'向导'", '向导'), ('"with"', 'with')):
            with self.subTest(name=name):
                raw = dialogue(name + ' "Source"', name + ' ""')
                with mock.patch('builtins.eval', side_effect=AssertionError('must not evaluate')):
                    result = parse_tl(raw)
                record = result.records[0]
                self.assertEqual((record.source, record.target, record.speaker.value),
                                 ('Source', '', speaker))
                slot = result.lexical.slots[0]
                self.assertEqual(raw[slot.source.literal.start:slot.source.literal.end], b'"Source"')
                self.assertEqual(raw[slot.target.literal.start:slot.target.literal.end], b'""')

    def test_quoted_speaker_preserves_attributes_transition_and_comment_quotes(self):
        raw = dialogue('"[character]" calm -sad @ happy "Source" with dissolve',
                       '"[character]" -calm "Target" with fade # "comment"')
        slot = scan_tl(raw).slots[0]
        self.assertEqual(slot.source.speaker, '[character]')
        self.assertEqual(slot.source.attributes, ('calm', '-sad'))
        self.assertEqual(slot.source.temporary_attributes, ('happy',))
        self.assertEqual(slot.source.transition, 'dissolve')
        self.assertEqual(slot.target.attributes, ('-calm',))
        self.assertEqual(slot.target.transition, 'fade')
        narrator = scan_tl(dialogue('"Source" with dissolve # "comment"',
                                   '"Target" # "comment"')).slots[0]
        self.assertIsNone(narrator.source.speaker)
        self.assertIsNone(narrator.target.speaker)

    def test_quoted_speaker_and_identifier_must_match_in_value_and_kind(self):
        for source, target in (('"A" "S"', '"B" "T"'),
                               ('"guide" "S"', 'guide "T"'),
                               ('guide "S"', '"guide" "T"'),
                               ('"" "S"', '"T"')):
            with self.subTest(source=source, target=target):
                self.assert_rejected(dialogue(source, target), 'ambiguous-slot')

    def test_quoted_speaker_keeps_finite_grammar_and_string_limits(self):
        for source in ('"Name""S"', '"Name" + "S"', '"Name" @ "S"',
                       '"Name" "S" "extra"', '"Name"[index] "S"',
                       '"Name" ""', '"Name" "[unclosed"'):
            with self.subTest(source=source):
                self.assert_rejected(dialogue(source, '"Name" ""'))
        self.assert_rejected(dialogue('"Long name" "S"', '"Long name" ""'),
                             'limit-exceeded', limits=RpyLexicalLimits(max_string_bytes=4))
        self.assert_rejected(dialogue(r'"N\x20" "S"', '"Name" ""'), 'invalid-escape')

    def test_comments_quotes_and_hashes_preserve_raw_without_becoming_slots(self):
        raw = (b'# "a quote" # arbitrary comment\ntranslate zh_Hans unit: # header comment\n'
               b'    # line.rpy:12 "quoted note"\n    # "Source # text" # source suffix\n'
               b'    "Target # text" # "not another literal"\n')
        document = scan_tl(raw)
        self.assertEqual(len(document.slots), 1)
        self.assertEqual(document.slots[0].target.literal.text, 'Target # text')
        self.assertIs(document.raw, raw)

    def test_unicode_identifier_and_mixed_newline_offsets(self):
        raw = ('\ufefftranslate 中文 片段:\r\n    # 向导 高兴 "雪"\n'
               '    向导 "Snow"\r\n').encode('utf-8')
        slot = scan_tl(raw).slots[0]
        self.assertEqual(slot.source.speaker, '向导')
        self.assertEqual(raw[slot.source.literal.start:slot.source.literal.end], '"雪"'.encode())

    def test_indent_requires_one_level_and_spaces(self):
        for raw in (dialogue(extra='        "Extra"\n'),
                    b'translate zh_Hans strings:\n    old "S"\n  new "T"\n',
                    b' translate zh_Hans strings:\n    old "S"\n    new "T"\n',
                    dialogue(target='"T"\r    python:'),
                    dialogue(target='"T"\x00')):
            with self.subTest(raw=raw):
                self.assert_rejected(raw, 'unsupported-syntax')

    def test_empty_or_comment_only_is_not_a_tl_document(self):
        for raw in (b'', b'\xef\xbb\xbf', b' # comment\n', b'translate zh_Hans unit:\n'):
            self.assert_rejected(raw)

    def test_supported_controls_require_finite_literals(self):
        for statement in ('voice sound_path', 'voice "ok" extra', 'nvl clear extra',
                          '$ dangerous()', 'python:', 'style foo:', 'if flag:'):
            with self.subTest(statement=statement):
                self.assert_rejected(dialogue(extra=f'    {statement}\n'), 'unsupported-syntax')

    def test_commented_control_is_preserved_and_not_a_speaker(self):
        raw = (b'translate zh_Hans unit:\n    # voice "audio.ogg"\n'
               b'    voice "audio.ogg"\n    # guide "S"\n    guide "T"\n')
        document = scan_tl(raw)
        self.assertEqual(len(document.slots), 1)
        self.assertEqual(document.slots[0].source.speaker, 'guide')
        self.assertIs(document.raw, raw)

    def test_strings_do_not_accept_dialogue_or_control_statements(self):
        for statement in ('guide "Text"', 'voice "audio"', 'nvl clear'):
            self.assert_rejected(('translate zh_Hans strings:\n    '+statement+'\n').encode(),
                                 'unsupported-syntax')

    def test_decode_only_declared_escapes_and_preserve_whitespace(self):
        slot = scan_tl(dialogue(r'"  A\nB\rC\tD\\E\"F  "', '""')).slots[0]
        self.assertEqual(slot.source.literal.text, '  A\nB\rC\tD\\E"F  ')
        self.assertEqual(slot.target.literal.text, '')
        for literal in (r'"\u1234"', r'"\x20"', r'"\a"', r'"\b"', r'"\v"'):
            self.assert_rejected(dialogue(literal), 'invalid-escape')

    def test_source_interpolation_is_structural_and_never_executed(self):
        source = r'''"[[literal [mapping['key]']!t] [items[index[0]]] {{literal {b}x{/b}"'''
        with mock.patch('builtins.eval', side_effect=AssertionError('must not evaluate')) as evaluate:
            self.assertEqual(len(scan_tl(dialogue(source)).slots), 1)
        evaluate.assert_not_called()
        for source in ('"[unclosed"', '"[mapping[0]] [broken"', '"[x(]]"', '"{unclosed"'):
            self.assert_rejected(dialogue(source), 'invalid-source')

    def test_existing_target_with_bad_tokens_remains_editable(self):
        self.assertEqual(scan_tl(dialogue('"[who]"', '"[broken"')).slots[0].target.literal.text,
                         '[broken')

    def test_full_input_error_after_valid_slot_returns_no_candidate(self):
        for suffix in (b'translate zh_Hans second:\n    # "Source"\n',
                       b'python:\n    dangerous()\n', b'\xff'):
            self.assert_rejected(dialogue()+suffix)

    def test_each_incomplete_prefix_of_final_target_fails(self):
        raw = dialogue(target='"Unicode 译文"')
        start = raw.rindex(b'    ')
        for end in range(start, len(raw)-2):
            with self.subTest(end=end):
                self.assert_rejected(raw[:end])

    def test_limits_are_finite_and_cannot_enlarge_profile(self):
        for field, value in (('max_input_bytes', 0), ('max_slots', True),
                             ('max_slots', 100001), ('max_string_bytes', 1048577)):
            with self.assertRaises((TypeError, ValueError)):
                RpyLexicalLimits(**{field: value})
        raw = dialogue('"雪雪"', '"T"')
        self.assertEqual(len(scan_tl(raw, limits=RpyLexicalLimits(max_input_bytes=len(raw), max_string_bytes=6)).slots), 1)
        self.assert_rejected(raw, 'limit-exceeded', limits=RpyLexicalLimits(max_input_bytes=len(raw)-1))
        self.assert_rejected(raw, 'limit-exceeded', limits=RpyLexicalLimits(max_string_bytes=5))
        self.assert_rejected(dialogue('"S"', '"Too long"'), 'limit-exceeded', limits=RpyLexicalLimits(max_string_bytes=3))
        pair = b'translate zh_Hans strings:\n    old "a"\n    new ""\n    old "b"\n    new ""\n'
        self.assert_rejected(pair, 'limit-exceeded', limits=RpyLexicalLimits(max_slots=1))

    def test_cancellation_and_handler_failure_cannot_return_partial_document(self):
        class Cancelled(Exception):
            pass
        for calls_before_cancel in (0, 1, 2, 3):
            checker = mock.Mock(side_effect=[None]*calls_before_cancel+[Cancelled()])
            with self.assertRaises(Cancelled):
                scan_tl(dialogue(), check_cancelled=checker)
            self.assertTrue(checker.called)

    def test_diagnostics_have_byte_location_and_no_body(self):
        raw = dialogue('"机密 PRIVATE-BODY"', '"ok"', '    $ forbidden()\n')
        error = self.assert_rejected(raw)
        self.assertNotIn('PRIVATE-BODY', str(error))
        self.assertNotIn('forbidden', error.issue.safe_summary)
        self.assertEqual(error.issue.line_number, 4)
        self.assertEqual(error.issue.byte_offset, raw.index(b'$'))
        self.assertEqual(error.byte_column, 5)

    def test_result_facts_are_immutable_and_not_parser_terminal_authority(self):
        document = scan_tl(dialogue())
        self.assertIs(type(document.slots), tuple)
        with self.assertRaises(FrozenInstanceError):
            document.language = 'changed'
        self.assertFalse(hasattr(document, 'terminal'))
        self.assertFalse(hasattr(document.slots[0], 'local_id'))


class RpyNeutralMappingTests(unittest.TestCase):
    def test_fixtures_map_to_neutral_unconfirmed_segments_in_order(self):
        cases = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))['cases']
        for case in cases:
            expected = case['expectation']
            if expected['classification'] != 'supported':
                continue
            with self.subTest(case=case['case_id']):
                result = parse_tl(materialize(case))
                self.assertEqual(len(result.records), len(expected['records']))
                for actual, record in zip(result.records, expected['records'], strict=True):
                    self.assertIs(type(actual), ParsedSegment)
                    self.assertEqual(actual.source, record['source'])
                    self.assertEqual(actual.target, record['target'])
                    self.assertEqual(actual.speaker, RawSpeaker(record['speaker'] or ''))
                    self.assertIs(actual.translation_state, TranslationState.UNCONFIRMED)
                    self.assertIs(actual.target_presence,
                                  TargetPresence.EXPLICIT_EMPTY if not record['target'] else TargetPresence.PRESENT)
                    self.assertEqual(actual.format_metadata, ())
                    self.assertTrue(actual.local_id)
                self.assertEqual(len({record.local_id for record in result.records}), len(result.records))

    def test_speaker_is_only_raw_identifier_and_strings_never_inherit_it(self):
        raw = dialogue('guide -sad @ happy "S" with dissolve', 'guide calm "T" with fade')
        raw += b'translate zh_Hans strings:\n    old "Menu"\n    new ""\n'
        result = parse_tl(raw)
        self.assertEqual([item.speaker.value for item in result.records], ['guide', ''])
        self.assertEqual(result.lexical.slots[0].source.attributes, ('-sad',))
        self.assertEqual(result.lexical.slots[0].target.transition, 'fade')
        self.assertIs(result.lexical.raw, raw)

    def test_special_say_and_narrator_do_not_infer_previous_character(self):
        raw = b''
        for index, speaker in enumerate(('guide', 'extend', 'centered', '')):
            prefix = speaker + ' ' if speaker else ''
            raw += dialogue(prefix+'"Source"', prefix+'""').replace(
                b'unit:', f'unit_{index}:'.encode(),
            )
        records = parse_tl(raw).records
        self.assertEqual([record.speaker.value for record in records],
                         ['guide', 'extend', 'centered', ''])
        self.assertEqual([record.target for record in records], ['', '', '', ''])
        self.assertEqual(len({record.local_id for record in records}), 4)

    def test_same_display_text_keeps_dialogue_and_strings_as_distinct_slots(self):
        raw = dialogue('"Same"', '""')
        raw += b'translate zh_Hans strings:\n    old "Same"\n    new "T"\n'
        records = parse_tl(raw).records
        self.assertEqual([record.source for record in records], ['Same', 'Same'])
        self.assertEqual([record.target for record in records], ['', 'T'])
        self.assertNotEqual(records[0].local_id, records[1].local_id)

    def test_label_identity_survives_source_and_display_changes(self):
        original = parse_tl(dialogue('guide happy "Original"', 'guide "T"')).records[0]
        changed = parse_tl(dialogue('guide -sad @ calm "Changed" with fade', 'guide "New target"')).records[0]
        self.assertEqual(original.local_id, changed.local_id)
        self.assertNotEqual(original.source, changed.source)
        other = parse_tl(dialogue().replace(b'unit:', b'other:')).records[0]
        other_language = parse_tl(dialogue().replace(b'zh_Hans', b'fr')).records[0]
        self.assertNotEqual(original.local_id, other.local_id)
        self.assertNotEqual(original.local_id, other_language.local_id)

    def test_identity_is_independent_of_file_order_and_blank_lines(self):
        one = dialogue('"Same"', '"T1"').replace(b'unit:', b'first:')
        two = dialogue('"Same"', '"T2"').replace(b'unit:', b'second:')
        strings = b'translate zh_Hans strings:\n    old "Open"\n    new ""\n'
        before = parse_tl(one+two+strings).records
        after = parse_tl(b'# location change\n\n'+strings+b'\n'+two+one).records
        self.assertEqual([r.local_id for r in before], [r.local_id for r in reversed(after)])
        self.assertEqual(len({r.local_id for r in before}), 3)

    def test_strings_identity_uses_full_decoded_old_and_language(self):
        def read(old, language='zh_Hans', target='""'):
            return parse_tl(f'translate {language} strings:\n    old {old}\n    new {target}\n'.encode()).records[0]
        plain = read('"Open"')
        escaped = read(r'"Line\nNext"')
        alternate_quote = read("'Line\\nNext'", target='"Translation"')
        self.assertEqual(escaped.local_id, alternate_quote.local_id)
        self.assertNotEqual(plain.local_id, read('"Open{#verb}"').local_id)
        self.assertNotEqual(plain.local_id, read('"Open"', 'fr').local_id)
        self.assertNotEqual(plain.local_id, read('"Open "').local_id)
        self.assertEqual(plain.local_id, read('"Open"', target='"Changed"').local_id)
        self.assertEqual(len(plain.local_id.rsplit(':', 1)[-1]), 64)

    def test_dialogue_language_label_encoding_is_unambiguous(self):
        left = parse_tl(dialogue().replace(b'zh_Hans unit', b'a bc')).records[0].local_id
        right = parse_tl(dialogue().replace(b'zh_Hans unit', b'ab c')).records[0].local_id
        self.assertNotEqual(left, right)

    def test_no_partial_neutral_result_for_duplicate_identity(self):
        for raw in (dialogue()+dialogue(),
                    b'translate zh_Hans strings:\n    old "a"\n    new ""\n    old \'a\'\n    new "T"\n'):
            with self.assertRaises(RpyInputError) as raised:
                parse_tl(raw)
            self.assertEqual(raised.exception.category, 'duplicate-identity')
            self.assertFalse(hasattr(raised.exception, 'records'))

    def test_invalid_final_block_cannot_return_earlier_neutral_records(self):
        raw = dialogue()+b'translate zh_Hans final:\n    # "Unfinished"\n'
        with self.assertRaises(RpyInputError) as raised:
            parse_tl(raw)
        self.assertEqual(raised.exception.category, 'ambiguous-slot')
        self.assertEqual(raised.exception.issue.line_number, 4)
        self.assertFalse(hasattr(raised.exception, 'records'))

    def test_digest_collision_is_rejected_instead_of_renumbered(self):
        raw = b'translate zh_Hans strings:\n    old "a"\n    new ""\n    old "b"\n    new ""\n'
        with mock.patch('parser_rpy_codec._strings_identity_digest', return_value='0'*64) as digest:
            with self.assertRaises(RpyInputError) as raised:
                parse_tl(raw)
        self.assertEqual(digest.call_count, 2)
        self.assertEqual(raised.exception.category, 'duplicate-identity')
        self.assertEqual(raised.exception.issue.line_number, 4)

    def test_mapping_cancellation_after_valid_lexing_returns_no_result(self):
        checks = []
        raw = dialogue()
        scan_tl(raw, check_cancelled=lambda: checks.append(None))
        checkpoint = mock.Mock(side_effect=[None]*len(checks)+[InterruptedError('cancelled')])
        with self.assertRaises(InterruptedError):
            parse_tl(raw, check_cancelled=checkpoint)


if __name__ == '__main__':
    unittest.main()
