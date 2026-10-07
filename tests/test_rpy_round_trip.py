"""Round-trip bytes are format data; slot edits never acquire publication rights."""

import hashlib
import json
import unittest
from unittest import mock

from parser_contracts import FormatId, PreparedFormatBytes, RoundTripLimits, RoundTripSegmentEdit
import parser_rpy_codec as codec
from tests.test_rpy_codec import dialogue
from tests.test_rpy_fixture_contract import MANIFEST_PATH, materialize


class Cancelled(Exception):
    pass


class RpyRoundTripTests(unittest.TestCase):
    def edits(self, raw, *targets):
        records = codec.parse_tl(raw).records
        return tuple(RoundTripSegmentEdit(record.local_id, targets[index] if targets else record.target)
                     for index, record in enumerate(records))

    def prepare(self, raw, *targets, **kwargs):
        return codec.prepare_round_trip_bytes(raw, codec.build_private_payload(raw),
                                             self.edits(raw, *targets), **kwargs)

    def assert_bad_target(self, raw, target, category='placeholder-mismatch'):
        with self.assertRaises(codec.RpyInputError) as caught:
            self.prepare(raw, target)
        error = caught.exception
        record = codec.parse_tl(raw).records[0]
        literal = codec.scan_tl(raw).slots[0].target.literal
        self.assertEqual(error.category, category)
        self.assertEqual(error.local_id, record.local_id)
        self.assertEqual(error.issue.record_number, 1)
        self.assertEqual(error.issue.line_number, literal.line)
        self.assertEqual(error.issue.byte_offset, literal.start)
        self.assertNotIn('secret', str(error))
        return error

    def test_legal_supported_fixtures_noop_are_exact_bytes(self):
        cases = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))['cases']
        for case in cases:
            if case['expectation']['classification'] != 'supported':
                continue
            raw = materialize(case)
            with self.subTest(case=case['case_id']):
                self.assertEqual(self.prepare(raw).payload, raw)

    def test_neutral_result_contains_only_format_bytes_and_fingerprints(self):
        raw = dialogue()
        result = self.prepare(raw, 'Changed')
        self.assertIs(type(result), PreparedFormatBytes)
        self.assertEqual(result.codec_identity, codec.RPY_CODEC_IDENTITY)
        self.assertEqual(result.format_id, FormatId('renpy-tl-v1'))
        self.assertEqual(result.source_fingerprint, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.output_fingerprint, hashlib.sha256(result.payload).hexdigest())
        self.assertEqual(result.diagnostics, ())
        for name in ('receipt', 'commit', 'publish', 'writer', 'terminal'):
            self.assertFalse(hasattr(result, name))

    def test_golden_only_target_interior_changes_with_bom_crlf_and_no_final_newline(self):
        raw = ('\ufeff# header " #\r\ntranslate zh_Hans unit:\r\n'
               '    # guide -thinking @ happy "Source" with dissolve # source\r\n'
               '    voice "voice/file.ogg"\r\n'
               '    guide calm "Old" with fade # target\r\n'
               '    nvl clear').encode()
        target = '雪 "quote" \\ path\nnext\rline\ttab  😀'
        expected = raw.replace(b'"Old"', '"雪 \\"quote\\" \\\\ path\\nnext\\rline\\ttab  😀"'.encode())
        result = self.prepare(raw, target)
        self.assertEqual(result.payload, expected)
        self.assertEqual(codec.parse_tl(result.payload).records[0].target, target)
        self.assertEqual(result.payload.count(b'\r\n'), raw.count(b'\r\n'))

    def test_single_quote_style_and_script_injection_are_encoded(self):
        raw = dialogue('"Source"', "'Old'")
        target = "a'b\"c\\\n    python:\n        secret()"
        result = self.prepare(raw, target)
        self.assertEqual(result.payload, raw.replace(b"'Old'", b"'a\\'b\"c\\\\\\n    python:\\n        secret()'"))
        self.assertEqual(codec.parse_tl(result.payload).records[0].target, target)

    def test_quoted_speaker_roundtrip_changes_only_second_literal_interior(self):
        raw = ('\ufefftranslate zh_Hans unit:\r\n'
               '    # "[character]" -sad @ happy "Source" with dissolve\r\n'
               '    "[character]" calm \'Old\' with fade # "comment"').encode()
        self.assertEqual(self.prepare(raw).payload, raw)
        target = "译文 'quoted' [character]"
        # Name interpolation is display metadata, not a source-text placeholder.
        self.assert_bad_target(raw, target)
        target = "译文 'quoted'"
        expected = raw.replace(b"'Old'", "'译文 \\'quoted\\''".encode())
        result = self.prepare(raw, target)
        self.assertEqual(result.payload, expected)
        record = codec.parse_tl(result.payload).records[0]
        self.assertEqual(record.target, target)
        self.assertEqual(record.speaker.value, '[character]')

    def test_quoted_name_span_cannot_be_substituted_for_private_target_span(self):
        raw = dialogue('"Name" "Source"', '"Name" "Target"')
        payload = json.loads(codec.build_private_payload(raw))
        name_start = raw.rindex(b'"Name"')
        next(iter(payload['slots'].values()))['target_span'] = [name_start, name_start + 6]
        with self.assertRaises(codec.RpyInputError) as caught:
            codec.prepare_round_trip_bytes(raw, json.dumps(payload).encode(), self.edits(raw, 'Other'))
        self.assertEqual(caught.exception.category, 'private-stale')

    def test_empty_targets_export_without_source_fill(self):
        raw = dialogue('"[who] {b}Source{/b}"', '""')
        self.assertEqual(self.prepare(raw).payload, raw)
        self.assertEqual(self.prepare(dialogue('"[who] {b}Source{/b}"'), '').payload,
                         dialogue('"[who] {b}Source{/b}"', '""'))

    def test_full_edit_set_can_be_reordered_and_multiple_slots_are_replaced(self):
        raw = dialogue() + b'\ntranslate zh_Hans strings:\n    old "Other"\n    new "Old"\n'
        edits = tuple(reversed(self.edits(raw, '第一', 'Second')))
        result = codec.prepare_round_trip_bytes(raw, codec.build_private_payload(raw), edits)
        self.assertEqual(result.payload, raw.replace(b'"Target"', '"第一"'.encode()).replace(b'new "Old"', b'new "Second"'))

    def test_noop_bad_existing_target_is_rejected_but_can_be_repaired(self):
        raw = dialogue('"{b}{i}Source{/i}{/b} [who]"', '"{b}{i}secret{/b}{/i} [who]"')
        self.assert_bad_target(raw, codec.parse_tl(raw).records[0].target)
        target = '{i}{b}修复{/b}{/i} [who]'
        self.assertEqual(codec.parse_tl(self.prepare(raw, target).payload).records[0].target, target)

    def test_exact_interpolation_multiset_allows_reordering(self):
        source = '[who!t] [items[index[0]]] [who!t] [score:.2f]'
        target = '[score:.2f] 雪 [who!t] [who!t] [items[index[0]]]'
        raw = dialogue('"' + source + '"', '""')
        self.assertEqual(codec.parse_tl(self.prepare(raw, target).payload).records[0].target, target)
        for invalid in (source.replace('[who!t]', '[who]', 1), source.replace('[who!t]', '', 1),
                        source + ' [secret()]', source.replace(':.2f', ':.3f'), '[unterminated', '[a)]'):
            with self.subTest(target=invalid):
                self.assert_bad_target(raw, invalid)

    def test_quoted_brackets_and_literal_escapes_are_not_evaluated(self):
        source = "[mapping['key]']!t] [[literal {{literal [secret()]"
        raw = dialogue('"' + source + '"', '""')
        with mock.patch('builtins.eval', side_effect=AssertionError('executed')), mock.patch('builtins.exec', side_effect=AssertionError('executed')):
            result = self.prepare(raw, source + ' 中文')
        self.assertEqual(codec.parse_tl(result.payload).records[0].target, source + ' 中文')
        self.assert_bad_target(dialogue('"Literal [[ {{"', '""'), '[new] {{')

    def test_tag_parameters_counts_and_order_are_protected(self):
        source = '{b}a{/b}{b}{color=#fff}b{/color}{/b}'
        raw = dialogue('"' + source + '"', '""')
        for bad in ('{b}a{/b}{color=#fff}b{/color}', source.replace('#fff', '#000'),
                    source + '{i}', source.replace('{/color}{/b}', '{/b}{/color}')):
            with self.subTest(target=bad):
                self.assert_bad_target(raw, bad)
        target = '{color=#fff}{b}雪{/b}{/color}{b}人{/b}'
        self.assertEqual(codec.parse_tl(self.prepare(raw, target).payload).records[0].target, target)

    def test_implicit_closing_and_self_closing_tags_are_supported(self):
        source = '{b}{image=x}{space=4}{vspace=8}{w}{p=1}{nw}{fast}{done}{clear}{#verb with spaces}Text'
        raw = dialogue('"' + source + '"', '""')
        target = source.replace('Text', '中文')
        self.assertEqual(codec.parse_tl(self.prepare(raw, target).payload).records[0].target, target)
        self.assert_bad_target(raw, source + '{/b}')

    def test_feature_style_and_custom_tag_pairing(self):
        source = '{feature:liga=0}{=mystyle}{custom=x}A{/custom}{custom_single=y}{/}{/feature}'
        raw = dialogue('"' + source + '"', '""')
        target = source.replace('A', '字')
        self.assertEqual(codec.parse_tl(self.prepare(raw, target).payload).records[0].target, target)
        self.assert_bad_target(raw, target.replace('{/}{/feature}', '{/feature}{/}'))
        self.assert_bad_target(raw, target.replace('{custom_single=y}', '{unknown=y}'))

    def test_unmatched_tags_and_malformed_token_syntax_fail_safely(self):
        for source, target in (('{b}secret{/b}', '{/b}secret{b}'), ('{b}secret{/b}', '{b secret'),
                               ('{image=x}{/image}', '{image=x}secret{/image}'),
                               ('{}', 'secret{}'), ('{/b=x}', 'secret{/b=x}')):
            self.assert_bad_target(dialogue('"' + source + '"', '""'), target)

    def test_controls_and_surrogates_have_structured_locatable_failures(self):
        raw = dialogue()
        for bad in ('secret\x00', 'secret\x1f', 'secret\x7f', 'secret\x85', 'secret\ud800'):
            with self.subTest(target=repr(bad)):
                self.assert_bad_target(raw, bad, 'invalid-escape')

    def test_missing_duplicate_foreign_or_non_neutral_edits_are_rejected(self):
        raw = dialogue()
        payload = codec.build_private_payload(raw)
        good = self.edits(raw)
        for edits in ((), good + good, (RoundTripSegmentEdit('foreign', 'secret'),)):
            with self.subTest(edits=edits), self.assertRaises(codec.RpyInputError) as caught:
                codec.prepare_round_trip_bytes(raw, payload, edits)
            self.assertEqual(caught.exception.category, 'invalid-edits')
        for edits in (list(good), iter(good), (object(),)):
            with self.subTest(edits=edits), self.assertRaises(TypeError):
                codec.prepare_round_trip_bytes(raw, payload, edits)

    def test_tampered_private_and_source_are_reverified(self):
        raw = dialogue()
        payload = codec.build_private_payload(raw)
        changed = json.loads(payload)
        next(iter(changed['slots'].values()))['target_span'] = [0, 1]
        for source, private in ((raw, json.dumps(changed).encode()), (raw.replace(b'Source', b'Other!'), payload)):
            with self.assertRaises(codec.RpyInputError) as caught:
                codec.prepare_round_trip_bytes(source, private, self.edits(source))
            self.assertEqual(caught.exception.category, 'private-stale')

    def test_smaller_output_limit_counts_encoded_utf8_and_unchanged_bytes(self):
        raw = dialogue()
        expected = self.prepare(raw, '雪\n').payload
        for limit in (len(expected) - 1, len(raw) - 1):
            target = '雪\n' if limit == len(expected) - 1 else 'Target'
            with self.assertRaises(codec.RpyInputError) as caught:
                self.prepare(raw, target, round_trip_limits=RoundTripLimits(limit, 32 * 1024 * 1024))
            self.assertEqual(caught.exception.category, 'limit-exceeded')
        self.assertEqual(self.prepare(raw, '雪\n', round_trip_limits=RoundTripLimits(len(expected), 32 * 1024 * 1024)).payload, expected)

    def test_smaller_source_private_and_string_limits_remain_enforced(self):
        raw = dialogue()
        payload = codec.build_private_payload(raw)
        for kwargs in ({'limits': codec.RpyLexicalLimits(max_input_bytes=len(raw)-1)},
                       {'limits': codec.RpyLexicalLimits(max_string_bytes=6)},
                       {'round_trip_limits': RoundTripLimits(32*1024*1024, len(payload)-1)}):
            with self.assertRaises(codec.RpyInputError) as caught:
                self.prepare(raw, '1234567', **kwargs)
            self.assertEqual(caught.exception.category, 'limit-exceeded')
        self.assert_bad_target(raw, '雪' * 349526, 'limit-exceeded')
        self.assertEqual(codec.parse_tl(self.prepare(raw, '雪雪', limits=codec.RpyLexicalLimits(max_string_bytes=6)).payload).records[0].target, '雪雪')

    def test_output_profile_ceiling_wins_over_a_larger_owner_budget(self):
        raw = b'translate zh_Hans strings:\n' + b''.join(
            f'    old "Source {index}"\n    new ""\n'.encode() for index in range(17))
        target = '"' * (1024 * 1024)
        with self.assertRaises(codec.RpyInputError) as caught:
            self.prepare(raw, *((target,) * 17),
                         round_trip_limits=RoundTripLimits(64*1024*1024, 64*1024*1024))
        self.assertEqual(caught.exception.category, 'limit-exceeded')
        self.assertIsNotNone(caught.exception.local_id)

    def test_limits_types_and_profile_ceiling(self):
        raw = dialogue()
        for kwargs in ({'round_trip_limits': object()}, {'limits': object()}, {'check_cancelled': 1}):
            with self.assertRaises(TypeError):
                self.prepare(raw, **kwargs)
        # A broader owner budget is intersected with this format's own ceiling.
        with self.assertRaises(codec.RpyInputError) as caught:
            self.prepare(raw, 'x' * (1024*1024+1), round_trip_limits=RoundTripLimits(64*1024*1024, 64*1024*1024))
        self.assertEqual(caught.exception.category, 'limit-exceeded')

    def test_every_checkpoint_cancels_without_returning_output(self):
        raw = dialogue('"[who] {b}Source{/b}"', '""')
        payload = codec.build_private_payload(raw)
        edits = self.edits(raw, '[who] {b}' + '雪' * 4200 + '{/b}')
        count = 0
        def count_calls():
            nonlocal count
            count += 1
        codec.prepare_round_trip_bytes(raw, payload, edits, check_cancelled=count_calls)
        self.assertGreater(count, 20)
        for stop in range(1, count + 1):
            observed = 0
            def cancel():
                nonlocal observed
                observed += 1
                if observed == stop:
                    raise Cancelled()
            with self.assertRaises(Cancelled):
                codec.prepare_round_trip_bytes(raw, payload, edits, check_cancelled=cancel)


if __name__ == '__main__':
    unittest.main()
