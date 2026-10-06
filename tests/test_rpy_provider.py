from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import parser_contracts as contracts
import parser_composition as composition
import parser_rpy_codec as rpy
from parser_source import CancellationToken


FIXTURES = Path(__file__).parent / 'fixtures' / 'rpy' / 'payloads'


class RpyProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'mixed.rpy'
        self.raw = (FIXTURES / 'mixed_dialogue_strings.rpy').read_bytes()
        self.path.write_bytes(self.raw)
        self.provider = rpy.RpyProvider()
        self.surface = composition.create_parser_application_surface(providers=(
            composition.ProviderBinding('localcat.rpy', self.provider, True, ('1',)),
        ))

    def opened(self, cancellation=None):
        opened = self.surface.open_input(
            contracts.SourceReference(str(self.root), str(self.path), 'synthetic'),
            contracts.SelectionRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT,
                                       contracts.FormatId('renpy-tl-v1')),
            contracts.ReadRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT,
                                  contracts.FormatId('renpy-tl-v1')),
            cancellation=cancellation,
        )
        self.addCleanup(opened.close)
        return opened

    def token(self, opened, payload):
        return contracts.RoundTripTokenEnvelope(
            rpy.RPY_CODEC_IDENTITY, opened.source_identity.content_sha256,
            hashlib.sha256(payload).hexdigest(), payload,
        )

    def test_descriptor_and_all_factories_share_registered_identity(self):
        descriptors = self.provider.descriptors()
        self.assertEqual(len(descriptors), 1)
        descriptor = descriptors[0]
        self.assertIs(self.provider.descriptors()[0], descriptor)
        self.assertEqual(descriptor.identity, rpy.RPY_CODEC_IDENTITY)
        self.assertEqual(descriptor.input_consumption_policy,
                         contracts.InputConsumptionPolicy.SEALED_BYTES_EOF)
        self.assertFalse(descriptor.capabilities.canonical_write)
        self.assertFalse(descriptor.capabilities.streaming_input)
        self.assertTrue(descriptor.capabilities.source_round_trip_write)
        for factory in (descriptor.reader_factory, descriptor.source_state_factory,
                        descriptor.round_trip_serializer_factory):
            self.assertIs(factory().descriptor, descriptor)
        self.assertEqual(descriptor.limit_profile.max_input_bytes, 16 * 1024**2)
        self.assertEqual(descriptor.limit_profile.max_records, 100000)
        self.assertEqual(descriptor.round_trip_limits.max_output_bytes, 32 * 1024**2)
        self.assertEqual(descriptor.source_state_limits.max_opaque_payload_bytes, 32 * 1024**2)

    def test_real_surface_mixed_handoff_and_roundtrip_preparation(self):
        opened = self.opened()
        handoff = self.surface.materialize_handoff(opened)
        records = handoff.materialized.records
        self.assertEqual(handoff.source_bytes, self.raw)
        self.assertEqual(len(records), 6)
        self.assertEqual(handoff.materialized.header.target_locale, 'zh_Hans')
        self.assertEqual([r.speaker.value for r in records], ['guide', '', '', '', '', 'guide'])
        self.assertTrue(all(r.translation_state is contracts.TranslationState.UNCONFIRMED for r in records))
        state = handoff.private_state
        self.assertEqual(state.profile_version, 'rpy-roundtrip-v1')
        self.assertEqual(state.payload, rpy.build_private_payload(self.raw))
        edits = tuple(contracts.RoundTripSegmentEdit(r.local_id, r.target) for r in records)
        prepared = self.surface.prepare_round_trip(opened, self.token(opened, state.payload), edits)
        self.addCleanup(prepared.close)
        self.assertEqual(prepared.output_fingerprint, hashlib.sha256(self.raw).hexdigest())
        edited = (replace(edits[0], target='A changed translation'), *edits[1:])
        changed = self.surface.prepare_round_trip(opened, self.token(opened, state.payload), edited)
        self.addCleanup(changed.close)
        expected = self.raw.replace('灯笼亮着。'.encode(), b'A changed translation')
        self.assertEqual(changed.output_fingerprint, hashlib.sha256(expected).hexdigest())

    def test_invalid_source_preserves_fatal_position_without_leaking_exception(self):
        self.path.write_bytes((FIXTURES / 'mixed_languages.rpy').read_bytes())
        report = self.opened().validate()
        self.assertIsNone(report.terminal)
        issue = next(i for i in report.issues if i.severity is contracts.IssueSeverity.FATAL)
        self.assertEqual(issue.code, 'PARSER.RPY.MIXED_LANGUAGE')
        self.assertGreater(issue.line_number, 1)
        self.assertGreater(issue.byte_offset, 0)

    def test_invalid_protection_token_preserves_roundtrip_position(self):
        opened = self.opened()
        handoff = self.surface.materialize_handoff(opened)
        records = handoff.materialized.records
        edits = tuple(contracts.RoundTripSegmentEdit(r.local_id,
                      'invalid [injected]' if n == 0 else r.target) for n, r in enumerate(records))
        with self.assertRaises(composition.RoundTripPreparationError) as rejected:
            self.surface.prepare_round_trip(opened, self.token(opened, handoff.private_state.payload), edits)
        self.assertEqual(rejected.exception.code, 'PARSER.RPY.PLACEHOLDER_MISMATCH')
        issue = rejected.exception.diagnostics[0]
        self.assertEqual(issue.line_number, 5)
        self.assertEqual(issue.record_number, 1)
        self.assertGreater(issue.byte_offset, 0)
        self.assertNotIn('injected', issue.safe_summary)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_smaller_owner_limits_reject_before_handoff_or_prepare(self):
        base = self.provider.descriptors()[0]
        for dimension in ('records', 'private', 'output'):
            with self.subTest(dimension=dimension):
                if dimension == 'records':
                    descriptor = replace(base, limit_profile=replace(
                        base.limit_profile, max_records=1, max_materialized_records=1))
                elif dimension == 'private':
                    descriptor = replace(base, source_state_limits=contracts.SourceStateLimits(1))
                else:
                    descriptor = replace(base, round_trip_limits=contracts.RoundTripLimits(1, 32 * 1024**2))
                with mock.patch.object(rpy, 'RPY_DESCRIPTOR', descriptor):
                    self.surface = composition.create_parser_application_surface(providers=(
                        composition.ProviderBinding('localcat.rpy', rpy.RpyProvider(), True, ('1',)),
                    ))
                    opened = self.opened()
                    if dimension != 'output':
                        with self.assertRaises(contracts.ContractViolation) as rejected:
                            self.surface.materialize_handoff(opened)
                    else:
                        handoff = self.surface.materialize_handoff(opened)
                        edits = tuple(contracts.RoundTripSegmentEdit(r.local_id, r.target)
                                      for r in handoff.materialized.records)
                        with self.assertRaises(composition.RoundTripPreparationError) as rejected:
                            self.surface.prepare_round_trip(opened, self.token(opened, handoff.private_state.payload), edits)
                    self.assertEqual(rejected.exception.code, 'PARSER.RPY.LIMIT_EXCEEDED')

    def test_private_bytes_are_revalidated_on_the_real_surface(self):
        opened = self.opened()
        handoff = self.surface.materialize_handoff(opened)
        edits = tuple(contracts.RoundTripSegmentEdit(r.local_id, r.target)
                      for r in handoff.materialized.records)
        payload = handoff.private_state.payload.replace(b'rpy-roundtrip-v1', b'rpy-roundtrip-v2')
        with self.assertRaises(composition.RoundTripPreparationError) as rejected:
            self.surface.prepare_round_trip(opened, self.token(opened, payload), edits)
        self.assertEqual(rejected.exception.code, 'PARSER.RPY.PRIVATE_STALE')

    def test_cancel_during_lexical_checkpoint_cannot_issue_handoff(self):
        cancellation = CancellationToken()
        opened = self.opened(cancellation)
        parse = rpy.parse_tl
        def cancel(*args, **kwargs):
            cancellation.cancel()
            return parse(*args, **kwargs)
        with mock.patch.object(rpy, 'parse_tl', side_effect=cancel):
            with self.assertRaises(contracts.ContractViolation):
                self.surface.materialize_handoff(opened)


if __name__ == '__main__':
    unittest.main()
