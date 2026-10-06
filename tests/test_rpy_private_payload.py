"""Private TL data is checked against source, never trusted as writer authority."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest
from unittest import mock

import parser_rpy_codec as codec
from parser_contracts import CodecIdentity
from tests.test_rpy_codec import dialogue
from tests.test_rpy_fixture_contract import MANIFEST_PATH, materialize


class Cancelled(Exception):
    pass


class RpyPrivatePayloadTests(unittest.TestCase):
    def setUp(self):
        self.raw = dialogue('"雪 [who!t] {b}Hello{/b} [who!t]"', '"原译文"')

    def document(self, raw=None):
        return json.loads(codec.build_private_payload(self.raw if raw is None else raw))

    def assert_rejected(self, payload, *, raw=None, category=None, **kwargs):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=True).encode()
        with self.assertRaises(codec.RpyInputError) as caught:
            codec.validate_private_payload(self.raw if raw is None else raw, payload, **kwargs)
        if category:
            self.assertEqual(caught.exception.category, category)
        self.assertNotIn('原译文', str(caught.exception))
        self.assertNotIn('Hello', str(caught.exception))

    def test_all_supported_fixtures_round_trip_format_facts(self):
        cases = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))['cases']
        for case in cases:
            if case['expectation']['classification'] != 'supported':
                continue
            with self.subTest(case=case['case_id']):
                raw = materialize(case)
                payload = codec.build_private_payload(raw)
                parsed = codec.validate_private_payload(raw, payload)
                self.assertEqual(parsed, codec.parse_tl(raw))
                self.assertEqual(payload, codec.build_private_payload(raw))

    def test_exact_header_identity_and_quote_inclusive_byte_spans(self):
        doc = self.document()
        self.assertEqual(codec.RPY_CODEC_IDENTITY, CodecIdentity('localcat.rpy', 'renpy-tl', '1'))
        self.assertEqual(set(doc), {'version', 'codec_identity', 'source_sha256', 'profile', 'language', 'slots'})
        self.assertEqual(doc['version'], 'rpy-roundtrip-v1')
        self.assertEqual(doc['codec_identity'], {'provider_id': 'localcat.rpy', 'codec_id': 'renpy-tl', 'codec_version': '1'})
        self.assertEqual(doc['source_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(doc['profile'], 'renpy-tl-v1')
        self.assertEqual(doc['language'], 'zh_Hans')
        row = doc['slots']['renpy-tl-v1:d:7:zh_Hans:unit']
        self.assertEqual(set(row), {'source_span', 'target_span', 'target_sha256', 'source_tokens'})
        self.assertEqual(self.raw[slice(*row['source_span'])].decode(), '"雪 [who!t] {b}Hello{/b} [who!t]"')
        self.assertEqual(self.raw[slice(*row['target_span'])].decode(), '"原译文"')
        self.assertEqual(row['target_sha256'], hashlib.sha256('原译文'.encode()).hexdigest())
        self.assertEqual(row['source_tokens'], ['[who!t]', '{b}', '{/b}', '[who!t]'])

    def test_payload_contains_no_edit_state_path_or_target_copy(self):
        payload = codec.build_private_payload(self.raw)
        self.assertNotIn('原译文'.encode(), payload)
        for key in ('target', 'confirmed', 'translation_state', 'path', 'source_ref'):
            self.assertNotIn(('"' + key + '":').encode(), payload)
        with self.assertRaises(TypeError):
            codec.build_private_payload(self.raw, target='changed', confirmed=True)

    def test_source_tokens_share_nested_bracket_quote_and_escape_recognition(self):
        raw = dialogue(r'''"[[literal {{literal [mapping['key]']!t] [items[index[0]]] {b}{i}x{/i}{/b} {custom=a}"''')
        row = next(iter(self.document(raw)['slots'].values()))
        self.assertEqual(row['source_tokens'], ["[mapping['key]']!t]", '[items[index[0]]]', '{b}', '{i}', '{/i}', '{/b}', '{custom=a}'])
        codec.validate_private_payload(raw, codec.build_private_payload(raw))

    def test_existing_bad_target_tokens_and_empty_target_remain_readable(self):
        for target in ('"[broken {b}{/i}"', '""'):
            raw = dialogue('"[who] {b}Source{/b}"', target)
            parsed = codec.validate_private_payload(raw, codec.build_private_payload(raw))
            self.assertEqual(parsed.records[0].target, target[1:-1])

    def test_semantic_target_digest_uses_decoded_unicode(self):
        raw = dialogue('"Source"', r'"A\nB\\C\"D"')
        row = next(iter(self.document(raw)['slots'].values()))
        self.assertEqual(row['target_sha256'], hashlib.sha256('A\nB\\C"D'.encode()).hexdigest())

    def test_validation_reparses_source_and_returns_only_immutable_facts(self):
        payload = codec.build_private_payload(self.raw)
        with mock.patch.object(codec, 'parse_tl', wraps=codec.parse_tl) as parse:
            result = codec.validate_private_payload(self.raw, payload)
        parse.assert_called_once()
        with self.assertRaises(FrozenInstanceError):
            result.records = ()
        for attr in ('terminal', 'receipt', 'writer', 'publish', 'commit'):
            self.assertFalse(hasattr(result, attr))

    def test_reordered_keys_whitespace_and_unicode_escapes_are_valid_json(self):
        doc = self.document()
        payload = json.dumps(dict(reversed(tuple(doc.items()))), ensure_ascii=True, indent=2).encode()
        self.assertEqual(codec.validate_private_payload(self.raw, payload), codec.parse_tl(self.raw))

    def test_header_and_exact_codec_identity_tampering_fails(self):
        for key, values in {'version': ('rpy-roundtrip-v2', 1, True),
                            'profile': ('other', None), 'language': ('en', []),
                            'source_sha256': ('0' * 64, 'A' * 64)}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    doc = self.document()
                    doc[key] = value
                    self.assert_rejected(doc)
        for key in ('provider_id', 'codec_id', 'codec_version'):
            doc = self.document()
            doc['codec_identity'][key] = 'foreign'
            self.assert_rejected(doc)

    def test_missing_extra_wrong_typed_keys_and_duplicate_slot_ids_fail(self):
        doc = self.document()
        for path in ((), ('codec_identity',), ('slots', next(iter(doc['slots'])))):
            for action in ('missing', 'extra', 'type'):
                mutated = self.document()
                obj = mutated
                for field in path:
                    obj = obj[field]
                if action == 'missing':
                    del obj[next(iter(obj))]
                elif action == 'extra':
                    obj['unexpected'] = 0
                else:
                    obj[next(iter(obj))] = []
                self.assert_rejected(mutated)
        for slots in ({}, {'forged': next(iter(doc['slots'].values()))}, []):
            mutated = self.document()
            mutated['slots'] = slots
            self.assert_rejected(mutated)
        row = json.dumps(next(iter(doc['slots'].values())))
        name = json.dumps(next(iter(doc['slots'])))
        prefix = json.dumps({k: v for k, v in doc.items() if k != 'slots'})[:-1]
        self.assert_rejected((prefix + ',"slots":{' + name + ':' + row + ',' + name + ':' + row + '}}').encode())

    def test_forged_overlapping_out_of_bounds_and_bool_spans_fail(self):
        for span in ([0, 1], [-1, 3], [2, len(self.raw) + 1], [4, 3], [3, 3],
                     [True, False], [1.0, 2.0], [1], [1, 2, 3], '1:2'):
            doc = self.document()
            next(iter(doc['slots'].values()))['target_span'] = span
            self.assert_rejected(doc)
        doc = self.document()
        row = next(iter(doc['slots'].values()))
        row['target_span'] = row['source_span']
        self.assert_rejected(doc)
        raw = self.raw + b'translate zh_Hans next:\n    # "Second"\n    "Two"\n'
        doc = self.document(raw)
        first, second = doc['slots'].values()
        second['source_span'] = first['source_span']
        self.assert_rejected(doc, raw=raw)

    def test_token_and_original_target_digest_tampering_fail(self):
        for field, values in {'source_tokens': ([], ['[new_expression()]'], ['[who!t]'], {}, [True]),
                              'target_sha256': ('0' * 64, 0)}.items():
            for value in values:
                doc = self.document()
                next(iter(doc['slots'].values()))[field] = value
                self.assert_rejected(doc)

    def test_stale_source_and_self_consistent_fake_source_digest_fail(self):
        doc = self.document()
        changed = self.raw.replace(b'[who!t]', b'[her!t]')
        self.assert_rejected(doc, raw=changed)
        doc['source_sha256'] = hashlib.sha256(changed).hexdigest()
        self.assert_rejected(doc, raw=changed)
        # A digest matching unsupported source does not authorize its offsets.
        changed = b'python:\n    forbidden()\n'
        doc['source_sha256'] = hashlib.sha256(changed).hexdigest()
        self.assert_rejected(doc, raw=changed)

    def test_duplicate_json_keys_at_every_object_level_fail(self):
        payload = codec.build_private_payload(self.raw)
        for key in (b'"version":', b'"codec_version":', b'"target_sha256":'):
            self.assert_rejected(payload.replace(key, key + b'"duplicate",' + key, 1))

    def test_strict_json_invalid_roots_constants_encoding_and_trailing_data(self):
        for payload in (b'', b'null', b'[]', b'"text"', b'{', b'{}{}', b'{} trailing',
                        b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
                        b'{"x":1.5}', b'{"x":1e10}', b'{"x":01}',
                        b'{"x":"\xff"}', b'\xef\xbb\xbf{}', b'{"x":"\x00"}'):
            with self.subTest(payload=payload):
                self.assert_rejected(payload)

    def test_deep_and_malformed_deep_json_rejected_before_materialization(self):
        for payload in (b'[' * 20000 + b'0' + b']' * 20000,
                        b'{"a":' * 20000 + b'0', b'[' * 20000):
            with mock.patch('parser_rpy_codec.json.loads', side_effect=AssertionError('must preflight depth')):
                self.assert_rejected(payload, category='limit-exceeded')

    def test_huge_integer_rejected_without_runtime_integer_conversion(self):
        for number in (b'9' * 50000, b'-' + b'9' * 50000):
            with mock.patch('parser_rpy_codec.json.loads', side_effect=AssertionError('must preflight integers')):
                self.assert_rejected(b'{"x":' + number + b'}')

    def test_flat_json_cannot_allocate_an_unrelated_large_object_graph(self):
        payload = b'[' + b'0,' * 100000 + b'0]'
        with mock.patch('parser_rpy_codec.json.loads', side_effect=AssertionError('must preflight item count')):
            self.assert_rejected(payload, category='limit-exceeded')

    def test_every_truncated_payload_prefix_is_rejected(self):
        payload = codec.build_private_payload(self.raw)
        for end in range(len(payload)):
            with self.subTest(end=end):
                self.assert_rejected(payload[:end])

    def test_late_cancellation_while_scanning_large_json_prevents_decode(self):
        payload = codec.build_private_payload(self.raw) + b' ' * 20000
        # Count source-recognition checkpoints, then stop at the second JSON
        # scan checkpoint, after the private preflight has begun.
        source_checker = mock.Mock()
        codec.parse_tl(self.raw, check_cancelled=source_checker)
        checker = mock.Mock(side_effect=[None] * (source_checker.call_count + 2) + [Cancelled()])
        with mock.patch('parser_rpy_codec.json.loads', side_effect=AssertionError('cancel before decode')):
            with self.assertRaises(Cancelled):
                codec.validate_private_payload(self.raw, payload, check_cancelled=checker)

    def test_private_bounds_exact_limit_and_smaller_caller_budget(self):
        payload = codec.build_private_payload(self.raw)
        self.assertEqual(codec.build_private_payload(self.raw, max_private_bytes=len(payload)), payload)
        codec.validate_private_payload(self.raw, payload, max_private_bytes=len(payload))
        for action in (lambda: codec.build_private_payload(self.raw, max_private_bytes=len(payload) - 1),
                       lambda: codec.validate_private_payload(self.raw, payload, max_private_bytes=len(payload) - 1),
                       lambda: codec.validate_private_payload(self.raw, b' ' * (32 * 1024 * 1024 + 1))):
            with self.assertRaises(codec.RpyInputError) as caught:
                action()
            self.assertEqual(caught.exception.category, 'limit-exceeded')

    def test_smaller_source_limits_apply_to_build_and_validation(self):
        payload = codec.build_private_payload(self.raw)
        raw = self.raw + b'translate zh_Hans next:\n    # "Second"\n    "Two"\n'
        for source, limits in ((self.raw, codec.RpyLexicalLimits(max_input_bytes=len(self.raw)-1)),
                               (self.raw, codec.RpyLexicalLimits(max_string_bytes=2)),
                               (raw, codec.RpyLexicalLimits(max_slots=1))):
            for action in (lambda: codec.build_private_payload(source, limits=limits),
                           lambda: codec.validate_private_payload(source, payload, limits=limits)):
                with self.assertRaises(codec.RpyInputError) as caught:
                    action()
                self.assertEqual(caught.exception.category, 'limit-exceeded')

    def test_argument_types_and_limit_ceiling_are_exact(self):
        payload = codec.build_private_payload(self.raw)
        for value in (True, 1.0, '100', 0, -1, 32 * 1024 * 1024 + 1):
            for call in (lambda: codec.build_private_payload(self.raw, max_private_bytes=value),
                         lambda: codec.validate_private_payload(self.raw, payload, max_private_bytes=value)):
                with self.assertRaises((TypeError, ValueError)):
                    call()
        for raw, opaque in ((bytearray(self.raw), payload), (self.raw, bytearray(payload)),
                            (self.raw, payload.decode()), (self.raw, None)):
            with self.assertRaises(TypeError):
                codec.validate_private_payload(raw, opaque)
        with self.assertRaises(TypeError):
            codec.build_private_payload(self.raw, check_cancelled=True)

    def test_cancellation_at_every_checkpoint_never_returns_partial_facts(self):
        payload = codec.build_private_payload(self.raw)
        for operation in (lambda checker: codec.build_private_payload(self.raw, check_cancelled=checker),
                          lambda checker: codec.validate_private_payload(self.raw, payload, check_cancelled=checker)):
            observer = mock.Mock()
            operation(observer)
            self.assertGreater(observer.call_count, 3)
            for count in range(observer.call_count):
                with self.subTest(checkpoint=count):
                    checker = mock.Mock(side_effect=[None] * count + [Cancelled()])
                    with self.assertRaises(Cancelled):
                        operation(checker)

    def test_interpolation_is_never_evaluated(self):
        raw = dialogue('"[dangerous()]"')
        with mock.patch('builtins.eval', side_effect=AssertionError('no expression execution')) as evaluate:
            codec.validate_private_payload(raw, codec.build_private_payload(raw))
        evaluate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
